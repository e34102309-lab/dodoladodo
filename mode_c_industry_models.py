from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple


SPECIALIZED_MODEL_KEYS = {
    "BANK",
    "INSURANCE_P_AND_C",
    "INSURANCE_LIFE",
    "REIT_EQUITY",
    "REIT_MORTGAGE",
    "REGULATED_UTILITY",
    "CYCLICAL_MIDCYCLE",
    "FINANCIAL_LENDER",
    "FINANCIAL_FEE",
    "ALTERNATIVE_ASSET_MANAGER",
    "TRADITIONAL_ASSET_MANAGER",
    "INSURANCE_LINKED_ASSET_MANAGER",
    "OTHER_FEE_FINANCIAL",
}


@dataclass
class IndustryModelEvaluation:
    model_key: str
    decision: str
    score: float
    metrics: Dict[str, Any] = field(default_factory=dict)
    components: Dict[str, float] = field(default_factory=dict)
    required_missing: List[str] = field(default_factory=list)
    optional_missing: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    hard_failures: List[str] = field(default_factory=list)
    score_coverage: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _number(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return math.nan
    return parsed if math.isfinite(parsed) else math.nan


def _finite(value: Any) -> bool:
    return math.isfinite(_number(value))


def _coalesce_number(value: Any, fallback: Any) -> float:
    parsed = _number(value)
    return parsed if math.isfinite(parsed) else _number(fallback)


def _safe_div(numerator: Any, denominator: Any) -> float:
    n = _number(numerator)
    d = _number(denominator)
    if not math.isfinite(n) or not math.isfinite(d) or d == 0:
        return math.nan
    return n / d


def _interest_coverage(earnings: Any, interest: Any, debt: Any) -> float:
    earnings_value = _number(earnings)
    interest_value = abs(_number(interest))
    debt_value = _number(debt)
    if not math.isfinite(earnings_value) or not math.isfinite(debt_value):
        return math.nan
    if math.isfinite(interest_value) and interest_value > 0:
        return earnings_value / interest_value
    return 10.0 if debt_value <= 0 else math.nan


def _bounded(value: Any, low: float, high: float) -> float:
    v = _number(value)
    if not math.isfinite(v) or high <= low:
        return math.nan
    return max(0.0, min(100.0, (v - low) / (high - low) * 100.0))


def _inverse(value: Any, bad: float, good: float) -> float:
    if bad == good:
        return math.nan
    return _bounded(value, bad, good) if good > bad else _bounded(value, good, bad) * -1.0 + 100.0


def _percentile(values: Sequence[float], percentile: float) -> float:
    clean = sorted(_number(value) for value in values if _finite(value))
    if not clean:
        return math.nan
    if len(clean) == 1:
        return clean[0]
    position = max(0.0, min(1.0, percentile)) * (len(clean) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return clean[lower]
    fraction = position - lower
    return clean[lower] * (1.0 - fraction) + clean[upper] * fraction


def _weighted_score(components: Mapping[str, Tuple[float, float]]) -> Tuple[float, float, Dict[str, float]]:
    total_weight = sum(max(0.0, float(weight)) for _, weight in components.values())
    available = {
        name: (float(score), float(weight))
        for name, (score, weight) in components.items()
        if _finite(score) and weight > 0
    }
    available_weight = sum(weight for _, weight in available.values())
    if total_weight <= 0 or available_weight <= 0:
        return math.nan, 0.0, {}
    coverage = available_weight / total_weight
    raw_score = sum(score * weight for score, weight in available.values()) / available_weight
    coverage_multiplier = min(1.0, coverage / 0.80)
    score = max(0.0, min(100.0, raw_score * coverage_multiplier))
    return round(score, 2), round(coverage, 4), {
        name: round(score, 2) for name, (score, _) in available.items()
    }


def _finish(
    model_key: str,
    metrics: Dict[str, Any],
    component_inputs: Mapping[str, Tuple[float, float]],
    required: Iterable[str],
    optional: Iterable[str],
    hard_failures: Iterable[str],
    warnings: Iterable[str] = (),
    pass_score: float = 60.0,
    minimum_coverage: float = 0.65,
) -> IndustryModelEvaluation:
    missing_required = [name for name in required if not _finite(metrics.get(name))]
    missing_optional = [name for name in optional if not _finite(metrics.get(name))]
    score, coverage, components = _weighted_score(component_inputs)
    hard = [str(item) for item in hard_failures if item]
    all_warnings = [str(item) for item in warnings if item]
    if hard:
        decision = "FAIL"
    elif missing_required:
        decision = "ABSTAIN"
        all_warnings.append("Missing required metrics: " + ", ".join(missing_required))
    elif coverage < minimum_coverage:
        decision = "ABSTAIN"
        all_warnings.append(f"Score coverage {coverage:.0%} is below {minimum_coverage:.0%}")
    else:
        decision = "PASS" if score >= pass_score else "FAIL"
    return IndustryModelEvaluation(
        model_key=model_key,
        decision=decision,
        score=score,
        metrics=metrics,
        components=components,
        required_missing=missing_required,
        optional_missing=missing_optional,
        warnings=all_warnings,
        hard_failures=hard,
        score_coverage=coverage,
    )


def _first_number(info: Mapping[str, Any], *keys: str) -> float:
    for key in keys:
        value = _number(info.get(key))
        if math.isfinite(value):
            return value
    return math.nan


def initial_screen_industry(model_key: str, info: Mapping[str, Any]) -> dict:
    """Conservative Yahoo-stage routing screen; SEC deep models remain authoritative."""
    book_value = _first_number(info, "bookValue", "bookValuePerShare")
    roe = _first_number(info, "returnOnEquity")
    ebitda = _first_number(info, "ebitda")
    ocf = _first_number(info, "operatingCashflow", "operatingCashFlow")
    ffo = _first_number(info, "fundsFromOperations", "fundsFromOperationsPerShare")
    warnings: List[str] = []
    hard_failures: List[str] = []

    if model_key in {"BANK", "FINANCIAL_LENDER", "INSURANCE_P_AND_C", "INSURANCE_LIFE"}:
        if math.isfinite(book_value) and book_value <= 0:
            hard_failures.append("reported book value is non-positive")
        if math.isfinite(roe) and roe <= -0.20:
            hard_failures.append("reported ROE is below -20%")
        if not math.isfinite(book_value):
            warnings.append("Yahoo book value unavailable; defer to SEC balance sheet")
    elif model_key == "REIT_EQUITY":
        if math.isfinite(ffo) and ffo <= 0:
            hard_failures.append("reported FFO is non-positive")
        if not math.isfinite(ffo):
            warnings.append("Yahoo FFO unavailable; rebuild Nareit FFO proxy from SEC facts")
    elif model_key == "REIT_MORTGAGE":
        if math.isfinite(book_value) and book_value <= 0:
            hard_failures.append("reported mortgage-REIT book value is non-positive")
    elif model_key == "REGULATED_UTILITY":
        if math.isfinite(ebitda) and math.isfinite(ocf) and ebitda <= 0 and ocf <= 0:
            hard_failures.append("both EBITDA and operating cash flow are non-positive")
    elif model_key in {
        "FINANCIAL_FEE",
        "ALTERNATIVE_ASSET_MANAGER",
        "TRADITIONAL_ASSET_MANAGER",
        "INSURANCE_LINKED_ASSET_MANAGER",
        "OTHER_FEE_FINANCIAL",
    }:
        if math.isfinite(ebitda) and ebitda <= 0 and math.isfinite(ocf) and ocf <= 0:
            hard_failures.append("fee-business EBITDA and operating cash flow are non-positive")
        if not math.isfinite(ebitda) or not math.isfinite(ocf):
            warnings.append(
                "Yahoo fee-business cash economics are incomplete; defer AUM/FRE/flow KPIs to company filings"
            )
    elif model_key == "CYCLICAL_MIDCYCLE":
        if math.isfinite(ebitda) and ebitda <= 0:
            warnings.append("current EBITDA is non-positive; trough-survival deep model required")
        if math.isfinite(ocf) and ocf <= 0:
            warnings.append("current operating cash flow is non-positive")

    available = sum(math.isfinite(value) for value in [book_value, roe, ebitda, ocf, ffo])
    data_quality_score = max(0.0, 100.0 - len(warnings) * 10.0 - (5 - available) * 3.0)
    return {
        "decision": "DROP" if hard_failures else "PASS",
        "data_quality_score": round(data_quality_score, 2),
        "warnings": warnings,
        "hard_failures": hard_failures,
        "observed": {
            "book_value": book_value,
            "roe": roe,
            "ebitda": ebitda,
            "ocf": ocf,
            "ffo": ffo,
        },
    }


def evaluate_bank(raw: Mapping[str, Any]) -> IndustryModelEvaluation:
    metrics = dict(raw)
    assets = _number(metrics.get("assets_b"))
    tangible_equity = _number(metrics.get("tangible_equity_b"))
    net_income = _number(metrics.get("net_income_ttm_b"))
    deposits = _number(metrics.get("deposits_b"))
    loans = _number(metrics.get("loans_b"))
    allowance = _number(metrics.get("credit_loss_allowance_b"))
    market_cap = _number(metrics.get("market_cap_b"))
    actual_capital = _number(metrics.get("tier1_ratio_pct"))
    well_capitalized = _number(metrics.get("tier1_well_capitalized_min_pct"))
    metrics.update(
        {
            "tier1_buffer_pp": actual_capital - well_capitalized,
            "tangible_equity_to_assets_pct": _safe_div(tangible_equity, assets) * 100.0,
            "rotce_pct": _safe_div(
                net_income,
                _coalesce_number(metrics.get("average_tangible_equity_b"), tangible_equity),
            ) * 100.0,
            "deposit_funding_pct": _safe_div(deposits, assets) * 100.0,
            "credit_loss_allowance_to_loans_pct": _safe_div(allowance, loans) * 100.0,
            "price_to_tangible_book_x": _safe_div(market_cap, tangible_equity),
            "aoci_to_tangible_equity_pct": _safe_div(metrics.get("aoci_b"), tangible_equity) * 100.0,
        }
    )
    hard = []
    if _finite(metrics["tier1_buffer_pp"]) and metrics["tier1_buffer_pp"] < 0:
        hard.append("Tier 1 capital ratio is below the reported well-capitalized minimum")
    if _finite(metrics["tangible_equity_to_assets_pct"]) and metrics["tangible_equity_to_assets_pct"] < 3.0:
        hard.append("tangible equity / assets is below 3%")
    if math.isfinite(net_income) and net_income <= 0:
        hard.append("TTM net income is non-positive")
    if _finite(metrics["aoci_to_tangible_equity_pct"]) and metrics["aoci_to_tangible_equity_pct"] < -50.0:
        hard.append("negative AOCI exceeds 50% of tangible equity")
    components = {
        "tier1_capital_buffer": (_bounded(metrics["tier1_buffer_pp"], 0.0, 5.0), 27.0),
        "tangible_capital": (_bounded(metrics["tangible_equity_to_assets_pct"], 3.0, 10.0), 18.0),
        "rotce": (_bounded(metrics["rotce_pct"], 0.0, 18.0), 18.0),
        "deposit_funding": (_bounded(metrics["deposit_funding_pct"], 30.0, 75.0), 10.0),
        "allowance_coverage_proxy": (_bounded(metrics["credit_loss_allowance_to_loans_pct"], 0.5, 2.0), 8.0),
        "net_interest_income_growth": (_bounded(metrics.get("net_interest_income_growth_pct"), -10.0, 10.0), 7.0),
        "aoci_capital_drag": (_bounded(metrics["aoci_to_tangible_equity_pct"], -40.0, 0.0), 7.0),
        "valuation": (_inverse(metrics["price_to_tangible_book_x"], 2.5, 0.7), 5.0),
    }
    return _finish(
        "BANK",
        metrics,
        components,
        required=[
            "assets_b",
            "tangible_equity_b",
            "net_income_ttm_b",
            "deposits_b",
            "loans_b",
            "tier1_ratio_pct",
            "tier1_well_capitalized_min_pct",
        ],
        optional=["credit_loss_allowance_b", "net_interest_income_growth_pct", "aoci_b", "market_cap_b"],
        hard_failures=hard,
    )


def evaluate_insurance_p_and_c(raw: Mapping[str, Any]) -> IndustryModelEvaluation:
    metrics = dict(raw)
    premiums = _number(metrics.get("premiums_earned_ttm_b"))
    claims = _number(metrics.get("claims_incurred_ttm_b"))
    underwriting = _number(metrics.get("underwriting_expense_ttm_b"))
    combined_expense = _number(metrics.get("combined_expense_ttm_b"))
    assets = _number(metrics.get("assets_b"))
    equity = _number(metrics.get("equity_b"))
    net_income = _number(metrics.get("net_income_ttm_b"))
    market_cap = _number(metrics.get("market_cap_b"))
    reported_input_combined = _number(
        metrics.get("company_reported_combined_ratio_pct")
    )
    monthly_combined = _number(metrics.get("monthly_combined_ratio_pct"))
    quarterly_combined = _number(metrics.get("quarterly_combined_ratio_pct"))
    trailing_combined = _number(metrics.get("ttm_combined_ratio_pct"))
    company_reported_combined = _coalesce_number(
        trailing_combined,
        _coalesce_number(quarterly_combined, reported_input_combined),
    )
    company_ratio_period = (
        "TTM"
        if _finite(trailing_combined)
        else "QUARTERLY"
        if _finite(quarterly_combined)
        else "COMPANY_REPORTED_UNSPECIFIED"
        if _finite(reported_input_combined)
        else "MISSING"
    )
    proxy_reconciled = bool(metrics.get("sec_proxy_reconciled", False))
    sec_proxy = _safe_div(combined_expense, premiums) * 100.0
    reconciliation_difference = (
        sec_proxy - company_reported_combined
        if _finite(sec_proxy) and _finite(company_reported_combined)
        else math.nan
    )
    if _finite(company_reported_combined):
        source_status = "COMPANY_REPORTED"
        combined_for_model = company_reported_combined
    elif _finite(sec_proxy) and proxy_reconciled:
        source_status = "SEC_PROXY_RECONCILED"
        combined_for_model = sec_proxy
    elif _finite(sec_proxy):
        source_status = "SEC_PROXY_UNRECONCILED"
        combined_for_model = sec_proxy
    else:
        source_status = "MISSING"
        combined_for_model = math.nan
    invested_assets = _number(metrics.get("invested_assets_b"))
    investment_income = _number(metrics.get("net_investment_income_ttm_b"))
    investment_yield = _number(metrics.get("investment_yield_pct"))
    if not _finite(investment_yield):
        investment_yield = _safe_div(investment_income, invested_assets) * 100.0
    metrics.update(
        {
            "company_reported_combined_ratio_pct": company_reported_combined,
            "monthly_combined_ratio_pct": monthly_combined,
            "quarterly_combined_ratio_pct": quarterly_combined,
            "ttm_combined_ratio_pct": trailing_combined,
            "combined_ratio_period_for_model": company_ratio_period,
            "sec_combined_ratio_proxy_pct": sec_proxy,
            "combined_ratio_proxy_pct": sec_proxy,
            "combined_ratio_for_model_pct": combined_for_model,
            "combined_ratio_reconciliation_difference_pp": reconciliation_difference,
            "combined_ratio_source_status": source_status,
            "loss_ratio_pct": _safe_div(claims, premiums) * 100.0,
            "equity_to_assets_pct": _safe_div(equity, assets) * 100.0,
            "roe_pct": _safe_div(
                net_income,
                _coalesce_number(metrics.get("average_equity_b"), equity),
            ) * 100.0,
            "price_to_book_x": _safe_div(market_cap, equity),
            "operating_roe_pct": _safe_div(
                _coalesce_number(metrics.get("operating_income_ttm_b"), net_income),
                _coalesce_number(metrics.get("average_equity_b"), equity),
            ) * 100.0,
            "investment_yield_pct": investment_yield,
        }
    )
    combined = _number(metrics["combined_ratio_for_model_pct"])
    capital_ratio = _number(metrics["equity_to_assets_pct"])
    hard = []
    warnings = []
    if math.isfinite(premiums) and premiums <= 0:
        hard.append("earned premiums are non-positive")
    if math.isfinite(combined) and combined > 110.0:
        hard.append("combined-ratio proxy exceeds 110%")
    if math.isfinite(capital_ratio) and capital_ratio < 8.0:
        hard.append("equity / assets is below 8%")
    if math.isfinite(net_income) and net_income <= 0:
        hard.append("TTM net income is non-positive")
    if source_status == "SEC_PROXY_UNRECONCILED":
        warnings.append(
            "SEC combined-ratio proxy is unreconciled to the company-reported definition"
        )
    if not _finite(company_reported_combined):
        warnings.append("company-reported combined ratio requires human KPI review")
    if _finite(monthly_combined) and not (
        _finite(quarterly_combined) or _finite(trailing_combined)
    ):
        warnings.append(
            "monthly combined ratio is not used as a normalized underwriting KPI without quarterly or TTM context"
        )
    premium_growth = _number(metrics.get("premium_growth_pct"))
    if _finite(premium_growth) and premium_growth < 5.0:
        warnings.append(
            "premium growth below 5% may indicate competition, pricing normalization or slower policy growth"
        )
    underwriting_score = _inverse(combined, 110.0, 90.0)
    if source_status == "SEC_PROXY_UNRECONCILED" and _finite(underwriting_score):
        underwriting_score = min(75.0, underwriting_score)
    source_quality_score = {
        "COMPANY_REPORTED": 100.0,
        "SEC_PROXY_RECONCILED": 85.0,
        "SEC_PROXY_UNRECONCILED": 40.0,
    }.get(source_status, math.nan)
    components = {
        "underwriting_profitability": (underwriting_score, 30.0),
        "premium_growth": (_bounded(metrics.get("premium_growth_pct"), -5.0, 15.0), 10.0),
        "policy_count_growth": (_bounded(metrics.get("policy_count_growth_pct"), -5.0, 10.0), 5.0),
        "reserve_development": (_inverse(metrics.get("reserve_development_to_premium_pct"), 5.0, -2.0), 15.0),
        "capital_strength": (_bounded(capital_ratio, 8.0, 25.0), 15.0),
        "roe": (_bounded(metrics["roe_pct"], 0.0, 18.0), 10.0),
        "valuation": (_inverse(metrics["price_to_book_x"], 3.0, 0.8), 5.0),
        "combined_ratio_source_quality": (source_quality_score, 10.0),
    }
    evaluation = _finish(
        "INSURANCE_P_AND_C",
        metrics,
        components,
        required=[
            "premiums_earned_ttm_b",
            "combined_expense_ttm_b",
            "assets_b",
            "equity_b",
            "net_income_ttm_b",
        ],
        optional=[
            "claims_incurred_ttm_b",
            "underwriting_expense_ttm_b",
            "premium_growth_pct",
            "reserve_development_to_premium_pct",
            "market_cap_b",
            "company_reported_combined_ratio_pct",
            "accident_year_combined_ratio_pct",
            "catastrophe_loss_ratio_pct",
            "policy_count_growth_pct",
            "invested_assets_b",
            "investment_yield_pct",
            "pretax_income_ttm_b",
        ],
        hard_failures=hard,
        warnings=warnings,
    )
    stress = calculate_p_and_c_stress(metrics)
    evaluation.metrics.update(stress)
    return evaluation


def calculate_p_and_c_stress(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Auditable P&C stress using reported premiums, invested assets and yield."""
    premiums = _number(raw.get("premiums_earned_ttm_b"))
    combined = _coalesce_number(
        raw.get("combined_ratio_for_model_pct"),
        raw.get("company_reported_combined_ratio_pct"),
    )
    if not _finite(combined):
        combined = _number(raw.get("sec_combined_ratio_proxy_pct"))
    premium_growth = _number(raw.get("premium_growth_pct"))
    investment_income = _number(raw.get("net_investment_income_ttm_b"))
    invested_assets = _number(raw.get("invested_assets_b"))
    investment_yield = _number(raw.get("investment_yield_pct"))
    pretax_income = _number(raw.get("pretax_income_ttm_b"))
    equity = _number(raw.get("equity_b"))
    average_equity = _coalesce_number(raw.get("average_equity_b"), equity)
    assets = _number(raw.get("assets_b"))
    reserve_development = _number(raw.get("reserve_development_to_premium_pct"))
    required = {
        "premiums_earned_ttm_b": premiums,
        "combined_ratio_pct": combined,
        "premium_growth_pct": premium_growth,
        "net_investment_income_ttm_b": investment_income,
        "invested_assets_b": invested_assets,
        "investment_yield_pct": investment_yield,
        "pretax_income_ttm_b": pretax_income,
        "equity_b": equity,
        "assets_b": assets,
    }
    missing = [name for name, value in required.items() if not _finite(value)]
    empty = {
        "p_and_c_stress_cr_mild": math.nan,
        "p_and_c_stress_cr_moderate": math.nan,
        "p_and_c_stress_cr_severe": math.nan,
        "p_and_c_stress_underwriting_income_mild_b": math.nan,
        "p_and_c_stress_underwriting_income_moderate_b": math.nan,
        "p_and_c_stress_underwriting_income_severe_b": math.nan,
        "p_and_c_stress_pretax_income_moderate_b": math.nan,
        "p_and_c_stress_roe_moderate_pct": math.nan,
        "p_and_c_stress_equity_to_assets_moderate_pct": math.nan,
        "p_and_c_stress_survival_moderate": False,
        "p_and_c_stress_status": "ABSTAIN" if missing else "MISSING",
        "p_and_c_stress_missing_inputs": missing,
    }
    if missing:
        return empty

    base_underwriting = premiums * (1.0 - combined / 100.0)
    other_pretax = pretax_income - base_underwriting - investment_income
    mild_cr = combined + 3.0
    moderate_cr = combined + 6.0
    severe_cr = combined + 10.0
    mild_premiums = premiums
    moderate_premiums = premiums * (1.0 + min(premium_growth, 0.0) / 100.0)
    severe_premiums = premiums * 0.95
    mild_underwriting = mild_premiums * (1.0 - mild_cr / 100.0)
    moderate_underwriting = moderate_premiums * (1.0 - moderate_cr / 100.0)
    severe_underwriting = severe_premiums * (1.0 - severe_cr / 100.0)
    moderate_yield = max(0.0, investment_yield - 0.50)
    severe_yield = max(0.0, investment_yield - 1.00)
    moderate_investment_income = invested_assets * moderate_yield / 100.0
    severe_investment_income = invested_assets * severe_yield / 100.0
    severe_reserve_shock_pct = max(2.0, reserve_development + 2.0) if _finite(reserve_development) else 2.0
    moderate_pretax = moderate_underwriting + moderate_investment_income + other_pretax
    severe_pretax = (
        severe_underwriting
        + severe_investment_income
        + other_pretax
        - severe_premiums * severe_reserve_shock_pct / 100.0
    )
    baseline_to_moderate_loss = max(0.0, pretax_income - moderate_pretax)
    moderate_equity = equity - baseline_to_moderate_loss
    moderate_equity_to_assets = _safe_div(moderate_equity, assets) * 100.0
    moderate_roe = _safe_div(moderate_pretax * (1.0 - 0.21), average_equity) * 100.0
    survives = bool(moderate_pretax >= 0.0 and moderate_equity_to_assets >= 8.0)
    return {
        "p_and_c_stress_cr_mild": mild_cr,
        "p_and_c_stress_cr_moderate": moderate_cr,
        "p_and_c_stress_cr_severe": severe_cr,
        "p_and_c_stress_underwriting_income_mild_b": mild_underwriting,
        "p_and_c_stress_underwriting_income_moderate_b": moderate_underwriting,
        "p_and_c_stress_underwriting_income_severe_b": severe_underwriting,
        "p_and_c_stress_pretax_income_moderate_b": moderate_pretax,
        "p_and_c_stress_pretax_income_severe_b": severe_pretax,
        "p_and_c_stress_roe_moderate_pct": moderate_roe,
        "p_and_c_stress_equity_to_assets_moderate_pct": moderate_equity_to_assets,
        "p_and_c_stress_survival_moderate": survives,
        "p_and_c_stress_status": "PASS" if survives else "FAIL",
        "p_and_c_stress_missing_inputs": [],
        "p_and_c_stress_investment_yield_moderate_pct": moderate_yield,
        "p_and_c_stress_investment_yield_severe_pct": severe_yield,
        "p_and_c_stress_severe_reserve_shock_pct": severe_reserve_shock_pct,
    }


def evaluate_insurance_life(raw: Mapping[str, Any]) -> IndustryModelEvaluation:
    metrics = dict(raw)
    premiums = _number(metrics.get("premiums_earned_ttm_b"))
    investment_income = _number(metrics.get("net_investment_income_ttm_b"))
    benefits = _number(metrics.get("policyholder_benefits_ttm_b"))
    assets = _number(metrics.get("assets_b"))
    equity = _number(metrics.get("equity_b"))
    net_income = _number(metrics.get("net_income_ttm_b"))
    market_cap = _number(metrics.get("market_cap_b"))
    operating_inflow = premiums + investment_income
    metrics.update(
        {
            "operating_inflow_ttm_b": operating_inflow,
            "benefit_ratio_pct": _safe_div(benefits, operating_inflow) * 100.0,
            "equity_to_assets_pct": _safe_div(equity, assets) * 100.0,
            "roe_pct": _safe_div(
                net_income,
                _coalesce_number(metrics.get("average_equity_b"), equity),
            ) * 100.0,
            "price_to_book_x": _safe_div(market_cap, equity),
        }
    )
    hard = []
    if math.isfinite(premiums) and premiums <= 0:
        hard.append("earned premiums are non-positive")
    if math.isfinite(operating_inflow) and operating_inflow <= 0:
        hard.append("premium plus investment income is non-positive")
    if _finite(metrics["benefit_ratio_pct"]) and metrics["benefit_ratio_pct"] > 105.0:
        hard.append("policyholder-benefit ratio exceeds 105% of premium plus investment income")
    if _finite(metrics["equity_to_assets_pct"]) and metrics["equity_to_assets_pct"] < 3.0:
        hard.append("equity / assets is below 3%")
    if math.isfinite(net_income) and net_income <= 0:
        hard.append("TTM net income is non-positive")
    components = {
        "benefit_burden": (_inverse(metrics["benefit_ratio_pct"], 105.0, 65.0), 30.0),
        "capital_strength": (_bounded(metrics["equity_to_assets_pct"], 3.0, 12.0), 25.0),
        "roe": (_bounded(metrics["roe_pct"], 0.0, 15.0), 20.0),
        "premium_growth": (_bounded(metrics.get("premium_growth_pct"), -5.0, 12.0), 15.0),
        "valuation": (_inverse(metrics["price_to_book_x"], 2.5, 0.7), 10.0),
    }
    return _finish(
        "INSURANCE_LIFE",
        metrics,
        components,
        required=[
            "premiums_earned_ttm_b",
            "net_investment_income_ttm_b",
            "policyholder_benefits_ttm_b",
            "assets_b",
            "equity_b",
            "net_income_ttm_b",
        ],
        optional=["premium_growth_pct", "market_cap_b"],
        hard_failures=hard,
    )


def evaluate_reit_equity(raw: Mapping[str, Any]) -> IndustryModelEvaluation:
    metrics = dict(raw)
    net_income = _number(metrics.get("net_income_ttm_b"))
    dna = abs(_number(metrics.get("real_estate_dna_ttm_b")))
    gain = _number(metrics.get("gain_on_property_sale_ttm_b"))
    impairment = abs(_number(metrics.get("real_estate_impairment_ttm_b")))
    maintenance = abs(_number(metrics.get("maintenance_capex_b")))
    interest = abs(_number(metrics.get("interest_ttm_b")))
    tax = _number(metrics.get("tax_ttm_b"))
    debt = _number(metrics.get("debt_b"))
    cash = _number(metrics.get("cash_b"))
    market_cap = _number(metrics.get("market_cap_b"))
    dividends = abs(_number(metrics.get("dividends_ttm_b")))
    ffo = net_income + dna - gain + impairment
    affo = ffo - maintenance
    ebitdare = net_income + interest + max(tax, 0.0) + dna - gain + impairment
    net_debt = max(debt - cash, 0.0)
    metrics.update(
        {
            "ffo_proxy_b": ffo,
            "affo_proxy_b": affo,
            "ebitdare_proxy_b": ebitdare,
            "affo_yield_pct": _safe_div(affo, market_cap) * 100.0,
            "price_to_ffo_x": _safe_div(market_cap, ffo),
            "dividend_to_affo_pct": _safe_div(dividends, affo) * 100.0,
            "net_debt_to_ebitdare_x": _safe_div(net_debt, ebitdare),
            "interest_coverage_x": _interest_coverage(ebitdare, interest, debt),
        }
    )
    hard = []
    if math.isfinite(ffo) and ffo <= 0:
        hard.append("Nareit FFO proxy is non-positive")
    if math.isfinite(affo) and affo <= 0:
        hard.append("AFFO proxy is non-positive")
    if _finite(metrics["net_debt_to_ebitdare_x"]) and metrics["net_debt_to_ebitdare_x"] > 8.0:
        hard.append("net debt / EBITDAre proxy exceeds 8x")
    if debt > 0 and _finite(metrics["interest_coverage_x"]) and metrics["interest_coverage_x"] < 1.5:
        hard.append("EBITDAre interest coverage is below 1.5x")
    if _finite(metrics["dividend_to_affo_pct"]) and metrics["dividend_to_affo_pct"] > 110.0:
        hard.append("dividends exceed 110% of AFFO proxy")
    components = {
        "affo_yield": (_bounded(metrics["affo_yield_pct"], 2.0, 8.0), 25.0),
        "price_to_ffo": (_inverse(metrics["price_to_ffo_x"], 30.0, 10.0), 20.0),
        "leverage": (_inverse(metrics["net_debt_to_ebitdare_x"], 8.0, 2.0), 20.0),
        "interest_coverage": (_bounded(metrics["interest_coverage_x"], 1.5, 5.0), 15.0),
        "dividend_coverage": (_inverse(metrics["dividend_to_affo_pct"], 110.0, 60.0), 10.0),
        "lease_revenue_growth": (_bounded(metrics.get("lease_revenue_growth_pct"), -5.0, 10.0), 10.0),
    }
    return _finish(
        "REIT_EQUITY",
        metrics,
        components,
        required=[
            "net_income_ttm_b",
            "real_estate_dna_ttm_b",
            "maintenance_capex_b",
            "interest_ttm_b",
            "debt_b",
            "cash_b",
            "market_cap_b",
        ],
        optional=[
            "gain_on_property_sale_ttm_b",
            "real_estate_impairment_ttm_b",
            "tax_ttm_b",
            "dividends_ttm_b",
            "lease_revenue_growth_pct",
        ],
        hard_failures=hard,
    )


def evaluate_reit_mortgage(raw: Mapping[str, Any]) -> IndustryModelEvaluation:
    metrics = dict(raw)
    assets = _number(metrics.get("assets_b"))
    equity = _number(metrics.get("equity_b"))
    net_income = _number(metrics.get("net_income_ttm_b"))
    recurring_earnings = _number(metrics.get("recurring_earnings_ttm_b"))
    net_interest_income = _number(metrics.get("net_interest_income_ttm_b"))
    market_cap = _number(metrics.get("market_cap_b"))
    dividends = abs(_number(metrics.get("dividends_ttm_b")))
    metrics.update(
        {
            "assets_to_equity_x": _safe_div(assets, equity),
            "equity_to_assets_pct": _safe_div(equity, assets) * 100.0,
            "roe_pct": _safe_div(
                net_income,
                _coalesce_number(metrics.get("average_equity_b"), equity),
            ) * 100.0,
            "price_to_book_x": _safe_div(market_cap, equity),
            "dividend_payout_pct": _safe_div(dividends, recurring_earnings) * 100.0,
            "dividend_to_gaap_net_income_pct": _safe_div(dividends, net_income) * 100.0,
            "dividend_to_gaap_net_interest_income_pct": (
                _safe_div(dividends, net_interest_income) * 100.0
            ),
        }
    )
    hard = []
    warnings = []
    if _finite(metrics["assets_to_equity_x"]) and metrics["assets_to_equity_x"] > 15.0:
        hard.append("assets / equity exceeds 15x")
    if _finite(metrics["equity_to_assets_pct"]) and metrics["equity_to_assets_pct"] < 5.0:
        hard.append("equity / assets is below 5%")
    if math.isfinite(net_income) and net_income <= 0:
        hard.append("TTM net income is non-positive")
    if (
        _finite(metrics["dividend_to_gaap_net_income_pct"])
        and metrics["dividend_to_gaap_net_income_pct"] > 120.0
    ):
        warnings.append(
            "dividend exceeds 120% of GAAP net income; recurring-earnings coverage is authoritative"
        )
    components = {
        "capital_strength": (_bounded(metrics["equity_to_assets_pct"], 5.0, 15.0), 25.0),
        "leverage": (_inverse(metrics["assets_to_equity_x"], 15.0, 5.0), 20.0),
        "roe": (_bounded(metrics["roe_pct"], 0.0, 15.0), 20.0),
        "dividend_coverage": (_inverse(metrics["dividend_payout_pct"], 120.0, 70.0), 15.0),
        "book_valuation": (_inverse(metrics["price_to_book_x"], 1.5, 0.7), 10.0),
        "net_interest_income_growth": (_bounded(metrics.get("net_interest_income_growth_pct"), -10.0, 10.0), 10.0),
    }
    return _finish(
        "REIT_MORTGAGE",
        metrics,
        components,
        required=[
            "assets_b",
            "equity_b",
            "net_income_ttm_b",
            "recurring_earnings_ttm_b",
            "market_cap_b",
        ],
        optional=[
            "dividends_ttm_b",
            "net_interest_income_ttm_b",
            "net_interest_income_growth_pct",
        ],
        hard_failures=hard,
        warnings=warnings,
    )


def evaluate_regulated_utility(raw: Mapping[str, Any]) -> IndustryModelEvaluation:
    metrics = dict(raw)
    ebit = _number(metrics.get("ebit_ttm_b"))
    interest = abs(_number(metrics.get("interest_ttm_b")))
    debt = _number(metrics.get("debt_b"))
    equity = _number(metrics.get("equity_b"))
    ocf = _number(metrics.get("ocf_ttm_b"))
    capex = abs(_number(metrics.get("capex_ttm_b")))
    net_income = _number(metrics.get("net_income_ttm_b"))
    dividends = abs(_number(metrics.get("dividends_ttm_b")))
    metrics.update(
        {
            "interest_coverage_x": _interest_coverage(ebit, interest, debt),
            "debt_to_capital_pct": _safe_div(debt, debt + equity) * 100.0,
            "ocf_to_capex_x": _safe_div(ocf, capex),
            "roe_pct": _safe_div(
                net_income,
                _coalesce_number(metrics.get("average_equity_b"), equity),
            ) * 100.0,
            "earnings_to_dividend_x": _safe_div(net_income, dividends),
        }
    )
    hard = []
    if math.isfinite(equity) and equity <= 0:
        hard.append("book equity is non-positive")
    if debt > 0 and _finite(metrics["interest_coverage_x"]) and metrics["interest_coverage_x"] < 1.5:
        hard.append("EBIT interest coverage is below 1.5x")
    if _finite(metrics["debt_to_capital_pct"]) and metrics["debt_to_capital_pct"] > 75.0:
        hard.append("debt / total capital exceeds 75%")
    if math.isfinite(ocf) and ocf <= 0:
        hard.append("TTM operating cash flow is non-positive")
    if math.isfinite(net_income) and net_income <= 0:
        hard.append("TTM net income is non-positive")
    components = {
        "interest_coverage": (_bounded(metrics["interest_coverage_x"], 1.5, 5.0), 25.0),
        "capital_structure": (_inverse(metrics["debt_to_capital_pct"], 75.0, 40.0), 20.0),
        "capex_funding": (_bounded(metrics["ocf_to_capex_x"], 0.5, 1.2), 20.0),
        "roe_proxy": (_bounded(metrics["roe_pct"], 3.0, 12.0), 15.0),
        "regulated_asset_growth_proxy": (_bounded(metrics.get("ppe_growth_pct"), 0.0, 8.0), 10.0),
        "dividend_coverage": (_bounded(metrics["earnings_to_dividend_x"], 0.8, 1.5), 10.0),
    }
    return _finish(
        "REGULATED_UTILITY",
        metrics,
        components,
        required=[
            "ebit_ttm_b",
            "interest_ttm_b",
            "debt_b",
            "equity_b",
            "ocf_ttm_b",
            "capex_ttm_b",
            "net_income_ttm_b",
        ],
        optional=["ppe_growth_pct", "dividends_ttm_b"],
        hard_failures=hard,
    )


def evaluate_cyclical_midcycle(raw: Mapping[str, Any]) -> IndustryModelEvaluation:
    metrics = dict(raw)
    history = [_number(value) for value in metrics.get("ebitda_history_b", []) if _finite(value)]
    if len(history) < 5:
        return IndustryModelEvaluation(
            model_key="CYCLICAL_MIDCYCLE",
            decision="ABSTAIN",
            score=math.nan,
            metrics=metrics,
            required_missing=["ebitda_history_b>=5"],
            warnings=["At least five point-in-time annual EBITDA observations are required"],
        )
    midcycle = statistics.median(history)
    trough = _percentile(history, 0.20)
    peak = _percentile(history, 0.90)
    current = _number(metrics.get("current_ebitda_b"))
    ev = _number(metrics.get("enterprise_value_b"))
    valuation_ev = ev if math.isfinite(ev) and ev > 0 else math.nan
    debt = _number(metrics.get("debt_b"))
    cash = _number(metrics.get("cash_b"))
    interest = abs(_number(metrics.get("interest_ttm_b")))
    dna = abs(_number(metrics.get("dna_ttm_b")))
    real_fcf = _number(metrics.get("real_fcf_ttm_b"))
    net_debt = max(debt - cash, 0.0)
    fully_cash_covered = bool(
        math.isfinite(debt)
        and math.isfinite(cash)
        and (debt <= 0.01 or cash >= debt)
    )
    trough_ebit = trough - dna
    trough_interest_coverage = (
        math.inf
        if fully_cash_covered
        else _interest_coverage(trough_ebit, interest, debt)
    )
    metrics.update(
        {
            "midcycle_ebitda_b": midcycle,
            "trough_ebitda_b": trough,
            "peak_ebitda_b": peak,
            "current_to_midcycle_x": _safe_div(current, midcycle),
            "ev_to_midcycle_ebitda_x": _safe_div(valuation_ev, midcycle),
            "net_debt_to_trough_ebitda_x": _safe_div(net_debt, trough),
            "trough_interest_coverage_x": trough_interest_coverage,
            "debt_service_method": (
                "net_cash_non_binding" if fully_cash_covered else "reported_interest"
            ),
            "real_fcf_to_ev_yield_pct": _safe_div(real_fcf, valuation_ev) * 100.0,
        }
    )
    warnings = []
    if math.isfinite(ev) and ev <= 0:
        metrics["enterprise_value_b"] = math.nan
        warnings.append(
            "non-positive enterprise value requires a separate net-cash special-situation model"
        )
    hard = []
    if midcycle <= 0:
        hard.append("mid-cycle EBITDA is non-positive")
    if current <= 0 and net_debt > 0:
        hard.append("current EBITDA is non-positive while net debt remains")
    if trough <= 0 and net_debt > 0:
        hard.append("trough EBITDA is non-positive while net debt remains")
    if not fully_cash_covered and debt > 0 and _finite(metrics["trough_interest_coverage_x"]) and metrics["trough_interest_coverage_x"] < 1.25:
        hard.append("trough interest coverage is below 1.25x")
    if _finite(metrics["net_debt_to_trough_ebitda_x"]) and metrics["net_debt_to_trough_ebitda_x"] > 5.0:
        hard.append("net debt / trough EBITDA exceeds 5x")
    components = {
        "midcycle_valuation": (_inverse(metrics["ev_to_midcycle_ebitda_x"], 15.0, 5.0), 30.0),
        "trough_interest_coverage": (
            100.0
            if fully_cash_covered
            else _bounded(metrics["trough_interest_coverage_x"], 1.25, 5.0),
            25.0,
        ),
        "trough_leverage": (_inverse(metrics["net_debt_to_trough_ebitda_x"], 5.0, 0.0), 20.0),
        "cash_generation": (_bounded(metrics["real_fcf_to_ev_yield_pct"], 0.0, 8.0), 15.0),
        "cycle_position": (
            0.0 if current <= 0 else _inverse(metrics["current_to_midcycle_x"], 1.6, 0.8),
            10.0,
        ),
    }
    return _finish(
        "CYCLICAL_MIDCYCLE",
        metrics,
        components,
        required=[
            "current_ebitda_b",
            "enterprise_value_b",
            "debt_b",
            "cash_b",
            *([] if fully_cash_covered else ["interest_ttm_b"]),
            "dna_ttm_b",
            "real_fcf_ttm_b",
        ],
        optional=[],
        hard_failures=hard,
        warnings=warnings,
    )


def evaluate_financial_lender(raw: Mapping[str, Any]) -> IndustryModelEvaluation:
    metrics = dict(raw)
    assets = _number(metrics.get("assets_b"))
    equity = _number(metrics.get("tangible_equity_b"))
    net_income = _number(metrics.get("net_income_ttm_b"))
    loans = _number(metrics.get("loans_b"))
    allowance = _number(metrics.get("credit_loss_allowance_b"))
    market_cap = _number(metrics.get("market_cap_b"))
    metrics.update(
        {
            "tangible_equity_to_assets_pct": _safe_div(equity, assets) * 100.0,
            "rotce_pct": _safe_div(
                net_income,
                _coalesce_number(metrics.get("average_tangible_equity_b"), equity),
            ) * 100.0,
            "credit_loss_allowance_to_loans_pct": _safe_div(allowance, loans) * 100.0,
            "price_to_tangible_book_x": _safe_div(market_cap, equity),
        }
    )
    hard = []
    if _finite(metrics["tangible_equity_to_assets_pct"]) and metrics["tangible_equity_to_assets_pct"] < 5.0:
        hard.append("tangible equity / assets is below 5%")
    if math.isfinite(net_income) and net_income <= 0:
        hard.append("TTM net income is non-positive")
    components = {
        "capital_strength": (_bounded(metrics["tangible_equity_to_assets_pct"], 5.0, 15.0), 30.0),
        "rotce": (_bounded(metrics["rotce_pct"], 0.0, 18.0), 25.0),
        "allowance_coverage_proxy": (_bounded(metrics["credit_loss_allowance_to_loans_pct"], 0.5, 2.5), 15.0),
        "net_interest_income_growth": (_bounded(metrics.get("net_interest_income_growth_pct"), -10.0, 10.0), 15.0),
        "valuation": (_inverse(metrics["price_to_tangible_book_x"], 2.5, 0.7), 15.0),
    }
    return _finish(
        "FINANCIAL_LENDER",
        metrics,
        components,
        required=[
            "assets_b",
            "tangible_equity_b",
            "net_income_ttm_b",
            "loans_b",
            "credit_loss_allowance_b",
        ],
        optional=["net_interest_income_growth_pct", "market_cap_b"],
        hard_failures=hard,
    )


def evaluate_financial_fee(raw: Mapping[str, Any]) -> IndustryModelEvaluation:
    metrics = dict(raw)
    revenue = _number(metrics.get("revenue_ttm_b"))
    ebit = _number(metrics.get("ebit_ttm_b"))
    ocf = _number(metrics.get("ocf_ttm_b"))
    net_income = _number(metrics.get("net_income_ttm_b"))
    equity = _number(metrics.get("tangible_equity_b"))
    assets = _number(metrics.get("assets_b"))
    debt = _number(metrics.get("debt_b"))
    cash = _number(metrics.get("cash_b"))
    ebitda = _number(metrics.get("ebitda_ttm_b"))
    market_cap = _number(metrics.get("market_cap_b"))
    metrics.update(
        {
            "operating_margin_pct": _safe_div(ebit, revenue) * 100.0,
            "rotce_pct": _safe_div(
                net_income,
                _coalesce_number(metrics.get("average_tangible_equity_b"), equity),
            ) * 100.0,
            "ocf_to_net_income_x": _safe_div(ocf, net_income),
            "net_debt_to_ebitda_x": _safe_div(max(debt - cash, 0.0), ebitda),
            "ocf_yield_pct": _safe_div(ocf, market_cap) * 100.0,
            "tangible_equity_to_assets_pct": _safe_div(equity, assets) * 100.0,
        }
    )
    hard = []
    if math.isfinite(equity) and equity <= 0:
        hard.append("tangible equity is non-positive")
    if math.isfinite(net_income) and net_income <= 0:
        hard.append("TTM net income is non-positive")
    if math.isfinite(ocf) and ocf <= 0:
        hard.append("TTM operating cash flow is non-positive")
    components = {
        "operating_margin": (_bounded(metrics["operating_margin_pct"], 5.0, 35.0), 25.0),
        "rotce": (_bounded(metrics["rotce_pct"], 0.0, 25.0), 20.0),
        "cash_conversion": (_bounded(metrics["ocf_to_net_income_x"], 0.5, 1.3), 20.0),
        "revenue_growth": (_bounded(metrics.get("revenue_growth_pct"), -5.0, 15.0), 15.0),
        "balance_sheet": (_inverse(metrics["net_debt_to_ebitda_x"], 5.0, 0.0), 10.0),
        "cash_yield": (_bounded(metrics["ocf_yield_pct"], 2.0, 10.0), 10.0),
    }
    return _finish(
        "FINANCIAL_FEE",
        metrics,
        components,
        required=[
            "revenue_ttm_b",
            "ebit_ttm_b",
            "ocf_ttm_b",
            "net_income_ttm_b",
            "tangible_equity_b",
            "assets_b",
            "debt_b",
            "cash_b",
            "ebitda_ttm_b",
            "market_cap_b",
        ],
        optional=["revenue_growth_pct"],
        hard_failures=hard,
    )


def _evaluate_asset_manager(
    raw: Mapping[str, Any], model_key: str
) -> IndustryModelEvaluation:
    metrics = dict(raw)
    revenue = _number(metrics.get("revenue_ttm_b"))
    ocf = _number(metrics.get("ocf_ttm_b"))
    net_income = _number(metrics.get("net_income_ttm_b"))
    debt = _number(metrics.get("debt_b"))
    cash = _number(metrics.get("cash_b"))
    fee_related_earnings = _number(metrics.get("fee_related_earnings_b"))
    ebitda = _number(metrics.get("ebitda_ttm_b"))
    metrics.update(
        {
            "ocf_to_net_income_x": _safe_div(ocf, net_income),
            "compensation_to_revenue_pct": _safe_div(
                metrics.get("compensation_expense_b"), revenue
            )
            * 100.0,
            "net_debt_to_fre_or_ebitda_x": _safe_div(
                max(debt - cash, 0.0),
                fee_related_earnings
                if math.isfinite(fee_related_earnings) and fee_related_earnings > 0
                else ebitda,
            ),
        }
    )
    required_by_model = {
        "ALTERNATIVE_ASSET_MANAGER": [
            "fee_related_earnings_b",
            "management_fee_revenue_b",
            "fee_paying_aum_b",
            "aum_growth_pct",
            "permanent_capital_pct",
            "compensation_expense_b",
        ],
        "TRADITIONAL_ASSET_MANAGER": [
            "management_fee_revenue_b",
            "aum_b",
            "aum_growth_pct",
            "organic_net_flows_pct",
            "compensation_expense_b",
        ],
        "INSURANCE_LINKED_ASSET_MANAGER": [
            "fee_related_earnings_b",
            "insurance_assets_pct",
            "permanent_capital_pct",
            "fee_paying_aum_b",
            "compensation_expense_b",
        ],
        "OTHER_FEE_FINANCIAL": [
            "management_fee_revenue_b",
            "aum_b",
            "compensation_expense_b",
        ],
    }
    required = [
        "revenue_ttm_b",
        "ocf_ttm_b",
        "net_income_ttm_b",
        "debt_b",
        "cash_b",
        *required_by_model[model_key],
    ]
    hard = []
    if math.isfinite(net_income) and net_income <= 0:
        hard.append("TTM net income is non-positive")
    if math.isfinite(ocf) and ocf <= 0:
        hard.append("TTM operating cash flow is non-positive")
    components = {
        "aum_growth": (_bounded(metrics.get("aum_growth_pct"), -5.0, 15.0), 20.0),
        "organic_net_flows": (_bounded(metrics.get("organic_net_flows_pct"), -5.0, 10.0), 15.0),
        "permanent_capital": (_bounded(metrics.get("permanent_capital_pct"), 0.0, 75.0), 15.0),
        "cash_conversion": (_bounded(metrics["ocf_to_net_income_x"], 0.5, 1.3), 20.0),
        "compensation_discipline": (_inverse(metrics["compensation_to_revenue_pct"], 60.0, 20.0), 15.0),
        "balance_sheet": (_inverse(metrics["net_debt_to_fre_or_ebitda_x"], 5.0, 0.0), 15.0),
    }
    evaluation = _finish(
        model_key,
        metrics,
        components,
        required=required,
        optional=[
            "performance_fees_b",
            "realized_carry_b",
            "net_flows_b",
            "insurance_assets_pct",
        ],
        hard_failures=hard,
        warnings=[
            "Company-defined AUM/FRE/flow KPIs must be point-in-time and may not be inferred from GAAP revenue"
        ],
    )
    return evaluation


def evaluate_alternative_asset_manager(raw: Mapping[str, Any]) -> IndustryModelEvaluation:
    return _evaluate_asset_manager(raw, "ALTERNATIVE_ASSET_MANAGER")


def evaluate_traditional_asset_manager(raw: Mapping[str, Any]) -> IndustryModelEvaluation:
    return _evaluate_asset_manager(raw, "TRADITIONAL_ASSET_MANAGER")


def evaluate_insurance_linked_asset_manager(raw: Mapping[str, Any]) -> IndustryModelEvaluation:
    return _evaluate_asset_manager(raw, "INSURANCE_LINKED_ASSET_MANAGER")


def evaluate_other_fee_financial(raw: Mapping[str, Any]) -> IndustryModelEvaluation:
    return _evaluate_asset_manager(raw, "OTHER_FEE_FINANCIAL")


MODEL_EVALUATORS = {
    "BANK": evaluate_bank,
    "INSURANCE_P_AND_C": evaluate_insurance_p_and_c,
    "INSURANCE_LIFE": evaluate_insurance_life,
    "REIT_EQUITY": evaluate_reit_equity,
    "REIT_MORTGAGE": evaluate_reit_mortgage,
    "REGULATED_UTILITY": evaluate_regulated_utility,
    "CYCLICAL_MIDCYCLE": evaluate_cyclical_midcycle,
    "FINANCIAL_LENDER": evaluate_financial_lender,
    "FINANCIAL_FEE": evaluate_financial_fee,
    "ALTERNATIVE_ASSET_MANAGER": evaluate_alternative_asset_manager,
    "TRADITIONAL_ASSET_MANAGER": evaluate_traditional_asset_manager,
    "INSURANCE_LINKED_ASSET_MANAGER": evaluate_insurance_linked_asset_manager,
    "OTHER_FEE_FINANCIAL": evaluate_other_fee_financial,
}


def evaluate_industry_model(model_key: str, metrics: Mapping[str, Any]) -> IndustryModelEvaluation:
    evaluator = MODEL_EVALUATORS.get(str(model_key or "").upper())
    if evaluator is None:
        return IndustryModelEvaluation(
            model_key=str(model_key or "UNKNOWN"),
            decision="ABSTAIN",
            score=math.nan,
            metrics=dict(metrics),
            required_missing=["implemented model route"],
            warnings=["No specialized evaluator is registered for this model key"],
        )
    return evaluator(metrics)


def assess_specialized_data_confidence(
    evidence_stats: Mapping[str, Any],
    evaluation: IndustryModelEvaluation,
    extra_optional_missing: Iterable[str] = (),
) -> dict:
    score = 100.0
    reasons: List[str] = []
    selected_count = int(evidence_stats.get("selected_source_count") or 0)
    accepted_ratio = _number(evidence_stats.get("accepted_at_ratio"))
    fallback_tag_ratio = _number(evidence_stats.get("fallback_tag_ratio"))
    period_anomalies = int(evidence_stats.get("period_anomaly_count") or 0)
    if selected_count <= 0:
        score -= 45.0
        reasons.append("No selected SEC source facts")
    elif not math.isfinite(accepted_ratio) or accepted_ratio < 0.50:
        score -= 35.0
        reasons.append("Less than half of selected SEC facts have EDGAR acceptance timestamps")
    elif accepted_ratio < 0.80:
        score -= 15.0
        reasons.append("Some selected SEC facts use filing-date availability fallback")
    if period_anomalies:
        score -= min(30.0, period_anomalies * 10.0)
        reasons.append(f"{period_anomalies} selected facts have abnormal periods")
    if math.isfinite(fallback_tag_ratio) and fallback_tag_ratio >= 0.75:
        score -= 10.0
        reasons.append("Most selected facts use alternate XBRL tag mappings")
    elif math.isfinite(fallback_tag_ratio) and fallback_tag_ratio >= 0.40:
        score -= 5.0
        reasons.append("A material share of facts uses alternate XBRL tag mappings")
    missing_optional = sorted(set(evaluation.optional_missing) | set(extra_optional_missing))
    if missing_optional:
        penalty = min(25.0, len(missing_optional) * 4.0)
        score -= penalty
        reasons.append("Optional model evidence missing: " + ", ".join(missing_optional))
    if evaluation.required_missing:
        score = min(score, 45.0)
        reasons.append("Required model evidence missing: " + ", ".join(evaluation.required_missing))
    if evaluation.score_coverage and evaluation.score_coverage < 0.80:
        score -= min(15.0, (0.80 - evaluation.score_coverage) * 50.0)
        reasons.append(f"Specialized score coverage is {evaluation.score_coverage:.0%}")
    score = round(max(0.0, min(100.0, score)), 2)
    return {
        "score": score,
        "abstain": score < 70.0 or evaluation.decision == "ABSTAIN",
        "reasons": reasons or ["Specialized model evidence coverage is complete"],
    }
