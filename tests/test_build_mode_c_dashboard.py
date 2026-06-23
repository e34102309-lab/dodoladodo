import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from build_mode_c_dashboard import build_dashboard


class DashboardTests(unittest.TestCase):
    def test_builds_dashboard_with_watchlist_sec_links_and_theme_radar(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pd.DataFrame(
                [
                    {
                        "Ticker": "APH",
                        "Status": "Pass",
                        "Long_Term_Score": 82.5,
                        "Long_Term_Eligible": True,
                        "Verdict": "優先研究",
                        "Sector": "Technology",
                        "Industry": "Electronic Components",
                        "Real_FCF_Yield_pct": 3.5,
                    },
                    {"Ticker": "TEST", "Status": "Fail", "Long_Term_Score": float("nan")},
                ]
            ).to_csv(root / "screen.csv", index=False)
            pd.DataFrame([{"Ticker": "APH"}]).to_csv(root / "shortlist.csv", index=False)
            pd.DataFrame([{"Ticker": "APH", "CIK": "820313"}]).to_csv(root / "universe.csv", index=False)

            index = build_dashboard(
                root / "screen.csv",
                root / "shortlist.csv",
                root / "universe.csv",
                root / "public",
            )

            html = index.read_text(encoding="utf-8")
            self.assertIn("Alpha Engine 長期價值研究台", html)
            self.assertIn("主題擴散鏈", html)
            self.assertIn("AI 晶片二階受益鏈", html)
            self.assertIn("二階：瓶頸零組件與設備", html)
            self.assertIn("localStorage", html)
            self.assertIn("複製 AI 研究提示", html)
            self.assertIn("二階受益是否已開始進財報", html)
            self.assertIn('"CIK":"0000820313"', html)
            self.assertNotIn("Gemini API", html)
            self.assertTrue((root / "public" / ".nojekyll").exists())

            data = json.loads((root / "public" / "data.json").read_text(encoding="utf-8"))
            self.assertEqual(data["stats"]["total"], 2)
            self.assertEqual(data["stats"]["shortlist"], 1)
            self.assertIsNone(data["stocks"][1]["Long_Term_Score"])
            self.assertIn("themes", data)
            self.assertIn("ai_chip_second_order", data["stocks"][0]["Theme_Ids"])
            self.assertIn("AI 晶片二階受益鏈", data["stocks"][0]["Theme_Tags"])
            self.assertEqual(
                data["stocks"][0]["Theme_Layer_Map"]["ai_chip_second_order"],
                "二階：瓶頸零組件與設備",
            )
            ai_theme = next(theme for theme in data["themes"] if theme["id"] == "ai_chip_second_order")
            second_layer = next(layer for layer in ai_theme["layers"] if layer["name"] == "二階：瓶頸零組件與設備")
            self.assertIn("APH", second_layer["top"])


if __name__ == "__main__":
    unittest.main()
