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

    def test_routing_policy_invalidates_old_checkpoints(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("HUNTER_POLICY_VERSION", source)
        self.assertIn('"policy_version": HUNTER_POLICY_VERSION', source)
        self.assertIn("initial_screen_industry", source)
        self.assertIn("IndustryInitialScore", source)


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
        self.assertEqual([row["Ticker"] for row in result], ["MSFT", "JPM"])

    def test_single_letter_common_share_class_is_supported(self):
        self.assertTrue(hunter.is_standard_common_stock_symbol("BRK-B"))
        self.assertTrue(hunter.is_standard_common_stock_symbol("BF.B"))
        self.assertFalse(hunter.is_standard_common_stock_symbol("BAC-PL"))

    def test_official_symbol_directory_classifies_security_type(self):
        directory = hunter.parse_nasdaq_symbol_directory(
            "\n".join(
                [
                    "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol",
                    "PBR.A|Petrobras ADS representing Preferred Shares|N|PBR.A|N|100|N|PBR-A",
                    "BBDO|Banco Bradesco ADS each representing one Common Share|N|BBDO|N|100|N|BBDO",
                    "CIG.C|CEMIG American Depositary Receipts|N|CIG.C|N|100|N|CIG-C",
                    "V|Visa Inc.|N|V|N|100|N|V",
                    "OMAB|Grupo Aeroportuario Series B Shares|N|OMAB|N|100|N|OMAB",
                    "File Creation Time: 0713202600|||||||",
                ]
            ),
            "NASDAQ Trader test fixture",
        )
        self.assertEqual(
            directory["PBR-A"]["SecurityClass"],
            "EXCLUDED_NON_COMMON",
        )
        self.assertEqual(
            directory["BBDO"]["SecurityClass"],
            "COMMON_OR_EQUIVALENT",
        )
        self.assertEqual(directory["CIG-C"]["SecurityClass"], "AMBIGUOUS_ADS")
        self.assertEqual(directory["V"]["SecurityClass"], "COMMON_OR_EQUIVALENT")
        self.assertEqual(
            directory["OMAB"]["SecurityClass"],
            "COMMON_OR_EQUIVALENT",
        )

    def test_sec_cover_facts_resolve_common_and_preferred_ads(self):
        document = """
        <html><body>
          <ix:nonNumeric name="dei:Security12bTitle" contextRef="preferred">
            American Depositary Shares, each representing one Preferred Share
          </ix:nonNumeric>
          <ix:nonNumeric name="dei:TradingSymbol" contextRef="preferred">BBD</ix:nonNumeric>
          <ix:nonNumeric name="dei:Security12bTitle" contextRef="common">
            American Depositary Shares, each representing one Common Share
          </ix:nonNumeric>
          <ix:nonNumeric name="dei:TradingSymbol" contextRef="common">BBDO</ix:nonNumeric>
        </body></html>
        """
        titles = hunter.extract_registered_security_titles(document)
        self.assertEqual(
            hunter.classify_security_name(titles["BBD"]),
            "EXCLUDED_NON_COMMON",
        )
        self.assertEqual(
            hunter.classify_security_name(titles["BBDO"]),
            "COMMON_OR_EQUIVALENT",
        )

    def test_cached_security_names_are_reclassified_under_current_policy(self):
        refreshed = hunter.reclassify_security_directory(
            {
                "V": {
                    "SecurityName": "Visa Inc.",
                    "SecurityClass": "UNKNOWN",
                    "SecurityClassEvidenceSource": "cached fixture",
                }
            }
        )
        self.assertEqual(
            refreshed["V"]["SecurityClass"],
            "COMMON_OR_EQUIVALENT",
        )

    def test_duplicate_cik_uses_common_class_liquidity_not_market_cap(self):
        rows = [
            {
                "Ticker": "LOWLIQ",
                "CIK": "0000000100",
                "Status": "Pass",
                "SecurityClass": "COMMON_OR_EQUIVALENT",
                "AverageDailyDollarVolume_M": 1.0,
                "MarketCap_B": 20.0,
            },
            {
                "Ticker": "HIGHLIQ",
                "CIK": "0000000100",
                "Status": "Pass",
                "SecurityClass": "COMMON_OR_EQUIVALENT",
                "AverageDailyDollarVolume_M": 50.0,
                "MarketCap_B": 10.0,
            },
        ]
        resolved = hunter.resolve_share_classes(rows)
        selected = [row for row in resolved if row["Status"] == "Pass"]
        self.assertEqual([row["Ticker"] for row in selected], ["HIGHLIQ"])
        self.assertTrue(resolved[0]["Status"].startswith("Drop: duplicate CIK"))

    def test_unresolved_ads_cannot_enter_qualified_universe(self):
        rows = [
            {
                "Ticker": "ADSX",
                "CIK": "0000000200",
                "Status": "Pass",
                "SecurityClass": "UNRESOLVED_ADS",
                "MarketCap_B": 10.0,
            }
        ]
        resolved = hunter.resolve_share_classes(rows)
        self.assertTrue(resolved[0]["Status"].startswith("Review: ADS"))
        self.assertEqual(hunter.qualified_rows(rows), [])

    def test_no_industry_is_silently_blacklisted_by_default(self):
        result = self._evaluate(
            sector="Consumer Defensive",
            industry="Tobacco",
        )
        self.assertEqual(result["Status"], "Pass")

    def test_specialized_industry_is_routed_without_general_company_filters(self):
        routed = self._evaluate(
            sector="Financial Services",
            industry="Banks - Regional",
            operatingCashflow=None,
            ebitda=None,
            totalDebt=None,
        )
        self.assertEqual(routed["Status"], "Pass")
        self.assertEqual(routed["ModelRouteHint"], "BANK")
        self.assertEqual(routed["IndustryModelKey"], "BANK")
        self.assertTrue(routed["ModelSupported"])
        self.assertTrue(routed["RoutedWithoutGeneralCorporateScoring"])

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

    def test_gross_margin_only_hard_fails_when_nonpositive(self):
        self.assertTrue(
            self._evaluate(grossMargins=0.0)["Status"].startswith(
                "Drop: 毛利率低於最低底線"
            )
        )
        peer_check = self._evaluate(grossMargins=0.10)
        self.assertTrue(peer_check["Status"].startswith("PeerCheck:"))
        self.assertEqual(peer_check["GrossMarginRule"], "等待同業中位數")
        self.assertEqual(self._evaluate(grossMargins=0.25)["Status"], "Pass")

    def test_missing_yahoo_fundamentals_defer_to_sec_instead_of_disappearing(self):
        deferred = self._evaluate(
            operatingCashflow=None,
            grossMargins=None,
            ebitda=None,
            totalDebt=None,
            totalRevenue=None,
        )
        self.assertEqual(deferred["Status"], "Pass")
        self.assertTrue(deferred["RoutedToSECForMissingYahoo"])
        self.assertIn("defer to SEC", deferred["FirstLayerWarnings"])
        single_negative = self._evaluate(ebitda=-1)
        self.assertEqual(single_negative["Status"], "Pass")
        self.assertTrue(single_negative["RoutedToSECForMissingYahoo"])
        self.assertTrue(
            self._evaluate(ebitda=-1, operatingCashflow=-1)["Status"].startswith(
                "Drop: Yahoo OCF 與 EBITDA 同時非正值"
            )
        )

    def test_debt_to_ebitda_is_graded_and_net_cash_is_recorded(self):
        warning = self._evaluate(
            totalDebt=4_500_000_000,
            totalCash=5_000_000_000,
        )
        self.assertEqual(warning["Status"], "Pass")
        self.assertFalse(warning["LeverageWarning"])
        self.assertTrue(warning["GrossLeverageWarning"])
        self.assertTrue(warning["NetCash"])
        net_cash_high_gross_debt = self._evaluate(
            totalDebt=6_000_000_000,
            totalCash=7_000_000_000,
        )
        self.assertEqual(net_cash_high_gross_debt["Status"], "Pass")
        self.assertEqual(net_cash_high_gross_debt["NetDebt_EBITDA"], 0.0)
        self.assertTrue(
            self._evaluate(totalDebt=5_600_000_000, totalCash=0)["Status"].startswith(
                "Drop: 淨負債/EBITDA>5.0"
            )
        )
        gross_only = self._evaluate(totalDebt=5_600_000_000, totalCash=None)
        self.assertEqual(gross_only["Status"], "Pass")
        self.assertTrue(gross_only["LeverageWarning"])
        self.assertIn("defer net leverage to SEC", gross_only["FirstLayerWarnings"])

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
        self.assertEqual(insufficient[0]["Status"], "Pass")
        self.assertTrue(insufficient[0]["GrossMarginWarning"])

        below_median = [dict(row) for row in rows]
        below_median[0]["GrossMargin"] = 17.0
        resolved_below = hunter.apply_peer_margin_rules(below_median)
        self.assertEqual(resolved_below[0]["Status"], "Pass")
        self.assertTrue(resolved_below[0]["GrossMarginWarning"])


if __name__ == "__main__":
    unittest.main()
