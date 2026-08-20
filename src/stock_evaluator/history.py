from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

from .market import EastmoneyProvider, MarketDataError


DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "candidate_history.json"
BOARD_PLAN_FILE = Path(__file__).resolve().parents[2] / "data" / "board_plan_snapshots.json"
_LOCK = threading.RLock()


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
    """保存可直接还原页面的完整竞价快照；观察快照和最终快照互不覆盖。"""
    if phase not in {"indicative", "final"}:
        raise ValueError("竞价快照阶段必须是 indicative 或 final")
    day_key = str(payload.get("selected_date") or _snapshot_date(payload))
    snapshot = json.loads(json.dumps(payload, ensure_ascii=False))
    snapshot.update({
        "selected_date": day_key,
        "auction_phase": phase,
        "snapshot_kind": "actual_final" if phase == "final" else "actual_indicative",
        "snapshot_label": "09:25当日最终快照" if phase == "final" else "09:20不可撤单观察快照",
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
    if index + 1 < len(bars):
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
        return_percent = _number(next_day.get("close_return_percent")) if next_day else None
    else:
        return_percent = round((outcome["close"] / reference - 1) * 100, 2) if reference else None
    eligible = bool(candidate.get("qualified"))
    success = bool(eligible and (
        bool(next_day.get("limit_up"))
        if source == "board" else return_percent is not None and return_percent >= 1
    ))
    result = {**candidate, "outcome": outcome, "return_percent": return_percent, "counted": eligible, "success": success}
    if not eligible:
        result.update({"attribution": "未纳入准确率", "cause": "原规则已过滤", "rule_suggestion": "无需调整"})
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
        day = database["days"].get(day_key)
        if not day:
            raise ValueError("该日期没有候选记录")
        sources = day.get("sources") or {}
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

    main_returns = []
    for candidate in (sources.get("main_board") or {}).get("candidates", []):
        outcome = outcomes.get(candidate["code"])
        if outcome and candidate.get("qualified") and candidate.get("reference_price"):
            main_returns.append((outcome["close"] / candidate["reference_price"] - 1) * 100)
    market_weak = bool(main_returns and sum(value < 0 for value in main_returns) / len(main_returns) >= 0.7 and sum(main_returns) / len(main_returns) <= -0.8)

    reviewed_sources, counted, successes, rule_issues, market_issues = {}, 0, 0, [], 0
    for source_name, snapshot in sources.items():
        reviewed = []
        for candidate in snapshot.get("candidates", []):
            outcome = outcomes.get(candidate["code"])
            if not outcome:
                reviewed.append({**candidate, "error": errors.get(candidate["code"], "收盘数据不可用"), "counted": False})
                continue
            item = _review_candidate(candidate, source_name, outcome, market_weak)
            reviewed.append(item)
            if item["counted"]:
                counted += 1
                successes += int(item["success"])
                if item["attribution"] == "规则问题":
                    rule_issues.append(item)
                elif item["attribution"] == "市场问题":
                    market_issues += 1
        reviewed_sources[source_name] = {**snapshot, "candidates": reviewed}

    accuracy = round(successes / counted * 100, 1) if counted else None
    if rule_issues:
        diagnosis = "规则问题"
    elif market_issues:
        diagnosis = "市场问题"
    elif counted:
        diagnosis = "规则有效"
    else:
        diagnosis = "无有效样本"
    suggestions = sorted({item["rule_suggestion"] for item in rule_issues})
    adjustment = {
        "status": "达到复核线，建议人工确认后调整" if len(rule_issues) >= 3 and counted >= 5 else "样本不足，继续观察",
        "suggestions": suggestions,
        "principle": "单日不自动改参数；同类失败至少3例且有效样本不少于5例，才进入规则调整。",
    }
    review = {
        "reviewed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "counted": counted, "successes": successes, "accuracy_percent": accuracy,
        "market_weak": market_weak, "diagnosis": diagnosis, "rule_adjustment": adjustment,
        "sources": reviewed_sources,
    }
    with _LOCK:
        database = _load()
        database["days"][day_key]["review"] = review
        _save(database)
    return {"date": day_key, **review}
