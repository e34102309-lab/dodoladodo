# dodoladodo 專案工作規則

本文件適用於整個 repository。任何 coding agent 開始工作前，都應先閱讀本文件；若子目錄日後有更具體的 `AGENTS.md`，則以較接近目標檔案的規則為準。

## 1. 溝通方式

- 全程使用繁體中文，程式識別字與既有英文欄位名稱維持原樣。
- 用清楚、可驗證的方式說明財務與工程名詞，不把推測寫成事實。
- 修改前先簡述要讀取的範圍與預計修改內容；小工作不需要冗長計畫。
- 一次只處理使用者指定的工作，不自行擴大成策略重寫或大量重構。
- 使用者要求「適量測試」：依變更風險選擇最小充分驗證，不反覆重跑相同測試。

## 2. 開始工作前必讀

依任務需要閱讀：

1. `PROJECT_ACCOUNT_HANDOFF.md`：專案位置、分支、進度、排程與接手注意事項。
2. `CURRENT_STRATEGY_LOGIC_FOR_AI_REVIEW.md`：目前實際執行的完整股票篩選邏輯。
3. 目標模組及相鄰測試，不可只根據欄位名稱猜測行為。
4. 涉及 GitHub Actions 時閱讀 `.github/workflows/alpha_hunt.yml`。

專案是一套美股長期價值「研究漏斗」，不是自動交易器。Global Research Queue 是人工研究順序，不是買進訊號或保證報酬。

## 3. 不可破壞的資料原則

### Point-in-time

- SEC fact 必須滿足 `available_to_model_at <= decision_timestamp`。
- acceptance time 缺失時只能使用程式既有的保守 fallback，並降低資料信心。
- 不可用目前的 Yahoo metadata 冒充歷史時點資料。
- 任何衍生指標必須保留來源 evidence IDs，且 lineage 不得有循環。

### TTM

- 禁止把單季數據乘四年化。
- 優先沿用既有順序：完整年報、`FY + current YTD - prior YTD`、四個重建單季、明確 annual fallback。
- 核心 TTM 無法可靠建立時必須 `ABSTAIN`，不可補 0 或猜值。

### 缺值與狀態

- 缺值不等於 0；不得用 `value or 0` 掩蓋缺資料。
- 合法狀態只有：`VALID`、`ESTIMATED`、`MISSING`、`NOT_APPLICABLE`、`ABSTAIN`、`STALE`、`INVALID`。
- `FAIL`：資料足夠且確定違反硬條件。
- `ABSTAIN`：證據不足、過舊、映射不可靠、匯率鏈缺失或模型無法可靠判斷。
- `NOT_APPLICABLE`：該指標不適用此模型；值應為 null，不可當成 0 分。
- 新增或改動重要欄位時，同步檢查 metric metadata、evidence、CSV、Dashboard 與 validator。

### 單位與公式

- 金額若無特別說明，以十億美元 `USD B` 處理。
- 百分比欄位為百分點，不可混成小數比例。
- FCF yield 必須明確區分 Market Cap 與 EV 分母。
- SBC 缺失時不可假設為 0，也不可在 FCF、稀釋、資本配置與風險分數重複懲罰同一經濟效果。
- Maintenance CapEx 是估計區間，必須保留 lower／base／upper、方法與信心，不可呈現為已揭露事實。
- ROIC 優先使用平均投入資本；ending capital fallback 必須標記並降低信心。

## 4. 模型與排名規則

- 一般企業與特殊產業必須依 `mode_c_routing.py` 分流。
- 銀行、保險、REIT、utility、cyclical、lender、fee financial 等不得硬套一般企業 FCF／EV-EBITDA 邏輯。
- P&C 公司揭露 Combined Ratio 與 SEC proxy 必須分開；未調和 proxy 為 `ESTIMATED`，需要人工檢查，不能成為 Starter。
- 月度、季度與 TTM KPI 不可混用或冒充彼此。
- DSI 等因子不適用時使用 `NOT_APPLICABLE`，剩餘權重按既有規則重新正規化。
- 不同 `Industry_Model_Key` 的 Raw Score 不得直接做全市場排名。
- 先在最終模型內計算 percentile，再使用：

  `50 + n / (n + 20) * (raw_percentile - 50)`

- 收縮後 percentile 只供模型內強弱與樣本量診斷；Global Research Queue 使用 coverage-first model round-robin，每輪每模型最多取一檔，輪內以資料信心優先，不得再把 percentile 直接跨模型混排。
- 在有 point-in-time walk-forward 校準前，`Cross_Model_Calibration_Status` 維持 `UNCALIBRATED`，不得新增虛構的 Alpha 或 Expected Return 分數。
- `Starter_Candidate` 必須通過資料、公司 KPI、專用壓力測試與 portfolio-fit；高 Raw Score 本身不夠。

## 5. 檔案責任

- `total_market_hunter_v2.py`：每月第一層全市場 universe。
- `AQR_ModeC_Agent_V12.py`：SEC 資料重建與 Mode C 主流程。
- `mode_c_routing.py`：產業路由。
- `mode_c_industry_models.py`：特殊產業模型。
- `mode_c_research_priority.py`：同模型排名與研究佇列。
- `mode_c_evidence.py`：point-in-time evidence ledger。
- `mode_c_metric_contract.py`：指標狀態與 metadata 契約。
- `validate_mode_c_outputs.py`：輸出不變量與資料完整性驗證。
- `build_mode_c_dashboard.py`、`enhance_dashboard_ui.py`：靜態 Dashboard。
- `mode_c_fixture_pipeline.py`：不連外的固定資料整合驗證。
- `tests/`：回歸測試。

優先沿用既有模組邊界與 helper，不複製同一套財務公式到多個檔案。共用行為改動時，應在最接近責任歸屬的模組修正。

## 6. 修改原則

- 先閱讀現有程式與測試，再修改。
- 修改範圍保持集中，不順手改寫無關程式或生成資料。
- 不刪除既有功能、資料、輸出欄位或排程，除非使用者明確要求。
- 新增相依套件前先確認標準函式庫或現有套件不能完成，並說明必要性。
- 財務結構化資料優先用 parser／DataFrame／JSON API，不用脆弱的字串拼接。
- 新增指標時必須定義：來源、as-of 時間、單位、公式、適用模型、缺值狀態、信心與驗證規則。
- 不手動修改大型生成輸出來偽造成功；應修正來源程式後重新產生最小必要 fixture 或輸出。
- 保留既有繁體中文文件編碼為 UTF-8。

## 7. 成本與測試規則

### 預設不可自行執行的高成本流程

除非使用者明確要求更新市場資料，否則不要執行：

- 完整 `total_market_hunter_v2.py` 全市場抓取。
- 完整 `AQR_ModeC_Agent_V12.py` 網路研究流程。
- 重複下載 SEC／Yahoo 全市場資料。
- 只為確認 UI 而重建所有歷史研究輸出。

### 最小充分驗證

- 文件或排程註解：檢查 diff／語法即可，不必跑完整測試。
- 單一純函式：執行相關 test module。
- 共用 metric contract、routing、ranking 或財務公式：執行相關測試，完成前最多再跑一次完整 unit suite。
- 跨模組輸出契約：執行 `mode_c_fixture_pipeline.py` 與 validator。
- Dashboard 邏輯：使用 fixture 或既有資料重建，不要為此重抓市場。
- 只有高風險或跨模組修改才需要完整測試；不要在每個小修改後重跑全部 tests。

Windows 本機若存在可優先使用：

```powershell
& 'D:\dobird\.venv\Scripts\python.exe' -m unittest discover -s tests -q
& 'D:\dobird\.venv\Scripts\python.exe' mode_c_fixture_pipeline.py --output-dir fixture_output_check
```

若該環境不存在，先尋找目前可用 Python；不要為小任務重建整套環境。

## 8. GitHub Actions 與排程

- 每週四 `22:00 UTC`：美股盤後執行 Mode C。
- 每月 1 日 `03:00 UTC`：全市場 universe refresh 後執行 Mode C。
- push／pull request 只應執行便宜的 compile 與 unit tests。
- 不得讓 partial hunter 輸出覆蓋上一份完整 `qualified_universe.csv`。
- Workflow 修改後檢查 `schedule`、`workflow_dispatch`、permissions、artifact 路徑與 timeout。
- `USER_EMAIL` 由 GitHub Actions secret 提供；缺少時應明確停止，不得使用個人信箱 fallback，也不得把 token、API key 或新密碼寫入 repository。

## 9. Git 安全

- 工作樹可能有使用者尚未提交的變更；不可回復、覆蓋或刪除不是自己建立的內容。
- 禁止使用 `git reset --hard`、強制 push 或破壞性 checkout，除非使用者明確要求並理解影響。
- 本機與 GitHub App 建立的 commit 即使名稱相同也不可假設內容等價；必須比較 tree／diff 與關鍵檔案大小，不可只看 SHA 或 commit message。
- 已知遠端 `codex/metric-integrity-audit-v2` 的舊提交 `52ff824` 有多個約 30 KB 的截斷檔案；修復前禁止把該遠端 tip 直接合併到 `main`。
- 不自行 commit、push、merge 或建立 PR，除非使用者要求。
- 推送前確認 branch、變更範圍、測試結果與遠端是否有新提交。

## 10. 完成回報

完成後簡潔回報：

1. 修改內容與原因。
2. 修改的檔案。
3. 實際執行的檢查與結果。
4. 沒有執行的高成本流程。
5. 已知限制、風險或仍需使用者決定的事項。
6. 是否已 commit／push，以及 branch／commit（只有實際完成時才宣稱）。

不得宣稱：

- 未執行的測試已通過。
- 離線 fixture 等同最新市場結果。
- Research Queue 是買進建議或預期報酬。
- 尚未回測的策略已證明能產生 Alpha。
