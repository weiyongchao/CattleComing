from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .evaluator import evaluate
from .market import EastmoneyProvider, MarketDataError
from .screener import is_main_board, sector_context
from .daily_recommend import screen_daily_recommendations
from .funds import individual_fund_flow, sector_fund_leaders
from .auction import screen_auction_candidates
from .auction_trajectory import attach_trajectory, capture_watchlist, record_payload_sample
from .peers import stock_sector_peers
from .board_plan import BOARD_STRATEGY_VERSION, _auction_phase, build_board_plan
from .intraday import build_intraday_plan
from .simple_plan import build_simple_plan
from .history import (
    list_history, load_board_plan_snapshot, load_recorded_board_plan, record_candidates, review_day,
    save_board_plan_snapshot,
)
from .open_guard import build_open_guard
from .next_day import build_next_day_strategy
from .premarket import build_premarket_watchlist
from .stock_search import resolve_stock_code, search_stocks


WEB_ROOT = Path(__file__).resolve().parents[2] / "web"


def _record_safely(source: str, payload: dict) -> None:
    try:
        record_candidates(source, payload)
    except OSError:
        # 历史留痕失败不能阻断当日行情页面。
        pass


class AppHandler(SimpleHTTPRequestHandler):
    provider = EastmoneyProvider()
    screen_cache: tuple[float, dict] | None = None
    sector_cache: tuple[float, dict] | None = None
    auction_cache: tuple[float, dict] | None = None
    board_plan_cache: tuple[float, dict] | None = None
    board_open_cache: tuple[float, dict] | None = None
    next_day_cache: tuple[float, dict] | None = None
    historical_board_cache: dict[str, dict] = {}
    premarket_cache: tuple[str, dict] | None = None
    trading_dates_cache: tuple[float, list[str]] | None = None
    intraday_plan_cache: tuple[float, dict] | None = None
    simple_plan_cache: dict[tuple[str, float, int], tuple[float, dict]] = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/trading-dates":
            import time
            cached_dates = type(self).trading_dates_cache
            if cached_dates and time.time() - cached_dates[0] < 300:
                return self._json(200, {"dates": cached_dates[1]})
            try:
                bars = self.provider.history("600519", limit=12)
                dates = [bar.trade_date.isoformat() for bar in bars if bar.trade_date <= date.today()][-5:]
                if not dates:
                    raise MarketDataError("最近交易日不可用")
                type(self).trading_dates_cache = (time.time(), dates)
                return self._json(200, {"dates": dates, "latest": dates[-1]})
            except MarketDataError as exc:
                return self._json(502, {"error": str(exc)})
        if parsed.path == "/api/history":
            return self._json(200, list_history())
        if parsed.path == "/api/stock-search":
            query = parse_qs(parsed.query).get("q", [""])[0].strip()
            if not query:
                return self._json(200, {"matches": []})
            try:
                return self._json(200, {"matches": search_stocks(query)})
            except MarketDataError as exc:
                return self._json(502, {"error": str(exc)})
        if parsed.path == "/api/board-preselect":
            today = date.today().isoformat()
            cached = type(self).premarket_cache
            if cached and cached[0] == today:
                return self._json(200, cached[1])
            try:
                payload = build_premarket_watchlist(date.today(), self.provider)
                type(self).premarket_cache = (today, payload)
                return self._json(200, payload)
            except MarketDataError as exc:
                return self._json(502, {"error": str(exc)})
        if parsed.path == "/api/simple-plan":
            import time
            from concurrent.futures import ThreadPoolExecutor
            query = parse_qs(parsed.query)
            code = query.get("code", ["600519"])[0]
            normalized_code = "".join(character for character in str(code) if character.isdigit())[-6:]
            if not is_main_board(normalized_code):
                return self._json(400, {"error": "今日决策仅支持沪深主板，不推荐创业板、科创板或北交所股票"})
            code = normalized_code
            try:
                cost_price = max(0.0, float(query.get("cost", ["0"])[0] or 0))
                shares = max(0, int(float(query.get("shares", ["0"])[0] or 0)))
            except ValueError:
                return self._json(400, {"error": "成本价和持仓数量必须是有效数字"})
            cache_key = (code, round(cost_price, 3), shares)
            cached_simple = type(self).simple_plan_cache.get(cache_key)
            if cached_simple and time.time() - cached_simple[0] < 20:
                return self._json(200, cached_simple[1])
            try:
                board_cached, intraday_cached = type(self).board_plan_cache, type(self).intraday_plan_cache
                board = board_cached[1] if board_cached and time.time() - board_cached[0] < 30 else None
                intraday = intraday_cached[1] if intraday_cached and time.time() - intraday_cached[0] < 30 else None
                with ThreadPoolExecutor(max_workers=2) as executor:
                    board_future = executor.submit(build_board_plan, 100_000) if board is None else None
                    intraday_future = executor.submit(build_intraday_plan, 100_000, 12) if intraday is None else None
                    if board_future:
                        board = board_future.result()
                        type(self).board_plan_cache = (time.time(), board)
                    if intraday_future:
                        intraday = intraday_future.result()
                        type(self).intraday_plan_cache = (time.time(), intraday)
                payload = build_simple_plan(board, intraday, code, cost_price, shares)
                type(self).simple_plan_cache[cache_key] = (time.time(), payload)
                return self._json(200, payload)
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            except (MarketDataError, KeyError, TypeError) as exc:
                return self._json(502, {"error": str(exc)})
        if parsed.path == "/api/funds":
            code = parse_qs(parsed.query).get("code", ["600519"])[0]
            try:
                return self._json(200, individual_fund_flow(code))
            except (MarketDataError, ValueError) as exc:
                return self._json(502, {"error": str(exc)})
        if parsed.path == "/api/sector-peers":
            code = parse_qs(parsed.query).get("code", ["600519"])[0]
            try:
                return self._json(200, stock_sector_peers(code))
            except (MarketDataError, ValueError) as exc:
                return self._json(502, {"error": str(exc)})
        if parsed.path == "/api/sector-funds":
            import time
            cached = type(self).sector_cache
            if cached and time.time() - cached[0] < 120:
                return self._json(200, cached[1])
            try:
                payload = sector_fund_leaders()
                type(self).sector_cache = (time.time(), payload)
                return self._json(200, payload)
            except MarketDataError as exc:
                return self._json(502, {"error": str(exc)})
        if parsed.path == "/api/screen":
            import time
            cached = type(self).screen_cache
            if cached and time.time() - cached[0] < 20:
                _record_safely("main_board", cached[1])
                return self._json(200, cached[1])
            payload = screen_daily_recommendations(limit=5)
            type(self).screen_cache = (time.time(), payload)
            _record_safely("main_board", payload)
            return self._json(200, payload)
        if parsed.path == "/api/auction-screen":
            import time
            cached = type(self).auction_cache
            if cached and time.time() - cached[0] < 60:
                return self._json(200, cached[1])
            payload = screen_auction_candidates(limit=3)
            type(self).auction_cache = (time.time(), payload)
            return self._json(200, payload)
        if parsed.path == "/api/board-plan":
            import time
            params = parse_qs(parsed.query)
            selected = params.get("date", [""])[0].strip()
            replay = params.get("mode", [""])[0].strip() == "replay"
            if selected:
                try:
                    target_date = date.fromisoformat(selected)
                except ValueError:
                    return self._json(400, {"error": "日期格式必须为 YYYY-MM-DD"})
                if target_date > date.today():
                    return self._json(400, {"error": "不能回放未来日期"})
                if target_date < date.today() and not replay:
                    latest_replay = load_board_plan_snapshot(selected, "replay")
                    if latest_replay and latest_replay.get("strategy_version") == BOARD_STRATEGY_VERSION:
                        return self._json(200, latest_replay)
                stored_final = None if replay else load_board_plan_snapshot(selected, "final")
                if stored_final and target_date < date.today():
                    return self._json(200, stored_final)
                if target_date < date.today():
                    recorded = None if replay else load_recorded_board_plan(selected)
                    if recorded:
                        return self._json(200, recorded)
                    try:
                        recent_dates = [
                            bar.trade_date.isoformat() for bar in self.provider.history("600519", limit=12)
                            if bar.trade_date < date.today()
                        ][-5:]
                    except MarketDataError as exc:
                        return self._json(502, {"error": str(exc)})
                    if selected not in recent_dates:
                        return self._json(400, {"error": "仅支持最近5个交易日"})
                    cached_history = None if replay else type(self).historical_board_cache.get(selected)
                    if cached_history and cached_history.get("strategy_version") == BOARD_STRATEGY_VERSION:
                        return self._json(200, cached_history)
                    try:
                        payload = build_board_plan(capital=100_000, target_date=target_date)
                    except (MarketDataError, ValueError) as exc:
                        return self._json(502, {"error": str(exc)})
                    payload.update({
                        "snapshot_kind": "strategy_replay",
                        "snapshot_label": "最新版策略历史回放",
                        "frozen": False,
                    })
                    type(self).historical_board_cache[selected] = payload
                    try:
                        save_board_plan_snapshot(payload, "replay")
                    except OSError:
                        pass
                    return self._json(200, payload)
            now = datetime.now()
            current_phase = _auction_phase(now)
            # 09:25后的首次最终结果立即冻结；之后只更新开盘确认，不再改变候选名单。
            if not replay and current_phase == "final":
                frozen = load_board_plan_snapshot(date.today().isoformat(), "final")
                if frozen and frozen.get("strategy_version") == BOARD_STRATEGY_VERSION:
                    return self._json(200, frozen)
                if frozen:
                    latest_replay = load_board_plan_snapshot(date.today().isoformat(), "replay")
                    if latest_replay and latest_replay.get("strategy_version") == BOARD_STRATEGY_VERSION:
                        return self._json(200, latest_replay)
                    cached = type(self).board_plan_cache
                    if (
                        cached and time.time() - cached[0] < 60
                        and cached[1].get("strategy_version") == BOARD_STRATEGY_VERSION
                        and cached[1].get("snapshot_kind") == "latest_strategy_recheck"
                    ):
                        return self._json(200, cached[1])
                    try:
                        payload = build_board_plan(capital=100_000)
                    except (MarketDataError, ValueError) as exc:
                        return self._json(502, {"error": str(exc)})
                    payload.update({
                        "snapshot_kind": "latest_strategy_recheck",
                        "snapshot_label": "当日09:25数据 · 最新规则复核",
                        "frozen": False,
                        "original_snapshot_generated_at": frozen.get("generated_at"),
                        "original_snapshot_candidate_count": len(frozen.get("candidates") or []),
                    })
                    type(self).board_plan_cache = (time.time(), payload)
                    try:
                        save_board_plan_snapshot(payload, "replay")
                    except OSError:
                        pass
                    return self._json(200, payload)
            cached = type(self).board_plan_cache
            if cached and time.time() - cached[0] < 15 and cached[1].get("auction_phase") == current_phase:
                _record_safely("board", cached[1])
                return self._json(200, cached[1])
            payload = build_board_plan(capital=100_000)
            payload.update({
                "snapshot_kind": "live_calculation" if not replay else "strategy_replay",
                "snapshot_label": "当日实时计算" if not replay else "最新版策略回放",
                "frozen": False,
            })
            type(self).board_plan_cache = (time.time(), payload)
            _record_safely("board", payload)
            try:
                if current_phase == "indicative":
                    record_payload_sample(payload, "indicative")
                    save_board_plan_snapshot(payload, "indicative")
                elif current_phase == "final":
                    record_payload_sample(payload, "final")
                    payload = attach_trajectory(payload)
                    save_board_plan_snapshot(payload, "final", replace=False)
                    payload = load_board_plan_snapshot(date.today().isoformat(), "final") or payload
                    type(self).board_plan_cache = (time.time(), payload)
            except OSError:
                pass
            return self._json(200, payload)
        if parsed.path == "/api/auction-trajectory":
            now = datetime.now()
            if not ((now.hour, now.minute) >= (9, 20) and (now.hour, now.minute) < (9, 25)):
                return self._json(409, {"error": "竞价轨迹仅在09:20–09:25采样"})
            snapshot = load_board_plan_snapshot(date.today().isoformat(), "indicative")
            if not snapshot:
                return self._json(409, {"error": "请先生成09:20观察池"})
            try:
                return self._json(200, capture_watchlist(snapshot, self.provider))
            except MarketDataError as exc:
                return self._json(502, {"error": str(exc)})
        if parsed.path == "/api/board-open-guard":
            import time
            now = datetime.now()
            if (now.hour, now.minute) < (9, 30):
                return self._json(409, {"error": "09:30开盘后才生成真实行情确认"})
            frozen_snapshot = load_board_plan_snapshot(date.today().isoformat(), "final")
            cached_board = type(self).board_plan_cache
            latest_board = cached_board[1] if (
                cached_board and cached_board[1].get("selected_date") == date.today().isoformat()
                and cached_board[1].get("strategy_version") == BOARD_STRATEGY_VERSION
                and cached_board[1].get("candidates")
            ) else None
            snapshot = latest_board or frozen_snapshot
            if not snapshot:
                return self._json(409, {"error": "今日09:25最终候选尚未冻结"})
            cached_open = type(self).board_open_cache
            if cached_open and time.time() - cached_open[0] < 10:
                return self._json(200, cached_open[1])
            try:
                payload = build_open_guard(snapshot, self.provider)
            except MarketDataError as exc:
                return self._json(502, {"error": str(exc)})
            type(self).board_open_cache = (time.time(), payload)
            return self._json(200, payload)
        if parsed.path == "/api/next-day-strategy":
            import time
            now = datetime.now()
            if (now.hour, now.minute) < (14, 50):
                return self._json(409, {"error": "14:50后才根据当天交易生成次日持仓预案"})
            snapshot = load_board_plan_snapshot(date.today().isoformat(), "final")
            if not snapshot:
                return self._json(409, {"error": "今日09:25最终候选尚未冻结"})
            cached_next = type(self).next_day_cache
            cache_seconds = 300 if (now.hour, now.minute) >= (15, 5) else 20
            if cached_next and time.time() - cached_next[0] < cache_seconds and cached_next[1].get("finalized") == ((now.hour, now.minute) >= (15, 5)):
                return self._json(200, cached_next[1])
            try:
                payload = build_next_day_strategy(snapshot, self.provider, now)
            except MarketDataError as exc:
                return self._json(502, {"error": str(exc)})
            type(self).next_day_cache = (time.time(), payload)
            return self._json(200, payload)
        if parsed.path == "/api/intraday-plan":
            import time
            cached = type(self).intraday_plan_cache
            if cached and time.time() - cached[0] < 30:
                return self._json(200, cached[1])
            try:
                payload = build_intraday_plan(capital=100_000, limit=12)
            except MarketDataError as exc:
                return self._json(503, {"error": f"盘中实时行情暂不可用：{exc}"})
            type(self).intraday_plan_cache = (time.time(), payload)
            return self._json(200, payload)
        if parsed.path != "/api/evaluate":
            self.path = parsed.path
            return super().do_GET()
        query = parse_qs(parsed.query).get("code", ["600519"])[0]
        try:
            code = resolve_stock_code(query)
            quote = self.provider.quote(code)
            bars = self.provider.history(code)
            context = sector_context(code, self.provider)
            self._json(200, evaluate(quote, bars, context))
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except (MarketDataError, KeyError, TypeError) as exc:
            self._json(502, {"error": str(exc)})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/history/review":
            return self._json(404, {"error": "接口不存在"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            day_key = str(body.get("date") or date.today().isoformat())
            target = date.fromisoformat(day_key)
            now = datetime.now()
            if target > date.today():
                return self._json(400, {"error": "不能复盘未来日期"})
            if target == date.today() and (now.hour, now.minute) < (15, 5):
                return self._json(409, {"error": "15:05后再执行收盘复盘，避免收盘K线尚未落库"})
            return self._json(200, review_day(day_key))
        except (ValueError, json.JSONDecodeError) as exc:
            return self._json(400, {"error": str(exc)})
        except MarketDataError as exc:
            return self._json(502, {"error": str(exc)})


def run() -> None:
    parser = argparse.ArgumentParser(description="启动 A 股量价评测仪表盘")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print(f"股票评测服务已启动：http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()
