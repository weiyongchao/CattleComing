from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
import math
import threading
import time

from .funds import _fetch_tencent_page, historical_fund_flow_before, historical_open_proxy
from .market import EastmoneyProvider, DailyBar, Quote, secid_for
from .screener import LEADER_GROUPS, is_main_board, is_risk_stock_name
from .universe import main_board_snapshots, previous_limit_up_pool
from .regulatory import regulatory_risk

PREFILTER_LIMIT = 300
LIVE_DEEP_LIMIT = 80
HISTORICAL_PREFILTER_LIMIT = 500
MAX_RECOMMENDATIONS = 10
_LIVE_HISTORY_CACHE: dict[str, tuple[float, list[DailyBar]]] = {}
_LIVE_HISTORY_LOCK = threading.RLock()


def _number(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _cache_live_history(code: str, bars: list[DailyBar], limit: int = 80) -> None:
    """盘前与竞价共享已完成K线，09:20重扫只更新竞价价量。"""
    if not code or len(bars) < 5:
        return
    with _LIVE_HISTORY_LOCK:
        _LIVE_HISTORY_CACHE[f"{code}:{limit}"] = (time.time(), bars[-limit:])


def _live_history(provider: EastmoneyProvider, code: str, limit: int = 80) -> list[DailyBar]:
    """竞价窗口内K线不会变化；缓存历史，20秒全市场重扫只更新实时行情。"""
    cache_key = f"{code}:{limit}"
    with _LIVE_HISTORY_LOCK:
        cached = _LIVE_HISTORY_CACHE.get(cache_key)
        if cached and time.time() - cached[0] < 900:
            return cached[1]
    bars = provider.history(code, limit)
    _cache_live_history(code, bars, limit)
    return bars


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


def _expand_live_prefilter_with_extremes(
    prefiltered: list[dict], snapshots: list[dict], limit: int = PREFILTER_LIMIT,
) -> list[dict]:
    """涨停/跌停附近竞价股必须深算，不让批量主力字段把一字板挤出Top300。"""
    base = list(prefiltered[:limit])
    seen = {str(item.get("f12") or "") for item in base}
    mandatory = []
    for snapshot in snapshots:
        code = str(snapshot.get("f12") or "")
        if code in seen or not is_main_board(code) or is_risk_stock_name(str(snapshot.get("f14") or "")):
            continue
        change = _number(snapshot.get("f3"))
        if (
            (-10.2 <= change <= -9.5 or 9.5 <= change <= 10.2)
            and _number(snapshot.get("f6")) >= 3_000_000
            and _number(snapshot.get("f20")) >= 3_000_000_000
        ):
            mandatory.append(snapshot)
    mandatory.sort(key=lambda item: (abs(_number(item.get("f3"))), _number(item.get("f6"))), reverse=True)
    return base + mandatory


def _expand_with_previous_limit_ups(
    prefiltered: list[dict], snapshots: list[dict], limit_up_pool: list[dict],
) -> list[dict]:
    """昨日涨停股无条件进入深扫，避免09:20延迟成交额把连板核心清空。"""
    result = list(prefiltered)
    by_code = {str(item.get("f12") or ""): item for item in snapshots}
    positions = {str(item.get("f12") or ""): index for index, item in enumerate(result)}
    for row in limit_up_pool:
        code, name = str(row.get("c") or ""), str(row.get("n") or "")
        if not is_main_board(code) or is_risk_stock_name(name):
            continue
        source = dict(by_code.get(code) or {})
        source.update({
            "f12": code, "f14": name or source.get("f14") or code,
            "f20": _number(source.get("f20")) or _number(row.get("tshare")),
            "f21": _number(source.get("f21")) or _number(row.get("ltsz")),
            "f100": source.get("f100") or row.get("hybk") or "未分类",
            "_force_live_quote": True,
            "_previous_limit_up_streak": int(_number(row.get("lbc"))),
            "_previous_limit_up_breaks": int(_number(row.get("zbc"))),
        })
        if code in positions:
            result[positions[code]] = source
        else:
            positions[code] = len(result)
            result.append(source)
    return result


def _auction_amount_qualification(
    auction_amount: float, continuation_primary: bool, consecutive_limit_ups: int,
    relay_score: float, support_status: str, leader_repair: bool = False,
    one_to_two: bool = False, one_price_core: bool = False,
) -> tuple[bool, str]:
    """5000万为A级；3000万为B级；高辨识度三板以上一字核心可进C级风险观察。"""
    if auction_amount > 50_000_000:
        return True, "A"
    mature_chain_watch = (
        auction_amount >= 30_000_000 and continuation_primary
        and consecutive_limit_ups >= 2 and relay_score >= 90
        and support_status != "weak"
    )
    leader_repair_watch = auction_amount >= 30_000_000 and leader_repair
    one_to_two_watch = auction_amount >= 30_000_000 and one_to_two and support_status != "weak"
    b_watch = mature_chain_watch or leader_repair_watch or one_to_two_watch
    if b_watch:
        return True, "B"
    if one_price_core and auction_amount >= 15_000_000:
        return True, "C"
    return False, "blocked"


def _nuclear_button_profile(
    *, previous_volume: float, prior_volume: float, previous_amount: float,
    auction_gap_percent: float, auction_amount: float,
    auction_turnover_percent: float, strong_characteristics: bool,
    exact_auction: bool,
) -> dict:
    """歌神小易“反核按钮”9:25竞价抄底条件；人工情绪判断不伪装成硬条件。"""
    checks = [
        {"name": "昨日成交额≥5亿", "passed": previous_amount >= 500_000_000,
         "value": f"{previous_amount / 1e8:.2f}亿" if previous_amount > 0 else "数据缺失"},
        {"name": "昨日成交量低于前日（缩量）", "passed": previous_volume > 0 and prior_volume > 0 and previous_volume < prior_volume,
         "value": f"昨日{previous_volume / 1e4:.2f}万 / 前日{prior_volume / 1e4:.2f}万" if previous_volume > 0 and prior_volume > 0 else "数据缺失"},
        {"name": "竞价成交额≥5000万", "passed": auction_amount >= 50_000_000,
         "value": f"{auction_amount / 1e8:.2f}亿"},
        {"name": "竞价涨幅≥7%", "passed": auction_gap_percent >= 7,
         "value": f"{auction_gap_percent:+.2f}%"},
        {"name": "竞价换手率≥3%", "passed": auction_turnover_percent >= 3,
         "value": f"{auction_turnover_percent:.2f}%"},
    ]
    hard_matched = all(item["passed"] for item in checks)
    matched = hard_matched and exact_auction
    score = 78
    score += min(6, int(max(0, previous_amount - 500_000_000) / 500_000_000) * 2)
    score += 5 if auction_amount >= 100_000_000 else 2 if auction_amount >= 70_000_000 else 0
    score += 5 if auction_turnover_percent >= 5 else 2 if auction_turnover_percent >= 4 else 0
    score += 4 if strong_characteristics else 0
    return {
        "matched": matched,
        "hard_matched": hard_matched,
        "score": min(95, score),
        "checks": checks,
        "reasons": [
            "昨日成交额充足且较前日缩量，今日竞价大额高开转强",
            "竞价换手达到反核按钮活跃线",
        ] + (["近10日具备涨停活性或创阶段新高"] if strong_characteristics else []),
        "risks": [
            "反核按钮属于高波动逆势战法，高开可能是兑现而非转强",
            "9:25条件命中后仍需人工确认市场情绪处于冰点、题材辨识度和主动买盘",
            "盘后竞价代理数据会失真，不使用该公式直接回测验证",
        ],
    }


def _recognition_channel_profile(
    *, consecutive_limit_ups: int, recent_5_limit_ups: int, recent_10_limit_ups: int,
    reversal_matched: bool, reversal_score: float, float_market_cap: float,
    gap_percent: float, auction_amount: float, auction_volume_percent: float,
    auction_turnover_percent: float, previous_volume_ratio: float,
    previous_close_position: float, previous_upper_shadow: float,
    ten_day_change: float, historical_proxy: bool,
) -> dict:
    """补充高辨识度容量、龙头反包和竞价抢筹形态，不绑定任何股票名称。"""
    volume_line = 8 if historical_proxy else 1
    strong_volume_line = 30 if historical_proxy else 3
    capacity_relay = (
        consecutive_limit_ups >= 1 and recent_10_limit_ups >= 4
        and 20_000_000_000 <= float_market_cap < 35_000_000_000
        and 5 <= gap_percent <= 10.2 and auction_amount >= 50_000_000
        and auction_volume_percent >= volume_line
        and previous_close_position >= 95 and previous_upper_shadow <= 0.10
        and ten_day_change <= 70
    )
    leader_reversal = (
        consecutive_limit_ups == 0 and recent_5_limit_ups >= 3 and recent_10_limit_ups >= 4
        and reversal_matched and reversal_score >= 90
        and 0 < float_market_cap < 20_000_000_000
        and 8.5 <= gap_percent <= 10.2 and auction_amount >= 50_000_000
        and auction_volume_percent >= strong_volume_line
        and previous_close_position >= 70 and previous_upper_shadow <= 0.35
        and previous_volume_ratio <= 6 and ten_day_change <= 80
    )
    auction_grab = (
        consecutive_limit_ups == 0 and recent_10_limit_ups >= 1
        and 0 < float_market_cap < 20_000_000_000
        and 9.5 <= gap_percent <= 10.2 and auction_amount >= 50_000_000
        and auction_volume_percent >= strong_volume_line and auction_turnover_percent >= 1
        and previous_close_position >= 70 and previous_upper_shadow <= 0.25
        and 0.8 <= previous_volume_ratio <= 4 and -5 <= ten_day_change <= 35
    )
    return {
        "capacity_relay": capacity_relay,
        "leader_reversal": leader_reversal,
        "auction_grab": auction_grab,
    }


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


def _one_to_two_score(
    gap_percent: float, auction_volume_percent: float, auction_amount: float,
    float_market_cap: float, listed_sessions: int, price_vs_ma5: float,
    three_day_change: float, ten_day_change: float, previous_volume_ratio: float,
    previous_close_position: float, previous_upper_shadow: float,
    historical_proxy: bool = False,
) -> tuple[int, bool, list[str], list[str]]:
    """一进二独立模型：允许首板后的短期过热，但要求竞价量价和前日封板结构。"""
    minimum_volume = 25 if historical_proxy else 1
    matched = (
        3 <= gap_percent <= 10.2
        and auction_volume_percent >= minimum_volume
        and auction_amount >= 30_000_000
        and 3_000_000_000 <= float_market_cap < 20_000_000_000
        and listed_sessions >= 60
        and 0 < price_vs_ma5 <= 20
        and -3 <= three_day_change <= 25
        and -10 <= ten_day_change <= 50
        and 0.4 <= previous_volume_ratio <= 4
        and previous_close_position >= 85
        and previous_upper_shadow <= 0.30
    )
    if not matched:
        return 0, False, [], []
    score = 70
    score += 7 if auction_amount >= 50_000_000 else 3
    score += 8 if auction_volume_percent >= (60 if historical_proxy else 10) else 5 if auction_volume_percent >= (35 if historical_proxy else 3) else 2
    score += 6 if 3 <= gap_percent < 8.5 else 2
    score += 5 if float_market_cap < 10_000_000_000 else 2
    score += 4 if previous_close_position >= 95 else 2
    score += 3 if previous_upper_shadow <= 0.10 else 1
    score += 4 if previous_volume_ratio <= 2.8 else 1
    risks = []
    if gap_percent >= 9.5:
        risks.append("一进二接近一字涨停，可能排不到；成交时需防封单松动")
    if price_vs_ma5 > 15:
        risks.append("首板后偏离MA5较大，只进入低优先级观察")
    if previous_volume_ratio > 2.8:
        risks.append("首板放量较大，二板分歧风险偏高")
    return min(100, score), True, [
        "昨日首板结构完整", "竞价量额达到一进二观察线",
        "前日强收且上影压力较小", "流通市值具备二板弹性",
    ], risks


def _capacity_one_to_two_score(
    consecutive_limit_ups: int, recent_5_limit_ups: int, gap_percent: float,
    auction_volume_percent: float, auction_amount: float, float_market_cap: float,
    previous_close_position: float, previous_upper_shadow: float, ten_day_change: float,
    historical_proxy: bool = False,
) -> tuple[int, bool, list[str], list[str]]:
    """200亿–500亿容量首板只做板块共振观察，最终资格由动态题材上下文确认。"""
    minimum_volume = 25 if historical_proxy else 5
    matched = (
        consecutive_limit_ups == 1 and recent_5_limit_ups >= 1
        and 20_000_000_000 <= float_market_cap < 50_000_000_000
        and 5 <= gap_percent <= 10.2 and auction_amount >= 100_000_000
        and auction_volume_percent >= minimum_volume
        and previous_close_position >= 95 and previous_upper_shadow <= 0.10
        and ten_day_change <= 50
    )
    if not matched:
        return 0, False, [], []
    score = 82
    score += 4 if auction_amount >= 150_000_000 else 2
    score += 4 if auction_volume_percent >= (50 if historical_proxy else 10) else 2
    score += 4 if float_market_cap < 35_000_000_000 else 0
    return min(96, score), True, [
        "容量首板竞价额过亿", "前日封板结构完整",
        "竞价高开或涨停撮合与量能达到容量二板线", "必须等待同主题竞价共振确认",
    ], ["流通市值超过200亿，只进入容量一进二观察层"]


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


def _execution_risk_profile(
    consecutive_limit_ups: int, gap_percent: float, price_vs_ma5: float,
    previous_volume_ratio: float, previous_open_gap_percent: float,
    regulation_level: str, fund_status: str,
) -> dict:
    """把形态强度与可成交性、次日生存风险分开，避免高位一字板获得可执行高分。"""
    reasons: list[str] = []
    tradable = gap_percent < 9.5
    if not tradable:
        reasons.append("竞价接近一字涨停，正常情况下难以成交；若能成交需先视为封板松动")
    limit_down_auction = gap_percent <= -9.5
    if limit_down_auction:
        reasons.append("跌停附近竞价，只可作为反核观察；开盘承接失败可能继续封跌停")
    # 2进3、3进4本身不是监管或透支否决条件；仅对更高板数叠加显著
    # MA5偏离做硬风险控制，避免把正常三连板预期提前剔除。
    high_exhaustion = consecutive_limit_ups >= 4 and price_vs_ma5 > 25
    if high_exhaustion:
        reasons.append(f"连续{consecutive_limit_ups}板且竞价价偏离MA5 {price_vs_ma5:.1f}%，高位透支")
    shrinking_acceleration = consecutive_limit_ups >= 2 and previous_volume_ratio < 0.85
    if shrinking_acceleration:
        reasons.append("连续板后前日缩量加速，次日一旦分歧可能缺少退出流动性")
    expectation_break = (
        consecutive_limit_ups >= 3
        and previous_open_gap_percent >= 8.5
        and gap_percent < 5
    )
    if expectation_break:
        reasons.append(
            f"前日开盘强度{previous_open_gap_percent:+.1f}%，今日竞价降至{gap_percent:+.1f}%，高位连板预期明显衰减"
        )
    regulatory_veto = consecutive_limit_ups >= 2 and regulation_level == "high"
    if regulatory_veto:
        reasons.append("高位连板已进入异动监管高风险区")
    downside = 30 if not tradable else 12 if gap_percent >= 8.5 else 0
    downside += 35 if limit_down_auction else 0
    downside += 30 if high_exhaustion else 0
    downside += 15 if shrinking_acceleration else 0
    downside += 25 if expectation_break else 0
    downside += 18 if regulatory_veto else 8 if regulation_level == "watch" else 0
    downside += 10 if fund_status == "unknown" and consecutive_limit_ups >= 2 else 15 if fund_status == "weak" else 0
    return {
        "tradable": tradable,
        "tradability_label": (
            "跌停竞价 · 等待打开跌停并确认主动买盘，否则不跟随"
            if limit_down_auction else
            "可等待开盘确认" if tradable else
            "一字板/近涨停 · 可挂单打板，但可能排不到；成交后需防开板"
        ),
        "high_exhaustion": high_exhaustion,
        "shrinking_acceleration": shrinking_acceleration,
        # 四板及以上偏离MA5只做风险降级；只有严重监管风险或明显的竞价预期破坏才硬否决。
        "risk_veto": regulatory_veto or expectation_break,
        "expectation_break": expectation_break,
        "limit_down_auction": limit_down_auction,
        "t1_downside_risk_score": min(100, downside),
        "risk_reasons": reasons,
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
        "资源金属": ("金属", "贵金属", "有色", "白银", "黄金", "铜", "铝"),
    }
    return next((name for name, keys in groups.items() if any(key in industry for key in keys)), industry or "未分类")


def _apply_dynamic_context(candidates: list[dict]) -> None:
    strong_by_theme: dict[str, int] = {}
    for item in candidates:
        bucket = _theme_bucket(str(item.get("industry") or ""))
        item["theme_bucket"] = bucket
        # 板块共振必须先于最终个股资格判断。否则两只容量核心都会先被
        # 200亿门槛挡掉，随后又因为彼此“不合格”而永远无法形成共振。
        theme_strength = (
            item.get("auction_gap_percent", 0) >= 3
            and item.get("auction_amount", 0) >= 30_000_000
            and item.get("auction_volume_percent", 0) >= 1
            and (
                item.get("eligible")
                or item.get("consecutive_limit_up_days", 0) >= 1
                or item.get("one_to_two_matched")
                or item.get("capacity_one_to_two_matched")
            )
        )
        if theme_strength:
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
        secondary_tiers = {"一进二观察", "容量一进二观察", "首板观察"}
        secondary_penalty = 8 if item.get("priority_tier") in secondary_tiers else 0
        downside_penalty = _number(item.get("t1_downside_risk_score")) * 0.45
        item["continuation_score"] = round(max(0, min(
            100, item.get("continuation_base_score", 0) + theme_score
            + continuation_support - continuation_regulation_penalty - secondary_penalty
            - downside_penalty,
        )))
        if item.get("risk_veto"):
            item["eligible"] = False
            item["priority_tier"] = "高位风险剔除"
        if item.get("priority_tier") == "容量一进二观察" and theme_score == 0:
            item["eligible"] = False
            item["priority_tier"] = "不入选"
            item.setdefault("risks", []).append("容量一进二缺少同主题竞价共振")
        elif item.get("priority_tier") in secondary_tiers and theme_score == 0:
            bright_one_to_two = (
                item.get("one_to_two_matched") and item.get("score", 0) >= 88
                and item.get("auction_amount", 0) >= 30_000_000
                and item.get("auction_volume_percent", 0) >= 5
                and support["status"] != "weak"
            )
            if (item["continuation_score"] < 58 and not bright_one_to_two) or support["status"] == "weak":
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
    liquidity_rank = lambda item: 2 if item.get("auction_liquidity_tier") == "A" else 1 if item.get("auction_liquidity_tier") == "B" else 0
    one_price_boards = [
        item for item in eligible
        if item.get("tradable") is False
        and item.get("priority_tier") in {"连板优先", "一进二观察"}
        and 9.5 <= item.get("auction_gap_percent", 0) <= 10.2
        and item.get("score", 0) >= 80
        and not item.get("risk_veto", False)
    ]
    limit_down_reversals = [
        item for item in eligible
        if item.get("priority_tier") == "跌停反核观察"
        and -10.2 <= item.get("auction_gap_percent", 0) <= -9.5
        and item.get("score", 0) >= 70
        and not item.get("risk_veto", False)
    ]
    leader_repairs = [
        item for item in eligible
        if item.get("leader_repair_matched")
        and item.get("score", 0) >= 80
        and not item.get("risk_veto", False)
    ]
    nuclear_buttons = [
        item for item in eligible
        if item.get("nuclear_button_matched")
        and item.get("score", 0) >= 78
        and not item.get("risk_veto", False)
    ]
    recognition_specials = [
        item for item in eligible
        if (
            item.get("capacity_relay_matched")
            or item.get("leader_reversal_matched")
            or item.get("auction_grab_matched")
        )
        and item.get("score", 0) >= 84
        and not item.get("risk_veto", False)
    ]
    layered_watches = [
        item for item in eligible
        if (
            item.get("priority_tier") == "容量一进二观察"
            or (
                item.get("priority_tier") == "一进二观察"
                and item.get("auction_liquidity_tier") == "B"
                and item.get("score", 0) >= 82
                and item.get("auction_volume_percent", 0) >= 5
            )
        )
        and not item.get("risk_veto", False)
    ]

    def include_special_candidates(selected: list[dict]) -> list[dict]:
        """一字板和跌停反核都保留，但统一排在普通可执行候选之后。"""
        one_price_reserved = sorted(
            one_price_boards,
            key=lambda item: (
                item.get("selection_score", item.get("score", 0)),
                item.get("core_chain_score", 0), item.get("score", 0), item.get("auction_amount", 0),
            ),
            reverse=True,
        )[:limit]
        reversal_reserved = sorted(
            limit_down_reversals,
            key=lambda item: (
                item.get("score", 0), item.get("auction_amount", 0),
                item.get("auction_volume_percent", 0),
            ),
            reverse=True,
        )[:limit]
        repair_reserved = sorted(
            leader_repairs,
            key=lambda item: (
                item.get("score", 0), item.get("auction_amount", 0),
                item.get("auction_volume_percent", 0),
            ),
            reverse=True,
        )[:limit]
        nuclear_reserved = sorted(
            nuclear_buttons,
            key=lambda item: (
                item.get("score", 0), item.get("auction_turnover_percent", 0),
                item.get("auction_amount", 0),
            ),
            reverse=True,
        )[:limit]
        recognition_reserved = sorted(
            recognition_specials,
            key=lambda item: (
                3 if item.get("leader_reversal_matched") else 2 if item.get("capacity_relay_matched") else 1,
                item.get("continuation_base_score", 0), item.get("score", 0), item.get("auction_amount", 0),
            ),
            reverse=True,
        )[:limit]
        layered_reserved = sorted(
            layered_watches,
            key=lambda item: (
                item.get("priority_tier") == "容量一进二观察",
                item.get("continuation_score", 0), item.get("score", 0),
                item.get("auction_amount", 0),
            ),
            reverse=True,
        )[:limit]
        other_reserved = one_price_reserved + reversal_reserved + repair_reserved + nuclear_reserved + layered_reserved
        all_special_codes = {
            item.get("code") for item in recognition_specials + one_price_boards
            + limit_down_reversals + leader_repairs + nuclear_buttons + layered_watches
        }
        ordinary_pool = [item for item in selected if item.get("code") not in all_special_codes]
        reserve_cap = max(0, limit - (1 if ordinary_pool else 0))
        reserved_by_code = {}
        for item in recognition_reserved + other_reserved:
            reserved_by_code.setdefault(item.get("code"), item)
        reserved = list(reserved_by_code.values())[:reserve_cap]
        ordinary = [item for item in selected if item.get("code") not in all_special_codes]
        return (ordinary[:max(0, limit - len(reserved))] + reserved)[:limit]

    eligible.sort(key=lambda item: (item.get("tradable", True), liquidity_rank(item), tier_rank(item), rank_score(item), item["score"], item["auction_amount"]), reverse=True)
    minimum_score = 55 if continuation_mode else 80
    if not eligible:
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
            key=lambda item: (item.get("tradable", True), liquidity_rank(item), tier_rank(item), rank_score(item), item["score"], item["auction_amount"]),
            reverse=True,
        )
        if selected:
            top_dynamic = rank_score(selected[0])
            headroom = 25 if continuation_mode else 14
            selected = [item for item in selected if rank_score(item) >= top_dynamic - headroom]
        return include_special_candidates(selected)
    standard_eligible = [item for item in eligible if rank_score(item) >= minimum_score]
    if not standard_eligible:
        return include_special_candidates([])
    top_score = rank_score(standard_eligible[0])
    selected = [
        item for item in standard_eligible
        if rank_score(item) >= max(minimum_score, top_score - (20 if continuation_mode else 12))
    ][:limit]
    if not continuation_mode and len(selected) == 1 and len(standard_eligible) > 1:
        runner_up = standard_eligible[1]
        runner_dynamic = runner_up.get("selection_score", runner_up["score"])
        if runner_up["score"] >= 76 and top_score - runner_dynamic <= 10:
            selected.append(runner_up)
    return include_special_candidates(selected)


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
        stock_name = "".join(str(snapshot.get("f14") or code).split())
        if preliminary:
            if snapshot.get("_force_live_quote"):
                quote = provider.quote(code)
                stock_name = quote.name
            else:
                price = _number(snapshot.get("f2"))
                change = _number(snapshot.get("f3"))
                previous_close = _number(snapshot.get("f18")) or (price / (1 + change / 100) if price and change > -99 else 0)
                quote = Quote(
                    code=code, name=stock_name, price=price, previous_close=previous_close,
                    change_percent=change, volume=int(_number(snapshot.get("f5"))),
                    amount=_number(snapshot.get("f6")), turnover_rate=_number(snapshot.get("f8")),
                    open_price=_number(snapshot.get("f17")), high_price=_number(snapshot.get("f15")),
                    low_price=_number(snapshot.get("f16")),
                )
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
        history_bars or (provider.history(code, 160) if target_date else _live_history(provider, code)), as_of
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
    float_market_cap = _number(snapshot.get("f21"))
    float_shares = float_market_cap / previous_close if float_market_cap > 0 and previous_close > 0 else 0
    auction_shares = auction_amount / auction_price if auction_amount > 0 and auction_price > 0 else 0
    auction_turnover_percent = auction_shares / float_shares * 100 if float_shares > 0 else 0
    auction_volume_percent = auction_volume / avg_volume5 * 100 if avg_volume5 else 0
    price_vs_ma5 = (auction_price / ma5 - 1) * 100 if ma5 else 0
    three_day_change = (closes[-1] / closes[-4] - 1) * 100 if closes[-4] else 0
    five_day_change = (closes[-1] / closes[-6] - 1) * 100 if closes[-6] else 0
    ten_day_change = (closes[-1] / closes[-11] - 1) * 100 if closes[-11] else 0
    previous_volume_ratio = volumes[-1] / earlier_volume5 if earlier_volume5 else 0
    previous_open_gap_percent = (
        (bars[-1].open / bars[-2].close - 1) * 100 if len(bars) >= 2 and bars[-2].close else 0
    )
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
    previous_new_high = previous_close >= max(closes[:-1]) if len(closes) > 1 else False
    strong_characteristics = recent_10_limit_up_count >= 1 or previous_new_high
    nuclear_button = _nuclear_button_profile(
        previous_volume=volumes[-1],
        prior_volume=volumes[-2],
        previous_amount=bars[-1].amount,
        auction_gap_percent=gap,
        auction_amount=auction_amount,
        auction_turnover_percent=auction_turnover_percent,
        strong_characteristics=strong_characteristics,
        exact_auction=not target_date and not preliminary,
    )
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
    one_to_two_score, one_to_two_matched, one_to_two_reasons, one_to_two_risks = _one_to_two_score(
        gap, auction_volume_percent, auction_amount, float_market_cap, listed_sessions,
        price_vs_ma5, three_day_change, ten_day_change, previous_volume_ratio,
        previous_close_position, previous_upper_shadow, historical_proxy=bool(target_date),
    ) if consecutive_limit_up_days == 1 else (0, False, [], [])
    capacity_one_to_two_score, capacity_one_to_two_matched, capacity_one_to_two_reasons, capacity_one_to_two_risks = _capacity_one_to_two_score(
        consecutive_limit_up_days, recent_5_limit_up_count, gap, auction_volume_percent,
        auction_amount, float_market_cap, previous_close_position, previous_upper_shadow,
        ten_day_change, historical_proxy=bool(target_date),
    )
    recognition_channels = _recognition_channel_profile(
        consecutive_limit_ups=consecutive_limit_up_days,
        recent_5_limit_ups=recent_5_limit_up_count,
        recent_10_limit_ups=recent_10_limit_up_count,
        reversal_matched=reversal_matched,
        reversal_score=reversal_score,
        float_market_cap=float_market_cap,
        gap_percent=gap,
        auction_amount=auction_amount,
        auction_volume_percent=auction_volume_percent,
        auction_turnover_percent=auction_turnover_percent,
        previous_volume_ratio=previous_volume_ratio,
        previous_close_position=previous_close_position,
        previous_upper_shadow=previous_upper_shadow,
        ten_day_change=ten_day_change,
        historical_proxy=bool(target_date),
    )
    continuation_base_score = _next_day_continuation_score(
        consecutive_limit_up_days, recent_5_limit_up_count, gap, auction_volume_percent,
        auction_amount, previous_volume_ratio, float_market_cap, reversal_matched,
    )
    if first_board_matched:
        continuation_base_score = max(continuation_base_score, first_board_score - 25)
    if one_to_two_matched:
        continuation_base_score = max(continuation_base_score, one_to_two_score - 12)
    if capacity_one_to_two_matched:
        continuation_base_score = max(continuation_base_score, 76)
    regulation = regulatory_risk(all_completed_bars, auction_price)
    current_hhmm = datetime.now().hour * 100 + datetime.now().minute
    live_book_valid = not target_date and 920 <= current_hhmm <= 930 and quote is not None
    support = _big_order_support(
        decision_main_ratio,
        quote.order_imbalance if live_book_valid and (quote.bid_volume5 + quote.ask_volume5) > 0 else None,
    )
    execution_risk = _execution_risk_profile(
        consecutive_limit_up_days, gap, price_vs_ma5, previous_volume_ratio,
        previous_open_gap_percent, str(regulation.get("level") or "normal"),
        str(support.get("status") or "unknown"),
    )
    acceleration = (
        recent_limit_up_count >= 1 and previous_close_position >= 90
        and previous_upper_shadow <= 0.15 and 8.5 <= gap <= 10.2
    )
    strategy_mode = (
        "分歧转强" if reversal_matched and reversal_score >= max(core_chain_score, relay_score)
        else "连板核心（历史代理）" if core_chain_matched and target_date
        else "连板核心" if core_chain_matched
        else "容量一进二" if capacity_one_to_two_matched
        else "一进二竞价接力" if one_to_two_matched
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
    elif strategy_mode == "容量一进二":
        score, reasons, risks = capacity_one_to_two_score, capacity_one_to_two_reasons, capacity_one_to_two_risks
    elif strategy_mode == "一进二竞价接力":
        score, reasons, risks = one_to_two_score, one_to_two_reasons, one_to_two_risks
        if decision_main_ratio is None:
            risks.append("前序交易日主力资金暂不可用，按B级观察等待盘中确认")
        elif decision_main_ratio <= -3:
            risks.append("前序交易日主力资金明显流出")
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
    if nuclear_button["matched"]:
        score = max(score, nuclear_button["score"])
        continuation_base_score = max(continuation_base_score, 60)
        strategy_mode = "反核按钮竞价抄底"
        reasons = list(dict.fromkeys(nuclear_button["reasons"] + reasons))
        risks = list(dict.fromkeys(nuclear_button["risks"] + risks))
    risks.extend(execution_risk["risk_reasons"])
    fresh_relay_activity = recent_5_limit_up_count >= 1 or recent_10_limit_up_count >= 2
    within_board_scale = 0 < float_market_cap < 20_000_000_000
    explicit_order_weakness = support.get("status") == "weak"
    strong_core_override = (
        core_chain_matched and consecutive_limit_up_days >= 3
        and auction_amount > 50_000_000
    )
    continuation_primary = (
        within_board_scale and (not explicit_order_weakness or strong_core_override) and (
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
        and (one_to_two_matched or relay_matched or first_board_matched)
        and continuation_base_score >= 55
        and auction_amount >= 10_000_000 and auction_volume_percent >= 1
        and 2 <= gap <= 10.2 and previous_close_position >= 85
    )
    capacity_one_to_two_secondary = capacity_one_to_two_matched and not explicit_order_weakness
    first_board_secondary = (
        consecutive_limit_up_days == 0 and
        within_board_scale and not explicit_order_weakness
        and first_board_matched and continuation_base_score >= 55
    )
    leader_repair_secondary = (
        consecutive_limit_up_days == 0
        and recent_5_limit_up_count >= 3 and recent_10_limit_up_count >= 4
        and within_board_scale and listed_sessions >= 60
        and 1 <= gap <= 5 and auction_amount >= 30_000_000
        and auction_volume_percent >= 3
        and previous_close_position >= 85
        and 1.5 <= previous_volume_ratio <= 4
        and previous_upper_shadow <= 0.35
        and ten_day_change <= 90
    )
    if leader_repair_secondary:
        score = max(score, min(90, 72 + min(recent_5_limit_up_count, 4) * 3 + (4 if gap >= 3 else 2)))
        continuation_base_score = max(continuation_base_score, 60)
        strategy_mode = "龙头断板修复"
        reasons = list(dict.fromkeys(reasons + [
            "近5/10日涨停活性显示高辨识度龙头",
            "前日放量分歧但收盘仍在区间高位",
            "竞价温和高开且量额达到修复确认线",
        ]))
        risks = list(dict.fromkeys(risks + [
            "断板修复不等于重新进入连板周期，失败时回撤可能较快",
            "竞价额未达5000万A级线，必须等待开盘主动买盘和快速封板确认",
        ]))
        if explicit_order_weakness:
            risks.append("前序主力资金偏弱，仅因竞价量价修复达到B级观察线，不得提前追高")
    limit_down_reversal_secondary = (
        consecutive_limit_up_days >= 1
        and recent_5_limit_up_count >= 3
        and within_board_scale and listed_sessions >= 60
        and not explicit_order_weakness
        and -10.2 <= gap <= -9.5
        and auction_amount >= 50_000_000
        and auction_volume_percent >= 5
        and previous_close_position >= 90
        and 0.3 <= previous_volume_ratio <= 5
    )
    if limit_down_reversal_secondary:
        score = max(score, min(90, 62 + recent_5_limit_up_count * 4 + (8 if auction_amount >= 70_000_000 else 4)))
        continuation_base_score = max(continuation_base_score, 58)
        strategy_mode = "跌停竞价反核"
        reasons = list(dict.fromkeys(reasons + [
            "前日仍保留涨停连续性",
            "近5日涨停活性强",
            "跌停竞价成交额与量能达到反核观察门槛",
        ]))
        risks = list(dict.fromkeys(risks + [
            "跌停竞价不代表必然反转，仅低优先级观察",
            "09:30后必须确认打开跌停、主动买盘和回封失败风险",
        ]))
    capacity_relay_secondary = recognition_channels["capacity_relay"]
    leader_reversal_secondary = recognition_channels["leader_reversal"]
    auction_grab_secondary = recognition_channels["auction_grab"]
    if capacity_relay_secondary:
        score = max(score, 88)
        continuation_base_score = max(continuation_base_score, 55)
        strategy_mode = "高辨识度容量接力"
        reasons = list(dict.fromkeys(reasons + [
            "近期涨停活性高且前日强势收盘",
            "竞价量额达到容量接力门槛",
            "流通市值虽超过200亿，但辨识度允许扩容观察",
        ]))
        risks = list(dict.fromkeys(risks + [
            "流通市值200亿–350亿，封板所需资金更大，只进入容量龙头观察层",
            "开盘主动买盘不足或板块不同步时不得因历史股性追高",
        ]))
    if leader_reversal_secondary:
        score = max(score, 92)
        continuation_base_score = max(continuation_base_score, 60)
        strategy_mode = "龙头分歧反包"
        reasons = list(dict.fromkeys(reasons + [
            "近5/10日涨停活性确认高辨识度龙头",
            "前日分歧后仍保留承接，竞价大幅超预期",
            "高竞价量额达到反包观察线",
        ]))
        risks = list(dict.fromkeys(risks + [
            "分歧反包失败时容易形成高位兑现，仅作为高风险观察",
            "接近涨停竞价可能难成交；可成交时需防封单松动",
        ]))
    if auction_grab_secondary:
        score = max(score, 84)
        continuation_base_score = max(continuation_base_score, 55)
        strategy_mode = "竞价抢筹首板"
        reasons = list(dict.fromkeys(reasons + [
            "近期存在涨停股性但昨日并非连续涨停",
            "竞价接近涨停且成交额、换手率显著放大",
            "前日趋势与承接未明显破坏",
        ]))
        risks = list(dict.fromkeys(risks + [
            "竞价抢筹首板不等于封板成功，开盘兑现风险高",
            "接近一字涨停可能无法成交；能成交时需警惕封单松动",
        ]))
    overnight_secondary = one_to_two_secondary or capacity_one_to_two_secondary or first_board_secondary
    one_price_core_watch = (
        continuation_primary
        and consecutive_limit_up_days >= 3
        and core_chain_matched and core_chain_score >= 90
        and 9.5 <= gap <= 10.2
        and auction_amount >= 15_000_000
        and auction_volume_percent >= 10
        and previous_close_position >= 95
        and previous_upper_shadow <= 0.10
        and not execution_risk["risk_veto"]
    )
    auction_amount_gate, auction_liquidity_tier = _auction_amount_qualification(
        auction_amount, continuation_primary, consecutive_limit_up_days,
        relay_score, str(support.get("status") or "unknown"), leader_repair_secondary,
        one_to_two=one_to_two_secondary or capacity_one_to_two_secondary,
        one_price_core=one_price_core_watch,
    )
    if nuclear_button["matched"]:
        auction_amount_gate, auction_liquidity_tier = True, "A"
    eligible = (
        continuation_primary or overnight_secondary or limit_down_reversal_secondary or leader_repair_secondary
        or nuclear_button["matched"] or capacity_relay_secondary
        or leader_reversal_secondary or auction_grab_secondary
    ) and auction_amount_gate
    if auction_liquidity_tier == "B":
        liquidity_reason = (
            "竞价成交额未达5000万A级线，仅按高辨识度龙头修复进入B级观察"
            if leader_repair_secondary else
            "竞价成交额未达5000万A级线，仅因成熟连板结构进入B级观察；前序主力不得明确走弱"
        )
        risks = list(dict.fromkeys(risks + [
            liquidity_reason,
            "竞价资金强度弱于A级核心，开盘后炸板风险较高，必须等待封单确认",
        ]))
    elif auction_liquidity_tier == "C":
        reasons = list(dict.fromkeys(reasons + [
            "三板以上高辨识度一字核心达到C级竞价真实性门槛",
        ]))
        risks = list(dict.fromkeys(risks + [
            "竞价额未达3000万B级线，仅作为C级一字板高风险观察",
            "正常封单可能无法成交；若开板后能够成交，需先视为封单松动而非低风险买点",
        ]))
    previous_limit_up_breaks = int(_number(snapshot.get("_previous_limit_up_breaks")))
    if previous_limit_up_breaks >= 2:
        score = max(0, score - 4)
        continuation_base_score = max(0, continuation_base_score - 5)
        risks = list(dict.fromkeys(risks + [
            f"前一交易日涨停过程中炸板{previous_limit_up_breaks}次，封板稳定性弱于零炸板核心",
        ]))
    priority_tier = (
        "不入选" if not auction_amount_gate else
        "反核按钮观察" if nuclear_button["matched"] else
        "龙头反包观察" if leader_reversal_secondary else
        "容量龙头观察" if capacity_relay_secondary else
        "抢筹首板观察" if auction_grab_secondary else
        "连板优先" if continuation_primary else
        "龙头修复观察" if leader_repair_secondary else
        "容量一进二观察" if capacity_one_to_two_secondary else
        "一进二观察" if one_to_two_secondary else
        "首板观察" if first_board_secondary else
        "跌停反核观察" if limit_down_reversal_secondary else "不入选"
    )
    board_stage = _board_stage(consecutive_limit_up_days)
    if leader_repair_secondary:
        board_stage = {
            "board_stage_label": "龙头断板修复",
            "previous_board_count": 0,
            "target_board_count": 1,
        }
    if nuclear_button["matched"]:
        board_stage = {
            "board_stage_label": "反核按钮竞价抄底",
            "previous_board_count": 0,
            "target_board_count": 1,
        }
    elif leader_reversal_secondary:
        board_stage = {"board_stage_label": "龙头分歧反包", "previous_board_count": 0, "target_board_count": 1}
    elif capacity_relay_secondary:
        board_stage = _board_stage(consecutive_limit_up_days)
        board_stage["board_stage_label"] = f"容量龙头 · {board_stage['board_stage_label']}"
    elif auction_grab_secondary:
        board_stage = {"board_stage_label": "竞价抢筹首板", "previous_board_count": 0, "target_board_count": 1}
    return {
        "code": code, "name": stock_name, "industry": industry, "category": industry,
        "evaluation_date": as_of.isoformat(),
        "score": score, "auction_base_score": base_score, "relay_score": relay_score,
        "relay_matched": relay_matched, "core_chain_matched": core_chain_matched,
        "core_chain_score": core_chain_score, "reversal_score": reversal_score,
        "reversal_matched": reversal_matched, "first_board_score": first_board_score,
        "first_board_matched": first_board_matched,
        "one_to_two_score": one_to_two_score, "one_to_two_matched": one_to_two_matched,
        "capacity_one_to_two_score": capacity_one_to_two_score,
        "capacity_one_to_two_matched": capacity_one_to_two_matched,
        "continuation_base_score": continuation_base_score,
        "leader_repair_matched": leader_repair_secondary,
        "nuclear_button_matched": nuclear_button["matched"],
        "nuclear_button_hard_matched": nuclear_button["hard_matched"],
        "nuclear_button_checks": nuclear_button["checks"],
        "capacity_relay_matched": capacity_relay_secondary,
        "leader_reversal_matched": leader_reversal_secondary,
        "auction_grab_matched": auction_grab_secondary,
        "strategy_mode": strategy_mode, "priority_tier": priority_tier, "eligible": eligible,
        "auction_liquidity_tier": auction_liquidity_tier,
        "auction_amount_threshold": 15_000_000 if auction_liquidity_tier == "C" else 30_000_000 if auction_liquidity_tier == "B" else 50_000_000,
        **execution_risk,
        **board_stage,
        "signal": "强竞价观察" if score >= 75 and eligible else "竞价关注" if eligible else "不进入打板候选",
        "auction_time": auction_time, "auction_price": auction_price,
        "auction_data_source": auction_data_source,
        "auction_gap_percent": round(gap, 2), "auction_volume": int(auction_volume),
        "auction_amount": auction_amount, "auction_volume_percent": round(auction_volume_percent, 2),
        "auction_turnover_percent": round(auction_turnover_percent, 2),
        "previous_close": round(previous_close, 2),
        "previous_open": round(bars[-1].open, 2),
        "previous_amount": bars[-1].amount,
        "previous_volume": volumes[-1],
        "prior_volume": volumes[-2],
        "previous_volume_contraction_ratio": round(volumes[-1] / volumes[-2], 3) if volumes[-2] > 0 else None,
        "previous_new_high": previous_new_high,
        "strong_characteristics": strong_characteristics,
        "ma5": round(ma5, 2), "price_vs_ma5_percent": round(price_vs_ma5, 2),
        "three_day_change_percent": round(three_day_change, 2),
        "five_day_change_percent": round(five_day_change, 2),
        "ten_day_change_percent": round(ten_day_change, 2),
        "previous_volume_ratio": round(previous_volume_ratio, 2),
        "previous_open_gap_percent": round(previous_open_gap_percent, 2),
        "previous_close_position_percent": round(previous_close_position, 2),
        "previous_upper_shadow_ratio": round(previous_upper_shadow, 3),
        "recent_limit_up_count": recent_limit_up_count,
        "recent_5_limit_up_count": recent_5_limit_up_count,
        "recent_10_limit_up_count": recent_10_limit_up_count,
        "previous_limit_up_breaks": previous_limit_up_breaks,
        "consecutive_limit_up_days": consecutive_limit_up_days,
        "previous_day_limit_up": consecutive_limit_up_days >= 1,
        "snapshot_main_net": _number(snapshot.get("f62")),
        "snapshot_main_ratio": _number(snapshot.get("f184")),
        "decision_main_net": decision_main_net,
        "decision_main_ratio": decision_main_ratio,
        "auction_order_imbalance": round(quote.order_imbalance, 4) if live_book_valid else None,
        "auction_bid_volume5": quote.bid_volume5 if live_book_valid else None,
        "auction_ask_volume5": quote.ask_volume5 if live_book_valid else None,
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
            "status": "secondary", "label": "一字板C级推荐 · 可挂单打板",
            "requirements": ["至少3连板且核心评分≥90", "竞价额≥1500万且竞价量占比≥10%", "无风险硬否决", "开板后主动买盘能够快速重新封板"],
            "note": "C级具备高辨识度一字板推荐资格，可按涨停价排队，固定排在可成交A级/B级之后；封死时可能排不到，开板能成交时先按封单松动处理。",
        } if auction_liquidity_tier == "C" else {
            "status": "secondary", "label": "等待反核主动买盘确认",
            "requirements": ["9:25五项硬条件全部成立", "市场情绪处于冰点", "个股具备强势股性和题材辨识度", "开盘后不快速跌破竞价价"],
            "note": "反核按钮只进入高风险观察层；高开无承接、卖盘占优或题材退潮时不得跟随。",
        } if priority_tier == "反核按钮观察" else {
            "status": "secondary", "label": "等待龙头反包封板确认",
            "requirements": ["竞价强势不快速回落", "主动买盘持续", "首次封板不反复炸板", "板块核心同步走强"],
            "note": "龙头分歧反包属于高波动观察；竞价高开兑现或封板失败时不跟随。",
        } if priority_tier == "龙头反包观察" else {
            "status": "secondary", "label": "等待容量龙头封板确认",
            "requirements": ["竞价后承接稳定", "成交额持续放大", "板块共振", "封单规模匹配大市值"],
            "note": "容量股超过200亿需要更多资金推动，未形成板块合力时不跟随。",
        } if priority_tier == "容量龙头观察" else {
            "status": "secondary", "label": "等待抢筹首板可成交确认",
            "requirements": ["实际可成交", "开盘不快速回落", "主动买盘持续", "首次封板稳定"],
            "note": "接近一字的抢筹首板可能买不到；若能成交且封单松动，需优先防炸板。",
        } if priority_tier == "抢筹首板观察" else {
            "status": "pending", "label": "等待T日盘中封板确认",
            "requirements": ["实际触及涨停价", "封单与主动买盘不弱", "炸板后快速回封", "板块核心未同步转弱"],
            "note": "09:25仅生成次日连板预选，未完成封板确认前不得视为打板信号。",
        } if priority_tier == "连板优先" else {
            "status": "secondary",
            "label": "等待龙头修复封板确认",
            "requirements": ["开盘主动买盘持续", "快速拉离竞价支撑", "首次封板不反复炸板", "医药板块核心同步走强"],
            "note": "龙头断板修复属于高波动B级观察；未快速封板或反复炸板时不跟随。",
        } if priority_tier == "龙头修复观察" else {
            "status": "secondary",
            "label": "等待打开跌停并确认承接",
            "requirements": ["快速打开跌停", "主动买盘持续放大", "价格快速拉离跌停价", "板块核心同步走强"],
            "note": "跌停竞价只是反核观察池；09:30后未打开跌停或买盘不连续时不跟随。",
        } if priority_tier == "跌停反核观察" else {
            "status": "secondary",
            "label": "等待容量二板与板块共振确认" if priority_tier == "容量一进二观察" else "等待二板封板确认" if priority_tier == "一进二观察" else "等待首板封板确认",
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
        snapshots = main_board_snapshots(cache_seconds=15)
        prefiltered = _expand_live_prefilter_with_extremes(
            _prefilter_auction_universe(snapshots, limit=LIVE_DEEP_LIMIT), snapshots, limit=LIVE_DEEP_LIMIT,
        )
        try:
            limit_up_pool = previous_limit_up_pool()
            prefiltered = _expand_with_previous_limit_ups(prefiltered, snapshots, limit_up_pool)
            universe_source = "东方财富延迟全主板批量快照 + 最近交易日完整涨停必扫池"
        except Exception:
            universe_source = "东方财富延迟全主板批量快照（昨日涨停池暂不可用）"
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
    failed_snapshots: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(_auction_candidate, snapshot, provider, None, None, preliminary): snapshot
            for snapshot in prefiltered
        }
        for future in as_completed(futures):
            snapshot = futures[future]
            try:
                candidate = future.result()
                if candidate:
                    candidates.append(candidate)
            except Exception:
                failed += 1
                failed_snapshots.append(snapshot)
    # 行情源在集合竞价高并发时可能主动断连；优先低并发补扫昨日涨停池，
    # 避免把“全部请求失败”误显示成“今日无候选”。
    priority_retry = [item for item in failed_snapshots if item.get("_previous_limit_up_streak")][:80]
    if priority_retry:
        time.sleep(0.8)
        with ThreadPoolExecutor(max_workers=3) as executor:
            retry_futures = {
                executor.submit(_auction_candidate, snapshot, provider, None, None, preliminary): snapshot
                for snapshot in priority_retry
            }
            for future in as_completed(retry_futures):
                try:
                    candidate = future.result()
                    failed -= 1
                    if candidate:
                        candidates.append(candidate)
                except Exception:
                    pass
    if not candidates and failed >= len(futures):
        raise MarketDataError("竞价候选深度行情全部读取失败，请稍后重试；本次不生成空榜单缓存")
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
        "one_price_c_count": sum(item.get("eligible") and item.get("auction_liquidity_tier") == "C" for item in candidates),
        "untradable_count": sum(not item.get("tradable", True) for item in candidates),
        "risk_veto_count": sum(item.get("risk_veto", False) for item in candidates),
        "one_to_two_count": sum(item.get("eligible") and item.get("priority_tier") in {"一进二观察", "容量一进二观察"} for item in candidates),
        "first_board_watch_count": sum(item.get("eligible") and item.get("priority_tier") == "首板观察" for item in candidates),
        "leader_repair_count": sum(item.get("eligible") and item.get("leader_repair_matched") for item in candidates),
        "nuclear_button_count": sum(item.get("eligible") and item.get("nuclear_button_matched") for item in candidates),
        "capacity_relay_count": sum(item.get("eligible") and item.get("capacity_relay_matched") for item in candidates),
        "leader_reversal_count": sum(item.get("eligible") and item.get("leader_reversal_matched") for item in candidates),
        "auction_grab_count": sum(item.get("eligible") and item.get("auction_grab_matched") for item in candidates),
        "overnight_secondary_count": sum(item.get("eligible") and item.get("priority_tier") in {"一进二观察", "容量一进二观察", "首板观察"} for item in candidates),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "method": (
            "09:20进入不可撤单阶段后，扫描全部非ST沪深主板并用参考撮合价量生成观察池；09:25再以最终竞价成交复核。"
            if preliminary else
            "先扫描全部非ST沪深主板，普通活跃股初筛Top80，并强制追加最近交易日完整涨停池及涨跌停附近竞价股；同时识别昨日成交额≥5亿且成交量低于前日、09:25竞价额≥5000万、高开≥7%、竞价换手≥3%的反核按钮高风险观察股，最多6只。"
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
            executor.submit(_live_history, provider, str(snapshot.get("f12")), 160): snapshot
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
        "one_price_c_count": sum(item.get("eligible") and item.get("auction_liquidity_tier") == "C" for item in candidates),
        "untradable_count": sum(not item.get("tradable", True) for item in candidates),
        "risk_veto_count": sum(item.get("risk_veto", False) for item in candidates),
        "one_to_two_count": sum(item.get("eligible") and item.get("priority_tier") in {"一进二观察", "容量一进二观察"} for item in candidates),
        "first_board_watch_count": sum(item.get("eligible") and item.get("priority_tier") == "首板观察" for item in candidates),
        "leader_repair_count": sum(item.get("eligible") and item.get("leader_repair_matched") for item in candidates),
        "nuclear_button_count": sum(item.get("eligible") and item.get("nuclear_button_matched") for item in candidates),
        "capacity_relay_count": sum(item.get("eligible") and item.get("capacity_relay_matched") for item in candidates),
        "leader_reversal_count": sum(item.get("eligible") and item.get("leader_reversal_matched") for item in candidates),
        "auction_grab_count": sum(item.get("eligible") and item.get("auction_grab_matched") for item in candidates),
        "overnight_secondary_count": sum(item.get("eligible") and item.get("priority_tier") in {"一进二观察", "容量一进二观察", "首板观察"} for item in candidates),
        "scanned": len(snapshots), "prefiltered": len(prefiltered),
        "deep_scanned": len(futures), "failed": history_failed + auction_failed,
        "universe_source": "全主板T-1历史K线 + 新浪目标日09:31首根一分钟线",
        "snapshot_time": None, "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "selected_date": target_date.isoformat(), "historical": True,
        "replay_warning": "历史逐笔接口不支持按日期回放，因此使用目标日09:31首根一分钟线的开盘价和累计量作为竞价代理；行业名称取当前分类，不参与评分。",
        "method": "全主板逐只读取目标日前K线并初筛500只；使用历史09:31代理量计算T+1连板预期分，2进3及以上优先，量价亮眼的一进二与首板降级观察，再结合题材、大单和异动风险排序。",
        "disclaimer": "历史结果只用于比较规则，不代表当时可成交，也不得作为当前交易信号。",
    }
