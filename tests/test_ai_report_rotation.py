import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from run_mode_c_ai_agent import (
    load_weekly_history,
    mark_tasks_reviewed,
    sanitize_report_text,
    select_weekly_tasks,
)


class AIReportRotationTests(unittest.TestCase):
    def test_weekly_selection_skips_already_reviewed_tickers(self):
        tasks = [{"ticker": ticker} for ticker in ["AAA", "BBB", "CCC", "DDD", "EEE"]]
        history = {"week": "2026-W25", "tickers": ["AAA", "BBB"]}
        selected = select_weekly_tasks(tasks, history, limit=2)
        self.assertEqual([task["ticker"] for task in selected], ["CCC", "DDD"])

    def test_mark_reviewed_keeps_unique_order(self):
        history = {"week": "2026-W25", "tickers": ["AAA"]}
        updated = mark_tasks_reviewed(
            history,
            [{"ticker": "BBB"}, {"ticker": "AAA"}, {"ticker": "CCC"}],
        )
        self.assertEqual(updated["tickers"], ["AAA", "BBB", "CCC"])

    def test_history_resets_at_new_iso_week(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "history.json"
            path.write_text(
                json.dumps({"week": "2026-W24", "tickers": ["AAA"]}),
                encoding="utf-8",
            )
            history = load_weekly_history(
                path,
                now=datetime(2026, 6, 20, tzinfo=timezone.utc),
            )
            self.assertEqual(history["week"], "2026-W25")
            self.assertEqual(history["tickers"], [])

    def test_report_sanitizer_removes_emoji_and_invalid_controls(self):
        raw = "公司：AAA 🚀\u200b\ufffd\n\n\n結論：研究優先"
        cleaned = sanitize_report_text(raw)
        self.assertNotIn("🚀", cleaned)
        self.assertNotIn("\u200b", cleaned)
        self.assertNotIn("\ufffd", cleaned)
        self.assertNotIn("\n\n\n", cleaned)
        self.assertIn("公司:AAA", cleaned)


if __name__ == "__main__":
    unittest.main()
