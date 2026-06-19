import math
import unittest

import pandas as pd

from AQR_ModeC_Agent_V12 import (
    ModeCResult,
    SECDataDistiller,
    common_equity_rejection_reason,
    composite_score_for_result,
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


if __name__ == "__main__":
    unittest.main()
