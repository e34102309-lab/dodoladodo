import pandas as pd
import requests
import logging

logging.basicConfig(level=logging.INFO, format='%(message)s')

def fetch_sec_cik_mapping() -> pd.DataFrame:
    """
    底層機制：
    SEC 官方提供了一份動態更新的 JSON 檔案，映射了全美股 Ticker 與 CIK。
    這是絕對防線，確保我們不會因為 yfinance 的 ticker 命名差異（如 BRK.B vs BRK-B）
    而在後續 XBRL 萃取時抓錯報表。
    """
    logging.info(">>> 正在向 SEC 請求官方 Ticker-CIK 實體映射表...")
    url = "https://www.sec.gov/files/company_tickers.json"
    headers = {'User-Agent': 'QuantResearchProject a7924177@gmail.com'}
    
    resp = requests.get(url, headers=headers)
    if resp.status_code != 200:
        raise ConnectionError("SEC CIK 映射表請求失敗")
        
    data = resp.json()
    # SEC JSON 結構為 dict of dicts，需轉換為 DataFrame
    df_sec = pd.DataFrame.from_dict(data, orient='index')
    df_sec = df_sec.rename(columns={'ticker': 'Ticker', 'cik_str': 'CIK', 'title': 'Company'})
    df_sec['Ticker'] = df_sec['Ticker'].str.upper()
    return df_sec

def fetch_growth_indices() -> set:
    """
    底層機制：
    加入 storage_options 傳遞 User-Agent，突破 Wikipedia 的防爬蟲 403 封鎖。
    """
    tickers = set()
    
    # 宣告合法的 User-Agent 偽裝成瀏覽器
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    # 1. 抓取 NASDAQ-100 成分股
    logging.info(">>> 正在解析 NASDAQ-100 成分股...")
    url_ndx = "https://en.wikipedia.org/wiki/Nasdaq-100"
    # 修正點：加入 storage_options=headers
    tables_ndx = pd.read_html(url_ndx, storage_options=headers)
    df_ndx = next(df for df in tables_ndx if 'Ticker' in df.columns)
    tickers.update(df_ndx['Ticker'].tolist())
    
    # 2. 抓取 S&P 500 成分股，並嚴格限縮板塊
    logging.info(">>> 正在解析 S&P 500 科技與通訊板塊...")
    url_sp500 = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    # 修正點：加入 storage_options=headers
    tables_sp500 = pd.read_html(url_sp500, storage_options=headers)
    df_sp500 = tables_sp500[0]
    
    # 定義高成長目標板塊
    target_sectors = ['Information Technology', 'Communication Services']
    df_sp500_tech = df_sp500[df_sp500['GICS Sector'].isin(target_sectors)]
    
    # 修正 Wikipedia 中常見的點號命名
    sp500_tickers = df_sp500_tech['Symbol'].str.replace('.', '-', regex=False).tolist()
    tickers.update(sp500_tickers)
    
    return tickers

def build_growth_universe(output_file="growth_universe.csv"):
    df_sec = fetch_sec_cik_mapping()
    growth_tickers = fetch_growth_indices()
    
    logging.info(f">>> 初步取得 {len(growth_tickers)} 檔高成長/科技標的，進行 CIK 實體對齊...")
    
    # 進行交集映射
    df_universe = df_sec[df_sec['Ticker'].isin(growth_tickers)].copy()
    
    # 確保 CIK 是 10 位數字字串 (SEC API 規範)
    df_universe['CIK'] = df_universe['CIK'].astype(str).str.zfill(10)
    
    # 輸出
    df_universe = df_universe[['Ticker', 'CIK', 'Company']].sort_values('Ticker')
    df_universe.to_csv(output_file, index=False)
    logging.info(f">>> 成功建立成長衛星母體！共計 {len(df_universe)} 檔標的已寫入 {output_file}。")

if __name__ == "__main__":
    build_growth_universe()