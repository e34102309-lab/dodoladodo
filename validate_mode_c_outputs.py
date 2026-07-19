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
    METRIC_CONTRACT_VERSION,
    METRIC_STATUSES,
    MODEL_APPLICABLE_METRICS,
    NULL_STATUSES,
)
from mode_c_research_priority import (
    CROSS_MODEL_CALIBRATION_STATUS,
    RESEARCH_PRIORITY_METHOD,
    RESEARCH_PRIORITY_VERSION,
    shrunk_percentile,
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
    "Research_Priority_Method",
    "Research_Priority_Version",
    "Model_Eligible",
    "Global_Research_Queue",
    "Human_KPI_Review_Required",
    "Specialized_Stress_Pending",
    "Portfolio_Fit_Pending",
    "Starter_Candidate",
    "Research_Action_State",
    "Research_Statuses",
    "DSI_Status",
    "DSI_Score",
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
    "Share_Count_Change_pct",
    "Share_Count_Change_3Y_pct",
    "Share_Basis_Discontinuity",
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
        raise Validatóü¶‰žËkºwµç}ÉÌô‰½•É”ˆ¤(€€€™…Ñ½É}½Ù•É…”€ôÁ¹Ñ½}¹Õµ•É¥Œ¡ÍÉ••¹l‰…Ñ½É}½Ù•É…”‰t°•ÉÉ½ÉÌô‰½•É”ˆ¤(€€€Ý•¥¡Ñ}É•¹½Éµ…±¥é•€ô}‰½½±}Í•É¥•Ì (€€€€€€€ÍÉ••¹l‰]•¥¡Ñ}I•¹½Éµ…±¥é•‰t°€‰]•¥¡Ñ}I•¹½Éµ…±¥é•ˆ(€€€€¤(€€€•¹•É…±}Í½É•€ôùÍÁ•¥…±¥é•€˜ÍÑ…Ñ•Ì¹•Ä ‰AMLˆ¤€˜Í½É•Ì¹¹½Ñ¹„ ¤(€€€¥¹Ù…±¥‘}™…Ñ½É}Ý•¥¡ÑÌ€ô•¹•É…±}Í½É•€˜€ (€€€€€€€…ÁÁ±¥…‰±•}Ý•¥¡Ð¹¥Í¹„ ¤(€€€€€€€ð…Ù…¥±…‰±•}Ý•¥¡Ð¹¥Í¹„ ¤(€€€€€€€ð™…Ñ½É}½Ù•É…”¹¥Í¹„ ¤(€€€€€€€ð…ÁÁ±¥…‰±•}Ý•¥¡Ð¹±” À¤(€€€€€€€ð…Ù…¥±…‰±•}Ý•¥¡Ð¹Ð¡…ÁÁ±¥…‰±•}Ý•¥¡Ð¤(€€€€€€€ð€ ¡™…Ñ½É}½Ù•É…”€´…Ù…¥±…‰±•}Ý•¥¡Ð€¼…ÁÁ±¥…‰±•}Ý•¥¡Ð¤¹…‰Ì ¤€ø€À¸ÀÄÄ¤(€€€€€€€ð€¡Ý•¥¡Ñ}É•¹½Éµ…±¥é•€„ôù…Ù…¥±…‰±•}Ý•¥¡Ð¹É½Õ¹ à¤¹•Ä ÄÀÀ¸À¤¤(€€€€¤(€€€¥˜¥¹Ù…±¥‘}™…Ñ½É}Ý•¥¡ÑÌ¹…¹ä ¤è(€€€€€€€‰…€ôÍÉ••¸¹±½m¥¹Ù…±¥‘}™…Ñ½É}Ý•¥¡ÑÌ°€‰Q¥­•È‰t¹Ñ½±¥ÍÐ ¤(€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È¡˜‰™…Ñ½È…ÁÁ±¥…‰¥±¥ÑäÝ•¥¡ÑÌÝ•É”¹½ÐÉ•¹½Éµ…±¥é•½ÉÉ•Ñ±äèí‰…‘ôˆ¤((€€€‘¥±ÕÑ¥½¹}¡•¬€ôÍÉ••¹l‰¥±ÕÑ¥½¹}½Õ‰±•}½Õ¹Ñ}¡•¬‰t¹™¥±±¹„ ˆˆ¤¹…ÍÑåÁ”¡ÍÑÈ¤¹ÍÑÈ¹ÕÁÁ•È ¤(€€€¥˜¹½Ð‘¥±ÕÑ¥½¹}¡•¬¹•Ä ‰AMLˆ¤¹…±° ¤è(€€€€€€€‰…€ôÍÉ••¸¹±½mù‘¥±ÕÑ¥½¹}¡•¬¹•Ä ‰AMLˆ¤°€‰Q¥­•È‰t¹Ñ½±¥ÍÐ ¤(€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È¡˜‰‘¥±ÕÑ¥½¸É•…Í½¸Ý…Ì½Õ¹Ñ•¥¸µ½É”Ñ¡…¸½¹”Í½É”±…å•Èèí‰…‘ôˆ¤(€€€½Ý¹•ÉÍ¡¥Á}Á•¹…±Ñä€ôÁ¹Ñ½}¹Õµ•É¥Œ (€€€€€€€ÍÉ••¸¹•Ð ‰=Ý¹•ÉÍ¡¥Á}¥±ÕÑ¥½¹}A•¹…±Ñäˆ¤°•ÉÉ½ÉÌô‰½•É”ˆ(€€€€¤¹™¥±±¹„ À¸À¤(€€€…Á¥Ñ…±}Á•¹…±Ñä€ôÁ¹Ñ½}¹Õµ•É¥Œ (€€€€€€€ÍÉ••¸¹•Ð ‰…Á¥Ñ…±}±±½…Ñ¥½¹}A•¹…±Ñäˆ¤°•ÉÉ½ÉÌô‰½•É”ˆ(€€€€¤¹™¥±±¹„ À¸À¤(€€€Ñ½Ñ…±}‘¥±ÕÑ¥½¹}¥µÁ…Ð€ôÁ¹Ñ½}¹Õµ•É¥Œ (€€€€€€€ÍÉ••¹l‰¥±ÕÑ¥½¹}Q½Ñ…±}M½É•}%µÁ…Ð‰t°•ÉÉ½ÉÌô‰½•É”ˆ(€€€€¤¹™¥±±¹„ À¸À¤(€€€‘ÕÁ±¥…Ñ•}‘¥±ÕÑ¥½¸€ô½Ý¹•ÉÍ¡¥Á}Á•¹…±Ñä¹Ð À¤€˜…Á¥Ñ…±}Á•¹…±Ñä¹Ð À¤(€€€¥¹Ù…±¥‘}‘¥±ÕÑ¥½¹}Ñ½Ñ…°€ô€ (€€€€€€€Ñ½Ñ…±}‘¥±ÕÑ¥½¹}¥µÁ…Ð€´€¡½Ý¹•ÉÍ¡¥Á}Á•¹…±Ñä€¬…Á¥Ñ…±}Á•¹…±Ñä€¨€À¸ÀÔ¤(€€€€¤¹…‰Ì ¤€ø€À¸ÀÄÄ(€€€¥˜‘ÕÁ±¥…Ñ•}‘¥±ÕÑ¥½¸¹…¹ä ¤½È¥¹Ù…±¥‘}‘¥±ÕÑ¥½¹}Ñ½Ñ…°¹…¹ä ¤è(€€€€€€€‰…€ôÍÉ••¸¹±½m‘ÕÁ±¥…Ñ•}‘¥±ÕÑ¥½¸ð¥¹Ù…±¥‘}‘¥±ÕÑ¥½¹}Ñ½Ñ…°°€‰Q¥­•È‰t¹Ñ½±¥ÍÐ ¤(€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È¡˜‰‘¥±ÕÑ¥½¸…ÑÑÉ¥‰ÕÑ¥½¸¥Ì‘ÕÁ±¥…Ñ•½È‘½•Ì¹½ÐÉ•½¹¥±”èí‰…‘ôˆ¤(€€€Á•ÉÍ¥ÍÑ•¹Ñ}¡…É‘}…Ñ”€ô}‰½½±}Í•É¥•Ì (€€€€€€€ÍÉ••¹l‰A•ÉÍ¥ÍÑ•¹Ñ}¥±ÕÑ¥½¹}!…É‘}…Ñ”‰t°€‰A•ÉÍ¥ÍÑ•¹Ñ}¥±ÕÑ¥½¹}!…É‘}…Ñ”ˆ(€€€€¤(€€€¥˜€¡Á•ÉÍ¥ÍÑ•¹Ñ}¡…É‘}…Ñ”€„ôÁ•ÉÍ¥ÍÑ•¹Ð¤¹…¹ä ¤è(€€€€€€€‰…€ôÍÉ••¸¹±½mÁ•ÉÍ¥ÍÑ•¹Ñ}¡…É‘}…Ñ”€„ôÁ•ÉÍ¥ÍÑ•¹Ð°€‰Q¥­•È‰t¹Ñ½±¥ÍÐ ¤(€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È¡˜‰Á•ÉÍ¥ÍÑ•¹Ð‘¥±ÕÑ¥½¸¡…Éµ…Ñ”™±…œ¥Ì¥¹½¹Í¥ÍÑ•¹Ðèí‰…‘ôˆ¤(€€€¥˜€¡Á•ÉÍ¥ÍÑ•¹Ð€˜€¡½Ý¹•ÉÍ¡¥Á}Á•¹…±Ñä¹Ð À¤ð…Á¥Ñ…±}Á•¹…±Ñä¹Ð À¤¤¤¹…¹ä ¤è(€€€€€€€‰…€ôÍÉ••¸¹±½l(€€€€€€€€€€€Á•ÉÍ¥ÍÑ•¹Ð€˜€¡½Ý¹•ÉÍ¡¥Á}Á•¹…±Ñä¹Ð À¤ð…Á¥Ñ…±}Á•¹…±Ñä¹Ð À¤¤°€‰Q¥­•Èˆ(€€€€€€€t¹Ñ½±¥ÍÐ ¤(€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È¡˜‰Á•ÉÍ¥ÍÑ•¹Ð‘¥±ÕÑ¥½¸¡…É…Ñ”Ý…Ì…±Í¼Í½É”µÁ•¹…±¥é•èí‰…‘ôˆ¤((€€€Á}…¹‘}Œ€ôÍÁ•¥…±¥é•‘}­•åÌ€ôô€‰%9MUI9}A}9}ˆ(€€€¥˜Á}…¹‘}Œ¹…¹ä ¤è(€€€€€€€Í½ÕÉ•}ÍÑ…ÑÕÌ€ôÍÉ••¸¹•Ð (€€€€€€€€€€€€‰½µ‰¥¹•‘}I…Ñ¥½}M½ÕÉ•}MÑ…ÑÕÌˆ°Á¹M•É¥•Ì ‰5%MM%9ˆ°¥¹‘•àõÍÉ••¸¹¥¹‘•à¤(€€€€€€€€¤¹™¥±±¹„ ‰5%MM%9ˆ¤¹…ÍÑåÁ”¡ÍÑÈ¤¹ÍÑÈ¹ÕÁÁ•È ¤(€€€€€€€¥˜¹½ÐÍ•Ð¡Í½ÕÉ•}ÍÑ…ÑÕÍmÁ}…¹‘}t¤¹¥ÍÍÕ‰Í•Ð (€€€€€€€€€€€ì‰=5A9e}IA=IQˆ°€‰M}AI=ae}I=9%1ˆ°€‰M}AI=ae}U9I=9%1ˆ°€‰5%MM%9‰ô(€€€€€€€€¤è(€€€€€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È ‰@™½µ‰¥¹•µÉ…Ñ¥¼Í½ÕÉ”ÍÑ…ÑÕÌ¥Ì¥¹Ù…±¥ˆ¤(€€€€€€€¡Õµ…¹}É•Ù¥•Ü€ô}‰½½±}Í•É¥•Ì (€€€€€€€€€€€ÍÉ••¹l‰!Õµ…¹}-A%}I•Ù¥•Ý}I•ÅÕ¥É•‰t°€‰!Õµ…¹}-A%}I•Ù¥•Ý}I•ÅÕ¥É•ˆ(€€€€€€€€¤(€€€€€€€ÍÑ…ÉÑ•È€ô}‰½½±}Í•É¥•Ì¡ÍÉ••¹l‰MÑ…ÉÑ•É}…¹‘¥‘…Ñ”‰t°€‰MÑ…ÉÑ•É}…¹‘¥‘…Ñ”ˆ¤(€€€€€€€Õ¹É•½¹¥±•€ôÁ}…¹‘}Œ€˜Í½ÕÉ•}ÍÑ…ÑÕÌ¹•Ä ‰M}AI=ae}U9I=9%1ˆ¤(€€€€€€€¥˜€¡Õ¹É•½¹¥±•€˜€¡ù¡Õµ…¹}É•Ù¥•ÜðÍÑ…ÉÑ•È¤¤¹…¹ä ¤è(€€€€€€€€€€€‰…€ôÍÉ••¸¹±½mÕ¹É•½¹¥±•€˜€¡ù¡Õµ…¹}É•Ù¥•ÜðÍÑ…ÉÑ•È¤°€‰Q¥­•È‰t¹Ñ½±¥ÍÐ ¤(€€€€€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È¡˜‰Õ¹É•½¹¥±•@™ÁÉ½áä‰åÁ…ÍÍ•¡Õµ…¸É•Ù¥•Ü½ÍÑ…ÉÑ•È…Ñ”èí‰…‘ôˆ¤(€€€€€€€™½È|°É½Ü¥¸ÍÉ••¸¹±½mÕ¹É•½¹¥±•‘t¹¥Ñ•ÉÉ½ÝÌ ¤è(€€€€€€€€€€€µ•Ñ…‘…Ñ„€ô}Á…ÉÍ•}µ•Ñ…‘…Ñ„¡É½Ýl‰5•ÑÉ¥}5•Ñ…‘…Ñ…})M=8‰t°ÍÑÈ¡É½Ýl‰Q¥­•È‰t¤¤(€€€€€€€€€€€ÁÉ½áå}µ•Ñ„€ôµ•Ñ…‘…Ñ„¹•Ð ‰M}½µ‰¥¹•‘}I…Ñ¥½}AÉ½áäˆ°íô¤(€€€€€€€€€€€¥˜ÁÉ½áå}µ•Ñ„¹•Ð ‰ÍÑ…ÑÕÌˆ¤€„ô€‰MQ%5Qˆè(€€€€€€€€€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È (€€€€€€€€€€€€€€€€€€€˜‰íÉ½ÝlQ¥­•ÈuôÕ¹É•½¹¥±•@™ÁÉ½áä¥Ì¹½ÐMQ%5Qˆ(€€€€€€€€€€€€€€€€¤(€€€€€€€ÍÑÉ•ÍÍ}ÍÑ…ÑÕÌ€ôÍÉ••¸¹•Ð (€€€€€€€€€€€€‰A}…¹‘}}MÑÉ•ÍÍ}MÑ…ÑÕÌˆ°Á¹M•É¥•Ì ‰	MQ%8ˆ°¥¹‘•àõÍÉ••¸¹¥¹‘•à¤(€€€€€€€€¤¹™¥±±¹„ ‰	MQ%8ˆ¤¹…ÍÑåÁ”¡ÍÑÈ¤¹ÍÑÈ¹ÕÁÁ•È ¤(€€€€€€€¥¹Ù…±¥‘}ÍÑ…ÉÑ•É}ÍÑÉ•ÍÌ€ôÁ}…¹‘}Œ€˜ÍÑÉ•ÍÍ}ÍÑ…ÑÕÌ¹¹” ‰AMLˆ¤€˜ÍÑ…ÉÑ•È(€€€€€€€¥˜¥¹Ù…±¥‘}ÍÑ…ÉÑ•É}ÍÑÉ•ÍÌ¹…¹ä ¤è(€€€€€€€€€€€‰…€ôÍÉ••¸¹±½m¥¹Ù…±¥‘}ÍÑ…ÉÑ•É}ÍÑÉ•ÍÌ°€‰Q¥­•È‰t¹Ñ½±¥ÍÐ ¤(€€€€€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È¡˜‰@™ÍÑ…ÉÑ•È…¹‘¥‘…Ñ•Ì±…¬„Á…ÍÍ¥¹œµ½‘•É…Ñ”ÍÑÉ•ÍÌèí‰…‘ôˆ¤((€€€¥˜ì(€€€€€€€€‰!¥ÍÑ½É¥…±}Y…±Õ…Ñ¥½¹}Y…±¥‘}e•…ÉÌˆ°(€€€€€€€€‰!¥ÍÑ½É¥…±}Y…±Õ…Ñ¥½¹}EÕ…¹Ñ¥±•}UÍ•ˆ°(€€€ô¹¥ÍÍÕ‰Í•Ð¡ÍÉ••¸¹½±Õµ¹Ì¤è(€€€€€€€Ù…±¥‘}å•…ÉÌ€ôÁ¹Ñ½}¹Õµ•É¥Œ (€€€€€€€€€€€ÍÉ••¹l‰!¥ÍÑ½É¥…±}Y…±Õ…Ñ¥½¹}Y…±¥‘}e•…ÉÌ‰t°•ÉÉ½ÉÌô‰½•É”ˆ(€€€€€€€€¤(€€€€€€€ÅÕ…¹Ñ¥±•}ÕÍ•€ôÁ¹Ñ½}¹Õµ•É¥Œ (€€€€€€€€€€€ÍÉ••¹l‰!¥ÍÑ½É¥…±}Y…±Õ…Ñ¥½¹}EÕ…¹Ñ¥±•}UÍ•‰t°•ÉÉ½ÉÌô‰½•É”ˆ(€€€€€€€€¤(€€€€€€€•áÁ•Ñ•‘}ÅÕ…¹Ñ¥±”€ôÙ…±¥‘}å•…ÉÌ¹µ…À (€€€€€€€€€€€±…µ‰‘„å•…ÉÌè€ (€€€€€€€€€€€€€€€™±½…Ð ‰¹…¸ˆ¤(€€€€€€€€€€€€€€€¥˜Á¹¥Í¹„¡å•…ÉÌ¤½Èå•…ÉÌ€ð€Ô(€€€€€€€€€€€€€€€•±Í”€ÈÔ¸À(€€€€€€€€€€€€€€€¥˜å•…ÉÌ€ðô€Ø(€€€€€€€€€€€€€€€•±Í”€ÈÀ¸À(€€€€€€€€€€€€€€€¥˜å•…ÉÌ€ðô€ä(€€€€€€€€€€€€€€€•±Í”€ÄÔ¸À(€€€€€€€€€€€€€€€¥˜å•…ÉÌ€ðô€ÄÐ(€€€€€€€€€€€€€€€•±Í”€ÄÀ¸À(€€€€€€€€€€€€¤(€€€€€€€€¤(€€€€€€€½µÁ…É…‰±•}ÅÕ…¹Ñ¥±”€ô•áÁ•Ñ•‘}ÅÕ…¹Ñ¥±”¹¹½Ñ¹„ ¤€˜ÅÕ…¹Ñ¥±•}ÕÍ•¹¹½Ñ¹„ ¤(€€€€€€€¥¹Ù…±¥‘}ÅÕ…¹Ñ¥±”€ô½µÁ…É…‰±•}ÅÕ…¹Ñ¥±”€˜€ (€€€€€€€€€€€€¡•áÁ•Ñ•‘}ÅÕ…¹Ñ¥±”€´ÅÕ…¹Ñ¥±•}ÕÍ•¤¹…‰Ì ¤€ø€À¸ÀÄÄ(€€€€€€€€¤(€€€€€€€¥˜¥¹Ù…±¥‘}ÅÕ…¹Ñ¥±”¹…¹ä ¤è(€€€€€€€€€€€‰…€ôÍÉ••¸¹±½m¥¹Ù…±¥‘}ÅÕ…¹Ñ¥±”°€‰Q¥­•È‰t¹Ñ½±¥ÍÐ ¤(€€€€€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È¡˜‰¡¥ÍÑ½É¥…°Ù…±Õ…Ñ¥½¸ÅÕ…¹Ñ¥±”‘½•Ì¹½Ðµ…Ñ Í…µÁ±”Í¥é”èí‰…‘ôˆ¤(4(€€€¥˜€‰!¥ÍÑ½É¥…±}Y…±Õ…Ñ¥½¹}MÑ…ÑÕÌˆ¥¸ÍÉ••¸¹½±Õµ¹Ìè4(€€€€€€€¡¥ÍÑ½É¥…±}ÍÑ…ÑÕÌ€ô€ 4(€€€€€€€€€€€ÍÉ••¹l‰!¥ÍÑ½É¥…±}Y…±Õ…Ñ¥½¹}MÑ…ÑÕÌ‰t¹™¥±±¹„ ˆˆ¤¹…ÍÑåÁ”¡ÍÑÈ¤¹ÍÑÈ¹ÕÁÁ•È ¤4(€€€€€€€€¤4(€€€€€€€¥¹Ù…±¥‘}¡¥ÍÑ½Éä€ô•±¥¥‰±”€˜¡¥ÍÑ½É¥…±}ÍÑ…ÑÕÌ¹¹” ˆˆ¤€˜¡¥ÍÑ½É¥…±}ÍÑ…ÑÕÌ¹¹” ‰Y1%ˆ¤4(€€€€€€€¥˜¥¹Ù…±¥‘}¡¥ÍÑ½Éä¹…¹ä ¤è4(€€€€€€€€€€€‰…€ôÍÉ••¸¹±½m¥¹Ù…±¥‘}¡¥ÍÑ½Éä°€‰Q¥­•È‰t¹Ñ½±¥ÍÐ ¤4(€€€€€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È¡˜‰•±¥¥‰±”É½ÝÌ¡…Ù”±½ÜÁ½¥¹Ðµ¥¸µÑ¥µ”Ù…±Õ…Ñ¥½¸½Ù•É…”èí‰…‘ôˆ¤4(4(€€€½¹ÑÉ…Ñ}Ù•ÉÍ¥½¹Ì€ôÍ•Ð (€€€€€€€ÍÉ••¹l‰5•ÑÉ¥}½¹ÑÉ…Ñ}Y•ÉÍ¥½¸‰t¹™¥±±¹„ ˆˆ¤¹…ÍÑåÁ”¡ÍÑÈ¤¹ÍÑÈ¹ÍÑÉ¥À ¤(€€€€¤(€€€¥˜½¹ÑÉ…Ñ}Ù•ÉÍ¥½¹Ì€„ôí5QI%}=9QIQ}YIM%=9ôè(€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È (€€€€€€€€€€€˜‰µ•ÑÉ¥Œ½¹ÑÉ…ÐÙ•ÉÍ¥½¸µ¥Íµ…Ñ èíÍ½ÉÑ•¡½¹ÑÉ…Ñ}Ù•ÉÍ¥½¹Ì¥ôˆ(€€€€€€€€¤(€€€}Ù…±¥‘…Ñ•}µ•ÑÉ¥}½¹ÑÉ…Ð¡ÍÉ••¸¤(€€€}Ù…±¥‘…Ñ•}™¥¹…¹¥…±}™½ÉµÕ±…Ì¡ÍÉ••¸¤(€€€é•É½}É•Á½ÉÐ€ô‰Õ¥±‘}é•É½}±…ÍÍ¥™¥…Ñ¥½¹}É•Á½ÉÐ¡ÍÉ••¸¤(€€€¥˜é•É½}É•Á½ÉÑl‰ÍÕµµ…Éä‰ul‰¥¹Ù…±¥‘}é•É¼‰tè(€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È (€€€€€€€€€€€˜‰¥¹Ù…±¥é•É¼Ù…±Õ•ÌÉ•µ…¥¸èíé•É½}É•Á½ÉÑlÍÕµµ…Éäul¥¹Ù…±¥‘}é•É¼uôˆ(€€€€€€€€¤(€€€É•ÅÕ¥É•‘}µ¥ÍÍ¥¹œ€ôÍÉ••¹l‰I•ÅÕ¥É•‘}5¥ÍÍ¥¹}5•ÑÉ¥Ì‰t¹™¥±±¹„ ˆˆ¤¹…ÍÑåÁ”¡ÍÑÈ¤¹ÍÑÈ¹ÍÑÉ¥À ¤4(€€€¥˜€¡•±¥¥‰±”€˜É•ÅÕ¥É•‘}µ¥ÍÍ¥¹œ¹¹” ˆˆ¤¤¹…¹ä ¤è4(€€€€€€€‰…€ôÍÉ••¸¹±½m•±¥¥‰±”€˜É•ÅÕ¥É•‘}µ¥ÍÍ¥¹œ¹¹” ˆˆ¤°€‰Q¥­•È‰t¹Ñ½±¥ÍÐ ¤4(€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È¡˜‰•±¥¥‰±”É½ÝÌ¡…Ù”É•ÅÕ¥É•µ•ÑÉ¥Œ…ÁÌèí‰…‘ôˆ¤4(4(€€€Í¡½ÉÑ±¥ÍÐ€ôÁ¹É•…‘}ÍØ¡Í¡½ÉÑ±¥ÍÑ}Á…Ñ °•¹½‘¥¹œô‰ÕÑ˜´àµÍ¥œˆ¤4(€€€}É•ÅÕ¥É•}½±Õµ¹Ì¡Í¡½ÉÑ±¥ÍÐ¹½±Õµ¹Ì°ì‰Q¥­•È‰ô°€‰Í¡½ÉÑ±¥ÍÐˆ¤4(€€€…ÑÕ…±}Í¡½ÉÑ±¥ÍÐ€ôÍ¡½ÉÑ±¥ÍÑl‰Q¥­•È‰t¹™¥±±¹„ ˆˆ¤¹…ÍÑåÁ”¡ÍÑÈ¤¹ÍÑÈ¹ÕÁÁ•È ¤¹Ñ½±¥ÍÐ ¤4(€€€•áÁ•Ñ•‘}Í¡½ÉÑ±¥ÍÐ€ô…ÑÕ…±}ÅÕ•Õ”(€€€¥˜…ÑÕ…±}Í¡½ÉÑ±¥ÍÐ€„ô•áÁ•Ñ•‘}Í¡½ÉÑ±¥ÍÐè4(€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È 4(€€€€€€€€€€€˜‰Í¡½ÉÑ±¥ÍÐÉ…¹­¥¹œµ¥Íµ…Ñ è•áÁ•Ñ•õí•áÁ•Ñ•‘}Í¡½ÉÑ±¥ÍÑô°…ÑÕ…°õí…ÑÕ…±}Í¡½ÉÑ±¥ÍÑôˆ4(€€€€€€€€¤4(4(€€€É•ÑÕÉ¸ÍÉ••¸°ì4(€€€€€€€€‰ÍÉ••¹}É½ÝÌˆè¥¹Ð¡±•¸¡ÍÉ••¸¤¤°4(€€€€€€€€‰•±¥¥‰±•}É½ÝÌˆè¥¹Ð¡•±¥¥‰±”¹ÍÕ´ ¤¤°4(€€€€€€€€‰Í¡½ÉÑ±¥ÍÑ}É½ÝÌˆè¥¹Ð¡±•¸¡Í¡½ÉÑ±¥ÍÐ¤¤°(€€€€€€€€‰‘•¥Í¥½¹}½Õ¹ÑÌˆèÍÑ…Ñ•Ì¹Ù…±Õ•}½Õ¹ÑÌ ¤¹Í½ÉÑ}¥¹‘•à ¤¹Ñ½}‘¥Ð ¤°(€€€€€€€€‰é•É½}±…ÍÍ¥™¥…Ñ¥½¸ˆèé•É½}É•Á½ÉÑl‰ÍÕµµ…Éä‰t°(€€€ô°‘•¥Í¥½¹}Ñ¥µ•ÍÑ…µÁÌ¹¥±½lÁt(4(4)‘•˜}Ù…±¥‘…Ñ•}•Ù¥‘•¹” 4(€€€•Ù¥‘•¹•}Á…Ñ èA…Ñ °4(€€€ÍÉ••¹}Ñ¥­•ÉÌèÍ•ÑmÍÑÉt°4(€€€ÍÉ••¹}‘•¥Í¥½¹}Ñ¥µ•ÍÑ…µÀè‘…Ñ•Ñ¥µ”°4(¤€´ø¥ÑmÍÑÈ°¹åtè4(€€€•Ù¥‘•¹•}¥‘ÌèÍ•ÑmÍÑÉt€ôÍ•Ð ¤4(€€€±¥¹•…”è¥ÑmÍÑÈ°±¥ÍÑmÍÑÉut€ôíô4(€€€…Ù…¥±…‰¥±¥Ñå}‰å}¥è¥ÑmÍÑÈ°‘…Ñ•Ñ¥µ•t€ôíô4(€€€Í½ÕÉ•}½Õ¹Ð€ô€À4(€€€‘•É¥Ù•‘}½Õ¹Ð€ô€À4(€€€Ý¥Ñ •Ù¥‘•¹•}Á…Ñ ¹½Á•¸ ‰Èˆ°•¹½‘¥¹œô‰ÕÑ˜´àµÍ¥œˆ°¹•Ý±¥¹”ôˆˆ¤…Ì¡…¹‘±”è4(€€€€€€€É•…‘•È€ôÍØ¹¥ÑI•…‘•È¡¡…¹‘±”¤4(€€€€€€€}É•ÅÕ¥É•}½±Õµ¹Ì¡É•…‘•È¹™¥•±‘¹…µ•Ì½Èmt°Y%9}=1U59L°€‰•Ù¥‘•¹”±•‘•Èˆ¤4(€€€€€€€™½È±¥¹•}¹Õµ‰•È°É½Ü¥¸•¹Õµ•É…Ñ”¡É•…‘•È°ÍÑ…ÉÐôÈ¤è4(€€€€€€€€€€€•Ù¥‘•¹•}¥€ôÍÑÈ¡É½Ü¹•Ð ‰•Ù¥‘•¹•}¥ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤4(€€€€€€€€€€€¥˜¹½Ð•Ù¥‘•¹•}¥½È•Ù¥‘•¹•}¥¥¸•Ù¥‘•¹•}¥‘Ìè4(€€€€€€€€€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È 4(€€€€€€€€€€€€€€€€€€€˜‰‰±…¹¬½È‘ÕÁ±¥…Ñ”•Ù¥‘•¹•}¥…Ð±¥¹”í±¥¹•}¹Õµ‰•Éôèí•Ù¥‘•¹•}¥…Éôˆ4(€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€•Ù¥‘•¹•}¥‘Ì¹…‘¡•Ù¥‘•¹•}¥¤4(€€€€€€€€€€€Ñ¥­•È€ôÍÑÈ¡É½Ü¹•Ð ‰Ñ¥­•Èˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤¹ÕÁÁ•È ¤4(€€€€€€€€€€€¥˜Ñ¥­•È¹½Ð¥¸ÍÉ••¹}Ñ¥­•ÉÌè4(€€€€€€€€€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È 4(€€€€€€€€€€€€€€€€€€€˜‰•Ù¥‘•¹”Ñ¥­•È¥Ì…‰Í•¹Ð™É½´ÍÉ••¸…Ð±¥¹”í±¥¹•}¹Õµ‰•ÉôèíÑ¥­•È…Éôˆ4(€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€¥˜¹½Ð}…Í}‰½½°¡É½Ü¹•Ð ‰Í•±•Ñ•‘}™½É}µ½‘•°ˆ¤°€‰Í•±•Ñ•‘}™½É}µ½‘•°ˆ¤è4(€€€€€€€€€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È¡˜‰Õ¹Í•±•Ñ••Ù¥‘•¹”•áÁ½ÉÑ•…Ð±¥¹”í±¥¹•}¹Õµ‰•Éôˆ¤4(€€€€€€€€€€€¥˜¹½Ð}…Í}‰½½° 4(€€€€€€€€€€€€€€€É½Ü¹•Ð ‰¥Í}…Ù…¥±…‰±•}…Ñ}‘•¥Í¥½¸ˆ¤°€‰¥Í}…Ù…¥±…‰±•}…Ñ}‘•¥Í¥½¸ˆ4(€€€€€€€€€€€€¤è4(€€€€€€€€€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È¡˜‰™ÕÑÕÉ”½Õ¹…Ù…¥±…‰±”•Ù¥‘•¹”Í•±•Ñ•…Ð±¥¹”í±¥¹•}¹Õµ‰•Éôˆ¤4(4(€€€€€€€€€€€…Ù…¥±…‰±•}…Ð€ô}Ñ¥µ•ÍÑ…µÀ¡É½Ü¹•Ð ‰…Ù…¥±…‰±•}Ñ½}µ½‘•±}…Ðˆ¤¤4(€€€€€€€€€€€‘•¥Í¥½¹}…Ð€ô}Ñ¥µ•ÍÑ…µÀ¡É½Ü¹•Ð ‰‘•¥Í¥½¹}Ñ¥µ•ÍÑ…µÀˆ¤¤4(€€€€€€€€€€€¥˜…Ù…¥±…‰±•}…Ð¥Ì9½¹”½È‘•¥Í¥½¹}…Ð¥Ì9½¹”½È…Ù…¥±…‰±•}…Ð€ø‘•¥Í¥½¹}…Ðè4(€€€€€€€€€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È 4(€€€€€€€€€€€€€€€€€€€˜‰¥¹Ù…±¥Á½¥¹Ðµ¥¸µÑ¥µ”…Ù…¥±…‰¥±¥Ñä…Ð±¥¹”í±¥¹•}¹Õµ‰•Éôˆ4(€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€¥˜‘•¥Í¥½¹}…Ð€„ôÍÉ••¹}‘•¥Í¥½¹}Ñ¥µ•ÍÑ…µÀè4(€€€€€€€€€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È 4(€€€€€€€€€€€€€€€€€€€˜‰•Ù¥‘•¹”‘•¥Í¥½¸Ñ¥µ•ÍÑ…µÀ‘¥™™•ÉÌ™É½´ÍÉ••¸…Ð±¥¹”í±¥¹•}¹Õµ‰•Éôˆ4(€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€…Ù…¥±…‰¥±¥Ñå}‰å}¥‘m•Ù¥‘•¹•}¥‘t€ô…Ù…¥±…‰±•}…Ð4(4(€€€€€€€€€€€É•½É‘}ÑåÁ”€ôÍÑÈ¡É½Ü¹•Ð ‰É•½É‘}ÑåÁ”ˆ¤½È€ˆˆ¤4(€€€€€€€€€€€¥˜É•½É‘}ÑåÁ”€ôô€‰Í½ÕÉ•}™…Ðˆè4(€€€€€€€€€€€€€€€Í½ÕÉ•}½Õ¹Ð€¬ô€Ä4(€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”4(€€€€€€€€€€€¥˜É•½É‘}ÑåÁ”€„ô€‰‘•É¥Ù•‘}µ•ÑÉ¥Œˆè4(€€€€€€€€€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È¡˜‰¥¹Ù…±¥É•½É‘}ÑåÁ”…Ð±¥¹”í±¥¹•}¹Õµ‰•ÉôèíÉ•½É‘}ÑåÁ”…Éôˆ¤4(€€€€€€€€€€€‘•É¥Ù•‘}½Õ¹Ð€¬ô€Ä4(€€€€€€€€€€€ÑÉäè4(€€€€€€€€€€€€€€€Í½ÕÉ•}¥‘Ì€ô©Í½¸¹±½…‘Ì¡É½Ü¹•Ð ‰Í½ÕÉ•}•Ù¥‘•¹•}¥‘Ìˆ¤½È€‰mtˆ¤4(€€€€€€€€€€€•á•ÁÐ©Í½¸¹)M=9•½‘•ÉÉ½È…Ì•áŒè4(€€€€€€€€€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È 4(€€€€€€€€€€€€€€€€€€€˜‰¥¹Ù…±¥Í½ÕÉ•}•Ù¥‘•¹•}¥‘Ì)M=8…Ð±¥¹”í±¥¹•}¹Õµ‰•Éôˆ4(€€€€€€€€€€€€€€€€¤™É½´•áŒ4(€€€€€€€€€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡Í½ÕÉ•}¥‘Ì°±¥ÍÐ¤½È¹½ÐÍ½ÕÉ•}¥‘Ìè4(€€€€€€€€€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È¡˜‰Í•±•Ñ•‘•É¥Ù•µ•ÑÉ¥Œ¡…Ì¹¼±¥¹•…”…Ð±¥¹”í±¥¹•}¹Õµ‰•Éôˆ¤4(€€€€€€€€€€€±¥¹•…•m•Ù¥‘•¹•}¥‘t€ômÍÑÈ¡¥Ñ•´¤™½È¥Ñ•´¥¸Í½ÕÉ•}¥‘Ít4(4(€€€¥˜¹½Ð•Ù¥‘•¹•}¥‘Ìè4(€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È ‰Í•±•Ñ••Ù¥‘•¹”±•‘•È¥Ì•µÁÑäˆ¤4(€€€µ¥ÍÍ¥¹}Í½ÕÉ•Ì€ôÍ½ÉÑ• 4(€€€€€€€ì4(€€€€€€€€€€€Í½ÕÉ•}¥4(€€€€€€€€€€€™½ÈÍ½ÕÉ•}¥‘Ì¥¸±¥¹•…”¹Ù…±Õ•Ì ¤4(€€€€€€€€€€€™½ÈÍ½ÕÉ•}¥¥¸Í½ÕÉ•}¥‘Ì4(€€€€€€€€€€€¥˜Í½ÕÉ•}¥¹½Ð¥¸•Ù¥‘•¹•}¥‘Ì4(€€€€€€€ô4(€€€€¤4(€€€¥˜µ¥ÍÍ¥¹}Í½ÕÉ•Ìè4(€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È 4(€€€€€€€€€€€˜‰‘•É¥Ù••Ù¥‘•¹”É•™•É•¹•Ìí±•¸¡µ¥ÍÍ¥¹}Í½ÕÉ•Ì¥ôµ¥ÍÍ¥¹œÍ½ÕÉ”¥‘Ìˆ4(€€€€€€€€¤4(€€€™½È•Ù¥‘•¹•}¥°Í½ÕÉ•}¥‘Ì¥¸±¥¹•…”¹¥Ñ•µÌ ¤è4(€€€€€€€‘•É¥Ù•‘}…Ð€ô…Ù…¥±…‰¥±¥Ñå}‰å}¥‘m•Ù¥‘•¹•}¥‘t4(€€€€€€€±…Ñ•ÍÑ}Í½ÕÉ•}…Ð€ôµ…à¡…Ù…¥±…‰¥±¥Ñå}‰å}¥‘mÍ½ÕÉ•}¥‘t™½ÈÍ½ÕÉ•}¥¥¸Í½ÕÉ•}¥‘Ì¤4(€€€€€€€¥˜‘•É¥Ù•‘}…Ð€ð±…Ñ•ÍÑ}Í½ÕÉ•}…Ðè4(€€€€€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È 4(€€€€€€€€€€€€€€€˜‰‘•É¥Ù••Ù¥‘•¹”ÁÉ•‘…Ñ•Ì½¹”½˜¥ÑÌÍ½ÕÉ•Ìèí•Ù¥‘•¹•}¥‘ôˆ4(€€€€€€€€€€€€¤4(4(€€€Ù¥Í¥Ñ¥¹œèÍ•ÑmÍÑÉt€ôÍ•Ð ¤4(€€€Ù¥Í¥Ñ•èÍ•ÑmÍÑÉt€ôÍ•Ð ¤4(4(€€€‘•˜Ù¥Í¥Ð¡•Ù¥‘•¹•}¥èÍÑÈ¤€´ø9½¹”è4(€€€€€€€¥˜•Ù¥‘•¹•}¥¥¸Ù¥Í¥Ñ¥¹œè4(€€€€€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È¡˜‰å±¥Œ•Ù¥‘•¹”±¥¹•…”‘•Ñ•Ñ•…Ðí•Ù¥‘•¹•}¥‘ôˆ¤4(€€€€€€€¥˜•Ù¥‘•¹•}¥¥¸Ù¥Í¥Ñ•½È•Ù¥‘•¹•}¥¹½Ð¥¸±¥¹•…”è4(€€€€€€€€€€€É•ÑÕÉ¸4(€€€€€€€Ù¥Í¥Ñ¥¹œ¹…‘¡•Ù¥‘•¹•}¥¤4(€€€€€€€™½ÈÍ½ÕÉ•}¥¥¸±¥¹•…•m•Ù¥‘•¹•}¥‘tè4(€€€€€€€€€€€Ù¥Í¥Ð¡Í½ÕÉ•}¥¤4(€€€€€€€Ù¥Í¥Ñ¥¹œ¹É•µ½Ù”¡•Ù¥‘•¹•}¥¤4(€€€€€€€Ù¥Í¥Ñ•¹…‘¡•Ù¥‘•¹•}¥¤4(4(€€€™½È•Ù¥‘•¹•}¥¥¸±¥¹•…”è4(€€€€€€€Ù¥Í¥Ð¡•Ù¥‘•¹•}¥¤4(€€€É•ÑÕÉ¸ì4(€€€€€€€€‰•Ù¥‘•¹•}É½ÝÌˆè±•¸¡•Ù¥‘•¹•}¥‘Ì¤°4(€€€€€€€€‰Í½ÕÉ•}•Ù¥‘•¹•}É½ÝÌˆèÍ½ÕÉ•}½Õ¹Ð°4(€€€€€€€€‰‘•É¥Ù•‘}•Ù¥‘•¹•}É½ÝÌˆè‘•É¥Ù•‘}½Õ¹Ð°4(€€€ô4(4(4)‘•˜Ù…±¥‘…Ñ•}½ÕÑÁÕÑÌ 4(€€€ÍÉ••¹}Á…Ñ èÍÑÈðA…Ñ €ôU1Q}MI8°4(€€€Í¡½ÉÑ±¥ÍÑ}Á…Ñ èÍÑÈðA…Ñ €ôU1Q}M!=IQ1%MP°4(€€€•Ù¥‘•¹•}Á…Ñ èÍÑÈðA…Ñ €ôU1Q}Y%9°4(€€€‘…Í¡‰½…É‘}Á…Ñ èÍÑÈðA…Ñ ð9½¹”€ô9½¹”°4(¤€´ø¥ÑmÍÑÈ°¹åtè4(€€€Á…Ñ¡Ì€ômA…Ñ ¡ÍÉ••¹}Á…Ñ ¤°A…Ñ ¡Í¡½ÉÑ±¥ÍÑ}Á…Ñ ¤°A…Ñ ¡•Ù¥‘•¹•}Á…Ñ ¥t4(€€€µ¥ÍÍ¥¹œ€ômÍÑÈ¡Á…Ñ ¤™½ÈÁ…Ñ ¥¸Á…Ñ¡Ì¥˜¹½ÐÁ…Ñ ¹¥Í}™¥±” ¥t4(€€€¥˜µ¥ÍÍ¥¹œè4(€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È¡˜‰µ¥ÍÍ¥¹œ½ÕÑÁÕÐ™¥±•Ìèìœ°€œ¹©½¥¸¡µ¥ÍÍ¥¹œ¥ôˆ¤4(€€€ÍÉ••¸°ÍÕµµ…Éä°‘•¥Í¥½¹}Ñ¥µ•ÍÑ…µÀ€ô}Ù…±¥‘…Ñ•}ÍÉ••¸¡Á…Ñ¡ÍlÁt°Á…Ñ¡ÍlÅt¤4(€€€ÍÕµµ…Éä¹ÕÁ‘…Ñ” 4(€€€€€€€}Ù…±¥‘…Ñ•}•Ù¥‘•¹” 4(€€€€€€€€€€€Á…Ñ¡ÍlÉt°4(€€€€€€€€€€€Í•Ð¡ÍÉ••¹l‰Q¥­•È‰t¤°4(€€€€€€€€€€€‘•¥Í¥½¹}Ñ¥µ•ÍÑ…µÀ°4(€€€€€€€€¤4(€€€€¤4(€€€¥˜‘…Í¡‰½…É‘}Á…Ñ ¥Ì¹½Ð9½¹”è4(€€€€€€€‘…Í¡‰½…É€ôA…Ñ ¡‘…Í¡‰½…É‘}Á…Ñ ¤4(€€€€€€€¥˜¹½Ð‘…Í¡‰½…É¹¥Í}™¥±” ¤è4(€€€€€€€€€€€É…¥Í”Y…±¥‘…Ñ¥½¹ÉÉ½È¡˜‰µ¥ÍÍ¥¹œ‘…Í¡‰½…É™¥±”èí‘…Í¡‰½…É‘ôˆ¤4(€€€€€€€ÍÕµµ…Éä¹ÕÁ‘…Ñ”¡}Ù…±¥‘…Ñ•}‘…Í¡‰½…É¡‘…Í¡‰½…É°ÍÉ••¸¤¤4(€€€É•ÑÕÉ¸ÍÕµµ…Éä4(4(4)‘•˜‰Õ¥±‘}Á…ÉÍ•È ¤€´ø…ÉÁ…ÉÍ”¹ÉÕµ•¹ÑA…ÉÍ•Èè(€€€Á…ÉÍ•È€ô…ÉÁ…ÉÍ”¹ÉÕµ•¹ÑA…ÉÍ•È¡‘•ÍÉ¥ÁÑ¥½¸ô‰Y…±¥‘…Ñ”5½‘”½ÕÑÁÕÐ¥¹Ù…É¥…¹ÑÌˆ¤4(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µÍÉ••¸ˆ°‘•™…Õ±ÐõU1Q}MI8¤4(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µÍ¡½ÉÑ±¥ÍÐˆ°‘•™…Õ±ÐõU1Q}M!=IQ1%MP¤4(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µ•Ù¥‘•¹”ˆ°‘•™…Õ±ÐõU1Q}Y%9¤4(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µ‘…Í¡‰½…Éˆ¤(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð (€€€€€€€€ˆ´µé•É¼µÉ•Á½ÉÐˆ°(€€€€€€€‘•™…Õ±Ðô‰µ½‘•}}é•É½}…Õ‘¥Ð¹©Í½¸ˆ°(€€€€€€€¡•±Àô‰]É¥Ñ”Ñ¡”±…ÍÍ¥™¥•¥µÁ½ÉÑ…¹Ðµµ•ÑÉ¥Œé•É¼…Õ‘¥Ð€¡•µÁÑä‘¥Í…‰±•Ì¥Ð¤ˆ°(€€€€¤(€€€É•ÑÕÉ¸Á…ÉÍ•È4(4(4)‘•˜µ…¥¸ ¤€´ø¥¹Ðè4(€€€…ÉÌ€ô‰Õ¥±‘}Á…ÉÍ•È ¤¹Á…ÉÍ•}…ÉÌ ¤4(€€€ÑÉäè4(€€€€€€€ÍÕµµ…Éä€ôÙ…±¥‘…Ñ•}½ÕÑÁÕÑÌ (€€€€€€€€€€€…ÉÌ¹ÍÉ••¸°4(€€€€€€€€€€€…ÉÌ¹Í¡½ÉÑ±¥ÍÐ°4(€€€€€€€€€€€…ÉÌ¹•Ù¥‘•¹”°4(€€€€€€€€€€€…ÉÌ¹‘…Í¡‰½…É°(€€€€€€€€¤(€€€€€€€¥˜…ÉÌ¹é•É½}É•Á½ÉÐè(€€€€€€€€€€€ÍÉ••¸€ôÁ¹É•…‘}ÍØ¡…ÉÌ¹ÍÉ••¸°•¹½‘¥¹œô‰ÕÑ˜´àµÍ¥œˆ¤(€€€€€€€€€€€É•Á½ÉÐ€ô‰Õ¥±‘}é•É½}±…ÍÍ¥™¥…Ñ¥½¹}É•Á½ÉÐ¡ÍÉ••¸¤(€€€€€€€€€€€A…Ñ ¡…ÉÌ¹é•É½}É•Á½ÉÐ¤¹ÝÉ¥Ñ•}Ñ•áÐ (€€€€€€€€€€€€€€€©Í½¸¹‘ÕµÁÌ¡É•Á½ÉÐ°•¹ÍÕÉ•}…Í¥¤õ…±Í”°¥¹‘•¹ÐôÈ¤°(€€€€€€€€€€€€€€€•¹½‘¥¹œô‰ÕÑ˜´àˆ°(€€€€€€€€€€€€¤(€€€€€€€€€€€ÍÕµµ…Éål‰é•É½}É•Á½ÉÐ‰t€ôÍÑÈ¡…ÉÌ¹é•É½}É•Á½ÉÐ¤(€€€•á•ÁÐY…±¥‘…Ñ¥½¹ÉÉ½È…Ì•áŒè4(€€€€€€€ÁÉ¥¹Ð¡˜‰5½‘”½ÕÑÁÕÐÙ…±¥‘…Ñ¥½¸™…¥±•èí•áôˆ¤4(€€€€€€€É•ÑÕÉ¸€Ä4(€€€ÁÉ¥¹Ð¡©Í½¸¹‘ÕµÁÌ¡ì‰ÍÑ…ÑÕÌˆè€‰½¬ˆ°€¨©ÍÕµµ…Éåô°Í½ÉÑ}­•åÌõQÉÕ”¤¤4(€€€É•ÑÕÉ¸€À4(4(4)¥˜}}¹…µ•}|€ôô€‰}}µ…¥¹}|ˆè4(€€€É…¥Í”MåÍÑ•µá¥Ð¡µ…¥¸ ¤¤4