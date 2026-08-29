from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from mode_c_research_priority import (
    GLOBAL_RESEARCH_QUEUE_SIZE,
    RESEARCH_PRIORITY_VERSION,
    annotate_research_priorities,
)

DASHBOARD_TREND_POLICY_VERSION = (
    f"2026-07-model-relative-trends-v4:{RESEARCH_PRIORITY_VERSION}"
)
TREND_INCLUDE_ESTIMATED = False


THEME_RULES: list[dict[str, Any]] = [
    {
        "id": "ai_chip_second_order",
        "name": "AI 晶片二階受益鏈",
        "thesis": "AI 晶片需求若持續，受益者不只 GPU；先進製程、設備、記憶體、電源管理、散熱、連接器、被動元件與測試量測都要一起檢查。",
        "segments": ["晶片設計", "晶圓代工", "半導體設備", "HBM/記憶體", "電源/類比", "散熱/電力", "連接器/被動元件", "測試量測"],
        "tickers": ["NVDA", "AMD", "AVGO", "MRVL", "TSM", "ASML", "AMAT", "LRCX", "KLAC", "MU", "WDC", "STX", "ADI", "TXN", "MPWR", "MCHP", "ON", "APH", "TEL", "VSH", "GLW", "KEYS", "TER", "VRT", "ETN", "ANET"],
        "keywords": ["semiconductor", "semiconductor equipment", "memory", "electronic components", "communication equipment", "computer hardware", "electrical equipment", "specialty industrial machinery", "connectors", "passive", "analog", "power", "thermal", "testing", "measurement"],
        "layers": [
            {"name": "一階：直接算力與核心晶片", "tickers": ["NVDA", "AMD", "AVGO", "MRVL", "TSM"], "keywords": ["semiconductors"]},
            {"name": "二階：瓶頸零組件與設備", "tickers": ["ASML", "AMAT", "LRCX", "KLAC", "MU", "WDC", "STX", "ADI", "TXN", "MPWR", "MCHP", "ON", "APH", "TEL", "VSH", "GLW", "KEYS", "TER", "ANET"], "keywords": ["semiconductor equipment", "memory", "electronic components", "communication equipment", "computer hardware", "connectors", "passive", "analog", "power", "testing", "measurement"]},
            {"name": "三階：資料中心基建外溢", "tickers": ["VRT", "ETN", "TT", "HUBB", "PWR"], "keywords": ["electrical equipment", "thermal", "cooling", "specialty industrial machinery"]},
        ],
        "questions": ["營收是否真的跟 AI/資料中心 capex 連動？", "供給是否吃緊到能維持毛利？", "這是必需品、規格升級，還是一次性拉貨？"],
    },
    {
        "id": "data_center_power_cooling",
        "name": "資料中心電力與散熱",
        "thesis": "AI cluster 功耗提高後，瓶頸可能從晶片轉到電力、UPS、配電、液冷、空調與機房工程。",
        "segments": ["電力設備", "UPS/配電", "液冷/散熱", "機房工程", "工業自動化"],
        "tickers": ["VRT", "ETN", "TT", "CARR", "JCI", "PWR", "HUBB", "EMR", "PH", "ROK"],
        "keywords": ["electrical equipment", "building products", "specialty industrial machinery", "engineering", "construction", "hvac", "thermal", "cooling", "automation"],
        "layers": [
            {"name": "一階：機房電力與散熱主設備", "tickers": ["VRT", "ETN", "TT", "CARR"], "keywords": ["electrical equipment", "hvac", "thermal", "cooling"]},
            {"name": "二階：配電、工程與關鍵零組件", "tickers": ["PWR", "HUBB", "JCI", "PH"], "keywords": ["engineering", "construction", "building products", "specialty industrial machinery"]},
            {"name": "三階：工業自動化與維運外溢", "tickers": ["EMR", "ROK"], "keywords": ["automation", "industrial"]},
        ],
        "questions": ["訂單是否來自資料中心而非一般景氣循環？", "毛利是否因競爭加劇而回落？", "capex 週期反轉時營收會掉多深？"],
    },
    {
        "id": "electrification_grid",
        "name": "電氣化與電網升級",
        "thesis": "AI、EV、再工業化與電力需求成長可能推動電網、變壓器、配電與工業電氣設備。",
        "segments": ["電網設備", "配電", "工業電氣", "工程服務"],
        "tickers": ["ETN", "HUBB", "PWR", "EMR", "PH", "ROK", "TT", "VRT", "ABBNY", "SIEGY"],
        "keywords": ["electrical equipment", "utilities regulated electric", "engineering", "industrial", "automation", "specialty industrial machinery"],
        "layers": [
            {"name": "一階：電網與配電設備", "tickers": ["ETN", "HUBB", "VRT", "ABBNY", "SIEGY"], "keywords": ["electrical equipment"]},
            {"name": "二階：工程服務與工業零組件", "tickers": ["PWR", "PH", "TT"], "keywords": ["engineering", "specialty industrial machinery"]},
            {"name": "三階：自動化與效率升級", "tickers": ["EMR", "ROK"], "keywords": ["automation", "industrial"]},
        ],
        "questions": ["需求是長週期基建還是短期補庫存？", "公司是否有定價權與 backlog？", "估值是否已把多年成長一次反映？"],
    },
    {
        "id": "cybersecurity_ai_software",
        "name": "AI 軟體與資安防線",
        "thesis": "企業導入 AI 與雲端後，資安、資料治理、雲端平台與自動化軟體可能成為伴隨支出。",
        "segments": ["資安", "雲端平台", "資料治理", "企業自動化"],
        "tickers": ["PANW", "FTNT", "CRWD", "ZS", "NET", "DDOG", "SNOW", "NOW", "MSFT", "ADBE", "CRM"],
        "keywords": ["software - infrastructure", "software - application", "cybersecurity", "cloud", "data", "security"],
        "layers": [
            {"name": "一階：資安與雲端防線", "tickers": ["PANW", "FTNT", "CRWD", "ZS", "NET"], "keywords": ["cybersecurity", "security", "software - infrastructure"]},
            {"name": "二階：資料平台與可觀測性", "tickers": ["DDOG", "SNOW", "NOW"], "keywords": ["data", "cloud", "software - application"]},
            {"name": "三階：企業軟體 AI 功能外溢", "tickers": ["MSFT", "ADBE", "CRM"], "keywords": ["software - application"]},
        ],
        "questions": ["ARR/留存率是否支持估值？", "SBC 是否吃掉 FCF？", "AI 是否提升護城河，還是壓低價格？"],
    },
    {
        "id": "healthcare_quality_defensive",
        "name": "高品質醫療與防守成長",
        "thesis": "當市場太集中在科技時，醫療器材、診斷、製藥與生命科學工具可作為品質型分散研究池。",
        "segments": ["醫療器材", "生命科學工具", "製藥", "診斷"],
        "tickers": ["LLY", "NVO", "ISRG", "TMO", "DHR", "SYK", "VRTX", "ABT", "MDT", "MRK", "JNJ"],
        "keywords": ["healthcare", "medical", "diagnostics", "drug manufacturers", "biotechnology", "life sciences"],
        "layers": [
            {"name": "一階：藥物與核心療法", "tickers": ["LLY", "NVO", "VRTX", "MRK", "JNJ"], "keywords": ["drug manufacturers", "biotechnology"]},
            {"name": "二階：醫療器材與手術平台", "tickers": ["ISRG", "SYK", "ABT", "MDT"], "keywords": ["medical"]},
            {"name": "三階：生命科學工具與診斷", "tickers": ["TMO", "DHR"], "keywords": ["diagnostics", "life sciences"]},
        ],
        "questions": ["專利/產品週期風險是否集中？", "估值是否已反映管線成功？", "Real FCF 與研發投入是否健康？"],
    },
]


TREND_FIELDS = {
    "avg_raw_score": "Raw_Model_Score",
    "avg_shrunk_score": "Shrunk_Within_Model_Percentile",
    "avg_confidence": "Data_Confidence_Score",
    "avg_kpi_coverage": "Core_Metric_Coverage_pct",
    "avg_quality": "Quality_Score",
    "avg_revenue_change": "Rev_3Q_Change_pct",
    "avg_margin_change": "GM_3Q_Change_pp",
    "avg_real_fcf_yield": "Real_FCF_Yield_pct",
}


def clean(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if isinstance(value, (bool, int, float)) else str(value)


def records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        {str(key): clean(value) for key, value in row.items()}
        for row in pd.read_csv(path).to_dict(orient="records")
    ]


def ticker(row: dict[str, Any]) -> str:
    return str(row.get("Ticker") or "").strip().upper()


def as_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def score(row: dict[str, Any]) -> float:
    value = as_number(row.get("Shrunk_Within_Model_Percentile"))
    return value if value is not None else -1.0


def median_value(rows: list[dict[str, Any]], field: str) -> float | None:
    values = sorted(
        value
        for row in rows
        if (value := as_number(row.get(field))) is not None
    )
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return round(values[middle], 2)
    return round((values[middle - 1] + values[middle]) / 2.0, 2)


def core_kpi_summary(row: dict[str, Any]) -> list[dict[str, Any]]:
    model_key = str(row.get("Industry_Model_Key") or "GENERAL_CORPORATE")
    metrics = parse_object(row.get("Industry_Model_Metrics_JSON"))

    def first(*names: str) -> Any:
        for name in names:
            if name in row and clean(row.get(name)) is not None:
                return clean(row.get(name))
            if name in metrics and clean(metrics.get(name)) is not None:
                return clean(metrics.get(name))
        return None

    if model_key == "INSURANCE_P_AND_C":
        source_status = str(row.get("Combined_Ratio_Source_Status") or "")
        ratio_label = (
            "Combined ratio (proxy; unreconciled)"
            if source_status == "SEC_PROXY_UNRECONCILED"
            else "Combined ratio"
        )
        values = (
            (ratio_label, first("Company_Reported_Combined_Ratio", "SEC_Combined_Ratio_Proxy", "combined_ratio_for_model_pct", "combined_ratio_proxy_pct"), "%"),
            ("Premium growth", first("Premium_Growth_pct", "premium_growth_pct"), "%"),
            ("P/B", first("Price_to_Book_x", "price_to_book_x"), "x"),
        )
    elif model_key == "BANK":
        values = (
            ("Tier 1 buffer", first("tier1_buffer_pp"), "pp"),
            ("ROTCE", first("rotce_pct"), "%"),
            ("P/TBV", first("price_to_tangible_book_x"), "x"),
        )
    elif model_key == "REIT_EQUITY":
        values = (
            ("AFFO yield", first("affo_yield_pct"), "%"),
            ("Net debt/EBITDAre", first("net_debt_to_ebitdare_x"), "x"),
            ("Dividend/AFFO", first("dividend_to_affo_pct"), "%"),
        )
    elif model_key in {
        "ALTERNATIVE_ASSET_MANAGER",
        "TRADITIONAL_ASSET_MANAGER",
        "INSURANCE_LINKED_ASSET_MANAGER",
        "OTHER_FEE_FINANCIAL",
    }:
        values = (
            ("AUM growth", first("AUM_Growth_pct", "aum_growth_pct"), "%"),
            ("Organic net flows", first("Organic_Net_Flows_pct", "organic_net_flows_pct"), "%"),
            ("Compensation/revenue", first("Compensation_to_Revenue_pct", "compensation_to_revenue_pct"), "%"),
        )
    elif model_key != "GENERAL_CORPORATE":
        specialized_fields = {
            "INSURANCE_LIFE": (
                ("Benefits/operating inflow", "benefit_ratio_pct", "%"),
                ("Equity/assets", "equity_to_assets_pct", "%"),
                ("ROE", "roe_pct", "%"),
            ),
            "REIT_MORTGAGE": (
                ("Recurring ROE", "recurring_roe_pct", "%"),
                ("Assets/equity", "assets_to_equity_x", "x"),
                ("Dividend payout", "dividend_payout_pct", "%"),
            ),
            "REGULATED_UTILITY": (
                ("Interest coverage", "interest_coverage_x", "x"),
                ("Debt/capital", "debt_to_capital_pct", "%"),
                ("Earnings/dividend", "earnings_to_dividend_x", "x"),
            ),
            "CYCLICAL_MIDCYCLE": (
                ("EV/midcycle EBITDA", "ev_to_midcycle_ebitda_x", "x"),
                ("Net debt/trough EBITDA", "net_debt_to_trough_ebitda_x", "x"),
                ("Trough interest coverage", "trough_interest_coverage_x", "x"),
            ),
            "FINANCIAL_LENDER": (
                ("ROTCE", "rotce_pct", "%"),
                ("Tangible equity/assets", "tangible_equity_to_assets_pct", "%"),
                ("Allowance/loans", "credit_loss_allowance_to_loans_pct", "%"),
            ),
            "FINANCIAL_FEE": (
                ("OCF/net income", "ocf_to_net_income_x", "x"),
                ("Net debt/EBITDA", "net_debt_to_ebitda_x", "x"),
                ("EBIT margin", "operating_margin_pct", "%"),
            ),
        }.get(model_key, ())
        values = tuple(
            (label, first(field), suffix)
            for label, field, suffix in specialized_fields
        )
        if len(values) < 3:
            padding = (
                ("Model score", first("Industry_Model_Score"), ""),
            ) * (3 - len(values))
            values = (*values, *padding)
    else:
        maintenance_yield_lower = as_number(
            first(
                "Maintenance_Real_FCF_Yield_Lower_pct",
                "Maintenance_Real_FCF_Yield_Low_pct",
            )
        )
        maintenance_yield_upper = as_number(
            first(
                "Maintenance_Real_FCF_Yield_Upper_pct",
                "Maintenance_Real_FCF_Yield_High_pct",
            )
        )
        maintenance_yield_range = first("Maintenance_FCF_Yield_Range_Text")
        if (
            maintenance_yield_range is None
            and maintenance_yield_lower is not None
            and maintenance_yield_upper is not None
        ):
            maintenance_yield_range = (
                f"{maintenance_yield_lower:.2f}%-{maintenance_yield_upper:.2f}%"
            )
        values = (
            ("Maintenance FCF yield range", maintenance_yield_range, ""),
            ("Conservative FCF yield", first("Conservative_Real_FCF_Yield_pct"), "%"),
            ("EV/EBITDA", first("EV_EBITDA_x"), "x"),
        )
    return [
        {"label": label, "value": value, "suffix": suffix}
        for label, value, suffix in values
    ]


def core_metric_coverage(row: dict[str, Any]) -> float | None:
    model_key = str(row.get("Industry_Model_Key") or "GENERAL_CORPORATE")
    value = (
        as_number(row.get("Factor_Coverage"))
        if model_key == "GENERAL_CORPORATE"
        else as_number(row.get("Industry_Model_Coverage"))
    )
    if value is not None and model_key == "GENERAL_CORPORATE" and value <= 1.0:
        value *= 100.0
    if value is None:
        value = as_number(row.get("Metric_Evidence_Coverage"))
        if value is not None and value <= 1.0:
            value *= 100.0
    return round(value, 2) if value is not None else None


def valuation_summary(row: dict[str, Any]) -> str:
    model_key = str(row.get("Industry_Model_Key") or "GENERAL_CORPORATE")
    metrics = parse_object(row.get("Industry_Model_Metrics_JSON"))
    if model_key == "INSURANCE_P_AND_C":
        value = as_number(row.get("Price_to_Book_x"))
        value = value if value is not None else as_number(metrics.get("price_to_book_x"))
        return f"P/B {value:.2f}x" if value is not None else "P/B N/A"
    if model_key == "BANK":
        value = as_number(metrics.get("price_to_tangible_book_x"))
        return f"P/TBV {value:.2f}x" if value is not None else "P/TBV N/A"
    if model_key == "REIT_EQUITY":
        value = as_number(metrics.get("price_to_ffo_x"))
        return f"P/FFO {value:.2f}x" if value is not None else "P/FFO N/A"
    if model_key in {
        "ALTERNATIVE_ASSET_MANAGER",
        "TRADITIONAL_ASSET_MANAGER",
        "INSURANCE_LINKED_ASSET_MANAGER",
        "OTHER_FEE_FINANCIAL",
    }:
        return str(row.get("Valuation_Method") or metrics.get("valuation_method") or "Valuation KPI pending")
    if model_key != "GENERAL_CORPORATE":
        return str(row.get("Valuation_Method") or "Specialized valuation in model details")
    value = as_number(row.get("EV_EBITDA_x"))
    return f"EV/EBITDA {value:.2f}x" if value is not None else "EV/EBITDA N/A"


def driver_risk_summary(row: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    positives: list[str] = []
    risks: list[str] = []
    tasks: list[str] = []
    model_key = str(row.get("Industry_Model_Key") or "GENERAL_CORPORATE")
    if model_key == "GENERAL_CORPORATE":
        for label, field in (
            ("Quality score", "Quality_Score"),
            ("Value score", "Value_Score"),
            ("Operating inflection", "Operating_Inflection_Score"),
            ("Conservative FCF yield", "Conservative_Real_FCF_Yield_pct"),
            ("ROIC", "ROIC_pct"),
        ):
            value = as_number(row.get(field))
            if value is not None:
                positives.append(f"{label}: {value:.2f}")
    else:
        components = parse_object(row.get("Industry_Model_Components_JSON"))
        ranked = sorted(
            ((name, as_number(value)) for name, value in components.items()),
            key=lambda item: (item[1] is None, -(item[1] or 0.0), item[0]),
        )
        positives.extend(
            f"{name}: {value:.2f}"
            for name, value in ranked
            if value is not None
        )

    required_missing = str(row.get("Required_Missing_Metrics") or "").strip()
    optional_missing = str(row.get("Optional_Missing_Metrics") or "").strip()
    if required_missing:
        risks.append(f"Required evidence missing: {required_missing}")
    if str(row.get("Specialized_Stress_Status") or "").upper() not in {
        "", "PASS", "IMPLEMENTED_PASS", "NOT_APPLICABLE"
    }:
        stress_reason = str(row.get("Specialized_Stress_Reason") or "").strip()
        risks.append(
            f"Specialized stress: {row.get('Specialized_Stress_Status')}"
            + (f" ({stress_reason})" if stress_reason else "")
        )
    growth_capex_state = str(row.get("Growth_CapEx_Risk_State") or "").upper()
    if growth_capex_state in {"WATCH", "HIGH_RISK"}:
        growth_capex_reasons = str(
            row.get("Growth_CapEx_Risk_Reasons") or ""
        ).strip()
        risks.append(
            f"Growth CapEx {growth_capex_state}"
            + (f": {growth_capex_reasons}" if growth_capex_reasons else "")
        )
    lower_fcf = as_number(row.get("Maintenance_Real_FCF_Yield_Lower_pct"))
    conservative_fcf = as_number(row.get("Conservative_Real_FCF_Yield_pct"))
    if lower_fcf is not None and lower_fcf < 0:
        risks.append(f"Lower-bound maintenance FCF yield: {lower_fcf:.2f}%")
    if conservative_fcf is not None and conservative_fcf < 0:
        risks.append(f"Conservative FCF yield: {conservative_fcf:.2f}%")
    if truthy(row.get("Persistent_Dilution_Hard_Gate")):
        risks.append("Persistent dilution hard gate")
    working_capital_state = str(
        row.get("Working_Capital_Quality_State") or ""
    ).upper()
    if working_capital_state in {"WATCH", "HIGH_RISK"}:
        working_capital_reasons = str(
            row.get("Working_Capital_Quality_Reasons") or ""
        ).strip()
        risks.append(
            f"Working capital {working_capital_state}"
            + (
                f": {working_capital_reasons}"
                if working_capital_reasons
                else ""
            )
        )
    flags = str(row.get("Data_Quality_Flags") or "").strip()
    if flags:
        risks.extend(item.strip() for item in flags.split("|") if item.strip())
    agent_tasks = row.get("Agent_Tasks")
    if isinstance(agent_tasks, list):
        tasks.extend(str(item) for item in agent_tasks if str(item).strip())
    else:
        tasks.extend(
            item.strip()
            for item in str(agent_tasks or "").split("|")
            if item.strip()
        )
    if truthy(row.get("Human_KPI_Review_Required")):
        tasks.insert(0, "Reconcile company-reported KPI with SEC proxy")
    if truthy(row.get("Specialized_Stress_Pending")):
        missing_stress = str(
            row.get("Specialized_Stress_Missing_Inputs") or ""
        ).strip()
        tasks.insert(
            0,
            "Complete specialized stress evidence"
            + (f": {missing_stress}" if missing_stress else ""),
        )
    if truthy(row.get("Acquisition_Accretion_Review_Required")):
        tasks.insert(0, "Verify acquisition-related issuance is accretive per share")
    if optional_missing:
        tasks.append(f"Optional evidence to collect: {optional_missing}")
    return positives[:3], risks[:3], tasks[:6]


def parse_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def data_integrity_summary(metadata: dict[str, Any]) -> dict[str, int]:
    counts = {status: 0 for status in (
        "VALID", "ESTIMATED", "MISSING", "NOT_APPLICABLE",
        "ABSTAIN", "STALE", "INVALID",
    )}
    for meta in metadata.values():
        if not isinstance(meta, dict):
            continue
        status = str(meta.get("status") or "").upper()
        if status in counts:
            counts[status] += 1
    return counts


def metric_summary(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    values: list[float] = []
    valid_count = 0
    estimated_count = 0
    status_counts: dict[str, int] = {}
    for row in rows:
        metadata = row.get("Metric_Metadata")
        meta = metadata.get(field, {}) if isinstance(metadata, dict) else {}
        status = str(meta.get("status") or "").upper()
        value = as_number(meta.get("value"))
        if not status:
            value = as_number(row.get(field))
            status = "VALID" if value is not None else "MISSING"
        status_counts[status] = status_counts.get(status, 0) + 1
        if status == "VALID" and value is not None:
            valid_count += 1
            values.append(value)
        elif status == "ESTIMATED" and value is not None:
            estimated_count += 1
            if TREND_INCLUDE_ESTIMATED:
                values.append(value)
    coverage = len(values) / len(rows) if rows else 0.0
    average_value = round(sum(values) / len(values), 2) if values else None
    if coverage >= 0.50:
        summary_status = "VALID"
    elif rows and status_counts.get("NOT_APPLICABLE", 0) == len(rows):
        summary_status = "NOT_APPLICABLE"
    elif rows and status_counts.get("ABSTAIN", 0) == len(rows):
        summary_status = "ABSTAIN"
    elif rows and status_counts.get("STALE", 0) == len(rows):
        summary_status = "STALE"
    else:
        summary_status = "MISSING"
    return {
        "value": average_value if coverage >= 0.50 else None,
        "valid_count": valid_count,
        "estimated_count": estimated_count,
        "used_count": len(values),
        "total_count": len(rows),
        "coverage": round(coverage, 4),
        "estimated_included": TREND_INCLUDE_ESTIMATED,
        "status_counts": status_counts,
        "status": summary_status,
    }


def truthy(value: Any) -> bool:
    return value is True or str(value).lower() == "true"


def row_text(row: dict[str, Any]) -> str:
    return " ".join(
        str(row.get(field) or "")
        for field in ("Ticker", "Name", "Sector", "Industry", "Status", "Verdict", "Research_Action")
    ).lower()


def matches_keywords_or_ticker(row: dict[str, Any], rule: dict[str, Any]) -> bool:
    symbol = ticker(row)
    if symbol and symbol in set(rule.get("tickers", [])):
        return True
    text = row_text(row)
    return any(str(keyword).lower() in text for keyword in rule.get("keywords", []))


def match_theme(row: dict[str, Any], theme: dict[str, Any]) -> bool:
    return matches_keywords_or_ticker(row, theme)


def match_theme_layer(row: dict[str, Any], theme: dict[str, Any]) -> str:
    for layer in theme.get("layers", []):
        if matches_keywords_or_ticker(row, layer):
            return str(layer.get("name") or "待判斷")
    return "待判斷：需人工確認受益層級"


def attach_theme_tags(stocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in stocks:
        ids: list[str] = []
        names: list[str] = []
        layer_map: dict[str, str] = {}
        layer_tags: list[str] = []
        for theme in THEME_RULES:
            if match_theme(row, theme):
                theme_id = str(theme["id"])
                layer_name = match_theme_layer(row, theme)
                ids.append(theme_id)
                names.append(str(theme["name"]))
                layer_map[theme_id] = layer_name
                layer_tags.append(f"{theme['name']} / {layer_name}")
        row["Theme_Ids"] = ids
        row["Theme_Tags"] = names
        row["Theme_Layer_Map"] = layer_map
        row["Theme_Layer_Tags"] = layer_tags
    return stocks


def build_theme_summary(stocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary = []
    for theme in THEME_RULES:
        theme_id = str(theme["id"])
        matched = [row for row in stocks if theme_id in row.get("Theme_Ids", [])]
        matched.sort(key=lambda row: (-score(row), ticker(row)))
        layers = []
        for layer in theme.get("layers", []):
            layer_name = str(layer.get("name") or "待判斷")
            layer_rows = [row for row in matched if row.get("Theme_Layer_Map", {}).get(theme_id) == layer_name]
            layer_rows.sort(key=lambda row: (-score(row), ticker(row)))
            layers.append(
                {
                    "name": layer_name,
                    "count": len(layer_rows),
                    "eligible": sum(truthy(row.get("Long_Term_Eligible")) for row in layer_rows),
                    "shortlist": sum(bool(row.get("IsShortlist")) for row in layer_rows),
                    "top": [ticker(row) for row in layer_rows[:8]],
                }
            )
        summary.append(
            {
                "id": theme_id,
                "name": theme["name"],
                "thesis": theme["thesis"],
                "segments": theme["segments"],
                "questions": theme["questions"],
                "count": len(matched),
                "eligible": sum(truthy(row.get("Long_Term_Eligible")) for row in matched),
                "shortlist": sum(bool(row.get("IsShortlist")) for row in matched),
                "top": [ticker(row) for row in matched[:8]],
                "layers": layers,
            }
        )
    return summary


def group_snapshot(group_key: str, name: str, kind: str, rows: list[dict[str, Any]], theme_id: str | None = None) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: (-score(row), ticker(row)))
    summaries = {
        metric: metric_summary(rows, field)
        for metric, field in TREND_FIELDS.items()
    }
    metrics = {metric: summary["value"] for metric, summary in summaries.items()}
    return {
        "key": group_key,
        "name": name,
        "kind": kind,
        "theme_id": theme_id,
        "model_keys": sorted({
            str(row.get("Industry_Model_Key") or "GENERAL_CORPORATE")
            for row in rows
        }),
        "model_version": RESEARCH_PRIORITY_VERSION,
        "count": len(rows),
        "eligible": sum(truthy(row.get("Long_Term_Eligible")) for row in rows),
        "research_queue": sum(bool(row.get("IsShortlist")) for row in rows),
        "shortlist": sum(bool(row.get("IsShortlist")) for row in rows),
        "top": [ticker(row) for row in ordered[:8]],
        "metric_coverage": summaries,
        **metrics,
    }


def build_trend_groups(stocks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}

    def add_group(key: str, name: str, kind: str, rows: list[dict[str, Any]], theme_id: str | None = None, min_count: int = 3) -> None:
        rows = [row for row in rows if ticker(row)]
        if len(rows) >= min_count:
            groups[key] = group_snapshot(key, name, kind, rows, theme_id)

    for field, label, min_count in (("Sector", "產業", 5), ("Industry", "行業", 3)):
        buckets: dict[str, list[dict[str, Any]]] = {}
        for row in stocks:
            name = str(row.get(field) or "").strip()
            if name:
                buckets.setdefault(name, []).append(row)
        for name, rows in buckets.items():
            add_group(f"{field.lower()}:{name}", f"{label}：{name}", label, rows, min_count=min_count)

    for theme in THEME_RULES:
        theme_id = str(theme["id"])
        for layer in theme.get("layers", []):
            layer_name = str(layer.get("name") or "待判斷")
            rows = [row for row in stocks if row.get("Theme_Layer_Map", {}).get(theme_id) == layer_name]
            add_group(
                f"theme_layer:{theme_id}:{layer_name}",
                f"{theme['name']} / {layer_name}",
                "主題層級",
                rows,
                theme_id=theme_id,
                min_count=2,
            )
    return groups


def load_trend_history(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def save_trend_history(path: Path | None, groups: dict[str, dict[str, Any]], generated_at: str) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(
            {
                "policy_version": DASHBOARD_TREND_POLICY_VERSION,
                "generated_at": generated_at,
                "groups": groups,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temp_path.replace(path)


def delta(current: dict[str, Any], previous: dict[str, Any], field: str) -> float | None:
    now = as_number(current.get(field))
    old = as_number(previous.get(field))
    if now is None or old is None:
        return None
    return round(now - old, 2)


def _build_legacy_emerging_candidates(groups: dict[str, dict[str, Any]], history: dict[str, Any]) -> list[dict[str, Any]]:
    previous_groups = history.get("groups", {}) if isinstance(history.get("groups"), dict) else {}
    candidates: list[dict[str, Any]] = []
    for key, group in groups.items():
        coverage = group.get("metric_coverage", {})
        if float(coverage.get("avg_score", {}).get("coverage") or 0.0) < 0.50:
            continue
        reasons: list[str] = []
        deltas: dict[str, Any] = {}
        signal = 0.0

        def covered_value(field: str) -> float | None:
            summary = coverage.get(field, {}) if isinstance(coverage, dict) else {}
            if float(summary.get("coverage") or 0.0) < 0.50:
                return None
            return as_number(group.get(field))

        avg_score = covered_value("avg_score")
        avg_revenue = covered_value("avg_revenue_change")
        avg_margin = covered_value("avg_margin_change")
        avg_fcf = covered_value("avg_real_fcf_yield")
        if avg_score is not None and avg_score >= 70:
            reasons.append("群組平均分數達 70 以上")
            signal += 2
        elif avg_score is not None and avg_score >= 65:
            reasons.append("群組平均分數達 65 以上")
            signal += 1
        if (group.get("eligible") or 0) >= 2:
            reasons.append("至少 2 檔進入研究合格")
            signal += 2
        elif (group.get("eligible") or 0) >= 1:
            reasons.append("至少 1 檔進入研究合格")
            signal += 1
        if (group.get("shortlist") or 0) >= 1:
            reasons.append("已有公司進入分散 shortlist")
            signal += 1.5
        if avg_revenue is not None and avg_revenue >= 8:
            reasons.append("三季營收變化平均偏強")
            signal += 1
        if avg_margin is not None and avg_margin >= 1:
            reasons.append("毛利變化平均改善")
            signal += 1
        if avg_fcf is not None and avg_fcf >= 3:
            reasons.append("Real FCF Yield 具備基本吸引力")
            signal += 1

        previous = previous_groups.get(key, {}) if isinstance(previous_groups, dict) else {}
        if previous:
            for field, label, threshold in (
                ("count", "樣本數", 2),
                ("eligible", "合格公司數", 1),
                ("shortlist", "shortlist 公司數", 1),
                ("avg_score", "平均分數", 3),
                ("avg_quality", "平均品質分數", 3),
                ("avg_revenue_change", "營收變化", 4),
                ("avg_margin_change", "毛利變化", 1),
                ("avg_real_fcf_yield", "Real FCF Yield", 1),
            ):
                change = delta(group, previous, field)
                if change is not None:
                    deltas[field] = change
                    if change >= threshold:
                        reasons.append(f"{label} 較上次改善 {change:g}")
                        signal += 1.5
        else:
            reasons.append("首次建立基準，需下次確認是否延續")
            signal += 0.5

        if signal >= 4 and reasons:
            confidence = "高" if signal >= 7 else "中" if signal >= 5 else "低"
            candidates.append(
                {
                    "key": key,
                    "name": group["name"],
                    "kind": group["kind"],
                    "theme_id": group.get("theme_id"),
                    "status": "待人工確認",
                    "confidence": confidence,
                    "signal_score": round(signal, 2),
                    "reasons": reasons[:6],
                    "metrics": {field: group.get(field) for field in ("count", "eligible", "shortlist", "avg_score", "avg_quality", "avg_revenue_change", "avg_margin_change", "avg_real_fcf_yield")},
                    "metric_coverage": coverage,
                    "deltas": deltas,
                    "top": group.get("top", []),
                }
            )
    candidates.sort(key=lambda item: (-float(item.get("signal_score") or 0), str(item.get("name") or "")))
    return candidates[:12]


def build_emerging_candidates(
    groups: dict[str, dict[str, Any]], history: dict[str, Any]
) -> list[dict[str, Any]]:
    """Find high-scoring clusters using model-relative scores only."""
    previous_groups = history.get("groups", {}) if isinstance(history.get("groups"), dict) else {}
    candidates: list[dict[str, Any]] = []
    for key, group in groups.items():
        coverage = group.get("metric_coverage", {})
        shrunk_coverage = coverage.get("avg_shrunk_score", {})
        if float(shrunk_coverage.get("coverage") or 0.0) < 0.50:
            continue

        reasons: list[str] = []
        deltas: dict[str, Any] = {}
        signal = 0.0

        def covered_value(field: str) -> float | None:
            summary = coverage.get(field, {}) if isinstance(coverage, dict) else {}
            if float(summary.get("coverage") or 0.0) < 0.50:
                return None
            return as_number(group.get(field))

        avg_shrunk = covered_value("avg_shrunk_score")
        avg_confidence = covered_value("avg_confidence")
        avg_kpi_coverage = covered_value("avg_kpi_coverage")
        avg_revenue = covered_value("avg_revenue_change")
        avg_margin = covered_value("avg_margin_change")
        avg_fcf = covered_value("avg_real_fcf_yield")
        if avg_shrunk is not None and avg_shrunk >= 60:
            reasons.append("模型內收縮百分位平均達 60 以上")
            signal += 2.0
        elif avg_shrunk is not None and avg_shrunk >= 55:
            reasons.append("模型內收縮百分位平均達 55 以上")
            signal += 1.0
        if (group.get("eligible") or 0) >= 2:
            reasons.append("至少兩檔通過模型門檻")
            signal += 2.0
        elif (group.get("eligible") or 0) >= 1:
            reasons.append("至少一檔通過模型門檻")
            signal += 1.0
        if (group.get("research_queue") or 0) >= 1:
            reasons.append("至少一檔進入 Global Research Queue")
            signal += 1.5
        if avg_confidence is not None and avg_confidence >= 75:
            reasons.append("平均資料信心達 75 以上")
            signal += 1.0
        if avg_kpi_coverage is not None and avg_kpi_coverage >= 70:
            reasons.append("核心 KPI 平均覆蓋率達 70% 以上")
            signal += 1.0
        if avg_revenue is not None and avg_revenue >= 8:
            reasons.append("近三季營收趨勢改善")
            signal += 1.0
        if avg_margin is not None and avg_margin >= 1:
            reasons.append("近三季毛利率擴張")
            signal += 1.0
        if avg_fcf is not None and avg_fcf >= 3:
            reasons.append("Real FCF yield 具研究吸引力")
            signal += 1.0

        previous = previous_groups.get(key, {}) if isinstance(previous_groups, dict) else {}
        if previous:
            for field, label, threshold in (
                ("count", "樣本數", 2),
                ("eligible", "合格檔數", 1),
                ("research_queue", "研究佇列", 1),
                ("avg_shrunk_score", "模型內收縮百分位", 3),
                ("avg_confidence", "資料信心", 3),
                ("avg_kpi_coverage", "核心 KPI 覆蓋率", 5),
                ("avg_revenue_change", "營收趨勢", 4),
                ("avg_margin_change", "毛利率趨勢", 1),
                ("avg_real_fcf_yield", "Real FCF yield", 1),
            ):
                change = delta(group, previous, field)
                if change is not None:
                    deltas[field] = change
                    if change >= threshold:
                        reasons.append(f"{label}較前期增加 {change:g}")
                        signal += 1.5
        else:
            reasons.append("模型版本或群組首次建立基準")
            signal += 0.5

        if signal >= 4 and reasons:
            confidence = "HIGH" if signal >= 7 else "MEDIUM" if signal >= 5 else "LOW"
            candidates.append(
                {
                    "key": key,
                    "name": group["name"],
                    "kind": group["kind"],
                    "theme_id": group.get("theme_id"),
                    "status": "RESEARCH_CLUSTER_SIGNAL",
                    "confidence": confidence,
                    "signal_score": round(signal, 2),
                    "reasons": reasons[:7],
                    "metrics": {
                        field: group.get(field)
                        for field in (
                            "count", "eligible", "research_queue",
                            "avg_raw_score", "avg_shrunk_score",
                            "avg_confidence", "avg_kpi_coverage",
                            "avg_revenue_change", "avg_margin_change",
                            "avg_real_fcf_yield",
                        )
                    },
                    "metric_coverage": coverage,
                    "deltas": deltas,
                    "top": group.get("top", []),
                    "model_keys": group.get("model_keys", []),
                    "model_version": group.get("model_version"),
                }
            )
    candidates.sort(
        key=lambda item: (-float(item.get("signal_score") or 0), str(item.get("name") or ""))
    )
    return candidates[:12]


def build_payload(screen: Path, shortlist: Path, universe: Path, history: Path | None = None) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    screen_rows = records(screen)
    priority_upgrade_required = any(
        str(row.get("Research_Priority_Version") or "")
        != RESEARCH_PRIORITY_VERSION
        for row in screen_rows
    )
    if priority_upgrade_required:
        annotate_research_priorities(
            screen_rows,
            queue_size=GLOBAL_RESEARCH_QUEUE_SIZE,
        )
        shortlist_set = {
            ticker(row)
            for row in screen_rows
            if ticker(row) and truthy(row.get("Global_Research_Queue"))
        }
    else:
        shortlist_set = {ticker(row) for row in records(shortlist) if ticker(row)}
    cik_map = {
        ticker(row): str(row.get("CIK") or "").replace(".0", "").zfill(10)
        for row in records(universe)
        if ticker(row)
    }
    stocks = []
    for row in screen_rows:
        symbol = ticker(row)
        if symbol:
            metadata = parse_object(row.get("Metric_Metadata_JSON"))
            normalized = {
                **row,
                "Ticker": symbol,
                "CIK": cik_map.get(symbol, ""),
                "IsShortlist": symbol in shortlist_set,
                "Metric_Metadata": metadata,
                "Data_Integrity_Summary": data_integrity_summary(metadata),
                "Data_Integrity_Complete": truthy(row.get("Metric_Data_Complete")),
            }
            for field, meta in metadata.items():
                if field in normalized and isinstance(meta, dict):
                    normalized[field] = clean(meta.get("value"))
            normalized["Core_KPI_Summary"] = core_kpi_summary(normalized)
            normalized["Core_Metric_Coverage_pct"] = core_metric_coverage(normalized)
            normalized["Valuation_Summary"] = valuation_summary(normalized)
            positives, risks, tasks = driver_risk_summary(normalized)
            normalized["Top_Positive_Drivers"] = positives
            normalized["Top_Risks"] = risks
            normalized["Manual_Review_Tasks"] = tasks
            stocks.append(normalized)

    stocks.sort(
        key=lambda row: (
            as_number(row.get("Research_Priority_Rank")) is None,
            as_number(row.get("Research_Priority_Rank")) or math.inf,
            -score(row),
            ticker(row),
        )
    )
    for rank, row in enumerate(stocks, 1):
        row["Dashboard_Rank"] = rank
        row["Rank"] = clean(row.get("Research_Priority_Rank")) or rank
    attach_theme_tags(stocks)
    integrity_totals = {
        status: sum(
            int(row.get("Data_Integrity_Summary", {}).get(status, 0))
            for row in stocks
        )
        for status in (
            "VALID", "ESTIMATED", "MISSING", "NOT_APPLICABLE",
            "ABSTAIN", "STALE", "INVALID",
        )
    }
    groups = build_trend_groups(stocks)
    history_payload = load_trend_history(history)
    previous_policy_version = history_payload.get("policy_version")
    model_version_changed = bool(
        history_payload
        and previous_policy_version != DASHBOARD_TREND_POLICY_VERSION
    )
    if history_payload.get("policy_version") != DASHBOARD_TREND_POLICY_VERSION:
        history_payload = {}
    generated_at = datetime.now(timezone.utc).isoformat()
    coverage_medians = {
        "data_confidence": median_value(stocks, "Data_Confidence_Score"),
        "core_metric_all": median_value(stocks, "Core_Metric_Coverage_pct"),
        "core_metric_eligible": median_value(
            [row for row in stocks if truthy(row.get("Long_Term_Eligible"))],
            "Core_Metric_Coverage_pct",
        ),
        "core_metric_research_queue": median_value(
            [row for row in stocks if row.get("IsShortlist")],
            "Core_Metric_Coverage_pct",
        ),
    }
    metadata = {
        "decision_timestamp": next(
            (row.get("Decision_Timestamp") for row in stocks if row.get("Decision_Timestamp")),
            "UNKNOWN",
        ),
        "price_data_date": max(
            (str(row.get("Price_Data_Date")) for row in stocks if row.get("Price_Data_Date")),
            default="UNKNOWN",
        ),
        "latest_sec_availability_date": max(
            (
                str(row.get("Latest_SEC_Availability_Date"))
                for row in stocks
                if row.get("Latest_SEC_Availability_Date")
                and str(row.get("Latest_SEC_Availability_Date")).upper() != "UNKNOWN"
            ),
            default="UNKNOWN",
        ),
        "universe_version": next(
            (row.get("Universe_Version") for row in stocks if row.get("Universe_Version")),
            "UNKNOWN",
        ),
        "model_version": RESEARCH_PRIORITY_VERSION,
        "git_commit": next(
            (row.get("Git_Commit") for row in stocks if row.get("Git_Commit")),
            "UNKNOWN",
        ),
    }
    total_metric_statuses = sum(integrity_totals.values())
    status_ratios = {
        status: round(count / total_metric_statuses, 4) if total_metric_statuses else 0.0
        for status, count in integrity_totals.items()
    }
    return {
        "generated_at": generated_at,
        "research_priority_version": RESEARCH_PRIORITY_VERSION,
        "trend_policy_version": DASHBOARD_TREND_POLICY_VERSION,
        "metadata": metadata,
        "score_disclaimer": "模型內百分位，不代表跨模型未來報酬已校準",
        "stats": {
            "total": len(stocks),
            "eligible": sum(truthy(row.get("Long_Term_Eligible")) for row in stocks),
            "research_queue": len(shortlist_set),
            "shortlist": len(shortlist_set),
            "metric_status_counts": integrity_totals,
            "metric_status_ratios": status_ratios,
            "coverage_medians": coverage_medians,
        },
        "trend_baseline": {
            "status": "與上次比較" if history_payload.get("groups") else "建立基準中",
            "previous_generated_at": history_payload.get("generated_at"),
            "previous_policy_version": previous_policy_version,
            "model_version_changed": model_version_changed,
        },
        "emerging_candidates": build_emerging_candidates(groups, history_payload),
        "trend_groups": groups,
        "themes": build_theme_summary(stocks),
        "stocks": stocks,
    }, groups


PAGE = r'''<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Alpha Engine 長期價值研究台</title>
<style>
:root{--bg:#07111f;--panel:#0d1b2d;--line:#29425f;--text:#eef5ff;--muted:#9fb2c9;--blue:#62adff;--green:#58d5a0;--yellow:#f3cb67;--red:#ff7b86}*{box-sizing:border-box}body{margin:0;background:linear-gradient(145deg,#07111f,#0a1b2e);color:var(--text);font-family:system-ui,"Noto Sans TC",sans-serif}.shell{width:min(1320px,calc(100% - 24px));margin:auto;padding:24px 0 56px}.panel{background:rgba(13,27,45,.96);border:1px solid var(--line);border-radius:18px;box-shadow:0 16px 44px #0005}.hero{padding:24px;display:flex;justify-content:space-between;gap:20px;align-items:end}.hero h1{margin:4px 0 8px;font-size:clamp(28px,4vw,46px)}.hero p,.muted{color:var(--muted)}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:14px 0}.stat{padding:16px}.stat small{display:block;color:var(--muted)}.stat strong{font-size:27px}.theme,.emerging{padding:16px;margin:14px 0}.theme h2,.emerging h2{margin:0 0 4px}.themegrid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-top:12px}.emerginggrid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:12px}.theme-card,.emerging-card{padding:12px;border:1px solid var(--line);border-radius:14px;background:#091827;cursor:pointer;min-height:190px}.emerging-card{cursor:default}.theme-card:hover,.theme-card.active{border-color:var(--blue);box-shadow:0 0 0 1px var(--blue) inset}.theme-card b,.emerging-card b{display:block;margin-bottom:7px}.theme-card small,.emerging-card small{display:block;color:var(--muted);line-height:1.45}.theme-card .nums,.emerging-card .nums{margin-top:9px;color:var(--green)}.layers,.reasons{margin-top:8px;font-size:12px;color:var(--muted);line-height:1.5}.tools{display:grid;grid-template-columns:2fr 1fr 1.3fr 1fr auto;gap:9px;padding:12px}input,select,button{font:inherit;border:1px solid var(--line);border-radius:11px;padding:10px;background:#091827;color:var(--text)}button{cursor:pointer}button:hover{border-color:var(--blue)}.watch{display:flex;gap:9px;align-items:center;padding:12px;margin-top:12px}.watch input{max-width:230px}.table{margin-top:12px;overflow:auto}table{width:100%;border-collapse:collapse}th,td{padding:12px;border-bottom:1px solid var(--line);text-align:left}th{font-size:12px;color:var(--muted)}tbody tr{cursor:pointer}tbody tr:hover{background:#17314b88}.badge{display:inline-block;padding:3px 7px;border:1px solid var(--line);border-radius:999px;font-size:12px;margin:1px}.good{color:var(--green)}.warn{color:var(--yellow)}.danger{color:var(--red)}dialog{width:min(1000px,calc(100% - 20px));max-height:90vh;padding:0;background:#0a1728;color:var(--text);border:1px solid var(--line);border-radius:18px}dialog::backdrop{background:#0010}.head{position:sticky;top:0;background:#0a1728ee;padding:15px;display:flex;justify-content:space-between;border-bottom:1px solid var(--line)}.body{padding:18px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}.metric,.box{padding:12px;border:1px solid var(--line);border-radius:12px;background:var(--panel)}.metric small{display:block;color:var(--muted);margin-bottom:5px}.section{margin-top:18px}.actions{display:flex;gap:8px;flex-wrap:wrap}a{color:var(--blue)}.footer{text-align:center;color:var(--muted);font-size:12px;margin-top:18px}@media(max-width:980px){.themegrid,.emerginggrid{grid-template-columns:repeat(2,1fr)}.tools{grid-template-columns:1fr 1fr}}@media(max-width:760px){.hero{display:block}.stats,.grid{grid-template-columns:repeat(2,1fr)}.watch{align-items:stretch;flex-direction:column}.watch input{max-width:none}.optional{display:none}}@media(max-width:520px){.stats,.grid,.tools,.themegrid,.emerginggrid{grid-template-columns:1fr}}
.integrity{padding:16px;margin:14px 0}.integrity h2{margin:0 0 10px}.integritygrid{display:grid;grid-template-columns:repeat(7,1fr);gap:8px}.integritygrid span{padding:10px;border:1px solid var(--line);background:#091827}.integritygrid small{display:block;color:var(--muted)}@media(max-width:760px){.integritygrid{grid-template-columns:repeat(2,1fr)}}
</style></head><body><main class="shell">
<section class="hero panel"><div><span class="badge good">不依賴外部 AI</span><h1>Alpha Engine 長期價值研究台</h1><p>Mode C 整理數據與風險；候選風口偵測只負責提出線索，不自動定案。</p></div><div class="muted">QQQ 40% · VOO 30% · 主動個股 0–30%<br>單股上限 3% · 單一主動產業上限 9%</div></section>
<section class="stats"><div class="stat panel"><small>本次分析</small><strong id="total">0</strong></div><div class="stat panel"><small>研究合格</small><strong id="eligible">0</strong></div><div class="stat panel"><small>分散 shortlist</small><strong id="shortlist">0</strong></div><div class="stat panel"><small>我的追蹤</small><strong id="watchCount">0</strong></div></section>
<section class="integrity panel"><h2>資料完整性</h2><div id="integritySummary" class="integritygrid"></div></section>
<section class="emerging panel"><h2>候選風口偵測</h2><p class="muted" id="baseline"></p><div id="emergingCards" class="emerginggrid"></div></section>
<section class="theme panel"><h2>主題擴散鏈：一階 → 二階 → 三階</h2><p class="muted">用主題找研究方向，不用主題替你下買賣決定。點卡片可篩出相關公司，卡片會列出每一層的高分候選。</p><div id="themeCards" class="themegrid"></div></section>
<section class="tools panel"><input id="search" placeholder="搜尋 ticker、產業、結論或主題"><select id="view"><option value="all">全部結果</option><option value="complete">只看完整資料</option><option value="estimated">含估計值</option><option value="missing">缺資料</option><option value="specialized">專用模型</option><option value="general">一般企業</option><option value="shortlist">Shortlist</option><option value="eligible">研究合格</option><option value="abstain">暫不判斷</option><option value="watch">我的追蹤</option></select><select id="theme"><option value="">不限主題</option></select><select id="min"><option value="0">不限分數</option><option>60</option><option>70</option><option>75</option><option>80</option></select><button id="refresh">重新整理</button></section>
<section class="watch panel"><input id="addTicker" maxlength="10" placeholder="輸入想追蹤的 ticker"><button id="add">加入追蹤</button><button id="export">匯出追蹤名單</button><span class="muted">名單只存於你的瀏覽器，不會上傳。</span></section>
<section class="table panel"><table><thead><tr><th>排名</th><th>Ticker</th><th>分數</th><th>結論</th><th class="optional">產業</th><th class="optional">主題</th><th class="optional">Real FCF Yield</th><th>追蹤</th></tr></thead><tbody id="rows"></tbody></table><p id="empty" class="muted" style="padding:20px" hidden>沒有符合條件的股票。</p></section><div id="updated" class="footer"></div></main>
<dialog id="detail"><div class="head"><strong id="detailTitle"></strong><button id="close">關閉</button></div><div class="body" id="detailBody"></div></dialog>
<script id="payload" type="application/json">__DATA__</script><script>
const data=JSON.parse(document.querySelector('#payload').textContent),stocks=data.stocks||[],themes=data.themes||[],emerging=data.emerging_candidates||[],map=new Map(stocks.map(x=>[x.Ticker,x])),themeMap=new Map(themes.map(x=>[x.id,x])),key='alphaEngineWatchlistV1';let watch=load();
const $=s=>document.querySelector(s),e=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),n=v=>{if(v===null||v===undefined||v===''||typeof v==='boolean')return null;v=Number(v);return Number.isFinite(v)?v:null},f=(v,d=2,s='')=>n(v)===null?'N/A':n(v).toFixed(d)+s,yes=v=>v===true||String(v).toLowerCase()==='true',norm=v=>String(v||'').toUpperCase().replace(/[^A-Z0-9.-]/g,'').slice(0,10),tags=x=>(x.Theme_Tags||[]),themeIds=x=>(x.Theme_Ids||[]),layerMap=x=>(x.Theme_Layer_Map||{}),layerTags=x=>(x.Theme_Layer_Tags||[]);
const metricStatusLabels={MISSING:'缺資料',NOT_APPLICABLE:'不適用',ABSTAIN:'暫不判定',STALE:'資料過期',INVALID:'無效'};
function metricText(x,k,d=2,s=''){let m=(x.Metric_Metadata||{})[k]||{},status=String(m.status||'').toUpperCase(),v=Object.prototype.hasOwnProperty.call(m,'value')?m.value:x[k];if(metricStatusLabels[status])return metricStatusLabels[status];let number=n(v),shown=number===null?(v===null||v===undefined||v===''?'缺資料':String(v)):number.toFixed(d)+s;return status==='ESTIMATED'?'估 '+shown:shown}
function metricHtml(x,k,d=2,s=''){let m=(x.Metric_Metadata||{})[k]||{},tip=[m.status,m.reason,m.as_of,m.source_method,(m.evidence_ids||[]).length?`evidence ${(m.evidence_ids||[]).length}`:''].filter(Boolean).join(' | ');return `<span${tip?` title="${e(tip)}"`:''}>${e(metricText(x,k,d,s))}</span>`}
function load(){try{return new Set((JSON.parse(localStorage.getItem(key)||'[]')).map(norm).filter(Boolean))}catch{return new Set()}}function save(){localStorage.setItem(key,JSON.stringify([...watch].sort()));$('#watchCount').textContent=watch.size}
function toggle(t){t=norm(t);if(!t)return;watch.has(t)?watch.delete(t):watch.add(t);save();render()}
function tagBadges(x){let t=tags(x);return t.length?t.slice(0,2).map(a=>`<span class="badge warn">${e(a)}</span>`).join(''):'<span class="muted">無</span>'}
function layerBadges(x){let t=layerTags(x);return t.length?t.map(a=>`<span class="badge good">${e(a)}</span>`).join(''):'<span class="muted">尚無層級</span>'}
function integrityCount(x,status){return Number((x.Data_Integrity_Summary||{})[status]||0)}
function visible(){let q=$('#search').value.toLowerCase(),v=$('#view').value,m=Number($('#min').value),theme=$('#theme').value;let out=stocks.filter(x=>{let hay=[x.Ticker,x.Sector,x.Industry,x.Status,x.Verdict,x.Model_Route,x.Industry_Model_Key,x.Decision_State,...tags(x),...layerTags(x)].join(' ').toLowerCase(),special=x.Industry_Model_Key&&x.Industry_Model_Key!=='GENERAL_CORPORATE';return(!q||hay.includes(q))&&(m<=0||(n(x.Long_Term_Score)??-1)>=m)&&(!theme||themeIds(x).includes(theme))&&(v!=='complete'||yes(x.Data_Integrity_Complete))&&(v!=='estimated'||integrityCount(x,'ESTIMATED')>0)&&(v!=='missing'||integrityCount(x,'MISSING')>0)&&(v!=='specialized'||special)&&(v!=='general'||!special)&&(v!=='shortlist'||yes(x.IsShortlist))&&(v!=='eligible'||yes(x.Long_Term_Eligible))&&(v!=='abstain'||x.Decision_State==='ABSTAIN')&&(v!=='watch'||watch.has(x.Ticker))});if(v==='watch')for(let t of watch)if(!map.has(t)&&(!q||t.toLowerCase().includes(q)))out.push({Ticker:t,Status:'尚未在本次篩選資料',Theme_Tags:[],Theme_Ids:[],Theme_Layer_Map:{},Theme_Layer_Tags:[]});return out}
function renderEmerging(){let base=data.trend_baseline||{};$('#baseline').textContent=`狀態：${base.status||'建立基準中'}${base.previous_generated_at?'；上次資料：'+new Date(base.previous_generated_at).toLocaleString('zh-TW'):''}。候選只代表待查線索，不是買入訊號。`;$('#emergingCards').innerHTML=emerging.length?emerging.map(c=>`<div class="emerging-card"><b>${e(c.name)}</b><span class="badge warn">${e(c.status)}</span><span class="badge ${c.confidence==='高'?'good':'warn'}">信心 ${e(c.confidence)}</span><div class="nums">分數 ${f(c.signal_score,1)} · ${e(c.kind)}</div><small>Top: ${(c.top||[]).map(e).join(', ')||'待資料'}</small><div class="reasons">${(c.reasons||[]).map(r=>'• '+e(r)).join('<br>')}</div></div>`).join(''):'<div class="emerging-card"><b>尚無明確候選風口</b><small>如果是第一次跑，系統正在建立基準；下一次開始會比較產業、行業與主題層級是否變強。</small></div>'}
function renderThemes(){let sel=$('#theme');sel.innerHTML='<option value="">不限主題</option>'+themes.map(t=>`<option value="${e(t.id)}">${e(t.name)}</option>`).join('');$('#themeCards').innerHTML=themes.map(t=>`<div class="theme-card" data-theme="${e(t.id)}"><b>${e(t.name)}</b><small>${e(t.thesis)}</small><div class="nums">${t.count} 檔 · eligible ${t.eligible} · shortlist ${t.shortlist}</div><small>Top: ${(t.top||[]).map(e).join(', ')||'待資料'}</small><div class="layers">${(t.layers||[]).map(l=>`${e(l.name)}：${(l.top||[]).slice(0,5).map(e).join(', ')||'待資料'}`).join('<br>')}</div></div>`).join('');document.querySelectorAll('[data-theme]').forEach(card=>card.onclick=()=>{$('#theme').value=card.dataset.theme;render()})}
function render(){let out=visible(),active=$('#theme').value;document.querySelectorAll('[data-theme]').forEach(card=>card.classList.toggle('active',card.dataset.theme===active));$('#rows').innerHTML=out.map(x=>`<tr data-t="${e(x.Ticker)}"><td>${x.Rank||'-'}</td><td><b>${e(x.Ticker)}</b> ${yes(x.IsShortlist)?'<span class="badge good">Shortlist</span>':''}</td><td>${metricHtml(x,'Long_Term_Score')}</td><td>${e(x.Verdict||x.Status||'待查')}</td><td class="optional">${e(x.Sector||x.Industry||'N/A')}</td><td class="optional">${tagBadges(x)}</td><td class="optional">${metricHtml(x,'Real_FCF_Yield_pct',2,'%')}</td><td><button data-w="${e(x.Ticker)}">${watch.has(x.Ticker)?'移除':'加入'}</button></td></tr>`).join('');$('#empty').hidden=out.length>0;document.querySelectorAll('tr[data-t]').forEach(r=>r.onclick=a=>{if(!a.target.dataset.w)openDetail(r.dataset.t)});document.querySelectorAll('[data-w]').forEach(b=>b.onclick=a=>{a.stopPropagation();toggle(b.dataset.w)})}
const fields=[['資料信心','Data_Confidence_Score'],['EDGAR acceptance 比例 (0-1)','Evidence_AcceptedAt_Ratio'],['長期綜合分數','Long_Term_Score'],['專用模型分數','Industry_Model_Score'],['專用模型覆蓋','Industry_Model_Coverage','%'],['品質分數','Quality_Score'],['價值分數','Value_Score'],['市場預期分數','Expectations_Score'],['營運拐點分數','Operating_Inflection_Score'],['資本配置分數','Capital_Allocation_Score'],['風險扣分','Risk_Penalty'],['TTM OCF','TTM_OCF_B','B'],['全部 CapEx','Dynamic_CapEx_B','B'],['Maintenance CapEx','Maintenance_CapEx_B','B'],['Growth CapEx','Growth_CapEx_B','B'],['CapEx / D&A','CapEx_to_DnA_x','x'],['TTM SBC','TTM_SBC_B','B'],['Real FCF Yield','Real_FCF_Yield_pct','%'],['全額 CapEx FCF Yield','Conservative_Real_FCF_Yield_pct','%'],['總負債','Total_Debt_B','B'],['現金','Cash_B','B'],['淨負債','Net_Debt_B','B'],['ICR','ICR','x'],['30% 壓力 ICR','Stress_ICR_30x','x'],['30% 壓力淨負債 / EBITDA','NetDebt_to_Stress_EBITDA_30x','x'],['30% 壓力 Real FCF','Stress_Real_FCF_30_B','B'],['ROIC','ROIC_pct','%'],['ROCE','ROCE_pct','%'],['5Y Real FCF 正值年數','Real_FCF_Positive_Years_5Y'],['5Y OCF / 淨利','OCF_to_NetIncome_5Y','x'],['EV / EBITDA','EV_EBITDA_x','x'],['P / E','PE_x','x'],['最新毛利率','GM_Latest_pct','%'],['三季毛利變化','GM_3Q_Change_pp','pp'],['三季營收變化','Rev_3Q_Change_pct','%'],['DSI 季變化','DSI_QoQ_Change_pct','%'],['DSI 年變化','DSI_YoY_Change_pct','%'],['一年股數變化','Share_Count_Change_pct','%'],['三年股數變化','Share_Count_Change_3Y_pct','%'],['一年拆股因子','Share_Split_Factor_1Y','x'],['三年拆股因子','Share_Split_Factor_3Y','x'],['隱含 EBITDA CAGR','Implied_EBITDA_CAGR_3Y_pct','%'],['CAGR 動態上限','Implied_CAGR_Limit_pct','%'],['CAGR 餘裕','Implied_CAGR_Headroom_pct','pp'],['反向估值必要報酬','Reverse_DCF_Required_Return_pct','%'],['EBITDA -30% 下檔','EBITDA_Drawdown_30_pct','%']];
fields.push(['DSO','DSO_Days',' days'],['DPO','DPO_Days',' days'],['Cash conversion cycle','Cash_Conversion_Cycle_Days',' days'],['AR growth vs revenue','AR_vs_Revenue_Growth_Gap_pp','pp'],['AP growth vs COGS','AP_vs_COGS_Growth_Gap_pp','pp'],['Working-capital risk penalty','Working_Capital_Risk_Penalty'],['Gross buyback','TTM_Gross_Buyback_B','B'],['Stock issuance','TTM_Stock_Issuance_B','B'],['Acquisition stock consideration','Acquisition_Stock_Consideration_B','B']);
function sec(x){let c=String(x.CIK||'').replace(/\D/g,'');return c?`https://www.sec.gov/edgar/browse/?CIK=${encodeURIComponent(c)}&owner=exclude&action=getcompany`:''}function yahoo(t,p=''){return`https://finance.yahoo.com/quote/${encodeURIComponent(t)}/${p}`}function obj(raw){try{let v=JSON.parse(raw||'{}');return v&&typeof v==='object'?v:{}}catch{return{}}}function specializedPanel(x){if(!x.Industry_Model_Key||x.Industry_Model_Key==='GENERAL_CORPORATE')return'';let metrics=obj(x.Industry_Model_Metrics_JSON),components=obj(x.Industry_Model_Components_JSON),items=o=>Object.entries(o).map(([k,v])=>`<div class="metric"><small>${e(k)}</small><b>${typeof v==='number'?f(v):e(v??'N/A')}</b></div>`).join('');return`<div class="section"><h3>${e(x.Industry_Model_Key)} 專用模型</h3><div class="grid">${items(components)}${items(metrics)}</div><div class="box">${e(x.Industry_Model_Hard_Failures||'無硬性失敗')}<br>${e(x.Industry_Model_Warnings||'無模型警示')}</div></div>`}
function prompt(x){let themeText=tags(x).length?tags(x).join('、'):'無明確主題標籤',layerText=layerTags(x).length?layerTags(x).join('；'):'尚無層級標籤',special=x.Industry_Model_Key&&x.Industry_Model_Key!=='GENERAL_CORPORATE';return[`請以中長期價值投資角度研究 ${x.Ticker}，不要直接下買賣指令。`,special?`專用模型：${x.Industry_Model_Key}；分數：${f(x.Industry_Model_Score)}；覆蓋：${f(x.Industry_Model_Coverage,1,'%')}；資料信心：${f(x.Data_Confidence_Score)}。`:`Quant 分數：${f(x.Long_Term_Score)}；品質：${f(x.Quality_Score)}；價值：${f(x.Value_Score)}；資本配置：${f(x.Capital_Allocation_Score)}。`,`主題標籤：${themeText}。受益層級：${layerText}。請判斷它是一階、二階或三階受益者，還是只是被題材蹭到。`,special?`專用指標：${x.Industry_Model_Metrics_JSON||'待查'}；警示：${x.Data_Quality_Flags||'無'}。`:`Real FCF Yield：${f(x.Real_FCF_Yield_pct,2,'%')}；ICR：${f(x.ICR,2,'x')}；ROIC：${f(x.ROIC_pct,2,'%')}。`,`債務來源：${x.Debt_Source_Method||'待查'}；ICR 口徑：${x.ICR_Method||'待查'}。`,'請用最新官方財報回答：','1. 三句話投資論點。','2. 最強反方論點。','3. thesis 失效條件。','4. 悲觀、基準、樂觀情境。','5. 核對該產業專用 KPI、現金流與資產負債表警訊。','6. 股數稀釋與管理層資本配置。','7. 與 QQQ/VOO 的重疊，以及額外持有理由。','8. 主題供應鏈位置、訂單能見度、瓶頸、二階受益是否已開始進財報，以及是否已反映在估值。','9. 尚無法確認的監管、產業或公司自訂揭露。'].join('\n')}
async function copy(t){try{await navigator.clipboard.writeText(t)}catch{let a=document.createElement('textarea');a.value=t;document.body.append(a);a.select();document.execCommand('copy');a.remove()}alert('已複製 AI 研究提示。')}
function openDetail(t){let x=map.get(t)||{Ticker:t,Status:'尚未在本次篩選資料',Theme_Tags:[],Theme_Ids:[],Theme_Layer_Map:{},Theme_Layer_Tags:[]},s=sec(x),related=themeIds(x).map(id=>themeMap.get(id)).filter(Boolean);$('#detailTitle').textContent=x.Ticker;$('#detailBody').innerHTML=`<div class="actions"><button id="dw">${watch.has(t)?'移除追蹤':'加入追蹤'}</button><button id="cp">複製 AI 研究提示</button>${s?`<a target="_blank" rel="noopener" href="${s}">SEC 官方財報</a>`:''}<a target="_blank" rel="noopener" href="${yahoo(t,'financials')}">財務報表頁</a><a target="_blank" rel="noopener" href="${yahoo(t)}">市場資料頁</a></div><div class="section"><h3>主題擴散鏈位置</h3><div class="box">${layerBadges(x)}<br><br>${related.map(r=>`<b>${e(r.name)} / ${e(layerMap(x)[r.id]||'待判斷')}</b><br>${e(r.thesis)}<br>要問：${(r.questions||[]).map(e).join('；')}`).join('<br><br>')||'尚無主題標籤，請從基本面而非題材開始。'}</div></div><div class="section"><h3>模型結論</h3><div class="box">${e(x.Scoring_Framework||x.Model_Route||'未分類')} / ${e(x.Decision_State||'待查')}<br>${e(x.Research_Action||x.Verdict||x.Status||'待查')}</div></div>${specializedPanel(x)}<div class="section"><h3>財務與風險指標</h3><div class="grid">${fields.map(a=>`<div class="metric"><small>${a[0]}</small><b>${f(x[a[1]],2,a[2]||'')}</b></div>`).join('')}</div></div><div class="section"><h3>資料品質與待查事項</h3><div class="box">債務來源：${e(x.Debt_Source_Method||'待查')}<br>ICR 口徑：${e(x.ICR_Method||'待查')}<br>${e(x.GM_Diagnosis||'')}<br>${e(x.Data_Quality_Flags||'無資料品質警示')}<br>${e(x.Data_Confidence_Reasons||'無信心降級原因')}<br>${e(x.Agent_Tasks||'請從 SEC 官方財報開始查核。')}</div></div>`;$('#dw').onclick=()=>toggle(t);$('#cp').onclick=()=>copy(prompt(x));if(!$('#detail').open)$('#detail').showModal()}
const integrityLabels={VALID:'有效數值',ESTIMATED:'估計值',MISSING:'缺資料',NOT_APPLICABLE:'不適用',ABSTAIN:'暫不判斷',STALE:'資料過舊',INVALID:'計算無效'},integrityTotals=(data.stats||{}).metric_status_counts||{};$('#integritySummary').innerHTML=Object.entries(integrityLabels).map(([k,label])=>`<span><small>${e(label)}</small><b>${e(integrityTotals[k]||0)}</b></span>`).join('');
$('#total').textContent=data.stats.total;$('#eligible').textContent=data.stats.eligible;$('#shortlist').textContent=data.stats.shortlist;$('#updated').textContent='資料更新：'+new Date(data.generated_at).toLocaleString('zh-TW')+' · 本網站僅供研究，不是投資建議。';renderEmerging();renderThemes();['#search','#view','#theme','#min'].forEach(s=>$(s).oninput=render);$('#refresh').onclick=()=>{$('#theme').value='';render()};$('#add').onclick=()=>{let t=norm($('#addTicker').value);if(t){watch.add(t);$('#addTicker').value='';save();render();openDetail(t)}};$('#addTicker').onkeydown=a=>{if(a.key==='Enter')$('#add').click()};$('#export').onclick=()=>{let text=[...watch].sort().join('\n'),a=document.createElement('a');a.href=URL.createObjectURL(new Blob([text+(text?'\n':'')],{type:'text/plain;charset=utf-8'}));a.download='alpha-engine-watchlist.txt';a.click();URL.revokeObjectURL(a.href)};$('#close').onclick=()=>$('#detail').close();save();render();
</script></body></html>'''


MODERN_DASHBOARD_SCRIPT = r'''
<script>
const researchScore=x=>n(x.Shrunk_Within_Model_Percentile)??-1;
function visible(){let q=$('#search').value.toLowerCase(),v=$('#view').value,m=Number($('#min').value),theme=$('#theme').value;let out=stocks.filter(x=>{let hay=[x.Ticker,x.Sector,x.Industry,x.Status,x.Verdict,x.Model_Route,x.Industry_Model_Key,x.Decision_State,x.Research_Action_State,x.Decision_Reason_Code,...tags(x),...layerTags(x)].join(' ').toLowerCase(),special=x.Industry_Model_Key&&x.Industry_Model_Key!=='GENERAL_CORPORATE';return(!q||hay.includes(q))&&(m<=0||researchScore(x)>=m)&&(!theme||themeIds(x).includes(theme))&&(v!=='complete'||yes(x.Data_Integrity_Complete))&&(v!=='estimated'||integrityCount(x,'ESTIMATED')>0)&&(v!=='missing'||integrityCount(x,'MISSING')>0)&&(v!=='specialized'||special)&&(v!=='general'||!special)&&(v!=='shortlist'||yes(x.IsShortlist))&&(v!=='eligible'||yes(x.Long_Term_Eligible))&&(v!=='abstain'||x.Decision_State==='ABSTAIN')&&(v!=='watch'||watch.has(x.Ticker))});if(v==='watch')for(let t of watch)if(!map.has(t)&&(!q||t.toLowerCase().includes(q)))out.push({Ticker:t,Status:'尚未出現在本次資料',Theme_Tags:[],Theme_Ids:[],Theme_Layer_Map:{},Theme_Layer_Tags:[],Core_KPI_Summary:[]});return out}
function coreKpiHtml(x){let items=x.Core_KPI_Summary||[];return items.map(k=>{let value=n(k.value);let shown=value===null?e(k.value??'N/A'):e(value.toFixed(2)+(k.suffix||''));return `<span class="badge" title="${e(k.label)}">${e(k.label)} ${shown}</span>`}).join('')||'<span class="muted">N/A</span>'}
function listHtml(items){return(items||[]).length?`<ul>${items.map(item=>`<li>${e(item)}</li>`).join('')}</ul>`:'<span class="muted">None reported</span>'}
function render(){let out=visible(),active=$('#theme').value;document.querySelectorAll('[data-theme]').forEach(card=>card.classList.toggle('active',card.dataset.theme===active));$('#rows').innerHTML=out.map(x=>`<tr data-t="${e(x.Ticker)}"><td>${x.Research_Priority_Rank||'-'}</td><td><b>${e(x.Ticker)}</b> ${yes(x.IsShortlist)?'<span class="badge good">Queue</span>':''}</td><td>${e(x.Industry_Model_Key||'GENERAL_CORPORATE')}</td><td>${metricHtml(x,'Raw_Model_Score')}</td><td>${metricHtml(x,'Shrunk_Within_Model_Percentile',2,'%')}</td><td>${metricHtml(x,'Data_Confidence_Score')}</td><td class="optional">${coreKpiHtml(x)}</td><td class="optional">${e(x.Valuation_Summary||'N/A')}</td><td>${e(x.Research_Action_State||x.Decision_State||'N/A')}</td><td><button data-w="${e(x.Ticker)}">${watch.has(x.Ticker)?'移除':'加入'}</button></td></tr>`).join('');$('#empty').hidden=out.length>0;document.querySelectorAll('tr[data-t]').forEach(r=>r.onclick=a=>{if(!a.target.dataset.w)openDetail(r.dataset.t)});document.querySelectorAll('[data-w]').forEach(b=>b.onclick=a=>{a.stopPropagation();toggle(b.dataset.w)})}
function renderEmerging(){let base=data.trend_baseline||{};$('#baseline').textContent=`基準狀態：${base.model_version_changed?'模型版本變更，重新建立基準':(base.status||'首次建立')}${base.previous_generated_at?'；前次 '+new Date(base.previous_generated_at).toLocaleString('zh-TW'):''}。訊號只使用模型內收縮百分位，模型改版會重設基準。`;$('#emergingCards').innerHTML=emerging.length?emerging.map(c=>{let m=c.metrics||{},delta=Object.entries(c.deltas||{}).map(([k,v])=>`${k} ${v>=0?'+':''}${v}`).join(' · ')||'首次基準';return `<div class="emerging-card" data-candidate="${e(c.key)}"><b>${e(c.name)}</b><span class="badge warn">研究線索</span><span class="badge ${c.confidence==='HIGH'?'good':'warn'}">${e(c.confidence)}</span><div class="nums">${e(c.kind)} · 樣本 ${e(m.count??'N/A')} · eligible ${e(m.eligible??'N/A')} · queue ${e(m.research_queue??'N/A')}</div><small>模型：${(c.model_keys||[]).map(e).join(', ')||'N/A'}<br>Raw ${f(m.avg_raw_score,1)} · Shrunk ${f(m.avg_shrunk_score,1)} · Confidence ${f(m.avg_confidence,1)} · KPI coverage ${f(m.avg_kpi_coverage,1,'%')}<br>變化：${e(delta)} · 版本 ${e(c.model_version||'N/A')}</small><div class="reasons">${(c.reasons||[]).map(r=>'• '+e(r)).join('<br>')}</div></div>`}).join(''):'<div class="emerging-card"><b>本期沒有達門檻的研究群聚</b><small>這不是負面投資訊號，只代表目前資料尚未形成足夠強的模型內群聚。</small></div>'}
const legacyOpenDetail=openDetail;
openDetail=function(t){legacyOpenDetail(t);let x=map.get(t)||{},body=$('#detailBody');if(!body)return;let source=x.Combined_Ratio_Source_Status||'N/A',stress=x.Specialized_Stress_Status||x.P_and_C_Stress_Status||'N/A';body.insertAdjacentHTML('afterbegin',`<div class="section"><h3>研究優先序與待查事項</h3><div class="grid"><div class="metric"><small>模型</small><b>${e(x.Industry_Model_Key||'GENERAL_CORPORATE')}</b></div><div class="metric"><small>Raw model score</small><b>${f(x.Raw_Model_Score)}</b></div><div class="metric"><small>Within-model percentile</small><b>${f(x.Within_Model_Percentile,2,'%')}</b></div><div class="metric"><small>Shrunk percentile</small><b>${f(x.Shrunk_Within_Model_Percentile,2,'%')}</b></div><div class="metric"><small>Data confidence</small><b>${f(x.Data_Confidence_Score)}</b></div><div class="metric"><small>Core KPI coverage</small><b>${f(x.Core_Metric_Coverage_pct,1,'%')}</b></div><div class="metric"><small>Research state</small><b>${e(x.Research_Action_State||'N/A')}</b></div><div class="metric"><small>Specialized stress</small><b>${e(stress)}</b></div><div class="metric"><small>Research round</small><b>${e(x.Research_Priority_Round||'N/A')}</b></div><div class="metric"><small>Cross-model calibration</small><b>${e(x.Cross_Model_Calibration_Status||'UNCALIBRATED')}</b></div></div><div class="box">${coreKpiHtml(x)}<br><b>Valuation</b>: ${e(x.Valuation_Summary||'N/A')}<br><b>Stress scenario</b>: ${e(x.Specialized_Stress_Scenario||'N/A')}<br><b>Stress reason</b>: ${e(x.Specialized_Stress_Reason||'N/A')}<br><small>Combined-ratio source: ${e(source)} · model version: ${e(data.research_priority_version||'N/A')}</small></div><div class="grid section"><div class="box"><b>Top 3 Positive Drivers</b>${listHtml(x.Top_Positive_Drivers)}</div><div class="box"><b>Top 3 Risks</b>${listHtml(x.Top_Risks)}</div><div class="box"><b>Manual Review Tasks</b>${listHtml(x.Manual_Review_Tasks)}</div></div><div class="box"><b>Required Missing Metrics</b>: ${e(x.Required_Missing_Metrics||'None')}<br><b>Optional Missing Metrics</b>: ${e(x.Optional_Missing_Metrics||'None')}<br><b>Decision reason</b>: ${e(x.Decision_Reason_Code||'N/A')}</div></div>`)};
const integrityOpenDetail=openDetail;
openDetail=function(t){integrityOpenDetail(t);let x=map.get(t)||{},body=$('#detailBody');if(!body||x.Industry_Model_Key!=='GENERAL_CORPORATE')return;body.insertAdjacentHTML('afterbegin',`<div class="section"><h3>營運資金、稀釋與組合閘門</h3><div class="box"><b>Working-capital quality</b>: ${e(x.Working_Capital_Quality_State||'MISSING')} · coverage ${f((n(x.Working_Capital_Quality_Coverage)||0)*100,1,'%')} · penalty ${f(x.Working_Capital_Risk_Penalty)}<br><b>Acquisition issuance</b>: ${e(x.Acquisition_Issuance_Attribution_Status||'MISSING')} · ${e(x.Acquisition_Issuance_Reconciliation_Status||'NOT_APPLICABLE')} · accretion review ${yes(x.Acquisition_Accretion_Review_Required)?'required':'not triggered'}<br><b>Portfolio fit</b>: ${e(x.Portfolio_Fit_Status||'PENDING_INPUT')} · ${e(x.Portfolio_Fit_Reason||'holdings and risk inputs are required')}<br><small>As of ${e(x.Portfolio_Fit_AsOf||'N/A')} · age ${f(x.Portfolio_Fit_Input_Age_Days,0,'d')} · ETF top-10 ${yes(x.Portfolio_ETF_Top10_Overlap)?'yes':'no'} · correlation ${e(x.Portfolio_Correlation_Stress_Status||'N/A')}</small><br><small>${e(x.Working_Capital_Quality_Reasons||'No auditable AR/AP growth comparison')}</small></div></div>`)};
const coverage=(data.stats||{}).coverage_medians||{},ratios=(data.stats||{}).metric_status_ratios||{},integrity=document.querySelector('#integritySummary');if(integrity){integrity.innerHTML=Object.entries(integrityLabels).map(([k,label])=>`<span><small>${e(label)}</small><b>${e(integrityTotals[k]||0)} (${f((ratios[k]||0)*100,1,'%')})</b></span>`).join('');integrity.insertAdjacentHTML('beforeend',`<span><small>Median Core Metric Coverage</small><b>${f(coverage.core_metric_all,1,'%')}</b></span><span><small>Eligible Core Metric Coverage</small><b>${f(coverage.core_metric_eligible,1,'%')}</b></span><span><small>Queue Core Metric Coverage</small><b>${f(coverage.core_metric_research_queue,1,'%')}</b></span>`)}
const runMeta=data.metadata||{},hero=document.querySelector('.hero');if(hero&&!document.querySelector('#runMetadata'))hero.insertAdjacentHTML('afterend',`<section id="runMetadata" class="panel" style="padding:12px;margin-top:12px"><div class="grid"><div><small>Decision Timestamp</small><br>${e(runMeta.decision_timestamp||'UNKNOWN')}</div><div><small>Price Data Date</small><br>${e(runMeta.price_data_date||'UNKNOWN')}</div><div><small>Latest SEC Availability Date</small><br>${e(runMeta.latest_sec_availability_date||'UNKNOWN')}</div><div><small>Universe Version</small><br>${e(runMeta.universe_version||'UNKNOWN')}</div><div><small>Model Version</small><br>${e(runMeta.model_version||'UNKNOWN')}</div><div><small>Git Commit</small><br>${e(runMeta.git_commit||'UNKNOWN')}</div></div></section>`);
document.querySelector('#shortlist').textContent=(data.stats||{}).research_queue||0;document.querySelector('#shortlist').previousElementSibling.textContent='Global Research Queue';
document.querySelector('#updated').insertAdjacentHTML('beforebegin',`<div class="footer warn">${e(data.score_disclaimer||'模型內百分位，不代表跨模型未來報酬已校準')}</div>`);
renderEmerging();render();
</script>
'''


def modernize_page(page: str) -> str:
    page = re.sub(
        r'(<section class="emerging panel"><h2>).*?(</h2>)',
        r'\1高分群聚與研究線索\2',
        page,
        count=1,
    )
    page = re.sub(
        r'(<section class="table panel"><table><thead><tr>).*?(</tr></thead>)',
        r'\1<th>研究排名</th><th>Ticker</th><th>模型</th><th>Raw score</th><th>模型內收縮百分位</th><th>資料信心</th><th class="optional">核心 KPI</th><th class="optional">估值</th><th>研究狀態</th><th>Watch</th>\2',
        page,
        count=1,
    )
    page = page.replace(
        "metricHtml(x, 'Real_FCF_Yield_pct'",
        "metricHtml(x,'Real_FCF_Yield_pct'",
    )
    return page.replace("</body></html>", MODERN_DASHBOARD_SCRIPT + "</body></html>")


def build_dashboard(screen: Path, shortlist: Path, universe: Path, output: Path, history: Path | None = None) -> Path:
    payload, trend_groups = build_payload(screen, shortlist, universe, history)
    output.mkdir(parents=True, exist_ok=True)
    embedded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    index = output / "index.html"
    index.write_text(modernize_page(PAGE).replace("__DATA__", embedded), encoding="utf-8")
    (output / "data.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / ".nojekyll").write_text("", encoding="utf-8")
    save_trend_history(history, trend_groups, payload["generated_at"])
    return index


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the static Mode C research dashboard.")
    parser.add_argument("--screen", default="mode_c_screen.csv")
    parser.add_argument("--shortlist", default="mode_c_shortlist.csv")
    parser.add_argument("--universe", default="qualified_universe.csv")
    parser.add_argument("--output", default="public")
    parser.add_argument("--history", default=".mode_c_state/dashboard_trend_history.json")
    parser.add_argument("--no-history", action="store_true", help="Do not read or update emerging-theme trend history.")
    args = parser.parse_args()
    history = None if args.no_history else Path(args.history)
    index = build_dashboard(Path(args.screen), Path(args.shortlist), Path(args.universe), Path(args.output), history)
    print(f"Dashboard written to {index}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
