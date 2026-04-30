"""
=============================================================================
GROWTH SATELLITE PIPELINE v3 (成長衛星管線 - 完全體)
=============================================================================
v3 vs v2 改動摘要：
1. [紅線] 新增 52 週高點防線 (距高點 ≤ 25%) — 抓動能反轉股
2. [紅線] 新增毛利率衰退紅線 (YoY 衰退 ≤ 2pp) — 抓殺價搶市佔的偽成長
3. [紅線] 新增 EV/Sales 極端紅線 (≤ 30) — 防泡沫頂部
4. [因子] 新增營收加速度 (3 年 YoY 比較) 作為 Alpha 加分因子
5. [資訊] 新增 R&D 強度欄位 (R&D/Revenue%) 作為目視判斷輔助
6. [資料] SEC 歷史抓取從 2 年延長到 3 年（給加速度計算用）
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
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import time
import random

# ==============================================================================
# 強力突破 Yahoo 401 封鎖 (Crumb 強制刷新機制)
# ==============================================================================
def create_stealth_session():
    """建立一個帶有完整瀏覽器偽裝與重試機制的 session"""
    session = requests.Session()
    # 針對 401 (Unauthorized) 進行指數退避重試
    retry = Retry(total=5, backoff_factor=1.5, status_forcelist=[401, 403, 429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive"
    })
    return session

def force_refresh_yf_crumb():
    """強制清理 yfinance 內部的 crumb 暫存，迫使它重新協商"""
    try:
        # 清除 yfinance 底層暫存
        yf.utils.empty_cache()
        # 強制替換 session
        yf.base._requests = create_stealth_session()
        # 初始化 crumb manager
        from yfinance.utils import crumb_manager
        crumb_manager._crumb = None
        crumb_manager._cookie = None
        crumb_manager.get_crumb() # 強制索取新 crumb
        return True
    except Exception as e:
        logger.debug(f"Crumb 刷新失敗: {e}")
        return False

# 程式啟動時先執行一次刷新
force_refresh_yf_crumb()
# ==============================================================================
# =======================================================
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

def safe_yf_info(ticker: str) -> dict:
    for attempt in range(4): # 最多嘗試 4 次
        # 每次嘗試前，稍微隨機暫停，避免被頻率偵測
        time.sleep(random.uniform(0.8, 2.5))
        
        # 建立一個獨立的 Ticker 物件，並強制塞入最新的 stealth session
        session = create_stealth_session()
        stock = yf.Ticker(ticker, session=session)
        
        try:
            info = stock.info
            # 嚴格驗證：不但要抓到 info，還必須有實質的價格數據才算過關
            if info and 'symbol' in info and (info.get('marketCap') or info.get('currentPrice') or info.get('regularMarketPrice')):
                return info
            else:
                # 抓到空殼字典，代表被擋了，觸發強制重置
                logger.debug(f"[{ticker}] 抓到空殼 info，準備重置 Crumb (第 {attempt+1} 次嘗試)")
                force_refresh_yf_crumb()
                
        except Exception as e:
            err_str = str(e)
            if "401" in err_str or "Invalid Crumb" in err_str:
                logger.debug(f"[{ticker}] 遭遇 401 封鎖，強制重置 Crumb (第 {attempt+1} 次嘗試)")
                force_refresh_yf_crumb()
            else:
                logger.debug(f"[{ticker}] YF 抓取異常: {err_str}")
            
            # 指數退避延遲：1.5s -> 3s -> 6s -> 12s
            time.sleep(1.5 * (2 ** attempt))
            
    # 如果撞了 4 次還是失敗，按照你的要求「不降級」，直接回傳空字典，讓外層管線 Fail
    logger.error(f"[{ticker}] YF info 徹底抓取失敗，放棄該標的。")
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
        self.shares_tag = 'EntityCommonStockSharesOutstanding'

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
        """回傳最近 n 個年度 10-K 數值（舊→新），單位：十億美元。v3 預設 3 年。"""
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
        return (float(df['val'].iloc[-1]) / 1e9,
                float(df_old['val'].iloc[-1]) / 1e9)

# ==============================================================================
# 整合的價格資料抓取（流動性 + 動能 + 相對動能 + 52週高點 一次搞定）
# ==============================================================================
def fetch_price_metrics(ticker: str, spy_close: Optional[pd.Series] = None) -> Optional[Dict]:
    """
    一次下載 14 個月價格，計算所有價格相關指標：
    - 30 日 dollar volume (流動性)
    - 12-1 動能 (skip-1-month)
    - 相對 SPY 動能
    - 距離 52 週高點百分比 (v3 新增)
    """
    try:
        hist = yf.download(ticker, period='14mo', progress=False, auto_adjust=True)
        if hist.empty or len(hist) < 200:
            return None
        close = flatten_close(hist, ticker)
        volume = flatten_col(hist, ticker, 'Volume')
        if close is None or volume is None or len(close) < 200:
            return None

        # 流動性
        dollar_volume = float((close * volume).tail(30).mean())

        # 12-1 動能
        m = close.resample('ME').last().dropna()
        mom_12m = None
        if len(m) >= 13:
            mom_12m = (float(m.iloc[-2]) / float(m.iloc[-13]) - 1) * 100

        # 相對 SPY 動能
        rel_mom = None
        if mom_12m is not None and spy_close is not None and len(spy_close) >= 13:
            spy_m = spy_close.resample('ME').last().dropna()
            if len(spy_m) >= 13:
                spy_mom = (float(spy_m.iloc[-2]) / float(spy_m.iloc[-13]) - 1) * 100
                rel_mom = mom_12m - spy_mom

        # ★v3 新增：52 週高點相對位置
        # 取最近 252 個交易日（約 1 年）的最高價
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
        hist = yf.download('SPY', period='14mo', progress=False, auto_adjust=True)
        return flatten_close(hist, 'SPY')
    except Exception:
        return None

# ==============================================================================
# 核心管線：成長引擎檢驗
# ==============================================================================
def run_growth_satellite_pipeline(ticker: str, cik: str, email: str,
                                   spy_close: Optional[pd.Series] = None) -> dict:
    try:
        # ── Stage 0: 即排型過濾 (零成本) ───────────────────────────────
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

        if mom_12m is None:
            return {"Ticker": ticker, "Status": "Fail: 動能無資料"}
        if mom_12m < MOMENTUM_MIN:
            return {"Ticker": ticker, "Status": f"Fail: 絕對動能不足 ({mom_12m:.1f}%)"}
        if rel_mom is not None and rel_mom < RELATIVE_MOMENTUM_MIN:
            return {"Ticker": ticker, "Status": f"Fail: 相對動能輸大盤 ({rel_mom:+.1f}%)"}

        # ★v3 新增：52 週高點防線 (動能反轉股偵測)
        if pct_from_high < PCT_FROM_52W_HIGH_MIN:
            return {"Ticker": ticker,
                    "Status": f"Fail: 距高點過遠 ({pct_from_high*100:+.1f}%)"}

        # ── Stage 2: yfinance 基本面快查 ────────────────────────────────
        info = safe_yf_info(ticker)
        if not info:
            return {"Ticker": ticker, "Status": "Fail: YF info 抓取失敗"}
        price = float(info.get('currentPrice') or info.get('regularMarketPrice') or 0.0)
        rev_growth_yf = float(info.get('revenueGrowth') or 0.0)
        total_revenue_yf = float(info.get('totalRevenue') or 0.0) / 1e9

        # ★v3 新增：EV/Sales 極端紅線
        ev_sales = float(info.get('enterpriseToRevenue') or 0.0)
        # 若 YF 沒給，用 marketCap / revenue 粗估 (忽略 net debt)
        if ev_sales == 0.0:
            mc = float(info.get('marketCap') or 0.0) / 1e9
            if mc > 0 and total_revenue_yf > 0:
                ev_sales = mc / total_revenue_yf
        if ev_sales > EV_SALES_MAX:
            return {"Ticker": ticker,
                    "Status": f"Fail: EV/Sales 過熱 ({ev_sales:.1f}x)"}

        # ── Stage 3: SEC XBRL 核心數據 ──────────────────────────────────
        sec = SECDataDistiller(email)
        df_ocf   = sec.fetch_concept(cik, 'OCF')
        df_capex = sec.fetch_concept(cik, 'CapEx')
        df_sbc   = sec.fetch_concept(cik, 'SBC')
        df_dna   = sec.fetch_concept(cik, 'DnA')
        df_rnd   = sec.fetch_concept(cik, 'RND')
        df_rev   = sec.fetch_concept(cik, 'Revenue')
        df_gross = sec.fetch_concept(cik, 'GrossProfit')
        df_cash  = sec.fetch_concept(cik, 'Cash')
        df_sti   = sec.fetch_concept(cik, 'ShortTermInvestments')
        df_shares = sec.fetch_shares_outstanding(cik)

        ocf   = sec.get_latest_annual(df_ocf)
        capex = abs(sec.get_latest_annual(df_capex))
        sbc   = abs(sec.get_latest_annual(df_sbc))
        dna   = abs(sec.get_latest_annual(df_dna))
        cash_total = sec.get_latest_annual(df_cash) + sec.get_latest_annual(df_sti)

        # ★v3 改動：歷史抓 3 年（給加速度計算用）
        rev_history   = sec.get_annual_history(df_rev,   3)
        rnd_history   = sec.get_annual_history(df_rnd,   3)
        gross_history = sec.get_annual_history(df_gross, 3)

        if not rev_history or rev_history[-1] <= 0:
            return {"Ticker": ticker, "Status": "Fail: SEC 無營收資料"}

        total_revenue = total_revenue_yf if total_revenue_yf > 0 else rev_history[-1]

        # 營收增速：優先 SEC 自算，YF 備援
        rev_growth = 0.0
        if len(rev_history) >= 2 and rev_history[-2] > 0:
            rev_growth = (rev_history[-1] / rev_history[-2]) - 1
        elif rev_growth_yf != 0.0:
            rev_growth = rev_growth_yf
        rev_growth_pct = rev_growth * 100

        # ★v3 新增：營收加速度 (3 年資料才能算)
        rev_acceleration = None  # 單位：百分點
        if (len(rev_history) >= 3 and rev_history[-3] > 0 and rev_history[-2] > 0):
            yoy_recent = (rev_history[-1] / rev_history[-2] - 1) * 100
            yoy_prior  = (rev_history[-2] / rev_history[-3] - 1) * 100
            rev_acceleration = yoy_recent - yoy_prior  # 正值=加速、負值=減速

        # ── Stage 4: 真實 FCF 計算 ──────────────────────────────────────
        if capex == 0:
            capex = abs(float(info.get('capitalExpenditures') or 0)) / 1e9
        if sbc == 0:
            sbc = abs(float(info.get('shareBasedCompensation') or 0)) / 1e9

        if dna > 0 and capex > 0:
            maint_capex = min(dna, capex)
        elif dna > 0:
            maint_capex = dna
        else:
            maint_capex = capex

        real_fcf = ocf - maint_capex - sbc
        real_fcf_margin = (real_fcf / total_revenue) * 100 if total_revenue > 0 else -100.0
        real_rule_of_40 = real_fcf_margin + rev_growth_pct

        # 毛利率：優先 SEC 自算
        if gross_history and rev_history and rev_history[-1] > 0:
            gross_margin = gross_history[-1] / rev_history[-1]
        else:
            gross_margin = float(info.get('grossMargins') or 0.0)

        # ★v3 新增：毛利率趨勢 (用最舊 vs 最新比較，有 3 年資料更能看趨勢)
        gm_trend = 0.0  # 單位：百分點
        if (len(gross_history) >= 2 and len(rev_history) >= 2
                and gross_history[0] > 0 and rev_history[0] > 0
                and rev_history[-1] > 0):
            gm_old = gross_history[0] / rev_history[0]
            gm_new = gross_history[-1] / rev_history[-1]
            gm_trend = gm_new - gm_old

        # ── Stage 5: R&D ROI (改進版：剔除規模擴張的偽訊號) ────────────
        rnd_roi = 0.0
        if (len(rnd_history) >= 2 and len(gross_history) >= 2
                and len(rev_history) >= 2 and rev_history[0] > 0
                and rev_history[-1] > 0 and rnd_history[0] > 0):
            gm_old = gross_history[0] / rev_history[0]
            gm_new = gross_history[-1] / rev_history[-1]
            margin_driven_gp = (gm_new - gm_old) * rev_history[-1]
            rnd_roi = (margin_driven_gp / rnd_history[0]) * 100

        # ★v3 新增：R&D 強度 (R&D / Revenue) — 作為資訊欄位，不當紅線
        rnd_intensity = 0.0
        if rnd_history and rev_history and rev_history[-1] > 0:
            rnd_intensity = rnd_history[-1] / rev_history[-1]

        # ── Stage 6: 財務結構紅線 ───────────────────────────────────────
        if real_fcf_margin < FCF_BURN_FLOOR:
            return {"Ticker": ticker,
                    "Status": f"Fail: 燒錢過深 ({real_fcf_margin:.1f}%)"}

        # 現金跑道
        runway_yrs = 99.0
        if real_fcf < 0 and cash_total > 0:
            annual_burn = abs(real_fcf)
            runway_yrs = cash_total / annual_burn if annual_burn > 0 else 99.0
            if runway_yrs < CASH_RUNWAY_MIN_YRS:
                return {"Ticker": ticker,
                        "Status": f"Fail: 現金跑道不足 ({runway_yrs:.1f}年)"}

        # 股本稀釋
        shares_now, shares_old = sec.get_latest_shares(df_shares)
        dilution_yoy = 0.0
        if shares_old > 0:
            dilution_yoy = (shares_now / shares_old) - 1
            if dilution_yoy > DILUTION_MAX_YOY:
                return {"Ticker": ticker,
                        "Status": f"Fail: 股本稀釋過快 ({dilution_yoy*100:+.1f}%)"}

        # ★v3 新增：毛利率衰退紅線
        if gm_trend < GROSS_MARGIN_TREND_MIN:
            return {"Ticker": ticker,
                    "Status": f"Fail: 毛利率衰退 ({gm_trend*100:+.1f}pp)"}

        # ── Stage 7: 成長引擎核心過濾 ───────────────────────────────────
        if rev_growth_pct < REV_GROWTH_MIN:
            return {"Ticker": ticker,
                    "Status": f"Fail: 營收失速 ({rev_growth_pct:.1f}%)"}
        if gross_margin < GROSS_MARGIN_MIN:
            return {"Ticker": ticker,
                    "Status": f"Fail: 無護城河毛利 ({gross_margin*100:.1f}%)"}
        if real_rule_of_40 < RULE_OF_40_MIN:
            return {"Ticker": ticker,
                    "Status": f"Fail: 真實Rule40破功 ({real_rule_of_40:.1f})"}

        # ── 出口分類訊號 ────────────────────────────────────────────────
        if real_fcf > 0:
            exit_signal = "🚀 成長超新星 (正現金流)"
        elif runway_yrs >= 4:
            exit_signal = "🔥 高速擴張 (跑道充足)"
        else:
            exit_signal = "⚠️ 燒錢成長 (跑道有限)"

        # 加速度標記 (用於後續排序加分顯示)
        accel_tag = ""
        if rev_acceleration is not None:
            if rev_acceleration > 5:
                accel_tag = " 📈加速"
            elif rev_acceleration < -10:
                accel_tag = " 📉減速"

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
            'Dilution_YoY(%)':       round(dilution_yoy * 100, 2),
            'Exit_Signal':           exit_signal + accel_tag,
        }
    except Exception as e:
        err_msg = str(e).split('\n')[0][:80]
        logger.error(f"[{ticker}] 成長管線崩潰: {err_msg}")
        return {"Ticker": ticker, "Status": "Error"}

# ==============================================================================
# Alpha 排序 (成長股專屬公式)
# ==============================================================================
def winsorize_series(series: pd.Series, pct: float = WINSORIZE_PCT) -> pd.Series:
    s = pd.to_numeric(series, errors='coerce').fillna(0.0)
    lower, upper = s.quantile(pct), s.quantile(1 - pct)
    return s.clip(lower=lower, upper=upper)

def calculate_growth_alpha(results: List[dict]) -> pd.DataFrame:
    df = pd.DataFrame([r for r in results if r.get('Status') == 'Pass'])
    if len(df) < 2:
        return df

    if 'CIK' in df.columns:
        df = df.drop_duplicates(subset=['CIK'], keep='first').reset_index(drop=True)

    # Winsorize
    df['_R40_w']    = winsorize_series(df['Real_Rule_of_40'])
    df['_MOM_w']    = winsorize_series(df['Momentum(%)'])
    df['_RMOM_w']   = winsorize_series(df['Rel_Momentum(%)'].fillna(0))
    df['_RND_w']    = winsorize_series(df['RND_ROI(%)'])
    df['_REV_w']    = winsorize_series(df['Rev_Growth(%)'])
    df['_ACCEL_w']  = winsorize_series(df['Rev_Acceleration(pp)'].fillna(0))

    # Z-score
    df['Z_Rule40']     = robust_zscore(df['_R40_w'])
    df['Z_Momentum']   = robust_zscore(df['_MOM_w'])
    df['Z_RelMom']     = robust_zscore(df['_RMOM_w'])
    df['Z_RND_ROI']    = robust_zscore(df['_RND_w'])
    df['Z_RevGrowth']  = robust_zscore(df['_REV_w'])
    df['Z_Acceleration'] = robust_zscore(df['_ACCEL_w'])

    # ★v3 Alpha 公式：把營收加速度納入主因子
    df['Alpha_Score'] = (
        df['Z_Rule40']       * 0.25 +   # 健康的高成長
        df['Z_Momentum']     * 0.15 +   # 絕對動能
        df['Z_RelMom']       * 0.15 +   # 相對動能 (跨週期穩健)
        df['Z_RevGrowth']    * 0.15 +   # 營收爆發力
        df['Z_Acceleration'] * 0.15 +   # ★新增：加速 vs 減速
        df['Z_RND_ROI']      * 0.15     # 研發效率
    ).round(3)

    # 出口訊號加分/扣分
    df.loc[df['Exit_Signal'].str.contains('燒錢', na=False), 'Alpha_Score'] -= 0.30
    df.loc[df['Exit_Signal'].str.contains('超新星', na=False), 'Alpha_Score'] += 0.15
    df.loc[df['Exit_Signal'].str.contains('減速', na=False), 'Alpha_Score'] -= 0.20

    df = df.drop(columns=[c for c in df.columns if c.startswith('_') or c.startswith('Z_')])
    return df.sort_values('Alpha_Score', ascending=False).reset_index(drop=True)

# ==============================================================================
# 報表發送
# ==============================================================================
def send_email_report(df: pd.DataFrame, receiver_email: str):
    sender_email = os.environ.get('EMAIL_SENDER')
    sender_pwd = os.environ.get('EMAIL_PASSWORD')
    if not sender_email or not sender_pwd:
        logger.warning("未設定 EMAIL_SENDER 或 EMAIL_PASSWORD，略過發信。")
        return
    msg = EmailMessage()
    msg['Subject'] = f"[🚀 成長衛星 v3] Alpha 報表 - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    msg['From'] = sender_email
    msg['To'] = receiver_email

    content = (
        "總工程師您好：\n\n【成長衛星 v3 掃描完成】\n"
        f"核心濾網：真實 Rule40>{RULE_OF_40_MIN}、營收增長>{REV_GROWTH_MIN}%、"
        f"絕對動能>{MOMENTUM_MIN}%、相對 SPY>0\n"
        f"財務紅線：燒錢深度>{FCF_BURN_FLOOR}%、現金跑道>{CASH_RUNWAY_MIN_YRS}年、"
        f"稀釋<{DILUTION_MAX_YOY*100:.0f}%\n"
        f"v3 新增紅線：距 52W 高<{abs(PCT_FROM_52W_HIGH_MIN)*100:.0f}%、"
        f"毛利率衰退<{abs(GROSS_MARGIN_TREND_MIN)*100:.0f}pp、"
        f"EV/Sales<{EV_SALES_MAX}x\n"
        + "-" * 70 + "\n\n"
    )

    if df.empty:
        content += "今日無成長標的通關。可能訊號：市場資金退潮、估值頂點、或無創新題材。"
    else:
        content += f"共計 {len(df)} 檔通關。\n\n【TOP 10 飆股】\n"
        cols = ['Ticker', 'Rev_Growth(%)', 'Rev_Acceleration(pp)', 'Real_Rule_of_40',
                'GM_Trend(pp)', 'Momentum(%)', 'EV_Sales(x)', 'Alpha_Score', 'Exit_Signal']
        cols = [c for c in cols if c in df.columns]
        content += df.head(10)[cols].to_string(index=False)

    msg.set_content(content)

    if not df.empty:
        msg.add_attachment(
            df.to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig'),
            maintype='text', subtype='csv',
            filename=f'Growth_Satellite_v3_{datetime.now().strftime("%Y%m%d")}.csv'
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
# 啟動區塊
# ==============================================================================
if __name__ == "__main__":
    USER_EMAIL = os.environ.get('USER_EMAIL', 'a7924177@gmail.com')
    GROWTH_CACHE_FILE = "growth_universe.csv"

    print("\n>>> 點火啟動：成長衛星管線 v3 (完全體) <<<\n")

    try:
        if not os.path.exists(GROWTH_CACHE_FILE):
            logger.error(f"找不到 {GROWTH_CACHE_FILE}，請先執行 generate_growth_universe.py！")
            exit(1)

        df_c = pd.read_csv(GROWTH_CACHE_FILE)
        df_c['CIK'] = df_c['CIK'].astype(str).str.zfill(10)
        growth_universe = dict(zip(df_c['Ticker'], df_c['CIK']))
        logger.info(f"讀取 {len(growth_universe)} 檔候選股")

        # 抓 SPY 基準
        logger.info("抓取 SPY 基準價格...")
        spy_close = fetch_spy_close()
        if spy_close is None:
            logger.warning("SPY 抓取失敗，相對動能將略過")

        # 多執行緒
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            res = list(executor.map(
                lambda p: run_growth_satellite_pipeline(p[0], str(p[1]), USER_EMAIL, spy_close),
                growth_universe.items()
            ))

        # 失敗原因分布
        fail_stats = {}
        for r in res:
            status = r.get('Status', 'Unknown')
            if status != 'Pass':
                key = status.split(':')[0] if ':' in status else status
                if ':' in status:
                    reason = status.split(':')[1].strip().split('(')[0].strip()
                    key = f"Fail: {reason}"
                fail_stats[key] = fail_stats.get(key, 0) + 1
        if fail_stats:
            logger.info("失敗原因分布：")
            for k, v in sorted(fail_stats.items(), key=lambda x: -x[1]):
                logger.info(f"  {k}: {v}")

        final_df = calculate_growth_alpha(res)
        send_email_report(final_df, USER_EMAIL)

        if not final_df.empty:
            print(f"\n>>> 共 {len(final_df)} 檔通關，TOP 15：\n")
            cols = ['Ticker', 'Rev_Growth(%)', 'Rev_Acceleration(pp)',
                    'Real_FCF_Margin(%)', 'Real_Rule_of_40', 'GM_Trend(pp)',
                    'RND_ROI(%)', 'RND_Intensity(%)', 'Momentum(%)',
                    'Pct_From_52W_High(%)', 'EV_Sales(x)', 'Alpha_Score', 'Exit_Signal']
            cols = [c for c in cols if c in final_df.columns]
            print(final_df.head(15)[cols].to_string(index=False))
        else:
            print("\n>>> 分析完成，今日無標的通過嚴格篩選。")

    except Exception as e:
        logger.critical(f"系統崩潰: {e}")
        traceback.print_exc()
