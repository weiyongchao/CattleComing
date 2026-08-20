from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .evaluator import evaluate
from .market import EastmoneyProvider, MarketDataError, secid_for
from .screener import is_main_board, is_risk_stock_name


HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}


def _fetch(url: str, params: dict, timeout: float = 12) -> dict:
    request = Request(f"{url}?{urlencode(params)}", headers=HEADERS)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except Exception as exc:
            last_error = exc
            time.sleep(0.2 * (attempt + 1))
    raise MarketDataError(f"所属板块服务连接失败：{last_error}")


def _items(payload: dict) -> list[dict]:
    diff = (payload.get("data") or {}).get("diff") or []
    return list(diff.values()) if isinstance(diff, dict) else diff


def _stock_boards(code: str) -> tuple[str, list[dict]]:
    secid = secid_for(code)
    info = _fetch("https://push2.eastmoney.com/api/qt/stock/get", {
        "fltt": 2, "invt": 2, "secid": secid, "fields": "f57,f58,f127",
    }).get("data") or {}
    industry = str(info.get("f127") or "未分类")
    boards = [{
        "code": str(row.get("f12") or ""), "name": str(row.get("f14") or ""),
        "change_percent": float(row.get("f3") or 0),
        "leader_name": str(row.get("f128") or "-"), "leader_code": str(row.get("f140") or ""),
    } for row in _items(_fetch("https://push2.eastmoney.com/api/qt/slist/get", {
        "fltt": 2, "invt": 2, "secid": secid, "spt": 3, "pi": 0, "pz": 200, "po": 1,
        "fields": "f12,f14,f3,f128,f140",
    }))]
    return industry, boards


def _primary_board(industry: str, boards: list[dict]) -> dict | None:
    exact = next((board for board in boards if board["name"] == industry), None)
    if exact:
        return exact
    return next((board for board in boards if not board["name"].endswith("_") and board["code"].startswith("BK")), None)


def stock_sector_peers(code: str, limit: int = 3) -> dict:
    provider = EastmoneyProvider(timeout=8)
    current = evaluate(provider.quote(code), provider.history(code))
    industry, boards = _stock_boards(code)
    primary = _primary_board(industry, boards)
    if not primary:
        raise MarketDataError("暂未找到该股票的可比行业板块")
    members = _items(_fetch("https://push2.eastmoney.com/api/qt/clist/get", {
        "pn": 1, "pz": 30, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f20",
        "fs": f"b:{primary['code']}", "fields": "f12,f14,f2,f3,f8,f10,f20",
    }))
    snapshots = {
        str(row.get("f12")): row for row in members
        if str(row.get("f12")) != code and is_main_board(str(row.get("f12") or ""))
        and not is_risk_stock_name(str(row.get("f14") or ""))
    }
    ranked, failed = [], 0
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(lambda c=peer: evaluate(provider.quote(c), provider.history(c))): peer
            for peer in list(snapshots)[:15]
        }
        for future in as_completed(futures):
            peer = futures[future]
            try:
                result, row = future.result(), snapshots[peer]
                ranked.append({
                    "code": peer, "name": result["quote"]["name"], "score": result["score"],
                    "rating": result["rating"], "score_delta": round(result["score"] - current["score"], 1),
                    "change_percent": result["quote"]["change_percent"],
                    "turnover_rate": result["metrics"]["turnover_rate"],
                    "volume_ratio": result["metrics"]["volume_ratio"],
                    "price_vs_ma5_percent": result["metrics"]["price_vs_ma5_percent"],
                    "market_cap": float(row.get("f20") or 0),
                    "signal": "技术面优于当前股" if result["score"] > current["score"] else "同板块备选",
                })
            except Exception:
                failed += 1
    ranked.sort(key=lambda item: (item["score"], item["market_cap"]), reverse=True)
    better = [item for item in ranked if item["score_delta"] > 0][:limit]
    display_boards = sorted(boards, key=lambda board: (board["name"] != industry, -board["change_percent"]))[:12]
    return {
        "code": code, "name": current["quote"]["name"], "industry": industry,
        "current_score": current["score"], "primary_board": primary, "boards": display_boards,
        "better_peers": better, "compared": len(ranked), "failed": failed,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "method": "优先使用东财细分行业板块，对非ST沪深主板成分股按与当前股相同的量价、MA5、换手和风险模型比较，仅列出评分更高的候选。",
        "disclaimer": "‘更优’仅指当前技术面评分更高，不代表公司基本面、估值或长期投资价值更优，不构成买入建议。",
    }
