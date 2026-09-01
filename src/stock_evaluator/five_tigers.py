"""连板“五虎”横向观察；只调整观察顺序，不授予交易权限。"""
from __future__ import annotations

from math import isfinite

FIVE_TIGERS_LIMIT = 5


def _number(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if isfinite(result) else default
    except (TypeError, ValueError):
        return default


def apply_five_tigers_profile(
    rows: list[dict], *, turnover_field: str, amount_field: str,
    phase: str, gap_field: str = "auction_gap_percent",
) -> dict:
    """先排换手前五，再识别>5%强合力与2%–3%开幅备选。"""
    profile_keys = {
        "five_tigers_member": False, "five_tigers_rank": None,
        "five_tigers_role": None, "five_tigers_priority": 0,
        "five_tigers_label": None, "five_tigers_basis": None,
        "five_tigers_phase": phase,
    }
    for row in rows:
        row.update(profile_keys)

    chain_pool = [
        row for row in rows
        if int(_number(row.get("consecutive_limit_up_days"))) >= 2
        and not row.get("risk_veto", False)
        and (row.get("corporate_event_risk") or {}).get("level") != "high"
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

    focus_pool = [
        row for row in chain_pool
        if row.get("tradable", row.get("auction_tradable", True)) is not False
        and _number(row.get(amount_field)) >= 30_000_000
        and 0 < _number(row.get("float_market_cap")) < 20_000_000_000
        and _number(row.get("listed_sessions")) >= 60
        and _number(row.get(gap_field), -99) >= 1
    ]
    strong = sorted(
        (row for row in focus_pool if _number(row.get(turnover_field)) > 5),
        key=lambda row: (
            _number(row.get(turnover_field)), _number(row.get(gap_field)),
            _number(row.get(amount_field)),
        ), reverse=True,
    )
    fallback = sorted(
        (row for row in focus_pool if 2 <= _number(row.get(turnover_field)) <= 3),
        key=lambda row: (
            _number(row.get(gap_field)), _number(row.get(turnover_field)),
            _number(row.get(amount_field)),
        ), reverse=True,
    )
    primary = strong[0] if strong else fallback[0] if fallback else None
    secondary = fallback[0] if strong and fallback and fallback[0] is not primary else None
    if primary is not None:
        strong_primary = primary in strong
        primary.update(
            five_tigers_role="strong_consensus" if strong_primary else "gap_primary",
            five_tigers_priority=2,
            five_tigers_label="五虎强合力首选观察" if strong_primary else "五虎开幅首选观察",
            five_tigers_basis=(
                f"{phase}换手{_number(primary.get(turnover_field)):.2f}%>5%，按换手率第一优先"
                if strong_primary else
                f"{phase}换手位于2%–3%，竞价涨幅{_number(primary.get(gap_field)):+.2f}%为区间最高"
            ),
        )
    if secondary is not None:
        secondary.update(
            five_tigers_role="gap_fallback", five_tigers_priority=1,
            five_tigers_label="五虎开幅备选观察",
            five_tigers_basis=(
                f"{phase}换手位于2%–3%，竞价涨幅{_number(secondary.get(gap_field)):+.2f}%为区间最高"
            ),
        )

    def view(row: dict) -> dict:
        return {
            "code": row.get("code"), "name": row.get("name"),
            "rank": row.get("five_tigers_rank"),
            "turnover_percent": round(_number(row.get(turnover_field)), 2),
            "auction_gap_percent": round(_number(row.get(gap_field)), 2),
            "role": row.get("five_tigers_role"), "label": row.get("five_tigers_label"),
        }

    focus_rows = [row for row in (primary, secondary) if row is not None]
    return {
        "available": bool(members), "phase": phase,
        "rule": "连板股换手前五；>5%取换手最高，2%–3%取竞价涨幅最高",
        "members": [view(row) for row in members],
        "focus": [view(row) for row in focus_rows],
        "primary_code": primary.get("code") if primary else None,
        "secondary_code": secondary.get("code") if secondary else None,
    }
