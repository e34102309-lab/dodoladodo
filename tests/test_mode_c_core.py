import inspect
import math
import unittest
from unittest.mock import patch

import pandas as pd

from AQR_ModeC_Agent_V12 import (
    STARTER_WEIGHT_PCT_TOTAL,
    ModeCResult,
    SECDataDistiller,
    _compose_total_debt,
    _specialized_average_balance,
    _specialized_balance_growth_pct,
    _specialized_ebitda_history,
    _specialized_ppe_capex_proxy,
    _specialized_total_debt,
    analyze_dsi_signal,
    annual_values_by_year,
    apply_long_term_framework,
    assess_inventory_factor_applicability,
    assess_data_confidence,
    build_agent_verification_plan,
    calc_dsi_series,
    calculate_fcf_stability,
    calculate_financial_stress,
    calculate_interest_coverage_gate,
    calculate_per_share_growth_3y,
    calculate_roic_capital_metrics,
    common_equity_rejection_reason,
    composite_score_for_result,
    dynamic_implied_cagr_limit,
    estimate_maintenance_capex_amount,
    estimate_maintenance_capex_profile,
    maintenance_fcf_research_warnings,
    get_upcoming_earnings,
    historical_valuation,
    historical_valuation_quantile,
    hydrate_info_cache_from_verified_universe,
    implied_ebitda_cagr,
    minimum_positive_fcf_years,
    pre_fetch_all_market_data,
    route_industry_model,
    run_mode_c_pipeline,
    run_specialized_mode_c_pipeline,
    safe_yf_info,
    select_diversified_shortlist,
    short_interest_data_age_days,
    split_adjusted_share_value,
    stale_required_fact_names,
)
from mode_c_evidence import GLOBAL_EVIDENCE_LEDGER


class ModeCCoreTests(unittest.TestCase):
    def test_ttm_flow_without_quarters_or_annual_fact_returns_missing_not_zero(self):
        sec = SECDataDistiller(
            "research@example.com",
            ticker="TEST",
            cik="1",
            decision_timestamp=pd.Timestamp("2026-01-02", tz="UTC"),
        )
        frame = SECDataDistiller._clean_facts(
            pd.DataFrame(
                [
                    {
                        "start": "2025-01-01",
                        "end": "2025-03-31",
                        "filed": "2025-04-15",
                        "val": 123.0,
                        "form": "8-K",
                        "fp": "",
                        "fy": 2025,
                        "concept": "FixtureMetric",
                    }
                ]
            )
        )
        value, method, details = sec.ttm_flow(frame, normalized_metric="Fixture")
        self.assertTrue(math.isnan(value))
        self.assertEqual(method, "missing")
        self.assertEqual(details, {})

    def test_per_share_growth_uses_positive_comparable_annual_endpoints(self):
        class FakeSec:
            ticker = "TEST"
            decision_timestamp = pd.Timestamp("2026-01-02")

            @staticmethod
            def _annual_facts(frame):
                return frame.copy()

            @staticmethod
            def _instant_facts(frame):
                return frame.copy()

            @staticmethod
            def _mark_rows_used(*_args, **_kwargs):
                return []

        def annual(base, latest):
            return pd.DataFrame(
                {
                    "end": pd.to_datetime(["2022-12-31", "2025-12-31"]),
                    "filed": pd.to_datetime(["2023-02-01", "2026-02-01"]),
                    "val": [base, latest],
                }
            )

        shares = pd.DataFrame(
            {
                "end": pd.to_datetime(["2022-12-31", "2025-12-31"]),
                "filed": pd.to_datetime(["2023-02-01", "2026-02-01"]),
                "val": [100e6, 100e6],
                "concept": ["EntityCommonStockSharesOutstanding"] * 2,
            }
        )
        growth = calculate_per_share_growth_3y(
            FakeSec(),
            annual(100e6, 200e6),
            annual(20e6, 40e6),
            annual(5e6, 10e6),
            annual(20e6, 30e6),
            annual(500e6, 800e6),
            annual(50e6, 100e6),
            shares,
        )
        self.assertEqual(growth["years"], 3.0)
        self.assertGreater(growth["fcf_cagr_pct"], 25.0)
        self.assertAlmostEqual(growth["eps_cagr_pct"], 25.992, places=2)

    def test_share_facts_are_adjusted_to_a_common_split_basis(self):
        split_data = pd.DataFrame(
            {("Stock Splits", "TEST"): [10.0]},
            index=[pd.Timestamp("2024-06-10")],
        )
        split_data.columns = pd.MultiIndex.from_tuples(split_data.columns)
        with patch("AQR_ModeC_Agent_V12._BULK_MARKET_DATA", split_data):
            adjusted, factor = split_adjusted_share_value(
                "TEST",
                2.5e9,
                "2023-12-31",
                "2024-02-01",
                "EntityCommonStockSharesOutstanding",
                "2025-01-01",
            )
            recast, recast_factor = split_adjusted_share_value(
                "TEST",
                25e9,
                "2023-12-31",
                "2024-07-01",
                "WeightedAverageNumberOfSharesOutstandingBasic",
                "2025-01-01",
            )
        self.assertAlmostEqual(adjusted, 25e9)
        self.assertEqual(factor, 10.0)
        self.assertAlmostEqual(recast, 25e9)
        self.assertEqual(recast_factor, 1.0)

    def test_market_history_prefetch_batches_and_preserves_multiindex(self):
        calls = []

        def fake_download(tickers, **kwargs):
            calls.append(list(tickers))
            columns = pd.MultiIndex.from_product([["Close", "Volume"], tickers])
            return pd.DataFrame(
                [[10.0] * len(columns)],
                index=[pd.Timestamp("2026-01-02")],
                columns=columns,
            )

        with patch("AQR_ModeC_Agent_V12.yf.download", side_effect=fake_download):
            pre_fetch_all_market_data(["CCC", "AAA", "BBB"], batch_size=2)
            import AQR_ModeC_Agent_V12 as mode_c

            downloaded = mode_c._BULK_MARKET_DATA
        self.assertEqual(calls, [["AAA", "BBB"], ["CCC"]])
        self.assertIn(("Close", "CCC"), downloaded.columns)

    def test_weighted_average_shares_only_apply_splits_after_the_filing_basis(self):
        split_data = pd.DataFrame(
            {("Stock Splits", "TEST"): [2.0, 3.0]},
            index=[pd.Timestamp("2024-06-10"), pd.Timestamp("2025-06-10")],
        )
        split_data.columns = pd.MultiIndex.from_tuples(split_data.columns)
        with patch("AQR_ModeC_Agent_V12._BULK_MARKET_DATA", split_data):
            weighted, weighted_factor = split_adjusted_share_value(
                "TEST",
                200e6,
                "2023-12-31",
                "2024-07-01",
                "WeightedAverageNumberOfSharesOutstandingBasic",
                "2026-01-01",
            )
            instant, instant_factor = split_adjusted_share_value(
                "TEST",
                100e6,
                "2023-12-31",
                "2024-07-01",
                "EntityCommonStockSharesOutstanding",
                "2026-01-01",
            )
        self.assertAlmostEqual(weighted, 600e6)
        self.assertEqual(weighted_factor, 3.0)
        self.assertAlmostEqual(instant, 600e6)
        self.assertEqual(instant_factor, 6.0)

    def test_historical_valuation_aligns_shares_to_the_valuation_date_split_basis(self):
        GLOBAL_EVIDENCE_LEDGER.reset()
        sec = SECDataDistiller(
            "research@example.com",
            ticker="TEST",
            cik="1",
            decision_timestamp=pd.Timestamp("2025-01-01", tz="UTC"),
        )
        period_start = pd.Timestamp("2023-01-01")
        period_end = pd.Timestamp("2023-12-31")
        filed_at = pd.Timestamp("2024-02-15")

        def fact_frame(metric, concept, value, unit="USD", instant=False):
            evidence_id = GLOBAL_EVIDENCE_LEDGER.register_source_fact(
                ticker="TEST",
                cik="0000000001",
                source_system="SEC",
                concept=concept,
                normalized_metric=metric,
                value=value,
                unit=unit,
                period_start="" if instant else period_start,
                period_end=period_end,
                filed_at=filed_at,
                accepted_at=filed_at,
                accession_number=f"TEST-{metric}",
                form="10-K",
                fy=2023,
                fp="FY",
                source_priority=0,
                availability_source="accepted_at",
                available_to_model_at=filed_at,
                decision_timestamp=sec.decision_timestamp,
                is_available_at_decision=True,
            )
            row = {
                "end": period_end,
                "filed": filed_at,
                "available_to_model_at": filed_at,
                "val": value,
                "form": "10-K",
                "fp": "FY",
                "fy": 2023,
                "concept": concept,
                "concept_priority": 0,
                "evidence_id": evidence_id,
            }
            if not instant:
                row["start"] = period_start
            return SECDataDistiller._clean_facts(pd.DataFrame([row]))

        ebit = fact_frame("EBIT", "OperatingIncomeLoss", 1e9)
        dna = fact_frame("D_and_A", "DepreciationDepletionAndAmortization", 0.2e9)
        cash = fact_frame("Cash", "CashAndCashEquivalentsAtCarryingValue", 0.0, instant=True)
        debt = fact_frame("DebtTotal", "DebtCurrentAndLongTerm", 0.0, instant=True)
        net_income = fact_frame("Net_Income", "NetIncomeLoss", 0.5e9)
        shares = fact_frame(
            "Shares_Outstanding",
            "EntityCommonStockSharesOutstanding",
            100e6,
            unit="shares",
            instant=True,
        )
        market_data = pd.DataFrame(
            {
                ("Close", "TEST"): [float("nan"), 10.0, float("nan")],
                ("Stock Splits", "TEST"): [10.0, float("nan"), 2.0],
            },
            index=[
                pd.Timestamp("2024-01-15"),
                pd.Timestamp("2024-02-16"),
                pd.Timestamp("2024-06-10"),
            ],
        )
        market_data.columns = pd.MultiIndex.from_tuples(market_data.columns)
        try:
            with patch("AQR_ModeC_Agent_V12._BULK_MARKET_DATA", market_data):
                result = historical_valuation(
                    "TEST",
                    sec,
                    ebit,
                    dna,
                    debt,
                    pd.DataFrame(),
                    cash,
                    net_income,
                    shares,
                    current_ev_ebitda=10.0,
                    current_pe=20.0,
                    shares_now=1.0,
                )

            self.assertEqual(len(result["ev_ebitda_hist"]), 1)
            self.assertAlmostEqual(result["ev_ebitda_hist"][0]["EV_EBITDA"], 10.0 / 1.2)
            evidence = {row["normalized_metric"]: row for row in GLOBAL_EVIDENCE_LEDGER.rows()}
            split_metric = "Historical_Share_Split_Factor_2023-12-31"
            market_cap_metric = "Historical_MarketCap_2023-12-31"
            self.assertEqual(float(evidence[split_metric]["value"]), 10.0)
            self.assertEqual(float(evidence[market_cap_metric]["value"]), 10.0)
            self.assertIn(
                evidence[split_metric]["evidence_id"],
                evidence[market_cap_metric]["source_evidence_ids"],
            )
        finally:
            GLOBAL_EVIDENCE_LEDGER.reset()

    def test_physical_check_uses_explicit_industry_not_incidental_summary_words(self):
        check, _ = build_agent_verification_plan(
            "JKHY",
            {
                "sector": "Technology",
                "industry": "Information Technology Services",
                "longBusinessSummary": "Provides data center software plus optional hardware services.",
            },
            implied_cagr_pct=8.0,
            real_fcf_yield_pct=5.0,
        )
        self.assertIn("è»Ÿé«”/ç¶²è·¯/ITæœå‹™", check)
        self.assertNotIn("åŠå°é«”/AIç¡¬é«”", check)

    def test_early_failures_receive_a_terminal_decision_state(self):
        self.assertEqual(ModeCResult(Ticker="TEST", Status="Fail: no price").Decision_State, "FAIL")
        self.assertEqual(
            ModeCResult(Ticker="TEST", Status="Abstain: missing").Decision_State,
            "ABSTAIN",
        )
        self.assertEqual(ModeCResult(Ticker="TEST", Status="Error: bad data").Decision_State, "ABSTAIN")

    @patch("AQR_ModeC_Agent_V12._run_mode_c_pipeline_core")
    def test_market_gate_failure_retains_verified_route_metadata(self, core):
        core.return_value = ModeCResult(Ticker="BANKX", Status="Fail: no price data")
        result = run_mode_c_pipeline(
            "BANKX",
            "0000000001",
            "name@example.com",
            security_metadata={
                "SecurityClass": "COMMON_OR_EQUIVALENT",
                "SecurityClassConfidence": "HIGH",
                "SecurityClassEvidenceSource": "NASDAQ Trader fixture",
                "Sector": "Financial Services",
                "Industry": "Banks - Regional",
                "IndustryModelKey": "BANK",
                "ModelRouteHint": "BANK",
                "RouteReason": "Verified bank route",
                "_HasVerifiedRoute": "True",
            },
        )
        self.assertEqual(result.Industry_Model_Key, "BANK")
        self.assertEqual(result.Initial_Industry_Model_Key, "BANK")
        self.assertEqual(result.Sector, "Financial Services")
        self.assertEqual(result.Scoring_Framework, "INDUSTRY_SPECIALIZED_BANK_V1")
        self.assertFalse(result.Model_Route_Refined)
        self.assertEqual(result.Decision_State, "ABSTAIN")
        self.assertEqual(result.Decision_Reason_Code, "MISSING_MARKET_DATA")

    def test_reit_capex_mapping_excludes_property_acquisitions(self):
        tags = SECDataDistiller("research@example.com").config["RealEstateCapEx"]
        self.assertEqual(tags, ["PaymentsForCapitalImprovements"])

    def test_cash_mapping_excludes_restricted_cash_composite(self):
        tags = SECDataDistiller("research@example.com").config["Cash"]
        self.assertIn("CashAndCashEquivalentsAtCarryingValue", tags)
        self.assertNotIn(
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            tags,
        )

    def test_utility_ppe_proxy_rejects_nonpositive_roll_forward(self):
        class FakeSec:
            ticker = "TEST"
            selected_roles = []

            @staticmethod
            def _instant_facts(frame):
                return frame

            @classmethod
            def _mark_rows_used(ï½{¶‰Ëkºwµçp4(€€€€€€€€¤4(4(€€€‘•˜Ñ•ÍÑ}µ…¥¹Ñ•¹…¹•}…Á•á}ÕÍ•Í}‘¹…}…¹¡½É}…¹‘}É•Ù•¹Õ•}É½İÑ ¡Í•±˜¤è(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ±µ½ÍÑÅÕ…°¡•ÍÑ¥µ…Ñ•}µ…¥¹Ñ•¹…¹•}…Á•á}…µ½Õ¹Ğ ÄÀÀ¸À°€ØÀ¸À°€À¸ÈÀ¤°€Øà¸À¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ±µ½ÍÑÅÕ…°¡•ÍÑ¥µ…Ñ•}µ…¥¹Ñ•¹…¹•}…Á•á}…µ½Õ¹Ğ ÄÀÀ¸À°€ØÀ¸À°€´À¸ÀÔ¤°€äÈ¸À¤((€€€‘•˜Ñ•ÍÑ}µ…¥¹Ñ•¹…¹•}…Á•á}ÁÉ½™¥±•}•áÁ½Í•Í}É…¹•}…¹‘}½¹™¥‘•¹”¡Í•±˜¤è(€€€€€€€±…ÍÌ…­•M•Œè(€€€€€€€€€€€ÍÑ…Ñ¥µ•Ñ¡½(€€€€€€€€€€€‘•˜ÅÕ…ÉÑ•É±å}Í•É¥•Ì¡™É…µ”°µ•ÑÉ¥Œ¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Á¹M•É¥•Ì (€€€€€€€€€€€€€€€€€€€lÈÀ¸Át€¨€Ğ€¬lÈÔ¸Át€¨€Ğ°(€€€€€€€€€€€€€€€€€€€¥¹‘•àõÁ¹‘…Ñ•}É…¹” ˆÈÀÈĞ´ÀÌ´ÌÄˆ°Á•É¥½‘Ìôà°™É•Äô‰Eˆ¤°(€€€€€€€€€€€€€€€€¤((€€€€€€€İ¥Ñ Á…Ñ  (€€€€€€€€€€€€‰EI}5½‘•}•¹Ñ}XÄÈ¹…¹¹Õ…±}Ù…±Õ•Í}‰å}å•…Èˆ°(€€€€€€€€€€€Í¥‘•}•™™•Ğõ±…µ‰‘„Í•Œ°™É…µ”°µ•ÑÉ¥Œè€ (€€€€€€€€€€€€€€€ìÈÀÈÌè€ÔÁ”ä°€ÈÀÈĞè€ÔÕ”ä°€ÈÀÈÔè€ØÁ”åô(€€€€€€€€€€€€€€€¥˜µ•ÑÉ¥Œ€ôô€‰¹ˆ(€€€€€€€€€€€€€€€•±Í”ìÈÀÈĞè€àÁ”ä°€ÈÀÈÔè€ÄÀÁ”åô(€€€€€€€€€€€€¤°(€€€€€€€€¤è(€€€€€€€€€€€ÁÉ½™¥±”€ô•ÍÑ¥µ…Ñ•}µ…¥¹Ñ•¹…¹•}…Á•á}ÁÉ½™¥±” (€€€€€€€€€€€€€€€…­•M•Œ ¤°Á¹…Ñ…É…µ” ¤°Á¹…Ñ…É…µ” ¤°Á¹…Ñ…É…µ” ¤°€ÄÀÀ¸À°€ØÀ¸À(€€€€€€€€€€€€¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡ÁÉ½™¥±•l‰½¹™¥‘•¹”‰t°€‰!% ˆ¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ1•ÍÌ¡ÁÉ½™¥±•l‰µ…¥¹Ñ•¹…¹•}…Á•á}±½İ}ˆ‰t°ÁÉ½™¥±•l‰µ…¥¹Ñ•¹…¹•}…Á•á}ˆ‰t¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ1•ÍÌ¡ÁÉ½™¥±•l‰µ…¥¹Ñ•¹…¹•}…Á•á}ˆ‰t°ÁÉ½™¥±•l‰µ…¥¹Ñ•¹…¹•}…Á•á}¡¥¡}ˆ‰t¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ±µ½ÍÑÅÕ…° (€€€€€€€€€€€ÁÉ½™¥±•l‰É½İÑ¡}…Á•á}ˆ‰t°(€€€€€€€€€€€€ÄÀÀ¸À€´ÁÉ½™¥±•l‰µ…¥¹Ñ•¹…¹•}…Á•á}ˆ‰t°(€€€€€€€€¤((€€€€€€€İ¥Ñ Á…Ñ  (€€€€€€€€€€€€‰EI}5½‘•}•¹Ñ}XÄÈ¹…¹¹Õ…±}Ù…±Õ•Í}‰å}å•…Èˆ°(€€€€€€€€€€€Í¥‘•}•™™•Ğõ±…µ‰‘„Í•Œ°™É…µ”°µ•ÑÉ¥Œè€ (€€€€€€€€€€€€€€€ìÈÀÈÔè€ØÁ”åô¥˜µ•ÑÉ¥Œ€ôô€‰¹ˆ•±Í”ìÈÀÈĞè€àÁ”ä°€ÈÀÈÔè€ÄÀÁ”åô(€€€€€€€€€€€€¤°(€€€€€€€€¤è(€€€€€€€€€€€µ•‘¥Õµ}½¹™¥‘•¹”€ô•ÍÑ¥µ…Ñ•}µ…¥¹Ñ•¹…¹•}…Á•á}ÁÉ½™¥±” (€€€€€€€€€€€€€€€…­•M•Œ ¤°Á¹…Ñ…É…µ” ¤°Á¹…Ñ…É…µ” ¤°Á¹…Ñ…É…µ” ¤°€ÄÀÀ¸À°€ØÀ¸À(€€€€€€€€€€€€¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡µ•‘¥Õµ}½¹™¥‘•¹•l‰½¹™¥‘•¹”‰t°€‰5%U4ˆ¤((€€€€€€€İ¥Ñ Á…Ñ  ‰EI}5½‘•}•¹Ñ}XÄÈ¹…¹¹Õ…±}Ù…±Õ•Í}‰å}å•…Èˆ°É•ÑÕÉ¹}Ù…±Õ”õíô¤è(€€€€€€€€€€€±½İ}½¹™¥‘•¹”€ô•ÍÑ¥µ…Ñ•}µ…¥¹Ñ•¹…¹•}…Á•á}ÁÉ½™¥±” (€€€€€€€€€€€€€€€…­•M•Œ ¤°Á¹…Ñ…É…µ” ¤°Á¹…Ñ…É…µ” ¤°Á¹…Ñ…É…µ” ¤°€ÄÀÀ¸À°µ…Ñ ¹¹…¸(€€€€€€€€€€€€¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡±½İ}½¹™¥‘•¹•l‰½¹™¥‘•¹”‰t°€‰1=\ˆ¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡±½İ}½¹™¥‘•¹•l‰µ…¥¹Ñ•¹…¹•}…Á•á}ˆ‰t°€ÄÀÀ¸À¤(4(€€€‘•˜Ñ•ÍÑ}‘å¹…µ¥}…É}±¥µ¥Ñ}ÑÉ…­Í}É½¥}…¹‘}µ…É¥¹}ÑÉ•¹¡Í•±˜¤è(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡‘å¹…µ¥}¥µÁ±¥•‘}…É}±¥µ¥Ğ Èà¸À°€Ä¸Ô¤°€ĞÀ¸à¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡‘å¹…µ¥}¥µÁ±¥•‘}…É}±¥µ¥Ğ ÈÈ¸À°€À¸Ô¤°€ÌÔ¸È¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡‘å¹…µ¥}¥µÁ±¥•‘}…É}±¥µ¥Ğ ÌÀ¸À°€´È¸Ô¤°€ÄÔ¸À¤((€€€‘•˜Ñ•ÍÑ}É½¥}ÕÍ•Í}…Ù•É…•}…Á¥Ñ…±}…¹‘}µ…É­Í}•¹‘¥¹}™…±±‰…¬¡Í•±˜¤è(€€€€€€€…Ù•É…•€ô…±Õ±…Ñ•}É½¥}…Á¥Ñ…±}µ•ÑÉ¥Ì ÈÀ¸À°€ÄÀÀ¸À°€ÌÀÀ¸À°€ÈÀ¸À°€ØÀ¸À¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡…Ù•É…•‘l‰…Á¥Ñ…±}µ•Ñ¡½‰t°€‰	%99%9}9%9}YIˆ¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ±µ½ÍÑÅÕ…°¡…Ù•É…•‘l‰…Ù•É…•}É½¥}ÁĞ‰t°€ÄÀ¸À¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ±µ½ÍÑÅÕ…°¡…Ù•É…•‘l‰•¹‘¥¹}É½¥}ÁĞ‰t°€ÈÀ¸À€¼€ÌÀÀ¸À€¨€ÄÀÀ¸À¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ±µ½ÍÑÅÕ…°¡…Ù•É…•‘l‰•á±Õ‘¥¹}½½‘İ¥±±}É½¥}ÁĞ‰t°€ÄÈ¸Ô¤((€€€€€€€™…±±‰…¬€ô…±Õ±…Ñ•}É½¥}…Á¥Ñ…±}µ•ÑÉ¥Ì ÈÀ¸À°µ…Ñ ¹¹…¸°€ÌÀÀ¸À¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡™…±±‰…­l‰…Á¥Ñ…±}µ•Ñ¡½‰t°€‰9%9}A%Q1}11	-}MQ%5Qˆ¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ±µ½ÍÑÅÕ…°¡™…±±‰…­l‰…Ù•É…•}É½¥}ÁĞ‰t°™…±±‰…­l‰•¹‘¥¹}É½¥}ÁĞ‰t¤((€€€‘•˜Ñ•ÍÑ}µ…¥¹Ñ•¹…¹•}™™}É¥Í­}…Í•Í}‰•½µ•}µ…¹Õ…±}É•Í•…É¡}Ñ…Í­Ì¡Í•±˜¤è(€€€€€€€İ…É¹¥¹Ì€ôµ…¥¹Ñ•¹…¹•}™™}É•Í•…É¡}İ…É¹¥¹Ì ´À¸Ä°€´Ä¸È°€Ğ¸Ä¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡±•¸¡İ…É¹¥¹Ì¤°€Ì¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡…¹ä ‰±½İ•Èµ‰½Õ¹ˆ¥¸İ…É¹¥¹œ™½Èİ…É¹¥¹œ¥¸İ…É¹¥¹Ì¤¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡…¹ä ‰½¹Í•ÉÙ…Ñ¥Ù”ˆ¥¸İ…É¹¥¹œ™½Èİ…É¹¥¹œ¥¸İ…É¹¥¹Ì¤¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡…¹ä ‰Í•¹Í¥Ñ¥Ù¥Ñäˆ¥¸İ…É¹¥¹œ™½Èİ…É¹¥¹œ¥¸İ…É¹¥¹Ì¤¤(4(€€€‘•˜Ñ•ÍÑ}É•Ù•ÉÍ•}Ù…±Õ…Ñ¥½¹}¥¹±Õ‘•Í}É•ÅÕ¥É•‘}É•ÑÕÉ¸¡Í•±˜¤è4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ±µ½ÍÑÅÕ…°¡¥µÁ±¥•‘}•‰¥Ñ‘…}…È ÄÀÀ¸À°€ÄÀ¸À°€ÄÀ¸À°å•…ÉÌôÌ°É•ÅÕ¥É•‘}É•ÑÕÉ¸ôÀ¸ÄÀ¤°€À¸ÄÀ¤4(4(€€€‘•˜Ñ•ÍÑ}™¥¹…¹¥…±}ÍÑÉ•ÍÍ}É•…±Õ±…Ñ•Í}¥É}İ¥Ñ¡}™¥á•‘}‘¹„¡Í•±˜¤è4(€€€€€€€¡•…±Ñ¡ä€ô…±Õ±…Ñ•}™¥¹…¹¥…±}ÍÑÉ•ÍÌ à¸À°€ÄÀ¸À°€È¸À°€ÈÀ¸À°€À¸À°€Ô¸À°€À¸ÈÀ°€À¸ÌÀ¤4(€€€€€€€İ•…¬€ô…±Õ±…Ñ•}™¥¹…¹¥…±}ÍÑÉ•ÍÌ à¸À°€ÄÀ¸À°€Ğ¸À°€ÈÀ¸À°€À¸À°€Ô¸À°€À¸ÈÀ°€À¸ÌÀ¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ±µ½ÍÑÅÕ…°¡¡•…±Ñ¡ål‰¥È‰t°€È¸Ô¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ±µ½ÍÑÅÕ…°¡¡•…±Ñ¡ål‰É•…±}™™}ˆ‰t°€È¸Ø¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡¡•…±Ñ¡ål‰ÍÕÉÙ¥Ù•Ì‰t¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ…±Í”¡İ•…­l‰ÍÕÉÙ¥Ù•Ì‰t¤4(4(€€€€€€€…Í¡}‰ÕÉ¸€ô…±Õ±…Ñ•}™¥¹…¹¥…±}ÍÑÉ•ÍÌ 4(€€€€€€€€€€€€à¸À°€ÄÀ¸À°€È¸À°€ÈÀ¸À°€À¸À°€Ä¸À°€À¸ÈÀ°€À¸ÌÀ4(€€€€€€€€¤4(€€€€€€€¹•Ñ}…Í¡}ÉÕ¹İ…ä€ô…±Õ±…Ñ•}™¥¹…¹¥…±}ÍÑÉ•ÍÌ 4(€€€€€€€€€€€€à¸À°€ÄÀ¸À°€À¸À°€Ä¸À°€Ô¸À°€Ä¸À°€À¸ÈÀ°€À¸ÌÀ4(€€€€€€€€¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ…±Í”¡…Í¡}‰ÕÉ¹l‰…Í¡}™±½İ}ÍÕÉÙ¥Ù•Ì‰t¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ…±Í”¡…Í¡}‰ÕÉ¹l‰ÍÕÉÙ¥Ù•Ì‰t¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡¹•Ñ}…Í¡}ÉÕ¹İ…ål‰…Í¡}™±½İ}ÍÕÉÙ¥Ù•Ì‰t¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡¹•Ñ}…Í¡}ÉÕ¹İ…ål‰ÍÕÉÙ¥Ù•Ì‰t¤4(4(€€€‘•˜Ñ•ÍÑ}¹•Ñ}…Í¡}‘½•Í}¹½Ñ}É•ÅÕ¥É•}µ¥ÍÍ¥¹}¥¹Ñ•É•ÍÑ}•Ù¥‘•¹”¡Í•±˜¤è4(€€€€€€€…Ñ”€ô…±Õ±…Ñ•}¥¹Ñ•É•ÍÑ}½Ù•É…•}…Ñ” Ä¸À°€À¸À°€À¸ÄÀ°€Ä¸ÔÀ¤4(€€€€€€€ÍÑÉ•ÍÌ€ô…±Õ±…Ñ•}™¥¹…¹¥…±}ÍÑÉ•ÍÌ 4(€€€€€€€€€€€€Ä¸À°€Ä¸È°€À¸À°€À¸ÄÀ°€Ä¸ÔÀ°€À¸à°€À¸ÈÄ°€À¸ÌÀ4(€€€€€€€€¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡…Ñ•l‰µ½‘”‰t°€‰¹•Ñ}…Í ˆ¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ…±Í”¡…Ñ•l‰µ¥ÍÍ¥¹}É¥Ñ¥…°‰t¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡µ…Ñ ¹¥Í¥¹˜¡…Ñ•l‰¥È‰t¤¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡µ…Ñ ¹¥Í¥¹˜¡ÍÑÉ•ÍÍl‰¥È‰t¤¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡ÍÑÉ•ÍÍl‰ÍÕÉÙ¥Ù•Ì‰t¤4(4(€€€€€€€É•Á½ÉÑ•‘}¥¹Ñ•É•ÍĞ€ô…±Õ±…Ñ•}¥¹Ñ•É•ÍÑ}½Ù•É…•}…Ñ” Ä¸À°€À¸à°€À¸ÄÀ°€Ä¸ÔÀ¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡É•Á½ÉÑ•‘}¥¹Ñ•É•ÍÑl‰µ½‘”‰t°€‰¹•Ñ}…Í ˆ¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡µ…Ñ ¹¥Í¥¹˜¡É•Á½ÉÑ•‘}¥¹Ñ•É•ÍÑl‰¥È‰t¤¤4(4(€€€‘•˜Ñ•ÍÑ}¥¹Ù•¹Ñ½Éå}¥¹™±•Ñ¥½¹}É•ÅÕ¥É•Í}Í•…Í½¹…±}½¹™¥Éµ…Ñ¥½¸¡Í•±˜¤è4(€€€€€€€¥‘à€ôÁ¹‘…Ñ•}É…¹” ˆÈÀÈĞ´ÀÌ´ÌÄˆ°Á•É¥½‘ÌôØ°™É•Äô‰Eˆ¤4(€€€€€€€½¹™¥Éµ•€ô…¹…±åé•}‘Í¥}Í¥¹…°¡Á¹M•É¥•Ì¡lÄÀÀ°€äÀ°€ÄÄÀ°€äÔ°€àÀ°€ÜÁt°¥¹‘•àõ¥‘à¤¤4(€€€€€€€Í•…Í½¹…±}½¹±ä€ô…¹…±åé•}‘Í¥}Í¥¹…°¡Á¹M•É¥•Ì¡lÄÀÀ°€ØÀ°€ÄÄÀ°€äÔ°€àÀ°€ÜÁt°¥¹‘•àõ¥‘à¤¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡½¹™¥Éµ•‘l‰¥¹™±•Ñ¥½¸‰t¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ…±Í”¡Í•…Í½¹…±}½¹±ål‰¥¹™±•Ñ¥½¸‰t¤4(€€€€€€€Õ¹Í•…Í½¹•€ô…¹…±åé•}‘Í¥}Í¥¹…°¡Á¹M•É¥•Ì¡läÔ°€àÀ°€ÜÁt°¥¹‘•àõ¥‘ál´Ìét¤¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ…±Í”¡Õ¹Í•…Í½¹•‘l‰¥¹™±•Ñ¥½¸‰t¤4(4(€€€‘•˜Ñ•ÍÑ}‘Í¥}ÕÍ•Í}…Ù•É…•}¥¹Ù•¹Ñ½Éå}½Ù•É}Ñ¡•}ÑÑµ}Á•É¥½¡Í•±˜¤è(€€€€€€€¥¹Ù•¹Ñ½Éä€ôÁ¹…Ñ…É…µ” 4(€€€€€€€€€€€ì4(€€€€€€€€€€€€€€€€‰•¹ˆèÁ¹Ñ½}‘…Ñ•Ñ¥µ”¡lˆÈÀÈĞ´ÄÈ´ÌÄˆ°€ˆÈÀÈÔ´ÄÈ´ÌÄ‰t¤°4(€€€€€€€€€€€€€€€€‰Ù…°ˆèlàÀ¸À°€ÄÀÀ¸Át°4(€€€€€€€€€€€ô4(€€€€€€€€¤4(€€€€€€€½Ì€ôÁ¹M•É¥•Ì 4(€€€€€€€€€€€lÄÀÀ¸À°€ÄÀÀ¸À°€ÄÀÀ¸À°€ÄÀÀ¸Át°4(€€€€€€€€€€€¥¹‘•àõÁ¹Ñ½}‘…Ñ•Ñ¥µ” 4(€€€€€€€€€€€€€€€lˆÈÀÈÔ´ÀÌ´ÌÄˆ°€ˆÈÀÈÔ´ÀØ´ÌÀˆ°€ˆÈÀÈÔ´Àä´ÌÀˆ°€ˆÈÀÈÔ´ÄÈ´ÌÄ‰t4(€€€€€€€€€€€€¤°4(€€€€€€€€¤4(€€€€€€€‘Í¤€ô…±}‘Í¥}Í•É¥•Ì¡¥¹Ù•¹Ñ½Éä°½Ì¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡±•¸¡‘Í¤¤°€Ä¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ±µ½ÍÑÅÕ…°¡™±½…Ğ¡‘Í¤¹¥±½l´Åt¤°€àÈ¸ÄÈÔ¤((€€€‘•˜Ñ•ÍÑ}‘Í¥}¥Í}¹½Ñ}¹•ÕÑÉ…±¥é•‘}İ¡•¹}¥¹Ù•¹Ñ½Éå}¥Í}¹½Ñ}…ÁÁ±¥…‰±”¡Í•±˜¤è(€€€€€€€Í½™Ñİ…É”€ô…ÍÍ•ÍÍ}¥¹Ù•¹Ñ½Éå}™…Ñ½É}…ÁÁ±¥…‰¥±¥Ñä (€€€€€€€€€€€€‰Q•¡¹½±½äˆ°€‰M½™Ñİ…É”€´ÁÁ±¥…Ñ¥½¸ˆ°µ…Ñ ¹¹…¸°€ÄÀ¸À°€Ô¸À(€€€€€€€€¤(€€€€€€€¥µµ…Ñ•É¥…°€ô…ÍÍ•ÍÍ}¥¹Ù•¹Ñ½Éå}™…Ñ½É}…ÁÁ±¥…‰¥±¥Ñä (€€€€€€€€€€€€‰%¹‘ÕÍÑÉ¥…±Ìˆ°€‰MÁ•¥…±Ñä%¹‘ÕÍÑÉ¥…°5…¡¥¹•Éäˆ°€À¸ÀÄ°€ÄÀ¸À°€ÈÀ¸À(€€€€€€€€¤(€€€€€€€µ…Ñ•É¥…°€ô…ÍÍ•ÍÍ}¥¹Ù•¹Ñ½Éå}™…Ñ½É}…ÁÁ±¥…‰¥±¥Ñä (€€€€€€€€€€€€‰%¹‘ÕÍÑÉ¥…±Ìˆ°€‰MÁ•¥…±Ñä%¹‘ÕÍÑÉ¥…°5…¡¥¹•Éäˆ°€È¸À°€ÄÀ¸À°€ÈÀ¸À(€€€€€€€€¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡Í½™Ñİ…É•l‰ÍÑ…ÑÕÌ‰t°€‰9=Q}AA1%	1ˆ¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡¥µµ…Ñ•É¥…±l‰ÍÑ…ÑÕÌ‰t°€‰9=Q}AA1%	1ˆ¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡µ…Ñ•É¥…±l‰ÍÑ…ÑÕÌ‰t°€‰Y1%ˆ¤((€€€‘•˜Ñ•ÍÑ}¡¥ÍÑ½É¥…±}Ù…±Õ…Ñ¥½¹}ÅÕ…¹Ñ¥±•}¡…¹•Í}İ¥Ñ¡}Í…µÁ±•}Í¥é”¡Í•±˜¤è(€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡µ…Ñ ¹¥Í¹…¸¡¡¥ÍÑ½É¥…±}Ù…±Õ…Ñ¥½¹}ÅÕ…¹Ñ¥±” Ğ¤¤¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡¡¥ÍÑ½É¥…±}Ù…±Õ…Ñ¥½¹}ÅÕ…¹Ñ¥±” Ô¤°€ÈÔ¸À¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡¡¥ÍÑ½É¥…±}Ù…±Õ…Ñ¥½¹}ÅÕ…¹Ñ¥±” à¤°€ÈÀ¸À¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡¡¥ÍÑ½É¥…±}Ù…±Õ…Ñ¥½¹}ÅÕ…¹Ñ¥±” ÄÈ¤°€ÄÔ¸À¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡¡¥ÍÑ½É¥…±}Ù…±Õ…Ñ¥½¹}ÅÕ…¹Ñ¥±” ÄÔ¤°€ÄÀ¸À¤(4(€€€‘•˜Ñ•ÍÑ}™™}¡¥ÍÑ½Éå}É•ÅÕ¥É•Í}Í‰}•Ù¥‘•¹•}…¹‘}½¹Í•ÕÑ¥Ù•}½™}å•…ÉÌ¡Í•±˜¤è4(€€€€€€€½˜€ôìÈÀÈÄè€Ñ”ä°€ÈÀÈÌè€Í”ä°€ÈÀÈÔè€Å”åô4(€€€€€€€…Á•à€ôìÈÀÈÄè€Å”ä°€ÈÀÈÌè€Å”ä°€ÈÀÈÔè€À¸É”åô4(€€€€€€€Í‰Œ€ôìÈÀÈÔè€À¸Å”åô4(€€€€€€€‘¹„€ôìÈÀÈÄè€À¸Õ”ä°€ÈÀÈÌè€À¸Õ”ä°€ÈÀÈÔè€À¸Õ”åô4(€€€€€€€É•Ù•¹Õ”€ôìÈÀÈÄè€á”ä°€ÈÀÈÌè€å”ä°€ÈÀÈÔè€ÄÁ”åô4(€€€€€€€¹•Ñ}¥¹½µ”€ôìÈÀÈÄè€Å”ä°€ÈÀÈÌè€Å”ä°€ÈÀÈÔè€Å”åô4(€€€€€€€İ¥Ñ Á…Ñ  4(€€€€€€€€€€€€‰EI}5½‘•}•¹Ñ}XÄÈ¹…¹¹Õ…±}Ù…±Õ•Í}‰å}å•…Èˆ°4(€€€€€€€€€€€Í¥‘•}•™™•Ğõm½˜°…Á•à°Í‰Œ°‘¹„°É•Ù•¹Õ”°¹•Ñ}¥¹½µ•t°4(€€€€€€€€¤è4(€€€€€€€€€€€É•ÍÕ±Ğ€ô…±Õ±…Ñ•}™™}ÍÑ…‰¥±¥Ñä 4(€€€€€€€€€€€€€€€M…Ñ…¥ÍÑ¥±±•È ‰É•Í•…É¡•á…µÁ±”¹½´ˆ¤°4(€€€€€€€€€€€€€€€€©mÁ¹…Ñ…É…µ” ¥t€¨€Ø°4(€€€€€€€€€€€€¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡É•ÍÕ±Ñl‰å•…ÉÍ}…Ù…¥±…‰±”‰t°€Ä¸À¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡É•ÍÕ±Ñl‰Á½Í¥Ñ¥Ù•}å•…ÉÌ‰t°€Ä¸À¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡É•ÍÕ±Ñl‰½™|Íå}å•…ÉÌ‰t°€Ä¸À¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡É•ÍÕ±Ñl‰½™|Íå}ÕµÕ±…Ñ¥Ù•}ˆ‰t°€Ä¸À¤4(4(€€€‘•˜Ñ•ÍÑ}Í¡½ÉÑ}¥¹Ñ•É•ÍÑ}…•}¥Í}•áÁ±¥¥Ğ¡Í•±˜¤è4(€€€€€€€¹½Ü€ôÁ¹Q¥µ•ÍÑ…µÀ ˆÈÀÈØ´ÀÜ´ÄÀˆ°Ñèô‰UQˆ¤4(€€€€€€€½‰Í•ÉÙ•€ôÁ¹Q¥µ•ÍÑ…µÀ ˆÈÀÈØ´ÀØ´ÌÀˆ°Ñèô‰UQˆ¤¹Ñ¥µ•ÍÑ…µÀ ¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ±µ½ÍÑÅÕ…°¡Í¡½ÉÑ}¥¹Ñ•É•ÍÑ}‘…Ñ…}…•}‘…åÌ¡ì‰‘…Ñ•M¡½ÉÑ%¹Ñ•É•ÍĞˆè½‰Í•ÉÙ•‘ô°¹½Üõ¹½Ü¤°€ÄÀ¸À¤4(4(€€€‘•˜}½½‘}…¹‘¥‘…Ñ”¡Í•±˜°Ñ¥­•Èô‰==ˆ°Í•Ñ½Èô‰Q•¡¹½±½äˆ¤è4(€€€€€€€É•ÑÕÉ¸5½‘•I•ÍÕ±Ğ 4(€€€€€€€€€€€Q¥­•ÈõÑ¥­•È°4(€€€€€€€€€€€MÑ…ÑÕÌô‰A…ÍÌˆ°4(€€€€€€€€€€€M•Ñ½ÈõÍ•Ñ½È°4(€€€€€€€€€€€I•…±}}e¥•±‘}ÁĞôà¸À°4(€€€€€€€€€€€%HôÄÀ¸À°4(€€€€€€€€€€€M¡…É•}½Õ¹Ñ}¡…¹•}ÁĞô´Ä¸À°4(€€€€€€€€€€€M¡…É•}½Õ¹Ñ}¡…¹•|Íe}ÁĞô´Ì¸À°4(€€€€€€€€€€€¥±ÕÑ¥½¹}%±±ÕÍ¥½¸õ…±Í”°4(€€€€€€€€€€€A•ÉÍ¥ÍÑ•¹Ñ}¥±ÕÑ¥½¸õ…±Í”°4(€€€€€€€€€€€I=%}ÁĞôÄà¸À°4(€€€€€€€€€€€I=}ÁĞôÈÈ¸À°4(€€€€€€€€€€€=|Íe}ÕµÕ±…Ñ¥Ù•}ôÔ¸À°4(€€€€€€€€€€€=|Íe}e•…ÉÌôÌ¸À°4(€€€€€€€€€€€I•…±}}A½Í¥Ñ¥Ù•}e•…ÉÍ|ÕdôÔ¸À°4(€€€€€€€€€€€I•…±}}e•…ÉÍ}Ù…¥±…‰±”ôÔ¸À°4(€€€€€€€€€€€I•…±}}5…É¥¹}MÑ‘|Õe}ÁĞôÌ¸À°4(€€€€€€€€€€€=}Ñ½}9•Ñ%¹½µ•|ÕdôÄ¸Ä°4(€€€€€€€€€€€I•…±}}Ñ½}9•Ñ%¹½µ•|ÕdôÀ¸à°4(€€€€€€€€€€€…Á¥Ñ…±}±±½…Ñ¥½¹}M½É”ôàÔ¸À°4(€€€€€€€€€€€Y}	%Q|ÄÁe}A•É•¹Ñ¥±”ôÄÀ¸À°4(€€€€€€€€€€€	%Q}É…İ‘½İ¹|ÌÁ}ÁĞô´ÈÀ¸À°4(€€€€€€€€€€€MÑÉ•ÍÍ}%I|ÌÁàôÔ¸À°4(€€€€€€€€€€€9•Ñ•‰Ñ}Ñ½}MÑÉ•ÍÍ}	%Q|ÌÁàôÄ¸À°4(€€€€€€€€€€€MÑÉ•ÍÍ}MÕÉÙ¥Ù…±|ÌÀõQÉÕ”°4(€€€€€€€€€€€5}¥…¹½Í¥Ìô‹’â·šŸ¾òk’â'–¶¢Ú£–.‹šr«Ö›–ëšb;Šë¦¦Š£¢¢+¢f|ˆ°4(€€€€€€€€€€€%µÁ±¥•‘}	%Q}I|Íe}ÁĞôÄÀ¸À°4(€€€€€€€€€€€5½µ•¹ÑÕµ|ÄÉ5}ÁĞôÄÔ¸À°4(€€€€€€€€€€€…Ñ…}EÕ…±¥Ñå}±…Ìô‰=,ˆ°4(€€€€€€€€¤4(4(€€€‘•˜Ñ•ÍÑ}ÅÕ…±¥Ñå}…¹‘}Ù…±Õ•}É…¥Í•}±½¹}Ñ•Éµ}Í½É”¡Í•±˜¤è4(€€€€€€€½½€ô…ÁÁ±å}±½¹}Ñ•Éµ}™É…µ•İ½É¬¡Í•±˜¹}½½‘}…¹‘¥‘…Ñ” ¤¤4(€€€€€€€İ•…¬€ôÍ•±˜¹}½½‘}…¹‘¥‘…Ñ”¡Ñ¥­•Èô‰],ˆ¤4(€€€€€€€İ•…¬¹I•…±}}e¥•±‘}ÁĞ€ô€Ä¸À4(€€€€€€€İ•…¬¹%H€ô€Ä¸Ô4(€€€€€€€İ•…¬¹M¡…É•}½Õ¹Ñ}¡…¹•}ÁĞ€ô€Ì¸À4(€€€€€€€İ•…¬¹Y}	%Q|ÄÁe}A•É•¹Ñ¥±”€ô€àÔ¸À4(€€€€€€€İ•…¬¹	%Q}É…İ‘½İ¹|ÌÁ}ÁĞ€ô€´ØÔ¸À4(€€€€€€€İ•…¬¹5}¥…¹½Í¥Ì€ô€‹ÖCš/šŸ–ç–ó¦fß¦bÇ¾òkšRÛšr«–Ò§’öš¾o–"§¦ê3–’Ç¢† ˆ4(€€€€€€€İ•…¬€ô…ÁÁ±å}±½¹}Ñ•Éµ}™É…µ•İ½É¬¡İ•…¬¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÉ•…Ñ•È¡½½¹1½¹}Q•Éµ}M½É”°İ•…¬¹1½¹}Q•Éµ}M½É”¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡½½¹1½¹}Q•Éµ}±¥¥‰±”¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ…±Í”¡İ•…¬¹1½¹}Q•Éµ}±¥¥‰±”¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡½½¹MÕ•ÍÑ•‘}MÑ…ÉÑ•É}]•¥¡Ñ}ÁÑ}Q½Ñ…°°MQIQI}]%!Q}AQ}Q=Q0¤4(4(€€€‘•˜Ñ•ÍÑ}Ñ¡É••}½É}™½ÕÉ}å•…É}™™}¡¥ÍÑ½Éå}É•ÅÕ¥É•Í}Í¥áÑå}Á•É•¹Ñ}Á½Í¥Ñ¥Ù”¡Í•±˜¤è4(€€€€€€€…¹‘¥‘…Ñ”€ôÍ•±˜¹}½½‘}…¹‘¥‘…Ñ”¡Ñ¥­•Èô‰5%aˆ¤4(€€€€€€€…¹‘¥‘…Ñ”¹I•…±}}e•…ÉÍ}Ù…¥±…‰±”€ô€Ğ¸À4(€€€€€€€…¹‘¥‘…Ñ”¹I•…±}}A½Í¥Ñ¥Ù•}e•…ÉÍ|Õd€ô€È¸À4(€€€€€€€…¹‘¥‘…Ñ”€ô…ÁÁ±å}±½¹}Ñ•Éµ}™É…µ•İ½É¬¡…¹‘¥‘…Ñ”¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡µ¥¹¥µÕµ}Á½Í¥Ñ¥Ù•}™™}å•…ÉÌ Ğ¸À¤°€Ì¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ…±Í”¡…¹‘¥‘…Ñ”¹1½¹}Q•Éµ}±¥¥‰±”¤4(4(€€€‘•˜Ñ•ÍÑ}‘¥±ÕÑ¥½¹}¥Í}¹½Ñ}‘½Õ‰±•}½Õ¹Ñ•‘}‰ÕÑ}Á•ÉÍ¥ÍÑ•¹Ñ}‘¥±ÕÑ¥½¹}•á±Õ‘•Ì¡Í•±˜¤è(€€€€€€€±•…¸€ô…ÁÁ±å}±½¹}Ñ•Éµ}™É…µ•İ½É¬¡Í•±˜¹}½½‘}…¹‘¥‘…Ñ” ¤¤((€€€€€€€İ…É¹¥¹œ€ôÍ•±˜¹}½½‘}…¹‘¥‘…Ñ”¡Ñ¥­•Èô‰]I8ˆ¤(€€€€€€€İ…É¹¥¹œ¹¥±ÕÑ¥½¹}%±±ÕÍ¥½¸€ôQÉÕ”(€€€€€€€İ…É¹¥¹œ€ô…ÁÁ±å}±½¹}Ñ•Éµ}™É…µ•İ½É¬¡İ…É¹¥¹œ¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡İ…É¹¥¹œ¹1½¹}Q•Éµ}±¥¥‰±”¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡İ…É¹¥¹œ¹1½¹}Q•Éµ}M½É”°±•…¸¹1½¹}Q•Éµ}M½É”¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡İ…É¹¥¹œ¹¥±ÕÑ¥½¹}½Õ‰±•}½Õ¹Ñ}¡•¬°€‰AMLˆ¤((€€€€€€€¥ÍÍÕ…¹”€ôÍ•±˜¹}½½‘}…¹‘¥‘…Ñ”¡Ñ¥­•Èô‰%MMUˆ¤(€€€€€€€¥ÍÍÕ…¹”¹M¡…É•}½Õ¹Ñ}¡…¹•}ÁĞ€ô€È¸À(€€€€€€€¥ÍÍÕ…¹”¹I•…±}	Õå‰…­}€ô€´À¸Ô(€€€€€€€¥ÍÍÕ…¹”¹…Á¥Ñ…±}±±½…Ñ¥½¹}M½É”€ô€ĞÀ¸À(€€€€€€€¥ÍÍÕ…¹”€ô…ÁÁ±å}±½¹}Ñ•Éµ}™É…µ•İ½É¬¡¥ÍÍÕ…¹”¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡¥ÍÍÕ…¹”¹=İ¹•ÉÍ¡¥Á}¥±ÕÑ¥½¹}A•¹…±Ñä°€À¸À¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡¥ÍÍÕ…¹”¹…Á¥Ñ…±}±±½…Ñ¥½¹}A•¹…±Ñä°€ÈÀ¸À¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡¥ÍÍÕ…¹”¹¥±ÕÑ¥½¹}Q½Ñ…±}M½É•}%µÁ…Ğ°€Ä¸À¤((€€€€€€€Á•ÉÍ¥ÍÑ•¹Ğ€ôÍ•±˜¹}½½‘}…¹‘¥‘…Ñ”¡Ñ¥­•Èô‰%1UQˆ¤(€€€€€€€Á•ÉÍ¥ÍÑ•¹Ğ¹A•ÉÍ¥ÍÑ•¹Ñ}¥±ÕÑ¥½¸€ôQÉÕ”(€€€€€€€Á•ÉÍ¥ÍÑ•¹Ğ¹M¡…É•}½Õ¹Ñ}¡…¹•|Íe}ÁĞ€ô€Ô¸À(€€€€€€€Á•ÉÍ¥ÍÑ•¹Ğ¹…Á¥Ñ…±}±±½…Ñ¥½¹}M½É”€ô€ĞÀ¸À(€€€€€€€Á•ÉÍ¥ÍÑ•¹Ğ€ô…ÁÁ±å}±½¹}Ñ•Éµ}™É…µ•İ½É¬¡Á•ÉÍ¥ÍÑ•¹Ğ¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ…±Í”¡Á•ÉÍ¥ÍÑ•¹Ğ¹1½¹}Q•Éµ}±¥¥‰±”¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡Á•ÉÍ¥ÍÑ•¹Ğ¹MÕ•ÍÑ•‘}MÑ…ÉÑ•É}]•¥¡Ñ}ÁÑ}Q½Ñ…°°€À¸À¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡Á•ÉÍ¥ÍÑ•¹Ğ¹…Á¥Ñ…±}±±½…Ñ¥½¹}A•¹…±Ñä°€À¸À¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡Á•ÉÍ¥ÍÑ•¹Ğ¹¥±ÕÑ¥½¹}Q½Ñ…±}M½É•}%µÁ…Ğ°€À¸À¤(4(€€€‘•˜Ñ•ÍÑ}‘…Ñ…}½¹™¥‘•¹•}¥Í}…}…Ñ•}¹½Ñ}…¹}•áÑÉ…}Í½É•}™…Ñ½È¡Í•±˜¤è4(€€€€€€€…¹‘¥‘…Ñ”€ôÍ•±˜¹}½½‘}…¹‘¥‘…Ñ”¡Ñ¥­•Èô‰1=]=9ˆ¤4(€€€€€€€…¹‘¥‘…Ñ”¹…Ñ…}½¹™¥‘•¹•}M½É”€ô€ØÀ¸À4(€€€€€€€…¹‘¥‘…Ñ”¹•¥Í¥½¹}MÑ…Ñ”€ô€‰	MQ%8ˆ4(€€€€€€€…¹‘¥‘…Ñ”€ô…ÁÁ±å}±½¹}Ñ•Éµ}™É…µ•İ½É¬¡…¹‘¥‘…Ñ”¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ…±Í”¡…¹‘¥‘…Ñ”¹1½¹}Q•Éµ}±¥¥‰±”¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡…¹‘¥‘…Ñ”¹Y•É‘¥Ğ°€‹šj¯’â7–"“šZß¾òk¢ÎšZg’ş‡–ş’â7¢ÚÏš"[š¢‡–z/’â7¦§R ˆ¤4(4(€€€‘•˜Ñ•ÍÑ}Í¡½ÉÑ±¥ÍÑ}‘•™…Õ±ÑÍ}Ñ½}Í½É•}™¥ÉÍÑ}İ¥Ñ¡½ÕÑ}Í•Ñ½É}…À¡Í•±˜¤è4(€€€€€€€É•ÍÕ±ÑÌ€ômt4(€€€€€€€™½È¥‘à¥¸É…¹” Ô¤è4(€€€€€€€€€€€È€ôÍ•±˜¹}½½‘}…¹‘¥‘…Ñ”¡Ñ¥­•Èõ˜‰Q!í¥‘áôˆ°Í•Ñ½Èô‰Q•¡¹½±½äˆ¤4(€€€€€€€€€€€È¹1½¹}Q•Éµ}±¥¥‰±”€ôQÉÕ”4(€€€€€€€€€€€È¹1½¹}Q•Éµ}M½É”€ô€äÔ¸À€´¥‘à4(€€€€€€€€€€€É•ÍÕ±ÑÌ¹…ÁÁ•¹¡È¤4(€€€€€€€™½È¥‘à¥¸É…¹” Ì¤è4(€€€€€€€€€€€È€ôÍ•±˜¹}½½‘}…¹‘¥‘…Ñ”¡Ñ¥­•Èõ˜‰!1Q!í¥‘áôˆ°Í•Ñ½Èô‰!•…±Ñ¡…É”ˆ¤4(€€€€€€€€€€€È¹1½¹}Q•Éµ}±¥¥‰±”€ôQÉÕ”4(€€€€€€€€€€€È¹1½¹}Q•Éµ}M½É”€ô€àÔ¸À€´¥‘à4(€€€€€€€€€€€É•ÍÕ±ÑÌ¹…ÁÁ•¹¡È¤4(4(€€€€€€€Í¡½ÉÑ±¥ÍĞ€ôÍ•±•Ñ}‘¥Ù•ÉÍ¥™¥•‘}Í¡½ÉÑ±¥ÍĞ¡É•ÍÕ±ÑÌ°Ñ…É•Ñ}Í¥é”ôĞ¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡mÈ¹Q¥­•È™½ÈÈ¥¸Í¡½ÉÑ±¥ÍÑt°l‰Q Àˆ°€‰Q Äˆ°€‰Q Èˆ°€‰Q Ì‰t¤4(4(€€€‘•˜Ñ•ÍÑ}Í¡½ÉÑ±¥ÍÑ}…¹}ÍÑ¥±±}…•ÁÑ}•áÁ±¥¥Ñ}Í•Ñ½É}…À¡Í•±˜¤è4(€€€€€€€É•ÍÕ±ÑÌ€ômt4(€€€€€€€™½È¥‘à¥¸É…¹” Ô¤è4(€€€€€€€€€€€È€ôÍ•±˜¹}½½‘}…¹‘¥‘…Ñ”¡Ñ¥­•Èõ˜‰Q!í¥‘áôˆ°Í•Ñ½Èô‰Q•¡¹½±½äˆ¤4(€€€€€€€€€€€È¹1½¹}Q•Éµ}±¥¥‰±”€ôQÉÕ”4(€€€€€€€€€€€È¹1½¹}Q•Éµ}M½É”€ô€äÔ¸À€´¥‘à4(€€€€€€€€€€€É•ÍÕ±ÑÌ¹…ÁÁ•¹¡È¤4(€€€€€€€™½È¥‘à¥¸É…¹” Ì¤è4(€€€€€€€€€€€È€ôÍ•±˜¹}½½‘}…¹‘¥‘…Ñ”¡Ñ¥­•Èõ˜‰!1Q!í¥‘áôˆ°Í•Ñ½Èô‰!•…±Ñ¡…É”ˆ¤4(€€€€€€€€€€€È¹1½¹}Q•Éµ}±¥¥‰±”€ôQÉÕ”4(€€€€€€€€€€€È¹1½¹}Q•Éµ}M½É”€ô€àÔ¸À€´¥‘à4(€€€€€€€€€€€É•ÍÕ±ÑÌ¹…ÁÁ•¹¡È¤4(4(€€€€€€€Í¡½ÉÑ±¥ÍĞ€ôÍ•±•Ñ}‘¥Ù•ÉÍ¥™¥•‘}Í¡½ÉÑ±¥ÍĞ¡É•ÍÕ±ÑÌ°Ñ…É•Ñ}Í¥é”ôĞ°µ…á}Á•É}Í•Ñ½ÈôÈ¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡±•¸¡Í¡½ÉÑ±¥ÍĞ¤°€Ğ¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ1•ÍÍÅÕ…°¡ÍÕ´¡È¹M•Ñ½È€ôô€‰Q•¡¹½±½äˆ™½ÈÈ¥¸Í¡½ÉÑ±¥ÍĞ¤°€È¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ1•ÍÍÅÕ…°¡ÍÕ´¡È¹M•Ñ½È€ôô€‰!•…±Ñ¡…É”ˆ™½ÈÈ¥¸Í¡½ÉÑ±¥ÍĞ¤°€È¤4(4(4)¥˜}}¹…µ•}|€ôô€‰}}µ…¥¹}|ˆè4(€€€Õ¹¥ÑÑ•ÍĞ¹µ…¥¸ ¤4(