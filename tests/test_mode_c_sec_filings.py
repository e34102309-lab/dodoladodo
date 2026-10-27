import unittest

from mode_c_sec_filings import parse_instance, parse_labels, parse_calculations, filing_base, select_recent_filings, review_plan, write_review_outputs
import csv
from pathlib import Path
import tempfile


def instance(facts, contexts="", units=""):
    return ('''<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:test="https://issuer.example/2026"
      xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <xbrli:context id="group"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">1</xbrli:identifier></xbrli:entity>
      <xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period></xbrli:context>
      <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
      ''' + contexts + units + facts + '</xbrli:xbrl>').encode()


class OriginalFilingTests(unittest.TestCase):
    def test_original_instance_value_is_already_scaled_and_keeps_exact_period(self):
        parsed = parse_instance(instance('<test:DepreciationAndAmortization contextRef="group" unitRef="usd" decimals="-6">125000000</test:DepreciationAndAmortization>'), "0000000001")
        self.assertEqual(parsed["facts"][0]["val"], 125000000)
        self.assertEqual(parsed["facts"][0]["start"], "2026-01-01")
        self.assertEqual(parsed["facts"][0]["qname"], "test:DepreciationAndAmortization")

    def test_segment_or_other_entity_cannot_substitute_consolidated_fact(self):
        contexts = '''<xbrli:context id="segment"><xbrli:entity><xbrli:identifier>1</xbrli:identifier><xbrli:segment><xbrldi:explicitMember dimension="test:SegmentAxis">test:OneMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity><xbrli:period><xbrli:instant>2026-06-30</xbrli:instant></xbrli:period></xbrli:context>
        <xbrli:context id="other"><xbrli:entity><xbrli:identifier>2</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>2026-06-30</xbrli:instant></xbrli:period></xbrli:context>'''
        data = instance(''.join(f'<test:Cash contextRef="{c}" unitRef="usd">10</test:Cash>' for c in ['group','segment','other']),contexts)
        self.assertEqual(len(parse_instance(data,"1")["facts"]),1)
        self.assertEqual(len(parse_instance(data,"1")["dimensional_facts"]),1)

    def test_missing_nonfinite_and_conflicting_facts_are_not_zero(self):
        facts = '''<test:Nil contextRef="group" unitRef="usd" xsi:nil="true"/>
        <test:Infinite contextRef="group" unitRef="usd">INF</test:Infinite>
        <test:Cash contextRef="group" unitRef="usd">1</test:Cash>
        <test:Cash contextRef="group" unitRef="usd">2</test:Cash>
        <test:RealZero contextRef="group" unitRef="usd">0</test:RealZero>'''
        rows = parse_instance(instance(facts),"1")["facts"]
        self.assertEqual([(r["qname"],r["val"]) for r in rows],[("test:RealZero",0.)])

    def test_currency_and_per_share_units_cannot_be_treated_as_usd_amounts(self):
        units = '''<xbrli:unit id="eur"><xbrli:measure>iso4217:EUR</xbrli:measure></xbrli:unit>
        <xbrli:unit id="eps"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unitNumerator><xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>'''
        data=instance('<test:Cash contextRef="group" unitRef="eur">5</test:Cash><test:EPS contextRef="group" unitRef="eps">5</test:EPS>',units=units)
        self.assertFalse(parse_instance(data,"1")["facts"])

    def test_rounded_duplicate_uses_more_precise_value_but_true_conflict_stays_missing(self):
        data = instance('''<test:Cash contextRef="group" unitRef="usd" decimals="-6">1000000</test:Cash>
        <test:Cash contextRef="group" unitRef="usd" decimals="0">1234567</test:Cash>''')
        self.assertEqual(parse_instance(data,"1")["facts"][0]["val"],1234567)
        conflict = data.replace(b'1234567',b'2345678')
        self.assertFalse(parse_instance(conflict,"1")["facts"])

    def test_denial_html_and_entity_documents_fail_closed(self):
        for data in [b'<html>Access Denied</html>',b'<!DOCTYPE x [<!ENTITY a "bad">]><x/>']:
            with self.subTest(data=data),self.assertRaises(ValueError):
                parse_instance(data,"1")

    def test_filing_identity_does_not_allow_arbitrary_urls(self):
        self.assertEqual(filing_base("1","0000000001-26-000001"),"https://www.sec.gov/Archives/edgar/data/1/000000000126000001/")
        for accession in ["../../other", "https://evil.test", "bad"]:
            with self.assertRaises(ValueError): filing_base("1",accession)

    def test_partial_amendment_does_not_replace_original_annual_financials(self):
        payload = {"filings": {"recent": {
            "accessionNumber": ["0000000001-26-000002", "0000000001-26-000001"],
            "form": ["10-K/A", "10-K"], "reportDate": ["2025-12-31"] * 2,
            "filingDate": ["2026-03-01", "2026-02-01"],
            "acceptanceDateTime": ["2026-03-01T12:00:00Z", "2026-02-01T12:00:00Z"],
        }}}
        rows = select_recent_filings(payload, "2026-04-01")
        self.assertEqual({row["form"] for row in rows}, {"10-K", "10-K/A"})
        self.assertEqual([r["form"] for r in select_recent_filings(payload, "2026-02-15")], ["10-K"])

    def test_all_61_review_targets_have_a_specific_note_and_an_output_receipt(self):
        targets=review_plan()["targets"]
        self.assertEqual(len(targets),61)
        self.assertEqual(len({r["ticker"] for r in targets}),61)
        self.assertTrue(all(r.get("review_note") and r.get("reviewed_source", "").startswith("https://www.sec.gov/Archives/") for r in targets))
        with tempfile.TemporaryDirectory() as temp:
            write_review_outputs([],[],temp)
            with (Path(temp)/"mode_c_data_gaps.csv").open(encoding="utf-8-sig",newline="") as handle:
                rows=list(csv.DictReader(handle))
            self.assertEqual({r["Ticker"] for r in rows},{r["ticker"] for r in targets})
            self.assertTrue(all(r["Current_Reason"]=="NO_RESULT" for r in rows))


if __name__ == "__main__":
    unittest.main()
