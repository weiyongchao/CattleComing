"""连板“五虎”横向观察；只调整观察顺序，不授予交易权限。"""
from __future__ import annotations

import copy
import json
from math import isfinite
import os
from pathlib import Path
from threading import RLock

FIVE_TIGERS_LIMIT = 5
OPENING_FIVE_TIGERS_FILE = Path(__file__).resolve().parents[2] / "data" / "five_tigers_opening.json"
_LOCK = RLock()


def _number(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _reset_profile(rows: list[dict], phase: str) -> None:
    profile = {
        "five_tigers_member": False, "five_tigers_rank": None,
        "five_tigers_role": None, "five_tigers_priority": 0,
        "five_tigers_label": None, "five_tigers_basis": None,
        "five_tigers_phase": phase,
    }
    for row in rows:
        row.update(profile)


def _risk_label(row: dict) -> str | None:
    event = row.get("corporate_event_risk") or {}
    if event.get("level") == "high" or event.get("is_restructuring") or event.get("is_merger_acquisition"):
        return event.get("label") or "并购重组风险剔除"
    if row.get("risk_veto"):
        return (row.get("regulatory_risk") or {}).get("label") or "结构或异动风险剔除"
    return None


def _formal_priority_allowed(row: dict, amount_field: str, gap_field: str) -> bool:
    """五虎名次不受风控改变；该函数只控制对正式候选排序的加权。"""
    return (
        _risk_label(row) is None
        and row.get("tradable", row.get("auction_tradable", True)) is not False
        and _number(row.get(amount_field)) >= 30_000_000
        and 0 < _number(row.get("float_market_cap")) < 20_000_000_000
        and _number(row.get("listed_sessions")) >= 60
        and _number(row.get(gap_field), -99) >= 1
    )


def apply_five_tigers_profile(
    rows: list[dict], *, turnover_field: str, amount_field: str,
    phase: str, gap_field: str = "auction_gap_percent",
) -> dict:
    """先排换手前五，再识别>5%强合力与2%–3%开幅备选。"""
    _reset_profile(rows, phase)

    # 原始五虎是连板横向榜，风险股仍保留名次并明确标注；风险只在
    # focus/正式推荐层剔除，避免名单口径与行情软件不一致。
    chain_pool = [
        row for row in rows
        if int(_number(row.get("consecutive_limit_up_days"))) >= 2
        and _number(row.get(turnover_field), -1) >= 0
    ]
    chain_pool.sort(key=lambda row: (
        _number(row.get(turnover_field)), _number(row.get(gap_field)),
        _number(row.get(amount_field)), str(row.get("code") or ""),
    ), reverse=True)
    members = chain_pool[:FIVE_TIGERS_LIMIT]
    for rank, row in enumerate(members, start=1):
        row.update(five_tigers_member=True, five_tigers_rank=rank,
                   five_tigers_label=f"五虎第{rank}")

    # 首选/开幅备选也只由连板、换手和开幅决定；风险和流动性只决定
    # 后续能否获得正式候选排序加权，不能倒过来篡改五虎定义。
    strong = sorted(
        (row for row in chain_pool if _number(row.get(turnover_field)) > 5),
        key=lambda row: (
            _number(row.get(turnover_field)), _number(row.get(gap_field)),
            _number(row.get(amount_field)),
        ), reverse=True,
    )
    fallback = sorted(
        (row for row in chain_pool if 2 <= _number(row.get(turnover_field)) <= 3),
        key=lambda row: (
            _number(row.get(gap_field)), _number(row.get(turnover_field)),
            _number(row.get(amount_field)),
        ), reverse=True,
    )
    primary = strong[0] if strong else fallback[0] if fallback else None
    secondary = fallback[0] if strong and fallback and fallback[0] is not primary else None
    if primary is not None:
        strong_primary = bool(strong and primary is strong[0])
        priority_allowed = _formal_priority_allowed(primary, amount_field, gap_field)
        primary.update(
            five_tigers_role="strong_consensus" if strong_primary else "gap_primary",
            five_tigers_priority=2 if priority_allowed else 0,
            five_tigers_label="五虎强合力首选观察" if strong_primary else "五虎开幅首选观察",
            five_tigers_basis=(
                f"{phase}换手{_number(primary.get(turnover_field)):.2f}%>5%，按换手率第一优先"
                if strong_primary else
                f"{phase}换手位于2%–3%，竞价涨幅{_number(primary.get(gap_field)):+.2f}%为区间最高"
            ),
        )
    if secondary is not None:
        priority_allowed = _formal_priority_allowed(secondary, amount_field, gap_field)
        secondary.update(
            five_tigers_role="gap_fallback", five_tigers_priority=1 if priority_allowed else 0,
            five_tigers_label="五虎开幅备选观察",
            five_tigers_basis=(
                f"{phase}换手位于2%–3%，竞价涨幅{_number(secondary.get(gap_field)):+.2f}%为区间最高"
            ),
        )

    def view(row: dict) -> dict:
        risk_label = _risk_label(row)
        return {
            "code": row.get("code"), "name": row.get("name"),
            "rank": row.get("five_tigers_rank"),
            "turnover_percent": round(_number(row.get(turnover_field)), 2),
            "auction_gap_percent": round(_number(row.get(gap_field)), 2),
            "amount": round(_number(row.get(amount_field)), 2),
            "role": row.get("five_tigers_role"), "label": row.get("five_tigers_label"),
            "risk_excluded": bool(risk_label), "risk_label": risk_label,
        }

    focus_rows = [row for row in (primary, secondary) if row is not None]
    return {
        "available": bool(members), "phase": phase,
        "rule": "连板股先按实际换手排前五；>5%锁定换手最高者，2%–3%取竞价涨幅最高者",
        "members": [view(row) for row in members],
        "focus": [view(row) for row in focus_rows],
        "primary_code": primary.get("code") if primary else None,
        "secondary_code": secondary.get("code") if secondary else None,
    }


def apply_frozen_five_tigers_profile(rows: list[dict], snapshot: dict) -> dict:
    """恢复09:30固定榜；后续只更新股票状态，不按累计换手更换成员。"""
    phase = str(snapshot.get("phase") or "09:30开盘定稿")
    _reset_profile(rows, phase)
    by_code = {str(row.get("code") or ""): row for row in rows}
    members = copy.deepcopy(snapshot.get("members") or [])
    focus = copy.deepcopy(snapshot.get("focus") or [])
    focus_by_code = {str(item.get("code") or ""): item for item in focus}
    for member in members:
        code = str(member.get("code") or "")
        row = by_code.get(code)
        if row is None:
            continue
        rank = int(_number(member.get("rank"))) or None
        row.update(five_tigers_member=True, five_tigers_rank=rank,
                   five_tigers_label=f"五虎第{rank}" if rank else "五虎观察")
    for code, item in focus_by_code.items():
        row = by_code.get(code)
        if row is None:
            continue
        role = item.get("role")
        risk_label = _risk_label(row)
        priority = 0 if risk_label else 2 if role in {"strong_consensus", "gap_primary"} else 1
        row.update(
            five_tigers_role=role, five_tigers_priority=priority,
            five_tigers_label=item.get("label"),
            five_tigers_basis=f"固定采用{phase}榜单，不随盘中累计换手换股",
        )
    for item in members + focus:
        row = by_code.get(str(item.get("code") or ""))
        risk_label = _risk_label(row) if row else item.get("risk_label")
        item["risk_excluded"] = bool(risk_label)
        item["risk_label"] = risk_label
    return {
        "available": bool(members), "frozen": True, "phase": phase,
        "captured_at": snapshot.get("captured_at"), "source": snapshot.get("source"),
        "rule": snapshot.get("rule") or "连板股09:30实际换手前五；>5%取换手最高，2%–3%取竞价涨幅最高",
        "members": members, "focus": focus,
        "primary_code": snapshot.get("primary_code"),
        "secondary_code": snapshot.get("secondary_code"),
    }


class OpeningFiveTigersStore:
    """按交易日只写一次开盘五虎，服务重启也不改榜。"""
    def __init__(self, path: Path = OPENING_FIVE_TIGERS_FILE):
        self.path = Path(path)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "days": {}}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("version") != 1 or not isinstance(payload.get("days"), dict):
            raise ValueError("开盘五虎快照格式不正确")
        return payload

    def get(self, day: str) -> dict | None:
        with _LOCK:
            value = self._read()["days"].get(day)
            return copy.deepcopy(value) if value else None

    def save_once(self, day: str, profile: dict, *, captured_at: str, source: str) -> dict:
        with _LOCK:
            payload = self._read()
            existing = payload["days"].get(day)
            if existing:
                return copy.deepcopy(existing)
            stored = {
                "phase": "09:30开盘定稿", "captured_at": captured_at, "source": source,
                "rule": profile.get("rule"), "members": copy.deepcopy(profile.get("members") or []),
                "focus": copy.deepcopy(profile.get("focus") or []),
                "primary_code": profile.get("primary_code"),
                "secondary_code": profile.get("secondary_code"),
            }
            payload["days"][day] = stored
            for old_day in sorted(payload["days"])[:-5]:
                payload["days"].pop(old_day, None)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)
            return copy.deepcopy(stored)


OPENING_FIVE_TIGERS_STORE = OpeningFiveTigersStore()
