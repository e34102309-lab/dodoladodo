from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd


METRIC_CONTRACT_VERSION = "2026-07-metric-status-v3"
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
    "Maintenance_CapEx_Lower_B",
    "Maintenance_CapEx_Base_B",
    "Maintenance_CapEx_Upper_B",
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
    "Maintenance_Real_FCF_Yield_Lower_pct",
    "Maintenance_Real_FCF_Yield_Base_pct",
    "Maintenance_Real_FCF_Yield_Upper_pct",
    "FCF_Sensitivity_Spread_pp",
    "Total_Debt_B",
    "Cash_B",
    "Net_Debt_B",
    "Maintenance_Real_FCF_B",
    "Conservative_Real_FCF_B",
    "ICR",
    "Stress_ICR_30x",
    "NetDebt_to_Stress_EBITDA_30x",
    "Stress_Real_FCF_30_B",
    "ROIC_pct",
    "ROIC_Average_Capital_pct",
    "ROIC_Ending_Capital_pct",
    "ROIC_Including_Goodwill_pct",
    "ROIC_Excluding_Goodwill_pct",
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
    "DSI_Latest",
    "DSI_Score",
    "Inventory_to_Revenue_pct",
    "Inventory_to_Assets_pct",
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
    "Historical_Valuation_Quantile_Used",
    "Historical_Valuation_Quantile_Value",
    "Exit_Multiple_Company_History",
    "Exit_Multiple_Peer",
    "Exit_Multiple_Rate_Adjusted",
    "Exit_Multiple_Final",
    "Point_in_Time_FX_Rate",
    "ADR_Ratio",
    "EBITDA_Drawdown_30_pct",
    "Company_Reported_Combined_Ratio",
    "SEC_Combined_Ratio_Proxy",
    "Combined_Ratio_Reconciliation_Difference_pp",
    "Accident_Year_Combined_Ratio",
    "Prior_Year_Reserve_Development_pct",
    "Catastrophe_Loss_Ratio_pct",
    "Premium_Growth_pct",
    "Policy_Count_Growth_pct",
    "Investment_Income_B",
    "Investment_Yield_pct",
    "Equity_to_Assets_pct",
    "Operating_ROE_pct",
    "Price_to_Book_x",
    "P_and_C_Stress_CR_Mild",
    "P_and_C_Stress_CR_Moderate",
    "P_and_C_Stress_CR_Severe",
    "P_and_C_Stress_Underwriting_Income_Mild_B",
    "P_and_C_Stress_Underwriting_Income_Moderate_B",
    "P_and_C_Stress_Underwriting_Income_Severe_B",
    "P_and_C_Stress_PreTax_Income_Moderate_B",
    "P_and_C_Stress_ROE_Moderate_pct",
    "P_and_C_Stress_Equity_to_Assets_Moderate_pct",
)


P_AND_C_METRICS = frozenset(
    metric
    for metric in DISPLAY_METRICS
    if metric.startswith("P_and_C_")
    or metric
    in {
        "Company_Reported_Combined_Ratio",
        "SEC_Combined_Ratio_Proxy",
        "Combined_Ratio_Reconciliation_Difference_pp",
        "Accident_Year_Combined_Ratio",
        "Prior_Year_Reserve_Development_pct",
        "Catastrophe_Loss_Ratio_pct",
        "Premium_Growth_pct",
        "Policy_Count_Growth_pct",
        "Investment_Income_B",
        "Investment_Yield_pct",
        "Equity_to_Assets_pct",
        "Operating_ROE_pct",
        "Price_to_Book_x",
    }
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
    "GENERAL_CORPORATE": (frozenset(DISPLAY_METRICS) - P_AND_C_METRICS) - {
        "Industry_Model_Score",
        "Industry_Model_Coverage",
    },
    "BANK": COMMON_SPECIALIZED_METRICS,
    "INSURANCE_P_AND_C": COMMON_SPECIALIZED_METRICS | P_AND_C_METRICS,
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
        "Maintenance_Real_FCF_B",
        "Conservative_Real_FCF_B",
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
        "Maintenance_CapEx_Lower_B",
        "Maintenance_CapEx_Base_B",
        "Maintenance_CapEx_Upper_B",
        "Growth_CapEx_B",
        "Maintenance_Real_FCF_B",
        "Real_FCF_Yield_pct",
        "Maintenance_Real_FCF_to_MarketCap_Yield_pct",
        "Maintenance_Real_FCF_to_EV_Yield_pct",
        "Maintenance_Real_FCF_Yield_Low_pct",
        "Maintenance_Real_FCF_Yield_High_pct",
        "Maintenance_Real_FCF_Yield_Lower_pct",
        "Maintenance_Real_FCF_Yield_Base_pct",
        "Maintenance_Real_FCF_Yield_Upper_pct",
        "FCF_Sensitivity_Spread_pp",
        "Stress_ICR_30x",
        "NetDebt_to_Stress_EBITDA_30x",
        "Stress_Real_FCF_30_B",
        "Implied_EBITDA_CAGR_3Y_pct",
        "Implied_CAGR_Limit_pct",
        "Implied_CAGR_Headroom_pct",
        "EBITDA_Drawdown_30_pct",
        "SEC_Combined_Ratio_Proxy",
        "P_and_C_Stress_CR_Mild",
        "P_and_C_Stress_CR_Moderate",
        "P_and_C_Stress_CR_Severe",
        "P_and_C_Stress_Underwriting_Income_Mild_B",
        "P_and_C_Stress_Underwriting_Income_Moderate_B",
        "P_and_C_Stress_Underwriting_Income_Severe_B",
        "P_and_C_Stress_PreTax_Income_Moderate_B",
        "P_and_C_Stress_ROE_Moderate_pct",
        "P_and_C_Stress_Equity_to_Assets_Moderate_pct",
    }
)


CSV_STATUS_METRICS = (
    "Real_FCF_Yield_pct",
    "Conservative_Real_FCF_Yield_pct",
    "Maintenance_CapEx_B",
    "Growth_CapEx_B",
    "Maintenance_Real_FCF_B",
    "Conservative_Real_FCF_B",
    "ROIC_pct",
    "ROIC_Average_Capital_pct",
    "ROIC_Ending_Capital_pct",
    "ROIC_Including_Goodwill_pct",
    "ROIC_Excluding_Goodwill_pct",
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
    "Maintenance_Real_FCF_B": ("Real_FCF",),
    "Conservative_Real_FCF_B": ("Conservative_Real_FCF",),
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
            ßmz¶‰žËkºwµçt(€€€ô4(4(4)‘•˜}©Í½¹}½‰©•Ð¡É…Üè¹ä¤€´ø‘¥ÑmÍÑÈ°¹åtè4(€€€¥˜¥Í¥¹ÍÑ…¹”¡É…Ü°‘¥Ð¤è4(€€€€€€€É•ÑÕÉ¸É…Ü4(€€€ÑÉäè4(€€€€€€€Á…ÉÍ•€ô©Í½¸¹±½…‘Ì¡ÍÑÈ¡É…Ü½È€‰íôˆ¤¤4(€€€•á•ÁÐ€¡QåÁ•ÉÉ½È°Y…±Õ•ÉÉ½È°©Í½¸¹)M=9•½‘•ÉÉ½È¤è4(€€€€€€€É•ÑÕÉ¸íô4(€€€É•ÑÕÉ¸Á…ÉÍ•¥˜¥Í¥¹ÍÑ…¹”¡Á…ÉÍ•°‘¥Ð¤•±Í”íô4(4(4)‘•˜}ÍÁ•¥…±¥é•‘}µ•Ñ…‘…Ñ„ 4(€€€É½Üè5…ÁÁ¥¹mÍÑÈ°¹åt°4(€€€¥¹‘•àè5…ÁÁ¥¹mÑÕÁ±•mÍÑÈ°ÍÑÉt°M•ÅÕ•¹•m5…ÁÁ¥¹mÍÑÈ°¹åuut°4(¤€´ø‘¥ÑmÍÑÈ°‘¥ÑmÍÑÈ°¹åutè4(€€€­•ä€ôµ½‘•±}­•ä¡É½Ü¤4(€€€¥˜­•ä€ôô€‰9I1}=IA=IQˆè4(€€€€€€€É•ÑÕÉ¸íô4(€€€Ñ¥­•È€ôÍÑÈ¡É½Ü¹•Ð ‰Q¥­•Èˆ¤½È€ˆˆ¤¹ÕÁÁ•È ¤4(€€€‘•¥Í¥½¹}…Ð€ôÉ½Ü¹•Ð ‰•¥Í¥½¹}Q¥µ•ÍÑ…µÀˆ¤4(€€€½ÕÑÁÕÐè‘¥ÑmÍÑÈ°‘¥ÑmÍÑÈ°¹åut€ôíô4(€€€™½È¹…µ•ÍÁ…”°É…Ý}™¥•±¥¸€ 4(€€€€€€€€ ‰¥¹‘ÕÍÑÉäˆ°€‰%¹‘ÕÍÑÉå}5½‘•±}5•ÑÉ¥Í})M=8ˆ¤°4(€€€€€€€€ ‰¥¹‘ÕÍÑÉå}½µÁ½¹•¹Ðˆ°€‰%¹‘ÕÍÑÉå}5½‘•±}½µÁ½¹•¹ÑÍ})M=8ˆ¤°4(€€€€¤è4(€€€€€€€™½È¹…µ”°É…Ý}Ù…±Õ”¥¸}©Í½¹}½‰©•Ð¡É½Ü¹•Ð¡É…Ý}™¥•±¤¤¹¥Ñ•µÌ ¤è4(€€€€€€€€€€€µ•ÑÉ¥}¹…µ”€ô˜‰í¹…µ•ÍÁ…•ô¹í¹…µ•ôˆ4(€€€€€€€€€€€Ù…±Õ”€ô±•…¹}Ù…±Õ”¡É…Ý}Ù…±Õ”¤4(€€€€€€€€€€€É•½É‘Ì€ô±¥ÍÐ¡¥¹‘•à¹•Ð ¡Ñ¥­•È°˜‰í­•åôéí¹…µ•ôˆ¤°mt¤¤4(€€€€€€€€€€€•Ù¥‘•¹•}¥‘Ì€ômÍÑÈ¡É•½É¹•Ð ‰•Ù¥‘•¹•}¥ˆ¤¤™½ÈÉ•½É¥¸É•½É‘Ít4(€€€€€€€€€€€¥˜Ù…±Õ”¥Ì9½¹”è(€€€€€€€€€€€€€€€ÍÑ…ÑÕÌ€ô€‰5%MM%9ˆ4(€€€€€€€€€€€€€€€É•…Í½¸€ô˜‰í­•åô‘¥¹½ÐÁÉ½‘Õ”í¹…µ•ôˆ4(€€€€€€€€€€€•±Í”è(€€€€€€€€€€€€€€€Õ¹É•½¹¥±•‘}Á}…¹‘}Œ€ô‰½½° (€€€€€€€€€€€€€€€€€€€­•ä€ôô€‰%9MUI9}A}9}ˆ(€€€€€€€€€€€€€€€€€€€…¹ÍÑÈ¡É½Ü¹•Ð ‰½µ‰¥¹•‘}I…Ñ¥½}M½ÕÉ•}MÑ…ÑÕÌˆ¤½È€ˆˆ¤¹ÕÁÁ•È ¤(€€€€€€€€€€€€€€€€€€€€ôô€‰M}AI=ae}U9I=9%1ˆ(€€€€€€€€€€€€€€€€€€€…¹€‰½µ‰¥¹•‘}É…Ñ¥¼ˆ¥¸¹…µ”¹±½Ý•È ¤(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€ÍÑ…ÑÕÌ€ô€ (€€€€€€€€€€€€€€€€€€€€‰MQ%5Qˆ(€€€€€€€€€€€€€€€€€€€¥˜Õ¹É•½¹¥±•‘}Á}…¹‘}Œ(€€€€€€€€€€€€€€€€€€€½È…¹ä¡Ñ½­•¸¥¸¹…µ”¹±½Ý•È ¤™½ÈÑ½­•¸¥¸€ ‰ÁÉ½áäˆ°€‰µ¥‘å±”ˆ°€‰ÑÉ½Õ ˆ°€‰ÍÑÉ•ÍÌˆ¤¤(€€€€€€€€€€€€€€€€€€€•±Í”€‰Y1%ˆ(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€É•…Í½¸€ô˜‰í­•åô‘•‘¥…Ñ•µ•ÑÉ¥Œˆ4(€€€€€€€€€€€½ÕÑÁÕÑmµ•ÑÉ¥}¹…µ•t€ôì4(€€€€€€€€€€€€€€€€‰Ù…±Õ”ˆèÙ…±Õ”°4(€€€€€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆèÍÑ…ÑÕÌ°4(€€€€€€€€€€€€€€€€‰É•…Í½¸ˆèÉ•…Í½¸°4(€€€€€€€€€€€€€€€€‰…Í}½˜ˆè}…Í}½˜¡É•½É‘Ì°‘•¥Í¥½¹}…Ð¤°4(€€€€€€€€€€€€€€€€‰Í½ÕÉ•}µ•Ñ¡½ˆè˜‰¥¹‘ÕÍÑÉå}µ½‘•°éí­•åôˆ°4(€€€€€€€€€€€€€€€€‰•Ù¥‘•¹•}¥‘Ìˆè•Ù¥‘•¹•}¥‘Ì°4(€€€€€€€€€€€ô4(€€€É•ÑÕÉ¸½ÕÑÁÕÐ4(4(4)‘•˜…¹¹½Ñ…Ñ•}É½ÝÌ 4(€€€É½ÝÌèM•ÅÕ•¹•m5…ÁÁ¥¹mÍÑÈ°¹åut°4(€€€•Ù¥‘•¹•}É½ÝÌè%Ñ•É…‰±•m5…ÁÁ¥¹mÍÑÈ°¹åut°4(¤€´ø±¥ÍÑm‘¥ÑmÍÑÈ°¹åutè4(€€€¥¹‘•à€ô}•Ù¥‘•¹•}¥¹‘•à¡•Ù¥‘•¹•}É½ÝÌ¤4(€€€…¹¹½Ñ…Ñ•è±¥ÍÑm‘¥ÑmÍÑÈ°¹åut€ômt(€€€™½ÈÍ½ÕÉ”¥¸É½ÝÌè(€€€€€€€É½Ü€ô‘¥Ð¡Í½ÕÉ”¤(€€€€€€€­•ä€ôµ½‘•±}­•ä¡É½Ü¤(€€€€€€€¥˜­•ä€ôô€‰%9MUI9}A}9}ˆè(€€€€€€€€€€€¥¹‘ÕÍÑÉå}µ•ÑÉ¥Ì€ô}©Í½¹}½‰©•Ð¡É½Ü¹•Ð ‰%¹‘ÕÍÑÉå}5½‘•±}5•ÑÉ¥Í})M=8ˆ¤¤(€€€€€€€€€€€Á}…¹‘}}µ…ÁÁ¥¹œ€ôì(€€€€€€€€€€€€€€€€‰½µÁ…¹å}I•Á½ÉÑ•‘}½µ‰¥¹•‘}I…Ñ¥¼ˆè€‰½µÁ…¹å}É•Á½ÉÑ•‘}½µ‰¥¹•‘}É…Ñ¥½}ÁÐˆ°(€€€€€€€€€€€€€€€€‰M}½µ‰¥¹•‘}I…Ñ¥½}AÉ½áäˆè€‰Í•}½µ‰¥¹•‘}É…Ñ¥½}ÁÉ½áå}ÁÐˆ°(€€€€€€€€€€€€€€€€‰½µ‰¥¹•‘}I…Ñ¥½}I•½¹¥±¥…Ñ¥½¹}¥™™•É•¹•}ÁÀˆè€‰½µ‰¥¹•‘}É…Ñ¥½}É•½¹¥±¥…Ñ¥½¹}‘¥™™•É•¹•}ÁÀˆ°(€€€€€€€€€€€€€€€€‰¥‘•¹Ñ}e•…É}½µ‰¥¹•‘}I…Ñ¥¼ˆè€‰…¥‘•¹Ñ}å•…É}½µ‰¥¹•‘}É…Ñ¥½}ÁÐˆ°(€€€€€€€€€€€€€€€€‰AÉ¥½É}e•…É}I•Í•ÉÙ•}•Ù•±½Áµ•¹Ñ}ÁÐˆè€‰É•Í•ÉÙ•}‘•Ù•±½Áµ•¹Ñ}Ñ½}ÁÉ•µ¥Õµ}ÁÐˆ°(€€€€€€€€€€€€€€€€‰…Ñ…ÍÑÉ½Á¡•}1½ÍÍ}I…Ñ¥½}ÁÐˆè€‰…Ñ…ÍÑÉ½Á¡•}±½ÍÍ}É…Ñ¥½}ÁÐˆ°(€€€€€€€€€€€€€€€€‰AÉ•µ¥Õµ}É½ÝÑ¡}ÁÐˆè€‰ÁÉ•µ¥Õµ}É½ÝÑ¡}ÁÐˆ°(€€€€€€€€€€€€€€€€‰A½±¥å}½Õ¹Ñ}É½ÝÑ¡}ÁÐˆè€‰Á½±¥å}½Õ¹Ñ}É½ÝÑ¡}ÁÐˆ°(€€€€€€€€€€€€€€€€‰%¹Ù•ÍÑµ•¹Ñ}%¹½µ•}ˆè€‰¹•Ñ}¥¹Ù•ÍÑµ•¹Ñ}¥¹½µ•}ÑÑµ}ˆˆ°(€€€€€€€€€€€€€€€€‰%¹Ù•ÍÑµ•¹Ñ}e¥•±‘}ÁÐˆè€‰¥¹Ù•ÍÑµ•¹Ñ}å¥•±‘}ÁÐˆ°(€€€€€€€€€€€€€€€€‰ÅÕ¥Ñå}Ñ½}ÍÍ•ÑÍ}ÁÐˆè€‰•ÅÕ¥Ñå}Ñ½}…ÍÍ•ÑÍ}ÁÐˆ°(€€€€€€€€€€€€€€€€‰=Á•É…Ñ¥¹}I=}ÁÐˆè€‰½Á•É…Ñ¥¹}É½•}ÁÐˆ°(€€€€€€€€€€€€€€€€‰AÉ¥•}Ñ½}	½½­}àˆè€‰ÁÉ¥•}Ñ½}‰½½­}àˆ°(€€€€€€€€€€€ô(€€€€€€€€€€€™½È½ÕÑÁÕÑ}¹…µ”°µ•ÑÉ¥}¹…µ”¥¸Á}…¹‘}}µ…ÁÁ¥¹œ¹¥Ñ•µÌ ¤è(€€€€€€€€€€€€€€€¥˜™¥¹¥Ñ•}¹Õµ‰•È¡É½Ü¹•Ð¡½ÕÑÁÕÑ}¹…µ”¤¤¥Ì9½¹”è(€€€€€€€€€€€€€€€€€€€É½Ým½ÕÑÁÕÑ}¹…µ•t€ô¥¹‘ÕÍÑÉå}µ•ÑÉ¥Ì¹•Ð¡µ•ÑÉ¥}¹…µ”¤(€€€€€€€€€€€¥˜™¥¹¥Ñ•}¹Õµ‰•È¡É½Ü¹•Ð ‰M}½µ‰¥¹•‘}I…Ñ¥½}AÉ½áäˆ¤¤¥Ì9½¹”è(€€€€€€€€€€€€€€€É½Ýl‰M}½µ‰¥¹•‘}I…Ñ¥½}AÉ½áä‰t€ô¥¹‘ÕÍÑÉå}µ•ÑÉ¥Ì¹•Ð (€€€€€€€€€€€€€€€€€€€€‰½µ‰¥¹•‘}É…Ñ¥½}ÁÉ½áå}ÁÐˆ(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€½µÁ…¹å}É…Ñ¥¼€ô™¥¹¥Ñ•}¹Õµ‰•È¡É½Ü¹•Ð ‰½µÁ…¹å}I•Á½ÉÑ•‘}½µ‰¥¹•‘}I…Ñ¥¼ˆ¤¤(€€€€€€€€€€€ÁÉ½áå}É…Ñ¥¼€ô™¥¹¥Ñ•}¹Õµ‰•È¡É½Ü¹•Ð ‰M}½µ‰¥¹•‘}I…Ñ¥½}AÉ½áäˆ¤¤(€€€€€€€€€€€Í½ÕÉ•}ÍÑ…ÑÕÌ€ôÍÑÈ (€€€€€€€€€€€€€€€É½Ü¹•Ð ‰½µ‰¥¹•‘}I…Ñ¥½}M½ÕÉ•}MÑ…ÑÕÌˆ¤(€€€€€€€€€€€€€€€½È¥¹‘ÕÍÑÉå}µ•ÑÉ¥Ì¹•Ð ‰½µ‰¥¹•‘}É…Ñ¥½}Í½ÕÉ•}ÍÑ…ÑÕÌˆ¤(€€€€€€€€€€€€€€€½È€ˆˆ(€€€€€€€€€€€€¤¹ÕÁÁ•È ¤(€€€€€€€€€€€¥˜¹½ÐÍ½ÕÉ•}ÍÑ…ÑÕÌè(€€€€€€€€€€€€€€€Í½ÕÉ•}ÍÑ…ÑÕÌ€ô€ (€€€€€€€€€€€€€€€€€€€€‰=5A9e}IA=IQˆ(€€€€€€€€€€€€€€€€€€€¥˜½µÁ…¹å}É…Ñ¥¼¥Ì¹½Ð9½¹”(€€€€€€€€€€€€€€€€€€€•±Í”€‰M}AI=ae}U9I=9%1ˆ(€€€€€€€€€€€€€€€€€€€¥˜ÁÉ½áå}É…Ñ¥¼¥Ì¹½Ð9½¹”(€€€€€€€€€€€€€€€€€€€•±Í”€‰5%MM%9ˆ(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€É½Ýl‰½µ‰¥¹•‘}I…Ñ¥½}M½ÕÉ•}MÑ…ÑÕÌ‰t€ôÍ½ÕÉ•}ÍÑ…ÑÕÌ(€€€€€€€€€€€¥˜Í½ÕÉ•}ÍÑ…ÑÕÌ€ôô€‰M}AI=ae}U9I=9%1ˆè(€€€€€€€€€€€€€€€É½Ýl‰!Õµ…¹}-A%}I•Ù¥•Ý}I•ÅÕ¥É•‰t€ôQÉÕ”(€€€€€€€€€€€¥˜¹½ÐÍÑÈ¡É½Ü¹•Ð ‰A}…¹‘}}MÑÉ•ÍÍ}MÑ…ÑÕÌˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤è(€€€€€€€€€€€€€€€É½Ýl‰A}…¹‘}}MÑÉ•ÍÍ}MÑ…ÑÕÌ‰t€ô€‰	MQ%8ˆ(€€€€€€€€€€€¥˜ÍÑÈ¡É½Ü¹•Ð ‰A}…¹‘}}MÑÉ•ÍÍ}MÑ…ÑÕÌˆ¤¤¹ÕÁÁ•È ¤€„ô€‰AMLˆè(€€€€€€€€€€€€€€€É½Ýl‰MÁ•¥…±¥é•‘}MÑÉ•ÍÍ}A•¹‘¥¹œ‰t€ôQÉÕ”(€€€€€€€€€€€€€€€É½Ýl‰MÑ…ÉÑ•É}…¹‘¥‘…Ñ”‰t€ô…±Í”(€€€€€€€¥˜­•ä€ôô€‰9I1}=IA=IQˆè(€€€€€€€€€€€É½Ýl‰5…¥¹Ñ•¹…¹•}…Áá}1½Ý•É}‰t€ôÉ½Ü¹•Ð ‰5…¥¹Ñ•¹…¹•}…Áá}1½Ý}ˆ¤(€€€€€€€€€€€É½Ýl‰5…¥¹Ñ•¹…¹•}…Áá}	…Í•}‰t€ôÉ½Ü¹•Ð ‰5…¥¹Ñ•¹…¹•}…Áá}ˆ¤(€€€€€€€€€€€É½Ýl‰5…¥¹Ñ•¹…¹•}…Áá}UÁÁ•É}‰t€ôÉ½Ü¹•Ð ‰5…¥¹Ñ•¹…¹•}…Áá}!¥¡}ˆ¤(€€€€€€€€€€€É½Ýl‰5…¥¹Ñ•¹…¹•}…Áá}5•Ñ¡½‰t€ôÉ½Ü¹•Ð ‰5…¥¹Ñ•¹…¹•}…Áá}5•Ñ¡½ˆ¤½ÈÉ½Ü¹•Ð ‰…Áá}I•¥¹Ù•ÍÑµ•¹Ñ}5•Ñ¡½ˆ¤(€€€€€€€€€€€É½Ýl‰5…¥¹Ñ•¹…¹•}I•…±}}e¥•±‘}1½Ý•É}ÁÐ‰t€ôÉ½Ü¹•Ð ‰5…¥¹Ñ•¹…¹•}I•…±}}e¥•±‘}1½Ý}ÁÐˆ¤(€€€€€€€€€€€É½Ýl‰5…¥¹Ñ•¹…¹•}I•…±}}e¥•±‘}	…Í•}ÁÐ‰t€ôÉ½Ü¹•Ð ‰I•…±}}e¥•±‘}ÁÐˆ¤(€€€€€€€€€€€É½Ýl‰5…¥¹Ñ•¹…¹•}I•…±}}e¥•±‘}UÁÁ•É}ÁÐ‰t€ôÉ½Ü¹•Ð ‰5…¥¹Ñ•¹…¹•}I•…±}}e¥•±‘}!¥¡}ÁÐˆ¤(€€€€€€€€€€€±½Ý•É}å¥•±€ô™¥¹¥Ñ•}¹Õµ‰•È¡É½Ü¹•Ð ‰5…¥¹Ñ•¹…¹•}I•…±}}e¥•±‘}1½Ý•É}ÁÐˆ¤¤(€€€€€€€€€€€ÕÁÁ•É}å¥•±€ô™¥¹¥Ñ•}¹Õµ‰•È¡É½Ü¹•Ð ‰5…¥¹Ñ•¹…¹•}I•…±}}e¥•±‘}UÁÁ•É}ÁÐˆ¤¤(€€€€€€€€€€€É½Ýl‰}M•¹Í¥Ñ¥Ù¥Ñå}MÁÉ•…‘}ÁÀ‰t€ô€ (€€€€€€€€€€€€€€€ÕÁÁ•É}å¥•±€´±½Ý•É}å¥•±(€€€€€€€€€€€€€€€¥˜±½Ý•É}å¥•±¥Ì¹½Ð9½¹”…¹ÕÁÁ•É}å¥•±¥Ì¹½Ð9½¹”(€€€€€€€€€€€€€€€•±Í”9½¹”(€€€€€€€€€€€€¤(€€€€€€€€€€€É½Ýl‰M	}½¹½µ¥}½ÍÐ‰t€ôÉ½Ü¹•Ð ‰M	}½¹½µ¥}½ÍÑ}ˆ¤(€€€€€€€€€€€½˜€ô™¥¹¥Ñ•}¹Õµ‰•È¡É½Ü¹•Ð ‰QQ5}=}ˆ¤¤(€€€€€€€€€€€Ñ½Ñ…±}…Á•à€ô™¥¹¥Ñ•}¹Õµ‰•È¡É½Ü¹•Ð ‰å¹…µ¥}…Áá}ˆ¤¤(€€€€€€€€€€€µ…¥¹Ñ•¹…¹•}…Á•à€ô™¥¹¥Ñ•}¹Õµ‰•È¡É½Ü¹•Ð ‰5…¥¹Ñ•¹…¹•}…Áá}ˆ¤¤(€€€€€€€€€€€Í‰Œ€ô™¥¹¥Ñ•}¹Õµ‰•È¡É½Ü¹•Ð ‰QQ5}M	}ˆ¤¤(€€€€€€€€€€€¥˜…±° (€€€€€€€€€€€€€€€Ù…±Õ”¥Ì¹½Ð9½¹”(€€€€€€€€€€€€€€€™½ÈÙ…±Õ”¥¸€¡½˜°Ñ½Ñ…±}…Á•à°µ…¥¹Ñ•¹…¹•}…Á•à°Í‰Œ¤(€€€€€€€€€€€€¤è(€€€€€€€€€€€€€€€¥˜™¥¹¥Ñ•}¹Õµ‰•È¡É½Ü¹•Ð ‰5…¥¹Ñ•¹…¹•}I•…±}}ˆ¤¤¥Ì9½¹”è(€€€€€€€€€€€€€€€€€€€É½Ýl‰5…¥¹Ñ•¹…¹•}I•…±}}‰t€ô½˜€´µ…¥¹Ñ•¹…¹•}…Á•à€´Í‰Œ(€€€€€€€€€€€€€€€¥˜™¥¹¥Ñ•}¹Õµ‰•È¡É½Ü¹•Ð ‰½¹Í•ÉÙ…Ñ¥Ù•}I•…±}}ˆ¤¤¥Ì9½¹”è(€€€€€€€€€€€€€€€€€€€É½Ýl‰½¹Í•ÉÙ…Ñ¥Ù•}I•…±}}‰t€ô½˜€´Ñ½Ñ…±}…Á•à€´Í‰Œ(€€€€€€€€€€€€€€€µ…¥¹Ñ•¹…¹•}™˜€ô™¥¹¥Ñ•}¹Õµ‰•È¡É½Ü¹•Ð ‰5…¥¹Ñ•¹…¹•}I•…±}}ˆ¤¤(€€€€€€€€€€€€€€€½¹Í•ÉÙ…Ñ¥Ù•}™˜€ô™¥¹¥Ñ•}¹Õµ‰•È¡É½Ü¹•Ð ‰½¹Í•ÉÙ…Ñ¥Ù•}I•…±}}ˆ¤¤(€€€€€€€€€€€€€€€µ…É­•Ñ}…À€ô™¥¹¥Ñ•}¹Õµ‰•È¡É½Ü¹•Ð ‰5…É­•Ñ…Á}ˆ¤¤(€€€€€€€€€€€€€€€•¹Ñ•ÉÁÉ¥Í•}Ù…±Õ”€ô™¥¹¥Ñ•}¹Õµ‰•È¡É½Ü¹•Ð ‰Y}ˆ¤¤(€€€€€€€€€€€€€€€¥˜µ…É­•Ñ}…À¥Ì¹½Ð9½¹”…¹µ…É­•Ñ}…À€ø€Àè(€€€€€€€€€€€€€€€€€€€µ…¥¹Ñ•¹…¹•}µ…É­•Ñ}…Á}å¥•±€ôµ…¥¹Ñ•¹…¹•}™˜€¼µ…É­•Ñ}…À€¨€ÄÀÀ(€€€€€€€€€€€€€€€€€€€½¹Í•ÉÙ…Ñ¥Ù•}µ…É­•Ñ}…Á}å¥•±€ô½¹Í•ÉÙ…Ñ¥Ù•}™˜€¼µ…É­•Ñ}…À€¨€ÄÀÀ(€€€€€€€€€€€€€€€€€€€É½Ýl‰I•…±}}e¥•±‘}ÁÐ‰t€ôµ…¥¹Ñ•¹…¹•}µ…É­•Ñ}…Á}å¥•±(€€€€€€€€€€€€€€€€€€€É½Ýl‰½¹Í•ÉÙ…Ñ¥Ù•}I•…±}}e¥•±‘}ÁÐ‰t€ô½¹Í•ÉÙ…Ñ¥Ù•}µ…É­•Ñ}…Á}å¥•±(€€€€€€€€€€€€€€€€€€€É½Ýl‰5…¥¹Ñ•¹…¹•}I•…±}}Ñ½}5…É­•Ñ…Á}e¥•±‘}ÁÐ‰t€ôµ…¥¹Ñ•¹…¹•}µ…É­•Ñ}…Á}å¥•±(€€€€€€€€€€€€€€€€€€€É½Ýl‰½¹Í•ÉÙ…Ñ¥Ù•}I•…±}}Ñ½}5…É­•Ñ…Á}e¥•±‘}ÁÐ‰t€ô½¹Í•ÉÙ…Ñ¥Ù•}µ…É­•Ñ}…Á}å¥•±(€€€€€€€€€€€€€€€¥˜•¹Ñ•ÉÁÉ¥Í•}Ù…±Õ”¥Ì¹½Ð9½¹”…¹•¹Ñ•ÉÁÉ¥Í•}Ù…±Õ”€ø€Àè(€€€€€€€€€€€€€€€€€€€¥˜™¥¹¥Ñ•}¹Õµ‰•È¡É½Ü¹•Ð ‰5…¥¹Ñ•¹…¹•}I•…±}}Ñ½}Y}e¥•±‘}ÁÐˆ¤¤¥Ì9½¹”è(€€€€€€€€€€€€€€€€€€€€€€€É½Ýl‰5…¥¹Ñ•¹…¹•}I•…±}}Ñ½}Y}e¥•±‘}ÁÐ‰t€ôµ…¥¹Ñ•¹…¹•}™˜€¼•¹Ñ•ÉÁÉ¥Í•}Ù…±Õ”€¨€ÄÀÀ(€€€€€€€€€€€€€€€€€€€¥˜™¥¹¥Ñ•}¹Õµ‰•È¡É½Ü¹•Ð ‰½¹Í•ÉÙ…Ñ¥Ù•}I•…±}}Ñ½}Y}e¥•±‘}ÁÐˆ¤¤¥Ì9½¹”è(€€€€€€€€€€€€€€€€€€€€€€€É½Ýl‰½¹Í•ÉÙ…Ñ¥Ù•}I•…±}}Ñ½}Y}e¥•±‘}ÁÐ‰t€ô½¹Í•ÉÙ…Ñ¥Ù•}™˜€¼•¹Ñ•ÉÁÉ¥Í•}Ù…±Õ”€¨€ÄÀÀ(€€€€€€€µ•Ñ…‘…Ñ„€ôì(€€€€€€€€€€€µ•ÑÉ¥Œè}µ•ÑÉ¥}µ•Ñ…‘…Ñ„¡É½Ü°µ•ÑÉ¥Œ°¥¹‘•à¤4(€€€€€€€€€€€™½Èµ•ÑÉ¥Œ¥¸%MA1e}5QI%L4(€€€€€€€ô4(€€€€€€€µ•Ñ…‘…Ñ„¹ÕÁ‘…Ñ”¡}ÍÁ•¥…±¥é•‘}µ•Ñ…‘…Ñ„¡É½Ü°¥¹‘•à¤¤4(€€€€€€€™½Èµ•ÑÉ¥Œ¥¸%MA1e}5QI%Lè4(€€€€€€€€€€€É½Ýmµ•ÑÉ¥t€ôµ•Ñ…‘…Ñ…mµ•ÑÉ¥ul‰Ù…±Õ”‰t4(€€€€€€€™½Èµ•ÑÉ¥Œ¥¸MY}MQQUM}5QI%Lè4(€€€€€€€€€€€µ•Ñ„€ôµ•Ñ…‘…Ñ…mµ•ÑÉ¥t4(€€€€€€€€€€€É½Ým˜‰íµ•ÑÉ¥õ}MÑ…ÑÕÌ‰t€ôµ•Ñ…l‰ÍÑ…ÑÕÌ‰t4(€€€€€€€€€€€É½Ým˜‰íµ•ÑÉ¥õ}I•…Í½¸‰t€ôµ•Ñ…l‰É•…Í½¸‰t4(€€€€€€€€€€€É½Ým˜‰íµ•ÑÉ¥õ}Í=˜‰t€ôµ•Ñ…l‰…Í}½˜‰t4(€€€€€€€€€€€É½Ým˜‰íµ•ÑÉ¥õ}M½ÕÉ•}5•Ñ¡½‰t€ôµ•Ñ…l‰Í½ÕÉ•}µ•Ñ¡½‰t4(€€€€€€€É•ÅÕ¥É•€ôÉ•ÅÕ¥É•‘}µ•ÑÉ¥Ì¡­•ä¤4(€€€€€€€µ¥ÍÍ¥¹}É•ÅÕ¥É•€ôÍ½ÉÑ• (€€€€€€€€€€€µ•ÑÉ¥Œ(€€€€€€€€€€€™½Èµ•ÑÉ¥Œ¥¸É•ÅÕ¥É•(€€€€€€€€€€€¥˜µ•Ñ…‘…Ñ…mµ•ÑÉ¥ul‰ÍÑ…ÑÕÌ‰t¥¸9U11}MQQUML(€€€€€€€€€€€…¹µ•Ñ…‘…Ñ…mµ•ÑÉ¥ul‰ÍÑ…ÑÕÌ‰t€„ô€‰9=Q}AA1%	1ˆ(€€€€€€€€¤(€€€€€€€¥¹‘ÕÍÑÉå}É•ÅÕ¥É•‘}µ¥ÍÍ¥¹œ€ôl(€€€€€€€€€€€¥Ñ•´¹ÍÑÉ¥À ¤(€€€€€€€€€€€™½È¥Ñ•´¥¸ÍÑÈ¡É½Ü¹•Ð ‰%¹‘ÕÍÑÉå}5½‘•±}I•ÅÕ¥É•‘}5¥ÍÍ¥¹œˆ¤½È€ˆˆ¤¹ÍÁ±¥Ð ‰ðˆ¤(€€€€€€€€€€€¥˜¥Ñ•´¹ÍÑÉ¥À ¤(€€€€€€€t(€€€€€€€µ¥ÍÍ¥¹}É•ÅÕ¥É•€ôÍ½ÉÑ• (€€€€€€€€€€€Í•Ð¡µ¥ÍÍ¥¹}É•ÅÕ¥É•¤(€€€€€€€€€€€ðí˜‰¥¹‘ÕÍÑÉä¹í¥Ñ•µôˆ™½È¥Ñ•´¥¸¥¹‘ÕÍÑÉå}É•ÅÕ¥É•‘}µ¥ÍÍ¥¹ô(€€€€€€€€¤(€€€€€€€½ÁÑ¥½¹…±}µ¥ÍÍ¥¹œ€ôÍ½ÉÑ• (€€€€€€€€€€€µ•ÑÉ¥Œ(€€€€€€€€€€€™½Èµ•ÑÉ¥Œ¥¸…ÁÁ±¥…‰±•}µ•ÑÉ¥Ì¡­•ä¤€´É•ÅÕ¥É•(€€€€€€€€€€€¥˜µ•Ñ…‘…Ñ…mµ•ÑÉ¥ul‰ÍÑ…ÑÕÌ‰t¥¸9U11}MQQUML4(€€€€€€€€€€€…¹µ•Ñ…‘…Ñ…mµ•ÑÉ¥ul‰ÍÑ…ÑÕÌ‰t€„ô€‰9=Q}AA1%	1ˆ(€€€€€€€€¤(€€€€€€€¥¹‘ÕÍÑÉå}½ÁÑ¥½¹…±}µ¥ÍÍ¥¹œ€ôl(€€€€€€€€€€€¥Ñ•´¹ÍÑÉ¥À ¤(€€€€€€€€€€€™½È¥Ñ•´¥¸ÍÑÈ¡É½Ü¹•Ð ‰%¹‘ÕÍÑÉå}5½‘•±}=ÁÑ¥½¹…±}5¥ÍÍ¥¹œˆ¤½È€ˆˆ¤¹ÍÁ±¥Ð ‰ðˆ¤(€€€€€€€€€€€¥˜¥Ñ•´¹ÍÑÉ¥À ¤(€€€€€€€t(€€€€€€€½ÁÑ¥½¹…±}µ¥ÍÍ¥¹œ€ôÍ½ÉÑ• (€€€€€€€€€€€Í•Ð¡½ÁÑ¥½¹…±}µ¥ÍÍ¥¹œ¤(€€€€€€€€€€€ðí˜‰¥¹‘ÕÍÑÉä¹í¥Ñ•µôˆ™½È¥Ñ•´¥¸¥¹‘ÕÍÑÉå}½ÁÑ¥½¹…±}µ¥ÍÍ¥¹ô(€€€€€€€€¤(€€€€€€€…ÁÁ±¥…‰±•}¹…µ•Ì€ôÍ•Ð¡…ÁÁ±¥…‰±•}µ•ÑÉ¥Ì¡­•ä¤¤ðì(€€€€€€€€€€€µ•ÑÉ¥Œ(€€€€€€€€€€€™½Èµ•ÑÉ¥Œ¥¸µ•Ñ…‘…Ñ„(€€€€€€€€€€€¥˜µ•ÑÉ¥Œ¹ÍÑ…ÉÑÍÝ¥Ñ   ‰¥¹‘ÕÍÑÉä¸ˆ°€‰¥¹‘ÕÍÑÉå}½µÁ½¹•¹Ð¸ˆ¤¤(€€€€€€€ô(€€€€€€€…ÁÁ±¥…‰±”€ômµ•Ñ…‘…Ñ…mµ•ÑÉ¥t™½Èµ•ÑÉ¥Œ¥¸…ÁÁ±¥…‰±•}¹…µ•Ít(€€€€€€€•Ù¥‘•¹•€ôÍÕ´ 4(€€€€€€€€€€€‰½½°¡µ•Ñ…l‰•Ù¥‘•¹•}¥‘Ì‰t¤4(€€€€€€€€€€€™½Èµ•Ñ„¥¸…ÁÁ±¥…‰±”4(€€€€€€€€€€€¥˜µ•Ñ…l‰ÍÑ…ÑÕÌ‰t¥¸ì‰Y1%ˆ°€‰MQ%5Q‰ô4(€€€€€€€€¤4(€€€€€€€…Ù…¥±…‰±”€ôÍÕ´ 4(€€€€€€€€€€€µ•Ñ…l‰ÍÑ…ÑÕÌ‰t¥¸ì‰Y1%ˆ°€‰MQ%5Q‰ô4(€€€€€€€€€€€™½Èµ•Ñ„¥¸…ÁÁ±¥…‰±”4(€€€€€€€€¤4(€€€€€€€½Ù•É…”€ô•Ù¥‘•¹•€¼…Ù…¥±…‰±”¥˜…Ù…¥±…‰±”•±Í”€À¸À4(€€€€€€€µ•Ñ¡½‘Í}Ñ•áÐ€ô€ˆ€ˆ¹©½¥¸ 4(€€€€€€€€€€€ÍÑÈ¡Ù…±Õ”½È€ˆˆ¤4(€€€€€€€€€€€™½ÈÙ…±Õ”¥¸€ 4(€€€€€€€€€€€€€€€É½Ü¹•Ð ‰•‰Ñ}M½ÕÉ•}5•Ñ¡½ˆ¤°4(€€€€€€€€€€€€€€€É½Ü¹•Ð ‰%I}5•Ñ¡½ˆ¤°4(€€€€€€€€€€€€€€€É½Ü¹•Ð ‰…Áá}I•¥¹Ù•ÍÑµ•¹Ñ}5•Ñ¡½ˆ¤°4(€€€€€€€€€€€€€€€É½Ü¹•Ð ‰…Ñ…}½¹™¥‘•¹•}I•…Í½¹Ìˆ¤°4(€€€€€€€€€€€€¤4(€€€€€€€€¤¹±½Ý•È ¤4(€€€€€€€É½Ýl‰5•ÑÉ¥}½¹ÑÉ…Ñ}Y•ÉÍ¥½¸‰t€ô5QI%}=9QIQ}YIM%=84(€€€€€€€É½Ýl‰5•ÑÉ¥}5•Ñ…‘…Ñ…})M=8‰t€ô©Í½¸¹‘ÕµÁÌ (€€€€€€€€€€€µ•Ñ…‘…Ñ„°•¹ÍÕÉ•}…Í¥¤õ…±Í”°Í½ÉÑ}­•åÌõQÉÕ”°Í•Á…É…Ñ½ÉÌô ˆ°ˆ°€ˆèˆ¤(€€€€€€€€¤(€€€€€€€ÍÑ…ÑÕÍ}½Õ¹ÑÌ€ôì(€€€€€€€€€€€ÍÑ…ÑÕÌèÍÕ´ (€€€€€€€€€€€€€€€µ•Ñ„¹•Ð ‰ÍÑ…ÑÕÌˆ¤€ôôÍÑ…ÑÕÌ(€€€€€€€€€€€€€€€™½Èµ•Ñ„¥¸µ•Ñ…‘…Ñ„¹Ù…±Õ•Ì ¤(€€€€€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡µ•Ñ„°‘¥Ð¤(€€€€€€€€€€€€¤(€€€€€€€€€€€™½ÈÍÑ…ÑÕÌ¥¸Í½ÉÑ•¡5QI%}MQQUML¤(€€€€€€€ô(€€€€€€€Ù…±¥‘}‘…Ñ•Ì€ôÍ½ÉÑ• (€€€€€€€€€€€ÍÑÈ¡µ•Ñ„¹•Ð ‰…Í}½˜ˆ¤¥lèÄÁt(€€€€€€€€€€€™½Èµ•Ñ„¥¸µ•Ñ…‘…Ñ„¹Ù…±Õ•Ì ¤(€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡µ•Ñ„°‘¥Ð¤(€€€€€€€€€€€…¹µ•Ñ„¹•Ð ‰ÍÑ…ÑÕÌˆ¤¥¸ì‰Y1%ˆ°€‰MQ%5Q‰ô(€€€€€€€€€€€…¹ÍÑÈ¡µ•Ñ„¹•Ð ‰…Í}½˜ˆ¤½È€ˆˆ¥lèÄÁt¹½Õ¹Ð ˆ´ˆ¤€ôô€È(€€€€€€€€¤(€€€€€€€É½Ýl‰I•ÅÕ¥É•‘}5¥ÍÍ¥¹}5•ÑÉ¥Ì‰t€ô€ˆð€ˆ¹©½¥¸¡µ¥ÍÍ¥¹}É•ÅÕ¥É•¤(€€€€€€€É½Ýl‰=ÁÑ¥½¹…±}5¥ÍÍ¥¹}5•ÑÉ¥Ì‰t€ô€ˆð€ˆ¹©½¥¸¡½ÁÑ¥½¹…±}µ¥ÍÍ¥¹œ¤(€€€€€€€É½Ýl‰ÁÁ±¥…‰±•}5•ÑÉ¥Ì‰t€ô€ˆð€ˆ¹©½¥¸¡Í½ÉÑ•¡…ÁÁ±¥…‰±•}¹…µ•Ì¤¤(€€€€€€€É½Ýl‰9½Ñ}ÁÁ±¥…‰±•}5•ÑÉ¥Ì‰t€ô€ˆð€ˆ¹©½¥¸ (€€€€€€€€€€€Í½ÉÑ• (€€€€€€€€€€€€€€€µ•ÑÉ¥Œ(€€€€€€€€€€€€€€€™½Èµ•ÑÉ¥Œ°µ•Ñ„¥¸µ•Ñ…‘…Ñ„¹¥Ñ•µÌ ¤(€€€€€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡µ•Ñ„°‘¥Ð¤…¹µ•Ñ„¹•Ð ‰ÍÑ…ÑÕÌˆ¤€ôô€‰9=Q}AA1%	1ˆ(€€€€€€€€€€€€¤(€€€€€€€€¤(€€€€€€€É½Ýl‰5•ÑÉ¥}MÑ…ÑÕÍ}½Õ¹ÑÍ})M=8‰t€ô©Í½¸¹‘ÕµÁÌ (€€€€€€€€€€€ÍÑ…ÑÕÍ}½Õ¹ÑÌ°Í½ÉÑ}­•åÌõQÉÕ”°Í•Á…É…Ñ½ÉÌô ˆ°ˆ°€ˆèˆ¤(€€€€€€€€¤(€€€€€€€É½Ýl‰1…Ñ•ÍÑ}5•ÑÉ¥}Í=˜‰t€ôÙ…±¥‘}‘…Ñ•Íl´Åt¥˜Ù…±¥‘}‘…Ñ•Ì•±Í”€ˆˆ(€€€€€€€É½Ýl‰5•ÑÉ¥}Ù¥‘•¹•}½Ù•É…”‰t€ôÉ½Õ¹¡½Ù•É…”°€Ð¤(€€€€€€€É½Ýl‰UÍ•Í}e…¡½½}…±±‰…¬‰t€ô€‰å…¡½¼ˆ¥¸µ•Ñ¡½‘Í}Ñ•áÐ(€€€€€€€É½Ýl‰UÍ•Í}¹¹Õ…±}…±±‰…¬‰t€ô€‰…¹¹Õ…°™…±±‰…¬ˆ¥¸µ•Ñ¡½‘Í}Ñ•áÐ½È€‰™…±±‰…¬…¹¹Õ…°ˆ¥¸µ•Ñ¡½‘Í}Ñ•áÐ(€€€€€€€É½Ýl‰UÍ•Í}a}½¹Ù•ÉÍ¥½¸‰t€ô€ (€€€€€€€€€€€µ•Ñ…‘…Ñ…l‰A½¥¹Ñ}¥¹}Q¥µ•}a}I…Ñ”‰ul‰ÍÑ…ÑÕÌ‰t€ôô€‰Y1%ˆ(€€€€€€€€¤(€€€€€€€É½Ýl‰UÍ•Í}ÍÑ¥µ…Ñ•‘}5…¥¹Ñ•¹…¹•}…Áà‰t€ô…¹ä (€€€€€€€€€€€µ•Ñ…‘…Ñ…mµ•ÑÉ¥ul‰ÍÑ…ÑÕÌ‰t€ôô€‰MQ%5Qˆ4(€€€€€€€€€€€™½Èµ•ÑÉ¥Œ¥¸€ 4(€€€€€€€€€€€€€€€€‰5…¥¹Ñ•¹…¹•}…Áá}ˆ°4(€€€€€€€€€€€€€€€€‰I•…±}}e¥•±‘}ÁÐˆ°(€€€€€€€€€€€€¤(€€€€€€€€¤(€€€€€€€É½Ýl‰5•ÑÉ¥}…Ñ…}½µÁ±•Ñ”‰t€ô¹½Ðµ¥ÍÍ¥¹}É•ÅÕ¥É•(€€€€€€€…¹¹½Ñ…Ñ•¹…ÁÁ•¹¡É½Ü¤(€€€É•ÑÕÉ¸…¹¹½Ñ…Ñ•4(4(4)‘•˜…¹¹½Ñ…Ñ•}‘…Ñ…™É…µ” 4(€€€™É…µ”èÁ¹…Ñ…É…µ”°4(€€€•Ù¥‘•¹•}É½ÝÌè%Ñ•É…‰±•m5…ÁÁ¥¹mÍÑÈ°¹åut°4(¤€´øÁ¹…Ñ…É…µ”è4(€€€É•ÑÕÉ¸Á¹…Ñ…É…µ”¡…¹¹½Ñ…Ñ•}É½ÝÌ¡™É…µ”¹Ñ½}‘¥Ð¡½É¥•¹Ðô‰É•½É‘Ìˆ¤°•Ù¥‘•¹•}É½ÝÌ¤¤4(4(4)‘•˜…ÁÁ±å}½¹ÑÉ…Ñ}Ñ½}™¥±•Ì 4(€€€ÍÉ••¹}Á…Ñ èA…Ñ °4(€€€Í¡½ÉÑ±¥ÍÑ}Á…Ñ èA…Ñ °4(€€€•Ù¥‘•¹•}Á…Ñ èA…Ñ °4(¤€´ø9½¹”è4(€€€•Ù¥‘•¹”€ôÁ¹É•…‘}ÍØ¡•Ù¥‘•¹•}Á…Ñ °•¹½‘¥¹œô‰ÕÑ˜´àµÍ¥œˆ¤¹Ñ½}‘¥Ð¡½É¥•¹Ðô‰É•½É‘Ìˆ¤4(€€€ÍÉ••¸€ôÁ¹É•…‘}ÍØ¡ÍÉ••¹}Á…Ñ °•¹½‘¥¹œô‰ÕÑ˜´àµÍ¥œˆ¤4(€€€…¹¹½Ñ…Ñ•€ô…¹¹½Ñ…Ñ•}‘…Ñ…™É…µ”¡ÍÉ••¸°•Ù¥‘•¹”¤4(€€€…¹¹½Ñ…Ñ•¹Ñ½}ÍØ¡ÍÉ••¹}Á…Ñ °¥¹‘•àõ…±Í”°•¹½‘¥¹œô‰ÕÑ˜´àµÍ¥œˆ¤4(€€€Í¡½ÉÑ±¥ÍÐ€ôÁ¹É•…‘}ÍØ¡Í¡½ÉÑ±¥ÍÑ}Á…Ñ °•¹½‘¥¹œô‰ÕÑ˜´àµÍ¥œˆ¤4(€€€½É‘•È€ômÍÑÈ¡Ù…±Õ”¤¹ÕÁÁ•È ¤™½ÈÙ…±Õ”¥¸Í¡½ÉÑ±¥ÍÐ¹•Ð ‰Q¥­•Èˆ°Á¹M•É¥•Ì¡‘ÑåÁ”õÍÑÈ¤¥t4(€€€‰å}Ñ¥­•È€ô…¹¹½Ñ…Ñ•¹…ÍÍ¥¸ 4(€€€€€€€}Ñ¥­•Èõ…¹¹½Ñ…Ñ•‘l‰Q¥­•È‰t¹…ÍÑåÁ”¡ÍÑÈ¤¹ÍÑÈ¹ÕÁÁ•È ¤4(€€€€¤¹Í•Ñ}¥¹‘•à ‰}Ñ¥­•Èˆ°‘É½ÀõQÉÕ”¤4(€€€Í•±•Ñ•€ô‰å}Ñ¥­•È¹±½mmÑ¥­•È™½ÈÑ¥­•È¥¸½É‘•È¥˜Ñ¥­•È¥¸‰å}Ñ¥­•È¹¥¹‘•áut4(€€€Í•±•Ñ•¹Ñ½}ÍØ¡Í¡½ÉÑ±¥ÍÑ}Á…Ñ °¥¹‘•àõ…±Í”°•¹½‘¥¹œô‰ÕÑ˜´àµÍ¥œˆ¤4(4(4)‘•˜µ…¥¸ ¤€´ø¥¹Ðè4(€€€Á…ÉÍ•È€ô…ÉÁ…ÉÍ”¹ÉÕµ•¹ÑA…ÉÍ•È¡‘•ÍÉ¥ÁÑ¥½¸ô‰ÁÁ±äÑ¡”5½‘”µ•ÑÉ¥ŒµÍÑ…ÑÕÌ½¹ÑÉ…Ðˆ¤4(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µÍÉ••¸ˆ°‘•™…Õ±Ðô‰µ½‘•}}ÍÉ••¸¹ÍØˆ¤4(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µÍ¡½ÉÑ±¥ÍÐˆ°‘•™…Õ±Ðô‰µ½‘•}}Í¡½ÉÑ±¥ÍÐ¹ÍØˆ¤4(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µ•Ù¥‘•¹”ˆ°‘•™…Õ±Ðô‰µ½‘•}}•Ù¥‘•¹•}±•‘•È¹ÍØˆ¤4(€€€…ÉÌ€ôÁ…ÉÍ•È¹Á…ÉÍ•}…ÉÌ ¤4(€€€…ÁÁ±å}½¹ÑÉ…Ñ}Ñ½}™¥±•Ì¡A…Ñ ¡…ÉÌ¹ÍÉ••¸¤°A…Ñ ¡…ÉÌ¹Í¡½ÉÑ±¥ÍÐ¤°A…Ñ ¡…ÉÌ¹•Ù¥‘•¹”¤¤4(€€€É•ÑÕÉ¸€À4(4(4)¥˜}}¹…µ•}|€ôô€‰}}µ…¥¹}|ˆè4(€€€É…¥Í”MåÍÑ•µá¥Ð¡µ…¥¸ ¤¤4(