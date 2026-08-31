import unittest
from datetime import date
from unittest.mock import patch

from src.stock_evaluator.corporate_events import _classify_announcements, attach_corporate_event_risks
from src.stock_evaluator.trade_advice import auction_entry_plan, live_entry_plan


class CorporateEventRiskTests(unittest.TestCase):
    def test_restructuring_preplan_is_high_risk(self):
        rows = [
            {
                "art_code": "AN1",
                "notice_date": "2026-08-25 00:00:00",
                "title": "建设机械发行股份及支付现金购买资产并募集配套资金暨关联交易预案",
                "columns": [{"column_name": "增发预案"}, {"column_name": "关联交易"}],
            },
            {
                "art_code": "AN2",
                "notice_date": "2026-08-25 00:00:00",
                "title": "关于披露本次交易相关预案的一般风险提示暨公司股票复牌的提示性公告",
                "columns": [{"column_name": "复牌公告"}],
            },
            {
                "art_code": "AN3",
                "notice_date": "2026-08-25 00:00:00",
                "title": "关于暂不召开股东会审议本次交易相关事项的公告",
                "columns": [{"column_name": "召开股东大会通知"}],
            },
        ]
        result = _classify_announcements("600984", rows, as_of=date(2026, 8, 26))
        self.assertEqual(result["level"], "high")
        self.assertTrue(result["is_restructuring"])
        self.assertTrue(result["is_related_transaction"])
        self.assertTrue(result["is_resumption"])
        self.assertTrue(result["approval_pending"])
        self.assertIn("审批", "；".join(result["risks"]))

    def test_old_restructuring_notice_does_not_create_current_warning(self):
        result = _classify_announcements("600001", [{
            "art_code": "OLD",
            "notice_date": "2025-01-01 00:00:00",
            "title": "重大资产重组预案",
            "columns": [],
        }], as_of=date(2026, 8, 26))
        self.assertEqual(result["level"], "normal")
        self.assertFalse(result["is_restructuring"])

    def test_acquisition_notice_is_high_risk_for_board_recommendations(self):
        result = _classify_announcements("600002", [{
            "art_code": "ACQ1",
            "notice_date": "2026-08-26 00:00:00",
            "title": "关于拟现金收购目标公司股权的公告",
            "columns": [],
        }], as_of=date(2026, 8, 27))
        self.assertEqual(result["level"], "high")
        self.assertTrue(result["is_merger_acquisition"])
        self.assertTrue(result["is_acquisition"])
        self.assertIn("不进入打板推荐", "；".join(result["risks"]))

    def test_merger_and_reorganization_notice_is_high_risk(self):
        result = _classify_announcements("600003", [{
            "art_code": "MA1",
            "notice_date": "2026-08-26 00:00:00",
            "title": "关于筹划并购重组事项的提示性公告",
            "columns": [],
        }], as_of=date(2026, 8, 27))
        self.assertEqual(result["level"], "high")
        self.assertTrue(result["is_merger_acquisition"])

    def test_normal_event_result_is_not_attached_to_reduce_page_noise(self):
        candidates = [{"code": "600001"}]
        with patch(
            "src.stock_evaluator.corporate_events.corporate_event_risk",
            return_value={"available": True, "level": "normal"},
        ):
            attach_corporate_event_risks(candidates)
        self.assertNotIn("corporate_event_risk", candidates[0])


class EntryAdviceTests(unittest.TestCase):
    def base_candidate(self):
        return {
            "previous_close": 10,
            "auction_price": 10.5,
            "auction_gap_percent": 5,
            "auction_amount": 80_000_000,
            "three_day_change_percent": 8,
            "five_day_change_percent": 15,
            "ten_day_change_percent": 25,
            "continuation_score": 72,
            "actionable": True,
            "board_entry_allowed": False,
            "action": "一进二A级观察",
            "regulatory_risk": {"level": "normal"},
        }

    def test_auction_restructuring_risk_overrides_buy_timing(self):
        candidate = {
            **self.base_candidate(),
            "corporate_event_risk": {
                "available": True,
                "level": "high",
                "risks": ["重组仍待审批"],
            },
        }
        plan = auction_entry_plan(candidate, "final", "可观察")
        self.assertEqual(plan["action"], "重组风险观望")
        self.assertEqual(plan["tone"], "negative")

    def test_final_one_price_candidate_must_wait_for_live_selection(self):
        candidate = {**self.base_candidate(), "actionable": False, "board_entry_allowed": True}
        plan = auction_entry_plan(candidate, "final", "可观察")
        self.assertEqual(plan["action"], "等待盘中封板确认")
        self.assertEqual(plan["tone"], "neutral")

    def test_indicative_candidate_must_wait_for_final_match(self):
        plan = auction_entry_plan(self.base_candidate(), "indicative", "可观察")
        self.assertEqual(plan["action"], "竞价观察")

    def test_live_restructuring_candidate_stays_observation_only(self):
        plan = live_entry_plan({
            "price": 11,
            "auction_price": 10.5,
            "limit_up_price": 11,
            "sealed": True,
            "board_entry_allowed": True,
            "tone": "confirm",
            "corporate_event_risk": {"level": "high", "risks": ["审批未完成"]},
        })
        self.assertEqual(plan["action"], "重组风险观望")

    def test_live_allowed_one_price_board_can_queue(self):
        plan = live_entry_plan({
            "price": 11,
            "auction_price": 11,
            "limit_up_price": 11,
            "sealed": True,
            "board_entry_allowed": True,
            "recommended": True,
            "recommendation_kind": "sealed",
            "tone": "confirm",
        })
        self.assertEqual(plan["action"], "排队打板")

    def test_live_rebound_confirmation_is_observation_not_small_buy(self):
        plan = live_entry_plan({
            "price": 10.6,
            "auction_price": 10.5,
            "limit_up_price": 11,
            "change_percent": 6,
            "sealed": False,
            "failed_board": False,
            "rebound_confirmed": True,
            "tone": "confirm",
        })
        self.assertEqual(plan["action"], "等待封板打板")
        self.assertEqual(plan["tone"], "neutral")

    def test_legacy_allowed_flag_does_not_authorize_live_entry(self):
        plan = live_entry_plan({"sealed": True, "board_entry_allowed": True, "tone": "confirm"})
        self.assertEqual(plan["action"], "观望（已封板）")

    def test_strong_open_requires_formal_selection(self):
        item = {"price": 10.6, "auction_price": 10.5, "change_percent": 6, "tone": "confirm"}
        self.assertEqual(live_entry_plan(item)["tone"], "neutral")
        item.update(recommended=True, recommendation_kind="strong_open")
        self.assertEqual(live_entry_plan(item)["action"], "极强开盘小仓观察")

    def test_live_failed_board_waits_for_reseal(self):
        plan = live_entry_plan({
            "price": 10.8,
            "auction_price": 10.5,
            "limit_up_price": 11,
            "change_percent": 8,
            "sealed": False,
            "failed_board": True,
            "tone": "watch",
        })
        self.assertEqual(plan["action"], "等待回封")


if __name__ == "__main__":
    unittest.main()
