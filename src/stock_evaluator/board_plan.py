from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .auction import screen_auction_candidates, screen_historical_auction_candidates
from .market import EastmoneyProvider, MarketDataError
from .screener import LEADER_POOL, is_main_board, is_risk_stock_name


HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}


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
    source = (
        "前序交易日K线 + 09:20不可撤单阶段参考撮合"
        if context.get("auction_phase") == "indicative"
        else "前序交易日K线 + 09:25最终竞价"
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


def _auction_decision(candidate: dict) -> dict:
    mode = candidate.get("strategy_mode") or ""
    priority_tier = candidate.get("priority_tier") or ""
    core_mode = mode.startswith("连板核心")
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
    if core_mode:
        historical_proxy = candidate.get("auction_time") == "09:31"
        checks = [
            {"name": "昨日涨停", "passed": candidate.get("previous_day_limit_up", False)},
            {"name": "连续涨停≥2天", "passed": candidate.get("consecutive_limit_up_days", 0) >= 2},
            {"name": "竞价涨幅≥1%", "passed": candidate["auction_gap_percent"] >= 1},
            {"name": "连板≥3或竞价涨幅≥5%", "passed": candidate.get("consecutive_limit_up_days", 0) >= 3 or candidate["auction_gap_percent"] >= 5},
            {"name": "历史首分钟量≥45%" if historical_proxy else "竞价量/近5日均量>1%", "passed": candidate["auction_volume_percent"] >= 45 if historical_proxy else candidate["auction_volume_percent"] > 1},
            {"name": "竞价成交额≥1000万", "passed": candidate["auction_amount"] >= 10_000_000},
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
            {"name": "竞价成交额≥1000万", "passed": candidate["auction_amount"] >= 10_000_000},
            {"name": "竞价高开2%–10.2%", "passed": 2 <= candidate["auction_gap_percent"] <= 10.2},
            {"name": "前日量比不过度爆量", "passed": 0.4 <= candidate["previous_volume_ratio"] <= 3.5},
            fund_check,
            {"name": "近10日涨幅未超过60%", "passed": candidate.get("ten_day_change_percent", 0) <= 60},
        ]
    elif mode in {"首板预期", "隔日启动"}:
        historical_proxy = candidate.get("auction_time") == "09:31"
        checks = [
            {"name": "竞价高开3%–8.5%", "passed": 3 <= candidate["auction_gap_percent"] <= 8.5},
            {"name": "竞价成交额≥2000万", "passed": candidate["auction_amount"] >= 20_000_000},
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
        {"name": "竞价成交额≥1000万", "passed": candidate["auction_amount"] >= 10_000_000},
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
    formal_modes = {"连板接力", "强势加速", "分歧转强", "首板预期", "隔日启动"}
    decision_score = candidate.get("continuation_score", candidate.get("selection_score", candidate["score"]))
    if not candidate.get("tradable", True):
        action = "一字板不可成交 · 仅记录强度"
    elif candidate.get("risk_veto", False):
        action = "高位透支 · 取消候选"
    elif decision_score >= (65 if core_mode or mode in formal_modes else 60) and passed >= 7 and fund_confirmed and not regulation_high:
        action = "一进二A级观察" if priority_tier == "一进二观察" else "首板A级观察" if priority_tier == "首板观察" else "连板核心A级预选" if core_mode else "弱转强A级预选" if mode == "分歧转强" else "连板接力A级预选" if mode in {"连板接力", "强势加速"} else "隔日启动A级观察" if mode in {"首板预期", "隔日启动"} else "竞价A级观察"
    elif candidate["score"] >= 68 and passed >= 6:
        action = "异动风险观察" if regulation_high else "一进二B级观察" if priority_tier == "一进二观察" else "首板B级观察" if priority_tier == "首板观察" else "连板核心B级预选" if core_mode else "弱转强B级预选" if mode == "分歧转强" else "连板接力B级预选" if mode in {"连板接力", "强势加速"} else "隔日启动B级观察" if mode in {"首板预期", "隔日启动"} else "竞价B级观察"
    else:
        action = "取消候选"
    return {
        **candidate, "checks": checks,
        "guard_passed": passed, "guard_total": known_total, "check_total": len(checks), "action": action,
        "actionable": action in {"隔日启动A级观察", "一进二A级观察", "首板A级观察"},
        "execution_ready": action in {"隔日启动A级观察", "一进二A级观察", "首板A级观察"},
        # 兼容旧页面首帧，字段值仍完全来自竞价或历史行情；新页面模块会替换为准确标签。
        "current_change_percent": candidate["auction_gap_percent"],
        "sector": {"average_change": candidate["three_day_change_percent"]},
    }


def _auction_phase(now: datetime) -> str:
    hhmm = now.hour * 100 + now.minute
    if hhmm < 920:
        return "preauction"
    if hhmm < 925:
        return "indicative"
    return "final"


def _session_stage(now: datetime) -> str:
    hhmm = now.hour * 100 + now.minute
    if hhmm < 920:
        return "竞价进行中 · 09:20刷新"
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
            "universe_source": "09:20不可撤单观察窗口尚未开始",
            "snapshot_time": None,
            "auction_phase": "preauction",
            "method": "09:20前仅运行盘前预选；09:20开始读取不可撤单阶段参考撮合数据。",
            "replay_warning": "请等待09:20:05自动刷新观察候选，09:25:10再刷新最终竞价。",
            "disclaimer": "当前仅显示盘前阶段，不构成今日竞价候选。",
        }
    else:
        auction = (
            screen_historical_auction_candidates(target_date, limit=6)
            if historical else screen_auction_candidates(limit=6, preliminary=auction_phase == "indicative")
        )
    candidates = [_auction_decision(candidate) for candidate in auction["candidates"]]
    if auction_phase == "indicative":
        for candidate in candidates:
            candidate["action"] = f"09:20观察 · {candidate['action']}"
            candidate["actionable"] = False
    candidates.sort(key=lambda item: (item["actionable"], item.get("tradable", True), item.get("priority_tier") == "连板优先", item.get("continuation_score", 0), item["score"], item["auction_amount"]), reverse=True)
    gate = _auction_gate(candidates, auction)
    actionable_total = sum(item["actionable"] for item in candidates)
    if historical or actionable_total == 0:
        max_positions, per_position = 0, 0
    elif gate["state"] == "可观察":
        max_positions, per_position = 2, min(15_000, capital * 0.15)
    elif gate["state"] == "谨慎":
        max_positions, per_position = 1, min(10_000, capital * 0.10)
    else:
        max_positions, per_position = 0, 0
    actionable = [item for item in candidates if item["actionable"]][:max_positions]
    return {
        "capital": capital,
        "stage": f"历史回放 · {target_date.isoformat()}" if historical else _session_stage(now),
        "selected_date": target_date.isoformat() if target_date else date.today().isoformat(),
        "historical": historical, "auction_phase": auction_phase, "market": gate,
        "candidates": candidates, "actionable_count": len(actionable),
        "screening": {
            "scanned": auction.get("scanned", 0), "prefiltered": auction.get("prefiltered", 0),
            "deep_scanned": auction.get("deep_scanned", 0), "qualified_count": auction.get("qualified_count", 0),
            "failed": auction.get("failed", 0), "source": auction.get("universe_source", ""),
            "method": auction.get("method", ""), "snapshot_time": auction.get("snapshot_time"),
            "relay_qualified_count": auction.get("relay_qualified_count", 0),
            "core_qualified_count": auction.get("core_qualified_count", 0),
            "reversal_qualified_count": auction.get("reversal_qualified_count", 0),
            "first_board_qualified_count": auction.get("first_board_qualified_count", 0),
            "continuation_primary_count": auction.get("continuation_primary_count", 0),
            "untradable_count": auction.get("untradable_count", 0),
            "risk_veto_count": auction.get("risk_veto_count", 0),
            "one_to_two_count": auction.get("one_to_two_count", 0),
            "first_board_watch_count": auction.get("first_board_watch_count", 0),
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
                "09:20只生成不可撤单阶段观察池，不分配仓位；09:25最终竞价复核通过后，A级候选才进入仓位计划。"
                if auction_phase == "indicative" else
                "只有A级候选才分配仓位；仓位由前序交易日走势、主力资金与09:25竞价质量决定，盘中变化不回写本页。"
            ),
        },
        "strategy_profile": {
            "name": "T+1涨停分层：连板优先 + 一进二观察 + 首板观察",
            "core_rule": "昨日涨停且连续涨停≥2天，竞价涨幅≥1%，并满足连板≥3或竞价涨幅≥5%；竞价量/近5日均量>1%、竞价额≥1000万、流通市值<200亿、上市满60个交易日。",
            "relay_rule": "最近5日至少1次或10日至少2次涨停，前日强收、短上影，竞价量额确认；只保留流通市值低于200亿的主板股。",
            "reversal_rule": "最近10日至少2次涨停，前日2–6倍量分歧但仍有承接，次日竞价高开5%–10.2%且量额确认。",
            "first_board_rule": "一进二和首板不进入连板优先；流通市值、竞价高开、量比、竞价额和前日承接足够亮眼时进入低优先级观察，并明确标注目标板数。",
            "risk_rule": "连板预选必须等待T日真实封板/回封才允许打板；一进二和首板降级观察，题材孤立且资金量价不够强、极端竞价爆量、大单偏弱或大市值则剔除。",
        },
        "data_scope": ["全部非ST沪深主板批量快照", "前序80个交易日K线", "近5/10日涨停活性与连续涨停", "T+1连板预期分", "连板优先与隔日启动双层", "近3/5/10/30日走势", "前日收盘位置与上影线", "竞价量额、大单/五档支撑", "题材板块共振", "T日盘中封板/回封执行门槛", "T+1开盘与收盘复盘"],
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "disclaimer": (
            auction.get("disclaimer", "历史回放只用于比较规则，不构成当前交易信号。")
            if historical else
            ("09:20后原委托不能撤销，但仍可新增委托，参考撮合价量并非最终开盘结果；本页仅生成观察池，09:25必须再次复核。" if auction_phase == "indicative" else "")
            + "本页竞价决策不使用09:30后的现价或盘口；批量主力字段以快照时间为准。仅为竞价研究决策，不保证盈利；A股T+1下存在炸板、无法成交及次日跳空风险。"
        ),
    }
