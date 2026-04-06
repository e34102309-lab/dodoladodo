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
TERMINAL_GROWTH = 0.025

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
# SEC 原生爬蟲 V9 (感測器頻寬升級)
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
            'SBC': [
                'ShareBasedCompensation', 
                'StockBasedCompensation',
                'AllocatedShareBasedCompensationExpense',
                'ShareBasedCompensationExpense',
                'AdjustmentForAmortization'
            ],
            'EBIT': ['OperatingIncomeLoss'],
            'Interest': ['InterestExpense', 'InterestExpenseDebt'],
            'DnA': ['DepreciationDepletionAndAmortization', 'DepreciationAndAmortization'],
            'Debt': ['LongTermDebt', 'LongTermDebtAndCapitalLeaseObligations', 'DebtCurrent'],
            'Cash': ['CashAndCashEquivalentsAtCarryingValue'],
            'Equity': ['StockholdersEquity']
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
# 核心管線 V9 (含 Stage 0 與雙層 SBC 備援)
# ==============================================================================
def run_v9_pipeline(ticker: str, cik: str, email: str) -> dict:
    time.sleep(random.uniform(0.5, 1.5))
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        
        # ── Stage 0 粗餾塔 ──────────────────────────────────────────────────
        sector = info.get('sector', 'Unknown')
        if sector in ['Financial Services', 'Real Estate', 'Financials']:
            return {"Ticker": ticker, "Status": "Drop: 產業隔離"}
            
        price = info.get('currentPrice', info.get('regularMarketPrice', 1))
        shares = info.get('sharesOutstanding', 1) / 1e9
        mcap = price * shares
        if mcap < 5.0: return {"Ticker": ticker, "Status": "Fail: 市值低於50億"}
        if info.get('trailingEps', -1) <= 0: return {"Ticker": ticker, "Status": "Fail: 過去一年虧損"}

        # ── Stage 1 精密 SEC 爬蟲與備援 ────────────────────────────────────
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

        ocf = sec.get_latest_annual(df_ocf)
        capex = abs(sec.get_latest_annual(df_capex))
        sbc = abs(sec.get_latest_annual(df_sbc))
        ebit = sec.get_latest_annual(df_ebit)
        interest = sec.get_latest_annual(df_int) or 0.05
        dna = abs(sec.get_latest_annual(df_dna))
        debt = sec.get_latest_annual(df_debt)
        cash = sec.get_latest_annual(df_cash)
        equity = sec.get_latest_annual(df_eq)

        if capex == 0: capex = abs(info.get('capitalExpenditures', 0)) / 1e9

        # SBC 雙層真實數據備援
        if sbc == 0: 
            sbc = abs(info.get('sharesBasedCompensation', 0) or 0) / 1e9
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

        # ── Stage 2 絕對安全邊際與雙殺推演 ──────────────────────────────────
        net_debt = debt - cash
        true_ev = max(mcap + net_debt, mcap * 0.10)
        maint_capex = min(dna, capex) if dna > 0 else capex
        real_fcf = ocf - maint_capex - sbc
        
        if real_fcf <= 0: return {"Ticker": ticker, "Status": "Fail: 實質FCF為負"}
        
        fcf_yield = (real_fcf / true_ev) * 100
        tax_rate = float(np.clip(info.get('effectiveTaxRate', 0.21) or 0.21, 0.1, 0.35))
        wacc = calculate_dynamic_wacc(ticker, debt, cash, mcap, equity, tax_rate)
        
        ic = max(debt + equity - cash, mcap * 0.10) if equity >= 0 else max(net_debt, mcap * 0.10)
        ic = max(ic, 0.05)
        roic = (ebit / ic) * 100
        icr = ebit / interest

        if roic < 10 or icr < 5: return {"Ticker": ticker, "Status": "Fail: ROIC或ICR破缺"}

        ebitda = ebit + dna + sbc
        if ebitda > 0:
            current_mult = true_ev / ebitda
            stress_ev = (ebitda * 0.70) * max(8.0, current_mult * 0.60)
            drawdown_risk = (((stress_ev - net_debt) - mcap) / mcap) * 100
        else:
            drawdown_risk = -999.0

        if drawdown_risk < -70: return {"Ticker": ticker, "Status": f"Drop: 極限回撤({drawdown_risk:.1f}%)"}

        # ── 動量與遲滯帶 (加入防爆指數退避機制) ──
        mom_12m = 0.0
        for attempt in range(3):  # 物理防線：最多容許 3 次連線重試
            try:
                # 隨機抖動 (Jitter)：錯開 4 個平行反應槽的請求時間，避免瞬間擊穿 API
                time.sleep(random.uniform(0.5, 2.0))
                
                hist = yf.download(ticker, start=datetime.now()-timedelta(days=420), progress=False)
                close = flatten_close(hist, ticker)
                
                if close is not None and not close.empty and len(close) > 200:
                    monthly = close.resample('ME').last()
                    if len(monthly) >= 13: 
                        mom_12m = (float(monthly.iloc[-2]) / float(monthly.iloc[-13]) - 1) * 100
                        break  # 數據成功萃取，強制跳出重試迴圈
            except Exception:
                # 若被伺服器阻擋，則休眠 2^attempt 秒後再試 (1秒 -> 2秒 -> 4秒)
                time.sleep(2 ** attempt)
        exit_signals = []
        rf = get_risk_free_rate() * 100
        if fcf_yield < rf and mom_12m < 0: exit_signals.append('🔴 停損: 溢酬消失且動量破滅')
        elif roic < wacc * 100: exit_signals.append('🔴 停損: 價值摧毀(ROIC < WACC)')
        if sec.check_q_yoy_decline(df_ocf): exit_signals.append('🟡 預警: OCF YoY衰退')

        return {
            'Ticker': ticker, 'Status': 'Pass', 'Price': round(price, 2),
            'WACC(%)': round(wacc * 100, 2), 'ROIC(%)': round(roic, 2),
            'FCF_Yield(%)': round(fcf_yield, 2), 'Real_FCF(B)': round(real_fcf, 3),
            'Momentum(%)': round(mom_12m, 2), 'Max_Drawdown_Risk(%)': round(drawdown_risk, 1),
            'Exit_Signal': exit_signals[0] if exit_signals else 'Hold ✅'
        }

    except Exception as e:
        return {"Ticker": ticker, "Status": "Error: 管線例外中斷"}

def calculate_composite_alpha(results: List[dict]) -> pd.DataFrame:
    df = pd.DataFrame([r for r in results if r.get('Status') == 'Pass'])
    if len(df) < 2: return df
    df['Z_Quality'] = safe_zscore(df['ROIC(%)'])
    df['Z_Value'] = safe_zscore(df['FCF_Yield(%)'])
    df['Z_Momentum'] = safe_zscore(df['Momentum(%)'])
    df['Z_Safety'] = safe_zscore(df['Max_Drawdown_Risk(%)'])
    df['Alpha_Score'] = (df['Z_Quality'] * 0.35 + df['Z_Value'] * 0.35 + df['Z_Momentum'] * 0.15 + df['Z_Safety'] * 0.15).round(3)
    return df.sort_values('Alpha_Score', ascending=False).reset_index(drop=True)

# ==============================================================================
# 自動寄信模組
# ==============================================================================
def send_email_report(df: pd.DataFrame, receiver_email: str):
    sender_email = os.environ.get('EMAIL_SENDER')
    sender_pwd = os.environ.get('EMAIL_PASSWORD')
    
    if not sender_email or not sender_pwd:
        logger.warning("未偵測到 GitHub Secrets，跳過郵件發送。")
        return

    msg = EmailMessage()
    msg['Subject'] = f"[AQR_Quant_V9] Alpha Candidates - {datetime.now().strftime('%Y-%m-%d')}"
    msg['From'] = sender_email
    msg['To'] = receiver_email
    
    content = "總工程師您好：\n\n本日全市場 V9 精餾作業已完成。\n"
    if df.empty:
        content += "警告：今日無任何標的通過物理邊界測試。"
    else:
        content += f"共計 {len(df)} 檔標的通關。詳細數據請見附件 CSV 檔案。\n\n"
        content += "【TOP 5 數據預覽】\n"
        display_cols = ['Ticker', 'Price', 'WACC(%)', 'ROIC(%)', 'FCF_Yield(%)', 'Alpha_Score', 'Exit_Signal']
        available = [c for c in display_cols if c in df.columns]
        content += df.head(5)[available].to_string(index=False)
    
    msg.set_content(content)
    
    if not df.empty:
        csv_data = df.to_csv(index=False, encoding='utf-8-sig')
        msg.add_attachment(csv_data.encode('utf-8-sig'), maintype='text', subtype='csv', filename='V9_Alpha_Candidates.csv')

    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(sender_email, sender_pwd)
            server.send_message(msg)
        logger.info(f"✅ 郵件已成功發送至 {receiver_email}")
    except Exception as e:
        logger.error(f"❌ 郵件發送失敗: {e}")

# ==============================================================================
# 系統點火 (多執行緒量產模式)
# ==============================================================================
if __name__ == "__main__":
    USER_EMAIL = os.environ.get('USER_EMAIL', 'a7924177@gmail.com')
    CACHE_FILE = "qualified_universe.csv"
    
    print("\n" + "="*80)
    print(" 啟動 V9 雲端量產管線 | Stage 0 粗餾 -> 平行反應槽 -> SMTP 輸出")
    print("="*80 + "\n")

    get_risk_free_rate()

    try:
        df_cache = pd.read_csv(CACHE_FILE)
        universe = dict(zip(df_cache['Ticker'], df_cache['CIK']))
        total = len(universe)
        print(f" >>> 載入 {total} 檔標的，準備進入平行精餾...\n" + "-"*80)
    except FileNotFoundError:
        print(f" [致命錯誤] 找不到 {CACHE_FILE}。")
        exit()

    results = []
    passed = 0
    processed = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        future_to_ticker = {executor.submit(run_v9_pipeline, t, c, USER_EMAIL): t for t, c in universe.items()}
        for future in concurrent.futures.as_completed(future_to_ticker):
            processed += 1
            ticker = future_to_ticker[future]
            res = future.result()
            results.append(res)
            
            if res.get('Status') == 'Pass':
                passed += 1
                msg = f"✓ WACC={res.get('WACC(%)')}% FCFY={res.get('FCF_Yield(%)')}% Risk={res.get('Max_Drawdown_Risk(%)')}%"
            else:
                msg = res.get('Status')
            print(f" [{processed:>4}/{total}] {ticker:<6} {msg}")

    print(f"\n 精餾完畢：{passed} / {total} 檔通關")
    final_df = calculate_composite_alpha(results)
    send_email_report(final_df, USER_EMAIL)
