import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from src.stock_evaluator.evaluator import evaluate
from src.stock_evaluator.market import DailyBar, EastmoneyProvider, Quote, secid_for
from src.stock_evaluator.screener import LEADER_POOL, is_main_board, is_risk_stock_name, screen_leaders
from src.stock_evaluator.funds import _aggregate_tencent_trades, _combined_fund_signal, _fund_period, _select_top_sectors
from src.stock_evaluator.auction import (
    _auction_score, _multi_factor_score, _prefilter_auction_universe,
    _expand_live_prefilter_with_extremes, _relay_pattern_score,
    _history_prefilter_score, _select_high_confidence_candidates,
    _consecutive_limit_up_days, _core_chain_score, _divergence_reversal_score,
    _first_board_score, _next_day_continuation_score, _board_stage,
    _big_order_support, _apply_dynamic_context, _execution_risk_profile,
    _expand_with_previous_limit_ups, _auction_amount_qualification,
    _nuclear_button_profile,
    _recognition_channel_profile,
)
from src.stock_evaluator.auction_trajectory import _profile
from src.stock_evaluator.next_day import _holding_strategy
from src.stock_evaluator.peers import _primary_board
from src.stock_evaluator.board_plan import _auction_decision, _auction_gate, _auction_phase, _market_gate
from src.stock_evaluator.intraday import (
    _intraday_score, _leadership_profile, _limit_down_reversal_profile, _prefilter_snapshots,
)
from src.stock_evaluator.simple_plan import _position_action, build_position_summary
from src.stock_evaluator import history as candidate_history
from src.stock_evaluator.history import (
    _review_candidate, load_board_plan_snapshot, record_candidates,
    save_board_plan_snapshot,
)
from src.stock_evaluator.premarket import _premarket_candidate
from src.stock_evaluator.stock_search import _parse_suggestions
from src.stock_evaluator.regulatory import regulatory_risk
from src.stock_evaluator.open_guard import _open_confirmation
from src.stock_evaluator.daily_recommend import _daily_candidate
from src.stock_evaluator.external_context import (
    _global_summary, _policy_signals, _tencent_global_rows, apply_external_context,
)


class EvaluatorTests(unittest.TestCase):
    def bars(self, closes, volumes=None):
        volumes = volumes or [1000] * len(closes)
        return [DailyBar(date(2026, 1, 1) + timedelta(days=i), p, p, p, p, v, p * v) for i, (p, v) in enumerate(zip(closes, volumes))]

    def test_market_code_mapping(self):
        self.assertEqual(secid_for("600519"), "1.600519")
        self.assertEqual(secid_for("sz000001"), "0.000001")
        with self.assertRaises(ValueError):
            secid_for("123")

    def test_stock_name_search_keeps_astock_and_prioritizes_exact_name(self):
        payload = {"QuotationCodeTable": {"Data": [
            {"Code": "00700", "Name": "腾讯控股", "Classify": "HKStock", "SecurityTypeName": "港股"},
            {"Code": "600519", "Name": "贵州茅台", "Classify": "AStock", "SecurityTypeName": "沪A"},
            {"Code": "600059", "Name": "古越龙山", "Classify": "AStock", "SecurityTypeName": "沪A"},
        ]}}
        matches = _parse_suggestions(payload, "贵州茅台")
        self.assertEqual(matches, [{"code": "600519", "name": "贵州茅台", "market": "沪A", "exact": True}])

    def test_strong_price_and_volume_score_above_neutral(self):
        quote = Quote("000001", "测试", 12, 11, 3, 2000, 24000)
        result = evaluate(quote, self.bars([10, 10.2, 10.4, 10.6, 11], [1000] * 5))
        self.assertGreater(result["score"], 50)
        self.assertGreater(result["metrics"]["price_vs_ma5_percent"], 0)

    def test_score_is_bounded(self):
        quote = Quote("000001", "测试", 1, 10, -90, 999999, 1)
        result = evaluate(quote, self.bars([10] * 5))
        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 100)

    def test_tencent_quote_parser(self):
        values = [""] * 39
        values[1:7] = ["贵州茅台", "600519", "1353.55", "1355.29", "1355.00", "8419"]
        values[32:39] = ["-0.13", "1359.00", "1350.03", "", "8419", "114069", "0.07"]
        raw = 'v_sh600519="' + "~".join(values) + '";'
        quote = EastmoneyProvider._parse_tencent_quote(raw)
        self.assertEqual(quote.code, "600519")
        self.assertEqual(quote.price, 1353.55)
        self.assertEqual(quote.change_percent, -0.13)
        self.assertEqual(quote.amount, 1_140_690_000)
        self.assertEqual(quote.turnover_rate, 0.07)
        self.assertEqual(quote.open_price, 1355.00)
        self.assertEqual(quote.high_price, 1359.00)
        self.assertEqual(quote.low_price, 1350.03)

    def test_yesterday_change_uses_completed_bars(self):
        bars = self.bars([10, 10, 11, 12, 12])
        quote = Quote("000001", "测试", 12, 12, 0, 1000, 12000, 2.5)
        result = evaluate(quote, bars)
        self.assertAlmostEqual(result["metrics"]["yesterday_change_percent"], 0.0)
        self.assertEqual(result["metrics"]["turnover_rate"], 2.5)

    def test_intraday_position(self):
        quote = Quote("000001", "测试", 18, 17, 1, 1000, 18000, 2, 16, 20, 10)
        result = evaluate(quote, self.bars([15, 16, 17, 17, 17]))
        self.assertEqual(result["metrics"]["intraday_position_percent"], 80.0)
        self.assertEqual(result["metrics"]["price_vs_open_percent"], 12.5)
        self.assertGreater(result["components"]["intraday"], 0)

    def test_weak_signal_blocks_new_position_and_adding(self):
        quote = Quote("000001", "测试", 8, 9, -4, 3000, 24000, 0.2, 9, 9, 8)
        result = evaluate(quote, self.bars([10, 10, 9.5, 9, 8.8]))
        self.assertIn("暂缓建仓", result["operation"]["new_position"])
        self.assertIn("不建议加仓", result["operation"]["existing_position"])
        self.assertTrue(result["operation"]["risk_points"])
        self.assertFalse(result["price_plan"]["build"]["enabled"])
        self.assertFalse(result["price_plan"]["add"]["enabled"])

    def test_price_plan_has_ordered_support_and_resistance(self):
        quote = Quote("000001", "测试", 12, 11, 3, 2000, 24000, 2, 11, 12.5, 10.5)
        result = evaluate(quote, self.bars([10, 10.2, 10.4, 10.6, 11]))
        plan = result["price_plan"]
        self.assertLessEqual(plan["levels"]["support5"], plan["levels"]["resistance5"])
        self.assertLess(plan["build"]["price_low"], plan["build"]["price_high"])
        self.assertIn("盘中", plan["note"])

    def test_order_book_imbalance(self):
        book = EastmoneyProvider._book_metrics([(10, 300), (9.9, 200)], [(10.1, 100), (10.2, 100)])
        self.assertGreater(book["order_imbalance"], 0.4)
        self.assertEqual(book["bid_wall_price"], 10)
        self.assertAlmostEqual(book["spread"], 0.1)

    def test_weak_sector_and_sell_pressure_disable_build(self):
        quote = Quote("000063", "测试", 12, 11, 3, 2000, 24000, 2, 11, 12.5, 10.5,
                      100, 500, -0.67, 0.01, 11.9, 12.1)
        context = {"category": "大科技应用", "average_change": -1.2, "advance_ratio": 0.2, "sample_size": 10}
        result = evaluate(quote, self.bars([10, 10.2, 10.4, 10.6, 11]), context)
        self.assertFalse(result["price_plan"]["build"]["enabled"])
        self.assertFalse(result["price_plan"]["add"]["enabled"])
        self.assertEqual(result["order_book"]["signal"], "卖盘占优")

    def test_combined_fund_signal_is_bounded_and_explainable(self):
        inflow = _combined_fund_signal(100_000_000, 20, -1)
        outflow = _combined_fund_signal(-100_000_000, -20, 1)
        self.assertEqual(inflow["score"], 80.0)
        self.assertEqual(inflow["label"], "强流入")
        self.assertEqual(outflow["score"], -80.0)
        self.assertIn("不是", inflow["description"])

    def test_tencent_trade_flow_uses_active_side_and_amount_tiers(self):
        rows = [
            ["0", "09:30:00", "10", "0", "1", "1200000", "B"],
            ["1", "09:30:03", "10", "0", "1", "300000", "S"],
            ["2", "09:30:06", "10", "0", "1", "80000", "B"],
            ["3", "09:30:09", "10", "0", "1", "20000", "M"],
        ]
        result = _aggregate_tencent_trades(rows)
        self.assertEqual(result["super_large"], 1200000)
        self.assertEqual(result["large"], -300000)
        self.assertEqual(result["medium"], 80000)
        self.assertEqual(result["small"], 0)
        self.assertEqual(result["total"], 1580000)

    def test_fund_period_distinguishes_today_and_yesterday(self):
        from datetime import date, timedelta
        today = date.today()
        self.assertEqual(_fund_period(today.isoformat())["period_label"], "当日资金")
        self.assertEqual(_fund_period((today - timedelta(days=1)).isoformat())["period_label"], "昨日资金")

    def test_sector_funds_selects_top_six_by_main_net(self):
        sectors = [{"name": str(index), "main_net": index} for index in range(8)]
        selected = _select_top_sectors(sectors)
        self.assertEqual(len(selected), 6)
        self.assertEqual([item["main_net"] for item in selected], [7, 6, 5, 4, 3, 2])

    def test_operation_contains_confirmation_and_invalidation(self):
        quote = Quote("000001", "测试", 12, 11, 3, 2000, 24000, 2, 11, 12.5, 10.5)
        result = evaluate(quote, self.bars([10, 10.2, 10.4, 10.6, 11]))
        self.assertIn("MA5", result["operation"]["confirmation"])
        self.assertIn("重新评估", result["operation"]["invalidation"])
        self.assertIn("综合评分", result["summary"])
        self.assertIn("量比", result["summary"])
        self.assertIn("未持仓参考", result["summary"])

    def test_main_board_code_filter(self):
        self.assertTrue(is_main_board("600519"))
        self.assertTrue(is_main_board("002594"))
        self.assertFalse(is_main_board("300750"))
        self.assertFalse(is_main_board("301308"))
        self.assertFalse(is_main_board("688981"))
        self.assertFalse(is_main_board("430047"))
        self.assertFalse(is_main_board("830799"))
        self.assertFalse(is_main_board("920799"))

    def test_simple_plan_rejects_unavailable_markets_before_fetching(self):
        for code in ("300750", "301308", "688981", "430047", "830799", "920799"):
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, "仅支持沪深主板"):
                build_position_summary(code)

    def test_st_and_delisting_names_are_excluded(self):
        self.assertTrue(is_risk_stock_name("*ST左江"))
        self.assertTrue(is_risk_stock_name("ST某某"))
        self.assertTrue(is_risk_stock_name("退市海创"))
        self.assertTrue(is_risk_stock_name("国华退"))
        self.assertFalse(is_risk_stock_name("贵州茅台"))

    def test_auction_score_rewards_active_moderate_gap(self):
        strong, reasons, risks = _auction_score(4, 60_000_000, 1.2, 3, 6, 1.4)
        overheated, _, hot_risks = _auction_score(9.6, 60_000_000, 1.2, 12, 25, 4)
        self.assertGreaterEqual(strong, 75)
        self.assertGreater(strong, overheated)
        self.assertIn("竞价温和高开", reasons)
        self.assertTrue(any("接近涨停" in risk for risk in hot_risks))

    def test_auction_multifactor_rewards_history_activity_and_funds(self):
        strong, _, _ = _multi_factor_score(85, 8, 15, 80, 2, 0.1, 5, 4)
        weak, _, risks = _multi_factor_score(85, 30, 40, 20, 0, 0.7, -6, 18)
        self.assertGreaterEqual(strong, 78)
        self.assertGreater(strong, weak)
        self.assertTrue(any("主力资金" in risk for risk in risks))

    def test_nuclear_button_requires_all_five_final_auction_conditions(self):
        matched = _nuclear_button_profile(
            previous_volume=8_000_000, prior_volume=10_000_000, previous_amount=600_000_000,
            auction_gap_percent=7.5, auction_amount=60_000_000,
            auction_turnover_percent=3.2, strong_characteristics=True,
            exact_auction=True,
        )
        self.assertTrue(matched["matched"])
        self.assertTrue(all(item["passed"] for item in matched["checks"]))
        preliminary = _nuclear_button_profile(
            previous_volume=8_000_000, prior_volume=10_000_000, previous_amount=600_000_000,
            auction_gap_percent=7.5, auction_amount=60_000_000,
            auction_turnover_percent=3.2, strong_characteristics=True,
            exact_auction=False,
        )
        self.assertTrue(preliminary["hard_matched"])
        self.assertFalse(preliminary["matched"])
        weak_turnover = _nuclear_button_profile(
            previous_volume=8_000_000, prior_volume=10_000_000, previous_amount=600_000_000,
            auction_gap_percent=7.5, auction_amount=60_000_000,
            auction_turnover_percent=2.99, strong_characteristics=True,
            exact_auction=True,
        )
        self.assertFalse(weak_turnover["matched"])
        not_shrinking = _nuclear_button_profile(
            previous_volume=10_000_000, prior_volume=8_000_000, previous_amount=600_000_000,
            auction_gap_percent=7.5, auction_amount=60_000_000,
            auction_turnover_percent=3.2, strong_characteristics=True,
            exact_auction=True,
        )
        self.assertFalse(not_shrinking["matched"])
        self.assertFalse(not_shrinking["checks"][1]["passed"])

    def test_recognition_channels_cover_capacity_reversal_and_auction_grab_without_names(self):
        capacity = _recognition_channel_profile(
            consecutive_limit_ups=2, recent_5_limit_ups=2, recent_10_limit_ups=4,
            reversal_matched=False, reversal_score=0, float_market_cap=30_800_000_000,
            gap_percent=5.48, auction_amount=272_000_000, auction_volume_percent=11.4,
            auction_turnover_percent=0.84, previous_volume_ratio=0.46,
            previous_close_position=100, previous_upper_shadow=0, ten_day_change=62,
            historical_proxy=True,
        )
        self.assertTrue(capacity["capacity_relay"])
        reversal = _recognition_channel_profile(
            consecutive_limit_ups=0, recent_5_limit_ups=4, recent_10_limit_ups=4,
            reversal_matched=True, reversal_score=100, float_market_cap=4_745_000_000,
            gap_percent=10.03, auction_amount=420_000_000, auction_volume_percent=79,
            auction_turnover_percent=8.05, previous_volume_ratio=4.97,
            previous_close_position=77.9, previous_upper_shadow=0.302, ten_day_change=57.5,
            historical_proxy=True,
        )
        self.assertTrue(reversal["leader_reversal"])
        grab = _recognition_channel_profile(
            consecutive_limit_ups=0, recent_5_limit_ups=1, recent_10_limit_ups=1,
            reversal_matched=False, reversal_score=0, float_market_cap=16_200_000_000,
            gap_percent=10, auction_amount=539_000_000, auction_volume_percent=43,
            auction_turnover_percent=3.02, previous_volume_ratio=2.63,
            previous_close_position=74.5, previous_upper_shadow=0.179, ten_day_change=17,
            historical_proxy=True,
        )
        self.assertTrue(grab["auction_grab"])
        immature = _recognition_channel_profile(
            consecutive_limit_ups=0, recent_5_limit_ups=0, recent_10_limit_ups=0,
            reversal_matched=False, reversal_score=0, float_market_cap=5_000_000_000,
            gap_percent=10, auction_amount=60_000_000, auction_volume_percent=40,
            auction_turnover_percent=3.5, previous_volume_ratio=2,
            previous_close_position=80, previous_upper_shadow=0.1, ten_day_change=10,
            historical_proxy=True,
        )
        self.assertFalse(any(immature.values()))

    def test_relay_pattern_recognizes_strong_close_and_auction_volume(self):
        score, matched, reasons, risks = _relay_pattern_score(
            3.97, 150_000_000, 17.9, 33.25, 33.92, 41.82,
            7.63, 100, 0, 3,
        )
        self.assertGreaterEqual(score, 85)
        self.assertTrue(matched)
        self.assertTrue(any("涨停活性" in reason for reason in reasons))
        self.assertTrue(any("极端爆量" in risk for risk in risks))

    def test_relay_pattern_rejects_weak_close_without_limit_activity(self):
        score, matched, _, _ = _relay_pattern_score(
            3, 50_000_000, 5, 3, 5, 8, 1.2, 45, 0.5, 0,
        )
        self.assertFalse(matched)
        self.assertLess(score, 78)

    def test_consecutive_limit_ups_only_counts_streak_ending_yesterday(self):
        active = self.bars([10, 10, 10, 11, 12.1, 13.31])
        broken = self.bars([10, 11, 12.1, 12, 13.2, 13])
        self.assertEqual(_consecutive_limit_up_days(active), 3)
        self.assertEqual(_consecutive_limit_up_days(broken), 0)

    def test_core_chain_rule_matches_user_thresholds(self):
        score, matched, reasons = _core_chain_score(2, 5, 1.1, 12_000_000, 15_000_000_000, 80)
        self.assertTrue(matched)
        self.assertGreaterEqual(score, 85)
        self.assertTrue(any("连续2个涨停" in reason for reason in reasons))
        self.assertFalse(_core_chain_score(2, 4.9, 1.1, 12_000_000, 15_000_000_000, 80)[1])
        self.assertFalse(_core_chain_score(3, 2, 1.1, 12_000_000, 21_000_000_000, 80)[1])
        self.assertFalse(_core_chain_score(3, 2, 1.1, 12_000_000, 15_000_000_000, 59)[1])

    def test_relay_pattern_recognizes_near_limit_acceleration(self):
        score, matched, reasons, _ = _relay_pattern_score(
            9.1, 80_000_000, 8, 12, 15, 24, 1.4, 100, 0.05, 1,
        )
        self.assertTrue(matched)
        self.assertGreaterEqual(score, 80)
        self.assertTrue(any("强势加速" in reason for reason in reasons))

    def test_historical_selection_is_dynamic_and_uses_open_volume_confirmation(self):
        base = {
            "eligible": True, "auction_time": "09:31", "previous_close_position_percent": 100,
            "previous_upper_shadow_ratio": 0.05, "previous_volume_ratio": 1.2,
            "auction_amount": 100_000_000,
        }
        candidates = [
            {**base, "code": "1", "score": 90, "auction_gap_percent": 9, "auction_volume_percent": 100},
            {**base, "code": "2", "score": 86, "auction_gap_percent": 5, "auction_volume_percent": 60},
            {**base, "code": "3", "score": 99, "auction_gap_percent": 3, "auction_volume_percent": 120},
            {**base, "code": "4", "score": 95, "auction_gap_percent": 9, "auction_volume_percent": 20},
            {**base, "code": "5", "strategy_mode": "连板核心（历史代理）", "previous_volume_ratio": 3.56,
             "score": 88, "auction_gap_percent": 5.5, "auction_volume_percent": 80},
        ]
        selected = _select_high_confidence_candidates(candidates, limit=6)
        self.assertEqual([item["code"] for item in selected], ["1", "5", "2"])

    def test_dynamic_selection_allows_empty_market(self):
        self.assertEqual(_select_high_confidence_candidates([
            {"eligible": True, "score": 79, "auction_amount": 10_000_000, "auction_time": "09:25"}
        ]), [])

    def test_dynamic_selection_does_not_force_core_ahead_of_stronger_model(self):
        candidates = [
            {"code": "core1", "eligible": True, "core_chain_matched": True, "score": 90, "auction_amount": 20_000_000, "auction_time": "09:25"},
            {"code": "core2", "eligible": True, "core_chain_matched": True, "score": 88, "auction_amount": 20_000_000, "auction_time": "09:25"},
            {"code": "fallback", "eligible": True, "core_chain_matched": False, "score": 100, "auction_amount": 100_000_000, "auction_time": "09:25"},
        ]
        self.assertEqual([item["code"] for item in _select_high_confidence_candidates(candidates)], ["fallback", "core1", "core2"])

    def test_dynamic_selection_keeps_strong_one_price_board_with_warning(self):
        candidate = {
            "code": "002412", "eligible": True, "priority_tier": "连板优先",
            "tradable": False, "risk_veto": False, "auction_gap_percent": 10.04,
            "score": 94, "selection_score": 90, "continuation_score": 40,
            "core_chain_score": 91, "auction_amount": 72_839_628, "auction_time": "09:25",
        }
        selected = _select_high_confidence_candidates([candidate], limit=6)
        self.assertEqual([item["code"] for item in selected], ["002412"])

    def test_dynamic_selection_keeps_special_candidates_behind_ordinary(self):
        common = {"eligible": True, "risk_veto": False, "auction_time": "09:25"}
        ordinary = {
            **common, "code": "ordinary", "priority_tier": "连板优先", "tradable": True,
            "auction_gap_percent": 5.2, "score": 88, "selection_score": 86,
            "continuation_score": 72, "auction_amount": 80_000_000,
        }
        one_price = {
            **common, "code": "one-price", "priority_tier": "连板优先", "tradable": False,
            "auction_gap_percent": 10.0, "score": 96, "selection_score": 92,
            "continuation_score": 40, "core_chain_score": 90, "auction_amount": 70_000_000,
        }
        limit_down = {
            **common, "code": "limit-down", "priority_tier": "跌停反核观察", "tradable": True,
            "auction_gap_percent": -10.0, "score": 82, "selection_score": 78,
            "continuation_score": 39, "auction_amount": 73_000_000, "auction_volume_percent": 13,
        }
        selected = _select_high_confidence_candidates([limit_down, one_price, ordinary], limit=6)
        self.assertEqual([item["code"] for item in selected], ["ordinary", "one-price", "limit-down"])

    def test_divergence_reversal_recognizes_high_volume_disagreement(self):
        score, matched, reasons, risks = _divergence_reversal_score(
            4, 0, 10.0, 35, 180_000_000, 4.8, 78, 0.30, 58,
        )
        self.assertTrue(matched)
        self.assertGreaterEqual(score, 90)
        self.assertTrue(reasons)
        self.assertTrue(risks)

    def test_first_board_requires_small_cap_and_strong_auction_structure(self):
        score, matched, reasons, _ = _first_board_score(
            4.5, 2.5, 60_000_000, 12_000_000_000, 80, 5,
            4, 12, 1.2, 82, 0.15,
        )
        self.assertTrue(matched)
        self.assertGreaterEqual(score, 85)
        self.assertTrue(reasons)
        self.assertFalse(_first_board_score(
            4.5, 2.5, 200_000_000, 415_000_000_000, 80, 5,
            4, 12, 1.2, 82, 0.15,
        )[1])
        self.assertFalse(_first_board_score(
            1.58, 2.2, 21_000_000, 13_800_000_000, 80, 4,
            4, 15, 1.1, 74, 0.25,
        )[1])

    def test_next_day_continuation_prioritizes_established_height_over_one_to_two(self):
        established = _next_day_continuation_score(
            2, 2, 9.99, 28.5, 78_000_000, 1.74, 5_500_000_000,
        )
        one_to_two_exhausted = _next_day_continuation_score(
            1, 1, 10.04, 123.8, 82_000_000, 1.01, 3_100_000_000,
        )
        self.assertGreaterEqual(established, 70)
        self.assertGreater(established - one_to_two_exhausted, 30)

    def test_board_stage_labels_first_board_and_target_board_count(self):
        self.assertEqual(_board_stage(0)["board_stage_label"], "首板候选")
        self.assertEqual(_board_stage(1)["board_stage_label"], "1进2 · 目标2连板")
        self.assertEqual(_board_stage(2)["board_stage_label"], "2进3 · 目标3连板")

    def test_bright_one_to_two_can_remain_secondary_without_theme_peers(self):
        strong = {
            "eligible": True, "priority_tier": "一进二观察", "industry": "独立题材",
            "score": 88, "continuation_base_score": 65, "float_market_cap": 8_000_000_000,
            "auction_gap_percent": 5, "big_order_support": {"status": "confirmed"},
            "regulatory_risk": {"level": "normal"}, "risks": [],
        }
        weak = {
            "eligible": True, "priority_tier": "首板观察", "industry": "另一独立题材",
            "score": 85, "continuation_base_score": 55, "float_market_cap": 8_000_000_000,
            "auction_gap_percent": 5, "big_order_support": {"status": "unknown"},
            "regulatory_risk": {"level": "normal"}, "risks": [],
        }
        _apply_dynamic_context([strong, weak])
        self.assertTrue(strong["eligible"])
        self.assertEqual(strong["priority_tier"], "一进二观察")
        self.assertFalse(weak["eligible"])
        self.assertEqual(weak["priority_tier"], "不入选")

    def test_board_review_counts_only_executable_and_targets_next_day_limit(self):
        outcome = {
            "close": 10, "daily_change_percent": 10,
            "next_day": {"close_return_percent": 10, "limit_up": True},
        }
        executable = _review_candidate(
            {"qualified": True, "decision": "连板核心A级预选", "reference_price": 10},
            "board", outcome, market_weak=False,
        )
        observation = _review_candidate(
            {"qualified": False, "decision": "连板核心B级预选", "reference_price": 10},
            "board", outcome, market_weak=False,
        )
        self.assertTrue(executable["success"])
        self.assertEqual(executable["cause"], "T+1涨停")
        self.assertFalse(observation["counted"])

    def test_big_order_support_distinguishes_confirmed_weak_and_unknown(self):
        self.assertEqual(_big_order_support(3, 0.2)["status"], "confirmed")
        self.assertEqual(_big_order_support(-4, -0.2)["status"], "weak")
        self.assertEqual(_big_order_support(None, None)["status"], "unknown")

    def test_dynamic_context_rewards_theme_and_penalizes_regulation(self):
        candidates = [
            {"eligible": True, "score": 90, "industry": "光学光电子", "auction_gap_percent": 6, "float_market_cap": 8_000_000_000, "big_order_support": {"status": "confirmed"}, "regulatory_risk": {"level": "normal"}},
            {"eligible": True, "score": 90, "industry": "通信设备", "auction_gap_percent": 5, "float_market_cap": 8_000_000_000, "big_order_support": {"status": "unknown"}, "regulatory_risk": {"level": "high"}},
        ]
        _apply_dynamic_context(candidates)
        self.assertEqual(candidates[0]["theme_bucket"], "大科技")
        self.assertGreater(candidates[0]["selection_score"], candidates[1]["selection_score"])

    def test_regulatory_risk_marks_near_thirty_day_threshold(self):
        bars = self.bars([5 + index * 0.1 for index in range(30)])
        risk = regulatory_risk(bars, 13.5)
        self.assertIn(risk["level"], {"watch", "high"})
        self.assertIsNotNone(risk["thirty_day_change"])

    def test_three_day_abnormal_move_is_watch_not_serious_veto(self):
        bars = self.bars([10] * 28 + [11, 12.1])
        risk = regulatory_risk(bars, 13.31)
        self.assertEqual(risk["level"], "watch")
        self.assertTrue(risk["ordinary_trigger"])
        self.assertFalse(risk["serious_trigger"])

    def test_historical_prefilter_uses_only_bars_before_target_date(self):
        strong = self.bars([10, 10, 10, 10, 11, 12.1, 13.31, 13.31, 13.5, 14, 14.5, 15])
        weak = self.bars([15, 14.8, 14.5, 14.2, 14, 13.8, 13.5, 13.2, 13, 12.8, 12.5, 12.2])
        target = date(2026, 2, 1)
        self.assertGreater(_history_prefilter_score(strong, target), _history_prefilter_score(weak, target))

    def test_premarket_candidate_uses_completed_history_only(self):
        bars = self.bars(
            [10, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9, 11, 12.1],
            [1000] * 11 + [1800],
        )
        result = _premarket_candidate(
            {"f12": "600001", "f14": "盘前样本", "f100": "测试", "f20": 8_000_000_000},
            bars, date(2026, 1, 13),
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["code"], "600001")
        self.assertGreaterEqual(result["recent_limit_up_count"], 1)
        self.assertTrue(result["reasons"])

    def test_auction_prefilter_covers_main_board_and_excludes_unavailable_markets(self):
        valid = [
            {"f12": f"6000{index:02d}", "f14": f"主板{index}", "f3": index / 10, "f6": 100_000_000 + index,
             "f8": 3, "f10": 1.2, "f20": 8_000_000_000, "f184": 2}
            for index in range(10)
        ]
        invalid = [
            {"f12": "300750", "f14": "创业板", "f3": 3, "f6": 1_000_000_000, "f8": 3, "f10": 2, "f20": 100_000_000_000, "f184": 8},
            {"f12": "600999", "f14": "ST风险", "f3": 3, "f6": 1_000_000_000, "f8": 3, "f10": 2, "f20": 100_000_000_000, "f184": 8},
        ]
        selected = _prefilter_auction_universe(valid + invalid, limit=6)
        self.assertEqual(len(selected), 6)
        self.assertTrue(all(is_main_board(item["f12"]) and "ST" not in item["f14"] for item in selected))

    def test_live_prefilter_adds_extreme_auction_stock_outside_top300(self):
        base = [{"f12": f"600{i:03d}", "f14": f"普通{i}"} for i in range(300)]
        hansen = {
            "f12": "002412", "f14": "汉森制药", "f3": 10.04,
            "f6": 220_000_000, "f20": 5_100_000_000,
        }
        expanded = _expand_live_prefilter_with_extremes(base, base + [hansen])
        self.assertEqual(len(expanded), 301)
        self.assertEqual(expanded[-1]["f12"], "002412")

    def test_previous_limit_up_pool_forces_hansen_into_deep_scan(self):
        snapshots = [{"f12": "002412", "f14": "汉森制药", "f20": 5_100_000_000, "f21": 5_000_000_000}]
        pool = [{"c": "002412", "n": "汉森制药", "lbc": 3, "zbc": 0, "hybk": "中药Ⅱ"}]
        expanded = _expand_with_previous_limit_ups([], snapshots, pool)
        self.assertEqual([item["f12"] for item in expanded], ["002412"])
        self.assertTrue(expanded[0]["_force_live_quote"])
        self.assertEqual(expanded[0]["_previous_limit_up_streak"], 3)

    def test_mature_chain_can_enter_b_watch_but_not_a_liquidity_tier(self):
        hansen = _auction_amount_qualification(143_452_720, True, 3, 100, "unknown")
        hasen = _auction_amount_qualification(36_429_336, True, 2, 98, "confirmed")
        shenqi = _auction_amount_qualification(42_193_504, False, 0, 82, "unknown", True)
        hasen_unknown = _auction_amount_qualification(36_429_336, True, 2, 98, "unknown")
        weak = _auction_amount_qualification(36_429_336, True, 2, 98, "weak")
        self.assertEqual(hansen, (True, "A"))
        self.assertEqual(hasen, (True, "B"))
        self.assertEqual(hasen_unknown, (True, "B"))
        self.assertEqual(shenqi, (True, "B"))
        self.assertEqual(weak, (False, "blocked"))

    def test_strong_a_liquidity_does_not_depend_on_auxiliary_fund_source(self):
        confirmed = _auction_amount_qualification(143_452_720, True, 3, 100, "confirmed")
        weak = _auction_amount_qualification(143_452_720, True, 3, 100, "weak")
        self.assertEqual(confirmed, (True, "A"))
        self.assertEqual(weak, (True, "A"))

    def test_dynamic_selection_reserves_leader_repair_behind_core(self):
        core = {"code": "002412", "eligible": True, "tradable": True, "priority_tier": "连板优先", "score": 100, "selection_score": 100, "continuation_score": 70, "auction_amount": 143_000_000, "auction_liquidity_tier": "A"}
        lower_liquidity = {"code": "603958", "eligible": True, "tradable": True, "priority_tier": "连板优先", "score": 94, "selection_score": 97, "continuation_score": 90, "auction_amount": 36_000_000, "auction_liquidity_tier": "B"}
        repair = {"code": "600613", "eligible": True, "tradable": True, "priority_tier": "龙头修复观察", "score": 85, "selection_score": 85, "continuation_score": 60, "auction_amount": 42_000_000, "auction_volume_percent": 4.4, "auction_liquidity_tier": "B", "leader_repair_matched": True, "risk_veto": False}
        selected = _select_high_confidence_candidates([lower_liquidity, core, repair], 6)
        self.assertEqual([item["code"] for item in selected], ["002412", "603958", "600613"])

    def test_primary_board_prefers_exact_industry(self):
        boards = [{"code": "BK1", "name": "食品饮料"}, {"code": "BK2", "name": "白酒Ⅱ"}]
        self.assertEqual(_primary_board("白酒Ⅱ", boards)["code"], "BK2")

    def test_market_gate_can_force_empty_position(self):
        strong = _market_gate(0.6, 45, 3, 0.8, 3)
        weak = _market_gate(0.3, 5, 30, -1.2, 0)
        self.assertEqual(strong["state"], "可观察")
        self.assertTrue(strong["allow_new_positions"])
        self.assertEqual(weak["state"], "空仓")
        self.assertFalse(weak["allow_new_positions"])

    def test_board_plan_gate_uses_only_auction_candidates(self):
        candidate = {
            "score": 80, "auction_gap_percent": 3, "auction_amount": 60_000_000,
            "decision_main_ratio": 1,
        }
        gate = _auction_gate([candidate, candidate, candidate])
        self.assertEqual(gate["state"], "可观察")
        self.assertEqual(gate["candidate_count"], 3)
        self.assertEqual(gate["source"], "前序交易日K线 + 09:25最终竞价")

    def test_auction_phase_starts_observation_at_0920(self):
        from datetime import datetime

        self.assertEqual(_auction_phase(datetime(2026, 8, 19, 9, 19, 59)), "preauction")
        self.assertEqual(_auction_phase(datetime(2026, 8, 19, 9, 20, 0)), "indicative")
        self.assertEqual(_auction_phase(datetime(2026, 8, 19, 9, 24, 59)), "indicative")
        self.assertEqual(_auction_phase(datetime(2026, 8, 19, 9, 25, 0)), "final")

    def test_indicative_gate_uses_0920_source_label(self):
        candidate = {
            "score": 80, "auction_gap_percent": 3, "auction_amount": 60_000_000,
            "decision_main_ratio": 1,
        }
        gate = _auction_gate([candidate], {"auction_phase": "indicative"})
        self.assertEqual(gate["source"], "前序交易日K线 + 09:20不可撤单阶段参考撮合")

    def test_board_decision_checks_history_and_auction_only(self):
        candidate = {
            "score": 85, "auction_gap_percent": 3, "auction_amount": 60_000_000,
            "auction_volume_percent": 1.0, "price_vs_ma5_percent": 2,
            "three_day_change_percent": 5, "previous_volume_ratio": 1.2,
            "decision_main_ratio": 1,
        }
        result = _auction_decision(candidate)
        self.assertEqual(result["action"], "竞价A级观察")
        self.assertEqual(result["guard_passed"], 11)
        self.assertEqual(result["guard_total"], 11)
        self.assertTrue(all("盘口" not in check["name"] and "板块" not in check["name"] for check in result["checks"]))

    def test_board_decision_downgrades_when_previous_funds_are_missing(self):
        candidate = {
            "score": 85, "auction_gap_percent": 3, "auction_amount": 20_000_000,
            "auction_volume_percent": 1.0, "price_vs_ma5_percent": 2,
            "three_day_change_percent": 5, "previous_volume_ratio": 1.2,
        }
        result = _auction_decision(candidate)
        self.assertEqual(result["action"], "竞价B级观察")
        self.assertFalse(result["actionable"])
        fund_check = next(check for check in result["checks"] if "主力资金" in check["name"])
        self.assertIsNone(fund_check["passed"])
        self.assertEqual(fund_check["state"], "unknown")
        self.assertEqual(result["guard_total"], 10)

    def test_board_decision_labels_core_chain_candidate(self):
        candidate = {
            "score": 95, "strategy_mode": "连板核心", "previous_day_limit_up": True,
            "consecutive_limit_up_days": 2, "auction_gap_percent": 5,
            "auction_volume_percent": 1.2, "auction_amount": 60_000_000,
            "float_market_cap": 10_000_000_000, "listed_sessions": 80,
            "decision_main_ratio": 1, "three_day_change_percent": 15,
        }
        result = _auction_decision(candidate)
        self.assertEqual(result["action"], "连板核心A级预选")
        self.assertFalse(result["actionable"])
        self.assertTrue(all(check["passed"] for check in result["checks"]))

    def test_strong_three_board_a_liquidity_can_replace_unstable_fund_confirmation(self):
        candidate = {
            "score": 100, "continuation_score": 70, "strategy_mode": "连板核心",
            "priority_tier": "连板优先", "auction_liquidity_tier": "A",
            "previous_day_limit_up": True, "consecutive_limit_up_days": 3,
            "auction_gap_percent": 6.9, "auction_volume_percent": 20,
            "auction_amount": 140_000_000, "float_market_cap": 5_500_000_000,
            "listed_sessions": 80, "decision_main_ratio": -4,
            "three_day_change_percent": 20, "tradable": True,
        }
        result = _auction_decision(candidate)
        self.assertEqual(result["action"], "连板核心A级预选")

    def test_simple_plan_allows_build_only_with_confirmed_funds(self):
        result = {
            "quote": {"price": 12}, "score": 75,
            "metrics": {"price_vs_ma5_percent": 3}, "order_book": {"imbalance": 0.2},
            "price_plan": {
                "build": {"enabled": True, "price_low": 11.5, "price_high": 12},
                "add": {"enabled": True, "price": 12.5},
                "reduce": {"enabled": False, "price": 10.8},
                "exit": {"enabled": False, "price": 10},
            },
        }
        decision = _position_action(result, {"available": True, "current": True, "score": 30}, 0, 0)
        self.assertEqual(decision["action"], "可建仓")
        waiting = _position_action(result, {"available": False, "score": None}, 0, 0)
        self.assertEqual(waiting["action"], "等待")
        stale = _position_action(result, {"available": True, "current": False, "score": 60}, 0, 0)
        self.assertEqual(stale["action"], "等待")

    def test_simple_plan_reduces_weak_held_position(self):
        result = {
            "quote": {"price": 8}, "score": 38,
            "metrics": {"price_vs_ma5_percent": -4}, "order_book": {"imbalance": -0.3},
            "price_plan": {
                "build": {"enabled": False, "price_low": 8, "price_high": 8.5},
                "add": {"enabled": False, "price": 9},
                "reduce": {"enabled": True, "price": 8.5},
                "exit": {"enabled": True, "price": 7.5},
            },
        }
        decision = _position_action(result, {"available": True, "current": True, "score": -40}, 10, 1000)
        self.assertEqual(decision["action"], "减仓")
        self.assertEqual(decision["profit_percent"], -20.0)

    def test_intraday_score_rewards_confirmed_breakout(self):
        strong = _intraday_score(75, 3, 1.5, 4, 70, 3, 0.2, 1)
        weak = _intraday_score(55, -2, 0.3, 18, 95, -3, -0.3, -1)
        self.assertGreaterEqual(strong, 65)
        self.assertGreater(strong, weak)

    def test_intraday_prefilter_excludes_st_and_inactive_by_input_contract(self):
        rows = [
            {"f12": "600001", "f14": "A", "f3": 3, "f6": 200_000_000, "f8": 3, "f10": 1.5, "f20": 5_000_000_000},
            {"f12": "600002", "f14": "B", "f3": -2, "f6": 200_000_000, "f8": 3, "f10": 1.5, "f20": 5_000_000_000},
        ]
        self.assertEqual([item["f12"] for item in _prefilter_snapshots(rows)], ["600001"])

    def test_intraday_prefilter_keeps_fast_limit_down_reversal_before_turning_red(self):
        rows = [{
            "f12": "600613", "f14": "神奇制药", "f2": 8.18, "f3": -4.44,
            "f6": 260_000_000, "f8": 8.0, "f10": 1.2, "f20": 4_800_000_000,
            "f18": 8.56, "f17": 7.70, "f16": 7.70,
        }]
        selected = _prefilter_snapshots(rows)
        self.assertEqual(selected[0]["f12"], "600613")
        self.assertTrue(selected[0]["limit_down_reversal_prefilter"])

    def test_limit_down_reversal_profile_marks_confirmation_and_risks(self):
        row = {
            "recent_5_limit_up_count": 4, "open_gap_percent": -10.05,
            "rebound_from_low_percent": 9.74, "change_percent": -1.29,
            "amount": 260_000_000, "order_imbalance": 0.20,
        }
        profile = _limit_down_reversal_profile(row, sector_change=0.6)
        self.assertTrue(profile["watch"])
        self.assertTrue(profile["confirmed"])
        self.assertEqual(profile["label"], "跌停反核确认")
        self.assertTrue(any("T+1" in risk for risk in profile["risks"]))

    def test_leadership_profile_filters_immature_activity(self):
        mature = _leadership_profile({
            "f12": "600001", "f100": "通信设备", "f20": 80_000_000_000,
            "f6": 600_000_000, "industry_cap_rank": 2, "industry_amount_rank": 1,
            "industry_size": 30,
        })
        immature = _leadership_profile({
            "f12": "600002", "f100": "通信设备", "f20": 3_000_000_000,
            "f6": 80_000_000, "industry_cap_rank": 22, "industry_amount_rank": 4,
            "industry_size": 30,
        })
        self.assertTrue(mature["leadership_qualified"])
        self.assertFalse(immature["leadership_qualified"])
        self.assertGreater(mature["leadership_score"], immature["leadership_score"])

    def test_candidate_history_freezes_first_valid_snapshot(self):
        first = {
            "generated_at": "2026-08-14T09:26:00+08:00",
            "candidates": [{"code": "600001", "name": "首选", "price": 10, "score": 70, "qualified": True}],
        }
        second = {
            "generated_at": "2026-08-14T14:50:00+08:00",
            "candidates": [{"code": "600002", "name": "后改", "price": 20, "score": 80, "qualified": True}],
        }
        original = candidate_history.DATA_FILE
        with TemporaryDirectory() as directory:
            candidate_history.DATA_FILE = Path(directory) / "history.json"
            try:
                self.assertTrue(record_candidates("main_board", first)["recorded"])
                self.assertFalse(record_candidates("main_board", second)["recorded"])
                saved = candidate_history.list_history()["days"][0]["sources"]["main_board"]["candidates"]
                self.assertEqual([item["code"] for item in saved], ["600001"])
            finally:
                candidate_history.DATA_FILE = original

    def test_full_board_snapshots_keep_observation_and_final_separate(self):
        indicative = {
            "selected_date": "2026-08-20", "generated_at": "2026-08-20T09:20:05+08:00",
            "candidates": [{"code": "600001", "name": "观察股"}],
        }
        final = {
            "selected_date": "2026-08-20", "generated_at": "2026-08-20T09:25:10+08:00",
            "candidates": [{"code": "600002", "name": "最终股", "checks": []}],
        }
        original = candidate_history.BOARD_PLAN_FILE
        with TemporaryDirectory() as directory:
            candidate_history.BOARD_PLAN_FILE = Path(directory) / "board-plans.json"
            try:
                self.assertTrue(save_board_plan_snapshot(indicative, "indicative")["saved"])
                self.assertTrue(save_board_plan_snapshot(final, "final")["saved"])
                replay = {**final, "strategy_version": "latest", "candidates": [{"code": "600004", "name": "回放股"}]}
                self.assertTrue(save_board_plan_snapshot(replay, "replay")["saved"])
                observed = load_board_plan_snapshot("2026-08-20", "indicative")
                frozen = load_board_plan_snapshot("2026-08-20", "final")
                latest = load_board_plan_snapshot("2026-08-20", "replay")
                self.assertEqual(observed["candidates"][0]["code"], "600001")
                self.assertEqual(frozen["candidates"][0]["code"], "600002")
                self.assertEqual(frozen["snapshot_kind"], "actual_final")
                self.assertTrue(frozen["frozen"])
                self.assertEqual(latest["candidates"][0]["code"], "600004")
                self.assertEqual(latest["snapshot_kind"], "latest_strategy_replay")
                self.assertFalse(latest["frozen"])
                later = {**final, "candidates": [{"code": "600003", "name": "晚到数据"}]}
                self.assertFalse(save_board_plan_snapshot(later, "final", replace=False)["saved"])
                still_frozen = load_board_plan_snapshot("2026-08-20", "final")
                self.assertEqual(still_frozen["candidates"][0]["code"], "600002")
            finally:
                candidate_history.BOARD_PLAN_FILE = original

    def test_open_confirmation_keeps_frozen_candidate_but_changes_buy_decision(self):
        candidate = {
            "code": "600001", "name": "开盘样本", "auction_price": 10,
            "auction_amount": 10_000_000, "continuation_score": 80,
            "priority_tier": "一进二观察", "board_stage_label": "1进2 · 目标2连板",
        }
        result = {
            "quote": {"price": 10.2, "open_price": 10, "change_percent": 3, "amount": 50_000_000},
            "metrics": {"price_vs_open_percent": 2, "price_vs_ma5_percent": 5, "volume_ratio": 0.2, "turnover_rate": 1},
            "order_book": {"imbalance": 0.2, "signal": "买盘占优"},
            "regulatory_risk": {"level": "normal", "label": "常规"},
        }
        funds = {"is_today": True, "date": date.today().isoformat(), "main_ratio": 2, "main_net": 8_000_000}
        confirmed = _open_confirmation(candidate, result, funds)
        self.assertEqual(confirmed["decision"], "开盘确认 · 小仓试错")
        self.assertEqual(confirmed["board_stage_label"], "1进2 · 目标2连板")
        weak = {**result, "quote": {**result["quote"], "price": 9.4, "change_percent": -3},
                "metrics": {**result["metrics"], "price_vs_open_percent": -6},
                "order_book": {"imbalance": -0.5, "signal": "卖盘占优"}}
        rejected = _open_confirmation(candidate, weak, funds)
        self.assertEqual(rejected["decision"], "放弃买入")
        self.assertEqual(rejected["code"], confirmed["code"])

    def test_open_confirmation_distinguishes_sealed_failed_and_unbuyable_boards(self):
        candidate = {
            "code": "600001", "name": "盘中样本", "auction_price": 10.5,
            "auction_amount": 50_000_000, "continuation_score": 80,
            "priority_tier": "连板优先", "board_stage_label": "2进3",
            "tradable": True, "action": "连板核心A级预选", "_auction_rank": 1,
        }
        common = {
            "metrics": {"price_vs_open_percent": 2, "price_vs_ma5_percent": 8, "volume_ratio": 1.2, "turnover_rate": 5},
            "order_book": {"imbalance": 0.2, "signal": "买盘占优"},
            "regulatory_risk": {"level": "normal", "label": "常规"},
        }
        funds = {"is_today": True, "date": date.today().isoformat(), "main_ratio": 2, "main_net": 8_000_000}
        sealed = _open_confirmation(candidate, {
            **common, "quote": {"price": 11, "previous_close": 10, "open_price": 10.5, "high_price": 11, "change_percent": 10, "amount": 150_000_000},
        }, funds)
        self.assertTrue(sealed["sealed"])
        self.assertEqual(sealed["decision"], "封板确认 · 已持有可观察")
        failed = _open_confirmation(candidate, {
            **common, "quote": {"price": 10.45, "previous_close": 10, "open_price": 10.5, "high_price": 11, "change_percent": 4.5, "amount": 180_000_000},
        }, funds)
        self.assertTrue(failed["failed_board"])
        self.assertEqual(failed["decision"], "炸板转弱 · 放弃追入")
        unbuyable = _open_confirmation({**candidate, "tradable": False}, {
            **common, "quote": {"price": 11, "previous_close": 10, "open_price": 11, "high_price": 11, "change_percent": 10, "amount": 20_000_000},
        }, funds)
        self.assertEqual(unbuyable["decision"], "一字封板 · 排队难成交")

    def test_open_confirmation_requires_nuclear_button_to_hold_auction_support(self):
        candidate = {
            "code": "600001", "name": "反核样本", "auction_price": 10.7,
            "auction_amount": 60_000_000, "continuation_score": 82,
            "priority_tier": "反核按钮观察", "strategy_mode": "反核按钮竞价抄底",
            "board_stage_label": "反核按钮竞价抄底", "tradable": True,
        }
        common = {
            "metrics": {"price_vs_open_percent": 0.5, "price_vs_ma5_percent": 8, "volume_ratio": 1.2, "turnover_rate": 5},
            "order_book": {"imbalance": 0.2, "signal": "买盘占优"},
            "regulatory_risk": {"level": "normal", "label": "常规"},
        }
        funds = {"is_today": True, "date": date.today().isoformat(), "main_ratio": 1, "main_net": 8_000_000}
        confirmed = _open_confirmation(candidate, {
            **common, "quote": {"price": 10.8, "previous_close": 10, "open_price": 10.7, "high_price": 10.8, "change_percent": 8, "amount": 100_000_000},
        }, funds)
        self.assertEqual(confirmed["decision"], "反核承接确认 · 小仓观察")
        rejected = _open_confirmation(candidate, {
            **common,
            "metrics": {**common["metrics"], "price_vs_open_percent": -2.8},
            "order_book": {"imbalance": -0.4, "signal": "卖盘占优"},
            "quote": {"price": 10.35, "previous_close": 10, "open_price": 10.7, "high_price": 10.8, "change_percent": 3.5, "amount": 120_000_000},
        }, funds)
        self.assertEqual(rejected["decision"], "反核承接失败 · 放弃追入")

    def test_execution_risk_rejects_untradable_high_extension(self):
        risk = _execution_risk_profile(4, 9.98, 28.0, 0.72, 9.9, "normal", "unknown")
        self.assertFalse(risk["tradable"])
        self.assertTrue(risk["high_exhaustion"])
        self.assertTrue(risk["shrinking_acceleration"])
        self.assertFalse(risk["risk_veto"])
        safe = _execution_risk_profile(1, 4.0, 7.5, 1.2, 2.0, "normal", "confirmed")
        self.assertTrue(safe["tradable"])
        self.assertFalse(safe["risk_veto"])

    def test_execution_risk_rejects_high_board_expectation_break(self):
        risk = _execution_risk_profile(3, 4.3, 22.8, 1.26, 9.68, "watch", "unknown")
        self.assertTrue(risk["expectation_break"])
        self.assertTrue(risk["risk_veto"])
        self.assertTrue(any("预期明显衰减" in reason for reason in risk["risk_reasons"]))

    def test_auction_trajectory_detects_late_price_and_bid_deterioration(self):
        profile = _profile([
            {"captured_at": "2026-08-20T09:20:05+08:00", "gap_percent": 7.0, "bid_volume5": 100000, "order_imbalance": 0.55},
            {"captured_at": "2026-08-20T09:24:45+08:00", "gap_percent": 4.2, "bid_volume5": 30000, "order_imbalance": -0.10},
            {"captured_at": "2026-08-20T09:25:10+08:00", "gap_percent": 4.0, "bid_volume5": 28000, "order_imbalance": -0.15},
        ])
        self.assertTrue(profile["late_price_deterioration"])
        self.assertTrue(profile["late_bid_withdrawal"])
        self.assertTrue(profile["risk_veto"])

    def test_next_day_strategy_sells_failed_board_and_reduces_extended_board(self):
        base_result = {
            "quote": {"price": 10.5, "previous_close": 10, "open_price": 10.8, "high_price": 11, "low_price": 10.4, "change_percent": 5},
            "metrics": {"price_vs_open_percent": -2.78, "price_vs_ma5_percent": 12, "volume_ratio": 2.2, "turnover_rate": 18},
        }
        candidate = {"code": "001229", "name": "魅视科技", "auction_price": 10.6, "target_board_count": 2, "price_vs_ma5_percent": 15.16}
        failed = _holding_strategy(candidate, base_result, None, True)
        self.assertEqual(failed["decision"], "次日优先卖出")
        sealed_result = {
            **base_result,
            "quote": {**base_result["quote"], "price": 11, "high_price": 11, "change_percent": 10},
            "metrics": {**base_result["metrics"], "price_vs_open_percent": 1.85},
        }
        sealed = _holding_strategy(candidate, sealed_result, None, True)
        self.assertEqual(sealed["decision"], "条件持有 · 冲高减仓")
        self.assertTrue(any("偏离MA5" in reason for reason in sealed["risk_reasons"]))

    def test_review_distinguishes_market_and_rule_failure(self):
        candidate = {
            "code": "600001", "reference_price": 10, "qualified": True,
            "decision": "主板关注", "score": 75, "price_vs_ma5_percent": 2, "volume_ratio": 1.2,
        }
        outcome = {"close": 9.8, "daily_change_percent": -2}
        market = _review_candidate(candidate, "main_board", outcome, market_weak=True)
        rule = _review_candidate(candidate, "main_board", outcome, market_weak=False)
        self.assertEqual(market["attribution"], "市场问题")
        self.assertEqual(rule["attribution"], "规则问题")

    def test_screener_isolates_single_stock_failure(self):
        bars = self.bars([10, 10.2, 10.4, 10.6, 11], [1000] * 5)
        failed_code = next(iter(LEADER_POOL))

        class FakeProvider:
            def quote(self, code):
                if code == failed_code:
                    raise RuntimeError("sample failure")
                return Quote(code, "测试股", 12, 11, 3, 2000, 24000, 2, 11, 12.5, 10.5)

            def history(self, code):
                return bars

        result = screen_leaders(FakeProvider(), per_group=1)
        self.assertEqual(result["scanned"], len(LEADER_POOL))
        self.assertEqual(result["failed"], 1)
        self.assertEqual(len(result["groups"]), 4)
        self.assertTrue(all(len(group["candidates"]) <= 1 for group in result["groups"]))


    def test_daily_recommend_accepts_steady_multi_average_uptrend(self):
        closes = [10 * (1.003 ** index) for index in range(100)]
        bars = self.bars(closes, [1_000_000 + index * 1000 for index in range(100)])
        snapshot = {
            "f12": "600001", "f14": "稳健样本", "f100": "测试行业",
            "f2": closes[-1] * 1.003, "f3": 0.3, "f6": 150_000_000,
            "f8": 2.0, "f20": 10_000_000_000,
        }
        result = _daily_candidate(snapshot, bars)
        self.assertIsNotNone(result)
        self.assertTrue(result["qualified"])
        self.assertGreaterEqual(result["score"], 70)
        self.assertLessEqual(result["max_drawdown_20_percent"], 8)

    def test_daily_recommend_opportunity_requires_live_funds_and_effective_volume(self):
        closes = [10 * (1.003 ** index) for index in range(100)]
        bars = self.bars(closes, [1_000_000 + index * 1000 for index in range(100)])
        base = {
            "f12": "600001", "f14": "精选样本", "f100": "测试行业",
            "f2": closes[-1] * 1.003, "f3": 0.3, "f6": 150_000_000,
            "f8": 2.0, "f10": 1.2, "f20": 10_000_000_000,
        }
        funded = _daily_candidate({**base, "f184": 5}, bars)
        outflow = _daily_candidate({**base, "f184": -3}, bars)
        self.assertTrue(funded["trend_opportunity_qualified"])
        self.assertFalse(outflow["opportunity_qualified"])
        self.assertGreater(funded["opportunity_score"], outflow["opportunity_score"])

    def test_daily_recommend_allows_hot_five_day_gain_and_fast_intraday_rally(self):
        closes = [10 * (1.003 ** index) for index in range(95)]
        for _ in range(5):
            closes.append(closes[-1] * 1.013)
        bars = self.bars(closes, [1_000_000 + index * 1000 for index in range(100)])
        snapshot = {
            "f12": "600001", "f14": "强势样本", "f100": "测试行业",
            "f2": closes[-1] * 1.04, "f3": 4.0, "f6": 150_000_000,
            "f8": 2.0, "f10": 1.2, "f20": 10_000_000_000, "f184": 5,
        }
        result = _daily_candidate(snapshot, bars)
        self.assertGreater(result["return_5_percent"], 8)
        self.assertTrue(result["trend_opportunity_qualified"])
        self.assertIn("近5日上涨偏快，追高需缩小仓位并防冲高回落", result["risks"])

    def test_daily_recommend_rejects_volatile_or_falling_stock(self):
        closes = [10 + (1.8 if index % 2 else -1.2) - index * 0.01 for index in range(100)]
        bars = self.bars(closes, [1_000_000] * 100)
        snapshot = {
            "f12": "600002", "f14": "震荡样本", "f100": "测试行业",
            "f2": 9.0, "f3": -1.0, "f6": 150_000_000,
            "f8": 2.0, "f20": 10_000_000_000,
        }
        result = _daily_candidate(snapshot, bars)
        self.assertIsNotNone(result)
        self.assertFalse(result["qualified"])

    def test_policy_signals_map_recent_official_policy_to_industries(self):
        signals = _policy_signals([{
            "TITLE": "国务院关于中医药振兴发展规划的批复",
            "DOCRELPUBTIME": "2026-07-10", "URL": "https://www.gov.cn/example",
        }], date(2026, 8, 21))
        self.assertEqual(signals[0]["theme"], "医药健康")
        self.assertIn("制药", signals[0]["industry_keywords"])

    def test_global_summary_combines_us_and_asian_markets(self):
        result = _global_summary([
            {"f12": "DJIA", "f14": "道琼斯", "f2": 5200000, "f3": -120},
            {"f12": "NDX", "f14": "纳斯达克", "f2": 2600000, "f3": -100},
            {"f12": "SPX", "f14": "标普500", "f2": 760000, "f3": -80},
            {"f12": "N225", "f14": "日经225", "f2": 6500000, "f3": -30},
            {"f12": "KS11", "f14": "韩国KOSPI", "f2": 690000, "f3": 70},
        ])
        self.assertEqual(result["state"], "外围承压")
        self.assertLess(result["adjustment"], 0)

    def test_tencent_global_fallback_parser(self):
        fields = ["0"] * 33
        fields[1], fields[3], fields[32] = "纳斯达克", "26067.17", "-1.00"
        rows = _tencent_global_rows(f'v_usIXIC="{"~".join(fields)}";')
        self.assertEqual(rows[0]["f12"], "NDX")
        self.assertEqual(rows[0]["f3"], -100)

    def test_external_context_rewards_matching_policy_without_overriding_base_score(self):
        candidate = {"industry": "化学制药", "opportunity_score": 88, "reasons": [], "risks": []}
        context = {
            "policy": {"signals": [{
                "theme": "医药健康", "score": 4, "industry_keywords": ["制药"],
                "policies": [{"title": "健康规划", "date": "2026-07-10", "url": "https://www.gov.cn/"}],
            }]},
            "global_market": {"state": "外围中性", "adjustment": 0, "indexes": []},
        }
        result = apply_external_context(candidate, context)
        self.assertEqual(result["base_opportunity_score"], 88)
        self.assertEqual(result["opportunity_score"], 92)
        self.assertIn("近期政策：医药健康", result["reasons"])


if __name__ == "__main__":
    unittest.main()
