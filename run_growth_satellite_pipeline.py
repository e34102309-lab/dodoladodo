"""
=============================================================================
GROWTH SATELLITE PIPELINE v3.6 (遠期折現與防禦版)
=============================================================================
v3.6 改動摘要 (vs v3.5)：
1. ★[估值淨化] 徹底廢除 EV/Sales，導入「遠期實質 FCF Yield」進行折現檢驗。
               若樂觀預期 3 年後的實質殖利率仍打不過無風險利率 (4.5%)，強制淘汰。
2. ★[防禦紅線] 實裝利息保障倍數 (ICR) 檢驗，低於 3.0 的舉債燒錢機強制出局。
3. ★[訊號校正] Alpha Score 權重向 Forward Yield 傾斜 (佔 40%)，尋求具備安全邊際的成長。
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

# === 成長衛星專屬門檻 ===
MIN_LIQUIDITY_USD       = 10_000_000   
REV_GROWTH_MIN          = 12.0         
GROSS_MARGIN_MIN        = 0.35         
RULE_OF_40_MIN          = 25.0         
MOMENTUM_MIN            = -5.0         
RELATIVE_MOMENTUM_MIN   = -10.0        
FCF_BURN_FLOOR          = -40.0        
DILUTION_MAX_YOY        = 0.10         
PCT_FROM_52W_HIGH_MIN   = -0.45        
GROSS_MARGIN_TREND_MIN  = -0.03        

MIN_MARKET_CAP_B        = 0.5          
WINSORIZE_PCT           = 0.025

# ★ [v3.6 新增] 遠期折現參數
RISK_FREE_RATE          = 4.5          # 無風險利率底線 (%)
PROJECTION_YEARS        = 3            # 遠期折現年限
ICR_MIN                 = 3.0          # 破產防線

# ==============================================================================
# 批量快取系統 (取代單檔抓取)
# ==============================================================================
_BULK_MARKET_DATA: Optional[pd.DataFrame] = None

def pre_fetch_all_market_data(tickers: List[str]):
    global _BULK_MARKET_DATA
    logger.info(f"開始一次性批量下載 {len(tickers)} 檔報價資料...對 Yahoo 只算 1 次請求！")
    _BULK_MARKET_DATA = yf.download(tickers, period='3y', progress=False, auto_adjust=True)
    logger.info("批量下載完成！")

def get_cached_series(ticker: str, col: str) -> Optional[pd.Series]:
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

_INFO_CACHE: Dict[str, dict] = {}

def pre_fetch_all_info(tickers: List[str]):
    global _INFO_CACHE
    logger.info(f"主線程批量抓取 {len(tickers)} 檔 yf.info...")
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

def safe_yf_info(ticker: str) -> dict:
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

    def get(self, url: str, headers: dict) -> Optional[requests.Response]:
        with self.lock:
            now = time.time()
            self.timestamps = [t for t in self.timestamps if now - t < self.period]
            if len(self.timestamps) >= self.calls:
                sleep_time = max(0, self.period - (now - self.timestamps[0]))
                time.sleep(sleep_time)
            self.timestamps.append(time.time())
        for attempt in range(3):
            try:
                resp = self.session.get(url, headers=headers, timeout=10)
                if resp.status_code == 200:
                    return resp
                elif resp.status_code in (429, 503):
                    time.sleep((2 ** attempt) * 1.5)
            except requests.RequestException:
                time.sleep(2)
        return None

_GLOBAL_SEC_SESSION = RateLimitedSession()

class SECDataDistiller:
    def __init__(self, email: str):
        self.headers = {'User-Agent': f'QuantResearchProject {email}'}
        self.session = _GLOBAL_SEC_SESSION
        # ★ [v3.6] 擴充抓取 EBIT 與 Interest
        self.config = {
            'OCF':         ['NetCashProvidedByUsedInOperatingActivities','NetCashProvidedByUsedInOperatingActivitiesContinuingOperations'],
            'CapEx':       ['PaymentsToAcquirePropertyPlantAndEquipment','PropertyPlantAndEquipmentAdditions'],
            'SBC':         ['ShareBasedCompensation', 'StockBasedCompensation','AllocatedShareBasedCompensationExpense','ShareBasedCompensationExpense'],
            'DnA':         ['DepreciationDepletionAndAmortization','DepreciationAndAmortization'],
            'EBIT':        ['OperatingIncomeLoss'], 
            'Interest':    ['InterestExpense', 'InterestExpenseDebt'], 
            'RND':         ['ResearchAndDevelopmentExpense'],
            'Revenue':     ['Revenues','RevenueFromContractWithCustomerExcludingAssessedTax','SalesRevenueNet','SalesRevenueGoodsNet'],
            'GrossProfit': ['GrossProfit', 'GrossMargin'],
            'Cash':        ['CashAndCashEquivalentsAtCarryingValue','CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'],
            'ShortTermInvestments': ['ShortTermInvestments','MarketableSecuritiesCurrent'],
            'Debt':        ['LongTermDebt', 'LongTermDebtNoncurrent','LongTermDebtAndCapitalLeaseObligations'],
            'ShortTermDebt':['DebtCurrent', 'LongTermDebtCurrent','ShortTermBorrowings', 'CommercialPaper'],
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
                        df = df.drop_duplicates(subset=['end'], keep='last').reset_index(drop=True)
                        return df
                except Exception:
                    pass
        return pd.DataFrame()

    def fetch_shares_outstanding(self, cik: str) -> pd.DataFrame:
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
        if df.empty: return 0.0
        annual = df[df['form'].astype(str).str.startswith('10-K')]
        if annual.empty: return float(df['val'].iloc[-1]) / 1e9
        return float(annual['val'].iloc[-1]) / 1e9

    def get_annual_history(self, df: pd.DataFrame, n_years: int = 3) -> List[float]:
        if df.empty: return []
        annual = df[df['form'].astype(str).str.startswith('10-K')]
        if annual.empty: return []
        if 'fy' in annual.columns:
            annual = annual.sort_values('end')
        vals = annual['val'].iloc[-n_years:].tolist()
        return [float(v) / 1e9 for v in vals]

    def get_latest_shares(self, df: pd.DataFrame) -> Tuple[float, float]:
        if df.empty or len(df) < 2: return 0.0, 0.0
        latest_date = df['end'].iloc[-1]
        one_year_ago = latest_date - pd.Timedelta(days=365)
        df_old = df[df['end'] <= one_year_ago]
        if df_old.empty: return float(df['val'].iloc[-1]) / 1e9, 0.0
        return float(df['val'].iloc[-1]) / 1e9, float(df_old['val'].iloc[-1]) / 1e9

# ==============================================================================
# 核心管線：成長引擎檢驗 (V3.6 遠期折現與防禦版)
# ==============================================================================
def run_growth_satellite_pipeline(ticker: str, cik: str, email: str,
                                   spy_close: Optional[pd.Series] = None) -> dict:
    try:
        # ── Stage 0: 即排型過濾 ──
        if '-' in ticker or '.' in ticker: return {"Ticker": ticker, "Status": "Fail: 排除特別股/ADR"}

        # ── Stage 1: 價格動能與環境校正 ──
        pm = fetch_price_metrics(ticker, spy_close)
        if pm is None: return {"Ticker": ticker, "Status": "Fail: 無價格資料"}
        if pm['dollar_volume'] < MIN_LIQUIDITY_USD: return {"Ticker": ticker, "Status": f"Fail: 流動性不足 (${pm['dollar_volume']/1e6:.1f}M)"}

        mom_12m, rel_mom, pct_from_high, last_close = pm['momentum'], pm['rel_momentum'], pm['pct_from_52w_high'], pm['last_close']
        if mom_12m is None: return {"Ticker": ticker, "Status": "Fail: 動能無資料"}

        spy_mom = (mom_12m - rel_mom) if rel_mom is not None else 0.0
        dynamic_mom_min = min(MOMENTUM_MIN, spy_mom * 1.5) 
        if mom_12m < dynamic_mom_min: return {"Ticker": ticker, "Status": f"Fail: 絕對動能不足 ({mom_12m:.1f}%)"}
            
        strict_rel_mom_min = RELATIVE_MOMENTUM_MIN if spy_mom > 0 else -5.0
        if rel_mom is not None and rel_mom < strict_rel_mom_min: return {"Ticker": ticker, "Status": f"Fail: 相對動能輸大盤 ({rel_mom:+.1f}%)"}
        if pct_from_high < PCT_FROM_52W_HIGH_MIN: return {"Ticker": ticker, "Status": f"Fail: 距高點過遠 ({pct_from_high*100:+.1f}%)"}

        # ── Stage 2: 基礎資料獲取 ──
        info = safe_yf_info(ticker)
        price = float(info.get('currentPrice') or info.get('regularMarketPrice') or last_close)
        if price == 0.0: return {"Ticker": ticker, "Status": "Fail: 價格獲取失敗"}

        sec = SECDataDistiller(email)
        df_rev   = sec.fetch_concept(cik, 'Revenue')
        df_gross = sec.fetch_concept(cik, 'GrossProfit')
        df_ocf   = sec.fetch_concept(cik, 'OCF')
        df_capex = sec.fetch_concept(cik, 'CapEx')
        df_sbc   = sec.fetch_concept(cik, 'SBC')
        df_dna   = sec.fetch_concept(cik, 'DnA')
        df_ebit  = sec.fetch_concept(cik, 'EBIT')
        df_int   = sec.fetch_concept(cik, 'Interest')
        df_cash  = sec.fetch_concept(cik, 'Cash')
        df_sti   = sec.fetch_concept(cik, 'ShortTermInvestments')
        df_debt  = sec.fetch_concept(cik, 'Debt')
        df_std   = sec.fetch_concept(cik, 'ShortTermDebt')
        df_shares = sec.fetch_shares_outstanding(cik)

        if df_rev.empty or df_gross.empty or df_ocf.empty: return {"Ticker": ticker, "Status": "Fail: SEC 核心資料缺失"}

        rev_history, gross_history = sec.get_annual_history(df_rev, 3), sec.get_annual_history(df_gross, 3)
        if not rev_history or not gross_history: return {"Ticker": ticker, "Status": "Fail: 無年度數據"}
        
        ocf, capex, sbc, dna = sec.get_latest_annual(df_ocf), abs(sec.get_latest_annual(df_capex)), abs(sec.get_latest_annual(df_sbc)), abs(sec.get_latest_annual(df_dna))
        ebit, interest = sec.get_latest_annual(df_ebit), abs(sec.get_latest_annual(df_int))
        cash_total = sec.get_latest_annual(df_cash) + sec.get_latest_annual(df_sti)
        debt_total = abs(sec.get_latest_annual(df_debt)) + abs(sec.get_latest_annual(df_std))

        shares_now, shares_old = sec.get_latest_shares(df_shares)
        if shares_now == 0: shares_now = float(info.get('sharesOutstanding', 0))
        mcap = (price * shares_now) / 1e9 if shares_now > 0 else float(info.get('marketCap', 0)) / 1e9
        if mcap < MIN_MARKET_CAP_B: return {"Ticker": ticker, "Status": f"Fail: 市值過小 ({mcap:.2f}B)"}

        # ── Stage 3: 第一原理防線 (ICR 與 實質現金流) ──
        total_revenue = rev_history[-1]
        rev_growth_pct = ((rev_history[-1] / rev_history[-2] - 1) * 100) if len(rev_history) >= 2 and rev_history[-2] > 0 else 0.0
        rev_acceleration = (rev_growth_pct - ((rev_history[-2] / rev_history[-3] - 1) * 100)) if len(rev_history) >= 3 and rev_history[-3] > 0 else None

        true_ev = max(mcap + debt_total - cash_total, mcap * 0.10)
        
        gross_margin = gross_history[-1] / rev_history[-1] if rev_history[-1] > 0 else 0.0
        gm_trend = gross_margin - (gross_history[0]/rev_history[0]) if len(gross_history) >= 2 and len(rev_history) >= 2 and rev_history[0] > 0 else 0.0

        maint_capex = min(dna, capex) if dna > 0 and capex > 0 else max(dna, capex)
        real_fcf = ocf - maint_capex - sbc
        real_fcf_margin = (real_fcf / total_revenue) * 100 if total_revenue > 0 else -100.0
        real_rule_of_40 = real_fcf_margin + rev_growth_pct

        # ★ [防禦] 實裝 ICR 
        icr = ebit / interest if interest > 1e-6 else 999.0
        if debt_total > 0 and icr < ICR_MIN: return {"Ticker": ticker, "Status": f"Fail: 破產風險, ICR低於3 ({icr:.1f}x)"}

        if real_fcf_margin < FCF_BURN_FLOOR: return {"Ticker": ticker, "Status": f"Fail: 燒錢過深 ({real_fcf_margin:.1f}%)"}
        if shares_old > 0 and ((shares_now / shares_old) - 1) > DILUTION_MAX_YOY: return {"Ticker": ticker, "Status": f"Fail: 股本稀釋過快"}
        if gm_trend < GROSS_MARGIN_TREND_MIN: return {"Ticker": ticker, "Status": f"Fail: 毛利率衰退"}
        if rev_growth_pct < REV_GROWTH_MIN: return {"Ticker": ticker, "Status": f"Fail: 營收失速 ({rev_growth_pct:.1f}%)"}
        if gross_margin < GROSS_MARGIN_MIN: return {"Ticker": ticker, "Status": f"Fail: 無定價權毛利 ({gross_margin*100:.1f}%)"}
        if real_rule_of_40 < RULE_OF_40_MIN: return {"Ticker": ticker, "Status": f"Fail: 真實Rule40破功 ({real_rule_of_40:.1f})"}

        # ── Stage 4: 遠期折現檢驗 (Forward Yield Engine) ──
        # 給予樂觀預期：假設能維持當前營收增速 (或至少 10%) 成長 3 年
        optimistic_growth = max(rev_growth_pct, 10.0) / 100.0 
        future_real_fcf = real_fcf * ((1 + optimistic_growth) ** PROJECTION_YEARS) if real_fcf > 0 else real_fcf
        
        if future_real_fcf <= 0:
            return {"Ticker": ticker, "Status": f"Fail: 深度燒錢，三年後實質FCF仍為負"}
            
        forward_yield = (future_real_fcf / true_ev) * 100
        
        if forward_yield < RISK_FREE_RATE:
            return {"Ticker": ticker, "Status": f"Fail: 估值透支，遠期殖利率僅 {forward_yield:.1f}%"}

        exit_signal = "🚀 成長超新星 (遠期高回報)"
        accel_tag = " 📈加速" if rev_acceleration and rev_acceleration > 5 else (" 📉減速" if rev_acceleration and rev_acceleration < -10 else "")

        return {
            'Ticker': ticker, 'Status': 'Pass', 'Price': round(price, 2),
            'CIK': str(cik),
            'Rev_Growth(%)':         round(rev_growth_pct, 2),
            'Real_R40':              round(real_rule_of_40, 2),
            'Real_FCF_Margin(%)':    round(real_fcf_margin, 2),
            'Forward_Yield(%)':      round(forward_yield, 2), # 替換掉 EV/Sales
            'ICR(x)':                round(icr, 1) if icr < 999 else "Safe",
            'Gross_Margin(%)':       round(gross_margin * 100, 2),
            'Momentum(%)':           round(mom_12m, 2),
            'Rel_Momentum(%)':       round(rel_mom, 2) if rel_mom is not None else None,
            'Pct_From_52W_High(%)':  round(pct_from_high * 100, 2),
            'Exit_Signal':           exit_signal + accel_tag,
        }
    except Exception as e:
        logger.error(f"[{ticker}] 成長管線崩潰: {str(e)[:80]}")
        return {"Ticker": ticker, "Status": "Error"}

# ==============================================================================
# Alpha 排序 (向 Forward Yield 傾斜)
# ==============================================================================
def calculate_growth_alpha(results: List[dict]) -> pd.DataFrame:
    df = pd.DataFrame([r for r in results if r.get('Status') == 'Pass'])
    if len(df) < 2: return df
    if 'CIK' in df.columns: df = df.drop_duplicates(subset=['CIK'], keep='first').reset_index(drop=True)

    df['_R40_w']    = robust_zscore(df['Real_R40'])
    df['_MOM_w']    = robust_zscore(df['Momentum(%)'])
    df['_FY_w']     = robust_zscore(df['Forward_Yield(%)'])
    df['_REV_w']    = robust_zscore(df['Rev_Growth(%)'])

    # ★ [v3.6] 權重分配：遠期殖利率(40%) + 真實R40(30%) + 成長(15%) + 動能(15%)
    df['Alpha_Score'] = (
        df['_FY_w'] * 0.40 + df['_R40_w'] * 0.30 + 
        df['_REV_w'] * 0.15 + df['_MOM_w'] * 0.15
    ).round(3)

    df.loc[df['Exit_Signal'].str.contains('減速'), 'Alpha_Score'] -= 0.20
    
    return df.drop(columns=[c for c in df.columns if c.startswith('_')]).sort_values('Alpha_Score', ascending=False).reset_index(drop=True)

# ==============================================================================
# 報表發送
# ==============================================================================
def send_email_report(df: pd.DataFrame, receiver_email: str):
    sender_email, sender_pwd = os.environ.get('EMAIL_SENDER'), os.environ.get('EMAIL_PASSWORD')
    if not sender_email or not sender_pwd: return
    msg = EmailMessage()
    msg['Subject'] = f"[🚀 成長衛星 v3.6] Alpha 報表 - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    msg['From'], msg['To'] = sender_email, receiver_email

    content = (
        "總工程師您好：\n\n【成長衛星 v3.6 遠期折現與防禦版 掃描完成】\n"
        f"核心：廢除 EV/Sales，強制要求 3 年 Forward Yield > {RISK_FREE_RATE}% 且 ICR > {ICR_MIN}\n"
        + "-" * 70 + "\n\n"
    )
    if df.empty: content += "今日無成長標的通關。市場估值可能已透支未來執行力。"
    else:
        content += f"共計 {len(df)} 檔通關。\n\n【TOP 10 具安全邊際之超新星】\n"
        cols = ['Ticker', 'Rev_Growth(%)', 'Real_R40', 'Forward_Yield(%)', 'ICR(x)', 'Momentum(%)', 'Alpha_Score', 'Exit_Signal']
        content += df.head(10)[[c for c in cols if c in df.columns]].to_string(index=False)
    msg.set_content(content)
    if not df.empty:
        msg.add_attachment(df.to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig'), 
                           maintype='text', subtype='csv', filename=f'Growth_v3_6_{datetime.now().strftime("%Y%m%d")}.csv')
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
    print("\n>>> 點火啟動：成長衛星管線 v3.6 (遠期折現與防禦版) <<<\n")
    try:
        if not os.path.exists(GROWTH_CACHE_FILE):
            logger.error(f"找不到 {GROWTH_CACHE_FILE}！"); exit(1)
            
        df_c = pd.read_csv(GROWTH_CACHE_FILE)
        df_c['CIK'] = df_c['CIK'].astype(str).str.zfill(10)
        growth_universe = dict(zip(df_c['Ticker'], df_c['CIK']))
        
        logger.info("獨立獲取 SPY 大盤基準...")
        spy_df = yf.download('SPY', period='3y', progress=False, auto_adjust=True)
        
        if isinstance(spy_df.columns, pd.MultiIndex):
            spy_close = spy_df['Close']['SPY'] if 'SPY' in spy_df['Close'] else None
        else:
            spy_close = spy_df['Close'] if 'Close' in spy_df.columns else None

        if spy_close is None or len(spy_close) < 13:
            logger.error("💀 致命錯誤：SPY 大盤基準獲取失敗！動態防線將全面失效退回預設值！")
        else:
            logger.info(f"✅ SPY 基準獲取成功，資料長度: {len(spy_close)} 筆")

        all_tickers = list(growth_universe.keys())
        pre_fetch_all_market_data(all_tickers)
        pre_fetch_all_info(all_tickers)
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            res = list(executor.map(lambda p: run_growth_satellite_pipeline(p[0], str(p[1]), USER_EMAIL, spy_close), growth_universe.items()))
            
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
            logger.info("💀 淘汰原因分布：")
            for k, v in sorted(fail_stats.items(), key=lambda x: -x[1]):
                logger.info(f"  {k}: {v} 檔")
            
        final_df = calculate_growth_alpha(res)
        send_email_report(final_df, USER_EMAIL)
        
        if not final_df.empty:
            print(f"\n>>> 共 {len(final_df)} 檔通關，TOP 15：\n")
            cols = ['Ticker', 'Rev_Growth(%)', 'Real_R40', 'Forward_Yield(%)', 'ICR(x)', 'Momentum(%)', 'Alpha_Score', 'Exit_Signal']
            print(final_df.head(15)[[c for c in cols if c in final_df.columns]].to_string(index=False))
        else: print("\n>>> 今日無標的通關。市場估值可能已透支未來執行力。")
    except Exception as e: 
        logger.critical(f"崩潰: {e}")
        traceback.print_exc()
