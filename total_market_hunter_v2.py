"""
Alpha Engine - conservative total-market pre-screener.

Design goals:
1. Use one SEC request for ticker/CIK/exchange mapping.
2. Use Yahoo's server-side screener in pages before requesting per-ticker data.
3. Fetch detailed Yahoo data sequentially with a global request interval.
4. Stop on rate limits instead of creating a retry storm.
5. Cache successful responses and resume from an append-only checkpoint.
6. Never overwrite the last complete qualified_universe.csv with a partial run.

This is a research pre-screen, not a trading signal.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import math
import os
import queue
import random
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests
import yfinance as yf

from mode_c_routing import route_industry_model
from mode_c_industry_models import initial_screen_industry
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ==============================================================================
# Default screening policy
# ==============================================================================
MIN_MCAP_B = 5.0
STANDARD_GROSS_MARGIN = 0.25
MIN_GROSS_MARGIN_FLOOR = 0.0
MIN_INDUSTRY_SAMPLE = 5
DEBT_EBITDA_WARNING = 4.0
MAX_DEBT_EBITDA = 5.0
MAX_PPE_REV_RATIO = 1.0
MIN_INSTITUTIONAL_OWN = 0.40

# Institutional ownership and PP&E are optional by default. They are useful
# research fields, but neither is worth multiplying network requests or
# excluding otherwise valid businesses during the very first screening stage.
DEFAULT_REQUIRE_INSTITUTIONAL_OWNERSHIP = False
DEFAULT_ENABLE_PPE_FILTER = False
HUNTER_POLICY_VERSION = "2026-09-industry-models-v8"

SUPPORTED_EQUITY_EXCHANGES = {"NMS", "NYQ", "NGM", "NCM", "ASE", "PCX"}
SUPPORTED_SEC_EXCHANGES = {"NASDAQ", "NYSE", "NYSE AMERICAN"}
COMMON_SECURITY_CLASSES = {
    "COMMON_OR_EQUIVALENT",
    "COMMON_ADS_INFERRED",
}

STRATEGY_EXCLUDED_SECTORS: set[str] = set()

BLOCKED_INDUSTRY_KEYWORDS: set[str] = set()

# Keys are symbols to remove; values are the preferred share class.
DUAL_CLASS_KEEP = {
    "FOX": "FOXA",
    "GOOG": "GOOGL",
    "NWS": "NWSA",
    "UA": "UAA",
}

WINDOWS_RESERVED_BASENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}

SEC_TICKER_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
NASDAQ_LISTED_URL = (
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
)
OTHER_LISTED_URL = (
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
)
SCREENER_PAGE_SIZE = 250
SCREENER_CACHE_HOURS = 24
SEC_CACHE_DAYS = 7
SEC_SECURITY_CLASS_CACHE_DAYS = 30
SECURITY_DIRECTORY_CACHE_DAYS = 7
INFO_CACHE_DAYS = 7
RESULT_CACHE_DAYS = 7

# Deliberately slow. One detailed request every 2.5 seconds is about 24/minute.
DEFAULT_YAHOO_INTERVAL_SECONDS = 2.5
DEFAULT_SCREENER_INTERVAL_SECONDS = 4.0
DEFAULT_MAX_TRANSIENT_FAILURES = 3
DEFAULT_REQUEST_TIMEOUT_SECONDS = 75.0

RATE_LIMIT_MARKERS = (
    "429",
    "too many requests",
    "rate limit",
    "rate-limit",
    "ratelimited",
    "yf ratelimit",
    "crumb",
)
TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "connection",
    "temporarily unavailable",
    "remote end closed",
    "502",
    "503",
    "504",
)


class RateLimitStop(RuntimeError):
    """Raised when the safest action is to stop and resume later."""


class TemporaryDataError(RuntimeError):
    """Raised for retryable connectivity failures that are not rate limits."""


class RequestTimeoutStop(RuntimeError):
    """Raised when one Yahoo request hangs and this run must stop safely."""


@dataclass(frozen=True)
class HunterConfig:
    min_mcap_b: float = MIN_MCAP_B
    standard_gross_margin: float = STANDARD_GROSS_MARGIN
    min_gross_margin_floor: float = MIN_GROSS_MARGIN_FLOOR
    min_industry_sample: int = MIN_INDUSTRY_SAMPLE
    debt_ebitda_warning: float = DEBT_EBITDA_WARNING
    max_debt_ebitda: float = MAX_DEBT_EBITDA
    require_institutional_ownership: bool = DEFAULT_REQUIRE_INSTITUTIONAL_OWNERSHIP
    min_institutional_own: float = MIN_INSTITUTIONAL_OWN
    enable_ppe_filter: bool = DEFAULT_ENABLE_PPE_FILTER
    max_ppe_rev_ratio: float = MAX_PPE_REV_RATIO

    def signature(self) -> str:
        payload = {**asdict(self), "policy_version": HUNTER_POLICY_VERSION}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class RequestPacer:
    def __init__(self, interval_seconds: float):
        self.interval_seconds = max(0.0, float(interval_seconds))
        self.last_request_at = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self.last_request_at
        remaining = self.interval_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining + random.uniform(0.05, 0.35))
        self.last_request_at = time.monotonic()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def is_fresh(timestamp: Any, max_age_seconds: float) -> bool:
    parsed = parse_iso_datetime(timestamp)
    if parsed is None:
        return False
    age = (datetime.now(timezone.utc) - parsed).total_seconds()
    return 0 <= age <= max_age_seconds


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return str(value)


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(json_safe(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp_path, path)


def atomic_write_csv(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    pd.DataFrame(rows).to_csv(
        temp_path,
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    os.replace(temp_path, path)


def append_checkpoint(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(json_safe(result), ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_checkpoint(path: Path, signature: str) -> Dict[str, dict]:
    latest: Dict[str, dict] = {}
    if not path.exists():
        return latest
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ticker = str(row.get("Ticker") or "").upper()
            if ticker and row.get("ConfigSignature") == signature:
                latest[ticker] = row
    return latest


def completed_result_is_fresh(result: dict) -> bool:
    status = str(result.get("Status") or "")
    if status.startswith("Retry:"):
        return False
    return is_fresh(
        result.get("EvaluatedAt"),
        RESULT_CACHE_DAYS * 24 * 60 * 60,
    )


def create_sec_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=2.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def validate_sec_contact_email(email: str) -> str:
    email = str(email or "").strip()
    if not email or "@" not in email:
        raise ValueError("請提供真正的聯絡信箱，例如 name@gmail.com。")
    try:
        email.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(
            "聯絡信箱不能包含中文；請把「你的信箱@gmail.com」換成真正的英文信箱。"
        ) from exc
    if any(char.isspace() for char in email):
        raise ValueError("聯絡信箱不能包含空格。")
    return email


def parse_sec_exchange_payload(payload: dict) -> Dict[str, dict]:
    fields = payload.get("fields") or []
    rows = payload.get("data") or []
    if not fields or not rows:
        raise ValueError("SEC ticker/exchange payload is empty")
    output: Dict[str, dict] = {}
    for values in rows:
        row = dict(zip(fields, values))
        ticker = str(row.get("ticker") or "").upper().strip()
        exchange = str(row.get("exchange") or "").upper().strip()
        cik = str(row.get("cik") or "").replace(".0", "").zfill(10)
        if not ticker or not cik.isdigit():
            continue
        output[ticker] = {
            "Ticker": ticker,
            "CIK": cik,
            "Name": str(row.get("name") or "").strip(),
            "SECExchange": exchange,
        }
    return output


def get_sec_ticker_map(email: str, cache_dir: Path, fresh: bool = False) -> Dict[str, dict]:
    email = validate_sec_contact_email(email)
    cache_path = cache_dir / "sec_tickers_exchange.json"
    cached = load_json(cache_path)
    if (
        not fresh
        and isinstance(cached, dict)
        and is_fresh(cached.get("fetched_at"), SEC_CACHE_DAYS * 24 * 60 * 60)
        and isinstance(cached.get("payload"), dict)
    ):
        return parse_sec_exchange_payload(cached["payload"])

    headers = {
        "User-Agent": f"AlphaEngineResearch {email}",
        "Accept-Encoding": "gzip, deflate",
        "Host": "www.sec.gov",
    }
    try:
        response = create_sec_session().get(
            SEC_TICKER_URL,
            headers=headers,
            timeout=(10, 30),
        )
        response.raise_for_status()
        payload = response.json()
        parsed = parse_sec_exchange_payload(payload)
        atomic_write_json(
            cache_path,
            {"fetched_at": utc_now_iso(), "payload": payload},
        )
        print(f"[SEC] 取得 {len(parsed):,} 筆 ticker / CIK / exchange 對照。")
        return parsed
    except Exception as exc:
        if isinstance(cached, dict) and isinstance(cached.get("payload"), dict):
            print(f"[SEC] 連線失敗，改用舊快取：{exc}")
            return parse_sec_exchange_payload(cached["payload"])
        raise RuntimeError(f"SEC ticker mapping unavailable: {exc}") from exc


def normalize_market_symbol(value: Any) -> str:
    symbol = str(value or "").upper().strip().replace(".", "-")
    return re.sub(r"\s+", "", symbol)


def classify_security_name(security_name: Any, *, is_etf: bool = False) -> str:
    """Classify the listed instrument without inferring from ticker punctuation."""
    if is_etf:
        return "EXCLUDED_NON_COMMON"
    name = re.sub(r"\s+", " ", html.unescape(str(security_name or ""))).strip()
    if not name:
        return "UNKNOWN"
    lowered = name.lower()
    if re.search(
        r"\b(preferred|preference|warrants?|rights?|notes?|bonds?|debentures?|"
        r"etf|exchange-traded fund)\b",
        lowered,
    ):
        return "EXCLUDED_NON_COMMON"
    if re.search(
        r"\b(common stock|common shares?|ordinary shares?|capital stock|"
        r"shares? of beneficial interest|common units?|registered shares?|"
        r"registry shares?|series\s+[a-z0-9]+\s+shares?)\b",
        lowered,
    ):
        return "COMMON_OR_EQUIVALENT"
    if re.search(r"\bclass\s+[a-z0-9]+\b", lowered):
        return "COMMON_OR_EQUIVALENT"
    if re.search(r"\b(units?|certificates?)\b", lowered):
        return "EXCLUDED_NON_COMMON"
    if re.search(
        r"\b(american depositary|american depository|depositary receipts?|"
        r"depository receipts?|sponsored adr|ads|adrs?)\b",
        lowered,
    ):
        return "AMBIGUOUS_ADS"
    return "COMMON_OR_EQUIVALENT"


def parse_nasdaq_symbol_directory(text: str, source: str) -> Dict[str, dict]:
    reader = csv.DictReader(io.StringIO(str(text or "")), delimiter="|")
    output: Dict[str, dict] = {}
    for row in reader:
        raw_symbol = row.get("Symbol") or row.get("ACT Symbol") or ""
        ticker = normalize_market_symbol(raw_symbol)
        if not is_standard_common_stock_symbol(ticker):
            continue
        security_name = str(row.get("Security Name") or "").strip()
        is_etf = str(row.get("ETF") or "").upper() == "Y"
        is_test = str(row.get("Test Issue") or "").upper() == "Y"
        security_class = classify_security_name(
            security_name,
            is_etf=is_etf or is_test,
        )
        output[ticker] = {
            "SecurityName": security_name,
            "SecurityClass": security_class,
            "SecurityClassConfidence": (
                "NEEDS_SEC" if security_class == "AMBIGUOUS_ADS" else "HIGH"
            ),
            "IsETF": is_etf,
            "SecurityClassEvidenceSource": source,
        }
    return output


def reclassify_security_directory(rows: Dict[str, dict]) -> Dict[str, dict]:
    """Rebuild derived class labels whenever screening policy changes."""
    output: Dict[str, dict] = {}
    for ticker, raw in rows.items():
        row = dict(raw or {})
        security_name = str(row.get("SecurityName") or "")
        is_etf = bool(row.get("IsETF")) or bool(
            re.search(r"\betf\b", security_name, flags=re.IGNORECASE)
        )
        security_class = classify_security_name(security_name, is_etf=is_etf)
        output[normalize_market_symbol(ticker)] = {
            **row,
            "SecurityClass": security_class,
            "SecurityClassConfidence": (
                "NEEDS_SEC" if security_class == "AMBIGUOUS_ADS" else "HIGH"
            ),
            "IsETF": is_etf,
        }
    return output


def get_nasdaq_security_directory(
    email: str,
    cache_dir: Path,
    fresh: bool = False,
) -> Dict[str, dict]:
    cache_path = cache_dir / "nasdaq_security_directory.json"
    cached = load_json(cache_path)
    cached_rows = cached.get("securities") if isinstance(cached, dict) else None
    if (
        not fresh
        and isinstance(cached_rows, dict)
        and is_fresh(
            cached.get("fetched_at"),
            SECURITY_DIRECTORY_CACHE_DAYS * 24 * 60 * 60,
        )
    ):
        return reclassify_security_directory(cached_rows)

    headers = {
        "User-Agent": f"AlphaEngineResearch {validate_sec_contact_email(email)}",
        "Accept-Encoding": "gzip, deflate",
    }
    session = create_sec_session()
    try:
        responses = []
        for url in (NASDAQ_LISTED_URL, OTHER_LISTED_URL):
            response = session.get(url, headers=headers, timeout=(10, 60))
            response.raise_for_status()
            responses.append(response.text)
        securities = {
            **parse_nasdaq_symbol_directory(responses[0], "NASDAQ Trader nasdaqlisted"),
            **parse_nasdaq_symbol_directory(responses[1], "NASDAQ Trader otherlisted"),
        }
        if len(securities) < 1_000:
            raise RuntimeError(
                f"NASDAQ Trader security directory is unexpectedly small: {len(securities)}"
            )
        atomic_write_json(
            cache_path,
            {"fetched_at": utc_now_iso(), "securities": securities},
        )
        print(f"[Security directory] loaded {len(securities):,} listed instruments.")
        return reclassify_security_directory(securities)
    except Exception as exc:
        if isinstance(cached_rows, dict) and len(cached_rows) >= 1_000:
            print(f"[Security directory] refresh failed; using cached data: {exc}")
            return reclassify_security_directory(cached_rows)
        raise RuntimeError(f"NASDAQ Trader security directory unavailable: {exc}") from exc


class InlineXBRLCoverParser(HTMLParser):
    TARGETS = {
        "dei:tradingsymbol": "symbol",
        "dei:security12btitle": "title",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: Dict[str, Dict[str, List[str]]] = {
            "symbol": {},
            "title": {},
        }
        self._active: Optional[dict] = None
        self._nested_depth = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if self._active is not None:
            self._nested_depth += 1
            return
        if tag.lower() != "ix:nonnumeric":
            return
        attributes = {str(key).lower(): value for key, value in attrs}
        kind = self.TARGETS.get(str(attributes.get("name") or "").lower())
        context = str(attributes.get("contextref") or "").strip()
        if kind and context:
            self._active = {"kind": kind, "context": context, "text": []}
            self._nested_depth = 0

    def handle_data(self, data: str) -> None:
        if self._active is not None:
            self._active["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._active is None:
            return
        if self._nested_depth > 0:
            self._nested_depth -= 1
            return
        if tag.lower() != "ix:nonnumeric":
            return
        value = re.sub(r"\s+", " ", "".join(self._active["text"])).strip()
        if value:
            self.values[self._active["kind"]].setdefault(
                self._active["context"],
                [],
            ).append(value)
        self._active = None


def extract_registered_security_titles(document: str) -> Dict[str, str]:
    parser = InlineXBRLCoverParser()
    parser.feed(str(document or ""))
    output: Dict[str, str] = {}
    shared_contexts = set(parser.values["symbol"]) & set(parser.values["title"])
    for context in shared_contexts:
        symbols = parser.values["symbol"][context]
        titles = parser.values["title"][context]
        if len(titles) == 1:
            titles = titles * len(symbols)
        for symbol, title in zip(symbols, titles):
            ticker = normalize_market_symbol(symbol)
            if is_standard_common_stock_symbol(ticker):
                output[ticker] = title
    return output


def latest_annual_filing_record(submissions: dict) -> Optional[dict]:
    recent = ((submissions.get("filings") or {}).get("recent") or {})
    forms = recent.get("form") or []
    candidates = []
    for index, form in enumerate(forms):
        if str(form).upper() not in {"10-K", "20-F", "40-F"}:
            continue
        try:
            accession = str((recent.get("accessionNumber") or [])[index])
            primary_document = str((recent.get("primaryDocument") or [])[index])
        except IndexError:
            continue
        accepted_values = recent.get("acceptanceDateTime") or []
        filed_values = recent.get("filingDate") or []
        accepted_at = (
            str(accepted_values[index])
            if index < len(accepted_values) and accepted_values[index]
            else str(filed_values[index])
            if index < len(filed_values)
            else ""
        )
        parsed = parse_iso_datetime(accepted_at)
        if not accession or not primary_document or parsed is None:
            continue
        if parsed > datetime.now(timezone.utc):
            continue
        candidates.append(
            {
                "form": str(form).upper(),
                "accession": accession,
                "primary_document": primary_document,
                "accepted_at": parsed.isoformat(),
            }
        )
    return max(candidates, key=lambda row: row["accepted_at"], default=None)


def get_sec_registered_security_titles(
    cik: str,
    email: str,
    cache_dir: Path,
    pacer: RequestPacer,
    session: requests.Session,
) -> dict:
    cik = str(cik).replace(".0", "").zfill(10)
    cache_path = cache_dir / "sec_security_classes" / f"CIK{cik}.json"
    cached = load_json(cache_path)
    if (
        isinstance(cached, dict)
        and isinstance(cached.get("titles"), dict)
        and is_fresh(
            cached.get("fetched_at"),
            SEC_SECURITY_CLASS_CACHE_DAYS * 24 * 60 * 60,
        )
    ):
        return cached

    headers = {
        "User-Agent": f"AlphaEngineResearch {validate_sec_contact_email(email)}",
        "Accept-Encoding": "gzip, deflate",
    }
    try:
        pacer.wait()
        submissions_response = session.get(
            SEC_SUBMISSIONS_URL.format(cik=cik),
            headers=headers,
            timeout=(10, 45),
        )
        submissions_response.raise_for_status()
        filing = latest_annual_filing_record(submissions_response.json())
        if filing is None:
            raise RuntimeError("no current 10-K/20-F/40-F filing record")
        archive_url = (
            "https://www.sec.gov/Archives/edgar/data/"
            f"{int(cik)}/{filing['accession'].replace('-', '')}/"
            f"{filing['primary_document']}"
        )
        pacer.wait()
        filing_response = session.get(
            archive_url,
            headers=headers,
            timeout=(10, 60),
        )
        filing_response.raise_for_status()
        titles = extract_registered_security_titles(filing_response.text)
        if not titles:
            raise RuntimeError("annual filing cover has no paired security-title facts")
        payload = {
            "fetched_at": utc_now_iso(),
            "accepted_at": filing["accepted_at"],
            "form": filing["form"],
            "accession": filing["accession"],
            "source_url": archive_url,
            "titles": titles,
        }
        atomic_write_json(cache_path, payload)
        return payload
    except Exception as exc:
        if isinstance(cached, dict) and isinstance(cached.get("titles"), dict):
            return {**cached, "fallback_reason": str(exc)}
        return {"titles": {}, "error": str(exc)}


def _normalise_screener_quote(quote: dict) -> dict:
    ticker = str(quote.get("symbol") or quote.get("ticker") or "").upper().strip()
    return {
        **quote,
        "symbol": ticker,
        "quoteType": str(quote.get("quoteType") or "").upper(),
        "exchange": str(
            quote.get("exchange")
            or quote.get("fullExchangeName")
            or ""
        ).upper(),
    }


def _build_yahoo_query(config: HunterConfig):
    equity_query = getattr(yf, "EquityQuery", None)
    if equity_query is None or not hasattr(yf, "screen"):
        raise RuntimeError(
            "目前的 yfinance 不支援 screen/EquityQuery；"
            "請安裝專案 ModeC_requirements.txt 指定版本。"
        )
    return equity_query(
        "and",
        [
            equity_query("eq", ["region", "us"]),
            equity_query(
                "gte",
                ["intradaymarketcap", int(config.min_mcap_b * 1_000_000_000)],
            ),
            equity_query(
                "is-in",
                ["exchange", *sorted(SUPPORTED_EQUITY_EXCHANGES)],
            ),
        ],
    )


def get_yahoo_screener_candidates(
    config: HunterConfig,
    cache_dir: Path,
    pacer: RequestPacer,
    fresh: bool = False,
    max_pages: int = 20,
) -> List[dict]:
    cache_path = cache_dir / f"yahoo_screen_{config.signature()}.json"
    cached = load_json(cache_path)
    if (
        not fresh
        and isinstance(cached, dict)
        and is_fresh(cached.get("fetched_at"), SCREENER_CACHE_HOURS * 60 * 60)
        and isinstance(cached.get("quotes"), list)
    ):
        print(f"[Yahoo Screener] 使用快取的 {len(cached['quotes']):,} 檔候選。")
        return [_normalise_screener_quote(q) for q in cached["quotes"]]

    query = _build_yahoo_query(config)
    all_quotes: List[dict] = []
    seen = set()
    offset = 0
    expected_total: Optional[int] = None

    for page_number in range(1, max_pages + 1):
        pacer.wait()
        try:
            response = yf.screen(
                query,
                offset=offset,
                size=SCREENER_PAGE_SIZE,
                sortField="intradaymarketcap",
                sortAsc=False,
            )
        except Exception as exc:
            message = str(exc).lower()
            if any(marker in message for marker in RATE_LIMIT_MARKERS):
                if isinstance(cached, dict) and isinstance(cached.get("quotes"), list):
                    print("[Yahoo Screener] 被限流，改用舊快取。")
                    return [
                        _normalise_screener_quote(q)
                        for q in cached["quotes"]
                    ]
                raise RateLimitStop(
                    "Yahoo Screener rate limited. Stop now and resume later."
                ) from exc
            raise TemporaryDataError(f"Yahoo Screener failed: {exc}") from exc

        quotes = response.get("quotes") if isinstance(response, dict) else None
        if not isinstance(quotes, list):
            raise TemporaryDataError("Yahoo Screener returned no quote list")
        if expected_total is None:
            try:
                expected_total = int(response.get("total"))
            except Exception:
                expected_total = None

        new_count = 0
        for raw_quote in quotes:
            quote = _normalise_screener_quote(raw_quote)
            ticker = quote["symbol"]
            if ticker and ticker not in seen:
                seen.add(ticker)
                all_quotes.append(quote)
                new_count += 1

        print(
            f"[Yahoo Screener] page={page_number} "
            f"received={len(quotes)} new={new_count} total={len(all_quotes)}"
        )
        if not quotes or len(quotes) < SCREENER_PAGE_SIZE:
            break
        offset += len(quotes)
        if expected_total is not None and offset >= expected_total:
            break

    if not all_quotes:
        raise TemporaryDataError("Yahoo Screener returned an empty candidate set")

    atomic_write_json(
        cache_path,
        {"fetched_at": utc_now_iso(), "quotes": all_quotes},
    )
    return all_quotes


def is_standard_common_stock_symbol(ticker: str) -> bool:
    # Admit a single-letter common share class (for example BRK-B), while
    # continuing to reject preferred-style multi-letter suffixes.
    return bool(re.fullmatch(r"[A-Z]{1,6}(?:[-.][A-Z])?", ticker))


def prefilter_candidates(
    quotes: List[dict],
    sec_map: Dict[str, dict],
    security_directory: Optional[Dict[str, dict]] = None,
    scan_limit: int = 0,
) -> List[dict]:
    security_directory = security_directory or {}
    candidates: List[dict] = []
    for quote in quotes:
        ticker = str(quote.get("symbol") or "").upper()
        sec_row = sec_map.get(ticker)
        if not sec_row or not is_standard_common_stock_symbol(ticker):
            continue
        if sec_row.get("SECExchange") not in SUPPORTED_SEC_EXCHANGES:
            continue
        if ticker in DUAL_CLASS_KEEP and DUAL_CLASS_KEEP[ticker] != ticker:
            continue

        quote_type = str(quote.get("quoteType") or "").upper()
        if quote_type and quote_type != "EQUITY":
            continue
        sector = str(quote.get("sector") or "").strip()
        if sector in STRATEGY_EXCLUDED_SECTORS:
            continue

        security = security_directory.get(ticker) or {}
        candidates.append(
            {
                **sec_row,
                "ScreenerQuote": quote,
                "SecurityName": str(security.get("SecurityName") or ""),
                "SecurityClass": str(security.get("SecurityClass") or "UNKNOWN"),
                "SecurityClassConfidence": str(
                    security.get("SecurityClassConfidence") or "MISSING"
                ),
                "SecurityClassEvidenceSource": str(
                    security.get("SecurityClassEvidenceSource") or "Unavailable"
                ),
            }
        )
        if scan_limit > 0 and len(candidates) >= scan_limit:
            break
    return candidates


def ticker_cache_path(cache_dir: Path, ticker: str) -> Path:
    safe_ticker = re.sub(r"[^A-Z0-9_-]", "_", ticker.upper())
    # Prefix the filename because Windows reserves names such as CON, PRN,
    # AUX, NUL, COM1 and LPT1 even when an extension is present.
    return cache_dir / "ticker_info" / f"ticker_{safe_ticker}.json"


def legacy_ticker_cache_path(cache_dir: Path, ticker: str) -> Optional[Path]:
    safe_ticker = re.sub(r"[^A-Z0-9_-]", "_", ticker.upper())
    if safe_ticker in WINDOWS_RESERVED_BASENAMES:
        return None
    return cache_dir / "ticker_info" / f"{safe_ticker}.json"


def classify_yahoo_exception(exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}".lower()
    if any(marker in message for marker in RATE_LIMIT_MARKERS):
        return "rate_limit"
    if any(marker in message for marker in TRANSIENT_MARKERS):
        return "transient"
    return "other"


def _yahoo_request_worker(ticker: str, operation: str, result_queue) -> None:
    """Run one request in a daemon thread; the main thread owns the timeout."""
    try:
        stock = yf.Ticker(ticker)
        if operation == "info":
            getter = getattr(stock, "get_info", None)
            payload = getter() if callable(getter) else stock.info
            if not isinstance(payload, dict) or len(payload) < 5:
                raise TemporaryDataError("empty or incomplete info payload")
            result_queue.put(("ok", json_safe(payload)))
            return
        if operation == "ppe":
            getter = getattr(stock, "get_balance_sheet", None)
            statement = (
                getter(freq="quarterly")
                if callable(getter)
                else stock.quarterly_balance_sheet
            )
            net_ppe = latest_statement_value(
                statement,
                (
                    "Net PPE",
                    "Property Plant Equipment",
                    "Property Plant And Equipment Net",
                ),
            )
            result_queue.put(("ok", net_ppe))
            return
        raise ValueError(f"unsupported Yahoo operation: {operation}")
    except Exception as exc:
        result_queue.put(
            (
                "error",
                {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            )
        )


def run_yahoo_request_with_timeout(
    ticker: str,
    operation: str,
    timeout_seconds: float,
) -> Any:
    result_queue = queue.Queue(maxsize=1)
    worker = threading.Thread(
        target=_yahoo_request_worker,
        args=(ticker, operation, result_queue),
        daemon=True,
    )
    worker.start()
    worker.join(max(5.0, float(timeout_seconds)))
    if worker.is_alive():
        raise RequestTimeoutStop(
            f"{ticker}: Yahoo {operation} request exceeded "
            f"{timeout_seconds:.0f} seconds"
        )
    try:
        status, payload = result_queue.get_nowait()
    except queue.Empty as exc:
        raise TemporaryDataError(
            f"{ticker}: Yahoo {operation} worker exited without a result"
        ) from exc
    if status == "ok":
        return payload
    raise RuntimeError(
        f"{ticker}: {payload.get('type', 'YahooError')}: "
        f"{payload.get('message', 'unknown Yahoo error')}"
    )


def fetch_ticker_info(
    ticker: str,
    cache_dir: Path,
    pacer: RequestPacer,
    fresh: bool = False,
    max_attempts: int = 2,
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
) -> Tuple[dict, bool]:
    cache_path = ticker_cache_path(cache_dir, ticker)
    cached = load_json(cache_path)
    if not isinstance(cached, dict):
        legacy_path = legacy_ticker_cache_path(cache_dir, ticker)
        if legacy_path is not None and legacy_path.exists():
            cached = load_json(legacy_path)
    if (
        not fresh
        and isinstance(cached, dict)
        and is_fresh(cached.get("fetched_at"), INFO_CACHE_DAYS * 24 * 60 * 60)
        and isinstance(cached.get("info"), dict)
    ):
        if not cache_path.exists():
            atomic_write_json(cache_path, cached)
        return cached["info"], True

    last_error: Optional[Exception] = None
    for attempt in range(max_attempts):
        pacer.wait()
        try:
            info = run_yahoo_request_with_timeout(
                ticker,
                "info",
                timeout_seconds,
            )
            atomic_write_json(
                cache_path,
                {"fetched_at": utc_now_iso(), "info": info},
            )
            return info, False
        except Exception as exc:
            last_error = exc
            category = classify_yahoo_exception(exc)
            if category == "rate_limit":
                raise RateLimitStop(
                    f"{ticker}: Yahoo rate limited; preserve checkpoint and stop."
                ) from exc
            if category == "transient" or isinstance(exc, TemporaryDataError):
                if attempt + 1 < max_attempts:
                    delay = 20 * (3 ** attempt) + random.uniform(3, 10)
                    print(
                        f"[{ticker}] 暫時性錯誤，{delay:.0f} 秒後做最後一次重試。"
                    )
                    time.sleep(delay)
                    continue
                raise TemporaryDataError(f"{ticker}: {exc}") from exc
            raise RuntimeError(f"{ticker}: non-retryable Yahoo error: {exc}") from exc
    raise TemporaryDataError(f"{ticker}: {last_error}")


def first_number(mapping: dict, *keys: str) -> Optional[float]:
    for key in keys:
        value = mapping.get(key)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return None


def first_text(mapping: dict, *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip().lower() not in {
            "", "nan", "none", "null", "n/a", "<na>", "nat",
        }:
            return value.strip()
    return ""


def latest_statement_value(statement: pd.DataFrame, labels: Iterable[str]) -> Optional[float]:
    if statement is None or statement.empty:
        return None
    for label in labels:
        if label not in statement.index:
            continue
        values = pd.to_numeric(statement.loc[label], errors="coerce").dropna()
        if not values.empty:
            return float(values.iloc[0])
    return None


def fetch_optional_net_ppe(
    ticker: str,
    cache_dir: Path,
    pacer: RequestPacer,
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
) -> Optional[float]:
    safe_ticker = re.sub(r"[^A-Z0-9_-]", "_", ticker.upper())
    cache_path = cache_dir / "ticker_ppe" / f"ticker_{safe_ticker}.json"
    cached = load_json(cache_path)
    if (
        isinstance(cached, dict)
        and is_fresh(cached.get("fetched_at"), INFO_CACHE_DAYS * 24 * 60 * 60)
    ):
        return first_number(cached, "net_ppe")

    pacer.wait()
    try:
        net_ppe = run_yahoo_request_with_timeout(
            ticker,
            "ppe",
            timeout_seconds,
        )
        atomic_write_json(
            cache_path,
            {"fetched_at": utc_now_iso(), "net_ppe": net_ppe},
        )
        return net_ppe
    except Exception as exc:
        category = classify_yahoo_exception(exc)
        if category == "rate_limit":
            raise RateLimitStop(f"{ticker}: rate limited while fetching PP&E") from exc
        if category == "transient":
            raise TemporaryDataError(f"{ticker}: PP&E fetch failed: {exc}") from exc
        return None


def make_result(
    candidate: dict,
    config: HunterConfig,
    status: str,
    **fields: Any,
) -> dict:
    quote = candidate.get("ScreenerQuote") or {}
    market_data = candidate.get("_MergedInfo") or quote
    price = first_number(
        market_data,
        "currentPrice",
        "regularMarketPrice",
        "intradayprice",
    )
    average_volume = first_number(
        market_data,
        "averageVolume",
        "averageDailyVolume3Month",
        "averageDailyVolume10Day",
    )
    average_dollar_volume_m = (
        price * average_volume / 1_000_000
        if price is not None
        and price > 0
        and average_volume is not None
        and average_volume > 0
        else None
    )
    return {
        "Ticker": candidate["Ticker"],
        "CIK": candidate["CIK"],
        "Name": candidate.get("Name", ""),
        "SECExchange": candidate.get("SECExchange", ""),
        "QuoteType": str(quote.get("quoteType") or "").upper(),
        "Exchange": str(quote.get("exchange") or "").upper(),
        "SecurityName": candidate.get("SecurityName", ""),
        "SecurityClass": candidate.get("SecurityClass", "UNKNOWN"),
        "SecurityClassConfidence": candidate.get(
            "SecurityClassConfidence",
            "MISSING",
        ),
        "SecurityClassEvidenceSource": candidate.get(
            "SecurityClassEvidenceSource",
            "Unavailable",
        ),
        "AverageDailyDollarVolume_M": (
            round(average_dollar_volume_m, 3)
            if average_dollar_volume_m is not None
            else None
        ),
        **fields,
        "Status": status,
        "HunterPolicyVersion": HUNTER_POLICY_VERSION,
        "ConfigSignature": config.signature(),
        "EvaluatedAt": utc_now_iso(),
    }


def evaluate_candidate(
    candidate: dict,
    info: dict,
    config: HunterConfig,
    cache_dir: Path,
    yahoo_pacer: RequestPacer,
    used_info_cache: bool,
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
) -> dict:
    ticker = candidate["Ticker"]
    quote = candidate.get("ScreenerQuote") or {}
    merged = {**quote, **info}
    candidate = {**candidate, "_MergedInfo": merged}

    if candidate.get("SecurityClass") == "EXCLUDED_NON_COMMON":
        return make_result(
            candidate,
            config,
            "Drop: official security directory identifies a non-common instrument",
        )

    quote_type = first_text(merged, "quoteType").upper()
    exchange = first_text(merged, "exchange").upper()
    if quote_type and quote_type != "EQUITY":
        return make_result(candidate, config, f"Drop: 非普通股商品 ({quote_type})")
    if exchange and exchange not in SUPPORTED_EQUITY_EXCHANGES:
        return make_result(candidate, config, f"Drop: 非主要美國交易所 ({exchange})")
    if merged.get("fundFamily") or merged.get("category"):
        return make_result(candidate, config, "Drop: ETF/基金")

    market_cap = first_number(merged, "marketCap", "intradaymarketcap")
    if market_cap is None:
        return make_result(candidate, config, "Review: 市值資料缺失")
    market_cap_b = market_cap / 1_000_000_000
    if market_cap_b < config.min_mcap_b:
        return make_result(
            candidate,
            config,
            f"Drop: 市值<{config.min_mcap_b:.1f}B ({market_cap_b:.2f}B)",
            MarketCap_B=round(market_cap_b, 3),
        )

    sector = first_text(merged, "sector")
    industry = first_text(merged, "industry")
    if not sector or not industry:
        return make_result(
            candidate,
            config,
            "Review: 產業資料缺失",
            MarketCap_B=round(market_cap_b, 3),
        )
    if sector in STRATEGY_EXCLUDED_SECTORS:
        return make_result(candidate, config, f"Drop: 產業隔離 ({sector})")
    model_route = route_industry_model(
        sector, industry, ticker=str(candidate.get("Ticker") or "")
    )
    if not bool(model_route["supported"]):
        return make_result(
            candidate,
            config,
            "Review: 專用產業模型路由不可判定",
            Sector=sector,
            Industry=industry,
            MarketCap_B=round(market_cap_b, 3),
            ModelRouteHint=str(model_route["route"]),
            IndustryModelKey=str(model_route.get("model_key") or "UNKNOWN"),
            ModelSupported=False,
            RouteReason=str(model_route["reason"]),
            InfoFromCache=used_info_cache,
        )
    industry_initial = initial_screen_industry(str(model_route["model_key"]), merged)
    if industry_initial["decision"] == "DROP":
        return make_result(
            candidate,
            config,
            "Drop: 專用產業初篩未通過",
            Sector=sector,
            Industry=industry,
            MarketCap_B=round(market_cap_b, 3),
            ModelRouteHint=str(model_route["route"]),
            IndustryModelKey=str(model_route["model_key"]),
            ModelSupported=True,
            RouteReason=str(model_route["reason"]),
            IndustryInitialScore=industry_initial["data_quality_score"],
            IndustryInitialWarnings=" | ".join(industry_initial["warnings"]),
            IndustryInitialFailures=" | ".join(industry_initial["hard_failures"]),
            InfoFromCache=used_info_cache,
        )
    if str(model_route["model_key"]) != "GENERAL_CORPORATE":
        return make_result(
            candidate,
            config,
            "Pass",
            Sector=sector,
            Industry=industry,
            MarketCap_B=round(market_cap_b, 3),
            ModelRouteHint=str(model_route["route"]),
            IndustryModelKey=str(model_route["model_key"]),
            ModelSupported=True,
            RouteReason=str(model_route["reason"]),
            RoutedWithoutGeneralCorporateScoring=True,
            IndustryInitialScore=industry_initial["data_quality_score"],
            IndustryInitialWarnings=" | ".join(industry_initial["warnings"]),
            IndustryInitialFailures="",
            InfoFromCache=used_info_cache,
        )
    industry_lower = industry.lower()
    blocked_keyword = next(
        (keyword for keyword in BLOCKED_INDUSTRY_KEYWORDS if keyword in industry_lower),
        None,
    )
    if blocked_keyword:
        return make_result(candidate, config, f"Drop: 行業隔離 ({industry})")

    first_layer_warnings: List[str] = []
    ocf = first_number(merged, "operatingCashflow", "operatingCashFlow")
    if ocf is None:
        first_layer_warnings.append("Yahoo OCF missing; defer to SEC")
    elif ocf <= 0:
        first_layer_warnings.append("Yahoo OCF is non-positive; require SEC corroboration")

    gross_margin = first_number(merged, "grossMargins", "grossMargin")
    if gross_margin is None or gross_margin > 1:
        gross_margin = None
        first_layer_warnings.append("Yahoo gross margin missing; defer to SEC")
    elif gross_margin <= config.min_gross_margin_floor:
        return make_result(
            candidate,
            config,
            (
                f"Drop: 毛利率低於最低底線 {config.min_gross_margin_floor * 100:.0f}% "
                f"({gross_margin * 100:.1f}%)"
            ),
            Sector=sector,
            Industry=industry,
            MarketCap_B=round(market_cap_b, 3),
            GrossMargin=round(gross_margin * 100, 2),
        )

    ebitda = first_number(merged, "ebitda")
    total_debt = first_number(merged, "totalDebt")
    revenue = first_number(merged, "totalRevenue")
    if ebitda is None:
        first_layer_warnings.append("Yahoo EBITDA missing; defer to SEC")
    elif ebitda <= 0:
        first_layer_warnings.append("Yahoo EBITDA is non-positive; require SEC corroboration")
    if (
        ocf is not None
        and ocf <= 0
        and ebitda is not None
        and ebitda <= 0
    ):
        return make_result(
            candidate,
            config,
            "Drop: Yahoo OCF 與 EBITDA 同時非正值",
            Sector=sector,
            Industry=industry,
            MarketCap_B=round(market_cap_b, 3),
        )
    if total_debt is None or total_debt < 0:
        total_debt = None
        first_layer_warnings.append("Yahoo total debt missing; defer to SEC")
    if revenue is None:
        first_layer_warnings.append("Yahoo revenue missing; defer to SEC")
    elif revenue <= 0:
        first_layer_warnings.append("Yahoo revenue is non-positive; require SEC corroboration")

    debt_ebitda = (
        total_debt / ebitda
        if total_debt is not None and ebitda is not None and ebitda > 0
        else None
    )
    total_cash = first_number(merged, "totalCash", "cash")
    cash_is_usable = total_cash is not None and total_cash >= 0
    net_debt_ebitda = (
        max(total_debt - total_cash, 0.0) / ebitda
        if cash_is_usable and total_debt is not None and ebitda is not None and ebitda > 0
        else None
    )
    screening_leverage = (
        net_debt_ebitda
        if net_debt_ebitda is not None
        else debt_ebitda
    )
    leverage_label = (
        "淨負債/EBITDA"
        if net_debt_ebitda is not None
        else "負債/EBITDA"
        if debt_ebitda is not None
        else "待 SEC 確認"
    )
    if net_debt_ebitda is not None and net_debt_ebitda > config.max_debt_ebitda:
        return make_result(
            candidate,
            config,
            (
                f"Drop: {leverage_label}>{config.max_debt_ebitda:.1f} "
                f"({screening_leverage:.1f}x)"
            ),
            Sector=sector,
            Industry=industry,
            MarketCap_B=round(market_cap_b, 3),
            GrossMargin=round(gross_margin * 100, 2) if gross_margin is not None else None,
            Debt_EBITDA=round(debt_ebitda, 3) if debt_ebitda is not None else None,
            NetDebt_EBITDA=(
                round(net_debt_ebitda, 3)
                if net_debt_ebitda is not None
                else None
            ),
            LeverageBasis=leverage_label,
        )
    if (
        net_debt_ebitda is None
        and debt_ebitda is not None
        and debt_ebitda > config.max_debt_ebitda
    ):
        first_layer_warnings.append(
            "Yahoo gross debt / EBITDA exceeds the limit but cash is unavailable; defer net leverage to SEC"
        )
    leverage_warning = bool(
        screening_leverage is not None
        and screening_leverage > config.debt_ebitda_warning
    )
    net_cash = (
        cash_is_usable
        and total_debt is not None
        and total_cash > total_debt
    )

    institutional_own = first_number(merged, "heldPercentInstitutions")
    if config.require_institutional_ownership:
        if institutional_own is None or not 0 <= institutional_own <= 1:
            return make_result(candidate, config, "Review: 機構持股資料缺失或失真")
        if institutional_own < config.min_institutional_own:
            return make_result(
                candidate,
                config,
                (
                    f"Drop: 機構持股<{config.min_institutional_own * 100:.0f}% "
                    f"({institutional_own * 100:.1f}%)"
                ),
            )

    ppe_revenue: Optional[float] = None
    if config.enable_ppe_filter:
        if revenue is None or revenue <= 0:
            first_layer_warnings.append(
                "Yahoo revenue is unavailable or non-positive; PP&E ratio deferred to SEC"
            )
        else:
            net_ppe = first_number(merged, "netPPE", "propertyPlantEquipment")
            if net_ppe is None:
                net_ppe = fetch_optional_net_ppe(
                    ticker,
                    cache_dir,
                    yahoo_pacer,
                    request_timeout_seconds,
                )
            if net_ppe is None or net_ppe < 0:
                return make_result(candidate, config, "Review: PP&E 資料缺失")
            ppe_revenue = net_ppe / revenue
            if ppe_revenue > config.max_ppe_rev_ratio:
                return make_result(
                    candidate,
                    config,
                    (
                        f"Drop: PP&E/Revenue>{config.max_ppe_rev_ratio:.1f} "
                        f"({ppe_revenue:.2f})"
                    ),
                )

    needs_peer_margin_check = bool(
        gross_margin is not None
        and gross_margin < config.standard_gross_margin
    )
    return make_result(
        candidate,
        config,
        "PeerCheck: 毛利率低於25%，等待同業中位數比較"
        if needs_peer_margin_check
        else "Pass",
        Sector=sector,
        Industry=industry,
        MarketCap_B=round(market_cap_b, 3),
        OperatingCashFlow_B=(
            round(ocf / 1_000_000_000, 3) if ocf is not None else None
        ),
        GrossMargin=(
            round(gross_margin * 100, 2) if gross_margin is not None else None
        ),
        InstitutionalOwnership=(
            round(institutional_own * 100, 2)
            if institutional_own is not None and 0 <= institutional_own <= 1
            else None
        ),
        Debt_EBITDA=round(debt_ebitda, 3) if debt_ebitda is not None else None,
        NetDebt_EBITDA=(
            round(net_debt_ebitda, 3) if net_debt_ebitda is not None else None
        ),
        LeverageBasis=leverage_label,
        LeverageWarning=leverage_warning,
        GrossLeverageWarning=bool(
            debt_ebitda is not None
            and debt_ebitda > config.debt_ebitda_warning
        ),
        NetCash=net_cash,
        GrossMarginRule=(
            "絕對毛利率>=25%"
            if gross_margin is not None and not needs_peer_margin_check
            else "等待同業中位數"
            if needs_peer_margin_check
            else "Yahoo 毛利率缺失，交由 SEC 深篩"
        ),
        RoutedToSECForMissingYahoo=bool(first_layer_warnings),
        FirstLayerWarnings=" | ".join(first_layer_warnings),
        PPE_Revenue=round(ppe_revenue, 3) if ppe_revenue is not None else None,
        InfoFromCache=used_info_cache,
    )


def summarize_reasons(rows: List[dict]) -> None:
    counts: Dict[str, int] = {}
    for row in rows:
        status = str(row.get("Status") or "Unknown")
        reason = status.split("(", 1)[0].strip()
        counts[reason] = counts.get(reason, 0) + 1
    print("\n[結果統計]")
    for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {reason}: {count}")


def rows_for_candidates(
    candidates: List[dict],
    results_by_ticker: Dict[str, dict],
) -> List[dict]:
    return [
        results_by_ticker[candidate["Ticker"]]
        for candidate in candidates
        if candidate["Ticker"] in results_by_ticker
    ]


def apply_peer_margin_rules(
    rows: List[dict],
    min_sample: int = MIN_INDUSTRY_SAMPLE,
) -> List[dict]:
    """Annotate sub-25% margins without making one ratio a hard exclusion."""
    adjusted = [dict(row) for row in rows]
    industry_margins: Dict[str, List[float]] = {}
    for row in adjusted:
        industry = str(row.get("Industry") or "").strip()
        try:
            margin = float(row.get("GrossMargin"))
        except (TypeError, ValueError):
            continue
        if industry and math.isfinite(margin):
            industry_margins.setdefault(industry, []).append(margin)

    for row in adjusted:
        if not str(row.get("Status") or "").startswith("PeerCheck:"):
            continue
        industry = str(row.get("Industry") or "").strip()
        peers = industry_margins.get(industry, [])
        if len(peers) < max(1, min_sample):
            row["Status"] = "Pass"
            row["GrossMarginRule"] = f"同業樣本不足({len(peers)})，交由 SEC 深篩"
            row["GrossMarginWarning"] = True
            continue
        median_margin = float(pd.Series(peers).median())
        row["IndustryMedianGrossMargin"] = round(median_margin, 2)
        own_margin = float(row.get("GrossMargin"))
        if own_margin >= median_margin:
            row["Status"] = "Pass"
            row["GrossMarginRule"] = "低於25%但高於同業中位數"
            row["GrossMarginWarning"] = False
        else:
            row["Status"] = "Pass"
            row["GrossMarginRule"] = "低於同業中位數，交由 SEC 深篩"
            row["GrossMarginWarning"] = True
    return adjusted


def enrich_ambiguous_ads_from_sec(
    rows: List[dict],
    email: str,
    cache_dir: Path,
) -> List[dict]:
    """Resolve generic ADS labels from point-in-time SEC annual cover facts."""
    adjusted = [dict(row) for row in rows]
    targets: Dict[str, List[dict]] = {}
    for row in adjusted:
        status = str(row.get("Status") or "")
        if row.get("SecurityClass") != "AMBIGUOUS_ADS":
            continue
        if status != "Pass" and not status.startswith("PeerCheck:"):
            continue
        cik = str(row.get("CIK") or "").replace(".0", "").zfill(10)
        if cik.isdigit() and int(cik) > 0:
            targets.setdefault(cik, []).append(row)
    if not targets:
        return adjusted

    session = create_sec_session()
    pacer = RequestPacer(0.12)
    resolved = 0
    unresolved = 0
    for cik, group in targets.items():
        payload = get_sec_registered_security_titles(
            cik,
            email,
            cache_dir,
            pacer,
            session,
        )
        titles = payload.get("titles") if isinstance(payload, dict) else {}
        source = (
            f"SEC {payload.get('form') or 'annual'} cover "
            f"{payload.get('accession') or 'unavailable'}; "
            f"accepted {payload.get('accepted_at') or 'unavailable'}"
        )
        for row in group:
            ticker = normalize_market_symbol(row.get("Ticker"))
            title = titles.get(ticker) if isinstance(titles, dict) else None
            if title:
                security_class = classify_security_name(title)
                row["SecurityName"] = title
                row["SecurityClass"] = security_class
                row["SecurityClassConfidence"] = (
                    "NEEDS_SEC" if security_class == "AMBIGUOUS_ADS" else "HIGH"
                )
                row["SecurityClassEvidenceSource"] = source
                if security_class in {
                    "COMMON_OR_EQUIVALENT",
                    "EXCLUDED_NON_COMMON",
                }:
                    resolved += 1
                    continue
            row["SecurityClass"] = "UNRESOLVED_ADS"
            row["SecurityClassConfidence"] = "LOW"
            row["SecurityClassEvidenceSource"] = (
                f"{source}; symbol-to-title mapping unresolved"
            )
            unresolved += 1

    inferred = 0
    by_cik: Dict[str, List[dict]] = {}
    for row in adjusted:
        status = str(row.get("Status") or "")
        if status == "Pass" or status.startswith("PeerCheck:"):
            cik = str(row.get("CIK") or "").replace(".0", "").zfill(10)
            by_cik.setdefault(cik, []).append(row)
    all_rows_by_cik: Dict[str, List[dict]] = {}
    for row in adjusted:
        cik = str(row.get("CIK") or "").replace(".0", "").zfill(10)
        all_rows_by_cik.setdefault(cik, []).append(row)
    for cik, eligible_group in by_cik.items():
        unresolved_rows = [
            row
            for row in eligible_group
            if row.get("SecurityClass") == "UNRESOLVED_ADS"
        ]
        if len(unresolved_rows) != 1:
            continue
        if any(
            row.get("SecurityClass") in COMMON_SECURITY_CLASSES
            for row in eligible_group
        ):
            continue
        has_preferred_sibling = any(
            row.get("SecurityClass") == "EXCLUDED_NON_COMMON"
            for row in all_rows_by_cik.get(cik, [])
        )
        row = unresolved_rows[0]
        inference = (
            "only unresolved ADS after an official preferred sibling was excluded"
            if has_preferred_sibling
            else "sole exchange-listed ADS class for this CIK"
        )
        row["SecurityClass"] = "COMMON_ADS_INFERRED"
        row["SecurityClassConfidence"] = "MEDIUM"
        row["SecurityClassEvidenceSource"] = (
            f"{row.get('SecurityClassEvidenceSource')}; inferred common: {inference}"
        )
        inferred += 1
        unresolved -= 1
    print(
        f"[SEC share-class check] resolved={resolved:,}; "
        f"inferred={inferred:,}; unresolved={unresolved:,}; CIKs={len(targets):,}."
    )
    return adjusted


def resolve_share_classes(rows: List[dict]) -> List[dict]:
    """Fail closed on non-common ADSs and choose duplicate CIKs by liquidity."""
    adjusted = [dict(row) for row in rows]
    for row in adjusted:
        if row.get("Status") != "Pass":
            continue
        security_class = str(row.get("SecurityClass") or "UNKNOWN")
        if security_class == "EXCLUDED_NON_COMMON":
            row["Status"] = "Drop: official evidence identifies a non-common security"
            row["ShareClassSelectionReason"] = row.get("SecurityClassEvidenceSource", "")
        elif security_class in {"AMBIGUOUS_ADS", "UNRESOLVED_ADS"}:
            row["Status"] = "Review: ADS common/preferred class is unresolved"
            row["ShareClassSelectionReason"] = row.get("SecurityClassEvidenceSource", "")
        elif security_class == "UNKNOWN":
            row["Status"] = "Review: official common-equity class evidence is missing"
            row["ShareClassSelectionReason"] = row.get("SecurityClassEvidenceSource", "")

    pass_groups: Dict[str, List[dict]] = {}
    for row in adjusted:
        if row.get("Status") != "Pass":
            continue
        cik = str(row.get("CIK") or "").replace(".0", "").zfill(10)
        pass_groups.setdefault(cik, []).append(row)

    def selection_key(row: dict) -> Tuple[float, float, str]:
        try:
            liquidity = float(row.get("AverageDailyDollarVolume_M"))
        except (TypeError, ValueError):
            liquidity = -1.0
        try:
            market_cap = float(row.get("MarketCap_B"))
        except (TypeError, ValueError):
            market_cap = -1.0
        if not math.isfinite(liquidity):
            liquidity = -1.0
        if not math.isfinite(market_cap):
            market_cap = -1.0
        return liquidity, market_cap, str(row.get("Ticker") or "")

    for cik, group in pass_groups.items():
        if len(group) == 1:
            group[0]["ShareClassSelectionReason"] = "Only eligible listed class for CIK"
            continue
        common = [
            row
            for row in group
            if row.get("SecurityClass") in COMMON_SECURITY_CLASSES
        ]
        if not common:
            for row in group:
                row["Status"] = "Review: duplicate CIK share classes are unresolved"
                row["ShareClassSelectionReason"] = (
                    "No listed class has official common-equity evidence"
                )
            continue
        selected = max(common, key=selection_key)
        selected_ticker = str(selected.get("Ticker") or "")
        selected["ShareClassSelectionReason"] = (
            "Official common-equity class with highest average daily dollar volume"
        )
        for row in group:
            if row is selected:
                continue
            row["Status"] = f"Drop: duplicate CIK share class; selected {selected_ticker}"
            row["ShareClassSelectionReason"] = (
                f"Selected {selected_ticker} using official class evidence and liquidity"
            )
    return adjusted


def qualified_rows(rows: List[dict]) -> List[dict]:
    resolved_rows = resolve_share_classes(apply_peer_margin_rules(rows))
    passes = [row for row in resolved_rows if row.get("Status") == "Pass"]
    passes.sort(key=lambda row: float(row.get("MarketCap_B") or 0), reverse=True)
    return passes


def write_partial_outputs(output_dir: Path, rows: List[dict]) -> None:
    resolved_rows = resolve_share_classes(apply_peer_margin_rules(rows))
    atomic_write_csv(output_dir / "hunter_audit.partial.csv", resolved_rows)
    atomic_write_csv(
        output_dir / "qualified_universe.partial.csv",
        qualified_rows(resolved_rows),
    )


def write_complete_outputs(output_dir: Path, rows: List[dict]) -> None:
    resolved_rows = resolve_share_classes(apply_peer_margin_rules(rows))
    atomic_write_csv(output_dir / "hunter_audit.csv", resolved_rows)
    atomic_write_csv(
        output_dir / "qualified_universe.csv",
        qualified_rows(resolved_rows),
    )
    for partial_name in (
        "hunter_audit.partial.csv",
        "qualified_universe.partial.csv",
    ):
        partial_path = output_dir / partial_name
        if partial_path.exists():
            partial_path.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Conservative, resumable US equity pre-screener."
    )
    parser.add_argument(
        "--email",
        default=os.environ.get("USER_EMAIL", ""),
        help="Contact email used in the SEC User-Agent.",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory for cache, checkpoints and CSV outputs.",
    )
    parser.add_argument(
        "--scan-limit",
        type=int,
        default=0,
        help="Process only the first N screener candidates (0 = all).",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore request/result freshness and refresh remote data.",
    )
    parser.add_argument(
        "--yahoo-interval",
        type=float,
        default=DEFAULT_YAHOO_INTERVAL_SECONDS,
        help="Minimum seconds between detailed Yahoo requests.",
    )
    parser.add_argument(
        "--screener-interval",
        type=float,
        default=DEFAULT_SCREENER_INTERVAL_SECONDS,
        help="Minimum seconds between Yahoo Screener pages.",
    )
    parser.add_argument(
        "--require-institutional-ownership",
        action="store_true",
        help="Enable the optional 40%% institutional ownership hard filter.",
    )
    parser.add_argument(
        "--enable-ppe-filter",
        action="store_true",
        help="Enable PP&E/Revenue; may add one Yahoo statement request per survivor.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=20,
        help="Safety limit for Yahoo Screener pagination.",
    )
    parser.add_argument(
        "--max-transient-failures",
        type=int,
        default=DEFAULT_MAX_TRANSIENT_FAILURES,
        help="Stop after this many consecutive temporary network failures.",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        help="Hard timeout in seconds for each detailed Yahoo request.",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    try:
        args.email = validate_sec_contact_email(args.email)
    except ValueError as exc:
        print(f"[設定錯誤] {exc}")
        return 2

    config = HunterConfig(
        require_institutional_ownership=args.require_institutional_ownership,
        enable_ppe_filter=args.enable_ppe_filter,
    )
    signature = config.signature()
    output_dir = Path(args.output_dir).expanduser().resolve()
    cache_dir = output_dir / ".hunter_cache"
    checkpoint_path = output_dir / "hunter_checkpoint.jsonl"
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("Alpha Engine conservative market hunter")
    print(f"Output: {output_dir}")
    print(f"Config signature: {signature}")
    print(
        "Policy: sequential detail requests, stop-on-rate-limit, "
        "cache + checkpoint resume"
    )
    print(
        f"Optional filters: institutional={config.require_institutional_ownership}, "
        f"PP&E={config.enable_ppe_filter}"
    )
    print("=" * 72)

    sec_map = get_sec_ticker_map(args.email, cache_dir, fresh=args.fresh)
    try:
        security_directory = get_nasdaq_security_directory(
            args.email,
            cache_dir,
            fresh=args.fresh,
        )
    except RuntimeError as exc:
        print(f"[安全停止] {exc}")
        return 3
    screener_pacer = RequestPacer(args.screener_interval)
    yahoo_pacer = RequestPacer(args.yahoo_interval)

    try:
        quotes = get_yahoo_screener_candidates(
            config,
            cache_dir,
            screener_pacer,
            fresh=args.fresh,
            max_pages=max(1, args.max_pages),
        )
    except (RateLimitStop, TemporaryDataError, RuntimeError) as exc:
        print(f"[停止] 無法建立 Yahoo 預篩名單：{exc}")
        return 3

    candidates = prefilter_candidates(
        quotes,
        sec_map,
        security_directory=security_directory,
        scan_limit=max(0, args.scan_limit),
    )
    if not candidates:
        print("[停止] 預篩後沒有候選；不會覆蓋既有 qualified_universe.csv。")
        return 4

    checkpoint = {} if args.fresh else load_checkpoint(checkpoint_path, signature)
    results_by_ticker = {
        ticker: row
        for ticker, row in checkpoint.items()
        if completed_result_is_fresh(row)
    }
    pending = [
        candidate
        for candidate in candidates
        if candidate["Ticker"] not in results_by_ticker
    ]
    print(
        f"[計畫] Yahoo 預篩={len(quotes):,}；SEC 對齊後={len(candidates):,}；"
        f"可重用結果={len(results_by_ticker):,}；待抓詳細資料={len(pending):,}"
    )

    stopped_early = False
    hard_timeout_stop = False
    consecutive_transient_failures = 0
    started_at = time.monotonic()

    try:
        for index, candidate in enumerate(pending, start=1):
            ticker = candidate["Ticker"]
            try:
                if candidate.get("SecurityClass") == "EXCLUDED_NON_COMMON":
                    info, used_cache = {}, False
                else:
                    info, used_cache = fetch_ticker_info(
                        ticker,
                        cache_dir,
                        yahoo_pacer,
                        fresh=args.fresh,
                        timeout_seconds=max(5.0, args.request_timeout),
                    )
                result = evaluate_candidate(
                    candidate,
                    info,
                    config,
                    cache_dir,
                    yahoo_pacer,
                    used_cache,
                    request_timeout_seconds=max(5.0, args.request_timeout),
                )
                consecutive_transient_failures = 0
            except RateLimitStop as exc:
                result = make_result(
                    candidate,
                    config,
                    f"Retry: Yahoo 限流或請求逾時；下次從此處接續 ({str(exc)[:120]})",
                )
                stopped_early = True
            except RequestTimeoutStop as exc:
                result = make_result(
                    candidate,
                    config,
                    f"Retry: Yahoo 請求逾時；下次從此處接續 ({str(exc)[:120]})",
                )
                stopped_early = True
                hard_timeout_stop = True
            except TemporaryDataError as exc:
                consecutive_transient_failures += 1
                result = make_result(
                    candidate,
                    config,
                    f"Retry: 暫時性網路錯誤 ({str(exc)[:120]})",
                )
                if consecutive_transient_failures >= max(
                    1,
                    args.max_transient_failures,
                ):
                    stopped_early = True
            except Exception as exc:
                result = make_result(
                    candidate,
                    config,
                    f"Review: 非暫時性資料錯誤 ({type(exc).__name__}: {str(exc)[:120]})",
                )
                consecutive_transient_failures = 0

            results_by_ticker[ticker] = result
            append_checkpoint(checkpoint_path, result)

            processed_rows = rows_for_candidates(candidates, results_by_ticker)
            if index % 25 == 0 or stopped_early or index == len(pending):
                write_partial_outputs(output_dir, processed_rows)

            if index % 10 == 0 or stopped_early or index == len(pending):
                elapsed = max(time.monotonic() - started_at, 0.001)
                print(
                    f"[進度] 本次 {index}/{len(pending)}；"
                    f"總完成 {len(processed_rows)}/{len(candidates)}；"
                    f"Pass={len(qualified_rows(processed_rows))}；"
                    f"{index / elapsed:.2f} 檔/秒"
                )

            if stopped_early:
                print(
                    "[安全停止] 已保留 checkpoint 與 partial CSV。"
                    "請稍後用相同指令重跑；既有完整 qualified_universe.csv 未被覆蓋。"
                )
                if hard_timeout_stop:
                    # Some Yahoo/curl backends keep non-daemon helper threads alive
                    # after the request thread times out. The checkpoint and partial
                    # CSV are already durable, so force process exit instead of
                    # waiting forever during interpreter shutdown.
                    sys.stdout.flush()
                    sys.stderr.flush()
                    os._exit(5)
                break
    except KeyboardInterrupt:
        stopped_early = True
        print("\n[使用者中斷] 已保留 checkpoint；下次會接續。")

    rows = rows_for_candidates(candidates, results_by_ticker)
    complete = (
        not stopped_early
        and len(rows) == len(candidates)
        and not any(str(row.get("Status") or "").startswith("Retry:") for row in rows)
    )
    if complete:
        rows = enrich_ambiguous_ads_from_sec(
            rows,
            args.email,
            cache_dir,
        )
        write_complete_outputs(output_dir, rows)
        print(
            f"[完成] {len(qualified_rows(rows))} 檔通過；"
            "已原子更新 qualified_universe.csv 與 hunter_audit.csv。"
        )
    else:
        write_partial_outputs(output_dir, rows)
        print(
            "[未完成] 僅更新 *.partial.csv；"
            "最後一次完整 qualified_universe.csv 保持不變。"
        )

    summarize_reasons(resolve_share_classes(apply_peer_margin_rules(rows)))
    return 0 if complete else 5


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    sys.exit(main())
