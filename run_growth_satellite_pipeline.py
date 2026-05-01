"""
=============================================================================
GROWTH SATELLITE PIPELINE v3.4.1 (成長衛星管線 - IndexError 修復補丁)
=============================================================================
v3.4.1 改動摘要 (vs v3.4)：
1. ★[Bug] form filter 從 == '10-K' 改為 startswith('10-K')。
   原本會過濾掉 10-K/A (修正案)，導致 AMD/APH/KLAC 等歷史有修正案的
   公司 rev_history 變空 list，下游 rev_history[-1] 觸發 IndexError 崩潰。
2. ★[Bug] rev_history / gross_history 抓取後加入空 list 防呆守門，
   回傳 "Fail: 無 10-K 年度數據" 而非讓 IndexError 擊穿整條管線。
   (例：剛 IPO 的 ARM 還沒累積足夠 10-K)
 
v3.4 改動摘要 (vs v3.3)：
1. ★[嚴重] EV 修正：補回 debt 進入 EV 計算。
2. ★[嚴重] 現金跑道修正：分母加入到期短債。
3. ★[嚴重] 併發/yf.Ticker 衝突修正：worker 線程零 yf.Ticker.info 呼叫。
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
# 設定日誌 & 巨集參數
# ==============================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)
 
# === 成長衛星專屬門檻 (v3.3 校準後) ===
MIN_LIQUIDITY_USD       = 10_000_000   # 日均成交額底線 $10M (不變)
REV_GROWTH_MIN          = 12.0         # 營收增速底線 (15→12)
GROSS_MARGIN_MIN        = 0.35         # 毛利率底線 (0.40→0.35)
RULE_OF_40_MIN          = 25.0         # 真實 Rule of 40 底線 (30→25)
MOMENTUM_MIN            = -5.0          # ★絕對動能底線 (15→0)：原本砍掉 77 檔
RELATIVE_MOMENTUM_MIN   = -10.0        # ★相對 SPY 動能底線 (0→-10)：配套放寬
FCF_BURN_FLOOR          = -40.0        # 燒錢深度紅線 (不變)
CASH_RUNWAY_MIN_YRS     = 2.0          # 燒錢公司現金跑道底線 (不變)
DILUTION_MAX_YOY        = 0.10         # 流通股年增上限 (0.07→0.10)
 
# === v3 新增紅線 (v3.3 微調) ===
PCT_FROM_52W_HIGH_MIN   = -0.45        # 距 52 週高點 (-0.35→-0.45)
GROSS_MARGIN_TREND_MIN  = -0.03        # 毛利率 YoY 衰退 (-0.02→-0.03)
EV_SALES_MAX            = 30.0         # EV/Sales 極端紅線 (不變)
 
# === v3.3 新增：市值地板 ===
MIN_MARKET_CAP_B        = 0.5          # 市值地板 (原 1.0B→0.5B)，搭配 marketCap fallback
 
WINSORIZE_PCT           = 0.025
 
# ==============================================================================
# 批量快取系統 (取代單檔抓取)
# ==============================================================================
_BULK_MARKET_DATA: Optional[pd.DataFrame] = None
 
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
# 強力突破 Yahoo 401 封鎖 (Session 偽裝與刷新)
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
 
# ==============================================================================
# YF 備援機制 (純快取驅動)
# ==============================================================================
def get_fallback_price(ticker: str) -> float:
    close = get_cached_series(ticker, 'Close')
    if close is not None and not close.empty:
        return float(close.iloc[-1])
    return 0.0
 
# v3.4: 啟動時填充的全域 info 快取，worker 線程「只讀」此 dict，不再呼叫 yf.Ticker
_INFO_CACHE: Dict[str, dict] = {}
 
def pre_fetch_all_info(tickers: List[str]):
    """主線程批量抓取所有 yf.info，避免 worker 線程觸發 Yahoo 限流。
    
    這是 v3.4 的關鍵修正：之前 safe_yf_info 在 worker 線程內 new yf.Ticker(t).info，
    高頻併發會觸發 401/429，自爆 _BULK_MARKET_DATA 的初衷。
    現在改為啟動時單線程順序抓一次，全部存進 _INFO_CACHE，worker 只讀字典。
    """
    global _INFO_CACHE
    logger.info(f"[v3.4] 主線程批量抓取 {len(tickers)} 檔 yf.info（單線程，避封鎖）...")
    success = 0
    for i, t in enumerate(tickers):
        try:
            info = yf.Ticker(t).info
            if info and isinstance(info, dict) and len(info) > 5:
                _INFO_CACHE[t] = dict(info)
                success += 1
            else:
                _INFO_CACHE[t] = {}
        except Exception:
            _INFO_CACHE[t] = {}
        if (i + 1) % 100 == 0:
            logger.info(f"  [info] 已抓取 {i+1}/{len(tickers)} (成功 {success})")
    logger.info(f"[v3.4] info 批量完成：成功 {success}/{len(tickers)}")
 
def safe_yf_info(ticker: str) -> dict:
    """v3.4 版：純讀 _INFO_CACHE，worker 線程零網路呼叫。
    若快取為空或無價格欄位，最終 fallback 到批量 K 線快取的最後收盤價。
    """
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
 
def fetch_price_metrics(ticker: str, spy_close: Optional[pd.Series] = None) -> Optional[Dict]:
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
            'OCF':         ['NetCashProvidedByUsedInOperatingActivities','NetCashProvidedByUsedInOperatingActivitiesContinuingOperations'],
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
                            'SalesRevenueNet','SalesRevenueGoodsNet'],
            'GrossProfit': ['GrossProfit','GrossMargin'],
            'Cash':        ['CashAndCashEquivalentsAtCarryingValue',
                            'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'],
            'ShortTermInvestments': ['ShortTermInvestments',
                                     'MarketableSecuritiesCurrent'],
            # v3.4 新增：debt 結構分離 (EV 與跑道計算用)
            'Debt':                ['LongTermDebt', 'LongTermDebtNoncurrent',
                                    'LongTermDebtAndCapitalLeaseObligations'],
            'ShortTermDebt':       ['DebtCurrent', 'LongTermDebtCurrent',
                                    'ShortTermBorrowings', 'CommercialPaper'],
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
        # 擴大雷達：加入更多常見的股本標籤，拯救那 37 檔因為標籤不同被錯殺的股票
        tags_to_try = [
            ('dei', 'EntityCommonStockSharesOutstanding'),
            ('us-gaap', 'CommonStockSharesOutstanding'),
            ('us-gaap', 'WeightedAverageNumberOfSharesOutstandingBasic')
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
        # v3.4.1: 涵蓋 10-K 與 10-K/A (修正案)，避免 AMD/APH/KLAC 之類的歷史修正案被過濾掉
        annual = df[df['form'].astype(str).str.startswith('10-K')]
        if annual.empty:
            return 0.0
        return float(annual['val'].iloc[-1]) / 1e9
 
    def get_annual_history(self, df: pd.DataFrame, n_years: int = 3) -> List[float]:
        if df.empty:
            return []
        # v3.4.1: 涵蓋 10-K 與 10-K/A
        annual = df[df['form'].astype(str).str.startswith('10-K')]
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

        # ====================================================================
        # ★ 動態環境動能校正 (Dynamic Regime-Adjusted Momentum)
        # ====================================================================
        # 1. 反推大盤 (SPY) 過去 12 個月的真實動能
        spy_mom = (mom_12m - rel_mom) if rel_mom is not None else 0.0
        
        # 2. 絕對動能防線 (動態放寬)：
        # 成長股的 Beta 通常較高。若大盤大跌，我們允許成長股承受大盤 1.5 倍的回撤。
        # 取 min() 是為了確保在多頭市場時，依然要守住原本設定的 MOMENTUM_MIN 底線。
        dynamic_mom_min = min(MOMENTUM_MIN, spy_mom * 1.5) 
        
        if mom_12m < dynamic_mom_min:
            return {"Ticker": ticker, "Status": f"Fail: 絕對動能不足 ({mom_12m:.1f}%, 底線 {dynamic_mom_min:.1f}%)"}
            
        # 3. 相對動能防線 (動態收緊)：
        # 當大盤處於跌勢 (spy_mom <= 0) 時，我們可以原諒你的絕對報酬為負，
        # 但你必須展現出強大的「相對抗跌性」。因此將相對動能底線嚴格拉高至 -5.0%。
        strict_rel_mom_min = RELATIVE_MOMENTUM_MIN if spy_mom > 0 else -5.0
        
        if rel_mom is not None and rel_mom < strict_rel_mom_min:
            return {"Ticker": ticker, "Status": f"Fail: 相對動能輸大盤 ({rel_mom:+.1f}%, 底線 {strict_rel_mom_min:+.1f}%)"}
        # ====================================================================
 
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
        df_debt  = sec.fetch_concept(cik, 'Debt')           # v3.4 新增
        df_std   = sec.fetch_concept(cik, 'ShortTermDebt')  # v3.4 新增
        df_shares = sec.fetch_shares_outstanding(cik)
 
        if df_rev.empty or df_gross.empty or df_ocf.empty:
            return {"Ticker": ticker, "Status": "Fail: SEC 核心資料缺失 (Rev/Gross/OCF)"}
 
        # 抓取歷史數據（3 年用於計算加速度與趨勢）
        rev_history   = sec.get_annual_history(df_rev,   3)
        gross_history = sec.get_annual_history(df_gross, 3)
        rnd_history   = sec.get_annual_history(df_rnd,   3)
        
        # v3.4.1 守門：history 為空表示沒有可用的 10-K 年度資料 (例如新 IPO 或 SEC tag 不匹配)
        # 不擋的話下游 rev_history[-1] 會直接 IndexError 把整支管線炸掉
        if not rev_history or not gross_history:
            return {"Ticker": ticker, "Status": "Fail: 無 10-K 年度數據"}
        
        ocf   = sec.get_latest_annual(df_ocf)
        capex = abs(sec.get_latest_annual(df_capex))
        sbc   = abs(sec.get_latest_annual(df_sbc))
        dna   = abs(sec.get_latest_annual(df_dna))
        cash_total = sec.get_latest_annual(df_cash) + sec.get_latest_annual(df_sti)
        # v3.4 新增：完整 debt 結構
        debt_lt = abs(sec.get_latest_annual(df_debt))     # 長期 + 部分總債
        debt_st = abs(sec.get_latest_annual(df_std))      # 短期 / 一年內到期
        debt_total = debt_lt + debt_st                     # 用於 EV 計算
 
        # ── Stage 4: 核心估值與基本面推算 (SEC 本位) ────────────────────
        # 1. 市值計算 (v3.3：加 marketCap fallback + 降低地板)
        shares_now, shares_old = sec.get_latest_shares(df_shares)
        if shares_now == 0:
            shares_now = info.get('sharesOutstanding') or info.get('impliedSharesOutstanding', 0) if info else 0.0
        mcap = (price * shares_now) / 1e9 if shares_now > 0 else 0.0
        # ★ v3.3 關鍵 fallback：若 SEC + YF shares 都拿不到，直接用 YF 的 marketCap
        if mcap < MIN_MARKET_CAP_B:
            mcap_yf = float(info.get('marketCap') or 0.0) / 1e9
            if mcap_yf >= MIN_MARKET_CAP_B:
                mcap = mcap_yf
        if mcap < MIN_MARKET_CAP_B:
            return {"Ticker": ticker, "Status": f"Fail: 市值無法獲取 ({mcap:.2f}B)"}
 
        # 2. 營收增速與加速度
        total_revenue = rev_history[-1]
        rev_growth_pct = 0.0
        rev_acceleration = None
        if len(rev_history) >= 2 and rev_history[-2] > 0:
            rev_growth_pct = (rev_history[-1] / rev_history[-2] - 1) * 100
            if len(rev_history) >= 3 and rev_history[-3] > 0:
                yoy_prior = (rev_history[-2] / rev_history[-3] - 1) * 100
                rev_acceleration = rev_growth_pct - yoy_prior
 
        # 3. EV/Sales 極端紅線 (v3.4: 還原 debt 進入 EV)
        true_ev = mcap + debt_total - cash_total
        # 防呆：負 EV 用 mcap 的 10% 當地板，避免淨現金公司因 cash 太多而 EV 變負
        true_ev = max(true_ev, mcap * 0.10)
        ev_sales = true_ev / total_revenue if total_revenue > 0 else 0.0
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
 
        # v3.4 修正：跑道分母加入到期短債 (debt wall 一次性吸乾流動性)
        runway_yrs = 99.0
        if real_fcf < 0 and cash_total > 0:
            annual_outflow = abs(real_fcf) + debt_st  # 年化燒錢 + 一年內到期短債
            runway_yrs = cash_total / annual_outflow if annual_outflow > 0 else 99.0
            if runway_yrs < CASH_RUNWAY_MIN_YRS:
                return {"Ticker": ticker, "Status": f"Fail: 現金跑道不足 ({runway_yrs:.1f}年, 含短債{debt_st:.2f}B)"}
 
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
            'Total_Debt(B)':         round(debt_total, 3),  # v3.4
            'ShortTerm_Debt(B)':     round(debt_st, 3),     # v3.4
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
    msg['Subject'] = f"[🚀 成長衛星 v3.4.1] Alpha 報表 - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    msg['From'], msg['To'] = sender_email, receiver_email
 
    content = (
        "總工程師您好：\n\n【成長衛星 v3.4.1 IndexError 補丁版掃描完成】\n"
        f"核心：真實 Rule40>{RULE_OF_40_MIN}、營收增長>{REV_GROWTH_MIN}%、絕對動能>{MOMENTUM_MIN}%\n"
        f"v3.4 修復：EV 補回 debt、跑道納入短債、worker 線程零 yf 呼叫\n"
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
                           maintype='text', subtype='csv', filename=f'Growth_v3_4_1_{datetime.now().strftime("%Y%m%d")}.csv')
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
    print("\n>>> 點火啟動：成長衛星管線 v3.4.1 (IndexError 補丁) <<<\n")
    try:
        if not os.path.exists(GROWTH_CACHE_FILE):
            logger.error(f"找不到 {GROWTH_CACHE_FILE}！"); exit(1)
            
        df_c = pd.read_csv(GROWTH_CACHE_FILE)
        df_c['CIK'] = df_c['CIK'].astype(str).str.zfill(10)
        growth_universe = dict(zip(df_c['Ticker'], df_c['CIK']))
        
        # ── 一次性批量下載所有資料 (取代原本在迴圈中的單筆請求) ──
        all_tickers = list(growth_universe.keys()) + ['SPY']
        pre_fetch_all_market_data(all_tickers)
        
        # v3.4 關鍵：主線程批量抓 info 一次，worker 線程不再碰 yf.Ticker
        pre_fetch_all_info(list(growth_universe.keys()))
        
        # 從快取提取 SPY 供相對動能運算使用
        spy_close = get_cached_series('SPY', 'Close')
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            res = list(executor.map(lambda p: run_growth_satellite_pipeline(p[0], str(p[1]), USER_EMAIL, spy_close), growth_universe.items()))
            
        # ==========================================
        # 死因統計 (證明程式有認真在跑)
        # ==========================================
        fail_stats = {}
        for r in res:
            status = r.get('Status', 'Unknown')
            if status != 'Pass':
                if ':' in status:
                    # 只抓取大分類，去掉括號裡的具體數字
                    reason = status.split(':')[1].strip().split('(')[0].strip()
                    key = f"Fail: {reason}"
                else:
                    key = status
                fail_stats[key] = fail_stats.get(key, 0) + 1
        
        if fail_stats:
            logger.info("💀 淘汰原因分布：")
            for k, v in sorted(fail_stats.items(), key=lambda x: -x[1]):
                logger.info(f"  {k}: {v} 檔")
        # ==========================================
            
        final_df = calculate_growth_alpha(res)
        send_email_report(final_df, USER_EMAIL)
        
        if not final_df.empty:
            print(f"\n>>> 共 {len(final_df)} 檔通關，TOP 15：\n")
            print(final_df.head(15).to_string(index=False))
        else: print("\n>>> 今日無標的通關。")
    except Exception as e: 
        logger.critical(f"崩潰: {e}")
        traceback.print_exc()
 
