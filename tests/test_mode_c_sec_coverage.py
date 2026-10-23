"""Regression cases observed in the first fixed-100 live run."""
import csv
import json
import math
from pathlib import Path
import tempfile
import unittest

import pandas as pd

import AQR_ModeC_Agent_V12 as engine
from mode_c_evidence import EVIDENCE_COLUMNS, PointInTimeEvidenceLedger
from validate_mode_c_outputs import ValidationError, _validate_sec_availability_dates


def fact(start, end, value, form="10-Q", fy=2026, fp="Q2", accn="a"):
    return dict(start=start, end=end, val=value, form=form, fy=fy, fp=fp,
                filed="2026-08-27", accn=accn)


class SECCoverageTests(unittest.TestCase):
    def sec(self, facts):
        sec = engine.SECDataDistiller("test@example.com", ticker="TEST", cik="1",
            decision_timestamp=pd.Timestamp("2026-10-24"))
        sec.ledger = PointInTimeEvidenceLedger()
        sec._submission_metadata_cache["0000000001"] = {}
        sec._companyfacts_cache["0000000001"] = {"us-gaap": {
            tag: {"units": {"USD": rows}} for tag, rows in facts.items()
        }}
        return sec

    def test_productive_assets_cash_capex_uses_a_consistent_disclosed_history(self):
        sec = self.sec({
            "PaymentsToAcquirePropertyPlantAndEquipment": [fact("2024-01-01", "2024-12-31", 1e9, "10-K")],
            "PaymentsToAcquireProductiveAssets": [
                fact("2024-01-01", "2024-12-31", 1.2e9, "10-K"),
                fact("2025-01-01", "2025-12-31", 2e9, "10-K"),
                fact("2025-01-01", "2025-06-30", .8e9),
                fact("2026-01-01", "2026-06-30", 1.1e9),
            ],
        })
        frame = sec.fetch_concept("1", "CapEx")
        self.assertEqual(set(frame["concept"]), {"PaymentsToAcquireProductiveAssets"})
        value, _, details = sec.ttm_flow(frame, signed=False, normalized_metric="TTM_CapEx")
        self.assertAlmostEqual(value, 2.3)
        self.assertEqual(details["period_end"], "2026-06-30")

    def test_noncash_ppe_additions_are_not_cash_capex(self):
        sec = self.sec({"PropertyPlantAndEquipmentAdditions": [fact("2026-01-01", "2026-06-30", 1e9)]})
        self.assertTrue(sec.fetch_concept("1", "CapEx").empty)

    def test_new_nonoperating_interest_expense_tag_is_supported(self):
        sec = self.sec({"InterestExpenseNonoperating": [fact("2025-07-01", "2026-06-30", 3.051e9, "10-K")]})
        value, _, _ = sec.ttm_flow(sec.fetch_concept("1", "Interest"))
        self.assertAlmostEqual(value, 3.051)

    def test_including_tax_revenue_keeps_its_original_basis_and_source_tag(self):
        sec = self.sec({
            "Revenues": [fact("2024-01-01", "2024-12-31", 2e9, "10-K")],
            "RevenueFromContractWithCustomerIncludingAssessedTax": [fact("2026-02-01", "2026-07-31", 2.856526e9)],
        })
        result = sec.fetch_concept("1", "Revenue")
        self.assertEqual(result["concept"].tolist(), ["RevenueFromContractWithCustomerIncludingAssessedTax"])

    def test_gross_profit_reconstruction_requires_same_period_accession_and_unit(self):
        sec = self.sec({
            "Revenues": [fact("2026-01-01", "2026-06-30", 229.692e9)],
            "CostOfRevenue": [fact("2026-01-01", "2026-06-30", 87.214e9)],
        })
        frame = sec.fetch_concept("1", "GrossProfit")
        self.assertEqual(len(frame), 1)
        self.assertAlmostEqual(frame.iloc[0]["val"] / 1e9, 142.478)
        ledger = {r["evidence_id"]: r for r in sec.ledger.rows(selected_only=True)}
        derived = ledger[frame.iloc[0]["evidence_id"]]
        self.assertEqual(derived["record_type"], "derived_metric")
        sources = json.loads(derived["source_evidence_ids"])
        self.assertEqual({ledger[i]["source_system"] for i in sources}, {"SEC"})
        self.assertEqual({ledger[i]["original_tag"] for i in sources}, {"Revenues", "CostOfRevenue"})

    def test_cost_from_another_accession_cannot_be_subtracted(self):
        sec = self.sec({
            "Revenues": [fact("2026-01-01", "2026-06-30", 10e9, accn="a")],
            "CostOfRevenue": [fact("2026-01-01", "2026-06-30", 4e9, accn="b")],
        })
        self.assertTrue(sec.fetch_concept("1", "GrossProfit").empty)

    def test_partial_cost_excluding_depreciation_cannot_create_gross_profit(self):
        sec = self.sec({
            "Revenues": [fact("2026-01-01", "2026-06-30", 10e9)],
            "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization": [fact("2026-01-01", "2026-06-30", 4e9)],
        })
        self.assertTrue(sec.fetch_concept("1", "GrossProfit").empty)

    def test_reported_gross_profit_wins_over_a_reconstructed_same_period(self):
        sec = self.sec({
            "Revenues": [fact("2026-01-01", "2026-06-30", 10e9)],
            "CostOfRevenue": [fact("2026-01-01", "2026-06-30", 4e9)],
            "GrossProfit": [fact("2026-01-01", "2026-06-30", 5.5e9)],
        })
        period = sec._duration_facts(sec.fetch_concept("1", "GrossProfit"))
        self.assertEqual(period.iloc[0]["val"], 5.5e9)

    def test_comparative_filing_fy_labels_do_not_break_ttm_bridge(self):
        sec = self.sec({})
        frame = sec._clean_facts(pd.DataFrame([
            fact("2025-01-01", "2025-12-31", 20e9, "10-K", fy=2026, fp="FY"),
            fact("2025-01-01", "2025-06-30", 8e9, fy=2026),
            fact("2026-01-01", "2026-06-30", 11e9, fy=2026),
        ]))
        value, method, details = sec.ttm_flow(frame)
        self.assertAlmostEqual(value, 23)
        self.assertIn("YTD", method)
        self.assertEqual(details["period_start"], "2025-07-01")

    def test_standalone_quarters_in_annual_comparatives_are_not_lost(self):
        sec = self.sec({})
        frame = sec._clean_facts(pd.DataFrame([
            fact("2025-01-01", "2025-03-31", 1e9, "10-K", fp="FY"),
            fact("2025-04-01", "2025-06-30", 2e9, "10-K", fp="FY"),
            fact("2025-07-01", "2025-09-30", 3e9, "10-K", fp="FY"),
            fact("2025-10-01", "2025-12-31", 4e9, "10-K", fp="FY"),
        ]))
        self.assertEqual(sec.quarterly_series(frame).tolist(), [1e9, 2e9, 3e9, 4e9])
        value, _, _ = sec.ttm_flow(frame)
        self.assertAlmostEqual(value, 10)

    def test_direct_and_ytd_quarters_are_not_double_counted(self):
        sec = self.sec({})
        frame = sec._clean_facts(pd.DataFrame([
            fact("2026-01-01", "2026-03-31", 1e9),
            fact("2026-01-01", "2026-06-30", 3e9),
            fact("2026-04-01", "2026-06-30", 2e9),
        ]))
        self.assertEqual(sec.quarterly_series(frame).tolist(), [1e9, 2e9])

    def test_same_end_but_different_ttm_start_is_rejected(self):
        result = engine.assess_ttm_period_alignment({
            "OCF": {"period_start": "2025-07-01", "period_end": "2026-06-30"},
            "SBC": {"period_start": "2026-01-01", "period_end": "2026-06-30"},
        }, pd.Timestamp("2026-10-24"), 240)
        self.assertEqual(result["mismatched"], ["OCF"])

    def test_overlapping_quarters_cannot_be_a_trailing_window(self):
        dates = pd.to_datetime(["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"])
        series = pd.Series([1.] * 4, index=dates)
        series.attrs["period_starts"] = {date.isoformat(): "2025-07-01" for date in dates}
        self.assertTrue(engine._trailing_quarter_window(series, 4, dates[-1]).empty)

    def test_sec_display_date_ignores_yahoo_model_and_future_sources(self):
        common = dict(ticker="TEST", record_type="source_fact", is_available_at_decision=True,
                      decision_timestamp="2026-10-24")
        rows = [
            dict(common, source_system="SEC", available_to_model_at="2026-07-31T20:05:00Z"),
            dict(common, source_system="YAHOO_FINANCE", available_to_model_at="2026-10-24"),
            dict(common, source_system="SEC", available_to_model_at="2026-10-25"),
            dict(common, source_system="MODEL", available_to_model_at="2026-10-24"),
        ]
        self.assertEqual(engine.latest_sec_availability_by_ticker(rows), {"TEST": "2026-07-31T20:05:00"})
        self.assertEqual(engine.latest_sec_availability_by_ticker(rows[1:2]), {})

    def test_validator_rejects_yahoo_observation_as_latest_sec_date(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "evidence.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=EVIDENCE_COLUMNS)
                writer.writeheader()
                writer.writerow(dict(ticker="TEST", source_system="SEC", record_type="source_fact",
                                     available_to_model_at="2026-07-31T20:05:00Z"))
            screen = pd.DataFrame([dict(Ticker="TEST", Latest_SEC_Availability_Date="2026-10-24")])
            with self.assertRaisesRegex(ValidationError, "latest SEC date"):
                _validate_sec_availability_dates(screen, path)
            screen["Latest_SEC_Availability_Date"] = "2026-07-31T20:05:00"
            _validate_sec_availability_dates(screen, path)


if __name__ == "__main__":
    unittest.main()
