import csv
import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import AQR_ModeC_Agent_V12 as engine
import build_mode_c_dashboard as dashboard
import total_market_hunter_v2 as legacy_hunter
from mode_c_watchlist import FIXED_WATCHLIST_PATH, load_fixed_watchlist


class FixedWatchlistTests(unittest.TestCase):
    def setUp(self):
        self.rows, self.version = load_fixed_watchlist()

    def write_list(self, root, rows, fields=None):
        target = Path(root) / "watchlist.csv"
        with target.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fields or list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return target

    def test_approved_membership_and_ciks_are_preserved(self):
        expected = (
            "NVDA AAPL MSFT AVGO MU AMD PLTR CSCO LRCX AMAT PANW DELL ANET CRWD KLAC "
            "CRM APH WDC UBER NOW GLW ADBE LITE MSI HPE "
            "CAT GE RTX UNP LMT PH GD VRT CSX PWR UPS MMM WM EMR CTAS ITW CMI FDX NSC RSG "
            "LLY JNJ ABBV MRK ABT DHR VRTX ISRG SYK MCK REGN BSX COR CAH EW BDX VEEV IDXX A RMD "
            "WMT COST KO PG PM PEP MO MNST TGT CL SYY EL KVUE HSY KR "
            "AMZN HD BKNG TJX ABNB DASH CVNA ROST NKE CMG EBAY DHI "
            "GOOGL META NFLX DIS APP CMCSA LYV TTWO"
        ).split()
        self.assertEqual([row["Ticker"] for row in self.rows], expected)
        self.assertEqual(len({row["CIK"] for row in self.rows}), 100)
        self.assertTrue(all(row["Watchlist_Status"] == "ACTIVE_FIXED" for row in self.rows))
        self.assertEqual(
            self.version, hashlib.sha256(FIXED_WATCHLIST_PATH.read_bytes()).hexdigest()[:16]
        )

    def test_wrong_size_is_rejected_without_auto_filling(self):
        with tempfile.TemporaryDirectory() as temp:
            for rows in (self.rows[:-1], self.rows + [self.rows[0]]):
                with self.subTest(size=len(rows)):
                    target = self.write_list(temp, rows)
                    with self.assertRaisesRegex(ValueError, "恰好 100"):
                        load_fixed_watchlist(target)

    def test_duplicate_ticker_or_cik_is_rejected_after_normalization(self):
        with tempfile.TemporaryDirectory() as temp:
            for field, value in (("Ticker", " nvda "), ("CIK", str(int(self.rows[0]["CIK"])))):
                rows = [dict(row) for row in self.rows]
                rows[1][field] = value
                with self.subTest(field=field):
                    with self.assertRaisesRegex(ValueError, "重複"):
                        load_fixed_watchlist(self.write_list(temp, rows))

    def test_invalid_identifiers_or_missing_columns_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            for field, value in (("Ticker", ""), ("Ticker", "^TNX"), ("CIK", "nan"),
                                 ("CIK", "0"), ("CIK", "1045810.0")):
                rows = [dict(row) for row in self.rows]
                rows[0][field] = value
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, "無效"):
                        load_fixed_watchlist(self.write_list(temp, rows))
            target = self.write_list(temp, [{"Ticker": row["Ticker"]} for row in self.rows])
            with self.assertRaisesRegex(ValueError, "欄位"):
                load_fixed_watchlist(target)

    def test_missing_watchlist_stops_before_any_network_fetch(self):
        with patch.object(engine, "load_fixed_watchlist", side_effect=FileNotFoundError("missing")), \
             patch.object(engine, "pre_fetch_all_market_data") as prices, \
             patch.object(engine, "pre_fetch_all_info") as info:
            with self.assertRaises(FileNotFoundError):
                engine.main()
        prices.assert_not_called()
        info.assert_not_called()

    def test_main_fetches_and_analyzes_only_fixed_names_even_with_legacy_override(self):
        # Halt after all per-company results are assembled, before writing artifacts.
        # A stale initial Pass/Fail field must not silently remove a watched company.
        rows = [dict(row) for row in self.rows]
        rows[0]["Status"] = "Fail"
        results = []

        def fake_pipeline(ticker, *args):
            result = engine.ModeCResult(Ticker=ticker, Status="Fail: 無價格資料")
            results.append(result)
            return result

        with patch.dict(os.environ, {"USER_EMAIL": "test@example.com", "QUALIFIED_UNIVERSE": "qualified_universe.csv"}), \
             patch.object(engine, "load_fixed_watchlist", return_value=(rows, self.version)), \
             patch.object(engine, "pre_fetch_all_market_data") as prices, \
             patch.object(engine, "pre_fetch_all_info") as info, \
             patch.object(engine, "hydrate_info_cache_from_verified_universe"), \
             patch.object(engine, "get_cached_series", return_value=None), \
             patch.object(engine, "run_mode_c_pipeline", side_effect=fake_pipeline) as pipeline, \
             patch.object(engine, "calibrate_exit_multiples", side_effect=RuntimeError("stop before outputs")):
            with self.assertRaisesRegex(RuntimeError, "stop before outputs"):
                engine.main()
        tickers = [row["Ticker"] for row in rows]
        prices.assert_called_once_with(tickers + ["SPY", "QQQ", "^TNX"], period="10y")
        info.assert_called_once_with(tickers)
        self.assertEqual(pipeline.call_count, 100)
        self.assertEqual({call.args[0] for call in pipeline.call_args_list}, set(tickers))
        self.assertTrue(all(result.Universe_Version == self.version for result in results))

    def test_default_input_does_not_depend_on_current_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            previous = Path.cwd()
            try:
                os.chdir(temp)
                rows, version = load_fixed_watchlist()
            finally:
                os.chdir(previous)
        self.assertEqual(rows, self.rows)
        self.assertEqual(version, self.version)

    def test_dashboard_cli_uses_the_same_fixed_file_and_separate_history(self):
        with patch("sys.argv", ["build_mode_c_dashboard.py"]), \
             patch.object(dashboard, "build_dashboard", return_value=Path("public/index.html")) as build, \
             patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(dashboard.main(), 0)
        self.assertEqual(build.call_args.args[2], FIXED_WATCHLIST_PATH)
        self.assertEqual(build.call_args.args[4], Path(".mode_c_state/fixed_watchlist_dashboard_trend_history.json"))

    def test_legacy_market_hunter_cli_cannot_start_network_run(self):
        with patch.object(legacy_hunter, "run") as run, patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(legacy_hunter.main(), 2)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
