import math
import unittest

import pandas as pd

from AQR_ModeC_Agent_V12 import (
    STARTER_WEIGHT_PCT_TOTAL,
    ModeCResult,
    SECDataDistiller,
    apply_long_term_framework,
    common_equity_rejection_reason,
    composite_score_for_result,
    select_diversified_shortlist,
)


class ModeCCoreTests(unittest.TestCase):
    def test_fiscal_year_is_normalized_to_integer(self):
        facts = pd.DataFrame(
            [{
                "val": 1.0,
                "end": "2025-12-31",
                "filed": "2026-02-01",
                "fy": 2025.0,
                "fp": "FY",
                "form": "10-K",
            }]
        )
        cleaned = SECDataDistiller._clean_facts(facts)
        self.assertEqual(str(cleaned.loc[0, "fy"]), "2025")

    def test_only_major_exchange_common_equity_is_accepted(self):
        valid = {
            "quoteType": "EQUITY",
            "exchange": "NMS",
            "sector": "Technology",
            "industry": "Software - Infrastructure",
        }
        self.assertEqual(common_equity_rejection_reason("MSFT", valid), "")
        self.assertIn(
            "非普通股商品",
            common_equity_rejection_reason("SPY", {**valid, "quoteType": "ETF"}),
        )
        self.assertIn(
            "非主要美國交易所",
            common_equity_rejection_reason("ASMLF", {**valid, "exchange": "PNK"}),
        )

    def test_zero_valuation_percentile_is_best_not_missing(self):
        cheapest = ModeCResult(
            Ticker="AAA",
            Status="Pass",
            EV_EBITDA_10Y_Percentile=0.0,
            Implied_EBITDA_CAGR_3Y_pct=12.0,
        )
        expensive = ModeCResult(
            Ticker="BBB",
            Status="Pass",
            EV_EBITDA_10Y_Percentile=99.0,
            Implied_EBITDA_CAGR_3Y_pct=12.0,
        )
        self.assertLess(
            composite_score_for_result(cheapest),
            composite_score_for_result(expensive),
        )

    def test_missing_growth_estimate_is_penalized(self):
        missing = ModeCResult(
            Ticker="AAA",
            Status="Pass",
            EV_EBITDA_10Y_Percentile=20.0,
            Implied_EBITDA_CAGR_3Y_pct=math.nan,
        )
        complete = ModeCResult(
            Ticker="BBB",
            Status="Pass",
            EV_EBITDA_10Y_Percentile=20.0,
            Implied_EBITDA_CAGR_3Y_pct=12.0,
        )
        self.assertGreater(
            composite_score_for_result(missing),
            composite_score_for_result(complete),
        )

    def _good_candidate(self, ticker="GOOD", sector="Technology"):
        return ModeCResult(
            Ticker=ticker,
            Status="Pass",
            Sector=sector,
            Real_FCF_Yield_pct=8.0,
            ICR=10.0,
            Share_Count_Change_pct=-1.0,
            Share_Count_Change_3Y_pct=-3.0,
            Dilution_Illusion=False,
            Persistent_Dilution=False,
            ROIC_pct=18.0,
            ROCE_pct=22.0,
            OCF_3Y_Cumulative_B=5.0,
            OCF_3Y_Years=3.0,
            Real_FCF_Positive_Years_5Y=5.0,
            Real_FCF_Years_Available=5.0,
            Real_FCF_Margin_Std_5Y_pct=3.0,
            OCF_to_NetIncome_5Y=1.1,
            Real_FCF_to_NetIncome_5Y=0.8,
            Capital_Allocation_Score=85.0,
            EV_EBITDA_10Y_Percentile=10.0,
            EBITDA_Drawdown_30_pct=-20.0,
            GM_Diagnosis="中性：三季趨勢未給出明確逆風訊號",
            Implied_EBITDA_CAGR_3Y_pct=10.0,
            Momentum_12M_pct=15.0,
            Data_Quality_Flags="OK",
        )

    def test_quality_and_value_raise_long_term_score(self):
        good = apply_long_term_framework(self._good_candidate())
        weak = self._good_candidate(ticker="WEAK")
        weak.Real_FCF_Yield_pct = 1.0
        weak.ICR = 1.5
        weak.Share_Count_Change_pct = 3.0
        weak.EV_EBITDA_10Y_Percentile = 85.0
        weak.EBITDA_Drawdown_30_pct = -65.0
        weak.GM_Diagnosis = "結構性價值陷阱：營收未崩但毛利連續失血"
        weak = apply_long_term_framework(weak)
        self.assertGreater(good.Long_Term_Score, weak.Long_Term_Score)
        self.assertTrue(good.Long_Term_Eligible)
        self.assertFalse(weak.Long_Term_Eligible)
        self.assertEqual(good.Suggested_Starter_Weight_pct_Total, STARTER_WEIGHT_PCT_TOTAL)

    def test_single_year_dilution_warns_but_persistent_dilution_excludes(self):
        clean = apply_long_term_framework(self._good_candidate())

        warning = self._good_candidate(ticker="WARN")
        warning.Dilution_Illusion = True
        warning = apply_long_term_framework(warning)
        self.assertTrue(warning.Long_Term_Eligible)
        self.assertLess(warning.Long_Term_Score, clean.Long_Term_Score)

        persistent = self._good_candidate(ticker="DILUTE")
        persistent.Persistent_Dilution = True
        persistent.Share_Count_Change_3Y_pct = 5.0
        persistent = apply_long_term_framework(persistent)
        self.assertFalse(persistent.Long_Term_Eligible)
        self.assertEqual(persistent.Suggested_Starter_Weight_pct_Total, 0.0)

    def test_shortlist_enforces_sector_cap(self):
        results = []
        for idx in range(5):
            r = self._good_candidate(ticker=f"TECH{idx}", sector="Technology")
            r.Long_Term_Eligible = True
            r.Long_Term_Score = 95.0 - idx
            results.append(r)
        for idx in range(3):
            r = self._good_candidate(ticker=f"HLTH{idx}", sector="Healthcare")
            r.Long_Term_Eligible = True
            r.Long_Term_Score = 85.0 - idx
            results.append(r)

        shortlist = select_diversified_shortlist(results, target_size=4, max_per_sector=2)
        self.assertEqual(len(shortlist), 4)
        self.assertLessEqual(sum(r.Sector == "Technology" for r in shortlist), 2)
        self.assertLessEqual(sum(r.Sector == "Healthcare" for r in shortlist), 2)


if __name__ == "__main__":
    unittest.main()
