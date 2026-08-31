"""竞价预选 → 最终复核 → 盘中确认；定时采集不依赖浏览器标签页。"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Callable

from .quote_sampling import china_time, parse_quote_time


def session_phase(now: datetime) -> str:
    now = china_time(now)
    clock = now.hour * 100 + now.minute
    if now.weekday() >= 5:
        return "closed"
    if 917 <= clock < 920:
        return "warmup"
    if 920 <= clock < 925:
        return "indicative"
    if 925 <= clock < 930:
        return "final"
    if 930 <= clock < 1130 or 1300 <= clock < 1457:
        return "opening" if clock < 935 else "intraday"
    return "closed"


def timely_final(snapshot: dict | None, now: datetime) -> bool:
    """只把当日09:25–09:30实际采集完的最终竞价当作盘前留痕。"""
    if not snapshot or snapshot.get("historical") or snapshot.get("stale_fallback") or snapshot.get("data_degraded"):
        return False
    now = china_time(now)
    captured = parse_quote_time(snapshot.get("generated_at"))
    return bool(
        captured and captured.date() == now.date() and captured <= now
        and snapshot.get("selected_date") == now.date().isoformat()
        and snapshot.get("auction_phase") == "final"
        and snapshot.get("snapshot_kind") in {"actual_final", "live_calculation"}
        and 925 <= captured.hour * 100 + captured.minute < 930
    )


def workflow_view(snapshot: dict) -> dict:
    phase = snapshot.get("auction_phase")
    replay = bool(snapshot.get("historical") or phase == "historical"
                  or "replay" in str(snapshot.get("snapshot_kind", "")))
    if replay:
        stage, title = "replay", "历史回放 · 不回写竞价预选"
        note = "回放只比较规则，不代表当时已经发出推荐。"
    elif phase in {"indicative", "cancelable"}:
        stage, title = "preselection", "09:20–09:25竞价预选 · 最多5只"
        if phase == "cancelable":
            title = "09:17预热观察 · 09:20重新筛选"
        note = "参考价量动态变化；先看历史结构和竞价强弱，09:25再核验最终成交，不作为买点。"
    elif phase == "preauction":
        stage, title = "waiting", "等待09:20竞价预选"
        note = "先准备历史候选，暂无今日竞价确认。"
    elif snapshot.get("snapshot_kind") == "live_opening_observation":
        stage, title = "late_observation", "开盘后新增观察 · 非09:25预选"
        note = "未留存有效最终竞价名单；当前发现时间单独记录，不倒写成竞价时已入选。"
    else:
        stage, title = "opening_confirmation", "09:25竞价预选定稿 · 等待开盘确认"
        note = "竞价名单保留；09:30后按真实成交、承接、盘口和资金重新排序，达标才提示唯一首选。"
    pool = [item for item in snapshot.get("candidates", []) if not item.get("risk_veto")]
    return {"stage": stage, "title": title, "note": note, "preselection_limit": 5,
            "observation_focus_code": pool[0].get("code") if pool and stage == "preselection"
            and not snapshot.get("stale_fallback") and not snapshot.get("data_degraded") else None,
            "generated_at": snapshot.get("generated_at"), "formal_recommendation": False}


class BoardSessionRunner:
    """单线程、可停止、无重叠采集；API与后台共享调用方的单飞锁。"""

    def __init__(self, auction: Callable, opening: Callable, *, interval: float = 20):
        self.auction, self.opening, self.interval = auction, opening, interval
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_key = None
        self._last_started: datetime | None = None
        self._state = {"running": False, "phase": "closed", "last_success_at": None,
                       "last_error": None, "interval_seconds": interval,
                       "note": "需本地服务持续运行；休市或行情未更新时不生成有效信号。"}

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def step(self, now: datetime | None = None) -> bool:
        now = china_time(now or datetime.now().astimezone())
        phase = session_phase(now)
        key = (now.date(), phase)
        with self._lock:
            self._state["phase"] = phase
        if phase == "closed":
            return False
        if self._last_key == key and self._last_started and 0 <= (now - self._last_started).total_seconds() < self.interval:
            return False
        self._last_key, self._last_started = key, now
        try:
            result = self.auction() if phase in {"warmup", "indicative", "final"} else self.opening()
            error = "本轮行情降级，仅保留观察" if result.get("data_degraded") or result.get("stale_fallback") else None
            with self._lock:
                self._state.update(last_success_at=(result.get("generated_at") or now.isoformat()) if not error else self._state["last_success_at"],
                                   last_error=error)
        except Exception as exc:
            with self._lock:
                self._state["last_error"] = str(exc)
        return True

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def run():
            with self._lock:
                self._state["running"] = True
            try:
                while not self._stop.is_set():
                    self.step()
                    self._stop.wait(1)
            finally:
                with self._lock:
                    self._state["running"] = False

        self._thread = threading.Thread(target=run, name="board-session", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
