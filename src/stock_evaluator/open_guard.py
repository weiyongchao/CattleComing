from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
from datetime import date, datetime
from math import isfinite
from threading import RLock

from .auction import _auction_candidate, _live_history, _theme_bucket
from .board_selection import MAX_BOARD_PICKS, intraday_selection_window, select_live_recommendations
from .board_plan import BOARD_STRATEGY_VERSION, _market_gate
from .board_focus import DAILY_FOCUS_STORE, DailyFocusError
from .board_research import BOARD_RESEARCH_STORE
from .quote_sampling import china_time, quote_time_text, quote_freshness
from .corporate_events import attach_corporate_event_risks
from .evaluator import evaluate
from .funds import individual_fund_flow
from .market import EastmoneyProvider, MarketDataError, Quote
from .trade_advice import live_entry_plan
from .universe import main_board_snapshots, previous_limit_up_pool


_OPENING_DISCOVERY_CACHE: dict[str, list[dict]] = {}
_SELECTION_OBSERVATIONS: dict[str, dict] = {}
_SELECTION_LOCK = RLock()


def _opening_market_context(snapshots: list[dict], now: datetime, auction_count: int, fallback: dict) -> dict:
    """开盘市场开关使用同一轮新行情，不能把09:25空候选永久当成全日空仓。"""
    fresh = [row for row in snapshots if not quote_freshness({"quote_time": row.get("f124")}, now)[1]
             and _number(row.get("f2")) > 0]
    if len(fresh) < 500:
        return {**fallback, "intraday_refreshed": False}
    changes = [_number(row.get("f3")) for row in fresh]
    advance = sum(value > 0 for value in changes) / len(changes)
    up, down = sum(value >= 9.5 for value in changes), sum(value <= -9.5 for value in changes)
    average = sum(changes) / len(changes)
    return {**_market_gate(advance, up, down, average, auction_count), "intraday_refreshed": True,
            "source": "本轮开盘/盘中主板行情", "sample_size": len(fresh), "advance_ratio": round(advance, 4),
            "limit_up": up, "limit_down": down, "average_change": round(average, 2),
            "generated_at": now.isoformat(timespec="seconds")}


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _opening_discovery_window(now: datetime) -> bool:
    hhmm = now.hour * 100 + now.minute
    return 930 <= hhmm <= 935


def _quote_from_snapshot(snapshot: dict) -> Quote:
    """全市场批量行情作为逐股接口失败时的只读降级数据。"""
    return Quote(
        code=str(snapshot.get("f12") or ""),
        name=str(snapshot.get("f14") or snapshot.get("f12") or ""),
        price=_number(snapshot.get("f2")),
        previous_close=_number(snapshot.get("f18")),
        change_percent=_number(snapshot.get("f3"), -99),
        volume=int(_number(snapshot.get("f5"))),
        amount=_number(snapshot.get("f6")),
        turnover_rate=_number(snapshot.get("f8")),
        open_price=_number(snapshot.get("f17")),
        high_price=_number(snapshot.get("f15")),
        low_price=_number(snapshot.get("f16")),
        quote_time=quote_time_text(snapshot.get("f124")), quote_source="eastmoney_batch",
    )


def _live_one_to_two_prefilter(snapshot: dict, previous_streak: int, theme_peer_count: int) -> bool:
    """识别09:25落选后盘中转强的昨日首板股，供全主板动态榜单补入。"""
    if previous_streak != 1:
        return False
    change = _number(snapshot.get("f3"), -99)
    amount = _number(snapshot.get("f6"))
    volume_ratio = _number(snapshot.get("f10"))
    price, open_price = _number(snapshot.get("f2")), _number(snapshot.get("f17"))
    low_price, high_price = _number(snapshot.get("f16")), _number(snapshot.get("f15"))
    price_vs_open = (price / open_price - 1) * 100 if price > 0 and open_price > 0 else -99
    high_vs_open = (high_price / open_price - 1) * 100 if high_price > 0 and open_price > 0 else -99
    intraday_position = (price - low_price) / (high_price - low_price) * 100 if high_price > low_price else 100 if price > 0 else 0
    main_ratio = _number(snapshot.get("f184"))
    support_confirmed = theme_peer_count >= 1 or main_ratio > 0
    late_breakout_override = change >= 7 and price_vs_open >= 4 and amount >= 200_000_000
    sealed_one_price = (
        9.5 <= change <= 10.2 and abs(price_vs_open) <= 0.2
        and amount >= 100_000_000 and volume_ratio >= 0.5
        and intraday_position >= 95
    )
    return (
        sealed_one_price or (
            3 <= change <= 10.2 and price_vs_open >= 2
            and amount >= 100_000_000 and volume_ratio >= 0.5
            and intraday_position >= 70
            and (support_confirmed or late_breakout_override)
        )
    )


def _live_multi_board_prefilter(snapshot: dict, previous_streak: int, max_streak: int) -> bool:
    """用09:30后成交确认补回竞价偏弱、但开盘主动转强的二板及以上连板股。"""
    if previous_streak < 2:
        return False
    change = _number(snapshot.get("f3"), -99)
    amount = _number(snapshot.get("f6"))
    volume_ratio = _number(snapshot.get("f10"))
    price, open_price = _number(snapshot.get("f2")), _number(snapshot.get("f17"))
    low_price, high_price = _number(snapshot.get("f16")), _number(snapshot.get("f15"))
    price_vs_open = (price / open_price - 1) * 100 if price > 0 and open_price > 0 else -99
    high_vs_open = (high_price / open_price - 1) * 100 if high_price > 0 and open_price > 0 else -99
    intraday_position = (price - low_price) / (high_price - low_price) * 100 if high_price > low_price else 100 if price > 0 else 0
    is_space_board = max_streak >= 3 and previous_streak == max_streak
    if is_space_board:
        return (
            0 <= change <= 10.2 and price_vs_open >= 0
            and amount >= 50_000_000 and volume_ratio >= 0.5
            and intraday_position >= 65
            and (price_vs_open >= 0.5 or high_vs_open >= 1.5)
        )
    return (
        3 <= change <= 10.2 and price_vs_open >= 1.5
        and amount >= 100_000_000 and volume_ratio >= 0.5
        and intraday_position >= 70
    )


def _discover_live_one_to_two(
    provider: EastmoneyProvider, existing_codes: set[str], limit: int = 4,
    snapshots: list[dict] | None = None,
) -> list[dict]:
    snapshots = snapshots or main_board_snapshots(cache_seconds=15)
    previous = previous_limit_up_pool(date.today())
    streak_by_code = {str(item.get("c") or ""): int(_number(item.get("lbc"))) for item in previous}
    first_board_rows = [
        row for row in snapshots
        if streak_by_code.get(str(row.get("f12") or "")) == 1
        and str(row.get("f12") or "") not in existing_codes
        and 3 <= _number(row.get("f3"), -99) <= 10.2
    ]
    strong_by_theme: dict[str, int] = {}
    for row in first_board_rows:
        bucket = _theme_bucket(str(row.get("f100") or ""))
        strong_by_theme[bucket] = strong_by_theme.get(bucket, 0) + 1
    filtered = [
        row for row in first_board_rows
        if _live_one_to_two_prefilter(
            row,
            streak_by_code.get(str(row.get("f12") or ""), 0),
            max(0, strong_by_theme.get(_theme_bucket(str(row.get("f100") or "")), 0) - 1),
        )
    ]
    filtered.sort(key=lambda row: (
        _number(row.get("f3")), _number(row.get("f184")), _number(row.get("f6")),
    ), reverse=True)
    targets = filtered[:limit]
    discovered: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(targets)))) as executor:
        evaluated = list(executor.map(lambda snapshot: _auction_candidate(snapshot, provider), targets))
    for candidate in evaluated:
        if candidate is None:
            continue
        candidate.update({
            "priority_tier": "盘中一进二观察",
            "strategy_mode": "盘中弱转强一进二",
            "board_stage_label": "盘中1进2 · 目标2连板",
            "action": "盘中全主板新增 · 非09:25冻结候选",
            "continuation_score": max(58, _number(candidate.get("continuation_score"))),
            "discovery_source": "盘中全主板新增",
            "live_entry_allowed": True,
            "_auction_rank": 0,
        })
        discovered.append(candidate)
    return discovered


def _discover_live_multi_board(
    provider: EastmoneyProvider, existing_codes: set[str], limit: int = 4,
    snapshots: list[dict] | None = None,
) -> list[dict]:
    snapshots = snapshots or main_board_snapshots(cache_seconds=15)
    previous = previous_limit_up_pool(date.today())
    streak_by_code = {str(item.get("c") or ""): int(_number(item.get("lbc"))) for item in previous}
    max_streak = max(streak_by_code.values(), default=0)
    filtered = [
        row for row in snapshots
        if str(row.get("f12") or "") not in existing_codes
        and _live_multi_board_prefilter(
            row, streak_by_code.get(str(row.get("f12") or ""), 0), max_streak,
        )
    ]
    filtered.sort(key=lambda row: (
        streak_by_code.get(str(row.get("f12") or ""), 0) == max_streak,
        streak_by_code.get(str(row.get("f12") or ""), 0),
        _number(row.get("f3")), _number(row.get("f6")),
    ), reverse=True)
    targets = filtered[:limit]
    discovered: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(targets)))) as executor:
        evaluated = list(executor.map(lambda snapshot: _auction_candidate(snapshot, provider), targets))
    for snapshot, candidate in zip(targets, evaluated):
        if candidate is None:
            continue
        streak = streak_by_code.get(str(snapshot.get("f12") or ""), 0)
        is_space_board = max_streak >= 3 and streak == max_streak
        candidate.update({
            "priority_tier": "盘中空间板观察" if is_space_board else "盘中连板补选",
            "strategy_mode": "空间板开盘确认" if is_space_board else "盘中连板转强",
            "board_stage_label": f"盘中{streak}进{streak + 1} · 开盘转强确认",
            "action": "09:30开盘确认补选 · 非09:25主榜",
            "continuation_score": max(60 if is_space_board else 58, _number(candidate.get("continuation_score"))),
            "discovery_source": "09:30开盘补选",
            "live_entry_allowed": True,
            "space_board_watch": is_space_board,
            "_auction_rank": 0,
        })
        discovered.append(candidate)
    return discovered


def _open_confirmation(candidate: dict, result: dict, funds: dict | None) -> dict:
    quote, metrics = result["quote"], result["metrics"]
    book = result["order_book"]
    auction_price = _number(candidate.get("auction_price"), _number(quote.get("open_price")))
    price = _number(quote.get("price"))
    price_vs_auction = (price / auction_price - 1) * 100 if auction_price else None
    auction_amount = _number(candidate.get("auction_amount"))
    current_amount = _number(quote.get("amount"))
    previous_close = _number(quote.get("previous_close"))
    if previous_close <= 0 and price > 0 and _number(quote.get("change_percent")) > -99:
        previous_close = price / (1 + _number(quote.get("change_percent")) / 100)
    limit_up_price = round(previous_close * 1.1 + 1e-8, 2) if previous_close > 0 else 0
    high_price = _number(quote.get("high_price"), price)
    reported_low = _number(quote.get("low_price"))
    low_price = reported_low if reported_low > 0 else price
    open_price = _number(quote.get("open_price"))
    low_vs_open = (low_price / open_price - 1) * 100 if low_price > 0 and open_price > 0 else 0.0
    rebound_from_low = (price / low_price - 1) * 100 if price > 0 and low_price > 0 else 0.0
    opening_dip = bool(reported_low > 0 and low_vs_open <= -3.0)
    reclaimed_previous_close = bool(previous_close > 0 and price >= previous_close)
    reclaimed_auction = bool(auction_price > 0 and price >= auction_price * 0.995)
    rebound_started = bool(opening_dip and rebound_from_low >= 1.5 and reclaimed_previous_close)
    rebound_confirmed = bool(rebound_started and rebound_from_low >= 3.0 and reclaimed_auction)
    touched_limit_up = bool(limit_up_price and high_price >= limit_up_price - 0.005)
    sealed = bool(limit_up_price and price >= limit_up_price - 0.005 and quote["change_percent"] >= 9.5)
    failed_board = touched_limit_up and not sealed
    high_change_percent = (high_price / previous_close - 1) * 100 if previous_close > 0 else 0.0
    near_limit_attempt = bool(limit_up_price and high_change_percent >= 9.8)
    near_limit_failure = bool(
        near_limit_attempt and not sealed
        and high_price > 0 and price <= high_price * 0.995
    )
    seal_value = int(_number(candidate.get("previous_final_seal_time")))
    late_final_seal_watch = bool(
        candidate.get("late_final_seal_watch")
        or (
            candidate.get("consecutive_limit_up_days", 0) >= 2
            and seal_value >= 113000
        )
    )
    auction_tradable = candidate.get("tradable", True)
    live_tradable = bool(
        auction_tradable
        or (limit_up_price and low_price > 0 and low_price < limit_up_price - 0.005)
    )
    opened_one_price_reseal = bool(not auction_tradable and live_tradable and sealed)
    board_entry_allowed = bool(candidate.get("board_entry_allowed"))
    event_high = (candidate.get("corporate_event_risk") or {}).get("level") == "high"
    live_entry_allowed = bool(candidate.get("live_entry_allowed")) and not event_high
    entry_authorized = board_entry_allowed or live_entry_allowed
    nuclear_mode = candidate.get("strategy_mode") == "反核按钮竞价抄底"
    fund_current = bool(funds and funds.get("is_today") and str(funds.get("date"))[:10] == date.today().isoformat())
    main_ratio = _number(funds.get("main_ratio"), float("nan")) if fund_current and funds else None
    if main_ratio is not None and not isfinite(main_ratio):
        main_ratio = None
    checks = [
        {"name": "未跌破竞价支撑", "passed": None if price_vs_auction is None else price_vs_auction >= -1.0,
         "value": "竞价价未留存" if price_vs_auction is None else f"较竞价价{price_vs_auction:+.2f}%"},
        {"name": "开盘后承接稳定", "passed": metrics["price_vs_open_percent"] >= -0.8,
         "value": f"较开盘{metrics['price_vs_open_percent']:+.2f}%"},
        {"name": "涨幅可交易且未透支", "passed": 0.5 <= quote["change_percent"] < 9.5,
         "value": f"当前{quote['change_percent']:+.2f}%"},
        {"name": "实际成交继续放大", "passed": current_amount >= max(20_000_000, auction_amount * 1.15),
         "value": f"成交额{current_amount / 1e8:.2f}亿"},
        {"name": "五档卖压未占优", "passed": book["imbalance"] > -0.15,
         "value": f"盘口失衡{book['imbalance'] * 100:+.1f}%"},
        {"name": "价格仍在MA5上方", "passed": metrics["price_vs_ma5_percent"] > 0,
         "value": f"MA5偏离{metrics['price_vs_ma5_percent']:+.2f}%"},
        {"name": "当日主力未明显流出", "passed": None if main_ratio is None else main_ratio > -3,
         "value": "当日资金暂不可用" if main_ratio is None else f"主力占比{main_ratio:+.2f}%"},
        {"name": "未处于高异动风险", "passed": result["regulatory_risk"]["level"] != "high",
         "value": result["regulatory_risk"]["label"]},
        {"name": "无并购重组高风险", "passed": not event_high,
         "value": (candidate.get("corporate_event_risk") or {}).get("summary") or "未识别到并购重组高风险事件"},
    ]
    passed = sum(check["passed"] is True for check in checks)
    known = sum(check["passed"] is not None for check in checks)
    base = _number(candidate.get("continuation_score"), _number(candidate.get("selection_score"), _number(candidate.get("score"))))
    score = round(base * 0.35 + passed * 9 + (5 if book["imbalance"] >= 0.15 else 0))
    if sealed:
        score += 18
    elif failed_board:
        score -= 28
    elif near_limit_failure:
        score -= 24
    if price_vs_auction is not None:
        score += round(max(-12, min(12, price_vs_auction * 2)))
    score = max(0, min(100, score))
    structural_break = (
        book["imbalance"] <= -0.35
        or (main_ratio is not None and main_ratio <= -5)
        or result["regulatory_risk"]["level"] == "high"
        or event_high
    )
    hard_reject = structural_break or (
        not opening_dip and (
            quote["change_percent"] < 0
            or metrics["price_vs_open_percent"] < -2
            or (price_vs_auction is not None and price_vs_auction < -3)
        )
    )
    nuclear_reject = nuclear_mode and (
        quote["change_percent"] < 3
        or metrics["price_vs_open_percent"] < -2
        or (price_vs_auction is not None and price_vs_auction < -2)
        or book["imbalance"] <= -0.25
        or (main_ratio is not None and main_ratio <= -4)
        or result["regulatory_risk"]["level"] == "high"
    )
    if event_high:
        decision, tone = "并购重组风险剔除", "reject"
        summary = "命中并购重组或重组终止等高风险事件；即使盘中走强也不进入打板推荐。"
    elif sealed and live_entry_allowed:
        decision, tone = "09:30补选封板 · 可排队打板", "confirm"
        summary = "开盘主动转强并已封住涨停，完成盘中补选确认；只能按涨停价排队，开板成交需确认快速回封。"
    elif sealed and auction_tradable:
        decision, tone = "封板确认 · 已持有可观察", "confirm"
        summary = "盘中已经封住涨停，实际走势确认竞价逻辑；未持仓不追高，仍需防后续炸板。"
    elif sealed and board_entry_allowed:
        decision, tone = "C级一字板 · 推荐挂单打板", "confirm"
        summary = "高辨识度一字板仍封住涨停，可按涨停价排队；挂单不等于成交，若开板成交则需确认快速回封。"
    elif sealed:
        decision, tone = "一字封板 · 排队难成交", "watch"
        summary = "走势很强但竞价阶段已接近一字，真实可成交性较低；不要把排队等同于已经买入。"
    elif failed_board and quote["change_percent"] >= 8.5 and book["imbalance"] >= 0.15:
        decision, tone = "炸板回封观察 · 暂不追", "watch"
        summary = "盘中已经炸板，但价格仍接近涨停且买盘占优；只有重新封板后才恢复确认。"
    elif failed_board:
        decision, tone = "炸板转弱 · 放弃追入", "reject"
        summary = "盘中触及涨停后未能封住，封板稳定性已经破坏09:25接力预期。"
    elif near_limit_failure:
        decision, tone = "冲板未封 · 放弃追入", "reject"
        summary = "盘中一度逼近涨停但始终未封住，随后已从高点明显回落；不把冲板动作误判为封板确认。"
    elif nuclear_reject:
        decision, tone = "反核承接失败 · 放弃追入", "reject"
        summary = "高开后的价格回落、盘口卖压或资金流已经破坏反核按钮条件，不能仅因公式曾命中而继续追入。"
    elif nuclear_mode and score >= 65 and passed >= 5 and (price_vs_auction is None or price_vs_auction >= 0):
        decision, tone = "反核承接确认 · 小仓观察", "confirm"
        summary = "9:25五项条件命中后，盘中价格未跌破竞价支撑且承接尚可；仍需人工确认市场冰点和题材辨识度。"
    elif nuclear_mode:
        decision, tone = "反核按钮观察 · 暂不追价", "watch"
        summary = "公式条件曾命中，但盘中主动买盘和承接确认不足，等待强势拉升或封板，不提前追高。"
    elif late_final_seal_watch:
        decision, tone = "晚封连板 · 等待实际封板", "watch"
        summary = "昨日最终封板不早于11:30，只保留B级观察；今日未实际封住涨停前不得升级为买入信号。"
    elif opening_dip and rebound_confirmed and score >= 58 and passed >= 4 and not structural_break:
        decision, tone = "深回踩修复 · 小仓试错", "confirm"
        summary = "开盘曾深度回踩，但已从低点明显反弹并收复竞价支撑；只在回踩不再创新低时小仓试错，不在急拉段加仓。"
    elif opening_dip and rebound_started and not structural_break:
        decision, tone = "回踩修复中 · 观察仓", "watch"
        summary = "价格已从日内低点修复并收复昨收，但尚未完全收复竞价价；可继续观察承接，不把第一次反弹当成确认。"
    elif opening_dip and not failed_board:
        decision, tone = "深回踩待止跌 · 当前不买", "reject" if structural_break else "watch"
        summary = "竞价候选正在经历开盘深回踩；当前不接下跌过程，等待从低点反弹并先后收复昨收、竞价价后再动态升级。"
    elif hard_reject or score < 55 or passed < 4:
        decision, tone = "放弃买入", "reject"
        summary = "开盘后的价格、承接、盘口或资金至少一项明显破坏09:25逻辑。"
    elif score >= 72 and passed >= 6 and (main_ratio is None or main_ratio > -1):
        decision, tone = "开盘确认 · 小仓试错", "confirm"
        summary = "冻结候选的开盘量价与承接仍成立；仅表示条件确认，不保证次日涨停。"
    else:
        decision, tone = "继续观察 · 暂不追价", "watch"
        summary = "尚未出现明确破坏，但确认项不足，等待成交与买盘进一步稳定。"

    if event_high:
        entry_advice = "未持有：并购重组风险硬剔除，不参与打板"
        holding_advice = "已持有：按事件风险和既定止盈止损纪律处理，不据此加仓"
    elif sealed and entry_authorized:
        entry_advice = "未持有：可按涨停价挂单排队打板；未成交不追改价，开板成交需确认快速回封"
        holding_advice = "已持有：继续观察封单；开板后不能快速回封则降低次日预期"
    elif sealed:
        entry_advice = "未持有：已经封板，不追价、不把排队视为成交"
        holding_advice = "已持有：继续观察封单；炸板后不能快速回封则降低次日预期"
    elif failed_board or near_limit_failure:
        entry_advice = "未持有：当前不买，等待重新封板并确认承接"
        holding_advice = "已持有：停止加仓，观察能否快速回封"
    elif late_final_seal_watch:
        entry_advice = "未持有：只观察，未实际封板前不买入"
        holding_advice = "已持有：不加仓；冲板不封或回落扩大时降低预期"
    elif tone == "confirm":
        size_text = "仅极小观察仓" if candidate.get("high_exhaustion") or result["regulatory_risk"]["level"] != "normal" else "小仓试错"
        entry_advice = f"未持有：{size_text}；回落再破昨收或日内低点则取消"
        holding_advice = "已持有：继续持有观察，不在快速拉升段追高加仓"
    elif rebound_started:
        entry_advice = "未持有：先观察，收复竞价价且盘口不转弱后再考虑小仓"
        holding_advice = "已持有：可继续观察，但在竞价价下方不加仓"
    elif opening_dip:
        entry_advice = "未持有：不接下跌，等待止跌与修复信号"
        holding_advice = "已持有：停止加仓；持续创新低或卖盘扩大则控制风险"
    elif tone == "reject":
        entry_advice = "未持有：当前不买，等待下一轮20秒刷新重新确认"
        holding_advice = "已持有：停止加仓，按预设风险位处理"
    else:
        entry_advice = "未持有：继续观察，不提前追价"
        holding_advice = "已持有：持有观察，确认前不加仓"

    recovery_label = (
        "深回踩后完全收复" if rebound_confirmed else
        "深回踩修复中" if rebound_started else
        "深回踩未止跌" if opening_dip else
        "常规开盘结构"
    )
    payload = {
        "code": candidate.get("code"), "name": candidate.get("name"),
        "quote_time": quote.get("quote_time"), "quote_provider": quote.get("quote_source"),
        "bid1_price": quote.get("bid1_price"), "bid1_volume": quote.get("bid1_volume"),
        "book_available": quote.get("book_available", False),
        "priority_tier": candidate.get("priority_tier"),
        "consecutive_limit_up_days": candidate.get("consecutive_limit_up_days"),
        "early_final_seal_chain_matched": candidate.get("early_final_seal_chain_matched"),
        "previous_day_limit_up": candidate.get("previous_day_limit_up"),
        "previous_limit_up_breaks": candidate.get("previous_limit_up_breaks"),
        "previous_final_seal_time": candidate.get("previous_final_seal_time"),
        "previous_turnover_rate": candidate.get("previous_turnover_rate"),
        "auction_volume_percent": candidate.get("auction_volume_percent"),
        "listed_sessions": candidate.get("listed_sessions"),
        "float_market_cap": candidate.get("float_market_cap"),
        "risk_veto": candidate.get("risk_veto", False),
        "auction_tradable": auction_tradable,
        "tradable": live_tradable,
        "opened_one_price_reseal": opened_one_price_reseal,
        "regulatory_risk": result.get("regulatory_risk"),
        "high_turnover_chain_matched": candidate.get("high_turnover_chain_matched", False),
        "auction_gap_percent": candidate.get("auction_gap_percent"),
        "auction_amount": candidate.get("auction_amount"),
        "auction_turnover_percent": candidate.get("auction_turnover_percent"),
        "bid_volume5": book.get("bid_volume5", 0),
        "ask_volume5": book.get("ask_volume5"),
        "strategy_mode": candidate.get("strategy_mode"),
        "board_stage_label": candidate.get("board_stage_label"),
        "corporate_event_risk": candidate.get("corporate_event_risk"),
        "corporate_event_checked": candidate.get("corporate_event_checked", False),
        "continuation_score": candidate.get("continuation_score"),
        "three_day_change_percent": candidate.get("three_day_change_percent"),
        "five_day_change_percent": candidate.get("five_day_change_percent"),
        "ten_day_change_percent": candidate.get("ten_day_change_percent"),
        "auction_action": candidate.get("action"),
        "board_entry_allowed": entry_authorized,
        "recommendation_badge": candidate.get("recommendation_badge"),
        "discovery_source": candidate.get("discovery_source") or "09:25冻结候选",
        "discovered_at": candidate.get("discovered_at"),
        "auction_rank": int(_number(candidate.get("_auction_rank"), 0)),
        "decision": decision, "tone": tone, "open_score": score,
        "passed": passed, "known_total": known, "checks": checks, "summary": summary,
        "entry_advice": entry_advice, "holding_advice": holding_advice,
        "recovery_label": recovery_label, "opening_dip": opening_dip,
        "rebound_started": rebound_started, "rebound_confirmed": rebound_confirmed,
        "price": price, "open_price": quote["open_price"], "auction_price": auction_price or None,
        "low_price": low_price or None, "low_vs_open_percent": round(low_vs_open, 2),
        "rebound_from_low_percent": round(rebound_from_low, 2),
        "reclaimed_previous_close": reclaimed_previous_close, "reclaimed_auction": reclaimed_auction,
        "change_percent": quote["change_percent"],
        "price_vs_open_percent": metrics["price_vs_open_percent"],
        "price_vs_auction_percent": round(price_vs_auction, 2) if price_vs_auction is not None else None,
        "volume_ratio": metrics["volume_ratio"], "turnover_rate": metrics["turnover_rate"],
        "limit_up_price": limit_up_price or None, "touched_limit_up": touched_limit_up,
        "sealed": sealed, "failed_board": failed_board,
        "near_limit_attempt": near_limit_attempt, "near_limit_failure": near_limit_failure,
        "late_final_seal_watch": late_final_seal_watch,
        "amount": current_amount, "order_imbalance": book["imbalance"], "order_signal": book["signal"],
        "funds": {
            "available": main_ratio is not None, "main_ratio": main_ratio,
            "main_net": _number(funds.get("main_net")) if funds else None,
            "date": funds.get("date") if funds else None,
            "source": funds.get("source") if funds else None,
            "retrieved_at": funds.get("updated_at") if funds else None,
            "source_time": funds.get("source_time") if funds else None,
            "label": "当日资金" if main_ratio is not None else "资金待确认",
        },
    }
    payload["entry_plan"] = live_entry_plan(payload)
    return payload


def _check_one(candidate: dict, provider: EastmoneyProvider, live_snapshot: dict | None = None) -> dict:
    code = str(candidate.get("code") or "")
    try:
        quote = provider.quote(code)
        quote_source = "逐股实时行情"
    except Exception:
        if not live_snapshot or _number(live_snapshot.get("f2")) <= 0:
            raise
        quote = _quote_from_snapshot(live_snapshot)
        quote_source = "全市场批量行情降级"
    result = evaluate(quote, _live_history(provider, code))
    try:
        funds = individual_fund_flow(code)
    except Exception:
        funds = None
    confirmation = _open_confirmation(candidate, result, funds)
    confirmation["quote_source"] = quote_source
    return confirmation


def build_open_guard(
    snapshot: dict, provider: EastmoneyProvider | None = None, *, discover_live: bool = True,
    now: datetime | None = None,
) -> dict:
    provider = provider or EastmoneyProvider(timeout=8)
    injected_clock = now is not None
    now = china_time(now or datetime.now().astimezone())
    discovery_key = now.date().isoformat()
    current_day_snapshot = snapshot.get("selected_date") == discovery_key and not snapshot.get("historical")
    opening_window = _opening_discovery_window(now)
    discovery_window = intraday_selection_window(now)
    live_snapshots: list[dict] = []
    live_snapshot_error = None
    if discover_live:
        try:
            live_snapshots = main_board_snapshots(cache_seconds=15)
        except Exception as exc:
            live_snapshot_error = str(exc)
    live_by_code = {str(item.get("f12") or ""): item for item in live_snapshots}
    candidates = [
        {**candidate, "_auction_rank": index}
        for index, candidate in enumerate((snapshot.get("candidates") or []) + (snapshot.get("watch_candidates") or []), start=1)
    ]
    focus_error = None
    try:
        existing_codes = {str(item.get("code")) for item in candidates}
        candidates.extend(item for item in (DAILY_FOCUS_STORE.monitored_candidates(now) if current_day_snapshot else [])
                          if str(item.get("code")) not in existing_codes)
    except DailyFocusError as exc:
        focus_error = str(exc)
    discovery_errors: list[dict] = []
    if discover_live and discovery_window:
        opening_cached = {
            str(item.get("code") or ""): item
            for item in _OPENING_DISCOVERY_CACHE.get(discovery_key, [])
        }
        newly_discovered: list[dict] = []
        discovery_snapshots = live_snapshots
        if not opening_window:
            # 9:35后只补入已逼近封板的标的，发现时间独立记录，不倒写竞价快照。
            discovery_snapshots = [row for row in live_snapshots if _number(row.get("f3")) >= 9]
        try:
            newly_discovered.extend(_discover_live_one_to_two(
                provider, {
                    str(candidate.get("code") or "") for candidate in candidates
                } | set(opening_cached),
                snapshots=discovery_snapshots,
            ) if discovery_snapshots else [])
        except Exception as exc:
            discovery_errors.append({"code": "", "name": "盘中一进二扫描", "error": str(exc)})
        try:
            newly_discovered.extend(_discover_live_multi_board(
                provider, {
                    str(candidate.get("code") or "")
                    for candidate in candidates + newly_discovered
                } | set(opening_cached),
                snapshots=discovery_snapshots,
            ) if discovery_snapshots else [])
        except Exception as exc:
            discovery_errors.append({"code": "", "name": "盘中连板扫描", "error": str(exc)})
        cached_by_code = dict(opening_cached)
        for item in newly_discovered:
            item["discovered_at"] = now.isoformat(timespec="seconds")
            if not opening_window:
                item["discovery_source"] = "盘中封板补选"
                item["action"] = "盘中新增观察 · 非09:25候选"
            cached_by_code.setdefault(str(item.get("code") or ""), item)
        _OPENING_DISCOVERY_CACHE.clear()
        _OPENING_DISCOVERY_CACHE[discovery_key] = list(cached_by_code.values())[-24:]
        frozen_codes = {str(candidate.get("code") or "") for candidate in candidates}
        candidates.extend(
            item for item in _OPENING_DISCOVERY_CACHE[discovery_key] if str(item.get("code") or "") not in frozen_codes
        )
    elif discover_live:
        existing_codes = {str(candidate.get("code") or "") for candidate in candidates}
        candidates.extend(
            {**item} for item in _OPENING_DISCOVERY_CACHE.get(discovery_key, [])
            if str(item.get("code") or "") not in existing_codes
        )
    if candidates:
        attach_corporate_event_risks(candidates)
    rows, errors = [], []
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(candidates)))) as executor:
        futures = {
            executor.submit(_check_one, candidate, provider, live_by_code.get(str(candidate.get("code") or ""))): candidate
            for candidate in candidates
        }
        for future in as_completed(futures):
            candidate = futures[future]
            try:
                rows.append(future.result())
            except Exception as exc:
                errors.append({"code": candidate.get("code"), "name": candidate.get("name"), "error": str(exc)})
    def live_rank(item: dict) -> tuple:
        event_clear = (item.get("corporate_event_risk") or {}).get("level") != "high"
        state_rank = (
            5 if item.get("sealed") and item.get("tone") == "confirm" else
            4 if item.get("tone") == "confirm" else
            3 if item.get("sealed") else
            2 if item.get("tone") == "watch" else 0
        )
        return event_clear, state_rank, item.get("open_score", 0), item.get("change_percent", 0), -item.get("auction_rank", 999)
    rows.sort(key=live_rank, reverse=True)
    for index, item in enumerate(rows, start=1):
        item["live_rank"] = index
        item["rank_change"] = item.get("auction_rank", index) - index
    # 使用整轮数据收集结束的时间检验新鲜度，不能把请求开始时间当行情时间。
    if not injected_clock:
        now = china_time(datetime.now().astimezone())
    market_context = _opening_market_context(live_snapshots, now, len(candidates), snapshot.get("market") or {})
    decision_snapshot = {**snapshot, "market": market_context}
    research_rows = copy.deepcopy(rows)
    for error in errors:
        research_rows.append({"code": error.get("code"), "name": error.get("name"), "quote_error": error.get("error")})
    with _SELECTION_LOCK:
        selection_key = f"{discovery_key}:{discover_live}"
        for old_key in list(_SELECTION_OBSERVATIONS):
            if not old_key.startswith(discovery_key + ":"):
                _SELECTION_OBSERVATIONS.pop(old_key, None)
        recommendations = select_live_recommendations(
            rows, now, market_context.get("state", "未知")
            if snapshot.get("selected_date") == discovery_key and not snapshot.get("historical") else "未知",
            _SELECTION_OBSERVATIONS.setdefault(selection_key, {}),
        )
        try:
            if current_day_snapshot:
                recommendations, daily_focus = DAILY_FOCUS_STORE.select(rows, now)
            else:
                recommendations = []
                daily_focus = {"available": False, "status": "historical", "message": "历史快照不产生当日首选", "issued": []}
        except DailyFocusError as exc:
            recommendations = []
            daily_focus = {"available": False, "status": "storage_error", "message": str(exc),
                           "issued_count": None, "daily_limit": MAX_BOARD_PICKS, "issued": []}
            for item in rows:
                item.update(primary_pick=False, recommended=False, actionable=False, execution_ready=False,
                            board_entry_allowed=False, recommendation_rank=None)
                item["selection_reason"] = str(exc)
                if item.get("tone") != "reject":
                    item.update(tone="watch", decision="每日提示记录待恢复 · 暂停首选")
        research = BOARD_RESEARCH_STORE.record(research_rows, now, snapshot=decision_snapshot, baseline_rows=rows)
    for item in rows:
        item["entry_plan"] = live_entry_plan(item)
        item["entry_advice"] = f"未持有：{item['entry_plan']['timing']}"
        item["summary"] = item["selection_reason"]
    errors.extend(discovery_errors)
    if focus_error:
        errors.append({"code": "", "name": "每日首选记录", "error": focus_error})
    if live_snapshot_error:
        errors.append({"code": "", "name": "全市场批量行情", "error": live_snapshot_error})
    if not rows and errors:
        raise MarketDataError("冻结候选的实时行情暂不可用")
    return {
        "selected_date": snapshot.get("selected_date"),
        "strategy_version": BOARD_STRATEGY_VERSION,
        "auction_seed_strategy_version": snapshot.get("strategy_version"),
        "historical": bool(snapshot.get("historical") or snapshot.get("selected_date") != discovery_key),
        "snapshot_label": snapshot.get("snapshot_label"),
        "market": market_context,
        "scope": "full_market" if discover_live else "frozen_candidates",
        "generated_at": now.astimezone().isoformat(timespec="seconds"),
        "opening_discovery_status": (
            "盘中动态补选中；09:35后仅发现封板附近标的" if discover_live and discovery_window else
            "非交易扫描时段；仅复核留存观察池" if discover_live else
            "仅复核09:25冻结名单"
        ),
        "candidates": recommendations, "errors": errors,
        "daily_focus": daily_focus,
        "research": research,
        "watch_candidates": [item for item in rows if not item.get("recommended")],
        "monitored_count": len(rows), "recommendation_limit": MAX_BOARD_PICKS,
        "confirmed_count": len(recommendations),
        "watch_count": sum(item["tone"] == "watch" for item in rows),
        "rejected_count": sum(item["tone"] == "reject" for item in rows),
        "method": (
            "09:25只建观察池；盘中仅提示一个首选，条件有效时不换票，全天累计最多5只不同股票。历史延续45%、盘中确认30%、资金最多15分、盘口最多6分、封单最多4分，风险扣分；首选潜力分≥80且历史延续≥75。实际封板要求买一封单≥3000万元，且占成交额≥5%或占流通市值≥0.5%；锁定后只看风险，不再新增买点。"
            if discover_live else
            "轻量模式只复核09:25冻结候选，不扫描全市场、不补入盘中一进二，用于策略执行页降低网络占用。"
        ),
        "disclaimer": "开盘确认只用于纪律化复核，不保证次日涨停。09:30附近波动剧烈，一字板可能无法成交，A股T+1买入后当日无法卖出。",
    }
