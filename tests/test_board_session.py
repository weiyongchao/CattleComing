import copy
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from src.stock_evaluator import history, server
from src.stock_evaluator.auction import _auction_candidate, _early_final_seal_chain_score, _high_turnover_auction_chain_score
from src.stock_evaluator.board_plan import auction_observation_view
from src.stock_evaluator.board_session import BoardSessionRunner, session_phase, timely_final
from src.stock_evaluator.market import MarketDataError
from src.stock_evaluator.open_guard import _opening_market_context
from src.stock_evaluator.quote_sampling import china_time


class BoardSessionTests(unittest.TestCase):
    def test_session_time_boundaries(self):
        for clock, phase in [("09:16:59", "closed"), ("09:17:00", "warmup"),
                             ("09:20:00", "indicative"), ("09:24:59", "indicative"),
                             ("09:25:00", "final"), ("09:29:59", "final"),
                             ("09:30:00", "opening"), ("09:35:00", "intraday"),
                             ("11:30:00", "closed"), ("13:00:00", "intraday"),
                             ("14:57:00", "closed")]:
            with self.subTest(clock=clock):
                self.assertEqual(session_phase(datetime.fromisoformat(f"2026-08-31T{clock}")), phase)
        self.assertEqual(session_phase(datetime(2026, 8, 30, 9, 22)), "closed")

    def test_runner_collects_without_browser_and_changes_phase_immediately(self):
        auction, opening = Mock(return_value={}), Mock(return_value={})
        runner = BoardSessionRunner(auction, opening)
        start = datetime(2026, 8, 31, 9, 24, 50)
        self.assertTrue(runner.step(start))
        self.assertFalse(runner.step(start + timedelta(seconds=5)))
        self.assertTrue(runner.step(start + timedelta(seconds=10)))
        self.assertTrue(runner.step(start.replace(minute=30, second=0)))
        self.assertFalse(runner.step(start.replace(hour=12)))
        self.assertEqual(auction.call_count, 2)
        self.assertEqual(opening.call_count, 1)

    def test_runner_recovers_from_provider_failure(self):
        auction = Mock(side_effect=[MarketDataError("offline"), {}])
        runner = BoardSessionRunner(auction, Mock())
        at = datetime(2026, 8, 31, 9, 22)
        runner.step(at)
        self.assertIn("offline", runner.status()["last_error"])
        runner.step(at + timedelta(seconds=20))
        self.assertIsNone(runner.status()["last_error"])
        self.assertIsNotNone(runner.status()["last_success_at"])

    def test_only_actual_preopen_final_is_timely(self):
        now = datetime(2026, 8, 31, 10)
        valid = {"selected_date": "2026-08-31", "generated_at": "2026-08-31T09:25:10+08:00",
                 "auction_phase": "final", "snapshot_kind": "actual_final"}
        self.assertTrue(timely_final(valid, now))
        for changes in [{"generated_at": "2026-08-31T16:00:00+08:00"},
                        {"generated_at": "2026-08-31T09:30:00+08:00"},
                        {"generated_at": "2026-08-31T09:24:59+08:00"},
                        {"selected_date": "2026-08-28"}, {"stale_fallback": True}, {"data_degraded": True},
                        {"snapshot_kind": "latest_strategy_replay"}, {"historical": True}]:
            self.assertFalse(timely_final({**valid, **changes}, now))

    def test_preselection_is_capped_and_never_a_buy_signal(self):
        rows = [{"code": f"60000{i}", "name": f"测试{i}", "recommended": True,
                 "actionable": True, "execution_ready": True} for i in range(7)]
        view = auction_observation_view({"auction_phase": "indicative", "candidates": rows})
        self.assertEqual(len(view["candidates"]), 5)
        self.assertEqual(view["workflow"]["stage"], "preselection")
        self.assertEqual(view["workflow"]["observation_focus_code"], "600000")
        self.assertTrue(all(not row["recommended"] and not row["actionable"] for row in view["candidates"]))
        self.assertIsNone(auction_observation_view({"auction_phase": "indicative", "candidates": rows,
                                                  "stale_fallback": True})["workflow"]["observation_focus_code"])

    def test_early_chain_can_preview_without_claiming_final_confirmation(self):
        values = dict(consecutive_limit_ups=2, previous_final_seal_time=100000,
                      gap_percent=4, auction_volume_percent=3, auction_amount=40_000_000,
                      float_market_cap=5_000_000_000, listed_sessions=100, exact_auction=False)
        self.assertFalse(_early_final_seal_chain_score(**values)[1])
        self.assertTrue(_early_final_seal_chain_score(**values, indicative=True)[1])
        self.assertFalse(_early_final_seal_chain_score(**{**values, "auction_amount": 30_000_000}, indicative=True)[1])

    def test_high_turnover_preview_does_not_loosen_thresholds(self):
        values = dict(consecutive_limit_ups=2, gap_percent=5, auction_turnover_percent=1.3,
                      auction_amount=60_000_000, float_market_cap=5_000_000_000,
                      listed_sessions=100, exact_auction=False)
        self.assertFalse(_high_turnover_auction_chain_score(**values)[1])
        result = _high_turnover_auction_chain_score(**values, indicative=True)
        self.assertTrue(result[1])
        self.assertIn("参考", result[2][1])
        for changes in [{"gap_percent": 4.99}, {"auction_turnover_percent": 1.2}, {"auction_amount": 50_000_000}]:
            self.assertFalse(_high_turnover_auction_chain_score(**{**values, **changes}, indicative=True)[1])

    def test_preliminary_missing_or_yesterday_timestamp_never_uses_clock_now(self):
        provider = Mock()
        provider.history.side_effect = AssertionError("不应继续读取历史")
        for timestamp in [None, china_time(datetime.now() - timedelta(days=1)).timestamp()]:
            with self.assertRaises(MarketDataError):
                _auction_candidate({"f12": "600001", "f14": "测试", "f2": 10, "f124": timestamp},
                                   provider, preliminary=True)
        provider.history.assert_not_called()

    def test_opening_market_can_update_an_empty_auction_gate_with_fresh_breadth(self):
        now = china_time(datetime(2026, 8, 31, 9, 32))
        rows = [{"f2": 10, "f3": 10 if i < 50 else 1, "f124": now.timestamp()} for i in range(500)]
        result = _opening_market_context(rows, now, 3, {"state": "空仓", "score": 0})
        self.assertTrue(result["intraday_refreshed"])
        self.assertEqual(result["state"], "可观察")
        stale = _opening_market_context(rows, now + timedelta(seconds=61), 3, {"state": "空仓", "score": 0})
        self.assertFalse(stale["intraday_refreshed"])
        self.assertEqual(stale["state"], "空仓")


class BoardSessionStorageTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for target, value in [("BOARD_PLAN_FILE", Path(directory.name) / "snapshots.json"),
                              ("DATA_FILE", Path(directory.name) / "history.json")]:
            patcher = patch.object(history, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, value in [("board_plan_cache", None), ("board_open_cache", None)]:
            patcher = patch.object(server.AppHandler, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.now = datetime.combine(date.today(), datetime.min.time()).replace(hour=9, minute=25, second=10)
        self.payload = {"selected_date": self.now.date().isoformat(), "generated_at": self.now.isoformat(),
                        "auction_phase": "final", "strategy_version": server.BOARD_STRATEGY_VERSION,
                        "candidates": [{"code": "600001", "name": "测试"}], "screening": {"failed": 0},
                        "market": {"state": "谨慎"}}

    def clock(self, at):
        patcher = patch.object(server, "datetime")
        clock = patcher.start()
        self.addCleanup(patcher.stop)
        clock.now.return_value = at
        return clock

    def test_final_is_frozen_once_and_never_replaced_by_larger_replay(self):
        clock = self.clock(self.now)
        with patch.object(server.AppHandler, "_build_board_plan_singleflight", return_value=copy.deepcopy(self.payload)) as build, \
             patch.object(server, "record_payload_sample"), patch.object(server, "attach_trajectory", side_effect=lambda p: p), \
             patch.object(server, "_record_safely"):
            first = server.AppHandler._session_board_plan()
            self.assertEqual(first["snapshot_kind"], "actual_final")
            history.save_board_plan_snapshot({**self.payload, "candidates": [{"code": "600002"}, {"code": "600003"}]}, "replay")
            clock.now.return_value = self.now.replace(hour=10)
            second = server.AppHandler._session_board_plan()
            self.assertEqual(second["candidates"], first["candidates"])
            self.assertEqual(build.call_count, 1)

    def test_scan_finishing_after_open_cannot_create_final(self):
        late = self.now.replace(minute=31)
        self.clock(late)
        with patch.object(server.AppHandler, "_build_board_plan_singleflight", return_value={**self.payload, "generated_at": late.isoformat()}):
            result = server.AppHandler._session_board_plan()
        self.assertEqual(result["snapshot_kind"], "live_opening_observation")
        self.assertEqual(result["candidates"][0]["discovery_source"], "开盘后新增观察")
        self.assertIsNone(history.load_board_plan_snapshot(self.now.date().isoformat(), "final"))

    def test_final_failure_does_not_freeze_empty_pool(self):
        self.clock(self.now)
        with patch.object(server.AppHandler, "_build_board_plan_singleflight", return_value={**self.payload, "candidates": [], "screening": {"failed": 7}}), \
             patch.object(server, "record_payload_sample"), patch.object(server, "attach_trajectory", side_effect=lambda p: p), \
             patch.object(server, "_record_safely"):
            result = server.AppHandler._session_board_plan()
        self.assertTrue(result["data_degraded"])
        self.assertIsNone(history.load_board_plan_snapshot(self.now.date().isoformat(), "final"))

    def test_opening_seed_ignores_replay_and_later_cache(self):
        self.clock(self.now.replace(hour=10))
        history.save_board_plan_snapshot(self.payload, "final")
        history.save_board_plan_snapshot({**self.payload, "candidates": [{"code": "600009"}]}, "replay")
        server.AppHandler.board_plan_cache = (0, {**self.payload, "candidates": [{"code": "600008"}]})
        with patch.object(server, "build_open_guard", return_value={}) as build:
            result = server.AppHandler._session_open_guard()
        self.assertEqual(build.call_args.args[0]["candidates"][0]["code"], "600001")
        self.assertTrue(result["auction_seed"]["actual_preopen"])

    def test_frozen_only_cannot_use_replay_when_no_final(self):
        self.clock(self.now.replace(hour=10))
        history.save_board_plan_snapshot(self.payload, "replay")
        with self.assertRaisesRegex(ValueError, "缺少开盘前"):
            server.AppHandler._session_open_guard(frozen_only=True)

    def test_yesterday_cache_is_not_reused_or_fallback(self):
        server.AppHandler.board_plan_cache = (server.time.time(), {**self.payload,
            "selected_date": (date.today() - timedelta(days=1)).isoformat()})
        with patch.object(server, "build_board_plan", side_effect=MarketDataError("offline")):
            with self.assertRaises(MarketDataError):
                server.AppHandler._build_board_plan_singleflight()


if __name__ == "__main__":
    unittest.main()
