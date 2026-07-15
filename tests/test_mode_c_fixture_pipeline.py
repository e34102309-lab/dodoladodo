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

            self.assertEqual(summary["screen_rows"], 3)
            self.assertEqual(zero_report["summary"]["invalid_zero"], 0)
            self.assertTrue((root / "public" / "index.html").is_file())
            self.assertTrue((root / "mode_c_agent_payload.json").is_file())

            by_ticker = screen.set_index("Ticker")
            self.assertAlmostEqual(
                by_ticker.loc["GLW", "Maintenance_Real_FCF_to_EV_Yield_pct"],
                6.56,
            )
            pgr_meta = json.loads(by_ticker.loc["PGR", "Metric_Metadata_JSON"])
            self.assertEqual(pgr_meta["Real_FCF_Yield_pct"]["status"], "NOT_APPLICABLE")
            self.assertTrue(pd.isna(by_ticker.loc["PGR", "Real_FCF_Yield_pct"]))
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
