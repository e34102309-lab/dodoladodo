# Alpha Engine V9：QQQ 40% / VOO 30% / 主動個股最多 30%

本專案用 ETF 承擔核心市場報酬，主動個股只作有紀律的研究與提高長期報酬機會。程式是可審計的研究漏斗，不是自動買入或保證提高年化率的訊號。

2026-10-24 起只追蹤使用者確認的固定 100 家 `research_watchlist.csv`。每週更新這 100 家的 Yahoo／SEC 資料並分析；已取消每月全市場抓取與手動刷新入口。

## 投資原則

- QQQ 目標 40%、VOO 目標 30%、主動個股 0%～30%。
- 沒有好標的時不必買滿 30%，其餘可留在 ETF 或現金。
- 主動持股通常 8～12 檔，起始 1%～1.5%，單股最多 3%，單一主動產業最多 9%。
- 只做多，不使用槓桿、期權或放空。
- 銀行、保險、REIT、公用事業、景氣循環股、特殊放款與金融服務會進入各自的初篩及深篩；缺少專用核心證據時一律 `ABSTAIN`，不得回退套用一般企業分數。
- 買入前必須完成投資論點、反方、失效條件、三情境、最新財報、稀釋、資本配置與 ETF 重疊檢查。

完整規則見 `INVESTMENT_POLICY.md`，研究紀錄見 `investment_journal_template.md`。

## 主要檔案

- `research_watchlist.csv`：正式固定 100 家名單，至少包含 `Ticker,CIK`；只有人工確認後才修改成分。
- `mode_c_watchlist.py`：連網前驗證家數、Ticker、CIK 與重複，並計算實際輸入檔版本。
- `total_market_hunter_v2.py`、`qualified_universe.csv`：保留舊邏輯與歷史資料，hunter CLI 已停用，主程式不會讀取舊全市場名單。
- `AQR_ModeC_Agent_V12.py`：價值、品質、預期、資本配置與下檔風險評分引擎。
- `mode_c_evidence.py`：point-in-time evidence ledger、來源血緣與衍生公式紀錄。
- `mode_c_metric_contract.py`：統一 `VALID/MISSING/NOT_APPLICABLE/ABSTAIN/STALE/INVALID/ESTIMATED` 指標狀態，防止缺值被顯示成零。
- `mode_c_routing.py`：第一層與 Mode C 共用的產業模型路由。
- `mode_c_industry_models.py`：九種專用產業模型的純計算、硬性風險、覆蓋率與信心閘門。
- `build_mode_c_dashboard.py`：把評分結果整理成不依賴外部 AI 的靜態研究網站。
- `enhance_dashboard_ui.py`：在網站生成後整理候選風口區塊，加入使用說明與可點選篩選互動。
- `mode_c_fixture_pipeline.py`：以 GLW、PGR、IFNNY 固定資料跑完整 CSV、JSON、dashboard 與 validator 整合測試。
- `run_mode_c_ai_agent.py`：保留為選用工具，不再由主要 GitHub Actions 自動呼叫。

## 固定 100 家的更新與分析

先執行不連網的名單檢查：

```bash
python mode_c_watchlist.py
```

設定 SEC 聯絡信箱後，執行下方 Mode C 指令即可。主程式一律讀取專案內的 `research_watchlist.csv`，不讀取舊 `qualified_universe.csv` 或 `QUALIFIED_UNIVERSE` 環境變數，也不搜尋其他股票。

- 固定名單必須恰好 100 家，Ticker 與 CIK 均唯一且有效；檔案不存在或內容異常時，連網前立即停止。
- 每次只更新這 100 家的價格、公司資訊與 SEC 財報。SPY、QQQ、^TNX 只供既有動能、估值與利率基準。
- 個別公司不合格或資料不足時仍保留 FAIL／ABSTAIN 與原因，不從外面補足候選，也不因評分變化自動換股。
- 第一版名額為科技 25、工業 20、醫療 20、必需消費 15、非必需消費 12、通訊與媒體 8；同一細產業最多 4 家。這些是研究名額，不是持倉權重。
- 名單源自 2026-09-01 初篩快照及 2026-07-12 Yahoo 資訊快取。初篩數字保留原日期，最新分析以每次產生的 `mode_c_screen.csv` 為準。
- Yahoo 資訊暫時缺失時，只沿用名單中已驗證的 quote type、交易所及產業身分資訊；不以舊快照補入價格、負債、SBC 或財報數字。
- 名單固定保存，只有人工確認後才替換公司；編輯後先跑 `python mode_c_watchlist.py`。缺失資料不等於這家公司應移出研究範圍。
- 舊全市場 hunter CLI 已停用，執行會直接停止且不發送抓取請求。舊模組與原始 universe 保留作歷史及離線測試。

## Mode C 評分邏輯

長期綜合分數：

- 品質 35%：ICR、maintenance CapEx 版 Real FCF、ROIC、毛利趨勢與五年現金流穩定性。
- 價值 30%：歷史 EV/EBITDA 分位與 Real FCF Yield。
- 市場預期差 20%：以 10% 必要報酬折現後，反推 EBITDA 成長是否合理。
- 動能 5%，僅作輔助。
- 營運拐點 5%：DSI 連續趨勢必須再通過年對年季節性確認。
- 資本配置 5%：回購是否真正降低股數、增發與持續稀釋。
- 再扣除壓力測試、價值陷阱與其他基本面風險；資料完整性由獨立信心閘門處理。

SEC 資料使用 Company Facts 合併跨年代的同義 XBRL 標籤，並以 Submissions API 的 accession/acceptance metadata 建立 point-in-time availability。每個 fact 都必須滿足 `available_to_model_at <= decision_timestamp` 才能進入計算；年度 selector 同時支援 10-K、20-F 與 40-F，YTD/TTM 再鎖定最新 period end 與最合理 duration。20-F/40-F 若缺少可比季報，只能作 annual fallback 並扣資料信心；外幣或尚未建立可靠映射的 IFRS facts，在沒有 point-in-time 匯率與可稽核換算鏈時維持 `ABSTAIN`，不把外幣數字冒充美元。若 acceptance timestamp 缺失，只能用「filing date 次日」保守 fallback，並降低資料信心。

資料信心是決策閘門，不是新的 alpha 因子。系統會檢查 acceptance 覆蓋、替代 XBRL tag 比例、申報期間異常、核心期間是否過舊、TTM fallback、股數歷史、負債來源、ROIC、歷史估值與 EBITDA 勾稽；低於 70 分直接 `ABSTAIN`，不讓缺值以中性分數混入 shortlist。一般企業總負債會用同一報表期的 SEC 組件去重組裝；無法建立時只允許使用明確的 Yahoo `totalDebt` 當日 fallback，現金完全覆蓋負債時扣 10 分、有淨負債時扣 20 分，兩邊都缺則直接 `ABSTAIN`，不把未知負債當成零。淨現金公司缺少獨立利息標籤時，ICR 不作硬門檻；有淨負債則必須有利息證據。若現金高到使 EV 非正，一般 EV Yield、EV/EBITDA 與反向估值不再可比，必須 `ABSTAIN` 交由淨現金特殊情境研究，不以 `0.01` 分母製造假便宜。歷史股數會先依 corporate actions 轉成同一拆股基準；歷史市場價格與股數也會先對齊拆股口徑，再重建 Market Cap、EV/EBITDA 與 P/E，拆股因子逐期寫入 evidence ledger。調整後仍超過 50% 的股數口徑跳變視為 IPO/重組/重編待查，不會誤判成持續稀釋。產業欄位缺失時同樣 `ABSTAIN`，不會默認套用一般企業模型。

核心資料新鮮度依申報制度分流：國內 10-Q/10-K 最長 240 天，20-F/40-F 外國年度申報最長 550 天。較長上限不會套到缺季報的美國公司。

這個 ledger 先解決 SEC 財報證據的 point-in-time 問題，不代表完整歷史回測已經無偏。正式 walk-forward 前仍需補齊歷史成分股、已下市股票、當時可得的市場/分析師資料與交易成本，不能拿目前的 Yahoo metadata 回填過去。

新增深篩包括近三年連續年度累計 OCF、最多五年具完整 SBC lineage 的連續 maintenance-FCF 正值年數與 margin 穩定性、OCF/Net Income、Real FCF/Net Income、ROIC、ROCE，以及拆股調整後的一年與三年股數變化。可得歷史至少三年時，Real FCF 必須至少 60% 年度為正。CapEx 會用 D&A 與營收成長拆分 Maintenance CapEx / Growth CapEx；全額 CapEx FCF Yield 仍保留作保守壓力欄位。單一年稀釋改為扣分；近三年股數累計增加超過 3% 才視為持續稀釋並排除。

市場隱含 EBITDA CAGR 不再用當期 EV 直接除終值倍數，而是假設 10% 必要報酬後反推三年後 EBITDA。15% 以上的容忍度用 ROIC 與近三季毛利變化連續調整；動態上限主要用來計算預期負擔分數，不在 30% 或任一單點直接歸零。毛利失血達 2 個百分點時，容忍度仍封頂 15%。

產業檔數不設硬上限；每家公司另做 EBITDA 下滑 15%/30% 的財務存續測試。壓力情境固定 D&A、重算 EBIT、ICR、淨負債/EBITDA 與 Real FCF；30% 情境 ICR 低於 1.5x（淨現金公司除外）不得進入長期候選，有淨負債者 stressed Real FCF 也不得轉負。淨現金公司可容許一年壓力燒錢，但現金必須足以覆蓋該缺口。

每次量化執行後會再跑 `validate_mode_c_outputs.py`：核對決策狀態、eligible 硬門檻、分數優先 shortlist、拆股後股數口徑，以及 evidence ledger 的 point-in-time availability 與完整來源鏈。任一不變條件失敗時 GitHub Actions 立即停止，不發布 dashboard 或研究檔。

## 產業專用深篩

- 銀行：Tier 1 相對公司揭露的 well-capitalized minimum、ROTCE、tangible equity/assets、deposit funding、allowance/loans、NII 趨勢、AOCI/tangible equity。
- P&C 保險：`BenefitsLossesAndExpenses / PremiumsEarnedNet` 的 combined-ratio proxy、premium growth、reserve development、equity/assets、ROE；claims 與 acquisition cost 只作拆解，不重複加總。
- 壽險：保費加投資收益對保戶給付、equity/assets、ROE、保費成長與 P/B。
- 權益型 REIT：依 Nareit 口徑建立 FFO 與 EBITDAre proxy，再以 maintenance CapEx 建立 AFFO proxy，檢查 payout、net debt/EBITDAre、利息覆蓋及租金成長；若 Company Facts 沒有近期可驗證的 recurring/maintenance CapEx，不拿物業收購支出冒充，直接 `ABSTAIN` 等待公司自訂 AFFO bridge 人工覆核。
- Mortgage REIT：book capital、assets/equity、ROE、GAAP dividend-coverage proxy、P/B 與 NII 趨勢；公司定義的 recurring earnings/net spread、repo haircut、duration gap 必須人工補查，不能以 GAAP 淨利冒充可分配盈餘。
- 公用事業：ICR、debt/capital、OCF/CapEx、ROE、PP&E growth proxy 與股息覆蓋；現金 CapEx 缺標準標籤時才以「PP&E 淨增加 + D&A」代理並扣資料信心，allowed ROE、正式 rate base 與 regulatory lag 必須人工補查。
- 景氣循環股：至少五個連續年度 annual EBITDA，分離 peak/current/mid-cycle/trough，核心估值改用 EV/mid-cycle EBITDA，並以 trough ICR 與 net debt/trough EBITDA 做存活測試；Real FCF 缺 SBC evidence 時不得把 SBC 當零。
- 特殊放款／金融服務：分成 lender 與 fee business，分別檢查 tangible capital、allowance、ROTCE，或 margin、cash conversion、organic revenue growth 與 balance sheet。

SEC Company Facts 只提供跨公司可比較的標準 taxonomy。CET1 精確值、uninsured deposits、statutory RBC、occupancy、same-store NOI、allowed ROE、AUM flows 等非標準或監管資料會列入信心扣分與人工待辦，程式不會把代理值冒充公司原始揭露。

方法口徑以 [SEC EDGAR XBRL API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)、[FDIC Bank Data API](https://api.fdic.gov/banks/docs)、[NAIC insurance glossary](https://content.naic.org/glossary-insurance-terms)、[Nareit FFO](https://www.reit.com/glossary/funds-operation-ffo) / [EBITDAre](https://www.reit.com/glossary/ebitdare) 與 [FERC Form 1](https://www.ferc.gov/general-information-0/electric-industry-forms/form-1-electric-utility-annual-report) 為準。

## 分數用途

- 60 分以上：研究候選。
- 70 分以上：優先研究。
- 75 分以上：完成研究後可考慮 1% 小部位。
- 80 分以上：較高優先度，可考慮 1.5% 起始部位。
- 若為 QQQ/VOO 最新前十大，至少 80 分才可考慮額外主動加碼。

加碼至少要等一次財報，確認 thesis、一般企業 Real FCF 或專用產業核心 KPI、股數及估值未惡化。分數跌破 60、資料信心跌破 70、一般企業硬性風險或專用模型 hard failure、明顯稀釋、資本配置失控、thesis 被證偽或估值過高時，必須強制檢討。

## 執行 Mode C

```bash
pip install -r ModeC_requirements.txt
export USER_EMAIL="your_email@example.com"
python AQR_ModeC_Agent_V12.py
python build_mode_c_dashboard.py
python enhance_dashboard_ui.py public/index.html
python validate_mode_c_outputs.py --dashboard public/data.json --zero-report mode_c_zero_audit.json
python mode_c_fixture_pipeline.py --output-dir fixture_output
```

完整的缺值、產業適用性、FCF 分母、CapEx 敏感度與 dashboard 稽核紀錄見 `METRIC_STATUS_AUDIT.md`。

本機產生的網站位於 `public/index.html`。GitHub Actions 每週於美股週四收盤後（台灣時間週五 06:00）只更新、分析 `research_watchlist.csv` 的固定 100 家；手動執行亦相同。沒有月度全市場排程，不會自動提交或替換名單。push／PR 只跑名單檢查、compile 與單元測試。

## 靜態研究網站

主要工作流程完成量化分析後，永遠會上傳 GitHub Actions artifact；如果 repository 是 public 且 GitHub Pages 已設定為 `GitHub Actions` 來源，還會自動部署線上網站。

- Private repo / 免費帳號：下載 `Alpha_Engine_Static_Dashboard` artifact，解壓縮後打開 `index.html`。
- Public repo / GitHub Pages 啟用：直接刷新線上網站。

線上網站網址：

`https://e34102309-lab.github.io/dodoladodo/`

artifact 下載方式：

1. 到 GitHub repository 的 `Actions`。
2. 打開最新一次 `Mode-C Fixed 100 Research Pipeline`。
3. 在頁面下方 `Artifacts` 下載 `Alpha_Engine_Static_Dashboard`。
4. 解壓縮 zip。
5. 直接打開 `index.html`。

網站功能：

- 搜尋、分數門檻、Shortlist、合格名單與自訂追蹤清單。
- 主題擴散鏈：把 AI 晶片、資料中心電力與散熱、電氣化、AI 軟體與資安、高品質醫療等主題拆成一階、二階、三階受益層，協助安排研究順序。
- 候選風口偵測：每次跑完依產業、行業與主題層級統計平均分數、合格公司數、shortlist 公司數、營收變化、毛利變化與 Real FCF Yield，並與上次 workflow 保存的基準比較。系統只標記「待人工確認」候選，不會自動加入正式主題庫。
- 候選風口卡片可點選；點擊後，下方股票表會自動篩出該產業、行業或主題層級的相關公司，方便從「可能風口」往下檢查實際標的。
- 點擊主題卡片可篩出相關公司，卡片會列出每一層的高分候選。例如 AI 晶片的一階是核心算力與晶片，二階是半導體設備、記憶體、電源管理、散熱、連接器、被動元件與測試量測，三階是資料中心基建外溢。
- 點擊股票查看它屬於哪個主題與受益層級，並檢查品質、價值、Real FCF、ICR、ROIC、ROCE、稀釋、估值與壓力測試。
- 直接開啟 SEC 官方公司申報頁及 Yahoo 財務資料頁。
- 一鍵複製固定格式的 AI 研究提示，再貼到你慣用的 AI 手動查核；提示會要求判斷該公司是否真的是一階、二階或三階受益者，以及二階受益是否已開始進財報。
- 追蹤名單只保存在目前瀏覽器的 `localStorage`，不會公開或上傳，並可匯出文字檔。

候選風口的設計原則：已知主題內部的股票與分數會自動更新；新主題只會被提出為候選，等你人工確認後，才適合加入正式主題庫。這樣可以追蹤「晶片 → 被動元件」、「AI cluster → 電力散熱」這種二階受益鏈，同時避免被短期股價亂帶方向。

主要流程不再需要 `GEMINI_API_KEY`，也不會因 Gemini 503 高需求錯誤而讓本批研究失敗。`Mode_C_Long_Term_Value_Outputs` artifact 仍會保留完整 CSV、Markdown、JSON 與 `public` 網站資料；`Alpha_Engine_Static_Dashboard` 則是給你最快打開網站用的精簡 artifact。

## 輸出

- `mode_c_screen.csv`：全部公司與落選原因。
- `mode_c_shortlist.csv`：分數優先、最多 12 檔研究候選；產業風險改由權重上限與壓力測試處理。
- `mode_c_evidence_ledger.csv`：只輸出實際採用的來源 facts 與衍生 metrics，包含 accession、acceptance time、可用時間、選用角色、衍生公式與 source evidence IDs；未被模型使用的大量候選 facts 不寫入正式 artifact，避免全市場輸出膨脹。
- `mode_c_zero_audit.json`：逐筆分類重要指標的真零值與無效零值；`invalid_zero` 非零時 pipeline 直接失敗。
- `mode_c_report.md`：長期價值研究摘要。
- `mode_c_agent_payload.json`：供手動 AI 研究或其他工具使用的九項反證任務包。
- `public/index.html`：可直接開啟的研究網站。
- `public/data.json`：網站使用的完整結構化資料。
- `.mode_c_state/fixed_watchlist_dashboard_trend_history.json`：固定名單的趨勢基準，與舊全市場趨勢分開；GitHub Actions cache 依名單內容隔離，避免把範圍縮小誤判為產業變化。
