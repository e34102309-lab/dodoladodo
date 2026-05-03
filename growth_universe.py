"""
=============================================================================
GROWTH UNIVERSE GENERATOR v2.0 (高階成長股狩獵區)
=============================================================================
戰略轉移：
1. 捨棄充滿生技盲盒與微型股的 S&P 600，轉向 S&P 500 與 Nasdaq 100。
2. 內建 MEGA_CAPS_EXCLUDE 濾網，自動剔除成長已經平庸化的超級巨頭。
3. 內建 ELITE_SAAS_WATCHLIST，強制納入高純度軟體/資安/雲端標的進行壓力測試。
=============================================================================
"""
import pandas as pd
import requests
import logging

# 設定日誌格式
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')

# ── 戰略濾網 1：手動排除名單 (剔除已被過度定價或進入低速成長期的巨頭) ──
MEGA_CAPS_EXCLUDE = {
    'AAPL', 'MSFT', 'NVDA', 'GOOGL', 'GOOG', 'AMZN', 'META', 'TSLA', 
    'BRK.B', 'BRK-B', 'LLY', 'AVGO', 'V', 'JPM', 'UNH', 'MA', 'PG', 'JNJ', 
    'HD', 'MRK', 'COST', 'ABBV', 'CRM', 'AMD', 'NFLX', 'ORCL', 'CSCO'
}

# ── 戰略濾網 2：強制注入名單 (高質量 SaaS、資安、雲端基礎設施) ──
ELITE_SAAS_WATCHLIST = {
    'CRWD', 'PLTR', 'DDOG', 'NET', 'MNDY', 'SNOW', 'ZS', 'PANW', 
    'FTNT', 'NOW', 'VEEV', 'MDB', 'TEAM', 'HUBS', 'CFLT', 'PATH', 'IOT',
    'APP', 'DUOL', 'CELH'
}

def fetch_sec_cik_mapping() -> pd.DataFrame:
    """底層機制：獲取 SEC 官方 Ticker-CIK 實體映射表"""
    logging.info(">>> 向 SEC 請求官方 Ticker-CIK 實體映射表...")
    url = "https://www.sec.gov/files/company_tickers.json"
    headers = {'User-Agent': 'QuantResearchProject a7924177@gmail.com'}
    
    resp = requests.get(url, headers=headers)
    if resp.status_code != 200:
        raise ConnectionError("SEC CIK 映射表請求失敗")
        
    data = resp.json()
    df_sec = pd.DataFrame.from_dict(data, orient='index')
    df_sec = df_sec.rename(columns={'ticker': 'Ticker', 'cik_str': 'CIK', 'title': 'Company'})
    df_sec['Ticker'] = df_sec['Ticker'].str.upper()
    return df_sec

def fetch_growth_indices() -> set:
    """底層機制：鎖定 S&P 500 成長板塊與 Nasdaq 100"""
    tickers = set()
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    target_sectors = ['Information Technology', 'Communication Services', 'Health Care']
    
    try:
        # 1. 抓取 S&P 500 (尋找已經跨越死亡之谷的中大型股)
        logging.info(">>> 解析 S&P 500 (精準打擊科技/通訊/醫療板塊)...")
        url_sp500 = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        tables_sp500 = pd.read_html(url_sp500, storage_options=headers)
        df_sp500 = tables_sp500[0]
        df_sp500_growth = df_sp500[df_sp500['GICS Sector'].isin(target_sectors)]
        sp500_tickers = set(df_sp500_growth['Symbol'].str.replace('.', '-', regex=False).tolist())
        tickers.update(sp500_tickers)
        
        # 2. 抓取 Nasdaq 100 (純度更高的科技與創新板塊)
        logging.info(">>> 解析 Nasdaq 100...")
        url_ndx = "https://en.wikipedia.org/wiki/Nasdaq-100"
        tables_ndx = pd.read_html(url_ndx, storage_options=headers)
        
        # 動態尋找包含 Ticker 的表格
        df_ndx = None
        for tbl in tables_ndx:
            if 'Ticker' in tbl.columns:
                df_ndx = tbl
                ticker_col = 'Ticker'
                break
            elif 'Symbol' in tbl.columns:
                df_ndx = tbl
                ticker_col = 'Symbol'
                break
                
        if df_ndx is not None:
            ndx_tickers = set(df_ndx[ticker_col].str.replace('.', '-', regex=False).tolist())
            tickers.update(ndx_tickers)
        else:
            logging.warning("⚠️ 無法從 Wikipedia 解析 Nasdaq 100 表格結構。")
            
    except Exception as e:
        logging.error(f"解析指數列表失敗: {e}")

    # 3. 執行戰略清洗：剔除飽和巨頭，注入精銳部隊
    original_len = len(tickers)
    tickers = tickers - MEGA_CAPS_EXCLUDE
    logging.info(f">>> 濾網啟動：剔除 {original_len - len(tickers)} 檔飽和超級巨頭 (Mega-Caps)。")
    
    tickers.update(ELITE_SAAS_WATCHLIST)
    logging.info(f">>> 增援抵達：強制注入 {len(ELITE_SAAS_WATCHLIST)} 檔 Elite SaaS/高成長觀察名單。")
    
    return tickers

def build_growth_universe(output_file="growth_universe.csv"):
    df_sec = fetch_sec_cik_mapping()
    growth_tickers = fetch_growth_indices()
    
    logging.info(f">>> 初步取得 {len(growth_tickers)} 檔中大型成長候選股，進行 CIK 實體對齊...")
    
    df_universe = df_sec[df_sec['Ticker'].isin(growth_tickers)].copy()
    df_universe['CIK'] = df_universe['CIK'].astype(str).str.zfill(10)
    df_universe = df_universe[['Ticker', 'CIK', 'Company']].sort_values('Ticker')
    
    df_universe.to_csv(output_file, index=False)
    logging.info(f">>> ✅ 成功升級成長衛星母體！共計 {len(df_universe)} 檔標的已寫入 {output_file}。")

if __name__ == "__main__":
    build_growth_universe()