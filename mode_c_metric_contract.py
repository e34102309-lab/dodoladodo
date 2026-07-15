from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd


METRIC_CONTRACT_VERSION = "2026-07-metric-status-v1"
METRIC_STATUSES = frozenset(
    {
        "VALID",
        "MISSING",
        "NOT_APPLICABLE",
        "ABSTAIN",
        "STALE",
        "INVALID",
        "ESTIMATED",
    }
)
NULL_STATUSES = frozenset(
    {"MISSING", "NOT_APPLICABLE", "ABSTAIN", "STALE", "INVALID"}
)


DISPLAY_METRICS: tuple[str, ...] = (
    "Data_Confidence_Score",
    "Evidence_AcceptedAt_Ratio",
    "Long_Term_Score",
    "Industry_Model_Score",
    "Industry_Model_Coverage",
    "Quality_Score",
    "Value_Score",
    "Expectations_Score",
    "Operating_Inflection_Score",
    "Capital_Allocation_Score",
    "Risk_Penalty",
    "Price",
    "MarketCap_B",
    "EV_B",
    "TTM_OCF_B",
    "Dynamic_CapEx_B",
    "Maintenance_CapEx_B",
    "Maintenance_CapEx_Low_B",
    "Maintenance_CapEx_High_B",
    "Growth_CapEx_B",
    "CapEx_to_DnA_x",
    "TTM_SBC_B",
    "SBC_Economic_Cost_B",
    "Real_FCF_Yield_pct",
    "Conservative_Real_FCF_Yield_pct",
    "Maintenance_Real_FCF_to_MarketCap_Yield_pct",
    "Conservative_Real_FCF_to_MarketCap_Yield_pct",
    "Maintenance_Real_FCF_to_EV_Yield_pct",
    "Conservative_Real_FCF_to_EV_Yield_pct",
    "Maintenance_Real_FCF_Yield_Low_pct",
    "Maintenance_Real_FCF_Yield_High_pct",
    "Total_Debt_B",
    "Cash_B",
    "Net_Debt_B",
    "ICR",
    "Stress_ICR_30x",
    "NetDebt_to_Stress_EBITDA_30x",
    "Stress_Real_FCF_30_B",
    "ROIC_pct",
    "ROCE_pct",
    "Real_FCF_Positive_Years_5Y",
    "OCF_to_NetIncome_5Y",
    "EV_EBITDA_x",
    "PE_x",
    "GM_Latest_pct",
    "GM_3Q_Change_pp",
    "Rev_3Q_Change_pct",
    "DSI_QoQ_Change_pct",
    "DSI_YoY_Change_pct",
    "Share_Count_Change_pct",
    "Share_Count_Change_3Y_pct",
    "Net_Buyback_Yield_pct",
    "Per_Share_FCF_CAGR_3Y_pct",
    "Per_Share_EPS_CAGR_3Y_pct",
    "Implied_EBITDA_CAGR_3Y_pct",
    "Implied_CAGR_Limit_pct",
    "Implied_CAGR_Headroom_pct",
    "Reverse_DCF_Required_Return_pct",
    "EV_EBITDA_10Y_Percentile",
    "PE_10Y_Percentile",
    "Historical_Valuation_Coverage",
    "Point_in_Time_FX_Rate",
    "ADR_Ratio",
    "EBITDA_Drawdown_30_pct",
)


COMMON_SPECIALIZED_METRICS = frozenset(
    {
        "Data_Confidence_Score",
        "Evidence_AcceptedAt_Ratio",
        "Long_Term_Score",
        "Industry_Model_Score",
        "Industry_Model_Coverage",
        "Price",
        "MarketCap_B",
        "Point_in_Time_FX_Rate",
        "ADR_Ratio",
    }
)
DEBT_COVERAGE_METRICS = frozenset(
    {"Total_Debt_B", "Cash_B", "Net_Debt_B", "ICR"}
)
REPORTED_CASH_FLOW_METRICS = frozenset(
    {"TTM_OCF_B", "Dynamic_CapEx_B", "Maintenance_CapEx_B"}
)


MODEL_APPLICABLE_METRICS: dict[str, frozenset[str]] = {
    "GENERAL_CORPORATE": frozenset(DISPLAY_METRICS) - {
        "Industry_Model_Score",
        "Industry_Model_Coverage",
    },
    "BANK": COMMON_SPECIALIZED_METRICS,
    "INSURANCE_P_AND_C": COMMON_SPECIALIZED_METRICS,
    "INSURANCE_LIFE": COMMON_SPECIALIZED_METRICS,
    "REIT_EQUITY": COMMON_SPECIALIZED_METRICS | DEBT_COVERAGE_METRICS,
    "REIT_MORTGAGE": COMMON_SPECIALIZED_METRICS,
    "REGULATED_UTILITY": (
        COMMON_SPECIALIZED_METRICS
        | DEBT_COVERAGE_METRICS
        | REPORTED_CASH_FLOW_METRICS
    ),
    "CYCLICAL_MIDCYCLE": (
        COMMON_SPECIALIZED_METRICS
        | DEBT_COVERAGE_METRICS
        | REPORTED_CASH_FLOW_METRICS
        | {"TTM_SBC_B", "SBC_Economic_Cost_B"}
    ),
    "FINANCIAL_LENDER": COMMON_SPECIALIZED_METRICS,
    "FINANCIAL_FEE": COMMON_SPECIALIZED_METRICS,
}


GENERAL_REQUIRED_METRICS = frozenset(
    {
        "Data_Confidence_Score",
        "Long_Term_Score",
        "MarketCap_B",
        "EV_B",
        "TTM_OCF_B",
        "Dynamic_CapEx_B",
        "Maintenance_CapEx_B",
        "TTM_SBC_B",
        "Real_FCF_Yield_pct",
        "ICR",
        "ROIC_pct",
        "EV_EBITDA_x",
    }
)
SPECIALIZED_REQUIRED_METRICS = frozenset(
    {
        "Data_Confidence_Score",
        "Long_Term_Score",
        "Industry_Model_Score",
        "Industry_Model_Coverage",
        "MarketCap_B",
    }
)


ESTIMATED_METRICS = frozenset(
    {
        "Maintenance_CapEx_B",
        "Maintenance_CapEx_Low_B",
        "Maintenance_CapEx_High_B",
        "Growth_CapEx_B",
        "Real_FCF_Yield_pct",
        "Maintenance_Real_FCF_to_MarketCap_Yield_pct",
        "Maintenance_Real_FCF_to_EV_Yield_pct",
        "Maintenance_Real_FCF_Yield_Low_pct",
        "Maintenance_Real_FCF_Yield_High_pct",
        "Stress_ICR_30x",
        "NetDebt_to_Stress_EBITDA_30x",
        "Stress_Real_FCF_30_B",
        "Implied_EBITDA_CAGR_3Y_pct",
        "Implied_CAGR_Limit_pct",
        "Implied_CAGR_Headroom_pct",
        "EBITDA_Drawdown_30_pct",
    }
)


CSV_STATUS_METRICS = (
    "Real_FCF_Yield_pct",
    "Conservative_Real_FCF_Yield_pct",
    "Maintenance_CapEx_B",
    "Growth_CapEx_B",
    "ROIC_pct",
    "ICR",
    "EV_EBITDA_x",
    "Long_Term_Score",
)


ZERO_REQUIRES_EVIDENCE_METRICS = frozenset(
    {
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
)


EVIDENCE_ALIASES: dict[str, tuple[str, ...]] = {
    "Data_Confidence_Score": ("Data_Confidence_Score",),
    "Long_Term_Score": ("Long_Term_Score",),
    "Quality_Score": ("Quality_Score",),
    "Value_Score": ("Value_Score",),
    "Expectations_Score": ("Expectations_Score",),
    "Operating_Inflection_Score": ("Operating_Inflection_Score",),
    "Capital_Allocation_Score": ("Capital_Allocation_Score",),
    "Risk_Penalty": ("Risk_Penalty",),
    "Price": ("MarketPrice",),
    "MarketCap_B": ("MarketCap",),
    "EV_B": ("EnterpriseValue",),
    "TTM_OCF_B": ("TTM_OCF",),
    "Dynamic_CapEx_B": ("TTM_CapEx",),
    "Maintenance_CapEx_B": ("Maintenance_CapEx",),
    "Maintenance_CapEx_Low_B": ("Maintenance_CapEx_Low",),
    "Maintenance_CapEx_High_B": ("Maintenance_CapEx_High",),
    "Growth_CapEx_B": ("Growth_CapEx",),
    "CapEx_to_DnA_x": ("TTM_CapEx", "TTM_DnA"),
    "TTM_SBC_B": ("TTM_SBC",),
    "SBC_Economic_Cost_B": ("TTM_SBC",),
    "Real_FCF_Yield_pct": ("Real_FCF_to_MarketCap_Yield", "Real_FCF_Yield"),
    "Conservative_Real_FCF_Yield_pct": (
        "Conservative_Real_FCF_to_MarketCap_Yield",
        "Conservative_Real_FCF_Yield",
    ),
    "Maintenance_Real_FCF_to_MarketCap_Yield_pct": (
        "Real_FCF_to_MarketCap_Yield",
    ),
    "Conservative_Real_FCF_to_MarketCap_Yield_pct": (
        "Conservative_Real_FCF_to_MarketCap_Yield",
    ),
    "Maintenance_Real_FCF_to_EV_Yield_pct": ("Real_FCF_to_EV_Yield",),
    "Conservative_Real_FCF_to_EV_Yield_pct": (
        "Conservative_Real_FCF_to_EV_Yield",
    ),
    "Maintenance_Real_FCF_Yield_Low_pct": (
        "Maintenance_Real_FCF_Yield_Low",
    ),
    "Maintenance_Real_FCF_Yield_High_pct": (
        "Maintenance_Real_FCF_Yield_High",
    ),
    "Total_Debt_B": ("DebtTotal", "DebtCurrent", "DebtShortTermTotal"),
    "Cash_B": ("Cash",),
    "Net_Debt_B": ("DebtTotal", "Cash"),
    "ICR": ("ICR",),
    "Stress_ICR_30x": ("Stress_ICR_30",),
    "NetDebt_to_Stress_EBITDA_30x": ("NetDebt_to_Stress_EBITDA_30",),
    "Stress_Real_FCF_30_B": ("Stress_Real_FCF_30",),
    "ROIC_pct": ("ROIC",),
    "ROCE_pct": ("ROCE",),
    "Real_FCF_Positive_Years_5Y": ("Real_FCF_Positive_Years_5Y",),
    "OCF_to_NetIncome_5Y": ("OCF_to_NetIncome_5Y",),
    "EV_EBITDA_x": ("EV_EBITDA",),
    "PE_x": ("PE",),
    "GM_Latest_pct": ("GM_Latest",),
    "GM_3Q_Change_pp": ("GM_3Q_Change",),
    "Rev_3Q_Change_pct": ("Revenue_3Q_Change",),
    "DSI_QoQ_Change_pct": ("DSI_QoQ_Change",),
    "DSI_YoY_Change_pct": ("DSI_YoY_Change",),
    "Share_Count_Change_pct": ("Share_Count_Change_1Y",),
    "Share_Count_Change_3Y_pct": ("Share_Count_Change_3Y",),
    "Net_Buyback_Yield_pct": ("Net_Buyback_Yield",),
    "Per_Share_FCF_CAGR_3Y_pct": ("Per_Share_FCF_CAGR_3Y",),
    "Per_Share_EPS_CAGR_3Y_pct": ("Per_Share_EPS_CAGR_3Y",),
    "Implied_EBITDA_CAGR_3Y_pct": ("Implied_EBITDA_CAGR_3Y",),
    "Implied_CAGR_Limit_pct": ("Implied_CAGR_Limit",),
    "EV_EBITDA_10Y_Percentile": ("EV_EBITDA_10Y_Percentile",),
    "PE_10Y_Percentile": ("PE_10Y_Percentile",),
    "Historical_Valuation_Coverage": ("Historical_Valuation_Coverage",),
}


def finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def clean_value(value: Any) -> Any:
    number = finite_number(value)
    if number is not None:
        return number
    if isinstance(value, bool):
        return value
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def model_key(row: Mapping[str, Any]) -> str:
    value = clean_value(row.get("Industry_Model_Key"))
    return str(value or "GENERAL_CORPORATE").upper()


def applicable_metrics(key: str) -> frozenset[str]:
    return MODEL_APPLICABLE_METRICS.get(key, COMMON_SPECIALIZED_METRICS)


def required_metrics(key: str) -> frozenset[str]:
    return (
        GENERAL_REQUIRED_METRICS
        if key == "GENERAL_CORPORATE"
        else SPECIALIZED_REQUIRED_METRICS
    )


def _evidence_index(
    evidence_rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for source in evidence_rows:
        if str(source.get("selected_for_model") or "").lower() not in {
            "true",
            "1",
            "yes",
        }:
            continue
        symbol = str(source.get("ticker") or "").upper()
        metric = str(source.get("normalized_metric") or "")
        if symbol and metric:
            index.setdefault((symbol, metric), []).append(dict(source))
    return index


def _metric_evidence(
    index: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]],
    ticker: str,
    metric: str,
    key: str,
) -> list[Mapping[str, Any]]:
    aliases = list(EVIDENCE_ALIASES.get(metric, (metric,)))
    if metric == "Industry_Model_Score":
        aliases.append(f"{key}:Industry_Model_Score")
    if metric == "Industry_Model_Coverage":
        aliases.extend(f"{key}:{name}" for name in ("Industry_Model_Score",))
    if metric == "Long_Term_Score" and key != "GENERAL_CORPORATE":
        aliases.append(f"{key}:Industry_Model_Score")
    records: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for alias in aliases:
        for record in index.get((ticker, alias), []):
            evidence_id = str(record.get("evidence_id") or "")
            if evidence_id and evidence_id not in seen:
                records.append(record)
                seen.add(evidence_id)
    return records


def _as_of(records: Sequence[Mapping[str, Any]], fallback: Any) -> str:
    candidates: list[str] = []
    for record in records:
        for field in ("period_end", "available_to_model_at", "filed_at"):
            candidate = clean_value(record.get(field))
            if candidate is not None:
                candidates.append(str(candidate))
                break
    if candidates:
        return max(candidates)
    fallback_value = clean_value(fallback)
    return str(fallback_value or "")[:10]


def _source_method(
    row: Mapping[str, Any],
    metric: str,
    records: Sequence[Mapping[str, Any]],
) -> str:
    if metric in {
        "Maintenance_CapEx_B",
        "Maintenance_CapEx_Low_B",
        "Maintenance_CapEx_High_B",
        "Growth_CapEx_B",
        "Real_FCF_Yield_pct",
        "Maintenance_Real_FCF_to_MarketCap_Yield_pct",
        "Maintenance_Real_FCF_to_EV_Yield_pct",
    }:
        explicit = str(row.get("CapEx_Reinvestment_Method") or "")
        if explicit:
            return explicit
    if metric == "ICR":
        explicit = str(row.get("ICR_Method") or "")
        if explicit:
            return explicit
    if metric in {"Total_Debt_B", "Cash_B", "Net_Debt_B"}:
        explicit = str(row.get("Debt_Source_Method") or "")
        if explicit:
            return explicit
    formulas = [clean_value(record.get("formula")) for record in records]
    formulas = [str(formula) for formula in formulas if formula is not None]
    return formulas[-1] if formulas else "screen_output"


def _null_status(row: Mapping[str, Any], metric: str) -> str:
    reasons = " ".join(
        str(row.get(field) or "")
        for field in ("Status", "Data_Confidence_Reasons", "Data_Quality_Flags")
    ).lower()
    if any(token in reasons for token in ("stale", "too old", "exceed", "overdue", "過舊")):
        return "STALE"
    if str(row.get("Decision_State") or "").upper() == "ABSTAIN" and metric in required_metrics(model_key(row)):
        return "ABSTAIN"
    return "MISSING"


def _metric_metadata(
    row: dict[str, Any],
    metric: str,
    index: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    key = model_key(row)
    ticker = str(row.get("Ticker") or "").upper()
    decision_at = row.get("Decision_Timestamp")
    records = _metric_evidence(index, ticker, metric, key)
    evidence_ids = [str(record.get("evidence_id")) for record in records]
    value = finite_number(row.get(metric))
    if metric not in applicable_metrics(key):
        return {
            "value": None,
            "status": "NOT_APPLICABLE",
            "reason": f"{key} uses dedicated industry metrics instead of {metric}",
            "as_of": str(decision_at or "")[:10],
            "source_method": "industry_route",
            "evidence_ids": [],
        }
    if (
        metric in {"Point_in_Time_FX_Rate", "ADR_Ratio"}
        and str(row.get("Input_Security_Class") or "").upper() != "COMMON_ADS_INFERRED"
    ):
        return {
            "value": None,
            "status": "NOT_APPLICABLE",
            "reason": "Domestic/common-equivalent security does not require ADS reconciliation",
            "as_of": str(decision_at or "")[:10],
            "source_method": "security_class_route",
            "evidence_ids": [],
        }
    if (
        metric in {"Point_in_Time_FX_Rate", "ADR_Ratio"}
        and str(row.get("Input_Security_Class") or "").upper() == "COMMON_ADS_INFERRED"
        and value is None
    ):
        return {
            "value": None,
            "status": "ABSTAIN",
            "reason": "ADS requires both a point-in-time FX rate and a verified ADR ratio",
            "as_of": str(decision_at or "")[:10],
            "source_method": "foreign_security_reconciliation_gate",
            "evidence_ids": evidence_ids,
        }
    if metric == "ICR" and str(row.get("ICR_Method") or "") in {
        "net_cash",
        "net_cash_non_binding",
        "immaterial_debt",
    }:
        return {
            "value": None,
            "status": "NOT_APPLICABLE",
            "reason": "Interest coverage is non-binding because cash covers debt or debt is immaterial",
            "as_of": _as_of(records, decision_at),
            "source_method": str(row.get("ICR_Method") or "net_cash"),
            "evidence_ids": evidence_ids,
        }
    if value is None:
        status = _null_status(row, metric)
        return {
            "value": None,
            "status": status,
            "reason": str(row.get("Data_Confidence_Reasons") or row.get("Status") or "No auditable value"),
            "as_of": _as_of(records, decision_at),
            "source_method": _source_method(row, metric, records),
            "evidence_ids": evidence_ids,
        }
    if (
        metric == "Historical_Valuation_Coverage"
        and str(row.get("Historical_Valuation_Status") or "").upper() != "VALID"
    ):
        return {
            "value": None,
            "status": "ABSTAIN" if str(row.get("Decision_State") or "").upper() == "ABSTAIN" else "MISSING",
            "reason": "Point-in-time valuation history has fewer than five valid years or less than 60% coverage",
            "as_of": _as_of(records, decision_at),
            "source_method": "point_in_time_annual_valuation_ledger",
            "evidence_ids": evidence_ids,
        }
    if metric in {"EV_B", "EV_EBITDA_x"} and value <= 0:
        return {
            "value": None,
            "status": "INVALID",
            "reason": "Non-positive enterprise value makes the metric non-comparable",
            "as_of": _as_of(records, decision_at),
            "source_method": _source_method(row, metric, records),
            "evidence_ids": evidence_ids,
        }
    if value == 0.0 and metric in ZERO_REQUIRES_EVIDENCE_METRICS and not evidence_ids:
        status = (
            "ABSTAIN"
            if str(row.get("Decision_State") or "").upper() == "ABSTAIN"
            else "MISSING"
        )
        return {
            "value": None,
            "status": status,
            "reason": "Reported zero has no selected evidence and is treated as a missing fallback",
            "as_of": _as_of(records, decision_at),
            "source_method": _source_method(row, metric, records),
            "evidence_ids": [],
        }
    if (
        metric in {
            "Maintenance_CapEx_B",
            "Maintenance_CapEx_Low_B",
            "Maintenance_CapEx_High_B",
            "Growth_CapEx_B",
            "Real_FCF_Yield_pct",
            "Maintenance_Real_FCF_to_MarketCap_Yield_pct",
            "Maintenance_Real_FCF_to_EV_Yield_pct",
        }
        and str(row.get("Maintenance_CapEx_Confidence") or "").upper() == "LOW"
    ):
        return {
            "value": None,
            "status": "ABSTAIN",
            "reason": "Maintenance CapEx estimate confidence is too low for a precise yield",
            "as_of": _as_of(records, decision_at),
            "source_method": _source_method(row, metric, records),
            "evidence_ids": evidence_ids,
        }
    status = "ESTIMATED" if metric in ESTIMATED_METRICS else "VALID"
    reason = (
        "Derived estimate or scenario; inspect method and sensitivity range"
        if status == "ESTIMATED"
        else "Auditable reported or derived metric"
    )
    return {
        "value": value,
        "status": status,
        "reason": reason,
        "as_of": _as_of(records, decision_at),
        "source_method": _source_method(row, metric, records),
        "evidence_ids": evidence_ids,
    }


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _specialized_metadata(
    row: Mapping[str, Any],
    index: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]],
) -> dict[str, dict[str, Any]]:
    key = model_key(row)
    if key == "GENERAL_CORPORATE":
        return {}
    ticker = str(row.get("Ticker") or "").upper()
    decision_at = row.get("Decision_Timestamp")
    output: dict[str, dict[str, Any]] = {}
    for namespace, raw_field in (
        ("industry", "Industry_Model_Metrics_JSON"),
        ("industry_component", "Industry_Model_Components_JSON"),
    ):
        for name, raw_value in _json_object(row.get(raw_field)).items():
            metric_name = f"{namespace}.{name}"
            value = clean_value(raw_value)
            records = list(index.get((ticker, f"{key}:{name}"), []))
            evidence_ids = [str(record.get("evidence_id")) for record in records]
            if value is None:
                status = "MISSING"
                reason = f"{key} did not produce {name}"
            else:
                status = (
                    "ESTIMATED"
                    if any(token in name.lower() for token in ("proxy", "midcycle", "trough"))
                    else "VALID"
                )
                reason = f"{key} dedicated metric"
            output[metric_name] = {
                "value": value,
                "status": status,
                "reason": reason,
                "as_of": _as_of(records, decision_at),
                "source_method": f"industry_model:{key}",
                "evidence_ids": evidence_ids,
            }
    return output


def annotate_rows(
    rows: Sequence[Mapping[str, Any]],
    evidence_rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    index = _evidence_index(evidence_rows)
    annotated: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        key = model_key(row)
        metadata = {
            metric: _metric_metadata(row, metric, index)
            for metric in DISPLAY_METRICS
        }
        metadata.update(_specialized_metadata(row, index))
        for metric in DISPLAY_METRICS:
            row[metric] = metadata[metric]["value"]
        for metric in CSV_STATUS_METRICS:
            meta = metadata[metric]
            row[f"{metric}_Status"] = meta["status"]
            row[f"{metric}_Reason"] = meta["reason"]
            row[f"{metric}_AsOf"] = meta["as_of"]
            row[f"{metric}_Source_Method"] = meta["source_method"]
        required = required_metrics(key)
        missing_required = sorted(
            metric
            for metric in required
            if metadata[metric]["status"] in NULL_STATUSES
            and metadata[metric]["status"] != "NOT_APPLICABLE"
        )
        optional_missing = sorted(
            metric
            for metric in applicable_metrics(key) - required
            if metadata[metric]["status"] in NULL_STATUSES
            and metadata[metric]["status"] != "NOT_APPLICABLE"
        )
        applicable = [metadata[metric] for metric in applicable_metrics(key)]
        evidenced = sum(
            bool(meta["evidence_ids"])
            for meta in applicable
            if meta["status"] in {"VALID", "ESTIMATED"}
        )
        available = sum(
            meta["status"] in {"VALID", "ESTIMATED"}
            for meta in applicable
        )
        coverage = evidenced / available if available else 0.0
        methods_text = " ".join(
            str(value or "")
            for value in (
                row.get("Debt_Source_Method"),
                row.get("ICR_Method"),
                row.get("CapEx_Reinvestment_Method"),
                row.get("Data_Confidence_Reasons"),
            )
        ).lower()
        row["Metric_Contract_Version"] = METRIC_CONTRACT_VERSION
        row["Metric_Metadata_JSON"] = json.dumps(
            metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        row["Required_Missing_Metrics"] = " | ".join(missing_required)
        row["Optional_Missing_Metrics"] = " | ".join(optional_missing)
        row["Metric_Evidence_Coverage"] = round(coverage, 4)
        row["Uses_Yahoo_Fallback"] = "yahoo" in methods_text
        row["Uses_Annual_Fallback"] = "fallback annual" in methods_text
        row["Uses_Estimated_Maintenance_CapEx"] = any(
            metadata[metric]["status"] == "ESTIMATED"
            for metric in (
                "Maintenance_CapEx_B",
                "Real_FCF_Yield_pct",
            )
        )
        annotated.append(row)
    return annotated


def annotate_dataframe(
    frame: pd.DataFrame,
    evidence_rows: Iterable[Mapping[str, Any]],
) -> pd.DataFrame:
    return pd.DataFrame(annotate_rows(frame.to_dict(orient="records"), evidence_rows))


def apply_contract_to_files(
    screen_path: Path,
    shortlist_path: Path,
    evidence_path: Path,
) -> None:
    evidence = pd.read_csv(evidence_path, encoding="utf-8-sig").to_dict(orient="records")
    screen = pd.read_csv(screen_path, encoding="utf-8-sig")
    annotated = annotate_dataframe(screen, evidence)
    annotated.to_csv(screen_path, index=False, encoding="utf-8-sig")
    shortlist = pd.read_csv(shortlist_path, encoding="utf-8-sig")
    order = [str(value).upper() for value in shortlist.get("Ticker", pd.Series(dtype=str))]
    by_ticker = annotated.assign(
        _ticker=annotated["Ticker"].astype(str).str.upper()
    ).set_index("_ticker", drop=True)
    selected = by_ticker.loc[[ticker for ticker in order if ticker in by_ticker.index]]
    selected.to_csv(shortlist_path, index=False, encoding="utf-8-sig")


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply the Mode C metric-status contract")
    parser.add_argument("--screen", default="mode_c_screen.csv")
    parser.add_argument("--shortlist", default="mode_c_shortlist.csv")
    parser.add_argument("--evidence", default="mode_c_evidence_ledger.csv")
    args = parser.parse_args()
    apply_contract_to_files(Path(args.screen), Path(args.shortlist), Path(args.evidence))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
