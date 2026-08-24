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
    touched_limit_up = bool(limit_up_price and high_price >= limit_up_price - 0.005)
    sealed = bool(limit_up_price and price >= limit_up_price - 0.005 and quote["change_percent"] >= 9.5)
    failed_board = touched_limit_up and not sealed
    auction_tradable = candidate.get("tradable", True)
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
    hard_reject = (
        quote["change_percent"] < 0
        or metrics["price_vs_open_percent"] < -2
        or (price_vs_auction is not None and price_vs_auction < -3)
        or book["imbalance"] <= -0.35
        or (main_ratio is not None and main_ratio <= -5)
        or result["regulatory_risk"]["level"] == "high"
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
    elif hard_reject or score < 55 or passed < 4:
        decision, tone = "放弃买入", "reject"
        summary = "开盘后的价格、承接、盘口或资金至少一项明显破坏09:25逻辑。"
    elif score >= 72 and passed >= 6 and (main_ratio is None or main_ratio > -1):
        decision, tone = "开盘确认 · 小仓试错", "confirm"
        summary = "冻结候选的开盘量价与承接仍成立；仅表示条件确认，不保证次日涨停。"
    else:
        decision, tone = "继续观察 · 暂不追价", "watch"
        summary = "尚未出现明确破坏，但确认项不足，等待成交与买盘进一步稳定。"
    return {
        "code": candidate.get("code"), "name": candidate.get("name"),
        "priority_tier": candidate.get("priority_tier"),
        "strategy_mode": candidate.get("strategy_mode"),
        "board_stage_label": candidate.get("board_stage_label"),
        "auction_action": candidate.get("action"),
        "auction_rank": int(_number(candidate.get("_auction_rank"), 0)),
        "decision": decision, "tone": tone, "open_score": score,
        "passed": passed, "known_total": known, "checks": checks, "summary": summary,
        "price": price, "open_price": quote["open_price"], "auction_price": auction_price or None,
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


def _check_one(candidate: dict, provider: EastmoneyProvider) -> dict:
    code = str(candidate.get("code") or "")
    result = evaluate(provider.quote(code), provider.history(code))
    try:
        funds = individual_fund_flow(code)
    except Exception:
        funds = None
    return _open_confirmation(candidate, result, funds)


def build_open_guard(snapshot: dict, provider: EastmoneyProvider | None = None) -> dict:
    provider = provider or EastmoneyProvider(timeout=8)
    candidates = [
        {**candidate, "_auction_rank": index}
        for index, candidate in enumerate(snapshot.get("candidates") or [], start=1)
    ]
    rows, errors = [], []
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(candidates)))) as executor:
        futures = {executor.submit(_check_one, candidate, provider): candidate for candidate in candidates}
        for future in as_completed(futures):
            candidate = futures[future]
            try:
                rows.append(future.result())
            except Exception as exc:
                errors.append({"code": candidate.get("code"), "name": candidate.get("name"), "error": str(exc)})
    def live_rank(item: dict) -> tuple:
        state_rank = (
            5 if item.get("sealed") and item.get("tone") == "confirm" else
            4 if item.get("tone") == "confirm" else
            3 if item.get("sealed") else
            2 if item.get("tone") == "watch" else 0
        )
        return state_rank, item.get("open_score", 0), item.get("change_percent", 0), -item.get("auction_rank", 999)
    rows.sort(key=live_rank, reverse=True)
    for index, item in enumerate(rows, start=1):
        item["live_rank"] = index
        item["rank_change"] = item.get("auction_rank", index) - index
    if not rows and errors:
        raise MarketDataError("冻结候选的实时行情暂不可用")
    return {
        "selected_date": snapshot.get("selected_date"),
        "snapshot_label": snapshot.get("snapshot_label"),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "candidates": rows, "errors": errors,
        "confirmed_count": sum(item["tone"] == "confirm" for item in rows),
        "watch_count": sum(item["tone"] == "watch" for item in rows),
        "rejected_count": sum(item["tone"] == "reject" for item in rows),
        "method": "09:25原始名单固定不变；09:30后每20秒更新实时价格、封板/炸板、成交额、五档盘口、MA5与当日资金，并动态升降级和重排。",
        "disclaimer": "开盘确认只用于纪律化复核，不保证次日涨停。09:30附近波动剧烈，一字板可能无法成交，A股T+1买入后当日无法卖出。",
    }
