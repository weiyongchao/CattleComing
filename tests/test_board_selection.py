import copy
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch
from tempfile import TemporaryDirectory
from pathlib import Path

from src.stock_evaluator.board_selection import intraday_selection_window, select_live_recommendations
from src.stock_evaluator import open_guard
from src.stock_evaluator.board_plan import auction_observation_view
from src.stock_evaluator.board_focus import DailyFocusStore


def candidate(code="600001", **changes):
    return {
        "code": code, "name": "测试股票", "listed_sessions": 100,
        "float_market_cap": 5_000_000_000, "corporate_event_checked": True,
        "regulatory_risk": {"level": "normal"}, "open_score": 95,
        "continuation_score": 80, "amount": 100_000_000, "bid_volume5": 10000,
        "ask_volume5": 0, "order_imbalance": 1,
        "funds": {"available": True, "main_ratio": 5}, "tone": "confirm",
        "sealed": True, "price": 11, "limit_up_price": 11,
        "change_percent": 10, "auction_gap_percent": 6,
        "price_vs_open_percent": 1, "price_vs_auction_percent": 1,
        "high_turnover_chain_matched": True, "auction_rank": 1,
        **changes,
    }


class BoardSelectionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 31, 9, 32)
        self.observations = {}

    def select(self, rows, seconds=0, market="可观察"):
        return select_live_recommendations(rows, self.now + timedelta(seconds=seconds), market, self.observations)

    def test_sealed_requires_two_spaced_samples(self):
        row = candidate()
        self.assertEqual(self.select([row]), [])
        self.assertFalse(row["recommended"])
        self.assertEqual(self.select([row], 5), [])
        self.assertEqual(row["confirmation_samples"], 1)
        self.assertEqual(len(self.select([row], 20)), 1)
        self.assertTrue(row["board_entry_allowed"])
        self.assertEqual(row["recommendation_kind"], "sealed")

    def test_ordinary_open_confirmation_is_never_a_pick(self):
        row = candidate(sealed=False, high_turnover_chain_matched=False, price=10.7, change_percent=7)
        self.select([row])
        self.assertEqual(self.select([row], 20), [])
        self.assertIn("等待实际封板", row["selection_reason"])

    def test_strong_open_exception_also_requires_two_samples(self):
        row = candidate(sealed=False, price=10.7, change_percent=7)
        self.assertEqual(self.select([row]), [])
        self.assertEqual(len(self.select([row], 20)), 1)
        self.assertEqual(row["recommendation_kind"], "strong_open")
        self.assertFalse(row["board_entry_allowed"])

    def test_strong_open_exception_closes_after_0935(self):
        row = candidate(sealed=False, price=10.7, change_percent=7)
        self.select([row])
        self.assertEqual(self.select([row], 240), [])

    def test_strong_open_exception_rejects_boundary_and_weak_fields(self):
        for changes in [{"change_percent": 8.5}, {"auction_gap_percent": 4.99},
                        {"price_vs_open_percent": .49}, {"order_imbalance": .34},
                        {"open_score": 89}, {"continuation_score": 74},
                        {"opening_dip": True}, {"late_final_seal_watch": True},
                        {"regulatory_risk": {"level": "watch"}},
                        {"funds": {"available": True, "main_ratio": 2.99}}]:
            with self.subTest(changes=changes):
                row = candidate(sealed=False, price=10.7, **{"change_percent": 7, **changes})
                self.select([row])
                self.assertEqual(self.select([row], 20), [])

    def test_max_five_with_sealed_priority_and_unique_codes(self):
        rows = [candidate(f"60000{i}", open_score=80 + i) for i in range(1, 8)]
        rows.append(candidate("600008", sealed=False, change_percent=7, open_score=100))
        rows.append(copy.deepcopy(rows[0]))
        self.select(rows)
        selected = self.select(rows, 20)
        self.assertEqual(len(selected), 5)
        self.assertEqual(len({row["code"] for row in selected}), 5)
        self.assertTrue(all(row["sealed"] for row in selected))
        self.assertEqual([row["recommendation_rank"] for row in selected], [1, 2, 3, 4, 5])
        self.assertEqual(sum(row["recommended"] for row in rows), 5)

    def test_failed_board_resets_qualification_and_reseal_needs_two_new_samples(self):
        row = candidate()
        self.select([row])
        self.select([row], 20)
        row.update(sealed=False, failed_board=True)
        self.assertEqual(self.select([row], 40), [])
        self.assertFalse(row["recommended"])
        row.update(sealed=True, failed_board=False)
        self.assertEqual(self.select([row], 60), [])
        self.assertEqual(len(self.select([row], 80)), 1)

    def test_quality_failures_never_promote(self):
        bad = [
            {"name": "*ST测试"}, {"name": "退市测试"}, {"code": "300001"},
            {"listed_sessions": 59}, {"float_market_cap": 20_000_000_000},
            {"corporate_event_risk": {"level": "high", "available": True}},
            {"corporate_event_risk": {"level": "unknown", "available": False}},
            {"corporate_event_checked": False}, {"regulatory_risk": None},
            {"regulatory_risk": {"level": "high"}}, {"risk_veto": True},
            {"near_limit_failure": True}, {"amount": 49_999_999},
            {"open_score": 79}, {"continuation_score": 59},
            {"bid_volume5": None}, {"ask_volume5": None}, {"ask_volume5": 100},
            {"order_imbalance": float("nan")}, {"price": 10.99},
            {"funds": {"available": False}},
            {"funds": {"available": True, "main_ratio": -1.01}},
        ]
        for changes in bad:
            with self.subTest(changes=changes):
                row = candidate(**changes)
                self.select([row])
                self.assertEqual(self.select([row], 20), [])
                self.assertFalse(row["execution_ready"])

    def test_market_gate_cannot_be_overridden(self):
        row = candidate()
        self.select([row])
        self.assertEqual(self.select([row], 20, "空仓"), [])
        self.assertEqual(self.select([row], 40, "未知"), [])

    def test_stale_or_missing_samples_reset_count(self):
        row = candidate()
        self.select([row])
        self.assertEqual(self.select([row], 91), [])
        self.assertEqual(row["confirmation_samples"], 1)
        self.select([], 100)
        self.assertEqual(self.select([row], 120), [])
        self.assertEqual(row["confirmation_samples"], 1)

    def test_switching_from_open_to_sealed_requires_new_samples(self):
        row = candidate(sealed=False, change_percent=7)
        self.select([row])
        self.select([row], 20)
        row.update(sealed=True, change_percent=10)
        self.assertEqual(self.select([row], 40), [])
        self.assertEqual(len(self.select([row], 60)), 1)

    def test_selection_sessions(self):
        for hour, minute, expected in [(9, 29, False), (9, 30, True), (10, 10, True),
                                      (11, 30, False), (12, 5, False), (13, 0, True),
                                      (14, 56, True), (14, 57, False), (15, 0, False)]:
            self.assertEqual(intraday_selection_window(self.now.replace(hour=hour, minute=minute)), expected)
        self.assertFalse(intraday_selection_window(datetime(2026, 8, 30, 10)))


class OpenGuardSelectionIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store_patch = patch.object(open_guard, "DAILY_FOCUS_STORE", DailyFocusStore(Path(temporary.name) / "focus.json"))
        store_patch.start()
        self.addCleanup(store_patch.stop)
        self.now = datetime(2026, 8, 31, 10, 10)
        self.snapshot = {"selected_date": "2026-08-31", "market": {"state": "可观察"},
                         "candidates": [candidate()], "watch_candidates": [candidate("600002")]}

    def test_missing_or_nonfinite_fund_ratio_is_not_zero_inflow(self):
        result = {
            "quote": {"price": 10.2, "open_price": 10, "change_percent": 3, "amount": 50_000_000},
            "metrics": {"price_vs_open_percent": 2, "price_vs_ma5_percent": 5, "volume_ratio": 1, "turnover_rate": 1},
            "order_book": {"imbalance": .2, "signal": "买盘占优"},
            "regulatory_risk": {"level": "normal", "label": "常规"},
        }
        for value in [None, "-", float("nan"), float("inf")]:
            with self.subTest(value=value):
                funds = {"is_today": True, "date": date.today().isoformat(), "main_ratio": value}
                row = open_guard._open_confirmation(candidate(auction_price=10), result, funds)
                self.assertFalse(row["funds"]["available"])
                self.assertIsNone(row["funds"]["main_ratio"])

    def test_guard_consumes_watch_pool_and_returns_only_confirmed_top_five(self):
        with patch.object(open_guard, "_SELECTION_OBSERVATIONS", {}), \
             patch.object(open_guard, "attach_corporate_event_risks"), \
             patch.object(open_guard, "_check_one", side_effect=lambda item, *_: copy.deepcopy(item)):
            first = open_guard.build_open_guard(self.snapshot, discover_live=False, now=self.now)
            self.assertEqual(first["candidates"], [])
            self.assertEqual(len(first["watch_candidates"]), 2)
            self.assertNotIn("小仓试错", first["watch_candidates"][0]["entry_advice"])
            second = open_guard.build_open_guard(self.snapshot, discover_live=False, now=self.now + timedelta(seconds=20))
            self.assertEqual(len(second["candidates"]), 1)
            self.assertTrue(second["candidates"][0]["primary_pick"])
            self.assertEqual(second["daily_focus"]["issued_count"], 1)
            self.assertEqual(second["recommendation_limit"], 5)

    def test_historical_snapshot_never_produces_current_picks(self):
        self.snapshot["selected_date"] = "2026-08-28"
        with patch.object(open_guard, "_SELECTION_OBSERVATIONS", {}), \
             patch.object(open_guard, "attach_corporate_event_risks"), \
             patch.object(open_guard, "_check_one", side_effect=lambda item, *_: copy.deepcopy(item)):
            open_guard.build_open_guard(self.snapshot, discover_live=False, now=self.now)
            result = open_guard.build_open_guard(self.snapshot, discover_live=False, now=self.now + timedelta(seconds=20))
            self.assertEqual(result["candidates"], [])
            self.assertTrue(result["historical"])

    def test_late_session_discovers_near_board_without_rewriting_auction_pool(self):
        quotes = [{"f12": "600003", "f3": 10}, {"f12": "600004", "f3": 5}]
        with patch.object(open_guard, "_SELECTION_OBSERVATIONS", {}), \
             patch.object(open_guard, "_OPENING_DISCOVERY_CACHE", {}), \
             patch.object(open_guard, "attach_corporate_event_risks"), \
             patch.object(open_guard, "main_board_snapshots", return_value=quotes), \
             patch.object(open_guard, "_discover_live_one_to_two", return_value=[candidate("600003")]) as discover, \
             patch.object(open_guard, "_discover_live_multi_board", return_value=[]), \
             patch.object(open_guard, "_check_one", side_effect=lambda item, *_: copy.deepcopy(item)):
            result = open_guard.build_open_guard(self.snapshot, now=self.now)
            self.assertEqual(discover.call_args.kwargs["snapshots"], quotes[:1])
            discovered = next(row for row in result["watch_candidates"] if row["code"] == "600003")
            self.assertEqual(discovered["discovery_source"], "盘中封板补选")
            self.assertTrue(discovered["discovered_at"].startswith("2026-08-31T10:10"))
            self.assertEqual(len(self.snapshot["candidates"]), 1)


class AuctionObservationViewTests(unittest.TestCase):
    def test_old_cache_cannot_restore_buy_flags_and_is_not_mutated(self):
        original = {"candidates": [candidate(f"60000{i}", recommended=True, actionable=True,
                    execution_ready=True, board_entry_allowed=True, recommendation_badge="旧版买点")
                    for i in range(1, 8)], "position_plan": {"max_positions": 2, "per_position": 15000}}
        before = copy.deepcopy(original)
        result = auction_observation_view(original)
        self.assertEqual(len(result["candidates"]), 5)
        self.assertEqual(len(result["watch_candidates"]), 2)
        self.assertEqual(result["position_plan"]["max_positions"], 0)
        self.assertTrue(all(not row["actionable"] and not row["recommended"] and not row["board_entry_allowed"]
                            for row in result["candidates"] + result["watch_candidates"]))
        self.assertEqual(original, before)
        self.assertEqual(auction_observation_view(result), result)


if __name__ == "__main__":
    unittest.main()
