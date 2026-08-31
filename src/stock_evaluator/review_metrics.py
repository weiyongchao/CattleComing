"""候选结果的描述性统计；收盘价作比较基准，不假设交易发生。"""
from __future__ import annotations

from math import isfinite
import json

STRONG_NEXT_CLOSE_PERCENT = 5.0


def matching_sources(left: dict, right: dict) -> bool:
    fields = ("code", "score", "reference_price", "qualified", "decision", "auction_gap_percent", "auction_amount",
              "continuation_score", "priority_tier", "consecutive_limit_up_days", "risks")
    def identity(source):
        return sorted(json.dumps({key: row.get(key) for key in fields}, sort_keys=True)
                      for row in source.get("candidates", []))
    return bool(left and right and all(left.get(key) == right.get(key) for key in ("rule_version", "snapshot_kind", "captured_at"))
                and identity(left) == identity(right))


def finite_number(value):
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = float(value)
        return number if isfinite(number) else None
    except (TypeError, ValueError):
        return None


def outcome_metrics(candidate: dict) -> dict:
    outcome = candidate.get("outcome") or {}
    next_day = outcome.get("next_day") or {}
    sealed = outcome.get("same_day_sealed")
    limit = next_day.get("limit_up")
    return {
        "t0_sealed": sealed if isinstance(sealed, bool) else None,
        "t1_open_premium": finite_number(next_day.get("open_return_percent")),
        "t1_close_premium": finite_number(next_day.get("close_return_percent")),
        "t1_limit_up": limit if isinstance(limit, bool) else None,
    }


def summarize_outcomes(rows: list[dict]) -> dict:
    values = [outcome_metrics(row) for row in rows]
    seals = [row["t0_sealed"] for row in values if row["t0_sealed"] is not None]
    opens = [row["t1_open_premium"] for row in values if row["t1_open_premium"] is not None]
    closes = [row["t1_close_premium"] for row in values if row["t1_close_premium"] is not None]
    limits = [row["t1_limit_up"] for row in values if row["t1_limit_up"] is not None]
    sealed_closes = [row["t1_close_premium"] for row in values
                     if row["t0_sealed"] is True and row["t1_close_premium"] is not None]
    rate = lambda items: round(sum(items) / len(items) * 100, 1) if items else None
    return {
        "candidate_count": len(rows), "t0_count": len(seals), "t0_sealed_count": sum(seals),
        "t0_sealed_percent": rate(seals), "t1_open_count": len(opens),
        "t1_open_positive_percent": rate([value > 0 for value in opens]),
        "t1_open_mean": round(sum(opens) / len(opens), 2) if opens else None,
        "t1_close_count": len(closes), "t1_close_positive_count": sum(value > 0 for value in closes),
        "t1_close_positive_percent": rate([value > 0 for value in closes]),
        "t1_close_mean": round(sum(closes) / len(closes), 2) if closes else None,
        "t1_limit_count": len(limits), "t1_limit_up_count": sum(limits), "t1_limit_up_percent": rate(limits),
        "strong_close_threshold": STRONG_NEXT_CLOSE_PERCENT,
        "t1_strong_close_count": sum(value >= STRONG_NEXT_CLOSE_PERCENT for value in closes),
        "t1_strong_close_percent": rate([value >= STRONG_NEXT_CLOSE_PERCENT for value in closes]),
        "sealed_t1_count": len(sealed_closes),
        "sealed_t1_strong_count": sum(value >= STRONG_NEXT_CLOSE_PERCENT for value in sealed_closes),
        "sealed_t1_strong_percent": rate([value >= STRONG_NEXT_CLOSE_PERCENT for value in sealed_closes]),
    }


def board_attribution(candidate: dict) -> tuple[str, str]:
    metrics = outcome_metrics(candidate)
    if not candidate.get("qualified"):
        return "未纳入统计", "原候选不满足观察资格"
    if metrics["t1_limit_up"] is True:
        return "次日涨停", "T+1涨停"
    close = metrics["t1_close_premium"]
    if close is None:
        return "待T+1复盘", "下一交易日收盘数据尚未齐备"
    if close >= STRONG_NEXT_CLOSE_PERCENT:
        return "次日强势", f"次日收盘较T日收盘{close:+.2f}%，达到{STRONG_NEXT_CLOSE_PERCENT:g}%观察线；不代表实得收益"
    if close > 0:
        return "次日正溢价", f"次日收盘较T日收盘{close:+.2f}%，未涨停不等于规则失败"
    if (metrics["t1_open_premium"] or 0) > 0:
        return "高开回落", f"次日有开盘溢价但收盘{close:+.2f}%，不能假设已在高点卖出"
    return ("次日负溢价" if close < 0 else "次日平收"), f"次日收盘较T日收盘{close:+.2f}%，只描述结果，不自动归因"
