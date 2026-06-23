# Alpha Engine V9：QQQ 40% / VOO 30% / 主動個股最多 30%

本專案用 ETF 承擔核心市場報酬，主動個股只作有紀律的研究與提高長期報酬機會。程式是可審計的研究漏斗，不是自動買入或保證提高年化率的訊號。

## 投資原則

- QQQ 目標 40%、VOO 目標 30%、主動個股 0%～30%。
- 沒有好標的時不必買滿 30%，其餘可留在 ETF 或現金。
- 主動持股通常 8～12 檔，起始 1%～1.5%，單股最多 3%，單一主動產業最多 9%。
- 只做多，不使用槓桿、期權或放空。
- 金融股暫不納入，因本模型的 OCF、CapEx、Real FCF、Debt/EBITDA 指標不適用銀行、保險、券商及資產管理公司。
- 買入前必須完成投資論點、反方、失效條件、三情境、最新財報、稀釋、資本配置與 ETF 重疊檢查。

完整規則見 `INVESTMENT_POLICY.md`，研究紀錄見 `investment_journal_template.md`。

## 主要檔案

- `total_market_hunter_v2.py`：本機執行、低請求量且可斷點續跑的全市場初篩器。
- `qualified_universe.csv`：手動更新並上傳的候選宇宙，至少包含 `Ticker,CIK`。
- `AQR_ModeC_Agent_V12.py`：價值、品質、預期、資本配置與下檔風險評分引擎。
- `build_mode_c_dashboard.py`：把評分結果整理成不依賴外部 AI 的靜態研究網站。
- `run_mode_c_ai_agent.py`：保留為選用工具，不再由主要 GitHub Actions 自動呼叫。

## 本機全市場初篩

PowerShell 先測試 20 檔：

```powershell
& D:\dobird\.venv\Scripts\python.exe D:\dobird\total_market_hunter_v2.py --email "your_email@gmail.com" --output-dir D:\dobird\hunter_output --scan-limit 20
```

測試成功後跑完整市場：

```powershell
& D:\dobird\.venv\Scripts\python.exe D:\dobird\total_market_hunter_v2.py --email "your_email@gmail.com" --output-dir D:\dobird\hunter_output
```

重要行為：

- 初篩市值至少 50 億美元。
- 最近一年 OCF 必須為正。
- 毛利率 25% 以上直接通過；15%～25% 至少要有 5 個同業樣本，且不低於同業中位數；低於 15% 排除。
- Debt/EBITDA <=4 通過、4～5 警示、>5 排除，另標記淨現金公司。
- Yahoo 詳細資料循序抓取，預設每次至少間隔 2.5 秒，不使用多執行緒轟炸。
- 中斷、斷線或限流後重跑相同指令即可接續；不要加 `--fresh`。
- 未完成時只更新 `*.partial.csv`，不覆蓋上次完整 `qualified_universe.csv`。
- `hunter_audit.csv` 保留全部通過、淘汰與待查原因。

## Mode C 評分邏輯

長期綜合分數：

- 品質 35%：ICR、Real FCF、ROIC、毛利趨勢與五年現金流穩定性。
- 價值 30%：歷史 EV/EBITDA 分位與 Real FCF Yield。
- 市場預期差 20%：反推 EBITDA 成長是否合理。
- 動能 10%，僅作輔助。
- 資本配置 5%：回購是否真正降低股數、增發與持續稀釋。
- 再扣除壓力測試、資料品質、價值陷阱與其他風險分。

新增深篩包括近三年累計 OCF、近五年 Real FCF 正值年數與 margin 穩定性、OCF/Net Income、Real FCF/Net Income、ROIC、ROCE，以及一年與三年股數變化。單一年稀釋改為扣分；近三年股數累計增加超過 3% 才視為持續稀釋並排除。

市場隱含 EBITDA CAGR 採分級而非假裝精準：-10%～15% 高分、15%～25% 可接受、25%～30% 警示、>30% 排除、<-10% 進入價值陷阱覆核。

## 分數用途

- 60 分以上：研究候選。
- 70 分以上：優先研究。
- 75 分以上：完成研究後可考慮 1% 小部位。
- 80 分以上：較高優先度，可考慮 1.5% 起始部位。
- 若為 QQQ/VOO 最新前十大，至少 80 分才可考慮額外主動加碼。

加碼至少要等一次財報，確認 thesis、Real FCF、股數及估值未惡化。分數跌破 60、Real FCF 轉負、ICR<3、連兩季營收與毛利惡化、明顯稀釋、資本配置失控、thesis 被證偽或估值過高時，必須強制檢討。

## 執行 Mode C

```bash
pip install -r ModeC_requirements.txt
export USER_EMAIL="your_email@example.com"
python AQR_ModeC_Agent_V12.py
python build_mode_c_dashboard.py
```

本機產生的網站位於 `public/index.html`。`qualified_universe.csv` 由你在本機需要時更新；GitHub Actions 不會為 PR 重跑耗時的全市場初篩。

## 靜態研究網站

主要工作流程完成量化分析後，永遠會上傳 GitHub Actions artifact；如果 repository 是 public 且 GitHub Pages 已設定為 `GitHub Actions` 來源，還會自動部署線上網站。

- Private repo / 免費帳號：下載 `Alpha_Engine_Static_Dashboard` artifact，解壓縮後打開 `index.html`。
- Public repo / GitHub Pages 啟用：直接刷新線上網站。

線上網站網址：

`https://e34102309-lab.github.io/dodoladodo/`

artifact 下載方式：

1. 到 GitHub repository 的 `Actions`。
2. 打開最新一次 `Mode-C Long-Term Value Research Pipeline`。
3. 在頁面下方 `Artifacts` 下載 `Alpha_Engine_Static_Dashboard`。
4. 解壓縮 zip。
5. 直接打開 `index.html`。

網站功能：

- 搜尋、分數門檻、Shortlist、合格名單與自訂追蹤清單。
- 供應鏈二階雷達：把 AI 晶片、資料中心電力與散熱、電氣化、AI 軟體與資安、高品質醫療等主題拆成受益鏈，協助安排研究順序。
- 點擊主題卡片可篩出相關公司，例如從 AI 晶片延伸到半導體設備、記憶體、電源管理、散熱、連接器、被動元件與測試量測。
- 點擊股票查看品質、價值、Real FCF、ICR、ROIC、ROCE、稀釋、估值與壓力測試。
- 直接開啟 SEC 官方公司申報頁及 Yahoo 財務資料頁。
- 一鍵複製固定格式的 AI 研究提示，再貼到你慣用的 AI 手動查核；提示會要求判斷該公司是一階、二階或三階受益者，還是只是被題材蹭到。
- 追蹤名單只保存在目前瀏覽器的 `localStorage`，不會公開或上傳，並可匯出文字檔。

主要流程不再需要 `GEMINI_API_KEY`，也不會因 Gemini 503 高需求錯誤而讓本批研究失敗。`Mode_C_Long_Term_Value_Outputs` artifact 仍會保留完整 CSV、Markdown、JSON 與 `public` 網站資料；`Alpha_Engine_Static_Dashboard` 則是給你最快打開網站用的精簡 artifact。

## 輸出

- `mode_c_screen.csv`：全部公司與落選原因。
- `mode_c_shortlist.csv`：產業分散後最多 12 檔研究候選。
- `mode_c_report.md`：長期價值研究摘要。
- `mode_c_agent_payload.json`：供手動 AI 研究或其他工具使用的九項反證任務包。
- `public/index.html`：可直接開啟的研究網站。
- `public/data.json`：網站使用的完整結構化資料。
