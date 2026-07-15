from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

import pandas as pd

from mode_c_metric_contract import (
    DISPLAY_METRICS,
    METRIC_STATUSES,
    MODEL_APPLICABLE_METRICS,
    NULL_STATUSES,
)


DEFAULT_SCREEN = "mode_c_screen.csv"
DEFAULT_SHORTLIST = "mode_c_shortlist.csv"
DEFAULT_EVIDENCE = "mode_c_evidence_ledger.csv"
MAX_SHORTLIST_SIZE = 12
MIN_SCORE = 60.0
MIN_CONFIDENCE = 70.0

SCREEN_COLUMNS = {
    "Ticker",
    "Status",
    "Scoring_Framework",
    "Input_Security_Class",
    "Security_Class_Confidence",
    "Security_Class_Evidence_Source",
    "Initial_Industry_Model_Key",
    "Model_Route_Refined",
    "Model_Route_Reason",
    "Model_Supported",
    "Industry_Model_Key",
    "Industry_Model_Decision",
    "Industry_Model_Score",
    "Industry_Model_Coverage",
    "Decision_State",
    "Decision_Timestamp",
    "Data_Confidence_Score",
    "EV_B",
    "Total_Debt_B",
    "Cash_B",
    "Net_Debt_B",
    "Debt_Source_Method",
    "ICR",
    "ICR_Method",
    "Long_Term_Score",
    "Long_Term_Eligible",
    "Stress_Survival_30",
    "Persistent_Dilution",
    "Share_Count_Change_pct",
    "Share_Count_Change_3Y_pct",
    "Share_Basis_Discontinuity",
    "Metric_Contract_Version",
    "Metric_Metadata_JSON",
    "Required_Missing_Metrics",
    "Optional_Missing_Metrics",
    "Metric_Evidence_Coverage",
}

EVIDENCE_COLUMNS = {
    "evidence_id",
    "record_type",
    "ticker",
    "available_to_model_at",
    "decision_timestamp",
    "is_available_at_decision",
    "selected_for_model",
    "source_evidence_ids",
}


class ValidationError(RuntimeError):
    pass


ZERO_EVIDENCE_METRICS = {
    "MarketCap_B",
    "EV_B",
    "TTM_OCF_B",
    "Dynamic_CapEx_B",
    "Maintenance_CapEx_B",
    "TTM_SBC_B",
    "Real_FCF_Yield_pct",
    "Conservative_Real_FCF_Yield_pct",
    "Total_Debt_B",
    "Cash_B",
    "Net_Debt_B",
    "ICR",
    "ROIC_pct",
    "EV_EBITDA_x",
}


def _as_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not pd.isna(value):
        if float(value) in {0.0, 1.0}:
            return bool(value)
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ValidationError(f"Invalid boolean in {field}: {value!r}")


def _bool_series(series: pd.Series, field: str) -> pd.Series:
    return series.map(lambda value: _as_bool(value, field))


def _number(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return math.nan
    return parsed if math.isfinite(parsed) else math.nan


def _timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _require_columns(actual: Iterable[str], required: set[str], label: str) -> None:
    missing = sorted(required - set(actual))
    if missing:
        raise ValidationError(f"{label} is missing columns: {', '.join(missing)}")


def _parse_metadata(raw: Any, ticker: str) -> Dict[str, Any]:
    try:
        metadata = json.loads(str(raw or "{}"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{ticker} has invalid Metric_Metadata_JSON") from exc
    if not isinstance(metadata, dict):
        raise ValidationError(f"{ticker} metric metadata must be an object")
    return metadata


def _validate_metric_contract(screen: pd.DataFrame) -> None:
    for _, row in screen.iterrows():
        ticker = str(row["Ticker"])
        model_key = str(row.get("Industry_Model_Key") or "GENERAL_CORPORATE").upper()
        metadata = _parse_metadata(row.get("Metric_Metadata_JSON"), ticker)
        missing_entries = sorted(set(DISPLAY_METRICS) - set(metadata))
        if missing_entries:
            raise ValidationError(
                f"{ticker} metric metadata is incomplete: {', '.join(missing_entries[:5])}"
            )
        applicable = MODEL_APPLICABLE_METRICS.get(model_key, frozenset())
        for metric in DISPLAY_METRICS:
            meta = metadata[metric]
            if not isinstance(meta, dict):
                raise ValidationError(f"{ticker} {metric} metadata is not an object")
            status = str(meta.get("status") or "").upper()
            if status not in METRIC_STATUSES:
                raise ValidationError(f"{ticker} {metric} has invalid status {status!r}")
            value = _number(meta.get("value"))
            raw_value = _number(row.get(metric))
            evidence_ids = meta.get("evidence_ids")
            if not isinstance(evidence_ids, list):
                raise ValidationError(f"{ticker} {metric} evidence_ids must be a list")
            for field in ("reason", "as_of", "source_method"):
                if field not in meta:
                    raise ValidationError(f"{ticker} {metric} lacks {field}")
            if status in NULL_STATUSES:
                if math.isfinite(value) or math.isfinite(raw_value):
                    raise ValidationError(
                        f"{ticker} {metric} exposes a value while status is {status}"
                    )
            elif not math.isfinite(value):
                raise ValidationError(f"{ticker} {metric} status {status} lacks a finite value")
            elif not math.isfinite(raw_value) or not math.isclose(raw_value, value, abs_tol=1e-9):
                raise ValidationError(f"{ticker} {metric} CSV value differs from metadata")
            if metric not in applicable and (
                status != "NOT_APPLICABLE" or math.isfinite(raw_value)
            ):
                raise ValidationError(
                    f"{ticker} specialized model exposes non-applicable generic metric {metric}"
                )
            if (
                status in {"VALID", "ESTIMATED"}
                and value == 0.0
                and metric in ZERO_EVIDENCE_METRICS
                and (not evidence_ids or not str(meta.get("source_method") or "").strip())
            ):
                raise ValidationError(f"{ticker} {metric} true zero lacks evidence")
        if str(row.get("Decision_State") or "").upper() == "ABSTAIN":
            if metadata["Long_Term_Score"]["status"] not in NULL_STATUSES:
                raise ValidationError(f"{ticker} ABSTAIN row has a displayable long-term score")
        if str(row.get("Input_Security_Class") or "").upper() == "COMMON_ADS_INFERRED":
            for metric in ("Point_in_Time_FX_Rate", "ADR_Ratio"):
                if metadata[metric]["status"] not in {"VALID", "ABSTAIN"}:
                    raise ValidationError(f"{ticker} {metric} must be VALID or ABSTAIN for ADS")


def _validate_dashboard(dashboard_path: Path, screen: pd.DataFrame) -> Dict[str, Any]:
    payload = json.loads(dashboard_path.read_text(encoding="utf-8"))
    stocks = payload.get("stocks")
    if not isinstance(stocks, list):
        raise ValidationError("dashboard stocks must be a list")
    dashboard_by_ticker = {
        str(item.get("Ticker") or "").upper(): item
        for item in stocks
        if isinstance(item, dict)
    }
    if set(dashboard_by_ticker) != set(screen["Ticker"]):
        raise ValidationError("dashboard ticker set differs from screen")
    for _, row in screen.iterrows():
        ticker = str(row["Ticker"])
        expected = _parse_metadata(row["Metric_Metadata_JSON"], ticker)
        actual = dashboard_by_ticker[ticker].get("Metric_Metadata")
        if actual != expected:
            raise ValidationError(f"{ticker} dashboard metric metadata differs from screen")
        for metric in DISPLAY_METRICS:
            expected_value = expected[metric].get("value")
            actual_value = dashboard_by_ticker[ticker].get(metric)
            if expected_value is None and actual_value is not None:
                raise ValidationError(f"{ticker} dashboard fabricates {metric}")
            if expected_value is not None and _number(actual_value) != _number(expected_value):
                raise ValidationError(f"{ticker} dashboard value differs for {metric}")
    groups = payload.get("trend_groups", {})
    for key, group in groups.items():
        coverage = group.get("metric_coverage", {})
        for field, summary in coverage.items():
            ratio = _number(summary.get("coverage"))
            if math.isfinite(ratio) and ratio < 0.50 and group.get(field) is not None:
                raise ValidationError(f"trend group {key} exposes low-coverage {field}")
    for candidate in payload.get("emerging_candidates", []):
        ratio = _number(
            candidate.get("metric_coverage", {})
            .get("avg_score", {})
            .get("coverage")
        )
        if not math.isfinite(ratio) or ratio < 0.50:
            raise ValidationError("low-coverage trend group became an emerging candidate")
    return {"dashboard_rows": len(stocks), "trend_groups": len(groups)}


def _validate_screen(
    screen_path: Path,
    shortlist_path: Path,
) -> tuple[pd.DataFrame, Dict[str, Any], datetime]:
    screen = pd.read_csv(screen_path, encoding="utf-8-sig")
    _require_columns(screen.columns, SCREEN_COLUMNS, "screen")
    if screen.empty:
        raise ValidationError("screen output is empty")

    tickers = screen["Ticker"].fillna("").astype(str).str.strip().str.upper()
    if (tickers == "").any() or tickers.duplicated().any():
        raise ValidationError("screen tickers must be non-empty and unique")
    screen = screen.copy()
    screen["Ticker"] = tickers

    states = screen["Decision_State"].fillna("").astype(str).str.strip().str.upper()
    invalid_states = sorted(set(states) - {"PASS", "FAIL", "ABSTAIN"})
    if invalid_states:
        raise ValidationError(f"invalid or blank decision states: {invalid_states}")
    decision_timestamps = screen["Decision_Timestamp"].map(_timestamp)
    if decision_timestamps.isna().any():
        bad = screen.loc[decision_timestamps.isna(), "Ticker"].tolist()
        raise ValidationError(f"screen rows have invalid decision timestamps: {bad}")
    if decision_timestamps.nunique() != 1:
        raise ValidationError("screen rows do not share one point-in-time decision timestamp")

    statuses = screen["Status"].fillna("").astype(str)
    expected_states = statuses.map(
        lambda status: (
            "PASS"
            if status == "Pass"
            else "ABSTAIN"
            if status.startswith(("Abstain", "Error"))
            else "FAIL"
            if status.startswith("Fail")
            else ""
        )
    )
    unknown_status = expected_states == ""
    if unknown_status.any():
        bad = screen.loc[unknown_status, "Ticker"].tolist()
        raise ValidationError(f"unknown status prefixes: {bad}")
    incoherent = (expected_states != "") & (states != expected_states)
    if incoherent.any():
        bad = screen.loc[incoherent, "Ticker"].tolist()
        raise ValidationError(f"status/decision-state mismatch: {bad}")

    eligible = _bool_series(screen["Long_Term_Eligible"], "Long_Term_Eligible")
    model_supported = _bool_series(screen["Model_Supported"], "Model_Supported")
    persistent = _bool_series(screen["Persistent_Dilution"], "Persistent_Dilution")
    discontinuity = _bool_series(
        screen["Share_Basis_Discontinuity"], "Share_Basis_Discontinuity"
    )
    stress_survival = _bool_series(screen["Stress_Survival_30"], "Stress_Survival_30")
    scores = pd.to_numeric(screen["Long_Term_Score"], errors="coerce")
    confidence = pd.to_numeric(screen["Data_Confidence_Score"], errors="coerce")
    enterprise_value = pd.to_numeric(screen["EV_B"], errors="coerce")
    total_debt = pd.to_numeric(screen["Total_Debt_B"], errors="coerce")
    cash = pd.to_numeric(screen["Cash_B"], errors="coerce")
    net_debt = pd.to_numeric(screen["Net_Debt_B"], errors="coerce")
    icr = pd.to_numeric(screen["ICR"], errors="coerce")
    debt_methods = screen["Debt_Source_Method"].fillna("").astype(str).str.strip()
    icr_methods = screen["ICR_Method"].fillna("").astype(str).str.strip()
    specialized = screen["Scoring_Framework"].fillna("").astype(str).str.startswith(
        "INDUSTRY_SPECIALIZED_"
    )

    invalid_eligible = eligible & (
        (states != "PASS")
        | (statuses != "Pass")
        | ~model_supported
        | scores.lt(MIN_SCORE)
        | scores.isna()
        | confidence.lt(MIN_CONFIDENCE)
        | confidence.isna()
        | persistent
    )
    if invalid_eligible.any():
        bad = screen.loc[invalid_eligible, "Ticker"].tolist()
        raise ValidationError(f"eligible rows violate hard gates: {bad}")
    abstain_with_score = (states == "ABSTAIN") & scores.notna()
    if abstain_with_score.any():
        bad = screen.loc[abstain_with_score, "Ticker"].tolist()
        raise ValidationError(f"abstain rows expose misleading long-term scores: {bad}")

    complete_debt_rows = total_debt.notna() & cash.notna() & net_debt.notna()
    inconsistent_net_debt = complete_debt_rows & (
        (net_debt - (total_debt - cash)).abs() > 0.011
    )
    if inconsistent_net_debt.any():
        bad = screen.loc[inconsistent_net_debt, "Ticker"].tolist()
        raise ValidationError(f"net debt does not equal debt minus cash: {bad}")
    missing_eligible_debt = eligible & ~specialized & (
        ~complete_debt_rows | (debt_methods == "") | (icr_methods == "")
    )
    if missing_eligible_debt.any():
        bad = screen.loc[missing_eligible_debt, "Ticker"].tolist()
        raise ValidationError(f"eligible rows lack auditable debt balances: {bad}")

    one_year = pd.to_numeric(screen["Share_Count_Change_pct"], errors="coerce")
    three_year = pd.to_numeric(screen["Share_Count_Change_3Y_pct"], errors="coerce")
    unexplained_jump = ((one_year.abs() > 50.0) | (three_year.abs() > 50.0)) & ~discontinuity
    if unexplained_jump.any():
        bad = screen.loc[unexplained_jump, "Ticker"].tolist()
        raise ValidationError(f"share-basis jumps lack discontinuity flags: {bad}")
    if (discontinuity & persistent).any():
        bad = screen.loc[discontinuity & persistent, "Ticker"].tolist()
        raise ValidationError(f"share discontinuity was misclassified as dilution: {bad}")

    general_eligible = eligible & ~specialized
    if (general_eligible & ~stress_survival).any():
        bad = screen.loc[general_eligible & ~stress_survival, "Ticker"].tolist()
        raise ValidationError(f"general eligible rows failed stress survival: {bad}")
    invalid_enterprise_value = general_eligible & (
        enterprise_value.isna() | enterprise_value.le(0.0)
    )
    if invalid_enterprise_value.any():
        bad = screen.loc[invalid_enterprise_value, "Ticker"].tolist()
        raise ValidationError(f"general eligible rows have non-positive enterprise value: {bad}")
    invalid_infinite_icr = general_eligible & (net_debt > 0.01) & icr.map(math.isinf)
    if invalid_infinite_icr.any():
        bad = screen.loc[invalid_infinite_icr, "Ticker"].tolist()
        raise ValidationError(f"positive-net-debt rows expose infinite ICR: {bad}")
    invalid_net_cash_method = icr_methods.isin(
        {"net_cash", "net_cash_non_binding"}
    ) & (net_debt > 0.01)
    if invalid_net_cash_method.any():
        bad = screen.loc[invalid_net_cash_method, "Ticker"].tolist()
        raise ValidationError(f"net-cash ICR method applied to positive net debt: {bad}")
    invalid_immaterial_method = (icr_methods == "immaterial_debt") & (total_debt > 0.01)
    if invalid_immaterial_method.any():
        bad = screen.loc[invalid_immaterial_method, "Ticker"].tolist()
        raise ValidationError(f"immaterial-debt ICR method applied to material debt: {bad}")

    specialized_scores = pd.to_numeric(screen["Industry_Model_Score"], errors="coerce")
    specialized_coverage = pd.to_numeric(
        screen["Industry_Model_Coverage"], errors="coerce"
    )
    specialized_decisions = (
        screen["Industry_Model_Decision"].fillna("").astype(str).str.upper()
    )
    specialized_keys = (
        screen["Industry_Model_Key"].fillna("").astype(str).str.upper()
    )
    initial_keys = (
        screen["Initial_Industry_Model_Key"].fillna("").astype(str).str.upper()
    )
    route_refined = _bool_series(
        screen["Model_Route_Refined"],
        "Model_Route_Refined",
    )
    route_reasons = screen["Model_Route_Reason"].fillna("").astype(str)
    comparable_initial = ~initial_keys.isin({"", "UNSPECIFIED"})
    route_changed = comparable_initial & (initial_keys != specialized_keys)
    valid_lender_refinement = (
        route_refined
        & (initial_keys == "FINANCIAL_FEE")
        & (specialized_keys == "FINANCIAL_LENDER")
        & route_reasons.str.contains("SEC balance-sheet refinement", regex=False)
    )
    invalid_route_change = route_changed & ~valid_lender_refinement
    invalid_refinement_flag = route_refined & ~route_changed
    if invalid_route_change.any() or invalid_refinement_flag.any():
        bad = screen.loc[invalid_route_change | invalid_refinement_flag, "Ticker"].tolist()
        raise ValidationError(f"unaudited industry-model route change: {bad}")

    security_classes = (
        screen["Input_Security_Class"].fillna("").astype(str).str.upper()
    )
    security_confidence = (
        screen["Security_Class_Confidence"].fillna("").astype(str).str.upper()
    )
    security_sources = (
        screen["Security_Class_Evidence_Source"].fillna("").astype(str).str.strip()
    )
    allowed_security_classes = {
        "COMMON_OR_EQUIVALENT",
        "COMMON_ADS_INFERRED",
        "UNSPECIFIED_MANUAL_INPUT",
    }
    invalid_security_class = ~security_classes.isin(allowed_security_classes)
    invalid_security_confidence = (
        ((security_classes == "COMMON_OR_EQUIVALENT") & (security_confidence != "HIGH"))
        | ((security_classes == "COMMON_ADS_INFERRED") & (security_confidence != "MEDIUM"))
        | (security_sources == "")
    )
    if invalid_security_class.any() or invalid_security_confidence.any():
        bad = screen.loc[
            invalid_security_class | invalid_security_confidence,
            "Ticker",
        ].tolist()
        raise ValidationError(f"screen rows lack auditable common-security evidence: {bad}")

    framework_keys = (
        screen["Scoring_Framework"]
        .fillna("")
        .astype(str)
        .str.extract(r"^INDUSTRY_SPECIALIZED_(.+)_V1$", expand=False)
        .fillna("")
        .str.upper()
    )
    mismatched_specialized_route = specialized & (
        (specialized_keys == "") | (framework_keys != specialized_keys)
    )
    if mismatched_specialized_route.any():
        bad = screen.loc[mismatched_specialized_route, "Ticker"].tolist()
        raise ValidationError(f"specialized framework/model-key mismatch: {bad}")
    invalid_specialized = eligible & specialized & (
        (specialized_decisions != "PASS")
        | specialized_scores.lt(MIN_SCORE)
        | specialized_scores.isna()
        | specialized_coverage.lt(65.0)
        | specialized_coverage.isna()
    )
    if invalid_specialized.any():
        bad = screen.loc[invalid_specialized, "Ticker"].tolist()
        raise ValidationError(f"specialized eligible rows violate model gates: {bad}")

    if "Historical_Valuation_Status" in screen.columns:
        historical_status = (
            screen["Historical_Valuation_Status"].fillna("").astype(str).str.upper()
        )
        invalid_history = eligible & historical_status.ne("") & historical_status.ne("VALID")
        if invalid_history.any():
            bad = screen.loc[invalid_history, "Ticker"].tolist()
            raise ValidationError(f"eligible rows have low point-in-time valuation coverage: {bad}")

    _validate_metric_contract(screen)
    required_missing = screen["Required_Missing_Metrics"].fillna("").astype(str).str.strip()
    if (eligible & required_missing.ne("")).any():
        bad = screen.loc[eligible & required_missing.ne(""), "Ticker"].tolist()
        raise ValidationError(f"eligible rows have required metric gaps: {bad}")

    shortlist = pd.read_csv(shortlist_path, encoding="utf-8-sig")
    _require_columns(shortlist.columns, {"Ticker"}, "shortlist")
    actual_shortlist = shortlist["Ticker"].fillna("").astype(str).str.upper().tolist()
    expected_shortlist = (
        screen.loc[eligible, ["Ticker"]]
        .assign(_score=scores[eligible].to_numpy())
        .sort_values(["_score", "Ticker"], ascending=[False, True])
        .head(MAX_SHORTLIST_SIZE)["Ticker"]
        .tolist()
    )
    if actual_shortlist != expected_shortlist:
        raise ValidationError(
            f"shortlist ranking mismatch: expected={expected_shortlist}, actual={actual_shortlist}"
        )

    return screen, {
        "screen_rows": int(len(screen)),
        "eligible_rows": int(eligible.sum()),
        "shortlist_rows": int(len(shortlist)),
        "decision_counts": states.value_counts().sort_index().to_dict(),
    }, decision_timestamps.iloc[0]


def _validate_evidence(
    evidence_path: Path,
    screen_tickers: set[str],
    screen_decision_timestamp: datetime,
) -> Dict[str, Any]:
    evidence_ids: set[str] = set()
    lineage: Dict[str, list[str]] = {}
    availability_by_id: Dict[str, datetime] = {}
    source_count = 0
    derived_count = 0
    with evidence_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        _require_columns(reader.fieldnames or [], EVIDENCE_COLUMNS, "evidence ledger")
        for line_number, row in enumerate(reader, start=2):
            evidence_id = str(row.get("evidence_id") or "").strip()
            if not evidence_id or evidence_id in evidence_ids:
                raise ValidationError(
                    f"blank or duplicate evidence_id at line {line_number}: {evidence_id!r}"
                )
            evidence_ids.add(evidence_id)
            ticker = str(row.get("ticker") or "").strip().upper()
            if ticker not in screen_tickers:
                raise ValidationError(
                    f"evidence ticker is absent from screen at line {line_number}: {ticker!r}"
                )
            if not _as_bool(row.get("selected_for_model"), "selected_for_model"):
                raise ValidationError(f"unselected evidence exported at line {line_number}")
            if not _as_bool(
                row.get("is_available_at_decision"), "is_available_at_decision"
            ):
                raise ValidationError(f"future/unavailable evidence selected at line {line_number}")

            available_at = _timestamp(row.get("available_to_model_at"))
            decision_at = _timestamp(row.get("decision_timestamp"))
            if available_at is None or decision_at is None or available_at > decision_at:
                raise ValidationError(
                    f"invalid point-in-time availability at line {line_number}"
                )
            if decision_at != screen_decision_timestamp:
                raise ValidationError(
                    f"evidence decision timestamp differs from screen at line {line_number}"
                )
            availability_by_id[evidence_id] = available_at

            record_type = str(row.get("record_type") or "")
            if record_type == "source_fact":
                source_count += 1
                continue
            if record_type != "derived_metric":
                raise ValidationError(f"invalid record_type at line {line_number}: {record_type!r}")
            derived_count += 1
            try:
                source_ids = json.loads(row.get("source_evidence_ids") or "[]")
            except json.JSONDecodeError as exc:
                raise ValidationError(
                    f"invalid source_evidence_ids JSON at line {line_number}"
                ) from exc
            if not isinstance(source_ids, list) or not source_ids:
                raise ValidationError(f"selected derived metric has no lineage at line {line_number}")
            lineage[evidence_id] = [str(item) for item in source_ids]

    if not evidence_ids:
        raise ValidationError("selected evidence ledger is empty")
    missing_sources = sorted(
        {
            source_id
            for source_ids in lineage.values()
            for source_id in source_ids
            if source_id not in evidence_ids
        }
    )
    if missing_sources:
        raise ValidationError(
            f"derived evidence references {len(missing_sources)} missing source ids"
        )
    for evidence_id, source_ids in lineage.items():
        derived_at = availability_by_id[evidence_id]
        latest_source_at = max(availability_by_id[source_id] for source_id in source_ids)
        if derived_at < latest_source_at:
            raise ValidationError(
                f"derived evidence predates one of its sources: {evidence_id}"
            )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(evidence_id: str) -> None:
        if evidence_id in visiting:
            raise ValidationError(f"cyclic evidence lineage detected at {evidence_id}")
        if evidence_id in visited or evidence_id not in lineage:
            return
        visiting.add(evidence_id)
        for source_id in lineage[evidence_id]:
            visit(source_id)
        visiting.remove(evidence_id)
        visited.add(evidence_id)

    for evidence_id in lineage:
        visit(evidence_id)
    return {
        "evidence_rows": len(evidence_ids),
        "source_evidence_rows": source_count,
        "derived_evidence_rows": derived_count,
    }


def validate_outputs(
    screen_path: str | Path = DEFAULT_SCREEN,
    shortlist_path: str | Path = DEFAULT_SHORTLIST,
    evidence_path: str | Path = DEFAULT_EVIDENCE,
    dashboard_path: str | Path | None = None,
) -> Dict[str, Any]:
    paths = [Path(screen_path), Path(shortlist_path), Path(evidence_path)]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise ValidationError(f"missing output files: {', '.join(missing)}")
    screen, summary, decision_timestamp = _validate_screen(paths[0], paths[1])
    summary.update(
        _validate_evidence(
            paths[2],
            set(screen["Ticker"]),
            decision_timestamp,
        )
    )
    if dashboard_path is not None:
        dashboard = Path(dashboard_path)
        if not dashboard.is_file():
            raise ValidationError(f"missing dashboard file: {dashboard}")
        summary.update(_validate_dashboard(dashboard, screen))
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate Mode C output invariants")
    parser.add_argument("--screen", default=DEFAULT_SCREEN)
    parser.add_argument("--shortlist", default=DEFAULT_SHORTLIST)
    parser.add_argument("--evidence", default=DEFAULT_EVIDENCE)
    parser.add_argument("--dashboard")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        summary = validate_outputs(
            args.screen,
            args.shortlist,
            args.evidence,
            args.dashboard,
        )
    except ValidationError as exc:
        print(f"Mode C output validation failed: {exc}")
        return 1
    print(json.dumps({"status": "ok", **summary}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
