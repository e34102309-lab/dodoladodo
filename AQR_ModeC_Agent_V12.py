"""
=============================================================================
AQR Mode-C Agent V13 — 70/30 長期價值研究框架
=============================================================================
用途：
1) 讀取每日硬篩初選清單 qualified_universe.csv（欄位：Ticker, CIK）
2) 使用 SEC XBRL Company Concept 對齊 TTM / 最新 10-K / 最新 10-Q
3) 產出：
   - mode_c_screen.csv              ：全量結構化數據總表
   - mode_c_shortlist.csv           ：產業分散後的長期研究候選
   - mode_c_report.md               ：長期價值研究報告
   - mode_c_agent_payload.json      ：交給 LLM / Web Agent 做物理限制驗證的任務包

核心修正：
- 完美還原稅務利益 (Tax Benefit)，強制執行三點勾稽防止非經常性損益欺騙。
- 將軋空水位 (Short Interest & DTC) 強制寫入 CSV 警示旗標。
- 徹底阻絕科技巨頭 (無傳統債務) 造成的 KeyError 熔斷。
=============================================================================
"""


from __future__ import annotations


import concurrent.futures
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
OUTPUT_SHORTLIST_CSV = "mode_c_shortlist.csv"
OUTPUT_MD = "mode_c_report.md"
OUTPUT_JSON = "mode_c_agent_payload.json"


# SEC Fair Access 官方上限是 10 req/s；這裡保守設 8。
SEC_MAX_CALLS_PER_SECOND = 8
SEC_TIMEOUT = 15


# ==============================================================================
# 70% ETF 核心 + 30% 主動選股：長期價值投資框架
# ==============================================================================
MIN_LIQUIDITY_USD = 15_000_000
MIN_MARKET_CAP_B = 5.0
ICR_WARNING = 3.0
SHORT_SQUEEZE_SI = 15.0
SHORT_SQUEEZE_DTC = 5.0
LOW_VALUATION_PERCENTILE = 5
ACTIVE_SLEEVE_LIMIT_PCT = 30.0
TARGET_SHORTLIST_SIZE = 12
MAX_PER_SECTOR = 3
STARTER_WEIGHT_PCT_TOTAL = 1.5
MAX_POSITION_WEIGHT_PCT_TOTAL = 3.0
MAX_SECTOR_WEIGHT_PCT_TOTAL = 9.0
MIN_LONG_TERM_SCORE = 60.0


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
        auto_adjust=False,
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
            "IncomeTaxExpenseBenefit": ["IncomeTaxExpenseBenefit", "CurrentIncomeTaxExpenseBenefit"]
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
        df["fy"] = pd.to_numeric(df["fy"], errors="coerce").astype("Int64")
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
    df_debt_current: pd.DataFrame,
    df_cash: pd.DataFrame,
    df_net_income: pd.DataFrame,
    df_shares: pd.DataFrame,
    current_ev_ebitda: float,
    current_pe: float,
    shares_now: float,
) -> dict:
    """Build comparable historical multiples from raw prices and filed annual data."""
    empty_result = {
        "ev_ebitda_hist": [],
        "pe_hist": [],
        "ev_ebitda_percentile": np.nan,
        "pe_percentile": np.nan,
        "ev_ebitda_floor": np.nan,
        "pe_floor": np.nan,
    }
    ebit_a = sec._annual_facts(df_ebit)
    if ebit_a.empty or "end" not in ebit_a.columns:
        return empty_result

    dna_a = sec._annual_facts(df_dna)
    ni_a = sec._annual_facts(df_net_income)
    debt_a = sec._annual_facts(df_debt)
    debt_current_a = sec._annual_facts(df_debt_current)
    cash_a = sec._annual_facts(df_cash)
    long_term_only = {
        "LongTermDebt",
        "LongTermDebtAndCapitalLeaseObligations",
        "LongTermDebtAndFinanceLeaseObligations",
    }
    debt_concept = str(df_debt["concept"].iloc[-1]) if not df_debt.empty and "concept" in df_debt.columns else ""

    rows = []
    for _, erow in ebit_a.tail(10).iterrows():
        period_end = pd.Timestamp(erow["end"])
        filed = erow.get("filed")
        valuation_date = pd.Timestamp(filed) if pd.notna(filed) else period_end
        price = get_price_asof(ticker, valuation_date)
        shares = sec.shares_asof(df_shares, period_end, fallback=shares_now)
        if price <= 0 or shares <= 0:
            continue
        mcap = price * shares

        def latest_asof(frame: pd.DataFrame) -> float:
            if frame.empty or "end" not in frame.columns:
                return 0.0
            sub = frame[frame["end"] <= period_end]
            return float(sub.sort_values("end")["val"].iloc[-1]) / 1e9 if not sub.empty else 0.0

        debt = latest_asof(debt_a)
        if debt_concept in long_term_only:
            debt += latest_asof(debt_current_a)
        cash = latest_asof(cash_a)
        dna_match = dna_a[dna_a["end"] == period_end] if not dna_a.empty else pd.DataFrame()
        ni_match = ni_a[ni_a["end"] == period_end] if not ni_a.empty else pd.DataFrame()
        dna = float(dna_match["val"].iloc[-1]) / 1e9 if not dna_match.empty else 0.0
        ni = float(ni_match["val"].iloc[-1]) / 1e9 if not ni_match.empty else np.nan
        ebit = float(erow["val"]) / 1e9
        ebitda = ebit + abs(dna)
        ev_ebitda = safe_div(mcap + debt - cash, ebitda)
        pe = safe_div(mcap, ni)
        rows.append({"end": str(period_end.date()), "filed": str(valuation_date.date()), "EV_EBITDA": ev_ebitda, "PE": pe})

    ev_hist = [r["EV_EBITDA"] for r in rows if math.isfinite(r["EV_EBITDA"]) and r["EV_EBITDA"] > 0]
    pe_hist = [r["PE"] for r in rows if math.isfinite(r["PE"]) and r["PE"] > 0]
    min_history = 5
    return {
        "ev_ebitda_hist": rows,
        "pe_hist": pe_hist,
        "ev_ebitda_percentile": percentile_rank(ev_hist, current_ev_ebitda) if len(ev_hist) >= min_history else np.nan,
        "pe_percentile": percentile_rank(pe_hist, current_pe) if len(pe_hist) >= min_history else np.nan,
        "ev_ebitda_floor": low_percentile(ev_hist) if len(ev_hist) >= min_history else np.nan,
        "pe_floor": low_percentile(pe_hist) if len(pe_hist) >= min_history else np.nan,
    }

def implied_ebitda_cagr(current_ev: float, net_debt: float, base_ebitda: float, target_multiple: float, years: int = 3) -> float:
    if base_ebitda <= 0 or current_ev <= 0 or target_multiple <= 0:
        return np.nan
    required = current_ev / target_multiple
    return (required / base_ebitda) ** (1 / years) - 1




# ==============================================================================
# 產業特定 Agent 驗證任務產生器
# ==============================================================================
def _info_blob(info: dict) -> str:
    """把 yfinance info 中可用的產業/業務描述合併成可搜尋字串。"""
    parts = [
        str(info.get("sector") or ""),
        str(info.get("industry") or ""),
        str(info.get("longBusinessSummary") or ""),
        str(info.get("quoteType") or ""),
    ]
    return " ".join(parts).lower()




def _has_any(haystack: str, needles: Iterable[str]) -> bool:
    haystack = haystack.lower()
    for needle in needles:
        n = needle.lower().strip()
        if not n:
            continue
        # 短詞如 chip/gpu/hbm/euv 需用字界，避免把 Chipotle、public、services 之類誤判成半導體。
        if len(n) <= 4 and n.replace("-", "").isalnum():
            if re.search(rf"\b{re.escape(n)}\b", haystack):
                return True
        elif n in haystack:
            return True
    return False




def build_agent_verification_plan(
    ticker: str,
    info: dict,
    implied_cagr_pct: float,
    real_fcf_yield_pct: float,
) -> Tuple[str, List[str]]:
    """
    依公司所屬產業動態產生 Physical_Check 與 must_verify。

    修正重點：
    - 只有半導體、AI 硬體、資料中心/CSP 相關標的才要求查 TSMC/ASML/CSP。
    - 非科技股改查自身產業的硬限制：需求、產能、庫存、法規、融資、商品價格等。
    - 高隱含 CAGR 標的一律增加「產業瓶頸可支撐性」壓力測試，但不套錯產業模板。
    """
    t = str(ticker).upper().strip()
    sector = str(info.get("sector") or "").strip()
    industry = str(info.get("industry") or "").strip()
    blob = _info_blob(info)


    csp_tickers = {"MSFT", "AMZN", "GOOG", "GOOGL", "META", "ORCL", "IBM"}
    semi_keywords = [
        "semiconductor", "semiconductors", "semi", "foundry", "fabless", "wafer",
        "lithography", "euv", "duv", "advanced packaging", "chip", "chips",
        "integrated circuit", "memory", "dram", "nand", "hbm",
        "gpu", "accelerator", "ai accelerator", "co-packaged optics",
    ]
    ai_hardware_keywords = [
        "data center", "datacenter", "server", "networking", "optical", "interconnect",
        "ethernet", "switching", "storage", "cooling", "power management",
        "electronic components", "hardware", "cloud infrastructure",
    ]
    software_keywords = [
        "software", "saas", "application", "cybersecurity", "internet content",
        "information technology services", "cloud software", "platform", "subscription",
    ]
    healthcare_keywords = [
        "healthcare", "biotechnology", "biotech", "drug", "pharmaceutical", "medical devices",
        "diagnostics", "clinical", "therapeutics", "managed care", "health information",
    ]
    financial_keywords = [
        "bank", "banks", "insurance", "asset management", "capital markets", "credit",
        "mortgage", "financial", "broker", "fintech", "payments",
    ]
    energy_keywords = [
        "oil", "gas", "lng", "energy", "refining", "exploration", "production",
        "midstream", "coal", "renewable", "solar", "wind", "uranium",
    ]
    consumer_keywords = [
        "retail", "restaurant", "apparel", "consumer", "food", "beverage", "travel",
        "hotel", "casino", "auto", "household", "personal products",
    ]
    industrial_keywords = [
        "industrial", "machinery", "aerospace", "defense", "transport", "railroad",
        "trucking", "logistics", "electrical equipment", "building products",
    ]
    materials_keywords = [
        "materials", "chemical", "steel", "aluminum", "copper", "mining", "paper",
        "packaging", "construction materials", "fertilizer",
    ]
    real_estate_keywords = ["reit", "real estate", "property", "residential", "office", "retail reit"]
    utility_keywords = ["utility", "utilities", "regulated electric", "water utility", "gas utility"]


    tasks: List[str] = [
        f"核對 {t} 最新 10-K/10-Q footnotes，確認 EBITDA non-recurring / restructuring / impairment / litigation / tax benefit 等調整項。",
        f"抓取 {t} 未來 30 天財報、法說、投資人日、重大產業會議與公司公告。",
    ]


    is_csp = t in csp_tickers or _has_any(blob, ["hyperscale", "cloud infrastructure", "public cloud", "data center"])
    is_semi_or_ai_hardware = _has_any(blob, semi_keywords) or (
        sector.lower() == "technology" and _has_any(blob, ai_hardware_keywords)
    )


    if is_semi_or_ai_hardware:
        physical_check = (
            "半導體/AI硬體：核對 TSMC 先進製程/CoWoS/先進封裝產能、ASML EUV/DUV backlog 與交期、"
            "HBM/ABF/電力/散熱/伺服器供應鏈，以及 CSP CapEx 是否足以支撐隱含 EBITDA CAGR。"
        )
        tasks.append(f"【適用】{physical_check}")
    elif is_csp:
        physical_check = (
            "CSP/雲端/資料中心：核對 MSFT/AMZN/GOOGL/META/ORCL 等 CapEx 指引、資料中心供電/併網/機櫃/冷卻、"
            "GPU/HBM 取得能力與折舊壓力；不直接用 TSMC 作為唯一瓶頸。"
        )
        tasks.append(f"【適用】{physical_check}")
    elif sector.lower() == "technology" or _has_any(blob, software_keywords):
        physical_check = (
            "軟體/網路/IT服務：不套用 TSMC/ASML。核對 ARR/RPO、NRR/churn、雲端用量、席次擴張、定價權、"
            "SBC 稀釋與客戶預算週期是否支撐隱含 EBITDA CAGR。"
        )
        tasks.append(f"【適用】{physical_check}")
    elif sector.lower() == "healthcare" or _has_any(blob, healthcare_keywords):
        physical_check = (
            "醫療/生技：不套用 TSMC/ASML。核對 FDA/PDUFA/臨床讀出、專利懸崖、reimbursement、藥價壓力、"
            "產能/供應短缺與 payer mix。"
        )
        tasks.append(f"【適用】{physical_check}")
    elif sector.lower() == "financial services" or _has_any(blob, financial_keywords):
        physical_check = (
            "金融：不套用 TSMC/ASML。核對 NIM、存款 beta、信用損失/逾放、資本適足率、流動性、商辦/消費信貸曝險與殖利率曲線。"
        )
        tasks.append(f"【適用】{physical_check}")
    elif sector.lower() == "energy" or _has_any(blob, energy_keywords):
        physical_check = (
            "能源：不套用 TSMC/ASML。核對油氣/LNG/電價曲線、crack spread、儲量/decline rate、hedging、"
            "管線/液化/運輸產能與維持性 CapEx。"
        )
        tasks.append(f"【適用】{physical_check}")
    elif sector.lower() in {"consumer cyclical", "consumer defensive"} or _has_any(blob, consumer_keywords):
        physical_check = (
            "消費/零售：不套用 TSMC/ASML。核對 same-store sales、客流/客單、促銷強度、庫存週轉、折扣壓力、"
            "消費信貸與供應鏈交期。"
        )
        tasks.append(f"【適用】{physical_check}")
    elif sector.lower() == "industrials" or _has_any(blob, industrial_keywords):
        physical_check = (
            "工業：不套用 TSMC/ASML。核對訂單/backlog、book-to-bill、產能利用率、交期、原物料/工資成本、"
            "PMI/終端需求與客戶 CapEx 週期。"
        )
        tasks.append(f"【適用】{physical_check}")
    elif sector.lower() == "basic materials" or _has_any(blob, materials_keywords):
        physical_check = (
            "原物料：不套用 TSMC/ASML。核對商品價格、礦山/冶煉產能、現金成本曲線、庫存、能源成本、"
            "環保/出口限制與下游客戶補庫。"
        )
        tasks.append(f"【適用】{physical_check}")
    elif sector.lower() == "real estate" or _has_any(blob, real_estate_keywords):
        physical_check = (
            "REIT/不動產：不套用 TSMC/ASML。核對 occupancy、leasing spread、租約到期、再融資牆、cap rate、"
            "利率敏感度與資產出售能力。"
        )
        tasks.append(f"【適用】{physical_check}")
    elif sector.lower() == "utilities" or _has_any(blob, utility_keywords):
        physical_check = (
            "公用事業：不套用 TSMC/ASML。核對核准 ROE、rate base 成長、燃料成本轉嫁、電網/發電 CapEx、"
            "利率與監管案件時程。"
        )
        tasks.append(f"【適用】{physical_check}")
    else:
        physical_check = (
            "通用產業瓶頸：非半導體/AI/CSP，不套用 TSMC/ASML/CSP 模板。核對本業需求、產能、訂單、庫存、價格、"
            "融資與法規限制是否支撐隱含 EBITDA CAGR。"
        )
        tasks.append(f"【適用】{physical_check}")


    if math.isfinite(implied_cagr_pct):
        if implied_cagr_pct > 25.0:
            tasks.append(
                f"高隱含成長壓力測試：{t} Implied EBITDA CAGR 3Y={implied_cagr_pct:.1f}%，必須用上述產業瓶頸逐項驗證；無證據則降級為博弈泡沫。"
            )
        elif implied_cagr_pct < -10.0:
            tasks.append(
                f"反向預期差壓力測試：{t} Implied EBITDA CAGR 3Y={implied_cagr_pct:.1f}%，確認市場是否過度折價或基本面永久受損。"
            )


    if math.isfinite(real_fcf_yield_pct) and real_fcf_yield_pct > 8.0:
        tasks.append(f"高 FCF Yield 防偽：核對 {t} 是否因維持性 CapEx 低估、一次性營運資金流入或裁員/重組造成短期美化。")


    tasks.append(f"抓取官方或付費短倉資料，複核 {t} Short Interest % Float 與 Days to Cover；高軋空僅列為波動風險，不放寬基本面門檻。")
    return physical_check, tasks




SUPPORTED_EQUITY_EXCHANGES = {"NMS", "NYQ", "NGM", "NCM", "ASE", "PCX"}


def common_equity_rejection_reason(ticker: str, info: dict) -> str:
    """Reject funds, OTC listings, preferred/debt instruments, and incomplete metadata."""
    if not re.fullmatch(r"[A-Z]{1,6}", str(ticker).upper()):
        return "非標準普通股代號"
    quote_type = str(info.get("quoteType") or "").upper()
    if quote_type != "EQUITY":
        return f"非普通股商品 quoteType={quote_type or 'missing'}"
    exchange = str(info.get("exchange") or "").upper()
    if exchange not in SUPPORTED_EQUITY_EXCHANGES:
        return f"非主要美國交易所 exchange={exchange or 'missing'}"
    if info.get("fundFamily") or info.get("category"):
        return "基金或 ETF"
    if not str(info.get("sector") or "").strip() or not str(info.get("industry") or "").strip():
        return "產業分類缺失"
    return ""


def prepare_uploaded_universe(df: pd.DataFrame) -> Dict[str, str]:
    """Validate a manually uploaded universe after yf.info has been prefetched."""
    work = df.copy()
    if "Status" in work.columns:
        work = work[work["Status"].astype(str).str.upper().eq("PASS")]
    accepted: Dict[str, str] = {}
    seen_ciks = set()
    rejected = []
    for _, row in work.iterrows():
        ticker = str(row["Ticker"]).upper().strip()
        cik = str(row["CIK"]).replace(".0", "").zfill(10)
        reason = common_equity_rejection_reason(ticker, safe_yf_info(ticker))
        if not cik.isdigit() or int(cik) <= 0:
            reason = reason or "CIK 無效"
        if cik in seen_ciks:
            reason = reason or "同一 CIK 重複上市商品"
        if reason:
            rejected.append(f"{ticker}: {reason}")
            continue
        accepted[ticker] = cik
        seen_ciks.add(cik)
    if rejected:
        logger.warning("名單驗證排除 %d 檔；範例：%s", len(rejected), "; ".join(rejected[:12]))
    if not accepted:
        raise ValueError("上傳名單經普通股與資料完整性驗證後為空。")
    return accepted


def _bounded_score(value: float, low: float, high: float, missing: float = 0.0) -> float:
    if not math.isfinite(value):
        return missing
    if high <= low:
        return missing
    return float(max(0.0, min(100.0, (value - low) / (high - low) * 100.0)))


def _expectations_score(implied_cagr: float) -> float:
    if not math.isfinite(implied_cagr):
        return 20.0
    if 0.0 <= implied_cagr <= 15.0:
        return 100.0
    if -5.0 <= implied_cagr < 0.0 or 15.0 < implied_cagr <= 20.0:
        return 75.0
    if -10.0 <= implied_cagr < -5.0 or 20.0 < implied_cagr <= 25.0:
        return 40.0
    if 25.0 < implied_cagr <= 30.0:
        return 20.0
    return 0.0


def calculate_long_term_scores(r: "ModeCResult") -> Dict[str, float]:
    valuation_pct = r.EV_EBITDA_10Y_Percentile
    valuation_score = 100.0 - valuation_pct if math.isfinite(valuation_pct) and 0.0 <= valuation_pct <= 100.0 else 20.0
    fcf_score = _bounded_score(r.Real_FCF_Yield_pct, 0.0, 10.0, missing=0.0)
    value_score = valuation_score * 0.55 + fcf_score * 0.45

    if math.isinf(r.ICR) and r.ICR > 0:
        icr_score = 100.0
    else:
        icr_score = _bounded_score(r.ICR, 1.0, 10.0, missing=20.0)
    fcf_quality = 100.0 if r.Real_FCF_Yield_pct >= 5.0 else 70.0 if r.Real_FCF_Yield_pct >= 2.0 else 35.0 if r.Real_FCF_Yield_pct > 0 else 0.0
    if "暫時落難好股" in r.GM_Diagnosis:
        trend_score = 85.0
    elif "中性" in r.GM_Diagnosis:
        trend_score = 70.0
    elif "結構性價值陷阱" in r.GM_Diagnosis or "雙重惡化" in r.GM_Diagnosis:
        trend_score = 0.0
    else:
        trend_score = 25.0
    if r.Dilution_Illusion:
        shareholder_score = 0.0
    elif math.isfinite(r.Share_Count_Change_pct):
        shareholder_score = 100.0 if r.Share_Count_Change_pct <= 0.0 else 65.0 if r.Share_Count_Change_pct <= 1.0 else 25.0
    else:
        shareholder_score = 40.0
    quality_score = icr_score * 0.35 + fcf_quality * 0.25 + trend_score * 0.25 + shareholder_score * 0.15

    expectations_score = _expectations_score(r.Implied_EBITDA_CAGR_3Y_pct)
    momentum_score = _bounded_score(r.Momentum_12M_pct, -30.0, 30.0, missing=50.0)

    risk_penalty = 0.0
    if math.isfinite(r.EBITDA_Drawdown_30_pct):
        if r.EBITDA_Drawdown_30_pct <= -80.0:
            risk_penalty += 35.0
        elif r.EBITDA_Drawdown_30_pct <= -60.0:
            risk_penalty += 25.0
        elif r.EBITDA_Drawdown_30_pct <= -40.0:
            risk_penalty += 15.0
        elif r.EBITDA_Drawdown_30_pct <= -25.0:
            risk_penalty += 5.0
    else:
        risk_penalty += 10.0
    if r.Dilution_Illusion:
        risk_penalty += 20.0
    if "結構性價值陷阱" in r.GM_Diagnosis:
        risk_penalty += 25.0
    if "雙重惡化" in r.GM_Diagnosis:
        risk_penalty += 35.0
    if r.Squeeze_Risk:
        risk_penalty += 8.0
    if r.Data_Quality_Flags and r.Data_Quality_Flags != "OK":
        risk_penalty += 5.0

    long_term_score = value_score * 0.35 + quality_score * 0.35 + expectations_score * 0.20 + momentum_score * 0.10 - risk_penalty
    return {
        "value_score": round(value_score, 2),
        "quality_score": round(quality_score, 2),
        "expectations_score": round(expectations_score, 2),
        "risk_penalty": round(risk_penalty, 2),
        "long_term_score": round(max(0.0, min(100.0, long_term_score)), 2),
    }


def apply_long_term_framework(r: "ModeCResult") -> "ModeCResult":
    scores = calculate_long_term_scores(r)
    r.Value_Score = scores["value_score"]
    r.Quality_Score = scores["quality_score"]
    r.Expectations_Score = scores["expectations_score"]
    r.Risk_Penalty = scores["risk_penalty"]
    r.Long_Term_Score = scores["long_term_score"]

    cagr_ok = math.isfinite(r.Implied_EBITDA_CAGR_3Y_pct) and -10.0 <= r.Implied_EBITDA_CAGR_3Y_pct <= 30.0
    drawdown_ok = math.isfinite(r.EBITDA_Drawdown_30_pct) and r.EBITDA_Drawdown_30_pct > -75.0
    trend_ok = not any(x in r.GM_Diagnosis for x in ["結構性價值陷阱", "雙重惡化", "資料不足"])
    r.Long_Term_Eligible = bool(
        r.Status == "Pass"
        and r.Long_Term_Score >= MIN_LONG_TERM_SCORE
        and math.isfinite(r.EV_EBITDA_10Y_Percentile)
        and r.Real_FCF_Yield_pct >= 2.0
        and (math.isinf(r.ICR) or r.ICR >= ICR_WARNING)
        and not r.Dilution_Illusion
        and cagr_ok
        and drawdown_ok
        and trend_ok
    )

    if r.Long_Term_Eligible and r.Long_Term_Score >= 70.0:
        r.Verdict = "研究優先：品質與估值同時達標"
        r.Research_Action = "完成投資論點、熊市情境、失效條件與 ETF 重疊檢查後，可考慮 1.5% 總資產起始部位"
        r.Suggested_Starter_Weight_pct_Total = STARTER_WEIGHT_PCT_TOTAL
    elif r.Long_Term_Eligible:
        r.Verdict = "研究候選：達標但安全邊際普通"
        r.Research_Action = "先列入觀察；只有在估值改善或研究信心提高時才建立小部位"
        r.Suggested_Starter_Weight_pct_Total = 0.0
    elif r.Status == "Pass":
        r.Verdict = "觀察：未達長期持有門檻"
        r.Research_Action = "不自動買入；等待品質、估值或下檔風險改善"
        r.Suggested_Starter_Weight_pct_Total = 0.0
    else:
        r.Verdict = "排除"
        r.Research_Action = "不進入主動投資研究池"
        r.Suggested_Starter_Weight_pct_Total = 0.0
    r.Trade_Tool = r.Research_Action
    return r


def composite_score_for_result(r: "ModeCResult") -> float:
    """Backward-compatible sort key: lower is better."""
    return 100.0 - calculate_long_term_scores(r)["long_term_score"]


def select_diversified_shortlist(
    results: List["ModeCResult"],
    target_size: int = TARGET_SHORTLIST_SIZE,
    max_per_sector: int = MAX_PER_SECTOR,
) -> List["ModeCResult"]:
    candidates = sorted(
        [r for r in results if r.Status == "Pass" and r.Long_Term_Eligible],
        key=lambda r: (-r.Long_Term_Score, r.Ticker),
    )
    selected: List[ModeCResult] = []
    sector_counts: Dict[str, int] = {}
    for r in candidates:
        sector = (r.Sector or "Unknown").strip() or "Unknown"
        if sector_counts.get(sector, 0) >= max_per_sector:
            continue
        selected.append(r)
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        if len(selected) >= target_size:
            break
    return selected


@dataclass
class ModeCResult:
    Ticker: str
    Status: str
    Price: float = np.nan
    Sector: str = ""
    Industry: str = ""
    MarketCap_B: float = np.nan
    EV_B: float = np.nan
    Real_FCF_Yield_pct: float = np.nan
    TTM_OCF_B: float = np.nan
    Dynamic_CapEx_B: float = np.nan
    TTM_SBC_B: float = np.nan
    ICR: float = np.nan
    Real_Buyback_B: float = np.nan
    Share_Count_Change_pct: float = np.nan
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
    Momentum_12M_pct: float = np.nan
    Value_Score: float = np.nan
    Quality_Score: float = np.nan
    Expectations_Score: float = np.nan
    Risk_Penalty: float = np.nan
    Long_Term_Score: float = np.nan
    Long_Term_Eligible: bool = False
    Research_Action: str = ""
    Suggested_Starter_Weight_pct_Total: float = 0.0
    Physical_Check: str = "依產業分類動態產生；非半導體/AI/CSP 不套用 TSMC/ASML/CSP。"
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
        rejection_reason = common_equity_rejection_reason(ticker, info)
        if rejection_reason:
            return ModeCResult(Ticker=ticker, Status=f"Fail: 名單驗證 ({rejection_reason})")
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
        df_tax = sec.fetch_concept(cik, "IncomeTaxExpenseBenefit") # 補齊：強制抓取稅務標籤以完成勾稽


        required_facts = {
            "OCF": df_ocf, "Revenue": df_rev, "EBIT": df_ebit, "CapEx": df_capex,
            "D&A": df_dna, "GrossProfit": df_gp, "NetIncome": df_net_income,
        }
        missing_facts = [name for name, frame in required_facts.items() if frame.empty]
        if missing_facts:
            return ModeCResult(Ticker=ticker, Status=f"Fail: SEC 核心資料缺失 {'/'.join(missing_facts)}")


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
        tax_ttm, _, _ = sec.ttm_flow(df_tax) if not df_tax.empty else (0.0, "", {}) # 補齊：計算稅務 TTM


        if capex_ttm <= 0:
            return ModeCResult(Ticker=ticker, Status="Fail: CapEx TTM 缺失或非正值")
        if sbc_ttm == 0:
            sbc_ttm = abs(float(info.get("shareBasedCompensation") or 0.0)) / 1e9


        shares_sec_now, shares_1y_ago = sec.get_shares_now_and_1y(df_shares)
        shares_now = get_robust_shares(ticker, df_shares, sec, info)
        mcap = price * shares_now if shares_now > 0 else float(info.get("marketCap") or 0.0) / 1e9
        if mcap <= 0 or mcap < MIN_MARKET_CAP_B:
            return ModeCResult(Ticker=ticker, Status=f"Fail: 市值過低或無法取得 {mcap:.2f}B")

        debt_total = sec.latest_balance(df_debt_total)
        debt_current = sec.latest_balance(df_debt_current)
        debt_concept = df_debt_total["concept"].iloc[-1] if not df_debt_total.empty else ""
        if debt_current > 0 and debt_concept in {"LongTermDebt", "LongTermDebtAndCapitalLeaseObligations", "LongTermDebtAndFinanceLeaseObligations"}:
            total_debt = debt_total + debt_current
        else:
            total_debt = debt_total
        cash = sec.latest_balance(df_cash)
        ev = max(mcap + total_debt - cash, 0.01)

        dynamic_capex = abs(capex_ttm)
        real_fcf = ocf_ttm - dynamic_capex - sbc_ttm
        real_fcf_yield = safe_div(real_fcf, ev) * 100
        ebitda = ebit_ttm + dna_ttm
        if ebitda <= 0:
            return ModeCResult(Ticker=ticker, Status="Fail: EBITDA 非正值")
        ev_ebitda = safe_div(ev, ebitda)
        pe = safe_div(mcap, net_income_ttm)
        if total_debt > 0.01:
            if df_int.empty or interest_ttm <= 0:
                return ModeCResult(Ticker=ticker, Status="Fail: 有負債但利息費用缺失")
            icr = safe_div(ebit_ttm, interest_ttm)
        else:
            icr = math.inf

        real_buyback = buyback_ttm - issuance_ttm
        share_change_pct = safe_div(shares_sec_now, shares_1y_ago) * 100 - 100 if shares_sec_now > 0 and shares_1y_ago > 0 else np.nan
        dilution_illusion = bool(
            (math.isfinite(share_change_pct) and share_change_pct > 0.5)
            or (real_buyback > 0 and math.isfinite(share_change_pct) and share_change_pct >= 0)
        )

        flags = []
        if not math.isfinite(share_change_pct):
            flags.append("股數歷史不足：無法驗證實質回購")
        rev_q = sec.quarterly_series(df_rev)
        if len(rev_q) >= 8:
            rev_ttm_now = rev_q.tail(4).sum()
            rev_ttm_prev = rev_q.iloc[-8:-4].sum()
            if rev_ttm_prev > 0 and rev_ttm_now < rev_ttm_prev and capex_ttm < dna_ttm * 0.60:
                flags.append("躺平式自殺虛高 Yield：CapEx 遠低於 D&A 且 TTM 營收下滑")


        if icr < ICR_WARNING:
            flags.append(f"財務脆弱：ICR<{ICR_WARNING}")
        if dilution_illusion:
            flags.append("稀釋幻覺：公司回購現金為正，但流通股數未下降或仍增加")


        # 三點勾稽僅在各組成資料存在時執行，避免把缺值當成零。
        yf_ebitda = float(info.get("ebitda") or 0.0) / 1e9
        if yf_ebitda > 0 and ebitda > 0:
            diff_1 = safe_div(abs(ebitda - yf_ebitda), max(abs(ebitda), 0.001))
            diff_2 = np.nan
            if not df_tax.empty and (total_debt <= 0.01 or not df_int.empty):
                sec_ebitda_2 = net_income_ttm + interest_ttm + tax_ttm + dna_ttm
                diff_2 = safe_div(abs(ebitda - sec_ebitda_2), max(abs(ebitda), 0.001))
            if diff_1 > 0.05 or (math.isfinite(diff_2) and diff_2 > 0.05):
                flags.append("數據勾稽警報：EBITDA 來源差異率超出5%，需查非經常性項目")


        hv = historical_valuation(
            ticker, sec, df_ebit, df_dna, df_debt_total, df_debt_current, df_cash, df_net_income, df_shares,
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


        gp_q = sec.quarterly_series(df_gp)
        gm_diag, gm_metrics = classify_three_quarter_trend(rev_q, gp_q)


        target_mult = np.nanmedian([x.get("EV_EBITDA") for x in hv.get("ev_ebitda_hist", []) if x.get("EV_EBITDA", np.nan) > 0])
        if not math.isfinite(target_mult) or target_mult <= 0:
            target_mult = max(ev_floor, 6.0)
        implied_cagr = implied_ebitda_cagr(ev, total_debt - cash, ebitda, target_mult) * 100


        # 【修正核心】：軋空動態數據旗標寫入
        si_float = info.get("shortPercentOfFloat")
        si_pct = float(si_float) * 100 if si_float is not None and si_float < 1 else float(si_float or np.nan)
        dtc = float(info.get("shortRatio") or info.get("daysToCover") or np.nan)
        squeeze = bool(math.isfinite(si_pct) and math.isfinite(dtc) and si_pct > SHORT_SQUEEZE_SI and dtc > SHORT_SQUEEZE_DTC)
        if squeeze:
            flags.append(f"高軋空波動風險(SI={si_pct:.1f}%, DTC={dtc:.1f})：不因事件題材放寬基本面門檻")


        cogs_q = sec.quarterly_series(df_cogs)
        dsi = calc_dsi_series(df_inv, cogs_q)
        dsi_latest = float(dsi.iloc[-1]) if len(dsi) else np.nan
        dsi_2q_down = bool(len(dsi) >= 3 and dsi.iloc[-1] < dsi.iloc[-2] < dsi.iloc[-3])


        catalysts = get_upcoming_earnings(ticker)
        if not catalysts:
            catalysts = ["未偵測到 30 天內財報；需 Agent 補查法說/供應鏈月營收/產業會議"]


        physical_check, agent_tasks = build_agent_verification_plan(
            ticker=ticker,
            info=info,
            implied_cagr_pct=implied_cagr,
            real_fcf_yield_pct=real_fcf_yield,
        )

        # 先排除財務結構明顯不適合長期持有的公司；其餘交給多因子框架排序。
        status = "Pass"
        if gm_diag.startswith("資料不足"):
            status = "Fail: 季度毛利資料不足"
        elif math.isfinite(icr) and icr < 1.0:
            status = "Fail: ICR < 1.0，財務韌性不足"
        elif real_fcf <= 0:
            status = "Fail: Real FCF 非正值"
        elif "雙重惡化" in gm_diag:
            status = "Fail: 營收與毛利同步惡化"


        result = ModeCResult(
            Ticker=ticker,
            Status=status,
            Price=round(price, 2),
            Sector=str(info.get("sector") or ""),
            Industry=str(info.get("industry") or ""),
            MarketCap_B=round(mcap, 3),
            EV_B=round(ev, 3),
            Real_FCF_Yield_pct=round(real_fcf_yield, 2),
            TTM_OCF_B=round(ocf_ttm, 3),
            Dynamic_CapEx_B=round(dynamic_capex, 3),
            TTM_SBC_B=round(sbc_ttm, 3),
            ICR=round(icr, 2),
            Real_Buyback_B=round(real_buyback, 3),
            Share_Count_Change_pct=round(share_change_pct, 2) if math.isfinite(share_change_pct) else np.nan,
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
            Momentum_12M_pct=round(pm["momentum_12m"], 2) if pm.get("momentum_12m") is not None and math.isfinite(pm["momentum_12m"]) else np.nan,
            Physical_Check=physical_check,
            ShortInterest_pctFloat=round(si_pct, 2) if math.isfinite(si_pct) else np.nan,
            DaysToCover=round(dtc, 2) if math.isfinite(dtc) else np.nan,
            Squeeze_Risk=squeeze,
            DSI_Latest=round(dsi_latest, 1) if math.isfinite(dsi_latest) else np.nan,
            DSI_2Q_Down=dsi_2q_down,
            Catalysts_30D="; ".join(catalysts),
            Data_Quality_Flags="; ".join(flags) if flags else "OK",
            Verdict="",
            Trade_Tool="",
            Agent_Tasks=agent_tasks,
        )
        return apply_long_term_framework(result)
    except Exception as e:
        logger.error(f"[{ticker}] pipeline error: {str(e)[:120]}")
        return ModeCResult(Ticker=ticker, Status=f"Error: {str(e)[:80]}")


# ==============================================================================
# 報告產生
# ==============================================================================
def render_stock_report(r: ModeCResult) -> str:
    lines = []
    lines.append(f"## {r.Ticker} — {r.Verdict}")
    lines.append("")
    lines.append(f"- 產業：{r.Sector or 'N/A'} / {r.Industry or 'N/A'}")
    lines.append(f"- 長期綜合分數：{r.Long_Term_Score:.2f} / 100")
    lines.append(f"- 研究動作：{r.Research_Action}")
    lines.append(f"- 建議起始權重：{r.Suggested_Starter_Weight_pct_Total:.1f}% 總資產；單一公司上限 {MAX_POSITION_WEIGHT_PCT_TOTAL:.1f}%")
    lines.append("")
    lines.append("### 四構面評分")
    lines.append("")
    lines.append("| 構面 | 分數/數值 |")
    lines.append("|---|---:|")
    lines.append(f"| 價值分數 | {r.Value_Score:.2f} |")
    lines.append(f"| 品質分數 | {r.Quality_Score:.2f} |")
    lines.append(f"| 市場預期分數 | {r.Expectations_Score:.2f} |")
    lines.append(f"| 風險扣分 | -{r.Risk_Penalty:.2f} |")
    lines.append(f"| Real FCF Yield | {r.Real_FCF_Yield_pct:.2f}% |")
    lines.append(f"| EV/EBITDA 10Y 分位 | {r.EV_EBITDA_10Y_Percentile if math.isfinite(r.EV_EBITDA_10Y_Percentile) else 'N/A'} |")
    lines.append(f"| ICR | {r.ICR:.2f}x |")
    lines.append(f"| 市場隱含 3Y EBITDA CAGR | {r.Implied_EBITDA_CAGR_3Y_pct if math.isfinite(r.Implied_EBITDA_CAGR_3Y_pct) else 'N/A'}% |")
    lines.append(f"| 12M 動能（僅輔助） | {r.Momentum_12M_pct if math.isfinite(r.Momentum_12M_pct) else 'N/A'}% |")
    lines.append("")
    lines.append("### 下檔與論點驗證")
    lines.append("")
    lines.append(f"- EBITDA -30% 壓力情境：{r.EBITDA_Drawdown_30_pct:.1f}%")
    lines.append(f"- 毛利診斷：{r.GM_Diagnosis}")
    lines.append(f"- 股數變化：{r.Share_Count_Change_pct if math.isfinite(r.Share_Count_Change_pct) else 'N/A'}%；稀釋幻覺={r.Dilution_Illusion}")
    lines.append(f"- 軋空風險：{r.Squeeze_Risk}（只作風險旗標，不作做空或期權訊號）")
    lines.append(f"- 數據品質：{r.Data_Quality_Flags}")
    lines.append(f"- 產業物理限制：{r.Physical_Check}")
    lines.append(f"- 近期事件：{r.Catalysts_30D}")
    lines.append("")
    return "\n".join(lines)



def build_agent_payload(results: List[ModeCResult]) -> dict:
    payload = {
        "generated_at": datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(),
        "mission": "70% ETF 核心 + 最多 30% 主動選股的長期價值研究。只做多、不使用槓桿、期權或放空；模型只產生研究候選，不是自動買入訊號。",
        "portfolio_limits": {
            "active_sleeve_pct_total": ACTIVE_SLEEVE_LIMIT_PCT,
            "starter_weight_pct_total": STARTER_WEIGHT_PCT_TOTAL,
            "max_position_weight_pct_total": MAX_POSITION_WEIGHT_PCT_TOTAL,
            "max_sector_weight_pct_total": MAX_SECTOR_WEIGHT_PCT_TOTAL,
            "max_names": TARGET_SHORTLIST_SIZE,
            "max_names_per_sector": MAX_PER_SECTOR,
        },
        "research_shortlist": [r.Ticker for r in results if r.Long_Term_Eligible],
        "tasks": [],
    }
    for r in results:
        if not r.Long_Term_Eligible:
            continue
        payload["tasks"].append(
            {
                "ticker": r.Ticker,
                "sector": r.Sector,
                "industry": r.Industry,
                "long_term_score": r.Long_Term_Score,
                "research_action": r.Research_Action,
                "suggested_starter_weight_pct_total": r.Suggested_Starter_Weight_pct_Total,
                "must_verify": r.Agent_Tasks + [
                    "寫出三句話投資論點與可反證條件",
                    "建立基準/樂觀/悲觀三情境估值區間",
                    "檢查既有 ETF 的個股與產業重疊",
                    "確認管理層資本配置與股數稀釋紀錄",
                ],
                "numbers_to_challenge": {
                    "Value_Score": r.Value_Score,
                    "Quality_Score": r.Quality_Score,
                    "Expectations_Score": r.Expectations_Score,
                    "Risk_Penalty": r.Risk_Penalty,
                    "Real_FCF_Yield_pct": r.Real_FCF_Yield_pct,
                    "EV_EBITDA_10Y_Percentile": r.EV_EBITDA_10Y_Percentile,
                    "Implied_EBITDA_CAGR_3Y_pct": r.Implied_EBITDA_CAGR_3Y_pct,
                    "EBITDA_Drawdown_30_pct": r.EBITDA_Drawdown_30_pct,
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
    msg["Subject"] = f"[Mode C 長期價值] 研究名單 - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
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
# 主程式：長期價值多因子研究漏斗（產業分散後最多 12 檔）
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
    raw_tickers = list(dict.fromkeys(df["Ticker"].tolist()))

    # 名單由本機人工更新；GitHub Actions 不重跑耗時的全市場獵人。
    pre_fetch_all_market_data(raw_tickers + ["SPY", "QQQ", "^TNX"], period="10y")
    pre_fetch_all_info(raw_tickers)
    universe = prepare_uploaded_universe(df)

    logger.info(f"開始長期價值清算：驗證後 {len(universe)} 檔 / 上傳 {len(raw_tickers)} 檔")
    max_workers = int(os.environ.get("MODE_C_WORKERS", "3"))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(ex.map(lambda kv: run_mode_c_pipeline(kv[0], kv[1], user_email), universe.items()))

    shortlist = select_diversified_shortlist(results)
    eligible_count = sum(1 for r in results if r.Long_Term_Eligible)
    logger.info(
        f"長期價值篩選完成：合格 {eligible_count} 檔，產業分散後研究名單 {len(shortlist)} 檔；"
        f"每產業最多 {MAX_PER_SECTOR} 檔。"
    )

    # 全量結果保留，方便檢查落選原因。
    rows = [asdict(r) for r in results]
    for row in rows:
        row["Agent_Tasks"] = " | ".join(row.get("Agent_Tasks", []))
    out_df = pd.DataFrame(rows)
    out_df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    # 另存真正需要深入研究的 8–12 檔候選；不足時不拿低品質公司硬湊數。
    shortlist_rows = [asdict(r) for r in shortlist]
    for row in shortlist_rows:
        row["Agent_Tasks"] = " | ".join(row.get("Agent_Tasks", []))
    shortlist_df = pd.DataFrame(shortlist_rows) if shortlist_rows else out_df.iloc[0:0].copy()
    shortlist_df.to_csv(OUTPUT_SHORTLIST_CSV, index=False, encoding="utf-8-sig")

    report = "# Mode C 長期價值研究名單（70% ETF 核心 + 最多 30% 主動選股）\n\n"
    report += f"清算時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    report += (
        f"**紀律：只做多、不使用槓桿/期權/放空；主動部位上限 {ACTIVE_SLEEVE_LIMIT_PCT:.0f}%；"
        f"單一公司上限 {MAX_POSITION_WEIGHT_PCT_TOTAL:.1f}%；單一產業上限 {MAX_SECTOR_WEIGHT_PCT_TOTAL:.1f}%；"
        "模型是研究漏斗，不是自動買入訊號。**\n\n"
    )
    if not shortlist:
        report += "本次沒有公司同時通過品質、估值、預期與下檔風險門檻；保留現金或 ETF，不硬湊個股。\n"
    for r in shortlist:
        report += render_stock_report(r) + "\n---\n\n"
    Path(OUTPUT_MD).write_text(report, encoding="utf-8")

    payload = build_agent_payload(shortlist)
    Path(OUTPUT_JSON).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    save_json_cache(CACHE_FILE_SHARES, _VECTOR_CACHE)

    logger.info(f"完成。Eligible={eligible_count} / Shortlist={len(shortlist)} / Total={len(out_df)}")
    logger.info(f"已輸出 {OUTPUT_SHORTLIST_CSV}、{OUTPUT_MD} 與 {OUTPUT_JSON}。")

    if os.environ.get("SEND_EMAIL", "0") == "1":
        send_email_report(report, OUTPUT_SHORTLIST_CSV, user_email)



if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logger.critical(f"系統崩潰：{exc}")
        traceback.print_exc()
        raise
