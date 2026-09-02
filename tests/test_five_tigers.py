import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.stock_evaluator.five_tigers import (
    OpeningFiveTigersStore, apply_five_tigers_profile,
    apply_frozen_five_tigers_profile,
)


def chain(code, name, turnover, gap, **changes):
    return {
        "code": code, "name": name, "consecutive_limit_up_days": 3,
        "auction_turnover_percent": turnover, "auction_gap_percent": gap,
        "auction_amount": 60_000_000, "float_market_cap": 8_000_000_000,
        "listed_sessions": 100, "tradable": True, "risk_veto": False,
        **changes,
    }


class FiveTigersProfileTests(unittest.TestCase):
    def test_turnover_leader_and_two_to_three_gap_fallback_are_both_kept(self):
        rows = [
            chain("600540", "新赛股份", 7.05, 9.89),
            chain("600371", "万向德农", 4.62, 7.25),
            chain("002084", "海鸥住工", 4.05, 10.03),
            chain("000560", "我爱我家", 3.96, 5.17),
            chain("002679", "福建金森", 3.57, 9.97),
            chain("002855", "捷荣技术", 2.28, 8.07),
        ]
        result = apply_five_tigers_profile(
            rows, turnover_field="auction_turnover_percent",
            amount_field="auction_amount", phase="09:25最终竞价",
        )
        self.assertEqual([item["code"] for item in result["members"]],
                         ["600540", "600371", "002084", "000560", "002679"])
        self.assertEqual(result["primary_code"], "600540")
        self.assertEqual(result["secondary_code"], "002855")
        self.assertEqual(rows[0]["five_tigers_role"], "strong_consensus")
        self.assertEqual(rows[-1]["five_tigers_role"], "gap_fallback")

    def test_without_turnover_above_five_highest_gap_in_two_to_three_is_primary(self):
        rows = [
            chain("600001", "四点换手", 4.2, 9.8),
            chain("600002", "高开备选", 2.1, 8.2),
            chain("600003", "高换手低开", 2.9, 6.5),
        ]
        result = apply_five_tigers_profile(
            rows, turnover_field="auction_turnover_percent",
            amount_field="auction_amount", phase="09:20动态竞价",
        )
        self.assertEqual(result["primary_code"], "600002")
        self.assertIsNone(result["secondary_code"])
        self.assertEqual(rows[1]["five_tigers_role"], "gap_primary")

    def test_risk_or_liquidity_failure_cannot_become_focus(self):
        rows = [
            chain("600001", "风险股", 8, 8, risk_veto=True),
            chain("600002", "不可交易", 7, 8, tradable=False),
            chain("600003", "金额不足", 6, 8, auction_amount=29_999_999),
        ]
        result = apply_five_tigers_profile(
            rows, turnover_field="auction_turnover_percent",
            amount_field="auction_amount", phase="09:25最终竞价",
        )
        self.assertEqual(result["primary_code"], "600001")
        self.assertEqual([item["code"] for item in result["members"]],
                         ["600001", "600002", "600003"])
        self.assertTrue(result["members"][0]["risk_excluded"])
        self.assertEqual(result["focus"][0]["code"], "600001")
        self.assertTrue(all(row["five_tigers_priority"] == 0 for row in rows))

    def test_frozen_opening_members_do_not_change_with_later_turnover(self):
        rows = [
            chain("600540", "新赛股份", 20, 9.89),
            chain("600227", "后来换手更高", 40, 3.0),
            chain("002855", "捷荣技术", 15, 8.07),
        ]
        snapshot = {
            "phase": "09:30开盘定稿", "captured_at": "2026-09-01T09:30:00+08:00",
            "source": "测试快照", "members": [{
                "code": "600540", "name": "新赛股份", "rank": 1,
                "turnover_percent": 7.05, "auction_gap_percent": 9.89,
                "role": "strong_consensus", "label": "五虎强合力首选观察",
            }],
            "focus": [{
                "code": "600540", "name": "新赛股份", "rank": 1,
                "turnover_percent": 7.05, "auction_gap_percent": 9.89,
                "role": "strong_consensus", "label": "五虎强合力首选观察",
            }, {
                "code": "002855", "name": "捷荣技术", "rank": None,
                "turnover_percent": 2.28, "auction_gap_percent": 8.07,
                "role": "gap_fallback", "label": "五虎开幅备选观察",
            }],
            "primary_code": "600540", "secondary_code": "002855",
        }
        result = apply_frozen_five_tigers_profile(rows, snapshot)
        self.assertEqual(result["primary_code"], "600540")
        self.assertFalse(rows[1]["five_tigers_member"])
        self.assertEqual(rows[0]["five_tigers_priority"], 2)
        self.assertEqual(rows[2]["five_tigers_priority"], 1)

    def test_opening_store_is_write_once_per_day(self):
        with TemporaryDirectory() as directory:
            store = OpeningFiveTigersStore(Path(directory) / "opening.json")
            first = {"members": [{"code": "600540"}], "focus": [], "primary_code": "600540"}
            second = {"members": [{"code": "600227"}], "focus": [], "primary_code": "600227"}
            store.save_once("2026-09-01", first, captured_at="09:30", source="first")
            stored = store.save_once("2026-09-01", second, captured_at="10:00", source="second")
            self.assertEqual(stored["primary_code"], "600540")
            self.assertEqual(store.get("2026-09-01")["source"], "first")


if __name__ == "__main__":
    unittest.main()
