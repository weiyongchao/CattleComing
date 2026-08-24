from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
import json
import re
import threading
import time
from urllib.request import Request, urlopen


POLICY_URL = "https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json"
GLOBAL_INDEX_URL = (
    "https://push2.eastmoney.com/api/qt/ulist.np/get?"
    "secids=100.DJIA,100.NDX,100.SPX,100.N225,100.KS11&fields=f12,f14,f2,f3,f4"
)
TENCENT_GLOBAL_URL = "https://qt.gtimg.cn/q=usDJI,usIXIC,usINX,jpN225,krKOSPI"
POLICY_THEMES = (
    ("科技自主", ("集成电路", "知识产权", "科学技术", "人工智能", "数字", "数据"),
     ("半导体", "电子", "计算机", "通信", "软件", "互联网", "元件", "消费电子")),
    ("医药健康", ("医药", "健康", "疾病", "医疗", "中医", "生物"),
     ("医药", "医疗", "生物", "制药", "中药")),
    ("绿色能源", ("碳达峰", "能源", "电力", "储能", "新能源", "绿色", "节能"),
     ("电力", "电网", "光伏", "风电", "环保", "能源", "电池", "电气")),
    ("扩大消费", ("扩大消费", "旅游强国", "体育强国", "全民健身", "消费品"),
     ("消费", "零售", "旅游", "酒店", "食品", "饮料", "家电", "纺织", "传媒", "文化", "汽车")),
    ("住房建设", ("住房", "房地产", "城市建设"),
     ("房地产", "建筑", "装修", "建材", "家居")),
    ("农业生态", ("农业", "粮食", "防汛", "水体", "林区", "自然资源", "生态环境"),
     ("农业", "种植", "农牧", "水务", "林业", "环保")),
    ("航天军工", ("商业航天", "航空航天产业", "国防科技工业", "军工产业"),
     ("航天", "航空", "军工", "船舶")),
)

_CACHE: tuple[float, dict] | None = None
_LOCK = threading.RLock()


def _json(url: str, timeout: float = 6) -> object:
    request = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.gov.cn/",
    })
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _text(url: str, timeout: float = 6) -> str:
    request = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124 Safari/537.36",
        "Referer": "https://gu.qq.com/",
    })
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("gb18030", errors="replace")


def _tencent_global_rows(payload: str) -> list[dict]:
    code_map = {"usDJI": "DJIA", "usIXIC": "NDX", "usINX": "SPX", "jpN225": "N225", "krKOSPI": "KS11"}
    rows = []
    for source_code, body in re.findall(r'v_([A-Za-z0-9]+)="([^"]*)"', payload):
        fields = body.split("~")
        if source_code not in code_map or len(fields) <= 32:
            continue
        try:
            rows.append({
                "f12": code_map[source_code], "f14": fields[1],
                "f2": float(fields[3]) * 100, "f3": float(fields[32]) * 100,
            })
        except (TypeError, ValueError):
            continue
    return rows


def _policy_signals(items: list[dict], current_date: date | None = None) -> list[dict]:
    current_date = current_date or date.today()
    signals = []
    for theme, title_keywords, industries in POLICY_THEMES:
        matches = []
        for item in items:
            title = str(item.get("TITLE") or "").strip()
            try:
                published = datetime.strptime(str(item.get("DOCRELPUBTIME") or ""), "%Y-%m-%d").date()
            except ValueError:
                continue
            age = (current_date - published).days
            if age < 0 or age > 90 or not any(keyword in title for keyword in title_keywords):
                continue
            matches.append({
                "title": title, "date": published.isoformat(), "url": str(item.get("URL") or ""),
                "age_days": age,
            })
        if not matches:
            continue
        matches.sort(key=lambda row: row["date"], reverse=True)
        newest_age = matches[0]["age_days"]
        score = 4 if newest_age <= 30 else 3 if newest_age <= 60 else 2
        signals.append({
            "theme": theme, "score": score, "industry_keywords": list(industries),
            "policies": matches[:3],
        })
    return sorted(signals, key=lambda row: (row["score"], row["policies"][0]["date"]), reverse=True)


def _global_summary(rows: list[dict]) -> dict:
    indexes = [{
        "code": str(row.get("f12") or ""), "name": str(row.get("f14") or ""),
        "price": round(float(row.get("f2") or 0) / 100, 2),
        "change_percent": round(float(row.get("f3") or 0) / 100, 2),
    } for row in rows]
    by_code = {row["code"]: row for row in indexes}
    us_values = [by_code[code]["change_percent"] for code in ("DJIA", "NDX", "SPX") if code in by_code]
    asia_values = [by_code[code]["change_percent"] for code in ("N225", "KS11") if code in by_code]
    us_average = sum(us_values) / len(us_values) if us_values else 0.0
    asia_average = sum(asia_values) / len(asia_values) if asia_values else 0.0
    adjustment = round(max(-4.0, min(3.0, us_average * 1.5 + asia_average)), 1)
    state = "外围偏强" if adjustment >= 1 else "外围承压" if adjustment <= -1 else "外围中性"
    return {
        "available": bool(indexes), "state": state, "adjustment": adjustment,
        "us_average": round(us_average, 2), "asia_average": round(asia_average, 2), "indexes": indexes,
    }


def _fetch_policy() -> dict:
    payload = _json(POLICY_URL)
    items = payload if isinstance(payload, list) else []
    return {"available": bool(items), "source": "中国政府网", "source_url": POLICY_URL,
            "signals": _policy_signals(items)}


def _fetch_global() -> dict:
    source, source_url = "东方财富全球指数", GLOBAL_INDEX_URL
    try:
        rows = []
        last_error = None
        for attempt in range(3):
            try:
                payload = _json(GLOBAL_INDEX_URL)
                rows = ((payload or {}).get("data") or {}).get("diff") or [] if isinstance(payload, dict) else []
                if not rows:
                    raise ValueError("全球指数返回空数据")
                break
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.2 * (attempt + 1))
        if not rows:
            raise last_error or ValueError("全球指数返回空数据")
    except Exception:
        rows = _tencent_global_rows(_text(TENCENT_GLOBAL_URL))
        source, source_url = "腾讯全球行情（备用）", TENCENT_GLOBAL_URL
    result = _global_summary(rows)
    result.update({"source": source, "source_url": source_url})
    return result


def external_market_context(cache_seconds: int = 900) -> dict:
    global _CACHE
    with _LOCK:
        if _CACHE and time.time() - _CACHE[0] < cache_seconds:
            return _CACHE[1]
    with ThreadPoolExecutor(max_workers=2) as executor:
        policy_future = executor.submit(_fetch_policy)
        global_future = executor.submit(_fetch_global)
        try:
            policy = policy_future.result()
        except Exception as exc:
            policy = {"available": False, "source": "中国政府网", "signals": [], "error": str(exc)}
        try:
            global_market = global_future.result()
        except Exception as exc:
            global_market = {"available": False, "source": "东方财富全球指数", "state": "外围数据暂不可用",
                             "adjustment": 0.0, "indexes": [], "error": str(exc)}
    result = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "policy": policy, "global_market": global_market,
        "note": "政策与外围市场仅用于行业倾向和风险修正，个股仍须先通过量价、趋势、资金与回撤门槛。",
    }
    with _LOCK:
        _CACHE = (time.time(), result)
    return result


def apply_external_context(candidate: dict, context: dict) -> dict:
    industry = str(candidate.get("industry") or "")
    policy_matches = [
        signal for signal in context.get("policy", {}).get("signals", [])
        if any(keyword in industry for keyword in signal.get("industry_keywords", []))
    ]
    policy_bonus = max((signal["score"] for signal in policy_matches), default=0)
    global_market = context.get("global_market", {})
    global_adjustment = float(global_market.get("adjustment") or 0) * 0.5
    indexes = {row.get("code"): row for row in global_market.get("indexes", [])}
    if any(keyword in industry for keyword in ("半导体", "电子", "计算机", "通信", "软件", "互联网")):
        global_adjustment += float(indexes.get("NDX", {}).get("change_percent") or 0) * 0.5
    adjustment = round(max(-4.0, min(6.0, policy_bonus + global_adjustment)), 1)
    result = dict(candidate)
    result["base_opportunity_score"] = candidate["opportunity_score"]
    result["macro_adjustment"] = adjustment
    result["policy_themes"] = [signal["theme"] for signal in policy_matches]
    result["policy_references"] = [policy for signal in policy_matches for policy in signal["policies"][:1]][:2]
    result["global_state"] = global_market.get("state", "外围数据暂不可用")
    result["opportunity_score"] = round(max(0, min(100, candidate["opportunity_score"] + adjustment)), 1)
    result["display_score"] = result["opportunity_score"]
    result["opportunity_level"] = "精选A" if result["opportunity_score"] >= 90 else "精选B"
    if policy_matches:
        result["reasons"] = list(result.get("reasons") or []) + [f"近期政策：{policy_matches[0]['theme']}"]
    if global_adjustment <= -1:
        result["risks"] = list(result.get("risks") or []) + [f"{result['global_state']}，追高仓位宜降低"]
    return result
