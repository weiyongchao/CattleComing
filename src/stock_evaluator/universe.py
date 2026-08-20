from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .market import MarketDataError
from .screener import is_main_board, is_risk_stock_name


HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
FIELDS = "f12,f14,f2,f3,f5,f6,f8,f10,f20,f21,f26,f62,f100,f124,f184"
_CACHE: dict[bool, tuple[float, list[dict]]] = {}


def _number(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _page(page: int, page_size: int = 100) -> tuple[int, list[dict]]:
    params = {
        "pn": page, "pz": page_size, "po": 1, "np": 1, "fltt": 2, "invt": 2,
        "fid": "f6", "fs": "m:0+t:6,m:1+t:2", "fields": FIELDS,
    }
    request = Request(
        "https://push2delay.eastmoney.com/api/qt/clist/get?" + urlencode(params), headers=HEADERS
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=15) as response:
                payload = json.load(response)
            data = payload.get("data") or {}
            return int(data.get("total") or 0), data.get("diff") or []
        except Exception as exc:
            last_error = exc
            time.sleep(0.15 * (attempt + 1))
    raise MarketDataError(f"全主板股票池第{page}页连接失败：{last_error}")


def main_board_snapshots(cache_seconds: int = 20, require_price: bool = True) -> list[dict]:
    cached = _CACHE.get(require_price)
    if cached and time.time() - cached[0] < cache_seconds:
        return cached[1]
    total, first = _page(1)
    if total < 1000:
        raise MarketDataError(f"全主板股票池数量异常：{total}")
    pages = math.ceil(total / 100)
    rows = list(first)
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(_page, page) for page in range(2, pages + 1)]
        for future in as_completed(futures):
            _, page_rows = future.result()
            rows.extend(page_rows)
    cleaned = [
        row for row in rows
        if is_main_board(str(row.get("f12") or ""))
        and not is_risk_stock_name(str(row.get("f14") or ""))
        and (not require_price or _number(row.get("f2")) > 0)
    ]
    unique = {str(row.get("f12")): row for row in cleaned}
    result = list(unique.values())
    if len(result) < 1000:
        mode = "带实时价格" if require_price else "基础代码"
        raise MarketDataError(f"全主板股票池清洗后数量异常（{mode}）：{len(result)}")
    _CACHE[require_price] = (time.time(), result)
    return result
