from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from statistics import pstdev
import threading
import time

from .funds import _fetch_tencent_page
from .external_context import apply_external_context, external_market_context
from .market import DailyBar, EastmoneyProvider, secid_for
from .screener import is_main_board, is_risk_stock_name
from .universe import main_board_snapshots


MAX_RECOMMENDATIONS = 5
_HISTORY_CACHE: dict[str, tuple[float, list[DailyBar]]] = {}
_HISTORY_LOCK = threading.RLock()


def _number(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _history(provider: EastmoneyProvider, code: str) -> list[DailyBar]:
    with _HISTORY_LOCK:
        cached = _HISTORY_CACHE.get(code)
        if cached and time.time() - cached[0] < 900:
            return cached[1]
    bars = provider.history(code, 120)
    with _HISTORY_LOCK:
        _HISTORY_CACHE[code] = (time.time(), bars)
    return bars


def _change(current: float, previous: float) -> float:
    return (current / previous - 1) * 100 if previous > 0 else 0.0


def _max_drawdown(values: list[float]) -> float:
    peak, drawdown = 0.0, 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            drawdown = max(drawdown, (peak - value) / peak * 100)
    return drawdown


def _auction_reference(candidate: dict) -> dict:
    code = candidate["code"]
    market, normalized = secid_for(code).split(".")
    symbol = ("sh" if market == "1" else "sz") + normalized
    try:
        row = next((item for item in _fetch_tencent_page(symbol, 0) if len(item) >= 7 and item[1].startswith("09:25")), None)
        if not row:
            raise ValueError("09:25成交未返回")
        price, amount = float(row[2]), float(row[5])
        previous_close = _number(candidate.get("previous_close"))
        gap = _change(price, previous_close)
        quality = "竞价温和" if -1 <= gap <= 3 else "竞价偏强" if 3 < gap <= 6 else "竞价偏弱" if gap < -1 else "竞价过热"
        return {
            "available": True, "time": row[1], "price": round(price, 2),
            "gap_percent": round(gap, 2), "amount": amount, "quality": quality,
        }
    except Exception as exc:
        return {"available": False, "quality": "竞价参考暂不可用", "error": str(exc)}


def _daily_candidate(snapshot: dict, bars: list[DailyBar]) -> dict | None:
    code, name = str(snapshot.get("f12") or ""), str(snapshot.get("f14") or "")
    if not is_main_board(code) or is_risk_stock_name(name):
        return None
    price = _number(snapshot.get("f2"))
    market_cap = _number(snapshot.get("f20"))
    change_percent = _number(snapshot.get("f3"))
    turnover_rate = _number(snapshot.get("f8"))
    amount = _number(snapshot.get("f6"))
    completed = [bar for bar in bars if bar.trade_date < date.today() and bar.close > 0]
    if price <= 0 or market_cap < 3_000_000_000 or len(completed) < 80:
        return None
    if not (-3.0 <= change_percent <= 6.0) or turnover_rate > 15:
        return None

    closes = [bar.close for bar in completed]
    volumes = [bar.volume for bar in completed]
    series = closes + [price]
    ma5 = sum(series[-5:]) / 5
    ma10 = sum(series[-10:]) / 10
    ma20 = sum(series[-20:]) / 20
    ma60 = sum(series[-60:]) / 60
    prior_ma20 = sum(closes[-20:]) / 20
    prior_ma5 = sum(closes[-5:]) / 5
    return_5 = _change(price, closes[-5])
    return_20 = _change(price, closes[-20])
    return_60 = _change(price, closes[-60])
    price_vs_ma20 = _change(price, ma20)
    ma20_slope = _change(ma20, prior_ma20)
    daily_returns = [_change(series[index], series[index - 1]) for index in range(len(series) - 20, len(series))]
    volatility_20 = pstdev(daily_returns) if len(daily_returns) >= 2 else 99.0
    max_drawdown_20 = _max_drawdown(series[-21:])
    positive_day_ratio = sum(value > 0 for value in daily_returns) / len(daily_returns) * 100
    high_20 = max([bar.high for bar in completed[-20:]] + [price])
    high_60 = max([bar.high for bar in completed[-60:]] + [price])
    pullback_from_high = _change(price, high_20)
    drawdown_from_high_60 = _change(price, high_60)
    prior_volume_ratio = volumes[-1] / (sum(volumes[-21:-1]) / 20) if len(volumes) >= 21 and sum(volumes[-21:-1]) else 0
    current_volume_ratio = _number(snapshot.get("f10"))
    current_main_ratio = _number(snapshot.get("f184"))
    recent_limit_ups = sum(
        _change(completed[index].close, completed[index - 1].close) >= 9.5
        for index in range(max(1, len(completed) - 20), len(completed))
    )

    trend_ok = (
        price > ma5 > ma10 > ma20
        and ma20 >= ma60
        and ma20_slope > 0
        and 2 <= return_20 <= 25
        and 4 <= return_60 <= 50
    )
    stability_ok = volatility_20 <= 4.0 and max_drawdown_20 <= 12 and positive_day_ratio >= 50
    entry_ok = 0 < price_vs_ma20 <= 12 and pullback_from_high >= -9 and recent_limit_ups <= 1
    volume_ok = 0.55 <= prior_volume_ratio <= 2.5
    qualified = trend_ok and stability_ok and entry_ok and volume_ok

    rebound_score = 0.0
    rebound_score += 22 if -35 <= drawdown_from_high_60 <= -18 else 14 if -45 <= drawdown_from_high_60 < -12 else 0
    rebound_score += 14 if -30 <= return_60 <= -8 else 8 if return_60 < -5 else 0
    rebound_score += 16 if price > ma5 and ma5 > prior_ma5 else 8 if price > ma5 else 0
    rebound_score += 10 if price >= ma10 else 6 if price >= ma10 * 0.97 else 0
    rebound_score += 10 if 1 <= return_5 <= 8 else 5 if return_5 > 0 else 0
    rebound_score += 8 if 0 < change_percent <= 4 else 4 if -1 <= change_percent <= 5 else 0
    rebound_score += 8 if current_main_ratio >= 3 else 5 if current_main_ratio >= 0 else 0
    rebound_score += 7 if 1 <= current_volume_ratio <= 2.5 else 4 if 0.7 <= current_volume_ratio <= 3.5 else 0
    rebound_score += 5 if volatility_20 <= 4 else 2 if volatility_20 <= 5 else 0
    rebound_score = round(min(100, rebound_score), 1)
    rebound_qualified = (
        rebound_score >= 70 and drawdown_from_high_60 <= -12 and return_60 <= -5
        and price > ma5 and ma5 > prior_ma5 and return_5 > 0
        and change_percent > -1 and current_main_ratio > -3 and 0.7 <= current_volume_ratio <= 3.5
    )

    score = 0.0
    score += 16 if price > ma5 > ma10 > ma20 >= ma60 else 10 if price > ma20 >= ma60 else 0
    score += 10 if 5 <= return_20 <= 16 else 7 if 2 <= return_20 <= 25 else 0
    score += 10 if 10 <= return_60 <= 35 else 6 if 4 <= return_60 <= 50 else 0
    score += 8 if ma20_slope >= 1 else 5 if ma20_slope > 0 else 0
    score += 10 if volatility_20 <= 2 else 7 if volatility_20 <= 3 else 4 if volatility_20 <= 4 else 0
    score += 10 if max_drawdown_20 <= 5 else 7 if max_drawdown_20 <= 8 else 4 if max_drawdown_20 <= 12 else 0
    score += 6 if positive_day_ratio >= 60 else 4 if positive_day_ratio >= 50 else 0
    score += 8 if 0.8 <= prior_volume_ratio <= 1.6 else 5 if 0.55 <= prior_volume_ratio <= 2.5 else 0
    score += 5 if amount >= 200_000_000 else 3 if amount >= 80_000_000 else 1 if amount >= 30_000_000 else 0
    score += 4 if 0.5 <= turnover_rate <= 6 else 2 if turnover_rate <= 10 else 0
    score += 7 if 2 <= price_vs_ma20 <= 8 else 4 if 0 < price_vs_ma20 <= 12 else 0
    score += 3 if -1 <= change_percent <= 3 else 1 if -3 <= change_percent <= 6 else 0
    score += 3 if pullback_from_high >= -4 else 1 if pullback_from_high >= -9 else 0
    if return_5 > 12:
        score -= 8
    if recent_limit_ups:
        score -= 5 * recent_limit_ups
    score = round(max(0, min(100, score)), 1)
    qualified = qualified and score >= 70

    main_bonus = 12 if current_main_ratio >= 10 else 10 if current_main_ratio >= 5 else 8 if current_main_ratio >= 3 else 5 if current_main_ratio >= 1 else 0
    trend_opportunity_score = round(min(100,
        score * 0.75
        + main_bonus
        + (6 if 0.8 <= current_volume_ratio <= 1.8 else 3 if 0.7 <= current_volume_ratio <= 2.2 else 0)
        + (7 if price_vs_ma20 <= 5 else 4 if price_vs_ma20 <= 12 else 0)
        + (3 if amount >= 100_000_000 else 2 if amount >= 50_000_000 else 0)
    ), 1)
    trend_opportunity_qualified = (
        qualified and score >= 88
        and current_main_ratio >= 1
        and 0.7 <= current_volume_ratio <= 2.2
        and -1.5 <= change_percent <= 6
        and price_vs_ma20 <= 12
        and volatility_20 <= 3 and max_drawdown_20 <= 8
        and amount >= 50_000_000
    )
    rebound_opportunity_score = round(min(100,
        rebound_score * 0.55
        + main_bonus
        + (8 if 0.8 <= current_volume_ratio <= 1.8 else 4 if 0.7 <= current_volume_ratio <= 2.2 else 0)
        + (8 if -1 <= price_vs_ma20 <= 4 and return_20 <= 10 else 4 if -5 <= price_vs_ma20 <= 6 else 0)
        + (5 if volatility_20 <= 3 and max_drawdown_20 <= 8 else 2)
        + (3 if amount >= 100_000_000 else 2 if amount >= 50_000_000 else 0)
    ), 1)
    rebound_opportunity_qualified = (
        rebound_qualified and rebound_score >= 85
        and current_main_ratio >= 5
        and 0.8 <= current_volume_ratio <= 2.2
        and -0.5 <= change_percent <= 6
        and return_5 >= 0.5
        and -5 <= return_20 <= 15
        and -5 <= price_vs_ma20 <= 6
        and volatility_20 <= 3.5 and max_drawdown_20 <= 10
        and amount >= 50_000_000
    )
    opportunity_qualified = trend_opportunity_qualified or rebound_opportunity_qualified
    opportunity_score = rebound_opportunity_score if rebound_opportunity_qualified else trend_opportunity_score

    reasons = []
    if price > ma5 > ma10 > ma20 >= ma60:
        reasons.append("均线多头排列")
    if 5 <= return_20 <= 16:
        reasons.append("20日趋势温和向上")
    if volatility_20 <= 3:
        reasons.append("日波动相对较低")
    if max_drawdown_20 <= 8:
        reasons.append("阶段回撤受控")
    if 0.8 <= prior_volume_ratio <= 1.6:
        reasons.append("前日量能健康")
    if current_main_ratio >= 3:
        reasons.append("实时主力资金净流入")
    if 0.8 <= current_volume_ratio <= 1.8:
        reasons.append("实时量比处于有效区间")
    if price_vs_ma20 <= 12:
        reasons.append("价格仍在MA20风险距离内")
    risks = []
    if price_vs_ma20 > 8:
        risks.append("距离MA20偏高，等待回踩更稳妥")
    if return_5 > 8:
        risks.append("近5日上涨偏快，追高需缩小仓位并防冲高回落")
    if prior_volume_ratio > 1.8:
        risks.append("前日放量较大，需防冲高回落")

    return {
        "code": code, "name": name, "industry": str(snapshot.get("f100") or "未分类"),
        "price": round(price, 2), "change_percent": round(change_percent, 2),
        "score": score, "qualified": qualified,
        "signal": "稳健趋势关注" if score >= 82 else "趋势候选" if qualified else "不入选",
        "ma5": round(ma5, 2), "ma10": round(ma10, 2), "ma20": round(ma20, 2), "ma60": round(ma60, 2),
        "return_5_percent": round(return_5, 2), "return_20_percent": round(return_20, 2),
        "return_60_percent": round(return_60, 2), "price_vs_ma20_percent": round(price_vs_ma20, 2),
        "ma20_slope_percent": round(ma20_slope, 2), "volatility_20_percent": round(volatility_20, 2),
        "max_drawdown_20_percent": round(max_drawdown_20, 2),
        "positive_day_ratio": round(positive_day_ratio, 1), "pullback_from_high_percent": round(pullback_from_high, 2),
        "previous_volume_ratio": round(prior_volume_ratio, 2), "turnover_rate": round(turnover_rate, 2),
        "amount": amount, "market_cap": market_cap, "recent_limit_up_count": recent_limit_ups,
        "previous_close": round(_number(snapshot.get("f18")) or closes[-1], 2),
        "current_volume_ratio": round(current_volume_ratio, 2),
        "current_main_ratio": round(current_main_ratio, 2),
        "drawdown_from_high_60_percent": round(drawdown_from_high_60, 2),
        "rebound_score": rebound_score, "rebound_qualified": rebound_qualified,
        "trend_opportunity_score": trend_opportunity_score,
        "rebound_opportunity_score": rebound_opportunity_score,
        "opportunity_score": opportunity_score,
        "opportunity_qualified": opportunity_qualified,
        "trend_opportunity_qualified": trend_opportunity_qualified,
        "rebound_opportunity_qualified": rebound_opportunity_qualified,
        "opportunity_level": "精选A" if opportunity_score >= 90 else "精选B",
        "entry_price_low": round(ma10 * 0.99, 2), "entry_price_high": round(ma5 * 1.01, 2),
        "invalidation_price": round(ma20 * 0.98, 2),
        "entry_note": "强势快速拉升可关注，但宜分批介入；追高后以MA5或当日分时支撑作为失效参考",
        "reasons": reasons, "risks": risks,
    }


def screen_daily_recommendations(provider: EastmoneyProvider | None = None, limit: int = MAX_RECOMMENDATIONS) -> dict:
    provider = provider or EastmoneyProvider(timeout=6)
    snapshots = main_board_snapshots(cache_seconds=15)
    # 先做不损害稳健趋势定义的基础过滤，再逐只读取历史K线；统计口径仍覆盖全主板。
    eligible_snapshots = [
        row for row in snapshots
        if _number(row.get("f2")) > 0
        and _number(row.get("f20")) >= 3_000_000_000
        and -3 <= _number(row.get("f3")) <= 6
        and _number(row.get("f8")) <= 15
    ]
    candidates, failed = [], 0
    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = {
            executor.submit(_history, provider, str(snapshot.get("f12") or "")): snapshot
            for snapshot in eligible_snapshots
        }
        for future in as_completed(futures):
            snapshot = futures[future]
            try:
                candidate = _daily_candidate(snapshot, future.result())
                if candidate:
                    candidates.append(candidate)
            except Exception:
                failed += 1
    raw_trend = [candidate for candidate in candidates if candidate["qualified"]]
    raw_rebound = [candidate for candidate in candidates if candidate["rebound_qualified"] and not candidate["qualified"]]
    trend = [candidate for candidate in candidates if candidate["trend_opportunity_qualified"]]
    rebound = [
        candidate for candidate in candidates
        if candidate["rebound_opportunity_qualified"] and not candidate["trend_opportunity_qualified"]
    ]
    external_context = external_market_context()
    opportunity_pool = [
        {**candidate, "strategy_type": "稳健趋势", "display_score": candidate["opportunity_score"]}
        for candidate in trend
    ] + [
        {**candidate, "strategy_type": "超跌修复", "display_score": candidate["opportunity_score"]}
        for candidate in rebound
    ]
    opportunity_pool = [apply_external_context(candidate, external_context) for candidate in opportunity_pool]
    opportunity_pool.sort(key=lambda item: (
        item["opportunity_score"], item["current_main_ratio"],
        -item["volatility_20_percent"], -item["max_drawdown_20_percent"], item["amount"],
    ), reverse=True)
    selected, industry_counts = [], {}
    for candidate in opportunity_pool:
        industry = candidate["industry"]
        if industry_counts.get(industry, 0) >= 3:
            continue
        selected.append(candidate)
        industry_counts[industry] = industry_counts.get(industry, 0) + 1
        if len(selected) >= min(limit, MAX_RECOMMENDATIONS):
            break
    trend_selected = [candidate for candidate in selected if candidate["strategy_type"] == "稳健趋势"]
    rebound_selected = [candidate for candidate in selected if candidate["strategy_type"] == "超跌修复"]
    with ThreadPoolExecutor(max_workers=8) as executor:
        auction_futures = {executor.submit(_auction_reference, candidate): candidate for candidate in selected}
        for future in as_completed(auction_futures):
            auction_futures[future]["auction_reference"] = future.result()
    return {
        "candidates": selected,
        "trend_candidates": trend_selected, "rebound_candidates": rebound_selected,
        "groups": [
            {"name": "稳健趋势", "candidates": trend_selected},
            {"name": "超跌修复", "candidates": rebound_selected},
        ],
        "qualified_count": len(opportunity_pool),
        "trend_qualified_count": len(trend), "rebound_qualified_count": len(rebound),
        "raw_trend_qualified_count": len(raw_trend), "raw_rebound_qualified_count": len(raw_rebound),
        "scanned": len(snapshots),
        "deep_scanned": len(eligible_snapshots), "failed": failed,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "external_context": external_context,
        "method": "扫描全部非ST沪深主板；允许近5日强势和当日快速拉升，先用真实量价、主力资金、MA20偏离、波动与回撤建立候选，再用中国政府网近期政策和美股、日经、韩国指数做小幅行业加减分。09:25竞价仅作媒介参考；同一行业最多3只，总计最多5只，不达标不凑数。",
        "disclaimer": "每日推荐是概率筛选而非收益承诺；趋势可能随市场、公告和资金变化而中断，买入前仍需控制仓位并设置退出条件。",
    }
