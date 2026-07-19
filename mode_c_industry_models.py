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
            "quarterly_combïû¶‰žËkºwµç@€€€€€€€€€€€‰Á•…­}•‰¥Ñ‘…}ˆˆèÁ•…¬°4(€€€€€€€€€€€€‰ÕÉÉ•¹Ñ}Ñ½}µ¥‘å±•}àˆè}Í…™•}‘¥Ø¡ÕÉÉ•¹Ð°µ¥‘å±”¤°4(€€€€€€€€€€€€‰•Ù}Ñ½}µ¥‘å±•}•‰¥Ñ‘…}àˆè}Í…™•}‘¥Ø¡Ù…±Õ…Ñ¥½¹}•Ø°µ¥‘å±”¤°4(€€€€€€€€€€€€‰¹•Ñ}‘•‰Ñ}Ñ½}ÑÉ½Õ¡}•‰¥Ñ‘…}àˆè}Í…™•}‘¥Ø¡¹•Ñ}‘•‰Ð°ÑÉ½Õ ¤°4(€€€€€€€€€€€€‰ÑÉ½Õ¡}¥¹Ñ•É•ÍÑ}½Ù•É…•}àˆèÑÉ½Õ¡}¥¹Ñ•É•ÍÑ}½Ù•É…”°4(€€€€€€€€€€€€‰‘•‰Ñ}Í•ÉÙ¥•}µ•Ñ¡½ˆè€ 4(€€€€€€€€€€€€€€€€‰¹•Ñ}…Í¡}¹½¹}‰¥¹‘¥¹œˆ¥˜™Õ±±å}…Í¡}½Ù•É••±Í”€‰É•Á½ÉÑ•‘}¥¹Ñ•É•ÍÐˆ4(€€€€€€€€€€€€¤°4(€€€€€€€€€€€€‰É•…±}™™}Ñ½}•Ù}å¥•±‘}ÁÐˆè}Í…™•}‘¥Ø¡É•…±}™˜°Ù…±Õ…Ñ¥½¹}•Ø¤€¨€ÄÀÀ¸À°4(€€€€€€€ô4(€€€€¤4(€€€Ý…É¹¥¹Ì€ômt4(€€€¥˜µ…Ñ ¹¥Í™¥¹¥Ñ”¡•Ø¤…¹•Ø€ðô€Àè4(€€€€€€€µ•ÑÉ¥Íl‰•¹Ñ•ÉÁÉ¥Í•}Ù…±Õ•}ˆ‰t€ôµ…Ñ ¹¹…¸4(€€€€€€€Ý…É¹¥¹Ì¹…ÁÁ•¹ 4(€€€€€€€€€€€€‰¹½¸µÁ½Í¥Ñ¥Ù”•¹Ñ•ÉÁÉ¥Í”Ù…±Õ”É•ÅÕ¥É•Ì„Í•Á…É…Ñ”¹•Ðµ…Í ÍÁ•¥…°µÍ¥ÑÕ…Ñ¥½¸µ½‘•°ˆ4(€€€€€€€€¤4(€€€¡…É€ômt4(€€€¥˜µ¥‘å±”€ðô€Àè4(€€€€€€€¡…É¹…ÁÁ•¹ ‰µ¥µå±”	%Q¥Ì¹½¸µÁ½Í¥Ñ¥Ù”ˆ¤4(€€€¥˜ÕÉÉ•¹Ð€ðô€À…¹¹•Ñ}‘•‰Ð€ø€Àè4(€€€€€€€¡…É¹…ÁÁ•¹ ‰ÕÉÉ•¹Ð	%Q¥Ì¹½¸µÁ½Í¥Ñ¥Ù”Ý¡¥±”¹•Ð‘•‰ÐÉ•µ…¥¹Ìˆ¤4(€€€¥˜ÑÉ½Õ €ðô€À…¹¹•Ñ}‘•‰Ð€ø€Àè4(€€€€€€€¡…É¹…ÁÁ•¹ ‰ÑÉ½Õ 	%Q¥Ì¹½¸µÁ½Í¥Ñ¥Ù”Ý¡¥±”¹•Ð‘•‰ÐÉ•µ…¥¹Ìˆ¤4(€€€¥˜¹½Ð™Õ±±å}…Í¡}½Ù•É•…¹‘•‰Ð€ø€À…¹}™¥¹¥Ñ”¡µ•ÑÉ¥Íl‰ÑÉ½Õ¡}¥¹Ñ•É•ÍÑ}½Ù•É…•}à‰t¤…¹µ•ÑÉ¥Íl‰ÑÉ½Õ¡}¥¹Ñ•É•ÍÑ}½Ù•É…•}à‰t€ð€Ä¸ÈÔè4(€€€€€€€¡…É¹…ÁÁ•¹ ‰ÑÉ½Õ ¥¹Ñ•É•ÍÐ½Ù•É…”¥Ì‰•±½Ü€Ä¸ÈÕàˆ¤4(€€€¥˜}™¥¹¥Ñ”¡µ•ÑÉ¥Íl‰¹•Ñ}‘•‰Ñ}Ñ½}ÑÉ½Õ¡}•‰¥Ñ‘…}à‰t¤…¹µ•ÑÉ¥Íl‰¹•Ñ}‘•‰Ñ}Ñ½}ÑÉ½Õ¡}•‰¥Ñ‘…}à‰t€ø€Ô¸Àè4(€€€€€€€¡…É¹…ÁÁ•¹ ‰¹•Ð‘•‰Ð€¼ÑÉ½Õ 	%Q•á••‘Ì€Õàˆ¤4(€€€½µÁ½¹•¹ÑÌ€ôì4(€€€€€€€€‰µ¥‘å±•}Ù…±Õ…Ñ¥½¸ˆè€¡}¥¹Ù•ÉÍ”¡µ•ÑÉ¥Íl‰•Ù}Ñ½}µ¥‘å±•}•‰¥Ñ‘…}à‰t°€ÄÔ¸À°€Ô¸À¤°€ÌÀ¸À¤°4(€€€€€€€€‰ÑÉ½Õ¡}¥¹Ñ•É•ÍÑ}½Ù•É…”ˆè€ 4(€€€€€€€€€€€€ÄÀÀ¸À4(€€€€€€€€€€€¥˜™Õ±±å}…Í¡}½Ù•É•4(€€€€€€€€€€€•±Í”}‰½Õ¹‘•¡µ•ÑÉ¥Íl‰ÑÉ½Õ¡}¥¹Ñ•É•ÍÑ}½Ù•É…•}à‰t°€Ä¸ÈÔ°€Ô¸À¤°4(€€€€€€€€€€€€ÈÔ¸À°4(€€€€€€€€¤°4(€€€€€€€€‰ÑÉ½Õ¡}±•Ù•É…”ˆè€¡}¥¹Ù•ÉÍ”¡µ•ÑÉ¥Íl‰¹•Ñ}‘•‰Ñ}Ñ½}ÑÉ½Õ¡}•‰¥Ñ‘…}à‰t°€Ô¸À°€À¸À¤°€ÈÀ¸À¤°4(€€€€€€€€‰…Í¡}•¹•É…Ñ¥½¸ˆè€¡}‰½Õ¹‘•¡µ•ÑÉ¥Íl‰É•…±}™™}Ñ½}•Ù}å¥•±‘}ÁÐ‰t°€À¸À°€à¸À¤°€ÄÔ¸À¤°4(€€€€€€€€‰å±•}Á½Í¥Ñ¥½¸ˆè€ 4(€€€€€€€€€€€€À¸À¥˜ÕÉÉ•¹Ð€ðô€À•±Í”}¥¹Ù•ÉÍ”¡µ•ÑÉ¥Íl‰ÕÉÉ•¹Ñ}Ñ½}µ¥‘å±•}à‰t°€Ä¸Ø°€À¸à¤°4(€€€€€€€€€€€€ÄÀ¸À°4(€€€€€€€€¤°4(€€€ô4(€€€É•ÑÕÉ¸}™¥¹¥Í  4(€€€€€€€€‰e1%1}5%e1ˆ°4(€€€€€€€µ•ÑÉ¥Ì°4(€€€€€€€½µÁ½¹•¹ÑÌ°4(€€€€€€€É•ÅÕ¥É•õl4(€€€€€€€€€€€€‰ÕÉÉ•¹Ñ}•‰¥Ñ‘…}ˆˆ°4(€€€€€€€€€€€€‰•¹Ñ•ÉÁÉ¥Í•}Ù…±Õ•}ˆˆ°4(€€€€€€€€€€€€‰‘•‰Ñ}ˆˆ°4(€€€€€€€€€€€€‰…Í¡}ˆˆ°4(€€€€€€€€€€€€¨¡mt¥˜™Õ±±å}…Í¡}½Ù•É••±Í”l‰¥¹Ñ•É•ÍÑ}ÑÑµ}ˆ‰t¤°4(€€€€€€€€€€€€‰‘¹…}ÑÑµ}ˆˆ°4(€€€€€€€€€€€€‰É•…±}™™}ÑÑµ}ˆˆ°4(€€€€€€€t°4(€€€€€€€½ÁÑ¥½¹…°õmt°4(€€€€€€€¡…É‘}™…¥±ÕÉ•Ìõ¡…É°4(€€€€€€€Ý…É¹¥¹ÌõÝ…É¹¥¹Ì°4(€€€€¤4(4(4)‘•˜•Ù…±Õ…Ñ•}™¥¹…¹¥…±}±•¹‘•È¡É…Üè5…ÁÁ¥¹mÍÑÈ°¹åt¤€´ø%¹‘ÕÍÑÉå5½‘•±Ù…±Õ…Ñ¥½¸è4(€€€µ•ÑÉ¥Ì€ô‘¥Ð¡É…Ü¤4(€€€…ÍÍ•ÑÌ€ô}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰…ÍÍ•ÑÍ}ˆˆ¤¤4(€€€•ÅÕ¥Ñä€ô}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰Ñ…¹¥‰±•}•ÅÕ¥Ñå}ˆˆ¤¤4(€€€¹•Ñ}¥¹½µ”€ô}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰¹•Ñ}¥¹½µ•}ÑÑµ}ˆˆ¤¤4(€€€±½…¹Ì€ô}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰±½…¹Í}ˆˆ¤¤4(€€€…±±½Ý…¹”€ô}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰É•‘¥Ñ}±½ÍÍ}…±±½Ý…¹•}ˆˆ¤¤4(€€€µ…É­•Ñ}…À€ô}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰µ…É­•Ñ}…Á}ˆˆ¤¤4(€€€µ•ÑÉ¥Ì¹ÕÁ‘…Ñ” 4(€€€€€€€ì4(€€€€€€€€€€€€‰Ñ…¹¥‰±•}•ÅÕ¥Ñå}Ñ½}…ÍÍ•ÑÍ}ÁÐˆè}Í…™•}‘¥Ø¡•ÅÕ¥Ñä°…ÍÍ•ÑÌ¤€¨€ÄÀÀ¸À°4(€€€€€€€€€€€€‰É½Ñ•}ÁÐˆè}Í…™•}‘¥Ø 4(€€€€€€€€€€€€€€€¹•Ñ}¥¹½µ”°4(€€€€€€€€€€€€€€€}½…±•Í•}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰…Ù•É…•}Ñ…¹¥‰±•}•ÅÕ¥Ñå}ˆˆ¤°•ÅÕ¥Ñä¤°4(€€€€€€€€€€€€¤€¨€ÄÀÀ¸À°4(€€€€€€€€€€€€‰É•‘¥Ñ}±½ÍÍ}…±±½Ý…¹•}Ñ½}±½…¹Í}ÁÐˆè}Í…™•}‘¥Ø¡…±±½Ý…¹”°±½…¹Ì¤€¨€ÄÀÀ¸À°4(€€€€€€€€€€€€‰ÁÉ¥•}Ñ½}Ñ…¹¥‰±•}‰½½­}àˆè}Í…™•}‘¥Ø¡µ…É­•Ñ}…À°•ÅÕ¥Ñä¤°4(€€€€€€€ô4(€€€€¤4(€€€¡…É€ômt4(€€€¥˜}™¥¹¥Ñ”¡µ•ÑÉ¥Íl‰Ñ…¹¥‰±•}•ÅÕ¥Ñå}Ñ½}…ÍÍ•ÑÍ}ÁÐ‰t¤…¹µ•ÑÉ¥Íl‰Ñ…¹¥‰±•}•ÅÕ¥Ñå}Ñ½}…ÍÍ•ÑÍ}ÁÐ‰t€ð€Ô¸Àè4(€€€€€€€¡…É¹…ÁÁ•¹ ‰Ñ…¹¥‰±”•ÅÕ¥Ñä€¼…ÍÍ•ÑÌ¥Ì‰•±½Ü€Ô”ˆ¤4(€€€¥˜µ…Ñ ¹¥Í™¥¹¥Ñ”¡¹•Ñ}¥¹½µ”¤…¹¹•Ñ}¥¹½µ”€ðô€Àè4(€€€€€€€¡…É¹…ÁÁ•¹ ‰QQ4¹•Ð¥¹½µ”¥Ì¹½¸µÁ½Í¥Ñ¥Ù”ˆ¤4(€€€½µÁ½¹•¹ÑÌ€ôì4(€€€€€€€€‰…Á¥Ñ…±}ÍÑÉ•¹Ñ ˆè€¡}‰½Õ¹‘•¡µ•ÑÉ¥Íl‰Ñ…¹¥‰±•}•ÅÕ¥Ñå}Ñ½}…ÍÍ•ÑÍ}ÁÐ‰t°€Ô¸À°€ÄÔ¸À¤°€ÌÀ¸À¤°4(€€€€€€€€‰É½Ñ”ˆè€¡}‰½Õ¹‘•¡µ•ÑÉ¥Íl‰É½Ñ•}ÁÐ‰t°€À¸À°€Äà¸À¤°€ÈÔ¸À¤°4(€€€€€€€€‰…±±½Ý…¹•}½Ù•É…•}ÁÉ½áäˆè€¡}‰½Õ¹‘•¡µ•ÑÉ¥Íl‰É•‘¥Ñ}±½ÍÍ}…±±½Ý…¹•}Ñ½}±½…¹Í}ÁÐ‰t°€À¸Ô°€È¸Ô¤°€ÄÔ¸À¤°4(€€€€€€€€‰¹•Ñ}¥¹Ñ•É•ÍÑ}¥¹½µ•}É½ÝÑ ˆè€¡}‰½Õ¹‘•¡µ•ÑÉ¥Ì¹•Ð ‰¹•Ñ}¥¹Ñ•É•ÍÑ}¥¹½µ•}É½ÝÑ¡}ÁÐˆ¤°€´ÄÀ¸À°€ÄÀ¸À¤°€ÄÔ¸À¤°4(€€€€€€€€‰Ù…±Õ…Ñ¥½¸ˆè€¡}¥¹Ù•ÉÍ”¡µ•ÑÉ¥Íl‰ÁÉ¥•}Ñ½}Ñ…¹¥‰±•}‰½½­}à‰t°€È¸Ô°€À¸Ü¤°€ÄÔ¸À¤°4(€€€ô4(€€€É•ÑÕÉ¸}™¥¹¥Í  4(€€€€€€€€‰%99%1}19Hˆ°4(€€€€€€€µ•ÑÉ¥Ì°4(€€€€€€€½µÁ½¹•¹ÑÌ°4(€€€€€€€É•ÅÕ¥É•õl4(€€€€€€€€€€€€‰…ÍÍ•ÑÍ}ˆˆ°4(€€€€€€€€€€€€‰Ñ…¹¥‰±•}•ÅÕ¥Ñå}ˆˆ°4(€€€€€€€€€€€€‰¹•Ñ}¥¹½µ•}ÑÑµ}ˆˆ°4(€€€€€€€€€€€€‰±½…¹Í}ˆˆ°4(€€€€€€€€€€€€‰É•‘¥Ñ}±½ÍÍ}…±±½Ý…¹•}ˆˆ°4(€€€€€€€t°4(€€€€€€€½ÁÑ¥½¹…°õl‰¹•Ñ}¥¹Ñ•É•ÍÑ}¥¹½µ•}É½ÝÑ¡}ÁÐˆ°€‰µ…É­•Ñ}…Á}ˆ‰t°4(€€€€€€€¡…É‘}™…¥±ÕÉ•Ìõ¡…É°4(€€€€¤4(4(4)‘•˜•Ù…±Õ…Ñ•}™¥¹…¹¥…±}™•”¡É…Üè5…ÁÁ¥¹mÍÑÈ°¹åt¤€´ø%¹‘ÕÍÑÉå5½‘•±Ù…±Õ…Ñ¥½¸è(€€€µ•ÑÉ¥Ì€ô‘¥Ð¡É…Ü¤4(€€€É•Ù•¹Õ”€ô}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰É•Ù•¹Õ•}ÑÑµ}ˆˆ¤¤4(€€€•‰¥Ð€ô}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰•‰¥Ñ}ÑÑµ}ˆˆ¤¤4(€€€½˜€ô}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰½™}ÑÑµ}ˆˆ¤¤4(€€€¹•Ñ}¥¹½µ”€ô}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰¹•Ñ}¥¹½µ•}ÑÑµ}ˆˆ¤¤4(€€€•ÅÕ¥Ñä€ô}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰Ñ…¹¥‰±•}•ÅÕ¥Ñå}ˆˆ¤¤4(€€€…ÍÍ•ÑÌ€ô}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰…ÍÍ•ÑÍ}ˆˆ¤¤4(€€€‘•‰Ð€ô}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰‘•‰Ñ}ˆˆ¤¤4(€€€…Í €ô}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰…Í¡}ˆˆ¤¤4(€€€•‰¥Ñ‘„€ô}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰•‰¥Ñ‘…}ÑÑµ}ˆˆ¤¤4(€€€µ…É­•Ñ}…À€ô}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰µ…É­•Ñ}…Á}ˆˆ¤¤4(€€€µ•ÑÉ¥Ì¹ÕÁ‘…Ñ” 4(€€€€€€€ì4(€€€€€€€€€€€€‰½Á•É…Ñ¥¹}µ…É¥¹}ÁÐˆè}Í…™•}‘¥Ø¡•‰¥Ð°É•Ù•¹Õ”¤€¨€ÄÀÀ¸À°4(€€€€€€€€€€€€‰É½Ñ•}ÁÐˆè}Í…™•}‘¥Ø 4(€€€€€€€€€€€€€€€¹•Ñ}¥¹½µ”°4(€€€€€€€€€€€€€€€}½…±•Í•}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰…Ù•É…•}Ñ…¹¥‰±•}•ÅÕ¥Ñå}ˆˆ¤°•ÅÕ¥Ñä¤°4(€€€€€€€€€€€€¤€¨€ÄÀÀ¸À°4(€€€€€€€€€€€€‰½™}Ñ½}¹•Ñ}¥¹½µ•}àˆè}Í…™•}‘¥Ø¡½˜°¹•Ñ}¥¹½µ”¤°4(€€€€€€€€€€€€‰¹•Ñ}‘•‰Ñ}Ñ½}•‰¥Ñ‘…}àˆè}Í…™•}‘¥Ø¡µ…à¡‘•‰Ð€´…Í °€À¸À¤°•‰¥Ñ‘„¤°4(€€€€€€€€€€€€‰½™}å¥•±‘}ÁÐˆè}Í…™•}‘¥Ø¡½˜°µ…É­•Ñ}…À¤€¨€ÄÀÀ¸À°4(€€€€€€€€€€€€‰Ñ…¹¥‰±•}•ÅÕ¥Ñå}Ñ½}…ÍÍ•ÑÍ}ÁÐˆè}Í…™•}‘¥Ø¡•ÅÕ¥Ñä°…ÍÍ•ÑÌ¤€¨€ÄÀÀ¸À°4(€€€€€€€ô4(€€€€¤4(€€€¡…É€ômt4(€€€¥˜µ…Ñ ¹¥Í™¥¹¥Ñ”¡•ÅÕ¥Ñä¤…¹•ÅÕ¥Ñä€ðô€Àè4(€€€€€€€¡…É¹…ÁÁ•¹ ‰Ñ…¹¥‰±”•ÅÕ¥Ñä¥Ì¹½¸µÁ½Í¥Ñ¥Ù”ˆ¤4(€€€¥˜µ…Ñ ¹¥Í™¥¹¥Ñ”¡¹•Ñ}¥¹½µ”¤…¹¹•Ñ}¥¹½µ”€ðô€Àè4(€€€€€€€¡…É¹…ÁÁ•¹ ‰QQ4¹•Ð¥¹½µ”¥Ì¹½¸µÁ½Í¥Ñ¥Ù”ˆ¤4(€€€¥˜µ…Ñ ¹¥Í™¥¹¥Ñ”¡½˜¤…¹½˜€ðô€Àè4(€€€€€€€¡…É¹…ÁÁ•¹ ‰QQ4½Á•É…Ñ¥¹œ…Í ™±½Ü¥Ì¹½¸µÁ½Í¥Ñ¥Ù”ˆ¤4(€€€½µÁ½¹•¹ÑÌ€ôì4(€€€€€€€€‰½Á•É…Ñ¥¹}µ…É¥¸ˆè€¡}‰½Õ¹‘•¡µ•ÑÉ¥Íl‰½Á•É…Ñ¥¹}µ…É¥¹}ÁÐ‰t°€Ô¸À°€ÌÔ¸À¤°€ÈÔ¸À¤°4(€€€€€€€€‰É½Ñ”ˆè€¡}‰½Õ¹‘•¡µ•ÑÉ¥Íl‰É½Ñ•}ÁÐ‰t°€À¸À°€ÈÔ¸À¤°€ÈÀ¸À¤°4(€€€€€€€€‰…Í¡}½¹Ù•ÉÍ¥½¸ˆè€¡}‰½Õ¹‘•¡µ•ÑÉ¥Íl‰½™}Ñ½}¹•Ñ}¥¹½µ•}à‰t°€À¸Ô°€Ä¸Ì¤°€ÈÀ¸À¤°4(€€€€€€€€‰É•Ù•¹Õ•}É½ÝÑ ˆè€¡}‰½Õ¹‘•¡µ•ÑÉ¥Ì¹•Ð ‰É•Ù•¹Õ•}É½ÝÑ¡}ÁÐˆ¤°€´Ô¸À°€ÄÔ¸À¤°€ÄÔ¸À¤°4(€€€€€€€€‰‰…±…¹•}Í¡••Ðˆè€¡}¥¹Ù•ÉÍ”¡µ•ÑÉ¥Íl‰¹•Ñ}‘•‰Ñ}Ñ½}•‰¥Ñ‘…}à‰t°€Ô¸À°€À¸À¤°€ÄÀ¸À¤°4(€€€€€€€€‰…Í¡}å¥•±ˆè€¡}‰½Õ¹‘•¡µ•ÑÉ¥Íl‰½™}å¥•±‘}ÁÐ‰t°€È¸À°€ÄÀ¸À¤°€ÄÀ¸À¤°4(€€€ô4(€€€É•ÑÕÉ¸}™¥¹¥Í  (€€€€€€€€‰%99%1}ˆ°4(€€€€€€€µ•ÑÉ¥Ì°4(€€€€€€€½µÁ½¹•¹ÑÌ°4(€€€€€€€É•ÅÕ¥É•õl4(€€€€€€€€€€€€‰É•Ù•¹Õ•}ÑÑµ}ˆˆ°4(€€€€€€€€€€€€‰•‰¥Ñ}ÑÑµ}ˆˆ°4(€€€€€€€€€€€€‰½™}ÑÑµ}ˆˆ°4(€€€€€€€€€€€€‰¹•Ñ}¥¹½µ•}ÑÑµ}ˆˆ°4(€€€€€€€€€€€€‰Ñ…¹¥‰±•}•ÅÕ¥Ñå}ˆˆ°4(€€€€€€€€€€€€‰…ÍÍ•ÑÍ}ˆˆ°4(€€€€€€€€€€€€‰‘•‰Ñ}ˆˆ°4(€€€€€€€€€€€€‰…Í¡}ˆˆ°4(€€€€€€€€€€€€‰•‰¥Ñ‘…}ÑÑµ}ˆˆ°4(€€€€€€€€€€€€‰µ…É­•Ñ}…Á}ˆˆ°4(€€€€€€€t°4(€€€€€€€½ÁÑ¥½¹…°õl‰É•Ù•¹Õ•}É½ÝÑ¡}ÁÐ‰t°4(€€€€€€€¡…É‘}™…¥±ÕÉ•Ìõ¡…É°(€€€€¤(()‘•˜}•Ù…±Õ…Ñ•}…ÍÍ•Ñ}µ…¹…•È (€€€É…Üè5…ÁÁ¥¹mÍÑÈ°¹åt°µ½‘•±}­•äèÍÑÈ(¤€´ø%¹‘ÕÍÑÉå5½‘•±Ù…±Õ…Ñ¥½¸è(€€€µ•ÑÉ¥Ì€ô‘¥Ð¡É…Ü¤(€€€É•Ù•¹Õ”€ô}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰É•Ù•¹Õ•}ÑÑµ}ˆˆ¤¤(€€€½˜€ô}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰½™}ÑÑµ}ˆˆ¤¤(€€€¹•Ñ}¥¹½µ”€ô}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰¹•Ñ}¥¹½µ•}ÑÑµ}ˆˆ¤¤(€€€‘•‰Ð€ô}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰‘•‰Ñ}ˆˆ¤¤(€€€…Í €ô}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰…Í¡}ˆˆ¤¤(€€€™••}É•±…Ñ•‘}•…É¹¥¹Ì€ô}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰™••}É•±…Ñ•‘}•…É¹¥¹Í}ˆˆ¤¤(€€€•‰¥Ñ‘„€ô}¹Õµ‰•È¡µ•ÑÉ¥Ì¹•Ð ‰•‰¥Ñ‘…}ÑÑµ}ˆˆ¤¤(€€€µ•ÑÉ¥Ì¹ÕÁ‘…Ñ” (€€€€€€€ì(€€€€€€€€€€€€‰½™}Ñ½}¹•Ñ}¥¹½µ•}àˆè}Í…™•}‘¥Ø¡½˜°¹•Ñ}¥¹½µ”¤°(€€€€€€€€€€€€‰½µÁ•¹Í…Ñ¥½¹}Ñ½}É•Ù•¹Õ•}ÁÐˆè}Í…™•}‘¥Ø (€€€€€€€€€€€€€€€µ•ÑÉ¥Ì¹•Ð ‰½µÁ•¹Í…Ñ¥½¹}•áÁ•¹Í•}ˆˆ¤°É•Ù•¹Õ”(€€€€€€€€€€€€¤(€€€€€€€€€€€€¨€ÄÀÀ¸À°(€€€€€€€€€€€€‰¹•Ñ}‘•‰Ñ}Ñ½}™É•}½É}•‰¥Ñ‘…}àˆè}Í…™•}‘¥Ø (€€€€€€€€€€€€€€€µ…à¡‘•‰Ð€´…Í °€À¸À¤°(€€€€€€€€€€€€€€€™••}É•±…Ñ•‘}•…É¹¥¹Ì(€€€€€€€€€€€€€€€¥˜µ…Ñ ¹¥Í™¥¹¥Ñ”¡™••}É•±…Ñ•‘}•…É¹¥¹Ì¤…¹™••}É•±…Ñ•‘}•…É¹¥¹Ì€ø€À(€€€€€€€€€€€€€€€•±Í”•‰¥Ñ‘„°(€€€€€€€€€€€€¤°(€€€€€€€ô(€€€€¤(€€€É•ÅÕ¥É•‘}‰å}µ½‘•°€ôì(€€€€€€€€‰1QI9Q%Y}MMQ}59Hˆèl(€€€€€€€€€€€€‰™••}É•±…Ñ•‘}•…É¹¥¹Í}ˆˆ°(€€€€€€€€€€€€‰µ…¹…•µ•¹Ñ}™••}É•Ù•¹Õ•}ˆˆ°(€€€€€€€€€€€€‰™••}Á…å¥¹}…Õµ}ˆˆ°(€€€€€€€€€€€€‰…Õµ}É½ÝÑ¡}ÁÐˆ°(€€€€€€€€€€€€‰Á•Éµ…¹•¹Ñ}…Á¥Ñ…±}ÁÐˆ°(€€€€€€€€€€€€‰½µÁ•¹Í…Ñ¥½¹}•áÁ•¹Í•}ˆˆ°(€€€€€€€t°(€€€€€€€€‰QI%Q%=91}MMQ}59Hˆèl(€€€€€€€€€€€€‰µ…¹…•µ•¹Ñ}™••}É•Ù•¹Õ•}ˆˆ°(€€€€€€€€€€€€‰…Õµ}ˆˆ°(€€€€€€€€€€€€‰…Õµ}É½ÝÑ¡}ÁÐˆ°(€€€€€€€€€€€€‰½É…¹¥}¹•Ñ}™±½ÝÍ}ÁÐˆ°(€€€€€€€€€€€€‰½µÁ•¹Í…Ñ¥½¹}•áÁ•¹Í•}ˆˆ°(€€€€€€€t°(€€€€€€€€‰%9MUI9}1%9-}MMQ}59Hˆèl(€€€€€€€€€€€€‰™••}É•±…Ñ•‘}•…É¹¥¹Í}ˆˆ°(€€€€€€€€€€€€‰¥¹ÍÕÉ…¹•}…ÍÍ•ÑÍ}ÁÐˆ°(€€€€€€€€€€€€‰Á•Éµ…¹•¹Ñ}…Á¥Ñ…±}ÁÐˆ°(€€€€€€€€€€€€‰™••}Á…å¥¹}…Õµ}ˆˆ°(€€€€€€€€€€€€‰½µÁ•¹Í…Ñ¥½¹}•áÁ•¹Í•}ˆˆ°(€€€€€€€t°(€€€€€€€€‰=Q!I}}%99%0ˆèl(€€€€€€€€€€€€‰µ…¹…•µ•¹Ñ}™••}É•Ù•¹Õ•}ˆˆ°(€€€€€€€€€€€€‰…Õµ}ˆˆ°(€€€€€€€€€€€€‰½µÁ•¹Í…Ñ¥½¹}•áÁ•¹Í•}ˆˆ°(€€€€€€€t°(€€€ô(€€€É•ÅÕ¥É•€ôl(€€€€€€€€‰É•Ù•¹Õ•}ÑÑµ}ˆˆ°(€€€€€€€€‰½™}ÑÑµ}ˆˆ°(€€€€€€€€‰¹•Ñ}¥¹½µ•}ÑÑµ}ˆˆ°(€€€€€€€€‰‘•‰Ñ}ˆˆ°(€€€€€€€€‰…Í¡}ˆˆ°(€€€€€€€€©É•ÅÕ¥É•‘}‰å}µ½‘•±mµ½‘•±}­•åt°(€€€t(€€€¡…É€ômt(€€€¥˜µ…Ñ ¹¥Í™¥¹¥Ñ”¡¹•Ñ}¥¹½µ”¤…¹¹•Ñ}¥¹½µ”€ðô€Àè(€€€€€€€¡…É¹…ÁÁ•¹ ‰QQ4¹•Ð¥¹½µ”¥Ì¹½¸µÁ½Í¥Ñ¥Ù”ˆ¤(€€€¥˜µ…Ñ ¹¥Í™¥¹¥Ñ”¡½˜¤…¹½˜€ðô€Àè(€€€€€€€¡…É¹…ÁÁ•¹ ‰QQ4½Á•É…Ñ¥¹œ…Í ™±½Ü¥Ì¹½¸µÁ½Í¥Ñ¥Ù”ˆ¤(€€€½µÁ½¹•¹ÑÌ€ôì(€€€€€€€€‰…Õµ}É½ÝÑ ˆè€¡}‰½Õ¹‘•¡µ•ÑÉ¥Ì¹•Ð ‰…Õµ}É½ÝÑ¡}ÁÐˆ¤°€´Ô¸À°€ÄÔ¸À¤°€ÈÀ¸À¤°(€€€€€€€€‰½É…¹¥}¹•Ñ}™±½ÝÌˆè€¡}‰½Õ¹‘•¡µ•ÑÉ¥Ì¹•Ð ‰½É…¹¥}¹•Ñ}™±½ÝÍ}ÁÐˆ¤°€´Ô¸À°€ÄÀ¸À¤°€ÄÔ¸À¤°(€€€€€€€€‰Á•Éµ…¹•¹Ñ}…Á¥Ñ…°ˆè€¡}‰½Õ¹‘•¡µ•ÑÉ¥Ì¹•Ð ‰Á•Éµ…¹•¹Ñ}…Á¥Ñ…±}ÁÐˆ¤°€À¸À°€ÜÔ¸À¤°€ÄÔ¸À¤°(€€€€€€€€‰…Í¡}½¹Ù•ÉÍ¥½¸ˆè€¡}‰½Õ¹‘•¡µ•ÑÉ¥Íl‰½™}Ñ½}¹•Ñ}¥¹½µ•}à‰t°€À¸Ô°€Ä¸Ì¤°€ÈÀ¸À¤°(€€€€€€€€‰½µÁ•¹Í…Ñ¥½¹}‘¥Í¥Á±¥¹”ˆè€¡}¥¹Ù•ÉÍ”¡µ•ÑÉ¥Íl‰½µÁ•¹Í…Ñ¥½¹}Ñ½}É•Ù•¹Õ•}ÁÐ‰t°€ØÀ¸À°€ÈÀ¸À¤°€ÄÔ¸À¤°(€€€€€€€€‰‰…±…¹•}Í¡••Ðˆè€¡}¥¹Ù•ÉÍ”¡µ•ÑÉ¥Íl‰¹•Ñ}‘•‰Ñ}Ñ½}™É•}½É}•‰¥Ñ‘…}à‰t°€Ô¸À°€À¸À¤°€ÄÔ¸À¤°(€€€ô(€€€•Ù…±Õ…Ñ¥½¸€ô}™¥¹¥Í  (€€€€€€€µ½‘•±}­•ä°(€€€€€€€µ•ÑÉ¥Ì°(€€€€€€€½µÁ½¹•¹ÑÌ°(€€€€€€€É•ÅÕ¥É•õÉ•ÅÕ¥É•°(€€€€€€€½ÁÑ¥½¹…°õl(€€€€€€€€€€€€‰Á•É™½Éµ…¹•}™••Í}ˆˆ°(€€€€€€€€€€€€‰É•…±¥é•‘}…ÉÉå}ˆˆ°(€€€€€€€€€€€€‰¹•Ñ}™±½ÝÍ}ˆˆ°(€€€€€€€€€€€€‰¥¹ÍÕÉ…¹•}…ÍÍ•ÑÍ}ÁÐˆ°(€€€€€€€t°(€€€€€€€¡…É‘}™…¥±ÕÉ•Ìõ¡…É°(€€€€€€€Ý…É¹¥¹Ìõl(€€€€€€€€€€€€‰½µÁ…¹äµ‘•™¥¹•U4½I½™±½Ü-A%ÌµÕÍÐ‰”Á½¥¹Ðµ¥¸µÑ¥µ”…¹µ…ä¹½Ð‰”¥¹™•ÉÉ•™É½´@É•Ù•¹Õ”ˆ(€€€€€€€t°(€€€€¤(€€€É•ÑÕÉ¸•Ù…±Õ…Ñ¥½¸(()‘•˜•Ù…±Õ…Ñ•}…±Ñ•É¹…Ñ¥Ù•}…ÍÍ•Ñ}µ…¹…•È¡É…Üè5…ÁÁ¥¹mÍÑÈ°¹åt¤€´ø%¹‘ÕÍÑÉå5½‘•±Ù…±Õ…Ñ¥½¸è(€€€É•ÑÕÉ¸}•Ù…±Õ…Ñ•}…ÍÍ•Ñ}µ…¹…•È¡É…Ü°€‰1QI9Q%Y}MMQ}59Hˆ¤(()‘•˜•Ù…±Õ…Ñ•}ÑÉ…‘¥Ñ¥½¹…±}…ÍÍ•Ñ}µ…¹…•È¡É…Üè5…ÁÁ¥¹mÍÑÈ°¹åt¤€´ø%¹‘ÕÍÑÉå5½‘•±Ù…±Õ…Ñ¥½¸è(€€€É•ÑÕÉ¸}•Ù…±Õ…Ñ•}…ÍÍ•Ñ}µ…¹…•È¡É…Ü°€‰QI%Q%=91}MMQ}59Hˆ¤(()‘•˜•Ù…±Õ…Ñ•}¥¹ÍÕÉ…¹•}±¥¹­•‘}…ÍÍ•Ñ}µ…¹…•È¡É…Üè5…ÁÁ¥¹mÍÑÈ°¹åt¤€´ø%¹‘ÕÍÑÉå5½‘•±Ù…±Õ…Ñ¥½¸è(€€€É•ÑÕÉ¸}•Ù…±Õ…Ñ•}…ÍÍ•Ñ}µ…¹…•È¡É…Ü°€‰%9MUI9}1%9-}MMQ}59Hˆ¤(()‘•˜•Ù…±Õ…Ñ•}½Ñ¡•É}™••}™¥¹…¹¥…°¡É…Üè5…ÁÁ¥¹mÍÑÈ°¹åt¤€´ø%¹‘ÕÍÑÉå5½‘•±Ù…±Õ…Ñ¥½¸è(€€€É•ÑÕÉ¸}•Ù…±Õ…Ñ•}…ÍÍ•Ñ}µ…¹…•È¡É…Ü°€‰=Q!I}}%99%0ˆ¤(4(4)5=1}Y1UQ=IL€ôì4(€€€€‰	9,ˆè•Ù…±Õ…Ñ•}‰…¹¬°4(€€€€‰%9MUI9}A}9}ˆè•Ù…±Õ…Ñ•}¥¹ÍÕÉ…¹•}Á}…¹‘}Œ°4(€€€€‰%9MUI9}1%ˆè•Ù…±Õ…Ñ•}¥¹ÍÕÉ…¹•}±¥™”°4(€€€€‰I%Q}EU%Qdˆè•Ù…±Õ…Ñ•}É•¥Ñ}•ÅÕ¥Ñä°4(€€€€‰I%Q}5=IQˆè•Ù…±Õ…Ñ•}É•¥Ñ}µ½ÉÑ…”°4(€€€€‰IU1Q}UQ%1%Qdˆè•Ù…±Õ…Ñ•}É•Õ±…Ñ•‘}ÕÑ¥±¥Ñä°4(€€€€‰e1%1}5%e1ˆè•Ù…±Õ…Ñ•}å±¥…±}µ¥‘å±”°4(€€€€‰%99%1}19Hˆè•Ù…±Õ…Ñ•}™¥¹…¹¥…±}±•¹‘•È°4(€€€€‰%99%1}ˆè•Ù…±Õ…Ñ•}™¥¹…¹¥…±}™•”°(€€€€‰1QI9Q%Y}MMQ}59Hˆè•Ù…±Õ…Ñ•}…±Ñ•É¹…Ñ¥Ù•}…ÍÍ•Ñ}µ…¹…•È°(€€€€‰QI%Q%=91}MMQ}59Hˆè•Ù…±Õ…Ñ•}ÑÉ…‘¥Ñ¥½¹…±}…ÍÍ•Ñ}µ…¹…•È°(€€€€‰%9MUI9}1%9-}MMQ}59Hˆè•Ù…±Õ…Ñ•}¥¹ÍÕÉ…¹•}±¥¹­•‘}…ÍÍ•Ñ}µ…¹…•È°(€€€€‰=Q!I}}%99%0ˆè•Ù…±Õ…Ñ•}½Ñ¡•É}™••}™¥¹…¹¥…°°)ô(4(4)‘•˜•Ù…±Õ…Ñ•}¥¹‘ÕÍÑÉå}µ½‘•°¡µ½‘•±}­•äèÍÑÈ°µ•ÑÉ¥Ìè5…ÁÁ¥¹mÍÑÈ°¹åt¤€´ø%¹‘ÕÍÑÉå5½‘•±Ù…±Õ…Ñ¥½¸è4(€€€•Ù…±Õ…Ñ½È€ô5=1}Y1UQ=IL¹•Ð¡ÍÑÈ¡µ½‘•±}­•ä½È€ˆˆ¤¹ÕÁÁ•È ¤¤4(€€€¥˜•Ù…±Õ…Ñ½È¥Ì9½¹”è4(€€€€€€€É•ÑÕÉ¸%¹‘ÕÍÑÉå5½‘•±Ù…±Õ…Ñ¥½¸ 4(€€€€€€€€€€€µ½‘•±}­•äõÍÑÈ¡µ½‘•±}­•ä½È€‰U9-9=]8ˆ¤°4(€€€€€€€€€€€‘•¥Í¥½¸ô‰	MQ%8ˆ°4(€€€€€€€€€€€Í½É”õµ…Ñ ¹¹…¸°4(€€€€€€€€€€€µ•ÑÉ¥Ìõ‘¥Ð¡µ•ÑÉ¥Ì¤°4(€€€€€€€€€€€É•ÅÕ¥É•‘}µ¥ÍÍ¥¹œõl‰¥µÁ±•µ•¹Ñ•µ½‘•°É½ÕÑ”‰t°4(€€€€€€€€€€€Ý…É¹¥¹Ìõl‰9¼ÍÁ•¥…±¥é••Ù…±Õ…Ñ½È¥ÌÉ•¥ÍÑ•É•™½ÈÑ¡¥Ìµ½‘•°­•ä‰t°4(€€€€€€€€¤4(€€€É•ÑÕÉ¸•Ù…±Õ…Ñ½È¡µ•ÑÉ¥Ì¤4(4(4)‘•˜…ÍÍ•ÍÍ}ÍÁ•¥…±¥é•‘}‘…Ñ…}½¹™¥‘•¹” 4(€€€•Ù¥‘•¹•}ÍÑ…ÑÌè5…ÁÁ¥¹mÍÑÈ°¹åt°4(€€€•Ù…±Õ…Ñ¥½¸è%¹‘ÕÍÑÉå5½‘•±Ù…±Õ…Ñ¥½¸°4(€€€•áÑÉ…}½ÁÑ¥½¹…±}µ¥ÍÍ¥¹œè%Ñ•É…‰±•mÍÑÉt€ô€ ¤°4(¤€´ø‘¥Ðè4(€€€Í½É”€ô€ÄÀÀ¸À4(€€€É•…Í½¹Ìè1¥ÍÑmÍÑÉt€ômt4(€€€Í•±•Ñ•‘}½Õ¹Ð€ô¥¹Ð¡•Ù¥‘•¹•}ÍÑ…ÑÌ¹•Ð ‰Í•±•Ñ•‘}Í½ÕÉ•}½Õ¹Ðˆ¤½È€À¤4(€€€…•ÁÑ•‘}É…Ñ¥¼€ô}¹Õµ‰•È¡•Ù¥‘•¹•}ÍÑ…ÑÌ¹•Ð ‰…•ÁÑ•‘}…Ñ}É…Ñ¥¼ˆ¤¤4(€€€™…±±‰…­}Ñ…}É…Ñ¥¼€ô}¹Õµ‰•È¡•Ù¥‘•¹•}ÍÑ…ÑÌ¹•Ð ‰™…±±‰…­}Ñ…}É…Ñ¥¼ˆ¤¤4(€€€Á•É¥½‘}…¹½µ…±¥•Ì€ô¥¹Ð¡•Ù¥‘•¹•}ÍÑ…ÑÌ¹•Ð ‰Á•É¥½‘}…¹½µ…±å}½Õ¹Ðˆ¤½È€À¤4(€€€¥˜Í•±•Ñ•‘}½Õ¹Ð€ðô€Àè4(€€€€€€€Í½É”€´ô€ÐÔ¸À4(€€€€€€€É•…Í½¹Ì¹…ÁÁ•¹ ‰9¼Í•±•Ñ•MÍ½ÕÉ”™…ÑÌˆ¤4(€€€•±¥˜¹½Ðµ…Ñ ¹¥Í™¥¹¥Ñ”¡…•ÁÑ•‘}É…Ñ¥¼¤½È…•ÁÑ•‘}É…Ñ¥¼€ð€À¸ÔÀè4(€€€€€€€Í½É”€´ô€ÌÔ¸À4(€€€€€€€É•…Í½¹Ì¹…ÁÁ•¹ ‰1•ÍÌÑ¡…¸¡…±˜½˜Í•±•Ñ•M™…ÑÌ¡…Ù”H…•ÁÑ…¹”Ñ¥µ•ÍÑ…µÁÌˆ¤4(€€€•±¥˜…•ÁÑ•‘}É…Ñ¥¼€ð€À¸àÀè4(€€€€€€€Í½É”€´ô€ÄÔ¸À4(€€€€€€€É•…Í½¹Ì¹…ÁÁ•¹ ‰M½µ”Í•±•Ñ•M™…ÑÌÕÍ”™¥±¥¹œµ‘…Ñ”…Ù…¥±…‰¥±¥Ñä™…±±‰…¬ˆ¤4(€€€¥˜Á•É¥½‘}…¹½µ…±¥•Ìè4(€€€€€€€Í½É”€´ôµ¥¸ ÌÀ¸À°Á•É¥½‘}…¹½µ…±¥•Ì€¨€ÄÀ¸À¤4(€€€€€€€É•…Í½¹Ì¹…ÁÁ•¹¡˜‰íÁ•É¥½‘}…¹½µ…±¥•ÍôÍ•±•Ñ•™…ÑÌ¡…Ù”…‰¹½Éµ…°Á•É¥½‘Ìˆ¤4(€€€¥˜µ…Ñ ¹¥Í™¥¹¥Ñ”¡™…±±‰…­}Ñ…}É…Ñ¥¼¤…¹™…±±‰…­}Ñ…}É…Ñ¥¼€øô€À¸ÜÔè4(€€€€€€€Í½É”€´ô€ÄÀ¸À4(€€€€€€€É•…Í½¹Ì¹…ÁÁ•¹ ‰5½ÍÐÍ•±•Ñ•™…ÑÌÕÍ”…±Ñ•É¹…Ñ”a	I0Ñ…œµ…ÁÁ¥¹Ìˆ¤4(€€€•±¥˜µ…Ñ ¹¥Í™¥¹¥Ñ”¡™…±±‰…­}Ñ…}É…Ñ¥¼¤…¹™…±±‰…­}Ñ…}É…Ñ¥¼€øô€À¸ÐÀè4(€€€€€€€Í½É”€´ô€Ô¸À4(€€€€€€€É•…Í½¹Ì¹…ÁÁ•¹ ‰µ…Ñ•É¥…°Í¡…É”½˜™…ÑÌÕÍ•Ì…±Ñ•É¹…Ñ”a	I0Ñ…œµ…ÁÁ¥¹Ìˆ¤4(€€€µ¥ÍÍ¥¹}½ÁÑ¥½¹…°€ôÍ½ÉÑ•¡Í•Ð¡•Ù…±Õ…Ñ¥½¸¹½ÁÑ¥½¹…±}µ¥ÍÍ¥¹œ¤ðÍ•Ð¡•áÑÉ…}½ÁÑ¥½¹…±}µ¥ÍÍ¥¹œ¤¤4(€€€¥˜µ¥ÍÍ¥¹}½ÁÑ¥½¹…°è4(€€€€€€€Á•¹…±Ñä€ôµ¥¸ ÈÔ¸À°±•¸¡µ¥ÍÍ¥¹}½ÁÑ¥½¹…°¤€¨€Ð¸À¤4(€€€€€€€Í½É”€´ôÁ•¹…±Ñä4(€€€€€€€É•…Í½¹Ì¹…ÁÁ•¹ ‰=ÁÑ¥½¹…°µ½‘•°•Ù¥‘•¹”µ¥ÍÍ¥¹œè€ˆ€¬€ˆ°€ˆ¹©½¥¸¡µ¥ÍÍ¥¹}½ÁÑ¥½¹…°¤¤4(€€€¥˜•Ù…±Õ…Ñ¥½¸¹É•ÅÕ¥É•‘}µ¥ÍÍ¥¹œè4(€€€€€€€Í½É”€ôµ¥¸¡Í½É”°€ÐÔ¸À¤4(€€€€€€€É•…Í½¹Ì¹…ÁÁ•¹ ‰I•ÅÕ¥É•µ½‘•°•Ù¥‘•¹”µ¥ÍÍ¥¹œè€ˆ€¬€ˆ°€ˆ¹©½¥¸¡•Ù…±Õ…Ñ¥½¸¹É•ÅÕ¥É•‘}µ¥ÍÍ¥¹œ¤¤4(€€€¥˜•Ù…±Õ…Ñ¥½¸¹Í½É•}½Ù•É…”…¹•Ù…±Õ…Ñ¥½¸¹Í½É•}½Ù•É…”€ð€À¸àÀè4(€€€€€€€Í½É”€´ôµ¥¸ ÄÔ¸À°€ À¸àÀ€´•Ù…±Õ…Ñ¥½¸¹Í½É•}½Ù•É…”¤€¨€ÔÀ¸À¤4(€€€€€€€É•…Í½¹Ì¹…ÁÁ•¹¡˜‰MÁ•¥…±¥é•Í½É”½Ù•É…”¥Ìí•Ù…±Õ…Ñ¥½¸¹Í½É•}½Ù•É…”è¸À•ôˆ¤4(€€€Í½É”€ôÉ½Õ¹¡µ…à À¸À°µ¥¸ ÄÀÀ¸À°Í½É”¤¤°€È¤4(€€€É•ÑÕÉ¸ì4(€€€€€€€€‰Í½É”ˆèÍ½É”°4(€€€€€€€€‰…‰ÍÑ…¥¸ˆèÍ½É”€ð€ÜÀ¸À½È•Ù…±Õ…Ñ¥½¸¹‘•¥Í¥½¸€ôô€‰	MQ%8ˆ°4(€€€€€€€€‰É•…Í½¹ÌˆèÉ•…Í½¹Ì½Èl‰MÁ•¥…±¥é•µ½‘•°•Ù¥‘•¹”½Ù•É…”¥Ì½µÁ±•Ñ”‰t°4(€€€ô4