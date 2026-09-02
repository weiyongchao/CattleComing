import copy
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch
from tempfile import TemporaryDirectory
from pathlib import Path

from src.stock_evaluator.board_selection import (
    intraday_selection_window, select_live_recommendations,
    select_priority_watch_candidates,
)
from src.stock_evaluator import open_guard
from src.stock_evaluator.board_plan import auction_observation_view
from src.stock_evaluator.board_focus import DailyFocusStore
from src.stock_evaluator.board_research import BoardResearchStore
from src.stock_evaluator.market import Quote


def candidate(code="600001", **changes):
    return {
        "code": code, "name": "测试股票", "listed_sessions": 100,
        "quote_time": "2026-08-31T09:32:00+08:00", "quote_source": "test", "book_available": True,
        "bid1_price": 11, "bid1_volume": 100000,
        "float_market_cap": 5_000_000_000, "corporate_event_checked": True,
        "regulatory_risk": {"level": "normal"}, "open_score": 95,
        "continuation_score": 80, "amount": 100_000_000, "bid_volume5": 100000,
        "ask_volume5": 0, "order_imbalance": 1,
        "funds": {"available": True, "main_ratio": 5, "date": "2026-08-31"}, "tone": "confirm",
        "sealed": True, "price": 11, "limit_up_price": 11,
        "change_percent": 10, "auction_gap_percent": 6,
        "price_vs_open_percent": 1, "price_vs_auction_percent": 1,
        "high_turnover_chain_matched": True, "auction_rank": 1,
        "consecutive_limit_up_days": 2, "previous_final_seal_time": 100000,
        "previous_limit_up_breaks": 0, "previous_turnover_rate": 8,
        "turnover_rate": 5, "auction_tradable": True, "tradable": True,
        **changes,
    }


class BoardSelectionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 31, 9, 32)
        self.observations = {}

    def select(self, rows, seconds=0, market="可观察"):
        for row in rows:
            row["quote_time"] = (self.now + timedelta(seconds=seconds)).isoformat()
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
        self.assertEqual(row["seal_amount"], 110_000_000)
        self.assertEqual(row["seal_to_amount_percent"], 110)

    def test_thin_seal_or_weak_relative_seal_never_promotes(self):
        for changes, expected in [
            ({"bid1_volume": 20_000, "bid_volume5": 20_000}, "不足3000万元"),
            ({"bid1_volume": 30_000, "bid_volume5": 30_000,
              "amount": 1_000_000_000, "float_market_cap": 19_000_000_000}, "承接强度不足"),
        ]:
            with self.subTest(changes=changes):
                row = candidate(**changes)
                self.select([row])
                self.assertEqual(self.select([row], 20), [])
                self.assertIn(expected, row["selection_reason"])

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

    def test_priority_watch_keeps_high_board_rebound_and_strong_chain_but_drops_repeated_break(self):
        rebound = candidate(
            "002855", sealed=False, tone="watch", consecutive_limit_up_days=6,
            continuation_score=58, amount=584_000_000, turnover_rate=13.43,
            change_percent=4.69, auction_gap_percent=-3.95,
            price_vs_auction_percent=8.99, rebound_from_low_percent=10.95,
            reclaimed_auction=True, observed_board_breaks=0,
        )
        chain = candidate(
            "601086", tone="watch", consecutive_limit_up_days=3,
            continuation_score=58, amount=50_000_000, turnover_rate=.68,
            seal_path="first_seal", observed_board_breaks=0,
            transition_quality_bonus=8, tradable=False,
        )
        repeated = candidate(
            "600540", tone="reject", failed_board=True, near_limit_failure=True,
            consecutive_limit_up_days=4, turnover_rate=25.59,
            observed_board_breaks=2,
        )

        selected = select_priority_watch_candidates([rebound, chain, repeated])

        self.assertEqual([item["code"] for item in selected], ["002855", "601086"])
        self.assertEqual(rebound["priority_watch_type"], "高位弱转强待封板")
        self.assertEqual(chain["priority_watch_type"], "高连板主动强封")
        self.assertIn("未封板前只观察", rebound["priority_watch_plan"]["now_action"])
        self.assertIn("不排一字板", chain["priority_watch_plan"]["now_action"])
        self.assertNotIn("priority_watch", repeated)

    def test_priority_watch_fast_guard_reports_approach_one_price_and_trigger_without_formal_pick(self):
        now = datetime(2026, 8, 31, 10, 0)
        source = {"selected_date": "2026-08-31", "generated_at": now.isoformat(), "historical": False,
                  "priority_watch_candidates": [
                      {"code": "600001", "name": "弱转强", "priority_watch_rank": 1,
                       "priority_watch_key": "high_board_rebound", "priority_watch_type": "高位弱转强待封板",
                       "priority_watch_plan": {}, "auction_price": 10},
                      {"code": "600002", "name": "一字连板", "priority_watch_rank": 2,
                       "priority_watch_key": "strong_chain_seal", "priority_watch_type": "高连板主动强封",
                       "priority_watch_plan": {}, "auction_price": 11},
                      {"code": "600003", "name": "快速回封", "priority_watch_rank": 3,
                       "priority_watch_key": "single_reseal", "priority_watch_type": "单次炸板回封",
                       "priority_watch_plan": {}, "auction_price": 10.5},
                      {"code": "600004", "name": "盘口缺失", "priority_watch_rank": 4,
                       "priority_watch_key": "single_reseal", "priority_watch_type": "单次炸板回封",
                       "priority_watch_plan": {}, "auction_price": 11},
                  ]}

        class Provider:
            def quote(self, code):
                values = {
                    "600001": dict(price=10.92, change_percent=9.2, low_price=10.1,
                                   book_available=True, bid1_price=10.91, bid1_volume=1000,
                                   bid_volume5=1000, ask_volume5=1000, order_imbalance=0),
                    "600002": dict(price=11, change_percent=10, low_price=11,
                                   book_available=True, bid1_price=11, bid1_volume=100_000,
                                   bid_volume5=100_000, ask_volume5=0, order_imbalance=1),
                    "600003": dict(price=11, change_percent=10, low_price=10.6,
                                   book_available=True, bid1_price=11, bid1_volume=100_000,
                                   bid_volume5=100_000, ask_volume5=0, order_imbalance=1),
                    "600004": dict(price=11, change_percent=10, low_price=10.6,
                                   book_available=False, bid1_price=0, bid1_volume=0,
                                   bid_volume5=0, ask_volume5=0, order_imbalance=0),
                }[code]
                return Quote(code=code, name=code, previous_close=10, volume=1_000_000,
                             amount=100_000_000, turnover_rate=5, open_price=10,
                             high_price=values["price"], quote_time=now.isoformat(),
                             quote_source="test", **values)

        result = open_guard.build_priority_watch_guard(source, Provider(), now=now)

        self.assertEqual([row["status"] for row in result["candidates"]],
                         ["approaching", "waiting_open", "triggered", "verifying"])
        self.assertTrue(all(row["formal_recommendation"] is False
                            and row["recommended"] is False and row["actionable"] is False
                            for row in result["candidates"]))

    def test_priority_watch_pool_retains_given_plan_when_candidate_temporarily_drops_out(self):
        stored = {}
        seed = candidate("002855", priority_watch=True, priority_watch_rank=1,
                         priority_watch_key="high_board_rebound", priority_watch_type="高位弱转强待封板",
                         priority_watch_reason="测试预案", priority_watch_plan={"trigger_text": "等待首次封板"})
        first = {"candidates": [], "watch_candidates": [seed], "priority_watch_candidates": [seed]}
        retained = open_guard.retain_priority_watch_candidates(first, stored)
        self.assertEqual([item["code"] for item in retained], ["002855"])

        weaker = candidate("002855", tone="watch", sealed=False, change_percent=1.8,
                           rebound_from_low_percent=6.5)
        second = {"candidates": [], "watch_candidates": [weaker], "priority_watch_candidates": []}
        retained = open_guard.retain_priority_watch_candidates(second, stored)
        self.assertEqual([item["code"] for item in retained], ["002855"])
        self.assertFalse(retained[0]["priority_watch_current_match"])
        self.assertIn("保留3秒监控", retained[0]["priority_watch_tracking_note"])

    def test_failed_board_reseal_requires_three_samples_and_sixty_seconds(self):
        row = candidate()
        self.select([row])
        self.select([row], 20)
        row.update(sealed=False, failed_board=True)
        self.assertEqual(self.select([row], 40), [])
        self.assertFalse(row["recommended"])
        row.update(sealed=True, failed_board=False)
        self.assertEqual(self.select([row], 60), [])
        self.assertEqual(self.select([row], 80), [])
        self.assertEqual(self.select([row], 100), [])
        selected = self.select([row], 120)
        self.assertEqual(len(selected), 1)
        self.assertEqual(row["seal_path"], "reseal")
        self.assertEqual(row["seal_grade"], "B")
        self.assertEqual(row["confirmation_samples"], 3)
        self.assertEqual(row["confirmation_span_seconds"], 60)

    def test_repeated_failed_snapshot_counts_as_one_break_but_second_break_is_rejected(self):
        row = candidate()
        self.select([row])
        row.update(sealed=False, failed_board=True)
        self.select([row], 20)
        self.select([row], 40)
        self.assertEqual(row["observed_board_breaks"], 1)
        row.update(sealed=True, failed_board=False)
        self.select([row], 60)
        row.update(sealed=False, failed_board=True)
        self.select([row], 80)
        self.assertEqual(row["observed_board_breaks"], 2)
        row.update(sealed=True, failed_board=False)
        for seconds in (100, 120, 140, 160):
            self.assertEqual(self.select([row], seconds), [])
        self.assertIn("至少2次炸板", row["selection_reason"])

    def test_reseal_requires_fast_repair_strong_book_and_funds(self):
        for changes, expected in [
            ({"order_imbalance": .34}, "盘口支撑不足"),
            ({"funds": {"available": True, "main_ratio": 2.99}}, "主力占比至少3%"),
        ]:
            with self.subTest(changes=changes):
                self.observations = {}
                row = candidate()
                self.select([row])
                row.update(sealed=False, failed_board=True)
                self.select([row], 20)
                row.update(sealed=True, failed_board=False, **changes)
                self.assertEqual(self.select([row], 40), [])
                self.assertIn(expected, row["selection_reason"])
        self.observations = {}
        slow = candidate()
        self.select([slow])
        slow.update(sealed=False, failed_board=True)
        self.select([slow], 20)
        slow.update(sealed=True, failed_board=False)
        self.assertEqual(self.select([slow], 220), [])
        self.assertIn("超过3分钟", slow["selection_reason"])

    def test_first_seal_ranks_before_higher_scoring_reseal(self):
        first = candidate("600001", open_score=80, continuation_score=75)
        reseal = candidate("600002", open_score=100, continuation_score=100)
        self.select([first, reseal])
        self.select([first, reseal], 20)
        reseal.update(sealed=False, failed_board=True)
        self.select([first, reseal], 40)
        reseal.update(sealed=True, failed_board=False)
        for seconds in (60, 80, 100):
            self.select([first, reseal], seconds)
        selected = self.select([first, reseal], 120)
        self.assertEqual([row["code"] for row in selected[:2]], ["600001", "600002"])
        self.assertEqual(reseal["seal_path"], "reseal")

    def test_five_tigers_priority_only_reorders_otherwise_qualified_first_seals(self):
        focus = candidate("600001", five_tigers_priority=2, continuation_score=75, open_score=85)
        higher_score = candidate("600002", continuation_score=100, open_score=100)
        self.select([focus, higher_score])
        selected = self.select([focus, higher_score], 20)
        self.assertEqual(selected[0]["code"], "600001")

    def test_five_tigers_gap_fallback_downgrades_previous_breaks_instead_of_hard_veto(self):
        row = candidate(previous_limit_up_breaks=5, five_tigers_role="gap_fallback",
                        five_tigers_priority=1)
        self.select([row])
        selected = self.select([row], 20)
        self.assertEqual(len(selected), 1)
        self.assertTrue(row["previous_breaks_downgraded"])

    def test_opening_relay_can_use_fallback_funds_without_treating_it_as_hard_veto(self):
        row = candidate(
            continuation_score=39, auction_gap_percent=9.89,
            auction_amount=164_000_000, five_tigers_role="strong_consensus",
            five_tigers_priority=2,
            funds={"available": True, "main_ratio": -67, "source": "腾讯逐笔成交推算（备用）"},
        )
        self.select([row])
        selected = self.select([row], 20)
        self.assertEqual([item["code"] for item in selected], [row["code"]])
        self.assertTrue(row["opening_relay_route"])
        self.assertTrue(row["funds_degraded"])

    def test_authoritative_outflow_still_rejects_opening_relay(self):
        row = candidate(
            continuation_score=39, auction_gap_percent=9.89,
            auction_amount=164_000_000, five_tigers_role="strong_consensus",
            five_tigers_priority=2,
            funds={"available": True, "main_ratio": -1.01, "source": "实时分钟累计"},
        )
        self.select([row])
        self.assertEqual(self.select([row], 20), [])
        self.assertIn("权威资金流出", row["selection_reason"])

    def test_fast_reseal_uses_book_confirmation_when_only_fallback_funds_exist(self):
        row = candidate(
            continuation_score=58, auction_gap_percent=8.07,
            auction_amount=43_430_000, five_tigers_role="gap_fallback",
            five_tigers_priority=1,
            funds={"available": True, "main_ratio": -46, "source": "腾讯逐笔成交推算（备用）"},
        )
        self.select([row])
        row.update(sealed=False, failed_board=True)
        self.select([row], 20)
        row.update(sealed=True, failed_board=False)
        for seconds in (40, 60, 80):
            self.assertEqual(self.select([row], seconds), [])
        selected = self.select([row], 100)
        self.assertEqual([item["code"] for item in selected], [row["code"]])
        self.assertEqual(row["seal_path"], "reseal")
        self.assertEqual(row["confirmation_span_seconds"], 60)

    def test_missing_reseal_book_is_reported_as_waiting_for_data(self):
        row = candidate()
        self.select([row])
        row.update(sealed=False, failed_board=True)
        self.select([row], 20)
        row.update(sealed=True, failed_board=False, book_available=False, bid_volume5=0)
        self.assertEqual(self.select([row], 40), [])
        self.assertIn("盘口数据缺失", row["selection_reason"])

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
            {"auction_tradable": False, "tradable": False}, {"previous_final_seal_time": 113000},
            {"previous_limit_up_breaks": 2}, {"previous_turnover_rate": 20.01},
            {"turnover_rate": 20.01},
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

    def test_multiboard_does_not_create_afternoon_pick(self):
        now = self.now.replace(hour=13, minute=0)
        row, observations = candidate(), {}
        for seconds in (0, 20):
            row["quote_time"] = (now + timedelta(seconds=seconds)).isoformat()
            self.assertEqual(select_live_recommendations(
                [row], now + timedelta(seconds=seconds), "可观察", observations,
            ), [])
        self.assertIn("11:30", row["selection_reason"])

    def test_clean_three_to_four_structure_gets_ranking_bonus(self):
        weaker = candidate("600001", consecutive_limit_up_days=2,
                           previous_limit_up_breaks=1, previous_turnover_rate=18)
        stronger = candidate("600002", consecutive_limit_up_days=3,
                             previous_limit_up_breaks=0, previous_turnover_rate=8)
        self.select([weaker, stronger])
        selected = self.select([weaker, stronger], 20)
        self.assertEqual(selected[0]["code"], "600002")
        self.assertGreater(stronger["transition_quality_bonus"], weaker["transition_quality_bonus"])


class OpenGuardSelectionIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store_patch = patch.object(open_guard, "DAILY_FOCUS_STORE", DailyFocusStore(Path(temporary.name) / "focus.json"))
        store_patch.start()
        self.addCleanup(store_patch.stop)
        research_patch = patch.object(open_guard, "BOARD_RESEARCH_STORE", BoardResearchStore(Path(temporary.name) / "research"))
        research_patch.start()
        self.addCleanup(research_patch.stop)
        self.now = datetime(2026, 8, 31, 10, 10)
        self.snapshot = {"selected_date": "2026-08-31", "market": {"state": "可观察"},
                         "candidates": [candidate()], "watch_candidates": [candidate("600002")]}
        for row in self.snapshot["candidates"] + self.snapshot["watch_candidates"]:
            row["quote_time"] = self.now.isoformat()

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

    def test_previous_board_quality_reaches_live_selection(self):
        result = {
            "quote": {"price": 11, "open_price": 10.5, "previous_close": 10,
                      "high_price": 11, "low_price": 10.5, "change_percent": 10,
                      "amount": 100_000_000, "quote_time": self.now.isoformat(),
                      "book_available": True},
            "metrics": {"price_vs_open_percent": 4.76, "price_vs_ma5_percent": 5,
                        "volume_ratio": 2, "turnover_rate": 6},
            "order_book": {"imbalance": .5, "signal": "买盘占优", "bid_volume5": 10000,
                           "ask_volume5": 0},
            "regulatory_risk": {"level": "normal", "label": "常规"},
        }
        row = open_guard._open_confirmation(
            candidate(previous_limit_up_breaks=1, previous_turnover_rate=12.5), result,
            {"is_today": True, "date": date.today().isoformat(), "main_ratio": 5},
        )
        self.assertEqual(row["previous_limit_up_breaks"], 1)
        self.assertEqual(row["previous_turnover_rate"], 12.5)
        self.assertTrue(row["tradable"])
        self.assertEqual(row["turnover_rate"], 6)

        opened = open_guard._open_confirmation(
            candidate(tradable=False, auction_tradable=False), result,
            {"is_today": True, "date": date.today().isoformat(), "main_ratio": 5},
        )
        self.assertTrue(opened["tradable"])
        self.assertTrue(opened["opened_one_price_reseal"])

    def test_guard_consumes_watch_pool_and_returns_only_confirmed_top_five(self):
        with patch.object(open_guard, "_SELECTION_OBSERVATIONS", {}), \
             patch.object(open_guard, "attach_corporate_event_risks"), \
             patch.object(open_guard, "_check_one", side_effect=lambda item, *_: copy.deepcopy(item)):
            first = open_guard.build_open_guard(self.snapshot, discover_live=False, now=self.now)
            self.assertEqual(first["candidates"], [])
            self.assertEqual(len(first["watch_candidates"]), 2)
            self.assertNotIn("小仓试错", first["watch_candidates"][0]["entry_advice"])
            for row in self.snapshot["candidates"] + self.snapshot["watch_candidates"]:
                row["quote_time"] = (self.now + timedelta(seconds=20)).isoformat()
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
             patch.object(open_guard, "_discover_live_multi_board", return_value=[]) as discover_multi, \
             patch.object(open_guard, "_check_one", side_effect=lambda item, *_: copy.deepcopy(item)):
            result = open_guard.build_open_guard(self.snapshot, now=self.now)
            self.assertEqual(discover.call_args.kwargs["snapshots"], quotes[:1])
            self.assertEqual(discover_multi.call_args.kwargs["snapshots"], quotes)
            discovered = next(row for row in result["watch_candidates"] if row["code"] == "600003")
            self.assertEqual(discovered["discovery_source"], "盘中封板补选")
            self.assertTrue(discovered["discovered_at"].startswith("2026-08-31T10:10"))
            self.assertEqual(len(self.snapshot["candidates"]), 1)

    def test_existing_candidate_receives_opening_relay_and_previous_board_metadata(self):
        existing = self.snapshot["candidates"][0]
        existing.update(previous_final_seal_time=None, previous_limit_up_breaks=None,
                        previous_turnover_rate=None, opening_chain_relay=False)
        enriched = candidate(existing["code"], opening_chain_relay=True,
                             previous_final_seal_time=100951,
                             previous_limit_up_breaks=1, previous_turnover_rate=14.49)
        with patch.object(open_guard, "_SELECTION_OBSERVATIONS", {}), \
             patch.object(open_guard, "_OPENING_DISCOVERY_CACHE", {}), \
             patch.object(open_guard, "attach_corporate_event_risks"), \
             patch.object(open_guard, "main_board_snapshots", return_value=[{"f12": existing["code"], "f3": 10}]), \
             patch.object(open_guard, "_discover_live_one_to_two", return_value=[]), \
             patch.object(open_guard, "_discover_live_multi_board", return_value=[enriched]), \
             patch.object(open_guard, "_check_one", side_effect=lambda item, *_: copy.deepcopy(item)):
            result = open_guard.build_open_guard(self.snapshot, now=self.now)
        merged = next(row for row in result["watch_candidates"] if row["code"] == existing["code"])
        self.assertTrue(merged["opening_chain_relay"])
        self.assertEqual(merged["previous_final_seal_time"], 100951)
        self.assertEqual(merged["previous_limit_up_breaks"], 1)


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
