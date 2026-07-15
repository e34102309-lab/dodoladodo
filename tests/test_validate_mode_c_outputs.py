import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from mode_c_evidence import EVIDENCE_COLUMNS
from mode_c_metric_contract import annotate_dataframe
from validate_mode_c_outputs import ValidationError, validate_outputs


class ModeCOutputValidationTests(unittest.TestCase):
    def _write_valid_outputs(self, directory: Path) -> tuple[Path, Path, Path]:
        screen = pd.DataFrame(
            [
                {
                    "Ticker": "TEST",
                    "Status": "Pass",
                    "Scoring_Framework": "GENERAL_CORPORATE_V13",
                    "Input_Security_Class": "COMMON_OR_EQUIVALENT",
                    "Security_Class_Confidence": "HIGH",
                    "Security_Class_Evidence_Source": "NASDAQ Trader fixture",
                    "Initial_Industry_Model_Key": "GENERAL_CORPORATE",
                    "Model_Route_Refined": False,
                    "Model_Route_Reason": "General corporate fixture",
                    "Model_Supported": True,
                    "Industry_Model_Key": "GENERAL_CORPORATE",
                    "Industry_Model_Decision": "",
                    "Industry_Model_Score": float("nan"),
                    "Industry_Model_Coverage": float("nan"),
                    "Decision_State": "PASS",
                    "Decision_Timestamp": "2026-01-02T00:00:00",
                    "Data_Confidence_Score": 90.0,
                    "MarketCap_B": 11.4,
                    "EV_B": 10.0,
                    "TTM_OCF_B": 1.2,
                    "Dynamic_CapEx_B": 0.3,
                    "Maintenance_CapEx_B": 0.2,
                    "Maintenance_CapEx_Confidence": "HIGH",
                    "TTM_SBC_B": 0.1,
                    "Real_FCF_Yield_pct": 7.9,
                    "ROIC_pct": 18.0,
                    "EV_EBITDA_x": 8.0,
                    "Historical_Valuation_Coverage": 0.8,
                    "Historical_Valuation_Status": "VALID",
                    "Total_Debt_B": 0.10,
                    "Cash_B": 1.50,
                    "Net_Debt_B": -1.40,
                    "Debt_Source_Method": "Yahoo totalDebt current fallback",
                    "ICR": 999.0,
                    "ICR_Method": "net_cash",
                    "Long_Term_Score": 80.0,
                    "Long_Term_Eligible": True,
                    "Stress_Survival_30": True,
                    "Persistent_Dilution": False,
                    "Share_Count_Change_pct": 1.0,
                    "Share_Count_Change_3Y_pct": 2.0,
                    "Share_Basis_Discontinuity": False,
                }
            ]
        )
        screen_path = directory / "screen.csv"
        shortlist_path = directory / "shortlist.csv"
        evidence_path = directory / "evidence.csv"
        common = {
            column: ""
            for column in EVIDENCE_COLUMNS
        }
        source = {
            **common,
            "evidence_id": "source-1",
            "record_type": "source_fact",
            "source_system": "SEC",
            "ticker": "TEST",
            "available_to_model_at": "2026-01-01T12:00:00",
            "decision_timestamp": "2026-01-02T00:00:00",
            "is_available_at_decision": True,
            "selected_for_model": True,
            "source_evidence_ids": "[]",
        }
        derived = {
            **common,
            "evidence_id": "derived-1",
            "record_type": "derived_metric",
            "source_system": "MODEL",
            "ticker": "TEST",
            "available_to_model_at": "2026-01-01T12:00:00",
            "decision_timestamp": "2026-01-02T00:00:00",
            "is_available_at_decision": True,
            "selected_for_model": True,
            "source_evidence_ids": json.dumps(["source-1"]),
        }
        pd.DataFrame([source, derived], columns=EVIDENCE_COLUMNS).to_csv(
            evidence_path, index=False, encoding="utf-8-sig"
        )
        screen = annotate_dataframe(screen, [source, derived])
        screen.to_csv(screen_path, index=False, encoding="utf-8-sig")
        screen.to_csv(shortlist_path, index=False, encoding="utf-8-sig")
        return screen_path, shortlist_path, evidence_path

    def test_valid_outputs_pass_all_invariants(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_valid_outputs(Path(tmp))
            summary = validate_outputs(*paths)
        self.assertEqual(summary["eligible_rows"], 1)
        self.assertEqual(summary["evidence_rows"], 2)

    def test_future_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_valid_outputs(Path(tmp))
            evidence = pd.read_csv(paths[2], encoding="utf-8-sig")
            evidence.loc[0, "available_to_model_at"] = "2026-01-03T00:00:00"
            evidence.to_csv(paths[2], index=False, encoding="utf-8-sig")
            with self.assertRaisesRegex(ValidationError, "point-in-time"):
                validate_outputs(*paths)

    def test_evidence_must_use_the_screen_decision_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_valid_outputs(Path(tmp))
            evidence = pd.read_csv(paths[2], encoding="utf-8-sig")
            evidence["decision_timestamp"] = "2026-01-04T00:00:00"
            evidence.to_csv(paths[2], index=False, encoding="utf-8-sig")
            with self.assertRaisesRegex(ValidationError, "differs from screen"):
                validate_outputs(*paths)

    def test_abstain_cannot_expose_a_long_term_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_valid_outputs(Path(tmp))
            screen = pd.read_csv(paths[0], encoding="utf-8-sig")
            screen.loc[0, "Status"] = "Abstain: incomplete evidence"
            screen.loc[0, "Decision_State"] = "ABSTAIN"
            screen.loc[0, "Long_Term_Eligible"] = False
            screen.to_csv(paths[0], index=False, encoding="utf-8-sig")
            pd.DataFrame(columns=screen.columns).to_csv(
                paths[1], index=False, encoding="utf-8-sig"
            )
            with self.assertRaisesRegex(ValidationError, "misleading long-term scores"):
                validate_outputs(*paths)

    def test_positive_net_debt_cannot_use_infinite_icr(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_valid_outputs(Path(tmp))
            screen = pd.read_csv(paths[0], encoding="utf-8-sig")
            screen.loc[0, "Total_Debt_B"] = 1.50
            screen.loc[0, "Cash_B"] = 0.10
            screen.loc[0, "Net_Debt_B"] = 1.40
            screen.to_csv(paths[0], index=False, encoding="utf-8-sig")
            screen.to_csv(paths[1], index=False, encoding="utf-8-sig")
            with self.assertRaisesRegex(ValidationError, "net-cash ICR method"):
                validate_outputs(*paths)

    def test_nonpositive_enterprise_value_cannot_be_general_eligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_valid_outputs(Path(tmp))
            screen = pd.read_csv(paths[0], encoding="utf-8-sig")
            screen.loc[0, "EV_B"] = -1.0
            screen.to_csv(paths[0], index=False, encoding="utf-8-sig")
            screen.to_csv(paths[1], index=False, encoding="utf-8-sig")
            with self.assertRaisesRegex(ValidationError, "non-positive enterprise value"):
                validate_outputs(*paths)

    def test_screen_rows_must_share_one_decision_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_valid_outputs(Path(tmp))
            screen = pd.read_csv(paths[0], encoding="utf-8-sig")
            second = screen.iloc[0].copy()
            second["Ticker"] = "OTHER"
            second["Decision_Timestamp"] = "2026-01-03T00:00:00"
            second["Long_Term_Eligible"] = False
            second["Status"] = "Fail: test"
            second["Decision_State"] = "FAIL"
            combined = pd.concat([screen, pd.DataFrame([second])], ignore_index=True)
            combined.to_csv(paths[0], index=False, encoding="utf-8-sig")
            with self.assertRaisesRegex(ValidationError, "one point-in-time"):
                validate_outputs(*paths)

    def test_specialized_framework_and_coverage_must_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_valid_outputs(Path(tmp))
            screen = pd.read_csv(paths[0], encoding="utf-8-sig")
            screen["Industry_Model_Decision"] = screen[
                "Industry_Model_Decision"
            ].astype(object)
            screen.loc[0, "Scoring_Framework"] = "INDUSTRY_SPECIALIZED_BANK_V1"
            screen.loc[0, "Industry_Model_Key"] = "BANK"
            screen.loc[0, "Initial_Industry_Model_Key"] = "BANK"
            screen.loc[0, "Industry_Model_Decision"] = "PASS"
            screen.loc[0, "Industry_Model_Score"] = 80.0
            screen.loc[0, "Industry_Model_Coverage"] = 60.0
            screen.to_csv(paths[0], index=False, encoding="utf-8-sig")
            screen.to_csv(paths[1], index=False, encoding="utf-8-sig")
            with self.assertRaisesRegex(ValidationError, "specialized eligible"):
                validate_outputs(*paths)

            screen.loc[0, "Industry_Model_Coverage"] = 90.0
            screen.loc[0, "Industry_Model_Key"] = "INSURANCE_LIFE"
            screen.loc[0, "Initial_Industry_Model_Key"] = "INSURANCE_LIFE"
            screen.to_csv(paths[0], index=False, encoding="utf-8-sig")
            screen.to_csv(paths[1], index=False, encoding="utf-8-sig")
            with self.assertRaisesRegex(ValidationError, "framework/model-key"):
                validate_outputs(*paths)

    def test_only_audited_fee_to_lender_route_refinement_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_valid_outputs(Path(tmp))
            screen = pd.read_csv(paths[0], encoding="utf-8-sig")
            screen.loc[0, "Initial_Industry_Model_Key"] = "FINANCIAL_FEE"
            screen.loc[0, "Industry_Model_Key"] = "FINANCIAL_LENDER"
            screen.loc[0, "Model_Route_Refined"] = True
            screen.loc[0, "Model_Route_Reason"] = "silent route change"
            screen.to_csv(paths[0], index=False, encoding="utf-8-sig")
            screen.to_csv(paths[1], index=False, encoding="utf-8-sig")
            with self.assertRaisesRegex(ValidationError, "unaudited industry-model"):
                validate_outputs(*paths)

    def test_evidence_lineage_must_be_acyclic_and_temporally_ordered(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_valid_outputs(Path(tmp))
            evidence = pd.read_csv(paths[2], encoding="utf-8-sig")
            evidence.loc[1, "source_evidence_ids"] = json.dumps(["derived-1"])
            evidence.to_csv(paths[2], index=False, encoding="utf-8-sig")
            with self.assertRaisesRegex(ValidationError, "cyclic evidence"):
                validate_outputs(*paths)

            paths = self._write_valid_outputs(Path(tmp))
            evidence = pd.read_csv(paths[2], encoding="utf-8-sig")
            evidence.loc[1, "available_to_model_at"] = "2026-01-01T11:00:00"
            evidence.to_csv(paths[2], index=False, encoding="utf-8-sig")
            with self.assertRaisesRegex(ValidationError, "predates"):
                validate_outputs(*paths)


if __name__ == "__main__":
    unittest.main()
