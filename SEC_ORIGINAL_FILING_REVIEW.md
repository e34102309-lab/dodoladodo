# 固定 100 家：剩餘 61 家 SEC 原始申報複核

使用者指定繼續查核上一輪 `40061919126` 的 61 家 ABSTAIN。名單仍是已確認的 `research_watchlist.csv` 固定 100 家，沒有恢復全市場抓取。原始 61 家、CIK、上輪原因、逐家查核說明與已複核來源收錄於 `sec_filing_review.json`。

## 來源與範圍

- 原始資料收集 [Actions 40367846506](https://github.com/e34102309-lab/dodoladodo/actions/runs/40367846506) 已完成，61 家共 372 份申報的 instance 可讀，沒有 HTTP 下載失敗。附屬檔案有 7 項警示：AMD／MO／DIS 的局部修訂缺 calculation linkbase，FDX 2024 年報的 label／calculation 檔案無法唯一選定；這些限制保留於快照，不能視為全部檔案完整。快照 artifact `12670727937` SHA256：`3541481c21d77f928a268ac97a6a6622a865ca39da54846bf30dc11de55ae5e8`。
- CompanyFacts 不涵蓋全部公司自訂 taxonomy 及維度事實，故補讀相同 accession 的 extracted XBRL instance、標籤及 calculation linkbase。SEC 官方說明：[EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)。
- 生產流程只對這 61 家讀取最近 3 個年度、3 個季度報告期間，各期間保留原報告與最多 2 份修訂報告。Part III 等局部修訂不取代原始年報。資料仍受 acceptance + 5 分鐘的 point-in-time 限制；缺 acceptance 時沿用保守 filed-date fallback。
- 申報依 accession 與 parser 版本快取。下載失敗、格式錯誤與缺乏科目分開記錄，不把傳輸失敗快取成永久缺值。

## 本輪資料修正

- 修正多科目去重：科目、單位及開始日納入識別，避免同期間的不同科目互相覆蓋。
- 對同一事實依 `decimals` 檢查精度區間，保留相容的較精確值；真衝突、nil、無限值仍排除。非 USD 金額、每股單位與其他實體不能充當合併美元金額。
- CVNA 採明確自訂 D&A；AMD、CRWD 按同申報及期間的完整組成重建。CRWD 排除不完整的直接標準標籤；D&A 也不可把只有 depreciation 的年報與完整當期 D&A 銜接。
- WMT、LMT、RTX、VEEV 依實際現金流量表標題核對 D&A；GLW、LLY 採實際現金資本支出。
- KLAC、NKE、TJX、JNJ、MRK、LLY 的營業利益採已複核的同申報／期間／USD 科目重建，與已揭露非營業項及稅前利益調和。MRK、LLY 另要求完整 calculation children 與已核對分類一致。
- GD、RTX 的產品及服務成本採明確完整分拆，必須兩部分皆在、起訖日與單位相同、兩部分營收能調和至合併營收，才可重建毛利。其他分部、地區及多重維度不得混入。
- DHR、PG、TGT、EL、HSY、MMM、CSX、EMR 的現金與約當現金已與當期資產負債表數值核對。較廣的 taxonomy 僅限已複核 accession；其他申報不推定受限現金為零，後續新申報仍須複核。

## 無法直接補的口徑

- HPE 的產品／服務成本排除另列攤銷且含融資業務；KR 的成本排除 D&A；多家運輸、平台與服務公司不提供模型需要的一致完整 COGS／毛利。不能以部分成本、全部營業費用或舊毛利替代。
- GE 的現金包含受限現金、保險及處分業務，成本及 D&A 也有不同口徑；不直接套用一般企業別名。
- CMI、RSG、MCK、PM、MO、WMT 的當期 SBC 鏈仍須檢查。APIC 變動、股權發行價值、含選擇權行使或所得稅的調整，不能當成費用。
- ABNB、APP 等的其他投資活動合計不能當完整獨立 CapEx；ISRG 的折舊及處分損益／合約成本攤銷合計不能直接當純 D&A。
- 總利息費用、利息淨額、現付利息、金融子公司利息與稅務利息分開，保留既有備援與扣分。
- 當期財報不等於歷史估值證據；期初投入資本缺漏、歷史估值不足、負債／現付利息備援仍可能讓資料信心低於門檻。本輪不降低原有信心、新鮮度或硬條件門檻。

## 輸出與驗證

每次生產流程新增 `mode_c_sec_filing_audit.json`（逐申報 URL、SHA256、錯誤與科目）及 `mode_c_data_gaps.csv`（原 61 家的前後原因、實際採用原始事實數與逐家說明）。Evidence ledger 保留文件 URL、SHA256、context、qname、映射依據及衍生來源鏈；validator 會檢查原始文件身分與證據欄位。

離線真實申報片段核對：GD 最新季度毛利 2.183B、RTX 5.805B，與原始損益表相符。片段由瀏覽器的原始 XML DOM 篩出必要 context、unit、營收及成本，未冒充完整原始文件的位元組雜湊。

## 2026-10-28 固定 100 家實跑結果

[Actions 40583805033](https://github.com/e34102309-lab/dodoladodo/actions/runs/40583805033) 使用 `41d8060b03019bc63cfe291bf68216cf5878aa9f`。固定 100 家逐家及名單檔案內容一致，原始申報複核範圍仍為原 61 家。價格日期：2026-10-27（100 家）。

| 結果 | 上輪 | 本輪 |
|---|---:|---:|
| PASS | 33 | 43 |
| FAIL | 6 | 8 |
| ABSTAIN | 61 | 49 |

原 61 家中，12 家解除 ABSTAIN：AMD、CRWD、GLW、LMT、JNJ、MRK、DHR、CAH、TGT、EL、TJX、CVNA。其中 FAIL 表示資料已足以判斷但未通過硬條件；不是資料補值失敗。補齊部分欄位後仍可因其他證據不足而 ABSTAIN。兩次運行的價格與新申報日期不同，不能把全部分數變動都歸因於程式修正。

本輪查核 372 份原始申報；1871 列原始事實進入證據鏈，覆蓋 61 家。378 項輸出公式及 160 項原始重建公式另行重算通過；63,919 列 evidence 通過日期、來源與血緣驗證，`invalid_zero = 0`。

49 家 ABSTAIN 的首要原因分組：毛利口徑／歷史序列 15 家、資料信心 12 家、利息覆蓋率 9 家、SBC 6 家、CapEx（含 ABNB 的 D&A）3 家、EBIT 2 家、ISRG D&A 1 家、GE 多項口徑 1 家。這不是 49 家下載失敗。

利息組中的 PANW 已取得當期真零：`TTM_Interest = 0.0B`，與年報營業利益使用同期間。現行有淨負債時的 ICR 模型要求可計算的覆蓋率，零分母仍保留 ABSTAIN；本次輸出的概括狀態寫「缺少同期間利息費用證據」，但此例的實際限制是模型無法處理該覆蓋率，不是公司沒有揭露或抓取失敗。後續逐家說明已補充這個區別，沒有用任意極大值放行。

先前 DHR 等公司只按較廣 taxonomy 保守處理；本輪進一步核對資產負債表後才接受已確認的現金數字，沒有把含受限現金的標籤全面改作現金。

逐申報附檔警示及可能的傳輸錯誤均保留於 Actions artifact 的 `mode_c_sec_filing_audit.json`；逐家完整原始原因與資料信心理由見 `mode_c_data_gaps.csv`。沒有執行全市場抓取。

## 原 61 家逐家結果及原因

PASS／FAIL 是本次模型判定；ABSTAIN 表示仍有明列的資料或信心限制。下列查核說明也列出已補齊的部分，並不把尚未補齊的欄位假裝為零。

| 股票 | 本輪 | 當前判定原因 | 原始申報查核說明 |
|---|---|---|---|
| AAPL | ABSTAIN | Abstain: 有淨負債但缺少同期間利息費用證據 | 當期原始申報未提供可用的獨立利息費用；歷史標準標籤只到 2023 年，不能用利息收入淨額或稅務利息替代。 [財報](https://www.sec.gov/Archives/edgar/data/320193/000032019326000020/aapl-20260627.htm) |
| AMD | PASS | Pass | D&A 由原始財報的 OtherDepreciationAndAmortization 與無形資產攤銷相加；保留原年報，不能以只更新 Part III 的 10-K/A 取代。 [財報](https://www.sec.gov/Archives/edgar/data/2488/000000248826000123/amd-20260627.htm) |
| PANW | ABSTAIN | 申報利息為零；有淨負債時現行 ICR 模型無法建立有效比率 | 已取得 `TTM_Interest = 0.0B` 真零，不是缺值；保留現有模型閘門，原始 artifact 的概括原因需依本說明解讀。 [財報](https://www.sec.gov/Archives/edgar/data/1327567/000132756726000023/panw-20260731.htm) |
| ANET | ABSTAIN | Abstain: Data confidence 45<70；ROIC 缺期初投入資本，使用期末備援; 歷史時點估值證據不足; 總負債使用非 SEC 備援; 現金覆蓋負債 | 原始當期財報可讀，但資料信心包含歷史估值、期初投入資本或負債／利息備援限制，不能僅補當期數值便視作解除。 [財報](https://www.sec.gov/Archives/edgar/data/1596532/000159653226000175/anet-20260630.htm) |
| CRWD | FAIL | Fail: 近三年股數持續明顯稀釋 | 現金流量表的自訂 D&A 不含收購無形資產攤銷；同期間加回該攤銷，排除另一個僅部分折舊的標準標籤。 [財報](https://www.sec.gov/Archives/edgar/data/1535527/000153552726000031/crwd-20260731.htm) |
| KLAC | ABSTAIN | Abstain: Data confidence 65<70；歷史時點估值證據不足 | 按已複核原始損益表重建營業利益；同申報、同起訖日、USD，並與稅前利益／非營業項調和。 [財報](https://www.sec.gov/Archives/edgar/data/319201/000031920126000027/klac-20260630.htm) |
| UBER | ABSTAIN | Abstain: SEC 核心資料缺失 GrossProfit | 當期原始申報未提供符合模型的合併 GrossProfit／完整 COGS 口徑；成本分類與營業費用／折舊分配無法安全銜接，維持資料不足。 [財報](https://www.sec.gov/Archives/edgar/data/1543151/000154315126000032/uber-20260630.htm) |
| NOW | ABSTAIN | Abstain: Data confidence 35<70；ROIC 缺期初投入資本，使用期末備援; 歷史時點估值證據不足; EBITDA 來源調和超出容許範圍; ICR 使用現付利息代理 | 原始當期財報可讀，但資料信心包含歷史估值、期初投入資本或負債／利息備援限制，不能僅補當期數值便視作解除。 [財報](https://www.sec.gov/Archives/edgar/data/1373715/000137371526000076/now-20260630.htm) |
| GLW | PASS | Pass | 採原始現金流量表 PaymentsForCapitalImprovements 的現金資本支出；不使用未付設備增加額。 [財報](https://www.sec.gov/Archives/edgar/data/24741/000002474126000255/glw-20260630.htm) |
| HPE | ABSTAIN | Abstain: SEC 核心資料過舊 GrossProfit | 主表產品／服務成本明列不含另列攤銷，且有融資業務；沒有可調和的一致完整毛利口徑，不能把部分成本當全部 COGS。 [財報](https://www.sec.gov/Archives/edgar/data/1645590/000164559026000080/hpe-20260731.htm) |
| CAT | ABSTAIN | Abstain: 有淨負債但缺少同期間利息費用證據 | 當期合併申報未取得與企業 TTM 一致的利息費用；金融子公司／分部數字不能直接冒充合併利息。 [財報](https://www.sec.gov/Archives/edgar/data/18230/000001823026000046/cat-20260630.htm) |
| GE | ABSTAIN | Abstain: SEC 核心資料過舊 EBIT/D&A/GrossProfit/Cash | 合併現金明確包含受限現金及保險／處分業務；成本含融資與非營業項，D&A 含 accretion，不能直接當一般企業的現金、EBIT 或純 D&A。 [財報](https://www.sec.gov/Archives/edgar/data/40545/000004054526000064/ge-20260930.htm) |
| RTX | ABSTAIN | Abstain: 有淨負債但缺少同期間利息費用證據 | D&A 依實際現金流量表標題映射；產品／服務完整成本分拆須先調和營收才重建毛利。 [財報](https://www.sec.gov/Archives/edgar/data/101829/000010182926000031/rtx-20260930.htm) |
| UNP | ABSTAIN | Abstain: SEC 核心資料缺失 GrossProfit | 運輸業按營運費用呈現，沒有可調和的獨立完整 GrossProfit／COGS；不能把全部營業費用或排除折舊的成本當一般毛利。 [財報](https://www.sec.gov/Archives/edgar/data/100885/000010088526000321/unp-20260930.htm) |
| LMT | PASS | Pass | 現金流量表明確列示 Depreciation and amortization，採 issuer-specific 映射，不把較廣的 taxonomy 當全市場通用別名。 [財報](https://www.sec.gov/Archives/edgar/data/936468/000162828026066928/lmt-20260927.htm) |
| GD | ABSTAIN | Abstain: 有淨負債但缺少同期間利息費用證據 | 產品與服務成本採已核對的完整維度分拆，加總前先驗證兩部分營收等於合併營收；利息現付代理仍保留信心扣分。 [財報](https://www.sec.gov/Archives/edgar/data/40533/000004053326000032/gd-20260705.htm) |
| CSX | ABSTAIN | Abstain: SEC 核心資料缺失 GrossProfit | 運輸業按營運費用呈現，沒有可調和的獨立完整 GrossProfit／COGS；不能把全部營業費用或排除折舊的成本當一般毛利。 [財報](https://www.sec.gov/Archives/edgar/data/277948/000027794826000040/csx-20260930.htm) |
| UPS | ABSTAIN | Abstain: SEC 核心資料缺失 GrossProfit | 運輸業按營運費用呈現，沒有可調和的獨立完整 GrossProfit／COGS；不能把全部營業費用或排除折舊的成本當一般毛利。 [財報](https://www.sec.gov/Archives/edgar/data/1090727/000162828026053249/ups-20260630.htm) |
| MMM | ABSTAIN | Abstain: Data confidence 55<70；歷史時點估值證據不足; EBITDA 來源調和超出容許範圍 | 已核對當期資產負債表現金及約當現金 4.835B；只允許已複核申報，其他含受限現金的申報不自動套用。 [財報](https://www.sec.gov/Archives/edgar/data/66740/000006674026000311/mmm-20260930.htm) |
| WM | ABSTAIN | Abstain: 有淨負債但缺少同期間利息費用證據 | 當期只取得利息淨額／利息及其他費用合計，不能直接當獨立總利息費用；年度利息不得冒充較新 TTM。 [財報](https://www.sec.gov/Archives/edgar/data/823768/000110465926088016/wm-20260630x10q.htm) |
| EMR | ABSTAIN | Abstain: SEC 核心資料缺失 EBIT | 已核對當期資產負債表現金 2.180B；EBIT 尚需區分 Other deductions 中的非營業項，淨利息不能冒充總利息費用。 [財報](https://www.sec.gov/Archives/edgar/data/32604/000003260426000043/emr-20260630.htm) |
| CMI | ABSTAIN | Abstain: TTM 證據鏈不完整，期間不一致 SBC | 當期只取得包含選擇權行使的 APIC 調整，不能當 SBC 費用；完整 SBC 標準證據停留在年報。 [財報](https://www.sec.gov/Archives/edgar/data/26172/000002617226000029/cmi-20260630.htm) |
| FDX | ABSTAIN | Abstain: SEC 核心資料過舊 GrossProfit | 運輸業按營運費用呈現，沒有可調和的獨立完整 GrossProfit／COGS；不能把全部營業費用或排除折舊的成本當一般毛利。 [財報](https://www.sec.gov/Archives/edgar/data/1048911/000104891126000105/fdx-20260531.htm) |
| NSC | ABSTAIN | Abstain: SEC 核心資料缺失 GrossProfit | 運輸業按營運費用呈現，沒有可調和的獨立完整 GrossProfit／COGS；不能把全部營業費用或排除折舊的成本當一般毛利。 [財報](https://www.sec.gov/Archives/edgar/data/702165/000162828026066836/nsc-20260930.htm) |
| RSG | ABSTAIN | Abstain: TTM 證據鏈不完整，期間不一致 SBC | 當期 APIC 成本認列不等於完整合併 SBC 費用；無可靠年報加當期 YTD 減前期 YTD 的同口徑鏈。 [財報](https://www.sec.gov/Archives/edgar/data/1060391/000106039126000275/rsg-20260630.htm) |
| LLY | ABSTAIN | Abstain: 有淨負債但缺少同期間利息費用證據 | 按已複核原始損益表重建營業利益；同申報、同起訖日、USD，並與稅前利益／非營業項調和。另補原始現金設備支出。 [財報](https://www.sec.gov/Archives/edgar/data/59478/000005947826000081/lly-20260630.htm) |
| JNJ | PASS | Pass | 按已複核原始損益表重建營業利益；同申報、同起訖日、USD，並與稅前利益／非營業項調和。 [財報](https://www.sec.gov/Archives/edgar/data/200406/000020040626000197/jnj-20260927.htm) |
| MRK | PASS | Pass | 按已複核原始損益表重建營業利益；同申報、同起訖日、USD，並與稅前利益／非營業項調和。 [財報](https://www.sec.gov/Archives/edgar/data/310158/000031015826000212/mrk-20260630.htm) |
| DHR | PASS | Pass | 已核對當期資產負債表 Cash and cash equivalents 與原始數值相符，僅允許已複核申報；不把其他申報的受限現金假設為零。 [財報](https://www.sec.gov/Archives/edgar/data/313616/000031361626000199/dhr-20260925.htm) |
| VRTX | ABSTAIN | Abstain: Data confidence 30<70；部分來源使用備援標籤; ROIC 缺期初投入資本，使用期末備援; 歷史時點估值證據不足; EBITDA 來源調和超出容許範圍; 總負債使用非 SEC 備援; 現金覆蓋負債 | 原始當期財報可讀，但資料信心包含歷史估值、期初投入資本或負債／利息備援限制，不能僅補當期數值便視作解除。 [財報](https://www.sec.gov/Archives/edgar/data/875320/000087532026000259/vrtx-20260630.htm) |
| ISRG | ABSTAIN | Abstain: SEC 核心資料過舊 D&A | 當期折舊與固定資產處分損益合併，自訂攤銷又含合約取得成本，無法直接當純 D&A；不猜處分損益為零。 [財報](https://www.sec.gov/Archives/edgar/data/1035267/000103526726000071/isrg-20260930.htm) |
| MCK | ABSTAIN | Abstain: TTM 證據鏈不完整，期間不一致 SBC | 當期 APIC 增加額不能直接當 SBC 費用；SBC 季度與年度 TTM 期間無法安全銜接。 [財報](https://www.sec.gov/Archives/edgar/data/927653/000092765326000234/mck-20260630.htm) |
| REGN | ABSTAIN | Abstain: SEC 核心資料過舊 GrossProfit | 當期營收包括協作等收入，但沒有與全部收入同口徑的完整合併 COGS／GrossProfit；不能只用產品成本。 [財報](https://www.sec.gov/Archives/edgar/data/872589/000087258926000025/regn-20260630.htm) |
| COR | ABSTAIN | Abstain: 有淨負債但缺少同期間利息費用證據 | 當期揭露為非營業利息淨額，不能當總利息費用；現付利息及其 TTM 仍受既有期間規則檢查。 [財報](https://www.sec.gov/Archives/edgar/data/1140859/000114085926000034/cor-20260630.htm) |
| CAH | PASS | Pass | 年報 TTM 毛利可取得，但合併季度毛利序列不足；不能把年度毛利拆成假季度。 [財報](https://www.sec.gov/Archives/edgar/data/721371/000072137126000038/cah-20260630.htm) |
| VEEV | ABSTAIN | Abstain: SEC 核心資料過舊 CapEx | 原始表明確揭露 OtherDepreciationAndAmortization 為總 D&A，可補；投資流量 Long-term assets 不是完整獨立 CapEx 證據。 [財報](https://www.sec.gov/Archives/edgar/data/1393052/000139305226000036/veev-20260731.htm) |
| WMT | ABSTAIN | Abstain: TTM 證據鏈不完整，期間不一致 SBC | 補現金流量表明列的總 D&A；當期 SBC 沒有可靠獨立費用／YTD 鏈，不能由舊年報或股權變動猜補。 [財報](https://www.sec.gov/Archives/edgar/data/104169/000010416926000154/wmt-20260731.htm) |
| PG | ABSTAIN | Abstain: Data confidence 50<70；部分來源使用備援標籤; 歷史時點估值證據不足; EBITDA 來源調和超出容許範圍 | 已核對當期資產負債表 Cash and cash equivalents 與原始數值相符，僅允許已複核申報；不把其他申報的受限現金假設為零。 [財報](https://www.sec.gov/Archives/edgar/data/80424/000008042426000256/pg-20260930.htm) |
| PM | ABSTAIN | Abstain: TTM 證據鏈不完整，期間不一致 SBC | 股份發行的 SBC 價值不是當期 SBC 費用；不以 StockIssuedDuringPeriodValueShareBasedCompensation 替代。 [財報](https://www.sec.gov/Archives/edgar/data/1413329/000162828026066976/pm-20260930.htm) |
| MO | ABSTAIN | Abstain: SBC TTM 證據缺失 | 自訂 APIC 調整包含所得稅淨影響，不能當純 SBC 費用；缺可靠同期間 SBC 鏈。 [財報](https://www.sec.gov/Archives/edgar/data/764180/000076418026000094/mo-20260630.htm) |
| MNST | ABSTAIN | Abstain: Data confidence 45<70；ROIC 缺期初投入資本，使用期末備援; 歷史時點估值證據不足; 總負債使用非 SEC 備援; 現金覆蓋負債 | 原始當期財報可讀，但資料信心包含歷史估值、期初投入資本或負債／利息備援限制，不能僅補當期數值便視作解除。 [財報](https://www.sec.gov/Archives/edgar/data/865752/000110465926092193/mnst-20260630x10q.htm) |
| TGT | PASS | Pass | 已核對當期資產負債表 Cash and cash equivalents 與原始數值相符，僅允許已複核申報；不把其他申報的受限現金假設為零。 [財報](https://www.sec.gov/Archives/edgar/data/27419/000002741926000042/tgt-20260801.htm) |
| EL | PASS | Pass | 已核對當期資產負債表 Cash and cash equivalents 與原始數值相符，僅允許已複核申報；不把其他申報的受限現金假設為零。 [財報](https://www.sec.gov/Archives/edgar/data/1001250/000100125026000041/el-20260630.htm) |
| KVUE | ABSTAIN | Abstain: Data confidence 25<70；ROIC 缺期初投入資本，使用期末備援; 歷史時點估值證據不足; EBITDA 來源調和超出容許範圍; 總負債使用當期行情備援 | 原始當期財報可讀，但資料信心包含歷史估值、期初投入資本或負債／利息備援限制，不能僅補當期數值便視作解除。 [財報](https://www.sec.gov/Archives/edgar/data/1944048/000194404826000142/kvue-20260628.htm) |
| HSY | ABSTAIN | Abstain: Data confidence 42<70；One-year share-count evidence is unavailable; Three-year dilution evidence is unavailable; 歷史時點估值證據不足; Current shares outstanding use a cache or market-data fallback | 已核對當期資產負債表 Cash and cash equivalents 與原始數值相符，僅允許已複核申報；不把其他申報的受限現金假設為零。 [財報](https://www.sec.gov/Archives/edgar/data/47111/000162828026050900/hsy-20260628.htm) |
| KR | ABSTAIN | Abstain: SEC 核心資料過舊 GrossProfit | 成本標籤明確排除折舊攤銷，缺少銷貨與營業費用之間的完整分攤；不能把部分成本當完整 COGS。 [財報](https://www.sec.gov/Archives/edgar/data/56873/000110465926108926/kr-20260815x10q.htm) |
| BKNG | ABSTAIN | Abstain: SEC 核心資料過舊 GrossProfit/NetIncome | 現行淨額／總額收入呈現與舊 GrossProfit 基礎不同，未取得一致完整 COGS；不得沿用 2017 毛利作當期。 [財報](https://www.sec.gov/Archives/edgar/data/1075531/000107553126000037/bkng-20260630.htm) |
| TJX | PASS | Pass | 按已複核原始損益表重建營業利益；同申報、同起訖日、USD，並與稅前利益／非營業項調和。 [財報](https://www.sec.gov/Archives/edgar/data/109198/000010919826000048/tjx-20260801.htm) |
| ABNB | ABSTAIN | Abstain: SEC 核心資料過舊 CapEx/D&A | 當期沒有獨立完整 CapEx／D&A；其他投資活動合計不可直接當 CapEx，舊年度不冒充當期 TTM。 [財報](https://www.sec.gov/Archives/edgar/data/1559720/000155972026000027/abnb-20260630.htm) |
| DASH | ABSTAIN | Abstain: SEC 核心資料缺失 GrossProfit | 當期原始申報未提供符合模型的合併 GrossProfit／完整 COGS 口徑；成本分類與營業費用／折舊分配無法安全銜接，維持資料不足。 [財報](https://www.sec.gov/Archives/edgar/data/1792789/000179278926000050/dash-20260630.htm) |
| CVNA | FAIL | Fail: 近三年股數持續明顯稀釋 | 採明確自訂 D&A excluding debt issuance costs，保留融資發行成本攤銷排除及原始來源。 [財報](https://www.sec.gov/Archives/edgar/data/1690820/000169082026000055/cvna-20260630.htm) |
| ROST | ABSTAIN | Abstain: Data confidence 50<70；部分來源使用備援標籤; 歷史時點估值證據不足; EBITDA 來源調和超出容許範圍 | 原始當期財報可讀，但資料信心包含歷史估值、期初投入資本或負債／利息備援限制，不能僅補當期數值便視作解除。 [財報](https://www.sec.gov/Archives/edgar/data/745732/000074573226000041/rost-20260801.htm) |
| NKE | ABSTAIN | Abstain: 有淨負債但缺少同期間利息費用證據 | 按已複核原始損益表重建營業利益；同申報、同起訖日、USD，並與稅前利益／非營業項調和。 [財報](https://www.sec.gov/Archives/edgar/data/320187/000032018726000193/nke-20260831.htm) |
| CMG | ABSTAIN | Abstain: SEC 核心資料缺失 GrossProfit | 當期原始申報未提供符合模型的合併 GrossProfit／完整 COGS 口徑；成本分類與營業費用／折舊分配無法安全銜接，維持資料不足。 [財報](https://www.sec.gov/Archives/edgar/data/1058090/000105809026000066/cmg-20260630.htm) |
| DHI | ABSTAIN | Abstain: SEC 核心資料缺失 EBIT | 主表以稅前利益為主，且建商利息資本化／結轉成本；不能把 Pretax 直接當 EBIT 或把 incurred interest 當費用。 [財報](https://www.sec.gov/Archives/edgar/data/882184/000088218426000096/dhi-20260630.htm) |
| GOOGL | ABSTAIN | Abstain: Data confidence 55<70；歷史時點估值證據不足; EBITDA 來源調和超出容許範圍 | 原始當期財報可讀，但資料信心包含歷史估值、期初投入資本或負債／利息備援限制，不能僅補當期數值便視作解除。 [財報](https://www.sec.gov/Archives/edgar/data/1652044/000165204426000071/goog-20260630.htm) |
| META | ABSTAIN | Abstain: Data confidence 55<70；歷史時點估值證據不足; EBITDA 來源調和超出容許範圍 | 原始當期財報可讀，但資料信心包含歷史估值、期初投入資本或負債／利息備援限制，不能僅補當期數值便視作解除。 [財報](https://www.sec.gov/Archives/edgar/data/1326801/000162828026066773/meta-20260930.htm) |
| DIS | ABSTAIN | Abstain: SEC 核心資料缺失 GrossProfit | 當期原始申報未提供符合模型的合併 GrossProfit／完整 COGS 口徑；成本分類與營業費用／折舊分配無法安全銜接，維持資料不足。 [財報](https://www.sec.gov/Archives/edgar/data/1744489/000174448926000057/dis-20260627.htm) |
| APP | ABSTAIN | Abstain: SEC 核心資料過舊 CapEx | 當期僅 Other investing activities 合計，不能直接視作現金 CapEx；不以零代替未單獨揭露的支出。 [財報](https://www.sec.gov/Archives/edgar/data/1751008/000175100826000059/app-20260630.htm) |
| CMCSA | ABSTAIN | Abstain: SEC 核心資料缺失 GrossProfit | 當期原始申報未提供符合模型的合併 GrossProfit／完整 COGS 口徑；成本分類與營業費用／折舊分配無法安全銜接，維持資料不足。 [財報](https://www.sec.gov/Archives/edgar/data/1166691/000162828026066863/cmcsa-20260930.htm) |
| LYV | ABSTAIN | Abstain: SEC 核心資料缺失 GrossProfit | 當期原始申報未提供符合模型的合併 GrossProfit／完整 COGS 口徑；成本分類與營業費用／折舊分配無法安全銜接，維持資料不足。 [財報](https://www.sec.gov/Archives/edgar/data/1335258/000133525826000035/lyv-20260630.htm) |
