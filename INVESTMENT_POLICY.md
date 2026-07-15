# 40/30/30 投資政策

## 目的

以 QQQ 40% 與 VOO 30% 承擔主要市場報酬；把 0%～30% 資產當成有紀律的主動研究實驗。模型只負責縮小研究範圍與暴露風險，不是自動買入訊號。

## 配置限制

- QQQ：目標 40%。
- VOO：目標 30%。
- 主動個股：0%～30%；沒有好標的時可只持有 10%～20%，其餘留在 ETF 或現金。
- 持股數：通常 8～12 檔，不為湊數而買入普通公司。
- 起始部位：1%～1.5% 總資產。
- 單一公司：最多 3% 總資產。
- 單一主動產業：最多 9% 總資產。
- 禁止：槓桿、融資、裸空、選擇權事件交易，以及用攤平取代重新研究。

## 模型適用範圍

模型先依產業路由。銀行、P&C/壽險、權益型/房貸型 REIT、公用事業、景氣循環、特殊放款與 fee-based 金融服務各自使用獨立初篩和深篩。任何路由缺少專用核心證據時一律 `ABSTAIN`，不能回退套用一般企業 OCF、CapEx、Real FCF 或 Debt/EBITDA 分數。

前置價格/流動性/市值閘門即使先失敗，也必須保留月度已驗證的原始 route，不能用一般企業預設值掩蓋「模型尚未執行」。唯一允許的深層 route refinement 是 `FINANCIAL_FEE -> FINANCIAL_LENDER`：SEC 必須同時顯示 loans/assets 至少 20% 與信用損失準備，且輸出需明示初始 key、refinement 旗標與原因。

所有 SEC 輸入必須保存 accession、原始 tag、期間、filing/acceptance time 與 `available_to_model_at`。只有 `available_to_model_at <= decision_timestamp` 的 evidence 可進入模型；修訂 filing 只能從其實際可用時間起生效。替代 tag 比例過高、申報期間異常、資料信心低於 70、關鍵來源無法反查、產業欄位缺失或專用模型缺失時，一律降級或 `ABSTAIN`。總負債必須由同報表期 SEC 組件去重組裝；若只剩市場資料 fallback，現金完全覆蓋負債時資料信心扣 10 分、有淨負債時扣 20 分，SEC 與市場資料皆無法確認時不得假設為零。淨現金公司可豁免缺失的利息標籤；有淨負債則利息證據是硬門檻。

可用現金只接受 cash-and-equivalents 本身，不把 restricted-cash composite 當成可自由償債的流動性。

## 初篩規則

- 市值至少 50 億美元。
- 一般企業最近一年 OCF 必須為正；近三年累計 OCF 在 Mode C 深篩再次驗證。
- 毛利率非正才在第一層排除；低於 25% 必須做同業比較，但低於中位數或樣本不足只標記警示並交給 SEC 深篩，不以單一毛利率淘汰低毛利、高周轉企業。
- 有可靠現金資料時使用 Net debt/EBITDA：<=4 通過、4～5 警示、>5 排除，並保留 Gross debt/EBITDA 供稽核；現金缺失才保守回退 gross debt 口徑。
- 專用產業由第一層執行自己的低誤殺硬性檢查，再轉送專用 SEC 模型；Yahoo 欄位缺失不等於中性分數，會保留警示並由深篩決定 `PASS`、`FAIL` 或 `ABSTAIN`。
- 一般企業的 Yahoo 核心欄位缺失同樣只標記後送 SEC；明確非正或槓桿超標才在第一層淘汰，SEC 仍缺核心 evidence 時必須 `ABSTAIN`。
- 股類身分採分級證據：Nasdaq Trader symbol directory 明示 Preferred、Unit、Right、Warrant、Note 或 Bond 時剔除；泛稱 ADS/ADR 必須再由最新可得 10-K/20-F/40-F cover-page 的 `Security12bTitle`/`TradingSymbol` 配對確認。SEC 仍只有泛稱時，僅允許「同 CIK 唯一 ADS」或「另一 class 已明示 preferred」兩種中等信心推定；其餘只列 `Review`。推定 ADS 在深篩仍承受外國申報信心折扣與幣別/準則閘門。
- 同一 CIK 的多個普通股 class 以官方股類證據和平均日成交金額選擇，不用 Yahoo 以全公司股數估算的 class market cap 排序，避免把優先股或低流動性股類帶入估值。

## Mode C 深篩

一般企業才使用以下 Real FCF、ROIC、毛利、DSI 與 EV/EBITDA 規則；專用產業不得混用。

- Real FCF = OCF - Maintenance CapEx - SBC，且 TTM 必須為正；全額 CapEx 版本另列為保守壓力測試。
- Maintenance CapEx 以 D&A 與近年營收成長估算；CapEx 遠高於 D&A 但營收不成長時，視為 Growth CapEx 陷阱並排除。
- ICR 至少 3 才能成為長期研究候選。
- EBITDA 下滑 15%/30% 時固定 D&A 並重算 EBIT、ICR、淨負債/EBITDA 與 Real FCF；30% 情境 ICR 必須至少 1.5x，淨現金公司除外。有淨負債者 stressed Real FCF 不得轉負；淨現金公司若轉負，現金仍須足以覆蓋一年壓力缺口。
- 檢查 ROIC、ROCE、近三個連續年度累計 OCF、最多五個具完整 SBC evidence 的連續年度 Real FCF 正值年數、Real FCF margin 穩定度、OCF/Net Income 與 Real FCF/Net Income；可得歷史至少三年時，正值年數必須達 60%。
- 單一年回購但股數增加：扣分與警示，不單獨排除。
- 所有歷史股數先依 corporate actions 轉為決策日拆股基準；歷史估值使用的價格與股數也必須位於同一拆股口徑，每期拆股因子納入 Market Cap 衍生證據鏈。拆股調整後仍有超過 50% 的口徑跳變時，不得直接判成稀釋，必須降低資料信心並 `ABSTAIN` 等待人工核對 IPO、重組或股本重編。
- 近三年股數累計增加超過 3%：視為持續明顯稀釋並排除。
- 市場隱含 EBITDA CAGR：先要求目前 EV 在三年內取得 10% 年化必要報酬，再以退出 EV/EBITDA 反推。-10%～15% 高分；15% 以上依 ROIC 與近三季毛利趨勢連續調整，動態上限是軟性預期負擔而非單點歸零。毛利失血 2 個百分點以上時容忍度封頂 15%。
- DSI 拐點必須同時具備連兩季改善與年對年下降確認；僅有季節性連降不得當作去庫存完成。
- Short Interest 與 Days to Cover 只有在資料日期不超過 45 天時才可啟用軋空風險旗標，且不得放寬基本面門檻。

## 專用產業深篩

- 銀行：Tier 1 buffer、ROTCE、tangible equity/assets、deposit funding、allowance/loans、NII growth、AOCI/tangible equity。
- 金融業未揭露 goodwill 或無形資產時，tangible equity 可暫以零調整計算，但必須列為 optional evidence gap 並降低資料信心，不能宣稱已確認為零。
- P&C 保險：直接使用 total benefits/losses/expenses 對 earned premium 的 combined-ratio proxy，另查 premium growth、reserve development、capital ratio 與 ROE；不得把 acquisition cost 再重複加進總費用。
- 壽險：premium + investment income 對 policyholder benefits、capital ratio、ROE、premium growth、P/B。
- 權益型 REIT：Nareit FFO/EBITDAre proxy、maintenance-CapEx 後 AFFO proxy、dividend/AFFO、net debt/EBITDAre、interest coverage、lease growth；maintenance CapEx 缺失時不得用物業收購支出替代，必須 `ABSTAIN` 並人工核對公司 AFFO bridge。
- Mortgage REIT：assets/equity、equity/assets、ROE、GAAP dividend-payout proxy、P/B、NII growth；公司定義的 recurring earnings/net spread coverage 必須人工驗證，GAAP 淨利不足覆蓋只作警示而非單點否決。
- 公用事業：ICR、debt/capital、OCF/CapEx、ROE、PP&E growth proxy、earnings/dividend coverage；現金 CapEx 缺失時允許以 PP&E roll-forward 加 D&A 代理，但必須降資料信心並保留人工覆核。
- 景氣循環：至少五個連續年度 annual EBITDA；以 median 作 mid-cycle、20th percentile 作 trough，檢查 EV/mid-cycle、trough ICR 與 net debt/trough EBITDA；SBC 缺失不得當零。
- 特殊放款：tangible capital、ROTCE、allowance/loans、NII growth、P/TBV。Fee financial：operating margin、ROTCE、OCF/NI、revenue growth、net debt/EBITDA、OCF yield。

所有專用模型都另有資料覆蓋率與 70 分信心閘門。Company Facts 無法跨公司標準化的 CET1 精確值、uninsured deposits、statutory RBC、occupancy、same-store NOI、allowed ROE、rate base、AUM flows、repo haircut 等，必須列入人工覆核；缺口過多時 `ABSTAIN`。

## 評分

- 品質 35%。
- 價值 30%。
- 市場預期差 20%。
- 動能 5%，僅作輔助。
- 營運拐點 5%。
- 資本配置 5%。
- 另扣除壓力測試、稀釋與其他基本面風險；資料品質由獨立信心閘門控制，不加入 alpha 總分。
- ETF 重疊由 AI 使用最新 QQQ/VOO 持股資料覆核；高度重疊時提高決策門檻或降低主動部位，不假裝已反映在靜態 Quant 分數。

## 分數與買入

- 60 分以上：研究候選，不代表可買。
- 70 分以上：優先研究。
- 75 分以上：完成研究後，可考慮 1% 小部位。
- 80 分以上：較高優先度，可考慮 1.5% 起始部位。
- 若為 QQQ 或 VOO 最新前十大持股，至少 80 分才可考慮額外主動加碼。

買入前必須回答：三句話投資論點、最強反方、thesis 失效條件、三情境、最新財報警訊、股數稀釋、資本配置、ETF 重疊，以及已透過 ETF 持有仍值得額外加碼的理由。

## 加碼

至少經過一次財報，且同時確認 thesis 未破壞、一般企業的 Real FCF 或專用產業核心 KPI 未惡化、股數未惡化稀釋、估值仍合理，才可加碼。股價上漲或下跌本身都不是加碼理由。

## 強制檢討與退出

遇到以下任一情況，必須重新研究並考慮減碼或退出：

- 分數跌破 60。
- 一般企業 Real FCF 轉負，或專用產業觸發 hard failure。
- 一般企業 ICR 跌破 3，或專用產業資料信心跌破 70。
- 30% EBITDA 壓力情境未通過 1.5x ICR 存續門檻（淨現金公司除外）。
- 連續兩季營收與毛利同步惡化。
- 股數明顯稀釋。
- 管理層資本配置失控。
- 原投資 thesis 被證偽。
- EV/EBITDA 升至自身歷史 90 分位以上、隱含 EBITDA CAGR 顯著超過動態容忍度，或 FCF Yield 低到不合理。

## 檢查節奏

- 每季：財報後更新研究日誌、估值區間、失效條件與持倉理由。
- 每半年：檢查 QQQ/VOO 與主動持股重疊、產業集中及實際主動權重。
- 每年：把主動部位和適合的基準比較，包含交易成本與稅負，檢討流程錯誤而不只看盈虧。
