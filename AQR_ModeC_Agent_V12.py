"""
=============================================================================
AQR Mode-C Agent V12 — 三階段雙殺模型可執行版
=============================================================================
用途：
1) 讀取每日硬篩初選清單 qualified_universe.csv（欄位：Ticker, CIK）
2) 使用 SEC XBRL Company Concept 對齊 TTM / 最新 10-K / 最新 10-Q
3) 產出：
   - mode_c_screen.csv              ：結構化數據總表
   - mode_c_report.md               ：三階段雙殺中文投資決策書
   - mode_c_agent_payload.json      ：交給 LLM / Web Agent 做物理限制驗證的任務包

核心修正：
- 不把單季數據直接年化；流量項改用 TTM：最新 10-K 或「最新 10-Q YTD + 前一年 10-K - 前一年同季 YTD」。
- Real FCF Yield = (TTM OCF - min(TTM CapEx, TTM D&A) - TTM SBC) / EV。
- EV = Market Cap + Total Debt - Cash & Equivalents，不再使用「excess cash」美化。
- 實質回購 = (TTM Buyback - TTM Stock Issuance) - TTM SBC；小於 0 直接標示稀釋幻覺。
- 雙殺壓力測試：EBITDA -15% / -30%，估值倍數打到自身近 10 年低分位。
- 加入最新三季毛利 / 營收診斷、Short Interest、Days to Cover、DSI、30 天催化劑。
=============================================================================
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import math
import os
import smtplib
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==============================================================================
# 基本設定
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
OUTPUT_MD = "mode_c_report.md"
OUTPUT_JSON = "mode_c_agent_payload.json"

# SEC Fair Access 官方上限是 10 req/s；這裡保守設 8。
SEC_MAX_CALLS_PER_SECOND = 8
SEC_TIMEOUT = 15

# 硬篩門檻：這些是「Agent 前置硬篩」，不是最終交易結論。
MIN_LIQUIDITY_USD = 5_000_000
MIN_MARKET_CAP_B = 1.0
ICR_WARNING = 3.0
SHORT_SQUEEZE_SI = 15.0
SHORT_SQUEEZE_DTC = 5.0

# 歷史估值底部取 5th percentile；比單一最低值更抗資料髒點。
LOW_VALUATION_PERCENTILE = 5

# ==============================================================================
# 快取
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


def pre_fetch_all_market_data(tickers: List[str], period: str = "10y") -> None:
    global _BULK_MARKET_DATA
    tickers = sorted(set([t for t in tickers if t]))
    logger.info(f"批量下載價格資料：{len(tickers)} 檔，period={period}")
    _BULK_MARKET_DATA = yf.download(
        tickers,
        period=period,
        progress=False,
        auto_adjust=True,
        group_by="column",
        threads=True,
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
            # 單檔下載時 yfinance 不一定產生 MultiIndex
            if col in _BULK_MARKET_DATA.columns:
                s = _BULK_MARKET_DATA[col].dropna()
                return s if not s.empty else None
    except Exception:
        return None
    return None


def get_price_asof(ticker: str, date_like: pd.Timestamp) -> float:
    close = get_cached_series(ticker, "Close")
    if close is None or close.empty:
        return 0.0
    date_like = pd.Timestamp(date_like).tz_localize(None)
    s = close.copy()
    s.index = pd.to_datetime(s.index).tz_localize(None)
    s = s[s.index <= date_like]
    if s.empty:
        return 0.0
    return float(s.iloc[-1])


def pre_fetch_all_info(tickers: List[str]) -> None:
    global _INFO_CACHE
    logger.info(f"批量抓取 yf.info：{len(tickers)} 檔")
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

# ==============================================================================
# SEC XBRL 抽取器
# ==============================================================================
class RateLimitedSession:
    def __init__(self, calls: int = SEC_MAX_CALLS_PER_SECOND, period: float = 1.0):
        self.session = create_retry_session()
        self.calls = calls
        self.period = period
        self.lock = threading.Lock()
        self.timestamps: List[float] = []

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
                r = self.session.get(url, headers=headers, timeout=SEC_TIMEOUT)
                if r.status_code == 200:
                    return r
                if r.status_code == 404:
                    return None
                if r.status_code in (429, 503):
                    time.sleep((2 ** attempt) * 1.5)
            except requests.RequestException:
                time.sleep((2 ** attempt) * 1.2)
        return None


_GLOBAL_SEC_SESSION = RateLimitedSession()


class SECDataDistiller:
    def __init__(self, email: str):
        self.headers = {"User-Agent": f"ModeCQuantResearch {email}"}
        self.session = _GLOBAL_SEC_SESSION
        self.config = {
            "OCF": [
                "NetCashProvidedByUsedInOperatingActivities",
                "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            ],
            "CapEx": [
                "PaymentsToAcquirePropertyPlantAndEquipment",
                "PropertyPlantAndEquipmentAdditions",
            ],
            "SBC": [
                "ShareBasedCompensation",
                "StockBasedCompensation",
                "AllocatedShareBasedCompensationExpense",
                "ShareBasedCompensationExpense",
            ],
            "EBIT": [
                "OperatingIncomeLoss",
                "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            ],
            "Interest": ["InterestExpense", "InterestExpenseDebt", "InterestExpenseNet", "InterestAndDebtExpense"],
            "DnA": ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization"],
            "Revenue": [
                "Revenues",
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "SalesRevenueNet",
                "SalesRevenueGoodsNet",
            ],
            "GrossProfit": ["GrossProfit"],
            "COGS": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization"],
            "Inventory": ["InventoryNet", "Inventory"],
            "DebtTotal": [
                "DebtCurrentAndLongTerm",
                "DebtAndFinanceLeaseObligations",
                "LongTermDebtAndFinanceLeaseObligations",
                "LongTermDebtAndCapitalLeaseObligations",
                "LongTermDebt",
            ],
            "DebtCurrent": [
                "DebtCurrent",
                "ShortTermBorrowings",
                "LongTermDebtAndFinanceLeaseObligationsCurrent",
                "LongTermDebtCurrent",
            ],
            "Cash": [
                "CashAndCashEquivalentsAtCarryingValue",
                "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            ],
            "Equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
            "NetIncome": ["NetIncomeLoss", "ProfitLoss"],
            "Buyback": ["PaymentsForRepurchaseOfCommonStock", "PaymentsForRepurchaseOfEquity"],
            "StockIssuance": ["ProceedsFromIssuanceOfCommonStock", "StockIssuedDuringPeriodValueNewIssues"],
            "Dividend": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
            "EPSDiluted": ["EarningsPerShareDiluted"],
            "SharesDiluted": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
        }

    def fetch_concept(self, cik: str, concept: str, units: Tuple[str, ...] = ("USD",)) -> pd.DataFrame:
        tags = self.config.get(concept, [concept])
        for tag in tags:
            url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{str(cik).zfill(10)}/us-gaap/{tag}.json"
            r = self.session.get(url, headers=self.headers)
            if not r:
                continue
            try:
                unit_map = r.json().get("units", {})
                rows = []
                for unit in units:
                    rows.extend(unit_map.get(unit, []))
                if not rows:
                    continue
                df = pd.DataFrame(rows)
                df["concept"] = tag
                return self._clean_facts(df)
            except Exception:
                continue
        return pd.DataFrame()

    def fetch_shares_outstanding(self, cik: str) -> pd.DataFrame:
        tag_candidates = [
            ("dei", "EntityCommonStockSharesOutstanding"),
            ("us-gaap", "CommonStocksIncludingAdditionalPaidInCapitalSharesOutstanding"),
            ("us-gaap", "CommonStockSharesOutstanding"),
            ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),
        ]
        for taxonomy, tag in tag_candidates:
            url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{str(cik).zfill(10)}/{taxonomy}/{tag}.json"
            r = self.session.get(url, headers=self.headers)
            if not r:
                continue
            try:
                unit_map = r.json().get("units", {})
                rows = unit_map.get("shares", [])
                if not rows:
                    continue
                df = pd.DataFrame(rows)
                df["concept"] = tag
                return self._clean_facts(df)
            except Exception:
                continue
        return pd.DataFrame()

    @staticmethod
    def _clean_facts(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        df = df.copy()
        for c in ["start", "end", "filed"]:
            if c in df.columns:
                df[c] = pd.to_datetime(df[c], errors="coerce")
        if "start" in df.columns and "end" in df.columns:
            df["duration_days"] = (df["end"] - df["start"]).dt.days
        else:
            df["duration_days"] = np.nan
        if "form" not in df.columns:
            df["form"] = ""
        if "fp" not in df.columns:
            df["fp"] = ""
        if "fy" not in df.columns:
            df["fy"] = np.nan
        df["val"] = pd.to_numeric(df["val"], errors="coerce")
        df = df.dropna(subset=["val", "end"])
        sort_cols = [c for c in ["end", "filed"] if c in df.columns]
        df = df.sort_values(sort_cols)
        dedup = [c for c in ["fy", "fp", "form", "end", "duration_days", "frame"] if c in df.columns]
        if dedup:
            df = df.drop_duplicates(subset=dedup, keep="last")
        return df.reset_index(drop=True)

    @staticmethod
    def _annual_facts(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        d = df.copy()
        is_10k = d["form"].astype(str).str.upper().eq("10-K")
        is_annual_duration = d["duration_days"].between(330, 380, inclusive="both")
        annual = d[is_10k & (is_annual_duration | d["fp"].astype(str).str.upper().eq("FY"))]
        if annual.empty:
            annual = d[is_10k]
        return annual.sort_values("end").reset_index(drop=True)

    @staticmethod
    def _instant_latest(df: pd.DataFrame) -> float:
        if df.empty:
            return 0.0
        d = df.sort_values(["end", "filed"] if "filed" in df.columns else ["end"])
        return float(d["val"].iloc[-1]) / 1e9

    def latest_balance(self, df: pd.DataFrame) -> float:
        return self._instant_latest(df)

    def latest_annual(self, df: pd.DataFrame) -> Tuple[float, str]:
        annual = self._annual_facts(df)
        if annual.empty:
            return 0.0, "missing"
        r = annual.iloc[-1]
        return float(r["val"]) / 1e9, f"10-K {r.get('fy', '')} end={pd.Timestamp(r['end']).date()}"

    @staticmethod
    def _select_ytd(df: pd.DataFrame, fy, fp: str) -> Optional[pd.Series]:
        if df.empty or pd.isna(fy):
            return None
        fp = str(fp).upper()
        expected = {"Q1": 90, "Q2": 180, "Q3": 270, "FY": 365}.get(fp)
        d = df[(df["fy"].astype(str) == str(fy)) & (df["fp"].astype(str).str.upper() == fp)].copy()
        if d.empty:
            return None
        if expected:
            d["score"] = (d["duration_days"].fillna(expected) - expected).abs()
            # 對 Q2/Q3，排除明顯單季 90 天版本；要 YTD。
            if fp == "Q2":
                d = d[d["duration_days"].fillna(180).between(140, 220, inclusive="both")]
            elif fp == "Q3":
                d = d[d["duration_days"].fillna(270).between(230, 310, inclusive="both")]
            elif fp == "Q1":
                d = d[d["duration_days"].fillna(90).between(60, 130, inclusive="both")]
        if d.empty:
            return None
        d = d.sort_values(["score", "filed"] if "filed" in d.columns else ["score"])
        return d.iloc[-1]

    def ttm_flow(self, df: pd.DataFrame, signed: bool = True) -> Tuple[float, str, Dict[str, float]]:
        """
        流量項 TTM。優先：若最新報告是 10-K，使用最新年度；若最新是 10-Q，使用：
        latest_10K + latest_YTD_10Q - prior_year_same_fp_YTD。
        fallback：由 quarterly_series 取近四季加總。
        回傳單位：十億美元。
        """
        if df.empty:
            return 0.0, "missing", {}
        d = df.copy().sort_values(["end", "filed"] if "filed" in df.columns else ["end"])
        annual = self._annual_facts(d)
        q = d[d["form"].astype(str).str.upper().eq("10-Q")].copy()

        latest_10k_end = annual["end"].max() if not annual.empty else pd.NaT
        latest_10q_end = q["end"].max() if not q.empty else pd.NaT

        if pd.notna(latest_10k_end) and (pd.isna(latest_10q_end) or latest_10k_end >= latest_10q_end):
            r = annual.sort_values("end").iloc[-1]
            val = float(r["val"]) / 1e9
            return (val if signed else abs(val)), f"TTM=latest 10-K end={pd.Timestamp(r['end']).date()}", {"annual": val}

        if pd.notna(latest_10q_end) and not annual.empty:
            latest_q_candidates = q[q["end"] == latest_10q_end]
            latest_q = latest_q_candidates.iloc[-1]
            fy, fp = latest_q.get("fy"), str(latest_q.get("fp", "")).upper()
            latest_ytd = self._select_ytd(d, fy, fp)
            prior_ytd = self._select_ytd(d, int(fy) - 1 if str(fy).isdigit() else fy, fp)
            annual_before = annual[annual["end"] < latest_q["end"]]
            if latest_ytd is not None and prior_ytd is not None and not annual_before.empty:
                ann = float(annual_before.iloc[-1]["val"])
                ly = float(latest_ytd["val"])
                py = float(prior_ytd["val"])
                ttm = (ann + ly - py) / 1e9
                return (ttm if signed else abs(ttm)), (
                    f"TTM=10-K + latest {fp} YTD - prior {fp} YTD; "
                    f"latest_end={pd.Timestamp(latest_q['end']).date()}"
                ), {"annual": ann / 1e9, "latest_ytd": ly / 1e9, "prior_ytd": py / 1e9}

        qs = self.quarterly_series(df)
        if len(qs) >= 4:
            ttm = float(qs.tail(4).sum()) / 1e9
            return (ttm if signed else abs(ttm)), "TTM=fallback sum(last 4 derived quarters)", {"q4sum": ttm}

        val, method = self.latest_annual(df)
        return (val if signed else abs(val)), f"fallback annual: {method}", {"annual": val}

    def quarterly_series(self, df: pd.DataFrame) -> pd.Series:
        """由 Q1/Q2/Q3 YTD 與 FY 推導單季流量，單位保留原始美元。"""
        if df.empty:
            return pd.Series(dtype=float)
        d = df.copy()
        d = d[d["form"].astype(str).str.upper().isin(["10-Q", "10-K"])]
        if d.empty or "fy" not in d.columns or "fp" not in d.columns:
            return pd.Series(dtype=float)
        years = sorted([y for y in d["fy"].dropna().unique() if str(y).replace(".", "").isdigit()])
        out: List[Tuple[pd.Timestamp, float]] = []
        for fy in years:
            q1 = self._select_ytd(d, fy, "Q1")
            q2 = self._select_ytd(d, fy, "Q2")
            q3 = self._select_ytd(d, fy, "Q3")
            fyv = self._select_ytd(d, fy, "FY")
            if fyv is None:
                annual = self._annual_facts(d[d["fy"].astype(str) == str(fy)])
                fyv = annual.iloc[-1] if not annual.empty else None
            q_vals = []
            if q1 is not None:
                q_vals.append((pd.Timestamp(q1["end"]), float(q1["val"])))
            if q2 is not None and q1 is not None:
                q_vals.append((pd.Timestamp(q2["end"]), float(q2["val"]) - float(q1["val"])))
            if q3 is not None and q2 is not None:
                q_vals.append((pd.Timestamp(q3["end"]), float(q3["val"]) - float(q2["val"])))
            if fyv is not None and q3 is not None:
                q_vals.append((pd.Timestamp(fyv["end"]), float(fyv["val"]) - float(q3["val"])))
            for end, val in q_vals:
                # 防 XBRL 混用導致極端負值。負數仍允許，但排除錯誤級離群。
                if math.isfinite(val):
                    out.append((end, val))
        if not out:
            return pd.Series(dtype=float)
        s = pd.Series({end: val for end, val in out}).sort_index()
        return s[~s.index.duplicated(keep="last")]

    def get_shares_now_and_1y(self, df: pd.DataFrame) -> Tuple[float, float]:
        if df.empty:
            return 0.0, 0.0
        d = df.sort_values("end")
        latest = d.iloc[-1]
        one_year_ago = pd.Timestamp(latest["end"]) - pd.Timedelta(days=365)
        old = d[d["end"] <= one_year_ago]
        now = float(latest["val"]) / 1e9
        old_val = float(old.iloc[-1]["val"]) / 1e9 if not old.empty else 0.0
        return now, old_val

    def shares_asof(self, df: pd.DataFrame, date_like: pd.Timestamp, fallback: float = 0.0) -> float:
        if df.empty:
            return fallback
        d = df[df["end"] <= pd.Timestamp(date_like)]
        if d.empty:
            return fallback
        return float(d.sort_values("end").iloc[-1]["val"]) / 1e9

# ==============================================================================
# 計算函式
# ==============================================================================
def get_robust_shares(ticker: str, df_shares: pd.DataFrame, sec: SECDataDistiller, info: dict) -> float:
    global _VECTOR_CACHE
    current_time = time.time()
    shares_now, shares_1y_ago = sec.get_shares_now_and_1y(df_shares)
    if shares_now > 0:
        drift = 0.02
        if shares_1y_ago > 0:
            drift = (shares_now / shares_1y_ago) - 1.0
            drift = max(-0.10, min(0.20, drift))
        _VECTOR_CACHE[ticker] = {"shares": shares_now, "drift": drift, "timestamp": current_time}
        return shares_now
    if ticker in _VECTOR_CACHE:
        c = _VECTOR_CACHE[ticker]
        days = (current_time - float(c.get("timestamp", current_time))) / 86400
        return float(c.get("shares", 0.0)) * (1 + float(c.get("drift", 0.02)) * days / 365)
    yf_shares = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
    if yf_shares and yf_shares > 0:
        return float(yf_shares) / 1e9
    return 0.0


def fetch_price_metrics(ticker: str) -> Optional[dict]:
    close = get_cached_series(ticker, "Close")
    volume = get_cached_series(ticker, "Volume")
    if close is None or volume is None or len(close) < 200:
        return None
    dollar_volume = float((close * volume).tail(30).mean())
    last_close = float(close.iloc[-1])
    high_52w = float(close.tail(252).max())
    m = close.resample("ME").last().dropna()
    momentum = None
    if len(m) >= 13:
        momentum = (float(m.iloc[-2]) / float(m.iloc[-13]) - 1) * 100
    return {
        "last_close": last_close,
        "dollar_volume": dollar_volume,
        "pct_from_52w_high": (last_close / high_52w - 1) * 100 if high_52w > 0 else np.nan,
        "momentum_12m": momentum,
    }


def safe_div(n: float, d: float, default: float = np.nan) -> float:
    try:
        if d == 0 or not math.isfinite(d):
            return default
        return n / d
    except Exception:
        return default


def percentile_rank(history: Iterable[float], current: float) -> float:
    vals = [float(x) for x in history if x is not None and math.isfinite(float(x)) and float(x) > 0]
    if not vals or not math.isfinite(current):
        return np.nan
    return float((np.sum(np.array(vals) <= current) / len(vals)) * 100)


def low_percentile(history: Iterable[float], pct: float = LOW_VALUATION_PERCENTILE) -> float:
    vals = [float(x) for x in history if x is not None and math.isfinite(float(x)) and float(x) > 0]
    if not vals:
        return np.nan
    return float(np.nanpercentile(vals, pct))


def classify_three_quarter_trend(rev_q: pd.Series, gp_q: pd.Series) -> Tuple[str, dict]:
    common = pd.concat([rev_q.rename("revenue"), gp_q.rename("gross_profit")], axis=1, join="inner").dropna()
    common = common[common["revenue"] > 0].tail(3)
    if len(common) < 3:
        return "資料不足：無法完成最新三季毛利診斷", {}
    common["gm"] = common["gross_profit"] / common["revenue"]
    rev_change = float(common["revenue"].iloc[-1] / common["revenue"].iloc[0] - 1)
    gm_change_pp = float((common["gm"].iloc[-1] - common["gm"].iloc[0]) * 100)
    gm_monotonic_down = bool(common["gm"].iloc[2] < common["gm"].iloc[1] < common["gm"].iloc[0])
    if rev_change < -0.02 and abs(gm_change_pp) <= 1.5:
        label = "暫時落難好股：營收下滑但毛利持穩，疑似仍有定價權"
    elif rev_change >= -0.02 and (gm_monotonic_down or gm_change_pp <= -2.0):
        label = "結構性價值陷阱：營收未崩但毛利連續失血"
    elif rev_change < -0.02 and gm_change_pp < -2.0:
        label = "雙重惡化：營收與毛利同步失血"
    else:
        label = "中性：三季趨勢未給出明確逆風訊號"
    metrics = {
        "rev_3q_change_pct": rev_change * 100,
        "gm_latest_pct": float(common["gm"].iloc[-1] * 100),
        "gm_3q_change_pp": gm_change_pp,
    }
    return label, metrics


def calc_dsi_series(inv_df: pd.DataFrame, cogs_q: pd.Series) -> pd.Series:
    if inv_df.empty or cogs_q.empty or len(cogs_q) < 4:
        return pd.Series(dtype=float)
    inv = inv_df.sort_values("end")[["end", "val"]].dropna().drop_duplicates("end", keep="last")
    inv.index = pd.to_datetime(inv["end"])
    inv_s = inv["val"]
    out = {}
    for end in cogs_q.index[-8:]:
        inv_asof = inv_s[inv_s.index <= end]
        if inv_asof.empty:
            continue
        last4 = cogs_q[cogs_q.index <= end].tail(4)
        if len(last4) < 4 or last4.sum() <= 0:
            continue
        avg_inv = float(inv_asof.tail(2).mean()) if len(inv_asof) >= 2 else float(inv_asof.iloc[-1])
        out[end] = avg_inv / float(last4.sum()) * 365
    return pd.Series(out).sort_index()


def get_upcoming_earnings(ticker: str) -> List[str]:
    events = []
    now = pd.Timestamp.utcnow().tz_localize(None)
    end = now + pd.Timedelta(days=30)
    try:
        cal = yf.Ticker(ticker).calendar
        if isinstance(cal, dict):
            candidates = cal.get("Earnings Date") or cal.get("EarningsDate") or []
            if not isinstance(candidates, list):
                candidates = [candidates]
            for c in candidates:
                dt = pd.to_datetime(c, errors="coerce")
                if pd.notna(dt):
                    dt = dt.tz_localize(None) if getattr(dt, "tzinfo", None) else dt
                    if now <= dt <= end:
                        events.append(f"Earnings: {dt.date()}")
        elif isinstance(cal, pd.DataFrame) and not cal.empty:
            for idx, row in cal.iterrows():
                for item in row.values:
                    dt = pd.to_datetime(item, errors="coerce")
                    if pd.notna(dt):
                        dt = dt.tz_localize(None) if getattr(dt, "tzinfo", None) else dt
                        if now <= dt <= end:
                            events.append(f"Earnings: {dt.date()}")
    except Exception:
        pass
    return sorted(set(events))


def historical_valuation(
    ticker: str,
    sec: SECDataDistiller,
    df_ebit: pd.DataFrame,
    df_dna: pd.DataFrame,
    df_debt: pd.DataFrame,
    df_cash: pd.DataFrame,
    df_net_income: pd.DataFrame,
    df_shares: pd.DataFrame,
    current_ev_ebitda: float,
    current_pe: float,
    shares_now: float,
) -> dict:
    ebit_a = sec._annual_facts(df_ebit)
    dna_a = sec._annual_facts(df_dna)
    ni_a = sec._annual_facts(df_net_income)
    debt_a = sec._annual_facts(df_debt)
    cash_a = sec._annual_facts(df_cash)
    if ebit_a.empty:
        return {
            "ev_ebitda_hist": [],
            "pe_hist": [],
            "ev_ebitda_percentile": np.nan,
            "pe_percentile": np.nan,
            "ev_ebitda_floor": np.nan,
            "pe_floor": np.nan,
        }
    rows = []
    for _, erow in ebit_a.tail(10).iterrows():
        end = pd.Timestamp(erow["end"])
        price = get_price_asof(ticker, end)
        if price <= 0:
            continue
        shares = sec.shares_asof(df_shares, end, fallback=shares_now)
        mcap = price * shares
        # 以年末前最近一筆 balance fact 近似。
        debt = float(debt_a[debt_a["end"] <= end]["val"].iloc[-1]) / 1e9 if not debt_a[debt_a["end"] <= end].empty else 0.0
        cash = float(cash_a[cash_a["end"] <= end]["val"].iloc[-1]) / 1e9 if not cash_a[cash_a["end"] <= end].empty else 0.0
        dna = float(dna_a[dna_a["end"] == erow["end"]]["val"].iloc[-1]) / 1e9 if not dna_a[dna_a["end"] == erow["end"]].empty else 0.0
        ni = float(ni_a[ni_a["end"] == erow["end"]]["val"].iloc[-1]) / 1e9 if not ni_a[ni_a["end"] == erow["end"]].empty else np.nan
        ebit = float(erow["val"]) / 1e9
        ebitda = ebit + abs(dna)
        ev = mcap + debt - cash
        ev_ebitda = safe_div(ev, ebitda)
        pe = safe_div(mcap, ni)
        rows.append({"end": str(end.date()), "EV_EBITDA": ev_ebitda, "PE": pe})
    ev_hist = [r["EV_EBITDA"] for r in rows if math.isfinite(r["EV_EBITDA"]) and r["EV_EBITDA"] > 0]
    pe_hist = [r["PE"] for r in rows if math.isfinite(r["PE"]) and r["PE"] > 0]
    return {
        "ev_ebitda_hist": rows,
        "pe_hist": pe_hist,
        "ev_ebitda_percentile": percentile_rank(ev_hist, current_ev_ebitda),
        "pe_percentile": percentile_rank(pe_hist, current_pe),
        "ev_ebitda_floor": low_percentile(ev_hist),
        "pe_floor": low_percentile(pe_hist),
    }


def implied_ebitda_cagr(current_ev: float, net_debt: float, base_ebitda: float, target_multiple: float, years: int = 3) -> float:
    """
    反推市場現價要求的 EBITDA CAGR：
    假設三年後 EV 仍等於 current_ev，且合理 terminal EV/EBITDA = 近十年中位或低分位上修值。
    required EBITDA_3Y = current_ev / target_multiple。
    """
    if base_ebitda <= 0 or current_ev <= 0 or target_multiple <= 0:
        return np.nan
    required = current_ev / target_multiple
    return (required / base_ebitda) ** (1 / years) - 1


@dataclass
class ModeCResult:
    Ticker: str
    Status: str
    Price: float = np.nan
    MarketCap_B: float = np.nan
    EV_B: float = np.nan
    Real_FCF_Yield_pct: float = np.nan
    TTM_OCF_B: float = np.nan
    Dynamic_CapEx_B: float = np.nan
    TTM_SBC_B: float = np.nan
    ICR: float = np.nan
    Real_Buyback_B: float = np.nan
    Dilution_Illusion: bool = False
    EBITDA_B: float = np.nan
    EV_EBITDA_x: float = np.nan
    PE_x: float = np.nan
    EV_EBITDA_10Y_Percentile: float = np.nan
    PE_10Y_Percentile: float = np.nan
    EBITDA_Drawdown_15_pct: float = np.nan
    EBITDA_Drawdown_30_pct: float = np.nan
    GM_Diagnosis: str = ""
    Rev_3Q_Change_pct: float = np.nan
    GM_Latest_pct: float = np.nan
    GM_3Q_Change_pp: float = np.nan
    Implied_EBITDA_CAGR_3Y_pct: float = np.nan
    Physical_Check: str = "需 Agent 連網驗證：TSMC/ASML/CSP CapEx/產能/交期"
    ShortInterest_pctFloat: float = np.nan
    DaysToCover: float = np.nan
    Squeeze_Risk: bool = False
    DSI_Latest: float = np.nan
    DSI_2Q_Down: bool = False
    Catalysts_30D: str = ""
    Data_Quality_Flags: str = ""
    Verdict: str = ""
    Trade_Tool: str = ""
    Agent_Tasks: List[str] = field(default_factory=list)


def run_mode_c_pipeline(ticker: str, cik: str, email: str) -> ModeCResult:
    try:
        if any(x in ticker for x in ["-", "."]):
            return ModeCResult(Ticker=ticker, Status="Fail: 排除特別股/多重股權")

        pm = fetch_price_metrics(ticker)
        if not pm:
            return ModeCResult(Ticker=ticker, Status="Fail: 無價格資料")
        if pm["dollar_volume"] < MIN_LIQUIDITY_USD:
            return ModeCResult(Ticker=ticker, Status=f"Fail: 流動性不足 ${pm['dollar_volume']/1e6:.1f}M")

        info = safe_yf_info(ticker)
        price = float(info.get("currentPrice") or info.get("regularMarketPrice") or pm["last_close"] or 0.0)
        if price <= 0:
            return ModeCResult(Ticker=ticker, Status="Fail: 價格失真")

        sec = SECDataDistiller(email)
        df_ocf = sec.fetch_concept(cik, "OCF")
        df_capex = sec.fetch_concept(cik, "CapEx")
        df_sbc = sec.fetch_concept(cik, "SBC")
        df_ebit = sec.fetch_concept(cik, "EBIT")
        df_int = sec.fetch_concept(cik, "Interest")
        df_dna = sec.fetch_concept(cik, "DnA")
        df_rev = sec.fetch_concept(cik, "Revenue")
        df_gp = sec.fetch_concept(cik, "GrossProfit")
        df_cogs = sec.fetch_concept(cik, "COGS")
        df_inv = sec.fetch_concept(cik, "Inventory")
        df_debt_total = sec.fetch_concept(cik, "DebtTotal")
        df_debt_current = sec.fetch_concept(cik, "DebtCurrent")
        df_cash = sec.fetch_concept(cik, "Cash")
        df_net_income = sec.fetch_concept(cik, "NetIncome")
        df_buyback = sec.fetch_concept(cik, "Buyback")
        df_issuance = sec.fetch_concept(cik, "StockIssuance")
        df_shares = sec.fetch_shares_outstanding(cik)

        if df_ocf.empty or df_rev.empty or df_ebit.empty:
            return ModeCResult(Ticker=ticker, Status="Fail: SEC 核心資料缺失 OCF/Revenue/EBIT")

        ocf_ttm, ocf_method, _ = sec.ttm_flow(df_ocf)
        capex_ttm, capex_method, _ = sec.ttm_flow(df_capex, signed=False)
        sbc_ttm, sbc_method, _ = sec.ttm_flow(df_sbc, signed=False)
        ebit_ttm, ebit_method, _ = sec.ttm_flow(df_ebit)
        dna_ttm, dna_method, _ = sec.ttm_flow(df_dna, signed=False)
        rev_ttm, rev_method, _ = sec.ttm_flow(df_rev)
        interest_ttm, _, _ = sec.ttm_flow(df_int, signed=False)
        net_income_ttm, _, _ = sec.ttm_flow(df_net_income)
        buyback_ttm, _, _ = sec.ttm_flow(df_buyback, signed=False)
        issuance_ttm, _, _ = sec.ttm_flow(df_issuance, signed=False)

        if capex_ttm == 0:
            capex_ttm = abs(float(info.get("capitalExpenditures") or 0.0)) / 1e9
        if sbc_ttm == 0:
            sbc_ttm = abs(float(info.get("shareBasedCompensation") or 0.0)) / 1e9

        shares_now = get_robust_shares(ticker, df_shares, sec, info)
        mcap = price * shares_now if shares_now > 0 else float(info.get("marketCap") or 0.0) / 1e9
        if mcap <= 0 or mcap < MIN_MARKET_CAP_B:
            return ModeCResult(Ticker=ticker, Status=f"Fail: 市值過低或無法取得 {mcap:.2f}B")

        debt_total = sec.latest_balance(df_debt_total)
        debt_current = sec.latest_balance(df_debt_current)
        # 若 DebtTotal 用的是純長債，補短債；若已包含流動+長期，補了會高估。因此只在 current 明顯且 total 概念不是 total 時保守補。
        debt_concept = df_debt_total["concept"].iloc[-1] if not df_debt_total.empty else ""
        if debt_current > 0 and debt_concept in {"LongTermDebt", "LongTermDebtAndCapitalLeaseObligations", "LongTermDebtAndFinanceLeaseObligations"}:
            total_debt = debt_total + debt_current
        else:
            total_debt = debt_total
        cash = sec.latest_balance(df_cash)
        ev = max(mcap + total_debt - cash, 0.01)

        dynamic_capex = min(abs(capex_ttm), abs(dna_ttm)) if dna_ttm > 0 and capex_ttm > 0 else max(abs(capex_ttm), abs(dna_ttm))
        real_fcf = ocf_ttm - dynamic_capex - sbc_ttm
        real_fcf_yield = safe_div(real_fcf, ev) * 100
        ebitda = ebit_ttm + dna_ttm
        ev_ebitda = safe_div(ev, ebitda)
        pe = safe_div(mcap, net_income_ttm)
        interest = max(interest_ttm, 0.001)
        icr = safe_div(ebit_ttm, interest)
        real_buyback = (buyback_ttm - issuance_ttm) - sbc_ttm
        dilution_illusion = real_buyback < 0

        flags = []
        rev_q = sec.quarterly_series(df_rev)
        if len(rev_q) >= 8:
            rev_ttm_now = rev_q.tail(4).sum()
            rev_ttm_prev = rev_q.iloc[-8:-4].sum()
            if rev_ttm_prev > 0 and rev_ttm_now < rev_ttm_prev and capex_ttm < dna_ttm * 0.60:
                flags.append("躺平式自殺虛高 Yield：CapEx 遠低於 D&A 且 TTM 營收下滑")

        if icr < ICR_WARNING:
            flags.append(f"財務脆弱：ICR<{ICR_WARNING}")
        if dilution_illusion:
            flags.append("稀釋幻覺：回購扣掉發股與 SBC 後為負")

        # EBITDA cross-check: yfinance TTM EBITDA vs SEC EBIT+D&A。
        yf_ebitda = float(info.get("ebitda") or 0.0) / 1e9
        if yf_ebitda > 0 and ebitda > 0:
            diff = abs(yf_ebitda - ebitda) / max(abs(ebitda), 0.001)
            if diff > 0.05:
                flags.append(f"EBITDA 交叉驗證差異>{diff*100:.1f}%：SEC EBIT+D&A vs yfinance EBITDA，需查 footnotes / non-recurring")

        # 歷史估值分位與雙殺測試。
        hv = historical_valuation(
            ticker, sec, df_ebit, df_dna, df_debt_total, df_cash, df_net_income, df_shares,
            ev_ebitda, pe, shares_now
        )
        ev_floor = hv["ev_ebitda_floor"]
        pe_floor = hv["pe_floor"]
        if not math.isfinite(ev_floor) or ev_floor <= 0:
            ev_floor = max(min(ev_ebitda * 0.60, ev_ebitda), 4.0)
            flags.append("歷史 EV/EBITDA 不足：雙殺改用保守 fallback floor")
        if not math.isfinite(pe_floor) or pe_floor <= 0:
            pe_floor = np.nan

        def stress_drawdown(drop: float) -> float:
            stress_ebitda = ebitda * (1 - drop)
            stress_ev = stress_ebitda * ev_floor
            stress_mcap_ev = max(0.0, stress_ev - (total_debt - cash))
            stress_mcap = stress_mcap_ev
            if math.isfinite(pe_floor) and pe_floor > 0 and net_income_ttm > 0:
                stress_ni = net_income_ttm * (1 - drop)
                stress_mcap_pe = max(0.0, stress_ni * pe_floor)
                stress_mcap = min(stress_mcap_ev, stress_mcap_pe)
            return safe_div(stress_mcap - mcap, mcap) * 100

        dd15 = stress_drawdown(0.15)
        dd30 = stress_drawdown(0.30)

        # 三季毛利診斷。
        gp_q = sec.quarterly_series(df_gp)
        gm_diag, gm_metrics = classify_three_quarter_trend(rev_q, gp_q)

        # 反推市場隱含 EBITDA CAGR。
        target_mult = np.nanmedian([x.get("EV_EBITDA") for x in hv.get("ev_ebitda_hist", []) if x.get("EV_EBITDA", np.nan) > 0])
        if not math.isfinite(target_mult) or target_mult <= 0:
            target_mult = max(ev_floor, 6.0)
        implied_cagr = implied_ebitda_cagr(ev, total_debt - cash, ebitda, target_mult) * 100

        # Short interest / Days to cover。
        si_float = info.get("shortPercentOfFloat")
        si_pct = float(si_float) * 100 if si_float is not None and si_float < 1 else float(si_float or np.nan)
        dtc = float(info.get("shortRatio") or info.get("daysToCover") or np.nan)
        squeeze = bool(math.isfinite(si_pct) and math.isfinite(dtc) and si_pct > SHORT_SQUEEZE_SI and dtc > SHORT_SQUEEZE_DTC)
        if squeeze:
            flags.append("高危易燃軋空結構：基本面看空也嚴禁裸空")

        # DSI。
        cogs_q = sec.quarterly_series(df_cogs)
        dsi = calc_dsi_series(df_inv, cogs_q)
        dsi_latest = float(dsi.iloc[-1]) if len(dsi) else np.nan
        dsi_2q_down = bool(len(dsi) >= 3 and dsi.iloc[-1] < dsi.iloc[-2] < dsi.iloc[-3])

        catalysts = get_upcoming_earnings(ticker)
        if not catalysts:
            catalysts = ["未偵測到 30 天內財報；需 Agent 補查法說/供應鏈月營收/產業會議"]

        # 直接結論與工具建議。
        if "結構性價值陷阱" in gm_diag or real_fcf <= 0 or (math.isfinite(icr) and icr < ICR_WARNING):
            verdict = "價值陷阱"
        elif math.isfinite(implied_cagr) and implied_cagr > 25 and (math.isfinite(hv["ev_ebitda_percentile"]) and hv["ev_ebitda_percentile"] > 70):
            verdict = "博弈泡沫"
        elif real_fcf_yield > 6 and (math.isfinite(hv["ev_ebitda_percentile"]) and hv["ev_ebitda_percentile"] <= 35) and not dilution_illusion:
            verdict = "實質防禦"
        else:
            verdict = "觀察名單：未構成重倉條件"

        if squeeze and verdict in {"價值陷阱", "博弈泡沫"}:
            trade_tool = "買入價外 Put / Put Spread，禁止裸空"
        elif verdict == "實質防禦":
            trade_tool = "正股小倉位 + 催化劑前加碼"
        elif verdict == "價值陷阱":
            trade_tool = "Sell Side / 排除；若要做空僅限期權"
        elif verdict == "博弈泡沫":
            trade_tool = "事件型 Put Spread；等待流動性鬆動"
        else:
            trade_tool = "不交易，等待 DSI/毛利/財報催化劑"

        agent_tasks = [
            f"核對 {ticker} 最新 10-K/10-Q footnotes，確認 EBITDA non-recurring / restructuring / impairment 調整。",
            f"抓取 {ticker} 未來 30 天財報、法說、投資人日、重大產業會議。",
            "若為半導體/AI 供應鏈：核對 TSMC 最新 CapEx / 先進製程產能、ASML EUV/DUV backlog 與交期。",
            "若客戶集中於 CSP：核對 MSFT/AMZN/GOOGL/META 最新 CapEx 指引與算力需求，判斷物理產能上限。",
            f"抓取官方或付費短倉資料，複核 {ticker} Short Interest % Float 與 Days to Cover。",
        ]

        return ModeCResult(
            Ticker=ticker,
            Status="Pass",
            Price=round(price, 2),
            MarketCap_B=round(mcap, 3),
            EV_B=round(ev, 3),
            Real_FCF_Yield_pct=round(real_fcf_yield, 2),
            TTM_OCF_B=round(ocf_ttm, 3),
            Dynamic_CapEx_B=round(dynamic_capex, 3),
            TTM_SBC_B=round(sbc_ttm, 3),
            ICR=round(icr, 2),
            Real_Buyback_B=round(real_buyback, 3),
            Dilution_Illusion=dilution_illusion,
            EBITDA_B=round(ebitda, 3),
            EV_EBITDA_x=round(ev_ebitda, 2),
            PE_x=round(pe, 2) if math.isfinite(pe) else np.nan,
            EV_EBITDA_10Y_Percentile=round(hv["ev_ebitda_percentile"], 1) if math.isfinite(hv["ev_ebitda_percentile"]) else np.nan,
            PE_10Y_Percentile=round(hv["pe_percentile"], 1) if math.isfinite(hv["pe_percentile"]) else np.nan,
            EBITDA_Drawdown_15_pct=round(dd15, 1),
            EBITDA_Drawdown_30_pct=round(dd30, 1),
            GM_Diagnosis=gm_diag,
            Rev_3Q_Change_pct=round(gm_metrics.get("rev_3q_change_pct", np.nan), 2),
            GM_Latest_pct=round(gm_metrics.get("gm_latest_pct", np.nan), 2),
            GM_3Q_Change_pp=round(gm_metrics.get("gm_3q_change_pp", np.nan), 2),
            Implied_EBITDA_CAGR_3Y_pct=round(implied_cagr, 2) if math.isfinite(implied_cagr) else np.nan,
            ShortInterest_pctFloat=round(si_pct, 2) if math.isfinite(si_pct) else np.nan,
            DaysToCover=round(dtc, 2) if math.isfinite(dtc) else np.nan,
            Squeeze_Risk=squeeze,
            DSI_Latest=round(dsi_latest, 1) if math.isfinite(dsi_latest) else np.nan,
            DSI_2Q_Down=dsi_2q_down,
            Catalysts_30D="; ".join(catalysts),
            Data_Quality_Flags="; ".join(flags) if flags else "OK",
            Verdict=verdict,
            Trade_Tool=trade_tool,
            Agent_Tasks=agent_tasks,
        )
    except Exception as e:
        logger.error(f"[{ticker}] pipeline error: {str(e)[:120]}")
        return ModeCResult(Ticker=ticker, Status=f"Error: {str(e)[:80]}")

# ==============================================================================
# 報告產生
# ==============================================================================
def render_stock_report(r: ModeCResult) -> str:
    if r.Status != "Pass":
        return f"## 【今日初選標的：{r.Ticker}】直接破題結論\n\n- 狀態：{r.Status}\n"

    squeeze_note = "高危易燃軋空，禁止裸空。" if r.Squeeze_Risk else "無明顯軋空禁制。"
    catalyst_note = r.Catalysts_30D
    lines = []
    lines.append(f"## 1. **【今日初選標的：{r.Ticker}】直接破題結論**")
    lines.append("")
    lines.append(f"**判定：{r.Verdict}。交易建議：{r.Trade_Tool}。**")
    lines.append(f"數據旗標：{r.Data_Quality_Flags}")
    lines.append("")
    lines.append("## 2. **【第一階段：防守清算數據表】**")
    lines.append("")
    lines.append("| 指標 | 數值 | 判讀 |")
    lines.append("|---|---:|---|")
    lines.append(f"| Real FCF Yield | {r.Real_FCF_Yield_pct:.2f}% | OCF - 動態CapEx - SBC，全扣。|")
    lines.append(f"| TTM OCF / Dynamic CapEx / TTM SBC | {r.TTM_OCF_B:.3f}B / {r.Dynamic_CapEx_B:.3f}B / {r.TTM_SBC_B:.3f}B | 不用單季年化。|")
    lines.append(f"| ICR | {r.ICR:.2f}x | {'脆弱' if r.ICR < ICR_WARNING else '可承受'} |")
    lines.append(f"| 實質回購 | {r.Real_Buyback_B:.3f}B | {'稀釋幻覺' if r.Dilution_Illusion else '真回饋'} |")
    lines.append(f"| EBITDA -15% 雙殺 Max Drawdown | {r.EBITDA_Drawdown_15_pct:.1f}% | 倍數打到自身近10年低分位。|")
    lines.append(f"| EBITDA -30% 雙殺 Max Drawdown | {r.EBITDA_Drawdown_30_pct:.1f}% | 壓力情境底線。|")
    lines.append("")
    lines.append("## 3. **【第二階段：進攻預期差與物理限制】**")
    lines.append("")
    lines.append("| 指標 | 數值 | 判讀 |")
    lines.append("|---|---:|---|")
    lines.append(f"| EV/EBITDA | {r.EV_EBITDA_x:.2f}x | 10年分位：{r.EV_EBITDA_10Y_Percentile}% |")
    lines.append(f"| P/E | {r.PE_x if math.isfinite(r.PE_x) else 'N/A'} | 10年分位：{r.PE_10Y_Percentile}% |")
    lines.append(f"| 最新三季營收變化 | {r.Rev_3Q_Change_pct:.2f}% | {r.GM_Diagnosis} |")
    lines.append(f"| 最新毛利率 / 三季毛利變化 | {r.GM_Latest_pct:.2f}% / {r.GM_3Q_Change_pp:.2f}pp | 毛利是價值陷阱的第一刀。|")
    lines.append(f"| 反推市場隱含 3Y EBITDA CAGR | {r.Implied_EBITDA_CAGR_3Y_pct:.2f}% | 需和上游物理產能對齊。|")
    lines.append(f"| 物理限制對齊 | - | {r.Physical_Check} |")
    lines.append("")
    lines.append("## 4. **【第三階段：動態博弈與催化劑定時】**")
    lines.append("")
    lines.append("| 指標 | 數值 | 判讀 |")
    lines.append("|---|---:|---|")
    lines.append(f"| Short Interest % Float | {r.ShortInterest_pctFloat if math.isfinite(r.ShortInterest_pctFloat) else 'N/A'} | {squeeze_note} |")
    lines.append(f"| Days to Cover | {r.DaysToCover if math.isfinite(r.DaysToCover) else 'N/A'} | 流動性擠壓測試。|")
    lines.append(f"| DSI 最新值 | {r.DSI_Latest if math.isfinite(r.DSI_Latest) else 'N/A'} | {'連續兩季下滑，有拐點' if r.DSI_2Q_Down else '未確認庫存拐點'} |")
    lines.append(f"| 未來30天事件 | - | {catalyst_note} |")
    lines.append(f"| 最終操盤工具 | - | {r.Trade_Tool} |")
    lines.append("")
    return "\n".join(lines)


def build_agent_payload(results: List[ModeCResult]) -> dict:
    payload = {
        "generated_at": datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(),
        "mission": "Mode C 三階段雙殺模型：Web Agent 對齊最新財報 footnotes、供應鏈物理限制、Short Interest 官方數據與催化劑。",
        "hard_screen_passed": [r.Ticker for r in results if r.Status == "Pass"],
        "tasks": [],
    }
    for r in results:
        if r.Status != "Pass":
            continue
        payload["tasks"].append(
            {
                "ticker": r.Ticker,
                "verdict_before_web": r.Verdict,
                "must_verify": r.Agent_Tasks,
                "numbers_to_challenge": {
                    "Real_FCF_Yield_pct": r.Real_FCF_Yield_pct,
                    "EV_EBITDA_x": r.EV_EBITDA_x,
                    "Implied_EBITDA_CAGR_3Y_pct": r.Implied_EBITDA_CAGR_3Y_pct,
                    "ShortInterest_pctFloat": r.ShortInterest_pctFloat,
                    "DaysToCover": r.DaysToCover,
                    "DSI_Latest": r.DSI_Latest,
                },
            }
        )
    return payload


def send_email_report(markdown: str, csv_path: str, receiver_email: str) -> None:
    sender_email = os.environ.get("EMAIL_SENDER")
    sender_pwd = os.environ.get("EMAIL_PASSWORD")
    if not sender_email or not sender_pwd:
        logger.warning("未設定 EMAIL_SENDER / EMAIL_PASSWORD，略過寄信。")
        return
    msg = EmailMessage()
    msg["Subject"] = f"[Mode C 三階段雙殺] 投資決策書 - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    msg["From"] = sender_email
    msg["To"] = receiver_email
    msg.set_content(markdown)
    if os.path.exists(csv_path):
        with open(csv_path, "rb") as f:
            msg.add_attachment(f.read(), maintype="text", subtype="csv", filename=os.path.basename(csv_path))
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender_email, sender_pwd)
        server.send_message(msg)
        logger.info("Email sent.")

# ==============================================================================
# 主程式
# ==============================================================================
def main() -> None:
    user_email = os.environ.get("USER_EMAIL", "a7924177@gmail.com")
    input_file = os.environ.get("QUALIFIED_UNIVERSE", QUALIFIED_UNIVERSE)
    if not os.path.exists(input_file):
        raise FileNotFoundError(f"找不到 {input_file}，請準備欄位 Ticker, CIK 的初選清單。")

    df = pd.read_csv(input_file)
    if "Ticker" not in df.columns or "CIK" not in df.columns:
        raise ValueError("qualified_universe.csv 必須包含 Ticker, CIK 欄位。")
    df["Ticker"] = df["Ticker"].astype(str).str.upper().str.strip()
    df["CIK"] = df["CIK"].astype(str).str.replace(".0", "", regex=False).str.zfill(10)
    universe = dict(zip(df["Ticker"], df["CIK"]))

    tickers = list(universe.keys()) + ["SPY", "QQQ", "^TNX"]
    pre_fetch_all_market_data(tickers, period="10y")
    pre_fetch_all_info(list(universe.keys()))

    logger.info(f"開始 Mode-C 清算：{len(universe)} 檔")
    max_workers = int(os.environ.get("MODE_C_WORKERS", "3"))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(ex.map(lambda kv: run_mode_c_pipeline(kv[0], kv[1], user_email), universe.items()))

    rows = [asdict(r) for r in results]
    for row in rows:
        row["Agent_Tasks"] = " | ".join(row.get("Agent_Tasks", []))
    out_df = pd.DataFrame(rows)
    out_df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    report = "# Mode C 三階段雙殺模型 — 今日投資決策書\n\n"
    report += f"產生時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    for r in results:
        report += render_stock_report(r) + "\n---\n\n"
    Path(OUTPUT_MD).write_text(report, encoding="utf-8")

    payload = build_agent_payload(results)
    Path(OUTPUT_JSON).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    save_json_cache(CACHE_FILE_SHARES, _VECTOR_CACHE)

    pass_df = out_df[out_df["Status"] == "Pass"].copy()
    logger.info(f"完成。Pass={len(pass_df)} / Total={len(out_df)}")
    logger.info(f"已輸出：{OUTPUT_CSV}, {OUTPUT_MD}, {OUTPUT_JSON}")

    if os.environ.get("SEND_EMAIL", "0") == "1":
        send_email_report(report, OUTPUT_CSV, user_email)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logger.critical(f"系統崩潰：{exc}")
        traceback.print_exc()
        raise
