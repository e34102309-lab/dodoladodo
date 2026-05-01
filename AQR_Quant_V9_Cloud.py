"""
=============================================================================
V11.2.1 SHARES MULTI-TAG PATCH (核心持股管線 - 數據管路強化版)
=============================================================================
v11.2.1 改動摘要 (vs v11.2)：
1. ★[Bug] fetch_shares_outstanding 從單 tag → 多 tag chain：
   - 之前只試 dei.EntityCommonStockSharesOutstanding，不少 us-gaap-only 申報
     的公司會抓不到，連帶 yfinance fallback 不穩，導致「市值無法獲取」灌水。
   - 改試三組 tag：dei.Entity..、us-gaap.CommonStockShares..、us-gaap.WeightedAverage..
2. [診斷] 拆分 Fail 訊息：「市值無法獲取」(SEC+YF 都失敗) vs「市值過低」(<$1B)，
   下次能直接從 fail_stats 看出真正瓶頸。
3. [防禦] pre_fetch_all_info 加入：失敗重試 1 次、每筆小睡 0.05s、印成功率，
   降低 yfinance 受速率限制干擾。

v11.2 改動摘要 (vs v11.1)：
1. ★[嚴重] EBITDA 壓力測試剔除 SBC 加回 (原本內部矛盾)。
2. ★[嚴重] worker 線程零 yf.Ticker.info 呼叫 (避免併發限流)。
=============================================================================
"""
import random
import requests
import time
import os
import smtplib
from email.message import EmailMessage
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import logging
import concurrent.futures
import threading
import traceback
import yfinance as yf
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==============================================================================
# 設定日誌
# ==============================================================================
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(message)s',
                    datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)

# ==============================================================================
# 強力突破 Yahoo 401 封鎖 (Session 偽裝機制)
# ==============================================================================
def create_stealth_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1.0, status_forcelist=[401, 403, 429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive"
    })
    return session

def force_refresh_yf_crumb():
    try:
        if hasattr(yf.utils, 'empty_cache'):
            yf.utils.empty_cache()
        if hasattr(yf, 'base') and hasattr(yf.base, '_requests'):
            yf.base._requests = create_stealth_session()
        from yfinance.utils import crumb_manager
        crumb_manager._crumb = None
        crumb_manager._cookie = None
        crumb_manager.get_crumb() 
        return True
    except Exception as e:
        logger.debug(f"Crumb refresh failed: {e}")
        return False

force_refresh_yf_crumb()

# ==============================================================================
# 全域快取與巨集參數
# ==============================================================================
_BULK_MARKET_DATA: Optional[pd.DataFrame] = None
_RF_CACHE: Optional[float] = None
MARKET_RISK_PREMIUM = 0.046

# === 核心門檻 ===
ROIC_THRESHOLD          = 12.0
ICR_THRESHOLD           = 5.0
EBIT_MARGIN_THRESHOLD   = 0.05
MIN_LIQUIDITY_USD       = 5_000_000
WINSORIZE_PCT           = 0.025

# === V10.3 沿用門檻 ===
ROIC_3Y_AVG_MIN         = 12.0
ROIC_3Y_MIN_FLOOR       = 8.0
FCF_YIELD_PREMIUM_BP    = 200
GROSS_MARGIN_VOL_MAX    = 0.10

# === V11.0 新增門檻 ===
EV_SALES_MAX            = 30.0
PCT_FROM_52W_HIGH_MIN   = -0.45

# ==============================================================================
# 批量快取系統 (取代單檔抓取)
# ==============================================================================
def pre_fetch_all_market_data(tickers: List[str]):
    """一次性批量下載所有候選股的 K 線資料，完美避開 Yahoo 封鎖"""
    global _BULK_MARKET_DATA
    logger.info(f"開始一次性批量下載 {len(tickers)} 檔報價資料...對 Yahoo 只算 1 次請求！")
    # 直接讓 yfinance 底層自己的 curl_cffi 去接管
    _BULK_MARKET_DATA = yf.download(tickers, period='3y', progress=False, auto_adjust=True)
    logger.info("批量下載完成！")

def get_cached_series(ticker: str, col: str) -> Optional[pd.Series]:
    """從全域快取中切片提取特定股票的特定欄位 (Close, Volume)"""
    global _BULK_MARKET_DATA
    if _BULK_MARKET_DATA is None or _BULK_MARKET_DATA.empty:
        return None
    try:
        if isinstance(_BULK_MARKET_DATA.columns, pd.MultiIndex):
            if (col, ticker) in _BULK_MARKET_DATA.columns:
                s = _BULK_MARKET_DATA[(col, ticker)].dropna()
                if not s.empty: return s
    except Exception:
        pass
    return None

# ==============================================================================
# 工具函式
# ==============================================================================
def get_risk_free_rate() -> float:
    global _RF_CACHE
    if _RF_CACHE is not None:
        return _RF_CACHE
    try:
        close = get_cached_series('^TNX', 'Close')
        if close is not None and not close.empty:
            _RF_CACHE = float(close.iloc[-1]) / 100
            return _RF_CACHE
    except Exception as e:
        logger.warning(f"無風險利率獲取失敗，使用預設值: {e}")
    _RF_CACHE = 0.0435
    return _RF_CACHE

def robust_zscore(series: pd.Series) -> pd.Series:
    s = pd.to_numeric(series, errors='coerce').fillna(0.0)
    if len(s) < 2:
        return pd.Series(np.zeros(len(s)), index=s.index)
    med = s.median()
    mad = (s - med).abs().median()
    if mad < 1e-9:
        s_w = s.clip(lower=s.quantile(WINSORIZE_PCT), upper=s.quantile(1 - WINSORIZE_PCT))
        std = s_w.std(ddof=1)
        if std < 1e-9:
            return pd.Series(np.zeros(len(s)), index=s.index)
        return pd.Series((s_w - s_w.mean()) / std, index=s.index)
    return pd.Series((s - med) / (1.4826 * mad), index=s.index).clip(-3.5, 3.5)

def check_global_trend() -> str:
    spy_close = get_cached_series('SPY', 'Close')
    qqq_close = get_cached_series('QQQ', 'Close')
    
    trend_msg = ""
    for name, close in [('SPY', spy_close), ('QQQ', qqq_close)]:
        if close is None or len(close) < 200:
            trend_msg += f"[{name}] 資料不足。\n"
            continue
        try:
            sma_200 = close.rolling(window=200).mean()
            last_close = float(close.iloc[-1])
            last_sma = float(sma_200.iloc[-1])
            recent_closes = close.iloc[-5:]
            recent_smas = sma_200.iloc[-5:]
            below_sma_week = all(float(c) < float(s) for c, s in zip(recent_closes, recent_smas))
            diff_pct = (last_close / last_sma - 1) * 100
            status = ("🔴 跌破 200SMA (進入冰河保護期)" if below_sma_week else "🟢 穩態多頭")
            trend_msg += (f"[{name}] 收盤: {last_close:.2f} | 200SMA: {last_sma:.2f} "
                          f"({diff_pct:+.2f}%) -> {status}\n")
        except Exception as e:
            trend_msg += f"[{name}] 趨勢計算異常: {e}\n"
    return trend_msg if trend_msg else "大氣壓力感測器離線。\n"

# ==============================================================================
# 價格與備援機制 (純快取驅動)
# ==============================================================================
def get_fallback_price(ticker: str) -> float:
    close = get_cached_series(ticker, 'Close')
    if close is not None and not close.empty:
        return float(close.iloc[-1])
    return 0.0

# v11.2: 啟動時填充的全域 info 快取，worker 線程「只讀」此 dict，不再呼叫 yf.Ticker
_INFO_CACHE: Dict[str, dict] = {}

def pre_fetch_all_info(tickers: List[str]):
    """主線程批量抓取所有 yf.info，避免 worker 線程觸發 Yahoo 限流。
    
    v11.2.1 強化：失敗重試 1 次 + 每筆 50ms 節流 + 印成功率，
    yfinance info 端點本就不穩，加重試把成功率拉到可接受範圍。
    """
    global _INFO_CACHE
    logger.info(f"[v11.2.1] 主線程批量抓取 {len(tickers)} 檔 yf.info（含重試）...")
    success = 0
    for i, t in enumerate(tickers):
        info_data = {}
        for attempt in range(2):
            try:
                info = yf.Ticker(t).info
                if info and isinstance(info, dict) and len(info) > 5:
                    info_data = dict(info)
                    success += 1
                    break
            except Exception:
                pass
            if attempt == 0:
                time.sleep(0.4)  # 第一次失敗時退避，避免連續 429
        _INFO_CACHE[t] = info_data
        time.sleep(0.05)  # 每筆間距 50ms，降低速率限制機率
        if (i + 1) % 100 == 0:
            rate = success / (i + 1) * 100
            logger.info(f"  [info] {i+1}/{len(tickers)} (成功率 {rate:.0f}%)")
    final_rate = success / len(tickers) * 100 if tickers else 0
    logger.info(f"[v11.2.1] info 批量完成：成功 {success}/{len(tickers)} ({final_rate:.0f}%)")

def safe_yf_info(ticker: str) -> dict:
    """v11.2 版：純讀 _INFO_CACHE，worker 線程零網路呼叫。"""
    info = _INFO_CACHE.get(ticker, {})
    if info and (info.get('currentPrice') or info.get('regularMarketPrice')):
        return info
    
    fallback_price = get_fallback_price(ticker)
    if fallback_price > 0:
        merged = dict(info) if info else {}
        merged.setdefault('currentPrice', fallback_price)
        merged.setdefault('regularMarketPrice', fallback_price)
        return merged
    return info if info else {}

def calculate_dynamic_beta(ticker: str, spy_returns: Optional[pd.Series] = None) -> float:
    try:
        tk_close = get_cached_series(ticker, 'Close')
        if tk_close is None or len(tk_close) < 50:
            return 1.0
        
        # 將日線轉換為週線計算 Beta
        tk_weekly = tk_close.resample('W').last().dropna()
        tk_ret = tk_weekly.pct_change().dropna()
        
        if spy_returns is None:
            spy_close = get_cached_series('SPY', 'Close')
            if spy_close is not None:
                spy_weekly = spy_close.resample('W').last().dropna()
                spy_returns = spy_weekly.pct_change().dropna()
                
        if spy_returns is not None:
            joined = pd.concat([tk_ret, spy_returns], axis=1, join='inner').dropna()
            joined.columns = ['tk', 'spy']
            if len(joined) < 50:
                return 1.0
            var_market = joined['spy'].var()
            if var_market == 0:
                return 1.0
            cov = joined.cov().loc['tk', 'spy']
            return float(np.clip(cov / var_market, 0.5, 2.5))
    except Exception:
        pass
    return 1.0

def calculate_dynamic_wacc(ticker: str, debt: float, cash: float,
                            market_cap: float, book_equity: float, tax_rate: float,
                            spy_returns: Optional[pd.Series] = None) -> float:
    rf = get_risk_free_rate()
    beta_u = calculate_dynamic_beta(ticker, spy_returns)
    anchor_equity = max(market_cap, book_equity * 1.5) if book_equity > 0 else market_cap
    net_debt = max(debt - cash, 0.0)
    de_ratio = net_debt / max(anchor_equity, 1.0)
    beta_l = float(np.clip(beta_u * (1.0 + (1.0 - tax_rate) * de_ratio), 0.5, 3.0))
    ke = rf + beta_l * MARKET_RISK_PREMIUM
    total = market_cap + net_debt
    w_e = market_cap / total if total > 0 else 1.0
    w_d = net_debt / total if total > 0 else 0.0
    kd = rf + 0.015
    wacc = w_e * ke + w_d * kd * (1.0 - tax_rate)
    return float(np.clip(wacc, 0.06, 0.20))

def fetch_price_metrics(ticker: str) -> Optional[Dict]:
    try:
        close = get_cached_series(ticker, 'Close')
        volume = get_cached_series(ticker, 'Volume')
        
        if close is None or volume is None or len(close) < 200:
            return None

        dollar_volume = float((close * volume).tail(30).mean())

        m = close.resample('ME').last().dropna()
        mom_12m = None
        if len(m) >= 13:
            mom_12m = (float(m.iloc[-2]) / float(m.iloc[-13]) - 1) * 100

        last_252 = close.tail(252)
        high_52w = float(last_252.max())
        last_close = float(close.iloc[-1])
        pct_from_high = (last_close / high_52w - 1) if high_52w > 0 else -1.0

        return {
            'dollar_volume': dollar_volume,
            'momentum': mom_12m,
            'pct_from_52w_high': pct_from_high,
            'last_close': last_close,
        }
    except Exception:
        return None

# ==============================================================================
# SEC 原生爬蟲
# ==============================================================================
class RateLimitedSession:
    def __init__(self, calls=8, period=1.0): 
        self.session = requests.Session()
        self.calls = calls
        self.period = period
        self.lock = threading.Lock()
        self.timestamps = []

    def _wait_for_capacity(self):
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
                resp = self.session.get(url, headers=headers, timeout=10)
                if resp.status_code == 200:
                    return resp
                elif resp.status_code in (429, 503):
                    time.sleep((2 ** attempt) * 1.5)
                elif resp.status_code == 404:
                    return None
            except requests.RequestException:
                time.sleep(2)
        return None

_GLOBAL_SEC_SESSION = RateLimitedSession()

class SECDataDistiller:
    def __init__(self, email: str):
        self.headers = {'User-Agent': f'QuantResearchProject {email}'}
        self.session = _GLOBAL_SEC_SESSION
        self.shares_tag = 'EntityCommonStockSharesOutstanding'
        self.config = {
            'OCF':         ['NetCashProvidedByUsedInOperatingActivities'],
            'CapEx':       ['PaymentsToAcquirePropertyPlantAndEquipment',
                            'PropertyPlantAndEquipmentAdditions'],
            'SBC':         ['ShareBasedCompensation', 'StockBasedCompensation',
                            'AllocatedShareBasedCompensationExpense',
                            'ShareBasedCompensationExpense'],
            'EBIT':        ['OperatingIncomeLoss',
                            'IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest'],
            'Interest':    ['InterestExpense', 'InterestExpenseDebt',
                            'InterestExpenseNet', 'InterestAndDebtExpense'],
            'DnA':         ['DepreciationDepletionAndAmortization',
                            'DepreciationAndAmortization'],
            'Debt':        ['LongTermDebt', 'LongTermDebtAndCapitalLeaseObligations',
                            'DebtCurrent'],
            'Cash':        ['CashAndCashEquivalentsAtCarryingValue'],
            'Equity':      ['StockholdersEquity'],
            'RND':         ['ResearchAndDevelopmentExpense'],
            'DefRev':      ['DeferredRevenue', 'ContractWithCustomerLiability'],
            'FinRec':      ['FinancingReceivableNet', 'NotesAndLoansReceivableNet',
                            'LoansAndLeasesReceivableNet'],
            'Revenue':     ['Revenues',
                            'RevenueFromContractWithCustomerExcludingAssessedTax',
                            'SalesRevenueNet'],
            'Buyback':     ['PaymentsForRepurchaseOfCommonStock',
                            'PaymentsForRepurchaseOfEquity'],
            'Dividend':    ['PaymentsOfDividendsCommonStock', 'PaymentsOfDividends'],
            'StockIssuance': ['ProceedsFromIssuanceOfCommonStock',
                              'StockIssuedDuringPeriodValueNewIssues'],
            'GrossProfit': ['GrossProfit'],
        }

    def fetch_concept(self, cik: str, concept: str) -> pd.DataFrame:
        for tag in self.config.get(concept, [concept]):
            url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{str(cik).zfill(10)}/us-gaap/{tag}.json"
            resp = self.session.get(url, headers=self.headers)
            if resp and resp.status_code == 200:
                try:
                    data = resp.json().get('units', {}).get('USD', [])
                    if data:
                        df = pd.DataFrame(data)
                        df['end'] = pd.to_datetime(df['end'])
                        if 'filed' in df.columns:
                            df['filed'] = pd.to_datetime(df['filed'])
                            df = df.sort_values('filed')
                        else:
                            df = df.sort_values('end')
                        dedup_keys = [k for k in ['fy', 'fp', 'form'] if k in df.columns]
                        if dedup_keys:
                            df = df.drop_duplicates(subset=dedup_keys, keep='last')
                        else:
                            df = df.drop_duplicates(subset=['end', 'form'], keep='last')
                        return df.sort_values('end').reset_index(drop=True)
                except Exception:
                    pass
        return pd.DataFrame()

    def fetch_shares_outstanding(self, cik: str) -> pd.DataFrame:
        # v11.2.1: 多 tag chain，覆蓋不同申報方式
        # 不少公司不在 dei taxonomy 下申報，只有 us-gaap 版本
        tags_to_try = [
            ('dei',     'EntityCommonStockSharesOutstanding'),
            ('us-gaap', 'CommonStockSharesOutstanding'),
            ('us-gaap', 'WeightedAverageNumberOfSharesOutstandingBasic'),
        ]
        for taxonomy, tag in tags_to_try:
            url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{str(cik).zfill(10)}/{taxonomy}/{tag}.json"
            resp = self.session.get(url, headers=self.headers)
            if resp and resp.status_code == 200:
                try:
                    data = resp.json().get('units', {}).get('shares', [])
                    if data:
                        df = pd.DataFrame(data)
                        df['end'] = pd.to_datetime(df['end'])
                        if 'filed' in df.columns:
                            df['filed'] = pd.to_datetime(df['filed'])
                            df = df.sort_values('filed')
                        return df.drop_duplicates(subset=['end'], keep='last').sort_values('end').reset_index(drop=True)
                except Exception:
                    continue
        return pd.DataFrame()

    def get_latest_annual(self, df: pd.DataFrame) -> float:
        if df.empty:
            return 0.0
        annual = df[df['form'] == '10-K']
        return float(annual['val'].iloc[-1]) / 1e9 if not annual.empty else 0.0

    def get_latest_shares(self, df: pd.DataFrame) -> Tuple[float, float]:
        if df.empty or len(df) < 2:
            return 0.0, 0.0
        latest_date = df['end'].iloc[-1]
        one_year_ago = latest_date - pd.Timedelta(days=365)
        df_old = df[df['end'] <= one_year_ago]
        if df_old.empty:
            return float(df['val'].iloc[-1]) / 1e9, 0.0
        return float(df['val'].iloc[-1]) / 1e9, float(df_old['val'].iloc[-1]) / 1e9

    def get_yoy_change(self, df: pd.DataFrame) -> float:
        if df.empty:
            return 0.0
        annual = df[df['form'] == '10-K']
        if len(annual) < 2:
            return 0.0
        return (float(annual['val'].iloc[-1]) - float(annual['val'].iloc[-2])) / 1e9

    def get_revenue_3yr(self, df: pd.DataFrame) -> Tuple[float, float]:
        if df.empty:
            return 0.0, 0.0
        annual = df[df['form'] == '10-K']
        if len(annual) < 4:
            return 0.0, 0.0
        return float(annual['val'].iloc[-4]) / 1e9, float(annual['val'].iloc[-1]) / 1e9

    def get_annual_history(self, df: pd.DataFrame, n_years: int = 3) -> List[float]:
        if df.empty:
            return []
        annual = df[df['form'] == '10-K']
        if len(annual) < n_years:
            return []
        return [float(v) / 1e9 for v in annual['val'].iloc[-n_years:].tolist()]

    def get_n_year_sum(self, df: pd.DataFrame, n: int = 3) -> float:
        hist = self.get_annual_history(df, n)
        return sum(hist) if hist else 0.0

    def check_q_yoy_decline(self, df: pd.DataFrame) -> bool:
        if df.empty:
            return False
        q_df = df[df['form'] == '10-Q'].copy()
        if len(q_df) < 4:
            return False
        latest = q_df.iloc[-1]
        if 'fp' in q_df.columns and 'fy' in q_df.columns:
            target = q_df[(q_df['fy'] == latest.get('fy', 0) - 1) &
                          (q_df['fp'] == latest.get('fp'))]
            if target.empty:
                return False
            target_val = float(target['val'].iloc[-1])
        else:
            target_date = latest['end'] - pd.Timedelta(days=365)
            q_df_excl = q_df.iloc[:-1].copy()
            q_df_excl['diff'] = (q_df_excl['end'] - target_date).abs().dt.days
            match = q_df_excl[q_df_excl['diff'] < 35]
            if match.empty:
                return False
            target_val = float(match['val'].iloc[-1])
        return float(latest['val']) < target_val * 0.85

# ==============================================================================
# 統一的 ROIC 計算函式
# ==============================================================================
def calc_roic_unified(ebit: float, equity: float, debt: float,
                       cash: float, revenue: float) -> float:
    excess_cash = max(0.0, cash - revenue * 0.02)
    ic_floor = max(revenue * 0.15, 0.5)
    ic = max(debt + max(equity, 0.0) - excess_cash, ic_floor)
    return (ebit / max(ic, 0.1)) * 100

# ==============================================================================
# 核心管線 V11.1
# ==============================================================================
def run_v11_pipeline(ticker: str, cik: str, email: str,
                      spy_returns: Optional[pd.Series] = None) -> dict:
    try:
        if '-' in ticker or '.' in ticker:
            return {"Ticker": ticker, "Status": "Fail: 排除特別股/多重股權"}

        pm = fetch_price_metrics(ticker)
        if pm is None:
            return {"Ticker": ticker, "Status": "Fail: 無價格資料"}
        if pm['dollar_volume'] < MIN_LIQUIDITY_USD:
            return {"Ticker": ticker,
                    "Status": f"Fail: 流動性不足 (${pm['dollar_volume']/1e6:.1f}M)"}

        mom_12m = pm['momentum']
        pct_from_high = pm['pct_from_52w_high']
        last_close = pm['last_close']

        if mom_12m is None:
            return {"Ticker": ticker, "Status": "Fail: 動能資料不足"}

        if pct_from_high < PCT_FROM_52W_HIGH_MIN:
            return {"Ticker": ticker,
                    "Status": f"Fail: 距高點過遠 ({pct_from_high*100:+.1f}%)"}

        info = safe_yf_info(ticker)
        price = float(info.get('currentPrice') or info.get('regularMarketPrice') or last_close)

        if price == 0.0:
            return {"Ticker": ticker, "Status": "Fail: 價格獲取失敗"}

        sec = SECDataDistiller(email)
        df_ocf       = sec.fetch_concept(cik, 'OCF')
        df_capex     = sec.fetch_concept(cik, 'CapEx')
        df_sbc       = sec.fetch_concept(cik, 'SBC')
        df_ebit      = sec.fetch_concept(cik, 'EBIT')
        df_int       = sec.fetch_concept(cik, 'Interest')
        df_dna       = sec.fetch_concept(cik, 'DnA')
        df_debt      = sec.fetch_concept(cik, 'Debt')
        df_cash      = sec.fetch_concept(cik, 'Cash')
        df_eq        = sec.fetch_concept(cik, 'Equity')
        df_rnd       = sec.fetch_concept(cik, 'RND')
        df_defrev    = sec.fetch_concept(cik, 'DefRev')
        df_fin_rec   = sec.fetch_concept(cik, 'FinRec')
        df_rev       = sec.fetch_concept(cik, 'Revenue')
        df_buyback   = sec.fetch_concept(cik, 'Buyback')
        df_div       = sec.fetch_concept(cik, 'Dividend')
        df_issuance  = sec.fetch_concept(cik, 'StockIssuance')
        df_gross     = sec.fetch_concept(cik, 'GrossProfit')
        df_shares    = sec.fetch_shares_outstanding(cik)

        if df_ocf.empty or df_ebit.empty or df_rev.empty:
            return {"Ticker": ticker, "Status": "Fail: SEC 核心資料缺失 (OCF/EBIT/Rev)"}

        ocf            = sec.get_latest_annual(df_ocf)
        capex          = abs(sec.get_latest_annual(df_capex))
        sbc            = abs(sec.get_latest_annual(df_sbc))
        ebit           = sec.get_latest_annual(df_ebit)
        dna            = abs(sec.get_latest_annual(df_dna))
        debt           = sec.get_latest_annual(df_debt)
        cash           = sec.get_latest_annual(df_cash)
        equity         = sec.get_latest_annual(df_eq)
        defrev_change  = sec.get_yoy_change(df_defrev)
        fin_rec        = sec.get_latest_annual(df_fin_rec)
        rev_3yr_ago, rev_latest = sec.get_revenue_3yr(df_rev)

        ebit_history   = sec.get_annual_history(df_ebit, 5)
        equity_history = sec.get_annual_history(df_eq,   3)
        debt_history   = sec.get_annual_history(df_debt, 3)
        cash_history   = sec.get_annual_history(df_cash, 3)
        rev_history    = sec.get_annual_history(df_rev,  5)
        gross_history  = sec.get_annual_history(df_gross, 3)
        ocf_history    = sec.get_annual_history(df_ocf,  3)

        buyback_3y     = sec.get_n_year_sum(df_buyback, 3)
        dividend_3y    = sec.get_n_year_sum(df_div, 3)
        issuance_3y    = sec.get_n_year_sum(df_issuance, 3)

        rev_cagr_3y = 0.0
        if rev_3yr_ago > 0 and rev_latest > 0:
            rev_cagr_3y = ((rev_latest / rev_3yr_ago) ** (1/3) - 1) * 100

        shares_now, _ = sec.get_latest_shares(df_shares)
        if shares_now == 0:
            shares_now = info.get('sharesOutstanding') or info.get('impliedSharesOutstanding', 0) if info else 0.0

        # v11.2.1: 區分「無法獲取」vs「過低」，方便下次 fail_stats 診斷
        if shares_now > 0:
            mcap = (price * shares_now) / 1e9
        elif info.get('marketCap'):
            mcap = float(info.get('marketCap')) / 1e9
        else:
            mcap = 0.0

        if mcap == 0.0:
            return {"Ticker": ticker, "Status": "Fail: 市值無法獲取 (SEC+YF 雙失敗)"}
        if mcap < 1.0:
            return {"Ticker": ticker, "Status": f"Fail: 市值過低 ({mcap:.2f}B)"}

        total_revenue = rev_history[-1] if len(rev_history) > 0 else float(info.get('totalRevenue') or 0.0) / 1e9
        
        gross_margin = 0.0
        if len(gross_history) > 0 and len(rev_history) > 0 and rev_history[-1] > 0:
            gross_margin = gross_history[-1] / rev_history[-1]
        else:
            gross_margin = float(info.get('grossMargins') or 0.0)
            
        rev_growth = 0.0
        if len(rev_history) >= 2 and rev_history[-2] > 0:
            rev_growth = (rev_history[-1] / rev_history[-2]) - 1
        else:
            rev_growth = float(info.get('revenueGrowth') or 0.0)

        ev_sales = 0.0
        if mcap > 0 and total_revenue > 0:
            ev_sales = (mcap + debt - cash) / total_revenue
        else:
            ev_sales = float(info.get('enterpriseToRevenue') or 0.0)
            
        if ev_sales > EV_SALES_MAX:
            return {"Ticker": ticker, "Status": f"Fail: EV/Sales 過熱 ({ev_sales:.1f}x)"}

        if capex == 0:
            capex = abs(float(info.get('capitalExpenditures') or 0)) / 1e9
        if sbc == 0:
            sbc = abs(float(info.get('shareBasedCompensation') or 0)) / 1e9

        minority_interest = float(info.get('minorityInterest') or 0.0) / 1e9

        raw_int = sec.get_latest_annual(df_int)
        interest = max(abs(raw_int), 0.05) if raw_int != 0 else 0.05

        adjusted_debt = max(0.0, debt - fin_rec)
        excess_cash = max(0.0, cash - (total_revenue * 0.02))
        net_debt = max(adjusted_debt - excess_cash, 0.0)

        true_ev = max(mcap + net_debt + minority_interest, mcap * 0.10)

        if dna > 0 and capex > 0:
            maint_capex = min(dna, capex)
        elif dna > 0:
            maint_capex = dna
        else:
            maint_capex = capex

        smoothed_ocf = float(np.mean(ocf_history)) if len(ocf_history) >= 2 else ocf
        real_fcf = smoothed_ocf - maint_capex - sbc

        if real_fcf <= 0:
            reason = "SBC吞噬" if sbc > smoothed_ocf * 0.4 else "重資本耗損"
            return {"Ticker": ticker, "Status": f"Fail: 實質FCF為負 ({reason})"}

        fcf_yield = (real_fcf / true_ev) * 100 if true_ev > 0 else 0.0

        tax_rate = float(np.clip(info.get('effectiveTaxRate') or 0.21, 0.1, 0.35))
        wacc = calculate_dynamic_wacc(ticker, adjusted_debt, cash, mcap, equity,
                                       tax_rate, spy_returns)

        roic = calc_roic_unified(ebit, equity, adjusted_debt, cash, total_revenue)
        icr = ebit / interest
        ebit_margin = (ebit / total_revenue) if total_revenue > 0 else 0.0

        if roic < ROIC_THRESHOLD:
            return {"Ticker": ticker, "Status": f"Fail: ROIC<{ROIC_THRESHOLD} ({roic:.1f})"}
        if icr < ICR_THRESHOLD:
            return {"Ticker": ticker, "Status": f"Fail: ICR<{ICR_THRESHOLD} ({icr:.1f})"}
        if ebit_margin < EBIT_MARGIN_THRESHOLD:
            return {"Ticker": ticker,
                    "Status": f"Fail: EBIT margin<{EBIT_MARGIN_THRESHOLD*100:.0f}% ({ebit_margin*100:.1f}%)"}

        roic_history = []
        for i in range(min(3, len(ebit_history), len(equity_history),
                           len(debt_history), len(rev_history))):
            cash_i = cash_history[i] if i < len(cash_history) else 0.0
            roic_history.append(calc_roic_unified(
                ebit_history[i], equity_history[i], debt_history[i],
                cash_i, rev_history[i]
            ))

        roic_3y_avg = float(np.mean(roic_history)) if roic_history else roic
        roic_3y_min = float(min(roic_history)) if roic_history else roic

        if len(roic_history) >= 3:
            if roic_3y_avg < ROIC_3Y_AVG_MIN:
                return {"Ticker": ticker,
                        "Status": f"Fail: 3年平均ROIC<{ROIC_3Y_AVG_MIN} ({roic_3y_avg:.1f})"}
            if roic_3y_min < ROIC_3Y_MIN_FLOOR:
                return {"Ticker": ticker,
                        "Status": f"Fail: 3年最低ROIC<{ROIC_3Y_MIN_FLOOR} ({roic_3y_min:.1f})"}

        gross_margin_vol = 0.0
        if len(gross_history) >= 3 and len(rev_history) >= 3:
            gm_series = [g/r for g, r in zip(gross_history[-3:], rev_history[-3:]) if r > 0]
            if len(gm_series) >= 3:
                gross_margin_vol = float(np.std(gm_series, ddof=1))
                if gross_margin_vol > GROSS_MARGIN_VOL_MAX:
                    return {"Ticker": ticker,
                            "Status": f"Fail: 毛利波動>{GROSS_MARGIN_VOL_MAX*100:.0f}pp ({gross_margin_vol*100:.1f}pp)"}

        rf_pct = get_risk_free_rate() * 100
        min_fcf_yield = rf_pct + (FCF_YIELD_PREMIUM_BP / 100)
        valuation_warning = fcf_yield < min_fcf_yield

        net_buyback_3y = max(0.0, buyback_3y - issuance_3y)
        buyback_yield = (net_buyback_3y / 3 / mcap) * 100 if mcap > 0 else 0.0
        dividend_yield_calc = (dividend_3y / 3 / mcap) * 100 if mcap > 0 else 0.0

        if buyback_yield > 0:
            if valuation_warning or roic < (wacc * 100):
                adjusted_buyback_yield = -buyback_yield  
            else:
                adjusted_buyback_yield = buyback_yield
        else:
            adjusted_buyback_yield = 0.0

        total_shareholder_yield = adjusted_buyback_yield + dividend_yield_calc

        cycle_top_warning = False
        cond_a = (fcf_yield > 12.0 and (rev_growth < 0 or rev_cagr_3y < 2.0))
        cond_b = False
        if len(ebit_history) >= 5 and len(rev_history) >= 5:
            margin_history = [e/r for e, r in zip(ebit_history[-5:], rev_history[-5:]) if r > 0]
            if len(margin_history) >= 5:
                current_margin = margin_history[-1]
                if current_margin == max(margin_history) and rev_cagr_3y < 5.0:
                    cond_b = True
        cycle_top_warning = cond_a or cond_b

        is_growth_monster = False
        if total_revenue > 0:
            billings_growth = rev_growth + (defrev_change / total_revenue)
            real_r40 = ((real_fcf / total_revenue) + billings_growth) * 100
            if gross_margin >= 0.70 and (roic - wacc * 100) > 5.0 and real_r40 >= 40.0:
                is_growth_monster = True

        # v11.2 修正：FCF 已扣 SBC，這裡不再加回 (避免內部會計幻覺矛盾)
        ebitda = ebit + dna
        if ebitda > 0:
            current_mult = true_ev / ebitda
            ebitda_stress = ebitda * 0.50
            floor_mult = (min(12.0, 5.0 + ((gross_margin - 0.30) / 0.10) * 1.5)
                          if gross_margin > 0.3 else 5.0)
            stress_mult = min(current_mult, max(floor_mult, current_mult * 0.50))
            stress_ev = ebitda_stress * stress_mult
            stress_mcap = max(0.0, stress_ev - net_debt)
            drawdown = ((stress_mcap - mcap) / mcap) * 100
            drawdown_risk = min(0.0, drawdown)
        else:
            drawdown_risk = -100.0

        if drawdown_risk < -70:
            return {"Ticker": ticker, "Status": f"Drop: 極限回撤({drawdown_risk:.1f}%)"}

        exit_signal = "Hold ✅"
        if is_growth_monster:
            exit_signal = "🚀 Rule of 40 通行證 ✅"
            if sec.check_q_yoy_decline(df_ocf):
                exit_signal += " | 🟡 預警: 成長股OCF衰退"
        else:
            if fcf_yield < (get_risk_free_rate() * 100) and mom_12m < 0:
                exit_signal = "🔴 停損: 溢酬消失"
            elif roic < wacc * 100:
                exit_signal = "🔴 停損: 價值摧毀"

            if sec.check_q_yoy_decline(df_ocf):
                if exit_signal == "Hold ✅":
                    exit_signal = "🟡 預警: OCF YoY衰退"
                else:
                    exit_signal += " | 🟡 OCF YoY衰退"

            if adjusted_buyback_yield < 0:
                exit_signal = ("⚠️ 高位接盤警告" if exit_signal == "Hold ✅"
                                else exit_signal + " | ⚠️ 溢價庫藏股")

        if cycle_top_warning:
            exit_signal = ("🟠 週期頂警告" if exit_signal == "Hold ✅"
                            else exit_signal + " | 🟠 週期頂")

        if valuation_warning and not is_growth_monster:
            exit_signal = ("🟡 溢酬偏薄" if exit_signal == "Hold ✅"
                            else exit_signal + " | 🟡 溢酬薄")

        return {
            'Ticker': ticker, 'Status': 'Pass', 'Price': round(price, 2),
            'CIK': str(cik),
            'WACC(%)':              round(wacc * 100, 2),
            'ROIC(%)':              round(roic, 2),
            'ROIC_3Y_Avg(%)':       round(roic_3y_avg, 2),
            'ROIC_3Y_Min(%)':       round(roic_3y_min, 2),
            'EBIT_Margin(%)':       round(ebit_margin * 100, 2),
            'GM_Vol(pp)':           round(gross_margin_vol * 100, 2),
            'FCF_Yield(%)':         round(fcf_yield, 2),
            'Real_FCF(B)':          round(real_fcf, 3),
            'Buyback_Yield(%)':     round(buyback_yield, 2),
            'Dividend_Yield(%)':    round(dividend_yield_calc, 2),
            'Total_SH_Yield(%)':    round(total_shareholder_yield, 2),
            'Rev_CAGR_3Y(%)':       round(rev_cagr_3y, 2),
            'Momentum(%)':          round(mom_12m, 2),
            'Pct_From_52W_High(%)': round(pct_from_high * 100, 2),
            'EV_Sales(x)':          round(ev_sales, 2),
            'Max_Drawdown_Risk(%)': round(drawdown_risk, 1),
            'Liquidity($M)':        round(pm['dollar_volume'] / 1e6, 1),
            'Exit_Signal':          exit_signal,
        }
    except Exception as e:
        err_msg = str(e).split('\n')[0][:80]
        logger.error(f"[{ticker}] 管線崩潰: {err_msg}")
        return {"Ticker": ticker, "Status": "Error"}

# ==============================================================================
# Alpha 排序
# ==============================================================================
def winsorize_series(series: pd.Series, pct: float = WINSORIZE_PCT) -> pd.Series:
    s = pd.to_numeric(series, errors='coerce').fillna(0.0)
    lower, upper = s.quantile(pct), s.quantile(1 - pct)
    return s.clip(lower=lower, upper=upper)

def deduplicate_by_cik(df: pd.DataFrame) -> pd.DataFrame:
    if 'CIK' not in df.columns or 'Liquidity($M)' not in df.columns:
        return df
    df = df.sort_values('Liquidity($M)', ascending=False)
    return df.drop_duplicates(subset=['CIK'], keep='first').reset_index(drop=True)

def calculate_composite_alpha(results: List[dict]) -> pd.DataFrame:
    df = pd.DataFrame([r for r in results if r.get('Status') == 'Pass'])
    if len(df) < 2:
        return df

    df = deduplicate_by_cik(df)

    df['_ROIC_w']    = winsorize_series(df['ROIC(%)'])
    df['_FCF_w']     = winsorize_series(df['FCF_Yield(%)'])
    df['_MOM_w']     = winsorize_series(df['Momentum(%)'])
    df['_DD_w']      = winsorize_series(df['Max_Drawdown_Risk(%)'])
    df['_ROIC_3Y_w'] = winsorize_series(df['ROIC_3Y_Avg(%)']) if 'ROIC_3Y_Avg(%)' in df.columns else df['_ROIC_w']
    df['_TSY_w']     = winsorize_series(df['Total_SH_Yield(%)']) if 'Total_SH_Yield(%)' in df.columns else pd.Series(0, index=df.index)

    df['Z_Quality']     = robust_zscore(df['_ROIC_w'])
    df['Z_Value']       = robust_zscore(df['_FCF_w'])
    df['Z_Momentum']    = robust_zscore(df['_MOM_w'])
    df['Z_Safety']      = robust_zscore(df['_DD_w'])
    df['Z_Persistence'] = robust_zscore(df['_ROIC_3Y_w'])
    df['Z_Shareholder'] = robust_zscore(df['_TSY_w'])

    df['_cycle_penalty']        = df['Exit_Signal'].apply(lambda x: -0.5 if '週期頂' in str(x) else 0.0)
    df['_thin_premium_penalty'] = df['Exit_Signal'].apply(lambda x: -0.3 if ('溢酬偏薄' in str(x) or '溢酬薄' in str(x)) else 0.0)
    df['_value_destruction']    = df['Exit_Signal'].apply(lambda x: -0.7 if '價值摧毀' in str(x) else 0.0)

    df['Alpha_Score'] = (
        df['Z_Quality']      * 0.25 +
        df['Z_Persistence']  * 0.20 +
        df['Z_Value']        * 0.20 +
        df['Z_Momentum']     * 0.15 +
        df['Z_Shareholder']  * 0.10 +
        df['Z_Safety']       * 0.10 +
        df['_cycle_penalty'] +
        df['_thin_premium_penalty'] +
        df['_value_destruction']
    ).round(3)

    df = df.drop(columns=[c for c in df.columns if c.startswith('_')])
    return df.sort_values('Alpha_Score', ascending=False).reset_index(drop=True)

# ==============================================================================
# 報表發送
# ==============================================================================
def send_email_report(df: pd.DataFrame, receiver_email: str, trend_report: str):
    sender_email = os.environ.get('EMAIL_SENDER')
    sender_pwd = os.environ.get('EMAIL_PASSWORD')
    if not sender_email or not sender_pwd:
        logger.warning("未設定 EMAIL_SENDER 或 EMAIL_PASSWORD，略過發信。")
        return
    msg = EmailMessage()
    msg['Subject'] = f"[V11.2.1 核心持股] Alpha 報表 - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    msg['From'] = sender_email
    msg['To'] = receiver_email
    content = f"總工程師您好：\n\n【全域監測】\n{trend_report}\n" + "-" * 60
    content += "\n[v11.2.1 數據管路強化版] 多 tag shares chain + pre_fetch 重試 + fail 訊息拆分。\n\n"
    if df.empty:
        content += "今日無通關標的。"
    else:
        content += f"共計 {len(df)} 檔通關。\n\n【TOP 10】\n"
        cols = ['Ticker', 'Price', 'ROIC_3Y_Avg(%)', 'ROIC_3Y_Min(%)', 'FCF_Yield(%)',
                'Total_SH_Yield(%)', 'Momentum(%)', 'EV_Sales(x)', 'Alpha_Score', 'Exit_Signal']
        cols = [c for c in cols if c in df.columns]
        content += df.head(10)[cols].to_string(index=False)
    msg.set_content(content)
    if not df.empty:
        msg.add_attachment(
            df.to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig'),
            maintype='text', subtype='csv',
            filename=f'V11.2_1_Alpha_{datetime.now().strftime("%Y%m%d")}.csv'
        )
    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(sender_email, sender_pwd)
            server.send_message(msg)
            logger.info("信件發送成功！")
    except Exception as e:
        logger.error(f"郵件發送失敗: {e}")

# ==============================================================================
# 主程式
# ==============================================================================
if __name__ == "__main__":
    USER_EMAIL = os.environ.get('USER_EMAIL', 'a7924177@gmail.com')
    CACHE_FILE = "qualified_universe.csv"

    print("\n>>> 點火啟動：V11.2.1 核心持股管線 (數據管路強化版) <<<\n")

    try:
        if not os.path.exists(CACHE_FILE):
            logger.error(f"找不到 {CACHE_FILE} 檔案，程式終止。")
            exit(1)

        df_c = pd.read_csv(CACHE_FILE)
        df_c['CIK'] = df_c['CIK'].astype(str).str.zfill(10)
        universe = dict(zip(df_c['Ticker'], df_c['CIK']))
        logger.info(f"讀取 {len(universe)} 檔候選股")

        # ── 一次性批量下載所有資料 (取代原本在迴圈中的單筆請求) ──
        all_tickers = list(universe.keys()) + ['SPY', 'QQQ', '^TNX']
        pre_fetch_all_market_data(all_tickers)
        
        # v11.2 關鍵：主線程批量抓 info 一次，worker 線程不再碰 yf.Ticker
        pre_fetch_all_info(list(universe.keys()))

        # 預先計算 SPY 的週報酬率，供迴圈內的 Beta 函數取用
        spy_close = get_cached_series('SPY', 'Close')
        spy_returns = None
        if spy_close is not None and not spy_close.empty:
            spy_weekly = spy_close.resample('W').last().dropna()
            spy_returns = spy_weekly.pct_change().dropna()

        # 檢查大盤趨勢 (已自動從快取中抓取)
        trend = check_global_trend()
        print(trend)

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            res = list(executor.map(
                lambda p: run_v11_pipeline(p[0], str(p[1]), USER_EMAIL, spy_returns),
                universe.items()
            ))

        fail_stats = {}
        for r in res:
            status = r.get('Status', 'Unknown')
            if status != 'Pass':
                if ':' in status:
                    reason = status.split(':')[1].strip().split('(')[0].strip()
                    key = f"Fail: {reason}"
                else:
                    key = status
                fail_stats[key] = fail_stats.get(key, 0) + 1
        
        if fail_stats:
            logger.info("失敗原因分布：")
            for k, v in sorted(fail_stats.items(), key=lambda x: -x[1]):
                logger.info(f"  {k}: {v}")

        final_df = calculate_composite_alpha(res)
        send_email_report(final_df, USER_EMAIL, trend)

        if not final_df.empty:
            print(f"\n>>> 共 {len(final_df)} 檔通關，TOP 15：\n")
            cols = ['Ticker', 'ROIC(%)', 'ROIC_3Y_Avg(%)', 'ROIC_3Y_Min(%)',
                    'FCF_Yield(%)', 'Total_SH_Yield(%)', 'Momentum(%)',
                    'EV_Sales(x)', 'Pct_From_52W_High(%)', 'Alpha_Score', 'Exit_Signal']
            cols = [c for c in cols if c in final_df.columns]
            print(final_df.head(15)[cols].to_string(index=False))
        else:
            print("\n>>> 分析完成，今日無標的通過嚴格篩選。")

    except Exception as e:
        logger.critical(f"系統崩潰: {e}")
        traceback.print_exc()
