from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

import pandas as pd

from mode_c_decision_inputs import PORTFOLIO_FIT_CONTRACT_VERSION
from mode_c_metric_contract import (
    DISPLAY_METRICS,
    METRIC_CONTRACT_VERSION,
    METRIC_STATUSES,
    MODEL_APPLICABLE_METRICS,
    NULL_STATUSES,
)
from mode_c_research_priority import (
    CROSS_MODEL_CALIBRATION_STATUS,
    RESEARCH_PRIORITY_METHOD,
    RESEARCH_PRIORITY_VERSION,
    research_priority_order,
    shrunk_percentile,
)


DEFAULT_SCREEN = "mode_c_screen.csv"
DEFAULT_SHORTLIST = "mode_c_shortlist.csv"
DEFAULT_EVIDENCE = "mode_c_evidence_ledger.csv"
MAX_SHORTLIST_SIZE = 12
MIN_SCORE = 60.0
MIN_CONFIDENCE = 70.0
MIN_STARTER_SCORE = 75.0
ETF_TOP10_MIN_STARTER_SCORE = 80.0
MAX_PORTFOLIO_INPUT_AGE_DAYS = 45.0
WORKING_CAPITAL_MIN_MATERIAL_DAYS = 5.0

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
    "Decision_Reason_Code",
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
    "Raw_Model_Score",
    "Model_Peer_Count",
    "Within_Model_Percentile",
    "Shrunk_Within_Model_Percentile",
    "Cross_Model_Calibration_Status",
    "Cross_Model_Comparable",
    "Research_Priority_Rank",
    "Research_Priority_Round",
    "Research_Priority_Method",
    "Research_Priority_Version",
    "Model_Eligible",
    "Global_Research_Queue",
    "Human_KPI_Review_Required",
    "Specialized_Stress_Pending",
    "Specialized_Stress_Failed",
    "Specialized_Stress_Status",
    "Specialized_Stress_Scenario",
    "Specialized_Stress_Reason",
    "Specialized_Stress_Survival",
    "Specialized_Stress_Missing_Inputs",
    "Portfolio_Fit_Pending",
    "Portfolio_Fit_Status",
    "Portfolio_Fit_Reason",
    "Portfolio_Fit_AsOf",
    "Portfolio_Fit_Input_Age_Days",
    "Portfolio_Current_Position_Weight_pct_Total",
    "Portfolio_PreTrade_Active_Sleeve_Weight_pct_Total",
    "Portfolio_PreTrade_Sector_Weight_pct_Total",
    "Portfolio_PreTrade_Economic_Risk_Weight_pct_Total",
    "Portfolio_PostTrade_Position_Weight_pct_Total",
    "Portfolio_PostTrade_Active_Sleeve_Weight_pct_Total",
    "Portfolio_PostTrade_Sector_Weight_pct_Total",
    "Portfolio_PostTrade_Economic_Risk_Weight_pct_Total",
    "ETF_Lookthrough_Weight_pct_Total",
    "Portfolio_ETF_Top10_Overlap",
    "Portfolio_Correlation_Stress_Status",
    "Economic_Risk_Bucket",
    "Portfolio_Fit_Contract_Version",
    "Starter_Candidate",
    "Suggested_Starter_Weight_pct_Total",
    "Research_Action_State",
    "Research_Statuses",
    "DSI_Status",
    "DSI_Score",
    "Working_Capital_Quality_Status",
    "Working_Capital_Quality_State",
    "Working_Capital_Quality_Coverage",
    "Working_Capital_Risk_Penalty",
    "Working_Capital_Quality_Reasons",
    "DSO_Days",
    "DPO_Days",
    "AR_vs_Revenue_Growth_Gap_pp",
    "AP_vs_COGS_Growth_Gap_pp",
    "Applicable_Factor_Weight",
    "Available_Factor_Weight",
    "Factor_Coverage",
    "Weight_Renormalized",
    "Dilution_Double_Count_Check",
    "Ownership_Dilution_Penalty",
    "Capital_Allocation_Penalty",
    "Persistent_Dilution_Hard_Gate",
    "Dilution_Total_Score_Impact",
    "Stress_Survival_30",
    "Persistent_Dilution",
    "TTM_Gross_Buyback_B",
    "TTM_Stock_Issuance_B",
    "Real_Buyback_B",
    "Acquisition_Stock_Consideration_B",
    "Acquisition_Issuance_Attribution_Status",
    "Acquisition_Issuance_Reconciliation_Status",
    "Acquisition_Related_Issuance_Flag",
    "Acquisition_Accretion_Review_Required",
    "Share_Count_Change_pct",
    "Share_Count_Change_3Y_pct",
    "Share_Basis_Discontinuity",
    "Growth_CapEx_Risk_State",
    "Growth_CapEx_Risk_Corroboration_Count",
    "Growth_CapEx_Risk_Reasons",
    "Metric_Contract_Version",
    "Metric_Metadata_JSON",
    "Required_Missing_Metrics",
    "Optional_Missing_Metrics",
    "Metric_Evidence_Coverage",
    "Applicable_Metrics",
    "Not_Applicable_Metrics",
    "Metric_Status_Counts_JSON",
    "Latest_Metric_AsOf",
    "Uses_Yahoo_Fallback",
    "Uses_Annual_Fallback",
    "Uses_Estimated_Maintenance_CapEx",
    "Uses_FX_Conversion",
    "Metric_Data_Complete",
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
    "TTM_Gross_Buyback_B",
    "TTM_Stock_Issuance_B",
    "Acquisition_Stock_Consideration_B",
    "Maintenance_Real_FCF_B",
    "Conservative_Real_FCF_B",
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


def _metadata_number(metadata: Dict[str, Any], metric: str) -> float:
    entry = metadata.get(metric, {})
    if not isinstance(entry, dict) or str(entry.get("status") or "") not in {
        "VALID", "ESTIMATED"
    }:
        return math.nan
    return _number(entry.get("value"))


def _validate_financial_formulas(screen: pd.DataFrame) -> None:
    output_metrics = (
        "Maintenance_Real_FCF_B",
        "Conservative_Real_FCF_B",
        "Real_FCF_Yield_pct",
        "Conservative_Real_FCF_Yield_pct",
        "Maintenance_Real_FCF_to_MarketCap_Yield_pct",
        "Conservative_Real_FCF_to_MarketCap_Yield_pct",
        "Maintenance_Real_FCF_to_EV_Yield_pct",
        "Conservative_Real_FCF_to_EV_Yield_pct",
    )
    for _, row in screen.iterrows():
        ticker = str(row["Ticker"])
        if str(row.get("Industry_Model_Key") or "GENERAL_CORPORATE").upper() != "GENERAL_CORPORATE":
            continue
        metadata = _parse_metadata(row.get("Metric_Metadata_JSON"), ticker)
        ocf = _metadata_number(metadata, "TTM_OCF_B")
        total_capex = _metadata_number(metadata, "Dynamic_CapEx_B")
        maintenance_capex = _metadata_number(metadata, "Maintenance_CapEx_B")
        sbc = _metadata_number(metadata, "TTM_SBC_B")
        market_cap = _metadata_number(metadata, "MarketCap_B")
        enterprise_value = _metadata_number(metadata, "EV_B")
        inputs_complete = all(
            math.isfinite(value)
            for value in (ocf, total_capex, maintenance_capex, sbc, market_cap)
        ) and market_cap > 0
        if not inputs_complete:
            leaked = [
                metric
                for metric in output_metrics[:6]
                if math.isfinite(_metadata_number(metadata, metric))
            ]
            if leaked:
                raise ValidationError(
                    f"{ticker} FCF outputs survive missing core inputs: {leaked}"
                )
            continue

        maintenance_fcf = ocf - maintenance_capex - sbc
        conservative_fcf = ocf - total_capex - sbc
        expected = {
            "Maintenance_Real_FCF_B": maintenance_fcf,
            "Conservative_Real_FCF_B": conservative_fcf,
            "Real_FCF_Yield_pct": maintenance_fcf / market_cap * 100.0,
            "Maintenance_Real_FCF_to_MarketCap_Yield_pct": maintenance_fcf / market_cap * 100.0,
            "Conservative_Real_FCF_Yield_pct": conservative_fcf / market_cap * 100.0,
            "Conservative_Real_FCF_to_MarketCap_Yield_pct": conservative_fcf / market_cap * 100.0,
        }
        if math.isfinite(enterprise_value) and enterprise_value > 0:
            expected.update(
                {
                    "Maintenance_Real_FCF_to_EV_Yield_pct": maintenance_fcf / enterprise_value * 100.0,
                    "Conservative_Real_FCF_to_EV_Yield_pct": conservative_fcf / enterprise_value * 100.0,
                }
            )
        else:
            leaked_ev_yields = [
                metric
                for metric in output_metrics[6:]
                if math.isfinite(_metadata_number(metadata, metric))
            ]
            if leaked_ev_yields:
                raise ValidationError(
                    f"{ticker} EV-based FCF yields survive missing enterprise value: "
                    f"{leaked_ev_yields}"
                )
        for metric, expected_value in expected.items():
            actual = _metadata_number(metadata, metric)
            tolerance = 0.08 if metric.endswith("pct") else 0.0025
            if not math.isfinite(actual) or not math.isclose(
                actual, expected_value, abs_tol=tolerance
            ):
                raise ValidationError(
                    f"{ticker} {metric} cannot be recomputed from audited inputs"
                )
        economic_cost = _metadata_number(metadata, "SBC_Economic_Cost_B")
        if math.isfinite(economic_cost) and not math.isclose(economic_cost, sbc, abs_tol=0.0025):
            raise ValidationError(f"{ticker} SBC economic cost differs from TTM SBC")


def _validate_bank_stress_formulas(screen: pd.DataFrame) -> None:
    for _, row in screen.iterrows():
        if str(row.get("Industry_Model_Key", "")).upper() != "BANK":
            continue
        status = str(row.get("Specialized_Stress_Status", "")).upper()
        if status not in {"PASS", "FAIL"}:
            continue
        ticker = str(row.get("Ticker", ""))
        try:
            raw = row.get("Industry_Model_Metrics_JSON", "")
            metrics = raw if isinstance(raw, dict) else json.loads(raw)
            names = (
                "assets_b", "tangible_equity_b", "loans_b", "credit_loss_allowance_b",
                "risk_weighted_assets_b", "tier1_ratio_pct", "tier1_well_capitalized_min_pct",
                "bank_stress_after_tax_capital_loss_b", "bank_stress_tier1_ratio_pct",
                "bank_stress_risk_weighted_assets_b",
            )
            values = {name: _number(metrics.get(name)) for name in names}
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValidationError(f"bank stress metrics are invalid: {ticker}") from exc
        if any(not math.isfinite(value) for value in values.values()) or values["risk_weighted_assets_b"] <= 0:
            raise ValidationError(f"bank stress lacks finite inputs or positive RWA: {ticker}")
        rwa = values["risk_weighted_assets_b"]
        loss = max(max(values["loans_b"], 0.0) * 0.03 - max(values["credit_loss_allowance_b"], 0.0), 0.0) * 0.79
        tier1 = values["tier1_ratio_pct"] - loss / rwa * 100.0
        stressed_equity = values["tangible_equity_b"] - loss
        stressed_assets = values["assets_b"] - loss
        expected_survival = (
            stressed_equity > 0 and stressed_assets > 0
            and stressed_equity / stressed_assets * 100.0 >= 3.0
            and tier1 >= values["tier1_well_capitalized_min_pct"]
        )
        reconciled = (
            math.isclose(values["bank_stress_risk_weighted_assets_b"], rwa, abs_tol=1e-6)
            and math.isclose(values["bank_stress_after_tax_capital_loss_b"], loss, abs_tol=1e-6)
            and math.isclose(values["bank_stress_tier1_ratio_pct"], tier1, abs_tol=0.011)
            and (status == "PASS") == expected_survival
        )
        if not reconciled:
            raise ValidationError(f"bank stress does not reconcile to RWA and capital loss: {ticker}")


def build_zero_classification_report(screen: pd.DataFrame) -> Dict[str, Any]:
    records: list[Dict[str, Any]] = []
    null_status_counts = {status: 0 for status in NULL_STATUSES}
    summary = {
        "true_zero": 0,
        "invalid_zero": 0,
        "not_applicable": 0,
        "missing": 0,
    }
    for _, row in screen.iterrows():
        ticker = str(row["Ticker"])
        metadata = _parse_metadata(row.get("Metric_Metadata_JSON"), ticker)
        for metric in DISPLAY_METRICS:
            meta = metadata.get(metric, {})
            status = str(meta.get("status") or "").upper()
            raw_value = _number(row.get(metric))
            if status in null_status_counts:
                null_status_counts[status] += 1
                if status == "NOT_APPLICABLE":
                    summary["not_applicable"] += 1
                elif status in {"MISSING", "ABSTAIN", "STALE", "INVALID"}:
                    summary["missing"] += 1
            if not math.isfinite(raw_value) or raw_value != 0.0:
                continue
            evidenced = bool(meta.get("evidence_ids"))
            derived = bool(str(meta.get("source_method") or "").strip())
            classification = (
                "true_zero"
                if status in {"VALID", "ESTIMATED"} and (evidenced or derived)
                else "invalid_zero"
            )
            summary[classification] += 1
            records.append(
                {
                    "ticker": ticker,
                    "metric": metric,
                    "classification": classification,
                    "status": status,
                    "reason": meta.get("reason"),
                    "source_method": meta.get("source_method"),
                    "evidence_ids": meta.get("evidence_ids", []),
                }
            )
    return {
        "metric_contract_version": METRIC_CONTRACT_VERSION,
        "summary": summary,
        "null_status_counts": null_status_counts,
        "zero_records": records,
    }


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
        dashboard_row = dashboard_by_ticker[ticker]
        actual = dashboard_row.get("Metric_Metadata")
        if actual != expected:
            raise ValidationError(f"{ticker} dashboard metric metadata differs from screen")
        for metric in DISPLAY_METRICS:
            expected_value = expected[metric].get("value")
            actual_value = dashboard_by_ticker[ticker].get(metric)
            if expected_value is None and actual_value is not None:
                raise ValidationError(f"{ticker} dashboard fabricates {metric}")
            if expected_value is not None and _number(actual_value) != _number(expected_value):
                raise ValidationError(f"{ticker} dashboard value differs for {metric}")
        expected_counts = json.loads(str(row.get("Metric_Status_Counts_JSON") or "{}"))
        if dashboard_row.get("Data_Integrity_Summary") != expected_counts:
            raise ValidationError(f"{ticker} dashboard integrity summary differs from screen")
        for field in (
            "Required_Missing_Metrics",
            "Optional_Missing_Metrics",
            "Applicable_Metrics",
            "Not_Applicable_Metrics",
            "Latest_Metric_AsOf",
        ):
            raw_text = row.get(field)
            expected_text = "" if pd.isna(raw_text) else str(raw_text)
            if str(dashboard_row.get(field) or "") != expected_text:
                raise ValidationError(f"{ticker} dashboard differs for {field}")
        for field in (
            "Uses_Yahoo_Fallback",
            "Uses_Annual_Fallback",
            "Uses_Estimated_Maintenance_CapEx",
            "Uses_FX_Conversion",
            "Metric_Data_Complete",
        ):
            if _as_bool(dashboard_row.get(field), field) != _as_bool(row.get(field), field):
                raise ValidationError(f"{ticker} dashboard differs for {field}")
    dashboard_status_counts = payload.get("stats", {}).get("metric_status_counts", {})
    expected_status_counts = {
        status: sum(
            json.loads(str(raw or "{}")).get(status, 0)
            for raw in screen["Metric_Status_Counts_JSON"]
        )
        for status in METRIC_STATUSES
    }
    if dashboard_status_counts != expected_status_counts:
        raise ValidationError("dashboard aggregate metric-status counts differ from screen")
    status_total = sum(expected_status_counts.values())
    expected_status_ratios = {
        status: round(count / status_total, 4) if status_total else 0.0
        for status, count in expected_status_counts.items()
    }
    if payload.get("stats", {}).get("metric_status_ratios") != expected_status_ratios:
        raise ValidationError("dashboard metric-status proportions differ from screen")
    if payload.get("research_priority_version") != RESEARCH_PRIORITY_VERSION:
        raise ValidationError("dashboard research-priority model version is stale")
    if payload.get("score_disclaimer") != "模型內百分位，不代表跨模型未來報酬已校準":
        raise ValidationError("dashboard omits the cross-model calibration disclaimer")
    metadata = payload.get("metadata", {})
    required_metadata = {
        "decision_timestamp",
        "price_data_date",
        "latest_sec_availability_date",
        "universe_version",
        "model_version",
        "git_commit",
    }
    if not required_metadata.issubset(metadata) or metadata.get("model_version") != RESEARCH_PRIORITY_VERSION:
        raise ValidationError("dashboard run metadata is incomplete or stale")
    baseline = payload.get("trend_baseline", {})
    previous_policy = baseline.get("previous_policy_version")
    current_policy = payload.get("trend_policy_version")
    version_changed = bool(baseline.get("model_version_changed"))
    if not current_policy or RESEARCH_PRIORITY_VERSION not in str(current_policy):
        raise ValidationError("dashboard trend policy is not tied to the model version")
    if previous_policy and previous_policy != current_policy and not version_changed:
        raise ValidationError("dashboard model-version change did not reset the trend baseline")
    for ticker, dashboard_row in dashboard_by_ticker.items():
        core_kpis = dashboard_row.get("Core_KPI_Summary")
        if not isinstance(core_kpis, list) or len(core_kpis) != 3:
            raise ValidationError(f"{ticker} dashboard core-KPI summary is incomplete")
        model_key = str(dashboard_row.get("Industry_Model_Key") or "GENERAL_CORPORATE")
        labels = " | ".join(str(item.get("label") or "") for item in core_kpis)
        if model_key != "GENERAL_CORPORATE" and "Maintenance FCF" in labels:
            raise ValidationError(f"{ticker} specialized dashboard exposes general-company FCF KPI")
        if (
            model_key == "INSURANCE_P_AND_C"
            and str(dashboard_row.get("Combined_Ratio_Source_Status") or "").upper()
            == "SEC_PROXY_UNRECONCILED"
            and "proxy; unreconciled" not in labels
        ):
            raise ValidationError(f"{ticker} dashboard hides an unreconciled combined-ratio proxy")
    groups = payload.get("trend_groups", {})
    for key, group in groups.items():
        coverage = group.get("metric_coverage", {})
        for field, summary in coverage.items():
            ratio = _number(summary.get("coverage"))
            valid_count = int(summary.get("valid_count") or 0)
            used_count = int(summary.get("used_count") or 0)
            if summary.get("estimated_included") is not False or used_count != valid_count:
                raise ValidationError(
                    f"trend group {key} {field} includes non-VALID observations"
                )
            if math.isfinite(ratio) and ratio < 0.50 and group.get(field) is not None:
                raise ValidationError(f"trend group {key} exposes low-coverage {field}")
    for candidate in payload.get("emerging_candidates", []):
        ratio = _number(
            candidate.get("metric_coverage", {})
            .get("avg_shrunk_score", {})
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
    specialized_stress_status = (
        screen["Specialized_Stress_Status"].fillna("").astype(str).str.upper()
    )

    reason_codes = screen["Decision_Reason_Code"].fillna("").astype(str).str.strip()
    if reason_codes.eq("").any():
        bad = screen.loc[reason_codes.eq(""), "Ticker"].tolist()
        raise ValidationError(f"decision reason code is blank: {bad}")
    insufficient_codes = {
        "INSUFFICIENT_QUARTERLY_EVIDENCE",
        "MISSING_COMPANY_REPORTED_KPI",
        "STALE_CORE_FACTS",
        "UNRECONCILED_PROXY",
        "FX_CHAIN_UNAVAILABLE",
        "MODEL_NOT_APPLICABLE",
        "SPECIALIZED_STRESS_NOT_AVAILABLE",
        "MISSING_REQUIRED_EVIDENCE",
        "XBRL_MAPPING_UNAVAILABLE",
        "MISSING_MARKET_DATA",
        "ACQUISITION_ACCRETION_REVIEW",
    }
    invalid_missing_fail = (states == "FAIL") & reason_codes.isin(insufficient_codes)
    if invalid_missing_fail.any():
        bad = screen.loc[invalid_missing_fail, "Ticker"].tolist()
        raise ValidationError(f"insufficient evidence was mislabeled as FAIL: {bad}")
    invalid_abstain_reason = (states == "ABSTAIN") & reason_codes.isin(
        {"MODEL_PASS", "PASS_MODEL_GATE", "FINANCIAL_HARD_GATE"}
    )
    invalid_pass_reason = (states == "PASS") & reason_codes.isin(insufficient_codes)
    if invalid_abstain_reason.any() or invalid_pass_reason.any():
        bad = screen.loc[invalid_abstain_reason | invalid_pass_reason, "Ticker"].tolist()
        raise ValidationError(f"decision reason code contradicts decision state: {bad}")

    raw_scores = pd.to_numeric(screen["Raw_Model_Score"], errors="coerce")
    specialized_raw = pd.to_numeric(screen["Industry_Model_Score"], errors="coerce")
    expected_raw = scores.where(~specialized, specialized_raw)
    raw_mismatch = raw_scores.notna() & expected_raw.notna() & (
        (raw_scores - expected_raw).abs() > 0.011
    )
    if raw_mismatch.any():
        bad = screen.loc[raw_mismatch, "Ticker"].tolist()
        raise ValidationError(f"raw model score does not match its own model output: {bad}")
    calibration_status = (
        screen["Cross_Model_Calibration_Status"].fillna("").astype(str).str.upper()
    )
    if set(calibration_status) != {CROSS_MODEL_CALIBRATION_STATUS}:
        raise ValidationError("cross-model calibration must remain UNCALIBRATED before walk-forward validation")
    cross_model_comparable = _bool_series(
        screen["Cross_Model_Comparable"], "Cross_Model_Comparable"
    )
    if cross_model_comparable.any():
        bad = screen.loc[cross_model_comparable, "Ticker"].tolist()
        raise ValidationError(f"uncalibrated rows claim cross-model comparability: {bad}")
    if any(
        forbidden in screen.columns
        for forbidden in ("Cross_Model_Alpha_Score", "Expected_Return_Score")
    ):
        raise ValidationError("uncalibrated outputs expose a forbidden alpha/expected-return score")
    if set(screen["Research_Priority_Method"].fillna("").astype(str)) != {
        RESEARCH_PRIORITY_METHOD
    }:
        raise ValidationError("global research ranking method is not the coverage-first model round-robin")

    model_keys_for_rank = screen["Industry_Model_Key"].fillna("GENERAL_CORPORATE").astype(str).str.upper()
    valid_rank = raw_scores.notna() & (states != "ABSTAIN") & model_supported
    expected_peer_count = valid_rank.groupby(model_keys_for_rank).transform("sum").astype(int)
    actual_peer_count = pd.to_numeric(screen["Model_Peer_Count"], errors="coerce").fillna(0).astype(int)
    if (actual_peer_count[valid_rank] != expected_peer_count[valid_rank]).any():
        bad = screen.loc[valid_rank & (actual_peer_count != expected_peer_count), "Ticker"].tolist()
        raise ValidationError(f"within-model peer counts are inconsistent: {bad}")
    expected_percentile = pd.Series(float("nan"), index=screen.index, dtype=float)
    expected_percentile.loc[valid_rank] = (
        screen.loc[valid_rank]
        .assign(_raw=raw_scores[valid_rank].to_numpy())
        .groupby(model_keys_for_rank[valid_rank])["_raw"]
        .rank(method="average", pct=True)
        .mul(100.0)
    )
    actual_percentile = pd.to_numeric(screen["Within_Model_Percentile"], errors="coerce")
    percentile_mismatch = valid_rank & ((actual_percentile - expected_percentile).abs() > 0.011)
    if percentile_mismatch.any():
        bad = screen.loc[percentile_mismatch, "Ticker"].tolist()
        raise ValidationError(f"within-model percentiles use an invalid comparison set: {bad}")
    expected_shrunk = pd.Series(float("nan"), index=screen.index, dtype=float)
    for index in screen.index[valid_rank]:
        expected_shrunk.loc[index] = shrunk_percentile(
            expected_percentile.loc[index], int(expected_peer_count.loc[index])
        )
    actual_shrunk = pd.to_numeric(screen["Shrunk_Within_Model_Percentile"], errors="coerce")
    shrunk_mismatch = valid_rank & ((actual_shrunk - expected_shrunk).abs() > 0.011)
    if shrunk_mismatch.any():
        bad = screen.loc[shrunk_mismatch, "Ticker"].tolist()
        raise ValidationError(f"small-sample percentile shrinkage is inconsistent: {bad}")

    model_eligible = _bool_series(screen["Model_Eligible"], "Model_Eligible")
    if (model_eligible != eligible).any():
        bad = screen.loc[model_eligible != eligible, "Ticker"].tolist()
        raise ValidationError(f"MODEL_ELIGIBLE does not match the model hard gates: {bad}")
    queue_flags = _bool_series(screen["Global_Research_Queue"], "Global_Research_Queue")
    queue_candidates = screen.loc[model_eligible & actual_shrunk.notna()].copy()
    expected_order = research_priority_order(
        queue_candidates.to_dict(orient="records")
    )
    expected_queue = [
        str(row.get("Ticker") or "")
        for row in expected_order[:MAX_SHORTLIST_SIZE]
    ]
    actual_queue = screen.loc[queue_flags].sort_values("Research_Priority_Rank")["Ticker"].tolist()
    if actual_queue != expected_queue:
        raise ValidationError(f"global research queue is not the expected coverage-first model round-robin: expected={expected_queue}, actual={actual_queue}")
    action_states = screen["Research_Action_State"].fillna("").astype(str).str.upper()
    human_review = _bool_series(
        screen["Human_KPI_Review_Required"], "Human_KPI_Review_Required"
    )
    stress_pending = _bool_series(
        screen["Specialized_Stress_Pending"], "Specialized_Stress_Pending"
    )
    stress_failed = _bool_series(
        screen["Specialized_Stress_Failed"], "Specialized_Stress_Failed"
    )
    expected_action = pd.Series("SCREENED", index=screen.index, dtype=object)
    expected_action.loc[model_eligible] = "MODEL_ELIGIBLE"
    expected_action.loc[queue_flags] = "GLOBAL_RESEARCH_QUEUE"
    expected_action.loc[stress_pending] = "SPECIALIZED_STRESS_PENDING"
    expected_action.loc[stress_failed] = "SPECIALIZED_STRESS_FAILED"
    expected_action.loc[human_review] = "HUMAN_KPI_REVIEW_REQUIRED"
    invalid_action = action_states != expected_action
    if invalid_action.any():
        bad = screen.loc[invalid_action, "Ticker"].tolist()
        raise ValidationError(f"research action state is inconsistent with review gates: {bad}")
    portfolio_pending = _bool_series(
        screen["Portfolio_Fit_Pending"], "Portfolio_Fit_Pending"
    )
    portfolio_status = (
        screen["Portfolio_Fit_Status"].fillna("").astype(str).str.upper()
    )
    allowed_portfolio_statuses = {
        "NOT_APPLICABLE",
        "PENDING_INPUT",
        "PENDING_REVIEW",
        "PENDING_CALIBRATION",
        "STALE",
        "INVALID",
        "FAIL",
        "PASS",
    }
    if not set(portfolio_status).issubset(allowed_portfolio_statuses):
        raise ValidationError("portfolio-fit status contains an unsupported state")
    portfolio_contract = (
        screen["Portfolio_Fit_Contract_Version"].fillna("").astype(str)
    )
    if not portfolio_contract.eq(PORTFOLIO_FIT_CONTRACT_VERSION).all():
        raise ValidationError("portfolio-fit contract version is missing or inconsistent")
    expected_portfolio_pending = portfolio_status.isin(
        {"PENDING_INPUT", "PENDING_REVIEW", "PENDING_CALIBRATION", "STALE", "INVALID"}
    )
    if (portfolio_pending != expected_portfolio_pending).any():
        bad = screen.loc[portfolio_pending != expected_portfolio_pending, "Ticker"].tolist()
        raise ValidationError(f"portfolio-fit pending flag contradicts its status: {bad}")
    starter_flags = _bool_series(screen["Starter_Candidate"], "Starter_Candidate")
    invalid_starter = starter_flags & (
        human_review
        | stress_pending
        | stress_failed
        | portfolio_pending
        | portfolio_status.ne("PASS")
        | (specialized & ~cross_model_comparable)
    )
    if invalid_starter.any():
        bad = screen.loc[invalid_starter, "Ticker"].tolist()
        raise ValidationError(f"starter candidate bypassed research/portfolio gates: {bad}")
    portfolio_pass = portfolio_status.eq("PASS")
    portfolio_position = pd.to_numeric(
        screen["Portfolio_PostTrade_Position_Weight_pct_Total"], errors="coerce"
    )
    portfolio_sleeve = pd.to_numeric(
        screen["Portfolio_PostTrade_Active_Sleeve_Weight_pct_Total"], errors="coerce"
    )
    portfolio_sector = pd.to_numeric(
        screen["Portfolio_PostTrade_Sector_Weight_pct_Total"], errors="coerce"
    )
    portfolio_economic_risk = pd.to_numeric(
        screen["Portfolio_PostTrade_Economic_Risk_Weight_pct_Total"], errors="coerce"
    )
    portfolio_as_of = pd.to_datetime(
        screen["Portfolio_Fit_AsOf"], utc=True, errors="coerce"
    )
    portfolio_input_age = pd.to_numeric(
        screen["Portfolio_Fit_Input_Age_Days"], errors="coerce"
    )
    portfolio_current_position = pd.to_numeric(
        screen["Portfolio_Current_Position_Weight_pct_Total"], errors="coerce"
    )
    portfolio_pre_sleeve = pd.to_numeric(
        screen["Portfolio_PreTrade_Active_Sleeve_Weight_pct_Total"], errors="coerce"
    )
    portfolio_pre_sector = pd.to_numeric(
        screen["Portfolio_PreTrade_Sector_Weight_pct_Total"], errors="coerce"
    )
    portfolio_pre_economic_risk = pd.to_numeric(
        screen["Portfolio_PreTrade_Economic_Risk_Weight_pct_Total"], errors="coerce"
    )
    portfolio_etf_lookthrough = pd.to_numeric(
        screen["ETF_Lookthrough_Weight_pct_Total"], errors="coerce"
    )
    portfolio_etf_top10 = _bool_series(
        screen["Portfolio_ETF_Top10_Overlap"], "Portfolio_ETF_Top10_Overlap"
    )
    portfolio_correlation = (
        screen["Portfolio_Correlation_Stress_Status"]
        .fillna("")
        .astype(str)
        .str.upper()
    )
    proposed_starter_weight = pd.to_numeric(
        screen["Suggested_Starter_Weight_pct_Total"], errors="coerce"
    )
    decision_for_portfolio = pd.to_datetime(
        screen["Decision_Timestamp"], utc=True, errors="coerce"
    )
    calculated_portfolio_age = (
        decision_for_portfolio.dt.normalize() - portfolio_as_of.dt.normalize()
    ).dt.days
    invalid_portfolio_bridge = portfolio_pass & (
        portfolio_current_position.isna()
        | portfolio_pre_sleeve.isna()
        | portfolio_pre_sector.isna()
        | portfolio_pre_economic_risk.isna()
        | proposed_starter_weight.isna()
        | proposed_starter_weight.le(0.0)
        | (
            portfolio_position
            - (
                portfolio_current_position
                + portfolio_etf_lookthrough
                + proposed_starter_weight
            )
        ).abs().gt(0.011)
        | (
            portfolio_sleeve
            - (portfolio_pre_sleeve + proposed_starter_weight)
        ).abs().gt(0.011)
        | (
            portfolio_sector
            - (portfolio_pre_sector + proposed_starter_weight)
        ).abs().gt(0.011)
        | (
            portfolio_economic_risk
            - (portfolio_pre_economic_risk + proposed_starter_weight)
        ).abs().gt(0.011)
    )
    invalid_portfolio_pass = portfolio_pass & (
        ~model_eligible
        | raw_scores.isna()
        | raw_scores.lt(MIN_STARTER_SCORE)
        | (specialized & ~cross_model_comparable)
        | portfolio_input_age.isna()
        | portfolio_input_age.lt(0.0)
        | portfolio_input_age.gt(MAX_PORTFOLIO_INPUT_AGE_DAYS)
        | calculated_portfolio_age.isna()
        | portfolio_as_of.gt(decision_for_portfolio)
        | (portfolio_input_age - calculated_portfolio_age).abs().gt(0.011)
        | portfolio_current_position.lt(0.0)
        | portfolio_pre_sleeve.lt(0.0)
        | portfolio_pre_sector.lt(0.0)
        | portfolio_pre_economic_risk.lt(0.0)
        | portfolio_position.isna()
        | portfolio_position.lt(0.0)
        | portfolio_position.gt(3.0)
        | portfolio_sleeve.isna()
        | portfolio_sleeve.lt(0.0)
        | portfolio_sleeve.gt(30.0)
        | portfolio_sector.isna()
        | portfolio_sector.lt(0.0)
        | portfolio_sector.gt(9.0)
        | portfolio_economic_risk.isna()
        | portfolio_economic_risk.lt(0.0)
        | portfolio_economic_risk.gt(9.0)
        | portfolio_etf_lookthrough.isna()
        | portfolio_etf_lookthrough.lt(0.0)
        | (portfolio_etf_top10 & raw_scores.lt(ETF_TOP10_MIN_STARTER_SCORE))
        | portfolio_correlation.ne("PASS")
        | portfolio_as_of.isna()
        | screen["Economic_Risk_Bucket"].fillna("").astype(str).str.strip().eq("")
    )
    if (invalid_portfolio_pass | invalid_portfolio_bridge).any():
        bad = screen.loc[
            invalid_portfolio_pass | invalid_portfolio_bridge, "Ticker"
        ].tolist()
        raise ValidationError(f"passing portfolio fit violates exposure limits: {bad}")
    expected_starter = (
        model_eligible
        & raw_scores.ge(MIN_STARTER_SCORE)
        & ~human_review
        & ~stress_pending
        & ~stress_failed
        & portfolio_pass
        & (~specialized | cross_model_comparable)
        & proposed_starter_weight.gt(0.0)
    )
    if (starter_flags != expected_starter).any():
        bad = screen.loc[starter_flags != expected_starter, "Ticker"].tolist()
        raise ValidationError(f"starter candidate does not match all decision gates: {bad}")

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

    acquisition_status = (
        screen["Acquisition_Issuance_Attribution_Status"]
        .fillna("MISSING")
        .astype(str)
        .str.upper()
    )
    if not set(acquisition_status).issubset(
        {"MISSING", "DIRECT_XBRL_EVIDENCE", "DIRECT_XBRL_ZERO"}
    ):
        raise ValidationError("acquisition issuance attribution status is invalid")
    acquisition_reconciliation = (
        screen["Acquisition_Issuance_Reconciliation_Status"]
        .fillna("NOT_APPLICABLE")
        .astype(str)
        .str.upper()
    )
    if not set(acquisition_reconciliation).issubset(
        {
            "NOT_APPLICABLE",
            "DIRECT_ACQUISITION_EVIDENCE_ONLY",
            "RECONCILED_TO_TTM_STOCK_ISSUANCE",
        }
    ):
        raise ValidationError("acquisition issuance reconciliation status is invalid")
    acquisition_consideration = pd.to_numeric(
        screen["Acquisition_Stock_Consideration_B"], errors="coerce"
    )
    stock_issuance = pd.to_numeric(
        screen["TTM_Stock_Issuance_B"], errors="coerce"
    )
    gross_buyback = pd.to_numeric(
        screen["TTM_Gross_Buyback_B"], errors="coerce"
    )
    real_buyback = pd.to_numeric(screen["Real_Buyback_B"], errors="coerce")
    complete_buyback_bridge = gross_buyback.notna() & stock_issuance.notna()
    invalid_buyback_bridge = (~specialized) & (
        (
            complete_buyback_bridge
            & (
                real_buyback.isna()
                | (real_buyback - (gross_buyback - stock_issuance)).abs().gt(0.011)
            )
        )
        | (real_buyback.notna() & ~complete_buyback_bridge)
    )
    if invalid_buyback_bridge.any():
        bad = screen.loc[invalid_buyback_bridge, "Ticker"].tolist()
        raise ValidationError(f"net buyback does not reconcile to gross flows: {bad}")
    acquisition_flag = _bool_series(
        screen["Acquisition_Related_Issuance_Flag"],
        "Acquisition_Related_Issuance_Flag",
    )
    acquisition_review = _bool_series(
        screen["Acquisition_Accretion_Review_Required"],
        "Acquisition_Accretion_Review_Required",
    )
    direct_acquisition_evidence = (
        acquisition_status.eq("DIRECT_XBRL_EVIDENCE")
        & acquisition_consideration.gt(0.0)
    )
    expected_acquisition_flag = direct_acquisition_evidence
    expected_acquisition_reconciliation = pd.Series(
        "NOT_APPLICABLE", index=screen.index, dtype=object
    )
    expected_acquisition_reconciliation.loc[direct_acquisition_evidence] = (
        "DIRECT_ACQUISITION_EVIDENCE_ONLY"
    )
    expected_acquisition_reconciliation.loc[
        direct_acquisition_evidence & stock_issuance.gt(0.0)
    ] = "RECONCILED_TO_TTM_STOCK_ISSUANCE"
    invalid_acquisition_value = (
        (acquisition_status.eq("MISSING") & acquisition_consideration.notna())
        | (
            acquisition_status.eq("DIRECT_XBRL_EVIDENCE")
            & ~acquisition_consideration.gt(0.0)
        )
        | (
            acquisition_status.eq("DIRECT_XBRL_ZERO")
            & acquisition_consideration.ne(0.0)
        )
    )
    invalid_acquisition_attribution = (
        acquisition_flag != expected_acquisition_flag
    ) | (
        acquisition_reconciliation != expected_acquisition_reconciliation
    ) | (acquisition_review != (persistent & acquisition_flag))
    invalid_acquisition_decision = acquisition_review & (
        states.ne("ABSTAIN")
        | reason_codes.ne("ACQUISITION_ACCRETION_REVIEW")
    )
    invalid_acquisition = (
        invalid_acquisition_value
        | invalid_acquisition_attribution
        | invalid_acquisition_decision
    )
    if invalid_acquisition.any():
        bad = screen.loc[invalid_acquisition, "Ticker"].tolist()
        raise ValidationError(f"acquisition-related dilution does not reconcile: {bad}")

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
        .str.extract(r"^INDUSTRY_SPECIALIZED_(.+)_V\d+$", expand=False)
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
        | specialized_stress_status.ne("PASS")
    )
    if invalid_specialized.any():
        bad = screen.loc[invalid_specialized, "Ticker"].tolist()
        raise ValidationError(f"specialized eligible rows violate model gates: {bad}")
    specialized_survival = _bool_series(
        screen["Specialized_Stress_Survival"], "Specialized_Stress_Survival"
    )
    inconsistent_specialized_stress = specialized & (
        (specialized_stress_status.eq("PASS") & ~specialized_survival)
        | (specialized_stress_status.ne("PASS") & specialized_survival)
    )
    if inconsistent_specialized_stress.any():
        bad = screen.loc[inconsistent_specialized_stress, "Ticker"].tolist()
        raise ValidationError(f"specialized stress status/survival mismatch: {bad}")
    _validate_bank_stress_formulas(screen)

    dsi_status = screen["DSI_Status"].fillna("MISSING").astype(str).str.upper()
    if not set(dsi_status).issubset({"VALID", "MISSING", "NOT_APPLICABLE", "ABSTAIN", "STALE"}):
        raise ValidationError("DSI status contains an unsupported applicability state")
    dsi_score = pd.to_numeric(screen["DSI_Score"], errors="coerce")
    invalid_na_dsi = (dsi_status == "NOT_APPLICABLE") & dsi_score.notna()
    if invalid_na_dsi.any():
        bad = screen.loc[invalid_na_dsi, "Ticker"].tolist()
        raise ValidationError(f"NOT_APPLICABLE DSI rows expose a neutral/numeric score: {bad}")

    working_capital_status = (
        screen["Working_Capital_Quality_Status"]
        .fillna("MISSING")
        .astype(str)
        .str.upper()
    )
    working_capital_state = (
        screen["Working_Capital_Quality_State"]
        .fillna("MISSING")
        .astype(str)
        .str.upper()
    )
    if not set(working_capital_status).issubset(
        {"VALID", "MISSING", "ABSTAIN", "STALE"}
    ):
        raise ValidationError("working-capital status contains an unsupported state")
    if not set(working_capital_state).issubset(
        {"CLEAR", "WATCH", "HIGH_RISK", "MISSING", "STALE"}
    ):
        raise ValidationError("working-capital quality contains an unsupported state")
    ar_gap = pd.to_numeric(
        screen["AR_vs_Revenue_Growth_Gap_pp"], errors="coerce"
    )
    ap_gap = pd.to_numeric(
        screen["AP_vs_COGS_Growth_Gap_pp"], errors="coerce"
    )
    dso_days = pd.to_numeric(screen["DSO_Days"], errors="coerce")
    dpo_days = pd.to_numeric(screen["DPO_Days"], errors="coerce")
    working_capital_coverage = pd.to_numeric(
        screen["Working_Capital_Quality_Coverage"], errors="coerce"
    )
    expected_working_capital_coverage = (
        (ar_gap.notna() & dso_days.notna()).astype(float)
        + (ap_gap.notna() & dpo_days.notna()).astype(float)
    ) / 2.0
    valid_working_capital = working_capital_status.eq("VALID")
    general_working_capital = ~specialized
    invalid_working_capital_coverage = general_working_capital & (
        working_capital_coverage.isna()
        | working_capital_coverage.lt(0.0)
        | working_capital_coverage.gt(1.0)
        | (
            valid_working_capital
            & (working_capital_coverage - expected_working_capital_coverage)
            .abs()
            .gt(0.011)
        )
    )
    invalid_working_capital_missing_state = (
        working_capital_status.isin({"MISSING", "ABSTAIN"})
        & working_capital_state.ne("MISSING")
    ) | (
        working_capital_status.eq("STALE")
        & working_capital_state.ne("STALE")
    )
    ar_adverse = ar_gap.ge(15.0) & dso_days.ge(
        WORKING_CAPITAL_MIN_MATERIAL_DAYS
    )
    ap_adverse = ap_gap.ge(20.0) & dpo_days.ge(
        WORKING_CAPITAL_MIN_MATERIAL_DAYS
    )
    expected_working_capital_state = pd.Series(
        "CLEAR", index=screen.index, dtype=object
    )
    expected_working_capital_state.loc[ar_adverse | ap_adverse] = "WATCH"
    expected_working_capital_state.loc[ar_adverse & ap_adverse] = "HIGH_RISK"
    invalid_working_capital_state = valid_working_capital & (
        working_capital_state != expected_working_capital_state
    )
    expected_working_capital_penalty = working_capital_state.map(
        {"HIGH_RISK": 10.0, "WATCH": 5.0}
    ).fillna(0.0)
    working_capital_penalty = pd.to_numeric(
        screen["Working_Capital_Risk_Penalty"], errors="coerce"
    )
    invalid_working_capital_penalty = general_working_capital & (
        working_capital_penalty.isna()
        | (working_capital_penalty - expected_working_capital_penalty)
        .abs()
        .gt(0.011)
    )
    invalid_working_capital = (
        invalid_working_capital_coverage
        | invalid_working_capital_missing_state
        | invalid_working_capital_state
        | invalid_working_capital_penalty
    )
    if invalid_working_capital.any():
        bad = screen.loc[invalid_working_capital, "Ticker"].tolist()
        raise ValidationError(f"working-capital diagnostic does not reconcile: {bad}")

    applicable_weight = pd.to_numeric(screen["Applicable_Factor_Weight"], errors="coerce")
    available_weight = pd.to_numeric(screen["Available_Factor_Weight"], errors="coerce")
    factor_coverage = pd.to_numeric(screen["Factor_Coverage"], errors="coerce")
    weight_renormalized = _bool_series(
        screen["Weight_Renormalized"], "Weight_Renormalized"
    )
    general_scored = ~specialized & states.eq("PASS") & scores.notna()
    invalid_factor_weights = general_scored & (
        applicable_weight.isna()
        | available_weight.isna()
        | factor_coverage.isna()
        | applicable_weight.le(0)
        | available_weight.gt(applicable_weight)
        | ((factor_coverage - available_weight / applicable_weight).abs() > 0.011)
        | (weight_renormalized != ~available_weight.round(8).eq(100.0))
    )
    if invalid_factor_weights.any():
        bad = screen.loc[invalid_factor_weights, "Ticker"].tolist()
        raise ValidationError(f"factor applicability weights were not renormalized correctly: {bad}")

    dilution_check = screen["Dilution_Double_Count_Check"].fillna("").astype(str).str.upper()
    if not dilution_check.eq("PASS").all():
        bad = screen.loc[~dilution_check.eq("PASS"), "Ticker"].tolist()
        raise ValidationError(f"dilution reason was counted in more than one score layer: {bad}")
    ownership_penalty = pd.to_numeric(
        screen.get("Ownership_Dilution_Penalty"), errors="coerce"
    ).fillna(0.0)
    capital_penalty = pd.to_numeric(
        screen.get("Capital_Allocation_Penalty"), errors="coerce"
    ).fillna(0.0)
    total_dilution_impact = pd.to_numeric(
        screen["Dilution_Total_Score_Impact"], errors="coerce"
    ).fillna(0.0)
    duplicate_dilution = ownership_penalty.gt(0) & capital_penalty.gt(0)
    capital_effective_weight = 5.0 / available_weight.where(available_weight.gt(0))
    invalid_dilution_total = (
        total_dilution_impact
        - (ownership_penalty + capital_penalty * capital_effective_weight.fillna(0.0))
    ).abs() > 0.011
    if duplicate_dilution.any() or invalid_dilution_total.any():
        bad = screen.loc[duplicate_dilution | invalid_dilution_total, "Ticker"].tolist()
        raise ValidationError(f"dilution attribution is duplicated or does not reconcile: {bad}")
    persistent_hard_gate = _bool_series(
        screen["Persistent_Dilution_Hard_Gate"], "Persistent_Dilution_Hard_Gate"
    )
    if (persistent_hard_gate != persistent).any():
        bad = screen.loc[persistent_hard_gate != persistent, "Ticker"].tolist()
        raise ValidationError(f"persistent dilution hard-gate flag is inconsistent: {bad}")
    if (persistent & (ownership_penalty.gt(0) | capital_penalty.gt(0))).any():
        bad = screen.loc[
            persistent & (ownership_penalty.gt(0) | capital_penalty.gt(0)), "Ticker"
        ].tolist()
        raise ValidationError(f"persistent dilution hard gate was also score-penalized: {bad}")

    p_and_c = specialized_keys == "INSURANCE_P_AND_C"
    if p_and_c.any():
        source_status = screen.get(
            "Combined_Ratio_Source_Status", pd.Series("MISSING", index=screen.index)
        ).fillna("MISSING").astype(str).str.upper()
        if not set(source_status[p_and_c]).issubset(
            {"COMPANY_REPORTED", "SEC_PROXY_RECONCILED", "SEC_PROXY_UNRECONCILED", "MISSING"}
        ):
            raise ValidationError("P&C combined-ratio source status is invalid")
        human_review = _bool_series(
            screen["Human_KPI_Review_Required"], "Human_KPI_Review_Required"
        )
        starter = _bool_series(screen["Starter_Candidate"], "Starter_Candidate")
        unreconciled = p_and_c & source_status.eq("SEC_PROXY_UNRECONCILED")
        if (unreconciled & (~human_review | starter)).any():
            bad = screen.loc[unreconciled & (~human_review | starter), "Ticker"].tolist()
            raise ValidationError(f"unreconciled P&C proxy bypassed human review/starter gate: {bad}")
        for _, row in screen.loc[unreconciled].iterrows():
            metadata = _parse_metadata(row["Metric_Metadata_JSON"], str(row["Ticker"]))
            proxy_meta = metadata.get("SEC_Combined_Ratio_Proxy", {})
            if proxy_meta.get("status") != "ESTIMATED":
                raise ValidationError(
                    f"{row['Ticker']} unreconciled P&C proxy is not ESTIMATED"
                )
        stress_status = screen.get(
            "P_and_C_Stress_Status", pd.Series("ABSTAIN", index=screen.index)
        ).fillna("ABSTAIN").astype(str).str.upper()
        invalid_starter_stress = p_and_c & stress_status.ne("PASS") & starter
        if invalid_starter_stress.any():
            bad = screen.loc[invalid_starter_stress, "Ticker"].tolist()
            raise ValidationError(f"P&C starter candidates lack a passing moderate stress: {bad}")

    if {
        "Historical_Valuation_Valid_Years",
        "Historical_Valuation_Quantile_Used",
    }.issubset(screen.columns):
        valid_years = pd.to_numeric(
            screen["Historical_Valuation_Valid_Years"], errors="coerce"
        )
        quantile_used = pd.to_numeric(
            screen["Historical_Valuation_Quantile_Used"], errors="coerce"
        )
        expected_quantile = valid_years.map(
            lambda years: (
                float("nan")
                if pd.isna(years) or years < 5
                else 25.0
                if years <= 6
                else 20.0
                if years <= 9
                else 15.0
            )
        )
        invalid_history_window = valid_years.gt(10.0)
        if invalid_history_window.any():
            bad = screen.loc[invalid_history_window, "Ticker"].tolist()
            raise ValidationError(
                f"historical valuation exceeds the configured ten-year window: {bad}"
            )
        comparable_quantile = expected_quantile.notna() & quantile_used.notna()
        invalid_quantile = comparable_quantile & (
            (expected_quantile - quantile_used).abs() > 0.011
        )
        if invalid_quantile.any():
            bad = screen.loc[invalid_quantile, "Ticker"].tolist()
            raise ValidationError(f"historical valuation quantile does not match sample size: {bad}")

    if "Historical_Valuation_Status" in screen.columns:
        historical_status = (
            screen["Historical_Valuation_Status"].fillna("").astype(str).str.upper()
        )
        invalid_history = eligible & historical_status.ne("") & historical_status.ne("VALID")
        if invalid_history.any():
            bad = screen.loc[invalid_history, "Ticker"].tolist()
            raise ValidationError(f"eligible rows have low point-in-time valuation coverage: {bad}")

    contract_versions = set(
        screen["Metric_Contract_Version"].fillna("").astype(str).str.strip()
    )
    if contract_versions != {METRIC_CONTRACT_VERSION}:
        raise ValidationError(
            f"metric contract version mismatch: {sorted(contract_versions)}"
        )
    _validate_metric_contract(screen)
    _validate_financial_formulas(screen)
    zero_report = build_zero_classification_report(screen)
    if zero_report["summary"]["invalid_zero"]:
        raise ValidationError(
            f"invalid zero values remain: {zero_report['summary']['invalid_zero']}"
        )
    required_missing = screen["Required_Missing_Metrics"].fillna("").astype(str).str.strip()
    if (eligible & required_missing.ne("")).any():
        bad = screen.loc[eligible & required_missing.ne(""), "Ticker"].tolist()
        raise ValidationError(f"eligible rows have required metric gaps: {bad}")

    shortlist = pd.read_csv(shortlist_path, encoding="utf-8-sig")
    _require_columns(shortlist.columns, {"Ticker"}, "shortlist")
    actual_shortlist = shortlist["Ticker"].fillna("").astype(str).str.upper().tolist()
    expected_shortlist = actual_queue
    if actual_shortlist != expected_shortlist:
        raise ValidationError(
            f"shortlist ranking mismatch: expected={expected_shortlist}, actual={actual_shortlist}"
        )

    return screen, {
        "screen_rows": int(len(screen)),
        "eligible_rows": int(eligible.sum()),
        "shortlist_rows": int(len(shortlist)),
        "decision_counts": states.value_counts().sort_index().to_dict(),
        "zero_classification": zero_report["summary"],
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
    parser.add_argument(
        "--zero-report",
        default="mode_c_zero_audit.json",
        help="Write the classified important-metric zero audit (empty disables it)",
    )
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
        if args.zero_report:
            screen = pd.read_csv(args.screen, encoding="utf-8-sig")
            report = build_zero_classification_report(screen)
            Path(args.zero_report).write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            summary["zero_report"] = str(args.zero_report)
    except ValidationError as exc:
        print(f"Mode C output validation failed: {exc}")
        return 1
    print(json.dumps({"status": "ok", **summary}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
