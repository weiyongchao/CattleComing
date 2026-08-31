"""每日单一首选与持久化提示名额；仅研究提示，不连接交易账户。"""
from __future__ import annotations

import copy
import json
import os
from datetime import datetime
from pathlib import Path
from math import isfinite
from threading import RLock

from .board_selection import MAX_BOARD_PICKS, _number, intraday_selection_window

FOCUS_FILE = Path(__file__).resolve().parents[2] / "data" / "board_daily_focus.json"
_LOCK = RLock()


class DailyFocusError(RuntimeError):
    pass


def potential_profile(item: dict) -> tuple[float, list[str]]:
    continuation = _number(item.get("continuation_score"))
    live = _number(item.get("open_score"))
    funds = _number((item.get("funds") or {}).get("main_ratio"))
    book = _number(item.get("order_imbalance"))
    penalty = (10 if (item.get("regulatory_risk") or {}).get("level") == "watch" else 0)
    penalty += 5 if item.get("opening_dip") else 0
    penalty += 5 if item.get("late_final_seal_watch") else 0
    score = round(max(0, min(100,
        continuation * .45 + live * .30 + max(0, min(10, funds)) * 1.5
        + max(0, min(1, book)) * 10 - penalty,
    )), 2)
    return score, [
        f"历史延续{continuation:g}分×45%", f"盘中确认{live:g}分×30%",
        f"当日主力占比{funds:+.2f}%（最多15分）", f"盘口支撑（最多10分），风险扣{penalty}分",
    ]


def _view(day: dict, status: str, message: str, primary: dict | None = None) -> dict:
    return {
        "available": True, "status": status, "message": message,
        "primary_code": primary.get("code") if primary else None,
        "locked_code": day.get("locked_code"),
        "issued_count": len(day["issued"]), "daily_limit": MAX_BOARD_PICKS,
        "remaining": max(0, MAX_BOARD_PICKS - len(day["issued"])),
        "issued": [{key: value for key, value in item.items() if key != "seed"} for item in day["issued"]],
    }


def _data_uncertain(item: dict | None) -> bool:
    if item is None or item.get("quote_data_uncertain"):
        return True
    event = item.get("corporate_event_risk") or {}
    if (item.get("failed_board") or item.get("near_limit_failure") or item.get("risk_veto")
            or event.get("level") == "high" or (item.get("regulatory_risk") or {}).get("level") == "high"):
        return False
    return (
        item.get("confirmation_samples") == 1
        or not (item.get("funds") or {}).get("available")
        or item.get("ask_volume5") is None or item.get("bid_volume5") is None
        or item.get("quote_source") == "全市场批量行情降级"
        or event.get("available") is False
        or not item.get("corporate_event_checked") and event.get("available") is not True
    )


class DailyFocusStore:
    def __init__(self, path: Path = FOCUS_FILE):
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            if not self.path.exists():
                return {"version": 1, "days": {}}
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") != 1 or not isinstance(payload.get("days"), dict):
                raise ValueError("每日记录格式不正确")
            for day in payload["days"].values():
                if not isinstance(day, dict) or not isinstance(day.get("issued"), list):
                    raise ValueError("每日名额记录不完整")
                codes = [item["code"] for item in day["issued"]]
                if len(codes) != len(set(codes)) or len(codes) > MAX_BOARD_PICKS:
                    raise ValueError("每日名额记录异常")
                if (any(not isinstance(code, str) or len(code) != 6 or not code.isdigit() for code in codes)
                        or not isinstance(day.get("retired_codes"), list)
                        or day.get("primary_code") not in [None, *codes]
                        or day.get("locked_code") not in [None, *codes]
                        or not isinstance(day.get("last_scan"), (int, float)) or not isfinite(day["last_scan"])):
                    raise ValueError("首选状态记录异常")
            return payload
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise DailyFocusError(f"每日提示记录不可用，停止新增首选：{exc}") from exc

    def _write(self, payload: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as exc:
            raise DailyFocusError(f"每日提示记录保存失败，停止新增首选：{exc}") from exc

    def monitored_candidates(self, now: datetime) -> list[dict]:
        with _LOCK:
            day = self._read()["days"].get(now.date().isoformat(), {})
            return [copy.deepcopy(item["seed"]) for item in day.get("issued", []) if item.get("seed")]

    def lock(self, code: str | None, now: datetime) -> dict:
        with _LOCK:
            payload = self._read()
            day = payload["days"].get(now.date().isoformat())
            if not day or (code and code not in {item["code"] for item in day["issued"]}):
                raise ValueError("只能锁定今日已经提示过的首选")
            day["locked_code"] = code or None
            self._write(payload)
            return _view(day, "locked" if code else "unlocked", "已锁定，仅看风险，不再新增买点" if code else "已取消锁定；当日名额不会重置")

    def select(self, rows: list[dict], now: datetime) -> tuple[list[dict], dict]:
        # 统一质量门槛与两次采样已由board_selection验证；不再授予其他合格备选执行标记。
        eligible = []
        for item in rows:
            valid = item.get("confirmation_samples", 0) >= 2 and item.get("recommendation_kind") in {"sealed", "strong_open"}
            item.update(primary_pick=False, recommended=False, actionable=False, execution_ready=False,
                        board_entry_allowed=False, recommendation_badge=None, recommendation_rank=None)
            if valid:
                item["potential_score"], item["potential_basis"] = potential_profile(item)
                item["tone"] = "watch"
                item["decision"] = "合格备选 · 不主动提示"
                item["selection_reason"] = "只关注唯一首选，其他合格标的不自动提示买入"
                if item["potential_score"] >= 80 and _number(item.get("continuation_score")) >= 75:
                    eligible.append(item)
        eligible.sort(key=lambda item: (item["recommendation_kind"] == "sealed", item["potential_score"],
                                       _number(item.get("continuation_score")), str(item["code"])), reverse=True)
        by_code = {str(item["code"]): item for item in eligible}
        all_rows = {str(item.get("code")): item for item in rows}
        with _LOCK:
            payload = self._read()
            day = payload["days"].setdefault(now.date().isoformat(), {
                "issued": [], "primary_code": None, "locked_code": None, "retired_codes": [], "last_scan": 0,
            })
            primary = None
            status, message = "waiting", "暂无达到首选线的股票，宁可不选；备选不会自动占用当日名额"
            if not intraday_selection_window(now):
                return [], _view(day, "non_trading", "非交易时段，不产生新首选")
            if now.timestamp() < day.get("last_scan", 0):
                return [], _view(day, "stale", "过时扫描不更新首选或当日名额")
            day["last_scan"] = now.timestamp()
            if day.get("locked_code"):
                primary = by_code.get(day["locked_code"])
                status, message = "locked", "今日只关注已选定股票，不再提示其他买点；风险提示继续"
            else:
                previous_code = day.get("primary_code")
                primary = by_code.get(previous_code)
                previous_row = all_rows.get(previous_code)
                uncertain = previous_code and not primary and _data_uncertain(previous_row)
                if primary:
                    status, message = "active", "首选条件仍有效，保持原首选，不随每轮小幅排名变化换票"
                elif uncertain:
                    status, message = "awaiting_data", "原首选数据待确认，暂停买点，不用临时替补催促换票"
                else:
                    if previous_code:
                        day["retired_codes"] = list(dict.fromkeys(day["retired_codes"] + [previous_code]))
                        day["primary_code"] = None
                    issued_codes = {item["code"] for item in day["issued"]}
                    options = [item for item in eligible if item["code"] not in day["retired_codes"]]
                    if len(issued_codes) >= MAX_BOARD_PICKS:
                        options = [item for item in options if item["code"] in issued_codes]
                        status, message = "daily_limit", "全天已提示5只不同股票，不再新增；已提示股票的风险监控继续"
                    if options:
                        primary = options[0]
                        code = str(primary["code"])
                        if code not in issued_codes:
                            seed = {key: copy.deepcopy(value) for key, value in primary.items()
                                    if key not in {"corporate_event_risk", "corporate_event_checked"}}
                            day["issued"].append({"code": code, "name": primary.get("name", code),
                                "first_at": now.isoformat(timespec="seconds"), "potential_score": primary["potential_score"], "seed": seed})
                        day["primary_code"] = code
                        status, message = "active", "当前唯一首选：封板优先，按历史延续、盘中确认、资金和盘口综合择优"
            # 名额必须先落盘，保存失败不返回买点，刷新和重启不能绕过当日上限。
            self._write(payload)
            if primary:
                primary.update(primary_pick=True, recommended=True, actionable=True, execution_ready=True,
                               board_entry_allowed=primary["recommendation_kind"] == "sealed", recommendation_rank=1,
                               tone="confirm", decision="今日唯一首选 · " + ("封板确认" if primary["sealed"] else "极强开盘例外"))
                primary["focus_locked"] = bool(day.get("locked_code"))
                if primary["focus_locked"]:
                    primary.update(actionable=False, execution_ready=False, board_entry_allowed=False)
                primary["selection_reason"] = f"首选潜力分{primary['potential_score']:.2f}；{message}。此分数不是收益概率。"
            return [primary] if primary else [], _view(day, status, message, primary)


DAILY_FOCUS_STORE = DailyFocusStore()
