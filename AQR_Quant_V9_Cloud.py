"""
=============================================================================
V10.3 QUALITY PERSISTENCE EDITION - 在 V10.2 基礎上加入品質持續性
=============================================================================
V10.2 已修正：硬截頂、Robust Z、CIK 去重、流動性過濾、週期頂警告

V10.3 新增：
9. 3 年 ROIC 平均 + 最低值（過濾單年僥倖標的）
10. Buyback Yield + Total Shareholder Yield（股東回報質量）
11. 估值絕對防線：FCF Yield > Rf + 200bp（避免溢酬太薄）
12. 毛利率波動度（過濾極端週期股漏網）
13. Alpha 公式加入 Z_Persistence 新維度
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
from scipy.stats import zscore
import yfinance as yf
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import logging
import concurrent.futures
import threading
import traceback

# ==============================================================================
# 設定日誌
# ==============================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)

# ==============================================================================
# 全域快取與巨集參數
# ==============================================================================
_RF_CACHE: Optional[float] = None
MARKET_RISK_PREMIUM = 0.046

# === V10.2 門檻 ===
ROIC_THRESHOLD = 12.0          # 從 10 提高到 12
ICR_THRESHOLD = 5.0
EBIT_MARGIN_THRESHOLD = 0.05   # 新增：EBIT margin 至少 5%
MIN_LIQUIDITY_USD = 5_000_000  # 新增：日均成交額至少 $5M
WINSORIZE_PCT = 0.025          # 雙尾各 winsorize 2.5%

# === V10.3 新增門檻 ===
ROIC_3Y_AVG_MIN = 12.0         # 3 年平均 ROIC
ROIC_3Y_MIN_FLOOR = 8.0        # 3 年最低 ROIC（過濾單年僥倖）
FCF_YIELD_PREMIUM_BP = 200     # FCF Yield 須超過 Rf 200bp
GROSS_MARGIN_VOL_MAX = 0.10    # 3 年毛利波動度上限 10pp（過濾極端週期）

def get_risk_free_rate() -> float:
    global _RF_CACHE
    if _RF_CACHE is not None: return _RF_CACHE
    try:
        hist = yf.Ticker('^TNX').history(period='5d')
        if not hist.empty:
            _RF_CACHE = float(hist['Close'].iloc[-1]) / 100
            return _RF_CACHE
    except Exception as e:
        logger.warning(f"無風險利率獲取失敗，使用預設值: {e}")
    _RF_CACHE = 0.0435
    return _RF_CACHE

def robust_zscore(series: pd.Series) -> pd.Series:
    """
    抗極端值的 Robust Z-score: (x - median) / (1.4826 * MAD)
    1.4826 是讓 MAD 在常態分布下與 std 一致的轉換常數
    """
    s = pd.to_numeric(series, errors='coerce').fillna(0.0)
    if len(s) < 2:
        return pd.Series(np.zeros(len(s)), index=s.index)
    med = s.median()
    mad = (s - med).abs().median()
    if mad < 1e-9:
        # 退回標準 Z (但用 winsorize 後的 std)
        s_w = s.clip(lower=s.quantile(WINSORIZE_PCT), upper=s.quantile(1-WINSORIZE_PCT))
        std = s_w.std(ddof=1)
        if std < 1e-9: return pd.Series(np.zeros(len(s)), index=s.index)
        return pd.Series((s_w - s_w.mean()) / std, index=s.index)
    return pd.Series((s - med) / (1.4826 * mad), index=s.index).clip(-3.5, 3.5)

def flatten_close(hist: pd.DataFrame, ticker: str) -> Optional[pd.Series]:
    if hist.empty: return None
    try:
        if isinstance(hist.columns, pd.MultiIndex):
            if ('Close', ticker) in hist.columns: return hist[('Close', ticker)]
            cols = [c for c in hist.columns if c[0] == 'Close']
            return hist[cols[0]] if cols else None
        return hist['Close'] if 'Close' in hist.columns else None
    except Exception:
        return None

def check_global_trend() -> str:
    trend_msg = ""
    try:
        for idx in ['SPY', 'QQQ']:
            data = yf.download(idx, period='1y', progress=False)
            if not data.empty and len(data) > 200:
                close = flatten_close(data, idx)
                if close is not None:
                    sma_200 = close.rolling(window=200).mean()
                    last_close = float(close.iloc[-1])
                    last_sma = float(sma_200.iloc[-1])
                    recent_closes = close.iloc[-5:]
                    recent_smas = sma_200.iloc[-5:]
                    below_sma_week = all(float(c) < float(s) for c, s in zip(recent_closes, recent_smas))
                    diff_pct = (last_close / last_sma - 1) * 100
                    status = "🔴 跌破 200SMA (進入冰河保護期)" if below_sma_week else "🟢 穩態多頭"
                    trend_msg += f"[{idx}] 收盤: {last_close:.2f} | 200SMA: {last_sma:.2f} ({diff_pct:+.2f}%) -> {status}\n"
    except Exception as e:
        logger.error(f"大氣壓力感測器異常: {e}")
        trend_msg += f"趨勢感測器異常: {e}\n"
    return trend_msg if trend_msg else "大氣壓力感測器離線。\n"

# ==============================================================================
# YFinance 終極安全包裝器
# ==============================================================================
def safe_yf_info(ticker: str) -> dict:
    time.sleep(random.uniform(0.1, 0.4))
    stock = yf.Ticker(ticker)
    for _ in range(3):
        try:
            info = stock.info
            if info and 'symbol' in info: return info
        except Exception:
            time.sleep(1.0)
    return {}

def calculate_dynamic_beta(ticker: str) -> float:
    try:
        end = datetime.now()
        start = end - timedelta(days=365 * 3)
        data = yf.download([ticker, 'SPY'], start=start, end=end, interval='1wk', progress=False, auto_adjust=True)
        if data.empty: return 1.0
        close_df = data['Close']
        if ticker not in close_df.columns or 'SPY' not in close_df.columns: return 1.0
        returns = close_df.pct_change().dropna()
        if len(returns) < 50: return 1.0
        var_market = returns['SPY'].var()
        if var_market == 0: return 1.0
        return float(np.clip(returns.cov().loc[ticker, 'SPY'] / var_market, 0.5, 2.5))
    except Exception:
        return 1.0

def calculate_dynamic_wacc(ticker: str, debt: float, cash: float, 
                           market_cap: float, book_equity: float, tax_rate: float) -> float:
    rf = get_risk_free_rate()
    beta_u = calculate_dynamic_beta(ticker)
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

# ==============================================================================
# 流動性檢查 (V10.2 新增)
# ==============================================================================
def check_liquidity(ticker: str) -> Tuple[bool, float]:
    """檢查 30 日平均成交額，過濾低流動性股票"""
    try:
        hist = yf.download(ticker, period='2mo', progress=False, auto_adjust=False)
        if hist.empty or len(hist) < 20: return False, 0.0
        close = flatten_close(hist, ticker)
        # 處理 Volume 多重索引
        if isinstance(hist.columns, pd.MultiIndex):
            vol_cols = [c for c in hist.columns if c[0] == 'Volume']
            volume = hist[vol_cols[0]] if vol_cols else None
        else:
            volume = hist.get('Volume')
        if close is None or volume is None: return False, 0.0
        dollar_volume = (close * volume).tail(30).mean()
        return float(dollar_volume) >= MIN_LIQUIDITY_USD, float(dollar_volume)
    except Exception:
        return False, 0.0

# ==============================================================================
# SEC 原生爬蟲
# ==============================================================================
class RateLimitedSession:
    def __init__(self, calls=9, period=1.0):
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
                if resp.status_code == 200: return resp
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
            'OCF': ['NetCashProvidedByUsedInOperatingActivities'],
            'CapEx': ['PaymentsToAcquirePropertyPlantAndEquipment', 'PropertyPlantAndEquipmentAdditions'],
            'SBC': ['ShareBasedCompensation', 'StockBasedCompensation', 'AllocatedShareBasedCompensationExpense', 'ShareBasedCompensationExpense', 'AdjustmentForAmortization'],
            'EBIT': ['OperatingIncomeLoss', 'IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest'],
            'Interest': ['InterestExpense', 'InterestExpenseDebt', 'InterestExpenseNet', 'InterestAndDebtExpense', 'InvestmentIncomeInterest'],
            'DnA': ['DepreciationDepletionAndAmortization', 'DepreciationAndAmortization'],
            'Debt': ['LongTermDebt', 'LongTermDebtAndCapitalLeaseObligations', 'DebtCurrent'],
            'Cash': ['CashAndCashEquivalentsAtCarryingValue'],
            'Equity': ['StockholdersEquity'],
            'RND': ['ResearchAndDevelopmentExpense'],
            'DefRev': ['DeferredRevenue', 'ContractWithCustomerLiability'],
            'FinRec': ['FinancingReceivableNet', 'NotesAndLoansReceivableNet', 'LoansAndLeasesReceivableNet'],
            'Revenue': ['Revenues', 'RevenueFromContractWithCustomerExcludingAssessedTax', 'SalesRevenueNet'],
            # === V10.3 新增：股東回報追蹤 ===
            'Buyback': ['PaymentsForRepurchaseOfCommonStock', 'PaymentsForRepurchaseOfEquity'],
            'Dividend': ['PaymentsOfDividendsCommonStock', 'PaymentsOfDividends'],
            'StockIssuance': ['ProceedsFromIssuanceOfCommonStock', 'StockIssuedDuringPeriodValueNewIssues'],
            'GrossProfit': ['GrossProfit'],  # 新增：毛利波動度檢查
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
                        df = df.sort_values('end').drop_duplicates(subset=['end', 'form'], keep='last')
                        return df
                except Exception: pass
        return pd.DataFrame()

    def get_latest_annual(self, df: pd.DataFrame) -> float:
        if df.empty: return 0.0
        annual = df[df['form'] == '10-K']
        return float(annual['val'].iloc[-1]) / 1e9 if not annual.empty else 0.0

    def get_yoy_change(self, df: pd.DataFrame) -> float:
        if df.empty: return 0.0
        annual = df[df['form'] == '10-K']
        if len(annual) < 2: return 0.0
        return (float(annual['val'].iloc[-1]) - float(annual['val'].iloc[-2])) / 1e9

    def get_revenue_3yr(self, df: pd.DataFrame) -> Tuple[float, float]:
        """回傳 (3年前營收, 最新營收)，用於計算 3 年 CAGR"""
        if df.empty: return 0.0, 0.0
        annual = df[df['form'] == '10-K']
        if len(annual) < 4: return 0.0, 0.0
        return float(annual['val'].iloc[-4]) / 1e9, float(annual['val'].iloc[-1]) / 1e9

    def get_annual_history(self, df: pd.DataFrame, n_years: int = 3) -> List[float]:
        """V10.3 新增：取最近 n 年 10-K 數據（單位 B），由舊到新排序"""
        if df.empty: return []
        annual = df[df['form'] == '10-K']
        if len(annual) < n_years: return []
        return [float(v) / 1e9 for v in annual['val'].iloc[-n_years:].tolist()]

    def get_n_year_sum(self, df: pd.DataFrame, n: int = 3) -> float:
        """V10.3 新增：n 年累計值（B）"""
        hist = self.get_annual_history(df, n)
        return sum(hist) if hist else 0.0

    def check_q_yoy_decline(self, df: pd.DataFrame) -> bool:
        if df.empty: return False
        q_df = df[df['form'] == '10-Q'].copy()
        if len(q_df) < 4: return False
        latest = q_df.iloc[-1]
        target_date = latest['end'] - pd.Timedelta(days=365)
        q_df['diff'] = abs((q_df['end'] - target_date).dt.days)
        match = q_df[q_df['diff'] < 30]
        return False if match.empty else (float(latest['val']) < float(match['val'].iloc[-1]) * 0.85)

# ==============================================================================
# 核心管線 V10.3 (品質持續性版)
# ==============================================================================
def run_v10_3_pipeline(ticker: str, cik: str, email: str) -> dict:
    try:
        # ── Stage 0.1: 實體錯位防線 ───────────────────────────────────────
        if '-' in ticker or '.' in ticker:
            return {"Ticker": ticker, "Status": "Fail: 排除特別股與多重股權"}
        
        stock = yf.Ticker(ticker)
        
        # ── Stage 0.2: 流動性絕對防線 (V10.2 新增) ────────────────────────
        liquid, daily_dollar_vol = check_liquidity(ticker)
        if not liquid:
            return {"Ticker": ticker, "Status": f"Fail: 流動性不足 (${daily_dollar_vol/1e6:.1f}M)"}
        
        # ── Stage 0: 價格與市值絕對防線 ──────────────────────────────────
        info = safe_yf_info(ticker)
        price = 0.0
        try: price = float(getattr(stock.fast_info, 'last_price', 0.0))
        except Exception: pass
        
        if price == 0.0: price = float(info.get('currentPrice', info.get('regularMarketPrice', 0.0)))
        if price == 0.0:
            try:
                hist = stock.history(period='1d')
                if not hist.empty: price = float(hist['Close'].iloc[-1])
            except Exception: pass

        shares_out = info.get('sharesOutstanding') or info.get('impliedSharesOutstanding', 0)
        mcap_fast = 0.0
        try: mcap_fast = float(getattr(stock.fast_info, 'market_cap', 0.0)) / 1e9
        except Exception: pass
        mcap = (price * shares_out) / 1e9 if shares_out > 0 else mcap_fast

        if mcap < 1.0 or price == 0.0: 
            return {"Ticker": ticker, "Status": "Fail: 市值或價格獲取失敗"}
        
        gross_margin = float(info.get('grossMargins') or 0.0)
        rev_growth = float(info.get('revenueGrowth') or 0.0)
        total_revenue = float(info.get('totalRevenue') or 0.0) / 1e9
        if total_revenue == 0.0:
            try:
                fins = stock.financials
                if not fins.empty and 'Total Revenue' in fins.index:
                    total_revenue = float(fins.loc['Total Revenue'].iloc[0]) / 1e9
            except Exception: pass

        # ── Stage 1: SEC XBRL 數據萃取 ───────────────────────────────────
        sec = SECDataDistiller(email)
        df_ocf = sec.fetch_concept(cik, 'OCF')
        df_capex = sec.fetch_concept(cik, 'CapEx')
        df_sbc = sec.fetch_concept(cik, 'SBC')
        df_ebit = sec.fetch_concept(cik, 'EBIT')
        df_int = sec.fetch_concept(cik, 'Interest')
        df_dna = sec.fetch_concept(cik, 'DnA')
        df_debt = sec.fetch_concept(cik, 'Debt')
        df_cash = sec.fetch_concept(cik, 'Cash')
        df_eq = sec.fetch_concept(cik, 'Equity')
        df_rnd = sec.fetch_concept(cik, 'RND')
        df_defrev = sec.fetch_concept(cik, 'DefRev')
        df_fin_rec = sec.fetch_concept(cik, 'FinRec')
        df_rev = sec.fetch_concept(cik, 'Revenue')
        # === V10.3 新增資料抓取 ===
        df_buyback = sec.fetch_concept(cik, 'Buyback')
        df_div = sec.fetch_concept(cik, 'Dividend')
        df_issuance = sec.fetch_concept(cik, 'StockIssuance')
        df_gross = sec.fetch_concept(cik, 'GrossProfit')

        ocf = sec.get_latest_annual(df_ocf)
        capex = abs(sec.get_latest_annual(df_capex))
        sbc = abs(sec.get_latest_annual(df_sbc))
        ebit = sec.get_latest_annual(df_ebit)
        dna = abs(sec.get_latest_annual(df_dna))
        debt = sec.get_latest_annual(df_debt)
        cash = sec.get_latest_annual(df_cash)
        equity = sec.get_latest_annual(df_eq)
        rnd = abs(sec.get_latest_annual(df_rnd))
        defrev_change = sec.get_yoy_change(df_defrev)
        fin_rec = sec.get_latest_annual(df_fin_rec)
        rev_3yr_ago, rev_latest = sec.get_revenue_3yr(df_rev)

        # === V10.3 新增：3 年歷史回溯 ===
        ebit_history = sec.get_annual_history(df_ebit, 3)      # [3年前, 2年前, 最新]
        equity_history = sec.get_annual_history(df_eq, 3)
        debt_history = sec.get_annual_history(df_debt, 3)
        rev_history = sec.get_annual_history(df_rev, 3)
        gross_history = sec.get_annual_history(df_gross, 3)
        
        # 3 年股東回報累計
        buyback_3y = sec.get_n_year_sum(df_buyback, 3)
        dividend_3y = sec.get_n_year_sum(df_div, 3)
        issuance_3y = sec.get_n_year_sum(df_issuance, 3)

        # 3 年 CAGR (V10.2 新增，用於週期頂判斷)
        rev_cagr_3y = 0.0
        if rev_3yr_ago > 0 and rev_latest > 0:
            rev_cagr_3y = ((rev_latest / rev_3yr_ago) ** (1/3) - 1) * 100

        # ── Stage 1.5: YF 數據填補 ───────────────────────────────────────
        if capex == 0: capex = abs(info.get('capitalExpenditures') or 0) / 1e9
        if sbc == 0: sbc = abs(info.get('shareBasedCompensation') or 0) / 1e9
        
        raw_int = sec.get_latest_annual(df_int)
        if raw_int == 0:
            try:
                fins = stock.financials
                if not fins.empty and 'Interest Expense' in fins.index:
                    raw_int = float(fins.loc['Interest Expense'].iloc[0]) / 1e9
            except Exception: pass
        interest = max(abs(raw_int), 0.05) if raw_int != 0 else 0.05

        # ── Stage 2: 物理限制器與核心運算 ────────────────────────────────
        adjusted_debt = max(0.0, debt - fin_rec)
        excess_cash = max(0.0, cash - (total_revenue * 0.02))
        net_debt = max(adjusted_debt - excess_cash, 0.0)
        true_ev = max(mcap + net_debt, mcap * 0.10)
        
        maint_capex = dna if dna > 0 else capex
        real_fcf = ocf - maint_capex - sbc
        
        if real_fcf <= 0:
            reason = "SBC吞噬" if sbc > ocf * 0.4 else "重資本耗損"
            return {"Ticker": ticker, "Status": f"Fail: 實質FCF為負 ({reason})"}
        
        fcf_yield = (real_fcf / true_ev) * 100 if true_ev > 0 else 0.0  # V10.2: 移除 50% 截頂
        
        tax_rate = float(np.clip(info.get('effectiveTaxRate') or 0.21, 0.1, 0.35))
        wacc = calculate_dynamic_wacc(ticker, adjusted_debt, cash, mcap, equity, tax_rate)
        
        adjusted_ebit = ebit 
        capitalized_rnd = rnd * 2.5
        ic_floor = max(true_ev * 0.10, total_revenue * 0.15)
        ic = max(adjusted_debt + max(equity, 0.0) + capitalized_rnd - excess_cash, ic_floor)
            
        roic = (adjusted_ebit / max(ic, 0.1)) * 100  # V10.2: 移除 100% 截頂，winsorize 後處理
        icr = adjusted_ebit / interest

        # EBIT margin 過濾 (V10.2 新增)
        ebit_margin = (adjusted_ebit / total_revenue) if total_revenue > 0 else 0.0
        
        if roic < ROIC_THRESHOLD: return {"Ticker": ticker, "Status": f"Fail: ROIC < {ROIC_THRESHOLD}"}
        if icr < ICR_THRESHOLD: return {"Ticker": ticker, "Status": "Fail: ICR < 5"}
        if ebit_margin < EBIT_MARGIN_THRESHOLD: return {"Ticker": ticker, "Status": f"Fail: EBIT margin < {EBIT_MARGIN_THRESHOLD*100:.0f}%"}

        # ── Stage 2.4: V10.3 新增 - 品質持續性檢查 ─────────────────────
        # 計算 3 年 ROIC（用簡化 IC = 債 + 股權）
        roic_history = []
        for i in range(min(3, len(ebit_history))):
            if i < len(equity_history) and i < len(debt_history):
                ic_i = max(equity_history[i] + debt_history[i], 0.5)
                roic_history.append((ebit_history[i] / ic_i) * 100)
        
        roic_3y_avg = float(np.mean(roic_history)) if roic_history else roic
        roic_3y_min = float(min(roic_history)) if roic_history else roic
        
        # 過濾「單年僥倖」的標的（CALM 雞蛋週期頂就會在這死掉）
        if len(roic_history) >= 3:
            if roic_3y_avg < ROIC_3Y_AVG_MIN:
                return {"Ticker": ticker, "Status": f"Fail: 3年平均ROIC<{ROIC_3Y_AVG_MIN} ({roic_3y_avg:.1f})"}
            if roic_3y_min < ROIC_3Y_MIN_FLOOR:
                return {"Ticker": ticker, "Status": f"Fail: 3年最低ROIC<{ROIC_3Y_MIN_FLOOR} ({roic_3y_min:.1f})"}

        # ── Stage 2.45: V10.3 新增 - 毛利率波動度 ─────────────────────
        gross_margin_vol = 0.0
        if len(gross_history) >= 3 and len(rev_history) >= 3:
            gm_series = [g/r for g, r in zip(gross_history, rev_history) if r > 0]
            if len(gm_series) >= 3:
                gross_margin_vol = float(np.std(gm_series, ddof=1))
                if gross_margin_vol > GROSS_MARGIN_VOL_MAX:
                    return {"Ticker": ticker, "Status": f"Fail: 毛利波動>{GROSS_MARGIN_VOL_MAX*100:.0f}pp ({gross_margin_vol*100:.1f}pp)"}

        # ── Stage 2.46: V10.3 新增 - 估值絕對防線 ─────────────────────
        rf_pct = get_risk_free_rate() * 100
        min_fcf_yield = rf_pct + (FCF_YIELD_PREMIUM_BP / 100)
        valuation_warning = fcf_yield < min_fcf_yield  # 標記但不 Fail（成長股例外）

        # ── Stage 2.47: V10.3 新增 - 股東回報計算 ─────────────────────
        # Buyback Yield = 3 年淨回購 / 當前市值
        net_buyback_3y = max(0.0, buyback_3y - issuance_3y)
        buyback_yield = (net_buyback_3y / 3 / mcap) * 100 if mcap > 0 else 0.0  # 年化
        dividend_yield_calc = (dividend_3y / 3 / mcap) * 100 if mcap > 0 else 0.0  # 年化
        total_shareholder_yield = buyback_yield + dividend_yield_calc

        # ── Stage 2.5: 週期頂警告 (V10.2 核心新增) ─────────────────────
        # 高 FCF Yield + 負成長 / 衰退中的 3年 CAGR = 危險的週期頂
        cycle_top_warning = False
        if fcf_yield > 12.0:  # 超過 12% 的 FCF Yield 在當前環境很罕見
            if rev_growth < 0 or rev_cagr_3y < 2.0:
                cycle_top_warning = True
        
        # ── Stage 2.6: 成長監測 ────────────────────────────────────────
        is_growth_monster = False
        if total_revenue > 0:
            billings_growth = rev_growth + (defrev_change / total_revenue)
            real_r40 = ((real_fcf / total_revenue) + billings_growth) * 100
            if gross_margin >= 0.75 and (roic - wacc*100) > 5.0 and real_r40 >= 40.0: 
                is_growth_monster = True

        # ── Stage 2.7: 回撤模型 ─────────────────────────────────────────
        ebitda = adjusted_ebit + dna + sbc
        if ebitda > 0:
            current_mult = true_ev / ebitda
            floor = min(20.0, 8.0 + ((gross_margin - 0.50) / 0.10) * 1.5) if gross_margin > 0.5 else 8.0
            stress_mult = min(current_mult, max(floor, current_mult * 0.60))
            drawdown = ((max(0.0, (ebitda * 0.70) * stress_mult - net_debt) - mcap) / mcap) * 100
            drawdown_risk = min(0.0, drawdown)
        else: drawdown_risk = -100.0

        if drawdown_risk < -70: return {"Ticker": ticker, "Status": f"Drop: 極限回撤({drawdown_risk:.1f}%)"}

        # ── Stage 2.8: 動能 (V10.2: 失敗直接 Fail) ─────────────────────
        mom_12m = None
        try:
            hist = yf.download(ticker, start=datetime.now()-timedelta(days=420), progress=False, auto_adjust=True)
            close = flatten_close(hist, ticker)
            if close is not None and len(close) > 200:
                m = close.resample('ME').last()
                if len(m) >= 13:
                    mom_12m = (float(m.iloc[-2]) / float(m.iloc[-13]) - 1) * 100  # V10.2: 移除 200% 截頂
        except Exception as e:
            logger.debug(f"[{ticker}] 動能計算異常: {e}")
        
        if mom_12m is None:
            return {"Ticker": ticker, "Status": "Fail: 動能資料不足"}  # V10.2: 不再給 0 混入排名

        # ── Stage 3: 決策訊號拼接 ──────────────────────────────────────
        exit_signal = "Hold ✅"
        if is_growth_monster:
            exit_signal = "🚀 成長旁通: Rule of 40 通行證 ✅"
            if sec.check_q_yoy_decline(df_ocf): 
                exit_signal += " | 🟡 預警: 成長股OCF衰退"
        else:
            if fcf_yield < (get_risk_free_rate()*100) and mom_12m < 0: 
                exit_signal = "🔴 停損: 溢酬消失"
            elif roic < wacc * 100: 
                exit_signal = "🔴 停損: 價值摧毀"
            
            if sec.check_q_yoy_decline(df_ocf): 
                if exit_signal == "Hold ✅":
                    exit_signal = "🟡 預警: OCF YoY衰退"
                else:
                    exit_signal += " | 🟡 OCF YoY衰退"
        
        # V10.2 新增：週期頂警告附加
        if cycle_top_warning:
            if exit_signal == "Hold ✅":
                exit_signal = "🟠 週期頂警告: 高FCF+低成長"
            else:
                exit_signal += " | 🟠 週期頂"
        
        # V10.3 新增：估值溢酬警告（非成長股才看）
        if valuation_warning and not is_growth_monster:
            if exit_signal == "Hold ✅":
                exit_signal = f"🟡 溢酬偏薄 (FCFY {fcf_yield:.1f}% < {min_fcf_yield:.1f}%)"
            elif "溢酬偏薄" not in exit_signal:
                exit_signal += " | 🟡 溢酬薄"

        return {
            'Ticker': ticker, 'Status': 'Pass', 'Price': round(price, 2),
            'CIK': str(cik),
            'WACC(%)': round(wacc * 100, 2), 
            'ROIC(%)': round(roic, 2),
            'ROIC_3Y_Avg(%)': round(roic_3y_avg, 2),     # V10.3 新增
            'ROIC_3Y_Min(%)': round(roic_3y_min, 2),     # V10.3 新增
            'EBIT_Margin(%)': round(ebit_margin * 100, 2),
            'GM_Vol(pp)': round(gross_margin_vol * 100, 2),  # V10.3 新增
            'FCF_Yield(%)': round(fcf_yield, 2), 
            'Real_FCF(B)': round(real_fcf, 3),
            'Buyback_Yield(%)': round(buyback_yield, 2),     # V10.3 新增
            'Dividend_Yield(%)': round(dividend_yield_calc, 2),  # V10.3 新增
            'Total_SH_Yield(%)': round(total_shareholder_yield, 2),  # V10.3 新增
            'Rev_CAGR_3Y(%)': round(rev_cagr_3y, 2),
            'Momentum(%)': round(mom_12m, 2), 
            'Max_Drawdown_Risk(%)': round(drawdown_risk, 1),
            'Liquidity($M)': round(daily_dollar_vol / 1e6, 1),
            'Exit_Signal': exit_signal
        }
    except Exception as e: 
        err_msg = str(e).split('\n')[0][:50]
        logger.error(f"[{ticker}] 管線崩潰: {err_msg}")
        return {"Ticker": ticker, "Status": "Error"}

# ==============================================================================
# Alpha 排序 (V10.2: Robust Z + Winsorize + CIK 去重)
# ==============================================================================
def winsorize_series(series: pd.Series, pct: float = WINSORIZE_PCT) -> pd.Series:
    """雙尾 winsorize，避免極端值污染分布"""
    s = pd.to_numeric(series, errors='coerce').fillna(0.0)
    lower, upper = s.quantile(pct), s.quantile(1 - pct)
    return s.clip(lower=lower, upper=upper)

def deduplicate_by_cik(df: pd.DataFrame) -> pd.DataFrame:
    """同 CIK 去重，保留流動性最高的那檔（避免 RUSHA/RUSHB、FOX/FOXA 雙計）"""
    if 'CIK' not in df.columns or 'Liquidity($M)' not in df.columns:
        return df
    df = df.sort_values('Liquidity($M)', ascending=False)
    return df.drop_duplicates(subset=['CIK'], keep='first').reset_index(drop=True)

def calculate_composite_alpha(results: List[dict]) -> pd.DataFrame:
    df = pd.DataFrame([r for r in results if r.get('Status') == 'Pass'])
    if len(df) < 2: return df
    
    # V10.2 Step 1: CIK 去重
    df = deduplicate_by_cik(df)
    
    # V10.2 Step 2: Winsorize 極端值（取代硬截頂）
    df['_ROIC_w'] = winsorize_series(df['ROIC(%)'])
    df['_FCF_w'] = winsorize_series(df['FCF_Yield(%)'])
    df['_MOM_w'] = winsorize_series(df['Momentum(%)'])
    df['_DD_w'] = winsorize_series(df['Max_Drawdown_Risk(%)'])
    # V10.3 新增 winsorize
    df['_ROIC_3Y_w'] = winsorize_series(df['ROIC_3Y_Avg(%)']) if 'ROIC_3Y_Avg(%)' in df.columns else df['_ROIC_w']
    df['_TSY_w'] = winsorize_series(df['Total_SH_Yield(%)']) if 'Total_SH_Yield(%)' in df.columns else pd.Series(0, index=df.index)
    
    # V10.2 Step 3: Robust Z-score
    df['Z_Quality'] = robust_zscore(df['_ROIC_w'])
    df['Z_Value'] = robust_zscore(df['_FCF_w'])
    df['Z_Momentum'] = robust_zscore(df['_MOM_w'])
    df['Z_Safety'] = robust_zscore(df['_DD_w'])
    # V10.3 新增兩個維度
    df['Z_Persistence'] = robust_zscore(df['_ROIC_3Y_w'])    # 品質持續性
    df['Z_Shareholder'] = robust_zscore(df['_TSY_w'])        # 股東回報
    
    # V10.2 Step 4: 週期頂警告對 Alpha 的扣分
    df['_cycle_penalty'] = df['Exit_Signal'].apply(lambda x: -0.5 if '週期頂' in str(x) else 0.0)
    # V10.3 新增：溢酬偏薄扣分
    df['_thin_premium_penalty'] = df['Exit_Signal'].apply(lambda x: -0.3 if '溢酬偏薄' in str(x) or '溢酬薄' in str(x) else 0.0)
    
    # V10.3 升級 Alpha 公式（六維度）
    df['Alpha_Score'] = (
        df['Z_Quality']      * 0.25 +    # 當前品質
        df['Z_Persistence']  * 0.20 +    # 品質持續性 (新)
        df['Z_Value']        * 0.20 +    # 估值
        df['Z_Momentum']     * 0.15 +    # 動能
        df['Z_Shareholder']  * 0.10 +    # 股東回報 (新)
        df['Z_Safety']       * 0.10 +    # 下檔保護
        df['_cycle_penalty'] +
        df['_thin_premium_penalty']
    ).round(3)
    
    # 清掉中間變數
    df = df.drop(columns=[c for c in df.columns if c.startswith('_')])
    return df.sort_values('Alpha_Score', ascending=False).reset_index(drop=True)

# ==============================================================================
# 報表發送
# ==============================================================================
def send_email_report(df: pd.DataFrame, receiver_email: str, trend_report: str):
    sender_email, sender_pwd = os.environ.get('EMAIL_SENDER'), os.environ.get('EMAIL_PASSWORD')
    if not sender_email or not sender_pwd: 
        logger.warning("未設定 EMAIL_SENDER 或 EMAIL_PASSWORD，略過發信。")
        return
    msg = EmailMessage()
    msg['Subject'] = f"[V10.3 品質持續性版] Alpha 報表 - {datetime.now().strftime('%H:%M:%S')}"
    msg['From'], msg['To'] = sender_email, receiver_email
    content = f"總工程師您好：\n\n【全域監測】\n{trend_report}\n" + "-"*50
    content += "\nV10.3 修正項目：3年ROIC持續性、股東總回報、毛利波動度、估值溢酬防線。\n\n"
    if df.empty: content += "今日無通關標的。"
    else:
        content += f"共計 {len(df)} 檔通關。\n\n【TOP 10】\n"
        cols = ['Ticker', 'Price', 'ROIC_3Y_Avg(%)', 'FCF_Yield(%)', 'Total_SH_Yield(%)', 
                'Momentum(%)', 'Alpha_Score', 'Exit_Signal']
        cols = [c for c in cols if c in df.columns]
        content += df.head(10)[cols].to_string(index=False)
    msg.set_content(content)
    if not df.empty:
        msg.add_attachment(df.to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig'), 
                           maintype='text', subtype='csv', filename='V10_3_Alpha_Final.csv')
    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(sender_email, sender_pwd)
            server.send_message(msg)
            logger.info("信件發送成功！")
    except Exception as e: 
        logger.error(f"郵件發送失敗: {e}")

if __name__ == "__main__":
    USER_EMAIL = os.environ.get('USER_EMAIL', 'a7924177@gmail.com')
    CACHE_FILE = "qualified_universe.csv"
    
    print("\n>>> 點火啟動：V10.3 品質持續性版 <<<\n")
    trend = check_global_trend()
    
    try:
        if not os.path.exists(CACHE_FILE):
            logger.error(f"找不到 {CACHE_FILE} 檔案，程式終止。")
            exit(1)
        df_c = pd.read_csv(CACHE_FILE)
        universe = dict(zip(df_c['Ticker'], df_c['CIK']))
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            res = list(executor.map(lambda p: run_v10_3_pipeline(p[0], str(p[1]), USER_EMAIL), universe.items()))
            
        final_df = calculate_composite_alpha(res)
        send_email_report(final_df, USER_EMAIL, trend)
        
        if not final_df.empty:
            print("\n>>> 分析完成，前 10 名通關標的：")
            cols = ['Ticker', 'ROIC_3Y_Avg(%)', 'FCF_Yield(%)', 'Total_SH_Yield(%)', 'Momentum(%)', 'Alpha_Score', 'Exit_Signal']
            cols = [c for c in cols if c in final_df.columns]
            print(final_df.head(10)[cols])
        else:
            print("\n>>> 分析完成，今日無標的通過嚴格篩選。")
            
    except Exception as e: 
        logger.critical(f"系統崩潰: {e}")
        traceback.print_exc()
