"""Original filing reconstruction must fail closed on incomplete identities."""
import csv
import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from AQR_ModeC_Agent_V12 import SECDataDistiller
from mode_c_evidence import EVIDENCE_COLUMNS, PointInTimeEvidenceLedger
from mode_c_sec_filings import filing_concept_rows
from validate_mode_c_outputs import ValidationError, _validate_evidence


class OriginalMetricTests(unittest.TestCase):
    def sec(self, values=(100, 30, 70)):
        sec = SECDataDistiller("offline@example.com", ticker="TEST", cik="1", decision_timestamp="2026-10-01")
        sec.ledger = PointInTimeEvidenceLedger()
        accession = "0000000001-26-000001"
        sec._submission_metadata_cache["0000000001"] = {
            accession: {"accepted_at": pd.Timestamp("2026-08-01T12:00:00")}}
        sec._original_filings_cache = [{
            "accessionNumber": accession, "reportDate": "2026-06-30", "filingDate": "2026-08-01", "form": "10-Q",
            "instance_url": "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/test-20260630_htm.xml",
            "sha256": "a"*64, "calculations": [],
            "facts": [dict(qname=name, val=value, unit="USD", start="2026-01-01", end="2026-06-30",
                context_id="group", decimals="0") for name,value in zip(["test:Revenue","test:Cost","test:Pretax"],values)],
        }]
        return sec

    def formula(self):
        terms = [{"qname":"test:Revenue","coefficient":1},{"qname":"test:Cost","coefficient":-1}]
        return dict(terms=terms,validation_terms=terms,validation_parent="test:Pretax",basis="reviewed exact identity")

    def test_reconstruction_keeps_all_components_and_document_lineage(self):
        sec=self.sec()
        frame=sec._reconstruct_original_metric("EBIT",("USD",),self.formula())
        self.assertEqual(frame["val"].tolist(),[70])
        rows={r["evidence_id"]:r for r in sec.ledger.rows(selected_only=True)}
        derived=rows[frame.iloc[0]["evidence_id"]]
        sources=json.loads(derived["source_evidence_ids"])
        self.assertEqual(len(sources),3)
        self.assertEqual({rows[i]["source_qname"] for i in sources},{"test:Revenue","test:Cost","test:Pretax"})
        self.assertTrue(all(rows[i]["source_document_sha256"]=="a"*64 for i in sources))

    def test_missing_component_and_reconciliation_failure_do_not_become_zero(self):
        for change in ["missing","different_start","different_end","different_unit","bad_total","future"]:
            with self.subTest(change=change):
                sec=self.sec()
                facts=sec._original_filings_cache[0]["facts"]
                if change=="missing":facts.pop(1)
                elif change=="bad_total":facts[-1]["val"]=80
                elif change=="future":sec.decision_timestamp=pd.Timestamp("2026-07-01")
                else:facts[1][{"different_start":"start","different_end":"end","different_unit":"unit"}[change]]={
                    "different_start":"2026-04-01","different_end":"2026-03-31","different_unit":"shares"}[change]
                self.assertTrue(sec._reconstruct_original_metric("EBIT",("USD",),self.formula()).empty)

    def test_different_accessions_cannot_supply_missing_components(self):
        sec=self.sec()
        first=sec._original_filings_cache[0]
        second={**first,"accessionNumber":"0000000001-26-000002","facts":[first["facts"].pop(1)]}
        sec._original_filings_cache.append(second)
        self.assertTrue(sec._reconstruct_original_metric("EBIT",("USD",),self.formula()).empty)

    def test_calculation_partition_must_match_reviewed_children(self):
        sec=self.sec()
        formula=self.formula()
        formula["calculation_children"]={"test:Pretax":{"test:Revenue":1,"test:Cost":-1}}
        self.assertTrue(sec._reconstruct_original_metric("EBIT",("USD",),formula).empty)
        sec._original_filings_cache[0]["calculations"]=[
            dict(role="statement",parent="test:Pretax",child=name,weight=weight)
            for name,weight in formula["calculation_children"]["test:Pretax"].items()]
        self.assertEqual(sec._reconstruct_original_metric("EBIT",("USD",),formula)["val"].tolist(),[70])

    def test_unreviewed_custom_tags_cannot_match_by_label_or_substring(self):
        sec=self.sec()
        self.assertEqual(filing_concept_rows(sec._original_filings_cache,"TEST","Revenue",["Revenue"],("USD",)),[])

    def test_reviewed_cash_alias_is_limited_to_the_verified_accession(self):
        sec=self.sec()
        mapping=[dict(qname="test:Revenue",basis="reviewed balance sheet",allowed_accessions=["0000000001-26-000002"])]
        self.assertFalse(filing_concept_rows(sec._original_filings_cache,"TEST","Cash",[],("USD",),mapping))

    def test_exhaustive_partition_requires_both_members_exact_period_and_reconciled_revenue(self):
        partition=dict(axis="test:Axis",members=["test:Product","test:Service"],cost_qname="test:Cost",revenue_qname="test:Revenue",basis="reviewed partition")
        for change in ["valid","missing","extra_dimension","mismatch","different_period"]:
            with self.subTest(change=change):
                sec=self.sec()
                filing=sec._original_filings_cache[0]
                filing["dimensional_facts"]=[]
                for member,revenue,cost in [("test:Product",60,20),("test:Service",40,10)]:
                    for name,value in [("test:Revenue",revenue),("test:Cost",cost)]:
                        filing["dimensional_facts"].append(dict(filing["facts"][0],qname=name,val=value,
                            context_id=member,dimensions={"test:Axis":member}))
                if change=="missing":filing["dimensional_facts"].pop()
                elif change=="extra_dimension":filing["dimensional_facts"][-1]["dimensions"]["test:Geography"]="test:US"
                elif change=="mismatch":filing["facts"][0]["val"]=110
                elif change=="different_period":filing["dimensional_facts"][-1]["start"]="2026-04-01"
                result=sec._reconstruct_original_partition("GrossProfitCost",partition)
                if change=="valid":
                    self.assertEqual(result["val"].tolist(),[30])
                    ledger=sec.ledger.rows(selected_only=True)
                    self.assertEqual(len([r for r in ledger if r["record_type"]=="source_fact"]),5)
                else:self.assertTrue(result.empty)

    def test_original_source_validator_rejects_missing_or_wrong_document_proof(self):
        sec=self.sec()
        sec._reconstruct_original_metric("EBIT",("USD",),self.formula())
        rows=sec.ledger.rows(selected_only=True)
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"evidence.csv"
            def save():
                with path.open("w",encoding="utf-8",newline="") as f:
                    writer=csv.DictWriter(f,fieldnames=EVIDENCE_COLUMNS)
                    writer.writeheader();writer.writerows(rows)
            save()
            _validate_evidence(path,{"TEST"},pd.Timestamp("2026-10-01",tz="UTC").to_pydatetime())
            source=next(r for r in rows if r["record_type"]=="source_fact")
            for field,value in [("source_document_sha256",""),("source_document_url","https://example.com/test_htm.xml"),("context_id","")]:
                original=source[field];source[field]=value;save()
                with self.assertRaises(ValidationError):
                    _validate_evidence(path,{"TEST"},pd.Timestamp("2026-10-01",tz="UTC").to_pydatetime())
                source[field]=original


if __name__=="__main__":
    unittest.main()
