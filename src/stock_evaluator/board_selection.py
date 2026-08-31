"""盘中精选的统一出口；阈值是保守筛选规则，不是收益保证。"""
from __future__ import annotations

from datetime import datetime
from math import isfinite

from .screener import is_main_board, is_risk_stock_name
from .quote_sampling import china_time, quote_freshness

MAX_BOARD_PICKS = 5
SAMPLE_INTERVAL_SECONDS = 20
MAX_SAMPLE_GAP_SECONDS = 90


def _number(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if isfinite(result) else default
    except (TypeError, ValueError):
        return default


def intraday_selection_window(now: datetime) -> bool:
    now = china_time(now)
    minute = now.hour * 60 + now.minute
    return now.weekday() < 5 and (570 <= minute < 690 or 780 <= minute < 897)


def _quality_failure(item: dict, market_state: str, *, minimum_continuation: float = 60) -> str | None:
    event = item.get("corporate_event_risk") or {}
    funds = item.get("funds") or {}
    if market_state not in {"可观察", "谨慎"}:
        return "市场总开关未允许参与"
    if not is_main_board(str(item.get("code") or "")) or is_risk_stock_name(str(item.get("name") or "")):
        return "不符合沪深主板非ST范围"
    if _number(item.get("listed_sessions")) < 60 or not 0 < _number(item.get("float_market_cap")) < 20_000_000_000:
        return "上市时长或流通市值不符合要求"
    if (event.get("level") in {"high", "unknown"} or event.get("available") is False
            or event.get("is_restructuring") or event.get("is_merger_acquisition")):
        return "并购重组风险或公告数据未完成核验"
    if not item.get("corporate_event_checked") and event.get("available") is not True:
        return "公告核验状态缺失，仅观察"
    if item.get("risk_veto") or (item.get("regulatory_risk") or {}).get("level") not in {"normal", "watch"}:
        return "结构或异动风险未通过"
    if item.get("failed_board") or item.get("near_limit_failure") or item.get("tone") == "reject":
        return "炸板、冲板失败或盘中条件失效"
    if _number(item.get("open_score")) < 80 or _number(item.get("continuation_score")) < minimum_continuation:
        return "量价确认或历史延续性评分不足"
    if _number(item.get("amount")) < 50_000_000:
        return "实时成交额不足5000万元"
    if _number(item.get("bid_volume5")) <= 0 or _number(item.get("order_imbalance"), -1) < 0.15:
        return "五档买盘数据缺失或支撑不足"
    if item.get("sealed") and (
        _number(item.get("ask_volume5"), -1) != 0
        or _number(item.get("limit_up_price")) <= 0
        or abs(_number(item.get("price")) - _number(item.get("limit_up_price"))) >= 0.005
    ):
        return "仅触及涨停价，尚未确认无卖盘封板"
    if not funds.get("available") or _number(funds.get("main_ratio"), -99) < -1:
        return "当日资金数据缺失或流出偏大"
    return None


def _strong_open_exception(item: dict, now: datetime) -> bool:
    return (
        930 <= now.hour * 100 + now.minute <= 935
        and item.get("high_turnover_chain_matched") is True
        and (item.get("regulatory_risk") or {}).get("level") == "normal"
        and not item.get("late_final_seal_watch")
        and not item.get("opening_dip")
        and 5 <= _number(item.get("auction_gap_percent")) < 8.5
        and 5 <= _number(item.get("change_percent")) < 8.5
        and _number(item.get("price_vs_open_percent")) >= 0.5
        and _number(item.get("price_vs_auction_percent"), -99) >= 0
        and _number(item.get("open_score")) >= 90
        and _number(item.get("continuation_score")) >= 75
        and _number(item.get("order_imbalance"), -1) >= 0.35
        and _number((item.get("funds") or {}).get("main_ratio"), -99) >= 3
    )


def select_live_recommendations(
    rows: list[dict], now: datetime, market_state: str, observations: dict,
) -> list[dict]:
    """就地标记当前精选；observations由调用方按日期/扫描范围隔离并加锁。"""
    now = china_time(now)
    stamp = now.timestamp()
    present = {str(item.get("code") or "") for item in rows}
    for code in list(observations):
        if code not in present:
            observations.pop(code, None)
    qualified: dict[str, dict] = {}
    for item in rows:
        code = str(item.get("code") or "")
        item.update(recommended=False, actionable=False, execution_ready=False,
                    board_entry_allowed=False, recommendation_kind=None, recommendation_rank=None,
                    recommendation_score=None, quote_data_uncertain=False)
        failure = _quality_failure(item, market_state)
        quote_at, time_failure = quote_freshness(item, now)
        item["quote_data_uncertain"] = bool(time_failure)
        failure = failure or time_failure
        if item.get("book_available") is False:
            failure = failure or "五档数据未完整提供"
            item["quote_data_uncertain"] = True
        if not intraday_selection_window(now):
            failure = "非盘中精选时段，仅复盘观察"
        kind = "sealed" if item.get("sealed") else "strong_open" if _strong_open_exception(item, now) else None
        if not failure and kind is None:
            failure = "普通开盘转强只观察，等待实际封板"
        if failure:
            observations.pop(code, None)
            item["selection_reason"] = failure
            item["confirmation_samples"] = 0
            if item.get("tone") != "reject":
                item["tone"] = "watch"
                item["decision"] = "封板质量待确认 · 仅观察" if item.get("sealed") else "等待封板 · 仅观察"
            continue
        previous = observations.get(code)
        quote_stamp = quote_at.timestamp()
        if previous and quote_stamp < previous["quote_stamp"]:
            observations.pop(code, None)
            item.update(confirmation_samples=0, selection_reason="行情时间倒退，重新等待有效采样", tone="watch", quote_data_uncertain=True)
            continue
        if not previous or previous["kind"] != kind or not 0 <= stamp - previous["last"] <= MAX_SAMPLE_GAP_SECONDS:
            previous = {"kind": kind, "last": stamp, "quote_stamp": quote_stamp, "count": 1}
        elif quote_stamp == previous["quote_stamp"]:
            item.update(confirmation_samples=previous["count"], selection_reason="重复行情快照，不增加确认次数", tone="watch", quote_data_uncertain=True)
            continue
        elif stamp - previous["last"] >= SAMPLE_INTERVAL_SECONDS and quote_stamp - previous["quote_stamp"] >= SAMPLE_INTERVAL_SECONDS:
            previous = {**previous, "last": stamp, "quote_stamp": quote_stamp, "count": min(2, previous["count"] + 1)}
        observations[code] = previous
        item["confirmation_samples"] = previous["count"]
        item["selection_reason"] = "需至少两次间隔20秒的有效量价、盘口和资金采样"
        if previous["count"] < 2:
            item["tone"] = "watch"
            item["decision"] = "封板待二次采样" if kind == "sealed" else "强势开盘待二次采样"
            continue
        item["recommendation_kind"] = kind
        item["recommendation_score"] = round(
            _number(item.get("open_score")) * 0.55
            + _number(item.get("continuation_score")) * 0.30
            + min(10, _number(item.get("order_imbalance")) * 10)
            + (5 if kind == "sealed" else 0), 2,
        )
        qualified.setdefault(code, item)
    ranked = sorted(qualified.values(), key=lambda item: (
        item["recommendation_kind"] == "sealed", item["recommendation_score"],
        _number(item.get("amount")), str(item.get("code")),
    ), reverse=True)
    for index, item in enumerate(ranked):
        if index >= MAX_BOARD_PICKS:
            item["tone"] = "watch"
            item["decision"] = "已达确认线 · 未入前五"
            item["selection_reason"] = "当前仅保留质量排名前五，其余继续监控"
            continue
        item.update(recommended=True, actionable=True, execution_ready=True,
                    board_entry_allowed=item["recommendation_kind"] == "sealed", recommendation_rank=index + 1,
                    tone="confirm")
        item["decision"] = "封板确认精选 · 排队观察" if item["recommendation_kind"] == "sealed" else "极强开盘例外 · 小仓观察"
        item["selection_reason"] = "两次有效采样通过并进入当前前五；封单仍可撤销，排队不保证成交"
    return ranked[:MAX_BOARD_PICKS]
