# 股票策略五段檢查

完成日期：2026-09-06。基準為 `codex/metric-integrity-audit-v2` 的 `e98fffe` 加上既有未提交修正。
本輪保留既有工作樹，不抓全市場、不調整未經驗證的權重，也不自行提交或推送。

| 段落 | 範圍 | 進度 |
|---|---|---|
| 1 | 初篩與產業分流 | 完成，26 項初篩測試通過 |
| 2 | 財報期間、PIT、TTM | 完成，7 項聚焦測試通過 |
| 3 | 選股公式與評分 | 完成，公式回歸案例通過 |
| 4 | 風險閘門與研究排序 | 完成，11 項聚焦測試通過 |
| 5 | 輸出契約、整合驗證與文件 | 完成；完整 suite 214/215，修正唯一過期斷言後定點通過 |

## 第一段：初篩與產業分流

- 檢查 `total_market_hunter_v2.py`、`mode_c_routing.py`、特殊產業初篩及相鄰測試。
- 修正金融 sector 搶先匹配 REIT 的問題；明確 REIT industry 優先於廣義金融分類。
- 修正負毛利率被當缺值，繞過既有非正毛利底線的問題；上限大於 100% 仍視為異常並後送 SEC。
- 非文字、NaN 或空值產業標籤不能誤入 GENERAL_CORPORATE。
- Hunter policy 升至 v8，使舊初篩 checkpoint 不會沿用修正前的判斷。
- 保留原市值、同業樣本、淨槓桿與特殊產業門檻；本段沒有校準這些門檻的投資有效性。

## 第二段：財報資料

- 年報流量必須有 330 至 380 天的實際期間，不能只因 `10-K` 或 `FY` 標籤就接受半年、單季、兩年或缺起日資料。
- 歷史估值的現金／負債改走既有時點餘額選取器，保留 first-filing 與日期對齊，避免因收緊年度流量規則而誤刪餘額。
- 特殊產業成長率也套用連續八季；年度 fallback 的兩期末必須相隔 350 至 380 天。
- 既有 acceptance time、可得時間延遲、來源 lineage 檢查保留，相關測試通過；不是對所有真實申報的完整證明。

## 第三段：公式與評分

- FCF 穩定性與每股三年成長改錨定最新已報告年度；最新年度缺少 SBC／OCF／CapEx／Revenue／Net Income 時，不能用較舊完整年度冒充最新歷史。
- 排除商譽 ROIC 的商譽必須與投入資本同基礎：平均資本使用完整期初期末商譽，期末 fallback 只使用期末商譽。
- `Dilution_Total_Score_Impact = Ownership Penalty + Capital Allocation Penalty * 5 / Available Factor Weight`，與因子退出／缺失後的實際重算權重一致，validator 同步修正。
- 一般模型原始權重、ROIC／FCF／反向估值門檻與特殊產業評分方向未擅自改動。
- 第一輪核心＋產業模型 114 項中 113 通過，1 項舊斷言仍假設固定 5%；已改為驗證 95 與 100 權重情況並通過。

## 第四段：風險與排序

- 銀行壓力：`Stressed Tier 1 = Reported Tier 1 - After-tax Capital Loss / RWA * 100`，修正錯用總資產的分母。
- 新增 SEC `us-gaap:RiskWeightedAssets` 抓取、來源證據與新鮮度映射；RWA 必須為正，且與資本比率及壓力餘額同一期末。缺失／錯期則壓力 `ABSTAIN`，不回填 total assets。
- 依據：[Federal Reserve 217.10](https://www.federalreserve.gov/frrs/regulations/section-21710-minimum-capital-requirements.htm) 的 Tier 1 定義；標籤與單位參考 [XBRL US DQC 0139](https://xbrl.us/data-rule/dqc_0139/)。固定 RWA、貸損 3% 與稅後係數 0.79 仍是研究情境假設，不是監管壓測。
- 已確認的專用壓力 `FAIL` 優先於其他資料信心不足，避免被改寫成 `ABSTAIN`。
- Starter 必須有明確 `Portfolio_Fit_Status = PASS`，不能只因 pending=false 就通過；未知風險 boolean 不得視為已解除。
- Model Eligible 與研究佇列再核對 PASS、模型支援、有效分數，NaN 或相互矛盾的 eligible 旗標不能入列。Round-robin 排序規則保留。

## 第五段：一致性與整理

- Validator 新增銀行壓力獨立重算：重新計算增量貸損、稅後資本損失、RWA 分母的 Tier 1 比率及存活結果；舊 total-assets 分母或 RWA 缺失都會被擋下。
- Validator 的稀釋總影響同步使用可用因子權重，避免程式與稽核器採不同公式。
- 完整 unit suite 共 215 項，首次執行 214 項通過；唯一失敗是原始碼字串斷言仍指向抽函式前的 `evaluation.decision`。更新為檢查 `determine_specialized_status` 的呼叫與明確 `ABSTAIN` gate 後，該項定點重跑通過。依低耗用原則沒有為純測試文字變更再跑第二次完整 suite；215 項案例已跨這兩次執行全部通過。
- 離線 fixture `fixture_output_five_stage_20260904/` 通過：5 檔、3 `PASS`、1 `FAIL`、1 `ABSTAIN`、3 eligible、3 shortlist，`invalid_zero = 0`。
- 七個本輪生產模組通過 `py_compile`，`git diff --check` 無錯誤；僅有 Git 對 Windows CRLF 正規化的提示。
- 已同步本文件、`CURRENT_STRATEGY_LOGIC_FOR_AI_REVIEW.md` 與 `PROJECT_ACCOUNT_HANDOFF.md`；沒有執行全市場即時抓取，也沒有提交或推送。

## 結論與邊界

- 五段檢查修掉的是可具體反證的期間、分母、路由、缺值與狀態優先序錯誤；沒有調高分數，也沒有以回測結果為名修改權重。
- 這次驗證證明固定測試與離線整合資料下的工程一致性，不等於無偏歷史績效、所有 SEC 公司標籤覆蓋或未來報酬有效性已被證明。
- 銀行壓力仍以固定 RWA、貸款損失 3% 與 21% 稅盾作研究情境。真正要校準強弱，仍需 point-in-time 歷史 universe、下市股與 walk-forward 結果。
