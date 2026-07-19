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
        "name": "AI æ™¶ç‰‡äºŒéšå—ç›Šéˆ",
        "thesis": "AI æ™¶ç‰‡éœ€æ±‚è‹¥æŒçºŒï¼Œå—ç›Šè€…ä¸åª GPUï¼›å…ˆé€²è£½ç¨‹ã€è¨­å‚™ã€è¨˜æ†¶é«”ã€é›»æºç®¡ç†ã€æ•£ç†±ã€é€£æ¥å™¨ã€è¢«å‹•å…ƒä»¶èˆ‡æ¸¬è©¦é‡æ¸¬éƒ½è¦ä¸€èµ·æª¢æŸ¥ã€‚",
        "segments": ["æ™¶ç‰‡è¨­è¨ˆ", "æ™¶åœ“ä»£å·¥", "åŠå°é«”è¨­å‚™", "HBM/è¨˜æ†¶é«”", "é›»æº/é¡æ¯”", "æ•£ç†±/é›»åŠ›", "é€£æ¥å™¨/è¢«å‹•å…ƒä»¶", "æ¸¬è©¦é‡æ¸¬"],
        "tickers": ["NVDA", "AMD", "AVGO", "MRVL", "TSM", "ASML", "AMAT", "LRCX", "KLAC", "MU", "WDC", "STX", "ADI", "TXN", "MPWR", "MCHP", "ON", "APH", "TEL", "VSH", "GLW", "KEYS", "TER", "VRT", "ETN", "ANET"],
        "keywords": ["semiconductor", "semiconductor equipment", "memory", "electronic components", "communication equipment", "computer hardware", "electrical equipment", "specialty industrial machinery", "connectors", "passive", "analog", "power", "thermal", "testing", "measurement"],
        "layers": [
            {"name": "ä¸€éšï¼šç›´æ¥ç®—åŠ›èˆ‡æ ¸å¿ƒæ™¶ç‰‡", "tickers": ["NVDA", "AMD", "AVGO", "MRVL", "TSM"], "keywords": ["semiconductors"]},
            {"name": "äºŒéšï¼šç“¶é ¸é›¶çµ„ä»¶èˆ‡è¨­å‚™", "tickers": ["ASML", "AMAT", "LRCX", "KLAC", "MU", "WDC", "STX", "ADI", "TXN", "MPWR", "MCHP", "ON", "APH", "TEL", "VSH", "GLW", "KEYS", "TER", "ANET"], "keywords": ["semiconductor equipment", "memory", "electronic components", "communication equipment", "computer hardware", "connectors", "passive", "analog", "power", "testing", "measurement"]},
            {"name": "ä¸‰éšï¼šè³‡æ–™ä¸­å¿ƒåŸºå»ºå¤–æº¢", "tickers": ["VRT", "ETN", "TT", "HUBB", "PWR"], "keywords": ["electrical equipment", "thermal", "cooling", "specialty industrial machinery"]},
        ],
        "questions": ["ç‡Ÿæ”¶æ˜¯å¦çœŸçš„è·Ÿ AI/è³‡æ–™ä¸­å¿ƒ capex é€£å‹•ï¼Ÿ", "ä¾›çµ¦æ˜¯å¦åƒç·Šåˆ°èƒ½ç¶­æŒæ¯›åˆ©ï¼Ÿ", "é€™æ˜¯å¿…éœ€å“ã€è¦æ ¼å‡ç´šï¼Œé‚„æ˜¯ä¸€æ¬¡æ€§æ‹‰è²¨ï¼Ÿ"],
    },
    {
        "id": "data_center_power_cooling",
        "name": "è³‡æ–™ä¸­å¿ƒé›»åŠ›èˆ‡æ•£ç†±",
        "thesis": "AI cluster åŠŸè€—æé«˜å¾Œï¼Œç“¶é ¸å¯èƒ½å¾æ™¶ç‰‡è½‰åˆ°é›»åŠ›ã€UPSã€é…é›»ã€æ¶²å†·ã€ç©ºèª¿èˆ‡æ©Ÿæˆ¿å·¥ç¨‹ã€‚",
        "segments": ["é›»åŠ›è¨­å‚™", "UPS/é…é›»", "æ¶²å†·/æ•£ç†±", "æ©Ÿæˆ¿å·¥ç¨‹", "å·¥æ¥­è‡ªå‹•åŒ–"],
        "tickers": ["VRT", "ETN", "TT", "CARR", "JCI", "PWR", "HUBB", "EMR", "PH", "ROK"],
        "keywords": ["electrical equipment", "building products", "specialty industrial machinery", "engineering", "construction", "hvac", "thermal", "cooling", "automation"],
        "layers": [
            {"name": "ä¸€éšï¼šæ©Ÿæˆ¿é›»åŠ›èˆ‡æ•£ç†±ä¸»è¨­å‚™", "tickers": ["VRT", "ETN", "TT", "CARR"], "keywords": ["electrical equipment", "hvac", "thermal", "cooling"]},
            {"name": "äºŒéšï¼šé…é›»ã€å·¥ç¨‹èˆ‡é—œéµé›¶çµ„ä»¶", "tickers": ["PWR", "HUBB", "JCI", "PH"], "keywords": ["engineering", "construction", "building products", "specialty industrial machinery"]},
            {"name": "ä¸‰éšï¼šå·¥æ¥­è‡ªå‹•åŒ–èˆ‡ç¶­é‹å¤–æº¢", "tickers": ["EMR", "ROK"], "keywords": ["automation", "industrial"]},
        ],
        "questions": ["è¨‚å–®æ˜¯å¦ä¾†è‡ªè³‡æ–™ä¸­å¿ƒè€Œéä¸€èˆ¬æ™¯æ°£å¾ªç’°ï¼Ÿ", "æ¯›åˆ©æ˜¯å¦å› ç«¶çˆ­åŠ åŠ‡è€Œå›è½ï¼Ÿ", "capex é€±æœŸåè½‰æ™‚ç‡Ÿæ”¶æœƒæ‰å¤šæ·±ï¼Ÿ"],
    },
    {
        "id": "electrification_grid",
        "name": "é›»æ°£åŒ–èˆ‡é›»ç¶²å‡ç´š",
        "thesis": "AIã€EVã€å†å·¥æ¥­åŒ–èˆ‡é›»åŠ›éœ€æ±‚æˆé•·å¯èƒ½æ¨å‹•é›»ç¶²ã€è®Šå£“å™¨ã€é…é›»èˆ‡å·¥æ¥­é›»æ°£è¨­å‚™ã€‚",
        "segments": ["é›»ç¶²è¨­å‚™", "é…é›»", "å·¥æ¥­é›»æ°£", "å·¥ç¨‹æœå‹™"],
        "tickers": ["ETN", "HUBB", "PWR", "EMR", "PH", "ROK", "TT", "VRT", "ABBNY", "SIEGY"],
        "keywords": ["electrical equipment", "utilities regulated electric", "engineering", "industrial", "automation", "specialty industrial machinery"],
        "layers": [
            {"name": "ä¸€éšï¼šé›»ç¶²èˆ‡é…é›»è¨­å‚™", "tickers": ["ETN", "HUBB", "VRT", "ABBNY", "SIEGY"], "keywords": ["electrical equipment"]},
            {"name": "äºŒéšï¼šå·¥ç¨‹æœå‹™èˆ‡å·¥æ¥­é›¶çµ„ä»¶", "tickers": ["PWR", "PH", "TT"], "keywords": ["engineering", "specialty industrial machinery"]},
            {"name": "ä¸‰éšï¼šè‡ªå‹•åŒ–èˆ‡æ•ˆç‡å‡ç´š", "tickers": ["EMR", "ROK"], "keywords": ["automation", "industrial"]},
        ],
        "questions": ["éœ€æ±‚æ˜¯é•·é€±æœŸåŸºå»ºé‚„æ˜¯çŸ­æœŸè£œåº«å­˜ï¼Ÿ", "å…¬å¸æ˜¯å¦æœ‰å®šåƒ¹æ¬Šèˆ‡ backlogï¼Ÿ", "ä¼°å€¼æ˜¯å¦å·²æŠŠå¤šå¹´æˆé•·ä¸€æ¬¡åæ˜ ï¼Ÿ"],
    },
    {
        "id": "cybersecurity_ai_software",
        "name": "AI è»Ÿé«”èˆ‡è³‡å®‰é˜²ç·š",
        "thesis": "ä¼æ¥­å°å…¥ AI èˆ‡é›²ç«¯å¾Œï¼Œè³‡å®‰ã€è³‡æ–™æ²»ç†ã€é›²ç«¯å¹³å°èˆ‡è‡ªå‹•åŒ–è»Ÿé«”å¯èƒ½æˆç‚ºä¼´éš¨æ”¯å‡ºã€‚",
        "segments": ["è³‡å®‰", "é›²ç«¯å¹³å°", "è³‡æ–™æ²»ç†", "ä¼æ¥­è‡ªå‹•åŒ–"],
        "tickers": ["PANW", "FTNT", "CRWD", "ZS", "NET", "DDOG", "SNOW", "NOW", "MSFT", "ADBE", "CRM"],
        "keywords": ["software - infrastructure", "software - application", "cybersecurity", "cloud", "data", "security"],
        "layers": [
            {"name": "ä¸€éšï¼šè³‡å®‰èˆ‡é›²ç«¯é˜²ç·š", "tickers": ["PANW", "FTNT", "CRWD", "ZS", "NET"], "keywords": ["cybersecurity", "security", "software - infrastructure"]},
            {"name": "äºŒéšï¼šè³‡æ–™å¹³å°èˆ‡å¯è§€æ¸¬æ€§", "tickers": ["DDOG", "SNOW", "NOW"], "keywords": ["data", "cloud", "software - application"]},
            {"name": "ä¸‰éšï¼šä¼æ¥­è»Ÿé«” AI åŠŸèƒ½å¤–æº¢", "tickers": ["MSFT", "ADBE", "CRM"], "keywords": ["software - application"]},
        ],
        "questions": ["ARR/ç•™å­˜ç‡æ˜¯å¦æ”¯æŒä¼°å€¼ï¼Ÿ", "SBC æ˜¯å¦åƒæ‰ FCFï¼Ÿ", "AI æ˜¯å¦æå‡è­·åŸæ²³ï¼Œé‚„æ˜¯å£“ä½åƒ¹æ ¼ï¼Ÿ"],
    },
    {
        "id": "healthcare_quality_defensive",
        "name": "é«˜å“è³ªé†«ç™‚èˆ‡é˜²å®ˆæˆé•·",
        "thesis": "ç•¶å¸‚å ´å¤ªé›†ä¸­åœ¨ç§‘æŠ€æ™‚ï¼Œé†«ç™‚å™¨æã€è¨ºæ–·ã€è£½è—¥èˆ‡ç”Ÿå‘½ç§‘å­¸å·¥å…·å¯ä½œç‚ºå“è³ªå‹åˆ†æ•£ç ”ç©¶æ± ã€‚",
        "segments": ["é†«ç™‚å™¨æ", "ç”Ÿå‘½ç§‘å­¸å·¥å…·", "è£½è—¥", "è¨ºæ–·"],
        "tickers": ["LLY", "NVO", "ISRG", "TMO", "DHR", "SYK", "VRTX", "ABT", "MDT", "MRK", "JNJ"],
        "keywords": ["healthcare", "medical", "diagnostics", "drug manufacturers", "biotechnology", "life sciences"],
        "layers": [
            {"name": "ä¸€éšï¼šè—¥ç‰©èˆ‡æ ¸å¿ƒç™‚æ³•", "tickers": ["LLY", "NVO", "VRTX", "MRK", "JNJ"], "keywords": ["drug manufacturers", "biotechnology"]},
            {"name": "äºŒéšï¼šé†«ç™‚å™¨æèˆ‡æ‰‹è¡“å¹³å°", "tickers": ["ISRG", "SYK", "ABT", "MDT"], "keywords": ["medical"]},
            {"name": "ä¸‰éšï¼šç”Ÿå‘½ç§‘å­¸å·¥å…·èˆ‡è¨ºæ–·", "tickers": ["TMO", "DHR"], "keywords": ["diagnostics", "life sciences"]},
        ],
        "questions": ["å°ˆåˆ©/ç”¢å“é€±æœŸé¢¨éšªæ˜¯å¦é›†ä¸­ï¼Ÿ", "ä¼°å€¼æ˜¯å¦å·²åæ˜ ç®¡ç·šæˆåŠŸï¼Ÿ", "Real FCF èˆ‡ç ”ç™¼æŠ•å…¥æ˜¯å¦å¥åº·ï¼Ÿ"],
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
                ("Benefits/premium", "benefits_to_premium_pct", "%"),
                ("Equity/assets", "equity_to_assets_pct", "%"),
                ("ROE", "roe_pct", "%"),
            ),
            "REIT_MORTGAGE": (
                ("Recurring earnings yield", "recurring_earnings_yield_pct", "%"),
                ("Assets/equity", "assets_to_equity_x", "x"),
                ("Dividend payout", "dividend_payout_pct", "%"),
            ),
            "REGULATED_UTILITY": (
                ("Interest coverage", "interest_coverage_x", "x"),
                ("Debt/capital", "debt_to_capital_pct", "%"),
                ("Dividend payout", "dividend_payout_pct", "%"),
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
                ("EBIT margin", "ebit_margin_pct", "%"),
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
      ×Ÿ}ÒÚ$z{-®éÜj×BÃ"ÂrRr—ŞûÉ´”5.ûÉ¢G¶b‡‚ä”5"Ã"Âw‚r—ŞûÉµ$ô”>ûÉ¢G¶b‡‚å$ô”5÷7BÃ"ÂrRr—Ş8&ÆX+^X¹Kènk©ûÉ¢G·‚äFV'Eõ6÷W&6UôÖWF†öGÇÂ~[è^iúRwŞûÉ´”5"Xú>[éûÉ¢G·‚ä”5%ôÖWF†öGÇÂ~[è^iúRwŞ8&Â~Š¸¾yJiÈikZéik‹*ZY¹îzÙNûÉ¢rÂsâKˆXú^Š›h©^‹8~Š¹n›¹î8"rÂs"âiÈ[Ë~XøŞikŠ¹n›¹î8"rÂs2âF†W6—2ZKiXj)ŞK»n8"rÂsBâh+.Šx8Yû®k©n8jˆ.Šxh8^Z(>8"rÂsRâj[ŞŠ›.yJ.jZŞ[yJ‚µ8xûî˜ykXˆˆ~‹8~yJ.‹*X+^ŠŠÚnŠˆ®8"rÂsbâˆ*i[zˆ˜x¾ˆˆ~zêyn[N‹8~iÊÎ˜XŞ{Úî8"rÂsrâˆˆrõdôòy¨N˜xŞyh®ûÈÎKº^Xø®šŞZInhÈiÈynyK8"rÂs‚âK‹¾šÎKé¾hx˜øKØŞ{Úî8Šˆ.Yjîˆ;ŞŠh¾[ªn8y;nš8K¨Î™¨îXù~y¸®iŠşY
n[{.™h¾Zx¾˜.‹*ZûÈÎKº^Xø®iŠşY
n[{.XøŞiŠYÊKËXÎ8"rÂs’â[	®xJk9^z+®Š¨Şy¨Nyº>zê8yJ.jZŞh‰nXZÎXûˆz®Šˆ.húŞ™Ë.8"uÒæ¦ö–â‚uÆâr—ĞĞ¦7–æ2gVæ7F–öâ6÷’‡B—·G'—¶v—Bæf–vF÷"æ6Æ—&ö&Bçw&—FUFW‡B‡B—Ö6F6‡¶ÆWBÖFö7VÖVçBæ7&VFTVÆVÖVçB‚wFW‡F&Vr“¶çfÇVS×C¶Fö7VÖVçBæ&öG’æVæB†“¶ç6VÆV7B‚“¶Fö7VÖVçBæW†V46öÖÖæB‚v6÷’r“¶ç&VÖ÷fR‚—ÖÆW'B‚~[{.ŠH~Š;Ò’z	Nz›nhùzK®8"r—ĞĞ¦gVæ7F–öâ÷VäFWF–Â‡B—¶ÆWBƒÖÖævWB‡B—ÇÇµF–6¶W#§BÅ7FGW3¢~[	®iÊ®YÊiÊÎjÊzú˜‹8~ii’rÅF†VÖUõFw3¥µÒÅF†VÖUô–G3¥µÒÅF†VÖUôÆ–W%ôÖ§·ÒÅF†VÖUôÆ–W%õFw3¥µ×ÒÇ3×6V2‡‚’Ç&VÆFVC×F†VÖT–G2‡‚’æÖ†–CÓçF†VÖTÖævWB†–B’’æf–ÇFW"„&ööÆVâ“²B‚r6FWF–ÅF—FÆRr’çFW‡D6öçFVçC×‚åF–6¶W#²B‚r6FWF–Ä&öG’r’æ–ææW$…DÔÃÖÆF—b6Æ73Ò&7F–öç2#ãÆ'WGFöâ–CÒ&Gr#âG·vF6‚æ†2‡B“ò~z{¾™šN‹ûŞ‹šBs¢~XªXZ^‹ûŞ‹šBwÓÂö'WGFöããÆ'WGFöâ–CÒ&7#îŠH~Š;Ò’z	Nz›nhùzK£Âö'WGFöãâG·3öÆF&vWCÒ%ö&Ææ²"&VÃÒ&æö÷VæW""‡&VcÒ"G·7Ò#å4T2Zéik‹*ZÂöæ¢rwÓÆF&vWCÒ%ö&Ææ²"&VÃÒ&æö÷VæW""‡&VcÒ"G·–†öò‡BÂvf–ææ6–Ç2r—Ò#î‹*X¹ZŠšÂöãÆF&vWCÒ%ö&Ææ²"&VÃÒ&æö÷VæW""‡&VcÒ"G·–†öò‡B—Ò#î[ˆ.ZN‹8~iišÂöãÂöF—cãÆF—b6Æ73Ò'6V7F–öâ#ãÆƒ3îK‹¾šÎi;NiZ>˜øKØŞ{ÚãÂöƒ3ãÆF—b6Æ73Ò&&÷‚#âG¶Æ–W$&FvW2‡‚—ÓÆ'#ãÆ'#âG·&VÆFVBæÖ‡#ÓæÆ#âG¶R‡"ææÖR—ÒòG¶R†Æ–W$Ö‡‚•·"æ–E×ÇÂ~[è^XŠNikrr—ÓÂö#ãÆ'#âG¶R‡"çF†W6—2—ÓÆ'#îŠhYXşûÉ¢G²‡"çVW7F–öç7ÇÅµÒ’æÖ†R’æ¦ö–â‚~ûÉ²r—Ö’æ¦ö–â‚sÆ'#ãÆ'#âr—ÇÂ~[	®xJK‹¾šÎj‰{NûÈÎŠ¸¾[éîYû®iÊÎ™Ú.ˆÎ™ÙîšÎiÙ™h¾Zx¾8"wÓÂöF—cãÂöF—cãÆF—b6Æ73Ò'6V7F–öâ#ãÆƒ3îjŠYè¾{YŠ¹cÂöƒ3ãÆF—b6Æ73Ò&&÷‚#âG¶R‡‚å66÷&–æuôg&ÖWv÷&·ÇÇ‚äÖöFVÅõ&÷WFWÇÂ~iÊ®Xˆnšâr—ÒòG¶R‡‚äFV6—6–öåõ7FFWÇÂ~[è^iúRr—ÓÆ'#âG¶R‡‚å&W6V&6…ô7F–öçÇÇ‚åfW&F–7GÇÇ‚å7FGW7ÇÂ~[è^iúRr—ÓÂöF—cãÂöF—câG·7V6–Æ—¦VEæVÂ‡‚—ÓÆF—b6Æ73Ò'6V7F–öâ#ãÆƒ3î‹*X¹ˆˆ~š*™ª®hÈ~j‰“Âöƒ3ãÆF—b6Æ73Ò&w&–B#âG¶f–VÆG2æÖ†ÓæÆF—b6Æ73Ò&ÖWG&–2#ãÇ6ÖÆÃâG¶³×ÓÂ÷6ÖÆÃãÆ#âG¶b‡…¶³ÕÒÃ"Æ³%×ÇÂrr—ÓÂö#ãÂöF—cæ’æ¦ö–â‚rr—ÓÂöF—cãÂöF—cãÆF—b6Æ73Ò'6V7F–öâ#ãÆƒ3î‹8~iiY8‹:®ˆˆ~[è^iú^K¨¾šSÂöƒ3ãÆF—b6Æ73Ò&&÷‚#îX+^X¹Kènk©ûÉ¢G¶R‡‚äFV'Eõ6÷W&6UôÖWF†öGÇÂ~[è^iúRr—ÓÆ'#ä”5"Xú>[éûÉ¢G¶R‡‚ä”5%ôÖWF†öGÇÂ~[è^iúRr—ÓÆ'#âG¶R‡‚ätÕôF–væ÷6—7ÇÂrr—ÓÆ'#âG¶R‡‚äFFõVÆ—G•ôfÆw7ÇÂ~xJ‹8~iiY8‹:®ŠÚnzK¢r—ÓÆ'#âG¶R‡‚äFFô6öæf–FVæ6Uõ&V6öç7ÇÂ~xJKú[ø>™˜Ş{I®XéşYºr—ÓÆ'#âG¶R‡‚ävVçEõF6·7ÇÂ~Š¸¾[éâ4T2Zéik‹*Z™h¾Zx¾iú^j8"r—ÓÂöF—cãÂöF—cæ²B‚r6Grr’æöæ6Æ–6³Ò‚“ÓçFövvÆR‡B“²B‚r67r’æöæ6Æ–6³Ò‚“Óæ6÷’‡&ö×B‡‚’“¶–b‚B‚r6FWF–Âr’æ÷Vâ’B‚r6FWF–Âr’ç6†÷tÖöFÂ‚—ĞĞ¦6öç7B–çFVw&—G”Æ&VÇ3×µdÄ”C¢~iÈiXi[XÂrÄU5D”ÔDTC¢~KËŠˆXÂrÄÔ•54”äs¢~{Ë®‹8~ii’rÄäõEôÄ”4$ÄS¢~KˆŞ˜yJ‚rÄ%5D”ã¢~iª¾KˆŞXŠNikrrÅ5DÄS¢~‹8~ii˜îˆˆ¢rÄ”ådÄ”C¢~Šˆzé~xJiX‚wÒÆ–çFVw&—G•F÷FÇ3Ò†FFç7FG7ÇÇ·Ò’æÖWG&–5÷7FGW5ö6÷VçG7ÇÇ·Ó²B‚r6–çFVw&—G•7VÖÖ'’r’æ–ææW$…DÔÃÔö&¦V7BæVçG&–W2†–çFVw&—G”Æ&VÇ2’æÖ‚…¶²ÆÆ&VÅÒ“ÓæÇ7ããÇ6ÖÆÃâG¶R†Æ&VÂ—ÓÂ÷6ÖÆÃãÆ#âG¶R†–çFVw&—G•F÷FÇ5¶µ×ÇÃ—ÓÂö#ãÂ÷7ãæ’æ¦ö–â‚rr“°¢B‚r7F÷FÂr’çFW‡D6öçFVçCÖFFç7FG2çF÷FÃ²B‚r6VÆ–v–&ÆRr’çFW‡D6öçFVçCÖFFç7FG2æVÆ–v–&ÆS²B‚r76†÷'FÆ—7Br’çFW‡D6öçFVçCÖFFç7FG2ç6†÷'FÆ—7C²B‚r7WFFVBr’çFW‡D6öçFVçCÒ~‹8~iii»NikûÉ¢r¶æWrFFR†FFævVæW&FVEöB’çFôÆö6ÆU7G&–ær‚w¦‚ÕErr’²r+riÊÎ{k.z¹X8^Ké¾z	Nz›nûÈÎKˆŞiŠşh©^‹8~[»®ŠÛ8"s·&VæFW$VÖW&v–ær‚“·&VæFW%F†VÖW2‚“µ²r76V&6‚rÂr7f–WrrÂr7F†VÖRrÂr6Ö–âuÒæf÷$V6‚‡3ÓâB‡2’æöæ–çWC×&VæFW"“²B‚r7&Vg&W6‚r’æöæ6Æ–6³Ò‚“Óç²B‚r7F†VÖRr’çfÇVSÒrs·&VæFW"‚—Ó²B‚r6FBr’æöæ6Æ–6³Ò‚“Óç¶ÆWBCÖæ÷&Ò‚B‚r6FEF–6¶W"r’çfÇVR“¶–b‡B—·vF6‚æFB‡B“²B‚r6FEF–6¶W"r’çfÇVSÒrs·6fR‚“·&VæFW"‚“¶÷VäFWF–Â‡B—×Ó²B‚r6FEF–6¶W"r’æöæ¶W–F÷vãÖÓç¶–b†æ¶W“ÓÓÒtVçFW"r’B‚r6FBr’æ6Æ–6²‚—Ó²B‚r6W‡÷'Br’æöæ6Æ–6³Ò‚“Óç¶ÆWBFW‡CÕ²ââçvF6…Òç6÷'B‚’æ¦ö–â‚uÆâr’ÆÖFö7VÖVçBæ7&VFTVÆVÖVçB‚vr“¶æ‡&VcÕU$Âæ7&VFTö&¦V7EU$Â†æWr&Æö"…·FW‡B²‡FW‡CòuÆâs¢rr•ÒÇ·G—S¢wFW‡B÷Æ–ã¶6†'6WC×WFbÓ‚wÒ’“¶æF÷væÆöCÒvÇ†ÖVæv–æR×vF6†Æ—7BçG‡Bs¶æ6Æ–6²‚“µU$Âç&Wfö¶Tö&¦V7EU$Â†æ‡&Vb—Ó²B‚r66Æ÷6Rr’æöæ6Æ–6³Ò‚“ÓâB‚r6FWF–Âr’æ6Æ÷6R‚“·6fR‚“·&VæFW"‚“°£Â÷67&—CãÂö&öG“ãÂö‡FÖÃârrp  ¤ÔôDU$åôD4„$ô$Eõ45$•BÒ"rrp£Ç67&—Cà¦6öç7B&W6V&6…66÷&S×ƒÓæâ‡‚å6‡'Væµõv—F†–åôÖöFVÅõW&6VçF–ÆR“óòÓ°¦gVæ7F–öâf—6–&ÆR‚—¶ÆWBÒB‚r76V&6‚r’çfÇVRçFôÆ÷vW$66R‚’ÇcÒB‚r7f–Wrr’çfÇVRÆÓÔçVÖ&W"‚B‚r6Ö–âr’çfÇVR’ÇF†VÖSÒB‚r7F†VÖRr’çfÇVS¶ÆWB÷WC×7Fö6·2æf–ÇFW"‡ƒÓç¶ÆWB†“Õ·‚åF–6¶W"Ç‚å6V7F÷"Ç‚ä–æGW7G'’Ç‚å7FGW2Ç‚åfW&F–7BÇ‚äÖöFVÅõ&÷WFRÇ‚ä–æGW7G'•ôÖöFVÅô¶W’Ç‚äFV6—6–öåõ7FFRÇ‚å&W6V&6…ô7F–öåõ7FFRÇ‚äFV6—6–öåõ&V6öåô6öFRÂââçFw2‡‚’ÂââæÆ–W%Fw2‡‚•Òæ¦ö–â‚rr’çFôÆ÷vW$66R‚’Ç7V6–Ã×‚ä–æGW7G'•ôÖöFVÅô¶W’bg‚ä–æGW7G'•ôÖöFVÅô¶W’ÓÒttTäU$Åô4õ%õ$DRs·&WGW&â‚ÇÆ†’æ–æ6ÇVFW2‡’’bb†ÓÃÓÇÇ&W6V&6…66÷&R‡‚“ãÖÒ’bb‚F†VÖWÇÇF†VÖT–G2‡‚’æ–æ6ÇVFW2‡F†VÖR’’bb‡bÓÒv6ö×ÆWFRwÇÇ–W2‡‚äFFô–çFVw&—G•ô6ö×ÆWFR’’bb‡bÓÒvW7F–ÖFVBwÇÆ–çFVw&—G”6÷VçB‡‚ÂtU5D”ÔDTBr“ã’bb‡bÓÒvÖ—76–ærwÇÆ–çFVw&—G”6÷VçB‡‚ÂtÔ•54”ärr“ã’bb‡bÓÒw7V6–Æ—¦VBwÇÇ7V6–Â’bb‡bÓÒvvVæW&ÂwÇÂ7V6–Â’bb‡bÓÒw6†÷'FÆ—7BwÇÇ–W2‡‚ä—56†÷'FÆ—7B’’bb‡bÓÒvVÆ–v–&ÆRwÇÇ–W2‡‚äÆöæuõFW&ÕôVÆ–v–&ÆR’’bb‡bÓÒv'7F–âwÇÇ‚äFV6—6–öåõ7FFSÓÓÒt%5D”âr’bb‡bÓÒwvF6‚wÇÇvF6‚æ†2‡‚åF–6¶W"’—Ò“¶–b‡cÓÓÒwvF6‚r–f÷"†ÆWBBöbvF6‚––b‚Öæ†2‡B’bb‚ÇÇBçFôÆ÷vW$66R‚’æ–æ6ÇVFW2‡’’–÷WBçW6‚‡µF–6¶W#§BÅ7FGW3¢~[	®iÊ®X{®xûîYÊiÊÎjÊ‹8~ii’rÅF†VÖUõFw3¥µÒÅF†VÖUô–G3¥µÒÅF†VÖUôÆ–W%ôÖ§·ÒÅF†VÖUôÆ–W%õFw3¥µÒÄ6÷&Uôµ•õ7VÖÖ'“¥µ×Ò“·&WGW&â÷WGĞ¦gVæ7F–öâ6÷&T·”‡FÖÂ‡‚—¶ÆWB—FV×3×‚ä6÷&Uôµ•õ7VÖÖ'—ÇÅµÓ·&WGW&â—FV×2æÖ†³Óç¶ÆWBfÇVSÖâ†²çfÇVR“¶ÆWB6†÷vã×fÇVSÓÓÖçVÆÃöR†²çfÇVSóòtâôr“¦R‡fÇVRçFôf—†VBƒ"’²†²ç7Vff—‡ÇÂrr’“·&WGW&âÇ7â6Æ73Ò&&FvR"F—FÆSÒ"G¶R†²æÆ&VÂ—Ò#âG¶R†²æÆ&VÂ—ÒG·6†÷vçÓÂ÷7ãæÒ’æ¦ö–â‚rr—ÇÂsÇ7â6Æ73Ò&×WFVB#äâôÂ÷7ãâwĞ¦gVæ7F–öâÆ—7D‡FÖÂ†—FV×2—·&WGW&â†—FV×7ÇÅµÒ’æÆVæwFƒöÇVÃâG¶—FV×2æÖ†—FVÓÓæÆÆ“âG¶R†—FVÒ—ÓÂöÆ“æ’æ¦ö–â‚rr—ÓÂ÷VÃæ¢sÇ7â6Æ73Ò&×WFVB#äæöæR&W÷'FVCÂ÷7ãâwĞ¦gVæ7F–öâ&VæFW"‚—¶ÆWB÷WC×f—6–&ÆR‚’Æ7F—fSÒB‚r7F†VÖRr’çfÇVS¶Fö7VÖVçBçVW'•6VÆV7F÷$ÆÂ‚u¶FF×F†VÖUÒr’æf÷$V6‚†6&CÓæ6&Bæ6Æ74Æ—7BçFövvÆR‚v7F—fRrÆ6&BæFF6WBçF†VÖSÓÓÖ7F—fR’“²B‚r7&÷w2r’æ–ææW$…DÔÃÖ÷WBæÖ‡ƒÓæÇG"FF×CÒ"G¶R‡‚åF–6¶W"—Ò#ãÇFCâG·‚å&W6V&6…õ&–÷&—G•õ&æ·ÇÂrÒwÓÂ÷FCãÇFCãÆ#âG¶R‡‚åF–6¶W"—ÓÂö#âG·–W2‡‚ä—56†÷'FÆ—7B“òsÇ7â6Æ73Ò&&FvRvööB#åVWVSÂ÷7ãâs¢rwÓÂ÷FCãÇFCâG¶R‡‚ä–æGW7G'•ôÖöFVÅô¶W—ÇÂttTäU$Åô4õ%õ$DRr—ÓÂ÷FCãÇFCâG¶ÖWG&–4‡FÖÂ‡‚Âu&uôÖöFVÅõ66÷&Rr—ÓÂ÷FCãÇFCâG¶ÖWG&–4‡FÖÂ‡‚Âu6‡'Væµõv—F†–åôÖöFVÅõW&6VçF–ÆRrÃ"ÂrRr—ÓÂ÷FCãÇFCâG¶ÖWG&–4‡FÖÂ‡‚ÂtFFô6öæf–FVæ6Uõ66÷&Rr—ÓÂ÷FCãÇFB6Æ73Ò&÷F–öæÂ#âG¶6÷&T·”‡FÖÂ‡‚—ÓÂ÷FCãÇFB6Æ73Ò&÷F–öæÂ#âG¶R‡‚åfÇVF–öåõ7VÖÖ'—ÇÂtâôr—ÓÂ÷FCãÇFCâG¶R‡‚å&W6V&6…ô7F–öåõ7FFWÇÇ‚äFV6—6–öåõ7FFWÇÂtâôr—ÓÂ÷FCãÇFCãÆ'WGFöâFF×sÒ"G¶R‡‚åF–6¶W"—Ò#âG·vF6‚æ†2‡‚åF–6¶W"“ò~z{¾™šBs¢~XªXZRwÓÂö'WGFöããÂ÷FCãÂ÷G#æ’æ¦ö–â‚rr“²B‚r6V×G’r’æ†–FFVãÖ÷WBæÆVæwFƒã¶Fö7VÖVçBçVW'•6VÆV7F÷$ÆÂ‚wG%¶FF×EÒr’æf÷$V6‚‡#Óç"æöæ6Æ–6³ÖÓç¶–b‚çF&vWBæFF6WBçr–÷VäFWF–Â‡"æFF6WBçB—Ò“¶Fö7VÖVçBçVW'•6VÆV7F÷$ÆÂ‚u¶FF×uÒr’æf÷$V6‚†#Óæ"æöæ6Æ–6³ÖÓç¶ç7F÷&÷vF–öâ‚“·FövvÆR†"æFF6WBçr—Ò—Ğ¦gVæ7F–öâ&VæFW$VÖW&v–ær‚—¶ÆWB&6SÖFFçG&VæEö&6VÆ–æWÇÇ·Ó²B‚r6&6VÆ–æRr’çFW‡D6öçFVçCÖYû®k©nx¸hX¾ûÉ¢G¶&6RæÖöFVÅ÷fW'6–öåö6†ævVCò~jŠYè¾x˜iÊÎŠè®i»NûÈÎ˜xŞik[»®z¸¾Yû®k©bs¢†&6Rç7FGW7ÇÂ~šinjÊ[»®z¸²r—ÒG¶&6Rç&Wf–÷W5övVæW&FVEöCò~ûÉ¾X˜ŞjÊr¶æWrFFR†&6Rç&Wf–÷W5övVæW&FVEöB’çFôÆö6ÆU7G&–ær‚w¦‚ÕErr“¢rwŞ8.Šˆ®‰™şXú®KÛşyJjŠYè¾XZ~iKn{Šîy›îXˆnKØŞûÈÎjŠYè¾iKx˜iÈ>˜xŞŠŠŞYû®k©n8&²B‚r6VÖW&v–æt6&G2r’æ–ææW$…DÔÃÖVÖW&v–æræÆVæwFƒöVÖW&v–æræÖ†3Óç¶ÆWBÓÖ2æÖWG&–77ÇÇ·ÒÆFVÇFÔö&¦V7BæVçG&–W2†2æFVÇF7ÇÇ·Ò’æÖ‚…¶²ÇeÒ“ÓæG¶·ÒG·cãÓòr²s¢rwÒG·gÖ’æ¦ö–â‚r+rr—ÇÂ~šinjÊYû®k©bs·&WGW&âÆF—b6Æ73Ò&VÖW&v–ærÖ6&B"FFÖ6æF–FFSÒ"G¶R†2æ¶W’—Ò#ãÆ#âG¶R†2ææÖR—ÓÂö#ãÇ7â6Æ73Ò&&FvRv&â#îz	Nz›n{y®{J#Â÷7ããÇ7â6Æ73Ò&&FvRG¶2æ6öæf–FVæ6SÓÓÒt„”t‚sòvvööBs¢wv&âwÒ#âG¶R†2æ6öæf–FVæ6R—ÓÂ÷7ããÆF—b6Æ73Ò&çV×2#âG¶R†2æ¶–æB—Ò+rjŠ>iÊÂG¶R†Òæ6÷VçCóòtâôr—Ò+rVÆ–v–&ÆRG¶R†ÒæVÆ–v–&ÆSóòtâôr—Ò+rVWVRG¶R†Òç&W6V&6…÷VWVSóòtâôr—ÓÂöF—cãÇ6ÖÆÃîjŠYè¾ûÉ¢G²†2æÖöFVÅö¶W—7ÇÅµÒ’æÖ†R’æ¦ö–â‚rÂr—ÇÂtâôwÓÆ'#å&rG¶b†Òæfu÷&u÷66÷&RÃ—Ò+r6‡'Væ²G¶b†Òæfu÷6‡'Væµ÷66÷&RÃ—Ò+r6öæf–FVæ6RG¶b†Òæfuö6öæf–FVæ6RÃ—Ò+rµ’6÷fW&vRG¶b†Òæfuö·•ö6÷fW&vRÃÂrRr—ÓÆ'#îŠè®XÉnûÉ¢G¶R†FVÇF—Ò+rx˜iÊÂG¶R†2æÖöFVÅ÷fW'6–öçÇÂtâôr—ÓÂ÷6ÖÆÃãÆF—b6Æ73Ò'&V6öç2#âG²†2ç&V6öç7ÇÅµÒ’æÖ‡#Óâ~(
"r¶R‡"’’æ¦ö–â‚sÆ'#âr—ÓÂöF—cãÂöF—cæÒ’æ¦ö–â‚rr“¢sÆF—b6Æ73Ò&VÖW&v–ærÖ6&B#ãÆ#îiÊÎiÉşk).iÈ˜N™hj«¾y¨Nz	Nz›n{êNˆ£Âö#ãÇ6ÖÆÃî˜	KˆŞiŠş‹*™Ú.h©^‹8~Šˆ®‰™şûÈÎXú®Kº>ŠyºîX˜Ş‹8~ii[	®iÊ®[Ú.h‰‹k>ZJ[Ë~y¨NjŠYè¾XZ~{êNˆ®8#Â÷6ÖÆÃãÂöF—câwĞ¦6öç7BÆVv7”÷VäFWF–ÃÖ÷VäFWF–Ã°¦÷VäFWF–ÃÖgVæ7F–öâ‡B—¶ÆVv7”÷VäFWF–Â‡B“¶ÆWBƒÖÖævWB‡B—ÇÇ·ÒÆ&öG“ÒB‚r6FWF–Ä&öG’r“¶–b‚&öG’—&WGW&ã¶ÆWB6÷W&6S×‚ä6öÖ&–æVEõ&F–õõ6÷W&6Uõ7FGW7ÇÂtâôrÇ7G&W73×‚å7V6–Æ—¦VEõ7G&W75õ7FGW7ÇÇ‚åöæEô5õ7G&W75õ7FGW7ÇÂtâôs¶&öG’æ–ç6W'DF¦6VçD…DÔÂ‚vgFW&&Vv–ârÆÆF—b6Æ73Ò'6V7F–öâ#ãÆƒ3îz	Nz›nXJ®XX[¨şˆˆ~[è^iú^K¨¾šSÂöƒ3ãÆF—b6Æ73Ò&w&–B#ãÆF—b6Æ73Ò&ÖWG&–2#ãÇ6ÖÆÃîjŠYè³Â÷6ÖÆÃãÆ#âG¶R‡‚ä–æGW7G'•ôÖöFVÅô¶W—ÇÂttTäU$Åô4õ%õ$DRr—ÓÂö#ãÂöF—cãÆF—b6Æ73Ò&ÖWG&–2#ãÇ6ÖÆÃå&rÖöFVÂ66÷&SÂ÷6ÖÆÃãÆ#âG¶b‡‚å&uôÖöFVÅõ66÷&R—ÓÂö#ãÂöF—cãÆF—b6Æ73Ò&ÖWG&–2#ãÇ6ÖÆÃåv—F†–âÖÖöFVÂW&6VçF–ÆSÂ÷6ÖÆÃãÆ#âG¶b‡‚åv—F†–åôÖöFVÅõW&6VçF–ÆRÃ"ÂrRr—ÓÂö#ãÂöF—cãÆF—b6Æ73Ò&ÖWG&–2#ãÇ6ÖÆÃå6‡'Væ²W&6VçF–ÆSÂ÷6ÖÆÃãÆ#âG¶b‡‚å6‡'Væµõv—F†–åôÖöFVÅõW&6VçF–ÆRÃ"ÂrRr—ÓÂö#ãÂöF—cãÆF—b6Æ73Ò&ÖWG&–2#ãÇ6ÖÆÃäFF6öæf–FVæ6SÂ÷6ÖÆÃãÆ#âG¶b‡‚äFFô6öæf–FVæ6Uõ66÷&R—ÓÂö#ãÂöF—cãÆF—b6Æ73Ò&ÖWG&–2#ãÇ6ÖÆÃä6÷&Rµ’6÷fW&vSÂ÷6ÖÆÃãÆ#âG¶b‡‚ä6÷&UôÖWG&–5ô6÷fW&vU÷7BÃÂrRr—ÓÂö#ãÂöF—cãÆF—b6Æ73Ò&ÖWG&–2#ãÇ6ÖÆÃå&W6V&6‚7FFSÂ÷6ÖÆÃãÆ#âG¶R‡‚å&W6V&6…ô7F–öåõ7FFWÇÂtâôr—ÓÂö#ãÂöF—cãÆF—b6Æ73Ò&ÖWG&–2#ãÇ6ÖÆÃå7V6–Æ—¦VB7G&W73Â÷6ÖÆÃãÆ#âG¶R‡7G&W72—ÓÂö#ãÂöF—cãÆF—b6Æ73Ò&ÖWG&–2#ãÇ6ÖÆÃä7&÷72ÖÖöFVÂ6Æ–'&F–öãÂ÷6ÖÆÃãÆ#âG¶R‡‚ä7&÷75ôÖöFVÅô6Æ–'&F–öåõ7FGW7ÇÂuTä4Ä”%$DTBr—ÓÂö#ãÂöF—cãÂöF—cãÆF—b6Æ73Ò&&÷‚#âG¶6÷&T·”‡FÖÂ‡‚—ÓÆ'#ãÆ#åfÇVF–öãÂö#ã¢G¶R‡‚åfÇVF–öåõ7VÖÖ'—ÇÂtâôr—ÓÆ'#ãÇ6ÖÆÃä6öÖ&–æVB×&F–ò6÷W&6S¢G¶R‡6÷W&6R—Ò+rÖöFVÂfW'6–öã¢G¶R†FFç&W6V&6…÷&–÷&—G•÷fW'6–öçÇÂtâôr—ÓÂ÷6ÖÆÃãÂöF—cãÆF—b6Æ73Ò&w&–B6V7F–öâ#ãÆF—b6Æ73Ò&&÷‚#ãÆ#åF÷2÷6—F—fRG&—fW'3Âö#âG¶Æ—7D‡FÖÂ‡‚åF÷õ÷6—F—fUôG&—fW'2—ÓÂöF—cãÆF—b6Æ73Ò&&÷‚#ãÆ#åF÷2&—6·3Âö#âG¶Æ—7D‡FÖÂ‡‚åF÷õ&—6·2—ÓÂöF—cãÆF—b6Æ73Ò&&÷‚#ãÆ#äÖçVÂ&Wf–WrF6·3Âö#âG¶Æ—7D‡FÖÂ‡‚äÖçVÅõ&Wf–WuõF6·2—ÓÂöF—cãÂöF—cãÆF—b6Æ73Ò&&÷‚#ãÆ#å&WV—&VBÖ—76–ærÖWG&–73Âö#ã¢G¶R‡‚å&WV—&VEôÖ—76–æuôÖWG&–77ÇÂtæöæRr—ÓÆ'#ãÆ#ä÷F–öæÂÖ—76–ærÖWG&–73Âö#ã¢G¶R‡‚ä÷F–öæÅôÖ—76–æuôÖWG&–77ÇÂtæöæRr—ÓÆ'#ãÆ#äFV6—6–öâ&V6öãÂö#ã¢G¶R‡‚äFV6—6–öåõ&V6öåô6öFWÇÂtâôr—ÓÂöF—cãÂöF—cæ—Ó°¦6öç7B6÷fW&vSÒ†FFç7FG7ÇÇ·Ò’æ6÷fW&vUöÖVF–ç7ÇÇ·ÒÇ&F–÷3Ò†FFç7FG7ÇÇ·Ò’æÖWG&–5÷7FGW5÷&F–÷7ÇÇ·ÒÆ–çFVw&—G“ÖFö7VÖVçBçVW'•6VÆV7F÷"‚r6–çFVw&—G•7VÖÖ'’r“¶–b†–çFVw&—G’—¶–çFVw&—G’æ–ææW$…DÔÃÔö&¦V7BæVçG&–W2†–çFVw&—G”Æ&VÇ2’æÖ‚…¶²ÆÆ&VÅÒ“ÓæÇ7ããÇ6ÖÆÃâG¶R†Æ&VÂ—ÓÂ÷6ÖÆÃãÆ#âG¶R†–çFVw&—G•F÷FÇ5¶µ×ÇÃ—Ò‚G¶b‚‡&F–÷5¶µ×ÇÃ’£ÃÂrRr—Ò“Âö#ãÂ÷7ãæ’æ¦ö–â‚rr“¶–çFVw&—G’æ–ç6W'DF¦6VçD…DÔÂ‚v&Vf÷&VVæBrÆÇ7ããÇ6ÖÆÃäÖVF–â6÷&RÖWG&–26÷fW&vSÂ÷6ÖÆÃãÆ#âG¶b†6÷fW&vRæ6÷&UöÖWG&–5öÆÂÃÂrRr—ÓÂö#ãÂ÷7ããÇ7ããÇ6ÖÆÃäVÆ–v–&ÆR6÷&RÖWG&–26÷fW&vSÂ÷6ÖÆÃãÆ#âG¶b†6÷fW&vRæ6÷&UöÖWG&–5öVÆ–v–&ÆRÃÂrRr—ÓÂö#ãÂ÷7ããÇ7ããÇ6ÖÆÃåVWVR6÷&RÖWG&–26÷fW&vSÂ÷6ÖÆÃãÆ#âG¶b†6÷fW&vRæ6÷&UöÖWG&–5÷&W6V&6…÷VWVRÃÂrRr—ÓÂö#ãÂ÷7ãæ—Ğ¦6öç7B'VäÖWFÖFFæÖWFFFÇÇ·ÒÆ†W&óÖFö7VÖVçBçVW'•6VÆV7F÷"‚ræ†W&òr“¶–b††W&òbbFö7VÖVçBçVW'•6VÆV7F÷"‚r7'VäÖWFFFr’–†W&òæ–ç6W'DF¦6VçD…DÔÂ‚vgFW&VæBrÆÇ6V7F–öâ–CÒ''VäÖWFFF"6Æ73Ò'æVÂ"7G–ÆSÒ'FF–æs£'ƒ¶Ö&v–â×F÷£'‚#ãÆF—b6Æ73Ò&w&–B#ãÆF—cãÇ6ÖÆÃäFV6—6–öâF–ÖW7F×Â÷6ÖÆÃãÆ'#âG¶R‡'VäÖWFæFV6—6–öå÷F–ÖW7F×ÇÂuTä´äõtâr—ÓÂöF—cãÆF—cãÇ6ÖÆÃå&–6RFFFFSÂ÷6ÖÆÃãÆ'#âG¶R‡'VäÖWFç&–6UöFFöFFWÇÂuTä´äõtâr—ÓÂöF—cãÆF—cãÇ6ÖÆÃäÆFW7B4T2f–Æ&–Æ—G’FFSÂ÷6ÖÆÃãÆ'#âG¶R‡'VäÖWFæÆFW7E÷6V5öf–Æ&–Æ—G•öFFWÇÂuTä´äõtâr—ÓÂöF—cãÆF—cãÇ6ÖÆÃåVæ—fW'6RfW'6–öãÂ÷6ÖÆÃãÆ'#âG¶R‡'VäÖWFçVæ—fW'6U÷fW'6–öçÇÂuTä´äõtâr—ÓÂöF—cãÆF—cãÇ6ÖÆÃäÖöFVÂfW'6–öãÂ÷6ÖÆÃãÆ'#âG¶R‡'VäÖWFæÖöFVÅ÷fW'6–öçÇÂuTä´äõtâr—ÓÂöF—cãÆF—cãÇ6ÖÆÃäv—B6öÖÖ—CÂ÷6ÖÆÃãÆ'#âG¶R‡'VäÖWFæv—Eö6öÖÖ—GÇÂuTä´äõtâr—ÓÂöF—cãÂöF—cãÂ÷6V7F–öãæ“°¦Fö7VÖVçBçVW'•6VÆV7F÷"‚r76†÷'FÆ—7Br’çFW‡D6öçFVçCÒ†FFç7FG7ÇÇ·Ò’ç&W6V&6…÷VWVWÇÃ¶Fö7VÖVçBçVW'•6VÆV7F÷"‚r76†÷'FÆ—7Br’ç&Wf–÷W4VÆVÖVçE6–&Æ–ærçFW‡D6öçFVçCÒtvÆö&Â&W6V&6‚VWVRs°¦Fö7VÖVçBçVW'•6VÆV7F÷"‚r7WFFVBr’æ–ç6W'DF¦6VçD…DÔÂ‚v&Vf÷&V&Vv–ârÆÆF—b6Æ73Ò&fö÷FW"v&â#âG¶R†FFç66÷&UöF—66Æ–ÖW'ÇÂ~jŠYè¾XZ~y›îXˆnKØŞûÈÎKˆŞKº>Š‹zjŠYè¾iÊ®KènZ˜ZÎ[{.j
k©br—ÓÂöF—cæ“°§&VæFW$VÖW&v–ær‚“·&VæFW"‚“°£Â÷67&—Cà¢rrp  ¦FVbÖöFW&æ—¦U÷vR‡vS¢7G"’Óâ7G# ¢vRÒ&Rç7V"€¢"rƒÇ6V7F–öâ6Æ73Ò&VÖW&v–æræVÂ#ãÆƒ#â’â£òƒÂöƒ#â’rÀ¢"uÃš¹Xˆn{êNˆ®ˆˆ~z	Nz›n{y®{J%Ã"rÀ¢vRÀ¢6÷VçCÓÀ¢¢vRÒ&Rç7V"€¢"rƒÇ6V7F–öâ6Æ73Ò'F&ÆRæVÂ#ãÇF&ÆSãÇF†VCãÇG#â’â£òƒÂ÷G#ãÂ÷F†VCâ’rÀ¢"uÃÇFƒîz	Nz›nhé.YÓÂ÷FƒãÇFƒåF–6¶W#Â÷FƒãÇFƒîjŠYè³Â÷FƒãÇFƒå&r66÷&SÂ÷FƒãÇFƒîjŠYè¾XZ~iKn{Šîy›îXˆnKØÓÂ÷FƒãÇFƒî‹8~iiKú[ø3Â÷FƒãÇF‚6Æ73Ò&÷F–öæÂ#îj[ø2µ“Â÷FƒãÇF‚6Æ73Ò&÷F–öæÂ#îKËXÃÂ÷FƒãÇFƒîz	Nz›nx¸hX³Â÷FƒãÇFƒåvF6ƒÂ÷FƒåÃ"rÀ¢vRÀ¢6÷VçCÓÀ¢¢vRÒvRç&WÆ6R€¢&ÖWG&–4‡FÖÂ‡‚Âu&VÅôd4eõ––VÆE÷7Br"À¢&ÖWG&–4‡FÖÂ‡‚Âu&VÅôd4eõ––VÆE÷7Br"À¢¢&WGW&âvRç&WÆ6R‚#Âö&öG“ãÂö‡FÖÃâ"ÂÔôDU$åôD4„$ô$Eõ45$•B²#Âö&öG“ãÂö‡FÖÃâ" Ğ Ğ¦FVb'V–ÆEöF6†&ö&B‡67&VVã¢F‚Â6†÷'FÆ—7C¢F‚ÂVæ—fW'6S¢F‚Â÷WGWC¢F‚Â†—7F÷'“¢F‚ÂæöæRÒæöæR’ÓâFƒ ¢–ÆöBÂG&VæEöw&÷W2Ò'V–ÆE÷–ÆöB‡67&VVâÂ6†÷'FÆ—7BÂVæ—fW'6RÂ†—7F÷'’Ğ¢÷WGWBæÖ¶F—"‡&VçG3ÕG'VRÂW†—7Eöö³ÕG'VRĞ¢VÖ&VFFVBÒ§6öâæGV×2‡–ÆöBÂVç7W&Uö66–“ÔfÇ6RÂ6W&F÷'3Ò‚"Â"Â#¢"’’ç&WÆ6R‚#Âò"Â#ÅÅÂò"Ğ¢–æFW‚Ò÷WGWBò&–æFW‚æ‡FÖÂ Ğ¢–æFW‚çw&—FU÷FW‡B†ÖöFW&æ—¦U÷vR…tR’ç&WÆ6R‚%õôDDõò"ÂVÖ&VFFVB’ÂVæ6öF–æsÒ'WFbÓ‚"¢†÷WGWBò&FFæ§6öâ"’çw&—FU÷FW‡B†§6öâæGV×2‡–ÆöBÂVç7W&Uö66–“ÔfÇ6RÂ–æFVçCÓ"’ÂVæ6öF–æsÒ'WFbÓ‚"Ğ¢†÷WGWBò"ææö¦V·–ÆÂ"’çw&—FU÷FW‡B‚""ÂVæ6öF–æsÒ'WFbÓ‚"Ğ¢6fU÷G&VæEö†—7F÷'’††—7F÷'’ÂG&VæEöw&÷W2Â–ÆöE²&vVæW&FVEöB%ÒĞ¢&WGW&â–æFW€Ğ Ğ Ğ¦FVbÖ–â‚’Óâ–çC Ğ¢'6W"Ò&w'6Rä&wVÖVçE'6W"†FW67&—F–öãÒ$'V–ÆBF†R7FF–2ÖöFR2&W6V&6‚F6†&ö&Bâ"Ğ¢'6W"æFEö&wVÖVçB‚"Ò×67&VVâ"ÂFVfVÇCÒ&ÖöFUö5÷67&VVâæ77b"Ğ¢'6W"æFEö&wVÖVçB‚"Ò×6†÷'FÆ—7B"ÂFVfVÇCÒ&ÖöFUö5÷6†÷'FÆ—7Bæ77b"Ğ¢'6W"æFEö&wVÖVçB‚"Ò×Væ—fW'6R"ÂFVfVÇCÒ'VÆ–f–VE÷Væ—fW'6Ræ77b"Ğ¢'6W"æFEö&wVÖVçB‚"ÒÖ÷WGWB"ÂFVfVÇCÒ'V&Æ–2"Ğ¢'6W"æFEö&wVÖVçB‚"ÒÖ†—7F÷'’"ÂFVfVÇCÒ"æÖöFUö5÷7FFRöF6†&ö&E÷G&VæEö†—7F÷'’æ§6öâ"Ğ¢'6W"æFEö&wVÖVçB‚"ÒÖæòÖ†—7F÷'’"Â7F–öãÒ'7F÷&U÷G'VR"Â†VÇÒ$Fòæ÷B&VB÷"WFFRVÖW&v–ær×F†VÖRG&VæB†—7F÷'’â"Ğ¢&w2Ò'6W"ç'6Uö&w2‚Ğ¢†—7F÷'’ÒæöæR–b&w2ææõö†—7F÷'’VÇ6RF‚†&w2æ†—7F÷'’Ğ¢–æFW‚Ò'V–ÆEöF6†&ö&B…F‚†&w2ç67&VVâ’ÂF‚†&w2ç6†÷'FÆ—7B’ÂF‚†&w2çVæ—fW'6R’ÂF‚†&w2æ÷WGWB’Â†—7F÷'’Ğ¢&–çB†b$F6†&ö&Bw&—GFVâFò¶–æFW‡Ò"Ğ¢&WGW&â Ğ Ğ Ğ¦–bõöæÖUõòÓÒ%õöÖ–åõò# Ğ¢&—6R7—7FVÔW†—B†Ö–â‚’Ğ