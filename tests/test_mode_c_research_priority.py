import math
import unittest

from mode_c_research_priority import (
    RESEARCH_PRIORITY_METHOD,
    annotate_research_priorities,
    global_research_queue,
    shrunk_percentile,
)


def row(ticker, model, raw_score, *, eligible=True, confidence=80.0):
    return {
        "Ticker": ticker,
        "Industry_Model_Key": model,
        "Long_Term_Score": raw_score if model == "GENERAL_CORPORATE" else 70.0,
        "Industry_Model_Score": raw_score if model != "GENERAL_CORPORATE" else None,
        "Long_Term_Eligible": eligible,
        "Data_Confidence_Score": confidence,
        "Decision_State": "PASS",
        "Model_Supported": True,
        "Industry_Model_Required_Missing": "",
        "Combined_Ratio_Source_Status": "COMPANY_REPORTED",
        "Specialized_Stress_Status": "PASS",
    }


class ResearchPriorityTests(unittest.TestCase):
    def test_raw_scores_are_ranked_only_inside_their_own_model(self):
        rows = [
            row("GEN1", "GENERAL_CORPORATE", 80.0),
            row("GEN2", "GENERAL_CORPORATE", 60.0),
            row("INS1", "INSURANCE_P_AND_C", 99.0),
        ]
        annotate_research_priorities(rows)
        by_ticker = {item["Ticker"]: item for item in rows}
        self.assertEqual(by_ticker["GEN1"]["Model_Peer_Count"], 2)
        self.assertEqual(by_ticker["INS1"]["Model_Peer_Count"], 1)
        self.assertEqual(by_ticker["INS1"]["Within_Model_Percentile"], 100.0)
        self.assertLess(
            by_ticker["INS1"]["Shrunk_Within_Model_Percentile"],
            by_ticker["GEN1"]["Shrunk_Within_Model_Percentile"],
        )
        self.assertTrue(all(not item["Cross_Model_Comparable"] for item in rows))
        self.assertTrue(all(item["Research_Priority_Method"] == RESEARCH_PRIORITY_METHOD for item in rows))

    def test_small_samples_shrink_toward_fifty(self):
        self.assertAlmostEqual(shrunk_percentile(100.0, 1), 52.380952, places=5)
        self.assertGreater(shrunk_percentile(100.0, 100), 90.0)
        self.assertTrue(math.isnan(shrunk_percentile(100.0, 0)))

    def test_unreconciled_p_and_c_proxy_requires_review_and_never_starts(self):
        candidate = row("ACGL", "INSURANCE_P_AND_C", 98.0)
        candidate["Combined_Ratio_Source_Status"] = "SEC_PROXY_UNRECONCILED"
        annotate_research_priorities([candidate])
        self.assertTrue(candidate["Human_KPI_Review_Required"])
        self.assertFalse(candidate["Starter_Candidate"])
        self.assertEqual(candidate["Research_Action_State"], "HUMAN_KPI_REVIEW_REQUIRED")
        self.assertEqual(global_research_queue([candidate]), [candidate])

    def test_missing_specialized_stress_can_enter_research_queue_but_not_start(self):
        candidate = row("ACGL", "INSURANCE_P_AND_C", 98.0)
        candidate["Combined_Ratio_Source_Status"] = "COMPANY_REPORTED"
        candidate["Specialized_Stress_Status"] = "ABSTAIN"
        annotate_research_priorities([candidate], queue_size=1)
        self.assertTrue(candidate["Model_Eligible"])
        self.assertTrue(candidate["Global_Research_Queue"])
        self.assertTrue(candidate["Specialized_Stress_Pending"])
        self.assertFalse(candidate["Starter_Candidate"])
        self.assertEqual(candidate["Research_Action_State"], "SPECIALIZED_STRESS_PENDING")


if __name__ == "__main__":
    unittest.main()
