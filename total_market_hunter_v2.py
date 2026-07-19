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
HUNTER_POLICY_VERSION = "2026-07-industry-models-v7"

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
        raise ValueError("è«‹æä¾›çœŸæ­£çš„è¯çµ¡ä¿¡ç®±ï¼Œä¾‹å¦‚ name@gmail.comã€‚")
    try:
        email.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(
            "è¯çµ¡ä¿¡ç®±ä¸èƒ½åŒ…å«ä¸­æ–‡ï¼›è«‹æŠŠã€Œä½ çš„ä¿¡ç®±@gmail.comã€æ›æˆçœŸæ­£çš„è‹±æ–‡ä¿¡ç®±ã€‚"
        ) from exc
    if any(char.isspace() for char in email):
        raise ValueError("è¯çµ¡ä¿¡ç®±ä¸èƒ½åŒ…å«ç©ºæ ¼ã€‚")
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
        print(f"[SEC] å–å¾— {len(parsed):,} ç­† ticker / CIK / exchange å°ç…§ã€‚")
        return parsed
    except Exception as exc:
        if isinstance(cached, dict) and isinstance(cached.get("payload"), dict):
            print(f"[SEC] é€£ç·šå¤±æ•—ï¼Œæ”¹ç”¨èˆŠå¿«å–ï¼š{exc}")
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
            "SecurityClas×vîÚ$z{-®éÜj×÷"F†—24”² ¢¢&÷u²%6V7W&—G”6Æ72%ÒÒ$4ôÔÔôåôE5ô”ädU%$TB ¢&÷u²%6V7W&—G”6Æ746öæf–FVæ6R%ÒÒ$ÔTD•TÒ ¢&÷u²%6V7W&—G”6Æ74Wf–FVæ6U6÷W&6R%ÒÒ€¢b'·&÷rævWB‚u6V7W&—G”6Æ74Wf–FVæ6U6÷W&6Rr—Ó²–æfW'&VB6öÖÖöã¢¶–æfW&Væ6WÒ ¢¢–æfW'&VB³Ò¢Vç&W6öÇfVBÓÒ¢&–çB€¢b%µ4T26†&RÖ6Æ726†V6µÒ&W6öÇfVC×·&W6öÇfVC¢ÇÓ² ¢b&–æfW'&VC×¶–æfW'&VC¢ÇÓ²Vç&W6öÇfVC×·Vç&W6öÇfVC¢ÇÓ²4”·3×¶ÆVâ‡F&vWG2“¢ÇÒâ ¢¢&WGW&âF§W7FV@  ¦FVb&W6öÇfU÷6†&Uö6Æ76W2‡&÷w3¢Æ—7E¶F–7EÒ’ÓâÆ—7E¶F–7EÓ ¢""$f–Â6Æ÷6VBöâæöâÖ6öÖÖöâE72æB6†ö÷6RGWÆ–6FR4”·2'’Æ—V–F—G’â"" ¢F§W7FVBÒ¶F–7B‡&÷r’f÷"&÷r–â&÷w5Ğ¢f÷"&÷r–âF§W7FVC ¢–b&÷rævWB‚%7FGW2"’Ò%72# ¢6öçF–çVP¢6V7W&—G•ö6Æ72Ò7G"‡&÷rævWB‚%6V7W&—G”6Æ72"’÷"%Tä´äõtâ"¢–b6V7W&—G•ö6Æ72ÓÒ$U„4ÅTDTEôäôåô4ôÔÔôâ# ¢&÷u²%7FGW2%ÒÒ$G&÷¢öff–6–ÂWf–FVæ6R–FVçF–f–W2æöâÖ6öÖÖöâ6V7W&—G’ ¢&÷u²%6†&T6Æ756VÆV7F–öå&V6öâ%ÒÒ&÷rævWB‚%6V7W&—G”6Æ74Wf–FVæ6U6÷W&6R"Â""¢VÆ–b6V7W&—G•ö6Æ72–â²$Ô$”uTõU5ôE2"Â%Tå$U4ôÅdTEôE2'Ó ¢&÷u²%7FGW2%ÒÒ%&Wf–Ws¢E26öÖÖöâ÷&VfW'&VB6Æ72—2Vç&W6öÇfVB ¢&÷u²%6†&T6Æ756VÆV7F–öå&V6öâ%ÒÒ&÷rævWB‚%6V7W&—G”6Æ74Wf–FVæ6U6÷W&6R"Â""¢VÆ–b6V7W&—G•ö6Æ72ÓÒ%Tä´äõtâ# ¢&÷u²%7FGW2%ÒÒ%&Wf–Ws¢öff–6–Â6öÖÖöâÖWV—G’6Æ72Wf–FVæ6R—2Ö—76–ær ¢&÷u²%6†&T6Æ756VÆV7F–öå&V6öâ%ÒÒ&÷rævWB‚%6V7W&—G”6Æ74Wf–FVæ6U6÷W&6R"Â"" ¢75öw&÷W3¢F–7E·7G"ÂÆ—7E¶F–7EÕÒÒ·Ğ¢f÷"&÷r–âF§W7FVC ¢–b&÷rævWB‚%7FGW2"’Ò%72# ¢6öçF–çVP¢6–²Ò7G"‡&÷rævWB‚$4”²"’÷"""’ç&WÆ6R‚"ã"Â""’ç¦f–ÆÂƒ¢75öw&÷W2ç6WFFVfVÇB†6–²ÂµÒ’æVæB‡&÷r ¢FVb6VÆV7F–öåö¶W’‡&÷s¢F–7B’ÓâGWÆU¶fÆöBÂfÆöBÂ7G%Ó ¢G'“ ¢Æ—V–F—G’ÒfÆöB‡&÷rævWB‚$fW&vTF–Ç”FöÆÆ%föÇVÖUôÒ"’¢W†6WB…G—TW'&÷"ÂfÇVTW'&÷"“ ¢Æ—V–F—G’ÒÓã ¢G'“ ¢Ö&¶WEö6ÒfÆöB‡&÷rævWB‚$Ö&¶WD6ô""’¢W†6WB…G—TW'&÷"ÂfÇVTW'&÷"“ ¢Ö&¶WEö6ÒÓã ¢–bæ÷BÖF‚æ—6f–æ—FR†Æ—V–F—G’“ ¢Æ—V–F—G’ÒÓã ¢–bæ÷BÖF‚æ—6f–æ—FR†Ö&¶WEö6“ ¢Ö&¶WEö6ÒÓã ¢&WGW&âÆ—V–F—G’ÂÖ&¶WEö6Â7G"‡&÷rævWB‚%F–6¶W""’÷""" ¢f÷"6–²Âw&÷W–â75öw&÷W2æ—FV×2‚“ ¢–bÆVâ†w&÷W’ÓÒ ¢w&÷W³Õ²%6†&T6Æ756VÆV7F–öå&V6öâ%ÒÒ$öæÇ’VÆ–v–&ÆRÆ—7FVB6Æ72f÷"4”² ¢6öçF–çVP¢6öÖÖöâÒ°¢&÷p¢f÷"&÷r–âw&÷W ¢–b&÷rævWB‚%6V7W&—G”6Æ72"’–â4ôÔÔôåõ4T5U$•E•ô4Ä54U0¢Ğ¢–bæ÷B6öÖÖöã ¢f÷"&÷r–âw&÷W ¢&÷u²%7FGW2%ÒÒ%&Wf–Ws¢GWÆ–6FR4”²6†&R6Æ76W2&RVç&W6öÇfVB ¢&÷u²%6†&T6Æ756VÆV7F–öå&V6öâ%ÒÒ€¢$æòÆ—7FVB6Æ72†2öff–6–Â6öÖÖöâÖWV—G’Wf–FVæ6R ¢¢6öçF–çVP¢6VÆV7FVBÒÖ‚†6öÖÖöâÂ¶W“×6VÆV7F–öåö¶W’¢6VÆV7FVE÷F–6¶W"Ò7G"‡6VÆV7FVBævWB‚%F–6¶W""’÷"""¢6VÆV7FVE²%6†&T6Æ756VÆV7F–öå&V6öâ%ÒÒ€¢$öff–6–Â6öÖÖöâÖWV—G’6Æ72v—F‚†–v†W7BfW&vRF–Ç’FöÆÆ"föÇVÖR ¢¢f÷"&÷r–âw&÷W ¢–b&÷r—26VÆV7FVC ¢6öçF–çVP¢&÷u²%7FGW2%ÒÒb$G&÷¢GWÆ–6FR4”²6†&R6Æ73²6VÆV7FVB·6VÆV7FVE÷F–6¶W'Ò ¢&÷u²%6†&T6Æ756VÆV7F–öå&V6öâ%ÒÒ€¢b%6VÆV7FVB·6VÆV7FVE÷F–6¶W'ÒW6–æröff–6–Â6Æ72Wf–FVæ6RæBÆ—V–F—G’ ¢¢&WGW&âF§W7FV@  ¦FVbVÆ–f–VE÷&÷w2‡&÷w3¢Æ—7E¶F–7EÒ’ÓâÆ—7E¶F–7EÓ ¢&W6öÇfVE÷&÷w2Ò&W6öÇfU÷6†&Uö6Æ76W2†Ç•÷VW%öÖ&v–å÷'VÆW2‡&÷w2’¢76W2Ò·&÷rf÷"&÷r–â&W6öÇfVE÷&÷w2–b&÷rævWB‚%7FGW2"’ÓÒ%72%Ğ¢76W2ç6÷'B†¶W“ÖÆÖ&F&÷s¢fÆöB‡&÷rævWB‚$Ö&¶WD6ô""’÷"’Â&WfW'6SÕG'VR¢&WGW&â76W0 Ğ Ğ¦FVbw&—FU÷'F–Åö÷WGWG2†÷WGWEöF—#¢F‚Â&÷w3¢Æ—7E¶F–7EÒ’ÓâæöæS ¢&W6öÇfVE÷&÷w2Ò&W6öÇfU÷6†&Uö6Æ76W2†Ç•÷VW%öÖ&v–å÷'VÆW2‡&÷w2’¢FöÖ–5÷w&—FUö77b†÷WGWEöF—"ò&‡VçFW%öVF—Bç'F–Âæ77b"Â&W6öÇfVE÷&÷w2¢FöÖ–5÷w&—FUö77b€Ğ¢÷WGWEöF—"ò'VÆ–f–VE÷Væ—fW'6Rç'F–Âæ77b"ÀĞ¢VÆ–f–VE÷&÷w2‡&W6öÇfVE÷&÷w2’ÀĞ¢Ğ Ğ Ğ¦FVbw&—FUö6ö×ÆWFUö÷WGWG2†÷WGWEöF—#¢F‚Â&÷w3¢Æ—7E¶F–7EÒ’ÓâæöæS ¢&W6öÇfVE÷&÷w2Ò&W6öÇfU÷6†&Uö6Æ76W2†Ç•÷VW%öÖ&v–å÷'VÆW2‡&÷w2’¢FöÖ–5÷w&—FUö77b†÷WGWEöF—"ò&‡VçFW%öVF—Bæ77b"Â&W6öÇfVE÷&÷w2Ğ¢FöÖ–5÷w&—FUö77b€Ğ¢÷WGWEöF—"ò'VÆ–f–VE÷Væ—fW'6Ræ77b"ÀĞ¢VÆ–f–VE÷&÷w2‡&W6öÇfVE÷&÷w2’ÀĞ¢Ğ¢f÷"'F–ÅöæÖR–â€Ğ¢&‡VçFW%öVF—Bç'F–Âæ77b"ÀĞ¢'VÆ–f–VE÷Væ—fW'6Rç'F–Âæ77b"ÀĞ¢“ Ğ¢'F–Å÷F‚Ò÷WGWEöF—"ò'F–ÅöæÖPĞ¢–b'F–Å÷F‚æW†—7G2‚“ Ğ¢'F–Å÷F‚çVæÆ–æ²‚Ğ Ğ Ğ¦FVb'V–ÆE÷'6W"‚’Óâ&w'6Rä&wVÖVçE'6W# Ğ¢'6W"Ò&w'6Rä&wVÖVçE'6W"€Ğ¢FW67&—F–öãÒ$6öç6W'fF—fRÂ&W7VÖ&ÆRU2WV—G’&R×67&VVæW"â Ğ¢Ğ¢'6W"æFEö&wVÖVçB€Ğ¢"ÒÖVÖ–Â"ÀĞ¢FVfVÇCÖ÷2æVçf—&öâævWB‚%U4U%ôTÔ”Â"Â""’ÀĞ¢†VÇÒ$6öçF7BVÖ–ÂW6VB–âF†R4T2W6W"ÔvVçBâ"ÀĞ¢Ğ¢'6W"æFEö&wVÖVçB€Ğ¢"ÒÖ÷WGWBÖF—""ÀĞ¢FVfVÇCÒ"â"ÀĞ¢†VÇÒ$F—&V7F÷'’f÷"66†RÂ6†V6·ö–çG2æB55b÷WGWG2â"ÀĞ¢Ğ¢'6W"æFEö&wVÖVçB€Ğ¢"Ò×66âÖÆ–Ö—B"ÀĞ¢G—SÖ–çBÀĞ¢FVfVÇCÓÀĞ¢†VÇÒ%&ö6W72öæÇ’F†Rf—'7Bâ67&VVæW"6æF–FFW2ƒÒÆÂ’â"ÀĞ¢Ğ¢'6W"æFEö&wVÖVçB€Ğ¢"ÒÖg&W6‚"ÀĞ¢7F–öãÒ'7F÷&U÷G'VR"ÀĞ¢†VÇÒ$–væ÷&R&WVW7B÷&W7VÇBg&W6†æW72æB&Vg&W6‚&VÖ÷FRFFâ"ÀĞ¢Ğ¢'6W"æFEö&wVÖVçB€Ğ¢"Ò×–†öòÖ–çFW'fÂ"ÀĞ¢G—SÖfÆöBÀĞ¢FVfVÇCÔDTdTÅEõ”„ôõô”åDU%dÅõ4T4ôäE2ÀĞ¢†VÇÒ$Ö–æ–×VÒ6V6öæG2&WGvVVâFWF–ÆVB–†öò&WVW7G2â"ÀĞ¢Ğ¢'6W"æFEö&wVÖVçB€Ğ¢"Ò×67&VVæW"Ö–çFW'fÂ"ÀĞ¢G—SÖfÆöBÀĞ¢FVfVÇCÔDTdTÅEõ45$TTäU%ô”åDU%dÅõ4T4ôäE2ÀĞ¢†VÇÒ$Ö–æ–×VÒ6V6öæG2&WGvVVâ–†öò67&VVæW"vW2â"ÀĞ¢Ğ¢'6W"æFEö&wVÖVçB€Ğ¢"Ò×&WV—&RÖ–ç7F—GWF–öæÂÖ÷væW'6†—"ÀĞ¢7F–öãÒ'7F÷&U÷G'VR"ÀĞ¢†VÇÒ$Væ&ÆRF†R÷F–öæÂCRR–ç7F—GWF–öæÂ÷væW'6†—†&Bf–ÇFW"â"ÀĞ¢Ğ¢'6W"æFEö&wVÖVçB€Ğ¢"ÒÖVæ&ÆR×RÖf–ÇFW""ÀĞ¢7F–öãÒ'7F÷&U÷G'VR"ÀĞ¢†VÇÒ$Væ&ÆRdRõ&WfVçVS²Ö’FBöæR–†öò7FFVÖVçB&WVW7BW"7W'f—f÷"â"ÀĞ¢Ğ¢'6W"æFEö&wVÖVçB€Ğ¢"ÒÖÖ‚×vW2"ÀĞ¢G—SÖ–çBÀĞ¢FVfVÇCÓ#ÀĞ¢†VÇÒ%6fWG’Æ–Ö—Bf÷"–†öò67&VVæW"v–æF–öââ"ÀĞ¢Ğ¢'6W"æFEö&wVÖVçB€Ğ¢"ÒÖÖ‚×G&ç6–VçBÖf–ÇW&W2"ÀĞ¢G—SÖ–çBÀĞ¢FVfVÇCÔDTdTÅEôÔ…õE$å4”TåEôd”ÅU$U2ÀĞ¢†VÇÒ%7F÷gFW"F†—2Öç’6öç6V7WF—fRFV×÷&'’æWGv÷&²f–ÇW&W2â"ÀĞ¢Ğ¢'6W"æFEö&wVÖVçB€Ğ¢"Ò×&WVW7B×F–ÖV÷WB"ÀĞ¢G—SÖfÆöBÀĞ¢FVfVÇCÔDTdTÅEõ$UTU5EõD”ÔTõUEõ4T4ôäE2ÀĞ¢†VÇÒ$†&BF–ÖV÷WB–â6V6öæG2f÷"V6‚FWF–ÆVB–†öò&WVW7Bâ"ÀĞ¢Ğ¢&WGW&â'6W Ğ Ğ Ğ¦FVb'Vâ†&w3¢&w'6RäæÖW76R’Óâ–çC Ğ¢G'“ Ğ¢&w2æVÖ–ÂÒfÆ–FFU÷6V5ö6öçF7EöVÖ–Â†&w2æVÖ–ÂĞ¢W†6WBfÇVTW'&÷"2W†3 Ğ¢&–çB†b%¾ŠŠŞZé®˜ÊşŠªEÒ¶W†7Ò"Ğ¢&WGW&â Ğ Ğ¢6öæf–rÒ‡VçFW$6öæf–r€Ğ¢&WV—&Uö–ç7F—GWF–öæÅö÷væW'6†—Ö&w2ç&WV—&Uö–ç7F—GWF–öæÅö÷væW'6†—ÀĞ¢Væ&ÆU÷Uöf–ÇFW#Ö&w2æVæ&ÆU÷Uöf–ÇFW"ÀĞ¢Ğ¢6–væGW&RÒ6öæf–rç6–væGW&R‚Ğ¢÷WGWEöF—"ÒF‚†&w2æ÷WGWEöF—"’æW‡æGW6W"‚’ç&W6öÇfR‚Ğ¢66†UöF—"Ò÷WGWEöF—"ò"æ‡VçFW%ö66†R Ğ¢6†V6·ö–çE÷F‚Ò÷WGWEöF—"ò&‡VçFW%ö6†V6·ö–çBæ§6öæÂ Ğ¢÷WGWEöF—"æÖ¶F—"‡&VçG3ÕG'VRÂW†—7Eöö³ÕG'VRĞ Ğ¢&–çB‚#Ò"¢s"Ğ¢&–çB‚$Ç†Væv–æR6öç6W'fF—fRÖ&¶WB‡VçFW""Ğ¢&–çB†b$÷WGWC¢¶÷WGWEöF—'Ò"Ğ¢&–çB†b$6öæf–r6–væGW&S¢·6–væGW&WÒ"Ğ¢&–çB€Ğ¢%öÆ–7“¢6WVVçF–ÂFWF–Â&WVW7G2Â7F÷Ööâ×&FRÖÆ–Ö—BÂ Ğ¢&66†R²6†V6·ö–çB&W7VÖR Ğ¢Ğ¢&–çB€Ğ¢b$÷F–öæÂf–ÇFW'3¢–ç7F—GWF–öæÃ×¶6öæf–rç&WV—&Uö–ç7F—GWF–öæÅö÷væW'6†—ÒÂ Ğ¢b%dS×¶6öæf–ræVæ&ÆU÷Uöf–ÇFW'Ò Ğ¢Ğ¢&–çB‚#Ò"¢s"Ğ Ğ¢6V5öÖÒvWE÷6V5÷F–6¶W%öÖ†&w2æVÖ–ÂÂ66†UöF—"Âg&W6ƒÖ&w2æg&W6‚¢G'“ ¢6V7W&—G•öF—&V7F÷'’ÒvWEöæ6F÷6V7W&—G•öF—&V7F÷'’€¢&w2æVÖ–ÂÀ¢66†UöF—"À¢g&W6ƒÖ&w2æg&W6‚À¢¢W†6WB'VçF–ÖTW'&÷"2W†3 ¢&–çB†b%¾ZèXZXÎjÚ%Ò¶W†7Ò"¢&WGW&â0¢67&VVæW%÷6W"Ò&WVW7E6W"†&w2ç67&VVæW%ö–çFW'fÂ¢–†öõ÷6W"Ò&WVW7E6W"†&w2ç–†öõö–çFW'fÂĞ Ğ¢G'“ Ğ¢V÷FW2ÒvWE÷–†öõ÷67&VVæW%ö6æF–FFW2€Ğ¢6öæf–rÀĞ¢66†UöF—"ÀĞ¢67&VVæW%÷6W"ÀĞ¢g&W6ƒÖ&w2æg&W6‚ÀĞ¢Ö…÷vW3ÖÖ‚ƒÂ&w2æÖ…÷vW2’ÀĞ¢Ğ¢W†6WB…&FTÆ–Ö—E7F÷ÂFV×÷&'”FFW'&÷"Â'VçF–ÖTW'&÷"’2W†3 Ğ¢&–çB†b%¾XÎjÚ%ÒxJk9^[»®z¸²–†öòš	zúYŞYjîûÉ§¶W†7Ò"Ğ¢&WGW&â0Ğ Ğ¢6æF–FFW2Ò&Vf–ÇFW%ö6æF–FFW2€¢V÷FW2À¢6V5öÖÀ¢6V7W&—G•öF—&V7F÷'“×6V7W&—G•öF—&V7F÷'’À¢66åöÆ–Ö—CÖÖ‚ƒÂ&w2ç66åöÆ–Ö—B’À¢¢–bæ÷B6æF–FFW3 Ğ¢&–çB‚%¾XÎjÚ%Òš	zú[èÎk).iÈX	˜ûÉ¾KˆŞiÈ>Šhn‰8¾iz.iÈ’VÆ–f–VE÷Væ—fW'6Ræ77n8""Ğ¢&WGW&â@Ğ Ğ¢6†V6·ö–çBÒ·Ò–b&w2æg&W6‚VÇ6RÆöEö6†V6·ö–çB†6†V6·ö–çE÷F‚Â6–væGW&RĞ¢&W7VÇG5ö'•÷F–6¶W"Ò°Ğ¢F–6¶W#¢&÷pĞ¢f÷"F–6¶W"Â&÷r–â6†V6·ö–çBæ—FV×2‚Ğ¢–b6ö×ÆWFVE÷&W7VÇEö—5ög&W6‚‡&÷rĞ¢ĞĞ¢VæF–ærÒ°Ğ¢6æF–FFPĞ¢f÷"6æF–FFR–â6æF–FFW0Ğ¢–b6æF–FFU²%F–6¶W"%Òæ÷B–â&W7VÇG5ö'•÷F–6¶W Ğ¢ĞĞ¢&–çB€Ğ¢b%¾ŠˆyZµÒ–†öòš	zú“×¶ÆVâ‡V÷FW2“¢ÇŞûÉµ4T2[Ş›Ø®[èÃ×¶ÆVâ†6æF–FFW2“¢ÇŞûÉ² Ğ¢b.Xúş˜xŞyJ{YiéÃ×¶ÆVâ‡&W7VÇG5ö'•÷F–6¶W"“¢ÇŞûÉ¾[è^h©>Š›>{K‹8~ii“×¶ÆVâ‡VæF–ær“¢ÇÒ Ğ¢Ğ Ğ¢7F÷VEöV&Ç’ÒfÇ6PĞ¢†&E÷F–ÖV÷WE÷7F÷ÒfÇ6PĞ¢6öç6V7WF—fU÷G&ç6–VçEöf–ÇW&W2Ò Ğ¢7F'FVEöBÒF–ÖRæÖöæ÷Föæ–2‚Ğ Ğ¢G'“ Ğ¢f÷"–æFW‚Â6æF–FFR–âVçVÖW&FR‡VæF–ærÂ7F'CÓ“ ¢F–6¶W"Ò6æF–FFU²%F–6¶W"%Ğ¢G'“ ¢–b6æF–FFRævWB‚%6V7W&—G”6Æ72"’ÓÒ$U„4ÅTDTEôäôåô4ôÔÔôâ# ¢–æfòÂW6VEö66†RÒ·ÒÂfÇ6P¢VÇ6S ¢–æfòÂW6VEö66†RÒfWF6…÷F–6¶W%ö–æfò€¢F–6¶W"À¢66†UöF—"À¢–†öõ÷6W"À¢g&W6ƒÖ&w2æg&W6‚À¢F–ÖV÷WE÷6V6öæG3ÖÖ‚ƒRãÂ&w2ç&WVW7E÷F–ÖV÷WB’À¢¢&W7VÇBÒWfÇVFUö6æF–FFR€¢6æF–FFRÀ¢–æfòÀ¢6öæf–rÀĞ¢66†UöF—"ÀĞ¢–†öõ÷6W"ÀĞ¢W6VEö66†RÀĞ¢&WVW7E÷F–ÖV÷WE÷6V6öæG3ÖÖ‚ƒRãÂ&w2ç&WVW7E÷F–ÖV÷WB’ÀĞ¢Ğ¢6öç6V7WF—fU÷G&ç6–VçEöf–ÇW&W2Ò Ğ¢W†6WB&FTÆ–Ö—E7F÷2W†3 Ğ¢&W7VÇBÒÖ¶U÷&W7VÇB€Ğ¢6æF–FFRÀĞ¢6öæf–rÀĞ¢b%&WG'“¢–†öò™™kXh‰nŠ¸¾k.˜îi˜.ûÉ¾Kˆ¾jÊ[éîjÚN‰™^hê^{¨Â‡·7G"†W†2•³£#×Ò’"ÀĞ¢Ğ¢7F÷VEöV&Ç’ÒG'VPĞ¢W†6WB&WVW7EF–ÖV÷WE7F÷2W†3 Ğ¢&W7VÇBÒÖ¶U÷&W7VÇB€Ğ¢6æF–FFRÀĞ¢6öæf–rÀĞ¢b%&WG'“¢–†öòŠ¸¾k.˜îi˜.ûÉ¾Kˆ¾jÊ[éîjÚN‰™^hê^{¨Â‡·7G"†W†2•³£#×Ò’"ÀĞ¢Ğ¢7F÷VEöV&Ç’ÒG'VPĞ¢†&E÷F–ÖV÷WE÷7F÷ÒG'VPĞ¢W†6WBFV×÷&'”FFW'&÷"2W†3 Ğ¢6öç6V7WF—fU÷G&ç6–VçEöf–ÇW&W2³ÒĞ¢&W7VÇBÒÖ¶U÷&W7VÇB€Ğ¢6æF–FFRÀĞ¢6öæf–rÀĞ¢b%&WG'“¢iª¾i˜.h
~{k.‹zş˜ÊşŠªB‡·7G"†W†2•³£#×Ò’"ÀĞ¢Ğ¢–b6öç6V7WF—fU÷G&ç6–VçEöf–ÇW&W2ãÒÖ‚€Ğ¢ÀĞ¢&w2æÖ…÷G&ç6–VçEöf–ÇW&W2ÀĞ¢“ Ğ¢7F÷VEöV&Ç’ÒG'VPĞ¢W†6WBW†6WF–öâ2W†3 Ğ¢&W7VÇBÒÖ¶U÷&W7VÇB€Ğ¢6æF–FFRÀĞ¢6öæf–rÀĞ¢b%&Wf–Ws¢™Ùîiª¾i˜.h
~‹8~ii˜ÊşŠªB‡·G—R†W†2’åõöæÖUõ÷Ó¢·7G"†W†2•³£#×Ò’"ÀĞ¢Ğ¢6öç6V7WF—fU÷G&ç6–VçEöf–ÇW&W2Ò Ğ Ğ¢&W7VÇG5ö'•÷F–6¶W%·F–6¶W%ÒÒ&W7VÇ@Ğ¢VæEö6†V6·ö–çB†6†V6·ö–çE÷F‚Â&W7VÇBĞ Ğ¢&ö6W76VE÷&÷w2Ò&÷w5öf÷%ö6æF–FFW2†6æF–FFW2Â&W7VÇG5ö'•÷F–6¶W"Ğ¢–b–æFW‚R#RÓÒ÷"7F÷VEöV&Ç’÷"–æFW‚ÓÒÆVâ‡VæF–ær“ Ğ¢w&—FU÷'F–Åö÷WGWG2†÷WGWEöF—"Â&ö6W76VE÷&÷w2Ğ Ğ¢–b–æFW‚RÓÒ÷"7F÷VEöV&Ç’÷"–æFW‚ÓÒÆVâ‡VæF–ær“ Ğ¢VÆ6VBÒÖ‚‡F–ÖRæÖöæ÷Föæ–2‚’Ò7F'FVEöBÂãĞ¢&–çB€Ğ¢b%¾˜.[ªeÒiÊÎjÊ¶–æFW‡Ò÷¶ÆVâ‡VæF–ær—ŞûÉ² Ğ¢b.{‹ŞZèÎh‰¶ÆVâ‡&ö6W76VE÷&÷w2—Ò÷¶ÆVâ†6æF–FFW2—ŞûÉ² Ğ¢b%73×¶ÆVâ‡VÆ–f–VE÷&÷w2‡&ö6W76VE÷&÷w2’—ŞûÉ² Ğ¢b'¶–æFW‚òVÆ6VC¢ã&gÒj©Bşzy" Ğ¢Ğ Ğ¢–b7F÷VEöV&Ç“ Ğ¢&–çB€Ğ¢%¾ZèXZXÎjÚ%Ò[{.KùŞyY’6†V6·ö–çBˆˆr'F–Â55n8" Ğ¢.Š¸¾zˆŞ[èÎyJy»YÎhÈ~KºN˜xŞ‹yûÉ¾iz.iÈZèÎi[BVÆ–f–VE÷Væ—fW'6Ræ77biÊ®Š*¾Šhn‰8¾8" Ğ¢Ğ¢–b†&E÷F–ÖV÷WE÷7F÷ Ğ¢26öÖR–†öòö7W&Â&6¶VæG2¶VWæöâÖFVÖöâ†VÇW"F‡&VG2Æ—fPĞ¢2gFW"F†R&WVW7BF‡&VBF–ÖW2÷WBâF†R6†V6·ö–çBæB'F–ÀĞ¢255b&RÇ&VG’GW&&ÆRÂ6òf÷&6R&ö6W72W†—B–ç7FVBö`Ğ¢2v—F–ærf÷&WfW"GW&–ær–çFW'&WFW"6‡WFF÷vâàĞ¢7—2ç7FF÷WBæfÇW6‚‚Ğ¢7—2ç7FFW'"æfÇW6‚‚Ğ¢÷2åöW†—BƒRĞ¢'&V°Ğ¢W†6WB¶W–&ö&D–çFW''WC Ğ¢7F÷VEöV&Ç’ÒG'VPĞ¢&–çB‚%Æå¾KÛşyJˆ^KŠŞikuÒ[{.KùŞyY’6†V6·ö–çNûÉ¾Kˆ¾jÊiÈ>hê^{¨Î8""Ğ Ğ¢&÷w2Ò&÷w5öf÷%ö6æF–FFW2†6æF–FFW2Â&W7VÇG5ö'•÷F–6¶W"Ğ¢6ö×ÆWFRÒ€Ğ¢æ÷B7F÷VEöV&ÇĞ¢æBÆVâ‡&÷w2’ÓÒÆVâ†6æF–FFW2Ğ¢æBæ÷Bç’‡7G"‡&÷rævWB‚%7FGW2"’÷"""’ç7F'G7v—F‚‚%&WG'“¢"’f÷"&÷r–â&÷w2Ğ¢¢–b6ö×ÆWFS ¢&÷w2ÒVç&–6…öÖ&–wV÷W5öG5ög&öÕ÷6V2€¢&÷w2À¢&w2æVÖ–ÂÀ¢66†UöF—"À¢¢w&—FUö6ö×ÆWFUö÷WGWG2†÷WGWEöF—"Â&÷w2¢&–çB€Ğ¢b%¾ZèÎh‰Ò¶ÆVâ‡VÆ–f–VE÷&÷w2‡&÷w2’—Òj©N˜	®˜îûÉ² Ğ¢.[{.XéşZÙi»NikVÆ–f–VE÷Væ—fW'6Ræ77bˆˆr‡VçFW%öVF—Bæ77n8" Ğ¢Ğ¢VÇ6S Ğ¢w&—FU÷'F–Åö÷WGWG2†÷WGWEöF—"Â&÷w2Ğ¢&–çB€Ğ¢%¾iÊ®ZèÎh‰ÒX8^i»Nik¢ç'F–Âæ77nûÉ² Ğ¢.iÈ[èÎKˆjÊZèÎi[BVÆ–f–VE÷Væ—fW'6Ræ77bKùŞhÈKˆŞŠè®8" Ğ¢Ğ Ğ¢7VÖÖ&—¦U÷&V6öç2‡&W6öÇfU÷6†&Uö6Æ76W2†Ç•÷VW%öÖ&v–å÷'VÆW2‡&÷w2’’¢&WGW&â–b6ö×ÆWFRVÇ6RPĞ Ğ Ğ¦FVbÖ–â‚’Óâ–çC Ğ¢&WGW&â'Vâ†'V–ÆE÷'6W"‚’ç'6Uö&w2‚’Ğ Ğ Ğ¦–bõöæÖUõòÓÒ%õöÖ–åõò# Ğ¢7—2æW†—B†Ö–â‚’Ğ