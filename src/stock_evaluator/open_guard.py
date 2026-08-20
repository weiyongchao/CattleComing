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
    score = min(100, round(base * 0.35 + passed * 9 + (5 if book["imbalance"] >= 0.15 else 0)))
    hard_reject = (
        quote["change_percent"] < 0
        or metrics["price_vs_open_percent"] < -2
        or (price_vs_auction is not None and price_vs_auction < -3)
        or book["imbalance"] <= -0.35
        or (main_ratio is not None and main_ratio <= -5)
        or result["regulatory_risk"]["level"] == "high"
    )
    sealed_or_unbuyable = quote["change_percent"] >= 9.5
    if sealed_or_unbuyable:
        decision, tone = "涨停排队 · 不追无法成交", "watch"
        summary = "走势很强，但需要先确认是否存在真实可成交量；不要把一字板排队等同于已买入。"
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
        "board_stage_label": candidate.get("board_stage_label"),
        "decision": decision, "tone": tone, "open_score": score,
        "passed": passed, "known_total": known, "checks": checks, "summary": summary,
        "price": price, "open_price": quote["open_price"], "auction_price": auction_price or None,
        "change_percent": quote["change_percent"],
        "price_vs_open_percent": metrics["price_vs_open_percent"],
        "price_vs_auction_percent": round(price_vs_auction, 2) if price_vs_auction is not None else None,
        "volume_ratio": metrics["volume_ratio"], "turnover_rate": metrics["turnover_rate"],
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
    candidates = snapshot.get("candidates") or []
    rows, errors = [], []
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(candidates)))) as executor:
        futures = {executor.submit(_check_one, candidate, provider): candidate for candidate in candidates}
        for future in as_completed(futures):
            candidate = futures[future]
            try:
                rows.append(future.result())
            except Exception as exc:
                errors.append({"code": candidate.get("code"), "name": candidate.get("name"), "error": str(exc)})
    order = {str(item.get("code")): index for index, item in enumerate(candidates)}
    rows.sort(key=lambda item: order.get(str(item.get("code")), 999))
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
        "method": "09:25名单固定不变；09:30后只更新实时价格、成交额、五档盘口、MA5、当日资金与异动风险，判断原竞价逻辑是否仍成立。",
        "disclaimer": "开盘确认只用于纪律化复核，不保证次日涨停。09:30附近波动剧烈，一字板可能无法成交，A股T+1买入后当日无法卖出。",
    }
