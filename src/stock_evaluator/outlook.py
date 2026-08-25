from __future__ import annotations

from datetime import date, datetime
from statistics import fmean

from .market import DailyBar, Quote


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _return_percent(current: float, previous: float) -> float:
    return (current / previous - 1) * 100 if previous > 0 else 0.0


def _series_with_current_quote(quote: Quote, bars: list[DailyBar]) -> list[DailyBar]:
    series = list(bars)
    if series and series[-1].trade_date == date.today():
        return series
    open_price = quote.open_price or quote.previous_close or quote.price
    high_price = quote.high_price or max(open_price, quote.price)
    low_price = quote.low_price or min(open_price, quote.price)
    return [
        *series,
        DailyBar(
            date.today(), open_price, quote.price, high_price, low_price,
            quote.volume, quote.amount,
        ),
    ]


def infer_next_day_outlook(
    quote: Quote,
    bars: list[DailyBar],
    evaluation: dict,
    funds: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """用当日量价、近期趋势、盘口、板块和资金流生成可解释的次日倾向。

    返回的是规则权重，不是经过统计校准的真实概率。隔夜公告与次日竞价仍可改变结论。
    """
    now = now or datetime.now()
    metrics = evaluation["metrics"]
    book = evaluation["order_book"]
    sector = evaluation.get("sector_context") or {}
    regulatory = evaluation.get("regulatory_risk") or {}
    levels = (evaluation.get("price_plan") or {}).get("levels") or {}
    series = _series_with_current_quote(quote, bars)
    closes = [bar.close for bar in series if bar.close > 0]
    ma5 = _number(metrics.get("ma5"), quote.price)
    ma10 = fmean(closes[-10:]) if closes else quote.price
    return3 = _return_percent(quote.price, closes[-4]) if len(closes) >= 4 else 0.0
    return5 = _return_percent(quote.price, closes[-6]) if len(closes) >= 6 else 0.0
    price_vs_ma10 = _return_percent(quote.price, ma10)
    today_change = _number(quote.change_percent)
    price_vs_open = _number(metrics.get("price_vs_open_percent"))
    close_position = _number(metrics.get("intraday_position_percent"), 50.0)
    volume_ratio = _number(metrics.get("volume_ratio"))
    turnover_rate = _number(metrics.get("turnover_rate"))
    order_imbalance = _number(book.get("imbalance"))
    sector_change = _number(sector.get("average_change"))
    sector_advance = _number(sector.get("advance_ratio"), 0.5)
    day_range = max(0.0, quote.high_price - quote.low_price)
    upper_shadow_ratio = (
        max(0.0, quote.high_price - quote.price) / day_range if day_range > 0 else 0.0
    )

    trend_component = _clamp(
        _number(metrics.get("price_vs_ma5_percent")) * 1.45
        + price_vs_ma10 * 0.75 + return3 * 0.35,
        -22, 22,
    )
    close_component = _clamp(
        today_change * 1.1 + price_vs_open * 0.6 + (close_position - 50) * 0.18,
        -24, 24,
    )
    if today_change >= 9.5 and close_position >= 95:
        volume_component = 6.0 if 0.35 <= volume_ratio <= 1.5 else -4.0 if volume_ratio > 2.5 else 1.0
    else:
        direction = 1 if today_change >= 0 else -1
        volume_component = _clamp((volume_ratio - 0.8) * 7 * direction, -7, 8)
    if 1 <= turnover_rate <= 12:
        volume_component += 2
    elif turnover_rate >= 20:
        volume_component -= 7
    elif turnover_rate < 0.5:
        volume_component -= 3
    volume_component = _clamp(volume_component, -10, 10)
    book_component = _clamp(order_imbalance * 8, -8, 8)
    sector_component = _clamp(
        sector_change * 1.5 + (sector_advance - 0.5) * 10,
        -8, 8,
    )

    funds_date = str((funds or {}).get("date") or "")
    funds_current = bool(
        funds and funds.get("is_today") and funds_date == date.today().isoformat()
    )
    combined = (funds or {}).get("combined_signal") or {}
    combined_score = _number(combined.get("score"))
    main_ratio = _number((funds or {}).get("main_ratio"))
    if funds:
        raw_fund_component = combined_score * 0.20 if combined else main_ratio * 1.6
        fund_component = _clamp(raw_fund_component, -20, 20)
        if not funds_current:
            fund_component *= 0.35
    else:
        fund_component = 0.0
    price_fund_divergence = bool(
        funds and (
            (today_change >= 3 and fund_component <= -8)
            or (today_change <= -3 and fund_component >= 8)
        )
    )

    risk_component = 0.0
    overheated = bool(
        _number(metrics.get("price_vs_ma5_percent")) >= 12 and return5 >= 20
    )
    if upper_shadow_ratio >= 0.40:
        risk_component -= 7
    if overheated:
        risk_component -= 9
    if price_fund_divergence:
        risk_component -= 6 if today_change > 0 else 3
    if regulatory.get("level") == "high":
        risk_component -= 14
    elif regulatory.get("level") == "watch":
        risk_component -= 5

    direction_score = round(_clamp(
        trend_component + close_component + volume_component + book_component
        + sector_component + fund_component + risk_component,
        -100, 100,
    ), 1)
    if direction_score >= 25:
        direction_label, direction_tone = "偏涨", "positive"
    elif direction_score >= 10:
        direction_label, direction_tone = "震荡偏强", "positive"
    elif direction_score > -10:
        direction_label, direction_tone = "震荡", "neutral"
    elif direction_score > -25:
        direction_label, direction_tone = "震荡偏弱", "negative"
    else:
        direction_label, direction_tone = "偏跌", "negative"

    open_score = round(_clamp(
        close_component * 0.55 + trend_component * 0.25 + fund_component * 0.55
        + book_component * 0.25 + sector_component * 0.30,
        -60, 60,
    ), 1)
    if open_score >= 12:
        opening_label, opening_tone = "高开倾向", "positive"
    elif open_score <= -12:
        opening_label, opening_tone = "低开倾向", "negative"
    else:
        opening_label, opening_tone = "平开倾向", "neutral"

    if direction_score >= 18:
        if open_score >= 12 and (price_fund_divergence or overheated or upper_shadow_ratio >= 0.30):
            path_label, path_tone = "高开后分歧，偏冲高回落", "neutral"
        elif open_score >= 12:
            path_label, path_tone = "高开高走倾向", "positive"
        elif open_score <= -12:
            path_label, path_tone = "低开后修复走高倾向", "positive"
        else:
            path_label, path_tone = "平开震荡后偏高走", "positive"
    elif direction_score <= -18:
        if open_score >= 12:
            path_label, path_tone = "高开回落倾向", "negative"
        else:
            path_label, path_tone = "低开低走倾向", "negative"
    elif open_score >= 12:
        path_label, path_tone = "高开后震荡分化", "neutral"
    elif open_score <= -12:
        path_label, path_tone = "低开后弱修复或低走", "negative"
    else:
        path_label, path_tone = "平开震荡，等待方向选择", "neutral"

    rise_weight = int(round(_clamp(34 + direction_score * 0.32, 10, 67)))
    fall_weight = int(round(_clamp(33 - direction_score * 0.28, 10, 67)))
    range_weight = max(10, 100 - rise_weight - fall_weight)
    overflow = rise_weight + range_weight + fall_weight - 100
    if overflow > 0:
        range_weight -= overflow

    signal_signs = [
        1 if value >= 3 else -1 if value <= -3 else 0
        for value in (trend_component, close_component, volume_component, book_component, sector_component)
    ]
    if funds:
        signal_signs.append(1 if fund_component >= 3 else -1 if fund_component <= -3 else 0)
    active_signs = [value for value in signal_signs if value]
    agreement = abs(sum(active_signs)) / len(active_signs) if active_signs else 0.0
    finalized = (now.hour, now.minute) >= (15, 5)
    confidence_score = (
        43 + agreement * 18 + (8 if funds_current else 0) + (5 if len(series) >= 10 else 0)
        - (8 if price_fund_divergence else 0) - (7 if not finalized else 0)
        - (5 if regulatory.get("level") != "normal" else 0)
    )
    confidence_score = int(round(_clamp(confidence_score, 32, 78)))
    confidence_label = "较高" if confidence_score >= 68 else "中等" if confidence_score >= 52 else "较低"

    drivers: list[str] = []
    drivers.append(
        f"趋势：现价较MA5 {_number(metrics.get('price_vs_ma5_percent')):+.2f}%，近3日{return3:+.2f}%"
    )
    drivers.append(
        f"当日结构：涨跌{today_change:+.2f}%，较开盘{price_vs_open:+.2f}%，收在日内{close_position:.1f}%位置"
    )
    drivers.append(
        f"量价：量比{volume_ratio:.2f}，换手{turnover_rate:.2f}%，盘口失衡{order_imbalance * 100:+.1f}%"
    )
    if funds:
        period = "当日" if funds_current else "最近交易日"
        drivers.append(
            f"资金：{period}主力占比{main_ratio:+.2f}%，综合资金{combined_score:+.1f}（{combined.get('label') or '待确认'}）"
        )
    else:
        drivers.append("资金：当前数据源不可用，本次未把资金流作为加减分项")
    if _number(sector.get("sample_size")) > 0:
        drivers.append(
            f"板块：{sector.get('category') or '未分类'}平均{sector_change:+.2f}%，上涨家数占比{sector_advance * 100:.0f}%"
        )

    risks: list[str] = []
    if not funds:
        risks.append("资金流未确认，方向置信度已下调")
    elif not funds_current:
        risks.append("资金流不是当日数据，只按较低权重参考")
    if price_fund_divergence:
        risks.append("价格与主力资金方向背离，次日容易出现冲高回落或低开分歧")
    if overheated:
        risks.append("短期涨幅及MA5偏离较高，获利盘兑现风险上升")
    if upper_shadow_ratio >= 0.40:
        risks.append("当日上影线较长，冲高抛压尚未完全消化")
    if turnover_rate >= 20:
        risks.append("换手率过高，筹码分歧明显")
    if regulatory.get("level") != "normal":
        risks.append(f"异动监管：{regulatory.get('label') or '需核对公告'}")
    if today_change >= 9.5 and funds and fund_component < 0:
        risks.append("涨停日主动成交口径可能放大资金流出读数，需结合次日封单变化复核")
    risks.append("隔夜公告、外围市场和次日集合竞价不在当日量价模型内")

    first_support = ma5
    risk_line = _number((evaluation.get("price_plan") or {}).get("reduce", {}).get("price"), first_support)
    bullish_confirmation = max(
        quote.high_price,
        _number(levels.get("resistance5"), quote.high_price),
    )
    return {
        "stage": "收盘推断" if finalized else "盘中动态推断",
        "generated_at": now.astimezone().isoformat(timespec="seconds"),
        "direction": {"label": direction_label, "score": direction_score, "tone": direction_tone},
        "opening": {"label": opening_label, "score": open_score, "tone": opening_tone},
        "path": {"label": path_label, "tone": path_tone},
        "confidence": {"label": confidence_label, "score": confidence_score},
        "weights": {"rise": rise_weight, "range": range_weight, "fall": fall_weight},
        "fund_flow": {
            "available": bool(funds), "current": funds_current, "date": funds_date or None,
            "main_net": _number((funds or {}).get("main_net")) if funds else None,
            "main_ratio": main_ratio if funds else None,
            "combined_score": combined_score if funds else None,
            "label": combined.get("label") if funds else "资金待确认",
            "effect_score": round(fund_component, 1),
            "source": (funds or {}).get("source") if funds else None,
        },
        "key_levels": {
            "reference_close": round(quote.price, 2),
            "bullish_confirmation": round(bullish_confirmation, 2),
            "first_support": round(first_support, 2),
            "invalidation": round(risk_line, 2),
        },
        "drivers": drivers,
        "risks": risks,
        "method": "方向、开盘和盘中路径由当日K线位置、MA5/MA10、近3/5日走势、量比、换手、五档盘口、板块及最近可用主力资金共同打分。",
        "note": "上涨/震荡/下跌百分比是规则权重，不是经过历史样本校准的真实概率，也不构成收益保证。",
    }
