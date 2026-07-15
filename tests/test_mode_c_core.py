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
    assess_data_confidence,
    build_agent_verification_plan,
    calc_dsi_series,
    calculate_fcf_stability,
    calculate_financial_stress,
    calculate_interest_coverage_gate,
    calculate_per_share_growth_3y,
    common_equity_rejection_reason,
    composite_score_for_result,
    dynamic_implied_cagr_limit,
    estimate_maintenance_capex_amount,
    get_upcoming_earnings,
    historical_valuation,
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
        self.assertIn("軟體/網路/IT服務", check)
        self.assertNotIn("半導體/AI硬體", check)

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
            def _mark_rows_used(cls, rows, role):
                cls.selected_roles.append(role)
                return []

            @staticmethod
            def _record_derived_from_ids(*args, **kwargs):
                return ""

        facts = pd.DataFrame(
            [
                {"end": pd.Timestamp("2024-12-31"), "val": 100e9},
                {"end": pd.Timestamp("2025-12-31"), "val": 80e9},
            ]
        )
        self.assertTrue(math.isnan(_specialized_ppe_capex_proxy(FakeSec(), facts, 10.0)))
        self.assertEqual(FakeSec.selected_roles, [])

    def test_specialized_market_cap_fallback_has_no_general_pipeline_locals(self):
        source = inspect.getsource(run_specialized_mode_c_pipeline)
        self.assertNotIn("tasks.append", source)
        self.assertNotIn("physical_check =", source)

    def test_prefetched_earnings_timestamp_avoids_calendar_request(self):
        now = pd.Timestamp("2026-07-11T00:00:00Z")
        event = pd.Timestamp("2026-07-20T12:00:00Z")
        events = get_upcoming_earnings(
            "TEST",
            {"earningsTimestamp": int(event.timestamp())},
            now=now,
        )
        self.assertEqual(events, ["Earnings: 2026-07-20"])

    def test_debt_components_avoid_double_counting(self):
        consolidated = _compose_total_debt(
            100.0,
            "DebtAndFinanceLeaseObligations",
            debt_current=5.0,
            debt_current_concept="LongTermDebtCurrent",
            commercial_paper=3.0,
            finance_lease=2.0,
        )
        synthesized = _compose_total_debt(
            90.0,
            "LongTermDebtNoncurrent",
            debt_current=4.0,
            debt_current_concept="LongTermDebtCurrent",
            other_short_term=1.0,
            commercial_paper=5.0,
            finance_lease=1.0,
        )
        notes_payable = _compose_total_debt(
            25.0,
            "NotesPayable",
            debt_current=4.0,
            debt_current_concept="LongTermDebtCurrent",
            commercial_paper=0.4,
            finance_lease=0.1,
        )
        lease_in_current_debt = _compose_total_debt(
            90.0,
            "LongTermDebtNoncurrent",
            debt_current=5.0,
            debt_current_concept="LongTermDebtAndFinanceLeaseObligationsCurrent",
            finance_lease=10.0,
        )
        self.assertEqual(consolidated, 100.0)
        self.assertEqual(synthesized, 101.0)
        self.assertAlmostEqual(notes_payable, 25.5)
        self.assertEqual(lease_in_current_debt, 95.0)

    def test_specialized_debt_ignores_stale_broad_short_term_tag(self):
        class FakeSEC:
            decision_timestamp = pd.Timestamp("2026-07-11")
            selected_roles = []

            @staticmethod
            def _instant_facts(frame):
                return SECDataDistiller._instant_facts(frame)

            @classmethod
            def _mark_rows_used(cls, rows, role):
                cls.selected_roles.append(role)
                return []

        def instant(value, end, concept, priority=0):
            return pd.DataFrame(
                [{
                    "val": value * 1e9,
                    "end": pd.Timestamp(end),
                    "concept": concept,
                    "concept_priority": priority,
                    "filed": pd.Timestamp(end),
                }]
            )

        debt, _, sources = _specialized_total_debt(
            FakeSEC(),
            {
                "DebtTotal": instant(90, "2026-03-31", "LongTermDebtNoncurrent"),
                "DebtCurrent": instant(4, "2026-03-31", "LongTermDebtCurrent"),
                "DebtShortTermTotal": instant(99, "2025-06-30", "ShortTermBorrowings"),
                "DebtOtherShortTerm": instant(1, "2026-03-31", "OtherShortTermBorrowings"),
                "DebtCommercialPaper": instant(5, "2026-03-31", "CommercialPaper"),
            },
        )
        self.assertEqual(debt, 100.0)
        self.assertNotIn("DebtShortTermTotal", sources)
        self.assertIn("DebtCommercialPaper", sources)
        self.assertEqual(
            set(FakeSEC.selected_roles),
            {
                "DebtTotal:latest-debt-component",
                "DebtCurrent:latest-debt-component",
                "DebtOtherShortTerm:latest-debt-component",
                "DebtCommercialPaper:latest-debt-component",
            },
        )

    def test_average_balance_uses_year_ago_not_previous_quarter(self):
        facts = pd.DataFrame(
            {
                "end": pd.to_datetime(
                    ["2024-12-31", "2025-03-31", "2025-06-30", "2025-12-31"]
                ),
                "val": [100e9, 110e9, 120e9, 140e9],
            }
        )

        class FakeSEC:
            selected = None

            @staticmethod
            def _instant_facts(frame):
                return frame

            def _mark_rows_used(self, rows, role):
                self.selected = rows

        sec = FakeSEC()
        average = _specialized_average_balance(sec, facts, "Equity")
        self.assertAlmostEqual(average, 120.0)
        self.assertEqual(len(sec.selected), 2)

    def test_balance_growth_rejects_a_multiyear_value_as_year_over_year(self):
        facts = pd.DataFrame(
            [
                {"end": pd.Timestamp("2023-12-31"), "val": 100.0},
                {"end": pd.Timestamp("2025-12-31"), "val": 130.0},
            ]
        )
        value = _specialized_balance_growth_pct(
            SECDataDistiller("research@example.com"),
            facts,
            "PPENet",
        )
        self.assertTrue(math.isnan(value))

    def test_cyclical_ebitda_history_requires_consecutive_annual_periods(self):
        class FakeSEC:
            selected = []

            @staticmethod
            def _annual_facts(frame):
                return frame

            @classmethod
            def _mark_rows_used(cls, row, role):
                cls.selected.append((row["end"], role))

        ends = pd.to_datetime(
            ["2021-12-31", "2023-12-31", "2024-12-31", "2025-12-31"]
        )
        ebit = pd.DataFrame({"end": ends, "val": [1e9, 2e9, 3e9, 4e9]})
        dna = pd.DataFrame({"end": ends, "val": [0.5e9] * 4})
        history = _specialized_ebitda_history(FakeSEC(), ebit, dna)
        self.assertEqual(history, [2.5, 3.5, 4.5])

    def test_share_change_requires_explicit_one_and_three_year_windows(self):
        sec = SECDataDistiller(
            "research@example.com",
            ticker="TEST",
            cik="1",
            decision_timestamp=pd.Timestamp("2026-01-01", tz="UTC"),
        )
        facts = pd.DataFrame(
            [
                {"end": pd.Timestamp("2020-12-31"), "val": 80e6},
                {"end": pd.Timestamp("2022-12-31"), "val": 90e6},
                {"end": pd.Timestamp("2025-12-31"), "val": 100e6},
            ]
        )
        current, one_year, three_year = sec.get_shares_now_1y_3y(facts)
        self.assertEqual(current, 0.1)
        self.assertEqual(one_year, 0.0)
        self.assertEqual(three_year, 0.09)

    def test_fiscal_year_is_normalized_to_integer(self):
        facts = pd.DataFrame(
            [{
                "val": 1.0,
                "end": "2025-12-31",
                "filed": "2026-02-01",
                "fy": 2025.0,
                "fp": "FY",
                "form": "10-K",
            }]
        )
        cleaned = SECDataDistiller._clean_facts(facts)
        self.assertEqual(str(cleaned.loc[0, "fy"]), "2025")

    def test_ytd_selection_uses_latest_period_and_best_duration(self):
        facts = pd.DataFrame(
            [
                {"fy": 2025, "fp": "Q2", "end": pd.Timestamp("2024-06-30"), "duration_days": 181, "val": 90, "filed": pd.Timestamp("2025-08-01"), "concept_priority": 0},
                {"fy": 2025, "fp": "Q2", "end": pd.Timestamp("2025-06-30"), "duration_days": 181, "val": 120, "filed": pd.Timestamp("2025-08-01"), "concept_priority": 0},
                {"fy": 2025, "fp": "Q2", "end": pd.Timestamp("2025-06-30"), "duration_days": 92, "val": 999, "filed": pd.Timestamp("2025-08-01"), "concept_priority": 0},
            ]
        )
        selected = SECDataDistiller._select_ytd(facts, 2025, "Q2")
        self.assertEqual(float(selected["val"]), 120.0)

    def test_annual_history_keys_by_period_end_not_filing_fy(self):
        facts = pd.DataFrame(
            [
                {"fy": 2025, "fp": "FY", "form": "10-K", "end": pd.Timestamp("2024-12-31"), "duration_days": 365, "val": 100, "filed": pd.Timestamp("2026-02-01"), "concept_priority": 0},
                {"fy": 2025, "fp": "FY", "form": "10-K", "end": pd.Timestamp("2025-12-31"), "duration_days": 365, "val": 120, "filed": pd.Timestamp("2026-02-01"), "concept_priority": 0},
            ]
        )
        values = annual_values_by_year(SECDataDistiller("research@example.com"), facts)
        self.assertEqual(values, {2024: 100.0, 2025: 120.0})

    def test_companyfacts_is_cached_and_merges_concept_history(self):
        payload = {
            "facts": {
                "us-gaap": {
                    "Revenues": {"units": {"USD": [{"end": "2023-12-31", "val": 100, "form": "10-K", "fp": "FY", "fy": 2023, "filed": "2024-02-01"}]}},
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [{"end": "2024-12-31", "val": 120, "form": "10-K", "fp": "FY", "fy": 2024, "filed": "2025-02-01"}]}},
                }
            }
        }

        class FakeResponse:
            def json(self):
                return payload

            def __bool__(self):
                return True

        class FakeSession:
            def __init__(self):
                self.calls = 0

            def get(self, url, headers):
                self.calls += 1
                return FakeResponse()

        sec = SECDataDistiller("research@example.com")
        sec.session = FakeSession()
        first = sec.fetch_concept("1", "Revenue")
        second = sec.fetch_concept("1", "Revenue")
        self.assertEqual(sec.session.calls, 1)
        self.assertEqual(set(first["concept"]), {"Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"})
        self.assertEqual(len(second), 2)

    def test_historical_valuation_can_use_first_filing_without_lookahead(self):
        raw = pd.DataFrame(
            [
                {"start": "2024-01-01", "end": "2024-12-31", "val": 100, "form": "10-K", "fp": "FY", "fy": 2024, "filed": "2025-02-01", "accn": "original"},
                {"start": "2024-01-01", "end": "2024-12-31", "val": 110, "form": "10-K", "fp": "FY", "fy": 2024, "filed": "2026-02-01", "accn": "restated"},
            ]
        )
        cleaned = SECDataDistiller._clean_facts(raw)
        first = SECDataDistiller._annual_facts(cleaned, latest_filed=False)
        latest = SECDataDistiller._annual_facts(cleaned, latest_filed=True)
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(float(first.iloc[0]["val"]), 100.0)
        self.assertEqual(float(latest.iloc[0]["val"]), 110.0)

    def test_foreign_annual_filings_are_valid_annual_facts(self):
        rows = []
        for index, form in enumerate(["20-F", "40-F"], start=1):
            rows.append(
                {
                    "start": f"202{index + 2}-01-01",
                    "end": f"202{index + 2}-12-31",
                    "val": index * 1e9,
                    "form": form,
                    "fp": "FY",
                    "fy": 2022 + index,
                    "filed": f"202{index + 3}-03-01",
                    "accn": f"foreign-{index}",
                }
            )
        facts = SECDataDistiller._clean_facts(pd.DataFrame(rows))
        annual = SECDataDistiller._annual_facts(facts)
        self.assertEqual(annual["form"].tolist(), ["20-F", "40-F"])

        value, method, _ = SECDataDistiller("research@example.com").ttm_flow(
            facts,
            normalized_metric="TTM_Revenue",
        )
        self.assertEqual(value, 2.0)
        self.assertIn("40-F", method)
        self.assertIn("fallback annual", method)

    def test_stale_core_facts_are_not_treated_as_current(self):
        required = {
            "Current": pd.DataFrame({"end": [pd.Timestamp("2025-12-31")]}),
            "Stale": pd.DataFrame({"end": [pd.Timestamp("2023-12-31")]}),
        }
        stale = stale_required_fact_names(
            required,
            pd.Timestamp("2026-07-12"),
        )
        self.assertEqual(stale, ["Stale"])

        domestic = pd.DataFrame(
            {"end": [pd.Timestamp("2025-03-31")], "form": ["10-Q"]}
        )
        foreign = pd.DataFrame(
            {"end": [pd.Timestamp("2025-03-31")], "form": ["20-F"]}
        )
        self.assertEqual(
            stale_required_fact_names(
                {"Domestic": domestic, "Foreign": foreign},
                pd.Timestamp("2026-07-12"),
            ),
            ["Domestic"],
        )

    def test_latest_balance_returns_the_selected_concept(self):
        facts = pd.DataFrame(
            [
                {"end": pd.Timestamp("2025-12-31"), "filed": pd.Timestamp("2026-02-01"), "val": 80e9, "concept": "LongTermDebt", "concept_priority": 4},
                {"end": pd.Timestamp("2025-12-31"), "filed": pd.Timestamp("2026-02-01"), "val": 100e9, "concept": "DebtCurrentAndLongTerm", "concept_priority": 0},
            ]
        )
        value, concept = SECDataDistiller("research@example.com").latest_balance_with_concept(facts)
        self.assertEqual(value, 100.0)
        self.assertEqual(concept, "DebtCurrentAndLongTerm")

    def test_only_major_exchange_common_equity_is_accepted(self):
        valid = {
            "quoteType": "EQUITY",
            "exchange": "NMS",
            "sector": "Technology",
            "industry": "Software - Infrastructure",
        }
        self.assertEqual(common_equity_rejection_reason("MSFT", valid), "")
        self.assertEqual(common_equity_rejection_reason("BRK-B", valid), "")
        self.assertIn(
            "非標準普通股代號",
            common_equity_rejection_reason("BAC-PL", valid),
        )
        self.assertIn(
            "非普通股商品",
            common_equity_rejection_reason("SPY", {**valid, "quoteType": "ETF"}),
        )
        self.assertIn(
            "非主要美國交易所",
            common_equity_rejection_reason("ASMLF", {**valid, "exchange": "PNK"}),
        )

    def test_verified_universe_metadata_survives_empty_daily_yahoo_info(self):
        universe = pd.DataFrame(
            [
                {
                    "Ticker": "BRK-B",
                    "CIK": "0001067983",
                    "Status": "Pass",
                    "SECExchange": "NYSE",
                    "Sector": "Financial Services",
                    "Industry": "Insurance - Diversified",
                }
            ]
        )
        with patch("AQR_ModeC_Agent_V12._INFO_CACHE", {"BRK-B": {}}):
            hydrate_info_cache_from_verified_universe(universe)
            info = safe_yf_info("BRK-B")
        self.assertEqual(info["quoteType"], "EQUITY")
        self.assertEqual(info["exchange"], "NYQ")
        self.assertEqual(info["sector"], "Financial Services")
        self.assertTrue(info["_verifiedUniverseMetadataFallback"])
        self.assertIn("sector", info["_verifiedUniverseMetadataFallbackFields"])
        self.assertEqual(common_equity_rejection_reason("BRK-B", info), "")

    def test_industry_routing_abstains_when_specialized_model_is_missing(self):
        self.assertEqual(route_industry_model("", "")["route"], "UNKNOWN")
        self.assertFalse(route_industry_model("Technology", "")["supported"])
        self.assertEqual(route_industry_model("Financial Services", "Banks - Regional")["route"], "BANK")
        self.assertTrue(route_industry_model("Financial Services", "Insurance - Property & Casualty")["supported"])
        self.assertEqual(
            route_industry_model("Financial Services", "Insurance - Property & Casualty")["model_key"],
            "INSURANCE_P_AND_C",
        )
        self.assertEqual(route_industry_model("Real Estate", "REIT - Retail")["route"], "REIT")
        self.assertEqual(route_industry_model("Energy", "Oil & Gas E&P")["route"], "CYCLICAL_MIDCYCLE")
        self.assertTrue(route_industry_model("Technology", "Software - Infrastructure")["supported"])

    def test_low_point_in_time_coverage_causes_abstain(self):
        confidence = assess_data_confidence(
            {"selected_source_count": 10, "accepted_at_ratio": 0.0},
            {"OCF": "TTM=latest 10-K", "Revenue": "TTM=latest 10-K"},
            share_change_1y_available=True,
            share_change_3y_available=True,
            roic_available=True,
            valuation_history_available=True,
            reconciliation_warning=False,
            sbc_sec_evidence_available=True,
            current_shares_sec_evidence_available=True,
        )
        self.assertTrue(confidence["abstain"])
        self.assertLess(confidence["score"], 70.0)

    def test_share_basis_discontinuity_forces_low_confidence(self):
        confidence = assess_data_confidence(
            {"selected_source_count": 10, "accepted_at_ratio": 1.0},
            {"OCF": "TTM=latest 10-K"},
            share_change_1y_available=True,
            share_change_3y_available=True,
            roic_available=True,
            valuation_history_available=True,
            reconciliation_warning=False,
            sbc_sec_evidence_available=True,
            current_shares_sec_evidence_available=True,
            share_basis_discontinuity=True,
        )
        self.assertTrue(confidence["abstain"])
        self.assertLess(confidence["score"], 70.0)

    def test_non_sec_debt_fallback_reduces_confidence(self):
        confidence = assess_data_confidence(
            {
                "selected_source_count": 20,
                "accepted_at_ratio": 1.0,
                "fallback_tag_ratio": 0.0,
                "period_anomaly_count": 0,
            },
            {"OCF": "TTM=latest 10-K", "Revenue": "TTM=latest 10-K"},
            share_change_1y_available=True,
            share_change_3y_available=True,
            roic_available=True,
            valuation_history_available=True,
            reconciliation_warning=False,
            sbc_sec_evidence_available=True,
            current_shares_sec_evidence_available=True,
            debt_sec_evidence_available=False,
        )
        self.assertEqual(confidence["score"], 80.0)
        self.assertFalse(confidence["abstain"])
        self.assertTrue(any("Total debt" in reason for reason in confidence["reasons"]))

        net_cash_confidence = assess_data_confidence(
            {
                "selected_source_count": 20,
                "accepted_at_ratio": 1.0,
                "fallback_tag_ratio": 0.0,
                "period_anomaly_count": 0,
            },
            {"OCF": "TTM=latest 10-K", "Revenue": "TTM=latest 10-K"},
            share_change_1y_available=True,
            share_change_3y_available=True,
            roic_available=True,
            valuation_history_available=True,
            reconciliation_warning=False,
            sbc_sec_evidence_available=True,
            current_shares_sec_evidence_available=True,
            debt_sec_evidence_available=False,
            debt_fully_cash_covered=True,
        )
        self.assertEqual(net_cash_confidence["score"], 90.0)

    def test_tax_rate_assumption_reduces_confidence(self):
        kwargs = {
            "evidence_stats": {
                "selected_source_count": 20,
                "accepted_at_ratio": 1.0,
                "fallback_tag_ratio": 0.0,
                "period_anomaly_count": 0,
            },
            "ttm_methods": {},
            "share_change_1y_available": True,
            "share_change_3y_available": True,
            "roic_available": True,
            "valuation_history_available": True,
            "reconciliation_warning": False,
            "sbc_sec_evidence_available": True,
            "current_shares_sec_evidence_available": True,
        }
        reported = assess_data_confidence(**kwargs)
        assumed = assess_data_confidence(
            **kwargs,
            tax_rate_sec_evidence_available=False,
        )
        self.assertEqual(reported["score"] - assumed["score"], 5.0)
        self.assertTrue(any("tax rate" in reason for reason in assumed["reasons"]))

    def test_cash_interest_proxy_reduces_confidence(self):
        kwargs = {
            "evidence_stats": {
                "selected_source_count": 20,
                "accepted_at_ratio": 1.0,
                "fallback_tag_ratio": 0.0,
                "period_anomaly_count": 0,
            },
            "ttm_methods": {},
            "share_change_1y_available": True,
            "share_change_3y_available": True,
            "roic_available": True,
            "valuation_history_available": True,
            "reconciliation_warning": False,
            "sbc_sec_evidence_available": True,
            "current_shares_sec_evidence_available": True,
        }
        reported = assess_data_confidence(**kwargs)
        cash_proxy = assess_data_confidence(
            **kwargs,
            interest_cash_proxy_used=True,
        )
        self.assertEqual(reported["score"] - cash_proxy["score"], 10.0)
        self.assertTrue(any("cash interest" in reason for reason in cash_proxy["reasons"]))

    def test_zero_valuation_percentile_is_best_not_missing(self):
        cheapest = ModeCResult(
            Ticker="AAA",
            Status="Pass",
            EV_EBITDA_10Y_Percentile=0.0,
            Implied_EBITDA_CAGR_3Y_pct=12.0,
        )
        expensive = ModeCResult(
            Ticker="BBB",
            Status="Pass",
            EV_EBITDA_10Y_Percentile=99.0,
            Implied_EBITDA_CAGR_3Y_pct=12.0,
        )
        self.assertLess(
            composite_score_for_result(cheapest),
            composite_score_for_result(expensive),
        )

    def test_missing_growth_estimate_is_penalized(self):
        missing = ModeCResult(
            Ticker="AAA",
            Status="Pass",
            EV_EBITDA_10Y_Percentile=20.0,
            Implied_EBITDA_CAGR_3Y_pct=math.nan,
        )
        complete = ModeCResult(
            Ticker="BBB",
            Status="Pass",
            EV_EBITDA_10Y_Percentile=20.0,
            Implied_EBITDA_CAGR_3Y_pct=12.0,
        )
        self.assertGreater(
            composite_score_for_result(missing),
            composite_score_for_result(complete),
        )

    def test_maintenance_capex_uses_dna_anchor_and_revenue_growth(self):
        self.assertAlmostEqual(estimate_maintenance_capex_amount(100.0, 60.0, 0.20), 68.0)
        self.assertAlmostEqual(estimate_maintenance_capex_amount(100.0, 60.0, -0.05), 92.0)

    def test_dynamic_cagr_limit_tracks_roic_and_margin_trend(self):
        self.assertEqual(dynamic_implied_cagr_limit(28.0, 1.5), 40.8)
        self.assertEqual(dynamic_implied_cagr_limit(22.0, 0.5), 35.2)
        self.assertEqual(dynamic_implied_cagr_limit(30.0, -2.5), 15.0)

    def test_reverse_valuation_includes_required_return(self):
        self.assertAlmostEqual(implied_ebitda_cagr(100.0, 10.0, 10.0, years=3, required_return=0.10), 0.10)

    def test_financial_stress_recalculates_icr_with_fixed_dna(self):
        healthy = calculate_financial_stress(8.0, 10.0, 2.0, 20.0, 0.0, 5.0, 0.20, 0.30)
        weak = calculate_financial_stress(8.0, 10.0, 4.0, 20.0, 0.0, 5.0, 0.20, 0.30)
        self.assertAlmostEqual(healthy["icr"], 2.5)
        self.assertAlmostEqual(healthy["real_fcf_b"], 2.6)
        self.assertTrue(healthy["survives"])
        self.assertFalse(weak["survives"])

        cash_burn = calculate_financial_stress(
            8.0, 10.0, 2.0, 20.0, 0.0, 1.0, 0.20, 0.30
        )
        net_cash_runway = calculate_financial_stress(
            8.0, 10.0, 0.0, 1.0, 5.0, 1.0, 0.20, 0.30
        )
        self.assertFalse(cash_burn["cash_flow_survives"])
        self.assertFalse(cash_burn["survives"])
        self.assertTrue(net_cash_runway["cash_flow_survives"])
        self.assertTrue(net_cash_runway["survives"])

    def test_net_cash_does_not_require_missing_interest_evidence(self):
        gate = calculate_interest_coverage_gate(1.0, 0.0, 0.10, 1.50)
        stress = calculate_financial_stress(
            1.0, 1.2, 0.0, 0.10, 1.50, 0.8, 0.21, 0.30
        )
        self.assertEqual(gate["mode"], "net_cash")
        self.assertFalse(gate["missing_critical"])
        self.assertTrue(math.isinf(gate["icr"]))
        self.assertTrue(math.isinf(stress["icr"]))
        self.assertTrue(stress["survives"])

        reported_interest = calculate_interest_coverage_gate(1.0, 0.8, 0.10, 1.50)
        self.assertEqual(reported_interest["mode"], "net_cash")
        self.assertTrue(math.isinf(reported_interest["icr"]))

    def test_inventory_inflection_requires_seasonal_confirmation(self):
        idx = pd.date_range("2024-03-31", periods=6, freq="QE")
        confirmed = analyze_dsi_signal(pd.Series([100, 90, 110, 95, 80, 70], index=idx))
        seasonal_only = analyze_dsi_signal(pd.Series([100, 60, 110, 95, 80, 70], index=idx))
        self.assertTrue(confirmed["inflection"])
        self.assertFalse(seasonal_only["inflection"])
        unseasoned = analyze_dsi_signal(pd.Series([95, 80, 70], index=idx[-3:]))
        self.assertFalse(unseasoned["inflection"])

    def test_dsi_uses_average_inventory_over_the_ttm_period(self):
        inventory = pd.DataFrame(
            {
                "end": pd.to_datetime(["2024-12-31", "2025-12-31"]),
                "val": [80.0, 100.0],
            }
        )
        cogs = pd.Series(
            [100.0, 100.0, 100.0, 100.0],
            index=pd.to_datetime(
                ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]
            ),
        )
        dsi = calc_dsi_series(inventory, cogs)
        self.assertEqual(len(dsi), 1)
        self.assertAlmostEqual(float(dsi.iloc[-1]), 82.125)

    def test_fcf_history_requires_sbc_evidence_and_consecutive_ocf_years(self):
        ocf = {2021: 4e9, 2023: 3e9, 2025: 1e9}
        capex = {2021: 1e9, 2023: 1e9, 2025: 0.2e9}
        sbc = {2025: 0.1e9}
        dna = {2021: 0.5e9, 2023: 0.5e9, 2025: 0.5e9}
        revenue = {2021: 8e9, 2023: 9e9, 2025: 10e9}
        net_income = {2021: 1e9, 2023: 1e9, 2025: 1e9}
        with patch(
            "AQR_ModeC_Agent_V12.annual_values_by_year",
            side_effect=[ocf, capex, sbc, dna, revenue, net_income],
        ):
            result = calculate_fcf_stability(
                SECDataDistiller("research@example.com"),
                *[pd.DataFrame()] * 6,
            )
        self.assertEqual(result["years_available"], 1.0)
        self.assertEqual(result["positive_years"], 1.0)
        self.assertEqual(result["ocf_3y_years"], 1.0)
        self.assertEqual(result["ocf_3y_cumulative_b"], 1.0)

    def test_short_interest_age_is_explicit(self):
        now = pd.Timestamp("2026-07-10", tz="UTC")
        observed = pd.Timestamp("2026-06-30", tz="UTC").timestamp()
        self.assertAlmostEqual(short_interest_data_age_days({"dateShortInterest": observed}, now=now), 10.0)

    def _good_candidate(self, ticker="GOOD", sector="Technology"):
        return ModeCResult(
            Ticker=ticker,
            Status="Pass",
            Sector=sector,
            Real_FCF_Yield_pct=8.0,
            ICR=10.0,
            Share_Count_Change_pct=-1.0,
            Share_Count_Change_3Y_pct=-3.0,
            Dilution_Illusion=False,
            Persistent_Dilution=False,
            ROIC_pct=18.0,
            ROCE_pct=22.0,
            OCF_3Y_Cumulative_B=5.0,
            OCF_3Y_Years=3.0,
            Real_FCF_Positive_Years_5Y=5.0,
            Real_FCF_Years_Available=5.0,
            Real_FCF_Margin_Std_5Y_pct=3.0,
            OCF_to_NetIncome_5Y=1.1,
            Real_FCF_to_NetIncome_5Y=0.8,
            Capital_Allocation_Score=85.0,
            EV_EBITDA_10Y_Percentile=10.0,
            EBITDA_Drawdown_30_pct=-20.0,
            Stress_ICR_30x=5.0,
            NetDebt_to_Stress_EBITDA_30x=1.0,
            Stress_Survival_30=True,
            GM_Diagnosis="中性：三季趨勢未給出明確逆風訊號",
            Implied_EBITDA_CAGR_3Y_pct=10.0,
            Momentum_12M_pct=15.0,
            Data_Quality_Flags="OK",
        )

    def test_quality_and_value_raise_long_term_score(self):
        good = apply_long_term_framework(self._good_candidate())
        weak = self._good_candidate(ticker="WEAK")
        weak.Real_FCF_Yield_pct = 1.0
        weak.ICR = 1.5
        weak.Share_Count_Change_pct = 3.0
        weak.EV_EBITDA_10Y_Percentile = 85.0
        weak.EBITDA_Drawdown_30_pct = -65.0
        weak.GM_Diagnosis = "結構性價值陷阱：營收未崩但毛利連續失血"
        weak = apply_long_term_framework(weak)
        self.assertGreater(good.Long_Term_Score, weak.Long_Term_Score)
        self.assertTrue(good.Long_Term_Eligible)
        self.assertFalse(weak.Long_Term_Eligible)
        self.assertEqual(good.Suggested_Starter_Weight_pct_Total, STARTER_WEIGHT_PCT_TOTAL)

    def test_three_or_four_year_fcf_history_requires_sixty_percent_positive(self):
        candidate = self._good_candidate(ticker="MIXEDFCF")
        candidate.Real_FCF_Years_Available = 4.0
        candidate.Real_FCF_Positive_Years_5Y = 2.0
        candidate = apply_long_term_framework(candidate)
        self.assertEqual(minimum_positive_fcf_years(4.0), 3)
        self.assertFalse(candidate.Long_Term_Eligible)

    def test_single_year_dilution_warns_but_persistent_dilution_excludes(self):
        clean = apply_long_term_framework(self._good_candidate())

        warning = self._good_candidate(ticker="WARN")
        warning.Dilution_Illusion = True
        warning = apply_long_term_framework(warning)
        self.assertTrue(warning.Long_Term_Eligible)
        self.assertLess(warning.Long_Term_Score, clean.Long_Term_Score)

        persistent = self._good_candidate(ticker="DILUTE")
        persistent.Persistent_Dilution = True
        persistent.Share_Count_Change_3Y_pct = 5.0
        persistent = apply_long_term_framework(persistent)
        self.assertFalse(persistent.Long_Term_Eligible)
        self.assertEqual(persistent.Suggested_Starter_Weight_pct_Total, 0.0)

    def test_data_confidence_is_a_gate_not_an_extra_score_factor(self):
        candidate = self._good_candidate(ticker="LOWCONF")
        candidate.Data_Confidence_Score = 60.0
        candidate.Decision_State = "ABSTAIN"
        candidate = apply_long_term_framework(candidate)
        self.assertFalse(candidate.Long_Term_Eligible)
        self.assertEqual(candidate.Verdict, "暫不判斷：資料信心不足或模型不適用")

    def test_shortlist_defaults_to_score_first_without_sector_cap(self):
        results = []
        for idx in range(5):
            r = self._good_candidate(ticker=f"TECH{idx}", sector="Technology")
            r.Long_Term_Eligible = True
            r.Long_Term_Score = 95.0 - idx
            results.append(r)
        for idx in range(3):
            r = self._good_candidate(ticker=f"HLTH{idx}", sector="Healthcare")
            r.Long_Term_Eligible = True
            r.Long_Term_Score = 85.0 - idx
            results.append(r)

        shortlist = select_diversified_shortlist(results, target_size=4)
        self.assertEqual([r.Ticker for r in shortlist], ["TECH0", "TECH1", "TECH2", "TECH3"])

    def test_shortlist_can_still_accept_explicit_sector_cap(self):
        results = []
        for idx in range(5):
            r = self._good_candidate(ticker=f"TECH{idx}", sector="Technology")
            r.Long_Term_Eligible = True
            r.Long_Term_Score = 95.0 - idx
            results.append(r)
        for idx in range(3):
            r = self._good_candidate(ticker=f"HLTH{idx}", sector="Healthcare")
            r.Long_Term_Eligible = True
            r.Long_Term_Score = 85.0 - idx
            results.append(r)

        shortlist = select_diversified_shortlist(results, target_size=4, max_per_sector=2)
        self.assertEqual(len(shortlist), 4)
        self.assertLessEqual(sum(r.Sector == "Technology" for r in shortlist), 2)
        self.assertLessEqual(sum(r.Sector == "Healthcare" for r in shortlist), 2)


if __name__ == "__main__":
    unittest.main()
