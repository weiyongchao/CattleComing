import copy
import io
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.stock_evaluator import open_guard, server
from src.stock_evaluator.board_focus import DailyFocusStore
from src.stock_evaluator.board_research import BoardResearchStore, ResearchError
from src.stock_evaluator.board_selection import select_live_recommendations
from src.stock_evaluator.market import EastmoneyProvider
from src.stock_evaluator.quote_sampling import CHINA_TZ, parse_quote_time, quote_freshness
from test_board_selection import candidate


class QuoteSamplingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 31, 10, tzinfo=CHINA_TZ)

    def row(self, seconds=0, **changes):
        return candidate(quote_time=(self.now + timedelta(seconds=seconds)).isoformat(), **changes)

    def test_source_times_parse_without_inventing_date_only_timestamp(self):
        self.assertEqual(parse_quote_time("20260831100000"), self.now)
        self.assertEqual(parse_quote_time(self.now.timestamp()), self.now)
        self.assertEqual(parse_quote_time("2026-08-31T02:00:00+00:00"), self.now)
        for value in [None, "2026-08-31", "-", float("nan"), -1, "bad"]:
            self.assertIsNone(parse_quote_time(value))

    def test_quote_freshness_rejects_missing_old_future_and_previous_day(self):
        for value in [None, (self.now-timedelta(seconds=61)).isoformat(),
                      (self.now+timedelta(seconds=6)).isoformat(), "2026-08-28T10:00:00+08:00"]:
            self.assertIsNotNone(quote_freshness({"quote_time": value}, self.now)[1])
        self.assertIsNone(quote_freshness(self.row(), self.now)[1])

    def test_repeated_source_time_cannot_be_second_sample(self):
        observations = {}
        row = self.row()
        select_live_recommendations([row], self.now, "可观察", observations)
        self.assertEqual(select_live_recommendations([row], self.now+timedelta(seconds=20), "可观察", observations), [])
        self.assertEqual(row["confirmation_samples"], 1)
        self.assertTrue(row["quote_data_uncertain"])
        row = self.row(20)
        self.assertEqual(len(select_live_recommendations([row], self.now+timedelta(seconds=20), "可观察", observations)), 1)

    def test_missing_book_or_stale_quote_never_becomes_formal(self):
        for changes in [{"quote_time": None}, {"quote_time": "2026-08-28T10:00:00+08:00"}, {"book_available": False}]:
            observations = {}
            for seconds in [0, 20]:
                row = self.row(seconds)
                row.update(changes)
                self.assertEqual(select_live_recommendations([row], self.now+timedelta(seconds=seconds), "可观察", observations), [])
                self.assertTrue(row["quote_data_uncertain"])

    def test_missing_book_pauses_existing_primary(self):
        with TemporaryDirectory() as directory:
            store = DailyFocusStore(Path(directory)/"focus.json")
            observations = {}
            for seconds in [0, 20, 40]:
                row = self.row(seconds, book_available=seconds != 40)
                select_live_recommendations([row], self.now+timedelta(seconds=seconds), "可观察", observations)
                picks, info = store.select([row], self.now+timedelta(seconds=seconds))
            self.assertEqual(picks, [])
            self.assertEqual(info["status"], "awaiting_data")
            self.assertEqual(info["issued_count"], 1)
            self.assertEqual(store._read()["days"]["2026-08-31"]["retired_codes"], [])

    def test_duplicate_pauses_primary_without_retiring_or_spending_quota(self):
        with TemporaryDirectory() as directory:
            store = DailyFocusStore(Path(directory)/"focus.json")
            observations = {}
            for seconds in [0, 20, 40]:
                row = self.row(min(seconds, 20))
                select_live_recommendations([row], self.now+timedelta(seconds=seconds), "可观察", observations)
                picks, info = store.select([row], self.now+timedelta(seconds=seconds))
            self.assertEqual(picks, [])
            self.assertEqual(info["status"], "awaiting_data")
            self.assertEqual(info["issued_count"], 1)
            self.assertEqual(store._read()["days"]["2026-08-31"]["retired_codes"], [])

    def test_tencent_parser_preserves_time_and_bid_one(self):
        values = ["0"]*39
        values[1:7] = ["测试股票", "600001", "11", "10", "10.6", "10000"]
        values[9:11] = ["11", "1234"]
        values[30] = "20260831100000"
        values[32:39] = ["10", "11", "10.6", "0", "10000", "10000", "3"]
        quote = EastmoneyProvider._parse_tencent_quote('v="'+'~'.join(values)+'";')
        self.assertEqual(quote.quote_time, self.now.isoformat())
        self.assertEqual(quote.bid1_volume, 1234)
        self.assertEqual(quote.bid1_price, 11)
        self.assertTrue(quote.book_available)
        values[20] = ""
        quote = EastmoneyProvider._parse_tencent_quote('v="'+'~'.join(values)+'";')
        self.assertFalse(quote.book_available)

    def test_eastmoney_time_field_requested_and_missing_book_not_treated_as_zero(self):
        data = {f"f{i}": 0 for i in (*range(11,21), *range(31,41))}
        data.update(f57="600001", f58="测试股票", f43=11, f60=10, f170=10, f47=10000, f48=1e8,
                    f19=11, f20=1234, f86=self.now.timestamp())
        provider = EastmoneyProvider()
        with patch.object(provider, "_get", return_value=data) as fetch:
            quote = provider.quote("600001")
        self.assertIn("f86", fetch.call_args.args[0])
        self.assertEqual(quote.quote_time, self.now.isoformat())
        self.assertEqual(quote.bid1_volume, 1234)
        self.assertTrue(quote.book_available)
        data.pop("f40")
        with patch.object(provider, "_get", return_value=data):
            self.assertFalse(provider.quote("600001").book_available)


class BoardResearchTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)/"research"
        self.store = BoardResearchStore(self.root)
        self.now = datetime(2026, 8, 31, 10, tzinfo=CHINA_TZ)
        self.snapshot = {"selected_date":"2026-08-31", "strategy_version":"test-baseline", "market":{"state":"可观察"}}

    def row(self, seconds=0, **changes):
        return candidate(quote_time=(self.now+timedelta(seconds=seconds)).isoformat(), continuation_score=73,
                         regulatory_risk={"level":"watch"}, **changes)

    def record(self, seconds=0, row=None, baseline=None):
        return self.store.record([row or self.row(seconds)], self.now+timedelta(seconds=seconds),
                                 snapshot=self.snapshot, baseline_rows=baseline or [])

    def test_four_fresh_samples_create_only_silent_event(self):
        original = self.row()
        before = copy.deepcopy(original)
        self.record(row=original)
        self.assertEqual(original, before)
        for seconds in [20,40]:
            self.record(seconds)
            self.assertEqual(self.store.query("2026-08-31")["summary"]["shadow_stock_count"], 0)
        self.record(60)
        data = self.store.query("2026-08-31")
        self.assertEqual(data["summary"]["shadow_stock_count"], 1)
        self.assertEqual(data["summary"]["formal_stock_count"], 0)
        self.assertTrue(data["samples"][0]["first_shadow_event"])
        self.assertEqual(data["samples"][0]["seal_amount"], 11*10000*100)
        self.assertEqual(data["stocks"][0]["first_shadow_at"], "2026-08-31T10:01:00+08:00")
        self.assertNotIn("recommended", data["samples"][0])

    def test_same_cached_quote_never_triggers_shadow(self):
        for seconds in [0,20,40,60]: self.record(seconds, self.row())
        data = self.store.query("2026-08-31")
        self.assertEqual(data["summary"]["shadow_stock_count"], 0)
        self.assertEqual(data["stocks"][0]["shadow_count"], 1)
        self.assertEqual(data["stocks"][0]["duplicate_count"], 3)

    def test_break_resets_count_and_records_reseal(self):
        for seconds in [0,20,40]: self.record(seconds)
        self.record(60, self.row(60, sealed=False, failed_board=True, price=10.8))
        for seconds in [80,100,120]: self.record(seconds)
        self.assertEqual(self.store.query("2026-08-31")["summary"]["shadow_stock_count"], 0)
        self.record(140)
        stock = self.store.query("2026-08-31")["stocks"][0]
        self.assertEqual(stock["observed_breaks"], 1)
        self.assertEqual(stock["observed_reseals"], 1)
        self.assertEqual(stock["first_shadow_at"], "2026-08-31T10:02:20+08:00")

    def test_missing_data_gap_and_time_reversal_restart_validation(self):
        for seconds in [0,20,40]: self.record(seconds)
        bad = self.row(60); bad["quote_time"] = None
        self.record(60, bad)
        self.record(80)
        self.assertEqual(self.store.query("2026-08-31")["stocks"][0]["shadow_count"], 1)
        self.record(180)
        self.assertEqual(self.store.query("2026-08-31")["stocks"][0]["shadow_count"], 1)
        self.record(200, self.row(170))
        data = self.store.query("2026-08-31")
        self.assertEqual(data["stocks"][0]["shadow_count"], 0)
        self.assertEqual(data["samples"][0]["data_reason"], "行情时间倒退")

    def test_risks_missing_funds_and_wrong_market_never_pass(self):
        cases = [{"corporate_event_risk":{"level":"high","available":True}}, {"corporate_event_checked":False},
                 {"funds":{"available":False}}, {"funds":{"available":True,"main_ratio":5,"date":"2026-08-28"}},
                 {"book_available":False}, {"open_score":89}, {"risk_veto":True}, {"ask_volume5":100}]
        for index, changes in enumerate(cases):
            for seconds in [0,20,40,60]: self.record(seconds, self.row(seconds, code=f"600{index:03d}", **changes))
        self.assertEqual(self.store.query("2026-08-31")["summary"]["shadow_stock_count"], 0)

    def test_history_outside_session_and_query_do_not_create_files(self):
        self.assertEqual(self.store.query("2026-08-31")["summary"]["sample_count"], 0)
        self.assertFalse(self.root.exists())
        self.snapshot["historical"] = True
        self.assertFalse(self.record()["recording"])
        self.snapshot["historical"] = False
        self.assertFalse(self.record(6*3600)["recording"])
        self.assertFalse(self.root.exists())

    def test_restart_preserves_dedupe_and_first_event(self):
        for seconds in [0,20,40,60]: self.record(seconds)
        self.store = BoardResearchStore(self.root)
        self.record(80, self.row(60))
        self.record(100)
        data = self.store.query("2026-08-31")
        self.assertEqual(sum(row["first_shadow_event"] for row in data["samples"]), 1)
        self.assertEqual(data["stocks"][0]["first_shadow_at"], "2026-08-31T10:01:00+08:00")

    def test_corrupt_log_is_not_repaired_or_overwritten(self):
        self.record()
        path = self.root/"2026-08-31.jsonl"
        with path.open("ab") as handle: handle.write(b'{"broken":')
        original = path.read_bytes()
        self.assertFalse(self.record(20)["available"])
        self.assertEqual(path.read_bytes(), original)
        with self.assertRaises(ResearchError): self.store.query("2026-08-31")

    def test_write_failure_returns_visible_error_without_changing_input(self):
        row = self.row(); before = copy.deepcopy(row)
        with patch.object(Path, "mkdir", side_effect=OSError("test denied")):
            result = self.record(row=row)
        self.assertFalse(result["available"])
        self.assertIn("test denied", result["message"])
        self.assertEqual(row, before)
        self.assertFalse(self.root.exists())

    def test_concurrent_duplicate_requests_and_pagination_are_safe(self):
        with ThreadPoolExecutor(max_workers=6) as executor: list(executor.map(lambda _: self.record(), range(6)))
        self.assertEqual(self.store.query("2026-08-31")["summary"]["sample_count"], 1)
        for seconds in [20,40,60]: self.record(seconds)
        first = self.store.query("2026-08-31", limit=2)
        second = self.store.query("2026-08-31", limit=2, before=first["next_before"])
        self.assertEqual([x["id"] for x in first["samples"]+second["samples"]], [4,3,2,1])
        self.assertIsNone(second["next_before"])

    def test_duplicate_still_preserves_new_formal_observation(self):
        self.record()
        self.record(5, self.row(), baseline=[{"code":"600001","recommended":True}])
        data = self.store.query("2026-08-31")
        self.assertEqual(data["summary"]["sample_count"], 2)
        self.assertEqual(data["summary"]["formal_stock_count"], 1)
        self.assertEqual(data["samples"][0]["status"], "duplicate")
        self.assertEqual(data["stocks"][0]["shadow_count"], 1)
        self.assertEqual(data["stocks"][0]["first_formal_at"], "2026-08-31T10:00:05+08:00")

    def test_formal_and_shadow_channels_remain_isolated(self):
        focus = DailyFocusStore(self.root.parent/"focus.json")
        with patch.object(open_guard,"BOARD_RESEARCH_STORE",self.store), patch.object(open_guard,"DAILY_FOCUS_STORE",focus), \
             patch.object(open_guard,"_SELECTION_OBSERVATIONS",{}), patch.object(open_guard,"attach_corporate_event_risks"), \
             patch.object(open_guard,"_check_one",side_effect=lambda row,*_:copy.deepcopy(row)):
            for seconds in [0,20,40,60]:
                rows = [self.row(seconds), candidate("600002",quote_time=(self.now+timedelta(seconds=seconds)).isoformat())]
                result = open_guard.build_open_guard({**self.snapshot,"candidates":rows}, discover_live=False, now=self.now+timedelta(seconds=seconds))
            self.assertEqual([row["code"] for row in result["candidates"]], ["600002"])
            self.assertEqual(result["daily_focus"]["issued_count"], 1)
            self.assertEqual(result["research"]["shadow_stock_count"], 1)
            self.assertFalse(next(row for row in result["watch_candidates"] if row["code"]=="600001")["recommended"])

    def test_research_storage_failure_does_not_block_formal_selection(self):
        self.root.write_text("blocked research directory", encoding="utf-8")
        focus = DailyFocusStore(self.root.parent/"focus.json")
        with patch.object(open_guard,"BOARD_RESEARCH_STORE",self.store), patch.object(open_guard,"DAILY_FOCUS_STORE",focus), \
             patch.object(open_guard,"_SELECTION_OBSERVATIONS",{}), patch.object(open_guard,"attach_corporate_event_risks"), \
             patch.object(open_guard,"_check_one",side_effect=lambda row,*_:copy.deepcopy(row)):
            for seconds in [0,20]:
                row = candidate("600002",quote_time=(self.now+timedelta(seconds=seconds)).isoformat())
                result = open_guard.build_open_guard({**self.snapshot,"candidates":[row]}, discover_live=False,
                                                    now=self.now+timedelta(seconds=seconds))
        self.assertFalse(result["research"]["available"])
        self.assertEqual([row["code"] for row in result["candidates"]], ["600002"])
        self.assertEqual(result["daily_focus"]["issued_count"], 1)
        self.assertEqual(self.root.read_text(encoding="utf-8"), "blocked research directory")

    def test_query_api_read_only_and_validates_parameters(self):
        self.record()
        path = self.root/"2026-08-31.jsonl"
        before = path.read_bytes()
        def get(url):
            handler = object.__new__(server.AppHandler)
            handler.path = url
            handler._json = lambda status,payload:(status,payload)
            return handler.do_GET()
        with patch.object(server,"BOARD_RESEARCH_STORE",self.store):
            self.assertEqual(get("/api/board-research?date=2026-08-31")[0], 200)
            for query in ["date=../../secret", "date=2026-02-30", "date=2026-08-31&code=abc", "limit=501", "limit=x", "before=-1"]:
                self.assertEqual(get("/api/board-research?"+query)[0], 400)
        self.assertEqual(path.read_bytes(), before)

    def test_new_day_has_independent_experiment_state(self):
        for seconds in [0,20,40,60]: self.record(seconds)
        self.now += timedelta(days=1)
        self.snapshot["selected_date"] = "2026-09-01"
        self.record(row=self.row(funds={"available":True,"main_ratio":5,"date":"2026-09-01"}))
        self.assertEqual(self.store.query("2026-09-01")["summary"]["shadow_stock_count"], 0)
        self.assertEqual(self.store.query("2026-08-31")["summary"]["shadow_stock_count"], 1)

    def test_corrupt_json_type_returns_503_instead_of_empty_success(self):
        self.root.mkdir()
        path = self.root/"2026-08-31.jsonl"
        path.write_text('[]\n',encoding="utf-8")
        handler = object.__new__(server.AppHandler)
        handler.path = "/api/board-research?date=2026-08-31"
        handler._json = lambda status,payload:(status,payload)
        with patch.object(server,"BOARD_RESEARCH_STORE",self.store):
            self.assertEqual(handler.do_GET()[0],503)
        self.assertEqual(path.read_text(encoding="utf-8"),'[]\n')

    def test_limits_exclude_too_low_or_formal_floor_scores(self):
        for continuation in [69,75,80]:
            for seconds in [0,20,40,60]:
                row = self.row(seconds, code=f"6000{continuation}")
                row["continuation_score"] = continuation
                self.record(seconds,row)
        self.assertEqual(self.store.query("2026-08-31")["summary"]["shadow_stock_count"],0)

    def test_gap_cannot_invent_unobserved_break_or_reseal(self):
        self.record()
        self.record(120,self.row(120,sealed=False,failed_board=True,price=10.8))
        data = self.store.query("2026-08-31")
        self.assertTrue(data["samples"][0]["coverage_gap"])
        self.assertEqual(data["stocks"][0].get("observed_breaks",0),0)

    def test_unknown_book_or_price_cannot_invent_breaks_or_reseals(self):
        for index, changes in enumerate([{"book_available":False}, {"price":0},
                                        {"price":None}, {"sealed":False,"ask_volume5":100}]):
            code = f"600{index:03d}"
            self.record(0,self.row(code=code))
            self.record(20,self.row(20,code=code,**changes))
            self.record(40,self.row(40,code=code))
            stock = self.store.query("2026-08-31",code=code)["stocks"][0]
            self.assertEqual(stock.get("observed_breaks",0),0)
            self.assertEqual(stock.get("observed_reseals",0),0)
            self.assertEqual(stock["shadow_count"],1)


if __name__ == "__main__":
    unittest.main()
