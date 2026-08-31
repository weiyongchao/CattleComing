from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .quote_sampling import quote_time_text


class MarketDataError(RuntimeError):
    """行情服务不可用或返回了无法识别的数据。"""


@dataclass(frozen=True)
class Quote:
    code: str
    name: str
    price: float
    previous_close: float
    change_percent: float
    volume: int
    amount: float
    turnover_rate: float = 0.0
    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    bid_volume5: int = 0
    ask_volume5: int = 0
    order_imbalance: float = 0.0
    spread: float = 0.0
    bid_wall_price: float = 0.0
    ask_wall_price: float = 0.0
    quote_time: str | None = None
    quote_source: str | None = None
    bid1_price: float = 0.0
    bid1_volume: int = 0
    book_available: bool = False


@dataclass(frozen=True)
class DailyBar:
    trade_date: date
    open: float
    close: float
    high: float
    low: float
    volume: int
    amount: float


def secid_for(code: str) -> str:
    normalized = code.strip().lower().replace("sh", "").replace("sz", "").replace("bj", "")
    if len(normalized) != 6 or not normalized.isdigit():
        raise ValueError("股票代码应为 6 位数字，例如 600519 或 000001")
    market = "1" if normalized.startswith(("5", "6", "9")) else "0"
    return f"{market}.{normalized}"


class EastmoneyProvider:
    quote_url = "https://push2.eastmoney.com/api/qt/stock/get"
    fallback_quote_url = "https://qt.gtimg.cn/q="
    history_url = "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get"
    sina_history_url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self._history_eastmoney_disabled_until = 0.0
        self._history_tencent_disabled_until = 0.0

    def _get(self, url: str) -> dict[str, Any]:
        request = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://quote.eastmoney.com/",
        })
        for attempt in range(3):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = json.load(response)
                break
            except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                if attempt == 2:
                    raise MarketDataError(f"行情服务连接失败：{exc}") from exc
                time.sleep(0.15 * (attempt + 1))
        if payload.get("data") is None:
            raise MarketDataError("未找到该股票，请检查代码或稍后重试")
        return payload["data"]

    def quote(self, code: str) -> Quote:
        secid = secid_for(code)
        fields = "f11,f12,f13,f14,f15,f16,f17,f18,f19,f20,f31,f32,f33,f34,f35,f36,f37,f38,f39,f40,f57,f58,f43,f44,f45,f46,f47,f48,f60,f86,f168,f170"
        try:
            data = self._get(
                f"{self.quote_url}?ut=fa5fd1943c7b386f172d6893dbfba10b"
                f"&fltt=2&invt=2&secid={secid}&fields={fields}"
            )
            bids = [(float(data.get(p) or 0), int(data.get(v) or 0)) for p, v in (("f19","f20"),("f17","f18"),("f15","f16"),("f13","f14"),("f11","f12"))]
            asks = [(float(data.get(p) or 0), int(data.get(v) or 0)) for p, v in (("f39","f40"),("f37","f38"),("f35","f36"),("f33","f34"),("f31","f32"))]
            book = self._book_metrics(bids, asks)
            return Quote(
                code=str(data["f57"]), name=str(data["f58"]),
                price=float(data["f43"]), previous_close=float(data["f60"]),
                change_percent=float(data["f170"]),
                volume=int(data["f47"]), amount=float(data["f48"]),
                turnover_rate=float(data.get("f168") or 0),
                open_price=float(data.get("f46") or 0),
                high_price=float(data.get("f44") or 0),
                low_price=float(data.get("f45") or 0),
                quote_time=quote_time_text(data.get("f86")), quote_source="eastmoney",
                book_available=all(data.get(f"f{i}") not in (None, "", "-") for i in (*range(11, 21), *range(31, 41))),
                **book,
            )
        except (MarketDataError, KeyError, TypeError, ValueError):
            return self._quote_tencent(secid)

    @staticmethod
    def _book_metrics(bids: list[tuple[float, int]], asks: list[tuple[float, int]]) -> dict:
        bid_total, ask_total = sum(v for _, v in bids), sum(v for _, v in asks)
        total = bid_total + ask_total
        best_bid, best_ask = (bids[0][0] if bids else 0), (asks[0][0] if asks else 0)
        return {
            "bid_volume5": bid_total, "ask_volume5": ask_total,
            "order_imbalance": round((bid_total - ask_total) / total, 4) if total else 0.0,
            "spread": round(best_ask - best_bid, 4) if best_ask and best_bid else 0.0,
            "bid_wall_price": max(bids, key=lambda x: x[1])[0] if bids else 0.0,
            "ask_wall_price": max(asks, key=lambda x: x[1])[0] if asks else 0.0,
            "bid1_price": best_bid, "bid1_volume": bids[0][1] if bids else 0,
        }

    @staticmethod
    def _parse_tencent_quote(text: str) -> Quote:
        try:
            body = text.split('="', 1)[1].rsplit('"', 1)[0]
            values = body.split("~")
            bids = [(float(values[i] or 0), int(float(values[i + 1] or 0))) for i in range(9, 19, 2)]
            asks = [(float(values[i] or 0), int(float(values[i + 1] or 0))) for i in range(19, 29, 2)]
            book = EastmoneyProvider._book_metrics(bids, asks)
            return Quote(
                code=values[2], name=values[1], price=float(values[3]),
                previous_close=float(values[4]), change_percent=float(values[32]),
                volume=int(float(values[6])), amount=float(values[37]) * 10_000,
                turnover_rate=float(values[38] or 0),
                open_price=float(values[5] or 0), high_price=float(values[33] or 0),
                low_price=float(values[34] or 0),
                quote_time=quote_time_text(values[30]), quote_source="tencent",
                book_available=all(value not in ("", "-") for value in values[9:29]),
                **book,
            )
        except (IndexError, TypeError, ValueError) as exc:
            raise MarketDataError("腾讯实时行情返回格式无法识别") from exc

    def _quote_tencent(self, secid: str) -> Quote:
        market, code = secid.split(".")
        symbol = ("sh" if market == "1" else "sz") + code
        request = Request(
            self.fallback_quote_url + symbol,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                text = response.read().decode("gbk", errors="replace")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise MarketDataError(f"主备实时行情服务均连接失败：{exc}") from exc
        return self._parse_tencent_quote(text)

    def history(self, code: str, limit: int = 30) -> list[DailyBar]:
        secid = secid_for(code)
        errors: list[str] = []
        now = time.monotonic()
        if now >= self._history_eastmoney_disabled_until:
            try:
                return self._history_eastmoney(secid, limit)
            except MarketDataError as exc:
                errors.append(f"东方财富：{exc}")
                self._history_eastmoney_disabled_until = time.monotonic() + 60
        else:
            errors.append("东方财富：短时熔断")
        if now >= self._history_tencent_disabled_until:
            try:
                return self._history_tencent(secid, limit)
            except MarketDataError as exc:
                errors.append(f"腾讯：{exc}")
                self._history_tencent_disabled_until = time.monotonic() + 60
        else:
            errors.append("腾讯：短时熔断")
        try:
            return self._history_sina(secid, limit)
        except MarketDataError as exc:
            errors.append(f"新浪：{exc}")
        raise MarketDataError("历史行情三级数据源均失败：" + "；".join(errors))

    def _history_eastmoney(self, secid: str, limit: int) -> list[DailyBar]:
        """优先读取带成交额的日K；失败时由 ``history`` 自动回退腾讯。"""
        url = (
            "https://push2his.eastmoney.com/api/qt/stock/kline/get"
            f"?secid={secid}&klt=101&fqt=1&end=20500101&lmt={limit}"
            "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57"
        )
        data = self._get(url)
        rows = data.get("klines") or []
        bars: list[DailyBar] = []
        try:
            for row in rows:
                values = row.split(",")
                bars.append(DailyBar(
                    trade_date=date.fromisoformat(values[0]), open=float(values[1]),
                    close=float(values[2]), high=float(values[3]), low=float(values[4]),
                    volume=int(float(values[5])), amount=float(values[6]),
                ))
        except (IndexError, TypeError, ValueError) as exc:
            raise MarketDataError("东方财富历史行情返回格式无法识别") from exc
        if len(bars) < 5:
            raise MarketDataError("东方财富历史交易数据不足 5 日")
        return bars

    def _history_tencent(self, secid: str, limit: int) -> list[DailyBar]:
        market, normalized = secid.split(".")
        symbol = ("sh" if market == "1" else "sz") + normalized
        request = Request(
            f"{self.history_url}?param={symbol},day,,,{limit},qfq",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
        )
        for attempt in range(3):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = json.load(response)
                break
            except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                if attempt == 2:
                    raise MarketDataError(f"历史行情服务连接失败：{exc}") from exc
                time.sleep(0.15 * (attempt + 1))
        stock_data = payload.get("data", {}).get(symbol, {})
        bars = self._parse_tencent_history(stock_data, limit)
        if len(bars) < 5:
            raise MarketDataError("腾讯历史交易数据不足 5 日")
        return bars

    @staticmethod
    def _parse_tencent_history(stock_data: dict[str, Any], limit: int) -> list[DailyBar]:
        rows = stock_data.get("qfqday") or stock_data.get("day") or []
        bars: list[DailyBar] = []
        try:
            for values in rows[-limit:]:
                # 新版腾讯接口成交量单位为手，成交额单位为万元。
                amount = float(values[8]) * 10_000 if len(values) > 8 and values[8] not in (None, "", "-") else 0.0
                bars.append(DailyBar(
                    trade_date=date.fromisoformat(values[0]), open=float(values[1]),
                    close=float(values[2]), high=float(values[3]), low=float(values[4]),
                    volume=int(float(values[5])), amount=amount,
                ))
        except (IndexError, TypeError, ValueError) as exc:
            raise MarketDataError("腾讯历史行情返回格式无法识别") from exc
        return bars

    def _history_sina(self, secid: str, limit: int) -> list[DailyBar]:
        market, normalized = secid.split(".")
        symbol = ("sh" if market == "1" else "sz") + normalized
        request = Request(
            f"{self.sina_history_url}?symbol={symbol}&scale=240&ma=no&datalen={limit}",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
        )
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    rows = json.loads(response.read().decode("gbk", errors="replace"))
                bars = self._parse_sina_history(rows, limit)
                if len(bars) < 5:
                    raise MarketDataError("新浪历史交易数据不足 5 日")
                return bars
            except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError, MarketDataError) as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.2)
        raise MarketDataError(f"新浪历史行情连接失败：{last_error}") from last_error

    @staticmethod
    def _parse_sina_history(rows: object, limit: int) -> list[DailyBar]:
        if not isinstance(rows, list):
            raise MarketDataError("新浪历史行情返回格式无法识别")
        bars: list[DailyBar] = []
        try:
            for values in rows[-limit:]:
                # 新浪日K成交量为股，统一换算为手；该接口没有可靠成交额，保持0防止策略误加分。
                bars.append(DailyBar(
                    trade_date=date.fromisoformat(str(values["day"])),
                    open=float(values["open"]), close=float(values["close"]),
                    high=float(values["high"]), low=float(values["low"]),
                    volume=int(float(values["volume"]) / 100), amount=0.0,
                ))
        except (KeyError, TypeError, ValueError) as exc:
            raise MarketDataError("新浪历史行情返回格式无法识别") from exc
        return bars
