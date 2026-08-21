from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

from .evaluator import evaluate
from .funds import individual_fund_flow
from .market import EastmoneyProvider, MarketDataError


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _holding_strategy(candidate: dict, result: dict, funds: dict | None, finalized: bool) -> dict:
    quote, metrics = result["quote"], result["metrics"]
    price, previous_close = _number(quote.get("price")), _number(quote.get("previous_close"))
    high, low = _number(quote.get("high_price")), _number(quote.get("low_price"))
    day_range = high - low
    close_position = (price - low) / day_range * 100 if day_range > 0 else 50.0
    entry_price = _number(candidate.get("auction_price"), _number(quote.get("open_price"), price))
    holding_return = (price / entry_price - 1) * 100 if entry_price else None
    touched_limit = bool(previous_close and high / previous_close >= 1.095)
    sealed = bool(touched_limit and quote["change_percent"] >= 9.5 and abs(price - high) <= 0.011)
    failed_board = touched_limit and not sealed
    fund_current = bool(funds and funds.get("is_today") and str(funds.get("date")) == date.today().isoformat())
    main_ratio = _number(funds.get("main_ratio")) if fund_current and funds else None
    target_board = int(_number(candidate.get("target_board_count")))
    downside = _number(candidate.get("t1_downside_risk_score"))
    entry_ma5_extension = _number(candidate.get("price_vs_ma5_percent"))
    high_extension = target_board >= 2 and entry_ma5_extension >= 12
    risk_reasons: list[str] = []
    if failed_board:
        risk_reasons.append("盘中触及涨停但收盘未封住，炸板已破坏接力预期")
    if close_position < 35:
        risk_reasons.append("收盘位于日内区间下部，尾盘承接偏弱")
    if metrics["price_vs_open_percent"] < -3:
        risk_reasons.append("收盘明显低于开盘价")
    if main_ratio is not None and main_ratio <= -3:
        risk_reasons.append("当日主力资金明显流出")
    if downside >= 50:
        risk_reasons.append("竞价阶段T+1下跌风险分较高")
    if high_extension:
        risk_reasons.append(f"入选时已偏离MA5 {entry_ma5_extension:.2f}%，隔夜获利盘兑现风险较高")

    if failed_board or quote["change_percent"] <= -7 or (close_position < 25 and metrics["price_vs_open_percent"] < -3):
        decision, tone = "次日优先卖出", "sell"
        reason = "当天结构已经失效，不再赌反包或次日涨停。"
    elif sealed and (target_board >= 4 or high_extension or downside >= 50 or (main_ratio is not None and main_ratio < 0)):
        decision, tone = "条件持有 · 冲高减仓", "reduce"
        reason = "虽然封板，但连板高度或风险较高，次日以兑现和保护利润为主。"
    elif sealed:
        decision, tone = "继续持有 · 观察连板", "hold"
        reason = "当天封板且收盘结构完整，保留次日连板观察资格。"
    elif quote["change_percent"] >= 2 and close_position >= 65 and metrics["price_vs_ma5_percent"] > 0 and (main_ratio is None or main_ratio > -3):
        decision, tone = "条件持有", "hold"
        reason = "未封板但趋势、收盘位置与资金未明显破坏，只允许条件持有。"
    else:
        decision, tone = "次日冲高卖出", "reduce"
        reason = "当天未形成足够强的封板或收盘确认，次日不再以涨停为默认目标。"

    return {
        "code": candidate.get("code"), "name": candidate.get("name"),
        "priority_tier": candidate.get("priority_tier"), "board_stage_label": candidate.get("board_stage_label"),
        "decision": decision, "tone": tone, "reason": reason, "risk_reasons": risk_reasons,
        "finalized": finalized, "price": price, "entry_price": entry_price or None,
        "holding_return_percent": round(holding_return, 2) if holding_return is not None else None,
        "change_percent": quote["change_percent"], "price_vs_open_percent": metrics["price_vs_open_percent"],
        "close_position_percent": round(close_position, 1), "volume_ratio": metrics["volume_ratio"],
        "turnover_rate": metrics["turnover_rate"], "sealed": sealed, "failed_board": failed_board,
        "funds": {"available": main_ratio is not None, "main_ratio": main_ratio,
                  "main_net": _number(funds.get("main_net")) if funds else None,
                  "date": funds.get("date") if funds else None},
        "next_day_plan": {
            "weak_open": "竞价低开≥3%：优先卖出；若跌停无法成交，只在打开时执行风险退出。",
            "flat_open": "低开3%以内或平开：9:35前不能收回前收盘价，卖出；禁止补仓摊薄。",
            "strong_open": "高开1%–7%：守住开盘价且买盘未转弱可观察；冲高不封板则分批卖出。",
            "extreme_open": "高开≥8.5%：只有持续封板才观察；开板或封单快速下降即兑现。",
        },
    }


def _one(candidate: dict, provider: EastmoneyProvider, finalized: bool) -> dict:
    code = str(candidate.get("code") or "")
    result = evaluate(provider.quote(code), provider.history(code))
    try:
        funds = individual_fund_flow(code)
    except Exception:
        funds = None
    return _holding_strategy(candidate, result, funds, finalized)


def build_next_day_strategy(snapshot: dict, provider: EastmoneyProvider | None = None, now: datetime | None = None) -> dict:
    provider, now = provider or EastmoneyProvider(timeout=8), now or datetime.now()
    finalized = (now.hour, now.minute) >= (15, 5)
    candidates = snapshot.get("candidates") or []
    rows, errors = [], []
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(candidates)))) as executor:
        futures = {executor.submit(_one, candidate, provider, finalized): candidate for candidate in candidates}
        for future in as_completed(futures):
            candidate = futures[future]
            try:
                rows.append(future.result())
            except Exception as exc:
                errors.append({"code": candidate.get("code"), "name": candidate.get("name"), "error": str(exc)})
    order = {str(item.get("code")): index for index, item in enumerate(candidates)}
    rows.sort(key=lambda item: order.get(str(item.get("code")), 999))
    if not rows and errors:
        raise MarketDataError("冻结候选的收盘数据暂不可用")
    return {
        "selected_date": snapshot.get("selected_date"), "finalized": finalized,
        "stage": "15:05收盘最终策略" if finalized else "14:50后收盘预案",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "candidates": rows, "errors": errors,
        "hold_count": sum(item["tone"] == "hold" for item in rows),
        "reduce_count": sum(item["tone"] == "reduce" for item in rows),
        "sell_count": sum(item["tone"] == "sell" for item in rows),
        "method": "名单沿用09:25冻结候选，根据当天封板/炸板、收盘位置、相对开盘、MA5、成交量、主力资金和原T+1下跌风险生成次日持仓预案。",
        "disclaimer": "次日策略是条件预案，不会自动下单。隔夜公告、板块消息和次日竞价可能改变结论；A股T+1及跌停会造成无法及时退出。",
    }
