from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
import math

from .funds import _fetch_tencent_page, historical_fund_flow_before, historical_open_proxy
from .market import EastmoneyProvider, DailyBar, Quote, secid_for
from .screener import LEADER_GROUPS, is_main_board, is_risk_stock_name
from .universe import main_board_snapshots
from .regulatory import regulatory_risk

PREFILTER_LIMIT = 300
HISTORICAL_PREFILTER_LIMIT = 500
MAX_RECOMMENDATIONS = 6


def _number(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _prefilter_auction_universe(snapshots: list[dict], limit: int = PREFILTER_LIMIT) -> list[dict]:
    ranked = []
    for source in snapshots:
        code, name = str(source.get("f12") or ""), str(source.get("f14") or "")
        if not is_main_board(code) or is_risk_stock_name(name):
            continue
        change, amount = _number(source.get("f3")), _number(source.get("f6"))
        turnover, volume_ratio = _number(source.get("f8")), _number(source.get("f10"))
        market_cap, main_ratio = _number(source.get("f20")), _number(source.get("f184"))
        if market_cap < 3_000_000_000 or amount < 3_000_000 or not (-4 <= change <= 10.2) or turnover > 35:
            continue
        item = dict(source)
        item["prefilter_score"] = round(
            max(-4, min(change, 10)) * 2
            + min(math.log10(max(amount, 1)) - 6, 4) * 5
            + min(max(volume_ratio, 0), 3) * 3
            + min(max(turnover, 0), 12)
            + max(-10, min(main_ratio, 10)) * 1.2
            + min(math.log10(market_cap / 1_000_000_000), 2) * 3,
            2,
        )
        ranked.append(item)
    ranked.sort(key=lambda item: (item["prefilter_score"], _number(item.get("f6"))), reverse=True)
    return ranked[:limit]


def _auction_score(
    gap_percent: float,
    auction_amount: float,
    auction_volume_percent: float,
    price_vs_ma5_percent: float,
    three_day_change: float,
    previous_volume_ratio: float,
) -> tuple[int, list[str], list[str]]:
    score, reasons, risks = 0, [], []
    if 2 <= gap_percent <= 6:
        score += 25; reasons.append("竞价温和高开")
    elif 1 <= gap_percent < 2:
        score += 18; reasons.append("竞价小幅高开")
    elif 6 < gap_percent < 8.5:
        score += 16; reasons.append("竞价强势高开")
    elif 8.5 <= gap_percent < 9.7:
        score += 5; risks.append("接近涨停开盘，成交机会和炸板风险并存")
    else:
        score -= 10; risks.append("竞价缺口不在打板观察区间")

    if auction_volume_percent >= 1:
        score += 20; reasons.append("竞价量显著放大")
    elif auction_volume_percent >= 0.5:
        score += 15; reasons.append("竞价量较活跃")
    elif auction_volume_percent >= 0.2:
        score += 8
    else:
        risks.append("竞价量占近5日均量偏低")

    if auction_amount >= 50_000_000:
        score += 15; reasons.append("竞价成交额充足")
    elif auction_amount >= 10_000_000:
        score += 10
    elif auction_amount >= 3_000_000:
        score += 5
    else:
        risks.append("竞价成交额偏低")

    if 0 < price_vs_ma5_percent <= 8:
        score += 15; reasons.append("竞价价位站上MA5")
    elif price_vs_ma5_percent > 8:
        score += 5; risks.append("偏离MA5较大")
    else:
        score -= 5; risks.append("竞价价位仍在MA5下方")

    if 0 <= three_day_change <= 12:
        score += 10; reasons.append("近3日趋势强但未明显透支")
    elif -3 <= three_day_change < 0:
        score += 5
    elif three_day_change > 20:
        score -= 10; risks.append("近3日涨幅过大")

    if 0.8 <= previous_volume_ratio <= 2.5:
        score += 15; reasons.append("昨日量能健康")
    elif 0.5 <= previous_volume_ratio < 0.8:
        score += 7
    elif previous_volume_ratio > 3.5:
        risks.append("昨日极端放量，分歧风险较高")
    return max(0, min(100, score)), reasons, risks


def _completed_bars(bars: list[DailyBar], as_of: date | None = None) -> list[DailyBar]:
    completed = [bar for bar in bars if bar.trade_date < (as_of or date.today())]
    return completed or bars[:-1]


def _recent_limit_up_count(bars: list[DailyBar], sessions: int = 20) -> int:
    window = bars[-(sessions + 1):]
    return sum(
        previous.close and (current.close / previous.close - 1) * 100 >= 9.5
        for previous, current in zip(window, window[1:])
    )


def _consecutive_limit_up_days(bars: list[DailyBar]) -> int:
    streak = 0
    for previous, current in reversed(list(zip(bars, bars[1:]))):
        if not previous.close or (current.close / previous.close - 1) * 100 < 9.5:
            break
        streak += 1
    return streak


def _core_chain_score(
    consecutive_limit_ups: int, gap_percent: float, auction_volume_percent: float,
    auction_amount: float, float_market_cap: float, listed_sessions: int,
    historical_proxy: bool = False,
) -> tuple[int, bool, list[str]]:
    volume_passed = auction_volume_percent >= 45 if historical_proxy else auction_volume_percent > 1
    matched = (
        consecutive_limit_ups >= 2
        and gap_percent >= 1
        and (consecutive_limit_ups >= 3 or gap_percent >= 5)
        and volume_passed
        and auction_amount >= 10_000_000
        and 0 < float_market_cap < 20_000_000_000
        and listed_sessions >= 60
    )
    if not matched:
        return 0, False, []
    score = 84
    score += min(max(consecutive_limit_ups - 2, 0), 3) * 3
    score += 5 if 2 <= gap_percent <= 7 else 2 if gap_percent < 9.5 else 0
    score += 4 if auction_volume_percent >= (60 if historical_proxy else 3) else 2
    score += 3 if auction_amount >= 50_000_000 else 1
    reasons = [
        f"截至昨日连续{consecutive_limit_ups}个涨停",
        "竞价涨幅与连板高度共同确认",
        "竞价量额达到核心门槛",
        "流通市值低于200亿且非新股",
    ]
    return min(100, score), True, reasons


def _divergence_reversal_score(
    recent_limit_ups: int, consecutive_limit_ups: int, gap_percent: float,
    auction_volume_percent: float, auction_amount: float, previous_volume_ratio: float,
    previous_close_position: float, previous_upper_shadow: float, ten_day_change: float,
) -> tuple[int, bool, list[str], list[str]]:
    """识别前一日高换手分歧、次日竞价重新转强的反包/弱转强形态。"""
    matched = (
        recent_limit_ups >= 2 and consecutive_limit_ups == 0
        and 5 <= gap_percent <= 10.2 and auction_volume_percent >= 1
        and auction_amount >= 10_000_000 and 2 <= previous_volume_ratio <= 6
        and previous_close_position >= 65 and previous_upper_shadow <= 0.35
        and ten_day_change <= 70
    )
    if not matched:
        return 0, False, [], []
    score = 78
    score += 7 if gap_percent >= 8.5 else 4
    score += 6 if auction_volume_percent >= 5 else 3
    score += 5 if auction_amount >= 50_000_000 else 2
    score += 4 if previous_close_position >= 75 else 2
    risks = ["前日高换手分歧，弱转强失败时回撤可能较快"]
    if ten_day_change >= 55:
        risks.append("近10日涨幅较大，反包后仍有兑现压力")
    return min(100, score), True, [
        "前日放量分歧但收盘仍有承接", "次日竞价大幅超预期", "近20日具备涨停活性",
    ], risks


def _first_board_score(
    gap_percent: float, auction_volume_percent: float, auction_amount: float,
    float_market_cap: float, listed_sessions: int, price_vs_ma5: float,
    three_day_change: float, ten_day_change: float, previous_volume_ratio: float,
    previous_close_position: float, previous_upper_shadow: float,
    historical_proxy: bool = False,
) -> tuple[int, bool, list[str], list[str]]:
    """严格识别尚未连板、但竞价结构具备首板预期的中小盘股票。"""
    minimum_volume = 25 if historical_proxy else 1.2
    matched = (
        3 <= gap_percent <= 8.5
        and auction_volume_percent >= minimum_volume
        and auction_amount >= 20_000_000
        and 3_000_000_000 <= float_market_cap < 20_000_000_000
        and listed_sessions >= 60
        and 0 < price_vs_ma5 <= 12
        and -3 <= three_day_change <= 12
        and -5 <= ten_day_change <= 30
        and 0.5 <= previous_volume_ratio <= 2.8
        and previous_close_position >= 70
        and previous_upper_shadow <= 0.30
    )
    if not matched:
        return 0, False, [], []
    score = 78
    score += 5 if auction_amount >= 50_000_000 else 2
    score += 5 if auction_volume_percent >= (60 if historical_proxy else 3) else 2
    score += 4 if gap_percent >= 5 else 2
    score += 4 if previous_close_position >= 85 else 2
    score += 3 if previous_upper_shadow <= 0.15 else 1
    risks = []
    if gap_percent >= 7:
        risks.append("首板预期竞价偏高，需防高开回落")
    return min(100, score), True, [
        "中小流通市值符合首板弹性", "竞价量额达到首板确认线",
        "前日收盘承接与上影结构良好", "短期趋势未明显透支",
    ], risks


def _next_day_continuation_score(
    consecutive_limit_ups: int, recent_5_limit_ups: int, gap_percent: float,
    auction_volume_percent: float, auction_amount: float, previous_volume_ratio: float,
    float_market_cap: float, reversal_matched: bool = False,
) -> int:
    """估计T日封板后T+1继续连板的结构质量，不表示确定概率。"""
    if consecutive_limit_ups >= 4:
        score = 42
    elif consecutive_limit_ups == 3:
        score = 38
    elif consecutive_limit_ups == 2:
        score = 34
    elif consecutive_limit_ups == 1:
        score = 10
    else:
        score = 0
    score += min(recent_5_limit_ups, 3) * 3
    if 3 <= gap_percent < 8.5:
        score += 15
    elif 8.5 <= gap_percent <= 10.2:
        score += 10
    elif 1 <= gap_percent < 3:
        score += 6
    else:
        score -= 12
    if 1 <= auction_volume_percent <= 40:
        score += 12
    elif 40 < auction_volume_percent <= 100:
        score += 6
    elif auction_volume_percent > 100:
        score += -12 if consecutive_limit_ups <= 1 else -3
    else:
        score -= 8
    score += 6 if auction_amount >= 50_000_000 else 3 if auction_amount >= 20_000_000 else 0
    if 0.7 <= previous_volume_ratio <= 2.5:
        score += 8
    elif 2.5 < previous_volume_ratio <= 4:
        score += 2
    elif previous_volume_ratio > 4:
        score -= 8
    if 3_000_000_000 <= float_market_cap < 10_000_000_000:
        score += 5
    elif float_market_cap >= 20_000_000_000:
        score -= 15
    if reversal_matched and recent_5_limit_ups >= 3:
        score += 25
    return round(max(0, min(100, score)))


def _board_stage(consecutive_limit_ups: int) -> dict:
    """竞价时用昨日连续板数描述今日目标，避免把首板和一进二混为一类。"""
    previous = max(0, int(consecutive_limit_ups))
    if previous == 0:
        return {"board_stage_label": "首板候选", "previous_board_count": 0, "target_board_count": 1}
    return {
        "board_stage_label": f"{previous}进{previous + 1} · 目标{previous + 1}连板",
        "previous_board_count": previous,
        "target_board_count": previous + 1,
    }


def _big_order_support(
    previous_main_ratio: float | None, order_imbalance: float | None,
) -> dict:
    signals: list[tuple[str, float]] = []
    if previous_main_ratio is not None:
        signals.append(("前序主力", previous_main_ratio / 3))
    if order_imbalance is not None:
        signals.append(("09:20–09:25五档", order_imbalance * 4))
    if not signals:
        return {"status": "unknown", "label": "大单支撑待确认", "score": 0, "details": []}
    score = sum(value for _, value in signals)
    status = "confirmed" if score >= 0.35 else "weak" if score <= -0.35 else "neutral"
    label = {"confirmed": "大单/买盘支撑", "weak": "大单支撑偏弱", "neutral": "大单方向中性"}[status]
    return {
        "status": status, "label": label, "score": round(score, 2),
        "details": [f"{name}{value:+.2f}" for name, value in signals],
    }


def _theme_bucket(industry: str) -> str:
    groups = {
        "大科技": ("电子", "通信", "半导体", "光学", "计算机", "元件", "软件", "设备"),
        "医药": ("制药", "医疗", "中药", "生物", "医药"),
        "农业消费": ("种植", "农化", "农产品", "食品", "养殖", "饮料"),
        "电力能源": ("电力", "电网", "能源", "煤炭", "石油", "燃气"),
        "基建地产": ("建筑", "装修", "地产", "工程", "水泥"),
    }
    return next((name for name, keys in groups.items() if any(key in industry for key in keys)), industry or "未分类")


def _apply_dynamic_context(candidates: list[dict]) -> None:
    strong_by_theme: dict[str, int] = {}
    for item in candidates:
        bucket = _theme_bucket(str(item.get("industry") or ""))
        item["theme_bucket"] = bucket
        if item.get("eligible") and item.get("auction_gap_percent", 0) >= 3:
            strong_by_theme[bucket] = strong_by_theme.get(bucket, 0) + 1
    for item in candidates:
        peer_count = max(0, strong_by_theme.get(item["theme_bucket"], 0) - 1)
        theme_score = 7 if peer_count >= 3 else 4 if peer_count >= 1 else 0
        support = item.get("big_order_support") or {"status": "unknown"}
        support_adjustment = 7 if support["status"] == "confirmed" else -10 if support["status"] == "weak" else 0
        regulation_level = (item.get("regulatory_risk") or {}).get("level", "normal")
        regulation_penalty = 10 if regulation_level == "high" else 4 if regulation_level == "watch" else 0
        float_market_cap = item.get("float_market_cap", 0)
        liquidity_penalty = 18 if float_market_cap >= 20_000_000_000 else 5 if 0 < float_market_cap < 3_000_000_000 else 0
        item["theme_context"] = {
            "bucket": item["theme_bucket"], "strong_peer_count": peer_count,
            "score": theme_score,
            "label": "题材/板块竞价共振" if theme_score else "题材共振不足",
            "policy_note": "政策催化需以权威新闻或公司公告另行确认",
        }
        item["selection_score"] = round(max(0, min(
            110, item.get("score", 0) + theme_score + support_adjustment
            - regulation_penalty - liquidity_penalty,
        )))
        continuation_support = 8 if support["status"] == "confirmed" else -18 if support["status"] == "weak" else -3 if support["status"] == "unknown" else 0
        continuation_regulation_penalty = 3 if regulation_level == "high" else 2 if regulation_level == "watch" else 0
        secondary_tiers = {"一进二观察", "首板观察"}
        secondary_penalty = 8 if item.get("priority_tier") in secondary_tiers else 0
        item["continuation_score"] = round(max(0, min(
            100, item.get("continuation_base_score", 0) + theme_score
            + continuation_support - continuation_regulation_penalty - secondary_penalty,
        )))
        if item.get("priority_tier") in secondary_tiers and theme_score == 0:
            if item["continuation_score"] < 58 or support["status"] == "weak":
                item["eligible"] = False
                item["priority_tier"] = "不入选"
                item.setdefault("risks", []).append("首板/一进二缺少题材共振，且量价资金强度不足")
            else:
                item.setdefault("risks", []).append("题材共振不足，仅凭亮眼量价进入低优先级观察")


def _history_prefilter_score(bars: list[DailyBar], target_date: date) -> float | None:
    completed = _completed_bars(bars, target_date)[-25:]
    if len(completed) < 11:
        return None
    recent = completed[-5:]
    low5, high5 = min(bar.low for bar in recent), max(bar.high for bar in recent)
    close_position = (completed[-1].close - low5) / (high5 - low5) * 100 if high5 > low5 else 50
    day_range = completed[-1].high - completed[-1].low
    upper_shadow = (completed[-1].high - max(completed[-1].open, completed[-1].close)) / day_range if day_range > 0 else 0
    limit_ups = _recent_limit_up_count(completed)
    closes, volumes = [bar.close for bar in completed], [bar.volume for bar in completed]
    three_day = (closes[-1] / closes[-4] - 1) * 100 if closes[-4] else 0
    five_day = (closes[-1] / closes[-6] - 1) * 100 if closes[-6] else 0
    previous_avg = sum(volumes[-6:-1]) / 5
    volume_ratio = volumes[-1] / previous_avg if previous_avg else 0
    return round(
        min(limit_ups, 4) * 22
        + max(0, min(close_position, 100)) * 0.22
        + max(0, 1 - upper_shadow) * 12
        + max(-5, min(three_day, 35)) * 0.55
        + max(-5, min(five_day, 45)) * 0.25
        + (8 if 0.3 <= volume_ratio <= 3.5 else 0),
        2,
    )


def _multi_factor_score(
    auction_score: int, five_day_change: float, ten_day_change: float,
    previous_close_position: float, recent_limit_up_count: int,
    previous_upper_shadow: float, main_ratio: float | None, turnover_rate: float,
) -> tuple[int, list[str], list[str]]:
    score, reasons, risks = auction_score * 0.55, [], []
    if 0 <= five_day_change <= 15:
        score += 8; reasons.append("近5日趋势健康")
    elif five_day_change > 25:
        score -= 8; risks.append("近5日涨幅透支")
    if -3 <= ten_day_change <= 25:
        score += 6; reasons.append("近10日趋势未失控")
    elif ten_day_change > 35:
        score -= 8; risks.append("近10日涨幅过大")
    if previous_close_position >= 65:
        score += 8; reasons.append("前日收盘靠近区间高位")
    elif previous_close_position < 30:
        score -= 5; risks.append("前日收盘承接偏弱")
    if recent_limit_up_count >= 1:
        score += min(12, 7 + recent_limit_up_count * 2); reasons.append("近20日存在涨停活性")
    else:
        score -= 5; risks.append("近20日缺少涨停活性")
    if previous_upper_shadow <= 0.25:
        score += 5; reasons.append("前日上影压力较小")
    elif previous_upper_shadow >= 0.5:
        score -= 6; risks.append("前日长上影抛压明显")
    if main_ratio is None:
        risks.append("前序交易日主力资金暂不可用")
    elif main_ratio >= 3:
        score += 10; reasons.append("批量主力资金净流入")
    elif main_ratio >= 0:
        score += 4
    elif main_ratio <= -3:
        score -= 10; risks.append("批量主力资金明显流出")
    if 0.5 <= turnover_rate <= 10:
        score += 5; reasons.append("换手处于成熟活跃区间")
    elif turnover_rate > 15:
        score -= 5; risks.append("换手过热")
    return round(max(0, min(100, score))), reasons, risks


def _relay_pattern_score(
    gap_percent: float, auction_amount: float, auction_volume_percent: float,
    three_day_change: float, five_day_change: float, ten_day_change: float,
    previous_volume_ratio: float, previous_close_position: float,
    previous_upper_shadow: float, recent_limit_up_count: int,
) -> tuple[int, bool, list[str], list[str]]:
    """识别强趋势连板接力，分数表示形态匹配度而非涨停概率。"""
    score, reasons, risks = 0, [], []
    if recent_limit_up_count >= 3:
        score += 30; reasons.append("近20日涨停活性很强")
    elif recent_limit_up_count == 2:
        score += 25; reasons.append("近20日已有两次涨停")
    elif recent_limit_up_count == 1:
        score += 12
    if previous_close_position >= 90:
        score += 12; reasons.append("前日强势收在区间高位")
    elif previous_close_position >= 70:
        score += 7
    if previous_upper_shadow <= 0.15:
        score += 8; reasons.append("前日几乎无上影抛压")
    elif previous_upper_shadow <= 0.30:
        score += 4
    else:
        score -= 8; risks.append("前日上影抛压较重")
    if auction_volume_percent >= 10:
        score += 20; reasons.append("竞价量达到强接力级别")
    elif auction_volume_percent >= 3:
        score += 16
    elif auction_volume_percent >= 1:
        score += 12
    elif auction_volume_percent >= 0.5:
        score += 7
    if auction_amount >= 50_000_000:
        score += 7; reasons.append("竞价成交额超过5000万")
    elif auction_amount >= 10_000_000:
        score += 5
    if 2 <= gap_percent < 8.5:
        score += 8; reasons.append("竞价缺口适合强势接力观察")
    elif 8.5 <= gap_percent <= 10.2:
        score += 5; risks.append("接近涨停开盘，可能无法成交或炸板")
    if 0.6 <= previous_volume_ratio <= 2.5:
        score += 10; reasons.append("前日量能延续而非极端爆量")
    elif 0.4 <= previous_volume_ratio <= 3.5:
        score += 6
    elif previous_volume_ratio > 5:
        risks.append("前日极端爆量，次日分歧风险高")
    if 10 <= three_day_change <= 35 and 10 <= five_day_change <= 45:
        score += 5; reasons.append("处于连板加速而非弱势启动")
    if ten_day_change > 60:
        score -= 8; risks.append("近10日涨幅超过60%，接力透支")
    if auction_volume_percent > 60:
        risks.append("竞价量异常大，需防高位集中兑现")
    classic_matched = (
        recent_limit_up_count >= 2 and previous_close_position >= 85
        and previous_upper_shadow <= 0.30 and auction_volume_percent >= 1
        and auction_amount >= 10_000_000 and 2 <= gap_percent <= 10.2
    )
    acceleration_matched = (
        recent_limit_up_count >= 1 and previous_close_position >= 90
        and previous_upper_shadow <= 0.15 and auction_volume_percent >= 1
        and auction_amount >= 10_000_000 and 8.5 <= gap_percent <= 10.2
    )
    if acceleration_matched:
        score += 12
        reasons.append("前日强收后竞价接近涨停，符合强势加速")
    matched = classic_matched or acceleration_matched
    return round(max(0, min(100, score))), matched, reasons, risks


def _select_high_confidence_candidates(candidates: list[dict], limit: int = MAX_RECOMMENDATIONS) -> list[dict]:
    """只保留真正聚集在头部的候选；弱市允许为空，不为凑数固定返回。"""
    eligible = [item for item in candidates if item.get("eligible")]
    continuation_mode = any("continuation_score" in item for item in eligible)
    rank_score = lambda item: item.get("continuation_score", item.get("selection_score", item["score"]))
    tier_rank = lambda item: 2 if item.get("priority_tier") == "连板优先" else 1
    eligible.sort(key=lambda item: (rank_score(item), tier_rank(item), item["score"], item["auction_amount"]), reverse=True)
    minimum_score = 55 if continuation_mode else 80
    if not eligible or rank_score(eligible[0]) < minimum_score:
        return []
    historical_proxy = any(item.get("auction_time") == "09:31" for item in eligible)
    if historical_proxy:
        # 09:31含竞价与首分钟成交；不同形态使用不同确认线，避免固定45%漏掉强接力。
        selected = [
            item for item in eligible
            if item["score"] >= 80
            and 4 <= item["auction_gap_percent"] <= 10.2
            and item["auction_volume_percent"] >= (
                10 if item.get("strategy_mode") in {"连板接力", "强势加速", "分歧转强"} else 35
            )
            and item.get("previous_close_position_percent", 0) >= 75
            and item.get("previous_upper_shadow_ratio", 1) <= 0.35
            and item.get("previous_volume_ratio", 99) <= (
                6 if item.get("strategy_mode") == "分歧转强"
                else 4.5 if str(item.get("strategy_mode", "")).startswith("连板核心")
                else 3.55
            )
        ]
        selected.sort(
            key=lambda item: (rank_score(item), tier_rank(item), item["score"], item["auction_amount"]),
            reverse=True,
        )
        if not selected:
            return []
        top_dynamic = rank_score(selected[0])
        headroom = 25 if continuation_mode else 14
        return [item for item in selected if rank_score(item) >= top_dynamic - headroom][:limit]
    top_score = rank_score(eligible[0])
    selected = [
        item for item in eligible
        if rank_score(item) >= max(minimum_score, top_score - (20 if continuation_mode else 12))
    ][:limit]
    if not continuation_mode and len(selected) == 1 and len(eligible) > 1:
        runner_up = eligible[1]
        runner_dynamic = runner_up.get("selection_score", runner_up["score"])
        if runner_up["score"] >= 76 and top_score - runner_dynamic <= 10:
            selected.append(runner_up)
    return selected


def _auction_candidate(
    snapshot: dict, provider: EastmoneyProvider, target_date: date | None = None,
    history_bars: list[DailyBar] | None = None, preliminary: bool = False,
) -> dict | None:
    code = str(snapshot.get("f12") or "")
    industry = str(snapshot.get("f100") or "未分类")
    quote: Quote | None = None
    if target_date:
        stock_name = "".join(str(snapshot.get("f14") or code).split())
    else:
        quote = provider.quote(code)
        stock_name = quote.name
    if is_risk_stock_name(stock_name):
        return None
    market, normalized = secid_for(code).split(".")
    symbol = ("sh" if market == "1" else "sz") + normalized
    as_of = target_date or date.today()
    if target_date:
        open_proxy = historical_open_proxy(symbol, target_date)
        auction_time = open_proxy["time"]
        auction_price = float(open_proxy["price"])
        auction_volume = float(open_proxy["volume"])
        auction_amount = float(open_proxy["amount"])
        auction_data_source = str(open_proxy["source"])
    elif preliminary:
        snapshot_timestamp = int(_number(snapshot.get("f124")))
        snapshot_dt = datetime.fromtimestamp(snapshot_timestamp).astimezone() if snapshot_timestamp else datetime.now().astimezone()
        auction_time = snapshot_dt.strftime("%H:%M:%S")
        auction_price = float(quote.price or _number(snapshot.get("f2")))
        auction_volume = float(quote.volume or _number(snapshot.get("f5")))
        auction_amount = float(quote.amount or _number(snapshot.get("f6")))
        if auction_price <= 0:
            return None
        auction_data_source = "东方财富09:20不可撤单阶段参考撮合快照"
    else:
        auction_rows = [
            row for row in _fetch_tencent_page(symbol, 0)
            if len(row) >= 7 and row[1].startswith("09:25")
        ]
        if not auction_rows:
            return None
        row = auction_rows[0]
        auction_time = row[1]
        auction_price, auction_volume, auction_amount = float(row[2]), float(row[4]), float(row[5])
        auction_data_source = "腾讯当日09:25逐笔成交"
    all_completed_bars = _completed_bars(
        history_bars or provider.history(code, 160 if target_date else 80), as_of
    )
    listed_sessions = len(all_completed_bars)
    bars = all_completed_bars[-25:]
    if len(bars) < 11:
        return None
    closes = [bar.close for bar in bars]
    volumes = [bar.volume for bar in bars]
    ma5 = sum(closes[-5:]) / 5
    avg_volume5 = sum(volumes[-5:]) / 5
    earlier_volume5 = sum(volumes[-6:-1]) / 5
    previous_close = closes[-1]
    gap = (auction_price / previous_close - 1) * 100 if previous_close else 0
    auction_volume_percent = auction_volume / avg_volume5 * 100 if avg_volume5 else 0
    price_vs_ma5 = (auction_price / ma5 - 1) * 100 if ma5 else 0
    three_day_change = (closes[-1] / closes[-4] - 1) * 100 if closes[-4] else 0
    five_day_change = (closes[-1] / closes[-6] - 1) * 100 if closes[-6] else 0
    ten_day_change = (closes[-1] / closes[-11] - 1) * 100 if closes[-11] else 0
    previous_volume_ratio = volumes[-1] / earlier_volume5 if earlier_volume5 else 0
    base_score, reasons, risks = _auction_score(
        gap, auction_amount, auction_volume_percent, price_vs_ma5, three_day_change, previous_volume_ratio
    )
    recent = bars[-5:]
    low5, high5 = min(bar.low for bar in recent), max(bar.high for bar in recent)
    previous_close_position = (bars[-1].close - low5) / (high5 - low5) * 100 if high5 > low5 else 50
    previous_range = bars[-1].high - bars[-1].low
    previous_upper_shadow = (bars[-1].high - max(bars[-1].open, bars[-1].close)) / previous_range if previous_range > 0 else 0
    recent_limit_up_count = _recent_limit_up_count(bars)
    recent_5_limit_up_count = _recent_limit_up_count(all_completed_bars, 5)
    recent_10_limit_up_count = _recent_limit_up_count(all_completed_bars, 10)
    consecutive_limit_up_days = _consecutive_limit_up_days(all_completed_bars)
    try:
        decision_fund = historical_fund_flow_before(code, as_of)
        decision_main_ratio = _number(decision_fund.get("main_ratio"))
        decision_main_net = _number(decision_fund.get("main_net"))
        fund_data_date = str(decision_fund.get("date") or "")
    except Exception:
        decision_main_ratio, decision_main_net, fund_data_date = None, None, None
    auxiliary_reasons, auxiliary_risks = [], []
    score, auxiliary_reasons, auxiliary_risks = _multi_factor_score(
        base_score, five_day_change, ten_day_change, previous_close_position,
        recent_limit_up_count, previous_upper_shadow, decision_main_ratio,
        0 if target_date else _number(snapshot.get("f8")),
    )
    reasons.extend(auxiliary_reasons)
    risks.extend(auxiliary_risks)
    relay_score, relay_matched, relay_reasons, relay_risks = _relay_pattern_score(
        gap, auction_amount, auction_volume_percent, three_day_change, five_day_change,
        ten_day_change, previous_volume_ratio, previous_close_position,
        previous_upper_shadow, recent_limit_up_count,
    )
    float_market_cap = _number(snapshot.get("f21"))
    core_chain_score, core_chain_matched, core_chain_reasons = _core_chain_score(
        consecutive_limit_up_days, gap, auction_volume_percent, auction_amount,
        float_market_cap, listed_sessions, historical_proxy=bool(target_date),
    )
    reversal_score, reversal_matched, reversal_reasons, reversal_risks = _divergence_reversal_score(
        recent_limit_up_count, consecutive_limit_up_days, gap, auction_volume_percent,
        auction_amount, previous_volume_ratio, previous_close_position,
        previous_upper_shadow, ten_day_change,
    )
    first_board_score, first_board_matched, first_board_reasons, first_board_risks = _first_board_score(
        gap, auction_volume_percent, auction_amount, float_market_cap, listed_sessions,
        price_vs_ma5, three_day_change, ten_day_change, previous_volume_ratio,
        previous_close_position, previous_upper_shadow, historical_proxy=bool(target_date),
    )
    continuation_base_score = _next_day_continuation_score(
        consecutive_limit_up_days, recent_5_limit_up_count, gap, auction_volume_percent,
        auction_amount, previous_volume_ratio, float_market_cap, reversal_matched,
    )
    if first_board_matched:
        continuation_base_score = max(continuation_base_score, first_board_score - 25)
    regulation = regulatory_risk(all_completed_bars, auction_price)
    current_hhmm = datetime.now().hour * 100 + datetime.now().minute
    live_book_valid = not target_date and 920 <= current_hhmm <= 930 and quote is not None
    support = _big_order_support(
        decision_main_ratio,
        quote.order_imbalance if live_book_valid and (quote.bid_volume5 + quote.ask_volume5) > 0 else None,
    )
    acceleration = (
        recent_limit_up_count >= 1 and previous_close_position >= 90
        and previous_upper_shadow <= 0.15 and 8.5 <= gap <= 10.2
    )
    strategy_mode = (
        "分歧转强" if reversal_matched and reversal_score >= max(core_chain_score, relay_score)
        else "连板核心（历史代理）" if core_chain_matched and target_date
        else "连板核心" if core_chain_matched
        else "强势加速" if relay_matched and acceleration
        else "连板接力" if relay_matched and relay_score > score
        else "隔日启动" if first_board_matched
        else "温和启动（观察）"
    )
    if strategy_mode == "分歧转强":
        score, reasons, risks = reversal_score, reversal_reasons, reversal_risks
    elif core_chain_matched:
        score = max(core_chain_score, relay_score)
        reasons = list(dict.fromkeys(core_chain_reasons + relay_reasons))
        risks = relay_risks
    elif strategy_mode in {"连板接力", "强势加速"}:
        score, reasons, risks = relay_score, relay_reasons, relay_risks
        if decision_main_ratio is None:
            risks.append("前序交易日主力资金暂不可用")
        elif decision_main_ratio <= -3:
            risks.append("前序交易日主力资金明显流出")
    elif strategy_mode == "隔日启动":
        score, reasons, risks = first_board_score, first_board_reasons, first_board_risks
        if decision_main_ratio is None:
            risks.append("前序交易日主力资金暂不可用，需由09:20–09:25盘口确认")
        elif decision_main_ratio <= -3:
            risks.append("前序交易日主力资金明显流出")
    fresh_relay_activity = recent_5_limit_up_count >= 1 or recent_10_limit_up_count >= 2
    within_board_scale = 0 < float_market_cap < 20_000_000_000
    explicit_order_weakness = support.get("status") == "weak"
    continuation_primary = (
        within_board_scale and not explicit_order_weakness and (
            core_chain_matched
            or (reversal_matched and recent_5_limit_up_count >= 3)
            or (
                relay_matched and relay_score >= 78 and fresh_relay_activity
                and consecutive_limit_up_days >= 2
            )
        )
        and continuation_base_score >= 55
    )
    one_to_two_secondary = (
        consecutive_limit_up_days == 1 and within_board_scale and not explicit_order_weakness
        and (relay_matched or first_board_matched)
        and continuation_base_score >= 55
        and auction_amount >= 10_000_000 and auction_volume_percent >= 1
        and 2 <= gap <= 10.2 and previous_close_position >= 85
    )
    first_board_secondary = (
        consecutive_limit_up_days == 0 and
        within_board_scale and not explicit_order_weakness
        and first_board_matched and continuation_base_score >= 55
    )
    overnight_secondary = one_to_two_secondary or first_board_secondary
    eligible = continuation_primary or overnight_secondary
    priority_tier = (
        "连板优先" if continuation_primary else
        "一进二观察" if one_to_two_secondary else
        "首板观察" if first_board_secondary else "不入选"
    )
    board_stage = _board_stage(consecutive_limit_up_days)
    return {
        "code": code, "name": stock_name, "industry": industry, "category": industry,
        "evaluation_date": as_of.isoformat(),
        "score": score, "auction_base_score": base_score, "relay_score": relay_score,
        "relay_matched": relay_matched, "core_chain_matched": core_chain_matched,
        "core_chain_score": core_chain_score, "reversal_score": reversal_score,
        "reversal_matched": reversal_matched, "first_board_score": first_board_score,
        "first_board_matched": first_board_matched,
        "continuation_base_score": continuation_base_score,
        "strategy_mode": strategy_mode, "priority_tier": priority_tier, "eligible": eligible,
        **board_stage,
        "signal": "强竞价观察" if score >= 75 and eligible else "竞价关注" if eligible else "不进入打板候选",
        "auction_time": auction_time, "auction_price": auction_price,
        "auction_data_source": auction_data_source,
        "auction_gap_percent": round(gap, 2), "auction_volume": int(auction_volume),
        "auction_amount": auction_amount, "auction_volume_percent": round(auction_volume_percent, 2),
        "previous_close": round(previous_close, 2),
        "ma5": round(ma5, 2), "price_vs_ma5_percent": round(price_vs_ma5, 2),
        "three_day_change_percent": round(three_day_change, 2),
        "five_day_change_percent": round(five_day_change, 2),
        "ten_day_change_percent": round(ten_day_change, 2),
        "previous_volume_ratio": round(previous_volume_ratio, 2),
        "previous_close_position_percent": round(previous_close_position, 2),
        "previous_upper_shadow_ratio": round(previous_upper_shadow, 3),
        "recent_limit_up_count": recent_limit_up_count,
        "recent_5_limit_up_count": recent_5_limit_up_count,
        "recent_10_limit_up_count": recent_10_limit_up_count,
        "consecutive_limit_up_days": consecutive_limit_up_days,
        "previous_day_limit_up": consecutive_limit_up_days >= 1,
        "snapshot_main_net": _number(snapshot.get("f62")),
        "snapshot_main_ratio": _number(snapshot.get("f184")),
        "decision_main_net": decision_main_net,
        "decision_main_ratio": decision_main_ratio,
        "auction_order_imbalance": round(quote.order_imbalance, 4) if live_book_valid else None,
        "big_order_support": support,
        "regulatory_risk": regulation,
        "fund_data_date": fund_data_date,
        "snapshot_turnover_rate": _number(snapshot.get("f8")),
        "market_cap": _number(snapshot.get("f20")),
        "float_market_cap": float_market_cap,
        "listing_date": str(int(_number(snapshot.get("f26")))) if _number(snapshot.get("f26")) else None,
        "listed_sessions": listed_sessions,
        "is_new_stock": listed_sessions < 60,
        "snapshot_time": int(_number(snapshot.get("f124"))) or None,
        "entry_confirmation": ({
            "status": "pending", "label": "等待T日盘中封板确认",
            "requirements": ["实际触及涨停价", "封单与主动买盘不弱", "炸板后快速回封", "板块核心未同步转弱"],
            "note": "09:25仅生成次日连板预选，未完成封板确认前不得视为打板信号。",
        } if priority_tier == "连板优先" else {
            "status": "secondary",
            "label": "等待二板封板确认" if priority_tier == "一进二观察" else "等待首板封板确认",
            "requirements": ["量价和竞价额达到亮眼门槛", "资金与买盘不弱", "开盘后不跌破竞价支撑", "封板或快速回封后再确认"],
            "note": f"{board_stage['board_stage_label']}属于低于连板优先的观察层，不与2进3及以上候选等权。",
        }),
        "reasons": reasons, "risks": risks,
    }


def screen_auction_candidates(
    provider: EastmoneyProvider | None = None, limit: int = 3, preliminary: bool = False,
) -> dict:
    provider = provider or EastmoneyProvider(timeout=8)
    candidates, failed = [], 0
    try:
        snapshots = main_board_snapshots(cache_seconds=20)
        prefiltered = _prefilter_auction_universe(snapshots, limit=PREFILTER_LIMIT)
        universe_source = "东方财富延迟全主板批量快照"
    except Exception:
        snapshots = [
            {"f12": code, "f14": code, "f100": industry, "f20": 10_000_000_000}
            for stocks in LEADER_GROUPS.values() for code, industry in stocks.items() if is_main_board(code)
        ]
        prefiltered, universe_source = snapshots, "固定主板代表池（全市场源失败备用）"
    snapshot_timestamps = [int(_number(item.get("f124"))) for item in snapshots if _number(item.get("f124")) > 0]
    snapshot_dt = datetime.fromtimestamp(max(snapshot_timestamps)).astimezone() if snapshot_timestamps else None
    snapshot_time = snapshot_dt.isoformat(timespec="seconds") if snapshot_dt else None
    replay_warning = None
    if snapshot_dt and snapshot_dt.hour * 100 + snapshot_dt.minute > 930:
        replay_warning = "收盘后首次运行时，批量初筛含盘中快照，只能作为形态复盘；正式候选应在09:25后立即生成并留存。"
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {
            executor.submit(_auction_candidate, snapshot, provider, None, None, preliminary): str(snapshot.get("f12"))
            for snapshot in prefiltered
        }
        for future in as_completed(futures):
            try:
                candidate = future.result()
                if candidate:
                    candidates.append(candidate)
            except Exception:
                failed += 1
    _apply_dynamic_context(candidates)
    candidates.sort(key=lambda item: (item["eligible"], item.get("selection_score", item["score"]), item["score"], item["auction_amount"]), reverse=True)
    eligible_candidates = [item for item in candidates if item["eligible"]]
    selected = _select_high_confidence_candidates(candidates, min(limit, MAX_RECOMMENDATIONS))
    relay_qualified_count = sum(item.get("relay_matched") and item.get("relay_score", 0) >= 78 for item in candidates)
    return {
        "candidates": selected, "ranked_count": len(candidates), "qualified_count": len(eligible_candidates),
        "scanned": len(snapshots), "prefiltered": len(prefiltered), "deep_scanned": len(futures), "failed": failed,
        "universe_source": (
            f"{universe_source} + 09:20不可撤单阶段参考撮合"
            if preliminary else f"{universe_source} + 09:25最终竞价成交"
        ),
        "auction_phase": "indicative" if preliminary else "final",
        "snapshot_time": snapshot_time,
        "replay_warning": replay_warning, "relay_qualified_count": relay_qualified_count,
        "core_qualified_count": sum(item.get("core_chain_matched", False) for item in candidates),
        "reversal_qualified_count": sum(item.get("reversal_matched", False) for item in candidates),
        "first_board_qualified_count": sum(item.get("first_board_matched", False) for item in candidates),
        "continuation_primary_count": sum(item.get("eligible") and item.get("priority_tier") == "连板优先" for item in candidates),
        "one_to_two_count": sum(item.get("eligible") and item.get("priority_tier") == "一进二观察" for item in candidates),
        "first_board_watch_count": sum(item.get("eligible") and item.get("priority_tier") == "首板观察" for item in candidates),
        "overnight_secondary_count": sum(item.get("eligible") and item.get("priority_tier") in {"一进二观察", "首板观察"} for item in candidates),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "method": (
            "09:20进入不可撤单阶段后，扫描全部非ST沪深主板并用参考撮合价量生成观察池；09:25再以最终竞价成交复核。"
            if preliminary else
            "先扫描全部非ST沪深主板并初筛300只，计算T+1连板预期分；2进3及以上进入连板优先，量价亮眼的一进二和首板进入低优先级观察，并结合大单、题材与异动风险排序，最多6只。"
        ),
        "disclaimer": (
            "09:20结果是不可撤单阶段的动态观察池，09:20后仍可新增委托，价格和量能会继续变化，必须等待09:25最终复核。"
            if preliminary else
            "仅为竞价后的短线研究观察池，不代表可成交或必然涨停，不构成买入建议。打板存在炸板、大幅回撤及T+1无法当日止损风险。"
        ),
    }


def screen_historical_auction_candidates(
    target_date: date, provider: EastmoneyProvider | None = None, limit: int = 10,
) -> dict:
    if target_date >= date.today():
        raise ValueError("历史回放日期必须早于今天")
    provider = provider or EastmoneyProvider(timeout=8)
    snapshots = main_board_snapshots(cache_seconds=120, require_price=False)
    history_by_code: dict[str, list[DailyBar]] = {}
    ranked, history_failed = [], 0
    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = {
            executor.submit(provider.history, str(snapshot.get("f12")), 160): snapshot
            for snapshot in snapshots
        }
        for future in as_completed(futures):
            snapshot = futures[future]
            code = str(snapshot.get("f12") or "")
            try:
                bars = future.result()
                score = _history_prefilter_score(bars, target_date)
                if score is not None:
                    history_by_code[code] = bars
                    ranked.append((score, snapshot))
            except Exception:
                history_failed += 1
    ranked.sort(key=lambda item: item[0], reverse=True)
    prefiltered = [snapshot for _, snapshot in ranked[:HISTORICAL_PREFILTER_LIMIT]]
    candidates, auction_failed = [], 0
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {
            executor.submit(
                _auction_candidate, snapshot, provider, target_date,
                history_by_code.get(str(snapshot.get("f12") or "")),
            ): snapshot
            for snapshot in prefiltered
        }
        for future in as_completed(futures):
            try:
                candidate = future.result()
                if candidate:
                    candidates.append(candidate)
            except Exception:
                auction_failed += 1
    _apply_dynamic_context(candidates)
    candidates.sort(key=lambda item: (item["eligible"], item.get("selection_score", item["score"]), item["score"], item["auction_amount"]), reverse=True)
    eligible = [item for item in candidates if item["eligible"]]
    selected = _select_high_confidence_candidates(candidates, min(limit, MAX_RECOMMENDATIONS))
    return {
        "candidates": selected, "ranked_count": len(candidates),
        "qualified_count": len(eligible), "relay_qualified_count": sum(
            item.get("relay_matched") and item.get("relay_score", 0) >= 78 for item in candidates
        ),
        "core_qualified_count": sum(item.get("core_chain_matched", False) for item in candidates),
        "reversal_qualified_count": sum(item.get("reversal_matched", False) for item in candidates),
        "first_board_qualified_count": sum(item.get("first_board_matched", False) for item in candidates),
        "continuation_primary_count": sum(item.get("eligible") and item.get("priority_tier") == "连板优先" for item in candidates),
        "one_to_two_count": sum(item.get("eligible") and item.get("priority_tier") == "一进二观察" for item in candidates),
        "first_board_watch_count": sum(item.get("eligible") and item.get("priority_tier") == "首板观察" for item in candidates),
        "overnight_secondary_count": sum(item.get("eligible") and item.get("priority_tier") in {"一进二观察", "首板观察"} for item in candidates),
        "scanned": len(snapshots), "prefiltered": len(prefiltered),
        "deep_scanned": len(futures), "failed": history_failed + auction_failed,
        "universe_source": "全主板T-1历史K线 + 新浪目标日09:31首根一分钟线",
        "snapshot_time": None, "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "selected_date": target_date.isoformat(), "historical": True,
        "replay_warning": "历史逐笔接口不支持按日期回放，因此使用目标日09:31首根一分钟线的开盘价和累计量作为竞价代理；行业名称取当前分类，不参与评分。",
        "method": "全主板逐只读取目标日前K线并初筛500只；使用历史09:31代理量计算T+1连板预期分，2进3及以上优先，量价亮眼的一进二与首板降级观察，再结合题材、大单和异动风险排序。",
        "disclaimer": "历史结果只用于比较规则，不代表当时可成交，也不得作为当前交易信号。",
    }
