import unittest

import pandas as pd

from mode_c_decision_inputs import (
    audit_calibration_input,
    evaluate_portfolio_fit,
)


class DecisionInputContractTests(unittest.TestCase):
    def _calibration_frame(self):
        rows = []
        for index, (ticker, model, decision) in enumerate(
            [
                ("GEN1", "GENERAL_CORPORATE", "2022-03-31"),
                ("GEN2", "GENERAL_CORPORATE", "2023-03-31"),
                ("BANK1", "BANK", "2022-06-30"),
                ("BANK2", "BANK", "2023-06-30"),
            ]
        ):
            decision_at = pd.Timestamp(decision, tz="UTC")
            rows.append(
                {
                    "Ticker": ticker,
                    "Industry_Model_Key": model,
                    "Decision_Timestamp": decision_at,
                    "Available_To_Model_At": decision_at - pd.Timedelta(days=1),
                    "Universe_Membership_AsOf": decision_at - pd.Timedelta(days=1),
                    "Raw_Model_Score": 60.0 + index,
                    "Forward_Return_End": decision_at + pd.Timedelta(days=365),
                    "Forward_Return_Horizon_Days": 365,
                    "Forward_Return_pct": 5.0 + index,
                    "Benchmark_Id": "SPY",
                    "Benchmark_Return_pct": 4.0,
                    "Forward_Excess_Return_pct": 1.0 + index,
                    "Delisted_Securities_Included": True,
                    "Transaction_Costs_Included": True,
                }
            )
        return pd.DataFrame(rows)

    def test_calibration_contract_accepts_only_mature_point_in_time_labels(self):
        result = audit_calibration_input(
            self._calibration_frame(),
            "2025-01-01",
            minimum_observations=4,
            minimum_observations_per_model=2,
            minimum_decision_years=2,
        )
        self.assertTrue(result["eligible_for_model_fitting"])
        self.assertEqual(result["status"], "ELIGIBLE_FOR_MODEL_FITTING")
        self.assertEqual(result["return_horizon_days"], 365)
        self.assertEqual(result["fitting_target"], "Forward_Excess_Return_pct")

    def test_calibration_contract_rejects_feature_lookahead(self):
        frame = self._calibration_frame()
        frame.loc[0, "Available_To_Model_At"] = pd.Timestamp(
            "2022-04-01", tz="UTC"
        )
        result = audit_calibration_input(
            frame,
            "2025-01-01",
            minimum_observations=4,
            minimum_observations_per_model=2,
            minimum_decision_years=2,
        )
        self.assertFalse(result["eligible_for_model_fitting"])
        self.assertTrue(any("leakage" in reason for reason in result["reasons"]))

    def test_calibration_contract_normalizes_ticker_before_duplicate_check(self):
        frame = self._calibration_frame()
        duplicate = frame.iloc[[0]].copy()
        duplicate.loc[:, "Ticker"] = "gen1"
        result = audit_calibration_input(
            pd.concat([frame, duplicate], ignore_index=True),
            "2025-01-01",
            minimum_observations=4,
            minimum_observations_per_model=2,
            minimum_decision_years=2,
        )
        self.assertFalse(result["eligible_for_model_fitting"])
        self.assertTrue(any("duplicate" in reason for reason in result["reasons"]))

    def test_calibration_contract_rejects_mixed_or_unreconciled_return_labels(self):
        frame = self._calibration_frame()
        frame.loc[0, "Forward_Return_Horizon_Days"] = 180
        frame.loc[1, "Forward_Excess_Return_pct"] = 99.0
        result = audit_calibration_input(
            frame,
            "2025-01-01",
            minimum_observations=4,
            minimum_observations_per_model=2,
            minimum_decision_years=2,
        )
        self.assertFalse(result["eligible_for_model_fitting"])
        self.assertTrue(any("horizon" in reason for reason in result["reasons"]))
        self.assertTrue(any("excess return" in reason for reason in result["reasons"]))

    def test_calibration_contract_requires_comparable_benchmark_labels(self):
        mixed = self._calibration_frame()
        mixed.loc[0, "Benchmark_Id"] = "QQQ"
        mixed_result = audit_calibration_input(
            mixed,
            "2025-01-01",
            minimum_observations=4,
            minimum_observations_per_model=2,
            minimum_decision_years=2,
        )
        self.assertFalse(mixed_result["eligible_for_model_fitting"])
        self.assertTrue(
            any("mixes benchmark ids" in reason for reason in mixed_result["reasons"])
        )

        inconsistent = self._calibration_frame()
        for column in (
            "Decision_Timestamp",
            "Available_To_Model_At",
            "Universe_Membership_AsOf",
            "Forward_Return_End",
            "Forward_Return_Horizon_Days",
        ):
            inconsistent.loc[1, column] = inconsistent.loc[0, column]
        inconsistent.loc[1, "Benchmark_Return_pct"] = 9.0
        inconsistent.loc[1, "Forward_Excess_Return_pct"] = (
            inconsistent.loc[1, "Forward_Return_pct"] - 9.0
        )
        inconsistent_result = audit_calibration_input(
            inconsistent,
            "2025-01-01",
            minimum_observations=4,
            minimum_observations_per_model=2,
            minimum_decision_years=2,
        )
        self.assertFalse(inconsistent_result["eligible_for_model_fitting"])
        self.assertTrue(
            any(
                "benchmark returns differ" in reason
                for reason in inconsistent_result["reasons"]
            )
        )

    def _portfolio_exposure(self):
        return {
            "Ticker": "TEST",
            "AsOf": "2026-01-01",
            "Current_Position_Weight_pct_Total": 0.5,
            "ETF_Lookthrough_Weight_pct_Total": 0.8,
            "Active_Sleeve_Weight_pct_Total": 20.0,
            "Active_Sector_Weight_pct_Total": 5.0,
            "Economic_Risk_Bucket": "AI_CAPEX",
            "Economic_Risk_Bucket_Weight_pct_Total": 4.0,
            "ETF_Top10_Overlap": False,
            "Correlation_Stress_Status": "PASS",
        }

    def _evaluate(self, exposure):
        return evaluate_portfolio_fit(
            exposure,
            decision_timestamp="2026-01-10",
            proposed_weight_pct_total=1.5,
            raw_model_score=80.0,
            single_name_limit_pct_total=3.0,
            active_sleeve_limit_pct_total=30.0,
            sector_limit_pct_total=9.0,
            economic_risk_limit_pct_total=9.0,
            etf_top10_min_score=80.0,
        )

    def test_portfolio_fit_passes_only_with_complete_risk_inputs(self):
        result = self._evaluate(self._portfolio_exposure())
        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["pending"])
        self.assertEqual(result["post_trade_position_weight_pct_total"], 2.8)
        self.assertEqual(result["pre_trade_sector_weight_pct_total"], 5.0)
        self.assertFalse(result["etf_top10_overlap"])
        self.assertEqual(result["correlation_stress_status"], "PASS")

    def test_portfolio_fit_blocks_limit_breaches_and_uncleared_correlation(self):
        oversized = self._portfolio_exposure()
        oversized["Current_Position_Weight_pct_Total"] = 2.0
        self.assertEqual(self._evaluate(oversized)["status"], "FAIL")

        pending = self._portfolio_exposure()
        pending["Correlation_Stress_Status"] = "PENDING"
        result = self._evaluate(pending)
        self.assertEqual(result["status"], "PENDING_REVIEW")
        self.assertTrue(result["pending"])

    def test_portfolio_fit_counts_etf_lookthrough_in_single_name_limit(self):
        overlapping = self._portfolio_exposure()
        overlapping["Current_Position_Weight_pct_Total"] = 0.1
        overlapping["ETF_Lookthrough_Weight_pct_Total"] = 1.5
        result = self._evaluate(overlapping)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("look-through", result["reason"])

    def test_missing_portfolio_input_stays_pending(self):
        result = self._evaluate(None)
        self.assertEqual(result["status"], "PENDING_INPUT")
        self.assertTrue(result["pending"])


if __name__ == "__main__":
    unittest.main()
