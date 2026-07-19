import math
import unittest

from mode_c_industry_models import (
    SPECIALIZED_MODEL_KEYS,
    assess_specialized_data_confidence,
    calculate_p_and_c_stress,
    evaluate_industry_model,
    initial_screen_industry,
)
from mode_c_routing import route_industry_model


class IndustryModelTests(unittest.TestCase):
    @staticmethod
    def healthy_samples():
        return {
            "BANK": {
                "assets_b": 100,
                "tangible_equity_b": 8,
                "average_tangible_equity_b": 7.5,
                "net_income_ttm_b": 1.2,
                "deposits_b": 70,
                "loans_b": 60,
                "credit_loss_allowance_b": 1,
                "tier1_ratio_pct": 13,
                "tier1_well_capitalized_min_pct": 8,
                "net_interest_income_growth_pct": 5,
                "market_cap_b": 12,
                "aoci_b": -1,
            },
            "INSURANCE_P_AND_C": {
                "premiums_earned_ttm_b": 10,
                "claims_incurred_ttm_b": 6.2,
                "underwriting_expense_ttm_b": 3.1,
                "combined_expense_ttm_b": 9.3,
                "assets_b": 40,
                "equity_b": 8,
                "average_equity_b": 7.8,
                "net_income_ttm_b": 1.1,
                "premium_growth_pct": 8,
                "reserve_development_to_premium_pct": -1,
                "market_cap_b": 14,
            },
            "INSURANCE_LIFE": {
                "premiums_earned_ttm_b": 10,
                "net_investment_income_ttm_b": 4,
                "policyholder_benefits_ttm_b": 9,
                "assets_b": 100,
                "equity_b": 8,
                "average_equity_b": 8,
                "net_income_ttm_b": 1,
                "premium_growth_pct": 5,
                "market_cap_b": 10,
            },
            "REIT_EQUITY": {
                "net_income_ttm_b": 1,
                "real_estate_dna_ttm_b": 1.5,
                "gain_on_property_sale_ttm_b": 0.1,
                "real_estate_impairment_ttm_b": 0,
                "maintenance_capex_b": 0.4,
                "interest_ttm_b": 0.5,
                "debt_b": 8,
                "cash_b": 1,
                "market_cap_b": 20,
                "dividends_ttm_b": 1.5,
                "tax_ttm_b": 0,
                "lease_revenue_growth_pct": 5,
            },
            "REIT_MORTGAGE": {
                "assets_b": 100,
                "equity_b": 10,
                "average_equity_b": 10,
                "net_income_ttm_b": 1.2,
                "recurring_earnings_ttm_b": 1.25,
                "market_cap_b": 9,
                "dividends_ttm_b": 1,
                "net_interest_income_growth_pct": 3,
                "net_interest_income_ttm_b": 1.1,
            },
            "REGULATED_UTILITY": {
                "ebit_ttm_b": 4,
                "interest_ttm_b": 1,
                "debt_b": 20,
                "equity_b": 20,
                "ocf_ttm_b": 6,
                "capex_ttm_b": 6,
                "net_income_ttm_b": 2.5,
                "average_equity_b": 20,
                "dividends_ttm_b": 2,
                "ppe_growth_pct": 5,
            },
            "CYCLICAL_MIDCYCLE": {
                "ebitda_history_b": [5, 7, 10, 8, 6, 9, 4],
                "current_ebitda_b": 8,
                "enterprise_value_b": 50,
                "debt_b": 12,
                "cash_b": 2,
                "interest_ttm_b": 1,
                "dna_ttm_b": 2,
                "real_fcf_ttm_b": 4,
            },
            "FINANCIAL_LENDER": {
                "assets_b": 100,
                "tangible_equity_b": 10,
                "average_tangible_equity_b": 9.5,
                "net_income_ttm_b": 1.5,
                "loans_b": 70,
                "credit_loss_allowance_b": 1.4,
                "net_interest_income_growth_pct": 5,
                "market_cap_b": 12,
            },
            "FINANCIAL_FEE": {
                "revenue_ttm_b": 10,
                "ebit_ttm_b": 3,
                "ocf_ttm_b": 2.5,
                "net_income_ttm_b": 2,
                "tangible_equity_b": 8,
                "average_tangible_equity_b": 8,
                "assets_b": 20,
                "debt_b": 3,
                "cash_b": 2,
                "ebitda_ttm_b": 3.5,
                "market_cap_b": 20,
                "revenue_growth_pct": 8,
            },
            "ALTERNATIVE_ASSET_MANAGER": {
                "revenue_ttm_b": 10,
                "ocf_ttm_b": 3,
                "net_income_ttm_b": 2,
                "debt_b": 3,
                "cash_b": 2,
                "ebitda_ttm_b": 3.5,
                "fee_related_earnings_b": 3,
                "management_fee_revenue_b": 7,
                "fee_paying_aum_b": 500,
                "aum_growth_pct": 12,
                "organic_net_flows_pct": 7,
                "permanent_capital_pct": 55,
                "compensation_expense_b": 3,
            },
            "TRADITIONAL_ASSET_MANAGER": {
                "revenue_ttm_b": 10,
                "ocf_ttm_b": 3,
                "net_income_ttm_b": 2,
                "debt_b": 3,
                "cash_b": 2,
                "ebitda_ttm_b": 3.5,
                "management_fee_revenue_b": 8,
                "aum_b": 700,
                "aum_growth_pct": 8,
                "organic_net_flows_pct": 4,
                "permanent_capital_pct": 20,
                "compensation_expense_b": 3,
            },
            "INSURANCE_LINKED_ASSET_MANAGER": {
                "revenue_ttm_b": 10,
                "ocf_ttm_b": 3,
                "net_income_ttm_b": 2,
                "debt_b": 3,
                "cash_b": 2,
                "ebitda_ttm_b": 3.5,
                "fee_related_earnings_b": 3,
                "insurance_assets_pct": 65,
                "permanent_capital_pct": 70,
                "fee_paying_aum_b": 500,
                "aum_growth_pct": 8,
                "organic_net_flows_pct": 4,
                "compensation_expense_b": 3,
            },
            "OTHER_FEE_FINANCIAL": {
                "revenue_ttm_b": 10,
                "ocf_ttm_b": 3,
                "net_income_ttm_b": 2,
                "debt_b": 3,
                "cash_b": 2,
                "ebitda_ttm_b": 3.5,
                "management_fee_revenue_b": 8,
                "aum_b": 700,
                "aum_growth_pct": 8,
                "organic_net_flows_pct": 4,
                "permanent_capital_pct": 20,
                "compensation_expense_b": 3,
            },
        }

    def test_every_registered_specialized_model_can_pass_a_healthy_case(self):
        samples = self.healthy_samples()
        self.assertEqual(set(samples), SPECIALIZED_MODEL_KEYS)
        for model_key, metrics in samples.items():
            with self.subTest(model_key=model_key):
                result = evaluate_industry_model(model_key, metrics)
                self.assertEqual(result.decision, "PASS")
                self.assertGreaterEqual(result.score, 60.0)
                self.assertGreaterEqual(result.score_coverage, 0.80)

    def test_missing_required_metric_abstains_instead_of_neutral_scoring(self):
        bank = self.healthy_samples()["BANK"]
        bank.pop("tier1_ratio_pct")
        result = evaluate_industry_model("BANK", bank)
        self.assertEqual(result.decision, "ABSTAIN")
        self.assertIn("tier1_ratio_pct", result.required_missing)

        fee_business = self.healthy_samples()["FINANCIAL_FEE"]
        fee_business.pop("debt_b")
        fee_result = evaluate_industry_model("FINANCIAL_FEE", fee_business)
        self.assertEqual(fee_result.decision, "ABSTAIN")
        self.assertIn("debt_b", fee_result.required_missing)

    def test_missing_average_capital_falls_back_to_current_balance(self):
        bank = self.healthy_samples()["BANK"]
        bank["average_tangible_equity_b"] = math.nan
        result = evaluate_industry_model("BANK", bank)
        self.assertTrue(math.isfinite(result.metrics["rotce_pct"]))
        self.assertAlmostEqual(result.metrics["rotce_pct"], 15.0)

    def test_bank_below_reported_well_capitalized_minimum_fails(self):
        bank = self.healthy_samples()["BANK"]
        bank["tier1_ratio_pct"] = 7.5
        result = evaluate_industry_model("BANK", bank)
        self.assertEqual(result.decision, "FAIL")
        self.assertTrue(any("well-capitalized" in reason for reason in result.hard_failures))

    def test_p_and_c_prefers_company_ratio_and_labels_unreconciled_proxy(self):
        reported = self.healthy_samples()["INSURANCE_P_AND_C"]
        reported["company_reported_combined_ratio_pct"] = 91.0
        result = evaluate_industry_model("INSURANCE_P_AND_C", reported)
        self.assertEqual(result.metrics["combined_ratio_source_status"], "COMPANY_REPORTED")
        self.assertAlmostEqual(result.metrics["combined_ratio_for_model_pct"], 91.0)

        proxy_only = self.healthy_samples()["INSURANCE_P_AND_C"]
        proxy_result = evaluate_industry_model("INSURANCE_P_AND_C", proxy_only)
        self.assertEqual(
            proxy_result.metrics["combined_ratio_source_status"],
            "SEC_PROXY_UNRECONCILED",
        )
        self.assertLessEqual(proxy_result.components["underwriting_profitability"], 75.0)
        self.assertTrue(any("human kpi review" in warning.lower() for warning in proxy_result.warnings))

    def test_p_and_c_regressions_keep_company_kpis_separate(self):
        acgl = self.healthy_samples()["INSURANCE_P_AND_C"]
        acgl.update(
            {
                "company_reported_combined_ratio_pct": 86.0,
                "monthly_combined_ratio_pct": 82.0,
                "quarterly_combined_ratio_pct": 86.0,
                "reserve_development_to_premium_pct": -1.5,
                "catastrophe_loss_ratio_pct": 3.2,
            }
        )
        acgl_result = evaluate_industry_model("INSURANCE_P_AND_C", acgl)
        self.assertEqual(acgl_result.metrics["combined_ratio_for_model_pct"], 86.0)
        self.assertEqual(acgl_result.metrics["monthly_combined_ratio_pct"], 82.0)
        self.assertEqual(acgl_result.metrics["reserve_development_to_premium_pct"], -1.5)
        self.assertEqual(acgl_result.metrics["catastrophe_loss_ratio_pct"], 3.2)

        knsl = self.healthy_samples()["INSURANCE_P_AND_C"]
        knsl.update(
            {
                "company_reported_combined_ratio_pct": 75.0,
                "premium_growth_pct": 1.0,
                "market_cap_b": 30.0,
            }
        )
        knsl_result = evaluate_industry_model("INSURANCE_P_AND_C", knsl)
        self.assertLess(knsl_result.score, 95.0)
        self.assertTrue(any("premium growth below" in warning.lower() for warning in knsl_result.warnings))

    def test_pgr_monthly_ratio_cannot_replace_quarterly_normalized_ratio(self):
        pgr = self.healthy_samples()["INSURANCE_P_AND_C"]
        pgr.update(
            {
                "company_reported_combined_ratio_pct": 84.0,
                "monthly_combined_ratio_pct": 84.0,
                "quarterly_combined_ratio_pct": 92.0,
                "policy_count_growth_pct": 7.0,
            }
        )
        result = evaluate_industry_model("INSURANCE_P_AND_C", pgr)
        self.assertEqual(result.metrics["monthly_combined_ratio_pct"], 84.0)
        self.assertEqual(result.metrics["quarterly_combined_ratio_pct"], 92.0)
        self.assertEqual(result.metrics["combined_ratio_for_model_pct"], 92.0)
        self.assertEqual(result.metrics["combined_ratio_period_for_model"], "QUARTERLY")
        self.assertIn("policy_count_growth", result.components)

    def test_p_and_c_stress_abstains_when_core_inputs_are_missing(self):
        missing = calculate_p_and_c_stress({"premium_growth_pct": 10.0})
        self.assertEqual(missing["p_and_c_stress_status"], "ABSTAIN")
        self.assertTrue(math.isnan(missing["p_and_c_stress_pretax_income_moderate_b"]))

        complete = calculate_p_and_c_stress(
            {
                "premiums_earned_ttm_b": 20.0,
                "company_reported_combined_ratio_pct": 90.0,
                "premium_growth_pct": 8.0,
                "net_investment_income_ttm_b": 1.8,
                "invested_assets_b": 40.0,
                "investment_yield_pct": 4.5,
                "pretax_income_ttm_b": 3.5,
                "equity_b": 10.0,
                "assets_b": 50.0,
            }
        )
        self.assertEqual(complete["p_and_c_stress_status"], "PASS")
        self.assertTrue(complete["p_and_c_stress_survival_moderate"])

    def test_known_hard_failure_dominates_an_unrelated_missing_metric(self):
        bank = self.healthy_samples()["BANK"]
        bank["tier1_ratio_pct"] = 7.5
        bank.pop("deposits_b")
        result = evaluate_industry_model("BANK", bank)
        self.assertEqual(result.decision, "FAIL")
        self.assertIn("deposits_b", result.required_missing)
        self.assertTrue(result.hard_failures)

    def test_cyclical_requires_five_year_midcycle_history(self):
        result = evaluate_industry_model(
            "CYCLICAL_MIDCYCLE",
            {
                "ebitda_history_b": [5, 6, 7, 8],
                "current_ebitda_b": 7,
            },
        )
        self.assertEqual(result.decision, "ABSTAIN")
        self.assertTrue(math.isnan(result.score))

    def test_reit_uses_ffo_and_affo_proxies_not_gaap_pe(self):
        result = evaluate_industry_model("REIT_EQUITY", self.healthy_samples()["REIT_EQUITY"])
        self.assertAlmostEqual(result.metrics["ffo_proxy_b"], 2.4)
        self.assertAlmostEqual(result.metrics["affo_proxy_b"], 2.0)
        self.assertIn("net_debt_to_ebitdare_x", result.metrics)

    def test_reit_missing_adjustments_are_not_silently_treated_as_zero(self):
        reit = self.healthy_samples()["REIT_EQUITY"]
        reit.pop("gain_on_property_sale_ttm_b")
        reit.pop("real_estate_impairment_ttm_b")
        reit.pop("tax_ttm_b")
        result = evaluate_industry_model("REIT_EQUITY", reit)
        self.assertEqual(result.decision, "ABSTAIN")
        self.assertTrue(math.isnan(result.metrics["ffo_proxy_b"]))
        self.assertTrue(math.isnan(result.metrics["ebitdare_proxy_b"]))
        self.assertIn("gain_on_property_sale_ttm_b", result.optional_missing)

    def test_mortgage_reit_requires_company_defined_recurring_earnings(self):
        mortgage_reit = self.healthy_samples()["REIT_MORTGAGE"]
        mortgage_reit.pop("recurring_earnings_ttm_b")
        result = evaluate_industry_model("REIT_MORTGAGE", mortgage_reit)
        self.assertEqual(result.decision, "ABSTAIN")
        self.assertIn("recurring_earnings_ttm_b", result.required_missing)

    def test_each_specialized_model_enforces_its_survival_hard_gate(self):
        mutations = {
            "INSURANCE_P_AND_C": {"combined_expense_ttm_b": 11.5},
            "INSURANCE_LIFE": {"policyholder_benefits_ttm_b": 15.0},
            "REIT_EQUITY": {"debt_b": 30.0, "cash_b": 0.0},
            "REIT_MORTGAGE": {"assets_b": 200.0, "equity_b": 10.0},
            "REGULATED_UTILITY": {"interest_ttm_b": 4.0},
            "CYCLICAL_MIDCYCLE": {"interest_ttm_b": 4.0},
            "FINANCIAL_LENDER": {"tangible_equity_b": 3.0},
            "FINANCIAL_FEE": {"ocf_ttm_b": -1.0},
        }
        for model_key, changes in mutations.items():
            with self.subTest(model_key=model_key):
                metrics = self.healthy_samples()[model_key]
                metrics.update(changes)
                result = evaluate_industry_model(model_key, metrics)
                self.assertEqual(result.decision, "FAIL")
                self.assertTrue(result.hard_failures)

    def test_negative_current_cyclical_earnings_are_not_rewarded(self):
        cyclical = self.healthy_samples()["CYCLICAL_MIDCYCLE"]
        cyclical["current_ebitda_b"] = -1.0
        result = evaluate_industry_model("CYCLICAL_MIDCYCLE", cyclical)
        self.assertEqual(result.decision, "FAIL")
        self.assertEqual(result.components["cycle_position"], 0.0)

    def test_net_cash_cyclical_does_not_require_interest_evidence(self):
        cyclical = self.healthy_samples()["CYCLICAL_MIDCYCLE"]
        cyclical["cash_b"] = cyclical["debt_b"] + 1.0
        cyclical["interest_ttm_b"] = float("nan")
        result = evaluate_industry_model("CYCLICAL_MIDCYCLE", cyclical)
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.metrics["debt_service_method"], "net_cash_non_binding")
        self.assertEqual(result.components["trough_interest_coverage"], 100.0)
        self.assertNotIn("interest_ttm_b", result.required_missing)

    def test_nonpositive_cyclical_enterprise_value_does_not_receive_a_value_score(self):
        cyclical = self.healthy_samples()["CYCLICAL_MIDCYCLE"]
        cyclical["enterprise_value_b"] = -1.0
        result = evaluate_industry_model("CYCLICAL_MIDCYCLE", cyclical)
        self.assertEqual(result.decision, "ABSTAIN")
        self.assertIn("enterprise_value_b", result.required_missing)
        self.assertNotIn("midcycle_valuation", result.components)

    def test_specialized_confidence_is_a_hard_gate(self):
        evaluation = evaluate_industry_model("BANK", self.healthy_samples()["BANK"])
        confidence = assess_specialized_data_confidence(
            {"selected_source_count": 10, "accepted_at_ratio": 0.0},
            evaluation,
            extra_optional_missing=["uninsured deposits"],
        )
        self.assertTrue(confidence["abstain"])
        self.assertLess(confidence["score"], 70.0)

    def test_first_layer_uses_industry_specific_hard_failures(self):
        dropped = initial_screen_industry("BANK", {"bookValue": -1, "returnOnEquity": -0.3})
        self.assertEqual(dropped["decision"], "DROP")
        deferred = initial_screen_industry("REIT_EQUITY", {})
        self.assertEqual(deferred["decision"], "PASS")
        self.assertTrue(deferred["warnings"])
        asset_manager = initial_screen_industry(
            "ALTERNATIVE_ASSET_MANAGER", {"ebitda": -1, "operatingCashflow": -1}
        )
        self.assertEqual(asset_manager["decision"], "DROP")

    def test_routing_covers_specialized_subtypes(self):
        cases = [
            (("Financial Services", "Banks - Regional"), "BANK"),
            (("Financial Services", "Insurance - Life"), "INSURANCE_LIFE"),
            (("Healthcare", "Healthcare Plans"), "INSURANCE_LIFE"),
            (("Real Estate", "REIT - Retail"), "REIT_EQUITY"),
            (("Real Estate", "REIT - Mortgage"), "REIT_MORTGAGE"),
            (("Utilities", "Utilities - Regulated Electric"), "REGULATED_UTILITY"),
            (("Energy", "Oil & Gas E&P"), "CYCLICAL_MIDCYCLE"),
            (("Financial Services", "Asset Management"), "OTHER_FEE_FINANCIAL"),
        ]
        for (sector, industry), model_key in cases:
            with self.subTest(model_key=model_key):
                route = route_industry_model(sector, industry)
                self.assertTrue(route["supported"])
                self.assertEqual(route["model_key"], model_key)

    def test_asset_manager_ticker_refines_the_subtype(self):
        self.assertEqual(
            route_industry_model("Financial Services", "Asset Management", ticker="BX")["model_key"],
            "ALTERNATIVE_ASSET_MANAGER",
        )
        self.assertEqual(
            route_industry_model("Financial Services", "Asset Management", ticker="BAM")["model_key"],
            "INSURANCE_LINKED_ASSET_MANAGER",
        )
        self.assertEqual(
            route_industry_model("Financial Services", "Asset Management", ticker="AMG")["model_key"],
            "TRADITIONAL_ASSET_MANAGER",
        )

    def test_nonregulated_utility_and_defense_are_not_forced_into_midcycle_models(self):
        merchant = route_industry_model("Utilities", "Utilities - Independent Power Producers")
        defense = route_industry_model("Industrials", "Aerospace & Defense")
        self.assertEqual(merchant["model_key"], "GENERAL_CORPORATE")
        self.assertEqual(defense["model_key"], "GENERAL_CORPORATE")


if __name__ == "__main__":
    unittest.main()
