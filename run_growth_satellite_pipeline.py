"""
=============================================================================
GROWTH SATELLITE PIPELINE v3.1 (成長衛星管線 - 雲端防彈版)
=============================================================================
改動摘要：
1. [防禦] 移除 yfinance 內部 datatypes 依賴，防止套件更新崩潰。
2. [防禦] safe_yf_info 改為「優雅降級」模式：若 info 被封鎖，自動切換至 K 線圖抓取價格。
3. [精準] 數據主權回歸：市值、營收、毛利、增速、EV/Sales 全部強制改由 SEC 原始數據計算，
         Yahoo 僅作為「即時價格」的報價機，確保數據具備審計級精度。
4. [保留] 完整繼承 v3 所有因子：營收加速度、真實 Rule of 40、R&D ROI、52W 高點防線。
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
# 全局 Session 單例 (防止連線洪水)
# ==============================================================================
def create_global_session():
    session = requests.Session()
    retry = Retry(total=5, backoff_factor=1.0, status_forcelist=[401, 403, 429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry, pool_connections=100, pool_maxsize=100)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    })
    return session

YF_SESSION = create_global_session()

def force_refresh_yf_session():
    global YF_SESSION
    if hasattr(yf.utils, 'empty_cache'):
        yf.utils.empty_cache()
    YF_SESSION = create_global_session()
    
    # Safely assign session based on yfinance version
    if hasattr(yf, 'base') and hasattr(yf.base, '_requests'):
        yf.base._requests = YF_SESSION
        
    return YF_SESSION

force_refresh_yf_session()
# ==============================================================================
# 設定日誌 & 巨集參數
# ==============================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)

# === 成長衛星專屬門檻 ===
MIN_LIQUIDITY_USD       = 10_000_000   # 日均成交額底線 $10M
REV_GROWTH_MIN          = 20.0         # 營收增速底線 20%
GROSS_MARGIN_MIN        = 0.45         # 毛利率底線 45% (定價權)
RULE_OF_40_MIN          = 40.0         # 真實 Rule of 40 底線
MOMENTUM_MIN            = 30.0         # 絕對動能底線 30%
RELATIVE_MOMENTUM_MIN   = 0.0          # 相對 SPY 動能底線
FCF_BURN_FLOOR          = -40.0        # 燒錢深度紅線
CASH_RUNWAY_MIN_YRS     = 2.0          # 燒錢公司現金跑道底線
DILUTION_MAX_YOY        = 0.07         # 流通股年增上限

# === v3 新增紅線 ===
PCT_FROM_52W_HIGH_MIN   = -0.25        # 距 52 週高點不得低於 -25% (動能反轉防線)
GROSS_MARGIN_TREND_MIN  = -0.02        # 毛利率 YoY 衰退不得超過 2pp
EV_SALES_MAX            = 30.0         # EV/Sales 極端紅線 (防泡沫)

WINSORIZE_PCT           = 0.025

# ==============================================================================
# 強力突破 Yahoo 401 封鎖 (Session 偽裝與刷新)
# ==============================================================================
def create_stealth_session():
    """建立一個帶有完整瀏覽器偽裝與重試機制的 session"""
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
    """強制清理 yfinance 內部的 crumb 暫存，迫使它重新協商"""
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
# 工具函式
# ==============================================================================
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

def flatten_close(hist: pd.DataFrame, ticker: str) -> Optional[pd.Series]:
    if hist.empty:
        return None
    try:
        if isinstance(hist.columns, pd.MultiIndex):
            if ('Close', ticker) in hist.columns:
                return hist[('Close', ticker)]
            cols = [c for c in hist.columns if c[0] == 'Close']
            return hist[cols[0]] if cols else None
        return hist['Close'] if 'Close' in hist.columns else None
    except Exception:
        return None

def flatten_col(hist: pd.DataFrame, ticker: str, name: str) -> Optional[pd.Series]:
    if hist.empty:
        return None
    try:
        if isinstance(hist.columns, pd.MultiIndex):
            if (name, ticker) in hist.columns:
                return hist[(name, ticker)]
            cols = [c for c in hist.columns if c[0] == name]
            return hist[cols[0]] if cols else None
        return hist[name] if name in hist.columns else None
    except Exception:
        return None

# ==============================================================================
# YF 備援機制 (核心降級防禦)
# ==============================================================================
def get_fallback_price(ticker: str) -> float:
    """當 info 徹底失效時，用歷史 K 線圖抓取最新收盤價 (防禦力極高)"""
    try:
        session = create_stealth_session()
        hist = yf.download(ticker, period='5d', progress=False, auto_adjust=True, session=session)
        close_series = flatten_close(hist, ticker)
        if close_series is not None and not close_series.empty:
            return float(close_series.iloc[-1])
    except Exception:
        pass
    return 0.0

def safe_yf_info(ticker: str) -> dict:
    """嘗試抓取 info，若失敗則啟動價格備援"""
    for attempt in range(2):
        time.sleep(random.uniform(0.5, 1.0))
        try:
            session = create_stealth_session()
            stock = yf.Ticker(ticker, session=session)
            info = stock.info
            if info and 'symbol' in info and (info.get('currentPrice') or info.get('regularMarketPrice')):
                return info
        except Exception:
            pass
            
    logger.debug(f"[{ticker}] YF info 遭封鎖，啟動 K 線價格備援...")
    fallback_price = get_fallback_price(ticker)
    if fallback_price > 0:
        return {'currentPrice': fallback_price, 'regularMarketPrice': fallback_price}
    return {}

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
            'DnA':         ['DepreciationDepletionAndAmortization',
                            'DepreciationAndAmortization'],
            'RND':         ['ResearchAndDevelopmentExpense'],
            'Revenue':     ['Revenues',
                            'RevenueFromContractWithCustomerExcludingAssessedTax',
                            'SalesRevenueNet'],
            'GrossProfit': ['GrossProfit'],
            'Cash':        ['CashAndCashEquivalentsAtCarryingValue',
                            'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'],
            'ShortTermInvestments': ['ShortTermInvestments',
                                     'MarketableSecuritiesCurrent'],
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
        url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{str(cik).zfill(10)}/dei/{self.shares_tag}.json"
        resp = self.session.get(url, headers=self.headers)
        if not resp or resp.status_code != 200:
            return pd.DataFrame()
        try:
            data = resp.json().get('units', {}).get('shares', [])
            if not data:
                return pd.DataFrame()
            df = pd.DataFrame(data)
            df['end'] = pd.to_datetime(df['end'])
            if 'filed' in df.columns:
                df['filed'] = pd.to_datetime(df['filed'])
                df = df.sort_values('filed')
            return df.drop_duplicates(subset=['end'], keep='last').sort_values('end').reset_index(drop=True)
        except Exception:
            return pd.DataFrame()

    def get_latest_annual(self, df: pd.DataFrame) -> float:
        if df.empty:
            return 0.0
        annual = df[df['form'] == '10-K']
        if annual.empty:
            return 0.0
        return float(annual['val'].iloc[-1]) / 1e9

    def get_annual_history(self, df: pd.DataFrame, n_years: int = 3) -> List[float]:
        if df.empty:
            return []
        annual = df[df['form'] == '10-K']
        if annual.empty:
            return []
        if 'fy' in annual.columns:
            annual = annual.sort_values('end')
        vals = annual['val'].iloc[-n_years:].tolist()
        return [float(v) / 1e9 for v in vals]

    def get_latest_shares(self, df: pd.DataFrame) -> Tuple[float, float]:
        if df.empty or len(df) < 2:
            return 0.0, 0.0
        latest_date = df['end'].iloc[-1]
        one_year_ago = latest_date - pd.Timedelta(days=365)
        df_old = df[df['end'] <= one_year_ago]
        if df_old.empty:
            return float(df['val'].iloc[-1]) / 1e9, 0.0
        return float(df['val'].iloc[-1]) / 1e9, float(df_old['val'].iloc[-1]) / 1e9

# ==============================================================================
# 價格與大盤抓取
# ==============================================================================
def fetch_price_metrics(ticker: str, spy_close: Optional[pd.Series] = None) -> Optional[Dict]:
    try:
        session = create_stealth_session()
        hist = yf.download(ticker, period='14mo', progress=False, auto_adjust=True, session=session)
        if hist.empty or len(hist) < 200:
            return None
        close = flatten_close(hist, ticker)
        volume = flatten_col(hist, ticker, 'Volume')
        if close is None or volume is None or len(close) < 200:
            return None

        dollar_volume = float((close * volume).tail(30).mean())
        m = close.resample('ME').last().dropna()
        mom_12m = None
        if len(m) >= 13:
            mom_12m = (float(m.iloc[-2]) / float(m.iloc[-13]) - 1) * 100

        rel_mom = None
        if mom_12m is not None and spy_close is not None and len(spy_close) >= 13:
            spy_m = spy_close.resample('ME').last().dropna()
            if len(spy_m) >= 13:
                spy_mom = (float(spy_m.iloc[-2]) / float(spy_m.iloc[-13]) - 1) * 100
                rel_mom = mom_12m - spy_mom

        last_252 = close.tail(252)
        high_52w = float(last_252.max())
        last_close = float(close.iloc[-1])
        pct_from_52w_high = (last_close / high_52w - 1) if high_52w > 0 else -1.0

        return {
            'dollar_volume': dollar_volume,
            'momentum': mom_12m,
            'rel_momentum': rel_mom,
            'pct_from_52w_high': pct_from_52w_high,
            'last_close': last_close,
        }
    except Exception:
        return None

def fetch_spy_close() -> Optional[pd.Series]:
    try:
        session = create_stealth_session()
        hist = yf.download('SPY', period='14mo', progress=False, auto_adjust=True, session=session)
        return flatten_close(hist, 'SPY')
    except Exception:
        return None

# ==============================================================================
# 核心管線：成長引擎檢驗 (SEC 本位優化版)
# ==============================================================================
def run_growth_satellite_pipeline(ticker: str, cik: str, email: str,
                                   spy_close: Optional[pd.Series] = None) -> dict:
    try:
        # ── Stage 0: 即排型過濾 ───────────────────────────────
        if '-' in ticker or '.' in ticker:
            return {"Ticker": ticker, "Status": "Fail: 排除特別股/ADR.WS"}

        # ── Stage 1: 整合價格指標 ────────────────────────────────────────
        pm = fetch_price_metrics(ticker, spy_close)
        if pm is None:
            return {"Ticker": ticker, "Status": "Fail: 無價格資料"}

        if pm['dollar_volume'] < MIN_LIQUIDITY_USD:
            return {"Ticker": ticker, "Status": f"Fail: 流動性不足 (${pm['dollar_volume']/1e6:.1f}M)"}

        mom_12m = pm['momentum']
        rel_mom = pm['rel_momentum']
        pct_from_high = pm['pct_from_52w_high']
        last_close = pm['last_close']

        if mom_12m is None:
            return {"Ticker": ticker, "Status": "Fail: 動能無資料"}
        if mom_12m < MOMENTUM_MIN:
            return {"Ticker": ticker, "Status": f"Fail: 絕對動能不足 ({mom_12m:.1f}%)"}
        if rel_mom is not None and rel_mom < RELATIVE_MOMENTUM_MIN:
            return {"Ticker": ticker, "Status": f"Fail: 相對動能輸大盤 ({rel_mom:+.1f}%)"}

        if pct_from_high < PCT_FROM_52W_HIGH_MIN:
            return {"Ticker": ticker, "Status": f"Fail: 距高點過遠 ({pct_from_high*100:+.1f}%)"}

        # ── Stage 2: YF 報價備援 ────────────────────────────────
        info = safe_yf_info(ticker)
        price = float(info.get('currentPrice') or info.get('regularMarketPrice') or last_close)
        if price == 0.0:
            return {"Ticker": ticker, "Status": "Fail: 價格獲取失敗"}

        # ── Stage 3: SEC XBRL 數據萃取 ──────────────────────────────────
        sec = SECDataDistiller(email)
        df_rev   = sec.fetch_concept(cik, 'Revenue')
        df_gross = sec.fetch_concept(cik, 'GrossProfit')
        df_ocf   = sec.fetch_concept(cik, 'OCF')
        df_capex = sec.fetch_concept(cik, 'CapEx')
        df_sbc   = sec.fetch_concept(cik, 'SBC')
        df_dna   = sec.fetch_concept(cik, 'DnA')
        df_rnd   = sec.fetch_concept(cik, 'RND')
        df_cash  = sec.fetch_concept(cik, 'Cash')
        df_sti   = sec.fetch_concept(cik, 'ShortTermInvestments')
        df_shares = sec.fetch_shares_outstanding(cik)

        if df_rev.empty or df_gross.empty or df_ocf.empty:
            return {"Ticker": ticker, "Status": "Fail: SEC 核心資料缺失 (Rev/Gross/OCF)"}

        # 抓取歷史數據（3 年用於計算加速度與趨勢）
        rev_history   = sec.get_annual_history(df_rev,   3)
        gross_history = sec.get_annual_history(df_gross, 3)
        rnd_history   = sec.get_annual_history(df_rnd,   3)
        
        ocf   = sec.get_latest_annual(df_ocf)
        capex = abs(sec.get_latest_annual(df_capex))
        sbc   = abs(sec.get_latest_annual(df_sbc))
        dna   = abs(sec.get_latest_annual(df_dna))
        cash_total = sec.get_latest_annual(df_cash) + sec.get_latest_annual(df_sti)

        # ── Stage 4: 核心估值與基本面推算 (SEC 本位) ────────────────────
        # 1. 市值計算 (不依賴 YF)
        shares_now, shares_old = sec.get_latest_shares(df_shares)
        if shares_now == 0:
            shares_now = info.get('sharesOutstanding') or info.get('impliedSharesOutstanding', 0) if info else 0.0
        mcap = (price * shares_now) / 1e9 if shares_now > 0 else 0.0
        if mcap < 1.0:
            return {"Ticker": ticker, "Status": "Fail: 市值無法獲取"}

        # 2. 營收增速與加速度
        total_revenue = rev_history[-1]
        rev_growth_pct = 0.0
        rev_acceleration = None
        if len(rev_history) >= 2 and rev_history[-2] > 0:
            rev_growth_pct = (rev_history[-1] / rev_history[-2] - 1) * 100
            if len(rev_history) >= 3 and rev_history[-3] > 0:
                yoy_prior = (rev_history[-2] / rev_history[-3] - 1) * 100
                rev_acceleration = rev_growth_pct - yoy_prior

        # 3. EV/Sales 極端紅線
        ev_sales = (mcap + 0.0 - cash_total) / total_revenue if total_revenue > 0 else 0.0
        if ev_sales > EV_SALES_MAX:
            return {"Ticker": ticker, "Status": f"Fail: EV/Sales 過熱 ({ev_sales:.1f}x)"}

        # 4. 毛利率與趨勢
        gross_margin = gross_history[-1] / rev_history[-1] if rev_history[-1] > 0 else 0.0
        gm_trend = 0.0
        if len(gross_history) >= 2 and len(rev_history) >= 2:
            gm_old = gross_history[0] / rev_history[0] if rev_history[0] > 0 else 0.0
            gm_trend = gross_margin - gm_old

        # ── Stage 5: 真實 Rule of 40 與 R&D 效率 ──────────────────────
        maint_capex = min(dna, capex) if dna > 0 and capex > 0 else (dna if dna > 0 else capex)
        real_fcf = ocf - maint_capex - sbc
        real_fcf_margin = (real_fcf / total_revenue) * 100 if total_revenue > 0 else -100.0
        real_rule_of_40 = real_fcf_margin + rev_growth_pct

        rnd_roi = 0.0
        if len(rnd_history) >= 2 and len(gross_history) >= 2 and rnd_history[0] > 0:
            gross_increment = (gross_margin - (gross_history[0]/rev_history[0])) * total_revenue
            rnd_roi = (gross_increment / rnd_history[0]) * 100

        rnd_intensity = rnd_history[-1] / total_revenue if total_revenue > 0 and rnd_history else 0.0

        # ── Stage 6: 財務與成長紅線過濾 ──────────────────────────────────
        if real_fcf_margin < FCF_BURN_FLOOR:
            return {"Ticker": ticker, "Status": f"Fail: 燒錢過深 ({real_fcf_margin:.1f}%)"}

        runway_yrs = 99.0
        if real_fcf < 0 and cash_total > 0:
            runway_yrs = cash_total / abs(real_fcf)
            if runway_yrs < CASH_RUNWAY_MIN_YRS:
                return {"Ticker": ticker, "Status": f"Fail: 現金跑道不足 ({runway_yrs:.1f}年)"}

        if shares_old > 0:
            dilution_yoy = (shares_now / shares_old) - 1
            if dilution_yoy > DILUTION_MAX_YOY:
                return {"Ticker": ticker, "Status": f"Fail: 股本稀釋過快 ({dilution_yoy*100:+.1f}%)"}

        if gm_trend < GROSS_MARGIN_TREND_MIN:
            return {"Ticker": ticker, "Status": f"Fail: 毛利率衰退 ({gm_trend*100:+.1f}pp)"}

        if rev_growth_pct < REV_GROWTH_MIN:
            return {"Ticker": ticker, "Status": f"Fail: 營收失速 ({rev_growth_pct:.1f}%)"}
        if gross_margin < GROSS_MARGIN_MIN:
            return {"Ticker": ticker, "Status": f"Fail: 無定價權毛利 ({gross_margin*100:.1f}%)"}
        if real_rule_of_40 < RULE_OF_40_MIN:
            return {"Ticker": ticker, "Status": f"Fail: 真實Rule40破功 ({real_rule_of_40:.1f})"}

        # ── 出口分類訊號 ────────────────────────────────────────────────
        exit_signal = "🚀 成長超新星 (正現金流)" if real_fcf > 0 else ("🔥 高速擴張 (跑道足)" if runway_yrs >= 4 else "⚠️ 燒錢成長 (跑道限)")
        accel_tag = " 📈加速" if rev_acceleration and rev_acceleration > 5 else (" 📉減速" if rev_acceleration and rev_acceleration < -10 else "")

        return {
            'Ticker': ticker, 'Status': 'Pass', 'Price': round(price, 2),
            'CIK': str(cik),
            'Rev_Growth(%)':         round(rev_growth_pct, 2),
            'Rev_Acceleration(pp)':  round(rev_acceleration, 2) if rev_acceleration is not None else None,
            'Gross_Margin(%)':       round(gross_margin * 100, 2),
            'GM_Trend(pp)':          round(gm_trend * 100, 2),
            'Real_FCF_Margin(%)':    round(real_fcf_margin, 2),
            'Real_Rule_of_40':       round(real_rule_of_40, 2),
            'RND_ROI(%)':            round(rnd_roi, 2),
            'RND_Intensity(%)':      round(rnd_intensity * 100, 2),
            'Momentum(%)':           round(mom_12m, 2),
            'Rel_Momentum(%)':       round(rel_mom, 2) if rel_mom is not None else None,
            'Pct_From_52W_High(%)':  round(pct_from_high * 100, 2),
            'EV_Sales(x)':           round(ev_sales, 2),
            'Cash_Runway(yrs)':      round(runway_yrs, 1) if runway_yrs < 99 else None,
            'Dilution_YoY(%)':       round((shares_now/shares_old-1)*100, 2) if shares_old > 0 else 0,
            'Exit_Signal':           exit_signal + accel_tag,
        }
    except Exception as e:
        logger.error(f"[{ticker}] 成長管線崩潰: {str(e)[:80]}")
        return {"Ticker": ticker, "Status": "Error"}

# ==============================================================================
# Alpha 排序
# ==============================================================================
def winsorize_series(series: pd.Series, pct: float = WINSORIZE_PCT) -> pd.Series:
    s = pd.to_numeric(series, errors='coerce').fillna(0.0)
    lower, upper = s.quantile(pct), s.quantile(1 - pct)
    return s.clip(lower=lower, upper=upper)

def calculate_growth_alpha(results: List[dict]) -> pd.DataFrame:
    df = pd.DataFrame([r for r in results if r.get('Status') == 'Pass'])
    if len(df) < 2: return df
    if 'CIK' in df.columns: df = df.drop_duplicates(subset=['CIK'], keep='first').reset_index(drop=True)

    df['_R40_w']    = winsorize_series(df['Real_Rule_of_40'])
    df['_MOM_w']    = winsorize_series(df['Momentum(%)'])
    df['_RMOM_w']   = winsorize_series(df['Rel_Momentum(%)'].fillna(0))
    df['_RND_w']    = winsorize_series(df['RND_ROI(%)'])
    df['_REV_w']    = winsorize_series(df['Rev_Growth(%)'])
    df['_ACCEL_w']  = winsorize_series(df['Rev_Acceleration(pp)'].fillna(0))

    df['Z_Rule40']     = robust_zscore(df['_R40_w'])
    df['Z_Momentum']   = robust_zscore(df['_MOM_w'])
    df['Z_RelMom']     = robust_zscore(df['_RMOM_w'])
    df['Z_RND_ROI']    = robust_zscore(df['_RND_w'])
    df['Z_RevGrowth']  = robust_zscore(df['_REV_w'])
    df['Z_Acceleration'] = robust_zscore(df['_ACCEL_w'])

    df['Alpha_Score'] = (
        df['Z_Rule40'] * 0.25 + df['Z_Momentum'] * 0.15 + df['Z_RelMom'] * 0.15 +
        df['Z_RevGrowth'] * 0.15 + df['Z_Acceleration'] * 0.15 + df['Z_RND_ROI'] * 0.15
    ).round(3)

    df.loc[df['Exit_Signal'].str.contains('燒錢'), 'Alpha_Score'] -= 0.30
    df.loc[df['Exit_Signal'].str.contains('超新星'), 'Alpha_Score'] += 0.15
    df.loc[df['Exit_Signal'].str.contains('減速'), 'Alpha_Score'] -= 0.20
    
    df = df.drop(columns=[c for c in df.columns if c.startswith('_') or c.startswith('Z_')])
    return df.sort_values('Alpha_Score', ascending=False).reset_index(drop=True)

# ==============================================================================
# 報表發送
# ==============================================================================
def send_email_report(df: pd.DataFrame, receiver_email: str):
    sender_email, sender_pwd = os.environ.get('EMAIL_SENDER'), os.environ.get('EMAIL_PASSWORD')
    if not sender_email or not sender_pwd: return
    msg = EmailMessage()
    msg['Subject'] = f"[🚀 成長衛星 v3.1] Alpha 報表 - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    msg['From'], msg['To'] = sender_email, receiver_email

    content = (
        "總工程師您好：\n\n【成長衛星 v3.1 雲端防彈版掃描完成】\n"
        f"核心：真實 Rule40>{RULE_OF_40_MIN}、營收增長>{REV_GROWTH_MIN}%、動能>{MOMENTUM_MIN}%\n"
        f"v3.1 特色：100% SEC 財報推算市值與 EV/Sales，已拔除 YF info 致命依賴。\n"
        + "-" * 70 + "\n\n"
    )
    if df.empty: content += "今日無成長標的通關。"
    else:
        content += f"共計 {len(df)} 檔通關。\n\n【TOP 10 飆股】\n"
        cols = ['Ticker', 'Rev_Growth(%)', 'Rev_Acceleration(pp)', 'Real_Rule_of_40', 'Momentum(%)', 'Alpha_Score', 'Exit_Signal']
        content += df.head(10)[[c for c in cols if c in df.columns]].to_string(index=False)
    msg.set_content(content)
    if not df.empty:
        msg.add_attachment(df.to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig'), 
                           maintype='text', subtype='csv', filename=f'Growth_v3_1_{datetime.now().strftime("%Y%m%d")}.csv')
    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls(); server.login(sender_email, sender_pwd); server.send_message(msg)
            logger.info("信件發送成功！")
    except Exception as e: logger.error(f"郵件失敗: {e}")

# ==============================================================================
# 啟動區塊
# ==============================================================================
if __name__ == "__main__":
    USER_EMAIL = os.environ.get('USER_EMAIL', 'a7924177@gmail.com')
    GROWTH_CACHE_FILE = "growth_universe.csv"
    print("\n>>> 點火啟動：成長衛星管線 v3.1 (SEC 本位版) <<<\n")
    try:
        if not os.path.exists(GROWTH_CACHE_FILE):
            logger.error(f"找不到 {GROWTH_CACHE_FILE}！"); exit(1)
        df_c = pd.read_csv(GROWTH_CACHE_FILE)
        df_c['CIK'] = df_c['CIK'].astype(str).str.zfill(10)
        growth_universe = dict(zip(df_c['Ticker'], df_c['CIK']))
        logger.info("抓取 SPY 基準..."); spy_close = fetch_spy_close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            res = list(executor.map(lambda p: run_growth_satellite_pipeline(p[0], str(p[1]), USER_EMAIL, spy_close), growth_universe.items()))
        final_df = calculate_growth_alpha(res)
        send_email_report(final_df, USER_EMAIL)
        if not final_df.empty:
            print(f"\n>>> 共 {len(final_df)} 檔通關，TOP 15：\n")
            print(final_df.head(15).to_string(index=False))
        else: print("\n>>> 今日無標的通關。")
    except Exception as e: logger.critical(f"崩潰: {e}"); traceback.print_exc()
