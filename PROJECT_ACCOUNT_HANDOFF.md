# dodoladodo 專案帳號交接文件

> 最後更新：2026-09-06（Asia/Taipei）
> 用途：更換 Codex／GitHub 帳號後，讓新的 AI 工作階段能從本文件直接接手。
> 本文件記錄的是工程狀態與研究流程，不是投資建議，也不代表策略已經通過無偏回測。

## 1. 三分鐘接手摘要

1. 本機專案：`D:\使用者\DoBird\Favorites\Documents\股票\dodoladodo-work`
2. GitHub：`https://github.com/e34102309-lab/dodoladodo`
3. 主要工作分支：`codex/metric-integrity-audit-v2`
4. 本機 HEAD：`e98fffe Reconcile truncated remote branch with complete local tree`；其上另有 2026-09-04 至 2026-09-06 尚未提交的五段策略稽核修正，禁止重置工作樹。
5. 完整策略修正提交：`0fb550d Harden Mode C strategy logic and decision contracts`；已包含 8 月份的累積修正。
6. 2026-08-30 已以非 force push 將完整樹 `e98fffe` 推到 GitHub 工作分支，修復舊 tip `52ff824` 的截斷檔案。尚未合併 main；本輪未重新連線查遠端，也未 commit／push。
7. 完整策略說明：`CURRENT_STRATEGY_LOGIC_FOR_AI_REVIEW.md`；本輪逐段紀錄：`STRATEGY_FIVE_STAGE_AUDIT.md`。
8. 自動排程：每週四美股盤後跑 Mode C；每月 1 日另重抓全市場第一層 universe。
9. 2026-09-06 五段工程驗證：完整 suite 215 項中 214 項先通過，修正唯一過期原始碼字串斷言後該項定點通過；離線 fixture validator 的 `invalid_zero = 0`。詳見第 9 節。
10. GitHub Actions 最近確認的週排程於 2026-08-20 成功完成；本機未重跑高成本全市場流程。

新帳號接手後，先執行：

```powershell
Set-Location 'D:\使用者\DoBird\Favorites\Documents\股票\dodoladodo-work'
git status --short --branch
git remote -v
git log --oneline --decorate -5
```

不要因為本機與遠端 commit SHA 不同就重置檔案。必須先比較檔案樹、實際 diff 與檔案大小；舊截斷提交已修復，仍不可用舊 tip 覆蓋完整版本。

### 本輪新增修正（2026-09-04 至 2026-09-06，尚未提交）

- SEC YTD 相減與 annual/YTD 橋接改查實際起訖日；四季／八季視窗必須連續，接受 52／53 週財年，拒絕錯置比較期與缺季。
- Maintenance CapEx 營收成長、三季毛利、DSI、DSO／DPO 與 working-capital growth gap 共用期間檢查；當期餘額必須對齊流量期末，最新資料缺失不能回退舊訊號冒充最新。
- Calibration input 拒絕正負無限大；portfolio input 拒絕決策同日較晚資料、未知 ETF overlap 與缺失風險分組。兩項 input contract 升為 2026-09 v3。
- 初篩修正負毛利率被當缺值、金融 sector 搶先攔截 REIT，以及 NaN 類別字串誤路由；Hunter policy 升為 v8。
- 年度流量只接受年度表單且實際期間 330 至 380 天；特殊產業成長率要求連續季度或相鄰年度，歷史 FCF 錨定最新已報告年度。
- 排除商譽 ROIC 改用同一平均／期末基礎；稀釋總分影響改依可用因子權重歸因。
- BANK 壓力新增 `us-gaap:RiskWeightedAssets`，Tier 1 損失使用 RWA 而非總資產作分母；RWA 缺失、非正或錯期一律 `ABSTAIN`。已確認壓力失敗優先保留 `FAIL`。
- Starter 必須有明確 portfolio-fit PASS，未知風險布林不得當 false；validator 同步重算銀行壓力與稀釋公式。
- 五段最終驗證涵蓋 215 個單元測試；完整執行 214/215，修正唯一過期靜態斷言後定點通過。Fixture 輸出在 `fixture_output_five_stage_20260904/`，屬離線測試資料。
- 未調整未經回測的權重、未執行全市場抓取、未改排程、未新增自動續跑；8 月完成後的 heartbeat 已移除。
- 下一步：經使用者授權後再檢查最新遠端、提交本輪修正並處理 main 整合；策略研究仍以 PIT walk-forward 與資料覆蓋率稽核為優先。

## 2. 專案定位

這是一套美股長期價值研究漏斗，不是自動交易器。主要工作是：

- 每月建立大型美股候選 universe。
- 每週用 SEC XBRL point-in-time 資料進行深度研究。
- 依產業模型分流，避免用同一套 FCF／EBITDA 邏輯評估銀行、保險、REIT 等企業。
- 把資料不足、真正不合格與不適用因素分開處理。
- 在各模型內計算研究優先順序，再產生最多 12 檔的 Global Research Queue。
- 輸出 CSV、evidence ledger、validator 結果與靜態 Dashboard，供人工研究。

系統不會自動買賣，也尚未完成完整 point-in-time walk-forward 回測、交易成本模擬或組合最佳化。

## 3. 核心檔案地圖

| 檔案 | 責任 |
|---|---|
| `total_market_hunter_v2.py` | 每月全市場第一層初篩、證券種類辨識、產生 `qualified_universe.csv` |
| `AQR_ModeC_Agent_V12.py` | Mode C 主流程、SEC TTM 重建、一般企業評分、壓力測試與輸出 |
| `mode_c_routing.py` | sector／industry 路由與特殊模型分流 |
| `mode_c_industry_models.py` | 銀行、保險、REIT、utility、cyclical、fee financial 等專用模型 |
| `mode_c_research_priority.py` | 同模型 percentile、小樣本收縮、Global Research Queue |
| `mode_c_evidence.py` | point-in-time evidence ledger 與來源 lineage |
| `mode_c_metric_contract.py` | `VALID/ESTIMATED/MISSING/NOT_APPLICABLE/ABSTAIN/STALE/INVALID` 資料契約 |
| `validate_mode_c_outputs.py` | 輸出、公式、狀態、零值、Dashboard 一致性驗證 |
| `build_mode_c_dashboard.py` | 建立 `public/data.json` 與 Dashboard |
| `enhance_dashboard_ui.py` | 增強 `public/index.html` 的 UI 與研究欄位 |
| `mode_c_fixture_pipeline.py` | 不連外的固定資料整合測試流程 |
| `run_mode_c_ai_agent.py` | 可選的 AI 報告流程；核心選股不依賴它才能完成 |
| `.github/workflows/alpha_hunt.yml` | GitHub Actions 測試、排程、輸出 artifact 與 GitHub Pages |
| `CURRENT_STRATEGY_LOGIC_FOR_AI_REVIEW.md` | 目前最完整的策略邏輯文件，交給外部 AI 審查時優先使用 |

## 4. 端到端流程

```text
Yahoo 美國股票候選
  -> 證券種類／交易所檢查
  -> 每月第一層全市場初篩
  -> qualified_universe.csv
  -> 產業模型路由
  -> SEC point-in-time facts 與 TTM 重建
  -> 一般企業或特殊產業模型
  -> FAIL／ABSTAIN／PASS 與資料信心
  -> 同模型 percentile 與小樣本收縮
  -> Global Research Queue（最多 12 檔）
  -> CSV／evidence ledger／validator／Dashboard
```

TTM 流量不可把單季乘四。優先使用：

1. 最新完整年報。
2. `最近年報 + 本期 YTD - 去年同期 YTD`。
3. 由 YTD 差額重建單季並合計最近四季。
4. 無法重建才使用明確標記的 annual fallback。
5. 核心證據不足、過舊或 lineage 不完整時 `ABSTAIN`。

## 5. 已完成的重要修正

### 5.1 跨產業排名

- 不再直接混排不同模型的 Raw Score。
- 先在最終 `Industry_Model_Key` 內算 percentile。
- 使用 `50 + n/(n+20) * (raw_percentile - 50)` 做小樣本收縮。
- Global Research Queue 採 coverage-first model round-robin；每輪每模型最多一檔，輪內以資料信心、模型內 percentile、model key、ticker 決勝。
- `Cross_Model_Calibration_Status = UNCALIBRATED`。
- Global Research Queue 是研究順序，不是預期報酬排名。
- Starter 必須另外通過 KPI、壓力測試與 portfolio-fit。

### 5.2 P&C 保險

- 公司揭露 Combined Ratio 與 SEC proxy 分開保存。
- 未調和的 SEC proxy 為 `ESTIMATED`，需要人工 KPI 檢查，不能成為 Starter。
- 月度、季度、TTM 不混用；月度數值只保留為月度證據。
- 加入保費成長、保單成長、準備金發展、巨災損失、資本與 ROE。
- 壓力測試使用 Combined Ratio `+3/+6/+10pp`，並測試保費、投資收益率與資本存活。
- 壓力資料不足時是 `ABSTAIN`，不是 0，也不是自動 `FAIL`。

### 5.3 Maintenance CapEx 與 FCF

- 不再只提供單點 Maintenance CapEx，改為 lower／base／upper 範圍與信心等級。
- D&A 與近三年資料作為維護支出的錨點，並依營收成長調整超額 CapEx。
- 同時輸出 Maintenance Real FCF 與扣除全部 CapEx 的 Conservative Real FCF。
- FCF yield 明確區分 Market Cap 與 EV 分母。
- validator 會從 OCF、CapEx、SBC 與分母重新計算，防止欄位漂移。

### 5.4 SBC 與稀釋

- SBC economic cost 在 Real FCF 端處理。
- 股數稀釋、資本配置與 persistent dilution hard gate 各自有明確角色。
- 已加入 double-count 檢查，避免同一 SBC 在多個分數重複扣分。
- 缺少 SBC 證據時不能假設為 0。

### 5.5 ROIC 與估值

- ROIC 優先使用期初、期末平均投入資本。
- 只有缺資料時才退回 ending capital，並標記 `ESTIMATED`、降低信心。
- 同時保留 including goodwill 與 excluding goodwill 口徑。
- 歷史估值固定最多 10 年，依有效年度數使用 P25／P20／P15，不再宣稱不可達成的 15 年／P10 樣本。
- Exit multiple 使用公司歷史 40%、同業 40%、利率調整 20%。Peer pool 只納入當期 PASS 公司，至少 3 家同 industry，否則至少 5 家同 sector；再不足就只用公司自身歷史，不跨產業混用 GENERAL_CORPORATE 中位數。

### 5.6 因子適用性與特殊產業

- DSI 對銀行、保險、REIT、軟體、平台等不適用模型標記 `NOT_APPLICABLE`，不給 0 分。
- 可用因子權重會重新正規化，並輸出 coverage。
- Asset Management 已分 Alternative、Traditional、Insurance-linked 與 Other。
- AUM、FRE、flows 等非標準 KPI 缺失時不猜數字。

### 5.7 FAIL 與 ABSTAIN

- `FAIL`：資料完整，且確定違反硬性條件。
- `ABSTAIN`：資料不足、過舊、映射不可靠、匯率鏈缺失或壓力測試無法成立。
- 已加入 reason codes，例如 `INSUFFICIENT_QUARTERLY_EVIDENCE`、`UNRECONCILED_PROXY`、`SPECIALIZED_STRESS_NOT_AVAILABLE`。

### 5.8 2026-08-28 選股公式完整性修正（已納入 0fb550d）

- 第一層 Yahoo 資料改為交叉確認：單一 OCF 或 EBITDA 非正先交 SEC 複核，兩者同時非正才淘汰；缺少 cash 不再假設為 0 後誤判淨槓桿。
- 壓力估值倍數不得高於當前倍數，避免所謂壓力情境反而靠 multiple expansion 減少跌幅。
- Quality 的 FCF 子分數改用五年 `Real FCF / Net Income`，當期 FCF yield 只留在 Value 與 eligible，避免重複計分。
- 資本配置比較 issuance 與 gross buyback；回購／發行或股數證據缺失時為 `N/A` 並降低 coverage，不再默認 60 分。
- 三季毛利結構風險保留 Quality 反映與一次 `-15`，不再同時作硬排除；營收與毛利雙重惡化仍直接排除，但不再重複追加同源 Risk Penalty。
- P&C 可直接使用公司揭露 Combined Ratio；壓力資本損失改為稅後並同步減少 assets，嚴重情境的保費成長一定不優於中度情境。
- Mortgage REIT 以 recurring earnings 作獲利與 ROE 閘門，GAAP mark-to-market 損失只作警示。
- Fee financial 不再用 tangible book 排除資產輕公司，改用 net margin；費用型金融與資產管理的淨負債比率只允許正 EBITDA／FRE 作分母，負分母不得反向變成高分。
- Short squeeze 在沒有可靠 point-in-time 借券資料前保持分數中性，只作事件與波動旗標。

### 5.9 2026-08-29 邊界條件、產業壓力與研究佇列修正（已納入 0fb550d）

- Growth CapEx 不再因「營收未增且 CapEx/D&A >=1.5」單一訊號直接淘汰，改為 `CLEAR/MONITOR/WATCH/HIGH_RISK`；只有連續兩年營收下降、ROIC <8%、全額 CapEx 後 FCF 非正三項中至少兩項佐證時才 `FAIL`，一項佐證只扣一次 8 分。
- 十三種特殊產業統一輸出 specialized stress 狀態、情境、存活、缺失輸入與原因；BANK、壽險、兩類 REIT、utility、cyclical、lender、fee financial 與四種 asset manager 均已有財報型壓力公式。資料不足使用 `Specialized_Stress_Pending`，已確認壓力失敗另用 `Specialized_Stress_Failed`。
- 特殊產業要成為 eligible，除了模型分數與資料信心，現在必須 `Specialized_Stress_Status = PASS`；壓力資料不足為 `ABSTAIN`，壓力存活失敗為 `FAIL`。
- Global Research Queue 不再把收縮後百分位直接跨模型排序，改為 coverage-first model round-robin：模型內先排序，每輪每模型最多取一檔，輪內以資料信心優先。
- `Research_Priority_Round`、Growth CapEx 風險欄位與特殊壓力欄位已同步 CSV、Dashboard、fixture 與 validator。
- 這些壓力是可稽核財報情境，不冒充 Fed／NAIC／公司正式監管壓力測試；跨模型仍維持 `UNCALIBRATED`。

### 5.10 2026-08-29 OCF 品質、併購稀釋與決策輸入契約（已納入 0fb550d）

- 一般企業新增 DSO、DPO、CCC、AR 對 Revenue 增長差、AP 對 COGS 增長差與 deferred revenue 診斷；增長差還必須有至少 5 天 DSO／DPO 實質曝險。單一不利訊號為 `WATCH/-5`，兩項為 `HIGH_RISK/-10`，缺值不當 0，也不假裝 `CLEAR`。
- 稀釋分析新增 gross buyback／stock issuance bridge 與直接 XBRL acquisition stock consideration。直接股票對價本身即可觸發併購歸因；股票發行現金流只作 reconciliation，避免非現金換股被錯判。Persistent dilution 若有直接併購發股證據改為 `ABSTAIN` 等待 accretion review；證據不足仍不能自動歸因或豁免。
- 新增 `mode_c_decision_inputs.py`。Calibration audit 會阻擋 look-ahead、未成熟或混合 horizon 的 forward label、無法由 security／benchmark 重算的 excess return、survivorship bias、未含交易成本與樣本不足；通過只代表資料可進模型擬合，不代表完成校準。
- 可用 `MODE_C_PORTFOLIO_FIT_FILE` 輸入 point-in-time 持倉與風險資料，檢查單股、主動 sleeve、sector、economic-risk、ETF top-10 與 correlation stress；單股上限使用「直接持倉 + ETF look-through + 擬新增部位」，validator 會重算資料日齡及 pre/post-trade 曝險橋接。沒有輸入時保持 pending，不會自動產生 Starter candidate。
- Calibration input 必須使用一致 benchmark；同一 benchmark 與 forward-return 起訖窗口的 benchmark return 也必須一致，避免不可比較或遭污染的 excess-return label 進入跨模型擬合。
- 一般企業決策已明定 reason precedence：ICR、三年 OCF、未歸因持續稀釋與營收／毛利雙重惡化等獨立硬失敗，不會再被低信心 Maintenance CapEx 的 `ABSTAIN` 覆蓋；只有依賴該估計的 FCF 判斷會暫不判斷。
- 指標契約升級為 `2026-08-metric-status-v5`，CSV、evidence、報告、Dashboard、fixture 與 validator 已同步。

### 5.11 2026-09-06 五段策略稽核（尚未提交）

- 完整紀錄在 `STRATEGY_FIVE_STAGE_AUDIT.md`，依初篩路由、財報期間、公式評分、風險排序、輸出驗證五段執行。
- 初篩與深篩現在共用更嚴格的缺值語意；有效的負值不再被當成 missing，無效分類也不會默認走一般企業。
- 完整年度、連續季度與最新年度錨點都有明確實際日期條件，避免 `FY` 標籤、缺季或缺最新 SBC 造成期間誤用。
- 銀行壓力的 Tier 1 分母修正為 RWA，並由 validator 獨立勾稽；固定 RWA、3% 貸損與 21% 稅盾仍只是研究假設，不是監管壓測。
- 所有本輪變更仍在工作樹，尚未 commit、push 或合併 main；沒有執行高成本即時市場抓取。

## 6. Dashboard 與主要輸出

主要輸出：

- `mode_c_screen.csv`：完整研究母表。
- `mode_c_shortlist.csv`：Global Research Queue，最多 12 檔。
- `mode_c_shortlist_by_model.csv`：各模型內研究名單。
- `mode_c_evidence_ledger.csv`：來源事實、時間與衍生 lineage。
- `mode_c_zero_audit.json`：重要零值分類，`invalid_zero` 必須為 0。
- `mode_c_report.md`：文字研究摘要。
- `mode_c_agent_payload.json`：供 AI 研究使用的結構化輸出。
- `public/data.json`：Dashboard 資料。
- `public/index.html`：可直接查看的靜態 Dashboard。

Dashboard 預設使用 coverage-first round-robin 產生的 `Research_Priority_Rank` 排序；shrunk within-model percentile 只作模型內診斷。畫面另顯示模型樣本數、資料信心、KPI coverage、缺失原因、壓力狀態、人工任務與資料版本。不同模型顯示不同核心 KPI。

2026-07 最後一次本機離線重建時：

- Universe rows：1,209。
- Eligible：79。
- Global Research Queue：12。
- ACGL／KNSL／PGR 的跨模型研究 rank 分別約為 31／35／38，不再因 P&C Raw Score 直接霸佔全市場榜首。

這些數字是當時資料快照，不應當成目前的最新市場結果。

## 7. GitHub Actions 排程

Workflow：`.github/workflows/alpha_hunt.yml`

- 每週四 `22:00 UTC`：美股盤後跑 Mode C，台北時間通常為週五 06:00。
- 每月 1 日 `03:00 UTC`：先做全市場第一層 universe refresh，再跑 Mode C。
- 手動執行可選 `refresh_universe=true`。
- push／pull request 只跑便宜的 compile 與單元測試，不跑高成本市場抓取。
- 網路研究 job timeout 為 360 分鐘。
- 月度 hunter 失敗時，如果 repo 仍有上一份完整 `qualified_universe.csv`，Mode C 可沿用舊完整 universe；partial 不會覆蓋完整輸出。
- 成功後上傳 Full Market Hunter、Mode C outputs 與 Static Dashboard artifacts。
- Public repository 時部署 GitHub Pages。

必要 GitHub Actions secret：

- `USER_EMAIL`：SEC API 聯絡信箱。換帳號後請在 repository Settings -> Secrets and variables -> Actions 確認仍存在；缺少時 workflow 與 Mode C 應明確停止，不再使用任何個人信箱 fallback。

不要把 token、API key 或個人憑證寫進本文件或 commit。

## 8. Git 與帳號交接注意事項

### 8.1 已知提交狀態與遠端截斷警告

- 本機分支：`codex/metric-integrity-audit-v2`
- 本機 HEAD：`e98fffe`；2026-08-30 已推送至工作分支。本輪未重新 fetch，遠端現況仍須於下次推送前確認。
- 2026-08-27 fetch 後，本機 `origin/main` 與遠端 main 均為 `0b764e0...`。
- 本機 upstream 為 `origin/codex/metric-integrity-audit-v2`；開始本輪時本機 tracking ref 與 HEAD 相同。
- 歷史事故：舊遠端工作分支 `52ff824...` 並非本機完整版本的等價提交。曾發現下列 8 個檔案被截在約 30 KB，現已由 `e98fffe` 修復：
  - `AQR_ModeC_Agent_V12.py`
  - `CURRENT_STRATEGY_LOGIC_FOR_AI_REVIEW.md`
  - `build_mode_c_dashboard.py`
  - `mode_c_industry_models.py`
  - `mode_c_metric_contract.py`
  - `tests/test_mode_c_core.py`
  - `total_market_hunter_v2.py`
  - `validate_mode_c_outputs.py`
- 修復保留完整本機檔案樹並連接舊遠端歷史，未使用 force push；禁止直接採用舊 `52ff824` 的檔案樹。
- `main` 的最後確認基準為 `0b764e0...`；本輪未查最新遠端。工作分支尚未合併 main，因此不能宣稱原生排程已使用全部最新策略修正。
- 8 月累積變更已提交推送；9 月 4 日新增修改仍在工作樹。新帳號不得用 clean/reset 動作清掉。

### 8.2 新帳號連接步驟

1. 在 Codex／GitHub connector 登入新帳號。
2. 確認新帳號可讀寫 `e34102309-lab/dodoladodo`；若 repository 仍屬舊帳號，先邀請新帳號為 collaborator 或移轉 repository。
3. 在本機重新設定 Git credential，但不要刪除工作資料夾。
4. 執行 `git fetch origin --prune`。
5. 比較本機與遠端分支：

```powershell
git log --left-right --graph --cherry-pick --oneline HEAD...origin/codex/metric-integrity-audit-v2
git diff --stat HEAD origin/codex/metric-integrity-audit-v2
```

6. 只有在 `git diff` 確實為空且關鍵檔案大小合理時，才能視為內容等價。
7. 舊截斷已由 `e98fffe` 修復，不需重做修復合併。先保留本輪未提交修改，再依最新遠端 diff 判斷如何整合；不可 force push。
8. 修復分支通過測試後，再以 PR 整合最新 `main` 的 universe refresh 與本機策略修改。
9. 不要使用 `git reset --hard`，並確認 Actions secret、workflow permissions、Pages 與排程仍可用。

## 9. 上次驗證結果

2026-09-06 最近一次工程驗證結果：

- Python compile：七個本輪生產模組已通過 `py_compile`。
- 單元測試：完整 suite 共 215 項，約 4.0 秒，先有 214 項通過；唯一失敗是抽出 specialized decision helper 後，舊測試仍搜尋原變數名稱。測試改為檢查 helper 呼叫及明確 `ABSTAIN` gate 後，該項定點重跑通過。依低耗用原則未為純測試字串更新再跑完整 suite；215 項案例已跨完整與定點執行全部通過。
- Fixture pipeline：5 檔，3 PASS、1 FAIL、1 ABSTAIN、3 eligible；KNSL fixture 因 P&C 壓力存活失敗退出 eligible。
- Zero audit：`true_zero = 5`、`invalid_zero = 0`、`missing = 151`、`not_applicable = 347`；缺值與不適用分開保留，不以 0 補值。
- Dashboard 離線重建成功。
- 沒有重跑高成本的全市場即時抓取，這是刻意節省資料與模型額度。

最小驗證指令：

```powershell
& 'D:\dobird\.venv\Scripts\python.exe' -m unittest discover -s tests -q
& 'D:\dobird\.venv\Scripts\python.exe' mode_c_fixture_pipeline.py --output-dir fixture_output_five_stage_20260904
```

只有在確實需要更新市場資料時，才執行全市場 hunter 或完整 Mode C 網路流程。

## 10. 尚未完成與限制

1. 尚未完成真正的歷史成分股、下市股與當時 universe 重建。
2. 已有 point-in-time calibration input audit，但尚未建立無偏歷史面板、執行 walk-forward 或校準跨模型 Expected Return。
3. 公司自訂 KPI，例如正式 Combined Ratio、AUM、FRE、法定 RBC、AFFO bridge，可能需要人工或額外可靠來源。
4. Maintenance CapEx 是透明估計區間，不是可直接觀測事實。
5. Yahoo 當前 metadata 不等於歷史時點資料。
6. 已有可選的 portfolio pre-trade 輸入閘門，但尚未自動生成持倉、ETF 穿透、相關性與壓力資料，也未完成交易成本、滑價、稅、容量或組合最佳化。
7. Global Research Queue 只代表研究優先順序，不能解讀成買進建議或預期報酬。
8. 下次原生排程成功後，才會刷新價格日、SEC availability date、universe version 與 run metadata。
9. 新增的特殊產業壓力是財報代理情境；CET1、RBC、reserve triangle、repo haircut、duration gap、same-store NOI、allowed ROE 等監管或公司 KPI 仍可能要求人工來源。
10. BANK 壓力新增 RWA 後，未提供標準 `us-gaap:RiskWeightedAssets` 或期末無法對齊的公司會增加 `ABSTAIN`；目前不猜公司 extension，也不以 total assets 代理。

## 11. 建議下一步優先順序

1. 新帳號先確認 GitHub repository 權限、branch、Actions secret 與最近一次 workflow 狀態。
2. 先跑便宜的 unit tests，不要立即做全市場重抓。
3. 檢查下一次週四排程能否正常完成並產生 artifacts／Pages。
4. 若要繼續提升策略，優先做 point-in-time walk-forward calibration，而不是再堆更多單股指標。
5. 接著做 portfolio layer：持倉、ETF 重疊、相關性、壓力情境與部位上限。
6. 任何新 KPI 都必須先定義來源、as-of 時間、缺值語意、適用模型與 validator 規則。

## 12. 可直接貼給新 Codex 帳號的接手提示

```text
請接手本機專案：
D:\使用者\DoBird\Favorites\Documents\股票\dodoladodo-work

請先完整閱讀：
1. PROJECT_ACCOUNT_HANDOFF.md
2. CURRENT_STRATEGY_LOGIC_FOR_AI_REVIEW.md
3. STRATEGY_FIVE_STAGE_AUDIT.md
4. .github/workflows/alpha_hunt.yml

Repository 是 e34102309-lab/dodoladodo，主要工作分支是
codex/metric-integrity-audit-v2。本機 HEAD 是 e98fffe，8 月累積修正已推送
工作分支，舊 52ff824 的截斷樹已修復，尚未合併 main。其上的 2026-09-04
至 2026-09-06 五段策略稽核修正尚未提交；215 項測試案例已跨完整 suite
與單一定點重跑通過，離線 fixture 亦已通過。
先看 git status、log、diff 確認真實狀態，保留未提交修改；未經授權不要推送。

這是一套美股長期價值研究漏斗，不是自動交易器。請維持 SEC point-in-time
證據、禁止單季乘四、FAIL/ABSTAIN/N/A 分流、產業專用模型、同模型 percentile
與小樣本收縮。跨模型尚未校準，不可把 Global Research Queue 稱為預期報酬。

先做便宜的單元測試與 fixture 驗證，不要未經要求重跑高成本全市場流程。
不要覆蓋使用者既有變更，也不要使用 git reset --hard。完成任何修改後，請列出
檔案、測試、限制與是否已推送 GitHub。
```

## 13. 交接完成條件

新帳號能回答並確認以下事項，即表示已成功接手：

- 能找到本機 repo 與正確工作分支。
- 能說明每月初篩、每週深篩與產業模型分流。
- 知道 Global Research Queue 不是跨模型 alpha 排名。
- 知道哪些情況是 `FAIL`、`ABSTAIN` 與 `NOT_APPLICABLE`。
- 能找到 evidence ledger、validator、Dashboard 與 fixture pipeline。
- 能確認 GitHub Actions 排程、secret、artifacts 與 Pages 狀態。
- 能在不跑全市場流程的情況下完成最小驗證。
