import io
import json
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.stock_evaluator import server
from src.stock_evaluator.board_focus import DailyFocusError, DailyFocusStore
from test_board_focus import qualified


class DailyFocusApiTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = DailyFocusStore(Path(directory.name) / "focus.json")
        self.now = datetime.combine(date.today(), datetime.min.time()).replace(hour=10)
        with patch("src.stock_evaluator.board_focus.intraday_selection_window", return_value=True):
            self.store.select([qualified()], self.now)
        for target, value in [
            ("DAILY_FOCUS_STORE", self.store), ("AppHandler.board_open_cache", None),
            ("AppHandler.board_plan_cache", None), ("AppHandler.board_focus_revision", 0),
        ]:
            patcher = patch(f"src.stock_evaluator.server.{target}", value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def handler(self, body=None, raw=None, length=None):
        handler = object.__new__(server.AppHandler)
        handler.path = "/api/board-focus/lock"
        encoded = raw if raw is not None else json.dumps(body).encode()
        handler.headers = {"Content-Length": str(len(encoded) if length is None else length)}
        handler.rfile = io.BytesIO(encoded)
        handler._json = lambda status, payload: (status, payload)
        return handler

    def test_lock_and_unlock_keep_quota_and_invalidate_cache(self):
        server.AppHandler.board_open_cache = (123, {"old": True})
        status, result = self.handler({"date": date.today().isoformat(), "code": "600001"}).do_POST()
        self.assertEqual(status, 200)
        self.assertEqual(result["locked_code"], "600001")
        self.assertIsNone(server.AppHandler.board_open_cache)
        self.assertEqual(server.AppHandler.board_focus_revision, 1)
        status, result = self.handler({"date": date.today().isoformat(), "code": None}).do_POST()
        self.assertEqual(status, 200)
        self.assertEqual(result["issued_count"], 1)
        self.assertIsNone(result["locked_code"])

    def test_invalid_requests_cannot_change_focus(self):
        today = date.today().isoformat()
        for body in [[], {"date": today}, {"date": "2000-01-01", "code": "600001"},
                     {"date": today, "code": "600999"}, {"date": today, "code": 600001},
                     {"date": today, "code": "300001"}, {"date": today, "code": ""}]:
            with self.subTest(body=body):
                self.assertEqual(self.handler(body).do_POST()[0], 400)
        for raw, length in [(b"broken", None), (b"{}", 9000), (b"", 0)]:
            self.assertEqual(self.handler(raw=raw, length=length).do_POST()[0], 400)
        self.assertEqual(server.AppHandler.board_focus_revision, 0)
        self.assertIsNone(self.store._read()["days"][today]["locked_code"])

    def test_storage_error_is_503_and_does_not_invalidate_valid_state(self):
        with patch.object(self.store, "lock", side_effect=DailyFocusError("test storage failure")):
            status, payload = self.handler({"date": date.today().isoformat(), "code": "600001"}).do_POST()
        self.assertEqual(status, 503)
        self.assertIn("test storage failure", payload["error"])
        self.assertEqual(server.AppHandler.board_focus_revision, 0)

    def test_scan_finishing_after_lock_cannot_restore_old_cache(self):
        handler = self.handler({})
        handler.path = "/api/board-open-guard"

        def scan(*args, **kwargs):
            # 模拟扫描期间另一个请求已成功锁定。
            server.AppHandler.board_focus_revision += 1
            return {"candidates": [{"code": "600001", "recommended": True}]}

        with patch("src.stock_evaluator.server.datetime") as clock, \
             patch("src.stock_evaluator.server.load_board_plan_snapshot", return_value={
                 "selected_date": date.today().isoformat(), "auction_phase": "final", "snapshot_kind": "actual_final",
                 "generated_at": self.now.replace(hour=9, minute=25).isoformat()}), \
             patch("src.stock_evaluator.server.build_open_guard", side_effect=scan):
            clock.now.return_value = self.now
            status, result = handler.do_GET()
        self.assertEqual(status, 409)
        self.assertIn("锁定状态已改变", result["error"])
        self.assertIsNone(server.AppHandler.board_open_cache)


if __name__ == "__main__":
    unittest.main()
