import ast
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "total_market_hunter_v2.py"

try:
    import total_market_hunter_v2 as hunter
except ModuleNotFoundError:
    hunter = None


class HunterSourceTests(unittest.TestCase):
    def test_source_parses(self):
        ast.parse(MODULE_PATH.read_text(encoding="utf-8"))

    def test_no_parallel_detail_request_pool(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("ThreadPoolExecutor", source)
        self.assertNotIn("as_completed", source)

    def test_rate_limit_stops_instead_of_dropping_stock(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("class RateLimitStop", source)
        self.assertIn("raise RateLimitStop", source)
        self.assertNotIn("ThreadPoolExecutor", source)

    def test_each_detail_request_has_a_hard_timeout(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("run_yahoo_request_with_timeout", source)
        self.assertIn("class RequestTimeoutStop", source)
        self.assertIn("threading.Thread", source)
        self.assertIn("daemon=True", source)
        self.assertIn("worker.join", source)
        self.assertIn("os._exit(5)", source)
        self.assertIn("--request-timeout", source)

    def test_partial_run_does_not_replace_complete_universe(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("qualified_universe.partial.csv", source)
        self.assertIn("write_complete_outputs", source)
        self.assertIn("if complete:", source)


@unittest.skipIf(hunter is None, "project dependencies are not installed locally")
class HunterRuntimeTests(unittest.TestCase):
    @staticmethod
    def _candidate(ticker="TEST"):
        return {
            "Ticker": ticker,
            "CIK": "0000000001",
            "Name": "Test Company",
            "SECExchange": "NASDAQ",
            "ScreenerQuote": {
                "symbol": ticker,
                "quoteType": "EQUITY",
                "exchange": "NMS",
                "sector": "Technology",
                "industry": "Software - Application",
            },
        }

    @staticmethod
    def _info(**overrides):
        info = {
            "marketCap": 10_000_000_000,
            "sector": "Technology",
            "industry": "Software - Application",
            "operatingCashflow": 1_000_000_000,
            "grossMargins": 0.30,
            "ebitda": 1_000_000_000,
            "totalDebt": 2_000_000_000,
            "totalCash": 500_000_000,
            "totalRevenue": 5_000_000_000,
        }
        info.update(overrides)
        return info

    def _evaluate(self, **overrides):
        return hunter.evaluate_candidate(
            self._candidate(),
            self._info(**overrides),
            hunter.HunterConfig(),
            Path("cache"),
            hunter.RequestPacer(0),
            used_info_cache=True,
        )

    def test_sec_contact_email_must_be_ascii(self):
        self.assertEqual(
            hunter.validate_sec_contact_email("name@gmail.com"),
            "name@gmail.com",
        )
        with self.assertRaisesRegex(ValueError, "不能包含中文"):
            hunter.validate_sec_contact_email("你的信箱@gmail.com")

    def test_sec_exchange_payload_is_normalized(self):
        parsed = hunter.parse_sec_exchange_payload(
            {
                "fields": ["cik", "name", "ticker", "exchange"],
                "data": [[789019, "Microsoft Corp", "MSFT", "Nasdaq"]],
            }
        )
        self.assertEqual(parsed["MSFT"]["CIK"], "0000789019")
        self.assertEqual(parsed["MSFT"]["SECExchange"], "NASDAQ")

    def test_prefilter_uses_server_side_quote_metadata(self):
        quotes = [
            {
                "symbol": "MSFT",
                "quoteType": "EQUITY",
                "exchange": "NMS",
                "sector": "Technology",
            },
            {
                "symbol": "JPM",
                "quoteType": "EQUITY",
                "exchange": "NYQ",
                "sector": "Financial Services",
            },
        ]
        sec_map = {
            "MSFT": {
                "Ticker": "MSFT",
                "CIK": "0000789019",
                "Name": "Microsoft Corp",
                "SECExchange": "NASDAQ",
            },
            "JPM": {
                "Ticker": "JPM",
                "CIK": "0000019617",
                "Name": "JPMorgan Chase",
                "SECExchange": "NYSE",
            },
        }
        result = hunter.prefilter_candidates(quotes, sec_map)
        self.assertEqual([row["Ticker"] for row in result], ["MSFT"])

    def test_yahoo_query_can_be_constructed(self):
        query = hunter._build_yahoo_query(hunter.HunterConfig())
        self.assertIsNotNone(query)

    def test_windows_reserved_ticker_uses_safe_cache_filename(self):
        cache_path = hunter.ticker_cache_path(Path("cache"), "CON")
        self.assertEqual(cache_path.name, "ticker_CON.json")
        self.assertIsNone(hunter.legacy_ticker_cache_path(Path("cache"), "CON"))
        self.assertEqual(
            hunter.legacy_ticker_cache_path(Path("cache"), "MSFT").name,
            "MSFT.json",
        )

    def test_initial_market_cap_floor_matches_mode_c(self):
        self.assertEqual(hunter.MIN_MCAP_B, 5.0)

    def test_gross_margin_has_absolute_floor_and_peer_review_band(self):
        self.assertTrue(
            self._evaluate(grossMargins=0.14)["Status"].startswith(
                "Drop: 毛利率低於最低底線"
            )
        )
        peer_check = self._evaluate(grossMargins=0.20)
        self.assertTrue(peer_check["Status"].startswith("PeerCheck:"))
        self.assertEqual(peer_check["GrossMarginRule"], "等待同業中位數")
        self.assertEqual(self._evaluate(grossMargins=0.25)["Status"], "Pass")

    def test_debt_to_ebitda_is_graded_and_net_cash_is_recorded(self):
        warning = self._evaluate(
            totalDebt=4_500_000_000,
            totalCash=5_000_000_000,
        )
        self.assertEqual(warning["Status"], "Pass")
        self.assertTrue(warning["LeverageWarning"])
        self.assertTrue(warning["NetCash"])
        self.assertTrue(
            self._evaluate(totalDebt=5_100_000_000)["Status"].startswith(
                "Drop: 負債/EBITDA>5.0"
            )
        )

    def test_peer_margin_rule_uses_industry_median_with_sample_guard(self):
        rows = [
            {
                "Ticker": f"T{i}",
                "Industry": "Software - Application",
                "GrossMargin": margin,
                "Status": (
                    "PeerCheck: 毛利率低於25%，等待同業中位數比較"
                    if i == 0
                    else "Pass"
                ),
            }
            for i, margin in enumerate([22.0, 18.0, 20.0, 21.0, 23.0])
        ]
        resolved = hunter.apply_peer_margin_rules(rows)
        self.assertEqual(resolved[0]["Status"], "Pass")
        self.assertEqual(resolved[0]["IndustryMedianGrossMargin"], 21.0)

        insufficient = hunter.apply_peer_margin_rules(rows[:4])
        self.assertEqual(insufficient[0]["Status"], "Review: 同業毛利率樣本不足")


if __name__ == "__main__":
    unittest.main()
