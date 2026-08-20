from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .evaluator import evaluate
from .market import EastmoneyProvider, MarketDataError, secid_for
from .screener import is_main_board, is_risk_stock_name


HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
TENCENT_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}


def _fund_period(trade_date: str) -> dict:
    try:
        parsed = date.fromisoformat(trade_date[:10])
    except (TypeError, ValueError):
        return {"is_today": False, "period_label": "交易日未知"}
    today = date.today()
    if parsed == today:
        label = "当日资金"
    elif parsed == today - timedelta(days=1):
        label = "昨日资金"
    else:
        label = "最近交易日资金"
    return {"is_today": parsed == today, "period_label": label}


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
    raise MarketDataError(f"资金流服务连接失败：{last_error}")


def _fetch_tencent_page(symbol: str, page: int, timeout: float = 8, trade_date: str | None = None) -> list[list[str]]:
    params = {"appn": "detail", "action": "data", "c": symbol, "p": page}
    if trade_date:
        params["d"] = trade_date.replace("-", "")
    url = (
        "https://stock.gtimg.cn/data/index.php?"
        + urlencode(params)
    )
    request = Request(url, headers=TENCENT_HEADERS)
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            with urlopen(request, timeout=timeout) as response:
                text = response.read().decode("gbk", errors="replace")
            if not text:
                return []
            start = text.find("[")
            payload = json.loads(text[start:])
            raw_rows = payload[1] if len(payload) > 1 else ""
            return [row.split("/") for row in raw_rows.split("|") if row]
        except Exception as exc:
            last_error = exc
            time.sleep(0.15 * (attempt + 1))
    raise MarketDataError(f"腾讯逐笔成交服务连接失败：{last_error}")


def historical_open_proxy(symbol: str, trade_date: date, timeout: float = 10) -> dict:
    """读取最近交易日09:31首根分钟线，作为不可回放的09:25竞价代理。"""
    url = (
        "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20auction=/"
        "CN_MarketDataService.getKLineData?"
        + urlencode({"symbol": symbol, "scale": 1, "ma": "no", "datalen": 1970})
    )
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            with urlopen(request, timeout=timeout) as response:
                text = response.read().decode("utf-8", errors="replace")
            match = re.search(r"=\((\[.*\])\)\s*;?\s*$", text, re.S)
            rows = json.loads(match.group(1)) if match else []
            target = trade_date.isoformat()
            row = next((item for item in rows if str(item.get("day", "")).startswith(target)), None)
            if not row:
                raise MarketDataError(f"{target}分钟行情不可用")
            price = float(row["open"])
            volume_shares = int(float(row.get("volume") or 0))
            return {
                "time": str(row["day"])[11:16],
                "price": price,
                "volume": volume_shares / 100,
                "amount": price * volume_shares,
                "source": "新浪目标日09:31首根一分钟线（竞价代理）",
            }
        except Exception as exc:
            last_error = exc
            time.sleep(0.15 * (attempt + 1))
    raise MarketDataError(f"历史开盘分钟行情连接失败：{last_error}")


def _last_tencent_page(symbol: str, max_pages: int = 128) -> int:
    """通过指数探测+二分查找末页，避免为寻找空页顺序发送大量请求。"""
    low, high = 0, 1
    while high < max_pages and _fetch_tencent_page(symbol, high):
        low, high = high, high * 2
    high = min(high, max_pages)
    while low + 1 < high:
        middle = (low + high) // 2
        if _fetch_tencent_page(symbol, middle):
            low = middle
        else:
            high = middle
    return low


def _aggregate_tencent_trades(rows: list[list[str]]) -> dict[str, float]:
    tiers = {"super_large": 0.0, "large": 0.0, "medium": 0.0, "small": 0.0}
    total = 0.0
    for values in rows:
        if len(values) < 7 or values[6] not in {"B", "S"}:
            continue
        try:
            amount = float(values[5])
        except ValueError:
            continue
        total += amount
        sign = 1 if values[6] == "B" else -1
        tier = "super_large" if amount >= 1_000_000 else "large" if amount >= 200_000 else "medium" if amount >= 50_000 else "small"
        tiers[tier] += sign * amount
    tiers["total"] = total
    return tiers


def _tencent_individual_fund_flow(code: str) -> dict:
    market, normalized = secid_for(code).split(".")
    symbol = ("sh" if market == "1" else "sz") + normalized
    last_page = _last_tencent_page(symbol)
    rows: list[list[str]] = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_fetch_tencent_page, symbol, page) for page in range(last_page + 1)]
        for future in as_completed(futures):
            rows.extend(future.result())
    if not rows:
        raise MarketDataError("东方财富与腾讯资金流数据均暂不可用")
    flow = _aggregate_tencent_trades(rows)
    main = flow["super_large"] + flow["large"]
    main_ratio = main / flow["total"] * 100 if flow["total"] else 0.0
    quote = EastmoneyProvider(timeout=8).quote(normalized)
    return {
        "date": time.strftime("%Y-%m-%d"), "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        **_fund_period(time.strftime("%Y-%m-%d")),
        "source": "腾讯逐笔成交推算（备用）",
        "main_net": main, "main_ratio": main_ratio,
        "super_large_net": flow["super_large"], "large_net": flow["large"],
        "medium_net": flow["medium"], "small_net": flow["small"],
        "price_change_percent": quote.change_percent,
        "inferred_hidden_signal": _hidden_signal(main, quote.change_percent),
        "combined_signal": _combined_fund_signal(main, main_ratio, quote.change_percent),
        "note": "东方财富资金流接口不可用，已切换至腾讯当日逐笔成交。按主动买入B/主动卖出S的成交额净额推算：超大单≥100万元、大单20–100万元、中单5–20万元、小单<5万元；中性盘M不计入净额。该口径与东方财富不同，仅供趋势参考。",
    }


def _hidden_signal(main: float, price_change: float) -> str:
    if main > 0 and price_change <= 0:
        return "疑似吸筹：价格偏弱但主力资金净流入"
    if main < 0 and price_change >= 0:
        return "疑似派发：价格上涨但主力资金净流出"
    return "资金与价格同步偏强" if main > 0 else "资金与价格同步偏弱"


def _combined_fund_signal(main: float, main_ratio: float, price_change: float) -> dict:
    public_score = max(-60.0, min(60.0, main_ratio * 5))
    if main > 0 and price_change <= 0:
        inferred_score = 20.0
    elif main < 0 and price_change >= 0:
        inferred_score = -20.0
    else:
        inferred_score = 10.0 if main > 0 else -10.0
    score = round(max(-100.0, min(100.0, public_score + inferred_score)), 1)
    label = "强流入" if score >= 45 else "流入" if score >= 15 else "均衡" if score > -15 else "流出" if score > -45 else "强流出"
    return {
        "score": score, "label": label, "public_score": round(public_score, 1),
        "inferred_score": inferred_score,
        "description": "公开主力净占比与量价隐性迹象合成；不是主力金额与真实暗盘金额相加。",
    }


def _realtime_individual_fund_flow(code: str) -> dict:
    payload = _fetch("https://push2.eastmoney.com/api/qt/stock/fflow/kline/get", {
        "lmt": 0, "klt": 1, "secid": secid_for(code), "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56", "ut": "b2884a393a59ad64002292a3e90d46a5",
    })
    rows = payload.get("data", {}).get("klines", [])
    if not rows:
        raise MarketDataError("暂无该股票实时资金流数据")
    values = rows[-1].split(",")
    main, small, medium, large, super_large = map(float, values[1:6])
    denominator = sum(abs(value) for value in (small, medium, large, super_large))
    main_ratio = main / denominator * 100 if denominator else 0.0
    quote = EastmoneyProvider(timeout=8).quote(code)
    return {
        "date": values[0], "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        **_fund_period(values[0]),
        "source": "实时分钟累计", "main_net": main,
        "main_ratio": main_ratio, "super_large_net": super_large, "large_net": large,
        "medium_net": medium, "small_net": small, "price_change_percent": quote.change_percent,
        "inferred_hidden_signal": _hidden_signal(main, quote.change_percent),
        "combined_signal": _combined_fund_signal(main, main_ratio, quote.change_percent),
        "note": "历史资金接口不可用，已自动切换为当日实时分钟累计；主力占比为按各档净额绝对值估算。隐性资金迹象不是真实暗盘成交。",
    }


def individual_fund_flow(code: str) -> dict:
    try:
        payload = _fetch("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get", {
            "lmt": 10, "klt": 101, "secid": secid_for(code), "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
            "ut": "b2884a393a59ad64002292a3e90d46a5", "_": int(time.time() * 1000),
        })
        rows = payload.get("data", {}).get("klines", [])
        if not rows:
            raise MarketDataError("历史资金流为空")
        values = rows[-1].split(",")
        main, small, medium, large, super_large = map(float, values[1:6])
        main_ratio, price_change = float(values[6]), float(values[12])
    except MarketDataError:
        try:
            return _realtime_individual_fund_flow(code)
        except MarketDataError:
            return _tencent_individual_fund_flow(code)
    return {
        "date": values[0], "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        **_fund_period(values[0]),
        "source": "历史日线", "main_net": main, "main_ratio": main_ratio,
        "super_large_net": super_large, "large_net": large, "medium_net": medium,
        "small_net": small, "price_change_percent": price_change,
        "inferred_hidden_signal": _hidden_signal(main, price_change),
        "combined_signal": _combined_fund_signal(main, main_ratio, price_change),
        "note": "公开资金流按成交单规模统计；隐性资金迹象仅为量价背离推断，不是真实暗盘成交。",
    }


def historical_fund_flow_before(code: str, before_date: date) -> dict:
    """读取指定日期之前最近一个已完成交易日的主力资金。"""
    payload = _fetch("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get", {
        "lmt": 15, "klt": 101, "secid": secid_for(code), "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "ut": "b2884a393a59ad64002292a3e90d46a5", "_": int(time.time() * 1000),
    })
    completed = []
    for row in payload.get("data", {}).get("klines", []):
        values = row.split(",")
        try:
            if date.fromisoformat(values[0]) < before_date:
                completed.append(values)
        except (ValueError, IndexError):
            continue
    if not completed:
        raise MarketDataError("前序交易日资金流为空")
    values = completed[-1]
    return {
        "date": values[0], "main_net": float(values[1]), "main_ratio": float(values[6]),
        "price_change_percent": float(values[12]), "source": "前序交易日主力资金",
    }


def _sector_top_stocks(board_code: str, limit: int = 5) -> list[dict]:
    payload = _fetch("https://push2.eastmoney.com/api/qt/clist/get", {
        "pn": 1, "pz": max(20, limit * 4), "po": 1, "np": 1, "ut": "b2884a393a59ad64002292a3e90d46a5",
        "fltt": 2, "invt": 2, "fid": "f62", "fs": f"b:{board_code}",
        "fields": "f12,f14,f2,f3,f62,f184,f66,f72,f78,f84",
    })
    stocks = [{
        "code": str(row.get("f12") or ""), "name": row.get("f14") or "-",
        "price": float(row.get("f2") or 0), "change_percent": float(row.get("f3") or 0),
        "main_net": float(row.get("f62") or 0), "main_ratio": float(row.get("f184") or 0),
        "super_large_net": float(row.get("f66") or 0), "large_net": float(row.get("f72") or 0),
    } for row in payload.get("data", {}).get("diff", [])
        if is_main_board(str(row.get("f12") or ""))
        and not is_risk_stock_name(row.get("f14") or "")]
    return stocks[:limit]


def _select_top_sectors(sectors: list[dict], limit: int = 6) -> list[dict]:
    return sorted(sectors, key=lambda item: item["main_net"], reverse=True)[:limit]


def sector_fund_leaders(limit: int = 6) -> dict:
    payload = _fetch("https://push2.eastmoney.com/api/qt/clist/get", {
        "pn": 1, "pz": 100, "po": 1, "np": 1, "ut": "b2884a393a59ad64002292a3e90d46a5",
        "fltt": 2, "invt": 2, "fid0": "f62", "fs": "m:90 t:2", "stat": 1,
        "fields": "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205,f124",
    })
    sectors = []
    for row in payload.get("data", {}).get("diff", []):
        main = float(row.get("f62") or 0)
        if main <= 0:
            continue
        sectors.append({
            "board_code": str(row.get("f12") or ""),
            "name": row.get("f14"), "change_percent": float(row.get("f3") or 0),
            "main_net": main, "main_ratio": float(row.get("f184") or 0),
            "super_large_net": float(row.get("f66") or 0), "large_net": float(row.get("f72") or 0),
            "leader_name": row.get("f204") or "-", "leader_code": str(row.get("f205") or ""),
        })
    selected = _select_top_sectors(sectors, limit)
    provider = EastmoneyProvider(timeout=8)
    for item in selected:
        try:
            item["stocks"] = _sector_top_stocks(item["board_code"])
        except Exception:
            item["stocks"] = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {}
        for item in selected:
            for stock in item["stocks"]:
                code = stock["code"]
                if len(code) == 6 and code.isdigit():
                    futures[executor.submit(lambda c=code: evaluate(provider.quote(c), provider.history(c)))] = stock
        for future in as_completed(futures):
            stock = futures[future]
            try:
                result = future.result()
                stock["score"] = result["score"]
                stock["rating"] = result["rating"]
                stock["recommendation"] = "龙头关注" if result["score"] >= 60 else "仅观察"
            except Exception:
                stock.update({"score": None, "rating": "数据不足", "recommendation": "仅观察"})
    for item in selected:
        item["stocks"].sort(key=lambda stock: ((stock.get("score") or -1), stock["main_net"]), reverse=True)
        leader = next((stock for stock in item["stocks"] if stock["code"] == item["leader_code"]), None)
        if not leader and item["stocks"]:
            leader = item["stocks"][0]
            item["leader_code"], item["leader_name"] = leader["code"], leader["name"]
        item["leader_score"] = leader.get("score") if leader else None
        item["leader_rating"] = leader.get("rating", "未进入资金前五") if leader else "未进入资金前五"
        item["recommendation"] = "板块龙头关注" if leader and (leader.get("score") or 0) >= 60 else "仅观察"
    for item in selected:
        item["message"] = f"{item['name']}主力净流入{item['main_net']/1e8:.2f}亿元，板块涨跌{item['change_percent']:+.2f}%"
    return {
        "sectors": selected, "selected_count": len(selected), "ranking_limit": limit,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "disclaimer": f"按板块主力净流入实时排名展示前{limit}名；仅筛选沪深主板，已排除创业板、科创板、北交所、ST、*ST和退市股。板块与个股仅为公开资金流和技术面联合筛选，不构成购买建议。",
    }
