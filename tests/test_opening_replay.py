import unittest

from scripts.replay_opening_candidates import opening_facts


class OpeningReplayTests(unittest.TestCase):
    def row(self, ident, clock, price, amount=100):
        return {"id": ident, "time": clock, "price": price, "amount": amount}

    def test_does_not_use_auction_or_after_cutoff_to_claim_touch(self):
        rows = [self.row(0, "09:25:00", 11), self.row(1, "09:30:00", 10.3),
                self.row(2, "09:35:00", 10.4), self.row(3, "09:35:03", 11)]
        result = opening_facts(rows, 10, 10.2)
        self.assertIsNone(result["first_limit_price_print"])
        self.assertEqual(result["reported_trade_count"], 2)
        self.assertEqual(result["reported_trade_amount_yuan"], 200)
        self.assertEqual(result["last_trade_price"], 10.4)

    def test_touch_and_recovery_never_claim_sealed(self):
        rows = [self.row(1, "09:30:03", 11), self.row(2, "09:30:06", 10.9),
                self.row(3, "09:35:00", 11)]
        result = opening_facts(rows, 10, 10.5)
        self.assertEqual(result["first_limit_price_print"], "09:30:03")
        self.assertEqual(result["below_limit_prints_after_first_touch"], 1)
        self.assertEqual(result["first_below_limit_after_touch"], "09:30:06")
        self.assertTrue(result["last_print_at_limit"])
        self.assertIsNone(result["sealed"])

    def test_main_board_reference_price_uses_cent_rounding(self):
        result = opening_facts([self.row(1, "09:30:00", 16.1)], 14.64, 15)
        self.assertEqual(result["reference_limit_up_price"], 16.1)
        self.assertTrue(result["last_print_at_limit"])

    def test_empty_opening_is_not_zero_return(self):
        self.assertEqual(opening_facts([], 10, 10), {})
        self.assertEqual(opening_facts([self.row(1, "09:36:00", 11)], 10, 10), {})


if __name__ == "__main__":
    unittest.main()
