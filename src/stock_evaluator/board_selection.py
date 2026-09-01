"""盘中精选的统一出口；阈值是保守筛选规则，不是收益保证。"""
from __future__ import annotations

from datetime import datetime
from math import isfinite

from .screener import is_main_board, is_risk_stock_name
from .quote_sampling import china_time, quote_freshness

MAX_BOARD_PICKS = 5
SAMPLE_INTERVAL_SECONDS = 20
MAX_SAMPLE_GAP_SECONDS = 90
RESEAL_REQUIRED_SAMPLES = 3
RESEAL_MIN_SPAN_SECONDS = 60
RESEAL_MAX_BREAK_SECONDS = 180
RESEAL_MIN_BID_RETENTION_PERCENT = 80
MIN_SEAL_AMOUNT = 30_000_000
MIN_SEAL_TO_AMOUNT_PERCENT = 5
MIN_SEAL_TO_FLOAT_CAP_PERCENT = 0.5


def _number(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _seal_profile(
    item: dict, *, attach: bool = True,
) -> tuple[float | None, float | None, float | None, float]:
    """用涨停价买一量计算封单；五档合计不能代替买一封单。"""
    if not item.get("sealed"):
        if attach:
            item.update(seal_amount=None, seal_to_amount_percent=None,
                        seal_to_float_cap_percent=None, seal_strength_score=0)
        return None, None, None, 0
    bid_price = _number(item.get("bid1_price"))
    bid_volume = _number(item.get("bid1_volume"))
    limit_price = _number(item.get("limit_up_price"))
    if bid_price <= 0 or bid_volume <= 0 or limit_price <= 0 or abs(bid_price - limit_price) >= .005:
        if attach:
            item.update(seal_amount=None, seal_to_amount_percent=None,
                        seal_to_float_cap_percent=None, seal_strength_score=0)
        return None, None, None, 0
    seal_amount = round(bid_price * bid_volume * 100, 2)
    traded_amount = _number(item.get("amount"))
    float_cap = _number(item.get("float_market_cap"))
    amount_ratio = round(seal_amount / traded_amount * 100, 2) if traded_amount > 0 else None
    float_ratio = round(seal_amount / float_cap * 100, 2) if float_cap > 0 else None
    relative_strength = max(amount_ratio or 0, (float_ratio or 0) * 10)
    strength_score = round(min(4, max(0, relative_strength - 4)), 2)
    if attach:
        item.update(seal_amount=seal_amount, seal_to_amount_percent=amount_ratio,
                    seal_to_float_cap_percent=float_ratio, seal_strength_score=strength_score)
    return seal_amount, amount_ratio, float_ratio, strength_score


def intraday_selection_window(now: datetime) -> bool:
    now = china_time(now)
    minute = now.hour * 60 + now.minute
    return now.weekday() < 5 and (570 <= minute < 690 or 780 <= minute < 897)


def _transition_quality_profile(item: dict) -> tuple[int, list[str]]:
    """连板高度只作小幅排序修正，不绕过正式质量门槛。"""
    board_height = int(_number(item.get("consecutive_limit_up_days")))
    if board_height < 2:
        return 0, []
    bonus, reasons = 0, []
    if int(_number(item.get("previous_limit_up_breaks"), 99)) == 0:
        bonus += 4
        reasons.append("前一日零炸板")
    previous_turnover = _number(item.get("previous_turnover_rate"), -1)
    if 0 <= previous_turnover <= 10:
        bonus += 2
        reasons.append("前一日换手不超过10%")
    if board_height in {3, 4}:
        bonus += 2
        reasons.append("处于三进四/四进五样本优势区间")
    return bonus, reasons


def _quality_failure(
    item: dict, market_state: str, *, now: datetime | None = None, minimum_continuation: float = 60,
) -> str | None:
    seal_amount, seal_to_amount, seal_to_float_cap, _ = _seal_profile(item, attach=False)
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
    observed_breaks = int(_number(item.get("observed_board_breaks")))
    if observed_breaks >= 2:
        return "当日已观察到至少2次炸板，反复回封不再推荐"
    if observed_breaks == 1 and not item.get("sealed"):
        return "炸板后必须实际重新封板，开板状态不升级"
    if item.get("seal_path") == "reseal":
        recovery_seconds = item.get("reseal_recovery_seconds")
        if recovery_seconds is not None and _number(recovery_seconds) > RESEAL_MAX_BREAK_SECONDS:
            return "炸板后超过3分钟才回封，修复速度不足"
        if _number(item.get("order_imbalance"), -1) < 0.35:
            return "二次回封盘口支撑不足35%"
        if not funds.get("available") or _number(funds.get("main_ratio"), -99) < 3:
            return "二次回封要求当日主力占比至少3%"
    if item.get("tradable", item.get("auction_tradable", True)) is False:
        return "一字结构尚未打开回封，实际可成交性不足"
    board_height = int(_number(item.get("consecutive_limit_up_days")))
    if board_height >= 2:
        previous_final_seal = int(_number(item.get("previous_final_seal_time")))
        if previous_final_seal <= 0:
            return "前一日最终封板时间缺失，仅观察"
        if item.get("late_final_seal_watch") or previous_final_seal >= 113000:
            return "前一日最终封板不早于11:30，仅观察"
        if item.get("previous_limit_up_breaks") is None:
            return "前一日炸板次数缺失，仅观察"
        if int(_number(item.get("previous_limit_up_breaks"))) >= 2:
            return "前一日炸板至少2次，连板稳定性不足"
        previous_turnover = _number(item.get("previous_turnover_rate"), -1)
        if previous_turnover < 0:
            return "前一日换手率缺失，仅观察"
        if previous_turnover > 20:
            return "前一日换手率超过20%，筹码分歧偏大"
        current_turnover = _number(item.get("turnover_rate"), -1)
        if current_turnover < 0:
            return "当日实际换手率缺失，仅观察"
        if current_turnover > 20:
            return "当日实际换手率超过20%，次日负反馈风险偏高"
        if now is not None and china_time(now).hour * 100 + china_time(now).minute >= 1130:
            return "11:30后不再生成新的连板买点"
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
    if item.get("sealed"):
        if seal_amount is None:
            return "涨停价买一封单数据缺失，仅观察"
        if seal_amount < MIN_SEAL_AMOUNT:
            return "买一封单金额不足3000万元，封板厚度不足"
        if ((seal_to_amount is None or seal_to_amount < MIN_SEAL_TO_AMOUNT_PERCENT)
                and (seal_to_float_cap is None or seal_to_float_cap < MIN_SEAL_TO_FLOAT_CAP_PERCENT)):
            return "买一封单占成交额不足5%且占流通市值不足0.5%，承接强度不足"
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
                    recommendation_score=None, quote_data_uncertain=False, seal_path=None,
                    seal_grade=None, reseal_recovery_seconds=None,
                    seal_bid_retention_percent=None, confirmation_span_seconds=0)
        _seal_profile(item)
        transition_bonus, transition_basis = _transition_quality_profile(item)
        item["transition_quality_bonus"] = transition_bonus
        item["transition_quality_basis"] = transition_basis
        quote_at, time_failure = quote_freshness(item, now)
        quote_stamp = quote_at.timestamp() if quote_at is not None else stamp
        item["quote_data_uncertain"] = bool(time_failure)
        previous = observations.get(code)
        observed_breaks = int(_number((previous or {}).get("observed_breaks")))
        break_signal = bool(item.get("failed_board") or item.get("near_limit_failure"))
        if item.get("opened_one_price_reseal"):
            observed_breaks = max(1, observed_breaks)
        if break_signal and (not previous or previous.get("board_state") != "broken"):
            observed_breaks += 1
        observed_breaks = min(2, observed_breaks)
        broken_at = (previous or {}).get("broken_at")
        if break_signal and (not previous or previous.get("board_state") != "broken"):
            broken_at = stamp
        item["observed_board_breaks"] = observed_breaks
        if item.get("sealed"):
            item["seal_path"] = "reseal" if observed_breaks == 1 else "repeated_reseal" if observed_breaks >= 2 else "first_seal"
            item["seal_grade"] = "B" if observed_breaks == 1 else "A" if observed_breaks == 0 else None
            if observed_breaks == 1 and broken_at is not None:
                item["reseal_recovery_seconds"] = max(0, round(stamp - _number(broken_at), 1))

        failure = _quality_failure(item, market_state, now=now) or time_failure
        if item.get("book_available") is False:
            failure = failure or "五档数据未完整提供"
            item["quote_data_uncertain"] = True
        if not intraday_selection_window(now):
            failure = "非盘中精选时段，仅复盘观察"
        kind = "sealed" if item.get("sealed") else "strong_open" if _strong_open_exception(item, now) else None
        if not failure and kind is None:
            failure = "普通开盘转强只观察，等待实际封板"
        if failure:
            if observed_breaks:
                observations[code] = {
                    "kind": None, "last": stamp, "quote_stamp": quote_stamp, "first": stamp,
                    "count": 0, "observed_breaks": observed_breaks,
                    "board_state": "broken" if break_signal else "sealed" if item.get("sealed") else "open",
                    "broken_at": broken_at, "first_bid1_volume": 0,
                }
            else:
                observations.pop(code, None)
            item["selection_reason"] = failure
            item["confirmation_samples"] = 0
            if item.get("tone") != "reject":
                item["tone"] = "watch"
                item["decision"] = "封板质量待确认 · 仅观察" if item.get("sealed") else "等待封板 · 仅观察"
            continue
        previous = observations.get(code)
        if previous and quote_stamp < previous["quote_stamp"]:
            if observed_breaks:
                observations[code] = {
                    "kind": None, "last": stamp, "quote_stamp": quote_stamp, "first": stamp,
                    "count": 0, "observed_breaks": observed_breaks,
                    "board_state": "sealed" if item.get("sealed") else "open",
                    "broken_at": broken_at, "first_bid1_volume": 0,
                }
            else:
                observations.pop(code, None)
            item.update(confirmation_samples=0, selection_reason="行情时间倒退，重新等待有效采样", tone="watch", quote_data_uncertain=True)
            continue
        if not previous or previous["kind"] != kind or not 0 <= stamp - previous["last"] <= MAX_SAMPLE_GAP_SECONDS:
            previous = {
                "kind": kind, "last": stamp, "quote_stamp": quote_stamp, "first": stamp, "count": 1,
                "observed_breaks": observed_breaks,
                "board_state": "sealed" if kind == "sealed" else "open",
                "broken_at": broken_at, "first_bid1_volume": _number(item.get("bid1_volume")),
            }
        elif quote_stamp == previous["quote_stamp"]:
            item.update(confirmation_samples=previous["count"], selection_reason="重复行情快照，不增加确认次数", tone="watch", quote_data_uncertain=True)
            continue
        elif stamp - previous["last"] >= SAMPLE_INTERVAL_SECONDS and quote_stamp - previous["quote_stamp"] >= SAMPLE_INTERVAL_SECONDS:
            required_samples = RESEAL_REQUIRED_SAMPLES if kind == "sealed" and observed_breaks == 1 else 2
            previous = {**previous, "last": stamp, "quote_stamp": quote_stamp,
                        "count": min(required_samples, previous["count"] + 1)}
        observations[code] = previous
        item["confirmation_samples"] = previous["count"]
        item["confirmation_span_seconds"] = round(max(0, stamp - _number(previous.get("first"), stamp)), 1)
        reseal = kind == "sealed" and observed_breaks == 1
        required_samples = RESEAL_REQUIRED_SAMPLES if reseal else 2
        required_span = RESEAL_MIN_SPAN_SECONDS if reseal else SAMPLE_INTERVAL_SECONDS
        if reseal:
            first_bid = _number(previous.get("first_bid1_volume"))
            current_bid = _number(item.get("bid1_volume"))
            item["seal_bid_retention_percent"] = round(current_bid / first_bid * 100, 1) if first_bid > 0 else None
            if item["seal_bid_retention_percent"] is None:
                item.update(selection_reason="二次回封买一封单数据缺失，重新等待确认", tone="watch",
                            decision="B级二次回封 · 封单待确认")
                continue
            if item["seal_bid_retention_percent"] < RESEAL_MIN_BID_RETENTION_PERCENT:
                previous.update(first=stamp, count=1, first_bid1_volume=current_bid)
                observations[code] = previous
                item.update(confirmation_samples=1, confirmation_span_seconds=0,
                            selection_reason="二次回封买一封单衰减超过20%，重新累计60秒稳定性",
                            tone="watch", decision="B级二次回封 · 封单衰减重新计时")
                continue
            item["selection_reason"] = "B级二次回封需3次有效采样、跨度至少60秒且买一封单保持不低于80%"
        else:
            item["selection_reason"] = "A级主动首封需至少两次间隔20秒的有效量价、盘口和资金采样"
        if previous["count"] < required_samples or item["confirmation_span_seconds"] < required_span:
            item["tone"] = "watch"
            item["decision"] = (
                "B级二次回封 · 60秒承接确认中" if reseal else
                "A级主动首封 · 待二次采样" if kind == "sealed" else
                "强势开盘待二次采样"
            )
            continue
        item["recommendation_kind"] = kind
        item["recommendation_score"] = round(
            _number(item.get("open_score")) * 0.55
            + _number(item.get("continuation_score")) * 0.30
            + min(10, _number(item.get("order_imbalance")) * 10)
            + (5 if kind == "sealed" else 0)
            + transition_bonus
            + _number(item.get("seal_strength_score"))
            - (6 if reseal else 0), 2,
        )
        qualified.setdefault(code, item)
    ranked = sorted(qualified.values(), key=lambda item: (
        item["recommendation_kind"] == "sealed", item.get("seal_path") != "reseal",
        item["recommendation_score"],
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
        item["decision"] = (
            "B级二次回封确认 · 排队观察" if item.get("seal_path") == "reseal" else
            "A级主动首封确认 · 排队观察" if item["recommendation_kind"] == "sealed" else
            "极强开盘例外 · 小仓观察"
        )
        item["selection_reason"] = (
            "一次炸板后完成60秒、3次采样及资金承接确认并进入前五；B级排在A级之后"
            if item.get("seal_path") == "reseal" else
            "主动首封完成两次有效采样并进入当前前五；封单仍可撤销，排队不保证成交"
        )
    return ranked[:MAX_BOARD_PICKS]
