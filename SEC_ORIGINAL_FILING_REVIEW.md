# 固定 100 家：剩餘 61 家 SEC 原始申報複核

使用者指定繼續查核上一輪 `40061919126` 的 61 家 ABSTAIN。名單仍是已確認的 `research_watchlist.csv` 固定 100 家，沒有恢復全市場抓取。原始 61 家、CIK、上輪原因、逐家查核說明與已複核來源收錄於 `sec_filing_review.json`。

## 來源與範圍

- 原始資料收集 [Actions 40367846506](https://github.com/e34102309-lab/dodoladodo/actions/runs/40367846506) 已完成，61 家皆有可讀的原始申報，下載錯誤為零。快照 artifact `12670727937` SHA256：`3541481c21d77f928a268ac97a6a6622a865ca39da54846bf30dc11de55ae5e8`。
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
