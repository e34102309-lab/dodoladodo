# Alpha Engine V9：70/30 長期價值研究框架

本專案把投資組合拆成 70% ETF 核心與最多 30% 主動個股。程式的角色是建立可審計的研究漏斗，不是自動買入或保證提高報酬的訊號。

## 投資原則

- 只做多，不使用槓桿、期權或放空。
- 主動部位最多 30% 總資產，目標 8–12 檔。
- 單一公司最多 3% 總資產；高信心候選先從 1.5% 起始。
- 單一產業最多 9% 總資產，候選名單每產業最多 3 檔。
- 模型未找到合格公司時可以空手，不用硬湊名單。
- 買入前必須完成投資論點、熊市情境、失效條件與 ETF 重疊檢查。

完整規則見 `INVESTMENT_POLICY.md`，研究紀錄見 `investment_journal_template.md`。

## 主要檔案

- `total_market_hunter_v2.py`：本機偶爾執行的全市場初篩器。
- `qualified_universe.csv`：手動更新並上傳的候選宇宙，至少包含 `Ticker,CIK`。
- `AQR_ModeC_Agent_V12.py`：價值、品質、預期與下檔風險評分引擎。
- `run_mode_c_ai_agent.py`：對候選做連網證據查核與投資論點反證。

## 評分邏輯

長期綜合分數由四部分組成：

1. 價值：歷史 EV/EBITDA 分位與 Real FCF Yield。
2. 品質：ICR、自由現金流、毛利趨勢與股數稀釋。
3. 市場預期：反推 EBITDA 成長是否合理。
4. 風險：壓力測試下檔、價值陷阱、稀釋、資料品質與高軋空波動。

12 個月動能只占小幅輔助權重，不取代基本面。通過分數後仍須滿足正 Real FCF、ICR、估值歷史、合理預期及壓力測試等硬門檻。

## 執行

```bash
pip install -r ModeC_requirements.txt
export USER_EMAIL="your_email@example.com"
python AQR_ModeC_Agent_V12.py
```

`qualified_universe.csv` 由你在本機需要時更新；GitHub Actions 不會為 PR 重跑耗時的全市場資料抓取。

## 輸出

- `mode_c_screen.csv`：全部公司與落選原因。
- `mode_c_shortlist.csv`：產業分散後最多 12 檔的研究候選。
- `mode_c_report.md`：長期價值研究摘要。
- `mode_c_agent_payload.json`：交給連網 Agent 的反證任務包。
- `Mode_C_Final_Decision_Memo.md`：AI 二審研究備忘錄。

## 正確使用方式

1. 用全量 CSV 查落選原因與資料品質。
2. 只對 shortlist 做深入研究，不把排名直接當買入順序。
3. 用研究日誌寫下估值區間、反方論點與失效條件。
4. 檢查 ETF 重疊與產業曝險後，才決定是否建立小部位。
5. 每季檢查論點；只有論點或估值改變才調整，不因日常價格波動頻繁交易。
