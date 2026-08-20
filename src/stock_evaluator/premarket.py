from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

from .auction import _completed_bars, _history_prefilter_score, _number, _recent_limit_up_count
from .market import DailyBar, EastmoneyProvider
from .screener import is_main_board, is_risk_stock_name
from .universe import main_board_snapshots


def _premarket_candidate(snapshot: dict, bars: list[DailyBar], target_date: date) -> dict | None:
    code = str(snapshot.get("f12") or "")
    name = "".join(str(snapshot.get("f14") or code).split())
    if not is_main_board(code) or is_risk_stock_name(name):
        return None
    completed = _completed_bars(bars, target_date)[-25:]
    if not completed or (target_date - completed[-1].trade_date).days > 10:
        return None
    score = _history_prefilter_score(bars, target_date)
    if score is None or len(completed) < 11:
        return None
    closes = [bar.close for bar in completed]
    volumes = [bar.volume for bar in completed]
    recent = completed[-5:]
    low5, high5 = min(bar.low for bar in recent), max(bar.high for bar in recent)
    close_position = (closes[-1] - low5) / (high5 - low5) * 100 if high5 > low5 else 50
    day_range = completed[-1].high - completed[-1].low
    upper_shadow = (
        (completed[-1].high - max(completed[-1].open, completed[-1].close)) / day_range
        if day_range > 0 else 0
    )
    previous_avg = sum(volumes[-6:-1]) / 5
    previous_volume_ratio = volumes[-1] / previous_avg if previous_avg else 0
    limit_ups = _recent_limit_up_count(completed)
    three_day = (closes[-1] / closes[-4] - 1) * 100 if closes[-4] else 0
    five_day = (closes[-1] / closes[-6] - 1) * 100 if closes[-6] else 0
    ten_day = (closes[-1] / closes[-11] - 1) * 100 if closes[-11] else 0
    reasons = []
    if limit_ups:
        reasons.append(f"近20日{limit_ups}次涨停")
    if close_position >= 85:
        reasons.append("前日强势收盘")
    if upper_shadow <= 0.2:
        reasons.append("前日上影较短")
    if 0.5 <= previous_volume_ratio <= 3.5:
        reasons.append("前日量能可延续")
    return {
        "code": code, "name": name,
        "industry": str(snapshot.get("f100") or "未分类"),
        "score": round(score, 1), "previous_close": round(closes[-1], 2),
        "three_day_change_percent": round(three_day, 2),
        "five_day_change_percent": round(five_day, 2),
        "ten_day_change_percent": round(ten_day, 2),
        "previous_volume_ratio": round(previous_volume_ratio, 2),
        "previous_close_position_percent": round(close_position, 1),
        "previous_upper_shadow_ratio": round(upper_shadow, 3),
        "recent_limit_up_count": limit_ups,
        "market_cap": _number(snapshot.get("f20")),
        "reasons": reasons,
    }


def build_premarket_watchlist(
    target_date: date | None = None, provider: EastmoneyProvider | None = None, limit: int = 6,
) -> dict:
    target_date = target_date or date.today()
    provider = provider or EastmoneyProvider(timeout=8)
    snapshots = main_board_snapshots(cache_seconds=120, require_price=False)
    candidates, failed = [], 0
    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = {
            executor.submit(provider.history, str(snapshot.get("f12") or ""), 80): snapshot
            for snapshot in snapshots
            if is_main_board(str(snapshot.get("f12") or ""))
            and not is_risk_stock_name(str(snapshot.get("f14") or ""))
        }
        for future in as_completed(futures):
            snapshot = futures[future]
            try:
                candidate = _premarket_candidate(snapshot, future.result(), target_date)
                if candidate:
                    candidates.append(candidate)
            except Exception:
                failed += 1
    candidates.sort(key=lambda item: (item["score"], item["recent_limit_up_count"], item["market_cap"]), reverse=True)
    if candidates and candidates[0]["score"] >= 75:
        floor = max(75, candidates[0]["score"] - 12)
        selected = [item for item in candidates if item["score"] >= floor][:limit]
    else:
        selected = []
    return {
        "date": target_date.isoformat(), "phase": "09:00盘前预选",
        "candidates": selected, "scanned": len(snapshots), "ranked_count": len(candidates),
        "failed": failed, "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "method": "仅使用目标日前已完成K线，按涨停活性、前日收盘位置、上影、近3/5/10日走势和前日量比动态选取最多6只。",
        "disclaimer": "盘前预选不包含当日集合竞价、封单撤单和题材消息，只是09:25最终筛选的输入池，不是买入信号。",
    }
