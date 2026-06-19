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
        self.assertIn("process.terminate()", source)
        self.assertIn("--request-timeout", source)

    def test_partial_run_does_not_replace_complete_universe(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("qualified_universe.partial.csv", source)
        self.assertIn("write_complete_outputs", source)
        self.assertIn("if complete:", source)


@unittest.skipIf(hunter is None, "project dependencies are not installed locally")
class HunterRuntimeTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
