# Mode C 三階段雙殺 Agent 使用說明

## 1. 檔案
- `AQR_ModeC_Agent_V12.py`：修正版主程式。
- `qualified_universe.csv`：每日 Python 硬篩輸入，至少要有 `Ticker,CIK` 兩欄。

## 2. 執行
```bash
pip install -r ModeC_requirements.txt
export USER_EMAIL="your_email@example.com"
python AQR_ModeC_Agent_V12.py
```

## 3. 輸出
- `mode_c_screen.csv`：每檔標的結構化數據。
- `mode_c_report.md`：中文投資決策書。
- `mode_c_agent_payload.json`：交給連網 LLM / Web Agent 做二次驗證的任務包。

## 4. 建議 Agent 架構
1. Hard Screener：每日產出 `qualified_universe.csv`。
2. Mode-C Quant Engine：執行 TTM、Real FCF Yield、ICR、實質回購、雙殺壓力測試、三季毛利、Short Interest、DSI、催化劑。
3. Web Evidence Agent：讀取 `mode_c_agent_payload.json`，逐檔查最新 10-K/10-Q footnotes、供應鏈物理限制、Short Interest 官方資料、30 天事件。
4. PM Decision Agent：把 Quant Engine 與 Web Evidence 合併，輸出最終交易建議。
