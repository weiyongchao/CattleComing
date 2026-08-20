from __future__ import annotations

import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .market import MarketDataError


SEARCH_URL = "https://searchapi.eastmoney.com/api/suggest/get"
SEARCH_TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"


def _parse_suggestions(payload: dict, query: str, limit: int = 8) -> list[dict]:
    rows = ((payload.get("QuotationCodeTable") or {}).get("Data") or [])
    normalized = "".join(query.split()).lower()
    matches: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        code = str(row.get("Code") or row.get("UnifiedCode") or "").strip()
        name = "".join(str(row.get("Name") or "").split())
        pinyin = str(row.get("PinYin") or "").strip().lower()
        if row.get("Classify") != "AStock" or len(code) != 6 or not code.isdigit() or code in seen:
            continue
        if normalized not in code.lower() and normalized not in name.lower() and normalized not in pinyin:
            continue
        seen.add(code)
        matches.append({
            "code": code,
            "name": name,
            "market": str(row.get("SecurityTypeName") or "A股"),
            "exact": normalized in {code.lower(), name.lower()},
        })
    matches.sort(key=lambda item: (
        not item["exact"],
        not item["name"].lower().startswith(normalized),
        item["code"],
    ))
    return matches[:max(1, min(limit, 20))]


def search_stocks(query: str, limit: int = 8) -> list[dict]:
    normalized = "".join(str(query or "").split())
    if not normalized:
        return []
    params = urlencode({"input": normalized, "type": 14, "token": SEARCH_TOKEN, "count": limit})
    request = Request(
        f"{SEARCH_URL}?{params}",
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
    )
    try:
        with urlopen(request, timeout=8) as response:
            payload = json.load(response)
    except Exception as exc:
        raise MarketDataError(f"股票名称检索服务连接失败：{exc}") from exc
    return _parse_suggestions(payload, normalized, limit)


def resolve_stock_code(query: str) -> str:
    normalized = "".join(str(query or "").split())
    if len(normalized) == 6 and normalized.isdigit():
        return normalized
    matches = search_stocks(normalized)
    exact = [item for item in matches if item["exact"]]
    if len(exact) == 1:
        return exact[0]["code"]
    if len(matches) == 1:
        return matches[0]["code"]
    if not matches:
        raise ValueError(f"未找到股票“{normalized}”，请检查名称或输入6位代码")
    names = "、".join(f"{item['name']}（{item['code']}）" for item in matches[:5])
    raise ValueError(f"名称匹配到多只股票，请从候选中选择：{names}")
