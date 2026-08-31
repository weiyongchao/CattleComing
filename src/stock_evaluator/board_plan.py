from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .auction import screen_auction_candidates, screen_historical_auction_candidates
from .board_selection import MAX_BOARD_PICKS
from .board_session import workflow_view
from .corporate_events import attach_corporate_event_risks
from .market import EastmoneyProvider, MarketDataError
from .trade_advice import auction_entry_plan


BOARD_STRATEGY_VERSION = "2026.08.31.3"
from .screener import LEADER_POOL, is_main_board, is_risk_stock_name


HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}


def auction_observation_view(snapshot: dict) -> dict:
    """只转换展示副本；旧版冻结数据仍保留原始策略留痕。"""
    view = dict(snapshot)
    pool = []
    phase = snapshot.get("auction_phase", "final")
    market = (snapshot.get("market") or {}).get("state", "未知")
    for original in (snapshot.get("candidates") or []) + (snapshot.get("watch_candidates") or []):
        item = dict(original)
        item.setdefault("auction_signal_action", item.get("action"))
        item.update(
            action="取消候选" if item.get("risk_veto") else f"{item.get('strategy_mode') or '竞价候选'} · 仅观察",
            recommended=False, board_entry_allowed=False, actionable=False,
            execution_ready=False, recommendation_badge=None,
            candidate_scope_label="竞价观察池 · 非买入推荐",
        )
        item["entry_plan"] = auction_entry_plan(item, phase, market)
        pool.append(item)
    view.update(candidates=pool[:MAX_BOARD_PICKS], watch_candidates=pool[MAX_BOARD_PICKS:],
                actionable_count=0, recommendation_limit=MAX_BOARD_PICKS)
    view["workflow"] = workflow_view(view)
    view["position_plan"] = {
        **(snapshot.get("position_plan") or {}), "max_positions": 0, "per_position": 0,
        "max_new_exposure": 0, "cash_reserve": snapshot.get("capital", 100_000),
        "rule": "竞价只建观察池、不分配执行仓位；盘中仅提示唯一首选，全天最多5个不同代码。",
    }
    return view


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fetch_market_rows() -> list[dict]:
    params = {
        "pn": 1, "pz": 6000, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f3",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23", "fields": "f12,f14,f2,f3,f8,f10,f20",
    }
    request = Request(
        "https://push2.eastmoney.com/api/qt/clist/get?" + urlencode(params), headers=HEADERS
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=15) as response:
                payload = json.load(response)
            return (payload.get("data") or {}).get("diff") or []
        except Exception as exc:
            last_error = exc
            time.sleep(0.2 * (attempt + 1))
    raise MarketDataError(f"市场情绪数据连接失败：{last_error}")


def _market_gate(advance_ratio: float, limit_up: int, limit_down: int, average_change: float, auction_count: int) -> dict:
    score = 30 if advance_ratio >= 0.55 else 20 if advance_ratio >= 0.45 else 5
    score += 25 if limit_up >= 40 else 15 if limit_up >= 20 else 5
    score += 15 if limit_down <= 5 else 8 if limit_down <= 15 else 0
    score += 15 if average_change >= 0.5 else 10 if average_change >= 0 else 0
    score += 15 if auction_count >= 2 else 8 if auction_count == 1 else 0
    state = "可观察" if score >= 65 else "谨慎" if score >= 45 else "空仓"
    return {"score": score, "state": state, "allow_new_positions": state != "空仓"}


def _market_emotion(auction_count: int) -> dict:
    source = "东方财富延迟全主板快照"
    try:
        from .universe import main_board_snapshots
        rows = main_board_snapshots(cache_seconds=20)
        if len(rows) < 500:
            raise MarketDataError("全市场情绪样本不完整")
    except MarketDataError:
        source = "腾讯行情跨行业主板样本（备用）"
        provider, rows = EastmoneyProvider(timeout=6), []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(provider.quote, code) for code in LEADER_POOL]
            for future in as_completed(futures):
                try:
                    quote = future.result()
                    if not is_risk_stock_name(quote.name):
                        rows.append({"f12": quote.code, "f14": quote.name, "f3": quote.change_percent})
                except Exception:
                    pass
    changes = [_safe_float(row.get("f3")) for row in rows]
    if not changes:
        raise MarketDataError("市场情绪样本为空")
    advance_ratio = sum(value > 0 for value in changes) / len(changes)
    limit_up = sum(value >= 9.5 for value in changes)
    limit_down = sum(value <= -9.5 for value in changes)
    average_change = sum(changes) / len(changes)
    gate = _market_gate(advance_ratio, limit_up, limit_down, average_change, auction_count)
    return {
        **gate, "source": source, "sample_size": len(changes), "advance_ratio": round(advance_ratio, 4),
        "limit_up": limit_up, "limit_down": limit_down, "average_change": round(average_change, 2),
    }


def _auction_gate(candidates: list[dict], context: dict | None = None) -> dict:
    context = context or {}
    phase = context.get("auction_phase")
    source = (
        "前序交易日K线 + 09:17可撤单阶段参考撮合"
        if phase == "cancelable" else
        "前序交易日K线 + 09:20不可撤单阶段参考撮合"
        if phase == "indicative" else
        "前序交易日K线 + 09:25最终竞价"
    )
    if not candidates:
        return {
            "score": 0, "state": "空仓", "allow_new_positions": False, "candidate_count": 0,
            "strong_count": 0, "average_score": 0, "average_gap": 0, "auction_amount": 0,
            "advance_ratio": 0, "average_change": 0, "limit_up": 0, "limit_down": 0, "sample_size": 0,
            "source": source,
        }
    average_score = sum(item["score"] for item in candidates) / len(candidates)
    strong_count = sum(item["score"] >= 75 for item in candidates)
    auction_amount = sum(item["auction_amount"] for item in candidates)
    qualified_count = int(context.get("qualified_count") or len(candidates))
    deep_scanned = int(context.get("deep_scanned") or len(candidates))
    fund_confirmed = sum(
        (item.get("big_order_support") or {}).get("status") == "confirmed"
        or (item.get("decision_main_ratio") is not None and item["decision_main_ratio"] >= 0)
        for item in candidates
    )
    score = round(min(
        100,
        average_score * 0.60
        + min(qualified_count / 6, 1) * 15
        + (strong_count / len(candidates)) * 15
        + (fund_confirmed / len(candidates)) * 10,
    ))
    if fund_confirmed == 0:
        score = min(score, 69)
    state = "可观察" if score >= 70 else "谨慎" if score >= 55 else "空仓"
    return {
        "score": score, "state": state, "allow_new_positions": state != "空仓",
        "candidate_count": len(candidates), "strong_count": strong_count,
        "qualified_count": qualified_count, "deep_scanned": deep_scanned,
        "fund_confirmed_count": fund_confirmed,
        "average_score": round(average_score, 1),
        "average_gap": round(sum(item["auction_gap_percent"] for item in candidates) / len(candidates), 2),
        "auction_amount": auction_amount, "source": source,
        "advance_ratio": round(strong_count / len(candidates), 4),
        "average_change": round(sum(item["auction_gap_percent"] for item in candidates) / len(candidates), 2),
        "limit_up": strong_count, "limit_down": 0, "sample_size": len(candidates),
    }


def _generalization_evidence(candidate: dict) -> dict:
    """用同一组独立证据约束所有形态，避免单个补漏通道直接产生推荐。"""
    liquidity_tier = str(candidate.get("auction_liquidity_tier") or "A")
    minimum_amount = 15_000_000 if liquidity_tier == "C" else 30_000_000 if liquidity_tier == "B" else 50_000_000
    previous_close_position = _safe_float(candidate.get("previous_close_position_percent"), 100)
    previous_upper_shadow = _safe_float(candidate.get("previous_upper_shadow_ratio"), 0)
    previous_volume_ratio = _safe_float(candidate.get("previous_volume_ratio"), 1)
    recent_activity = max(
        int(candidate.get("recent_10_limit_up_count") or 0),
        int(candidate.get("consecutive_limit_up_days") or 0),
    )
    gap = _safe_float(candidate.get("auction_gap_percent"))
    volume_percent = _safe_float(candidate.get("auction_volume_percent"))
    history_signal = bool(
        candidate.get("previous_day_limit_up")
        or recent_activity >= 1
        or candidate.get("strong_characteristics")
        or (gap >= 3 and volume_percent >= 1)
    )
    history_structure = bool(
        history_signal
        and previous_close_position >= 70
        and previous_upper_shadow <= 0.35
        and 0.3 <= previous_volume_ratio <= 4.5
    )
    special_reversal = bool(
        candidate.get("nuclear_button_matched")
        or candidate.get("priority_tier") == "跌停反核观察"
    )
    auction_reality = bool(
        _safe_float(candidate.get("auction_amount")) >= minimum_amount
        and volume_percent >= (5 if special_reversal else 1)
        and ((-10.2 <= gap <= -9.5) if special_reversal else (-3 <= gap <= 10.2))
    )
    float_market_cap = _safe_float(
        candidate.get("float_market_cap") if "float_market_cap" in candidate else 10_000_000_000
    )
    listed_sessions = int(candidate.get("listed_sessions") if "listed_sessions" in candidate else 80)
    liquidity = bool(
        0 < float_market_cap < 50_000_000_000
        and listed_sessions >= 60
    )
    continuation_score = _safe_float(
        candidate.get("continuation_score"),
        _safe_float(candidate.get("continuation_base_score"), _safe_float(candidate.get("score"))),
    )
    continuation = continuation_score >= 55
    regulation_level = str((candidate.get("regulatory_risk") or {}).get("level") or "normal")
    support_status = str((candidate.get("big_order_support") or {}).get("status") or "unknown")
    risk_control = bool(
        not candidate.get("risk_veto", False)
        and regulation_level != "high"
        and support_status != "weak"
    )
    groups = {
        "history_structure": history_structure,
        "auction_reality": auction_reality,
        "liquidity": liquidity,
        "continuation": continuation,
        "risk_control": risk_control,
    }
    passed = sum(groups.values())
    score = (
        (25 if history_structure else 0)
        + (25 if auction_reality else 0)
        + (15 if liquidity else 0)
        + (25 if continuation else 0)
        + (10 if risk_control else 0)
    )
    # 风险控制必须通过，其余四组至少三组成立。形态命中本身不计作独立证据。
    gate = risk_control and sum((history_structure, auction_reality, liquidity, continuation)) >= 3
    return {
        "score": score,
        "passed": passed,
        "total": len(groups),
        "gate": gate,
        "groups": groups,
        "rule": "风险控制必过；历史结构、竞价真实性、流动性、T+1延续性至少通过三组",
    }


def _auction_decision(candidate: dict) -> dict:
    mode = candidate.get("strategy_mode") or ""
    priority_tier = candidate.get("priority_tier") or ""
    liquidity_b_watch = candidate.get("auction_liquidity_tier") == "B"
    liquidity_c_watch = candidate.get("auction_liquidity_tier") == "C"
    generalization = _generalization_evidence(candidate)
    amount_check = {
        "name": "竞价成交额≥1500万（三板以上一字C级）" if liquidity_c_watch else "竞价成交额≥3000万（一进二B级）" if liquidity_b_watch and candidate.get("one_to_two_matched") else "竞价成交额≥3000万（龙头修复B级）" if mode == "龙头断板修复" else "竞价成交额≥3000万（成熟连板B级）" if liquidity_b_watch else "竞价成交额>5000万",
        "passed": candidate["auction_amount"] >= 15_000_000 if liquidity_c_watch else candidate["auction_amount"] >= 30_000_000 if liquidity_b_watch else candidate["auction_amount"] > 50_000_000,
        "note": "C级只保留至少3连板、核心评分≥90、竞价量占比≥10%且无硬否决的一字核心" if liquidity_c_watch else "未达5000万A级线，仅在至少2连板、接力评分≥90且前序主力确认时保留",
    }
    core_mode = mode.startswith("连板核心")
    seal_value = int(_safe_float(candidate.get("previous_final_seal_time")))
    seal_text = f"{seal_value:06d}" if seal_value else ""
    seal_label = f"{seal_text[:2]}:{seal_text[2:4]}:{seal_text[4:]}" if seal_text else "数据缺失"
    late_final_seal_watch = bool(
        candidate.get("consecutive_limit_up_days", 0) >= 2
        and seal_value >= 113000
    )
    historical_auction_proxy = candidate.get("auction_time") == "09:31"
    fund_ratio = candidate.get("decision_main_ratio")
    support = candidate.get("big_order_support")
    if support:
        support_status = support.get("status", "unknown")
        fund_check = {
            "name": "大单/买盘支撑",
            "passed": None if support_status == "unknown" else support_status != "weak",
            "state": support_status,
            "note": f"{support.get('label', '待确认')} {'、'.join(support.get('details') or [])}",
        }
    else:
        fund_check = {
            "name": "前序主力资金未明显流出",
            "passed": None if fund_ratio is None else fund_ratio > -3,
            "state": "unknown" if fund_ratio is None else "passed" if fund_ratio > -3 else "failed",
            "note": "数据源暂不可用" if fund_ratio is None else f"主力净占比{fund_ratio:+.2f}%",
        }
    if mode == "高换手强竞价连板":
        checks = [
            {"name": "昨日连续涨停≥2天", "passed": candidate.get("consecutive_limit_up_days", 0) >= 2},
            {"name": "参考换手>1.2%（09:25复核）" if candidate.get("auction_value_kind") == "indicative_reference" else "真实09:25竞价换手率>1.2%",
             "passed": candidate.get("auction_time") != "09:31" and candidate.get("auction_turnover_percent", 0) > 1.2},
            {"name": "竞价涨幅≥5%", "passed": candidate.get("auction_gap_percent", 0) >= 5},
            {"name": "竞价成交额>5000万", "passed": candidate.get("auction_amount", 0) > 50_000_000},
            {"name": "流通市值<200亿", "passed": 0 < candidate.get("float_market_cap", 0) < 20_000_000_000},
            {"name": "非新股（上市≥60个交易日）", "passed": candidate.get("listed_sessions", 0) >= 60},
            fund_check,
        ]
    elif mode == "09:30空间板补选（历史代理）":
        opening_price = candidate.get("auction_price", 0)
        opening_high = candidate.get("opening_proxy_high", 0) or 0
        opening_low = candidate.get("opening_proxy_low", 0) or 0
        opening_close = candidate.get("opening_proxy_close", 0) or 0
        high_vs_open = (opening_high / opening_price - 1) * 100 if opening_price else 0
        minute_position = (
            (opening_close - opening_low) / (opening_high - opening_low) * 100
            if opening_high > opening_low else 100
        )
        checks = [
            {"name": "昨日连续涨停≥3天", "passed": candidate.get("consecutive_limit_up_days", 0) >= 3},
            {"name": "仅使用09:31首分钟回放", "passed": candidate.get("auction_time") == "09:31"},
            {"name": "竞价平开区间-1%至2%", "passed": -1 <= candidate.get("auction_gap_percent", 0) < 2},
            {"name": "首分钟成交额≥1亿元", "passed": candidate.get("auction_amount", 0) >= 100_000_000},
            {"name": "首分钟换手率≥1%", "passed": candidate.get("auction_turnover_percent", 0) >= 1},
            {"name": "首分钟最高较开盘≥1.5%", "passed": high_vs_open >= 1.5},
            {"name": "首分钟收盘位置≥65%", "passed": minute_position >= 65},
        ]
    elif mode == "早盘最终封板连板":
        checks = [
            {"name": "昨日及2个交易日前均涨停", "passed": candidate.get("consecutive_limit_up_days", 0) >= 2},
            {"name": "昨日最终封板早于11:30", "passed": 0 < seal_value < 113000, "note": f"最终封板时间{seal_label}"},
            {"name": "竞价涨幅≥1%且<9.8%", "passed": 1 <= candidate.get("auction_gap_percent", 0) < 9.8},
            {
                "name": "历史09:31代理量≥45%" if historical_auction_proxy else "竞价量比>1",
                "passed": candidate.get("auction_volume_percent", 0) >= 45 if historical_auction_proxy else candidate.get("auction_volume_percent", 0) > 1,
                "note": "历史回放使用首分钟累计量，不等同于真实09:25竞价量" if historical_auction_proxy else "系统竞价量相对近5日均量口径",
            },
            {"name": "竞价成交额>3000万", "passed": candidate.get("auction_amount", 0) > 30_000_000},
            {"name": "流通市值<200亿", "passed": 0 < candidate.get("float_market_cap", 0) < 20_000_000_000},
            {"name": "非新股（上市≥60个交易日）", "passed": candidate.get("listed_sessions", 0) >= 60},
            fund_check,
        ]
    elif mode == "低高开爆量二进三":
        checks = [
            {"name": "昨日二连板", "passed": candidate.get("consecutive_limit_up_days", 0) == 2},
            {"name": "竞价温和高开1%–2%", "passed": 1 <= candidate.get("auction_gap_percent", 0) < 2},
            {"name": "竞价成交额≥5000万", "passed": candidate.get("auction_amount", 0) >= 50_000_000},
            {"name": "竞价量占比≥15%", "passed": candidate.get("auction_volume_percent", 0) >= 15},
            {"name": "竞价换手率≥0.8%", "passed": candidate.get("auction_turnover_percent", 0) >= 0.8},
            {"name": "前日零炸板且强收", "passed": candidate.get("previous_limit_up_breaks", 0) == 0 and candidate.get("previous_close_position_percent", 0) >= 95},
            {"name": "前日量比0.6–2.5倍", "passed": 0.6 <= candidate.get("previous_volume_ratio", 0) <= 2.5},
            fund_check,
        ]
    elif mode == "容量一进二":
        checks = [
            {"name": "昨日首板", "passed": candidate.get("consecutive_limit_up_days", 0) == 1},
            {"name": "流通市值200亿–500亿", "passed": 20_000_000_000 <= candidate.get("float_market_cap", 0) < 50_000_000_000},
            {"name": "竞价高开5%–8.5%", "passed": 5 <= candidate.get("auction_gap_percent", 0) <= 8.5},
            {"name": "竞价成交额≥1亿元", "passed": candidate.get("auction_amount", 0) >= 100_000_000},
            {"name": "竞价量达到容量确认线", "passed": candidate.get("auction_volume_percent", 0) >= (25 if candidate.get("auction_time") == "09:31" else 5)},
            {"name": "前日强收且短上影", "passed": candidate.get("previous_close_position_percent", 0) >= 95 and candidate.get("previous_upper_shadow_ratio", 1) <= 0.10},
            {"name": "同主题竞价共振", "passed": (candidate.get("theme_context") or {}).get("score", 0) > 0},
            fund_check,
        ]
    elif mode in {"一进二竞价接力", "强竞价一进二一字板豁免"}:
        checks = [
            {"name": "昨日首板", "passed": candidate.get("consecutive_limit_up_days", 0) == 1},
            {"name": "竞价高开3%–10.2%", "passed": 3 <= candidate.get("auction_gap_percent", 0) <= 10.2},
            amount_check,
            {"name": "竞价量达到一进二确认线", "passed": candidate.get("auction_volume_percent", 0) >= (25 if candidate.get("auction_time") == "09:31" else 1)},
            {"name": "流通市值30亿–200亿", "passed": 3_000_000_000 <= candidate.get("float_market_cap", 0) < 20_000_000_000},
            {"name": "前日强收且短上影", "passed": candidate.get("previous_close_position_percent", 0) >= 85 and candidate.get("previous_upper_shadow_ratio", 1) <= 0.30},
            {"name": "首板量比不过度爆量", "passed": 0.4 <= candidate.get("previous_volume_ratio", 0) <= 4},
            fund_check,
        ]
        if mode == "强竞价一进二一字板豁免":
            checks.insert(5, {
                "name": "MA5偏离20%–25%仅按强竞价豁免",
                "passed": 20 < candidate.get("price_vs_ma5_percent", 0) <= 25,
                "note": "只适用于09:25接近涨停、竞价额≥5000万、竞价量占比≥10%、前日强收短上影的一进二",
            })
    elif mode == "高辨识度容量接力":
        checks = [
            {"name": "昨日仍有涨停连续性", "passed": candidate.get("previous_day_limit_up", False)},
            {"name": "近10日至少4次涨停", "passed": candidate.get("recent_10_limit_up_count", 0) >= 4},
            {"name": "流通市值200亿–350亿", "passed": 20_000_000_000 <= candidate.get("float_market_cap", 0) < 35_000_000_000},
            {"name": "竞价高开5%–10.2%", "passed": 5 <= candidate.get("auction_gap_percent", 0) <= 10.2},
            {"name": "竞价成交额≥5000万", "passed": candidate.get("auction_amount", 0) >= 50_000_000},
            {"name": "前日强收且短上影", "passed": candidate.get("previous_close_position_percent", 0) >= 95 and candidate.get("previous_upper_shadow_ratio", 1) <= 0.10},
            fund_check,
        ]
    elif mode == "龙头分歧反包":
        checks = [
            {"name": "近5/10日涨停活性≥3/4次", "passed": candidate.get("recent_5_limit_up_count", 0) >= 3 and candidate.get("recent_10_limit_up_count", 0) >= 4},
            {"name": "前日分歧后仍有承接", "passed": candidate.get("previous_close_position_percent", 0) >= 70 and candidate.get("previous_upper_shadow_ratio", 1) <= 0.35},
            {"name": "竞价高开8.5%–10.2%", "passed": 8.5 <= candidate.get("auction_gap_percent", 0) <= 10.2},
            {"name": "竞价成交额≥5000万", "passed": candidate.get("auction_amount", 0) >= 50_000_000},
            {"name": "分歧转强评分≥90", "passed": candidate.get("reversal_score", 0) >= 90},
            fund_check,
        ]
    elif mode == "竞价抢筹首板":
        checks = [
            {"name": "近10日存在涨停股性", "passed": candidate.get("recent_10_limit_up_count", 0) >= 1},
            {"name": "竞价接近涨停9.5%–10.2%", "passed": 9.5 <= candidate.get("auction_gap_percent", 0) <= 10.2},
            {"name": "竞价成交额≥5000万", "passed": candidate.get("auction_amount", 0) >= 50_000_000},
            {"name": "竞价换手率≥1%", "passed": candidate.get("auction_turnover_percent", 0) >= 1},
            {"name": "前日承接和上影未破坏", "passed": candidate.get("previous_close_position_percent", 0) >= 70 and candidate.get("previous_upper_shadow_ratio", 1) <= 0.25},
            fund_check,
        ]
    elif mode == "反核按钮竞价抄底":
        checks = list(candidate.get("nuclear_button_checks") or []) + [
            {
                "name": "市场情绪处于冰点（人工确认）",
                "passed": None,
                "note": "需结合跌停家数、昨日连板溢价和题材退潮程度人工判断，公式不能替代。",
            },
            {
                "name": "前期具备强势股性",
                "passed": candidate.get("strong_characteristics", False),
                "note": "量化代理：近10日有涨停，或昨日收盘创近25日新高；仍需人工确认题材辨识度。",
            },
        ]
    elif core_mode:
        historical_proxy = candidate.get("auction_time") == "09:31"
        checks = [
            {"name": "昨日涨停", "passed": candidate.get("previous_day_limit_up", False)},
            {"name": "连续涨停≥2天", "passed": candidate.get("consecutive_limit_up_days", 0) >= 2},
            {"name": "竞价涨幅≥1%", "passed": candidate["auction_gap_percent"] >= 1},
            {"name": "连板≥3或竞价涨幅≥5%", "passed": candidate.get("consecutive_limit_up_days", 0) >= 3 or candidate["auction_gap_percent"] >= 5},
            {"name": "历史首分钟量≥45%" if historical_proxy else "竞价量/近5日均量>1%", "passed": candidate["auction_volume_percent"] >= 45 if historical_proxy else candidate["auction_volume_percent"] > 1},
            amount_check,
            {"name": "流通市值<200亿", "passed": 0 < candidate.get("float_market_cap", 0) < 20_000_000_000},
            {"name": "上市满60个交易日", "passed": candidate.get("listed_sessions", 0) >= 60},
            fund_check,
        ]
    elif mode == "分歧转强":
        checks = [
            {"name": "近10日至少2次涨停", "passed": candidate.get("recent_10_limit_up_count", 0) >= 2},
            {"name": "前日高换手分歧", "passed": 2 <= candidate.get("previous_volume_ratio", 0) <= 6},
            {"name": "前日收盘仍有承接", "passed": candidate.get("previous_close_position_percent", 0) >= 65},
            {"name": "竞价高开5%–10.2%", "passed": 5 <= candidate["auction_gap_percent"] <= 10.2},
            {"name": "竞价量额确认", "passed": candidate["auction_volume_percent"] >= 1 and candidate["auction_amount"] >= 10_000_000},
            {"name": "近10日涨幅未超过70%", "passed": candidate.get("ten_day_change_percent", 0) <= 70},
            fund_check,
        ]
    elif mode in {"连板接力", "强势加速"}:
        checks = [
            {"name": "涨停活性足够新鲜", "passed": candidate.get("recent_5_limit_up_count", 0) >= 1 or candidate.get("recent_10_limit_up_count", 0) >= 2},
            {"name": "前日收盘位于近5日高位", "passed": candidate.get("previous_close_position_percent", 0) >= 85},
            {"name": "前日上影线较短", "passed": candidate.get("previous_upper_shadow_ratio", 1) <= 0.30},
            {"name": "竞价量达到均量1%", "passed": candidate["auction_volume_percent"] >= 1},
            amount_check,
            {"name": "竞价高开2%–10.2%", "passed": 2 <= candidate["auction_gap_percent"] <= 10.2},
            {"name": "前日量比不过度爆量", "passed": 0.4 <= candidate["previous_volume_ratio"] <= 3.5},
            fund_check,
            {"name": "近10日涨幅未超过60%", "passed": candidate.get("ten_day_change_percent", 0) <= 60},
        ]
    elif mode == "跌停竞价反核":
        checks = [
            {"name": "前日仍有涨停连续性", "passed": candidate.get("consecutive_limit_up_days", 0) >= 1},
            {"name": "近5日至少3次涨停", "passed": candidate.get("recent_5_limit_up_count", 0) >= 3},
            {"name": "竞价位于跌停附近", "passed": -10.2 <= candidate["auction_gap_percent"] <= -9.5},
            {"name": "竞价成交额>5000万", "passed": candidate["auction_amount"] > 50_000_000},
            {"name": "竞价量/近5日均量≥5%", "passed": candidate["auction_volume_percent"] >= 5},
            {"name": "前日收盘承接强", "passed": candidate.get("previous_close_position_percent", 0) >= 90},
            {"name": "流通市值<200亿", "passed": 0 < candidate.get("float_market_cap", 0) < 20_000_000_000},
            fund_check,
        ]
        if candidate.get("high_board_turnover_relay"):
            checks.insert(5, {
                "name": "高位换手接力仅作B级观察",
                "passed": True,
                "note": "三板以上由一字加速转为-3%至+5%竞价；低开采用更高量额门槛，盘中必须收复昨收、竞价价并快速回封。",
            })
    elif mode == "龙头断板修复":
        checks = [
            {"name": "近5日至少3次涨停", "passed": candidate.get("recent_5_limit_up_count", 0) >= 3},
            {"name": "近10日至少4次涨停", "passed": candidate.get("recent_10_limit_up_count", 0) >= 4},
            {"name": "竞价温和高开1%–5%", "passed": 1 <= candidate["auction_gap_percent"] <= 5},
            amount_check,
            {"name": "竞价量/近5日均量≥3%", "passed": candidate["auction_volume_percent"] >= 3},
            {"name": "前日收盘承接≥85%", "passed": candidate.get("previous_close_position_percent", 0) >= 85},
            {"name": "前日分歧量比1.5–4倍", "passed": 1.5 <= candidate.get("previous_volume_ratio", 0) <= 4},
            {"name": "前日上影线≤35%", "passed": candidate.get("previous_upper_shadow_ratio", 1) <= 0.35},
            fund_check,
        ]
    elif mode in {"首板预期", "隔日启动"}:
        historical_proxy = candidate.get("auction_time") == "09:31"
        checks = [
            {"name": "竞价高开3%–8.5%", "passed": 3 <= candidate["auction_gap_percent"] <= 8.5},
            {"name": "竞价成交额>5000万", "passed": candidate["auction_amount"] > 50_000_000},
            {"name": "竞价量达到隔日启动确认线", "passed": candidate["auction_volume_percent"] >= (25 if historical_proxy else 1.2)},
            {"name": "流通市值30亿–200亿", "passed": 3_000_000_000 <= candidate.get("float_market_cap", 0) < 20_000_000_000},
            {"name": "前日收盘有承接", "passed": candidate.get("previous_close_position_percent", 0) >= 70},
            {"name": "前日上影线≤30%", "passed": candidate.get("previous_upper_shadow_ratio", 1) <= 0.30},
            {"name": "量比健康且趋势未透支", "passed": 0.5 <= candidate.get("previous_volume_ratio", 0) <= 2.8 and candidate.get("ten_day_change_percent", 0) <= 30},
            fund_check,
        ]
    else:
        checks = [
        {"name": "竞价高开1%–8.5%", "passed": 1 <= candidate["auction_gap_percent"] < 8.5},
        {"name": "竞价成交额>5000万", "passed": candidate["auction_amount"] > 50_000_000},
        {"name": "竞价量达到近5日均量0.5%", "passed": candidate["auction_volume_percent"] >= 0.5},
        {"name": "竞价价站上MA5", "passed": candidate["price_vs_ma5_percent"] > 0},
        {"name": "近3日涨幅未透支", "passed": -3 <= candidate["three_day_change_percent"] <= 15},
        {"name": "昨日量比不过热", "passed": 0.5 <= candidate["previous_volume_ratio"] <= 3.5},
        {"name": "近5/10日趋势未透支", "passed": -3 <= candidate.get("five_day_change_percent", 0) <= 20 and -5 <= candidate.get("ten_day_change_percent", 0) <= 30},
        fund_check,
        {"name": "具备涨停活性或强竞价", "passed": candidate.get("recent_limit_up_count", 0) >= 1 or (candidate["auction_gap_percent"] >= 3 and candidate["auction_volume_percent"] >= 1)},
        ]
    regulation = candidate.get("regulatory_risk")
    if regulation:
        checks.append({
            "name": "未进入高异动风险区",
            "passed": regulation.get("level") != "high",
            "note": f"{regulation.get('label')} · {regulation.get('summary')}",
        })
    theme = candidate.get("theme_context")
    if theme:
        checks.append({
            "name": "题材/板块共振",
            "passed": True if theme.get("score", 0) > 0 else None,
            "note": f"{theme.get('label')}；{theme.get('policy_note')}",
        })
    if "continuation_score" in candidate:
        checks.append({
            "name": "T+1涨停预期分达到观察线",
            "passed": candidate.get("continuation_score", 0) >= 55,
            "note": f"当前{candidate.get('continuation_score', 0)}分；只表示结构匹配，不是涨停概率",
        })
    checks.append({
        "name": "具备实际可成交性",
        "passed": candidate.get("tradable", True),
        "note": candidate.get("tradability_label", "等待开盘确认"),
    })
    checks.append({
        "name": "未触发高位透支硬否决",
        "passed": not candidate.get("risk_veto", False),
        "note": "；".join(candidate.get("risk_reasons") or []) or "未触发高位透支",
    })
    passed = sum(item["passed"] is True for item in checks)
    known_total = sum(item["passed"] is not None for item in checks)
    fund_confirmed = (
        support.get("status") == "confirmed" if support
        else candidate.get("decision_main_ratio") is not None and candidate["decision_main_ratio"] >= 0
    )
    regulation_high = bool(regulation and regulation.get("level") == "high")
    formal_modes = {"高换手强竞价连板", "早盘最终封板连板", "连板接力", "强势加速", "分歧转强", "一进二竞价接力", "强竞价一进二一字板豁免", "首板预期", "隔日启动"}
    decision_score = candidate.get("continuation_score", candidate.get("selection_score", candidate["score"]))
    strong_core_auction_confirmation = (
        core_mode and candidate.get("auction_liquidity_tier") == "A"
        and candidate.get("consecutive_limit_up_days", 0) >= 3
        and candidate.get("score", 0) >= 95 and decision_score >= 55
    )
    strong_one_price_queue = bool(
        mode == "强竞价一进二一字板豁免"
        and candidate.get("strong_one_price_one_to_two")
        and candidate.get("auction_time") != "09:31"
        and candidate.get("auction_liquidity_tier") == "A"
        and not candidate.get("tradable", True)
        and candidate.get("eligible", True)
        and not candidate.get("risk_veto", False)
        and not regulation_high
    )
    board_entry_allowed = generalization["gate"] and (bool(
        liquidity_c_watch
        and not candidate.get("tradable", True)
        and candidate.get("eligible", True)
        and not candidate.get("risk_veto", False)
    ) or strong_one_price_queue)
    if candidate.get("risk_veto", False):
        action = "高位兑现风险 · 取消候选"
    elif late_final_seal_watch:
        action = "晚封连板B级观察 · 等待实际封板"
    elif mode == "早盘最终封板连板" and not candidate.get("tradable", True):
        action = "早封连板接近一字 · 排队难成交"
    elif mode == "龙头分歧反包" and not candidate.get("tradable", True):
        action = "龙头反包一字观察 · 排队难成交"
    elif mode == "高辨识度容量接力" and not candidate.get("tradable", True):
        action = "容量龙头一字观察 · 排队难成交"
    elif mode == "竞价抢筹首板" and not candidate.get("tradable", True):
        action = "抢筹首板一字观察 · 排队难成交"
    elif strong_one_price_queue:
        action = "强竞价1进2一字板豁免 · 可挂单打板"
    elif not candidate.get("tradable", True):
        action = "高辨识度一字板C级推荐 · 可挂单打板" if board_entry_allowed else "一字板打板观察 · 挂单未必成交"
    elif mode == "反核按钮竞价抄底":
        action = "反核按钮竞价抄底 · 高风险观察"
    elif mode == "龙头分歧反包":
        action = "龙头分歧反包 · 高风险观察"
    elif mode == "高辨识度容量接力":
        action = "容量龙头接力 · B级观察"
    elif mode == "容量一进二":
        action = "容量一进二 · 板块共振B级观察"
    elif mode == "竞价抢筹首板":
        action = "竞价抢筹首板 · 高风险观察"
    elif priority_tier == "跌停反核观察":
        action = "跌停竞价反核观察 · 仅低优先级"
    elif mode == "早盘最终封板连板":
        action = "早封连板A级预选" if candidate.get("auction_liquidity_tier") == "A" else "早封连板B级预选"
    elif mode == "高换手强竞价连板":
        action = "高换手强竞价连板A级预选"
    elif mode == "09:30空间板补选（历史代理）":
        action = "09:30空间板补选（历史代理） · 仅复盘观察"
    elif mode == "低高开爆量二进三":
        action = "低高开爆量二进三 · 等待09:30确认"
    elif candidate.get("space_board_watch"):
        action = "最高空间板B级观察 · 等待09:30确认"
    elif candidate.get("high_board_turnover_relay"):
        action = "高位换手接力B级观察"
    elif not generalization["gate"]:
        action = "多因子证据不足 · 仅观察"
    elif not liquidity_b_watch and decision_score >= (65 if core_mode or mode in formal_modes else 60) and passed >= 7 and (fund_confirmed or strong_core_auction_confirmation) and not regulation_high:
        action = "一进二A级观察" if priority_tier == "一进二观察" else "首板A级观察" if priority_tier == "首板观察" else "连板核心A级预选" if core_mode else "弱转强A级预选" if mode == "分歧转强" else "连板接力A级预选" if mode in {"连板接力", "强势加速"} else "隔日启动A级观察" if mode in {"首板预期", "隔日启动"} else "竞价A级观察"
    elif candidate["score"] >= 68 and passed >= 6:
        action = "异动风险观察" if regulation_high else "容量一进二 · 板块共振B级观察" if priority_tier == "容量一进二观察" else "龙头修复B级观察" if mode == "龙头断板修复" else "一进二B级观察" if priority_tier == "一进二观察" else "首板B级观察" if priority_tier == "首板观察" else "连板核心B级预选" if core_mode else "弱转强B级预选" if mode == "分歧转强" else "连板接力B级预选" if mode in {"连板接力", "强势加速"} else "隔日启动B级观察" if mode in {"首板预期", "隔日启动"} else "竞价B级观察"
    else:
        action = "取消候选"
    actionable = bool(
        not late_final_seal_watch
        and generalization["gate"]
        and action in {"隔日启动A级观察", "一进二A级观察", "首板A级观察"}
    )
    candidate_scope_label = (
        "涨停价排队许可" if board_entry_allowed else
        "09:30后待确认" if actionable else
        "观察池 · 非买入推荐"
    )
    return {
        **candidate, "checks": checks, "generalization_evidence": generalization,
        "guard_passed": passed, "guard_total": known_total, "check_total": len(checks), "action": action,
        "late_final_seal_watch": late_final_seal_watch,
        "candidate_scope_label": candidate_scope_label,
        "recommended": board_entry_allowed,
        "board_entry_allowed": board_entry_allowed,
        "recommendation_badge": "豁免观察 · 可挂单打板" if strong_one_price_queue else "推荐 · 可挂单打板" if board_entry_allowed else None,
        "actionable": actionable,
        "execution_ready": actionable,
        # 兼容旧页面首帧，字段值仍完全来自竞价或历史行情；新页面模块会替换为准确标签。
        "current_change_percent": candidate["auction_gap_percent"],
        "sector": {"average_change": candidate["three_day_change_percent"]},
    }


def _exclude_high_corporate_event_candidates(candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    """并购重组、重组终止等高风险事件只进入剔除留痕，不再混入主候选榜。"""
    safe, excluded = [], []
    for candidate in candidates:
        event = candidate.get("corporate_event_risk") or {}
        if event.get("level") != "high":
            safe.append(candidate)
            continue
        candidate.update({
            "eligible": False,
            "risk_veto": True,
            "event_risk_veto": True,
            "priority_tier": "重大事项风险剔除",
            "action": "重大事项高风险 · 取消候选",
            "recommended": False,
            "board_entry_allowed": False,
            "recommendation_badge": None,
            "actionable": False,
            "execution_ready": False,
        })
        candidate.setdefault("risks", []).append(
            f"{event.get('label', '重大事项高风险')}：仅保留风险留痕，不进入打板推荐"
        )
        excluded.append(candidate)
    return safe, excluded


def _auction_phase(now: datetime) -> str:
    hhmm = now.hour * 100 + now.minute
    if hhmm < 917:
        return "preauction"
    if hhmm < 920:
        return "cancelable"
    if hhmm < 925:
        return "indicative"
    return "final"


def _session_stage(now: datetime) -> str:
    hhmm = now.hour * 100 + now.minute
    if hhmm < 917:
        return "竞价进行中 · 09:17开始预选"
    if hhmm < 920:
        return "09:17可撤单预选 · 等待09:20确认"
    if hhmm < 925:
        return "09:20不可撤单观察 · 等待09:25确认"
    if hhmm < 930:
        return "09:25竞价决策已生成"
    if hhmm < 1500:
        return "盘中复核（竞价结论不回写）"
    return "收盘复盘（非盘中买入信号）"


def build_board_plan(
    capital: float = 100_000, target_date: date | None = None, now: datetime | None = None,
) -> dict:
    historical = target_date is not None and target_date < date.today()
    now = now or datetime.now()
    auction_phase = "historical" if historical else _auction_phase(now)
    if auction_phase == "preauction":
        auction = {
            "candidates": [], "ranked_count": 0, "qualified_count": 0,
            "relay_qualified_count": 0, "scanned": 0, "prefiltered": 0,
            "deep_scanned": 0, "failed": 0,
            "universe_source": "09:17可撤单预选窗口尚未开始",
            "snapshot_time": None,
            "auction_phase": "preauction",
            "method": "09:17前仅运行盘前历史预选；09:17开始读取可撤单阶段参考撮合数据。",
            "replay_warning": "请等待09:17自动生成竞价预选，09:20:05进入不可撤单阶段实时更新，09:25:10最终复核。",
            "disclaimer": "当前仅显示盘前阶段，不构成今日竞价候选。",
        }
    else:
        auction = (
            screen_historical_auction_candidates(target_date, limit=10)
            if historical else screen_auction_candidates(limit=10, preliminary=auction_phase in {"cancelable", "indicative"})
        )
        if auction_phase == "cancelable":
            auction.update({
                "auction_phase": "cancelable",
                "universe_source": str(auction.get("universe_source") or "").replace(
                    "09:20不可撤单阶段参考撮合", "09:17可撤单阶段参考撮合"
                ),
                "method": "09:17扫描全部非ST沪深主板，按可撤单阶段参考撮合价量生成早期预选；09:20后必须根据不可撤单挂单重新排序。",
                "disclaimer": "09:17–09:20挂单仍可撤销，预选排名可能大幅变化，只用于提前关注，不构成下单依据。",
            })
    candidates = [_auction_decision(candidate) for candidate in auction["candidates"]]
    risk_exclusions = [_auction_decision(candidate) for candidate in auction.get("risk_exclusions", [])]
    event_exclusions: list[dict] = []
    event_exclusion_codes: set[str] = set()
    if (candidates or risk_exclusions) and not historical:
        attach_corporate_event_risks(candidates + risk_exclusions)
        candidates, event_exclusions = _exclude_high_corporate_event_candidates(candidates)
        structural_exclusions, nested_event_exclusions = _exclude_high_corporate_event_candidates(risk_exclusions)
        event_exclusion_codes = {
            str(item.get("code") or "") for item in event_exclusions + nested_event_exclusions
        }
        excluded_by_code = {
            item.get("code"): item
            for item in structural_exclusions + event_exclusions + nested_event_exclusions
        }
        risk_exclusions = list(excluded_by_code.values())
    if auction_phase in {"cancelable", "indicative"}:
        for candidate in candidates:
            phase_label = "09:17预选" if auction_phase == "cancelable" else "09:20观察"
            candidate["action"] = f"{phase_label} · {candidate['action']}"
            candidate["actionable"] = False
    candidates.sort(key=lambda item: (
        item["actionable"],
        item.get("tradable", True) and item.get("priority_tier") != "跌停反核观察",
        item.get("priority_tier") in {"连板优先", "早封连板优先", "高换手连板优先", "盘中空间板观察"} and item.get("tradable", True),
        item.get("previous_board_count", 0),
        2 if item.get("auction_liquidity_tier") == "A" else 1 if item.get("auction_liquidity_tier") == "B" else 0,
        item.get("continuation_score", 0), item["score"], item["auction_amount"],
    ), reverse=True)
    gate = _auction_gate(candidates, auction)
    for candidate in candidates:
        candidate["auction_signal_action"] = candidate.get("action")
        candidate.update(
            action=f"{candidate.get('strategy_mode') or '竞价候选'} · 仅观察",
            recommended=False, board_entry_allowed=False, actionable=False,
            execution_ready=False, recommendation_badge=None,
            candidate_scope_label="竞价观察池 · 非买入推荐",
        )
        candidate["entry_plan"] = auction_entry_plan(candidate, auction_phase, gate["state"])
        candidate["execution_ready"] = (
            candidate["entry_plan"]["action"] == "开盘确认后买入"
            and candidate.get("actionable", False)
        )
    for candidate in risk_exclusions:
        candidate["entry_plan"] = auction_entry_plan(candidate, auction_phase, gate["state"])
    actionable_total = sum(item["execution_ready"] for item in candidates)
    if historical or actionable_total == 0:
        max_positions, per_position = 0, 0
    elif gate["state"] == "可观察":
        max_positions, per_position = 2, min(15_000, capital * 0.15)
    elif gate["state"] == "谨慎":
        max_positions, per_position = 1, min(10_000, capital * 0.10)
    else:
        max_positions, per_position = 0, 0
    actionable = [item for item in candidates if item["execution_ready"]][:max_positions]
    return {
        "capital": capital,
        "strategy_version": BOARD_STRATEGY_VERSION,
        "stage": f"历史回放 · {target_date.isoformat()}" if historical else _session_stage(now),
        "selected_date": target_date.isoformat() if target_date else date.today().isoformat(),
        "historical": historical, "auction_phase": auction_phase, "market": gate,
        "candidates": candidates[:MAX_BOARD_PICKS], "watch_candidates": candidates[MAX_BOARD_PICKS:],
        "recommendation_limit": MAX_BOARD_PICKS, "risk_exclusions": risk_exclusions,
        "actionable_count": len(actionable),
        "screening": {
            "scanned": auction.get("scanned", 0), "prefiltered": auction.get("prefiltered", 0),
            "deep_scanned": auction.get("deep_scanned", 0), "qualified_count": auction.get("qualified_count", 0),
            "failed": auction.get("failed", 0), "source": auction.get("universe_source", ""),
            "method": auction.get("method", ""), "snapshot_time": auction.get("snapshot_time"),
            "relay_qualified_count": auction.get("relay_qualified_count", 0),
            "core_qualified_count": auction.get("core_qualified_count", 0),
            "early_final_seal_chain_count": auction.get("early_final_seal_chain_count", 0),
            "high_turnover_chain_count": auction.get("high_turnover_chain_count", 0),
            "historical_opening_space_count": auction.get("historical_opening_space_count", 0),
            "reversal_qualified_count": auction.get("reversal_qualified_count", 0),
            "first_board_qualified_count": auction.get("first_board_qualified_count", 0),
            "continuation_primary_count": auction.get("continuation_primary_count", 0),
            "one_price_c_count": auction.get("one_price_c_count", 0),
            "untradable_count": auction.get("untradable_count", 0),
            "risk_veto_count": auction.get("risk_veto_count", 0) + len(event_exclusions),
            "event_risk_excluded_count": len(event_exclusion_codes),
            "one_to_two_count": auction.get("one_to_two_count", 0),
            "strong_one_price_one_to_two_count": auction.get("strong_one_price_one_to_two_count", 0),
            "high_board_turnover_relay_count": auction.get("high_board_turnover_relay_count", 0),
            "first_board_watch_count": auction.get("first_board_watch_count", 0),
            "leader_repair_count": auction.get("leader_repair_count", 0),
            "nuclear_button_count": auction.get("nuclear_button_count", 0),
            "capacity_relay_count": auction.get("capacity_relay_count", 0),
            "leader_reversal_count": auction.get("leader_reversal_count", 0),
            "auction_grab_count": auction.get("auction_grab_count", 0),
            "overnight_secondary_count": auction.get("overnight_secondary_count", 0),
            "replay_warning": auction.get("replay_warning"),
        },
        "position_plan": {
            "max_positions": max_positions, "per_position": round(per_position, 2),
            "max_new_exposure": round(per_position * max_positions, 2),
            "cash_reserve": round(capital - per_position * max_positions, 2),
            "rule": (
                "历史回放不生成可执行仓位，只用于比较规则。"
                if historical else
                "09:17可撤单预选、09:20不可撤单观察与09:25最终竞价均不分配执行仓位，等待盘中精选。"
                if auction_phase in {"cancelable", "indicative"} else
                "竞价阶段不分配执行仓位；盘中仅提示唯一首选，全天累计最多5只，不代表同时持有5只。"
            ),
        },
        "strategy_profile": {
            "name": "封板确认优先 · 唯一首选 · 全天最多5只",
            "selection_rule": "09:25只建观察池；盘中动态补选，实际封板、盘口、资金和风险需两次间隔20秒采样通过。普通开盘转强只观察，极强开盘采用严格例外。只提示唯一首选、有效时不换票，全天累计最多5个代码；可锁定一只仅看风险。",
            "core_rule": "最近交易日完整涨停池强制深扫；高换手强竞价连板通道仅使用真实09:25数据：至少2连板、竞价换手率>1.2%、高开≥5%、竞价额>5000万。早封连板通道要求昨日及2个交易日前均涨停、昨日最终封板早于11:30、竞价涨幅≥1%且<9.8%、竞价量比>1、竞价额>3000万、流通市值<200亿、上市≥60个交易日。",
            "relay_rule": "最近5日至少1次或10日至少2次涨停，前日强收、短上影，竞价量额确认；三板以上核心由一字加速转为-3%至+5%竞价时，量额、换手和前日结构达标可进入高位换手B级观察，低开不直接视为买点。",
            "reversal_rule": "最近10日至少2次涨停，前日2–6倍量分歧但仍有承接，次日竞价高开5%–10.2%且量额确认。",
            "first_board_rule": "一进二使用独立模型：普通通道MA5偏离≤20%；实时09:25若竞价接近涨停、竞价额≥5000万、竞价量占比≥10%、前日强收短上影，可将MA5偏离放宽到25%并标记为强竞价一字板豁免。3000万–5000万竞价额可进入B级；200亿–500亿容量股需板块共振。",
            "nuclear_button_rule": "仅用当日09:25最终竞价：昨日成交额≥5亿且成交量低于前日、竞价额≥5000万、高开≥7%、竞价换手≥3%；市场冰点与强势股性保留人工确认，命中后仍属于高风险观察。",
            "recognition_rule": "新增三条动态观察通道：200亿–350亿但近10日涨停活性很高的容量接力；高辨识度龙头前日分歧后竞价反包；近期有股性且竞价接近涨停、量额换手显著的抢筹首板。均不直接获得可执行仓位。",
            "risk_rule": "连板预选必须等待T日真实封板/回封才允许打板；昨日最终封板不早于11:30的连板股强制降为B级观察，未实际封板不得升级；盘中逼近涨停但未封住并明显回落时直接放弃追入。前日缩量连板后，一字竞价量超过近5日均量50%、MA5偏离超过20%的集中兑现结构硬否决；并购、收购、重大重组及重组终止等事件一律移出主榜。",
            "generalization_rule": "所有形态统一经过五组独立证据复核：风险控制必须通过，历史结构、竞价真实性、流动性、T+1延续性至少通过三组；命中某个形态或漏选过某只历史强股都不能单独产生推荐。",
        },
        "strategy_governance": {
            "objective": "在可成交前提下提高T+1收益质量，并控制炸板、次日低开和重大事项尾部风险",
            "universe_policy": "全部非ST沪深主板使用同一套特征和阈值，不按股票名称、代码或博主名单加分",
            "change_policy": "单日漏选或单只成功案例只用于提出假设；至少经过分离日期的正反样本验证后，才允许提升为可执行推荐",
            "validation_policy": "按时间顺序进行样本外/滚动验证，同时统计候选命中率、次日收益、最大不利波动、炸板率与候选数量；不以命中已知名单为唯一指标",
            "current_status": "通用五组证据门槛已启用；新增形态默认先进入观察层",
        },
        "data_scope": ["全部非ST沪深主板批量快照", "前序80个交易日K线与昨日成交额", "昨日首次/最终封板时间", "近5/10日涨停活性与连续涨停", "T+1连板预期分", "早封连板、连板优先与隔日启动分层", "反核按钮09:25五项硬条件", "近3/5/10/30日走势", "前日收盘位置与上影线", "竞价量额、竞价换手、大单/五档支撑", "题材板块共振", "近60日重组/停复牌/关联交易公告风险", "未持有者操作时机与失效条件", "T日盘中封板/回封执行门槛", "T+1开盘与收盘复盘"],
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "disclaimer": (
            auction.get("disclaimer", "历史回放只用于比较规则，不构成当前交易信号。")
            if historical else
            ("09:17–09:20委托仍可撤销，榜单只用于提前关注；09:20后按不可撤单挂单持续更新，09:25必须最终复核。" if auction_phase == "cancelable" else "09:20后原委托不能撤销，但仍可新增委托，参考撮合价量并非最终开盘结果；本页仅生成观察池，09:25必须再次复核。" if auction_phase == "indicative" else "")
            + "本页竞价决策不使用09:30后的现价或盘口；批量主力字段以快照时间为准。仅为竞价研究决策，不保证盈利；A股T+1下存在炸板、无法成交及次日跳空风险。"
        ),
    }
