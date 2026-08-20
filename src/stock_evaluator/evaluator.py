from __future__ import annotations

from dataclasses import asdict
from datetime import date
from statistics import fmean

from .market import DailyBar, Quote
from .regulatory import regulatory_risk


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _operation_guidance(
    score: int, price_vs_ma5: float, volume_ratio: float, turnover_rate: float,
    intraday_position: float, today_change: float, yesterday_change: float,
    ma5: float, day_low: float,
) -> dict:
    chasing_risk = intraday_position >= 85 or today_change >= 5
    if score >= 70 and price_vs_ma5 > 0 and not chasing_risk:
        new_position = "可考虑分批试仓，避免一次性建满"
    elif score >= 55:
        new_position = "等待放量或回踩确认，仅适合小仓观察"
    else:
        new_position = "暂缓建仓，等待趋势重新转强"

    if score >= 70 and volume_ratio >= 0.8 and not chasing_risk:
        existing_position = "已有仓位可考虑小幅加仓，仍需保留风险余量"
    elif score >= 55:
        existing_position = "持有观察，暂不追高加仓"
    elif score >= 45:
        existing_position = "保持谨慎，停止加仓并观察支撑"
    else:
        existing_position = "控制风险，不建议加仓"

    risks: list[str] = []
    if price_vs_ma5 < 0:
        risks.append("现价低于 MA5，短线趋势尚未转强")
    if volume_ratio < 0.6:
        risks.append("量比偏低，当前价格信号缺少成交量确认")
    if turnover_rate >= 10:
        risks.append("换手率较高，短线博弈和波动风险上升")
    elif turnover_rate < 0.5:
        risks.append("换手率偏低，交投活跃度不足")
    if intraday_position >= 85:
        risks.append("现价接近日内高点，存在追高回落风险")
    elif intraday_position <= 20:
        risks.append("现价接近日内低点，日内承接偏弱")
    if today_change < 0 and yesterday_change < 0:
        risks.append("连续两个交易时段走弱，需防范下行延续")
    if not risks:
        risks.append("当前未触发突出量价风险，但仍需关注市场整体波动")

    reference = min(ma5, day_low) if day_low > 0 else ma5
    risk_level = "较高" if score < 45 or len(risks) >= 3 else "中等" if score < 70 else "一般"
    return {
        "new_position": new_position,
        "existing_position": existing_position,
        "confirmation": "放量站稳 MA5，且日内位置不处于极端高位",
        "invalidation": f"若有效跌破参考观察位 ¥{reference:.2f}，当前技术判断需要重新评估",
        "risk_level": risk_level,
        "risk_points": risks,
    }


def evaluate(quote: Quote, bars: list[DailyBar], sector_context: dict | None = None) -> dict:
    sector_context = sector_context or {
        "category": "未分类", "average_change": 0.0, "advance_ratio": 0.5, "sample_size": 0,
    }
    completed = bars[-5:]
    ma5 = fmean(bar.close for bar in completed)
    avg_volume5 = fmean(bar.volume for bar in completed)
    price_vs_ma5 = (quote.price / ma5 - 1) * 100
    volume_ratio = quote.volume / avg_volume5 if avg_volume5 else 0
    day_range = quote.high_price - quote.low_price
    intraday_position = (
        (quote.price - quote.low_price) / day_range * 100 if day_range > 0 else 50.0
    )
    price_vs_open = (
        (quote.price / quote.open_price - 1) * 100 if quote.open_price > 0 else 0.0
    )

    complete_bars = bars[:-1] if bars[-1].trade_date == date.today() else bars
    yesterday_change = 0.0
    if len(complete_bars) >= 2 and complete_bars[-2].close:
        yesterday_change = (complete_bars[-1].close / complete_bars[-2].close - 1) * 100

    trend_score = _clamp(price_vs_ma5 * 3.5, -20, 20)
    momentum_score = _clamp(quote.change_percent * 2.5, -15, 15)
    yesterday_score = _clamp(yesterday_change * 2, -10, 10)
    direction = 1 if quote.change_percent >= 0 else -1
    volume_score = _clamp((volume_ratio - 1) * 10 * direction, -10, 10)
    turnover_score = _clamp((quote.turnover_rate - 1) * 2, -5, 5)
    intraday_score = _clamp((intraday_position - 50) / 5, -10, 10)
    score = round(_clamp(
        50 + trend_score + momentum_score + yesterday_score + volume_score
        + turnover_score + intraday_score,
        0, 100,
    ))

    if score >= 70:
        rating, tone = "积极关注", "positive"
    elif score >= 55:
        rating, tone = "谨慎关注", "positive"
    elif score >= 45:
        rating, tone = "观望", "neutral"
    elif score >= 30:
        rating, tone = "谨慎回避", "negative"
    else:
        rating, tone = "回避", "negative"

    reasons = [
        f"现价{'高于' if price_vs_ma5 >= 0 else '低于'}五日均线 {abs(price_vs_ma5):.2f}%",
        f"当日涨跌幅 {quote.change_percent:+.2f}%",
        f"昨日涨跌幅 {yesterday_change:+.2f}%",
        f"当前成交量为近五日均量的 {volume_ratio:.2f} 倍",
        f"实时换手率 {quote.turnover_rate:.2f}%",
        f"现价处于今日高低区间的 {intraday_position:.1f}% 位置，相对开盘 {price_vs_open:+.2f}%",
    ]
    operation = _operation_guidance(
        score, price_vs_ma5, volume_ratio, quote.turnover_rate, intraday_position,
        quote.change_percent, yesterday_change, ma5, quote.low_price,
    )
    history_bars = complete_bars or bars
    regulatory = regulatory_risk(history_bars, quote.price)
    if regulatory["level"] != "normal":
        operation["risk_points"].append(
            f"异动监管关注：{regulatory['summary']}；需核对交易所公告和指数偏离值"
        )
        operation["risk_level"] = "较高" if regulatory["level"] == "high" else "中等"
    recent5 = history_bars[-5:]
    recent20 = history_bars[-20:]
    support5 = min(bar.low for bar in recent5)
    resistance5 = max(bar.high for bar in recent5)
    support20 = min(bar.low for bar in recent20)
    resistance20 = max(bar.high for bar in recent20)
    avg_range = fmean(bar.high - bar.low for bar in recent20)
    buffer_value = max(avg_range * 0.2, quote.price * 0.003)
    sector_weak = sector_context["average_change"] < -0.5 or sector_context["advance_ratio"] < 0.35
    can_build = (
        score >= 55 and price_vs_ma5 >= 0 and quote.order_imbalance >= -0.15
        and not sector_weak and regulatory["level"] != "high"
    )
    can_add = (
        score >= 65 and volume_ratio >= 0.8 and intraday_position < 85
        and quote.order_imbalance >= 0.1 and sector_context["average_change"] > 0
        and sector_context["advance_ratio"] >= 0.5 and regulatory["level"] == "normal"
    )
    bearish_confirmation = quote.order_imbalance <= -0.15 or sector_weak
    reduce_buffer = buffer_value * (0.6 if bearish_confirmation else 1.0)
    price_plan = {
        "levels": {
            "support5": round(support5, 2), "resistance5": round(resistance5, 2),
            "support20": round(support20, 2), "resistance20": round(resistance20, 2),
        },
        "build": {
            "enabled": can_build,
            "price_low": round(ma5 - buffer_value, 2), "price_high": round(ma5 + buffer_value, 2),
            "condition": "回踩MA5企稳，且五档卖压不强、板块未明显走弱" if can_build else "评分、趋势、盘口或板块条件未同时达标",
        },
        "add": {
            "enabled": can_add, "price": round(resistance5 + buffer_value, 2),
            "condition": "放量突破压力，五档买盘占优且板块多数上涨" if can_add else "暂不加仓；等待评分、量比、买盘和板块共振",
        },
        "reduce": {
            "enabled": True, "price": round(max(support5, ma5) - reduce_buffer, 2),
            "condition": "有效跌破且卖盘占优或板块走弱时，可考虑降低仓位",
        },
        "exit": {
            "enabled": True, "price": round(support20 - buffer_value, 2),
            "condition": "跌破20日支撑，并伴随卖盘及板块共同恶化时重新评估退出",
        },
        "note": "价格仅为历史量价触发参考，不是保证成交价；盘中短暂触及不等于条件成立。",
    }
    trend_text = "强于" if price_vs_ma5 >= 0 else "弱于"
    volume_text = "放量" if volume_ratio >= 1 else "缩量"
    intraday_text = "偏强" if intraday_position >= 65 else "偏弱" if intraday_position <= 35 else "中性"
    summary = (
        f"{quote.name}当前综合评分 {score} 分，技术面评级为“{rating}”。"
        f"现价 ¥{quote.price:.2f}，{trend_text}五日均线 {abs(price_vs_ma5):.2f}%；"
        f"今日涨跌 {quote.change_percent:+.2f}%，昨日涨跌 {yesterday_change:+.2f}%。"
        f"量比 {volume_ratio:.2f}，呈{volume_text}状态；换手率 {quote.turnover_rate:.2f}%，"
        f"日内位置 {intraday_position:.1f}%，日内表现{intraday_text}。"
        f"未持仓参考：{operation['new_position']}；已有仓位参考：{operation['existing_position']}。"
        f"当前风险等级为{operation['risk_level']}。"
        + (f"异动监管提示：{regulatory['label']}（{regulatory['summary']}）。" if regulatory["level"] != "normal" else "")
    )
    return {
        "quote": asdict(quote), "score": score, "rating": rating, "tone": tone,
        "metrics": {
            "ma5": round(ma5, 3), "price_vs_ma5_percent": round(price_vs_ma5, 2),
            "avg_volume5": round(avg_volume5), "volume_ratio": round(volume_ratio, 2),
            "yesterday_change_percent": round(yesterday_change, 2),
            "turnover_rate": round(quote.turnover_rate, 2),
            "intraday_position_percent": round(intraday_position, 1),
            "price_vs_open_percent": round(price_vs_open, 2),
        },
        "components": {
            "trend": round(trend_score, 1), "momentum": round(momentum_score, 1),
            "yesterday": round(yesterday_score, 1), "volume": round(volume_score, 1),
            "turnover": round(turnover_score, 1),
            "intraday": round(intraday_score, 1),
        },
        "reasons": reasons,
        "operation": operation,
        "regulatory_risk": regulatory,
        "price_plan": price_plan,
        "order_book": {
            "bid_volume5": quote.bid_volume5, "ask_volume5": quote.ask_volume5,
            "imbalance": round(quote.order_imbalance, 3), "spread": quote.spread,
            "bid_wall_price": quote.bid_wall_price, "ask_wall_price": quote.ask_wall_price,
            "signal": "买盘占优" if quote.order_imbalance >= 0.15 else "卖盘占优" if quote.order_imbalance <= -0.15 else "盘口均衡",
        },
        "sector_context": sector_context,
        "summary": summary,
        "history": [{"date": str(b.trade_date), "close": b.close, "volume": b.volume} for b in bars],
        "disclaimer": "本结果仅基于量价与五日均线的规则评测，不构成投资建议。",
    }
