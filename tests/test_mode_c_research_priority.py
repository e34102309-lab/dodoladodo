import math
import unittest

import pandas as pd

from mode_c_research_priority import (
    RESEARCH_PRIORITY_METHOD,
    annotate_research_priorities,
    global_research_queue,
    research_priority_order,
    shrunk_percentile,
    starter_candidate_gate,
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
    def test_starter_requires_explicit_portfolio_pass_and_known_risk_flags(self):
        candidate = row("GEN", "GENERAL_CORPORATE", 80.0)
        annotate_research_priorities([candidate])
        candidate.update(Portfolio_Fit_Status="PASS", Portfolio_Fit_Pending=False)
        self.assertTrue(starter_candidate_gate(candidate))
        candidate.pop("Portfolio_Fit_Status")
        self.assertFalse(starter_candidate_gate(candidate))
        candidate["Portfolio_Fit_Status"] = "PASS"
        for flag in ("Human_KPI_Review_Required", "Specialized_Stress_Pending", "Specialized_Stress_Failed", "Portfolio_Fit_Pending"):
            for unknown in (None, math.nan, pd.NA, "unknown"):
                with self.subTest(flag=flag, unknown=unknown):
                    corrupted = {**candidate, flag: unknown}
                    self.assertFalse(starter_candidate_gate(corrupted))

    def test_contradictory_or_unknown_eligibility_cannot_enter_queue(self):
        for changes in (
            {"Long_Term_Eligible": math.nan}, {"Long_Term_Eligible": pd.NA},
            {"Decision_State": "FAIL"}, {"Decision_State": "ABSTAIN"},
            {"Model_Supported": math.nan}, {"Long_Term_Score": 101.0},
        ):
            with self.subTest(changes=changes):
                candidate = {**row("GEN", "GENERAL_CORPORATE", 80.0), **changes}
                annotate_research_priorities([candidate])
                self.assertFalse(candidate["Model_Eligible"])
                self.assertFalse(candidate["Global_Research_Queue"])
                self.assertFalse(candidate["Starter_Candidate"])

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

    def test_failed_specialized_stress_is_not_mislabeled_as_pending(self):
        candidate = row("MREIT", "REIT_MORTGAGE", 80.0, eligible=False)
        candidate["Decision_State"] = "FAIL"
        candidate["Specialized_Stress_Status"] = "FAIL"
        annotate_research_priorities([candidate])
        self.assertFalse(candidate["Specialized_Stress_Pending"])
        self.assertTrue(candidate["Specialized_Stress_Failed"])
        self.assertEqual(candidate["Research_Action_State"], "SPECIALIZED_STRESS_FAILED")
        self.assertFalse(candidate["Starter_Candidate"])

    def test_global_queue_interleaves_models_before_taking_a_second_name(self):
        rows = [
            row(f"GEN{index}", "GENERAL_CORPORATE", 100.0 - index)
            for index in range(10)
        ]
        rows.append(row("BANK1", "BANK", 70.0, confidence=75.0))

        annotate_research_priorities(rows, queue_size=3)

        queue = global_research_queue(rows)
        self.assertEqual([item["Ticker"] for item in queue], ["GEN0", "BANK1", "GEN1"])
        self.assertEqual(queue[0]["Research_Priority_Round"], 1)
        self.assertEqual(queue[1]["Research_Priority_Round"], 1)
        self.assertEqual(queue[2]["Research_Priority_Round"], 2)
        self.assertEqual(
            [item["Ticker"] for item in research_priority_order(rows)[:3]],
            ["GEN0", "BANK1", "GEN1"],
        )


if __name__ == "__main__":
    unittest.main()
