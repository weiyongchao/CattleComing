from __future__ import annotations

from datetime import datetime
import copy
import unittest
from unittest.mock import patch

from src.stock_evaluator.late_red import (
    LateRedRunner,
    aggregate_ten_minute_bars,
    build_late_red_screen,
    evaluate_late_red_snapshot,
    refresh_late_red_screen,
    waiting_late_red_screen,
)
from src.stock_evaluator.server import AppHandler


def five_minute_rows(closes: list[float]) -> list[str]:
    ends = ["13:40", "13:50", "14:00", "14:10", "14:20", "14:30", "14:40"]
    rows: list[str] = []
    for close, end in zip(closes, ends):
        hour, minute = map(int, end.split(":"))
        first_minute = minute - 5
        first_hour = hour
        if first_minute < 0:
            first_hour -= 1
            first_minute += 60
        for time_text, amount in ((f"{first_hour:02d}:{first_minute:02d}", 500_000), (end, 600_000)):
            rows.append(
                f"2026-09-02 {time_text},{close:.2f},{close:.2f},{close:.2f},{close:.2f},1000,{amount:.2f},0,0,0,0"
            )
    return rows


def snapshot(code: str = "600001", *, change: float = 0.1, listed: str = "20200101") -> dict:
    return {
        "f12": code, "f14": f"样本{code}", "f2": 10.01, "f3": change,
        "f6": 100_000_000, "f8": 2.0, "f15": 10.05, "f16": 9.8,
        "f18": 10.0, "f20": 10_000_000_000, "f21": 8_000_000_000,
        "f26": listed, "f62": 50_000_000, "f184": 3.0,
        "previous_day_change_percent": -1.0,
    }


class LateRedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 2, 14, 41)
        self.matching = five_minute_rows([9.98, 9.96, 9.94, 9.93, 9.92, 9.91, 10.01])

    def test_aggregates_complete_five_minute_pairs_only(self):
        rows = self.matching + [
            "2026-09-02 14:45,10.02,10.02,10.02,10.02,100,100000,0,0,0,0"
        ]
        bars = aggregate_ten_minute_bars(rows)
        self.assertEqual(len(bars), 7)
        self.assertEqual(bars[-1]["end"], "14:40")
        self.assertEqual(bars[-1]["amount"], 1_100_000)

    def test_requires_previous_day_green_today_red_and_fresh_ma5_turn(self):
        result = evaluate_late_red_snapshot(snapshot(), self.matching, -1.0)
        self.assertTrue(result["red_reversal"])
        self.assertTrue(result["ma5_turned_up"])
        self.assertEqual(result["target_change_percent"], 0.1)

        earlier_red = evaluate_late_red_snapshot(
            snapshot(), five_minute_rows([10.02, 9.96, 9.94, 9.93, 9.92, 9.91, 10.01]),
            -1.0,
        )
        self.assertTrue(earlier_red["red_reversal"])

        previous_day_red = evaluate_late_red_snapshot(snapshot(), self.matching, 1.0)
        self.assertFalse(previous_day_red["red_reversal"])

        already_rising = evaluate_late_red_snapshot(
            snapshot(), five_minute_rows([9.90, 9.91, 9.92, 9.93, 9.94, 9.95, 10.01]),
            -1.0,
        )
        self.assertTrue(already_rising["red_reversal"])
        self.assertFalse(already_rising["ma5_turned_up"])

    def test_build_filters_new_and_event_risk_then_ranks_safe_match(self):
        rows = [
            snapshot("600001"),
            snapshot("600002"),
            snapshot("600003", listed="20260820"),
        ]

        def fetcher(code, target):
            self.assertEqual(target.isoformat(), "2026-09-02")
            return self.matching

        def attach(items):
            for item in items:
                item["corporate_event_risk"] = (
                    {"level": "high", "label": "并购重组事项进行中", "is_merger_acquisition": True}
                    if item["code"] == "600002" else {"level": "normal", "label": "常规"}
                )

        payload = build_late_red_screen(
            self.now, snapshots=rows, fetcher=fetcher, event_attacher=attach,
        )
        self.assertEqual(payload["prefiltered"], 2)
        self.assertEqual(payload["matched_count"], 2)
        self.assertEqual([item["code"] for item in payload["candidates"]], ["600001"])
        self.assertEqual(payload["risk_excluded_count"], 1)
        self.assertEqual(payload["coverage_percent"], 100.0)

    def test_prefilter_does_not_drop_fast_first_red_above_two_percent(self):
        payload = build_late_red_screen(
            self.now, snapshots=[snapshot(change=5.0)],
            fetcher=lambda code, target: self.matching,
            event_attacher=lambda items: [item.update(corporate_event_risk={"level": "normal"}) for item in items],
        )
        self.assertEqual(payload["prefiltered"], 1)
        self.assertEqual(payload["qualified_count"], 1)

    def test_refresh_removes_match_after_it_turns_green(self):
        payload = build_late_red_screen(
            self.now, snapshots=[snapshot()], fetcher=lambda code, target: self.matching,
            event_attacher=lambda items: [item.update(corporate_event_risk={"level": "normal"}) for item in items],
        )
        self.assertEqual(payload["qualified_count"], 1)
        refresh_late_red_screen(payload, [{**snapshot(), "f3": -0.1, "f2": 9.99}], now=self.now)
        self.assertEqual(payload["qualified_count"], 0)
        self.assertEqual(payload["invalidated_count"], 1)
        self.assertEqual(payload["matches"][0]["status"], "翻绿失效")

    def test_refresh_only_returns_top_three_with_one_primary(self):
        matches = []
        snapshots = []
        for index, ratio in enumerate((0.8, 1.1, 1.4, 2.2), start=1):
            code = f"60000{index}"
            matches.append({
                "code": code, "name": f"样本{index}", "target_change_percent": 0.1,
                "previous_day_change_percent": -1.0, "ten_minute_volume_ratio": ratio,
                "ma5_slope_bp": 8.0, "amount": 100_000_000,
                "turnover_rate": 2.0, "corporate_event_risk": {"level": "normal"},
            })
            snapshots.append({
                "f12": code, "f2": 10.1, "f3": 1.0,
                "f6": 100_000_000, "f8": 2.0,
            })
        payload = refresh_late_red_screen({"matches": matches}, snapshots, now=self.now)
        self.assertEqual(payload["eligible_count"], 4)
        self.assertEqual(payload["qualified_count"], 3)
        self.assertEqual(len(payload["candidates"]), 3)
        self.assertEqual(payload["recommendation_limit"], 3)
        self.assertEqual(payload["primary_code"], payload["candidates"][0]["code"])
        scores = [item["potential_score"] for item in payload["candidates"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual([item["rank"] for item in payload["candidates"]], [1, 2, 3])

    def test_ranking_prefers_main_flow_then_turnover_and_only_references_cap(self):
        matches = [{
            "code": code, "name": code, "target_change_percent": 0.1,
            "previous_day_change_percent": -1.0, "ten_minute_volume_ratio": 1.2,
            "ma5_slope_bp": 8.0, "corporate_event_risk": {"level": "normal"},
        } for code in ("600001", "600002")]
        snapshots = [
            {"f12": "600001", "f2": 10.1, "f3": 1.0, "f6": 100_000_000,
             "f8": 1.2, "f20": 120_000_000_000, "f21": 110_000_000_000,
             "f62": 200_000_000, "f184": 6.0},
            {"f12": "600002", "f2": 10.1, "f3": 1.0, "f6": 100_000_000,
             "f8": 5.0, "f20": 6_000_000_000, "f21": 5_000_000_000,
             "f62": 0, "f184": 0.0},
        ]
        payload = refresh_late_red_screen(
            {"matches": matches}, snapshots, now=self.now.replace(minute=45),
        )
        self.assertEqual(payload["eligible_count"], 2)
        self.assertEqual(payload["primary_code"], "600001")
        factors = payload["candidates"][0]["ranking_factor_scores"]
        self.assertGreater(factors["main_flow"], factors["turnover"])
        self.assertGreater(factors["turnover"], factors["market_cap"])

    def test_invalid_fund_data_does_not_reuse_old_inflow_or_change_membership(self):
        for invalid in (None, "-", "bad", float("nan"), float("inf")):
            with self.subTest(invalid=invalid):
                payload = build_late_red_screen(
                    self.now, snapshots=[snapshot()], fetcher=lambda code, target: self.matching,
                    event_attacher=lambda items: None,
                )
                self.assertTrue(payload["candidates"][0]["main_flow_available"])
                refresh_late_red_screen(payload, [{**snapshot(), "f184": invalid}], now=self.now)
                self.assertEqual(payload["eligible_count"], 1)
                item = payload["candidates"][0]
                self.assertFalse(item["main_flow_available"])
                self.assertIsNone(item["main_ratio"])
                self.assertEqual(item["ranking_factor_scores"]["main_flow"], 0)

    def test_total_cap_is_not_used_as_float_cap_when_float_cap_is_missing(self):
        payload = build_late_red_screen(
            self.now, snapshots=[{**snapshot(), "f21": "-"}],
            fetcher=lambda code, target: self.matching, event_attacher=lambda items: None,
        )
        item = payload["candidates"][0]
        self.assertIsNone(item["float_market_cap"])
        self.assertEqual(item["ranking_factor_scores"]["market_cap"], 0)
        self.assertEqual(item["market_cap"], 10_000_000_000)

    def test_refresh_marks_dynamic_window_then_freezes_at_1450(self):
        item = {
            "code": "600001", "name": "样本", "target_change_percent": 0.1,
            "previous_day_change_percent": -1.0, "ten_minute_volume_ratio": 1.2,
            "ma5_slope_bp": 8.0, "corporate_event_risk": {"level": "normal"},
        }
        live = refresh_late_red_screen(
            {"matches": [item]}, [snapshot()], now=self.now.replace(minute=49),
        )
        self.assertFalse(live["frozen"])
        self.assertIn("动态确认", live["stage"])
        before = copy.deepcopy(live["candidates"])
        frozen = refresh_late_red_screen(
            live, [snapshot(change=-2)], now=self.now.replace(minute=50),
        )
        self.assertTrue(frozen["frozen"])
        self.assertEqual(frozen["stage"], "14:50名单已冻结")
        self.assertEqual(frozen["candidates"], before)
        refresh_late_red_screen(frozen, [snapshot(change=-5)], now=self.now.replace(minute=55))
        self.assertEqual(frozen["candidates"], before)

    def test_late_first_build_is_reference_not_actual_frozen_recommendation(self):
        payload = build_late_red_screen(
            self.now.replace(hour=15), snapshots=[snapshot()],
            fetcher=lambda code, target: self.matching, event_attacher=lambda items: None,
        )
        self.assertTrue(payload["frozen"])
        self.assertEqual(payload["snapshot_kind"], "late_reference")
        self.assertIn("事后参考", payload["stage"])
        self.assertIn("未在14:40", payload["timing_warning"])

    def test_runner_operates_only_during_decision_window(self):
        calls = []
        runner = LateRedRunner(lambda: calls.append(1))
        self.assertFalse(runner.step(self.now.replace(minute=39)))
        self.assertTrue(runner.step(self.now.replace(minute=40)))
        self.assertTrue(runner.step(self.now.replace(minute=50)))
        self.assertFalse(runner.step(self.now.replace(minute=51)))
        self.assertEqual(len(calls), 2)

    def test_before_1440_returns_waiting_without_scan(self):
        payload = waiting_late_red_screen(self.now.replace(hour=14, minute=39))
        self.assertEqual(payload["status"], "waiting")
        self.assertEqual(payload["qualified_count"], 0)

    def test_server_cached_result_refreshes_current_quote(self):
        payload = build_late_red_screen(
            self.now, snapshots=[snapshot()], fetcher=lambda code, target: self.matching,
            event_attacher=lambda items: [item.update(corporate_event_risk={"level": "normal"}) for item in items],
        )
        previous_cache, previous_runner = AppHandler.late_red_cache, AppHandler.late_red_runner
        try:
            AppHandler.late_red_cache = ("2026-09-02", payload)
            AppHandler.late_red_runner = None
            with (
                patch("src.stock_evaluator.server.china_time", return_value=self.now),
                patch("src.stock_evaluator.server.main_board_snapshots", return_value=[snapshot()]),
            ):
                view = AppHandler._late_red_screen_view()
            self.assertEqual(view["status"], "ready")
            self.assertEqual(view["qualified_count"], 1)
            self.assertEqual(AppHandler.late_red_cache[1]["candidates"], view["candidates"])
        finally:
            AppHandler.late_red_cache = previous_cache
            AppHandler.late_red_runner = previous_runner

    def test_server_freezes_without_fetching_post_cutoff_quotes(self):
        payload = build_late_red_screen(
            self.now, snapshots=[snapshot()], fetcher=lambda code, target: self.matching,
            event_attacher=lambda items: None,
        )
        with (
            patch.object(AppHandler, "late_red_cache", ("2026-09-02", payload)),
            patch.object(AppHandler, "late_red_runner", None),
            patch("src.stock_evaluator.server.china_time", return_value=self.now.replace(minute=50)),
            patch("src.stock_evaluator.server.main_board_snapshots") as fetch,
        ):
            view = AppHandler._late_red_screen_view()
            fetch.assert_not_called()
            self.assertTrue(view["frozen"])
            self.assertEqual(view["candidates"], payload["candidates"])
            self.assertTrue(AppHandler.late_red_cache[1]["frozen"])

    def test_quote_request_finishing_after_cutoff_does_not_change_ranking(self):
        payload = build_late_red_screen(
            self.now, snapshots=[snapshot()], fetcher=lambda code, target: self.matching,
            event_attacher=lambda items: None,
        )
        times = [self.now.replace(minute=49)] * 2 + [self.now.replace(minute=50)]
        with (
            patch.object(AppHandler, "late_red_cache", ("2026-09-02", payload)),
            patch.object(AppHandler, "late_red_runner", None),
            patch("src.stock_evaluator.server.china_time", side_effect=times),
            patch("src.stock_evaluator.server.main_board_snapshots", return_value=[snapshot(change=-2)]),
        ):
            view = AppHandler._late_red_screen_view()
        self.assertTrue(view["frozen"])
        self.assertEqual(view["candidates"], payload["candidates"])


if __name__ == "__main__":
    unittest.main()
