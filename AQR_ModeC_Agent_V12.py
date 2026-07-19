"""
=============================================================================
AQR Mode-C Agent V13 â€” 70/30 é•·æœŸåƒ¹å€¼ç ”ç©¶æ¡†æ¶
=============================================================================
ç”¨é€”ï¼š
1) è®€å–æ¯æœˆå…¨å¸‚å ´åˆç¯©æ¸…å–® qualified_universe.csvï¼ˆæ¬„ä½ï¼šTicker, CIKï¼‰
2) ä½¿ç”¨ SEC XBRL Company Facts å°é½Š TTM / æœ€æ–° 10-Kã€20-Fã€40-F / æœ€æ–° 10-Q
3) ç”¢å‡ºï¼š
   - mode_c_screen.csv              ï¼šå…¨é‡çµæ§‹åŒ–æ•¸æ“šç¸½è¡¨
   - mode_c_shortlist.csv           ï¼šåˆ†æ•¸å„ªå…ˆçš„é•·æœŸç ”ç©¶å€™é¸
   - mode_c_evidence_ledger.csv     ï¼špoint-in-time åŸå§‹è­‰æ“šèˆ‡è¡ç”Ÿè¡€ç·£
   - mode_c_report.md               ï¼šé•·æœŸåƒ¹å€¼ç ”ç©¶å ±å‘Š
   - mode_c_agent_payload.json      ï¼šäº¤çµ¦ LLM / Web Agent åšç‰©ç†é™åˆ¶é©—è­‰çš„ä»»å‹™åŒ…

æ ¸å¿ƒä¿®æ­£ï¼š
- å®Œç¾é‚„åŸç¨…å‹™åˆ©ç›Š (Tax Benefit)ï¼Œå¼·åˆ¶åŸ·è¡Œä¸‰é»å‹¾ç¨½é˜²æ­¢éç¶“å¸¸æ€§æç›Šæ¬ºé¨™ã€‚
- å°‡è»‹ç©ºæ°´ä½ (Short Interest & DTC) å¼·åˆ¶å¯«å…¥ CSV è­¦ç¤ºæ——æ¨™ã€‚
- å¾¹åº•é˜»çµ•ç§‘æŠ€å·¨é ­ (ç„¡å‚³çµ±å‚µå‹™) é€ æˆçš„ KeyError ç†”æ–·ã€‚
=============================================================================
"""


from __future__ import annotations


import concurrent.futures
import hashlib
import json
import logging
import math
import os
import re
import smtplib
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


import numpy as np
import pandas as pd
import requests
import yfinance as yf
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from mode_c_evidence import EVIDENCE_COLUMNS, GLOBAL_EVIDENCE_LEDGER
from mode_c_metric_contract import annotate_rows, finite_number
from mode_c_industry_models import (
    SPECIALIZED_MODEL_KEYS,
    IndustryModelEvaluation,
    assess_specialized_data_confidence,
    evaluate_industry_model,
)
from mode_c_routing import route_industry_model
from mode_c_research_priority import (
    CROSS_MODEL_CALIBRATION_STATUS,
    RESEARCH_PRIORITY_METHOD,
    annotate_research_priorities,
    global_research_queue,
    refresh_research_status,
    shortlist_by_model,
)


# ==============================================================================
# åŸºæœ¬è¨­å®š
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ModeC")


CACHE_FILE_SHARES = "local_shares_vector_cache.json"
QUALIFIED_UNIVERSE = "qualified_universe.csv"
OUTPUT_CSV = "mode_c_screen.csv"
OUTPUT_SHORTLIST_CSV = "mode_c_shortlist.csv"
OUTPUT_SHORTLIST_BY_MODEL_CSV = "mode_c_shortlist_by_model.csv"
OUTPUT_MD = "mode_c_report.md"
OUTPUT_JSON = "mode_c_agent_payload.json"
OUTPUT_EVIDENCE_CSV = "mode_c_evidence_ledger.csv"


# SEC Fair Access å®˜æ–¹ä¸Šé™æ˜¯ 10 req/sï¼›é€™è£¡ä¿å®ˆè¨­ 8ã€‚
SEC_MAX_CALLS_PER_SECOND = 8
SEC_TIMEOUT = 15
SEC_DATA_AVAILABILITY_LAG_MINUTES = 5
ANNUAL_FILING_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}


# ==============================================================================
# QQQ 40% + VOO 30% + æœ€å¤š 30% ä¸»å‹•é¸è‚¡ï¼šé•·æœŸåƒ¹å€¼æŠ•è³‡æ¡†æ¶
# ==============================================================================
MIN_LIQUIDITY_USD = 15_000_000
MIN_MARKET_CAP_B = 5.0
ICR_WARNING = 3.0
STRESS_ICR_MIN = 1.5
REVERSE_DCF_REQUIRED_RETURN = 0.10
SHORT_SQUEEZE_SI = 15.0
SHORT_SQUEEZE_DTC = 5.0
LOW_VALUATION_PERCENTILE = 5
ACTIVE_SLEEVE_LIMIT_PCT = 30.0
TARGET_SHORTLIST_SIZE = 12
MAX_PER_SECTOR = 0
STARTER_WEIGHT_PCT_TOTAL = 1.5
MAX_POSITION_WEIGHT_PCT_TOTAL = 3.0
MAX_SECTOR_WEIGHT_PCT_TOTAL = 9.0
MIN_LONG_TERM_SCORE = 60.0
RESEARCH_PRIORITY_SCORE = 70.0
SMALL_POSITION_SCORE = 75.0
HIGH_PRIORITY_SCORE = 80.0
ETF_TOP10_MIN_BUY_SCORE = 80.0
STARTER_WEIGHT_MIN_PCT_TOTAL = 1.0
MIN_DATA_CONFIDENCE = 70.0
MIN_INVENTORY_TO_REVENUE_PCT = 1.0
MIN_INVENTORY_TO_ASSETS_PCT = 1.0
MAX_DOMESTIC_CORE_FACT_AGE_DAYS = 240
MAX_FOREIGN_ANNUAL_FACT_AGE_DAYS = 550
MAX_CORE_FACT_AGE_DAYS = MAX_FOREIGN_ANNUAL_FACT_AGE_DAYS


# ==============================================================================
# å¿«å–
# ==============================================================================
_BULK_MARKET_DATA: Optional[pd.DataFrame] = None
_INFO_CACHE: Dict[str, dict] = {}
_RF_CACHE: Optional[float] = None




def load_json_cache(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}




def save_json_cache(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)




_VECTOR_CACHE = load_json_cache(CACHE_FILE_SHARES)


# ==============================================================================
# HTTP / Yahoo
# ==============================================================================


def create_retry_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=[401, 403, 429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": "ModeCQuantResearch/12.0 contact@example.com",
            "Accept": "application/json,text/plain,*/*",
        }
    )
    return session




def pre_fetch_all_market_data(
    tickers: List[str],
    period: str = "10y",
    batch_size: int = 100,
) -> None:
    global _BULK_MARKET_DATA
    tickers = sorted(set([t for t in tickers if t]))
    logger.info(f"æ‰¹é‡ä¸‹è¼‰åƒ¹æ ¼è³‡æ–™ï¼š{len(tickers)} æª”ï¼Œperiod={period}")
    frames = []
    size = max(1, int(batch_size))
    for start in range(0, len(tickers), size):
        batch = tickers[start : start + size]
        frame = yf.download(
            batch,
            period=period,
            actions=True,
            progress=False,
            auto_adjust=False,
            group_by="column",
            threads=True,
        )
        if frame is None or frame.empty:
            logger.warning("åƒ¹æ ¼æ‰¹æ¬¡ä¸‹è¼‰ç‚ºç©ºï¼š%s", ",".join(batch[:5]))
            continue
        if not isinstance(frame.columns, pd.MultiIndex):
            if len(batch) != 1:
                logger.warning("å¤šè‚¡ç¥¨åƒ¹æ ¼æ‰¹æ¬¡æ¬„ä½æ ¼å¼ç•°å¸¸ï¼Œç•¥éè©²æ‰¹æ¬¡")
                continue
            frame = frame.copy()
            frame.columns = pd.MultiIndex.from_tuples(
                [(str(column), batch[0]) for column in frame.columns]
            )
        frames.append(frame)
    _BULK_MARKET_DATA = (
        pd.concat(frames, axis=1).loc[:, lambda data: ~data.columns.duplicated()]
        if frames
        else pd.DataFrame()
    )




def get_cached_series(ticker: str, col: str) -> Optional[pd.Series]:
    if _BULK_MARKET_DATA is None or _BULK_MARKET_DATA.empty:
        return None
    try:
        if isinstance(_BULK_MARKET_DATA.columns, pd.MultiIndex):
            if (col, ticker) in _BULK_MARKET_DATA.columns:
                s = _BULK_MARKET_DATA[(col, ticker)].dropna()
                return s if not s.empty else None
        else:
            if col in _BULK_MARKET_DATA.columns:
                s = _BULK_MARKET_DATA[col].dropna()
                return s if not s.empty else None
    except Exception:
        return None
    return None


def split_adjusted_share_value(
    ticker: str,
    value: float,
    fact_end: Any,
    fact_filed_at: Any,
    concept: str,
    target_date: Any,
) -> Tuple[float, float]:
    """Convert a reported share fact to the target-date split basis."""
    if not math.isfinite(float(value)) or float(value) <= 0:
        return float(value), 1.0
    splits = get_cached_series(ticker, "Stock Splits")
    if splits is None or splits.empty:
        return float(value), 1.0
    start = pd.to_datetime(fact_end, utc=True, errors="coerce")
    target = pd.to_datetime(target_date, utc=True, errors="coerce")
    if pd.isna(start) or pd.isna(target) or target <= start:
        return float(value), 1.0

    # Weighted-average share facts filed after a split are normally recast to
    # that filing-date basis. Instant share facts retain their period-end basis.
    filed = pd.to_datetime(fact_filed_at, utc=True, errors="coerce")
    is_weighted_average = "WeightedAverageNumberOfShares" in str(concept or "")
    basis_start = max(start, filed) if is_weighted_average and pd.notna(filed) else start
    events = splits.copy()
    events.index = pd.to_datetime(events.index, utc=True, errors="coerce")
    events = pd.to_numeric(events, errors="coerce")
    events = events[(events.index > basis_start) & (events.index <= target) & (events > 0)]
    events = events[~np.isclose(events, 1.0)]
    if events.empty:
        return float(value), 1.0
    factor = float(events.prod())
    if not math.isfinite(factor) or factor <= 0 or math.isclose(factor, 1.0):
        return float(value), 1.0

    return float(value) * factor, factor




def get_price_asof(ticker: str, date_like: pd.Timestamp) -> float:
    close = get_cached_series(ticker, "Close")
    if close is None or close.empty:
        return np.nan
    date_like = pd.Timestamp(date_like).tz_localize(None)
    s = close.copy()
    s.index = pd.to_datetime(s.index).tz_localize(None)
    s = s[s.index <= date_like]
    if s.empty:
        return np.nan
    return float(s.iloc[-1])


def get_price_on_or_after(ticker: str, date_like: pd.Timestamp) -> float:
    close = get_cached_series(ticker, "Close")
    if close is None or close.empty:
        return np.nan
    date_like = pd.Timestamp(date_like).tz_localize(None).normalize()
    s = close.copy()
    s.index = pd.to_datetime(s.index).tz_localize(None)
    s = s[s.index >= date_like]
    return float(s.iloc[0]) if not s.empty else np.nan




def pre_fetch_all_info(tickers: List[str]) -> None:
    global _INFO_CACHE
    logger.info(f"æ‰¹é‡æŠ“å– yf.infoï¼š{len(tickers)} æª”")
    for i, t in enumerate(tickers, start=1):
        info_data = {}
        for attempt in range(2):
            try:
                info = yf.Ticker(t).info
                if isinstance(info, dict) and len(info) > 5:
                    info_data = dict(info)
                    break
            except Exception:
                if attempt == 0:
                    time.sleep(0.5)
        _INFO_CACHE[t] = info_data
        if i % 50 == 0:
            logger.info(f"  yf.info {i}/{len(tickers)}")
        time.sleep(0.05)




def safe_yf_info(ticker: str) -> dict:
    info = _INFO_CACHE.get(ticker, {}) or {}
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    if price:
        return info
    close = get_cached_series(ticker, "Close")
    if close is not None and not close.empty:
        info = dict(info)
        info.setdefault("currentPrice", float(close.iloc[-1]))
        info.setdefault("regularMarketPrice", float(close.iloc[-1]))
    return info


def hydrate_info_cache_from_verified_universe(df: pd.DataFrame) -> None:
    """Use monthly hunter identity metadata when a daily yf.info call is empty."""
    sec_to_yahoo_exchange = {
        "NASDAQ": "NMS",
        "NYSE": "NYQ",
        "NYSE AMERICAN": "ASE",
    }

    def text(value: Any) -> str:
        try:
            if pd.isna(value):
                return ""
        except (TypeError, ValueError):
            pass
        return str(value or "").strip()

    for _, row in df.iterrows():
        if text(row.get("Status")).upper() != "PASS":
            continue
        ticker = text(row.get("Ticker")).upper()
        cik = text(row.get("CIK")).replace(".0", "").zfill(10)
        sec_exchange = text(row.get("SECExchange")).upper()
        if not ticker or not cik.isdigit() or int(cik) <= 0:
            continue
        info = dict(_INFO_CACHE.get(ticker) or {})
        quote_type = text(row.get("QuoteType")).upper()
        exchange = text(row.get("Exchange")).upper()
        if not quote_type and sec_exchange in sec_to_yahoo_exchange:
            quote_type = "EQUITY"
        if not exchange:
            exchange = sec_to_yahoo_exchange.get(sec_exchange, "")
        fallback_fields = []
        for key, value in (
            ("quoteType", quote_type),
            ("exchange", exchange),
            ("sector", text(row.get("Sector"))),
            ("industry", text(row.get("Industry"))),
        ):
            if value and not info.get(key):
                info[key] = value
                fallback_fields.append(key)
        if fallback_fields:
            info["_verifiedUniverseMetadataFallback"] = True
            info["_verifiedUniverseMetadataFallbackFields"] = fallback_fields
        _INFO_CACHE[ticker] = info


# ==============================================================================
# SEC XBRL æŠ½å–å™¨
# ==============================================================================
class RateLimitedSession:
    def __init__(self, calls: int = SEC_MAX_CALLS_PER_SECOND, period: float = 1.0):
        self.calls = calls
        self.period = period
        self.lock = threading.Lock()
        self.timestamps: List[float] = []
        self._thread_local = threading.local()


    def _session(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = create_retry_session()
            self._thread_local.session = session
        return session


    def _wait_for_capacity(self) -> None:
        with self.lock:
            now = time.time()
            self.timestamps = [t for t in self.timestamps if now - t < self.period]
            if len(self.timestamps) >= self.calls:
                sleep_time = self.period - (now - self.timestamps[0])
                if sleep_time > 0:
                    time.sleep(sleep_time)
            self.timestamps.append(time.time())


    def get(self, url: str, headers: dict) -> Optional[requests.Response]:
        for attempt in range(5):
            self._wait_for_capacity()
            try:
                r = self._session().get(url, headers=headers, timeout=SEC_TIMEOUT)
                if r.status_code == 200:
                    return r
      ÷­xâÚ$z{-®éÜj×şjÈ®h‰niKîz›®ûÉ¾jŠYè¾Xú®yJ.yIşz	Nz›nX	˜ûÈÎKˆŞiŠşˆz®X¹^‹+~XZ^Šˆ®‰™ş8""ÀĞ¢'÷'FföÆ–õöÆ–Ö—G2#¢°Ğ¢'÷F&vWE÷7E÷F÷FÂ#¢CãÀĞ¢'föõ÷F&vWE÷7E÷F÷FÂ#¢3ãÀĞ¢&7F—fU÷6ÆVWfU÷&ævU÷7E÷F÷FÂ#¢³ãÂ5D•dUõ4ÄTUdUôÄ”Ô•Eõ5EÒÀĞ¢'7F'FW%÷vV–v‡E÷&ævU÷7E÷F÷FÂ#¢µ5D%DU%õtT”t…EôÔ”åõ5EõDõDÂÂ5D%DU%õtT”t…Eõ5EõDõDÅÒÀĞ¢&Ö…÷÷6—F–öå÷vV–v‡E÷7E÷F÷FÂ#¢Ô…õõ4•D”ôåõtT”t…Eõ5EõDõDÂÀĞ¢&Ö…÷6V7F÷%÷vV–v‡E÷7E÷F÷FÂ#¢Ô…õ4T5Dõ%õtT”t…Eõ5EõDõDÂÀĞ¢&Ö…öæÖW2#¢D$tUEõ4„õ%DÄ•5Eõ4•¤RÀĞ¢&Ö…öæÖW5÷W%÷6V7F÷"#¢Ô…õU%õ4T5Dõ"ÀĞ¢ÒÀĞ¢'&W6V&6…÷6†÷'FÆ—7B#¢·"åF–6¶W"f÷""–â&W7VÇG2–b"äÆöæuõFW&ÕôVÆ–v–&ÆUÒÀĞ¢'F6·2#¢µÒÀĞ¢ĞĞ¢f÷""–â&W7VÇG3 Ğ¢–bæ÷B"äÆöæuõFW&ÕôVÆ–v–&ÆS Ğ¢6öçF–çVPĞ¢–ÆöE²'F6·2%ÒæVæB€Ğ¢°Ğ¢'F–6¶W"#¢"åF–6¶W"ÀĞ¢'6V7F÷"#¢"å6V7F÷"ÀĞ¢&–æGW7G'’#¢"ä–æGW7G'’ÀĞ¢'66÷&–æuög&ÖWv÷&²#¢"å66÷&–æuôg&ÖWv÷&²ÀĞ¢&ÖöFVÅ÷&÷WFR#¢"äÖöFVÅõ&÷WFRÀĞ¢&–æGW7G'•öÖöFVÂ#¢°Ğ¢&¶W’#¢"ä–æGW7G'•ôÖöFVÅô¶W’ÀĞ¢&FV6—6–öâ#¢"ä–æGW7G'•ôÖöFVÅôFV6—6–öâÀĞ¢'66÷&R#¢"ä–æGW7G'•ôÖöFVÅõ66÷&RÀĞ¢&6÷fW&vU÷7B#¢"ä–æGW7G'•ôÖöFVÅô6÷fW&vRÀĞ¢&ÖWG&–72#¢§6öâæÆöG2‡"ä–æGW7G'•ôÖöFVÅôÖWG&–75ô¥4ôâ÷"'·Ò"’ÀĞ¢&6ö×öæVçG2#¢§6öâæÆöG2‡"ä–æGW7G'•ôÖöFVÅô6ö×öæVçG5ô¥4ôâ÷"'·Ò"’ÀĞ¢'v&æ–æw2#¢"ä–æGW7G'•ôÖöFVÅõv&æ–æw2ÀĞ¢&†&Eöf–ÇW&W2#¢"ä–æGW7G'•ôÖöFVÅô†&Eôf–ÇW&W2ÀĞ¢ÒÀĞ¢&FV6—6–öå÷7FFR#¢"äFV6—6–öåõ7FFRÀĞ¢&FFö6öæf–FVæ6U÷66÷&R#¢"äFFô6öæf–FVæ6Uõ66÷&RÀĞ¢&FFö6öæf–FVæ6U÷&V6öç2#¢"äFFô6öæf–FVæ6Uõ&V6öç2ÀĞ¢&Æöæu÷FW&Õ÷66÷&R#¢"äÆöæuõFW&Õõ66÷&RÀĞ¢'&W6V&6…ö7F–öâ#¢"å&W6V&6…ô7F–öâÀĞ¢'7VvvW7FVE÷7F'FW%÷vV–v‡E÷7E÷F÷FÂ#¢"å7VvvW7FVEõ7F'FW%õvV–v‡E÷7EõF÷FÂÀĞ¢&'W•÷F‡&W6†öÆG2#¢°Ğ¢&æ÷&ÖÅ÷7Fö6²#¢4ÔÄÅõõ4•D”ôåõ44õ$RÀĞ¢'ö÷%÷föõ÷F÷ó#¢UDeõDõôÔ”åô%U•õ44õ$RÀĞ¢ÒÀĞ¢&×W7E÷fW&–g’#¢"ävVçEõF6·2²°Ğ¢#âyJKˆXú^Š›Zú¾X{®h©^‹8~Š¹n›¹â"ÀĞ¢#"âZú¾X{®iÈ[Ë~XøŞikŠ¹n›¹â"ÀĞ¢#2âiˆîz+®X‰~X{¢F†W6—2ZKiXj)ŞK»b"ÀĞ¢#Bâ[»®z¸¾h+.ŠxşYû®k©bşjˆ.ŠxKˆh8^Z(2"ÀĞ¢#Râiú^jiÈik[›N[ªnyK>ZûÈƒÔ²ó#ÔbóCÔnûÈ8Õˆˆ~k9^Šª®ŠÚnŠˆ¢"ÀĞ¢#bâiú^jKˆ[›NXø®Kˆ[›Nˆ*i[zˆ˜x²"ÀĞ¢#râŠ™^KËY¹î‹;Î8Z)îy›Î8KÛ^‹;Î8ˆ*hşˆˆ~XhŞh©^‹8~zØ‹8~iÊÎ˜XŞ{Úâ"ÀĞ¢#‚âKº^iÈik‹8~iiiú^jiŠşY
nx+¢õdôòh‰XˆnXø®X˜ŞXØZJ~hÈˆ*"ÀĞ¢#’âˆº^[{.˜ş˜âUDbhÈiÈûÈÎŠª®iˆîx+®KÙ^K¸ŞXÎ[é~K‹¾X¹^Xªz+ÎûÉ¾ynyKKˆŞ‹k>X˜~™˜Ş{I¢"ÀĞ¢.ˆºRUDb˜xŞyh®š¹ûÈÎXúnXªRÓXˆnk®zÙn™hj«¾h‰n™˜ŞKØîK‹¾X¹^˜:KØŞûÈÎKˆŞ[é~X~Š9Ş[{.XøŞiŠYÊ‚VçBXˆni[‚"ÀĞ¢.Xªz+ÎX˜Şˆ{>[	zØ[è^KˆjÊ‹*ZûÈÎz+®Š¨ÒF†W6—>8Š›.yJ.jZŞ[yJ‚µ8ˆ*i[ˆˆ~KËXÎiÊ®h:XÉb"ÀĞ¢.jª.iú^[Ë~X‹n‹:>X{¢şjª.Šˆîj)ŞK»nûÉ®Xˆni[ƒÃc8[yJjŠYè¾zÎh
~š*™ª®Š{y›Î8‹8~iiKú[ø3Ãs8‹8~iÊÎ˜XŞ{ÚîZKhê~h‰bF†W6—2Š*¾ŠØXÒ"ÀĞ¢.jª.iú^KËXÎ˜X{®j)ŞK»nûÉ®Š›.yJ.jZŞj[ø>KËXÎ[{.š¹ikÎYynjˆ.Šxh8^Z(>ûÈÎh‰nš	iÉşZ˜ZÎKØîikÎiÈKØî™hj«²"ÀĞ¢ÒÀĞ¢&çVÖ&W'5÷Fõö6†ÆÆVævR#¢°Ğ¢%fÇVUõ66÷&R#¢"åfÇVUõ66÷&RÀĞ¢%VÆ—G•õ66÷&R#¢"åVÆ—G•õ66÷&RÀĞ¢$W‡V7FF–öç5õ66÷&R#¢"äW‡V7FF–öç5õ66÷&RÀĞ¢%&—6µõVæÇG’#¢"å&—6µõVæÇG’ÀĞ¢$6—FÅôÆÆö6F–öåõ66÷&R#¢"ä6—FÅôÆÆö6F–öåõ66÷&RÀĞ¢%$ô”5÷7B#¢"å$ô”5÷7BÀĞ¢%$ô4U÷7B#¢"å$ô4U÷7BÀĞ¢$Ö–çFVææ6Uô6W…ô"#¢"äÖ–çFVææ6Uô6W…ô"ÀĞ¢$w&÷wF…ô6W…ô"#¢"äw&÷wF…ô6W…ô"ÀĞ¢$6öç6W'fF—fUõ&VÅôd4eõ––VÆE÷7B#¢"ä6öç6W'fF—fUõ&VÅôd4eõ––VÆE÷7BÀĞ¢%&VÅôd4eõ÷6—F—fUõ–V'5óU’#¢"å&VÅôd4eõ÷6—F—fUõ–V'5óU’ÀĞ¢%6†&Uô6÷VçEô6†ævUó5•÷7B#¢"å6†&Uô6÷VçEô6†ævUó5•÷7BÀĞ¢%&VÅôd4eõ––VÆE÷7B#¢"å&VÅôd4eõ––VÆE÷7BÀĞ¢$UeôT$•DDó•õW&6VçF–ÆR#¢"äUeôT$•DDó•õW&6VçF–ÆRÀĞ¢$–×Æ–VEôT$•DDô4u%ó5•÷7B#¢"ä–×Æ–VEôT$•DDô4u%ó5•÷7BÀĞ¢$–×Æ–VEô4u%ôÆ–Ö—E÷7B#¢"ä–×Æ–VEô4u%ôÆ–Ö—E÷7BÀĞ¢$–×Æ–VEô4u%ô†VG&ööÕ÷7B#¢"ä–×Æ–VEô4u%ô†VG&ööÕ÷7BÀĞ¢%&WfW'6UôD4eõ&WV—&VEõ&WGW&å÷7B#¢"å&WfW'6UôD4eõ&WV—&VEõ&WGW&å÷7BÀĞ¢$T$•DDôG&vF÷våó3÷7B#¢"äT$•DDôG&vF÷våó3÷7BÀĞ¢%7G&W75ô”5%ó3‚#¢"å7G&W75ô”5%ó3‚ÀĞ¢%F÷FÅôFV'Eô"#¢"åF÷FÅôFV'Eô"ÀĞ¢$66…ô"#¢"ä66…ô"ÀĞ¢$æWEôFV'Eô"#¢"äæWEôFV'Eô"ÀĞ¢$FV'Eõ6÷W&6UôÖWF†öB#¢"äFV'Eõ6÷W&6UôÖWF†öBÀĞ¢$”5%ôÖWF†öB#¢"ä”5%ôÖWF†öBÀĞ¢$æWDFV'E÷Fõõ7G&W75ôT$•DDó3‚#¢"äæWDFV'E÷Fõõ7G&W75ôT$•DDó3‚ÀĞ¢%7G&W75õ&VÅôd4eó3ô"#¢"å7G&W75õ&VÅôd4eó3ô"ÀĞ¢$–çfVçF÷'•õ6–væÂ#¢"ä–çfVçF÷'•õ6–væÂÀĞ¢$E4•õ–õ•ô6†ævU÷7B#¢"äE4•õ–õ•ô6†ævU÷7BÀĞ¢$–æGW7G'•ôÖöFVÅõ66÷&R#¢"ä–æGW7G'•ôÖöFVÅõ66÷&RÀĞ¢$–æGW7G'•ôÖöFVÅô6÷fW&vR#¢"ä–æGW7G'•ôÖöFVÅô6÷fW&vRÀĞ¢$–æGW7G'•ôÖöFVÅôÖWG&–75ô¥4ôâ#¢"ä–æGW7G'•ôÖöFVÅôÖWG&–75ô¥4ôâÀĞ¢ÒÀĞ¢&ÖWG&–5öÖWFFF#¢ÖWG&–5öÖWFFFö'•÷F–6¶W"ævWB‡"åF–6¶W"Â·Ò’ÀĞ¢ĞĞ¢Ğ¢&WGW&â–Æö@Ğ Ğ Ğ Ğ¦FVb6VæEöVÖ–Å÷&W÷'B†Ö&¶F÷vã¢7G"Â77e÷Fƒ¢7G"Â&V6V—fW%öVÖ–Ã¢7G"’ÓâæöæS Ğ¢6VæFW%öVÖ–ÂÒ÷2æVçf—&öâævWB‚$TÔ”Åõ4TäDU""Ğ¢6VæFW%÷vBÒ÷2æVçf—&öâævWB‚$TÔ”Åõ55tõ$B"Ğ¢–bæ÷B6VæFW%öVÖ–Â÷"æ÷B6VæFW%÷vC Ğ¢ÆövvW"çv&æ–ær‚.iÊ®ŠŠŞZé¢TÔ”Åõ4TäDU"òTÔ”Åõ55tõ$NûÈÎyZ^˜îZøNKú8""Ğ¢&WGW&àĞ¢×6rÒVÖ–ÄÖW76vR‚Ğ¢×6u²%7V&¦V7B%ÒÒb%´ÖöFR2™[~iÉşX;XÅÒz	Nz›nYŞYjâÒ¶FFWF–ÖRææ÷r‚’ç7G&gF–ÖR‚rU’ÒVÒÒVBTƒ¢TÒr—Ò Ğ¢×6u²$g&öÒ%ÒÒ6VæFW%öVÖ–ÀĞ¢×6u²%Fò%ÒÒ&V6V—fW%öVÖ–ÀĞ¢×6rç6WEö6öçFVçB†Ö&¶F÷vâĞ¢–b÷2çF‚æW†—7G2†77e÷F‚“ Ğ¢v—F‚÷Vâ†77e÷F‚Â'&""’2c Ğ¢×6ræFEöGF6†ÖVçB†bç&VB‚’ÂÖ–çG—SÒ'FW‡B"Â7V'G—SÒ&77b"Âf–ÆVæÖSÖ÷2çF‚æ&6VæÖR†77e÷F‚’Ğ¢v—F‚6×GÆ–"å4ÕE‚'6×GævÖ–Âæ6öÒ"ÂSƒr’26W'fW# Ğ¢6W'fW"ç7F'GFÇ2‚Ğ¢6W'fW"æÆöv–â‡6VæFW%öVÖ–ÂÂ6VæFW%÷vBĞ¢6W'fW"ç6VæEöÖW76vR†×6rĞ¢ÆövvW"æ–æfò‚$VÖ–Â6VçBâ"Ğ Ğ Ğ¢2ÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓĞĞ¢2K‹¾zˆ¾[ÈşûÉ®™[~iÉşX;XÎZI®YºZÙz	Nz›nkÈşii~ûÈXˆni[XJ®XX8iÈZI¢"j©NûÈĞ¢2ÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓĞĞ¦FVbÖ–â‚’ÓâæöæS Ğ¢tÄô$ÅôUd”DTä4UôÄTDtU"ç&W6WB‚Ğ¢W6W%öVÖ–ÂÒ÷2æVçf—&öâævWB‚%U4U%ôTÔ”Â"’÷"&s“#CstvÖ–Âæ6öÒ Ğ¢–çWEöf–ÆRÒ÷2æVçf—&öâævWB‚%TÄ”d”TEõTä•dU%4R"ÂTÄ”d”TEõTä•dU%4RĞ¢–bæ÷B÷2çF‚æW†—7G2†–çWEöf–ÆR“ Ğ¢&—6Rf–ÆTæ÷Df÷VæDW'&÷"†b.h›îKˆŞX‹¶–çWEöf–ÆWŞûÈÎŠ¸¾k©nX)jÈNKØÒF–6¶W"Â4”²y¨NX‰Ş˜kˆ^Yjî8""Ğ Ğ¢FbÒBç&VEö77b†–çWEöf–ÆRĞ¢–b%F–6¶W""æ÷B–âFbæ6öÇVÖç2÷"$4”²"æ÷B–âFbæ6öÇVÖç3 Ğ¢&—6RfÇVTW'&÷"‚'VÆ–f–VE÷Væ—fW'6Ræ77b[ø^šXÈ^Y
²F–6¶W"Â4”²jÈNKØŞ8""Ğ¢Fe²%F–6¶W"%ÒÒFe²%F–6¶W"%Òæ7G—R‡7G"’ç7G"çWW"‚’ç7G"ç7G&—‚Ğ¢Fe²$4”²%ÒÒFe²$4”²%Òæ7G—R‡7G"’ç7G"ç&WÆ6R‚"ã"Â""Â&VvWƒÔfÇ6R’ç7G"ç¦f–ÆÂƒĞ¢&u÷F–6¶W'2ÒÆ—7B†F–7Bæg&öÖ¶W—2†Fe²%F–6¶W"%ÒçFöÆ—7B‚’’Ğ¢6V7W&—G•öÖWFFFö'•÷F–6¶W#¢F–7E·7G"ÂF–7E·7G"Â7G%ÕÒÒ·ĞĞ¢†5÷fW&–f–VE÷&÷WFRÒ$–æGW7G'”ÖöFVÄ¶W’"–âFbæ6öÇVÖç0Ğ¢f÷"òÂ&÷r–âFbæ—FW'&÷w2‚“ Ğ¢F–6¶W"Ò7G"‡&÷rævWB‚%F–6¶W""’÷"""’çWW"‚’ç7G&—‚Ğ¢–bæ÷BF–6¶W# Ğ¢6öçF–çVPĞ¢ÖWFFF¢F–7E·7G"Â7G%ÒÒ·ĞĞ¢f÷"f–VÆB–â€Ğ¢%6V7W&—G”6Æ72"ÀĞ¢%6V7W&—G”6Æ746öæf–FVæ6R"ÀĞ¢%6V7W&—G”6Æ74Wf–FVæ6U6÷W&6R"ÀĞ¢%6V7F÷""ÀĞ¢$–æGW7G'’"ÀĞ¢$–æGW7G'”ÖöFVÄ¶W’"ÀĞ¢$ÖöFVÅ&÷WFT†–çB"ÀĞ¢%&÷WFU&V6öâ"ÀĞ¢%ö–çD–åF–ÖTe…&FR"ÀĞ¢$E%&F–ò"ÀĞ¢“ Ğ¢fÇVRÒ&÷rævWB†f–VÆBÂ""Ğ¢ÖWFFF¶f–VÆEÒÒ""–bBæ—6æ‡fÇVR’VÇ6R7G"‡fÇVRĞ¢ÖWFFF²%ô†5fW&–f–VE&÷WFR%ÒÒ7G"††5÷fW&–f–VE÷&÷WFRĞ¢6V7W&—G•öÖWFFFö'•÷F–6¶W%·F–6¶W%ÒÒÖWFFFĞ Ğ¢2jøşiÈhé.zˆ¾i»NikXZ[ˆ.ZNYŞYjîûÉ·W6‚õ"X8^˜xŞ‹yÖöFR2ˆˆ~kŠÎŠšn8 Ğ¢&UöfWF6…öÆÅöÖ&¶WEöFF‡&u÷F–6¶W'2²²%5’"Â%"Â%åDå‚%ÒÂW&–öCÒ#’"Ğ¢&UöfWF6…öÆÅö–æfò‡&u÷F–6¶W'2Ğ¢‡–G&FUö–æfõö66†Uög&öÕ÷fW&–f–VE÷Væ—fW'6R†FbĞ¢Væ—fW'6RÒ&W&U÷WÆöFVE÷Væ—fW'6R†FbĞ¢'VåöFV6—6–öå÷F–ÖW7F×ÒBåF–ÖW7F×ææ÷r‡G£Ò%UD2"’çG¥ö6öçfW'B„æöæRĞ Ğ¢ÆövvW"æ–æfò†b.™h¾Zx¾™[~iÉşX;XÎkˆ^zé~ûÉ®š™~ŠØ[èÂ¶ÆVâ‡Væ—fW'6R—Òj©BòKˆ®X+2¶ÆVâ‡&u÷F–6¶W'2—Òj©B"Ğ¢Ö…÷v÷&¶W'2Ò–çB†÷2æVçf—&öâævWB‚$ÔôDUô5õtõ$´U%2"Â#2"’Ğ¢Væ—fW'6Uö—FV×2ÒÆ—7B‡Væ—fW'6Ræ—FV×2‚’Ğ¢&W7VÇG3¢Æ—7E´÷F–öæÅ´ÖöFT5&W7VÇEÕÒÒ´æöæUÒ¢ÆVâ‡Væ—fW'6Uö—FV×2Ğ¢v—F‚6öæ7W'&VçBægWGW&W2åF‡&VEööÄW†V7WF÷"†Ö…÷v÷&¶W'3ÖÖ…÷v÷&¶W'2’2Wƒ Ğ¢gWGW&W2Ò°Ğ¢W‚ç7V&Ö—B€Ğ¢'VåöÖöFUö5÷—VÆ–æRÀĞ¢F–6¶W"ÀĞ¢6–²ÀĞ¢W6W%öVÖ–ÂÀĞ¢'VåöFV6—6–öå÷F–ÖW7F×ÀĞ¢6V7W&—G•öÖWFFFö'•÷F–6¶W"ævWB‡F–6¶W"’ÀĞ¢“¢–æFW€Ğ¢f÷"–æFW‚Â‡F–6¶W"Â6–²’–âVçVÖW&FR‡Væ—fW'6Uö—FV×2Ğ¢ĞĞ¢f÷"6ö×ÆWFVBÂgWGW&R–âVçVÖW&FR†6öæ7W'&VçBægWGW&W2æ5ö6ö×ÆWFVB†gWGW&W2’Â7F'CÓ“ Ğ¢&W7VÇG5¶gWGW&W5¶gWGW&UÕÒÒgWGW&Rç&W7VÇB‚Ğ¢–b6ö×ÆWFVBR#RÓÒ÷"6ö×ÆWFVBÓÒÆVâ†gWGW&W2“ Ğ¢ÆövvW"æ–æfò‚$ÖöFR2k{zú˜.[ªbVBòVB"Â6ö×ÆWFVBÂÆVâ†gWGW&W2’Ğ¢&W7VÇG2Ò·&W7VÇBf÷"&W7VÇB–â&W7VÇG2–b&W7VÇB—2æ÷BæöæUĞ¢Væ—fW'6U÷F‚ÒF‚…TÄ”d”TEõTä•dU%4R¢Væ—fW'6U÷fW'6–öâÒ€¢†6†Æ–"ç6†#Sb‡Væ—fW'6U÷F‚ç&VEö'—FW2‚’’æ†W†F–vW7B‚•³£eĞ¢–bVæ—fW'6U÷F‚æW†—7G2‚¢VÇ6R%Tä´äõtâ ¢¢v—Eö6öÖÖ—BÒ÷2æVçf—&öâævWB‚$t•D…T%õ4„"Â$Äô4Åõtõ$µE$TR"¢6VÆV7FVEöWf–FVæ6RÒtÄô$ÅôUd”DTä4UôÄTDtU"ç&÷w2‡6VÆV7FVEööæÇ“ÕG'VR¢ÆFW7E÷6V5ö'•÷F–6¶W#¢F–7E·7G"Â7G%ÒÒ·Ğ¢f÷"Wf–FVæ6R–â6VÆV7FVEöWf–FVæ6S ¢Wf–FVæ6U÷F–6¶W"Ò7G"†Wf–FVæ6RævWB‚'F–6¶W""’÷"""’çWW"‚¢f–Æ&ÆUöBÒ7G"†Wf–FVæ6RævWB‚&f–Æ&ÆU÷FõöÖöFVÅöB"’÷"""¢–bWf–FVæ6U÷F–6¶W"æBf–Æ&ÆUöC ¢ÆFW7E÷6V5ö'•÷F–6¶W%¶Wf–FVæ6U÷F–6¶W%ÒÒÖ‚€¢ÆFW7E÷6V5ö'•÷F–6¶W"ævWB†Wf–FVæ6U÷F–6¶W"Â""’Âf–Æ&ÆUö@¢¢f÷"&W7VÇB–â&W7VÇG3 ¢–bæ÷B&W7VÇBäFV6—6–öåõF–ÖW7F× ¢&W7VÇBäFV6—6–öåõF–ÖW7F×Ò'VåöFV6—6–öå÷F–ÖW7F×æ—6öf÷&ÖB‚¢6Æ÷6RÒvWEö66†VE÷6W&–W2‡&W7VÇBåF–6¶W"Â$6Æ÷6R"¢–b6Æ÷6R—2æ÷BæöæRæBæ÷B6Æ÷6RæV×G“ ¢&W7VÇBå&–6UôFFôFFRÒBåF–ÖW7F×†6Æ÷6Ræ–æFW…²ÓÒ’æFFR‚’æ—6öf÷&ÖB‚¢&W7VÇBäÆFW7Eõ4T5ôf–Æ&–Æ—G•ôFFRÒÆFW7E÷6V5ö'•÷F–6¶W"ævWB€¢&W7VÇBåF–6¶W"çWW"‚’Â%Tä´äõtâ ¢¢&W7VÇBåVæ—fW'6UõfW'6–öâÒVæ—fW'6U÷fW'6–öà¢&W7VÇBäv—Eô6öÖÖ—BÒv—Eö6öÖÖ—@ ¢6Æ–'&FUöW†—Eö×VÇF—ÆW2‡&W7VÇG2¢6†÷'FÆ—7BÒ6VÆV7EöF—fW'6–f–VE÷6†÷'FÆ—7B‡&W7VÇG2¢VÆ–v–&ÆUö6÷VçBÒ7VÒƒf÷""–â&W7VÇG2–b"äÆöæuõFW&ÕôVÆ–v–&ÆRĞ¢6V7F÷%÷öÆ–7’Ò&æò†&B6V7F÷"6÷VçB6"–bÔ…õU%õ4T5Dõ"ÃÒVÇ6Rb&Ö‚´Ô…õU%õ4T5Dõ'ÒæÖW2W"6V7F÷" Ğ¢ÆövvW"æ–æfò€Ğ¢b.™[~iÉşX;XÎzú˜ZèÎh‰ûÉ®YjÂ¶VÆ–v–&ÆUö6÷VçGÒj©NûÈÄvÆö&Â&W6V&6‚VWVR¶ÆVâ‡6†÷'FÆ—7B—Òj©NûÉ² ¢b'·6V7F÷%÷öÆ–7—Ş8" Ğ¢Ğ Ğ¢2XZ˜xş{YiéÎKùŞyYûÈÎikKëşjª.iú^‰Ş˜XéşYº8 Ğ¢&÷w2Ò¶6F–7B‡"’f÷""–â&W7VÇG5ĞĞ¢f÷"&÷r–â&÷w3 Ğ¢&÷u²$vVçEõF6·2%ÒÒ"Â"æ¦ö–â‡&÷rævWB‚$vVçEõF6·2"ÂµÒ’Ğ¢Wf–FVæ6U÷&÷w2ÒtÄô$ÅôUd”DTä4UôÄTDtU"ç&÷w2‡6VÆV7FVEööæÇ“ÕG'VRĞ¢Wf–FVæ6UöFbÒBäFFg&ÖR†Wf–FVæ6U÷&÷w2Â6öÇVÖç3ÔUd”DTä4Uô4ôÅTÔå2Ğ¢Wf–FVæ6UöFbçFõö77b„õUEUEôUd”DTä4Uô55bÂ–æFWƒÔfÇ6RÂVæ6öF–æsÒ'WFbÓ‚×6–r"Ğ Ğ¢&÷w2Òææ÷FFU÷&÷w2‡&÷w2ÂWf–FVæ6U÷&÷w2Ğ¢÷WEöFbÒBäFFg&ÖR‡&÷w2Ğ¢÷WEöFbçFõö77b„õUEUEô55bÂ–æFWƒÔfÇ6RÂVæ6öF–æsÒ'WFbÓ‚×6–r"Ğ Ğ¢2XúnZÙyÉşjÚ>™ÈŠhk{XZ^z	Nz›ny¨B(	3"j©NX	˜ûÉ¾KˆŞ‹k>i˜.KˆŞh»şKØîY8‹:®XZÎXûzÎk˜®i[8 Ğ¢6†÷'FÆ—7Eö÷&FW"Ò·&W7VÇBåF–6¶W#¢–æFW‚f÷"–æFW‚Â&W7VÇB–âVçVÖW&FR‡6†÷'FÆ—7B—ĞĞ¢6†÷'FÆ—7EöFbÒ÷WEöFe¶÷WEöFe²%F–6¶W"%Òæ—6–â‡6†÷'FÆ—7Eö÷&FW"•Òæ6÷’‚Ğ¢–bæ÷B6†÷'FÆ—7EöFbæV×G“ Ğ¢6†÷'FÆ—7EöFe²%÷6†÷'FÆ—7Eö÷&FW"%ÒÒ6†÷'FÆ—7EöFe²%F–6¶W"%ÒæÖ‡6†÷'FÆ—7Eö÷&FW"Ğ¢6†÷'FÆ—7EöFbÒ6†÷'FÆ—7EöFbç6÷'E÷fÇVW2‚%÷6†÷'FÆ—7Eö÷&FW""’æG&÷†6öÇVÖç3Ò%÷6†÷'FÆ—7Eö÷&FW""Ğ¢6†÷'FÆ—7EöFbçFõö77b„õUEUEõ4„õ%DÄ•5Eô55bÂ–æFWƒÔfÇ6RÂVæ6öF–æsÒ'WFbÓ‚×6–r"¢BäFFg&ÖR‡6†÷'FÆ—7Eö'•öÖöFVÂ‡&÷w2’’çFõö77b€¢õUEUEõ4„õ%DÄ•5Eô%•ôÔôDTÅô55bÀ¢–æFWƒÔfÇ6RÀ¢Væ6öF–æsÒ'WFbÓ‚×6–r"À¢ Ğ¢&W÷'BÒ"2ÖöFR2™[~iÉşX;XÎz	Nz›nYŞYjîûÈ…CR²dôò3R²iÈZI¢3RK‹¾X¹^˜ˆ*ûÈ•ÆåÆâ Ğ¢&W÷'B³Òb.kˆ^zé~i˜.™i>ûÉ§¶FFWF–ÖRææ÷r‚’ç7G&gF–ÖR‚rU’ÒVÒÒVBTƒ¢TÓ¢U2r—ÕÆåÆâ Ğ¢&W÷'B³Ò€Ğ¢b"¢®{H[è¾ûÉ®Xú®X®ZI®8KˆŞKÛşyJjy>jòşiÉşjÈ¢şiKîz›®ûÉ¾K‹¾X¹^˜:KØŞKˆ®™™´5D•dUõ4ÄTUdUôÄ”Ô•Eõ5C¢ãgÒ^ûÉ² Ğ¢b.YjîKˆXZÎXûKˆ®™™´Ô…õõ4•D”ôåõtT”t…Eõ5EõDõDÃ¢ãgÒ^ûÉ¾YjîKˆyJ.jZŞKˆ®™™´Ô…õ4T5Dõ%õtT”t…Eõ5EõDõDÃ¢ãgÒ^ûÉ² Ğ¢.jŠYè¾iŠşz	Nz›nkÈşii~ûÈÎKˆŞiŠşˆz®X¹^‹+~XZ^Šˆ®‰™ş8"¢¥ÆåÆâ Ğ¢Ğ¢–bæ÷B6†÷'FÆ—7C Ğ¢&W÷'B³Ò.iÊÎjÊk).iÈXZÎXûYÎi˜.˜	®˜îY8‹:®8KËXÎ8š	iÉşˆˆ~Kˆ¾j©Nš*™ª®™hj«¾ûÉ¾KùŞyYxûî˜yh‰bUDnûÈÎKˆŞzÎk˜®X¾ˆ*8%Æâ Ğ¢f÷""–â6†÷'FÆ—7C Ğ¢&W÷'B³Ò&VæFW%÷7Fö6µ÷&W÷'B‡"’²%ÆâÒÒÕÆåÆâ Ğ¢F‚„õUEUEôÔB’çw&—FU÷FW‡B‡&W÷'BÂVæ6öF–æsÒ'WFbÓ‚"Ğ Ğ¢ÖWG&–5öÖWFFFö'•÷F–6¶W"Ò°Ğ¢7G"‡&÷u²%F–6¶W"%Ò“¢§6öâæÆöG2‡7G"‡&÷u²$ÖWG&–5ôÖWFFFô¥4ôâ%Ò’Ğ¢f÷"&÷r–â&÷w0Ğ¢ĞĞ¢–ÆöBÒ'V–ÆEövVçE÷–ÆöB‡6†÷'FÆ—7BÂÖWG&–5öÖWFFFö'•÷F–6¶W"Ğ¢F‚„õUEUEô¥4ôâ’çw&—FU÷FW‡B†§6öâæGV×2‡–ÆöBÂVç7W&Uö66–“ÔfÇ6RÂ–æFVçCÓ"’ÂVæ6öF–æsÒ'WFbÓ‚"Ğ¢6fUö§6öåö66†R„44„Uôd”ÄUõ4„$U2ÂõdT5Dõ%ô44„RĞ Ğ¢ÆövvW"æ–æfò†b.ZèÎh‰8$VÆ–v–&ÆS×¶VÆ–v–&ÆUö6÷VçGÒò6†÷'FÆ—7C×¶ÆVâ‡6†÷'FÆ—7B—ÒòF÷FÃ×¶ÆVâ†÷WEöFb—Ò"Ğ¢ÆövvW"æ–æfò€Ğ¢b.[{.‹ËX{¢´õUEUEõ4„õ%DÄ•5Eô55gŞ8´õUEUEôÔGŞ8´õUEUEô¥4ôçÒˆˆr´õUEUEôUd”DTä4Uô55gŞ8" Ğ¢Ğ Ğ¢–b÷2æVçf—&öâævWB‚%4TäEôTÔ”Â"Â#"’ÓÒ## Ğ¢6VæEöVÖ–Å÷&W÷'B‡&W÷'BÂõUEUEõ4„õ%DÄ•5Eô55bÂW6W%öVÖ–ÂĞ Ğ Ğ Ğ¦–bõöæÖUõòÓÒ%õöÖ–åõò# Ğ¢G'“ Ğ¢Ö–â‚Ğ¢W†6WBW†6WF–öâ2W†3 Ğ¢ÆövvW"æ7&—F–6Â†b.{;¾{[[JkÛûÉ§¶W†7Ò"Ğ¢G&6V&6²ç&–çEöW†2‚Ğ¢&—6PĞ 