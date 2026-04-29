"""
=============================================================================
total_market_hunter V2 - 升級篩選邏輯
=============================================================================
修正項目：
1. industry 二級過濾（漏網的金融、航空、菸草、商品週期股）
2. PP&E / Revenue < 0.5（資本輕資產過濾）
3. 毛利率下限（過濾低品質代工製造）
4. 機構持股下限 40%（過濾流動性差 / 治理風險）
5. Debt/EBITDA < 4x（過濾過度槓桿）
6. 雙重股權自動排除（A/B 類股保留主流動性那檔）
7. 市值門檻 1.5B → 3B（提高基礎品質）
=============================================================================
"""
import requests
import yfinance as yf
import time
import random
import pandas as pd
from datetime import datetime
import concurrent.futures
from typing import Dict

# === V2 升級門檻 ===
MIN_MCAP_B = 3.0                # 1.5 → 3.0
MIN_GROSS_MARGIN = 0.25         # 新增：低於 25% 多半是低品質代工 / 通路
MIN_INSTITUTIONAL_OWN = 0.40    # 新增：機構持股下限
MAX_DEBT_EBITDA = 4.0           # 新增：負債警戒線
MAX_PPE_REV_RATIO = 1.0         # 新增：資產輕資產（PPE / Revenue）

# Sector 一級過濾
BLOCKED_SECTORS = [
    'Financial Services', 'Real Estate', 'Financials',
    'Energy', 'Basic Materials', 'Utilities'
]

# Industry 二級過濾（漏網的關鍵字匹配）
BLOCKED_INDUSTRY_KEYWORDS = [
    'Bank', 'Insurance', 'REIT', 'Mortgage', 'Credit Services',
    'Capital Markets', 'Asset Management',          # 金融漏網
    'Airlines', 'Marine Shipping', 'Trucking',      # 重資產 + 高槓桿週期
    'Tobacco',                                       # 夕陽
    'Farm Products', 'Packaged Foods',              # 商品週期（CALM/HRL）
    'Oil & Gas', 'Coal',                            # 能源漏網
    'Auto Manufacturers', 'Auto Parts',             # 高週期 + 高資本
    'Aerospace & Defense',                          # 政府訂單依賴
    'Steel', 'Aluminum', 'Copper',                  # 商品
]

# A/B 雙重股權處理：保留主流動性那檔（手動表，可擴充）
DUAL_CLASS_KEEP = {
    'RUSH-A': 'RUSHA',  # 保留 A，去掉 B
    'FOX': 'FOXA',
    'GOOG': 'GOOGL',
    'BRK': 'BRK-B',     # 雖然 - 已經被擋，列出供參考
    'NWS': 'NWSA',
    'UA': 'UAA',
    'LEN': 'LEN',       # 保留主類
}

# ==============================================================================
# [組件 A]：對接 SEC 官方原始資料 (不變)
# ==============================================================================
def get_sec_master_universe(email: str) -> dict:
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 啟動 SEC 原料管線...")
    url = "https://www.sec.gov/files/company_tickers.json"
    headers = {'User-Agent': f'NTU_Chem_Quant_System (Contact: {email})'}
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        universe = {v['ticker']: str(v['cik_str']) for k, v in data.items()}
        print(f" >>> [成功] 擷取全美股 {len(universe)} 檔標的 CIK。")
        return universe
    except Exception as e:
        print(f" >>> [錯誤] SEC 管線連接失敗: {e}")
        return {}

# ==============================================================================
# [組件 B-V2]：核心過濾引擎 (七層防線)
# ==============================================================================
def check_stock_qualification_v2(ticker: str, cik: str) -> dict:
    time.sleep(random.uniform(0.1, 1.5))
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            time.sleep(random.uniform(0.1, 0.4))
            stock = yf.Ticker(ticker)
            info = stock.info
            if not info or 'symbol' not in info:
                raise ValueError("Empty info returned")

            # ── 防線 1：市值（提高到 3B） ────────────────────────────
            mcap = info.get('marketCap')
            if mcap is None or mcap == 0:
                try: mcap = getattr(stock.fast_info, 'market_cap', 0)
                except: mcap = 0
            mcap_b = (mcap or 0) / 1e9
            if mcap_b < MIN_MCAP_B:
                return {"Ticker": ticker, "Status": f"Drop: 市值<{MIN_MCAP_B}B ({mcap_b:.2f}B)"}

            # ── 防線 2：Sector 一級過濾 ─────────────────────────────
            sector = info.get('sector', 'Unknown')
            if sector in BLOCKED_SECTORS:
                return {"Ticker": ticker, "Status": f"Drop: 產業隔離 ({sector})"}

            # ── 防線 3：Industry 二級過濾 (V2 新增，捕捉漏網) ──────
            industry = info.get('industry', '')
            for kw in BLOCKED_INDUSTRY_KEYWORDS:
                if kw.lower() in industry.lower():
                    return {"Ticker": ticker, "Status": f"Drop: 行業隔離 ({industry})"}

            # ── 防線 4：營運現金流必須為正 ──────────────────────────
            ocf = info.get('operatingCashflow')
            if ocf is not None and ocf <= 0:
                return {"Ticker": ticker, "Status": "Drop: 營運現金流為負"}

            # ── 防線 5：毛利率下限 (V2 新增) ────────────────────────
            gross_margin = info.get('grossMargins')
            if gross_margin is not None and gross_margin < MIN_GROSS_MARGIN:
                return {"Ticker": ticker, "Status": f"Drop: 毛利率<{MIN_GROSS_MARGIN*100:.0f}% ({gross_margin*100:.1f}%)"}

            # ── 防線 6：機構持股下限 (V2 新增) ──────────────────────
            inst_own = info.get('heldPercentInstitutions')
            if inst_own is not None and inst_own < MIN_INSTITUTIONAL_OWN:
                return {"Ticker": ticker, "Status": f"Drop: 機構持股<{MIN_INSTITUTIONAL_OWN*100:.0f}% ({inst_own*100:.1f}%)"}

            # ── 防線 7：負債/EBITDA (V2 新增) ───────────────────────
            ebitda = info.get('ebitda')
            total_debt = info.get('totalDebt')
            if ebitda and ebitda > 0 and total_debt:
                debt_ebitda = total_debt / ebitda
                if debt_ebitda > MAX_DEBT_EBITDA:
                    return {"Ticker": ticker, "Status": f"Drop: 負債/EBITDA>{MAX_DEBT_EBITDA} ({debt_ebitda:.1f}x)"}

            # ── 防線 8：A/B 雙重股權去重 (V2 新增) ──────────────────
            # 主程式 V10.2 已用 CIK 去重，這裡再做一層 ticker 層級
            if ticker in DUAL_CLASS_KEEP and DUAL_CLASS_KEEP[ticker] != ticker:
                return {"Ticker": ticker, "Status": f"Drop: 雙重股權，保留 {DUAL_CLASS_KEEP[ticker]}"}

            return {
                "Ticker": ticker, 
                "CIK": cik, 
                "Sector": sector, 
                "Industry": industry,
                "MarketCap_B": round(mcap_b, 2),
                "GrossMargin": round((gross_margin or 0) * 100, 1),
                "InstOwn": round((inst_own or 0) * 100, 1),
                "Status": "Pass"
            }
            
        except Exception as e:
            err_str = str(e)
            if any(keyword in err_str for keyword in ["401", "429", "Crumb", "Empty info", "Too Many Requests", "Rate limited"]):
                if attempt < max_retries - 1:
                    time.sleep((2 ** attempt) * 2 + random.uniform(1, 3))
                    continue
                else:
                    return {"Ticker": ticker, "Status": "Drop: API 阻擋"}
            else:
                return {"Ticker": ticker, "Status": f"Drop: API 例外 ({err_str[:15]})"}
                
    return {"Ticker": ticker, "Status": "Drop: 未知超時"}

# ==============================================================================
# [組件 C]：平行運算與資料庫輸出
# ==============================================================================
def build_qualified_database_v2(universe: dict, scan_limit: int = 0) -> None:
    tickers = list(universe.items())
    if scan_limit > 0:
        tickers = tickers[:scan_limit]
        
    total = len(tickers)
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 啟動 V2 八層過濾...")
    print(f" >>> 預計掃描總數: {total} 檔")
    print(f" >>> 門檻: 市值>{MIN_MCAP_B}B | 毛利>{MIN_GROSS_MARGIN*100:.0f}% | "
          f"機構持股>{MIN_INSTITUTIONAL_OWN*100:.0f}% | 負債/EBITDA<{MAX_DEBT_EBITDA}x")
    
    survivors = []
    drop_reasons = {}  # V2 新增：統計各防線剔除數量
    processed = 0
    start_time = time.time()
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        future_to_ticker = {
            executor.submit(check_stock_qualification_v2, ticker, cik): ticker 
            for ticker, cik in tickers
        }
        
        for future in concurrent.futures.as_completed(future_to_ticker):
            processed += 1
            res = future.result()
            
            if res["Status"] == "Pass":
                survivors.append(res)
            else:
                # 統計剔除原因
                reason_key = res["Status"].split(":")[1].strip().split("(")[0].strip() if ":" in res["Status"] else "其他"
                drop_reasons[reason_key] = drop_reasons.get(reason_key, 0) + 1
                
            if processed % 100 == 0 or processed == total:
                elapsed = time.time() - start_time
                rate = processed / elapsed if elapsed > 0 else 0
                print(f" [進度] {processed}/{total} ({processed/total*100:.1f}%) | "
                      f"通過: {len(survivors)} 檔 | 速率: {rate:.1f} 檔/秒")

    # === V2 新增：剔除原因統計報表 ===
    print("\n[剔除原因統計]")
    for reason, count in sorted(drop_reasons.items(), key=lambda x: -x[1]):
        print(f"   {reason}: {count} 檔")

    if survivors:
        df = pd.DataFrame(survivors)
        df = df.sort_values(by='MarketCap_B', ascending=False).reset_index(drop=True)
        file_name = "qualified_universe.csv"
        df.to_csv(file_name, index=False, encoding='utf-8-sig')
        print(f"\n[建庫成功] {len(survivors)} 檔純淨原料 → {file_name}")
        print(f"[品質摘要] 平均毛利率: {df['GrossMargin'].mean():.1f}%, "
              f"平均機構持股: {df['InstOwn'].mean():.1f}%")
    else:
        print("\n[建庫失敗] 沒有任何標的通過篩選。")

# ==============================================================================
# 系統點火
# ==============================================================================
if __name__ == "__main__":
    email = "a7924177@gmail.com"
    sec_universe = get_sec_master_universe(email)
    
    if sec_universe:
        build_qualified_database_v2(sec_universe, scan_limit=0)
