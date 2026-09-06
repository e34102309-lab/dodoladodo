# 股票篩選策略完整邏輯（供外部 AI 審查）

> 策略版本：`1fcbd6e`，已於 2026-09-06 透過 merge commit `a7a0c1a` 整合至 `main`。五段檢查紀錄見 `STRATEGY_FIVE_STAGE_AUDIT.md`。
> 本文件描述「程式目前真的會做什麼」，不是理想藍圖，也不是投資建議。
> 金額若無特別註明，以十億美元（USD B）處理；百分比欄位以程式輸出的百分點表示。

## 1. 系統目標與邊界

本系統是一套只做多的研究漏斗，不是自動交易器：

1. 每月建立一次美股大型股候選宇宙。
2. 每週四美股盤後以最新候選宇宙執行 SEC point-in-time 深篩。
3. 一般企業與特殊產業分流，不把銀行、保險、REIT 等硬塞進同一套 FCF/EBITDA 模型。
4. 先用資料完整度、財務存續與硬性風險淘汰，再以同模型百分位建立研究順序。
5. 最多輸出 12 檔 `Global_Research_Queue`，供人工研究，不直接下單；另輸出每個模型內的研究名單。

程式目前不等於完整無偏回測，也沒有自動完成：

- 歷史成分股、下市股與當時可投資 universe 重建。
- 交易成本、稅、滑價、成交衝擊與再平衡模擬。
- 預測資料、法說逐字稿、供應鏈、監管表單與公司自訂 KPI 的完整 point-in-time 歷史。
- 組合最佳化、相關性／共變異數、現有持倉與 ETF 穿透重疊計算。
- 自動買進、賣出或券商連線。

## 2. 端到端流程

```mermaid
flowchart TD
    A[Yahoo 美國股票 Screener] --> B[證券種類與交易所檢查]
    B --> C[每月第一層全市場初篩]
    C --> D[qualified_universe.csv]
    D --> E[共用產業路由]
    E --> F{模型類型}
    F -->|一般企業| G[SEC point-in-time 財報重建]
    F -->|特殊產業| H[十三種產業專用模型路由]
    G --> I[資料信心與核心資料閘門]
    G --> J[FCF 品質 估值 預期 拐點 壓力測試]
    H --> K[專用核心 KPI 覆蓋與硬風險]
    I --> L[PASS FAIL ABSTAIN]
    J --> L
    K --> L
    L --> M[Eligible 硬門檻]
    M --> N[同模型百分位與小樣本收縮]
    N --> P[Coverage-first 模型 round-robin Queue 最多 12 檔]
    P --> O[CSV Evidence Ledger Dashboard Validator]
```

## 3. 資料來源與 point-in-time 規則

### 3.1 Yahoo Finance

Yahoo 用於：

- 初始美股候選、價格、市值、成交量、sector、industry 與 quote type。
- 第一層低成本財務初篩。
- Mode C 當日價格、30 日平均成交金額、動能等市場欄位。
- SEC 無法建立總負債時的明確 `totalDebt` fallback。
- SEC SBC 或股數不足時的有限 fallback，但會扣資料信心。
- Short interest 與 days to cover 風險旗標；此來源不被視為權威空單資料。

Yahoo 當前 metadata 不得被當成歷史時點基本面，因此目前的 ledger 只解決 SEC 財報證據的 point-in-time，不代表整個策略已可無偏回測。

### 3.2 SEC EDGAR

主要使用 Company Facts 與 Submissions：

- Company Facts 提供 XBRL facts。
- Submissions 提供 accession、filing date、acceptance time 與表單資訊。
- 每個 fact 必須滿足 `available_to_model_at <= decision_timestamp`。
- acceptance time 缺失時，保守使用 filing date 次日，並降低資料信心。
- 國內公司核心 fact 最長可舊 240 天。
- 20-F／40-F 外國年度申報最長可舊 550 天。
- 非美元或無可靠 IFRS 映射時不得把數值假裝成美元，維持 `ABSTAIN`。

### 3.3 TTM 計算順序

程式明確禁止把單季乘四年化。流量型指標依序採用：

1. 若最新年報期末不早於最新季報：直接用最新完整年報。
2. 有最新季報及可比較 YTD 時：

   `TTM = 最近完整年報 + 本期 YTD - 去年同期 YTD`

3. 若上式無法成立：由 Q1、Q2-Q1、Q3-Q2、FY-Q3 重建單季，合計最近四季。
4. 仍無法建立時：退回最近年報，標記 annual fallback 並扣資料信心。
5. 核心 TTM 仍缺失、過舊或沒有 evidence lineage 時：`ABSTAIN`。

完整年度流量 fact 必須同時來自年度表單，且實際期間為 330 至 380 天；`FY` 標籤本身不足以證明它是全年流量。現金、負債等時點餘額改由 instant-fact 路徑選取，不套用流量期間規則。

期間檢查不能只依 SEC 的 `fy`／`fp` 標籤：YTD 相減必須有相同起日、相鄰期末；年度橋接要求本期 YTD 起日緊接年報期末、去年 YTD 起日等於該年報起日、同期末相隔 350 至 380 天且累計期間長度差不超過 7 天。這容許 52／53 週財年，但不把錯置的比較期相減。

最近 N 季視窗要求相鄰期末相隔 60 至 130 天；有第五季以上時，每四季的同期末還須相隔 350 至 380 天。最新來源期間不能被缺值清理悄悄移成較舊期間。缺季時不會拿最後四筆跨年度加總，也不會拿最後八筆冒充兩個相鄰 TTM；此規則同時用於營收成長與 Maintenance CapEx 的成長判斷。季度成長不可建立時，Maintenance CapEx 沿用明示的年度成長 fallback。

### 3.4 證據帳本

每個實際使用的來源或衍生值記錄：

- ticker、CIK、concept、form、period end、filed／accepted／available time。
- accession、單位、原始值、選用角色。
- 衍生公式與 source evidence IDs。
- 衍生圖必須無循環，且不得引用決策時間之後的 evidence。

## 4. 每月第一層全市場初篩

入口：`total_market_hunter_v2.py`

### 4.1 候選來源與證券種類

- Yahoo 美國區股票 screener。
- 市值先限制至少 50 億美元。
- 每頁 250 檔，最多 20 頁。
- 支援 NASDAQ、NYSE、NYSE American 等指定交易所代碼。
- 只保留普通股或有足夠證據推定為普通股的 ADS。
- 排除 ETF、基金、preferred、unit、right、warrant、note、bond 等非普通股。
- symbol 主要格式為 1 至 6 個英文字母，可帶一組 `-` 或 `.` 股類尾碼。
- 同一 CIK 多個普通股類別時，優先官方普通股證據完整者，再選平均日成交金額較高者。
- 已明確處理 FOX/FOXA、GOOG/GOOGL、NWS/NWSA、UA/UAA 等雙股類重複。

ADS/ADR 模糊時會用 SEC 年報 cover page 的 `Security12bTitle` 與 `TradingSymbol` 交叉確認；無法確認則 `Review`，不硬猜。

### 4.2 共通前置條件

- 市值 `< 5B`：淘汰。
- 市值缺失：`Review`。
- sector 或 industry 缺失：`Review`。
- 不支援交易所、非股票或基金：淘汰。

### 4.3 一般企業初篩

- Yahoo OCF 與 EBITDA **同時** `<= 0`：淘汰。
- OCF 或 EBITDA 只有單一項非正、或任一項缺值：警示並後送 SEC，以免第三方暫時值誤殺。
- 毛利率 `<= 0%`：淘汰；缺值則後送 SEC。
- 毛利率 `< 25%`：執行同業檢查，但不直接淘汰。
- 同業樣本至少 5 家才計中位數；樣本不足或低於中位數只警示。
- Revenue `<= 0` 或缺值：視為 Yahoo 資料異常，後送 SEC，不直接淘汰。
- 有可靠 cash 時：`Net Debt / EBITDA`。
- 有可靠 cash 且 `Net Debt / EBITDA > 5x`：淘汰。
- cash 缺失時只顯示 `Gross Debt / EBITDA`；即使超過 5x 也先警示並交 SEC 確認淨槓桿。
- 槓桿 `> 4x` 且 `<= 5x`：警示，但可後送。

目前預設關閉：

- PP&E／Revenue 上限篩選。
- 機構持股至少 40% 篩選。
- sector 黑名單與 industry 黑名單。

### 4.4 特殊產業的 Yahoo 初篩

第一層只排除極明確風險，SEC 專用模型才是權威：

- 銀行、放款金融、P&C、壽險：book value `<= 0` 或 ROE `<= -20%` 淘汰。
- Equity REIT：已揭露 FFO `<= 0` 淘汰；缺 FFO 則後送 SEC 重建。
- Mortgage REIT：book value `<= 0` 淘汰。
- Regulated Utility：EBITDA 與 OCF 同時 `<= 0` 淘汰。
- Fee Financial：EBITDA 與 OCF 同時 `<= 0` 淘汰。
- Cyclical：EBITDA 或 OCF 非正只警示，交由 trough survival 模型判斷。

### 4.5 初篩輸出與容錯

- `qualified_universe.csv`：完整跑完才更新。
- `hunter_audit.csv`：保留 Pass、Drop、Review 與原因。
- 中斷只更新 partial/checkpoint，不覆蓋上次完整 universe。
- Yahoo 限流時可沿用最近完整 hunter 驗證過的證券種類、交易所與產業 metadata。
- 舊 cache 不得拿來猜價格、SBC、負債或核心財報數字。

## 5. 共用產業路由

入口：`mode_c_routing.py`

路由順序概念如下：

- 保險經紀：`FINANCIAL_FEE`。
- 保險、healthcare plan、managed care：依關鍵字分到 `INSURANCE_LIFE` 或 `INSURANCE_P_AND_C`。
- REIT：在廣義金融 sector 之前分流為 `REIT_EQUITY` 或 `REIT_MORTGAGE`，避免 mortgage REIT 被金融預設路由攔截。
- 金融 sector：
  - bank／savings：`BANK`。
  - mortgage finance、consumer finance、specialty finance、credit union：`FINANCIAL_LENDER`。
  - 其餘：`FINANCIAL_FEE`。
- Utilities：merchant／renewable／independent power 走 `GENERAL_CORPORATE`，其餘走 `REGULATED_UTILITY`。
- Energy、Basic Materials，或油氣、金屬、煤、化學、木材、紙、航運、航空、卡車、汽車、農業等關鍵字：`CYCLICAL_MIDCYCLE`。
- 其餘：`GENERAL_CORPORATE`。

sector／industry 只接受有效文字；空白、NaN、`None`、`null`、`N/A` 或 `<NA>` 不得因字串轉換而被誤認為一般企業。

`FINANCIAL_FEE` 若 SEC 顯示 loans/assets `>= 20%` 且存在信用損失準備，可透明改路由為 `FINANCIAL_LENDER`。輸出保留初始模型、最終模型與改路由原因。

## 6. Mode C 共通前置閘門

入口：`AQR_ModeC_Agent_V12.py`

- 市值至少 50 億美元。
- 30 日平均成交金額至少 1,500 萬美元。
- 必須是支援交易所的普通股。
- sector／industry 不可缺失。
- 價格、市值、流動性或證券種類明確不合格：`FAIL`。
- 核心 SEC 資料缺失、過舊、幣別不可靠或 lineage 不足：`ABSTAIN`。
- 一般企業所需核心 facts：OCF、Revenue、EBIT、CapEx、D&A、Gross Profit、Net Income、Cash、Equity。
- 一般企業核心 TTM：OCF、CapEx、EBIT、D&A、Revenue、Net Income。
- SBC 不可在缺失時默認為 0。
- 總負債 SEC 優先；Yahoo `totalDebt` 只可作明確 fallback。
- cash、equity 必須有可用的 SEC balance-sheet evidence。
- EV `<= 0` 時一般 EV-based 模型不再可比，改為 `ABSTAIN` 交由淨現金特殊情境研究。

## 7. 一般企業 Maintenance CapEx 與 FCF

### 7.1 D&A 錨點

若資料可得：

`D&A Anchor = max(TTM D&A, 最近三年平均年度 D&A)`

若三年 D&A 不完整，仍可用 TTM D&A，但信心較低。

### 7.2 Maintenance CapEx 估計

先定義：

`Base = min(Total CapEx, D&A Anchor)`

`Excess = max(Total CapEx - Base, 0)`

再依營收成長決定 Excess 中被視為維護性支出的比例：

| TTM Revenue Growth | Maintenance Ratio of Excess |
|---|---:|
| `<= 0%` | 80% |
| `>= 15%` | 20% |
| `>= 5%` 且 `< 15%` | 35% |
| 其餘或缺失 | 55% |

`Maintenance CapEx = min(Total CapEx, Base + Excess * Ratio)`

敏感度區間使用 Ratio 上下 15 個百分點。完全沒有 D&A 時用全部 CapEx，且 Maintenance CapEx 信心為 `LOW`；`LOW` 會使一般企業 `ABSTAIN`。

### 7.3 FCF 公式

- `Maintenance Real FCF = TTM OCF - Maintenance CapEx - TTM SBC`
- `Conservative Real FCF = TTM OCF - Total CapEx - TTM SBC`
- `Real FCF Yield = Maintenance Real FCF / Market Cap`
- 同時保留兩種 FCF 對 Market Cap 與 EV 的 yield，避免分母混用。

主要資格門檻使用 Maintenance Real FCF／Market Cap，保守版本用於敏感度與風險檢查。

### 7.4 CapEx 風險旗標

- Revenue growth `<= 0` 且 `CapEx / D&A >= 1.5` 只建立基礎監控訊號，不再單獨 `FAIL`。
- 再檢查三個獨立佐證：最近至少兩個連續年度營收下降、ROIC `< 8%`、扣除全部 CapEx 與 SBC 後 FCF `<= 0`。
- 無佐證為 `MONITOR`；一項佐證為 `WATCH` 並只扣一次 8 分；至少兩項為 `HIGH_RISK`，才直接 `FAIL`。
- 因此高 ROIC 建設期公司即使全額 CapEx 後 FCF 暫時非正，也不會只靠該單一結果被淘汰。
- Revenue 下滑且 `CapEx < 60% * D&A`：標記 underinvestment high-yield 警示。
- Maintenance Real FCF `<= 0`：直接 `FAIL`。

### 7.5 FCF 歷史品質

- 最多取最近 5 個連續年度。
- 每年必須同時有 OCF、CapEx、SBC、Revenue、Net Income lineage；SBC 缺失不能當 0。
- 歷史視窗必須錨定上述五種流量中最新已報告的年度；若最新年度缺任一必要流量，不得跳過它後把較舊的完整五年冒充最新歷史。
- 至少有 3 年時，Real FCF 為正的年度比例必須 `>= 60%`。
- 有 3 年完整資料時，三年累計 OCF 必須 `> 0`。
- FCF margin 標準差用於穩定性評分。
- 每股 FCF／EPS 三年 CAGR 只在精確三年端點與拆股調整股數可得時建立。

## 8. 一般企業資產品質與償債能力

### 8.1 ROIC／ROCE

- `Tax Rate = TTM Tax / (Net Income + Tax)`，限制在 0% 至 35%。
- 無法建立時假設 21%，並扣資料信心。
- `Invested Capital = Equity + Debt - Cash`
- `Average Invested Capital = (Beginning Invested Capital + Ending Invested Capital) / 2`
- `ROIC = EBIT * (1 - Tax Rate) / Average Invested Capital`
- 缺期初資本時才回退期末資本，標記 `ENDING_CAPITAL_FALLBACK_ESTIMATED` 並扣資料信心。
- 同時輸出期末資本 ROIC，以及含／不含 goodwill 的 ROIC，供口徑勾稽。平均資本口徑只有在期初與期末 goodwill 都可得時才計算 excluding-goodwill ROIC；期末 fallback 只可扣期末 goodwill，不混用期間。
- `ROCE = EBIT / Invested Capital`

### 8.2 ICR

- 一般情況：`ICR = EBIT / Interest Expense`。
- Debt 很小（`<= 0.01B`）或 Cash `>= Debt`：ICR 視為 non-binding／無限大。
- 有淨負債且缺 Interest evidence：`ABSTAIN`。
- 最終 eligible 需要 ICR `>= 3x`，除非 ICR non-binding。
- ICR `< 1x`：一般企業直接 `FAIL`。

### 8.3 EBITDA 15%／30% 壓力測試

假設 D&A 固定：

- `Stressed EBITDA = EBITDA * (1 - Drop)`
- `Stressed EBIT = EBIT - (EBITDA - Stressed EBITDA)`
- `Stressed Real FCF = Real FCF - EBITDA Loss * (1 - Tax Rate)`
- 重算 ICR 與 Net Debt／Stressed EBITDA。

Survival 必須同時滿足：

- Stressed EBITDA `> 0`。
- Stressed Real FCF `>= 0`；淨現金公司可允許一年負 FCF，但剩餘現金必須仍為正。
- 有淨負債者 stressed ICR `>= 1.5x`。

30% 情境 survival 失敗不得列入 eligible。

## 9. 一般企業歷史估值與反向估值

### 9.1 歷史估值

- 僅在 filing inputs 已可被市場取得後再重建該年估值，另加一日保守 lag。
- 股數與價格先調整到一致拆股基準。
- 用當時 debt、cash、EBITDA 重建 Market Cap 與 EV/EBITDA。
- 至少 5 個正值 EV/EBITDA 年，且可用年度覆蓋率 `>= 60%` 才有效。
- 主要 value 因子使用目前 EV/EBITDA 在自身歷史中的 percentile。
- 價格下載與歷史估值視窗固定為最多 10 年；下檔估值依有效樣本數使用 P25（5–6 年）、P20（7–9 年）或 P15（10 年）。不足時採保守 fallback floor，不宣稱不存在的 15 年樣本。
- 壓力情境的 EV/EBITDA 與 P/E floor 均不得高於當前倍數；歷史低分位若高於現值，只能維持現值，不能在「壓力」中假設估值擴張。
- 30% 財務壓力下的估值回撤必須大於 `-75%` 才可 eligible。

### 9.2 反推三年 EBITDA CAGR

- 必要報酬率：10%。
- `Future EV Required = Current EV * (1.10)^3`
- `Required EBITDA in Year 3 = Future EV Required / Target Exit Multiple`
- Target multiple 使用 40% 公司歷史中位數、40% peer 中位數、20% 利率調整值。Peer pool 只收當期 `PASS` 公司：先用至少 3 家同 industry，再用至少 5 家同 sector；兩者都不足時退回公司自身歷史，不再拿整個 GENERAL_CORPORATE 跨產業混配。
- 再由目前 EBITDA 反推三年 CAGR。

### 9.3 動態 CAGR 容忍上限

基礎上限為 30%，再依 ROIC 與近三季毛利變化調整：

- `ROIC Adjustment = clamp((ROIC% - 15) * 0.6, -6, +10)`
- `GM Adjustment = clamp(GM Change pp * 2, -15, +6)`
- `Limit = clamp(30 + ROIC Adjustment + GM Adjustment, 15, 45)`
- 毛利三季變化 `<= -2pp` 時，上限封頂 15%。

預期負擔分數：

- Implied CAGR 在 `-10%` 至 `15%`：100 分。
- 低於 `-10%`：35 分。
- `15%` 至動態上限：由 100 線性降到 55。
- 超過動態上限後，再於 15 個百分點內由 55 線性降到 0。
- 缺失：20 分，但最終 eligible 仍要求 implied CAGR 有效。
- 最終 eligible 要求 expectations score `>= 15`。

## 10. 一般企業營運拐點與博弈風險

### 10.1 近三季營收與毛利

- Revenue `< -2%` 且毛利變化絕對值 `<= 1.5pp`：暫時落難好公司，trend score 85。
- Revenue `>= -2%` 且毛利連續下降，或毛利變化 `<= -2pp`：結構性風險，trend score 0 並扣 15 分，但不再僅憑三季訊號作永久 hard gate。
- Revenue `< -2%` 且毛利變化 `< -2pp`：雙重惡化，直接 `FAIL`。
- 其他：中性，trend score 70。
- 最新三季必須連續，營收與毛利期末必須完全對齊；缺季、最新期缺值或期間不一致：`ABSTAIN`，不得把不可判斷誤寫成基本面失敗。

### 10.2 DSI

`DSI = Average Inventory / Trailing 4Q COGS * 365`

Average Inventory 使用當期與約一年前（300 至 450 天）的存貨平均：

- 當期存貨期末必須等於 COGS 視窗期末，分母必須是連續四季；不以前一季餘額替代當期，也不回退舊 DSI 冒充最新值。
- 至少有最新連續三季 DSI 才能計趨勢分數；YoY 季節性確認另要求連續五季。最新值可觀測但趨勢資料不足時，保留數值，趨勢分數為缺失。

- 三個觀察點連續下降，且 YoY `<= -5%`：去庫存改善，80 分。
- 三個觀察點連續上升，且 YoY `>= 5%`：庫存惡化，20 分並扣 8 分風險。
- 連續下降但未通過 YoY 季節性確認：60 分。
- 只有有效且適用的庫存資料才計分；缺失為 `MISSING`，不適用為 `NOT_APPLICABLE`，兩者都不給中性 50。
- BANK、INSURANCE、REIT、貸款／費用型金融、資產管理、software、platform 與低庫存實質性企業，DSI 退出分母。
- 輸出 `Applicable_Factor_Weight`、`Available_Factor_Weight`、`Factor_Coverage` 與 `Weight_Renormalized`。

### 10.3 營運資金與 OCF 品質

- `DSO = Average Accounts Receivable / Trailing 4Q Revenue * 365`
- `DPO = Average Accounts Payable / Trailing 4Q COGS * 365`
- `CCC = DSO + DSI - DPO`；任一組件缺失，或 Revenue／COGS 最新期末不一致時 CCC 維持缺失，不以 0 補值。
- 資產負債表平均值使用當期與距離 300 至 450 天的前期餘額；當期餘額必須與流量視窗同一期末，分母只用連續 trailing 4Q，不用單季乘四。TTM 年增比較必須有連續八季，不跨缺季計算成長差或風險扣分。
- AR 年增率高於 TTM Revenue 增長至少 `15pp`，且 DSO 至少 5 天：應收帳款回收品質警示。
- AP 年增率高於 TTM COGS 增長至少 `20pp`，且 DPO 至少 5 天：OCF 可能受延後付款支撐。低於 5 天視為小基期、經濟影響不具實質性，只保留診斷而不扣分。
- 一項警示為 `WATCH` 並扣 5 分；兩項同時成立為 `HIGH_RISK` 並扣 10 分。這是同一個 working-capital risk penalty，不在其他分數重複扣除。
- Deferred revenue 年增率、DSO/DPO 年變化與 CCC 會輸出供研究，但不直接套單一好壞方向，避免錯罰訂閱、平台與預收型商業模式。
- AR／AP 可比性 coverage 為 0%、50% 或 100%；完全無可比證據時為 `MISSING`，不作風險扣分也不假裝 `CLEAR`。

### 10.4 Short squeeze

- Short Interest `> 15%`。
- Days to Cover `> 5`。
- 資料距決策日 `<= 45` 天。

同時成立只標記 `Squeeze_Risk`，目前對分數中性：不當作正向 Alpha、不作風險扣分，也不放寬基本面門檻。原因是 Yahoo 空單資料不是完整 point-in-time 權威來源。

## 11. SBC、回購與稀釋

- SBC 已作為經濟成本從 Real FCF 扣除。
- `Real Buyback = Share Repurchases - Stock Issuance`
- `Net Buyback Yield = Real Buyback / Market Cap`
- 回購很多但一年股數沒有下降：在 Capital Allocation 評估回購是否真正抵銷 SBC。
- 非持續性股數增加只有在沒有實質回購、且尚未在資本配置層懲罰時，才使用一次 Ownership Dilution Penalty。
- 拆股調整後三年股數增加 `> 3%`：`Persistent Dilution Hard Gate`。沒有直接併購發股證據時 `FAIL`；只要有直接 XBRL 併購股票對價，即先 `ABSTAIN` 等待增益性覆核，不自動原諒也不先判死刑。
- 拆股調整後仍出現 `> 50%` 股數跳變：視為 IPO、重組或口徑不連續，不直接當稀釋，但資料信心扣 40。
- 併購歸因只接受直接 XBRL 股票對價；證據超過 450 天即丟棄。缺少股票對價 evidence 是 `MISSING`，不能推論發行與併購無關；有直接 0 證據則標記 `DIRECT_XBRL_ZERO`。股票發行現金流只作 reconciliation：正值為 `RECONCILED_TO_TTM_STOCK_ISSUANCE`，缺失或非正為 `DIRECT_ACQUISITION_EVIDENCE_ONLY`；後者可能是非現金換股，不得因此把併購歸因取消。

Capital Allocation Score 從 60 分開始：

- TTM 毛回購、TTM 發行金額，或一年／三年至少一組股數變化證據不足時，分數為 `N/A`；不以 60 分冒充中性，該 5% 因子留在適用分母但退出可用分子，使 `Factor_Coverage` 下降。
- 三年股數下降至少 3%：`+15`。
- Real Buyback `> 0` 且一年股數下降：`+15`。
- 上述情況且 Net Buyback Yield `>= 1%`：再 `+10`。
- Real Buyback `> 0` 但股數未下降：`-35`。
- 發行額大於**毛回購額**且一年股數增加：`-10`；不得拿已扣發行的 Real Buyback 再比較一次。
- 三年股數增加超過 3% 由 Persistent Dilution hard gate 直接排除，不再於資本配置分數重複扣分。
- 最後限制在 0 至 100。

## 12. 一般企業分數

### 12.1 Value Score

- `Valuation Score = 100 - Current Historical EV/EBITDA Percentile`；缺失暫記 20，但 eligible 仍會被擋。
- FCF score：Real FCF Yield 由 0% 至 10% 線性映射 0 至 100。
- `Value = 55% * Valuation + 45% * FCF Yield Score`

### 12.2 Quality Score

- ICR 25%：1x 至 10x 線性；淨現金／non-binding 為 100。
- FCF quality 20%：五年 `Real FCF / Net Income` 由 0.2x 至 1.0x 線性映射；缺失為 35。當期 FCF yield 只留在 Value Score 與 eligible 門檻，不在 Quality 重複計分。
- Revenue／GM trend 20%。
- ROIC 20%：5% 至 20% 線性；缺失 35，但資料信心會另外重罰。
- 五年穩定性 15%。

穩定性內部：

- FCF 正值年度比例：55%。
- FCF margin 標準差：25%；`<=5pp` 100、`<=10pp` 75、`<=20pp` 40、其餘 10。
- 五年 OCF／Net Income：20%，0.5x 至 1.2x 線性。
- 少於三年 FCF 時正值比例子分數先給 40，但最終仍受資料完整與歷史門檻約束。

### 12.3 最終分數

`Long-Term Score =`

- Quality 35%
- Value 30%
- Expectations 20%
- 12M Momentum 5%（-30% 至 +30% 線性）
- DSI／Operating Inflection 5%
- Capital Allocation 5%
- 再減 Risk Penalty

### 12.4 風險扣分

- 30% 壓力估值回撤 `<= -80%`：`-35`。
- `<= -60%`：`-25`。
- `<= -40%`：`-15`。
- `<= -25%`：`-5`。
- 壓力回撤缺失：`-10`。
- Ownership dilution：符合去重條件時一次性 `-5`。
- Persistent dilution：由 hard gate 排除，不再偽裝為另一個獨立 Risk Penalty。
- 三年累計 OCF `<= 0`：`-15`。
- Structural trap：`-15`；已在 Quality trend 反映，因此不再另設 hard gate。
- Revenue + GM double deterioration：由 pipeline hard gate 排除，不再用同一訊號追加獨立 Risk Penalty。
- Squeeze risk：分數中性，只保留事件／波動旗標。
- DSI deterioration：`-8`。
- Working-capital quality `WATCH`：`-5`；`HIGH_RISK`：`-10`，只扣一次。
- Stressed ICR `<1x`：`-25`；`<1.5x`：`-15`；`<2x`：`-8`。
- Net Debt／Stressed EBITDA `>5x`：`-15`；`>4x`：`-8`。

輸出的 `Dilution_Total_Score_Impact` 是可稽核的歸因值：`Ownership Penalty + Capital Allocation Penalty * 5 / Available Factor Weight`。因子缺失而重新正規化時，不能仍用固定 5% 低估或高估資本配置對最終分數的實際影響。

## 13. 一般企業決策與 eligible 硬門檻

### 13.1 直接 FAIL

- ICR `< 1x`。
- Maintenance Real FCF `<= 0`。
- Growth CapEx `HIGH_RISK`，且至少兩項獨立佐證成立。
- 三年累計 OCF `<= 0`。
- 至少三年歷史時，正 FCF 年度比例 `< 60%`。
- 三年股數增加 `> 3%`，且沒有直接併購發股證據需要先做增益性覆核。
- Revenue 與 GM 雙重惡化。
- 其他共通市場、證券或核心風險 hard failure。

### 13.2 ABSTAIN

- 近三季營收／毛利資料不足或期間無法對齊。
- Maintenance CapEx confidence 為 `LOW`。
- Data confidence `< 70`。
- 核心 SEC evidence 缺失、過舊、未能 point-in-time 對齊。
- 有淨負債但無法取得 interest evidence。
- EV 非正或無法建立可比估值。
- 幣別／IFRS taxonomy 無可靠換算鏈。
- Persistent dilution 同時有直接 acquisition-related issuance 證據，但尚未完成每股增益、商譽與整合代價覆核。
- `ABSTAIN` 不得覆蓋已由獨立證據確認的硬失敗：例如 ICR < 1、三年累計 OCF 非正、未獲併購歸因的持續稀釋或營收／毛利雙重惡化仍維持 `FAIL`。Maintenance CapEx 信心低只會阻擋依賴該估計的 FCF／Growth CapEx 判斷。

### 13.3 Eligible 必須全部通過

- `Status = Pass` 且 `Decision_State = PASS`。
- 模型受支援，Data confidence `>= 70`。
- Long-Term Score `>= 60`。
- 歷史 EV/EBITDA percentile 有效。
- Real FCF Yield `>= 2%`。
- ICR `>= 3x` 或 non-binding。
- 無 persistent dilution。
- Implied EBITDA CAGR 有效且 expectations score `>= 15`。
- 30% 壓力估值回撤 `> -75%`。
- 30% stress survival = true。
- 無 double deterioration 或季度毛利資料不足；structural margin risk 改以 Quality 與一次性風險扣分處理。
- FCF 與 OCF 歷史硬門檻通過。

## 14. 特殊產業共通評分機制

入口：`mode_c_industry_models.py`

- 可用子分數按權重重新正規化。
- 再乘 `min(1, Score Coverage / 80%)`，避免缺很多欄位仍拿高分。
- Hard failure：`FAIL`。
- Required metric 缺失：`ABSTAIN`。
- Score coverage `< 65%`：`ABSTAIN`。
- 其餘 score `>= 60` 為 `PASS`，否則 `FAIL`。
- 已由專用壓力測試確認的 `FAIL` 優先於其他資料信心不足；不能把已知存活失敗降格成 `ABSTAIN`。若只有模型分數或 required evidence 不完整，Specialized data confidence `< 70` 或模型本身 `ABSTAIN` 才使最終狀態為 `ABSTAIN`。
- Specialized eligible：基礎模型 `PASS`、score `>= 60`、confidence `>= 70`，且該產業 `Specialized_Stress_Status = PASS`。

特殊產業資料信心從 100 開始，主要扣分：

- 無 SEC sources：`-45`。
- acceptance coverage `<50%`：`-35`；`<80%`：`-15`。
- period anomaly：每個 `-10`，最多 `-30`。
- fallback tag 比例 `>=75%`：`-10`；`>=40%`：`-5`。
- optional missing：每個 `-4`，最多 `-25`。
- required missing：信心最高只能 45。
- score coverage 低於 80%：最多再扣 15。

注意：一般企業與特殊產業雖同為 0 至 100 分，但來源、權重與風險閘門不同，不應直接假設跨模型分數已經統計校準。

## 15. 十三種特殊產業模型路由

### 15.1 BANK

核心評分指標：Tier 1 buffer、tangible equity/assets、ROTCE、deposits/assets、allowance/loans、NII growth、AOCI/tangible equity、P/TBV。壓力測試另要求正值 `us-gaap:RiskWeightedAssets`，並要求 RWA、Tier 1 比率、公司最低資本比率、資產、權益、貸款與 allowance 的期末一致。

Hard failures：

- Tier 1 buffer `< 0pp`。
- Tangible equity/assets `< 3%`。
- Net income `<= 0`。
- AOCI/tangible equity `< -50%`。

權重：Tier 1 buffer 27%、tangible capital 18%、ROTCE 18%、deposit funding 10%、allowance 8%、NII growth 7%、AOCI drag 7%、P/TBV 5%。

0 至 100 映射端點：Tier 1 buffer 0 至 5pp、tangible capital 3% 至 10%、ROTCE 0% 至 18%、deposit funding 30% 至 75%、allowance 0.5% 至 2%、NII growth -10% 至 10%、AOCI drag -40% 至 0%；P/TBV 由 2.5x 至 0.7x 反向計分。

### 15.2 INSURANCE_P_AND_C

核心指標：公司揭露 combined ratio、獨立 SEC proxy、premium／policy growth、reserve development、cat loss、investment income／yield、equity/assets、operating ROE、P/B。

公司口徑優先使用 TTM，其次季度；月度值保留稽核但不得挑選單月最好數字冒充正常化獲利。只有 SEC proxy 時標為 `SEC_PROXY_UNRECONCILED`、`ESTIMATED` 並要求人工勾稽。

Hard failures：

- Premiums `<= 0`。
- Combined ratio `> 110%`。
- Equity/assets `< 8%`。
- Net income `<= 0`。

權重：underwriting 30%、premium growth 10%、policy count growth 5%、reserve development 15%、capital 15%、ROE 10%、P/B 5%、來源品質 10%。缺值不給中性分數，依有效權重重新正規化。

映射端點：combined ratio 110% 至 90% 反向、premium growth -5% 至 15%、reserve development 5% 至 -2% 反向、capital 8% 至 25%、ROE 0% 至 18%、P/B 3.0x 至 0.8x 反向。

專用壓力測試：Mild combined ratio `+3pp`；Moderate `+6pp`、premium growth 降至 `min(current,0%)`、investment yield `-50bps`；Severe `+10pp`、premium growth 再比 Moderate 低 5pp、yield `-100bps` 並增加不利 reserve development。壓力造成的淨值減損以 21% 稅率換算稅後增量損失，且資產與權益同步減少後再計 equity/assets。Moderate 必須維持稅前獲利非負且 equity/assets `>=8%`；缺 invested assets、yield 或其他核心輸入時為 `ABSTAIN`，不可補 0。

### 15.3 INSURANCE_LIFE

核心指標：premium + investment income、benefit ratio、equity/assets、ROE、premium growth、P/B。

Hard failures：

- Premium `<= 0`。
- Operating inflow `<= 0`。
- Benefit ratio `> 105%`。
- Equity/assets `< 3%`。
- Net income `<= 0`。

權重：benefit ratio 30%、capital 25%、ROE 20%、premium growth 15%、P/B 10%。

映射端點：benefit ratio 105% 至 65% 反向、capital 3% 至 12%、ROE 0% 至 15%、premium growth -5% 至 12%、P/B 2.5x 至 0.7x 反向。

### 15.4 REIT_EQUITY

代理公式：

- `FFO = Net Income + Real Estate D&A - Property Gain + Impairment`
- `AFFO = FFO - Maintenance CapEx`
- `EBITDAre = Net Income + Interest + max(Tax,0) + D&A - Property Gain + Impairment`
- `Net Debt = max(Debt - Cash, 0)`

Hard failures：

- FFO `<= 0`。
- AFFO `<= 0`。
- Net debt／EBITDAre `> 8x`。
- 有債務時 interest coverage `< 1.5x`。
- Dividend／AFFO `> 110%`。

權重：AFFO yield 25%、P/FFO 20%、leverage 20%、interest coverage 15%、dividend coverage 10%、lease growth 10%。

映射端點：AFFO yield 2% 至 8%、P/FFO 30x 至 10x 反向、net debt/EBITDAre 8x 至 2x 反向、interest coverage 1.5x 至 5x、dividend/AFFO 110% 至 60% 反向、lease growth -5% 至 10%。

### 15.5 REIT_MORTGAGE

核心指標：assets/equity、equity/assets、recurring ROE、P/B、dividend／recurring earnings、NII growth；GAAP ROE 只作診斷。

Hard failures：

- Assets/equity `> 15x`。
- Equity/assets `< 5%`。
- Recurring earnings `<= 0`。GAAP net income 非正只警示，不得因未實現評價損益取代經常性獲利閘門。

權重：capital 25%、leverage 20%、recurring ROE 20%、dividend coverage 15%、P/B 10%、NII growth 10%。GAAP payout 只作警示，不能替代公司 recurring earnings。

映射端點：equity/assets 5% 至 15%、assets/equity 15x 至 5x 反向、ROE 0% 至 15%、dividend/recurring earnings 120% 至 70% 反向、P/B 1.5x 至 0.7x 反向、NII growth -10% 至 10%。

### 15.6 REGULATED_UTILITY

核心指標：EBIT interest coverage、debt/capital、OCF/CapEx、ROE、earnings/dividend、PP&E growth。

Hard failures：

- Equity `<= 0`。
- ICR `< 1.5x`。
- Debt/capital `> 75%`。
- OCF `<= 0`。
- Net income `<= 0`。

權重：ICR 25%、capital structure 20%、CapEx funding 20%、ROE 15%、PP&E growth 10%、dividend coverage 10%。現金 CapEx 缺失時可用 `Delta PP&E + D&A` 代理並警示。

映射端點：ICR 1.5x 至 5x、debt/capital 75% 至 40% 反向、OCF/CapEx 0.5x 至 1.2x、ROE 3% 至 12%、PP&E growth 0% 至 8%、earnings/dividend 0.8x 至 1.5x。

### 15.7 CYCLICAL_MIDCYCLE

至少需要 5 個 point-in-time 年度 EBITDA：

- Mid-cycle：歷史中位數。
- Trough：P20。
- Peak：P90。

Hard failures：

- Mid-cycle EBITDA `<= 0`。
- 有淨負債且 current EBITDA `<= 0`。
- 有淨負債且 trough EBITDA `<= 0`。
- 非淨現金時 trough ICR `< 1.25x`。
- Net debt／trough EBITDA `> 5x`。

權重：EV/mid-cycle EBITDA 30%、trough ICR 25%、trough leverage 20%、Real FCF/EV 15%、cycle position 10%。

映射端點：EV/mid-cycle EBITDA 15x 至 5x 反向、trough ICR 1.25x 至 5x、net debt/trough EBITDA 5x 至 0x 反向、Real FCF/EV 0% 至 8%、current/mid-cycle EBITDA 1.6x 至 0.8x 反向；淨現金公司的 trough ICR 子分數直接為 100。

### 15.8 FINANCIAL_LENDER

核心指標：tangible equity/assets、ROTCE、allowance/loans、NII growth、P/TBV。

Hard failures：

- Tangible equity/assets `< 5%`。
- Net income `<= 0`。

權重：capital 30%、ROTCE 25%、allowance 15%、NII growth 15%、valuation 15%。

映射端點：tangible equity/assets 5% 至 15%、ROTCE 0% 至 18%、allowance/loans 0.5% 至 2.5%、NII growth -10% 至 10%、P/TBV 2.5x 至 0.7x 反向。

### 15.9 FINANCIAL_FEE

核心指標：EBIT/revenue、net income/revenue、OCF/net income、net debt/EBITDA、revenue growth、OCF yield。有形淨值保留診斷，但不作此資產輕型模型的必要條件或分數。

Hard failures：

- Net income `<= 0`。
- OCF `<= 0`。
- 淨負債 `> 0` 且 EBITDA `<= 0`；負的 Debt/EBITDA 不得經反向映射變成高分。淨現金時槓桿視為 non-binding，EBITDA 不作此子分數的必要證據。

權重：operating margin 25%、net margin 20%、cash conversion 20%、revenue growth 15%、balance sheet 10%、OCF yield 10%。

映射端點：operating margin 5% 至 35%、net margin 5% 至 25%、OCF/net income 0.5x 至 1.3x、revenue growth -5% 至 15%、net debt/EBITDA 5x 至 0x 反向、OCF yield 2% 至 10%。

### 15.10–15.13 資產管理分流

- `ALTERNATIVE_ASSET_MANAGER`：BX、ARES、OWL、TPG、KKR 類；核心為 FRE、management fee revenue、fee-paying AUM、AUM growth、permanent capital、compensation/revenue。
- `TRADITIONAL_ASSET_MANAGER`：AMG、VCTR 類；核心為 management fee revenue、AUM、organic net flows、AUM growth、compensation/revenue。
- `INSURANCE_LINKED_ASSET_MANAGER`：BAM 類；核心為 FRE、insurance assets、permanent capital、fee-paying AUM 與成本紀律。
- `OTHER_FEE_FINANCIAL`：無法歸入上述三型的費用型金融；使用較保守 required KPI。

公司自訂 KPI 若無 point-in-time 來源，一律維持 `MISSING`；專用模型可 `ABSTAIN` 或降低 coverage，不得拿傳統資產管理 margin 結構替另類資產管理公司補值。

資產管理公司的 balance-sheet 分數只允許 `Net Debt / 正值 FRE 或 EBITDA`；淨現金為 0x。若有淨負債但償債盈餘缺失則 `ABSTAIN`，已知相關 FRE／EBITDA 均非正則 `FAIL`，不可讓負分母產生 100 分。

### 15.14 統一的特殊產業壓力契約

所有十三種特殊模型都輸出 `specialized_stress_status/scenario/survival/missing_inputs/reason`。缺少壓力核心資料時為 `ABSTAIN` 並標記 `Specialized_Stress_Pending`；存活條件失敗為 `FAIL` 並獨立標記 `Specialized_Stress_Failed`，不得把已確認失敗誤寫成待補資料；兩者都不能通過 specialized eligible。

- BANK：累計貸款損失 3%，先由既有 allowance 吸收，增量損失乘 0.79 後減少資本；`Stressed Tier 1 = Reported Tier 1 - After-tax Incremental Loss / RWA * 100`，不得用 total assets 作分母。Tier 1 仍須高於公司揭露最低值，tangible equity/assets `>=3%`；RWA 缺失、非正或錯期時為 `ABSTAIN`，不以資產回填。
- INSURANCE_P_AND_C：沿用 Combined Ratio `+6pp`、保費成長封頂 0%、投資收益率 `-50bps` 的 Moderate 情境。
- INSURANCE_LIFE：保費 `-5%`、投資收益 `-10%`、保戶給付 `+10%`；壓力淨利非負且 equity/assets `>=3%`。
- REIT_EQUITY：EBITDAre `-20%`、利息 `+25%`；AFFO 正值、ICR `>=1.25x`、net debt/EBITDAre `<=10x`。
- REIT_MORTGAGE：資產價值 `-5%`、recurring earnings `-30%`；經常性獲利正值且 equity/assets `>=5%`。
- REGULATED_UTILITY：EBIT 與 OCF `-15%`、利息 `+20%`、CapEx 不變；ICR `>=1.25x` 且 OCF/CapEx `>=0.5x`。
- CYCLICAL_MIDCYCLE：使用 point-in-time 年度 EBITDA 的 P20 trough；trough EBITDA 正值、ICR `>=1.25x` 或淨現金、net debt/trough EBITDA `<=5x`。
- FINANCIAL_LENDER：累計貸款損失 5%，扣除 allowance 後的增量損失稅後減少資本；tangible equity/assets `>=5%`。
- FINANCIAL_FEE：EBITDA `-30%`、OCF `-30%`；壓力現金流與償債盈餘正值，net debt/earnings `<=5x`。
- 四種 asset manager：正值 FRE／EBITDA `-35%`、OCF `-30%`；同樣要求壓力槓桿 `<=5x`。

這些是可稽核的財報情境，不等於 Fed、NAIC 或公司正式監管壓力測試；非標準監管資料仍須人工補查。

## 16. 特殊產業目前仍需人工補查

程式不會用標準 Company Facts 假裝取得下列非標準 KPI：

- 銀行：精確 CET1、uninsured deposits、監管壓力測試。
- 保險：statutory RBC、完整 reserve triangle。
- Equity REIT：same-store NOI、occupancy、公司自訂 AFFO bridge。
- Mortgage REIT：recurring earnings、net spread、repo haircut、duration gap。
- Utility：allowed ROE、正式 rate base、regulatory lag。
- Asset management：FRE、AUM／fee-paying AUM、flows、carry、permanent capital、insurance assets 與公司定義估值橋接。

上述特殊產業已有統一可執行壓力閘門；但 CET1、RBC、reserve triangle、same-store NOI、repo haircut、duration gap、allowed ROE 等非標準 KPI 仍不會由 GAAP Company Facts 猜值，缺失時維持人工補查或 `ABSTAIN`。

## 17. 指標狀態契約

入口：`mode_c_metric_contract.py`

允許狀態：

- `VALID`：有效且有來源證據。
- `ESTIMATED`：有可稽核公式的估計值。
- `MISSING`：缺資料。
- `NOT_APPLICABLE`：該產業不適用。
- `ABSTAIN`：缺資料已影響決策。
- `STALE`：資料過舊。
- `INVALID`：值或來源不合法。

規則：

- `MISSING/NOT_APPLICABLE/ABSTAIN/STALE/INVALID` 不可攜帶數值。
- 重要指標的數值 0 必須有 evidence 或公式，不能把 null 轉成 0。
- 一般企業與各特殊產業有不同 required/applicable metric 集合。
- Maintenance CapEx、壓力測試與 reverse valuation 等衍生值可標為 `ESTIMATED`。
- Dashboard 趨勢統計只使用 `VALID`，不把 `ESTIMATED` 混入群體趨勢。

## 18. 資料信心閘門

一般企業 data confidence 從 100 開始，主要扣分：

- 無 selected evidence：`-35`。
- acceptance coverage `<50%`：`-35`；`<80%`：`-15`。
- period anomaly：每個 `-10`，最多 `-30`。
- fallback concept 比例過高：`-5` 或 `-10`。
- annual fallback：每個 `-10`，最多 `-30`。
- quarter fallback：每個 `-3`，最多 `-15`。
- 一年股數缺失：`-5`；三年缺失：`-8`。
- ROIC 缺失：`-35`。
- 歷史估值無效：`-35`。
- EBITDA 勾稽差異超過 5%：`-10`。
- SBC 使用非 SEC fallback：`-10`。
- Shares 使用非 SEC fallback：`-10`。
- Debt 使用 Yahoo fallback：淨現金 `-10`，有淨負債 `-20`。
- 稅率使用 21% 假設：`-5`。
- Interest 使用 cash-interest proxy：`-10`。
- 拆股後股數仍有重大不連續：`-40`。

信心 `< 70` 一律 `ABSTAIN`，不靠高分補救。

## 19. Global Research Queue 與組合政策

- 只從 `Eligible = True` 中選。
- 一般企業 Raw Score 使用 `Long_Term_Score`，特殊產業使用 `Industry_Model_Score`；Raw Score 只供模型內審計。
- `Within_Model_Percentile` 只在相同最終 `Industry_Model_Key` 內計算。
- 小樣本收縮公式：`50 + n/(n+20) * (percentile-50)`。
- `Shrunk_Within_Model_Percentile` 保留作模型內強弱與樣本量診斷，不再直接拿來跨模型排序。
- Global Research Queue 採 coverage-first model round-robin：先在每個模型內依 percentile、資料信心、ticker 排序；每輪每個模型最多取一檔，輪內以資料信心、模型內 percentile、model key、ticker 決勝，再進下一輪。
- Round-robin 是研究額度分配，不是 sector 持倉上限，也不代表較早輪次具有已校準的較高預期報酬。
- `Cross_Model_Calibration_Status = UNCALIBRATED`，模型內百分位不代表跨模型預期報酬已校準。
- 最多 12 檔。
- 程式中的 sector 檔數硬上限已停用（`MAX_PER_SECTOR = 0`）。
- 分數 60 至 69：watch／research candidate。
- 70 至 74：priority research，不建議直接建倉。
- 75 至 79、80 以上仍保留原模型門檻。一般企業必須再通過組合適配；特殊產業還必須通過人工 KPI、專用壓力與跨模型校準，才能成為 `STARTER_CANDIDATE`。
- 未提供組合檔時，合格候選維持 `Portfolio_Fit_Status=PENDING_INPUT`，不會自動升級為可建倉狀態。特殊產業目前仍因 `Cross_Model_Calibration_Status=UNCALIBRATED` 維持 `PENDING_CALIBRATION`。
- Starter gate 只接受明確 `Portfolio_Fit_Status = PASS`；`pending = false` 不能替代 PASS。人工 KPI、專用壓力與 portfolio pending／failed 等布林欄位也必須能明確解析，缺值或未知字串不得當成 `false` 放行。

投資政策而非目前完整自動最佳化：

- QQQ 40%、VOO 30%、主動個股 0% 至 30%。
- 主動持股通常 8 至 12 檔。
- 單股最多 3%。
- 單一主動產業最多 9%。
- QQQ／VOO 前十大若要額外主動加碼，政策要求至少 80 分。

程式可透過 `MODE_C_PORTFOLIO_FIT_FILE` 讀取 point-in-time 組合輸入，逐檔檢查資料新鮮度、post-trade 單股穿透曝險 3%、主動部位 30%、主動 sector 9%、economic-risk bucket 9%、ETF top-10 的 80 分門檻，以及 correlation stress 是否 `PASS`。單股穿透曝險明確等於「現有直接持倉 + ETF look-through + 擬新增部位」，避免 ETF 重疊造成偽分散。輸入缺失、過舊、格式錯誤或壓力覆核未完成時分別維持 `PENDING_INPUT`、`STALE`、`INVALID` 或 `PENDING_REVIEW`，不能建倉。

這個契約不會自行下載 ETF 最新持股、不會替使用者推導現有持倉／相關性，也不是組合最佳化器；CSV 中的曝險與壓力狀態必須由外部的 point-in-time 組合程序產生。因此它是可稽核的 pre-trade gate，不是即時風控平台。

Portfolio input v3 將時間轉成 UTC 後逐時間戳比較 `AsOf <= Decision_Timestamp`，同日稍晚才產生的資料也會拒絕；45 天新鮮度仍依 UTC 日期差計算。`ETF_Top10_Overlap` 必須明確為 true／false（可接受 1／0、yes／no），缺值或未知不可當成 false；缺少 economic-risk bucket 也不可通過。輸出 validator 同步檢查完整時間戳。

`mode_c_decision_inputs.py` 另提供 calibration input audit：要求每筆特徵與 universe membership 在 decision timestamp 前可得、forward label 已成熟、全資料使用同一且可由日期重算的 horizon、使用一致 benchmark、同一 benchmark 起訖窗口的報酬完全一致、`Forward Excess Return = Security Return - Benchmark Return`、包含下市證券與交易成本、無重複觀察，並達最低樣本數與年份。模型擬合目標明定為 excess return。通過只代表 `ELIGIBLE_FOR_MODEL_FITTING`，不代表模型已校準，也不會把目前的 `UNCALIBRATED` 自動改為可比較。

Calibration input v3 明確拒絕 NaN、正負無限大與非數值分數／報酬／horizon；空白 nullable boolean 也不能冒充已包含交易成本或下市股票。

## 20. Dashboard 的高分群聚與研究線索

Dashboard 內建硬編碼研究主題，例如：

- AI 晶片。
- 資料中心電力與散熱。
- 電氣化／電網。
- AI 軟體／資安。
- 醫療防禦。

這些標籤只用於研究分組，不是股票 eligible 條件。

趨勢群組最低樣本：sector 5、industry 3、theme layer 2；群體欄位至少 50% coverage 才顯示。群聚訊號使用平均收縮後模型內百分位、eligible／queue 數量、資料信心、KPI coverage 與可用營運變化，不以跨模型 Raw Score 建立風口。模型版本改變時重設 trend baseline。

## 21. Validator 的不可變條件

入口：`validate_mode_c_outputs.py`

每次主要執行後檢查：

- 指標狀態與 numeric value 是否一致。
- PASS／FAIL／ABSTAIN 與原因是否一致。
- Eligible 是否真的通過所有硬門檻。
- Debt 組件、Market Cap／EV、FCF、yield、stress 與反向估值公式是否可重算。
- 銀行壓力是否使用同一 metrics JSON 中的正值 RWA，且增量貸損、稅後資本損失、stressed Tier 1 與存活狀態能被獨立重算；舊 total-assets 分母會被拒絕。
- 股數拆股與不連續處理是否一致。
- 產業路由與 initial/refined model 是否一致。
- 特殊模型 required metric、coverage、confidence 是否一致。
- FCF／估值歷史是否達最低要求。
- Evidence ledger 是否 point-in-time、無未來 evidence、無循環。
- 重要 0 是否有證據，`invalid_zero` 必須為 0。
- Global Research Queue 是否等於 eligible 股票的 coverage-first model round-robin 前 12 名，且 Raw Score／收縮百分位未被冒充跨模型 Alpha。
- DSI `NOT_APPLICABLE` 是否保持 null、因子權重是否正規化。
- Working-capital 狀態、coverage、15pp／20pp 門檻與 5／10 分扣分是否一致。
- P&C proxy／公司口徑、壓力狀態、Starter gate 是否一致。
- 稀釋是否只在一個評分層扣分、回購橋接是否等於 gross buyback 減 issuance、併購發股歸因與 accretion review 是否一致。
- 稀釋總分影響是否依實際 `Available_Factor_Weight` 重算，而非永遠套固定 5%。
- Portfolio fit 的狀態、pending flag、版本、資料日齡、pre/post-trade 曝險橋接、ETF top-10、correlation stress、上限與 Starter gate 是否一致。
- 歷史估值 quantile 是否符合樣本數。
- Dashboard 與 CSV／JSON 結果是否一致。

任一不可變條件失敗，GitHub Actions 不發布 dashboard。

## 22. GitHub Actions 排程

入口：`.github/workflows/alpha_hunt.yml`

- 每週四 22:00 UTC：美股週四盤後，台灣時間週五 06:00，使用最近完整 `qualified_universe.csv` 跑 Mode C。
- 每月 1 日 03:00 UTC：重跑全市場 hunter，再跑 Mode C。
- 可手動 dispatch，並選擇是否先跑 hunter。
- PR 只跑 compile、測試與品質檢查，不跑昂貴全市場抓取。
- 月度 hunter 中途失敗時保留上次完整 universe，不用 partial 覆蓋。
- 成功後輸出 CSV、JSON、evidence、audit、dashboard artifacts；符合 Pages 條件才部署網站。

## 23. 建議另一個 AI 優先攻擊的問題

以下不是已證實 bug，而是最值得做破壞性審查的假設：

1. Maintenance CapEx 的 D&A + revenue-growth 啟發式，能否跨 software、semiconductor、telecom、industrial 與 commodity 公司一致成立？
2. Growth CapEx 已改為多證據分級；請檢查 ROIC 8%、兩年營收衰退與至少兩項佐證的門檻，是否仍會錯殺長建設週期或監管型資本支出。
3. 目前已把一般企業季度資料不足改為 ABSTAIN；請繼續搜尋是否仍有漏網的 missing-data FAIL。
4. 十三種特殊模型已有財報型壓力情境；請攻擊 3%／5% credit loss、REIT 資產與 EBITDAre shock、utility funding shock 是否過鬆或過嚴，並區分正式監管壓力測試仍缺的資料。
5. 未校準模型已改用 coverage-first round-robin；請檢查它是否過度獎勵小模型或犧牲同一模型內第二名，直到 PIT walk-forward 能提供真正校準前都不得稱為 alpha 排名。
6. 毛利 `<25%` 的 peer check 目前實際只警示、不淘汰，是否還有保留此規則的必要？
7. DSI 已對服務業、平台、金融、訂閱型軟體與低庫存實質性公司設為 N/A；請攻擊目前庫存實質性門檻是否穩健。
8. Short squeeze 已改為分數中性；後續若取得可靠 point-in-time 借券資料，再驗證它應作催化劑、風險或兩者分流。
9. 反向估值已改為 40/40/20 公司、同業與利率混合，並移除跨產業同模型 fallback；請檢查 industry 3 家、sector 5 家與只納入 PASS peers 是否仍會產生樣本偏誤。
10. ROIC 已改用期初期末平均投入資本；請檢查季度平均與重大併購日內時點仍可能造成的偏差。
11. OCF 已加入 AR／Revenue、AP／COGS、DSO、DPO、deferred revenue 與 CCC 分解，並要求至少 5 天實質曝險；請攻擊 15pp／20pp、5 天門檻、300 至 450 天配對窗口，以及不同行業營運資金季節性是否仍會誤判。
12. Acquisition-related issuance 已用直接 XBRL 股票對價辨識並轉為 accretion review；股票發行現金流不再是必要條件。請檢查公司 extension、非股票對價、分期交割與 450 天新鮮度是否仍會漏判。
13. `Real FCF Yield >=2%`、`ICR >=3x`、市值 5B、流動性 15M 等靜態門檻，是否應按產業、利率、波動與景氣階段校準？
14. 歷史估值已依樣本數使用 P25/P20/P15/P10；至少 5 年仍未必覆蓋完整景氣週期。
15. 使用目前 Yahoo universe 進行任何歷史測試會有 survivorship bias；在完成 delisted securities 與 historical constituents 前，不應宣稱年化績效。
16. Dashboard 的硬編碼主題會不會造成敘事偏誤？主題只分組但可能影響人工注意力。
17. ETF top-10、單股 3%、sector 9%、economic-risk 9% 已有可選 pre-trade 輸入契約，但持倉、ETF look-through 與相關性仍不會自動生成；請檢查外部組合資料 lineage 與 stale policy。
18. 外國公司 annual fallback 與 550 天 freshness 容忍，是否會讓不同申報頻率的公司資料可比性失真？
19. XBRL 同義標籤與公司 extension 的跨公司映射，是否有足夠 fixture 覆蓋負值符號、restatement、duplicate facts、52/53 週年度與併購重編？
20. 已有防 look-ahead／survivorship／交易成本、混合 horizon 與 raw-return regime bias 的 calibration input audit，但尚未取得合格歷史面板、擬合模型或完成 walk-forward、out-of-sample IC、turnover、drawdown 與 calibration curve；不能只靠財務直覺認定閾值與權重最優。

## 24. 可直接貼給另一個 AI 的審查提示詞

```text
你是一名頂級量化基本面研究主管與資料工程審計員。以下文件是我目前股票篩選程式的實際邏輯，不是概念提案。

請做破壞性審查，但不要只提出更多指標，也不要用空泛的「需要回測」結束。請逐項：

1. 找出公式錯誤、資料洩漏、look-ahead bias、survivorship bias、單位／符號／期間錯置、重複計分、產業錯配與 FAIL/ABSTAIN 語義錯誤。
2. 區分會造成錯選、會造成漏選、只影響排序、只影響顯示的問題。
3. 特別檢查 TTM、Maintenance CapEx、SBC、ROIC、歷史 EV/EBITDA、反推 CAGR、DSI、稀釋與壓力測試。
4. 檢查一般企業與十三種特殊產業模型路由的硬門檻、權重，以及收縮後研究順序是否仍有系統偏差。
5. 每個問題給出：嚴重度、具體反例、建議新公式／狀態流程、所需資料、最小測試案例。
6. 修正時必須維持 point-in-time：available_to_model_at 不得晚於 decision_timestamp；禁止單季乘四年化；缺核心證據不得默認為 0。
7. 優先改善資料正確性、決策語義、產業模型與組合風險，不要先增加表面上的因子數量。
8. 最後給出 P0/P1/P2 實作清單，以及哪些建議在沒有無偏 walk-forward 驗證前不應上線。

請明確區分「程式已實作」、「政策但未自動執行」、「建議新增」，不要把三者混在一起。
```

## 25. 主要程式對照

- `total_market_hunter_v2.py`：每月 universe 與第一層初篩。
- `mode_c_routing.py`：共用產業路由。
- `mode_c_industry_models.py`：特殊產業初篩與十三種專用深篩路由。
- `mode_c_research_priority.py`：模型內百分位、小樣本收縮、研究狀態與全球研究佇列。
- `mode_c_decision_inputs.py`：PIT 校準資料稽核與可選的組合適配輸入契約。
- `AQR_ModeC_Agent_V12.py`：point-in-time SEC 資料、一般企業評分、產業分流與研究輸出。
- `mode_c_evidence.py`：證據帳本。
- `mode_c_metric_contract.py`：指標狀態與適用性。
- `validate_mode_c_outputs.py`：輸出不可變條件。
- `build_mode_c_dashboard.py`：靜態研究網站與群體趨勢。
- `.github/workflows/alpha_hunt.yml`：每週／每月排程與發布。
