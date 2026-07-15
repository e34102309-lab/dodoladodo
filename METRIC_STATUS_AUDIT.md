# Mode C Metric-Status Audit

## A. Root Causes

1. The dashboard used `Number(null)`, which evaluates to zero in JavaScript. Missing CSV values therefore rendered as `0.00`.
2. Specialized industry rows shared generic corporate output columns. A model-specific zero or absent metric could leak into an inapplicable generic field.
3. Trend averages accepted every numeric-looking value and candidate rules used `value or 0`, hiding missingness and coverage.
4. `Real_FCF_Yield_pct` used enterprise value as its denominator even though the label did not identify the denominator.
5. Net-cash interest coverage was represented as infinity, even though the correct display state is non-binding/not applicable.

## B. Changed Files

- `mode_c_metric_contract.py`: typed metric contract, applicability rules, evidence metadata, and output annotation.
- `AQR_ModeC_Agent_V12.py`: CapEx sensitivity, explicit FCF denominators, SBC attribution, per-share growth, historical coverage, ADS gate, and contract output.
- `mode_c_industry_models.py`: explicit cyclical FCF-to-EV metric name.
- `build_mode_c_dashboard.py`: status-aware data payload and coverage-aware trends.
- `enhance_dashboard_ui.py`: status-aware display labels and audit details.
- `validate_mode_c_outputs.py`: CSV, metadata, evidence, dashboard, and trend invariants.
- `.github/workflows/alpha_hunt.yml`: compile the contract and validate the final dashboard.
- `tests/`: regression fixtures and edge-condition coverage.

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

## E. Test Coverage

The suite contains 133 tests. New regressions cover GLW missing CapEx, PGR P&C applicability, IFNNY-style ADS reconciliation, evidence-backed true zero, low historical/trend coverage, split-adjusted per-share growth, dashboard null handling, and output consistency.

Existing tests continue to cover missing SBC, negative FCF, net cash with missing interest, non-positive EV, bank/insurance/REIT/utility/cyclical routes, split handling, amendments, and point-in-time decision availability.

## F. Before And After

| Case | Before | After |
| --- | --- | --- |
| GLW | Missing general metrics could render `0.00` | FCF, CapEx, and EV/EBITDA are null with `ABSTAIN` |
| PGR | Generic corporate fields could appear as zeros beside P&C results | Insurance metrics remain visible; generic FCF, CapEx, and EV/EBITDA are `NOT_APPLICABLE` |
| IFNNY | Foreign/ADS reconciliation could proceed without explicit FX/ADR metadata | Fixture and runtime gate return `ABSTAIN` with null FX and ADR ratio |
| FRO | Missing specialized CapEx leaked as `0.0` | Unsupported zero is null with missing/abstain status |

IFNNY is not present in the current 1,209-stock output, so its behavior is enforced by a dedicated fixture rather than claimed as a live-row comparison.

## G. Known Limits

- Maintenance CapEx is an auditable D&A/revenue-growth estimate, not company-disclosed maintenance spending. Low/base/high scenarios and confidence are shown.
- Per-share CAGR requires positive, comparable annual endpoints exactly three years apart. It abstains across missing periods or non-positive bases.
- Generic corporate EBITDA stress is implemented. Specialized industry stress uses an explicit extension status and does not display generic stress values as zeros; dedicated regulatory/catastrophe/credit stress modules remain future work.
- Short-interest freshness still depends on the configured market-data source and is not treated as SEC evidence.

## H. Reproduce

```powershell
& D:\dobird\.venv\Scripts\python.exe -m unittest discover -s tests -q
& D:\dobird\.venv\Scripts\python.exe mode_c_metric_contract.py
& D:\dobird\.venv\Scripts\python.exe build_mode_c_dashboard.py
& D:\dobird\.venv\Scripts\python.exe enhance_dashboard_ui.py public/index.html
& D:\dobird\.venv\Scripts\python.exe validate_mode_c_outputs.py --dashboard public/data.json
```

The local backfill validated 1,209 screen rows, 12 shortlist rows, 164,765 selected evidence records, and 135 trend groups without another SEC fetch.
