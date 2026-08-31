from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

from .market import EastmoneyProvider, MarketDataError
from .review_metrics import board_attribution, finite_number, matching_sources, outcome_metrics, summarize_outcomes
from .rule_audit import build_rule_audit
from .quote_sampling import china_time


DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "candidate_history.json"
BOARD_PLAN_FILE = Path(__file__).resolve().parents[2] / "data" / "board_plan_snapshots.json"
_LOCK = threading.RLock()
REVIEW_WINDOW_DAYS = 5


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _load() -> dict:
    if not DATA_FILE.exists():
        return {"version": 1, "rule_version": "2026.08.1", "days": {}}
    try:
        payload = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "rule_version": "2026.08.1", "days": {}}
    payload.setdefault("days", {})
    payload.setdefault("rule_version", "2026.08.1")
    return payload


def _save(payload: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATA_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, DATA_FILE)


def _load_board_plans() -> dict:
    if not BOARD_PLAN_FILE.exists():
        return {"version": 1, "days": {}}
    try:
        payload = json.loads(BOARD_PLAN_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "days": {}}
    payload.setdefault("days", {})
    return payload


def _save_board_plans(payload: dict) -> None:
    BOARD_PLAN_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = BOARD_PLAN_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, BOARD_PLAN_FILE)


def save_board_plan_snapshot(payload: dict, phase: str, *, replace: bool = True) -> dict:
    """保存完整竞价快照；原始观察、最终快照和最新版策略回放互不覆盖。"""
    if phase not in {"indicative", "final", "replay"}:
        raise ValueError("竞价快照阶段必须是 indicative、final 或 replay")
    day_key = str(payload.get("selected_date") or _snapshot_date(payload))
    snapshot = json.loads(json.dumps(payload, ensure_ascii=False))
    snapshot.update({
        "selected_date": day_key,
        "auction_phase": "historical" if phase == "replay" else phase,
        "snapshot_kind": "latest_strategy_replay" if phase == "replay" else "actual_final" if phase == "final" else "actual_indicative",
        "snapshot_label": "最新版策略历史回放" if phase == "replay" else "09:25当日最终快照" if phase == "final" else "09:20不可撤单观察快照",
        "frozen": phase == "final",
    })
    with _LOCK:
        database = _load_board_plans()
        day = database["days"].setdefault(day_key, {})
        if phase in day and not replace:
            return {"saved": False, "reason": "快照已存在", "date": day_key, "phase": phase}
        day[phase] = snapshot
        _save_board_plans(database)
    return {"saved": True, "date": day_key, "phase": phase, "count": len(snapshot.get("candidates") or [])}


def load_board_plan_snapshot(day_key: str, phase: str = "final") -> dict | None:
    with _LOCK:
        snapshot = (_load_board_plans().get("days", {}).get(day_key) or {}).get(phase)
    return json.loads(json.dumps(snapshot, ensure_ascii=False)) if snapshot else None


def load_recorded_board_plan(day_key: str) -> dict | None:
    """将早期保存的09:25精简候选还原为可展示页面。

    精简存档优先于09:31历史代理回放；未存储的字段保持未知，不补造数据。
    """
    with _LOCK:
        database = _load()
        source = (((database.get("days") or {}).get(day_key) or {}).get("sources") or {}).get("board")
    if not source or not source.get("candidates"):
        return None
    candidates = []
    for stored in source["candidates"]:
        gap = _number(stored.get("auction_gap_percent"))
        tradable = gap < 9.5
        candidates.append({
            **stored,
            "category": stored.get("industry") or "未分类",
            "auction_price": _number(stored.get("reference_price")),
            "action": stored.get("decision") or stored.get("signal") or "当日观察",
            "actionable": bool(stored.get("qualified")),
            "tradable": tradable,
            "tradability_label": (
                "当日09:25快照显示可等待开盘确认" if tradable
                else "一字板/近涨停 · 可挂单打板，但可能排不到"
            ),
            "checks": [],
            "snapshot_source": "当日09:25精简存档",
        })
    market = dict(source.get("market") or {})
    market.setdefault("score", 0)
    market.setdefault("state", "历史快照")
    market.setdefault("source", "当日09:25原始精简存档")
    return {
        "selected_date": day_key,
        "historical": True,
        "auction_phase": "historical",
        "stage": f"历史快照 · {day_key}",
        "market": market,
        "candidates": candidates,
        "actionable_count": sum(item["actionable"] for item in candidates),
        "screening": {
            "scanned": market.get("scanned", 0),
            "prefiltered": market.get("prefiltered", 0),
            "deep_scanned": market.get("deep_scanned", 0),
            "qualified_count": market.get("qualified_count", len(candidates)),
            "continuation_primary_count": sum(item.get("priority_tier") == "连板优先" for item in candidates),
            "one_to_two_count": sum(item.get("priority_tier") == "一进二观察" for item in candidates),
            "first_board_watch_count": sum(item.get("priority_tier") == "首板观察" for item in candidates),
            "source": "当日09:25原始精简存档",
            "method": source.get("method") or "",
            "snapshot_time": source.get("captured_at"),
        },
        "position_plan": {
            "max_positions": 0, "per_position": 0, "max_new_exposure": 0,
            "cash_reserve": 100_000,
            "rule": "历史精简快照只还原当时候选，不重新生成仓位。",
        },
        "strategy_profile": {
            "name": f"当日规则 {source.get('rule_version') or database.get('rule_version') or '--'}",
            "core_rule": source.get("method") or "以当日存档为准",
            "first_board_rule": "未留存字段显示为--，不用最新规则倒推。",
            "relay_rule": "候选名单与竞价价量来自当日冻结记录。",
            "reversal_rule": "如需最新规则对比，使用历史回放模式。",
            "risk_rule": "历史快照不构成当前交易信号。",
        },
        "generated_at": source.get("captured_at") or f"{day_key}T09:25:00+08:00",
        "snapshot_kind": "recorded_compact_final",
        "snapshot_label": "当日09:25原始候选快照（精简存档）",
        "frozen": True,
        "disclaimer": "本页优先还原当日09:25候选；早期精简存档未保留的字段不做推测。仅用于策略复盘。",
    }


def _snapshot_date(payload: dict) -> str:
    generated = str(payload.get("generated_at") or "")
    try:
        return datetime.fromisoformat(generated).date().isoformat()
    except ValueError:
        return date.today().isoformat()


def _compact_candidate(candidate: dict, source: str) -> dict:
    common = {
        "code": str(candidate.get("code") or ""),
        "name": str(candidate.get("name") or ""),
        "industry": str(candidate.get("industry") or candidate.get("category") or "未分类"),
        "score": _number(candidate.get("score")),
        "signal": str(candidate.get("signal") or candidate.get("action") or "观察"),
        "reference_price": _number(candidate.get("auction_price") if source == "board" else candidate.get("price")),
        "qualified": bool(candidate.get("actionable") if source == "board" else candidate.get("qualified")),
    }
    if source == "board":
        common.update({
            "decision": str(candidate.get("action") or "观察"),
            "auction_gap_percent": _number(candidate.get("auction_gap_percent")),
            "auction_amount": _number(candidate.get("auction_amount")),
            "auction_volume_percent": _number(candidate.get("auction_volume_percent")),
            "price_vs_ma5_percent": _number(candidate.get("price_vs_ma5_percent")),
            "decision_main_ratio": candidate.get("decision_main_ratio"),
            "recent_limit_up_count": int(_number(candidate.get("recent_limit_up_count"))),
            "consecutive_limit_up_days": int(_number(candidate.get("consecutive_limit_up_days"))),
            "continuation_score": _number(candidate.get("continuation_score")),
            "priority_tier": str(candidate.get("priority_tier") or ""),
            "reasons": list(candidate.get("reasons") or []),
            "risks": list(candidate.get("risks") or []),
        })
    else:
        common.update({
            "decision": "主板关注" if candidate.get("qualified") else "技术观察",
            "change_percent_at_selection": _number(candidate.get("change_percent")),
            "price_vs_ma5_percent": _number(candidate.get("price_vs_ma5_percent")),
            "volume_ratio": _number(candidate.get("volume_ratio")),
            "turnover_rate": _number(candidate.get("turnover_rate")),
            "risk_level": str(candidate.get("risk_level") or ""),
            "risks": list(candidate.get("risk_points") or []),
        })
    return common


def _board_source_from_plan(payload: dict) -> dict | None:
    """把完整 final/replay 快照转换成历史复盘使用的紧凑打板来源。"""
    candidates = payload.get("candidates") or []
    if not candidates:
        return None
    compacted = []
    historical_replay = payload.get("snapshot_kind") in {"latest_strategy_replay", "strategy_replay"} or bool(payload.get("historical"))
    for candidate in candidates:
        item = _compact_candidate(candidate, "board")
        if historical_replay:
            item["qualified"] = bool(
                candidate.get("eligible", True)
                and not candidate.get("risk_veto", False)
                and "取消候选" not in str(candidate.get("action") or "")
            )
        compacted.append(item)
    return {
        "source": "board",
        "captured_at": payload.get("generated_at") or datetime.now().astimezone().isoformat(timespec="seconds"),
        "rule_version": str(payload.get("strategy_version") or "--"),
        "market": payload.get("market"),
        "method": (payload.get("screening") or {}).get("method") or "",
        "snapshot_kind": payload.get("snapshot_kind") or "actual_final",
        "snapshot_label": payload.get("snapshot_label") or "打板快照",
        "historical_proxy": historical_replay,
        "replay_warning": (payload.get("screening") or {}).get("replay_warning"),
        "candidates": compacted,
    }


def _latest_board_source(day_key: str, stored_day: dict | None = None) -> dict | None:
    """最新版策略回放优先；缺失时回退当日精简记录和真实09:25快照。"""
    plans = (_load_board_plans().get("days") or {}).get(day_key) or {}
    replay = _board_source_from_plan(plans.get("replay") or {})
    if replay:
        return replay
    stored = (((stored_day or {}).get("sources") or {}).get("board"))
    if stored and stored.get("candidates"):
        return stored
    return _board_source_from_plan(plans.get("final") or {})


def _review_matches_source(review: dict | None, board: dict) -> bool:
    reviewed_board = ((review or {}).get("sources") or {}).get("board") or {}
    return matching_sources(reviewed_board, board)


def record_candidates(source: str, payload: dict) -> dict:
    """冻结每天首次有效候选快照，避免收盘后刷新覆盖早盘选择。"""
    if source not in {"board", "main_board"}:
        raise ValueError("未知候选来源")
    candidates = payload.get("candidates") or []
    if not candidates:
        return {"recorded": False, "reason": "没有有效候选"}
    day_key = _snapshot_date(payload)
    with _LOCK:
        database = _load()
        day = database["days"].setdefault(day_key, {"date": day_key, "sources": {}})
        sources = day.setdefault("sources", {})
        if source in sources and sources[source].get("candidates"):
            return {"recorded": False, "reason": "当日首次快照已冻结", "date": day_key}
        sources[source] = {
            "source": source,
            "captured_at": payload.get("generated_at") or datetime.now().astimezone().isoformat(timespec="seconds"),
            "rule_version": database["rule_version"],
            "market": payload.get("market") if source == "board" else None,
            "method": payload.get("method") or (payload.get("screening") or {}).get("method") or "",
            "candidates": [_compact_candidate(item, source) for item in candidates],
        }
        day.pop("review", None)
        _save(database)
    return {"recorded": True, "date": day_key, "count": len(candidates)}


def list_history() -> dict:
    with _LOCK:
        database = _load()
    days = sorted(database["days"].values(), key=lambda item: item["date"], reverse=True)
    return {"rule_version": database["rule_version"], "days": days, "storage": str(DATA_FILE)}


def _board_review_view(review: dict | None) -> dict | None:
    """只保留打板复盘结果，并按打板样本重新计算汇总。"""
    if not review:
        return None
    board = (review.get("sources") or {}).get("board")
    if not board:
        return None
    candidates = [_review_candidate(row, "board", row["outcome"], False) if row.get("outcome") else dict(row)
                  for row in board.get("candidates") or []]
    board = {**board, "candidates": candidates}
    counted_items = [item for item in candidates if item.get("counted")]
    successes = sum(bool(item.get("success")) for item in counted_items)
    return {
        **review,
        "counted": len(counted_items),
        "successes": successes,
        "accuracy_percent": round(successes / len(counted_items) * 100, 1) if counted_items else None,
        "diagnosis": "候选结果统计" if candidates else "无有效样本",
        "metric_label": "T+1涨停占比（非胜率）", "metrics": summarize_outcomes(candidates),
        "market_attribution_verified": False, "market_weak": None,
        "rule_adjustment": {
            "status": "结果仅作描述，不自动调整买入阈值",
            "suggestions": [],
            "principle": "T日封板、次日开盘/收盘溢价、次日涨停分开统计；候选不等于正式买点，不从同批候选涨跌推断市场原因。",
        },
        "sources": {"board": board},
    }


def list_board_history() -> dict:
    """只展示最近五个已留存交易日；不清理磁盘原始候选或复盘。"""
    history = list_history()
    stored_days = {str(day.get("date") or ""): day for day in history["days"]}
    plan_days = (_load_board_plans().get("days") or {})
    days = []
    now = china_time(datetime.now().astimezone())
    for day_key in sorted(set(stored_days) | set(plan_days), reverse=True):
        try:
            target = date.fromisoformat(day_key)
        except ValueError:
            continue
        if target > now.date() or target.weekday() >= 5:
            continue
        day = stored_days.get(day_key) or {"date": day_key, "sources": {}}
        board = _latest_board_source(day_key, day)
        if not board:
            continue
        item = {**day, "sources": {"board": board}}
        review = _board_review_view(day.get("review")) if _review_matches_source(day.get("review"), board) else None
        if review:
            item["review"] = review
        else:
            item.pop("review", None)
        days.append(item)
        if len(days) >= REVIEW_WINDOW_DAYS:
            break
    result = {**history, "days": days, "retention":{"display_days":REVIEW_WINDOW_DAYS,"physical_delete":False}}
    completed_day = now.date() if now.hour * 60 + now.minute >= 905 else now.date() - timedelta(days=1)
    result["rule_audit"] = build_rule_audit(result, as_of=now.date(),closed_through=completed_day,limit=REVIEW_WINDOW_DAYS)
    return result


def _closing_outcome(provider: EastmoneyProvider, code: str, target: date) -> dict:
    bars = provider.history(code, limit=160)
    index = next((i for i, bar in enumerate(bars) if bar.trade_date == target), None)
    if index is None:
        raise MarketDataError("当日收盘K线尚不可用")
    bar = bars[index]
    previous_close = bars[index - 1].close if index > 0 else 0.0
    daily_change = (bar.close / previous_close - 1) * 100 if previous_close else None
    result = {
        "close": round(bar.close, 2), "open": round(bar.open, 2),
        "high": round(bar.high, 2), "low": round(bar.low, 2),
        "daily_change_percent": round(daily_change, 2) if daily_change is not None else None,
        "same_day_limit_up": bool(daily_change is not None and daily_change >= 9.5),
        "same_day_sealed": bool(
            daily_change is not None and daily_change >= 9.5
            and abs(bar.close - bar.high) <= 0.011
        ),
    }
    now = china_time(datetime.now().astimezone())
    if index + 1 < len(bars) and (bars[index + 1].trade_date < now.date() or (
            bars[index + 1].trade_date == now.date() and now.hour * 60 + now.minute >= 905)):
        next_bar = bars[index + 1]
        next_change = (next_bar.close / bar.close - 1) * 100 if bar.close else None
        result["next_day"] = {
            "date": next_bar.trade_date.isoformat(),
            "open": round(next_bar.open, 2), "high": round(next_bar.high, 2),
            "low": round(next_bar.low, 2), "close": round(next_bar.close, 2),
            "open_return_percent": round((next_bar.open / bar.close - 1) * 100, 2) if bar.close else None,
            "high_return_percent": round((next_bar.high / bar.close - 1) * 100, 2) if bar.close else None,
            "close_return_percent": round(next_change, 2) if next_change is not None else None,
            "limit_up": bool(next_change is not None and next_change >= 9.5),
            "sealed": bool(
                next_change is not None and next_change >= 9.5
                and abs(next_bar.close - next_bar.high) <= 0.011
            ),
        }
    else:
        result["next_day"] = None
    return result


def _rule_cause(candidate: dict, source: str) -> tuple[str, str]:
    if source == "board":
        if candidate.get("auction_gap_percent", 0) >= 8.5:
            return "竞价高开过度", "收紧接近涨停开盘的权重，避免高位一致性透支"
        if candidate.get("auction_volume_percent", 0) < 0.5:
            return "竞价量能门槛偏松", "提高竞价量占近5日均量的最低要求"
        if candidate.get("decision_main_ratio") is not None and _number(candidate.get("decision_main_ratio")) < 0:
            return "资金过滤不足", "负主力资金候选降级，不进入A级候选"
        if candidate.get("recent_limit_up_count", 0) == 0:
            return "涨停活性不足", "无近20日涨停活性的股票提高准入分"
        return "综合评分阈值偏松", "复核高分失败样本，重新校准A级分数线"
    if candidate.get("price_vs_ma5_percent", 0) > 8:
        return "趋势偏离过大", "限制距离MA5过远的追涨候选"
    if candidate.get("volume_ratio", 0) < 0.8:
        return "量能确认不足", "提高主板候选最低量比"
    return "主板评分区分度不足", "增加板块强度和收盘承接权重"


def _review_candidate(candidate: dict, source: str, outcome: dict, market_weak: bool) -> dict:
    reference = _number(candidate.get("reference_price"))
    next_day = outcome.get("next_day") or {}
    if source == "board":
        return_percent = finite_number(next_day.get("close_return_percent"))
    else:
        return_percent = round((outcome["close"] / reference - 1) * 100, 2) if reference else None
    eligible = bool(candidate.get("qualified"))
    evaluation_ready = source != "board" or bool(
        next_day and ("limit_up" in next_day or next_day.get("close_return_percent") is not None)
    )
    counted = eligible and evaluation_ready
    success = bool(counted and (
        bool(next_day.get("limit_up"))
        if source == "board" else return_percent is not None and return_percent >= 1
    ))
    result = {**candidate, "outcome": outcome, "return_percent": return_percent, "counted": counted, "success": success}
    if source == "board":
        attribution, cause = board_attribution(result)
        result.update(attribution=attribution, cause=cause, rule_suggestion="仅描述结果，不自动归因为规则或市场",
                      metrics=outcome_metrics(result), execution_verified=False)
        return result
    if not eligible:
        result.update({"attribution": "未纳入准确率", "cause": "原规则已过滤", "rule_suggestion": "无需调整"})
    elif not evaluation_ready:
        result.update({"attribution": "待T+1复盘", "cause": "下一交易日行情尚未产生", "rule_suggestion": "等待T+1收盘后再统计"})
    elif success:
        result.update({"attribution": "规则有效", "cause": "T+1涨停" if source == "board" else "达到收盘验证标准", "rule_suggestion": "保持规则"})
    elif market_weak:
        result.update({"attribution": "市场问题", "cause": "同批主板候选普遍走弱，属于系统性环境压制", "rule_suggestion": "不因单日市场弱势修改选股参数"})
    else:
        cause, suggestion = _rule_cause(candidate, source)
        result.update({"attribution": "规则问题", "cause": cause, "rule_suggestion": suggestion})
    return result


def review_day(day_key: str, provider: EastmoneyProvider | None = None) -> dict:
    target = date.fromisoformat(day_key)
    provider = provider or EastmoneyProvider(timeout=8)
    with _LOCK:
        database = _load()
        day = database["days"].get(day_key) or {"date": day_key, "sources": {}}
        board = _latest_board_source(day_key, day)
        if not board:
            raise ValueError("该日期没有打板候选记录")
        sources = {"board": board}
    unique_codes = {item["code"] for source in sources.values() for item in source.get("candidates", [])}
    outcomes, errors = {}, {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_closing_outcome, provider, code, target): code for code in unique_codes}
        for future in as_completed(futures):
            code = futures[future]
            try:
                outcomes[code] = future.result()
            except Exception as exc:
                errors[code] = str(exc)

    reviewed_sources = {}
    for source_name, snapshot in sources.items():
        reviewed = []
        for candidate in snapshot.get("candidates", []):
            outcome = outcomes.get(candidate["code"])
            if not outcome:
                reviewed.append({**candidate, "error": errors.get(candidate["code"], "收盘数据不可用"), "counted": False})
                continue
            item = _review_candidate(candidate, source_name, outcome, False)
            reviewed.append(item)
        reviewed_sources[source_name] = {**snapshot, "candidates": reviewed}

    review = _board_review_view({
        "reviewed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sources": reviewed_sources,
    })
    with _LOCK:
        database = _load()
        day = database["days"].setdefault(day_key, {"date": day_key, "sources": {}})
        if not matching_sources(board, _latest_board_source(day_key, day) or {}):
            raise ValueError("复盘期间候选快照已变化，请重新复盘；未覆盖原记录")
        day.setdefault("sources", {})["board"] = board
        day["review"] = review
        _save(database)
    return {"date": day_key, **review}
