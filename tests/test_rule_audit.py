import copy
import json
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from src.stock_evaluator import history, open_guard
from src.stock_evaluator.board_focus import DailyFocusStore
from src.stock_evaluator.board_research import BoardResearchStore, _sample
from src.stock_evaluator.board_selection import select_live_recommendations
from src.stock_evaluator.market import DailyBar
from src.stock_evaluator.quote_sampling import CHINA_TZ
from src.stock_evaluator.review_metrics import finite_number, matching_sources, summarize_outcomes
from src.stock_evaluator.rule_audit import build_rule_audit
from test_board_selection import candidate


def history_day(day="2026-08-28", *, premium=6.16, sealed=True):
    row = {"code":"600001", "name":"测试候选", "qualified":True, "continuation_score":59,
           "priority_tier":"早封连板优先", "reference_price":10, "score":99}
    source = {"rule_version":"test", "snapshot_kind":"latest_strategy_replay", "historical_proxy":True,
              "captured_at":day+"T16:00:00+08:00", "candidates":[row]}
    reviewed = copy.deepcopy(source)
    reviewed["candidates"][0]["outcome"] = {"close":11, "same_day_sealed":sealed,
        "next_day":{"date":"2026-08-31", "open_return_percent":9.08, "close_return_percent":premium, "limit_up":False}}
    return {"date":day, "sources":{"board":source}, "review":{"sources":{"board":reviewed}}}


class RuleAuditTests(unittest.TestCase):
    def test_missing_values_are_not_zero_or_false_outcomes(self):
        for value in [None, "", "bad", float("nan"), float("inf"), True]:
            self.assertIsNone(finite_number(value))
        metrics = summarize_outcomes([{"outcome":{"same_day_sealed":None, "next_day":{"limit_up":None}}}])
        self.assertEqual(metrics["t0_count"],0)
        self.assertIsNone(metrics["t1_close_mean"])
        self.assertEqual(metrics["t1_limit_count"],0)

    def test_same_day_and_next_day_denominators_are_independent(self):
        rows = [{"outcome":{"same_day_sealed":True}},
                {"outcome":{"same_day_sealed":False,"next_day":{"close_return_percent":0,"limit_up":False}}}]
        metrics = summarize_outcomes(rows)
        self.assertEqual(metrics["t0_sealed_count"],1)
        self.assertEqual(metrics["t0_count"],2)
        self.assertEqual(metrics["t1_close_count"],1)
        self.assertEqual(metrics["t1_close_positive_count"],0)

    def test_positive_premium_without_limit_is_not_rule_failure(self):
        row = history_day()["review"]["sources"]["board"]["candidates"][0]
        result = history._review_candidate(row,"board",row["outcome"],True)
        self.assertEqual(result["attribution"],"次日强势")
        self.assertFalse(result["success"])  # 旧字段仅表示次日涨停，不能重命名成胜率。
        self.assertFalse(result["execution_verified"])
        view = history._board_review_view(history_day()["review"])
        self.assertIn("非胜率",view["metric_label"])
        self.assertIsNone(view["market_weak"])
        self.assertEqual(view["rule_adjustment"]["suggestions"],[])

    def test_negative_candidate_batch_cannot_prove_market_causation(self):
        row = history_day(premium=-4)["review"]["sources"]["board"]["candidates"][0]
        result = history._review_candidate(row,"board",row["outcome"],True)
        self.assertEqual(result["attribution"],"高开回落")
        row["outcome"]["next_day"]["open_return_percent"] = -2
        self.assertEqual(history._review_candidate(row,"board",row["outcome"],True)["attribution"],"次日负溢价")

    def test_same_version_different_snapshot_or_inputs_do_not_match(self):
        source = history_day()["sources"]["board"]
        self.assertTrue(matching_sources(source,copy.deepcopy(source)))
        for key,value in [("code","600002"),("reference_price",12),("continuation_score",80),("qualified",False)]:
            changed = copy.deepcopy(source); changed["candidates"][0][key] = value
            self.assertFalse(matching_sources(source,changed))
        changed = copy.deepcopy(source); changed["captured_at"] += "changed"
        self.assertFalse(matching_sources(source,changed))

    def test_audit_rejects_mismatched_review_and_preserves_inputs(self):
        day = history_day()
        day["review"]["sources"]["board"]["candidates"][0]["score"] = 1
        payload = {"days":[day]}; original = copy.deepcopy(payload)
        audit = build_rule_audit(payload,as_of=date(2026,8,31))
        self.assertEqual(audit["summary"]["t0_count"],0)
        self.assertFalse(audit["coverage"][0]["review_available"])
        self.assertEqual(payload,original)

    def test_latest_five_keeps_pending_day_instead_of_five_mature_days(self):
        days = [history_day(day) for day in ["2026-08-24","2026-08-25","2026-08-26","2026-08-27","2026-08-28","2026-08-31"]]
        audit = build_rule_audit({"days":days},as_of=date(2026,8,31))
        self.assertEqual(audit["dates"],["2026-08-31","2026-08-28","2026-08-27","2026-08-26","2026-08-25"])
        self.assertEqual(audit["summary"]["t0_count"],5)
        self.assertEqual(audit["summary"]["t1_close_count"],4)
        self.assertEqual(audit["summary"]["replay_count"],5)
        self.assertFalse(audit["can_calibrate_live"])

    def test_future_weekend_or_missing_next_date_does_not_count(self):
        for next_date in ["2026-09-01","2026-08-29",None,"bad","2026-08-28"]:
            day = history_day(); day["review"]["sources"]["board"]["candidates"][0]["outcome"]["next_day"]["date"] = next_date
            self.assertEqual(build_rule_audit({"days":[day]},as_of=date(2026,8,31))["summary"]["t1_close_count"],0)

    def test_intraday_window_keeps_today_but_does_not_use_partial_closes(self):
        days = [history_day(day) for day in ["2026-08-25","2026-08-26","2026-08-27","2026-08-28","2026-08-31"]]
        audit = build_rule_audit({"days":days},as_of=date(2026,8,31),closed_through=date(2026,8,30))
        self.assertEqual(audit["day_count"],5)
        self.assertEqual(audit["summary"]["t0_count"],4)
        self.assertEqual(audit["summary"]["t1_close_count"],0)

    def test_duplicate_codes_do_not_inflate_descriptive_denominator(self):
        day = history_day()
        for source in [day["sources"]["board"],day["review"]["sources"]["board"]]:
            source["candidates"] *= 2
        self.assertEqual(build_rule_audit({"days":[day]},as_of=date(2026,8,31))["summary"]["candidate_count"],1)

    def test_history_read_recalculates_view_without_rewriting_storage(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/"history.json"; plans = Path(directory)/"plans.json"
            path.write_text(json.dumps({"days":{"2026-08-28":history_day()}}),encoding="utf-8")
            plans.write_text('{"days":{}}',encoding="utf-8")
            original = path.read_bytes()
            with patch.object(history,"DATA_FILE",path),patch.object(history,"BOARD_PLAN_FILE",plans):
                result = history.list_board_history()
            self.assertEqual(path.read_bytes(),original)
            self.assertEqual(result["days"][0]["review"]["sources"]["board"]["candidates"][0]["attribution"],"次日强势")

    def test_review_cannot_overwrite_a_snapshot_changed_during_fetch(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/"history.json"
            day = history_day(); source = day["sources"]["board"]
            changed = copy.deepcopy(source); changed["captured_at"] += "-new"
            path.write_text(json.dumps({"days":{"2026-08-28":day}}),encoding="utf-8")
            original = path.read_bytes()
            with patch.object(history,"DATA_FILE",path), \
                 patch.object(history,"_latest_board_source",side_effect=[source,changed]), \
                 patch.object(history,"_closing_outcome",return_value={"close":11,"same_day_sealed":True}):
                with self.assertRaisesRegex(ValueError,"快照已变化"):
                    history.review_day("2026-08-28",provider=Mock())
            self.assertEqual(path.read_bytes(),original)

    def test_strong_next_day_is_separate_from_positive_and_requires_t0_seal(self):
        rows = [{"outcome":{"same_day_sealed":sealed,"next_day":{"close_return_percent":value}}}
                for sealed,value in [(True,5),(True,4.99),(False,10),(True,-2)]]
        result = summarize_outcomes(rows)
        self.assertEqual(result["t1_strong_close_count"],2)
        self.assertEqual(result["sealed_t1_count"],3)
        self.assertEqual(result["sealed_t1_strong_count"],1)

    def test_history_endpoint_only_exposes_five_dates_without_deleting_old_records(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/"history.json"; plans = Path(directory)/"plans.json"
            dates = ["2026-08-24","2026-08-25","2026-08-26","2026-08-27","2026-08-28","2026-08-31"]
            path.write_text(json.dumps({"days":{day:history_day(day) for day in dates}}),encoding="utf-8")
            plans.write_text('{"days":{}}',encoding="utf-8")
            before = path.read_bytes()
            with patch.object(history,"DATA_FILE",path),patch.object(history,"BOARD_PLAN_FILE",plans):
                result = history.list_board_history()
            self.assertEqual(len(result["days"]),5)
            self.assertNotIn("2026-08-24",[row["date"] for row in result["days"]])
            self.assertFalse(result["retention"]["physical_delete"])
            self.assertEqual(path.read_bytes(),before)

    def test_next_day_partial_daily_bar_is_not_a_closing_outcome(self):
        provider = Mock()
        provider.history.return_value = [DailyBar(date(2026,8,27),10,10,10,10,100,1000),
            DailyBar(date(2026,8,28),10,11,11,10,100,1000),DailyBar(date(2026,8,31),11,12,12,11,100,1000)]
        with patch.object(history,"datetime") as clock:
            clock.now.return_value = datetime(2026,8,31,10,tzinfo=CHINA_TZ)
            self.assertIsNone(history._closing_outcome(provider,"600001",date(2026,8,28))["next_day"])
            clock.now.return_value = datetime(2026,8,31,15,5,tzinfo=CHINA_TZ)
            self.assertIn("next_day",history._closing_outcome(provider,"600001",date(2026,8,28)))


class EarlyChainExperimentTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026,8,31,10,tzinfo=CHINA_TZ)

    def row(self,seconds=0,**changes):
        row = candidate(continuation_score=59,early_final_seal_chain_matched=True,previous_day_limit_up=True,
                        previous_final_seal_time=100000,consecutive_limit_up_days=2,auction_amount=60_000_000,
                        auction_volume_percent=3,quote_time=(self.now+timedelta(seconds=seconds)).isoformat())
        row.update(changes)
        return row

    def sample(self,seconds,state=None,**changes):
        return _sample(self.row(seconds,**changes),state or {},self.now+timedelta(seconds=seconds),"可观察",{})

    def test_low_score_chain_requires_own_four_new_samples(self):
        state = {}
        for seconds in [0,20,40,60]:
            sample = self.sample(seconds,state); state = sample["state"]
        self.assertTrue(sample["early_chain_experiment"]["matched"])
        self.assertFalse(sample["shadow_match"])
        self.assertEqual(state["shadow_count"],0)
        self.assertEqual(state["early_chain"]["first_at"],"2026-08-31T10:01:00+08:00")

    def test_original_samples_cannot_be_borrowed_by_structure_experiment(self):
        state = {}
        for seconds in [0,20,40]:
            state = self.sample(seconds,state,continuation_score=73,early_final_seal_chain_matched=False)["state"]
        sample = self.sample(60,state,continuation_score=73)
        self.assertTrue(sample["shadow_match"])
        self.assertFalse(sample["early_chain_experiment"]["matched"])
        self.assertEqual(sample["state"]["early_chain"]["count"],1)

    def test_missing_structure_and_risk_cases_never_pass(self):
        cases = [{"previous_final_seal_time":None},{"previous_final_seal_time":113000},{"consecutive_limit_up_days":1},
                 {"early_final_seal_chain_matched":False},{"auction_gap_percent":9.8},{"auction_volume_percent":1},
                 {"auction_amount":30_000_000},{"book_available":False},{"continuation_score":54},
                 {"continuation_score":75},{"open_score":89},{"funds":{"available":False}},
                 {"corporate_event_risk":{"available":True,"level":"normal","is_restructuring":True}},
                 {"corporate_event_risk":{"available":True,"level":"normal","is_merger_acquisition":True}}]
        for changes in cases:
            state = {}
            for seconds in [0,20,40,60]:
                sample = self.sample(seconds,state,**changes); state = sample["state"]
            self.assertFalse(sample["early_chain_experiment"]["matched"],changes)

    def test_break_gap_stale_or_duplicate_cannot_complete_chain(self):
        for changes,seconds in [({"sealed":False,"price":10.8},60),({"book_available":False},60),
                                ({"quote_time":self.row()["quote_time"]},60),({},150)]:
            state = {}
            for at in [0,20,40]: state = self.sample(at,state)["state"]
            sample = self.sample(seconds,state,**changes)
            self.assertFalse(sample["early_chain_experiment"]["matched"])

    def test_formal_floor_stays_unchanged_and_store_restores_experiment(self):
        with TemporaryDirectory() as directory:
            store = BoardResearchStore(Path(directory)/"research")
            focus = DailyFocusStore(Path(directory)/"focus.json")
            observations = {}; snapshot = {"selected_date":"2026-08-31","market":{"state":"可观察"}}
            for seconds in [0,20,40,60]:
                row = self.row(seconds); now = self.now+timedelta(seconds=seconds)
                original = copy.deepcopy(row)
                store.record([row],now,snapshot=snapshot,baseline_rows=[])
                self.assertEqual(row,original)
                self.assertEqual(select_live_recommendations([row],now,"可观察",observations),[])
                picks, info = focus.select([row],now)
            self.assertEqual(info["issued_count"],0)
            data = BoardResearchStore(store.root).query("2026-08-31")
            self.assertEqual(data["summary"]["early_chain_stock_count"],1)
            self.assertEqual(data["summary"]["shadow_stock_count"],0)
            self.assertEqual(data["summary"]["formal_stock_count"],0)


if __name__ == "__main__":
    unittest.main()
