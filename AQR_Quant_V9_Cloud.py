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
from typing import Dict, List, Optional
import logging
import concurrent.futures

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)

# ==============================================================================
# 全域快取與巨集參數
# ==============================================================================
_RF_CACHE: Optional[float] = None
MARKET_RISK_PREMIUM = 0.046

def get_risk_free_rate() -> float:
    global _RF_CACHE
    if _RF_CACHE is not None:
        return _RF_CACHE
    try:
        hist = yf.Ticker('^TNX').history(period='5d')
        if not hist.empty:
            _RF_CACHE = float(hist['Close'].iloc[-1]) / 100
            return _RF_CACHE
    except Exception:
        pass
    _RF_CACHE = 0.0435
    return _RF_CACHE

def safe_zscore(series: pd.Series) -> pd.Series:
    s = pd.to_numeric(series, errors='coerce').fillna(0.0)
    if len(s) < 2 or s.std(ddof=1) < 1e-9:
        return pd.Series(np.zeros(len(s)), index=s.index)
    return pd.Series(zscore(s, ddof=1), index=s.index)

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

# ==============================================================================
# 全域大氣壓力感測器 (200-Day Trend Filter)
# ==============================================================================
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
                    status = "🔴 跌破 200SMA (系統進入冰河保護期)" if below_sma_week else "🟢 穩態多頭"
                    trend_msg += f"[{idx}] 收盤: {last_close:.2f} | 200SMA: {last_sma:.2f} ({diff_pct:+.2f}%) -> {status}\n"
    except Exception as e:
        trend_msg += f"趨勢感測器異常: {e}\n"
    return trend_msg if trend_msg else "大氣壓力感測器離線。\n"

# ==============================================================================
# 動態 Beta 與 WACC 引擎
# ==============================================================================
def calculate_dynamic_beta(ticker: str) -> float:
    try:
        end = datetime.now()
        start = end - timedelta(days=365 * 3)
        data = yf.download([ticker, 'SPY'], start=start, end=end, interval='1wk', progress=False, auto_adjust=True)
        if data.empty or 'Close' not in data.columns: return 1.0
        close_df = data['Close']
        if ticker not in close_df.columns or 'SPY' not in close_df.columns: return 1.0
        returns = close_df.pct_change().dropna()
        if len(returns) < 50: return 1.0
        var_market = returns['SPY'].var()
        if var_market == 0: return 1.0
        beta = returns.cov().loc[ticker, 'SPY'] / var_market
        return float(np.clip(beta, 0.5, 2.5))
    except Exception:
        return 1.0

def calculate_dynamic_wacc(ticker: str, debt: float, cash: float, 
                           market_cap: float, book_equity: float, tax_rate: float) -> float:
    rf = get_risk_free_rate()
    beta_u = calculate_dynamic_beta(ticker)
    anchor_equity = max(market_cap, book_equity * 1.5) if book_equity > 0 else market_cap
    anchor_equity = max(anchor_equity, 1.0)
    net_debt = max(debt - cash, 0.0)
    
    de_ratio = net_debt / anchor_equity
    beta_l = float(np.clip(beta_u * (1.0 + (1.0 - tax_rate) * de_ratio), 0.5, 3.0))
    ke = rf + beta_l * MARKET_RISK_PREMIUM
    total = market_cap + net_debt
    w_e = market_cap / total if total > 0 else 1.0
    w_d = net_debt / total if total > 0 else 0.0
    kd = rf + 0.015
    wacc = w_e * ke + w_d * kd * (1.0 - tax_rate)
    return float(np.clip(wacc, 0.06, 0.20))

# ==============================================================================
# SEC 原生爬蟲 V9 
# ==============================================================================
class RateLimitedSession:
    def __init__(self):
        self.session = requests.Session()
    def get(self, url: str, headers: dict) -> Optional[requests.Response]:
        for attempt in range(5):
            time.sleep(0.2)
            try:
                resp = self.session.get(url, headers=headers, timeout=15)
                if resp.status_code == 200: return resp
                elif resp.status_code in (429, 503): time.sleep((2 ** attempt) * 2)
                else: return resp
            except requests.RequestException:
                time.sleep(3)
        return None

class SECDataDistiller:
    def __init__(self, email: str):
        self.headers = {'User-Agent': f'AQR_Quant_V9 (Contact: {email})'}
        self.session = RateLimitedSession()
        self.config = {
            'OCF': ['NetCashProvidedByUsedInOperatingActivities'],
            'CapEx': ['PaymentsToAcquirePropertyPlantAndEquipment', 'PropertyPlantAndEquipmentAdditions'],
            'SBC': ['ShareBasedCompensation', 'StockBasedCompensation', 'AllocatedShareBasedCompensationExpense', 'ShareBasedCompensationExpense', 'AdjustmentForAmortization'],
            'EBIT': ['OperatingIncomeLoss'],
            'Interest': ['InterestExpense', 'InterestExpenseDebt'],
            'DnA': ['DepreciationDepletionAndAmortization', 'DepreciationAndAmortization'],
            'Debt': ['LongTermDebt', 'LongTermDebtAndCapitalLeaseObligations', 'DebtCurrent'],
            'Cash': ['CashAndCashEquivalentsAtCarryingValue'],
            'Equity': ['StockholdersEquity'],
            'RND': ['ResearchAndDevelopmentExpense', 'ResearchAndDevelopmentExpenseSoftwareExcludingAcquiredInProcessCost'],
            'DefRev': ['DeferredRevenue', 'ContractWithCustomerLiability']
        }

    def fetch_concept(self, cik: str, concept: str) -> pd.DataFrame:
        for tag in self.config.get(concept, [concept]):
            url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{str(cik).zfill(10)}/us-gaap/{tag}.json"
            resp = self.session.get(url, headers=self.headers)
            if resp and resp.status_code == 200:
                data = resp.json().get('units', {}).get('USD', [])
                if data:
                    df = pd.DataFrame(data)
                    df['end'] = pd.to_datetime(df['end'])
                    return df.sort_values('end').drop_duplicates(subset=['end', 'form'], keep='last')
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

    def check_q_yoy_decline(self, df: pd.DataFrame) -> bool:
        if df.empty: return False
        q_df = df[df['form'] == '10-Q'].copy()
        if len(q_df) < 4: return False
        latest = q_df.iloc[-1]
        target_date = latest['end'] - pd.Timedelta(days=365)
        q_df['diff'] = abs((q_df['end'] - target_date).dt.days)
        match = q_df[q_df['diff'] < 30]
        if not match.empty:
            past_val = float(match['val'].iloc[-1])
            if past_val > 0 and float(latest['val']) < past_val * 0.85: return True
        return False

# ==============================================================================
# 核心管線 V9 
# ==============================================================================
def run_v9_pipeline(ticker: str, cik: str, email: str) -> dict:
    time.sleep(random.uniform(0.5, 1.5))
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        
        # ── Stage 0 ─────────────────────────────
        price = info.get('currentPrice', info.get('regularMarketPrice', 1))
        shares = info.get('sharesOutstanding', 1) / 1e9
        mcap = price * shares
        if mcap < 1.0: return {"Ticker": ticker, "Status": "Fail: 市值異常 (<1.0B)"}
        
        gross_margin = float(info.get('grossMargins', 0.0) or 0.0)
        rev_growth = float(info.get('revenueGrowth', 0.0) or 0.0)
        total_revenue = float(info.get('totalRevenue', 0.0) or 0.0) / 1e9

        # ── Stage 1 ────────────────────────────────────
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

        ocf = sec.get_latest_annual(df_ocf)
        capex = abs(sec.get_latest_annual(df_capex))
        sbc = abs(sec.get_latest_annual(df_sbc))
        ebit = sec.get_latest_annual(df_ebit)
        interest = sec.get_latest_annual(df_int) or 0.05
        dna = abs(sec.get_latest_annual(df_dna))
        debt = sec.get_latest_annual(df_debt)
        cash = sec.get_latest_annual(df_cash)
        equity = sec.get_latest_annual(df_eq)
        rnd = abs(sec.get_latest_annual(df_rnd))
        defrev_change = sec.get_yoy_change(df_defrev)

        if capex == 0: capex = abs(info.get('capitalExpenditures', 0)) / 1e9
        if sbc == 0: sbc = abs(info.get('sharesBasedCompensation', 0) or 0) / 1e9
        if sbc == 0:
            try:
                cf = stock.cashflow
                for label in ['Stock Based Compensation', 'Share Based Compensation']:
                    if cf is not None and label in cf.index:
                        val = float(cf.loc[label].iloc[0])
                        if not np.isnan(val) and val != 0:
                            sbc = abs(val) / 1e9
                            break
            except Exception: pass
        if sbc == 0: return {"Ticker": ticker, "Status": "Drop: SBC數據破缺"}

        # TTM 滾動
        try:
            qcf = stock.quarterly_cashflow
            if qcf is not None and not qcf.empty and len(qcf.columns) >= 4:
                if 'Operating Cash Flow' in qcf.index:
                    ttm_ocf = float(qcf.loc['Operating Cash Flow'].iloc[:4].fillna(0).sum()) / 1e9
                    if ttm_ocf != 0: ocf = ttm_ocf  
                if 'Capital Expenditure' in qcf.index:
                    ttm_capex = float(abs(qcf.loc['Capital Expenditure'].iloc[:4].fillna(0).sum())) / 1e9
                    if ttm_capex != 0: capex = ttm_capex 
                for label in ['Stock Based Compensation', 'Share Based Compensation']:
                    if label in qcf.index:
                        ttm_sbc = float(abs(qcf.loc[label].iloc[:4].fillna(0).sum())) / 1e9
                        if ttm_sbc != 0: sbc = ttm_sbc; break
        except Exception: pass

        # ── Stage 2: 物理極限封頂 (Winsorization) ─────────────────────────
        
        excess_cash = max(0.0, cash - (total_revenue * 0.02))
        net_debt = debt - excess_cash
        true_ev = max(mcap + net_debt, mcap * 0.10)
        
        maint_capex = max(dna * 1.15, capex * 0.70) if dna > 0 else capex
        real_fcf = ocf - maint_capex - sbc
        if real_fcf <= 0: return {"Ticker": ticker, "Status": "Fail: 實質FCF為負 (SBC吞噬)"}
        
        # 🛡️【限制器一：FCF Yield 物理封頂於 50%】防止 EV 逼近零導致的奇點挾持
        fcf_yield = min((real_fcf / true_ev) * 100, 50.0)
        
        tax_rate = float(np.clip(info.get('effectiveTaxRate', 0.21) or 0.21, 0.1, 0.35))
        wacc = calculate_dynamic_wacc(ticker, debt, cash, mcap, equity, tax_rate)
        
        adjusted_ebit = ebit + rnd 
        adjusted_equity = max(equity, 0.0) 
        capitalized_rnd = rnd * 2.5 
        
        ic = max(debt + adjusted_equity + capitalized_rnd - excess_cash, true_ev * 0.10)
        ic = max(ic, 0.05)
        # 🛡️【限制器二：ROIC 物理封頂於 100%】
        roic = min((adjusted_ebit / ic) * 100, 100.0) 
        icr = adjusted_ebit / interest

        if roic < 10 or icr < 5: return {"Ticker": ticker, "Status": "Fail: ROIC或ICR破缺"}

        is_growth_monster = False
        if total_revenue > 0:
            real_fcf_margin = real_fcf / total_revenue
            billings_growth = rev_growth + (defrev_change / total_revenue if total_revenue > 0 else 0)
            real_rule_of_40 = (real_fcf_margin + billings_growth) * 100
            if gross_margin >= 0.75 and (roic - wacc * 100) > 5.0 and real_rule_of_40 >= 40.0:
                is_growth_monster = True

        dynamic_floor = 8.0
        if gross_margin > 0.50:
            dynamic_floor = min(20.0, 8.0 + ((gross_margin - 0.50) / 0.10) * 1.5)

        ebitda = adjusted_ebit + dna + sbc
        if ebitda > 0:
            current_mult = true_ev / ebitda
            # 🛡️【限制器三：壓力乘數絕對不可高於現實】
            stress_mult = min(current_mult, max(dynamic_floor, current_mult * 0.60))
            stress_ev = (ebitda * 0.70) * stress_mult
            stress_mcap = max(0.0, stress_ev - net_debt)
            drawdown_risk = ((stress_mcap - mcap) / mcap) * 100
            # 🛡️【限制器四：回撤率必須小於等於 0% (絕對不可能變正數)】
            drawdown_risk = min(0.0, drawdown_risk)
        else:
            drawdown_risk = -100.0

        if drawdown_risk < -70: return {"Ticker": ticker, "Status": f"Drop: 極限回撤({drawdown_risk:.1f}%)"}

        mom_12m = 0.0
        for attempt in range(3):
            try:
                time.sleep(random.uniform(0.5, 2.0))
                hist = yf.download(ticker, start=datetime.now()-timedelta(days=420), progress=False)
                close = flatten_close(hist, ticker)
                if close is not None and not close.empty and len(close) > 200:
                    monthly = close.resample('ME').last()
                    if len(monthly) >= 13: 
                        raw_mom = (float(monthly.iloc[-2]) / float(monthly.iloc[-13]) - 1) * 100
                        # 🛡️【限制器五：動能封頂於 200%】防止妖股挾持
                        mom_12m = min(raw_mom, 200.0)
                        break
            except Exception:
                time.sleep(2 ** attempt)

        exit_signals = []
        rf = get_risk_free_rate() * 100
        
        if is_growth_monster:
            if sec.check_q_yoy_decline(df_ocf): exit_signals.append('🟡 預警: 成長股OCF衰退')
            else: exit_signals.append('🚀 成長旁通: Rule of 40 通行證 ✅')
        else:
            if fcf_yield < rf and mom_12m < 0: exit_signals.append('🔴 停損: 溢酬消失且動量破滅')
            elif roic < wacc * 100: exit_signals.append('🔴 停損: 價值摧毀(ROIC < WACC)')
            if sec.check_q_yoy_decline(df_ocf): exit_signals.append('🟡 預警: OCF YoY衰退')
            
        if not exit_signals: exit_signals.append('Hold ✅')

        return {
            'Ticker': ticker, 'Status': 'Pass', 'Price': round(price, 2),
            'WACC(%)': round(wacc * 100, 2), 'ROIC(%)': round(roic, 2),
            'FCF_Yield(%)': round(fcf_yield, 2), 'Real_FCF(B)': round(real_fcf, 3),
            'Momentum(%)': round(mom_12m, 2), 'Max_Drawdown_Risk(%)': round(drawdown_risk, 1),
            'Exit_Signal': exit_signals[0]
        }
    except Exception as e:
        return {"Ticker": ticker, "Status": "Error"}

def calculate_composite_alpha(results: List[dict]) -> pd.DataFrame:
    df = pd.DataFrame([r for r in results if r.get('Status') == 'Pass'])
    if len(df) < 2: return df
    df['Z_Quality'] = safe_zscore(df['ROIC(%)'])
    df['Z_Value'] = safe_zscore(df['FCF_Yield(%)'])
    df['Z_Momentum'] = safe_zscore(df['Momentum(%)'])
    df['Z_Safety'] = safe_zscore(df['Max_Drawdown_Risk(%)'])
    df['Alpha_Score'] = (df['Z_Quality'] * 0.35 + df['Z_Value'] * 0.35 + df['Z_Momentum'] * 0.15 + df['Z_Safety'] * 0.15).round(3)
    return df.sort_values('Alpha_Score', ascending=False).reset_index(drop=True)

def send_email_report(df: pd.DataFrame, receiver_email: str, trend_report: str):
    sender_email = os.environ.get('EMAIL_SENDER')
    sender_pwd = os.environ.get('EMAIL_PASSWORD')
    if not sender_email or not sender_pwd: return

    msg = EmailMessage()
    # 【信號分離】：強迫 Gmail 不折疊信件
    msg['Subject'] = f"[V9 絕對封頂版] Alpha 報表 - {datetime.now().strftime('%H:%M:%S')}"
    msg['From'] = sender_email
    msg['To'] = receiver_email
    
    content = f"總工程師您好：\n\n【全域大氣壓力監測】\n{trend_report}\n"
    content += "-"*50 + "\n已實裝四維物理限制器 (Winsorization)，徹底消滅 Z-Score 挾持。\n\n"
    
    if df.empty: content += "警告：今日無任何標的通關。"
    else:
        content += f"共計 {len(df)} 檔通關。\n\n【TOP 5】\n"
        display_cols = ['Ticker', 'Price', 'WACC(%)', 'ROIC(%)', 'FCF_Yield(%)', 'Alpha_Score', 'Exit_Signal']
        content += df.head(5)[[c for c in display_cols if c in df.columns]].to_string(index=False)
    
    msg.set_content(content)
    
    if not df.empty:
        csv_data = df.to_csv(index=False, encoding='utf-8-sig')
        # 【強制覆寫快取】：換一個全新檔名
        msg.add_attachment(csv_data.encode('utf-8-sig'), maintype='text', subtype='csv', filename='V9_Alpha_Winsorized.csv')

    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(sender_email, sender_pwd)
            server.send_message(msg)
    except Exception: pass

if __name__ == "__main__":
    USER_EMAIL = os.environ.get('USER_EMAIL', 'a7924177@gmail.com')
    CACHE_FILE = "qualified_universe.csv"
    
    print("\n>>> 曳光彈測試：V9 物理終極限制器版上線 <<<\n")
    trend_report = check_global_trend()
    get_risk_free_rate()

    try:
        df_cache = pd.read_csv(CACHE_FILE)
        universe = dict(zip(df_cache['Ticker'], df_cache['CIK']))
        total = len(universe)
    except FileNotFoundError: exit()

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        future_to_ticker = {executor.submit(run_v9_pipeline, t, c, USER_EMAIL): t for t, c in universe.items()}
        for future in concurrent.futures.as_completed(future_to_ticker):
            results.append(future.result())

    final_df = calculate_composite_alpha(results)
    send_email_report(final_df, USER_EMAIL, trend_report)
