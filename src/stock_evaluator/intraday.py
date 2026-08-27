from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

from .board_plan import _market_emotion
from .corporate_events import attach_corporate_event_risks
from .evaluator import evaluate
from .market import EastmoneyProvider
from .regulatory import regulatory_risk
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
    bars = provider.history(code)
    result = evaluate(quote, bars)
    metrics, book = result["metrics"], result["order_book"]
    completed = [bar for bar in bars if bar.trade_date < date.today()]
    recent_5_limit_ups = sum(
        completed[index - 1].close > 0
        and (completed[index].close / completed[index - 1].close - 1) * 100 >= 9.5
        for index in range(max(1, len(completed) - 5), len(completed))
    )
    recent_10_limit_ups = sum(
        completed[index - 1].close > 0
        and (completed[index].close / completed[index - 1].close - 1) * 100 >= 9.5
        for index in range(max(1, len(completed) - 10), len(completed))
    )
    consecutive_limit_ups = 0
    for previous_bar, current_bar in reversed(list(zip(completed, completed[1:]))):
        if previous_bar.close <= 0 or (current_bar.close / previous_bar.close - 1) * 100 < 9.5:
            break
        consecutive_limit_ups += 1
    previous = completed[-1]
    previous_range = previous.high - previous.low
    previous_close_position = (previous.close - previous.low) / previous_range * 100 if previous_range > 0 else 100
    previous_upper_shadow = (previous.high - max(previous.open, previous.close)) / previous_range if previous_range > 0 else 0
    previous_window = completed[-6:-1]
    average_previous_volume = sum(bar.volume for bar in previous_window) / max(1, len(previous_window))
    previous_volume_ratio = previous.volume / average_previous_volume if average_previous_volume > 0 else 0
    open_gap = (quote.open_price / quote.previous_close - 1) * 100 if quote.previous_close else 0
    rebound_from_low = (quote.price / quote.low_price - 1) * 100 if quote.low_price else 0
    return {
        "code": code, "name": quote.name, "industry": industry, "category": industry,
        "base_score": result["score"], "price": quote.price,
        "change_percent": quote.change_percent, "volume_ratio": metrics["volume_ratio"],
        "turnover_rate": metrics["turnover_rate"],
        "intraday_position_percent": metrics["intraday_position_percent"],
        "price_vs_ma5_percent": metrics["price_vs_ma5_percent"],
        "order_imbalance": book["imbalance"], "order_signal": book["signal"],
        "amount": quote.amount, "open_gap_percent": round(open_gap, 2),
        "rebound_from_low_percent": round(rebound_from_low, 2),
        "recent_5_limit_up_count": recent_5_limit_ups,
        "recent_10_limit_up_count": recent_10_limit_ups,
        "consecutive_limit_up_days": consecutive_limit_ups,
        "previous_close_position_percent": round(previous_close_position, 2),
        "previous_upper_shadow_ratio": round(previous_upper_shadow, 3),
        "previous_volume_ratio": round(previous_volume_ratio, 2),
        "regulatory_risk": regulatory_risk(completed, quote.price),
        "sealed": quote.ask_volume5 == 0 and quote.bid_volume5 > 0 and quote.change_percent >= 9.5,
    }


def _prefilter_snapshots(snapshots: list[dict], limit: int = 80) -> list[dict]:
    active = []
    for row in snapshots:
        change, amount = _number(row.get("f3")), _number(row.get("f6"))
        turnover, volume_ratio = _number(row.get("f8")), _number(row.get("f10"))
        market_cap = _number(row.get("f20"))
        price, previous_close = _number(row.get("f2")), _number(row.get("f18"))
        open_price, low_price = _number(row.get("f17")), _number(row.get("f16"))
        open_gap = (open_price / previous_close - 1) * 100 if previous_close and open_price else 0
        rebound_from_low = (price / low_price - 1) * 100 if low_price and price else 0
        ordinary = 0.3 <= change < 8.5 and 0.2 <= turnover <= 15 and 0.6 <= volume_ratio <= 5
        card_anomaly = 8.5 <= change <= 10.3 and amount >= 100_000_000 and 1 <= turnover <= 25 and volume_ratio >= 0.5
        reversal = (
            -10.3 <= open_gap <= -9.3 and rebound_from_low >= 5
            and -7 <= change < 8.5 and turnover <= 35 and volume_ratio >= 0.2
        )
        if not (amount >= 50_000_000 and market_cap >= 2_000_000_000 and (ordinary or reversal or card_anomaly)):
            continue
        row = dict(row)
        row["limit_down_reversal_prefilter"] = reversal
        row["card_anomaly_prefilter"] = card_anomaly
        row["activity_score"] = (
            change * 2 + min(volume_ratio, 3) * 3 + min(turnover, 10) * 2
            + min(amount / 100_000_000, 10) + (25 + rebound_from_low if reversal else 0)
            + (20 if card_anomaly else 0)
        )
        active.append(row)
    active.sort(key=lambda item: (item["activity_score"], float(item.get("f6") or 0)), reverse=True)
    return active[:limit]


def _limit_down_reversal_profile(row: dict, sector_change: float) -> dict:
    checks = [
        {"name": "近5日至少3次涨停", "passed": row.get("recent_5_limit_up_count", 0) >= 3},
        {"name": "跌停附近开盘", "passed": -10.3 <= row.get("open_gap_percent", 0) <= -9.3},
        {"name": "已拉离跌停至少6%", "passed": row.get("rebound_from_low_percent", 0) >= 6},
        {"name": "当前跌幅收窄至5%内", "passed": row.get("change_percent", -99) >= -5},
        {"name": "盘中成交额≥1亿元", "passed": row.get("amount", 0) >= 100_000_000},
        {"name": "盘口或板块至少一项承接", "passed": row.get("order_imbalance", -1) > -0.15 or sector_change >= 0},
    ]
    passed = sum(item["passed"] for item in checks)
    watch = passed == len(checks)
    confirmed = (
        watch and row.get("rebound_from_low_percent", 0) >= 8
        and row.get("change_percent", -99) >= -2
        and row.get("order_imbalance", -1) > -0.15
    )
    score = 62
    score += min(12, row.get("recent_5_limit_up_count", 0) * 3)
    score += 8 if row.get("rebound_from_low_percent", 0) >= 8 else 4 if row.get("rebound_from_low_percent", 0) >= 6 else 0
    score += 6 if row.get("change_percent", -99) >= -2 else 3 if row.get("change_percent", -99) >= -5 else 0
    score += 5 if row.get("amount", 0) >= 200_000_000 else 3 if row.get("amount", 0) >= 100_000_000 else 0
    score += 4 if row.get("order_imbalance", -1) >= 0.15 else 2 if row.get("order_imbalance", -1) > -0.15 else 0
    risks = ["跌停反核失败时可能再次封跌停，A股T+1无法当日止损", "仅限小仓位高风险观察，不按普通龙头仓位执行"]
    if row.get("change_percent", -99) < 0:
        risks.append("尚未翻红，反核仍处确认阶段")
    if sector_change < 0:
        risks.append("所属板块未同步走强")
    if row.get("order_imbalance", -1) <= -0.15:
        risks.append("盘口卖压仍占优")
    return {
        "watch": watch, "confirmed": confirmed, "score": min(96, score),
        "passed": passed, "checks": checks, "risks": risks,
        "label": "跌停反核确认" if confirmed else "跌停反核观察",
    }


def _card_anomaly_late_profile(row: dict, now: datetime) -> dict:
    """识别疑似成功卡住10日严重异动线、可在尾盘确认的高辨识度股票。"""
    regulation = row.get("regulatory_risk") or {}
    ten_day_change = _number(regulation.get("ten_day_change"))
    event_level = str((row.get("corporate_event_risk") or {}).get("level") or "normal")
    checks = [
        {"name": "10日涨幅代理值85%–100%", "passed": 85 <= ten_day_change < 100},
        {"name": "近10日至少4次涨停", "passed": row.get("recent_10_limit_up_count", 0) >= 4},
        {"name": "前一日主动断板", "passed": row.get("consecutive_limit_up_days", 0) == 0},
        {"name": "当前涨幅≥7%", "passed": row.get("change_percent", 0) >= 7},
        {"name": "价格位于日内高位90%以上", "passed": row.get("intraday_position_percent", 0) >= 90},
        {"name": "成交额≥5亿元", "passed": row.get("amount", 0) >= 500_000_000},
        {"name": "换手率2.5%–20%", "passed": 2.5 <= row.get("turnover_rate", 0) <= 20},
        {"name": "前日强收短上影", "passed": row.get("previous_close_position_percent", 0) >= 95 and row.get("previous_upper_shadow_ratio", 1) <= 0.10},
        {"name": "前日量能未极端放大", "passed": 0.6 <= row.get("previous_volume_ratio", 0) <= 1.5},
        {"name": "盘口买盘未转弱", "passed": row.get("order_imbalance", -1) > -0.15},
        {"name": "无并购重组高风险", "passed": event_level != "high"},
    ]
    passed = sum(item["passed"] for item in checks)
    matched = passed == len(checks) and not regulation.get("serious_trigger", False)
    hhmm = now.hour * 100 + now.minute
    execution_window = 1445 <= hhmm <= 1455
    if not matched:
        action, signal, buy_ready = "不进入卡异动候选", "卡异动条件不足", False
    elif hhmm > 1455:
        action, signal, buy_ready = "尾盘窗口已过 · 不追价", "卡异动成立但停止追价", False
    elif not execution_window:
        action, signal, buy_ready = "等待14:45尾盘确认", "疑似卡异动尾盘候选", False
    elif row.get("sealed"):
        action, signal, buy_ready = "涨停价小仓排队 · 未必成交", "卡异动尾盘封板确认", True
    else:
        action, signal, buy_ready = "尾盘小仓买入", "卡异动尾盘确认", True
    return {
        "matched": matched, "buy_ready": buy_ready, "execution_window": execution_window,
        "score": min(98, 70 + passed * 2), "passed": passed, "checks": checks,
        "action": action, "signal": signal, "position_cap_percent": 5,
        "ten_day_change_proxy": ten_day_change,
        "note": "10日个股涨幅仅为偏离值代理；需结合对应指数及交易所公告复核，不能冒充正式异动认定。",
        "risks": ["卡异动属于高位博弈，次日可能低开或快速补跌", "A股T+1买入后当日无法卖出"],
    }


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


def build_intraday_plan(capital: float = 100_000, limit: int = 12, now: datetime | None = None) -> dict:
    now = now or datetime.now()
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
        regular_checks = [
            {"name": "价格站上MA5", "passed": row["price_vs_ma5_percent"] > 0},
            {"name": "板块同步走强", "passed": sector_change >= 0},
            {"name": "盘口卖压未占优", "passed": row["order_imbalance"] > -0.15},
            {"name": "涨幅未过度透支", "passed": 0.5 <= row["change_percent"] < 8},
            {"name": "量能处于可接受区间", "passed": 0.8 <= row["volume_ratio"] <= 4},
        ]
        row["checks"] = regular_checks
        row["passed"] = sum(item["passed"] for item in regular_checks)
        row["technical_qualified"] = row["score"] >= 65 and row["passed"] >= 4
    card_targets = [row for row in rows if 80 <= _number((row.get("regulatory_risk") or {}).get("ten_day_change")) < 100]
    if card_targets:
        attach_corporate_event_risks(card_targets)
    for row in rows:
        sector_change = row["sector_change_percent"]
        reversal = _limit_down_reversal_profile(row, sector_change)
        card_anomaly = _card_anomaly_late_profile(row, now)
        row["card_anomaly"] = card_anomaly["matched"]
        row["tail_buy_ready"] = card_anomaly["buy_ready"]
        row["entry_action"] = card_anomaly["action"] if card_anomaly["matched"] else None
        row["limit_down_reversal"] = reversal["watch"]
        row["reversal_confirmed"] = reversal["confirmed"]
        row["strategy_type"] = "跌停反核" if reversal["watch"] else "普通盘中"
        row["risks"] = reversal["risks"] if reversal["watch"] else []
        if card_anomaly["matched"]:
            row["score"] = max(row["score"], card_anomaly["score"])
            row["checks"] = card_anomaly["checks"]
            row["passed"] = card_anomaly["passed"]
            row["technical_qualified"] = True
            row["eligible"] = True
            row["strategy_type"] = "卡异动尾盘"
            row["signal"] = card_anomaly["signal"]
            row["position_cap_percent"] = card_anomaly["position_cap_percent"]
            row["risks"] = card_anomaly["risks"]
            row["card_anomaly_note"] = card_anomaly["note"]
        elif reversal["watch"]:
            row["score"] = max(row["score"], reversal["score"])
            row["checks"] = reversal["checks"]
            row["passed"] = reversal["passed"]
            row["technical_qualified"] = True
            row["eligible"] = True
            row["signal"] = reversal["label"]
            row["position_cap_percent"] = 5 if reversal["confirmed"] else 3
        else:
            row["eligible"] = row["technical_qualified"] and row["leadership_qualified"]
            row["position_cap_percent"] = 15
        if not card_anomaly["matched"] and not reversal["watch"] and row["eligible"] and row["volume_ratio"] >= 1.2 and row["intraday_position_percent"] >= 60:
            row["signal"] = "放量突破观察"
        elif not card_anomaly["matched"] and not reversal["watch"] and row["eligible"]:
            row["signal"] = "回踩承接观察"
        elif not card_anomaly["matched"] and not reversal["watch"]:
            row["signal"] = "不进入盘中候选"
    rows.sort(key=lambda item: (
        item["eligible"], item.get("tail_buy_ready", False), item.get("card_anomaly", False),
        item.get("reversal_confirmed", False), item.get("limit_down_reversal", False),
        item["leadership_score"], item["score"], item["volume_ratio"],
    ), reverse=True)
    technical_qualified_count = sum(1 for row in rows if row["technical_qualified"])
    eligible_rows = [row for row in rows if row["eligible"]]
    reversal_rows = [row for row in eligible_rows if row.get("limit_down_reversal")][:3]
    card_rows = [row for row in eligible_rows if row.get("card_anomaly")][:3]
    regular_rows = [row for row in eligible_rows if not row.get("limit_down_reversal") and not row.get("card_anomaly")]
    reserved = reversal_rows + card_rows
    selected = regular_rows[:max(0, limit - len(reserved))] + reserved
    for index, row in enumerate(selected):
        row["tier"] = "卡异动尾盘" if row.get("card_anomaly") else "高风险反核" if row.get("limit_down_reversal") else "核心龙头" if index < 5 else "龙头备选"
    market = _market_emotion(len(selected))
    if market["state"] == "可观察":
        max_positions, per_position = 2, min(15_000, capital * 0.15)
    elif market["state"] == "谨慎":
        max_positions, per_position = 1, min(10_000, capital * 0.10)
    else:
        max_positions, per_position = 0, 0
    return {
        "market": market, "candidates": selected, "qualified_count": len(eligible_rows),
        "reversal_candidates": reversal_rows, "reversal_count": len(reversal_rows),
        "card_anomaly_candidates": card_rows, "card_anomaly_count": len(card_rows),
        "tail_buy_ready_count": sum(1 for row in card_rows if row.get("tail_buy_ready")),
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
        "method": "先扫描全部非ST沪深主板并深度评分80只；普通候选执行技术面与龙头成熟度门槛。另设跌停反核与疑似卡异动尾盘通道；卡异动仅在14:45–14:55满足高辨识度、10日涨幅代理值、成交额、换手、前日结构、盘口及重大事项风控后给出小仓执行提示。",
        "disclaimer": "卡异动使用个股涨幅做代理，不等同交易所偏离值认定；尾盘高位博弈可能次日低开，A股T+1无法当日止损。页面只提供纪律化研究提示，不自动下单。",
    }
