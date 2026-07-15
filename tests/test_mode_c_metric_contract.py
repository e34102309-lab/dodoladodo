import json
import unittest

from mode_c_metric_contract import annotate_rows


DECISION_AT = "2026-07-16T00:00:00"


def row(ticker, **overrides):
    base = {
        "Ticker": ticker,
        "Status": "Pass",
        "Decision_State": "PASS",
        "Decision_Timestamp": DECISION_AT,
        "Industry_Model_Key": "GENERAL_CORPORATE",
        "Data_Confidence_Reasons": "",
        "Data_Quality_Flags": "OK",
    }
    base.update(overrides)
    return base


def evidence(ticker, metric, evidence_id):
    return {
        "ticker": ticker,
        "normalized_metric": metric,
        "evidence_id": evidence_id,
        "selected_for_model": True,
        "period_end": "2026-06-30",
        "formula": "reported SEC fact",
    }


class ModeCMetricContractTests(unittest.TestCase):
    def test_glw_missing_capex_is_abstain_not_zero(self):
        annotated = annotate_rows(
            [
                row(
                    "GLW",
                    Status="Abstain: Missing critical SEC metrics: CapEx",
                    Decision_State="ABSTAIN",
                    Dynamic_CapEx_B=None,
                    Maintenance_CapEx_B=None,
                    Real_FCF_Yield_pct=None,
                    Long_Term_Score=None,
                )
            ],
            [],
        )[0]
        metadata = json.loads(annotated["Metric_Metadata_JSON"])
        for metric in (
            "Dynamic_CapEx_B",
            "Maintenance_CapEx_B",
            "Real_FCF_Yield_pct",
        ):
            self.assertIsNone(annotated[metric])
            self.assertEqual(metadata[metric]["status"], "ABSTAIN")

    def test_pgr_insurance_metrics_do_not_expose_generic_false_zeros(self):
        annotated = annotate_rows(
            [
                row(
                    "PGR",
                    Scoring_Framework="INDUSTRY_SPECIALIZED_INSURANCE_P_AND_C_V1",
                    Industry_Model_Key="INSURANCE_P_AND_C",
                    Industry_Model_Decision="PASS",
                    Industry_Model_Score=82.0,
                    Industry_Model_Coverage=91.0,
                    Long_Term_Score=82.0,
                    Data_Confidence_Score=90.0,
                    MarketCap_B=120.0,
                    Industry_Model_Metrics_JSON=json.dumps(
                        {"combined_ratio_pct": 94.3, "premium_growth_pct": 11.2}
                    ),
                    Real_FCF_Yield_pct=0.0,
                    Maintenance_CapEx_B=0.0,
                    EV_EBITDA_x=0.0,
                )
            ],
            [],
        )[0]
        metadata = json.loads(annotated["Metric_Metadata_JSON"])
        for metric in ("Real_FCF_Yield_pct", "Maintenance_CapEx_B", "EV_EBITDA_x"):
            self.assertIsNone(annotated[metric])
            self.assertEqual(metadata[metric]["status"], "NOT_APPLICABLE")
        self.assertEqual(metadata["industry.combined_ratio_pct"]["status"], "VALID")
        self.assertEqual(metadata["industry.combined_ratio_pct"]["value"], 94.3)

    def test_ifnny_ads_without_fx_or_ratio_abstains(self):
        annotated = annotate_rows(
            [
                row(
                    "IFNNY",
                    Status="Abstain: ADS lacks point-in-time FX rate or ADR ratio",
                    Decision_State="ABSTAIN",
                    Input_Security_Class="COMMON_ADS_INFERRED",
                    Point_in_Time_FX_Rate=None,
                    ADR_Ratio=None,
                    Long_Term_Score=None,
                )
            ],
            [],
        )[0]
        metadata = json.loads(annotated["Metric_Metadata_JSON"])
        self.assertIsNone(annotated["Point_in_Time_FX_Rate"])
        self.assertIsNone(annotated["ADR_Ratio"])
        self.assertEqual(metadata["Point_in_Time_FX_Rate"]["status"], "ABSTAIN")
        self.assertEqual(metadata["ADR_Ratio"]["status"], "ABSTAIN")

    def test_true_zero_survives_when_it_has_evidence(self):
        annotated = annotate_rows(
            [row("ZERO", Cash_B=0.0)],
            [evidence("ZERO", "Cash", "cash-zero")],
        )[0]
        metadata = json.loads(annotated["Metric_Metadata_JSON"])
        self.assertEqual(annotated["Cash_B"], 0.0)
        self.assertEqual(metadata["Cash_B"]["status"], "VALID")
        self.assertEqual(metadata["Cash_B"]["evidence_ids"], ["cash-zero"])

    def test_low_historical_coverage_is_not_displayed_as_zero(self):
        annotated = annotate_rows(
            [
                row(
                    "HIST",
                    Historical_Valuation_Coverage=0.0,
                    Historical_Valuation_Status="MISSING",
                )
            ],
            [],
        )[0]
        metadata = json.loads(annotated["Metric_Metadata_JSON"])
        self.assertIsNone(annotated["Historical_Valuation_Coverage"])
        self.assertEqual(metadata["Historical_Valuation_Coverage"]["status"], "MISSING")


if __name__ == "__main__":
    unittest.main()
