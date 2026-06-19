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
import hashlib
import json
import math
import multiprocessing
import os
import queue
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests
import yfinance as yf
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ==============================================================================
# Default screening policy
# ==============================================================================
MIN_MCAP_B = 3.0
MIN_GROSS_MARGIN = 0.25
MAX_DEBT_EBITDA = 4.0
MAX_PPE_REV_RATIO = 1.0
MIN_INSTITUTIONAL_OWN = 0.40

# Institutional ownership and PP&E are optional by default. They are useful
# research fields, but neither is worth multiplying network requests or
# excluding otherwise valid businesses during the very first screening stage.
DEFAULT_REQUIRE_INSTITUTIONAL_OWNERSHIP = False
DEFAULT_ENABLE_PPE_FILTER = False

SUPPORTED_EQUITY_EXCHANGES = {"NMS", "NYQ", "NGM", "NCM", "ASE", "PCX"}
SUPPORTED_SEC_EXCHANGES = {"NASDAQ", "NYSE", "NYSE AMERICAN"}

BLOCKED_SECTORS = {
    "Financial Services",
    "Financials",
    "Real Estate",
    "Energy",
    "Basic Materials",
    "Utilities",
}

BLOCKED_INDUSTRY_KEYWORDS = {
    "bank",
    "insurance",
    "reit",
    "mortgage",
    "credit services",
    "capital markets",
    "asset management",
    "airlines",
    "marine shipping",
    "trucking",
    "tobacco",
    "farm products",
    "packaged foods",
    "oil & gas",
    "coal",
    "auto manufacturers",
    "auto parts",
    "aerospace & defense",
    "steel",
    "aluminum",
    "copper",
}

# Keys are symbols to remove; values are the preferred share class.
DUAL_CLASS_KEEP = {
    "FOX": "FOXA",
    "GOOG": "GOOGL",
    "NWS": "NWSA",
    "UA": "UAA",
}

SEC_TICKER_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SCREENER_PAGE_SIZE = 250
SCREENER_CACHE_HOURS = 24
SEC_CACHE_DAYS = 7
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


@dataclass(frozen=True)
class HunterConfig:
    min_mcap_b: float = MIN_MCAP_B
    min_gross_margin: float = MIN_GROSS_MARGIN
    max_debt_ebitda: float = MAX_DEBT_EBITDA
    require_institutional_ownership: bool = DEFAULT_REQUIRE_INSTITUTIONAL_OWNERSHIP
    min_institutional_own: float = MIN_INSTITUTIONAL_OWN
    enable_ppe_filter: bool = DEFAULT_ENABLE_PPE_FILTER
    max_ppe_rev_ratio: float = MAX_PPE_REV_RATIO

    def signature(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
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
    pd.DataFrame(rows).to_csv(temp_path, index=False, encoding="utf-8-sig")
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
    return bool(re.fullmatch(r"[A-Z]{1,6}", ticker))


def prefilter_candidates(
    quotes: List[dict],
    sec_map: Dict[str, dict],
    scan_limit: int = 0,
) -> List[dict]:
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
        if sector in BLOCKED_SECTORS:
            continue

        candidates.append({**sec_row, "ScreenerQuote": quote})
        if scan_limit > 0 and len(candidates) >= scan_limit:
            break
    return candidates


def ticker_cache_path(cache_dir: Path, ticker: str) -> Path:
    safe_ticker = re.sub(r"[^A-Z0-9_-]", "_", ticker.upper())
    return cache_dir / "ticker_info" / f"{safe_ticker}.json"


def classify_yahoo_exception(exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}".lower()
    if any(marker in message for marker in RATE_LIMIT_MARKERS):
        return "rate_limit"
    if any(marker in message for marker in TRANSIENT_MARKERS):
        return "transient"
    return "other"


def _yahoo_request_worker(ticker: str, operation: str, result_queue) -> None:
    """Run one Yahoo request in an isolated process so Windows can enforce timeout."""
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
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(
        target=_yahoo_request_worker,
        args=(ticker, operation, result_queue),
        daemon=True,
    )
    process.start()
    process.join(max(5.0, float(timeout_seconds)))
    if process.is_alive():
        process.terminate()
        process.join(5)
        result_queue.close()
        result_queue.join_thread()
        raise TemporaryDataError(
            f"{ticker}: Yahoo {operation} request exceeded "
            f"{timeout_seconds:.0f} seconds"
        )
    try:
        status, payload = result_queue.get_nowait()
    except queue.Empty as exc:
        raise TemporaryDataError(
            f"{ticker}: Yahoo {operation} worker exited without a result"
        ) from exc
    finally:
        result_queue.close()
        result_queue.join_thread()
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
    if (
        not fresh
        and isinstance(cached, dict)
        and is_fresh(cached.get("fetched_at"), INFO_CACHE_DAYS * 24 * 60 * 60)
        and isinstance(cached.get("info"), dict)
    ):
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
        value = str(mapping.get(key) or "").strip()
        if value:
            return value
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
    cache_path = cache_dir / "ticker_ppe" / f"{ticker}.json"
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
    return {
        "Ticker": candidate["Ticker"],
        "CIK": candidate["CIK"],
        "Name": candidate.get("Name", ""),
        "SECExchange": candidate.get("SECExchange", ""),
        **fields,
        "Status": status,
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
    if sector in BLOCKED_SECTORS:
        return make_result(candidate, config, f"Drop: 產業隔離 ({sector})")
    industry_lower = industry.lower()
    blocked_keyword = next(
        (keyword for keyword in BLOCKED_INDUSTRY_KEYWORDS if keyword in industry_lower),
        None,
    )
    if blocked_keyword:
        return make_result(candidate, config, f"Drop: 行業隔離 ({industry})")

    ocf = first_number(merged, "operatingCashflow", "operatingCashFlow")
    if ocf is None:
        return make_result(candidate, config, "Review: OCF 資料缺失")
    if ocf <= 0:
        return make_result(candidate, config, "Drop: 營運現金流非正值")

    gross_margin = first_number(merged, "grossMargins", "grossMargin")
    if gross_margin is None or not 0 <= gross_margin <= 1:
        return make_result(candidate, config, "Review: 毛利率資料缺失或失真")
    if gross_margin < config.min_gross_margin:
        return make_result(
            candidate,
            config,
            (
                f"Drop: 毛利率<{config.min_gross_margin * 100:.0f}% "
                f"({gross_margin * 100:.1f}%)"
            ),
        )

    ebitda = first_number(merged, "ebitda")
    total_debt = first_number(merged, "totalDebt")
    revenue = first_number(merged, "totalRevenue")
    if ebitda is None or ebitda <= 0:
        return make_result(candidate, config, "Review: EBITDA 資料缺失或非正值")
    if total_debt is None or total_debt < 0:
        return make_result(candidate, config, "Review: 總負債資料缺失或失真")
    if revenue is None or revenue <= 0:
        return make_result(candidate, config, "Review: 營收資料缺失或非正值")

    debt_ebitda = total_debt / ebitda
    if debt_ebitda > config.max_debt_ebitda:
        return make_result(
            candidate,
            config,
            (
                f"Drop: 負債/EBITDA>{config.max_debt_ebitda:.1f} "
                f"({debt_ebitda:.1f}x)"
            ),
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

    return make_result(
        candidate,
        config,
        "Pass",
        Sector=sector,
        Industry=industry,
        MarketCap_B=round(market_cap_b, 3),
        OperatingCashFlow_B=round(ocf / 1_000_000_000, 3),
        GrossMargin=round(gross_margin * 100, 2),
        InstitutionalOwnership=(
            round(institutional_own * 100, 2)
            if institutional_own is not None and 0 <= institutional_own <= 1
            else None
        ),
        Debt_EBITDA=round(debt_ebitda, 3),
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


def qualified_rows(rows: List[dict]) -> List[dict]:
    passes = [row for row in rows if row.get("Status") == "Pass"]
    passes.sort(key=lambda row: float(row.get("MarketCap_B") or 0), reverse=True)
    seen_ciks = set()
    output = []
    for row in passes:
        cik = row.get("CIK")
        if cik in seen_ciks:
            continue
        seen_ciks.add(cik)
        output.append(row)
    return output


def write_partial_outputs(output_dir: Path, rows: List[dict]) -> None:
    atomic_write_csv(output_dir / "hunter_audit.partial.csv", rows)
    atomic_write_csv(
        output_dir / "qualified_universe.partial.csv",
        qualified_rows(rows),
    )


def write_complete_outputs(output_dir: Path, rows: List[dict]) -> None:
    atomic_write_csv(output_dir / "hunter_audit.csv", rows)
    atomic_write_csv(
        output_dir / "qualified_universe.csv",
        qualified_rows(rows),
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
    consecutive_transient_failures = 0
    started_at = time.monotonic()

    try:
        for index, candidate in enumerate(pending, start=1):
            ticker = candidate["Ticker"]
            try:
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
                    f"Retry: Yahoo 限流；下次從此處接續 ({str(exc)[:120]})",
                )
                stopped_early = True
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

    summarize_reasons(rows)
    return 0 if complete else 5


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    sys.exit(main())
