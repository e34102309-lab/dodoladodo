from __future__ import annotations

from typing import Dict


def route_industry_model(sector: str, industry: str) -> Dict[str, object]:
    sector_text = str(sector or "").strip().lower()
    industry_text = str(industry or "").strip().lower()
    blob = f"{sector_text} {industry_text}"
    if not sector_text or not industry_text:
        return {
            "route": "UNKNOWN",
            "model_key": "UNKNOWN",
            "supported": False,
            "reason": "Sector and industry are both required before an accounting model can be selected",
        }
    if "insurance broker" in industry_text:
        return {
            "route": "FINANCIAL_SPECIALTY",
            "model_key": "FINANCIAL_FEE",
            "supported": True,
            "reason": "Fee-based insurance intermediary model using margin, cash conversion and tangible capital",
        }
    insurance_business = (
        "insurance" in blob
        or "healthcare plans" in industry_text
        or "managed healthcare" in industry_text
        or "managed health care" in industry_text
    )
    if insurance_business:
        model_key = (
            "INSURANCE_LIFE"
            if any(
                token in industry_text
                for token in ["life", "health", "healthcare plans", "managed healthcare"]
            )
            else "INSURANCE_P_AND_C"
        )
        return {
            "route": "INSURANCE",
            "model_key": model_key,
            "supported": True,
            "reason": "Dedicated underwriting, claims, capital and reserve-development model",
        }
    if sector_text in {"financial services", "financials"}:
        if any(token in industry_text for token in ["bank", "banks", "savings", "mortgage finance"]):
            if "mortgage finance" in industry_text and "bank" not in industry_text:
                route = "FINANCIAL_SPECIALTY"
                model_key = "FINANCIAL_LENDER"
                reason = "Specialty-lender tangible capital, credit allowance and earnings model"
            else:
                route = "BANK"
                model_key = "BANK"
                reason = "Tier 1 capital buffer, ROTCE, deposit funding and credit-loss model"
        else:
            route = "FINANCIAL_SPECIALTY"
            lender_tokens = ["consumer finance", "credit union", "specialty finance"]
            model_key = "FINANCIAL_LENDER" if any(token in industry_text for token in lender_tokens) else "FINANCIAL_FEE"
            reason = (
                "Specialty-lender tangible capital, credit allowance and earnings model"
                if model_key == "FINANCIAL_LENDER"
                else "Fee-financial margin, cash conversion, tangible capital and balance-sheet model"
            )
        return {"route": route, "model_key": model_key, "supported": True, "reason": reason}
    if "reit" in industry_text or "real estate investment trust" in industry_text:
        return {
            "route": "REIT",
            "model_key": "REIT_MORTGAGE" if "mortgage" in industry_text else "REIT_EQUITY",
            "supported": True,
            "reason": (
                "Mortgage-REIT book capital, leverage, income and dividend-coverage model"
                if "mortgage" in industry_text
                else "Nareit FFO/EBITDAre proxy, AFFO coverage, leverage and lease-growth model"
            ),
        }
    if sector_text == "utilities" and any(
        token in industry_text
        for token in ["independent power", "renewable", "power producer"]
    ):
        return {
            "route": "GENERAL_CORPORATE",
            "model_key": "GENERAL_CORPORATE",
            "supported": True,
            "reason": "Merchant or renewable power producer; use project cash-flow and corporate solvency model rather than regulated-utility assumptions",
        }
    if sector_text == "utilities":
        return {
            "route": "REGULATED_UTILITY",
            "model_key": "REGULATED_UTILITY",
            "supported": True,
            "reason": "Regulated-asset growth proxy, capex funding, capital structure and coverage model",
        }
    cyclical_keywords = [
        "oil & gas",
        "steel",
        "aluminum",
        "copper",
        "industrial metals",
        "gold",
        "coal",
        "chemical",
        "lumber",
        "paper",
        "marine shipping",
        "airline",
        "trucking",
        "auto manufacturer",
        "auto parts",
        "farm products",
    ]
    if sector_text in {"energy", "basic materials"} or any(token in blob for token in cyclical_keywords):
        return {
            "route": "CYCLICAL_MIDCYCLE",
            "model_key": "CYCLICAL_MIDCYCLE",
            "supported": True,
            "reason": "Peak/current/mid-cycle/trough EBITDA and trough-survival model",
        }
    return {
        "route": "GENERAL_CORPORATE",
        "model_key": "GENERAL_CORPORATE",
        "supported": True,
        "reason": "General corporate OCF, CapEx, EBIT, leverage and return-on-capital model",
    }
