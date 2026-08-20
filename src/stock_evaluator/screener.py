from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import time

from .evaluator import evaluate
from .market import EastmoneyProvider


# 沪深主板主题代表股观察池；用于扩大样本，不代表基本面龙头认定。
LEADER_GROUPS = {
    "科技硬件": {
        "002371": "半导体设备", "002475": "消费电子", "002463": "PCB", "002916": "PCB",
        "002938": "PCB", "000725": "面板", "000100": "面板", "002241": "光学",
        "600584": "封测", "603986": "芯片设计", "002049": "封测", "600183": "电子材料",
    },
    "大科技应用": {
        "000063": "通信设备", "002415": "安防", "002230": "人工智能", "600536": "网络安全",
        "600570": "金融科技", "002410": "软件", "600588": "企业软件", "000977": "算力",
        "002439": "数据中心", "603019": "数据中心", "600498": "光通信", "600487": "光通信",
    },
    "电力能源": {
        "600900": "水电", "600025": "水电", "601985": "核电", "003816": "核电",
        "600011": "火电", "600027": "火电", "600795": "火电", "600905": "绿电",
        "000027": "综合电力", "000883": "电力", "601877": "电气设备", "600406": "电网设备",
    },
    "医药健康": {
        "600276": "创新药", "600196": "综合医药", "000538": "中药", "600085": "中药",
        "600436": "中药", "000963": "医药商业", "002007": "生物制品", "002422": "医疗服务",
        "600332": "中药", "000661": "生物制品", "600161": "疫苗", "002653": "医疗器械",
    },
}
LEADER_POOL = {code: industry for group in LEADER_GROUPS.values() for code, industry in group.items()}
_CONTEXT_CACHE: dict[str, tuple[float, dict]] = {}


def is_main_board(code: str) -> bool:
    return code.startswith(("600", "601", "603", "605", "000", "001", "002", "003", "004"))


def is_risk_stock_name(name: str) -> bool:
    normalized = "".join(str(name).upper().split())
    return "ST" in normalized or "退市" in normalized or normalized.endswith("退")


def sector_context(code: str, provider: EastmoneyProvider | None = None) -> dict:
    category = next((name for name, stocks in LEADER_GROUPS.items() if code in stocks), None)
    if not category:
        return {"category": "未分类", "average_change": 0.0, "advance_ratio": 0.5, "sample_size": 0}
    cached = _CONTEXT_CACHE.get(category)
    if cached and time.time() - cached[0] < 60:
        return cached[1]
    provider = provider or EastmoneyProvider(timeout=6)
    changes = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(provider.quote, peer) for peer in LEADER_GROUPS[category]]
        for future in as_completed(futures):
            try:
                changes.append(future.result().change_percent)
            except Exception:
                pass
    result = {
        "category": category,
        "average_change": round(sum(changes) / len(changes), 2) if changes else 0.0,
        "advance_ratio": round(sum(value > 0 for value in changes) / len(changes), 2) if changes else 0.5,
        "sample_size": len(changes),
    }
    _CONTEXT_CACHE[category] = (time.time(), result)
    return result


def _evaluate_one(code: str, industry: str, provider: EastmoneyProvider, category: str = "综合") -> dict | None:
    quote = provider.quote(code)
    if is_risk_stock_name(quote.name):
        return None
    result = evaluate(quote, provider.history(code))
    metrics, operation = result["metrics"], result["operation"]
    qualified = result["score"] >= 55 and metrics["price_vs_ma5_percent"] > 0 and operation["risk_level"] != "较高"
    signal = "买入关注" if qualified and result["score"] >= 70 else "谨慎关注" if qualified else "观察"
    return {
        "code": code, "name": result["quote"]["name"], "industry": industry, "category": category,
        "price": result["quote"]["price"], "change_percent": result["quote"]["change_percent"],
        "score": result["score"], "signal": signal, "qualified": qualified,
        "price_vs_ma5_percent": metrics["price_vs_ma5_percent"],
        "volume_ratio": metrics["volume_ratio"], "turnover_rate": metrics["turnover_rate"],
        "intraday_position_percent": metrics["intraday_position_percent"],
        "risk_level": operation["risk_level"], "risk_points": operation["risk_points"],
    }


def screen_leaders(provider: EastmoneyProvider | None = None, per_group: int = 3) -> dict:
    provider = provider or EastmoneyProvider(timeout=8)
    candidates: list[dict] = []
    errors = 0
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {}
        for category, stocks in LEADER_GROUPS.items():
            for code, industry in stocks.items():
                if is_main_board(code):
                    futures[executor.submit(_evaluate_one, code, industry, provider, category)] = code
        for future in as_completed(futures):
            try:
                candidate = future.result()
                if candidate:
                    candidates.append(candidate)
            except Exception:
                errors += 1
    candidates.sort(key=lambda item: (item["score"], item["volume_ratio"]), reverse=True)
    qualified_count = sum(1 for item in candidates if item["qualified"])
    groups = []
    flattened = []
    for category in LEADER_GROUPS:
        ranked = [item for item in candidates if item["category"] == category][:per_group]
        groups.append({"name": category, "candidates": ranked})
        flattened.extend(ranked)
    return {
        "candidates": flattened, "groups": groups, "qualified_count": qualified_count,
        "scanned": len(futures), "failed": errors,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "method": "科技硬件、大科技应用、电力能源、医药健康分别排名；已排除ST、*ST和退市股；评分≥55、站上MA5且风险非较高才标记为关注候选",
        "disclaimer": "候选仅为当日量价技术筛选，不代表基本面龙头认定，不构成买入建议。",
    }
