import math
import time
import unittest
from unittest.mock import Mock, patch

import pandas as pd

import AQR_ModeC_Agent_V12 as engine


class DataAndFormulaAuditTests(unittest.TestCase):
    def test_pretax_income_is_not_used_as_operating_ebit(self):
        sec = engine.SECDataDistiller("test@example.com")
        pretax_tag = "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"
        sec._companyfacts_cache["0000000001"] = {"us-gaap": {pretax_tag: {"units": {"USD": [
            {"start": "2025-01-01", "end": "2025-12-31", "val": 6e9,
             "filed": "2026-02-01", "fy": 2025, "fp": "FY", "form": "10-K"}
        ]}}}}
        self.assertTrue(sec.fetch_concept("1", "EBIT").empty)
        self.assertEqual(sec.fetch_concept("1", "PretaxIncome")["val"].tolist(), [6e9])

    def test_current_only_tax_is_not_used_as_total_income_tax(self):
        sec = engine.SECDataDistiller("test@example.com")
        sec._companyfacts_cache["0000000001"] = {"us-gaap": {"CurrentIncomeTaxExpenseBenefit": {"units": {"USD": [
            {"start": "2025-01-01", "end": "2025-12-31", "val": 1e9,
             "filed": "2026-02-01", "fy": 2025, "fp": "FY", "form": "10-K"}
        ]}}}}
        self.assertTrue(sec.fetch_concept("1", "IncomeTaxExpenseBenefit").empty)

    def test_sec_rejects_nonfinite_source_facts_before_registering_evidence(self):
        raw = pd.DataFrame([
            {"end": "2025-12-31", "val": value, "filed": "2026-02-01", "accn": str(index)}
            for index, value in enumerate([10.0, math.inf, -math.inf, math.nan])
        ])
        self.assertEqual(engine.SECDataDistiller._clean_facts(raw)["val"].tolist(), [10.0])

    def test_current_market_shares_take_priority_over_extrapolated_cache(self):
        sec = engine.SECDataDistiller("test@example.com")
        cache = {"TEST": {"shares": 0.1, "drift": 0.2, "timestamp": time.time() - 365 * 86400}}
        with patch.object(engine, "_VECTOR_CACHE", cache):
            value, source = engine.get_robust_shares("TEST", pd.DataFrame(), sec, {"sharesOutstanding": 200e6})
        self.assertEqual(value, 0.2)
        self.assertEqual(source, "YAHOO_FINANCE")

    def test_cache_cannot_fabricate_current_shares_when_reported_inputs_are_missing(self):
        sec = engine.SECDataDistiller("test@example.com")
        cache = {"TEST": {"shares": 0.1, "drift": 0.2, "timestamp": time.time() - 365 * 86400}}
        with patch.object(engine, "_VECTOR_CACHE", cache):
            value, source = engine.get_robust_shares("TEST", pd.DataFrame(), sec, {})
        self.assertEqual((value, source), (0.0, "MISSING"))

    def test_invalid_share_values_do_not_become_market_cap_inputs(self):
        sec = engine.SECDataDistiller("test@example.com")
        with patch.object(engine, "_VECTOR_CACHE", {}):
            for value in (math.inf, math.nan, -1, "bad"):
                with self.subTest(value=value):
                    self.assertEqual(
                        engine.get_robust_shares("TEST", pd.DataFrame(), sec, {"sharesOutstanding": value}),
                        (0.0, "MISSING"),
                    )

    def test_http_adapter_does_not_retry_outside_sec_rate_limiter(self):
        session = engine.create_retry_session()
        try:
            self.assertEqual(session.get_adapter("https://data.sec.gov").max_retries.total, 0)
        finally:
            session.close()

    def test_sec_transient_retries_each_pass_the_rate_limiter(self):
        session = engine.RateLimitedSession()
        unavailable = Mock(status_code=503)
        success = Mock(status_code=200)
        http = Mock()
        http.get.side_effect = [unavailable, unavailable, success]
        with patch.object(session, "_session", return_value=http), \
             patch.object(session, "_wait_for_capacity") as wait, \
             patch.object(engine.time, "sleep"):
            self.assertIs(session.get("https://data.sec.gov/test", {}), success)
        self.assertEqual(http.get.call_count, 3)
        self.assertEqual(wait.call_count, 3)

    def test_sec_permanent_denial_stops_instead_of_repeating_requests(self):
        session = engine.RateLimitedSession()
        http = Mock()
        http.get.return_value = Mock(status_code=403)
        with patch.object(session, "_session", return_value=http), \
             patch.object(session, "_wait_for_capacity"), \
             patch.object(engine.time, "sleep"):
            self.assertIsNone(session.get("https://data.sec.gov/test", {}))
        self.assertEqual(http.get.call_count, 1)

    def test_liquidity_requires_complete_finite_recent_price_and_volume_pairs(self):
        dates = pd.bdate_range("2025-01-01", periods=220)
        for bad in (math.nan, math.inf, -1.0):
            close = pd.Series(100.0, index=dates)
            volume = pd.Series(1e6, index=dates)
            volume.iloc[-1] = bad
            with self.subTest(volume=bad), patch.object(engine, "get_cached_series", side_effect=[close, volume]):
                self.assertIsNone(engine.fetch_price_metrics("TEST"))
        with patch.object(engine, "get_cached_series", side_effect=[
            pd.Series(100.0, index=dates), pd.Series(1e6, index=dates), None
        ]):
            metrics = engine.fetch_price_metrics("TEST")
        self.assertEqual(metrics["dollar_volume"], 100e6)

    def test_old_stock_prices_cannot_pass_as_current_against_this_runs_market_reference(self):
        dates = pd.bdate_range("2025-01-01", periods=220)
        close = pd.Series(100.0, index=dates)
        volume = pd.Series(1e6, index=dates)
        market_dates = pd.bdate_range("2025-01-01", periods=221)
        benchmark = pd.Series(500.0, index=market_dates)
        with patch.object(engine, "get_cached_series", side_effect=[close, volume, benchmark]):
            self.assertIsNone(engine.fetch_price_metrics("TEST"))

    def test_ttm_reports_the_actual_selected_period_end_including_annual_fallback(self):
        sec = engine.SECDataDistiller("test@example.com")
        facts = engine.SECDataDistiller._clean_facts(pd.DataFrame([
            {"start": "2024-01-01", "end": "2024-12-31", "val": 10e9,
             "filed": "2025-02-01", "fy": 2024, "fp": "FY", "form": "10-K"},
            {"start": "2025-04-01", "end": "2025-06-30", "val": 3e9,
             "filed": "2025-08-01", "fy": 2025, "fp": "Q2", "form": "10-Q"},
        ]))
        value, method, details = sec.ttm_flow(facts)
        self.assertEqual(value, 10.0)
        self.assertTrue(method.startswith("fallback annual:"))
        self.assertEqual(details["period_end"], "2024-12-31")

    def test_ttm_comparison_rejects_different_periods_and_stale_selected_fallbacks(self):
        aligned = {name: {"period_end": "2026-06-30"} for name in ("OCF", "CapEx", "Revenue")}
        self.assertEqual(engine.assess_ttm_period_alignment(aligned, pd.Timestamp("2026-10-24"), 240),
                         {"mismatched": [], "stale": []})
        aligned["OCF"]["period_end"] = "2025-12-31"
        check = engine.assess_ttm_period_alignment(aligned, pd.Timestamp("2026-10-24"), 240)
        self.assertEqual(check, {"mismatched": ["OCF"], "stale": ["OCF"]})

    def _run_stubbed_core(self, period_end):
        raw = pd.DataFrame({"end": [pd.Timestamp("2026-09-30")], "form": ["10-Q"]})
        sec = Mock()
        sec.fetch_concept.return_value = raw
        sec.fetch_shares_outstanding.return_value = pd.DataFrame()
        sec.get_shares_now_1y_3y.return_value = (0.0, 0.0, 0.0)
        sec.ttm_flow.side_effect = lambda frame, **kw: (
            1.0, "test",
            {"evidence_id": "test-evidence", "period_end": period_end(kw.get("normalized_metric", ""))},
        )
        parse_timestamp = engine.SECDataDistiller._utc_naive_timestamp
        with patch.object(engine, "SECDataDistiller") as constructor, \
             patch.object(engine, "fetch_price_metrics", return_value={"dollar_volume": 1e9, "last_close": 100.0}), \
             patch.object(engine, "safe_yf_info", return_value={
                 "quoteType": "EQUITY", "exchange": "NMS", "sector": "Technology", "industry": "Software - Infrastructure"
             }), \
             patch.object(engine, "calculate_fcf_stability", return_value={}), \
             patch.object(engine, "calculate_per_share_growth_3y", return_value={}), \
             patch.object(engine, "get_robust_shares", return_value=(0.0, "MISSING")):
            constructor._utc_naive_timestamp.side_effect = parse_timestamp
            constructor.return_value = sec
            return engine._run_mode_c_pipeline_core("TEST", "1", "test@example.com", pd.Timestamp("2026-10-24"))

    def test_core_stops_before_formulas_can_mix_annual_ocf_with_quarterly_ttm(self):
        result = self._run_stubbed_core(lambda metric: "2025-12-31" if metric == "TTM_OCF" else "2026-09-30")
        self.assertEqual(result.Decision_State, "ABSTAIN")
        self.assertIn("期間不一致", result.Status)

    def test_core_checks_selected_ttm_age_instead_of_raw_frames_latest_end(self):
        result = self._run_stubbed_core(lambda metric: "2025-12-31")
        self.assertEqual(result.Decision_State, "ABSTAIN")
        self.assertIn("期間過舊", result.Status)

    def test_unknown_market_cap_is_missing_data_rather_than_a_hard_financial_failure(self):
        result = self._run_stubbed_core(lambda metric: "2026-09-30")
        self.assertEqual(result.Decision_State, "ABSTAIN")
        self.assertIn("市值資料缺失", result.Status)
        result = engine.enforce_decision_semantics(result)
        self.assertEqual(result.Decision_Reason_Code, "MISSING_MARKET_DATA")


if __name__ == "__main__":
    unittest.main()
