import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from build_mode_c_dashboard import (
    DASHBOARD_TREND_POLICY_VERSION,
    build_dashboard,
    build_emerging_candidates,
    build_payload,
    group_snapshot,
    metric_summary,
)
from enhance_dashboard_ui import enhance_dashboard
from mode_c_research_priority import RESEARCH_PRIORITY_VERSION


class DashboardTests(unittest.TestCase):
    def test_builds_dashboard_with_watchlist_sec_links_theme_radar_and_emerging_candidates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            screen = pd.DataFrame(
                [
                    {
                        "Ticker": "APH",
                        "Status": "Pass",
                        "Long_Term_Score": 82.5,
                        "Raw_Model_Score": 82.5,
                        "Within_Model_Percentile": 100.0,
                        "Shrunk_Within_Model_Percentile": 56.52,
                        "Research_Priority_Rank": 1,
                        "Data_Confidence_Score": 90.0,
                        "Factor_Coverage": 1.0,
                        "Research_Action_State": "GLOBAL_RESEARCH_QUEUE",
                        "Decision_Timestamp": "2026-07-15T00:00:00+00:00",
                        "Price_Data_Date": "2026-07-14",
                        "Latest_SEC_Availability_Date": "2026-07-13T20:00:00+00:00",
                        "Universe_Version": "test-universe",
                        "Git_Commit": "test-commit",
                        "Long_Term_Eligible": True,
                        "Verdict": "優先研究",
                        "Sector": "Technology",
                        "Industry": "Electronic Components",
                        "Quality_Score": 81.0,
                        "Rev_3Q_Change_pct": 12.0,
                        "GM_3Q_Change_pp": 2.0,
                        "Real_FCF_Yield_pct": 3.5,
                        "Debt_Source_Method": "SEC component composition",
                        "ICR_Method": "net_cash",
                    },
                    {
                        "Ticker": "TEL",
                        "Status": "Pass",
                        "Long_Term_Score": 78.0,
                        "Raw_Model_Score": 78.0,
                        "Within_Model_Percentile": 66.67,
                        "Shrunk_Within_Model_Percentile": 52.17,
                        "Research_Priority_Rank": 2,
                        "Data_Confidence_Score": 85.0,
                        "Factor_Coverage": 0.9,
                        "Research_Action_State": "MODEL_ELIGIBLE",
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
                        "Raw_Model_Score": 71.0,
                        "Within_Model_Percentile": 33.33,
                        "Shrunk_Within_Model_Percentile": 47.83,
                        "Research_Priority_Rank": 3,
                        "Data_Confidence_Score": 80.0,
                        "Factor_Coverage": 0.8,
                        "Research_Action_State": "MODEL_ELIGIBLE",
                        "Long_Term_Eligible": True,
                        "Verdict": "研究候選",
                        "Sector": "Technology",
                        "Industry": "Electronic Components",
                        "Quality_Score": 72.0,
                        "Rev_3Q_Change_pct": 9.0,
                        "GM_3Q_Change_pp": 1.2,
                        "Real_FCF_Yield_pct": 4.1,
                    },
                    {"Ticker": "TEST", "Status": "Abstain: model unavailable", "Decision_State": "ABSTAIN", "Model_Route": "BANK", "Long_Term_Score": float("nan")},
                ]
            )
            screen["Research_Priority_Version"] = RESEARCH_PRIORITY_VERSION
            screen.to_csv(root / "screen.csv", index=False)
            pd.DataFrame([{"Ticker": "APH"}]).to_csv(root / "shortlist.csv", index=False)
            pd.DataFrame(
                [
                    {"Ticker": "APH", "CIK": "820313"},
                    {"Ticker": "TEL", "CIK": "1385157"},
                    {"Ticker": "VSH", "CIK": "103730"},
                ]
            ).to_csv(root / "universe.csv", index=False)

            history = root / "history.json"
            history.write_text(
                json.dumps(
                    {
                        "policy_version": "old-policy",
                        "generated_at": "2026-01-01T00:00:00+00:00",
                        "groups": {"industry:Electronic Components": {"avg_score": 1}},
                    }
                ),
                encoding="utf-8",
            )
            index = build_dashboard(
                root / "screen.csv",
                root / "shortlist.csv",
                root / "universe.csv",
                root / "public",
                history,
            )
            self.assertTrue(enhance_dashboard(index))
            self.assertFalse(enhance_dashboard(index))

            html = index.read_text(encoding="utf-8")
            self.assertIn("Alpha Engine 長期價值研究台", html)
            self.assertIn("主題擴散鏈", html)
            self.assertIn("高分群聚與研究線索", html)
            self.assertIn("這裡顯示什麼", html)
            self.assertIn("這裡不代表什麼", html)
            self.assertIn("清除群聚篩選", html)
            self.assertIn("data-candidate", html)
            self.assertIn("clearCandidate", html)
            self.assertIn('value="abstain"', html)
            self.assertIn('value="complete"', html)
            self.assertIn('value="estimated"', html)
            self.assertIn('value="specialized"', html)
            self.assertIn("資料完整性", html)
            self.assertIn("m<=0||", html)
            self.assertIn("v===null||v===undefined||v===''", html)
            self.assertIn("NOT_APPLICABLE:'不適用'", html)
            self.assertIn("metricHtml(x,'Shrunk_Within_Model_Percentile'", html)
            self.assertIn("Raw_Model_Score", html)
            self.assertIn("Core_KPI_Summary", html)
            self.assertIn("模型內百分位，不代表跨模型未來報酬已校準", html)
            self.assertIn("Top 3 Positive Drivers", html)
            self.assertIn("Required Missing Metrics", html)
            self.assertIn("Cross-model calibration", html)
            self.assertIn("Decision Timestamp", html)
            self.assertIn("Git Commit", html)
            self.assertIn("Industry_Model_Score", html)
            self.assertIn("specializedPanel", html)
            self.assertIn("Industry_Model_Metrics_JSON", html)
            self.assertIn("債務來源：", html)
            self.assertIn("ICR 口徑：", html)
            self.assertIn("SEC component composition", html)
            self.assertIn("net_cash", html)
            self.assertIn("一年拆股因子", html)
            self.assertIn("三年拆股因子", html)
            self.assertIn("AI 晶片二階受益鏈", html)
            self.assertIn("二階：瓶頸零組件與設備", html)
            self.assertIn("RESEARCH_CLUSTER_SIGNAL", html)
            self.assertIn("localStorage", html)
            self.assertIn("複製 AI 研究提示", html)
            self.assertIn("二階受益是否已開始進財報", html)
            self.assertIn('"CIK":"0000820313"', html)
            self.assertNotIn("Gemini API", html)
            self.assertTrue((root / "public" / ".nojekyll").exists())

            data = json.loads((root / "public" / "data.json").read_text(encoding="utf-8"))
            self.assertEqual(data["stats"]["total"], 4)
            self.assertEqual(data["stats"]["shortlist"], 1)
            self.assertEqual(data["stats"]["research_queue"], 1)
            self.assertIn("coverage_medians", data["stats"])
            self.assertIn("metric_status_ratios", data["stats"])
            self.assertEqual(data["metadata"]["universe_version"], "test-universe")
            self.assertEqual(data["stocks"][0]["Core_Metric_Coverage_pct"], 100.0)
            self.assertIsNone(data["stocks"][3]["Long_Term_Score"])
            self.assertEqual(data["trend_baseline"]["status"], "建立基準中")
            saved_history = json.loads(history.read_text(encoding="utf-8"))
            self.assertEqual(saved_history["policy_version"], DASHBOARD_TREND_POLICY_VERSION)
            self.assertIn("emerging_candidates", data)
            self.assertIn("metric_status_counts", data["stats"])
            self.assertTrue(data["emerging_candidates"])
            self.assertTrue(
                any(candidate["status"] == "RESEARCH_CLUSTER_SIGNAL" for candidate in data["emerging_candidates"])
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

    def test_low_metric_coverage_cannot_create_emerging_candidate(self):
        rows = [
            {
                "Ticker": "ONE",
                "Shrunk_Within_Model_Percentile": 60.0,
                "Metric_Metadata": {
                    "Shrunk_Within_Model_Percentile": {"status": "VALID", "value": 60.0}
                },
            },
            {
                "Ticker": "TWO",
                "Shrunk_Within_Model_Percentile": None,
                "Metric_Metadata": {
                    "Shrunk_Within_Model_Percentile": {"status": "MISSING", "value": None}
                },
            },
            {
                "Ticker": "THREE",
                "Shrunk_Within_Model_Percentile": None,
                "Metric_Metadata": {
                    "Shrunk_Within_Model_Percentile": {"status": "ABSTAIN", "value": None}
                },
            },
        ]
        group = group_snapshot("industry:test", "Test", "industry", rows)
        self.assertIsNone(group["avg_shrunk_score"])
        self.assertEqual(group["metric_coverage"]["avg_shrunk_score"]["valid_count"], 1)
        self.assertAlmostEqual(group["metric_coverage"]["avg_shrunk_score"]["coverage"], 1 / 3, places=3)
        self.assertEqual(
            build_emerging_candidates({"industry:test": group}, {}),
            [],
        )

    def test_legacy_screen_gets_offline_research_priority_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [
                {
                    "Ticker": "GEN",
                    "Industry_Model_Key": "GENERAL_CORPORATE",
                    "Long_Term_Score": 75.0,
                    "Long_Term_Eligible": True,
                    "Decision_State": "PASS",
                    "Model_Supported": True,
                    "Data_Confidence_Score": 85.0,
                },
                {
                    "Ticker": "INS",
                    "Industry_Model_Key": "INSURANCE_P_AND_C",
                    "Industry_Model_Score": 95.0,
                    "Long_Term_Eligible": True,
                    "Decision_State": "PASS",
                    "Model_Supported": True,
                    "Data_Confidence_Score": 90.0,
                    "Combined_Ratio_Source_Status": "SEC_PROXY_UNRECONCILED",
                },
            ]
            pd.DataFrame(rows).to_csv(root / "screen.csv", index=False)
            pd.DataFrame([{"Ticker": "INS"}]).to_csv(root / "shortlist.csv", index=False)
            pd.DataFrame(
                [{"Ticker": "GEN", "CIK": "1"}, {"Ticker": "INS", "CIK": "2"}]
            ).to_csv(root / "universe.csv", index=False)
            payload, _ = build_payload(
                root / "screen.csv",
                root / "shortlist.csv",
                root / "universe.csv",
                history=None,
            )
            by_ticker = {row["Ticker"]: row for row in payload["stocks"]}
            self.assertEqual(
                by_ticker["GEN"]["Research_Priority_Version"],
                RESEARCH_PRIORITY_VERSION,
            )
            self.assertTrue(by_ticker["GEN"]["Global_Research_Queue"])
            self.assertTrue(by_ticker["INS"]["Human_KPI_Review_Required"])
            self.assertFalse(by_ticker["INS"]["Starter_Candidate"])

    def test_trends_exclude_estimates_and_preserve_not_applicable(self):
        estimate_rows = [
            {"Metric_Metadata": {"metric": {"status": "VALID", "value": 3.0}}},
            {"Metric_Metadata": {"metric": {"status": "ESTIMATED", "value": 9.0}}},
            {"Metric_Metadata": {"metric": {"status": "ESTIMATED", "value": 12.0}}},
        ]
        summary = metric_summary(estimate_rows, "metric")
        self.assertEqual(summary["valid_count"], 1)
        self.assertEqual(summary["estimated_count"], 2)
        self.assertEqual(summary["used_count"], 1)
        self.assertFalse(summary["estimated_included"])
        self.assertIsNone(summary["value"])

        not_applicable = metric_summary(
            [
                {"Metric_Metadata": {"metric": {"status": "NOT_APPLICABLE", "value": None}}},
                {"Metric_Metadata": {"metric": {"status": "NOT_APPLICABLE", "value": None}}},
            ],
            "metric",
        )
        self.assertEqual(not_applicable["status"], "NOT_APPLICABLE")
        self.assertEqual(not_applicable["coverage"], 0.0)
        self.assertIsNone(not_applicable["value"])


if __name__ == "__main__":
    unittest.main()
