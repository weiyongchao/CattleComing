from __future__ import annotations

from .market import DailyBar


def _period_change(closes: list[float], current_price: float, sessions: int) -> float | None:
    if len(closes) < sessions or closes[-sessions] <= 0:
        return None
    return round((current_price / closes[-sessions] - 1) * 100, 2)


def regulatory_risk(bars: list[DailyBar], current_price: float) -> dict:
    """用个股累计涨幅做公开监管阈值的保守代理，不冒充交易所偏离值认定。"""
    closes = [bar.close for bar in bars if bar.close > 0]
    changes = {
        "three_day_change": _period_change(closes, current_price, 3),
        "ten_day_change": _period_change(closes, current_price, 10),
        "thirty_day_change": _period_change(closes, current_price, 30),
    }
    three, ten, thirty = changes.values()
    ordinary_trigger = three is not None and three >= 20
    serious_trigger = (
        (ten is not None and ten >= 100)
        or (thirty is not None and thirty >= 200)
    )
    possible_trigger = ordinary_trigger or serious_trigger
    near_threshold = (
        possible_trigger
        or (three is not None and three >= 17)
        or (ten is not None and ten >= 80)
        or (thirty is not None and thirty >= 160)
    )
    if serious_trigger:
        level, label = "high", "接近严重异常波动数值区间"
    elif ordinary_trigger:
        level, label = "watch", "可能触发普通异常波动公告"
    elif near_threshold:
        level, label = "watch", "接近异动监管公开阈值"
    else:
        level, label = "normal", "未接近公开数值阈值"
    parts = []
    for name, value in (("3日", three), ("10日", ten), ("30日", thirty)):
        if value is not None:
            parts.append(f"{name}累计{value:+.2f}%")
    return {
        **changes,
        "level": level,
        "label": label,
        "ordinary_trigger": ordinary_trigger,
        "serious_trigger": serious_trigger,
        "possible_trigger": possible_trigger,
        "near_threshold": near_threshold,
        "summary": "、".join(parts) or "历史数据不足",
        "basis": "主板公开参考：3日偏离值累计20%属于普通异常波动；严重异常参考10日4次同向异常、10日100%或30日200%。",
        "note": "本地仅计算个股累计涨幅，未扣除对应指数涨幅，也不知道异常公告后的指标重置状态；只能预警，不能替代交易所认定。",
    }
