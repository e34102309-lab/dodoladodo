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
import threading

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
    except Exception as e:
        logger.debug(f"[{ticker}] 收盤價展平失敗: {e}")
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
    except Exception as e:
        # V9.3 修正：補回靜默失敗的 Log 追蹤
        logger.debug(f"[{ticker}] Beta 計算異常，使用預設值 1.0: {e}")
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
                resp = self.session.get(url, headers=headers, timeout=15)
                if resp.status_code == 200: return resp
                elif resp.status_code in (429, 503):
                    time.sleep((2 ** attempt) * 2)
                else: 
                    return resp
            except requests.RequestException:
                time.sleep(3)
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
            'EBIT': ['OperatingIncomeLoss'],
            'Interest': ['InterestExpense', 'InterestExpenseDebt'],
            'DnA': ['DepreciationDepletionAndAmortization', 'DepreciationAndAmortization'],
            'Debt': ['LongTermDebt', 'LongTermDebtAndCapitalLeaseObligations', 'DebtCurrent'],
            'Cash': ['CashAndCashEquivalentsAtCarryingValue'],
            'Equity': ['StockholdersEquity'],
            'RND': ['ResearchAndDevelopmentExpense'],
            'DefRev': ['DeferredRevenue', 'ContractWithCustomerLiability'],
            'Assets': ['Assets'],
            'CurrLiab': ['LiabilitiesCurrent'],
            'FinRec': ['FinancingReceivableNet', 'NotesAndLoansReceivableNet', 'LoansAndLeasesReceivableNet']
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
                        return df.sort_values('end').drop_duplicates(subset=['end', 'form'], keep='last')
                except Exception:
                    pass
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
        return False if match.empty else (float(latest['val']) < float(match['val'].iloc[-1]) * 0.85)

# ==============================================================================
# 核心管線 V9.3
# ==============================================================================
def run_v9_pipeline(ticker: str, cik: str, email: str) -> dict:
    try:
        stock = yf.Ticker(ticker)
        
        try:
            price = float(stock.fast_info.get('lastPrice', 1.0))
        except Exception:
            info = stock.info
            price = info.get('currentPrice', info.get('regularMarketPrice', 1.0))
            
        info = stock.info
        shares_out = info.get('sharesOutstanding') or info.get('impliedSharesOutstanding', 0)
        mcap = (price * shares_out) / 1e9 if shares_out > 0 else float(stock.fast_info.get('marketCap', 0.0)) / 1e9

        if mcap < 1.0: return {"Ticker": ticker, "Status": "Fail: 市值異常或破缺"}
        
        gross_margin = float(info.get('grossMargins') or 0.0)
        rev_growth = float(info.get('revenueGrowth') or 0.0)
        total_revenue = float(info.get('totalRevenue') or 0.0) / 1e9
        
        if total_revenue == 0.0:
            try:
                fins = stock.financials
                if 'Total Revenue' in fins.index:
                    total_revenue = float(fins.loc['Total Revenue'].iloc[0]) / 1e9
            except Exception: pass

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
        df_assets = sec.fetch_concept(cik, 'Assets')
        df_curr_liab = sec.fetch_concept(cik, 'CurrLiab')
        df_fin_rec = sec.fetch_concept(cik, 'FinRec')

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
        assets = sec.get_latest_annual(df_assets)
        curr_liab = sec.get_latest_annual(df_curr_liab)
        fin_rec = sec.get_latest_annual(df_fin_rec)

        # --- 備援機制：yfinance 數據填補 ---
        if capex == 0: capex = abs(info.get('capitalExpenditures') or 0) / 1e9
        if sbc == 0: sbc = abs(info.get('shareBasedCompensation') or 0) / 1e9
        
        # V9.3 補丁：總資產與流動負債的強力備援 (防止負淨值算法崩潰)
        if assets == 0 or curr_liab == 0:
            try:
                bs = stock.balance_sheet
                if not bs.empty:
                    if assets == 0 and 'Total Assets' in bs.index:
                        assets = float(bs.loc['Total Assets'].iloc[0]) / 1e9
                    if curr_liab == 0 and 'Current Liabilities' in bs.index:
                        curr_liab = float(bs.loc['Current Liabilities'].iloc[0]) / 1e9
            except Exception as e:
                logger.debug(f"[{ticker}] 資產負債表備援失敗: {e}")
        # -----------------------------------
        raw_int = sec.get_latest_annual(df_int)
        interest = max(abs(raw_int), 0.05) if raw_int != 0 else 0.05

        adjusted_debt = max(0.0, debt - fin_rec)
        excess_cash = max(0.0, cash - (total_revenue * 0.02))
        net_debt = max(adjusted_debt - excess_cash, 0.0)
        true_ev = max(mcap + net_debt, mcap * 0.10)
        
        maint_capex = dna if dna > 0 else capex
        real_fcf = ocf - maint_capex - sbc
        
        if real_fcf <= 0:
            reason = "SBC吞噬" if sbc > ocf * 0.4 else "重資本耗損"
            return {"Ticker": ticker, "Status": f"Fail: 實質FCF為負 ({reason})"}
        
        fcf_yield = min((real_fcf / true_ev) * 100, 50.0) if true_ev > 0 else 0.0
        
        tax_rate = float(np.clip(info.get('effectiveTaxRate') or 0.21, 0.1, 0.35))
        wacc = calculate_dynamic_wacc(ticker, adjusted_debt, cash, mcap, equity, tax_rate)
        
        adjusted_ebit = ebit 
        capitalized_rnd = rnd * 2.5
        
        if equity > 0:
            ic = max(adjusted_debt + equity + capitalized_rnd - excess_cash, true_ev * 0.10)
        else:
            ic = max(assets - curr_liab - excess_cash, true_ev * 0.10)
            
        roic = min((adjusted_ebit / max(ic, 0.1)) * 100, 100.0)
        icr = adjusted_ebit / interest

        if roic < 10 or icr < 5: return {"Ticker": ticker, "Status": "Fail: ROIC或ICR過低"}

        is_growth_monster = False
        if total_revenue > 0:
            billings_growth = rev_growth + (defrev_change / total_revenue)
            real_r40 = ((real_fcf / total_revenue) + billings_growth) * 100
            if gross_margin >= 0.75 and (roic - wacc*100) > 5.0 and real_r40 >= 40.0: is_growth_monster = True

        ebitda = adjusted_ebit + dna + sbc
        if ebitda > 0:
            current_mult = true_ev / ebitda
            floor = min(20.0, 8.0 + ((gross_margin - 0.50) / 0.10) * 1.5) if gross_margin > 0.5 else 8.0
            stress_mult = min(current_mult, max(floor, current_mult * 0.60))
            drawdown = ((max(0.0, (ebitda * 0.70) * stress_mult - net_debt) - mcap) / mcap) * 100
            drawdown_risk = min(0.0, drawdown)
        else: drawdown_risk = -100.0

        if drawdown_risk < -70: return {"Ticker": ticker, "Status": f"Drop: 極限回撤({drawdown_risk:.1f}%)"}

        mom_12m = 0.0
        try:
            hist = yf.download(ticker, start=datetime.now()-timedelta(days=420), progress=False)
            close = flatten_close(hist, ticker)
            if close is not None and len(close) > 200:
                m = close.resample('ME').last()
                mom_12m = min((float(m.iloc[-2]) / float(m.iloc[-13]) - 1) * 100, 200.0)
        except Exception: pass

        # V9.3 修正：字串拼接邏輯，完美保留所有決策軌跡
        exit_signal = "Hold ✅"
        if is_growth_monster:
            exit_signal = "🚀 成長旁通: Rule of 40 通行證 ✅"
            if sec.check_q_yoy_decline(df_ocf): 
                exit_signal += " | 🟡 預警: 成長股OCF衰退"
        else:
            # 先判定最嚴格的停損條件
            if fcf_yield < (get_risk_free_rate()*100) and mom_12m < 0: 
                exit_signal = "🔴 停損: 溢酬消失"
            elif roic < wacc * 100: 
                exit_signal = "🔴 停損: 價值摧毀"
            
            # 再判定附加預警條件，確保不會洗掉停損訊號
            if sec.check_q_yoy_decline(df_ocf): 
                if exit_signal == "Hold ✅":
                    exit_signal = "🟡 預警: OCF YoY衰退"
                else:
                    exit_signal += " | 🟡 OCF YoY衰退"

        return {
            'Ticker': ticker, 'Status': 'Pass', 'Price': round(price, 2),
            'WACC(%)': round(wacc * 100, 2), 'ROIC(%)': round(roic, 2),
            'FCF_Yield(%)': round(fcf_yield, 2), 'Real_FCF(B)': round(real_fcf, 3),
            'Momentum(%)': round(mom_12m, 2), 'Max_Drawdown_Risk(%)': round(drawdown_risk, 1),
            'Exit_Signal': exit_signal
        }
    except Exception as e: 
        logger.error(f"[{ticker}] 處理時發生未預期錯誤: {e}")
        return {"Ticker": ticker, "Status": "Error"}

def calculate_composite_alpha(results: List[dict]) -> pd.DataFrame:
    df = pd.DataFrame([r for r in results if r.get('Status') == 'Pass'])
    if len(df) < 2: return df
    df['Z_Quality'], df['Z_Value'] = safe_zscore(df['ROIC(%)']), safe_zscore(df['FCF_Yield(%)'])
    df['Z_Momentum'], df['Z_Safety'] = safe_zscore(df['Momentum(%)']), safe_zscore(df['Max_Drawdown_Risk(%)'])
    df['Alpha_Score'] = (df['Z_Quality']*0.35 + df['Z_Value']*0.35 + df['Z_Momentum']*0.15 + df['Z_Safety']*0.15).round(3)
    return df.sort_values('Alpha_Score', ascending=False).reset_index(drop=True)

def send_email_report(df: pd.DataFrame, receiver_email: str, trend_report: str):
    sender_email, sender_pwd = os.environ.get('EMAIL_SENDER'), os.environ.get('EMAIL_PASSWORD')
    if not sender_email or not sender_pwd: 
        logger.warning("未設定 EMAIL_SENDER 或 EMAIL_PASSWORD，略過發信。")
        return
    msg = EmailMessage()
    msg['Subject'] = f"[V9.3 邏輯補丁版] Alpha 報表 - {datetime.now().strftime('%H:%M:%S')}"
    msg['From'], msg['To'] = sender_email, receiver_email
    content = f"總工程師您好：\n\n【全域監測】\n{trend_report}\n" + "-"*50 + "\n已實裝訊號拼接軌跡與日誌追蹤修復。\n\n"
    if df.empty: content += "今日無通關標的。"
    else:
        content += f"共計 {len(df)} 檔通關。\n\n【TOP 5】\n"
        content += df.head(5)[['Ticker', 'Price', 'ROIC(%)', 'FCF_Yield(%)', 'Alpha_Score', 'Exit_Signal']].to_string(index=False)
    msg.set_content(content)
    if not df.empty:
        msg.add_attachment(df.to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig'), 
                           maintype='text', subtype='csv', filename='V9_3_Alpha_Final.csv')
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
    
    print("\n>>> 點火啟動：V9.3 邏輯補丁修復版本 <<<\n")
    trend = check_global_trend()
    
    try:
        if not os.path.exists(CACHE_FILE):
            logger.error(f"找不到 {CACHE_FILE} 檔案，程式終止。")
            exit(1)
            
        df_c = pd.read_csv(CACHE_FILE)
        universe = dict(zip(df_c['Ticker'], df_c['CIK']))
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            res = list(executor.map(lambda p: run_v9_pipeline(p[0], str(p[1]), USER_EMAIL), universe.items()))
            
        final_df = calculate_composite_alpha(res)
        send_email_report(final_df, USER_EMAIL, trend)
        
        if not final_df.empty:
            print("\n>>> 分析完成，前 5 名通關標的：")
            print(final_df.head(5)[['Ticker', 'Alpha_Score', 'Exit_Signal']])
        else:
            print("\n>>> 分析完成，今日無標的通過嚴格篩選。")
            
    except Exception as e: 
        logger.critical(f"系統崩潰: {e}")
