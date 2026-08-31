"""仅用于浏览器回归：隔离假数据，无行情请求、数据库写入或真实交易。

运行 python tests/board_alert_fixture_server.py 后访问 http://127.0.0.1:8001。
"""
import json
import sys
import atexit
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from test_board_selection import candidate
from src.stock_evaluator.board_plan import auction_observation_view
from src.stock_evaluator.board_selection import select_live_recommendations
from src.stock_evaluator.board_focus import DailyFocusStore
from src.stock_evaluator.trade_advice import live_entry_plan

QA_DIRECTORY = TemporaryDirectory(prefix="board-focus-qa-")
atexit.register(QA_DIRECTORY.cleanup)
FOCUS_STORE = DailyFocusStore(Path(QA_DIRECTORY.name) / "focus.json")


def fixtures():
    now = datetime.now().astimezone()
    rows = [candidate(f"60000{i}", name=f"测试股票{i}", industry="测试行业", category="离线回归",
                      previous_close=10, auction_price=10.6, auction_amount=80_000_000,
                      open_price=10.6, score=85, checks=[], passed=8, known_total=9,
                      guard_passed=8, guard_total=9, discovery_source="盘中封板补选" if i == 5 else "09:25冻结候选",
                      strategy_mode="高换手强竞价连板", volume_ratio=2, turnover_rate=5,
                      order_signal="买盘占优", recommendation_badge=None,
                      funds={"available": True, "main_ratio": 5, "main_net": 500_000, "label": "测试资金"})
            for i in range(1, 8)]
    observations = {}
    # 交易时段规则固定在周一，页面时间使用实际时钟；不据此产生真实信号。
    sample_time = datetime(2026, 8, 31, 10, 10)
    select_live_recommendations(rows, sample_time, "可观察", observations)
    select_live_recommendations(rows, sample_time + timedelta(seconds=20), "可观察", observations)
    with patch("src.stock_evaluator.board_focus.intraday_selection_window", return_value=True):
        selected, daily_focus = FOCUS_STORE.select(rows, now)
    for item in rows:
        item["entry_plan"] = live_entry_plan(item)
        item["summary"] = item["selection_reason"]
    board = auction_observation_view({
        "selected_date": now.date().isoformat(), "generated_at": now.isoformat(),
        "auction_phase": "final", "historical": False, "capital": 100000,
        "market": {"state": "可观察", "score": 80, "average_gap": 6},
        "candidates": rows, "screening": {"qualified_count": 7},
        "stage": "离线测试数据", "snapshot_label": "浏览器回归专用 · 非真实推荐",
        "disclaimer": "测试数据，不可据此交易。",
    })
    live = {
        "selected_date": now.date().isoformat(), "generated_at": now.isoformat(),
        "candidates": selected, "watch_candidates": [row for row in rows if not row["recommended"]],
        "confirmed_count": len(selected), "monitored_count": len(rows), "errors": [],
        "daily_focus": daily_focus,
        "recommendation_limit": 5, "method": "测试数据：唯一首选，全天累计最多5只。",
        "disclaimer": "仅浏览器回归，不可交易。",
    }
    return board, live


class FixtureHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "web"), **kwargs)

    def do_POST(self):
        if urlparse(self.path).path != "/api/board-focus/lock":
            return self.send_error(404)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        payload = FOCUS_STORE.lock(body.get("code"), datetime.now().astimezone())
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            board, live = fixtures()
            payload = {
                "/api/board-plan": board, "/api/board-open-guard": live,
                "/api/trading-dates": {"dates": [board["selected_date"]]},
                "/api/premarket": {"candidates": [], "date": board["selected_date"], "method": "测试"},
            }.get(path, {"error": "此接口不在离线回归范围"})
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200 if "error" not in payload else 409)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8001), FixtureHandler).serve_forever()
