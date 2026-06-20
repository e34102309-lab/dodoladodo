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
- `run_mode_c_ai_agent.py`：對候選做連網證據查核、ETF 重疊確認、每週去重與投資論點反證。

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
- 動能 10%：只作輔助。
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
```

`qualified_universe.csv` 由你在本機需要時更新；GitHub Actions 不會為 PR 重跑耗時的全市場初篩。

## 輸出

- `mode_c_screen.csv`：全部公司與落選原因。
- `mode_c_shortlist.csv`：產業分散後最多 12 檔研究候選。
- `mode_c_report.md`：長期價值研究摘要。
- `mode_c_agent_payload.json`：交給連網 Agent 的九項反證任務包。
- `Mode_C_Final_Decision_Memo.md`：AI 二審研究備忘錄。

AI 報告使用純文字繁體中文，會移除 emoji、異常 Unicode 與裝飾符號。同一股票在同一 ISO 週最多分析一次；再次執行時依排名改選本週尚未分析的其他候選，每次最多 5 檔。
