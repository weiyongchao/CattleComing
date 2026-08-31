import copy
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_board_selection import candidate
from src.stock_evaluator.board_focus import DailyFocusError, DailyFocusStore, potential_profile
from src.stock_evaluator.board_selection import select_live_recommendations


def qualified(code="600001", **changes):
    row = candidate(code, **changes)
    now = datetime(2026, 8, 31, 10)
    observations = {}
    select_live_recommendations([row], now, "可观察", observations)
    select_live_recommendations([row], now + timedelta(seconds=20), "可观察", observations)
    return row


def failed(code):
    return candidate(code, sealed=False, failed_board=True, confirmation_samples=0,
                     recommendation_kind=None, selection_reason="炸板", tone="reject")


class DailyFocusTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "focus.json"
        self.store = DailyFocusStore(self.path)
        self.now = datetime(2026, 8, 31, 10)

    def test_one_primary_and_backups_do_not_consume_daily_quota(self):
        rows = [qualified(f"60000{i}", continuation_score=80 + i) for i in range(1, 8)]
        picks, info = self.store.select(rows, self.now)
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0]["code"], "600007")
        self.assertEqual(info["issued_count"], 1)
        self.assertEqual(sum(row["actionable"] for row in rows), 1)
        self.assertEqual(len(self.store.monitored_candidates(self.now)), 1)

    def test_live_primary_is_sticky_even_if_another_scores_higher(self):
        self.store.select([qualified()], self.now)
        picks, info = self.store.select([qualified(), qualified("600002", continuation_score=100)], self.now + timedelta(seconds=20))
        self.assertEqual(picks[0]["code"], "600001")
        self.assertEqual(info["issued_count"], 1)

    def test_only_replace_after_failure_and_daily_max_five_distinct_codes(self):
        previous = None
        for index in range(1, 8):
            rows = [qualified(f"60000{index}")]
            if previous:
                rows.append(failed(previous))
            picks, info = self.store.select(rows, self.now + timedelta(seconds=index * 20))
            self.assertEqual(info["issued_count"], min(index, 5))
            self.assertEqual(len(picks), 1 if index <= 5 else 0)
            if picks:
                previous = picks[0]["code"]
        self.assertEqual(info["status"], "daily_limit")

    def test_refresh_and_process_restart_do_not_reset_quota(self):
        self.store.select([qualified()], self.now)
        restarted = DailyFocusStore(self.path)
        picks, info = restarted.select([qualified()], self.now + timedelta(seconds=20))
        self.assertEqual(info["issued_count"], 1)
        self.assertEqual(picks[0]["code"], "600001")

    def test_failed_primary_does_not_reenter_and_prompt_again(self):
        self.store.select([qualified()], self.now)
        self.store.select([failed("600001"), qualified("600002")], self.now + timedelta(seconds=20))
        picks, info = self.store.select([qualified(), failed("600002")], self.now + timedelta(seconds=40))
        self.assertEqual(picks, [])
        self.assertEqual(info["issued_count"], 2)

    def test_missing_data_pauses_without_promoting_another(self):
        self.store.select([qualified()], self.now)
        for rows in [[qualified("600002")], [candidate(confirmation_samples=0, funds={"available": False}), qualified("600002")]]:
            picks, info = self.store.select(rows, self.now + timedelta(seconds=20))
            self.assertEqual(picks, [])
            self.assertEqual(info["status"], "awaiting_data")
            self.assertEqual(info["issued_count"], 1)

    def test_no_forced_choice_when_potential_or_continuation_below_floor(self):
        for row in [qualified(continuation_score=60), qualified(continuation_score=75, open_score=80, funds={"available": True, "main_ratio": 0})]:
            picks, info = self.store.select([row], self.now)
            self.assertEqual(picks, [])
            self.assertEqual(info["issued_count"], 0)

    def test_lock_stops_new_names_even_when_locked_stock_fails(self):
        self.store.select([qualified()], self.now)
        self.store.lock("600001", self.now)
        picks, info = self.store.select([failed("600001"), qualified("600002")], self.now + timedelta(seconds=20))
        self.assertEqual(picks, [])
        self.assertEqual(info["locked_code"], "600001")
        self.assertEqual(info["issued_count"], 1)
        recovered, _ = self.store.select([qualified()], self.now + timedelta(seconds=40))
        self.assertFalse(recovered[0]["actionable"])
        self.assertTrue(recovered[0]["focus_locked"])

    def test_unlock_does_not_refund_daily_quota(self):
        self.store.select([qualified()], self.now)
        self.store.lock("600001", self.now)
        result = self.store.lock(None, self.now)
        self.assertEqual(result["issued_count"], 1)
        with self.assertRaises(ValueError):
            self.store.lock("600999", self.now)

    def test_next_day_has_independent_quota_and_lock(self):
        self.store.select([qualified()], self.now)
        self.store.lock("600001", self.now)
        _, info = self.store.select([qualified("600002")], self.now + timedelta(days=1))
        self.assertEqual(info["issued_count"], 1)
        self.assertIsNone(info["locked_code"])
        self.assertEqual(info["issued"][0]["code"], "600002")

    def test_corrupt_or_unwritable_ledger_fails_closed(self):
        with patch.object(Path, "read_text", return_value="bad"), patch.object(Path, "exists", return_value=True):
            with self.assertRaises(DailyFocusError):
                self.store.select([qualified()], self.now)
        row = qualified()
        with patch.object(Path, "write_text", side_effect=OSError("disk failure")):
            with self.assertRaises(DailyFocusError):
                self.store.select([row], self.now)
        self.assertFalse(row["recommended"])

    def test_out_of_order_scan_cannot_change_focus(self):
        self.store.select([qualified()], self.now)
        picks, info = self.store.select([failed("600001"), qualified("600002")], self.now - timedelta(seconds=20))
        self.assertEqual(picks, [])
        self.assertEqual(info["status"], "stale")
        self.assertEqual(info["issued_count"], 1)

    def test_multiple_request_threads_share_the_same_first_choice(self):
        def scan(index):
            return self.store.select([qualified("600001"), qualified("600002")], self.now)[1]
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(scan, range(12)))
        self.assertTrue(all(result["issued_count"] == 1 for result in results))

    def test_potential_score_is_transparent_and_risk_penalized(self):
        score, basis = potential_profile(candidate())
        risky, _ = potential_profile(candidate(regulatory_risk={"level": "watch"}))
        self.assertEqual(score, 82)
        self.assertEqual(risky, score - 10)
        self.assertEqual(len(basis), 4)


if __name__ == "__main__":
    unittest.main()
