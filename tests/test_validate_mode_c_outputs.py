import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from mode_c_decision_inputs import PORTFOLIO_FIT_CONTRACT_VERSION
from mode_c_evidence import EVIDENCE_COLUMNS
from mode_c_metric_contract import annotate_dataframe
from mode_c_industry_models import calculate_specialized_stress
from mode_c_research_priority import annotate_research_priorities
from validate_mode_c_outputs import (
    ValidationError,
    _validate_financial_formulas,
    _validate_bank_stress_formulas,
    build_zero_classification_report,
    validate_outputs,
)


class ModeCOutputValidationTests(unittest.TestCase):
    def test_bank_stress_validator_rejects_total_assets_denominator(self):
        inputs = {
            "assets_b": 100.0, "tangible_equity_b": 8.0, "loans_b": 60.0,
            "credit_loss_allowance_b": 1.0, "risk_weighted_assets_b": 20.0,
            "tier1_ratio_pct": 13.0, "tier1_well_capitalized_min_pct": 8.0,
        }
        metrics = {**inputs, **calculate_specialized_stress("BANK", inputs)}
        row = {
            "Ticker": "BANKTEST", "Industry_Model_Key": "BANK",
            "Specialized_Stress_Status": metrics["specialized_stress_status"],
            "Industry_Model_Metrics_JSON": json.dumps(metrics),
        }
        _validate_bank_stress_formulas(pd.DataFrame([row]))
        metrics["bank_stress_tier1_ratio_pct"] = 13.0 - metrics["bank_stress_after_tax_capital_loss_b"] / 100.0 * 100.0
        row["Industry_Model_Metrics_JSON"] = json.dumps(metrics)
        with self.assertRaisesRegex(ValidationError, "bank stress"):
            _validate_bank_stress_formulas(pd.DataFrame([row]))
        metrics.pop("risk_weighted_assets_b")
        row["Industry_Model_Metrics_JSON"] = json.dumps(metrics)
        with self.assertRaisesRegex(ValidationError, "positive RWA"):
            _validate_bank_stress_formulas(pd.DataFrame([row]))

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
                    "Decision_Reason_Code": "PASS_MODEL_GATE",
                    "Decision_Timestamp": "2026-01-02T00:00:00",
                    "Data_Confidence_Score": 90.0,
                    "MarketCap_B": 11.4,
                    "EV_B": 10.0,
                    "TTM_OCF_B": 1.2,
                    "Dynamic_CapEx_B": 0.3,
                    "Maintenance_CapEx_B": 0.2,
                    "Maintenance_CapEx_Confidence": "HIGH",
                    "TTM_SBC_B": 0.1,
                    "SBC_Economic_Cost_B": 0.1,
                    "Maintenance_Real_FCF_B": 0.9,
                    "Conservative_Real_FCF_B": 0.8,
                    "Real_FCF_Yield_pct": 7.9,
                    "Conservative_Real_FCF_Yield_pct": 7.02,
                    "Maintenance_Real_FCF_to_MarketCap_Yield_pct": 7.9,
                    "Conservative_Real_FCF_to_MarketCap_Yield_pct": 7.02,
                    "Maintenance_Real_FCF_to_EV_Yield_pct": 9.0,
                    "Conservative_Real_FCF_to_EV_Yield_pct": 8.0,
                    "ROIC_pct": 18.0,
                    "EV_EBITDA_x": 8.0,
                    "Historical_Valuation_Coverage": 0.8,
                    "Historical_Valuation_Valid_Years": 8,
                    "Historical_Valuation_Quantile_Used": 20.0,
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
                    "Growth_CapEx_Risk_State": "CLEAR",
                    "Growth_CapEx_Risk_Corroboration_Count": 0,
                    "Growth_CapEx_Risk_Reasons": "",
                    "Specialized_Stress_Status": "NOT_APPLICABLE",
                    "Specialized_Stress_Scenario": "",
                    "Specialized_Stress_Reason": "",
                    "Specialized_Stress_Survival": False,
                    "Specialized_Stress_Missing_Inputs": "",
                    "Portfolio_Fit_Status": "NOT_APPLICABLE",
                    "Portfolio_Fit_Reason": "",
                    "Portfolio_Fit_AsOf": "",
                    "Portfolio_Fit_Input_Age_Days": float("nan"),
                    "Portfolio_Current_Position_Weight_pct_Total": float("nan"),
                    "Portfolio_PreTrade_Active_Sleeve_Weight_pct_Total": float("nan"),
                    "Portfolio_PreTrade_Sector_Weight_pct_Total": float("nan"),
                    "Portfolio_PreTrade_Economic_Risk_Weight_pct_Total": float("nan"),
                    "Portfolio_PostTrade_Position_Weight_pct_Total": float("nan"),
                    "Portfolio_PostTrade_Active_Sleeve_Weight_pct_Total": float("nan"),
                    "Portfolio_PostTrade_Sector_Weight_pct_Total": float("nan"),
                    "Portfolio_PostTrade_Economic_Risk_Weight_pct_Total": float("nan"),
                    "ETF_Lookthrough_Weight_pct_Total": float("nan"),
                    "Portfolio_ETF_Top10_Overlap": False,
                    "Portfolio_Correlation_Stress_Status": "",
                    "Economic_Risk_Bucket": "",
                    "Portfolio_Fit_Contract_Version": PORTFOLIO_FIT_CONTRACT_VERSION,
                    "Suggested_Starter_Weight_pct_Total": 0.0,
                    "DSI_Status": "NOT_APPLICABLE",
                    "DSI_Score": float("nan"),
                    "Working_Capital_Quality_Status": "MISSING",
                    "Working_Capital_Quality_State": "MISSING",
                    "Working_Capital_Quality_Coverage": 0.0,
                    "Working_Capital_Risk_Penalty": 0.0,
                    "Working_Capital_Quality_Reasons": "fixture has no AR/AP history",
                    "AR_vs_Revenue_Growth_Gap_pp": float("nan"),
                    "AP_vs_COGS_Growth_Gap_pp": float("nan"),
                    "Dilution_Double_Count_Check": "PASS",
                    "Ownership_Dilution_Penalty": 0.0,
                    "Capital_Allocation_Penalty": 0.0,
                    "Persistent_Dilution_Hard_Gate": False,
                    "Dilution_Total_Score_Impact": 0.0,
                    "TTM_Gross_Buyback_B": float("nan"),
                    "TTM_Stock_Issuance_B": float("nan"),
                    "Real_Buyback_B": float("nan"),
                    "Acquisition_Stock_Consideration_B": float("nan"),
                    "Acquisition_Issuance_Attribution_Status": "MISSING",
                    "Acquisition_Issuance_Reconciliation_Status": "NOT_APPLICABLE",
                    "Acquisition_Related_Issuance_Flag": False,
                    "Acquisition_Accretion_Review_Required": False,
                    "Applicable_Factor_Weight": 95.0,
                    "Available_Factor_Weight": 95.0,
                    "Factor_Coverage": 1.0,
                    "Weight_Renormalized": True,
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
        ranked_rows = annotate_research_priorities(screen.to_dict(orient="records"))
        screen = annotate_dataframe(pd.DataFrame(ranked_rows), [source, derived])
        screen.to_csv(screen_path, index=False, encoding="utf-8-sig")
        screen.to_csv(shortlist_path, index=False, encoding="utf-8-sig")
        return screen_path, shortlist_path, evidence_path

    def test_valid_outputs_pass_all_invariants(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_valid_outputs(Path(tmp))
            summary = validate_outputs(*paths)
        self.assertEqual(summary["eligible_rows"], 1)
        self.assertEqual(summary["evidence_rows"], 2)

    def test_passing_portfolio_fit_reconciles_pre_and_post_trade_exposure(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_valid_outputs(Path(tmp))
            screen = pd.read_csv(paths[0], encoding="utf-8-sig")
            updates = {
                "Portfolio_Fit_Pending": False,
                "Portfolio_Fit_Status": "PASS",
                "Portfolio_Fit_Reason": "all portfolio gates pass",
                "Portfolio_Fit_AsOf": "2026-01-01T00:00:00",
                "Portfolio_Fit_Input_Age_Days": 1.0,
                "Portfolio_Current_Position_Weight_pct_Total": 0.5,
                "Portfolio_PreTrade_Active_Sleeve_Weight_pct_Total": 20.0,
                "Portfolio_PreTrade_Sector_Weight_pct_Total": 5.0,
                "Portfolio_PreTrade_Economic_Risk_Weight_pct_Total": 4.0,
                "Portfolio_PostTrade_Position_Weight_pct_Total": 2.8,
                "Portfolio_PostTrade_Active_Sleeve_Weight_pct_Total": 21.5,
                "Portfolio_PostTrade_Sector_Weight_pct_Total": 6.5,
                "Portfolio_PostTrade_Economic_Risk_Weight_pct_Total": 5.5,
                "ETF_Lookthrough_Weight_pct_Total": 0.8,
                "Portfolio_ETF_Top10_Overlap": False,
                "Portfolio_Correlation_Stress_Status": "PASS",
                "Economic_Risk_Bucket": "AI_CAPEX",
                "Starter_Candidate": True,
                "Suggested_Starter_Weight_pct_Total": 1.5,
            }
            for column, value in updates.items():
                screen[column] = value
            screen.to_csv(paths[0], index=False, encoding="utf-8-sig")
            screen.to_csv(paths[1], index=False, encoding="utf-8-sig")
            validate_outputs(*paths)

            for corruptions in [
                {"Portfolio_PostTrade_Sector_Weight_pct_Total": 7.0},
                {"Portfolio_Fit_AsOf": "2026-01-02T23:59:59", "Portfolio_Fit_Input_Age_Days": 0.0},
            ]:
                with self.subTest(corruptions=corruptions):
                    corrupted = screen.copy()
                    for column, value in corruptions.items():
                        corrupted[column] = value
                    corrupted.to_csv(paths[0], index=False, encoding="utf-8-sig")
                    corrupted.to_csv(paths[1], index=False, encoding="utf-8-sig")
                    with self.assertRaisesRegex(ValidationError, "portfolio fit"):
                        validate_outputs(*paths)

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
            screen.loc[0, "Decision_Reason_Code"] = "MISSING_REQUIRED_EVIDENCE"
            screen.loc[0, "Long_Term_Eligible"] = False
            screen.loc[0, "Model_Eligible"] = False
            screen.loc[0, "Global_Research_Queue"] = False
            screen.loc[0, "Research_Action_State"] = "SCREENED"
            screen.loc[0, "Research_Statuses"] = "SCREENED"
            screen.loc[0, "Model_Peer_Count"] = 0
            screen.loc[0, "Within_Model_Percentile"] = float("nan")
            screen.loc[0, "Shrunk_Within_Model_Percentile"] = float("nan")
            screen.loc[0, "Research_Priority_Rank"] = float("nan")
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

    def test_dilution_attribution_uses_effective_factor_weight(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_valid_outputs(Path(tmp))
            screen = pd.read_csv(paths[0], encoding="utf-8-sig")
            screen["Capital_Allocation_Penalty"] = 20.0
            screen["Dilution_Total_Score_Impact"] = round(20.0 * 5.0 / 95.0, 2)
            for path in paths[:2]:
                screen.to_csv(path, index=False, encoding="utf-8-sig")
            validate_outputs(*paths)
            screen["Dilution_Total_Score_Impact"] = 1.0
            for path in paths[:2]:
                screen.to_csv(path, index=False, encoding="utf-8-sig")
            with self.assertRaisesRegex(ValidationError, "dilution attribution"):
                validate_outputs(*paths)

    def test_fcf_amounts_and_yields_must_recompute_from_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_valid_outputs(Path(tmp))
            screen = pd.read_csv(paths[0], encoding="utf-8-sig")
            metadata = json.loads(screen.loc[0, "Metric_Metadata_JSON"])
            screen.loc[0, "Maintenance_Real_FCF_B"] = 9.9
            metadata["Maintenance_Real_FCF_B"]["value"] = 9.9
            screen.loc[0, "Metric_Metadata_JSON"] = json.dumps(metadata)
            screen.to_csv(paths[0], index=False, encoding="utf-8-sig")
            screen.to_csv(paths[1], index=False, encoding="utf-8-sig")
            with self.assertRaisesRegex(ValidationError, "cannot be recomputed"):
                validate_outputs(*paths)

    def test_ev_yields_cannot_survive_missing_enterprise_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_valid_outputs(Path(tmp))
            screen = pd.read_csv(paths[0], encoding="utf-8-sig")
            metadata = json.loads(screen.loc[0, "Metric_Metadata_JSON"])
            metadata["EV_B"].update(
                {"value": None, "status": "MISSING", "evidence_ids": []}
            )
            screen.loc[0, "Metric_Metadata_JSON"] = json.dumps(metadata)
            with self.assertRaisesRegex(
                ValidationError, "EV-based FCF yields survive missing enterprise value"
            ):
                _validate_financial_formulas(screen)

    def test_zero_report_classifies_evidenced_zero_and_rejects_invalid_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_valid_outputs(Path(tmp))
            screen = pd.read_csv(paths[0], encoding="utf-8-sig")
            metadata = json.loads(screen.loc[0, "Metric_Metadata_JSON"])
            metadata["Cash_B"] = {
                "value": 0.0,
                "status": "VALID",
                "reason": "reported zero",
                "as_of": "2026-01-01",
                "source_method": "SEC fact",
                "evidence_ids": ["cash-zero"],
            }
            screen.loc[0, "Cash_B"] = 0.0
            screen.loc[0, "Metric_Metadata_JSON"] = json.dumps(metadata)
            report = build_zero_classification_report(screen)
            self.assertEqual(report["summary"]["true_zero"], 3)
            self.assertEqual(report["summary"]["invalid_zero"], 0)

            metadata["Cash_B"]["status"] = "MISSING"
            metadata["Cash_B"]["evidence_ids"] = []
            screen.loc[0, "Metric_Metadata_JSON"] = json.dumps(metadata)
            report = build_zero_classification_report(screen)
            self.assertEqual(report["summary"]["invalid_zero"], 1)

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
