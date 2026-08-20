from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from .board_plan import _market_emotion
from .evaluator import evaluate
from .market import EastmoneyProvider
from .screener import LEADER_POOL, is_risk_stock_name
from .universe import main_board_snapshots


def _number(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _intraday_score(
    base_score: float,
    change_percent: float,
    volume_ratio: float,
    turnover_rate: float,
    intraday_position: float,
    price_vs_ma5: float,
    order_imbalance: float,
    sector_change: float,
) -> int:
    score = base_score * 0.45
    score += 12 if 1 <= change_percent <= 5 else 7 if 5 < change_percent < 8 else -15 if change_percent >= 9.2 else -8 if change_percent < 0 else 3
    score += 12 if 0.8 <= volume_ratio <= 2.5 else 7 if 2.5 < volume_ratio <= 4 else -3 if volume_ratio > 5 else 2
    score += 8 if 1 <= turnover_rate <= 10 else 3 if 0.3 <= turnover_rate < 1 else -5 if turnover_rate > 15 else 0
    score += 10 if 55 <= intraday_position <= 85 else -3 if intraday_position > 92 else 2
    score += 8 if price_vs_ma5 > 0 else -8
    score += 10 if order_imbalance >= 0.15 else 4 if order_imbalance > -0.1 else -6
    score += 10 if sector_change >= 0.5 else 5 if sector_change >= 0 else -8
    return round(max(0, min(100, score)))


def _scan_one(code: str, industry: str, provider: EastmoneyProvider) -> dict | None:
    quote = provider.quote(code)
    if is_risk_stock_name(quote.name):
        return None
    result = evaluate(quote, provider.history(code))
    metrics, book = result["metrics"], result["order_book"]
    return {
        "code": code, "name": quote.name, "industry": industry, "category": industry,
        "base_score": result["score"], "price": quote.price,
        "change_percent": quote.change_percent, "volume_ratio": metrics["volume_ratio"],
        "turnover_rate": metrics["turnover_rate"],
        "intraday_position_percent": metrics["intraday_position_percent"],
        "price_vs_ma5_percent": metrics["price_vs_ma5_percent"],
        "order_imbalance": book["imbalance"], "order_signal": book["signal"],
    }


def _prefilter_snapshots(snapshots: list[dict], limit: int = 80) -> list[dict]:
    active = []
    for row in snapshots:
        change, amount = _number(row.get("f3")), _number(row.get("f6"))
        turnover, volume_ratio = _number(row.get("f8")), _number(row.get("f10"))
        market_cap = _number(row.get("f20"))
        if not (0.3 <= change < 8.5 and amount >= 50_000_000 and 0.2 <= turnover <= 15 and 0.6 <= volume_ratio <= 5 and market_cap >= 2_000_000_000):
            continue
        row = dict(row)
        row["activity_score"] = change * 2 + min(volume_ratio, 3) * 3 + min(turnover, 10) * 2 + min(amount / 100_000_000, 10)
        active.append(row)
    active.sort(key=lambda item: (item["activity_score"], float(item.get("f6") or 0)), reverse=True)
    return active[:limit]


def _attach_leadership_ranks(snapshots: list[dict]) -> None:
    industries: dict[str, list[dict]] = {}
    for row in snapshots:
        industries.setdefault(str(row.get("f100") or "未分类"), []).append(row)
    for members in industries.values():
        for rank, row in enumerate(sorted(members, key=lambda item: _number(item.get("f20")), reverse=True), 1):
            row["industry_cap_rank"] = rank
        for rank, row in enumerate(sorted(members, key=lambda item: _number(item.get("f6")), reverse=True), 1):
            row["industry_amount_rank"] = rank
        for row in members:
            row["industry_size"] = len(members)


def _leadership_profile(snapshot: dict) -> dict:
    code = str(snapshot.get("f12") or "")
    market_cap, amount = _number(snapshot.get("f20")), _number(snapshot.get("f6"))
    cap_rank = int(snapshot.get("industry_cap_rank") or 9999)
    amount_rank = int(snapshot.get("industry_amount_rank") or 9999)
    curated = code in LEADER_POOL
    score = 25 if curated else 0
    score += 35 if cap_rank == 1 else 30 if cap_rank <= 3 else 24 if cap_rank <= 5 else 14 if cap_rank <= 10 else 0
    score += 25 if amount_rank == 1 else 20 if amount_rank <= 3 else 15 if amount_rank <= 5 else 8 if amount_rank <= 10 else 0
    score += 15 if market_cap >= 50_000_000_000 else 12 if market_cap >= 20_000_000_000 else 8 if market_cap >= 10_000_000_000 else 0
    score += 10 if amount >= 500_000_000 else 7 if amount >= 200_000_000 else 4 if amount >= 100_000_000 else 0
    score = min(100, score)
    mature = (
        curated and market_cap >= 5_000_000_000 and amount >= 50_000_000
    ) or (
        score >= 55 and market_cap >= 8_000_000_000 and amount >= 100_000_000
    )
    if curated:
        label = "核心代表"
    elif cap_rank <= 3:
        label = "行业市值龙头"
    elif amount_rank <= 3:
        label = "行业交易龙头"
    else:
        label = "成熟行业候选"
    reasons = [f"{snapshot.get('f100') or '未分类'}行业分类匹配", f"行业市值第{cap_rank}名", f"行业成交额第{amount_rank}名"]
    if curated:
        reasons.append("进入项目核心代表池")
    return {
        "leadership_score": score, "leadership_qualified": mature, "leader_label": label,
        "leadership_reasons": reasons, "market_cap": market_cap, "amount": amount,
        "industry_cap_rank": cap_rank, "industry_amount_rank": amount_rank,
        "industry_size": int(snapshot.get("industry_size") or 0),
    }


def build_intraday_plan(capital: float = 100_000, limit: int = 12) -> dict:
    snapshots = main_board_snapshots()
    _attach_leadership_ranks(snapshots)
    prefiltered = _prefilter_snapshots(snapshots, limit=80)
    snapshot_by_code = {str(item.get("f12") or ""): item for item in prefiltered}
    provider, rows, failed = EastmoneyProvider(timeout=8), [], 0
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(_scan_one, str(item.get("f12")), str(item.get("f100") or "未分类"), provider): str(item.get("f12"))
            for item in prefiltered
        }
        for future in as_completed(futures):
            try:
                row = future.result()
                if row:
                    rows.append(row)
            except Exception:
                failed += 1
    industry_values: dict[str, list[float]] = {}
    for item in snapshots:
        industry = str(item.get("f100") or "未分类")
        industry_values.setdefault(industry, []).append(_number(item.get("f3")))
    industry_changes = {
        industry: round(sum(values) / len(values), 2) for industry, values in industry_values.items() if values
    }
    for row in rows:
        row.update(_leadership_profile(snapshot_by_code.get(row["code"], {})))
        sector_change = industry_changes.get(row["industry"], 0.0)
        row["sector_change_percent"] = sector_change
        row["score"] = _intraday_score(
            row["base_score"], row["change_percent"], row["volume_ratio"], row["turnover_rate"],
            row["intraday_position_percent"], row["price_vs_ma5_percent"], row["order_imbalance"], sector_change,
        )
        checks = [
            {"name": "价格站上MA5", "passed": row["price_vs_ma5_percent"] > 0},
            {"name": "板块同步走强", "passed": sector_change >= 0},
            {"name": "盘口卖压未占优", "passed": row["order_imbalance"] > -0.15},
            {"name": "涨幅未过度透支", "passed": 0.5 <= row["change_percent"] < 8},
            {"name": "量能处于可接受区间", "passed": 0.8 <= row["volume_ratio"] <= 4},
        ]
        row["checks"] = checks
        row["passed"] = sum(item["passed"] for item in checks)
        row["technical_qualified"] = row["score"] >= 65 and row["passed"] >= 4
        row["eligible"] = row["technical_qualified"] and row["leadership_qualified"]
        if row["eligible"] and row["volume_ratio"] >= 1.2 and row["intraday_position_percent"] >= 60:
            row["signal"] = "放量突破观察"
        elif row["eligible"]:
            row["signal"] = "回踩承接观察"
        else:
            row["signal"] = "不进入盘中候选"
    rows.sort(key=lambda item: (item["eligible"], item["leadership_score"], item["score"], item["volume_ratio"]), reverse=True)
    technical_qualified_count = sum(1 for row in rows if row["technical_qualified"])
    eligible_rows = [row for row in rows if row["eligible"]]
    selected = eligible_rows[:limit]
    for index, row in enumerate(selected):
        row["tier"] = "核心龙头" if index < 5 else "龙头备选"
    market = _market_emotion(len(selected))
    if market["state"] == "可观察":
        max_positions, per_position = 2, min(15_000, capital * 0.15)
    elif market["state"] == "谨慎":
        max_positions, per_position = 1, min(10_000, capital * 0.10)
    else:
        max_positions, per_position = 0, 0
    return {
        "market": market, "candidates": selected, "qualified_count": len(eligible_rows),
        "technical_qualified_count": technical_qualified_count,
        "leadership_filtered_count": technical_qualified_count - len(eligible_rows), "scanned": len(snapshots),
        "prefiltered": len(prefiltered), "deep_scanned": len(futures), "failed": failed,
        "category_changes": dict(sorted(industry_changes.items(), key=lambda item: item[1], reverse=True)[:8]),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "position_plan": {
            "capital": capital, "max_positions": max_positions, "per_position": round(per_position, 2),
            "max_new_exposure": round(max_positions * per_position, 2),
            "cash_reserve": round(capital - max_positions * per_position, 2),
        },
        "method": "先扫描全部非ST沪深主板并深度评分80只；技术面达到65分且通过至少4/5项守卫后，还必须满足行业市值排名、行业成交额排名、总市值和流动性构成的龙头成熟度门槛。最多展示12只，前5只为核心龙头，其余为龙头备选。行业分类匹配不等同于主营收入纯度。",
        "disclaimer": "盘中挂单可随时撤销，量价信号也可能失效。本页仅供研究观察，不构成买入建议，不自动下单。",
    }
