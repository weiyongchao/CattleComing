"""只读五日规则复核：不请求行情、不写历史、不根据收益优化参数。"""
from __future__ import annotations

import copy
from datetime import date

from .review_metrics import finite_number, matching_sources, summarize_outcomes

AUDIT_VERSION = "2026.08.31-review-v2"
LIMITATION = "候选不是成交；盘后回放不能证明盘中可选或可成交。次日溢价以T日收盘为基准，未扣费用，不是策略收益。五日样本含重复股票与不同规则版本，不据此自动改买点。"


def build_rule_audit(history: dict, *, as_of: date, limit: int = 5, closed_through: date | None = None) -> dict:
    if not 1 <= limit <= 20:
        raise ValueError("复核窗口须为1–20个已留存交易日")
    closed_through = min(as_of, closed_through or as_of)
    days = {}
    for day in history.get("days", []):
        try:
            day_date = date.fromisoformat(day["date"])
        except (KeyError, TypeError, ValueError):
            continue
        if day_date <= as_of and day_date.weekday() < 5:
            days[day["date"]] = day
    selected = [days[key] for key in sorted(days, reverse=True)[:limit]]
    rows, coverage = [], []
    for day in selected:
        source = (day.get("sources") or {}).get("board") or {}
        reviewed = ((day.get("review") or {}).get("sources") or {}).get("board") or {}
        if not matching_sources(reviewed, source):
            reviewed = {}
        outcomes = {str(row.get("code")): row for row in reviewed.get("candidates", [])}
        replay = bool(source.get("historical_proxy") or "replay" in str(source.get("snapshot_kind")))
        day_rows, seen = [], set()
        for candidate in source.get("candidates", []):
            code = str(candidate.get("code") or "")
            if not code or code in seen:
                continue
            seen.add(code)
            row = copy.deepcopy(candidate)
            stored = outcomes.get(code, {})
            row["outcome"] = copy.deepcopy(stored.get("outcome") or {})
            if date.fromisoformat(day["date"]) > closed_through:
                row["outcome"] = {}
            next_day = row["outcome"].get("next_day") or {}
            # T+1必须是后续已收盘交易日；缺失日期不能当作已实现结果。
            try:
                next_date = date.fromisoformat(next_day.get("date", ""))
                if not date.fromisoformat(day["date"]) < next_date <= closed_through or next_date.weekday() >= 5:
                    row["outcome"].pop("next_day", None)
            except (TypeError, ValueError):
                row["outcome"].pop("next_day", None)
            row.update(date=day["date"], replay=replay, rule_version=source.get("rule_version"))
            day_rows.append(row)
        rows.extend(day_rows)
        coverage.append({"date": day["date"], "captured_at": source.get("captured_at"),
                         "rule_version": source.get("rule_version"), "snapshot_kind": source.get("snapshot_kind"),
                         "replay": replay, "review_available": bool(reviewed), **summarize_outcomes(day_rows)})
    early = lambda row: row.get("priority_tier") == "早封连板优先"
    first = lambda row: finite_number(row.get("consecutive_limit_up_days")) == 1 or "一进二" in str(row.get("priority_tier"))
    grouped = [("早封连板", [row for row in rows if early(row)]),
               ("一进二", [row for row in rows if not early(row) and first(row)]),
               ("其他结构", [row for row in rows if not early(row) and not first(row)])]
    score_groups = []
    for name, low, high in [("延续分<55", 0, 55), ("延续分55–74", 55, 75), ("延续分≥75", 75, 101)]:
        group = [row for row in rows if (value := finite_number(row.get("continuation_score"))) is not None and low <= value < high]
        score_groups.append({"name": name, **summarize_outcomes(group)})
    return {
        "version": AUDIT_VERSION, "as_of": as_of.isoformat(), "closed_through":closed_through.isoformat(), "requested_days": limit,
        "day_count": len(selected), "dates": [day["date"] for day in selected], "coverage": coverage,
        "summary": {**summarize_outcomes(rows), "replay_count": sum(row["replay"] for row in rows),
                    "unique_stock_count": len({row["code"] for row in rows}),
                    "rule_versions": sorted({str(row.get("rule_version") or "未知") for row in rows})},
        "structure_groups": [{"name": name, **summarize_outcomes(group)} for name, group in grouped],
        "score_groups": score_groups, "rows": rows,
        "decision": "保持正式首选门槛；新增早封连板55–74分静默对照，不自动升级推荐",
        "objective": "优先检验T日封板且次日收盘较T日收盘上涨≥5%；开盘溢价、次日涨停另列，不把最高价视为卖出收益。",
        "can_calibrate_live": False, "limitation": LIMITATION,
    }
