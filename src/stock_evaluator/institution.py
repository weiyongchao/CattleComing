from __future__ import annotations

import json
import threading
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .market import EastmoneyProvider, MarketDataError


_CACHE: dict[str, tuple[float, dict[str, dict]]] = {}
_PREVIOUS_DATE_CACHE: dict[str, date] = {}
_LOCK = threading.RLock()
_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "institution_snapshots.json"


def _load_disk() -> dict:
    if not DATA_FILE.exists():
        return {"version": 1, "days": {}}
    try:
        payload = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        payload.setdefault("days", {})
        return payload
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "days": {}}


def _save_disk(day_key: str, rows: dict[str, dict]) -> None:
    payload = _load_disk()
    payload["days"][day_key] = rows
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATA_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(DATA_FILE)


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def previous_trade_date(provider: EastmoneyProvider, target_date: date) -> date:
    with _LOCK:
        if target_date.isoformat() in _PREVIOUS_DATE_CACHE:
            return _PREVIOUS_DATE_CACHE[target_date.isoformat()]
    bars = provider.history("600519", limit=16)
    previous = [bar.trade_date for bar in bars if bar.trade_date < target_date]
    if not previous:
        raise MarketDataError("无法确定机构龙虎榜对应的上一交易日")
    result = previous[-1]
    with _LOCK:
        _PREVIOUS_DATE_CACHE[target_date.isoformat()] = result
    return result


def institutional_trades(trade_date: date, timeout: float = 10) -> dict[str, dict]:
    day_key = trade_date.isoformat()
    with _LOCK:
        cached = _CACHE.get(day_key)
        if cached and time.time() - cached[0] < 900:
            return cached[1]
    params = {
        "reportName": "RPT_ORGANIZATION_TRADE_DETAILS", "columns": "ALL",
        "filter": f"(TRADE_DATE='{day_key}')", "pageNumber": 1, "pageSize": 500,
        "sortColumns": "NET_BUY_AMT", "sortTypes": -1, "source": "WEB", "client": "WEB",
    }
    request = Request(
        f"{_URL}?{urlencode(params)}",
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/stock/"},
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
            rows = (payload.get("result") or {}).get("data") or []
            result = {str(row.get("SECURITY_CODE") or ""): row for row in rows if row.get("SECURITY_CODE")}
            with _LOCK:
                _CACHE[day_key] = (time.time(), result)
                _save_disk(day_key, result)
            return result
        except Exception as exc:
            last_error = exc
            time.sleep(0.2 * (attempt + 1))
    with _LOCK:
        disk_rows = (_load_disk().get("days", {}).get(day_key) or {})
        if disk_rows:
            _CACHE[day_key] = (time.time(), disk_rows)
            return disk_rows
    raise MarketDataError(f"机构龙虎榜服务连接失败：{last_error}")


def institutional_risk(
    row: dict | None, gap_percent: float, price_vs_ma5: float,
    consecutive_limit_ups: int, available: bool = True,
) -> dict:
    if not available:
        return {
            "available": False, "on_list": None, "level": "unknown", "risk_score": 0,
            "label": "机构龙虎榜暂不可用", "trade_date": None,
            "buy_count": None, "sell_count": None, "net_ratio": None,
            "turnover_rate": None, "reasons": [], "risk_veto": False,
        }
    if not row:
        return {
            "available": True, "on_list": False, "level": "normal", "risk_score": 0,
            "label": "前一交易日未上机构龙虎榜", "trade_date": None,
            "buy_count": 0, "sell_count": 0, "net_ratio": 0.0, "turnover_rate": None,
            "reasons": [], "risk_veto": False,
        }
    buy_count = int(_number(row.get("BUY_COUNT") or row.get("BUY_TIMES")))
    sell_count = int(_number(row.get("SELL_COUNT") or row.get("SELL_TIMES")))
    buy_amount, sell_amount = _number(row.get("BUY_AMT")), _number(row.get("SELL_AMT"))
    net_ratio = _number(row.get("RATIO"))
    turnover = _number(row.get("TURNOVERRATE"))
    score, reasons = 0, []
    if net_ratio <= -3:
        score += 28; reasons.append("机构席位净卖出占比较高")
    elif net_ratio >= 3 and turnover >= 15:
        score += 14; reasons.append("机构净买伴随高换手，次日存在兑现压力")
    if buy_amount > 0 and sell_amount / buy_amount >= 0.8:
        score += 12; reasons.append("机构买卖金额接近，席位分歧较大")
    if sell_count > buy_count:
        score += 10; reasons.append("卖方机构席位数量多于买方")
    if gap_percent >= 5 and net_ratio > 0:
        score += 12; reasons.append("机构净买后次日高开，需防借强兑现")
    if price_vs_ma5 >= 12:
        score += 12; reasons.append("股价高位偏离MA5，机构筹码存在止盈空间")
    if consecutive_limit_ups >= 2:
        score += 8; reasons.append("已进入连板高位，机构兑现冲击可能放大")
    score = min(100, score)
    level = "high" if score >= 35 else "watch" if score >= 18 else "normal"
    risk_veto = level == "high" and (gap_percent >= 7 or price_vs_ma5 >= 15 or consecutive_limit_ups >= 3)
    return {
        "available": True, "on_list": True, "level": level, "risk_score": score,
        "label": "机构次日兑现高风险" if level == "high" else "机构席位需观察" if level == "watch" else "机构席位风险常规",
        "trade_date": str(row.get("TRADE_DATE") or "")[:10],
        "buy_count": buy_count, "sell_count": sell_count,
        "net_ratio": round(net_ratio, 2), "turnover_rate": round(turnover, 2),
        "reason": str(row.get("EXPLANATION") or ""), "reasons": reasons,
        "risk_veto": risk_veto,
    }


def institutional_map_before(provider: EastmoneyProvider, target_date: date) -> tuple[str, dict[str, dict]]:
    previous = previous_trade_date(provider, target_date)
    return previous.isoformat(), institutional_trades(previous)
