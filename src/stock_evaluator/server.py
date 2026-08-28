from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import date, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .evaluator import evaluate
from .market import EastmoneyProvider, MarketDataError
from .screener import is_main_board, sector_context
from .funds import individual_fund_flow, sector_fund_leaders
from .auction import screen_auction_candidates
from .auction_trajectory import attach_trajectory, capture_watchlist, record_payload_sample
from .peers import stock_sector_peers
from .board_plan import BOARD_STRATEGY_VERSION, _auction_phase, build_board_plan
from .intraday import build_intraday_plan
from .simple_plan import build_simple_plan
from .history import (
    list_board_history, list_history, load_board_plan_snapshot, load_recorded_board_plan, record_candidates, review_day,
    save_board_plan_snapshot,
)
from .open_guard import build_open_guard
from .next_day import build_next_day_strategy
from .outlook import infer_next_day_outlook
from .premarket import build_premarket_watchlist
from .stock_search import resolve_stock_code, search_stocks


WEB_ROOT = Path(__file__).resolve().parents[2] / "web"


def _record_safely(source: str, payload: dict) -> None:
    try:
        record_candidates(source, payload)
    except OSError:
        # 历史留痕失败不能阻断当日行情页面。
        pass


def _best_saved_board_snapshot(day_key: str) -> dict | None:
    """同日快照不完整时选候选最多的一份，避免空final遮住有效留痕。"""
    snapshots = [
        item for item in (
            load_board_plan_snapshot(day_key, "final"),
            load_board_plan_snapshot(day_key, "replay"),
            load_recorded_board_plan(day_key),
        ) if item
    ]
    return max(snapshots, key=lambda item: len(item.get("candidates") or [])) if snapshots else None


def _is_actual_final_snapshot(snapshot: dict | None) -> bool:
    """09:25冻结分支只接受真实final留痕，禁止replay冒充当日最终名单。"""
    if not snapshot:
        return False
    kind = str(snapshot.get("snapshot_kind") or "")
    return kind in {"actual_final", "recorded_compact_final", "recovered_final"} or bool(
        snapshot.get("frozen") and snapshot.get("auction_phase") == "final"
    )


def _local_trading_dates(today_value: date | None = None) -> list[str]:
    """行情日历暂不可用时，从已落盘候选和竞价快照恢复最近交易日。"""
    today_value = today_value or date.today()
    dates = {
        str(item.get("date") or "")
        for item in list_history().get("days", [])
        if str(item.get("date") or "") <= today_value.isoformat()
    }
    today_key = today_value.isoformat()
    if any(load_board_plan_snapshot(today_key, phase) for phase in ("indicative", "final", "replay")):
        dates.add(today_key)
    return sorted(item for item in dates if item)[-5:]


class AppHandler(SimpleHTTPRequestHandler):
    provider = EastmoneyProvider()
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
    evaluation_cache: dict[str, tuple[float, object, list, dict]] = {}
    fund_flow_cache: dict[str, tuple[float, dict]] = {}
    board_plan_scan_lock = threading.Lock()

    @classmethod
    def _cached_fund_flow(cls, code: str, cache_seconds: int = 20) -> dict:
        cached = cls.fund_flow_cache.get(code)
        if cached and time.time() - cached[0] < cache_seconds:
            return json.loads(json.dumps(cached[1], ensure_ascii=False))
        payload = individual_fund_flow(code)
        cls.fund_flow_cache[code] = (time.time(), payload)
        return json.loads(json.dumps(payload, ensure_ascii=False))

    @classmethod
    def _build_board_plan_singleflight(
        cls, *, force: bool = False, cache_seconds: int = 15,
    ) -> dict:
        """全站共享一轮竞价扫描，避免多标签页并发打爆历史行情源。"""
        requested_at = time.time()
        expected_phase = _auction_phase(datetime.now())
        cached = cls.board_plan_cache
        if (
            not force and cached and requested_at - cached[0] < cache_seconds
            and cached[1].get("auction_phase") == expected_phase
        ):
            payload = json.loads(json.dumps(cached[1], ensure_ascii=False))
            payload.setdefault("screening", {})["cache_status"] = "memory_hit"
            return payload
        waited_for_scan = cls.board_plan_scan_lock.locked()
        with cls.board_plan_scan_lock:
            cached = cls.board_plan_cache
            now_value = time.time()
            cache_reusable = (
                cached and cached[1].get("auction_phase") == expected_phase and (
                    (not force and now_value - cached[0] < cache_seconds)
                    or (force and cached[0] >= requested_at)
                )
            )
            if cache_reusable:
                payload = json.loads(json.dumps(cached[1], ensure_ascii=False))
                payload.setdefault("screening", {})["cache_status"] = (
                    "singleflight_shared" if waited_for_scan else "memory_hit"
                )
                return payload
            fallback = cached[1] if cached and cached[1].get("candidates") else None
            started = time.time()
            try:
                payload = build_board_plan(capital=100_000)
            except (MarketDataError, ValueError) as exc:
                if not fallback:
                    raise
                payload = json.loads(json.dumps(fallback, ensure_ascii=False))
                payload["data_degraded"] = True
                payload["stale_fallback"] = True
                payload.setdefault("screening", {})["cache_status"] = "stale_fallback"
                payload["screening"]["replay_warning"] = (
                    f"本轮全市场刷新失败，继续展示上一份有效候选，禁止据此新增仓位：{exc}"
                )
                return payload
            payload.setdefault("screening", {}).update({
                "cache_status": "singleflight_built",
                "scan_duration_seconds": round(time.time() - started, 2),
            })
            cls.board_plan_cache = (time.time(), payload)
            return json.loads(json.dumps(payload, ensure_ascii=False))

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
                dates = _local_trading_dates()
                if dates:
                    type(self).trading_dates_cache = (time.time(), dates)
                    return self._json(200, {
                        "dates": dates, "latest": dates[-1], "degraded": True,
                        "warning": f"实时交易日历暂不可用，已使用本地行情留痕：{exc}",
                    })
                return self._json(502, {"error": str(exc)})
        if parsed.path == "/api/history":
            return self._json(200, list_board_history())
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
                if board is None:
                    board = type(self)._build_board_plan_singleflight(cache_seconds=30)
                if intraday is None:
                    intraday = build_intraday_plan(100_000, 12)
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
                return self._json(200, type(self)._cached_fund_flow(code))
            except (MarketDataError, ValueError) as exc:
                return self._json(502, {"error": str(exc)})
        if parsed.path == "/api/next-day-outlook":
            query = parse_qs(parsed.query).get("code", [""])[0]
            try:
                code = resolve_stock_code(query)
                cached = type(self).evaluation_cache.get(code)
                if cached and time.time() - cached[0] < 30:
                    _, quote, bars, result = cached
                else:
                    quote = self.provider.quote(code)
                    bars = self.provider.history(code)
                    context = sector_context(code, self.provider)
                    result = evaluate(quote, bars, context)
                    type(self).evaluation_cache[code] = (time.time(), quote, bars, result)
                funds, funds_error = None, None
                try:
                    funds = type(self)._cached_fund_flow(code)
                except (MarketDataError, ValueError) as exc:
                    funds_error = str(exc)
                return self._json(200, {
                    "code": code, "funds": funds, "funds_error": funds_error,
                    "outlook": infer_next_day_outlook(quote, bars, result, funds),
                })
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            except (MarketDataError, KeyError, TypeError) as exc:
                return self._json(502, {"error": str(exc)})
        if parsed.path == "/api/sector-peers":
            code = parse_qs(parsed.query).get("code", ["600519"])[0]
            try:
                return self._json(200, stock_sector_peers(code))
            except (MarketDataError, ValueError) as exc:
                return self._json(502, {"error": str(exc)})
        if parsed.path == "/api/sector-funds":
            cached = type(self).sector_cache
            if cached and time.time() - cached[0] < 120:
                return self._json(200, cached[1])
            try:
                payload = sector_fund_leaders()
                type(self).sector_cache = (time.time(), payload)
                return self._json(200, payload)
            except MarketDataError as exc:
                return self._json(502, {"error": str(exc)})
        if parsed.path == "/api/auction-screen":
            cached = type(self).auction_cache
            if cached and time.time() - cached[0] < 60:
                return self._json(200, cached[1])
            payload = screen_auction_candidates(limit=3)
            type(self).auction_cache = (time.time(), payload)
            return self._json(200, payload)
        if parsed.path == "/api/board-plan":
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
                    saved = _best_saved_board_snapshot(selected)
                    if saved and saved.get("candidates"):
                        saved = dict(saved)
                        if saved.get("strategy_version") != BOARD_STRATEGY_VERSION:
                            saved.update({
                                "snapshot_label": "当日有效留痕 · 旧规则版本",
                                "strategy_update_pending": True,
                                "next_strategy_version": BOARD_STRATEGY_VERSION,
                            })
                        return self._json(200, saved)
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
                    except MarketDataError:
                        recent_dates = _local_trading_dates()
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
                recovered = _best_saved_board_snapshot(date.today().isoformat())
                if recovered and recovered.get("candidates") and _is_actual_final_snapshot(recovered):
                    displayed = recovered
                    if displayed.get("strategy_version") != BOARD_STRATEGY_VERSION:
                        cached = type(self).board_plan_cache
                        if (
                            cached and time.time() - cached[0] < 60
                            and cached[1].get("snapshot_kind") == "latest_strategy_recheck"
                            and cached[1].get("strategy_version") == BOARD_STRATEGY_VERSION
                        ):
                            return self._json(200, cached[1])
                        try:
                            payload = type(self)._build_board_plan_singleflight(force=True, cache_seconds=60)
                        except (MarketDataError, ValueError) as exc:
                            displayed = dict(displayed)
                            displayed.update({
                                "snapshot_label": "当日09:25原始冻结名单 · 最新策略复核失败",
                                "strategy_update_pending": True,
                                "next_strategy_version": BOARD_STRATEGY_VERSION,
                            })
                            displayed.setdefault("screening", {})["replay_warning"] = f"保留原始名单；新版复核失败：{exc}"
                            return self._json(200, displayed)
                        payload.update({
                            "snapshot_kind": "latest_strategy_recheck",
                            "snapshot_label": "09:25原始留痕保留 · 最新策略复核榜单",
                            "frozen": False,
                            "original_snapshot_generated_at": recovered.get("generated_at"),
                            "original_strategy_version": recovered.get("strategy_version"),
                        })
                        type(self).board_plan_cache = (time.time(), payload)
                        if payload.get("candidates"):
                            try:
                                save_board_plan_snapshot(payload, "replay")
                            except OSError:
                                pass
                        return self._json(200, payload)
                    return self._json(200, displayed)
                if frozen:
                    cached = type(self).board_plan_cache
                    if (
                        cached and time.time() - cached[0] < 60
                        and cached[1].get("snapshot_kind") == "latest_strategy_recheck"
                    ):
                        return self._json(200, cached[1])
                    try:
                        payload = type(self)._build_board_plan_singleflight(force=True, cache_seconds=60)
                    except (MarketDataError, ValueError) as exc:
                        degraded = dict(frozen)
                        degraded["data_degraded"] = True
                        degraded.setdefault("screening", {})["replay_warning"] = (
                            f"09:25原始空榜存在行情失败，暂不视为有效空仓结论；补算失败：{exc}"
                        )
                        type(self).board_plan_cache = (time.time(), degraded)
                        return self._json(200, degraded)
                    payload.update({
                        "snapshot_kind": "latest_strategy_recheck",
                        "snapshot_label": "09:25空快照 · 行情恢复后复核",
                        "frozen": False,
                        "original_snapshot_generated_at": frozen.get("generated_at"),
                    })
                    failed = int((payload.get("screening") or {}).get("failed") or 0)
                    if not payload.get("candidates") and failed:
                        payload["data_degraded"] = True
                        payload.setdefault("screening", {})["replay_warning"] = (
                            f"本轮仍有{failed}只历史行情读取失败，空榜不作为最终空仓结论，60秒后可继续重试。"
                        )
                    type(self).board_plan_cache = (time.time(), payload)
                    if payload.get("candidates"):
                        try:
                            save_board_plan_snapshot(payload, "replay")
                        except OSError:
                            pass
                    return self._json(200, payload)
            payload = type(self)._build_board_plan_singleflight(cache_seconds=15)
            payload.update({
                "snapshot_kind": "live_calculation" if not replay else "strategy_replay",
                "snapshot_label": "当日实时计算" if not replay else "最新版策略回放",
                "frozen": False,
            })
            type(self).board_plan_cache = (time.time(), payload)
            _record_safely("board", payload)
            try:
                if replay:
                    save_board_plan_snapshot(payload, "replay")
                elif current_phase == "indicative":
                    record_payload_sample(payload, "indicative")
                    save_board_plan_snapshot(payload, "indicative")
                elif current_phase == "final":
                    record_payload_sample(payload, "final")
                    payload = attach_trajectory(payload)
                    failed = int((payload.get("screening") or {}).get("failed") or 0)
                    if payload.get("candidates") or failed == 0:
                        save_board_plan_snapshot(payload, "final", replace=False)
                        payload = load_board_plan_snapshot(date.today().isoformat(), "final") or payload
                        type(self).board_plan_cache = (time.time(), payload)
                    else:
                        payload["data_degraded"] = True
                        payload.setdefault("screening", {})["replay_warning"] = (
                            f"本轮有{failed}只历史行情读取失败且候选为空，未冻结空榜；下一轮继续使用内存K线缓存补扫。"
                        )
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
            now = datetime.now()
            frozen_only = (parse_qs(parsed.query).get("scope") or [""])[0] == "frozen"
            guard_scope = "frozen_candidates" if frozen_only else "full_market"
            if (now.hour, now.minute) < (9, 30):
                return self._json(409, {"error": "09:30开盘后才生成真实行情确认"})
            frozen_snapshot = _best_saved_board_snapshot(date.today().isoformat())
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
            if cached_open and time.time() - cached_open[0] < 10 and cached_open[1].get("scope") == guard_scope:
                return self._json(200, cached_open[1])
            try:
                payload = build_open_guard(snapshot, self.provider, discover_live=not frozen_only)
            except MarketDataError as exc:
                return self._json(502, {"error": str(exc)})
            type(self).board_open_cache = (time.time(), payload)
            return self._json(200, payload)
        if parsed.path == "/api/next-day-strategy":
            now = datetime.now()
            if (now.hour, now.minute) < (14, 50):
                return self._json(409, {"error": "14:50后才根据当天交易生成次日持仓预案"})
            snapshot = _best_saved_board_snapshot(date.today().isoformat())
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
            result = evaluate(quote, bars, context)
            type(self).evaluation_cache[code] = (time.time(), quote, bars, result)
            self._json(200, result)
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
