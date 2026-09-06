import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from mode_c_fixture_pipeline import build_fixture_pipeline


class ModeCFixturePipelineTests(unittest.TestCase):
    def test_fixture_pipeline_builds_and_validates_all_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            summary = build_fixture_pipeline(root)
            screen = pd.read_csv(root / "mode_c_screen.csv", encoding="utf-8-sig")
            dashboard = json.loads(
                (root / "public" / "data.json").read_text(encoding="utf-8")
            )
            zero_report = json.loads(
                (root / "mode_c_zero_audit.json").read_text(encoding="utf-8")
            )

            self.assertEqual(summary["screen_rows"], 5)
            self.assertEqual(zero_report["summary"]["invalid_zero"], 0)
            self.assertTrue((root / "public" / "index.html").is_file())
            self.assertTrue((root / "mode_c_agent_payload.json").is_file())
            self.assertTrue((root / "mode_c_shortlist_by_model.csv").is_file())

            by_ticker = screen.set_index("Ticker")
            self.assertAlmostEqual(
                by_ticker.loc["GLW", "Maintenance_Real_FCF_to_EV_Yield_pct"],
                6.56,
            )
            pgr_meta = json.loads(by_ticker.loc["PGR", "Metric_Metadata_JSON"])
            self.assertEqual(pgr_meta["Real_FCF_Yield_pct"]["status"], "NOT_APPLICABLE")
            self.assertTrue(pd.isna(by_ticker.loc["PGR", "Real_FCF_Yield_pct"]))
            self.assertEqual(
                by_ticker.loc["PGR", "Combined_Ratio_Source_Status"],
                "COMPANY_REPORTED",
            )
            pgr_metrics = json.loads(by_ticker.loc["PGR", "Industry_Model_Metrics_JSON"])
            self.assertEqual(pgr_metrics["monthly_combined_ratio_pct"], 84.0)
            self.assertEqual(pgr_metrics["quarterly_combined_ratio_pct"], 92.0)
            self.assertEqual(pgr_metrics["combined_ratio_for_model_pct"], 92.0)
            self.assertAlmostEqual(by_ticker.loc["PGR", "Industry_Model_Score"], 84.1)
            acgl_meta = json.loads(by_ticker.loc["ACGL", "Metric_Metadata_JSON"])
            self.assertEqual(
                acgl_meta["SEC_Combined_Ratio_Proxy"]["status"], "ESTIMATED"
            )
            self.assertAlmostEqual(
                by_ticker.loc["ACGL", "Company_Reported_Combined_Ratio"], 86.0
            )
            self.assertAlmostEqual(
                by_ticker.loc["ACGL", "SEC_Combined_Ratio_Proxy"], 85.2
            )
            self.assertFalse(by_ticker.loc["ACGL", "Human_KPI_Review_Required"])
            self.assertFalse(by_ticker.loc["ACGL", "Starter_Candidate"])
            self.assertEqual(by_ticker.loc["GLW", "Research_Priority_Rank"], 1)
            self.assertEqual(by_ticker.loc["ACGL", "Research_Priority_Rank"], 2)
            self.assertEqual(by_ticker.loc["ACGL", "Research_Priority_Round"], 1)
            self.assertEqual(
                by_ticker.loc["ACGL", "Prior_Year_Reserve_Development_pct"],
                -1.5,
            )
            self.assertEqual(by_ticker.loc["ACGL", "Catastrophe_Loss_Ratio_pct"], 3.2)
            self.assertLess(by_ticker.loc["KNSL", "Industry_Model_Score"], 90.0)
            self.assertEqual(by_ticker.loc["KNSL", "P_and_C_Stress_Status"], "FAIL")
            self.assertFalse(by_ticker.loc["KNSL", "Long_Term_Eligible"])
            self.assertEqual(by_ticker.loc["KNSL", "Decision_State"], "FAIL")
            self.assertFalse(by_ticker.loc["KNSL", "Specialized_Stress_Pending"])
            self.assertTrue(by_ticker.loc["KNSL", "Specialized_Stress_Failed"])
            self.assertEqual(
                by_ticker.loc["KNSL", "Research_Action_State"],
                "SPECIALIZED_STRESS_FAILED",
            )
            self.assertFalse(by_ticker.loc["KNSL", "Starter_Candidate"])
            ifnny_meta = json.loads(by_ticker.loc["IFNNY", "Metric_Metadata_JSON"])
            self.assertEqual(ifnny_meta["Point_in_Time_FX_Rate"]["status"], "ABSTAIN")
            self.assertEqual(ifnny_meta["ADR_Ratio"]["status"], "ABSTAIN")

            dashboard_by_ticker = {row["Ticker"]: row for row in dashboard["stocks"]}
            self.assertEqual(
                dashboard_by_ticker["PGR"]["Metric_Metadata"]["EV_EBITDA_x"]["status"],
                "NOT_APPLICABLE",
            )


if __name__ == "__main__":
    unittest.main()
