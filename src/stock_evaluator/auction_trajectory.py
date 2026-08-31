from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

from .market import EastmoneyProvider, MarketDataError
from .quote_sampling import quote_freshness


DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "auction_trajectory.json"
_LOCK = threading.RLock()


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _load() -> dict:
    if not DATA_FILE.exists():
        return {"version": 1, "days": {}}
    try:
        payload = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "days": {}}
    payload.setdefault("days", {})
    return payload


def _save(payload: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATA_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, DATA_FILE)


def _append_samples(day_key: str, samples: list[dict]) -> None:
    if not samples:
        return
    with _LOCK:
        database = _load()
        day = database["days"].setdefault(day_key, {"stocks": {}})
        stocks = day.setdefault("stocks", {})
        for sample in samples:
            code = str(sample.get("code") or "")
            if not code:
                continue
            rows = stocks.setdefault(code, [])
            timestamp = str(sample.get("quote_time") or sample.get("captured_at") or "")
            if rows and (rows[-1].get("quote_time") or rows[-1].get("captured_at")) == timestamp:
                rows[-1] = sample
            else:
                rows.append(sample)
            stocks[code] = rows[-30:]
        _save(database)


def record_payload_sample(payload: dict, phase: str) -> int:
    """记录一次完整筛选返回中的候选，final 使用真实09:25成交价量。"""
    day_key = str(payload.get("selected_date") or date.today().isoformat())
    captured_at = str(payload.get("generated_at") or datetime.now().astimezone().isoformat(timespec="seconds"))
    samples = []
    for candidate in payload.get("candidates") or []:
        samples.append({
            "code": str(candidate.get("code") or ""),
            "name": str(candidate.get("name") or ""),
            "captured_at": captured_at,
            "quote_time": candidate.get("auction_quote_time"),
            "phase": phase,
            "price": _number(candidate.get("auction_price")),
            "gap_percent": _number(candidate.get("auction_gap_percent")),
            "volume": _number(candidate.get("auction_volume")),
            "amount": _number(candidate.get("auction_amount")),
            "order_imbalance": candidate.get("auction_order_imbalance"),
            "bid_volume5": _number(candidate.get("auction_bid_volume5")),
            "ask_volume5": _number(candidate.get("auction_ask_volume5")),
        })
    _append_samples(day_key, samples)
    return len(samples)


def capture_watchlist(snapshot: dict, provider: EastmoneyProvider | None = None) -> dict:
    """轻量跟踪09:20观察池，不重复扫描全市场与历史K线。"""
    provider = provider or EastmoneyProvider(timeout=6)
    candidates = snapshot.get("candidates") or []
    now = datetime.now().astimezone()
    samples, errors = [], []

    def capture(candidate: dict) -> dict:
        quote = provider.quote(str(candidate["code"]))
        captured = datetime.now().astimezone()
        source_at, failure = quote_freshness({"quote_time": quote.quote_time}, captured)
        if failure or not source_at or not 920 <= source_at.hour * 100 + source_at.minute < 925:
            raise MarketDataError(failure or "行情不在09:20–09:25竞价窗口")
        previous_close = _number(quote.previous_close)
        return {
            "code": str(candidate["code"]), "name": quote.name,
            "captured_at": captured.isoformat(timespec="seconds"), "phase": "tracking",
            "quote_time": source_at.isoformat(timespec="seconds"),
            "price": quote.price,
            "gap_percent": round((quote.price / previous_close - 1) * 100, 2) if previous_close else 0,
            "volume": quote.volume, "amount": quote.amount,
            "order_imbalance": quote.order_imbalance,
            "bid_volume5": quote.bid_volume5, "ask_volume5": quote.ask_volume5,
        }

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(candidates)))) as executor:
        futures = {executor.submit(capture, item): item for item in candidates}
        for future in as_completed(futures):
            try:
                samples.append(future.result())
            except Exception as exc:
                errors.append({"code": str(futures[future].get("code") or ""), "error": str(exc)})
    _append_samples(str(snapshot.get("selected_date") or date.today().isoformat()), samples)
    return {
        "captured_at": now.isoformat(timespec="seconds"), "count": len(samples),
        "samples": samples, "errors": errors,
        "note": "仅跟踪09:20观察池的参考撮合价、累计量额和五档挂单；09:25仍以最终撮合成交为准。",
    }


def _profile(rows: list[dict]) -> dict:
    ordered = sorted(rows, key=lambda item: str(item.get("captured_at") or ""))
    gaps = [_number(item.get("gap_percent")) for item in ordered]
    bids = [_number(item.get("bid_volume5")) for item in ordered if _number(item.get("bid_volume5")) > 0]
    imbalances = [
        _number(item.get("order_imbalance")) for item in ordered
        if item.get("order_imbalance") is not None
    ]
    first_gap, final_gap = gaps[0], gaps[-1]
    gap_drift = final_gap - first_gap
    max_gap_drop = max(gaps) - final_gap
    bid_retention = bids[-1] / max(bids) if bids and max(bids) else None
    imbalance_drift = imbalances[-1] - imbalances[0] if len(imbalances) >= 2 else None
    late_price_deterioration = gap_drift <= -1.0 or max_gap_drop >= 1.5
    late_bid_withdrawal = bid_retention is not None and bid_retention < 0.5
    unstable = max(gaps) - min(gaps) >= 3.0
    risk_veto = gap_drift <= -2.5 or (late_price_deterioration and late_bid_withdrawal)
    reasons = []
    if late_price_deterioration:
        reasons.append(f"09:20–09:25参考撮合走弱{abs(min(gap_drift, -max_gap_drop)):.2f}个百分点")
    if late_bid_withdrawal:
        reasons.append(f"五档买单仅保留峰值的{bid_retention * 100:.0f}%")
    if unstable:
        reasons.append("不可撤单阶段参考价格波动过大")
    if not reasons and len(ordered) >= 2:
        reasons.append("09:20–09:25价量与挂单未明显恶化")
    return {
        "sample_count": len(ordered), "first_time": ordered[0].get("captured_at"),
        "final_time": ordered[-1].get("captured_at"),
        "first_gap_percent": round(first_gap, 2), "final_gap_percent": round(final_gap, 2),
        "gap_drift_percent": round(gap_drift, 2), "max_gap_drop_percent": round(max_gap_drop, 2),
        "bid_retention_percent": round(bid_retention * 100, 1) if bid_retention is not None else None,
        "imbalance_drift": round(imbalance_drift, 4) if imbalance_drift is not None else None,
        "late_price_deterioration": late_price_deterioration,
        "late_bid_withdrawal": late_bid_withdrawal, "unstable": unstable,
        "risk_veto": risk_veto, "reasons": reasons,
        "label": "竞价轨迹恶化" if risk_veto else "竞价轨迹需警惕" if late_price_deterioration or late_bid_withdrawal else "竞价轨迹稳定",
    }


def attach_trajectory(payload: dict) -> dict:
    """把09:20–09:25轨迹接入09:25最终名单，并对临近撮合恶化执行否决。"""
    day_key = str(payload.get("selected_date") or date.today().isoformat())
    with _LOCK:
        stocks = ((_load().get("days", {}).get(day_key) or {}).get("stocks") or {})
    for candidate in payload.get("candidates") or []:
        rows = list(stocks.get(str(candidate.get("code") or "")) or [])
        if not rows:
            continue
        profile = _profile(rows)
        candidate["auction_trajectory"] = profile
        candidate.setdefault("checks", []).append({
            "name": "09:20–09:25挂单稳定",
            "passed": not profile["risk_veto"],
            "note": "；".join(profile["reasons"]),
        })
        if profile["late_price_deterioration"] or profile["late_bid_withdrawal"]:
            candidate.setdefault("risks", []).extend(profile["reasons"])
            candidate["continuation_score"] = max(0, _number(candidate.get("continuation_score")) - (18 if profile["risk_veto"] else 8))
        if profile["risk_veto"]:
            candidate["risk_veto"] = True
            candidate["actionable"] = False
            candidate["action"] = "竞价尾段恶化 · 取消候选"
            candidate["decision"] = candidate["action"]
    return payload
