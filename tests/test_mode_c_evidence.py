import unittest

import pandas as pd

from AQR_ModeC_Agent_V12 import SECDataDistiller
from mode_c_evidence import GLOBAL_EVIDENCE_LEDGER, PointInTimeEvidenceLedger


class EvidenceLedgerTests(unittest.TestCase):
    def tearDown(self):
        GLOBAL_EVIDENCE_LEDGER.reset()

    def test_derived_metric_keeps_source_lineage(self):
        ledger = PointInTimeEvidenceLedger()
        source_id = ledger.register_source_fact(
            ticker="TEST",
            cik="0000000001",
            source_system="SEC",
            concept="NetCashProvidedByUsedInOperatingActivities",
            normalized_metric="OCF",
            value=100.0,
            unit="USD",
            period_start="2025-01-01",
            period_end="2025-12-31",
            filed_at="2026-02-01",
            accepted_at="2026-02-01T21:00:00",
            accession_number="0000000001-26-000001",
            form="10-K",
            fy=2025,
            fp="FY",
            source_priority=0,
            availability_source="accepted_at",
            available_to_model_at="2026-02-01T21:05:00",
            decision_timestamp="2026-02-02T00:00:00",
            is_available_at_decision=True,
        )
        derived_id = ledger.register_derived_metric(
            ticker="TEST",
            cik="0000000001",
            normalized_metric="TTM_OCF",
            value=0.0000001,
            unit="USD_B",
            formula="latest annual filing value",
            source_evidence_ids=[source_id],
            decision_timestamp="2026-02-02T00:00:00",
            role="TTM_OCF:model-input",
        )
        rows = {row["evidence_id"]: row for row in ledger.rows(selected_only=True)}
        self.assertIn(source_id, rows)
        self.assertIn(derived_id, rows)
        self.assertTrue(rows[source_id]["selected_for_model"])
        self.assertIn(source_id, rows[derived_id]["source_evidence_ids"])
        self.assertEqual(
            ledger.source_original_tags(derived_id),
            ["NetCashProvidedByUsedInOperatingActivities"],
        )
        self.assertEqual(rows[source_id]["period_duration_days"], 364)
        self.assertFalse(rows[source_id]["period_anomaly_flag"])

    def test_derived_metric_requires_complete_available_lineage(self):
        ledger = PointInTimeEvidenceLedger()
        unavailable_source_id = ledger.register_source_fact(
            ticker="TEST",
            cik="0000000001",
            source_system="SEC",
            concept="Revenues",
            normalized_metric="Revenue",
            value=100.0,
            unit="USD",
            period_start="2025-01-01",
            period_end="2025-12-31",
            filed_at="2026-02-01",
            accepted_at="2026-02-01T21:00:00",
            accession_number="0000000001-26-000001",
            form="10-K",
            fy=2025,
            fp="FY",
            source_priority=0,
            availability_source="accepted_at",
            available_to_model_at="2026-02-01T21:05:00",
            decision_timestamp="2026-01-31T00:00:00",
            is_available_at_decision=False,
        )
        derived_ids = [
            ledger.register_derived_metric(
                ticker="TEST",
                cik="0000000001",
                normalized_metric=metric,
                value=1.0,
                unit="USD_B",
                formula="test formula",
                source_evidence_ids=source_ids,
                decision_timestamp="2026-01-31T00:00:00",
                role=f"{metric}:model-input",
            )
            for metric, source_ids in (
                ("NoSources", []),
                ("MissingSource", ["missing-evidence-id"]),
                ("UnavailableSource", [unavailable_source_id]),
            )
        ]

        self.assertEqual(ledger.rows(selected_only=True), [])
        rows = {row["evidence_id"]: row for row in ledger.rows()}
        for derived_id in derived_ids:
            self.assertFalse(rows[derived_id]["is_available_at_decision"])
            self.assertFalse(rows[derived_id]["selected_for_model"])
            self.assertEqual(rows[derived_id]["selection_roles"], "")
        self.assertFalse(rows[unavailable_source_id]["selected_for_model"])

        ledger.mark_used([unavailable_source_id], "manual-attempt")
        rows = {row["evidence_id"]: row for row in ledger.rows()}
        self.assertFalse(rows[unavailable_source_id]["selected_for_model"])

    def test_period_anomaly_and_fallback_tag_are_visible_in_confidence_stats(self):
        ledger = PointInTimeEvidenceLedger()
        source_id = ledger.register_source_fact(
            ticker="TEST",
            cik="0000000001",
            source_system="SEC",
            concept="AlternateRevenueTag",
            normalized_metric="Revenue",
            value=100.0,
            unit="USD",
            period_start="2025-01-01",
            period_end="2026-12-31",
            filed_at="2027-02-01",
            accepted_at="2027-02-01T21:00:00",
            accession_number="0000000001-27-000001",
            form="10-K",
            fy=2026,
            fp="FY",
            source_priority=2,
            availability_source="accepted_at",
            available_to_model_at="2027-02-01T21:05:00",
            decision_timestamp="2027-02-02T00:00:00",
            is_available_at_decision=True,
        )
        ledger.mark_used([source_id], "Revenue:model-input")
        stats = ledger.selected_source_stats("TEST")
        self.assertEqual(stats["period_anomaly_count"], 1)
        self.assertEqual(stats["fallback_tag_count"], 1)
        self.assertLess(stats["average_source_confidence"], 100.0)

    def test_sec_facts_are_filtered_by_availability_not_period_end(self):
        companyfacts = {
            "facts": {
                "us-gaap": {
                    "Revenues": {
                        "units": {
                            "USD": [
                                {
                                    "start": "2024-01-01",
                                    "end": "2024-12-31",
                                    "val": 100,
                                    "form": "10-K",
                                    "fp": "FY",
                                    "fy": 2024,
                                    "filed": "2025-02-01",
                                    "accn": "0000000001-25-000001",
                                },
                                {
                                    "start": "2024-01-01",
                                    "end": "2024-12-31",
                                    "val": 110,
                                    "form": "10-K/A",
                                    "fp": "FY",
                                    "fy": 2024,
                                    "filed": "2025-08-01",
                                    "accn": "0000000001-25-000002",
                                },
                            ]
                        }
                    }
                }
            }
        }
        submissions = {
            "filings": {
                "recent": {
                    "accessionNumber": ["0000000001-25-000001", "0000000001-25-000002"],
                    "filingDate": ["2025-02-01", "2025-08-01"],
                    "reportDate": ["2024-12-31", "2024-12-31"],
                    "acceptanceDateTime": ["2025-02-01T21:00:00Z", "2025-08-01T21:00:00Z"],
                    "form": ["10-K", "10-K/A"],
                },
                "files": [],
            }
        }

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

            def __bool__(self):
                return True

        class FakeSession:
            def get(self, url, headers):
                if "/submissions/" in url:
                    return FakeResponse(submissions)
                return FakeResponse(companyfacts)

        GLOBAL_EVIDENCE_LEDGER.reset()
        sec = SECDataDistiller(
            "research@example.com",
            ticker="TEST",
            cik="1",
            decision_timestamp=pd.Timestamp("2025-06-01", tz="UTC"),
        )
        sec.session = FakeSession()
        facts = sec.fetch_concept("1", "Revenue")
        self.assertEqual(len(facts), 1)
        self.assertEqual(float(facts.iloc[0]["val"]), 100.0)
        self.assertLessEqual(facts.iloc[0]["available_to_model_at"], sec.decision_timestamp)
        value, method, evidence = sec.ttm_flow(facts, normalized_metric="TTM_Revenue")
        self.assertEqual(value, 100 / 1e9)
        self.assertIn("latest 10-K", method)
        self.assertTrue(evidence["evidence_id"])
        selected = GLOBAL_EVIDENCE_LEDGER.rows(selected_only=True)
        self.assertTrue(any(row["accession_number"] == "0000000001-25-000001" for row in selected))
        self.assertFalse(any(row["accession_number"] == "0000000001-25-000002" for row in selected))


if __name__ == "__main__":
    unittest.main()
