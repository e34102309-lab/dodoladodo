import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from build_mode_c_dashboard import build_dashboard
from enhance_dashboard_ui import enhance_dashboard


class DashboardTests(unittest.TestCase):
    def test_builds_dashboard_with_watchlist_sec_links_theme_radar_and_emerging_candidates(self):
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
                        "Quality_Score": 81.0,
                        "Rev_3Q_Change_pct": 12.0,
                        "GM_3Q_Change_pp": 2.0,
                        "Real_FCF_Yield_pct": 3.5,
                    },
                    {
                        "Ticker": "TEL",
                        "Status": "Pass",
                        "Long_Term_Score": 78.0,
                        "Long_Term_Eligible": True,
                        "Verdict": "研究候選",
                        "Sector": "Technology",
                        "Industry": "Electronic Components",
                        "Quality_Score": 76.0,
                        "Rev_3Q_Change_pct": 10.0,
                        "GM_3Q_Change_pp": 1.5,
                        "Real_FCF_Yield_pct": 3.2,
                    },
                    {
                        "Ticker": "VSH",
                        "Status": "Pass",
                        "Long_Term_Score": 71.0,
                        "Long_Term_Eligible": True,
                        "Verdict": "研究候選",
                        "Sector": "Technology",
                        "Industry": "Electronic Components",
                        "Quality_Score": 72.0,
                        "Rev_3Q_Change_pct": 9.0,
                        "GM_3Q_Change_pp": 1.2,
                        "Real_FCF_Yield_pct": 4.1,
                    },
                    {"Ticker": "TEST", "Status": "Fail", "Long_Term_Score": float("nan")},
                ]
            ).to_csv(root / "screen.csv", index=False)
            pd.DataFrame([{"Ticker": "APH"}]).to_csv(root / "shortlist.csv", index=False)
            pd.DataFrame(
                [
                    {"Ticker": "APH", "CIK": "820313"},
                    {"Ticker": "TEL", "CIK": "1385157"},
                    {"Ticker": "VSH", "CIK": "103730"},
                ]
            ).to_csv(root / "universe.csv", index=False)

            index = build_dashboard(
                root / "screen.csv",
                root / "shortlist.csv",
                root / "universe.csv",
                root / "public",
            )
            self.assertTrue(enhance_dashboard(index))
            self.assertFalse(enhance_dashboard(index))

            html = index.read_text(encoding="utf-8")
            self.assertIn("Alpha Engine 長期價值研究台", html)
            self.assertIn("主題擴散鏈", html)
            self.assertIn("候選風口偵測", html)
            self.assertIn("它會自動偵測什麼？", html)
            self.assertIn("它不會自動做什麼？", html)
            self.assertIn("點我 → 下面只看這群股票", html)
            self.assertIn("data-candidate", html)
            self.assertIn("clearCandidate", html)
            self.assertIn("AI 晶片二階受益鏈", html)
            self.assertIn("二階：瓶頸零組件與設備", html)
            self.assertIn("待人工確認", html)
            self.assertIn("localStorage", html)
            self.assertIn("複製 AI 研究提示", html)
            self.assertIn("二階受益是否已開始進財報", html)
            self.assertIn('"CIK":"0000820313"', html)
            self.assertNotIn("Gemini API", html)
            self.assertTrue((root / "public" / ".nojekyll").exists())

            data = json.loads((root / "public" / "data.json").read_text(encoding="utf-8"))
            self.assertEqual(data["stats"]["total"], 4)
            self.assertEqual(data["stats"]["shortlist"], 1)
            self.assertIsNone(data["stocks"][3]["Long_Term_Score"])
            self.assertEqual(data["trend_baseline"]["status"], "建立基準中")
            self.assertIn("emerging_candidates", data)
            self.assertTrue(data["emerging_candidates"])
            self.assertTrue(
                any(candidate["status"] == "待人工確認" for candidate in data["emerging_candidates"])
            )
            self.assertTrue(
                any("Electronic Components" in candidate["name"] for candidate in data["emerging_candidates"])
            )
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
            self.assertIn("TEL", second_layer["top"])
            self.assertIn("VSH", second_layer["top"])


if __name__ == "__main__":
    unittest.main()
