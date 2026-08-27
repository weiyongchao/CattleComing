from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

from .auction import _auction_candidate, _live_history, _theme_bucket
from .corporate_events import attach_corporate_event_risks
from .evaluator import evaluate
from .funds import individual_fund_flow
from .market import EastmoneyProvider, MarketDataError, Quote
from .trade_advice import live_entry_plan
from .universe import main_board_snapshots, previous_limit_up_pool


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
    intraday_position = (price - low_price) / (high_price - low_price) * 100 if high_price > low_price else 100 if price > 0 else 0
    main_ratio = _number(snapshot.get("f184"))
    support_confirmed = theme_peer_count >= 1 or main_ratio > 0
    late_breakout_override = change >= 7 and price_vs_open >= 4 and amount >= 200_000_000
    return (
        3 <= change <= 10.2 and price_vs_open >= 2
        and amount >= 100_000_000 and volume_ratio >= 0.5
        and intraday_position >= 70
        and (support_confirmed or late_breakout_override)
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
    auction_tradable = candidate.get("tradable", True)
    board_entry_allowed = bool(candidate.get("board_entry_allowed"))
    nuclear_mode = candidate.get("strategy_mode") == "反核按钮竞价抄底"
    fund_current = bool(funds and funds.get("is_today") and str(funds.get("date")) == date.today().isoformat())
    main_ratio = _number(funds.get("main_ratio")) if fund_current and funds else None
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
    ]
    passed = sum(check["passed"] is True for check in checks)
    known = sum(check["passed"] is not None for check in checks)
    base = _number(candidate.get("continuation_score"), _number(candidate.get("selection_score"), _number(candidate.get("score"))))
    score = round(base * 0.35 + passed * 9 + (5 if book["imbalance"] >= 0.15 else 0))
    if sealed:
        score += 18
    elif failed_board:
        score -= 28
    if price_vs_auction is not None:
        score += round(max(-12, min(12, price_vs_auction * 2)))
    score = max(0, min(100, score))
    structural_break = (
        book["imbalance"] <= -0.35
        or (main_ratio is not None and main_ratio <= -5)
        or result["regulatory_risk"]["level"] == "high"
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
    if sealed and auction_tradable:
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
    elif nuclear_reject:
        decision, tone = "反核承接失败 · 放弃追入", "reject"
        summary = "高开后的价格回落、盘口卖压或资金流已经破坏反核按钮条件，不能仅因公式曾命中而继续追入。"
    elif nuclear_mode and score >= 65 and passed >= 5 and (price_vs_auction is None or price_vs_auction >= 0):
        decision, tone = "反核承接确认 · 小仓观察", "confirm"
        summary = "9:25五项条件命中后，盘中价格未跌破竞价支撑且承接尚可；仍需人工确认市场冰点和题材辨识度。"
    elif nuclear_mode:
        decision, tone = "反核按钮观察 · 暂不追价", "watch"
        summary = "公式条件曾命中，但盘中主动买盘和承接确认不足，等待强势拉升或封板，不提前追高。"
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

    if sealed and board_entry_allowed:
        entry_advice = "未持有：可按涨停价挂单排队打板；未成交不追改价，开板成交需确认快速回封"
        holding_advice = "已持有：继续观察封单；开板后不能快速回封则降低次日预期"
    elif sealed:
        entry_advice = "未持有：已经封板，不追价、不把排队视为成交"
        holding_advice = "已持有：继续观察封单；炸板后不能快速回封则降低次日预期"
    elif failed_board:
        entry_advice = "未持有：当前不买，等待重新封板并确认承接"
        holding_advice = "已持有：停止加仓，观察能否快速回封"
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
        "priority_tier": candidate.get("priority_tier"),
        "strategy_mode": candidate.get("strategy_mode"),
        "board_stage_label": candidate.get("board_stage_label"),
        "corporate_event_risk": candidate.get("corporate_event_risk"),
        "continuation_score": candidate.get("continuation_score"),
        "three_day_change_percent": candidate.get("three_day_change_percent"),
        "five_day_change_percent": candidate.get("five_day_change_percent"),
        "ten_day_change_percent": candidate.get("ten_day_change_percent"),
        "auction_action": candidate.get("action"),
        "board_entry_allowed": board_entry_allowed,
        "recommendation_badge": candidate.get("recommendation_badge"),
        "discovery_source": candidate.get("discovery_source") or "09:25冻结候选",
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
        "amount": current_amount, "order_imbalance": book["imbalance"], "order_signal": book["signal"],
        "funds": {
            "available": main_ratio is not None, "main_ratio": main_ratio,
            "main_net": _number(funds.get("main_net")) if funds else None,
            "date": funds.get("date") if funds else None,
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
) -> dict:
    provider = provider or EastmoneyProvider(timeout=8)
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
        for index, candidate in enumerate(snapshot.get("candidates") or [], start=1)
    ]
    discovery_errors: list[dict] = []
    if discover_live:
        try:
            candidates.extend(_discover_live_one_to_two(
                provider, {str(candidate.get("code") or "") for candidate in candidates},
                snapshots=live_snapshots or None,
            ))
        except Exception as exc:
            discovery_errors.append({"code": "", "name": "盘中一进二扫描", "error": str(exc)})
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
    errors.extend(discovery_errors)
    if live_snapshot_error:
        errors.append({"code": "", "name": "全市场批量行情", "error": live_snapshot_error})
    if not rows and errors:
        raise MarketDataError("冻结候选的实时行情暂不可用")
    return {
        "selected_date": snapshot.get("selected_date"),
        "snapshot_label": snapshot.get("snapshot_label"),
        "scope": "full_market" if discover_live else "frozen_candidates",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "candidates": rows, "errors": errors,
        "confirmed_count": sum(item["tone"] == "confirm" for item in rows),
        "watch_count": sum(item["tone"] == "watch" for item in rows),
        "rejected_count": sum(item["tone"] == "reject" for item in rows),
        "method": (
            "09:25原始名单固定不变；09:30后每20秒扫描全主板，并按深回踩、止跌、收复昨收、收复竞价价、封板/炸板逐级更新。未持有与已持有结论分开；同时补入昨日首板且盘中放量转强的一进二观察股。"
            if discover_live else
            "轻量模式只复核09:25冻结候选，不扫描全市场、不补入盘中一进二，用于策略执行页降低网络占用。"
        ),
        "disclaimer": "开盘确认只用于纪律化复核，不保证次日涨停。09:30附近波动剧烈，一字板可能无法成交，A股T+1买入后当日无法卖出。",
    }
