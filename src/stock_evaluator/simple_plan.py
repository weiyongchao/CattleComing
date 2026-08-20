from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from .evaluator import evaluate
from .funds import individual_fund_flow
from .market import EastmoneyProvider
from .screener import is_main_board, sector_context


def _fund_snapshot(code: str) -> dict:
    try:
        data = individual_fund_flow(code)
        signal = data["combined_signal"]
        is_today = bool(data["is_today"])
        return {
            "available": True, "current": is_today, "score": float(signal["score"]),
            "label": f"{'当日' if is_today else '上一交易日'}{signal['label']}",
            "main_net": float(data["main_net"]), "main_ratio": float(data["main_ratio"]),
            "date": data["date"], "is_today": is_today, "source": data["source"],
        }
    except Exception as exc:
        return {"available": False, "score": None, "label": "资金待确认", "error": str(exc)}


def _position_action(result: dict, funds: dict, cost_price: float, shares: int) -> dict:
    quote, metrics = result["quote"], result["metrics"]
    price_plan, book = result["price_plan"], result["order_book"]
    price, score = float(quote["price"]), int(result["score"])
    fund_score = funds.get("score") if funds.get("available") and funds.get("current") else None
    held = shares > 0 or cost_price > 0
    profit_percent = round((price / cost_price - 1) * 100, 2) if cost_price > 0 else None
    weak_funds = fund_score is not None and fund_score <= -20
    strong_funds = fund_score is not None and fund_score >= 15
    reduce_trigger = price_plan["reduce"]["enabled"] and price <= price_plan["reduce"]["price"]

    if held:
        if score < 45 and (weak_funds or reduce_trigger):
            action, level, tone = "减仓", "技术转弱且资金/价格触发风险条件", "negative"
        elif score >= 68 and strong_funds and book["imbalance"] >= 0 and metrics["price_vs_ma5_percent"] > 0:
            action, level, tone = "可加仓", "趋势、资金和买盘同时确认", "positive"
        elif score >= 50 and not weak_funds:
            action, level, tone = "持有", "趋势尚可，等待加仓条件完整出现", "neutral"
        else:
            action, level, tone = "控制仓位", "资金或趋势尚未确认，不追加风险", "negative"
    else:
        if score >= 68 and strong_funds and book["imbalance"] >= 0 and metrics["price_vs_ma5_percent"] > 0:
            action, level, tone = "可建仓", "技术、资金和盘口共同确认", "positive"
        else:
            action, level, tone = "等待", "尚未同时满足技术、资金与买盘条件", "neutral"

    return {
        "action": action, "tone": tone, "reason": level, "held": held,
        "cost_price": cost_price or None, "shares": shares, "profit_percent": profit_percent,
        "market_value": round(price * shares, 2) if shares else 0,
        "build": price_plan["build"], "add": price_plan["add"],
        "reduce": price_plan["reduce"], "exit": price_plan["exit"],
    }


def build_position_summary(code: str, cost_price: float = 0, shares: int = 0) -> dict:
    normalized = "".join(character for character in str(code) if character.isdigit())[-6:]
    if not is_main_board(normalized):
        raise ValueError("今日决策仅支持沪深主板，不推荐创业板、科创板或北交所股票")
    code = normalized
    provider = EastmoneyProvider(timeout=8)
    quote = provider.quote(code)
    history = provider.history(code)
    context = sector_context(code, provider)
    result = evaluate(quote, history, context)
    funds = _fund_snapshot(code)
    action = _position_action(result, funds, cost_price, shares)
    return {
        "quote": result["quote"], "score": result["score"], "rating": result["rating"],
        "metrics": result["metrics"], "order_book": result["order_book"],
        "sector": result["sector_context"], "funds": funds, "decision": action,
        "risk_points": result["operation"]["risk_points"], "summary": result["summary"],
    }


def _enrich_intraday(candidates: list[dict], market: dict, limit: int = 5) -> list[dict]:
    selected = candidates[:limit]
    funds_by_code: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(5, max(1, len(selected)))) as executor:
        futures = {executor.submit(_fund_snapshot, item["code"]): item["code"] for item in selected}
        for future in as_completed(futures):
            funds_by_code[futures[future]] = future.result()
    enriched = []
    for item in selected:
        funds = funds_by_code.get(item["code"], {"available": False, "score": None, "label": "资金待确认"})
        fund_score = funds.get("score") if funds.get("available") and funds.get("current") else None
        if market["state"] == "空仓":
            action, reason = "等待", "市场总开关为空仓"
        elif fund_score is None:
            action, reason = "等待", "当日资金流尚未确认"
        elif item["score"] >= 75 and item["leadership_score"] >= 60 and fund_score >= 15 and item["order_imbalance"] >= 0:
            action, reason = "可建仓", "龙头、量价、资金和买盘同时确认"
        elif item["score"] >= 70 and fund_score >= 0:
            action, reason = "观察建仓", "技术较强，等待资金或买盘进一步确认"
        else:
            action, reason = "等待", "资金或买卖盘未达到建仓条件"
        enriched.append({**item, "funds": funds, "simple_action": action, "simple_reason": reason})
    return enriched


def build_simple_plan(board_plan: dict, intraday_plan: dict, code: str, cost_price: float = 0, shares: int = 0) -> dict:
    position = build_position_summary(code, cost_price, shares)
    intraday = _enrich_intraday(intraday_plan["candidates"], intraday_plan["market"], limit=5)
    now = datetime.now().astimezone()
    hhmm = now.hour * 100 + now.minute
    stage = "竞价前" if hhmm < 925 else "竞价决策" if hhmm < 930 else "盘中决策" if hhmm < 1500 else "收盘复盘"
    return {
        "stage": stage, "generated_at": now.isoformat(timespec="seconds"),
        "auction": {
            "gate": board_plan["market"], "candidates": board_plan["candidates"][:3],
            "position_plan": board_plan["position_plan"],
        },
        "intraday": {
            "market": intraday_plan["market"], "candidates": intraday,
            "position_plan": intraday_plan["position_plan"],
        },
        "position": position,
        "data_rules": [
            "所有自动候选仅限沪深主板，排除创业板、科创板、北交所和ST股票",
            "竞价候选先扫描全主板成交与主力快照，再结合前序25日K线与09:25最终竞价",
            "盘中候选使用实时成交、量比、换手、MA5、行业与五档买卖盘",
            "建仓/加仓必须有当日资金流确认；资金不可用时自动降级为等待",
            "减仓由技术转弱、资金流出和价格风险位共同确认",
        ],
        "disclaimer": "研究型决策支持，不自动下单、不保证收益。五档挂单可撤销，逐笔资金为公开成交方向推算，不能视为真实暗盘。",
    }
