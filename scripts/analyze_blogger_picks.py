from __future__ import annotations

import json
from datetime import date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.stock_evaluator.auction import _auction_candidate
from src.stock_evaluator.history import load_board_plan_snapshot
from src.stock_evaluator.market import EastmoneyProvider
from src.stock_evaluator.screener import is_main_board
from src.stock_evaluator.universe import main_board_snapshots


PICKS = {
    "2026-08-18": {
        "600127": "金健米业", "600353": "旭光电子", "603773": "沃格光电", "000620": "盈新发展",
    },
    "2026-08-19": {"600613": "神奇制药", "600313": "农发种业"},
    "2026-08-20": {"600613": "神奇制药", "300765": "石药创新"},
    "2026-08-21": {"002412": "汉森制药", "600613": "神奇制药"},
    "2026-08-24": {"002412": "汉森制药", "603958": "哈森股份"},
}


def main() -> None:
    provider = EastmoneyProvider(timeout=10)
    snapshots = {str(row.get("f12") or ""): row for row in main_board_snapshots(cache_seconds=120, require_price=False)}
    results: list[dict] = []
    for day_key, stocks in PICKS.items():
        target = date.fromisoformat(day_key)
        replay = load_board_plan_snapshot(day_key, "replay") or {}
        selected = {str(item.get("code") or "") for item in replay.get("candidates") or []}
        for code, supplied_name in stocks.items():
            row = snapshots.get(code)
            if not is_main_board(code):
                results.append({
                    "date": day_key, "code": code, "name": supplied_name,
                    "main_board": False, "system_selected": False, "excluded_reason": "非沪深主板",
                })
                continue
            try:
                history = provider.history(code, 160)
                candidate = _auction_candidate(dict(row or {"f12": code, "f14": supplied_name}), provider, target, history)
                target_bar = next((bar for bar in history if bar.trade_date == target), None)
                if not candidate:
                    raise RuntimeError("候选特征不足")
                results.append({
                    "date": day_key, "code": code, "name": candidate.get("name") or supplied_name,
                    "main_board": True, "system_selected": code in selected,
                    "eligible": candidate.get("eligible"), "mode": candidate.get("strategy_mode"),
                    "tier": candidate.get("priority_tier"), "score": candidate.get("score"),
                    "continuation": candidate.get("continuation_base_score"),
                    "previous_limit_up": candidate.get("previous_day_limit_up"),
                    "consecutive_limit_ups": candidate.get("consecutive_limit_up_days"),
                    "recent_5_limit_ups": candidate.get("recent_5_limit_up_count"),
                    "recent_10_limit_ups": candidate.get("recent_10_limit_up_count"),
                    "previous_amount_yi": round(float(candidate.get("previous_amount") or 0) / 1e8, 2),
                    "previous_volume_ratio": candidate.get("previous_volume_ratio"),
                    "previous_close_position": candidate.get("previous_close_position_percent"),
                    "previous_upper_shadow": candidate.get("previous_upper_shadow_ratio"),
                    "auction_gap": candidate.get("auction_gap_percent"),
                    "auction_amount_yi": round(float(candidate.get("auction_amount") or 0) / 1e8, 2),
                    "auction_volume_percent": candidate.get("auction_volume_percent"),
                    "auction_turnover": candidate.get("auction_turnover_percent"),
                    "float_cap_yi": round(float(candidate.get("float_market_cap") or 0) / 1e8, 2),
                    "three_day_change": candidate.get("three_day_change_percent"),
                    "five_day_change": candidate.get("five_day_change_percent"),
                    "ten_day_change": candidate.get("ten_day_change_percent"),
                    "risk_veto": candidate.get("risk_veto"),
                    "target_change": round((target_bar.close / target_bar.open - 1) * 100, 2) if target_bar and target_bar.open else None,
                    "target_close_change": round((target_bar.close / candidate["previous_close"] - 1) * 100, 2) if target_bar and candidate.get("previous_close") else None,
                    "reasons": candidate.get("reasons"), "risks": candidate.get("risks"),
                })
            except Exception as exc:
                results.append({
                    "date": day_key, "code": code, "name": supplied_name,
                    "main_board": True, "system_selected": False, "error": str(exc),
                })
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
