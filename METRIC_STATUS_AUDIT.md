# Mode C Metric-Status Audit

2026-10-24 固定 100 家的公式與抓取複查見 I 節；A–H 節中的全市場家數與測試數量為歷史紀錄。

## A. Root Causes

1. The dashboard used `Number(null)`, which evaluates to zero in JavaScript. Missing CSV values therefore rendered as `0.00`.
2. Specialized industry rows shared generic corporate output columns. A model-specific zero or absent metric could leak into an inapplicable generic field.
3. Trend averages accepted every numeric-looking value and candidate rules used `value or 0`, hiding missingness and coverage.
4. `Real_FCF_Yield_pct` used enterprise value as its denominator even though the label did not identify the denominator.
5. Net-cash interest coverage was represented as infinity, even though the correct display state is non-binding/not applicable.
6. A non-empty SEC series without a usable annual/quarterly chain could fall through to a derived annual value of zero.
7. SEC-reported zero SBC could be overwritten by a positive Yahoo fallback, while absent Yahoo SBC, price, market cap, or EBITDA first became zero.
8. REIT goodwill, intangibles, property gains, impairment, and tax gaps were coalesced to zero before FFO/EBITDAre calculation.

## B. Changed Files

- `mode_c_metric_contract.py`: typed metric contract, applicability rules, evidence metadata, and output annotation.
- `AQR_ModeC_Agent_V12.py`: CapEx sensitivity, explicit FCF denominators, SBC attribution, per-share growth, historical coverage, ADS gate, and contract output.
- `mode_c_industry_models.py`: explicit cyclical FCF-to-EV metric name.
- `build_mode_c_dashboard.py`: status-aware data payload and coverage-aware trends.
- `enhance_dashboard_ui.py`: status-aware display labels and audit details.
- `validate_mode_c_outputs.py`: CSV, metadata, evidence, dashboard, and trend invariants.
- `.github/workflows/alpha_hunt.yml`: compile the contract and validate the final dashboard.
- `tests/`: regression fixtures and edge-condition coverage.
- `mode_c_fixture_pipeline.py`: deterministic end-to-end GLW/PGR/IFNNY output pipeline.

## C. Metric Schema

Every dashboard metric has:

```json
{
  "value": null,
  "status": "MISSING",
  "reason": "No auditable value",
  "as_of": "2026-07-16",
  "source_method": "screen_output",
  "evidence_ids": []
}
```

Allowed statuses are `VALID`, `MISSING`, `NOT_APPLICABLE`, `ABSTAIN`, `STALE`, `INVALID`, and `ESTIMATED`. Null statuses cannot carry a value. `VALID` and `ESTIMATED` must carry a finite value. A reported financial zero is retained only when selected evidence supports it.

Contract v2 adds explicit `Maintenance_Real_FCF_B` and `Conservative_Real_FCF_B`, applicable/not-applicable lists, per-row status counts, latest metric date, required/optional gaps, Yahoo/annual/FX fallback flags, estimated-CapEx usage, and a data-complete flag.

## D. Validation Rules

- CSV values must equal `Metric_Metadata_JSON` values.
- Specialized models must null generic fields outside their applicability allowlist.
- ABSTAIN rows cannot expose a long-term score.
- Non-positive EV valuation metrics are invalid, not zero.
- Net-cash/immaterial-debt ICR is not applicable, not infinity or zero.
- ADS rows without both point-in-time FX and ADR ratio abstain.
- Low point-in-time valuation coverage downgrades data confidence; new outputs cannot be eligible with a non-valid coverage status.
- Trend averages include only `VALID` and separately counted `ESTIMATED` observations.
- Trend metrics below 50% coverage are null; their groups cannot become emerging candidates.
- Dashboard stock metadata and values must match the validated screen exactly.
- Maintenance and conservative FCF amounts are recomputed from OCF, CapEx, and SBC; market-cap and EV yields are recomputed from those amounts.
- `mode_c_zero_audit.json` classifies every literal important-metric zero and rejects the pipeline when `invalid_zero` is nonzero.
- Dashboard aggregate integrity counts and per-row audit fields must match the CSV.
- Trend summaries must have `estimated_included=false` and `used_count == valid_count`.

## E. Test Coverage

The suite contains 142 tests. New regressions cover GLW/PGR/IFNNY end-to-end fixtures, missing SBC, true-zero CapEx, negative FCF, REIT missing adjustments, unusable annual fallback, explicit FCF/yield recomputation, EV-yield suppression when enterprise value is unavailable, zero classification, dashboard integrity filters, and exclusion of estimated trend values.

Existing tests continue to cover missing SBC, negative FCF, net cash with missing interest, non-positive EV, bank/insurance/REIT/utility/cyclical routes, split handling, amendments, and point-in-time decision availability.

## F. Before And After

| Case | Before | After |
| --- | --- | --- |
| GLW | Missing general metrics could render `0.00` | Live row: FCF, CapEx, and EV/EBITDA are null with `ABSTAIN`; complete deterministic fixture produces auditable positive FCF yields |
| PGR | Generic corporate fields could appear as zeros beside P&C results | Insurance metrics remain visible; generic FCF, CapEx, and EV/EBITDA are `NOT_APPLICABLE` |
| IFNNY | Foreign/ADS reconciliation could proceed without explicit FX/ADR metadata | Fixture and runtime gate return `ABSTAIN` with null FX and ADR ratio |
| FRO | Missing specialized CapEx leaked as `0.0` | Unsupported zero is null with missing/abstain status |

IFNNY is not present in the current 1,209-stock output, so its behavior is enforced by a dedicated fixture rather than claimed as a live-row comparison.

The current 1,209-row backfill classifies 1,430 evidenced/formula zeros, 31,050 not-applicable metric states, and 30,565 missing/abstain/stale/invalid states. `invalid_zero` is zero.

## G. Known Limits

- Maintenance CapEx is an auditable D&A/revenue-growth estimate, not company-disclosed maintenance spending. Low/base/high scenarios and confidence are shown.
- Per-share CAGR requires positive, comparable annual endpoints exactly three years apart. It abstains across missing periods or non-positive bases.
- Generic corporate EBITDA stress is implemented. Specialized industry stress uses an explicit extension status and does not display generic stress values as zeros; dedicated regulatory/catastrophe/credit stress modules remain future work.
- Short-interest freshness still depends on the configured market-data source and is not treated as SEC evidence.
- Goodwill, intangibles, REIT gain/impairment/tax, statutory insurance capital, bank regulatory detail, occupancy, duration gap, and similar company-specific disclosures can still require manual primary-document review. Missing values now abstain instead of becoming zero.

## H. Reproduce

```powershell
& D:\dobird\.venv\Scripts\python.exe -m unittest discover -s tests -q
& D:\dobird\.venv\Scripts\python.exe mode_c_fixture_pipeline.py --output-dir fixture_output
& D:\dobird\.venv\Scripts\python.exe mode_c_metric_contract.py
& D:\dobird\.venv\Scripts\python.exe build_mode_c_dashboard.py
& D:\dobird\.venv\Scripts\python.exe enhance_dashboard_ui.py public/index.html
& D:\dobird\.venv\Scripts\python.exe validate_mode_c_outputs.py --dashboard public/data.json --zero-report mode_c_zero_audit.json
```

The local backfill validated 1,209 screen rows, 12 shortlist rows, 164,765 selected evidence records, and 135 trend groups without another SEC fetch.

## I. 2026-10-24 固定 100 家公式與資料抓取複查

本輪針對一般企業分析路徑及共用 SEC／Yahoo 抓取邏輯做離線複查；固定名單與因子權重未改動，沒有執行最新行情／財報更新或全市場抓取。

| 問題 | 已確認的影響 | 修正 |
| --- | --- | --- |
| 歷史價格與股數拆股基準不同 | Yahoo `Close` 已調整拆股，股數卻只調整到歷史估值日；後續拆股會使歷史市值與倍數失真 | 歷史股數與 Yahoo 價格統一到分析日拆股基準，保留拆股來源鏈；順拆、反拆的回歸案例確認歷史市值不因單位改變 |
| 稅前利益被用作 EBIT | 稅前利益可能已包含利息與其他非營業項目，影響 EBITDA、ICR、ROIC | EBIT 僅接受 `OperatingIncomeLoss`；無可靠營業利益時沿用缺資料的 ABSTAIN 閘門，稅前利益保留在獨立概念 |
| 當期所得稅被用作總所得稅 | `CurrentIncomeTaxExpenseBenefit` 不含遞延稅，會扭曲有效稅率與 NOPAT | 不再作總稅額備援；缺值或與淨利期間不同時使用既有明示 21% 假設及信心扣分 |
| 個別 TTM 有效卻屬不同期末 | 一個指標使用舊年報，另一個使用較新季度，仍可能組合成 FCF／EBITDA | TTM 回傳實際採用期末；核心流量與 SEC SBC 必須同一期末，且採用期間本身須符合既有新鮮度限制；有淨負債時利息也須與 EBIT 同期 |
| 股數快取外推 | 已有 Yahoo 當期股數仍先採用過去股數加漂移率，或缺值時製造估算市值 | SEC 股數優先，其次採用明確有限且為正的 Yahoo 股數；不再用快取漂移推估當期股數；市值未知為 ABSTAIN |
| 非有限數字及價格／成交量缺漏 | Infinity 或缺漏成交量可能繞過流動性檢查，舊價格被當成目前資料 | SEC 清理時排除非有限值；近 30 日價格與成交量需完整配對，最近價格不得落後本批 SPY 的最後交易日（若有該基準資料） |
| SEC 雙層重試 | HTTP adapter 內部重試繞過全域限速，永久 403 仍重送五次 | 關閉 adapter 內部重試，只由外層對暫時錯誤重試；每個請求都先經限速，永久錯誤立即停止 |

拆股案例：原始股數 1 億股、估值日前 10:1 拆股、估值日後 2:1 拆股，Yahoo 正規化歷史 Close 為 10 美元。正確市值為 `10 × 20億股 = 200億美元`，舊計算只有 `10 × 10億股 = 100億美元`。若後續改為 1:2 反拆，正規化 Close 變為 40 美元、股數變為 5 億股，市值仍應是 200 億美元。測試同時確認 EV/EBITDA 與 P/E。

本輪另核對了 `OCF - Maintenance CapEx - SBC`、Market Cap／EV yield 分母、平均投入資本 ROIC、三年必要 EBITDA CAGR、固定 D&A 壓力情境，以及可用因子權重的重新正規化；沒有調整原始權重或宣稱投資成效。

驗證：新增 16 個針對錯誤來源與抓取行為的測試，並更正原本假設 Yahoo Close 為未拆股價格的歷史估值測試；完整 suite 245/245 通過。`fixture_output_formula_data_audit_20261024/` 的 CSV、Dashboard 與 validator 通過，`invalid_zero = 0`。三個本輪 Python 檔案通過 AST 語法檢查。這些是固定案例與程式行為驗證，尚未逐家下載、對照 100 家最新原始財報。

參考：[yfinance 維護者的拆股處理說明](https://github.com/ranaroussi/yfinance/wiki/Price-repair)、[SEC XBRL Guide](https://www.sec.gov/files/edgar/filer-information/specifications/xbrl-guide-2025-11-19.pdf)（總稅額與當期稅額區別）、[FASB 所得稅 taxonomy 範例](https://xbrl.fasb.org/impdocs/IT_TIG/incometaxes.htm)、[SEC Company Facts API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)。
