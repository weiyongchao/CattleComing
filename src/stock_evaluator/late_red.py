"""14:40 昨日收绿、今日翻红与 10 分钟 MA5 拐头筛选。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
import json
import math
import threading
import time
from typing import Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .corporate_events import attach_corporate_event_risks
from .market import EastmoneyProvider, MarketDataError, secid_for
from .quote_sampling import china_time
from .universe import HEADERS, main_board_snapshots


TARGET_TIME = "14:40"
SIGNAL_CLOCK = 1440
FREEZE_CLOCK = 1450
MIN_AMOUNT = 50_000_000
MIN_TURNOVER = 0.5
MIN_LISTED_DAYS = 60
MAX_RECOMMENDATIONS = 3


def _finite_number(value: object) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _number(value: object, default: float = 0.0) -> float:
    number = _finite_number(value)
    return number if number is not None else default


def _listing_date(value: object) -> date | None:
    text = str(value or "").strip().replace("-", "")
    try:
        return datetime.strptime(text, "%Y%m%d").date() if len(text) == 8 else None
    except ValueError:
        return None


def _listed_long_enough(snapshot: dict, target: date) -> bool:
    listed = _listing_date(snapshot.get("f26"))
    return bool(listed and target - listed >= timedelta(days=MIN_LISTED_DAYS))


def _sina_five_minute_klines(code: str, target: date, *, timeout: int = 6) -> list[str]:
    symbol = ("sh" if code.startswith(("5", "6", "9")) else "sz") + code
    request = Request(
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData?" + urlencode({
            "symbol": symbol, "scale": 5, "ma": "no", "datalen": 80,
        }),
        headers={
            "User-Agent": HEADERS["User-Agent"],
            "Referer": "https://finance.sina.com.cn/",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    day_prefix = target.isoformat()
    rows: list[str] = []
    for item in payload if isinstance(payload, list) else []:
        stamp = str(item.get("day") or "")
        if not stamp.startswith(day_prefix):
            continue
        try:
            open_price = float(item["open"])
            close_price = float(item["close"])
            volume = float(item["volume"])
            # 新浪分钟数据没有成交额，使用均价×成交量近似；量比排序不受该近似影响。
            amount = (open_price + close_price) / 2 * volume
            rows.append(",".join([
                stamp[:16], str(open_price), str(close_price), str(float(item["high"])),
                str(float(item["low"])), str(volume), str(amount), "0", "0", "0", "0",
            ]))
        except (KeyError, TypeError, ValueError):
            continue
    if not rows:
        raise MarketDataError(f"{code}新浪五分钟K线无数据")
    return rows


def _eastmoney_five_minute_klines(code: str, target: date, *, timeout: int = 6) -> list[str]:
    day = target.strftime("%Y%m%d")
    params = {
        "secid": secid_for(code), "klt": 5, "fqt": 1,
        "beg": day, "end": day, "lmt": 100,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    }
    request = Request(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + urlencode(params),
        headers=HEADERS,
    )
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            with urlopen(request, timeout=timeout) as response:
                rows = (json.load(response).get("data") or {}).get("klines") or []
            if rows:
                return rows
        except Exception as exc:
            last_error = exc
        time.sleep(0.2 * (attempt + 1))
    raise MarketDataError(f"{code}五分钟K线读取失败：{last_error or '无数据'}")


def _five_minute_klines(code: str, target: date) -> list[str]:
    errors: list[str] = []
    for source in (_sina_five_minute_klines, _eastmoney_five_minute_klines):
        try:
            return source(code, target)
        except Exception as exc:
            errors.append(str(exc))
    raise MarketDataError(f"{code}五分钟K线双源失败：{'；'.join(errors)}")


def _sina_previous_day_change(code: str, target: date, *, timeout: int = 6) -> float:
    symbol = ("sh" if code.startswith(("5", "6", "9")) else "sz") + code
    request = Request(
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData?" + urlencode({
            "symbol": symbol, "scale": 240, "ma": "no", "datalen": 8,
        }),
        headers={
            "User-Agent": HEADERS["User-Agent"],
            "Referer": "https://finance.sina.com.cn/",
        },
    )
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("gbk", errors="replace"))
            bars = sorted(
                (
                    date.fromisoformat(str(item["day"])[:10]),
                    float(item["close"]),
                )
                for item in payload if isinstance(item, dict)
                and date.fromisoformat(str(item["day"])[:10]) < target
            )
            if len(bars) >= 2 and bars[-2][1] > 0:
                return round((bars[-1][1] / bars[-2][1] - 1) * 100, 4)
            raise MarketDataError(f"{code}昨日完整日K不足")
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.2)
    raise MarketDataError(f"{code}新浪昨日涨跌读取失败：{last_error}")


def _previous_day_change(code: str, target: date) -> float:
    try:
        return _sina_previous_day_change(code, target)
    except MarketDataError as sina_error:
        try:
            bars = [
                bar for bar in EastmoneyProvider(timeout=6).history(code, limit=8)
                if bar.trade_date < target
            ]
            if len(bars) < 2 or bars[-2].close <= 0:
                raise MarketDataError(f"{code}昨日完整日K不足")
            return round((bars[-1].close / bars[-2].close - 1) * 100, 4)
        except Exception as fallback_error:
            raise MarketDataError(
                f"{code}昨日涨跌双源失败：{sina_error}；{fallback_error}"
            ) from fallback_error


def aggregate_ten_minute_bars(rows: list[str], *, target_time: str = TARGET_TIME) -> list[dict]:
    """按 09:31/13:01 起点，将两根完整 5 分钟K线合成为一根 10 分钟K线。"""
    grouped: dict[tuple[str, int], list[dict]] = {}
    for raw in rows:
        values = raw.split(",")
        try:
            stamp = datetime.strptime(values[0], "%Y-%m-%d %H:%M")
            minute = stamp.hour * 60 + stamp.minute
            if 575 <= minute <= 690:
                session, bucket = "morning", (minute - 571) // 10
                expected_end = 580 + bucket * 10
            elif 785 <= minute <= 900:
                session, bucket = "afternoon", (minute - 781) // 10
                expected_end = 790 + bucket * 10
            else:
                continue
            grouped.setdefault((session, bucket), []).append({
                "time": stamp.strftime("%H:%M"), "minute": minute,
                "open": float(values[1]), "close": float(values[2]),
                "high": float(values[3]), "low": float(values[4]),
                "amount": float(values[6]), "expected_end": expected_end,
            })
        except (IndexError, TypeError, ValueError):
            continue
    bars: list[dict] = []
    for items in grouped.values():
        items.sort(key=lambda item: item["minute"])
        expected_end = int(items[0]["expected_end"])
        if len(items) != 2 or items[-1]["minute"] != expected_end:
            continue
        end_text = f"{expected_end // 60:02d}:{expected_end % 60:02d}"
        if end_text > target_time:
            continue
        bars.append({
            "end": end_text, "open": items[0]["open"], "close": items[-1]["close"],
            "high": max(item["high"] for item in items),
            "low": min(item["low"] for item in items),
            "amount": sum(item["amount"] for item in items),
        })
    return sorted(bars, key=lambda item: item["end"] if item["end"] >= "13:00" else "0" + item["end"])


def evaluate_late_red_snapshot(
    snapshot: dict, rows: list[str], previous_day_change_percent: float,
) -> dict | None:
    bars = aggregate_ten_minute_bars(rows)
    target_index = next((index for index, bar in enumerate(bars) if bar["end"] == TARGET_TIME), None)
    previous_close = _number(snapshot.get("f18"))
    if target_index is None or target_index < 6 or previous_close <= 0:
        return None
    bars = bars[:target_index + 1]
    closes = [float(bar["close"]) for bar in bars]
    target_close = closes[-1]
    today_red = target_close > previous_close
    previous_day_green = previous_day_change_percent < 0
    red_reversal = previous_day_green and today_red
    ma5_now = sum(closes[-5:]) / 5
    ma5_previous = sum(closes[-6:-1]) / 5
    ma5_previous2 = sum(closes[-7:-2]) / 5
    ma5_turned_up = ma5_now > ma5_previous and ma5_previous <= ma5_previous2
    previous_amount = sum(float(bar["amount"]) for bar in bars[-6:-1]) / 5
    return {
        "code": str(snapshot.get("f12") or ""), "name": str(snapshot.get("f14") or ""),
        "previous_close": previous_close, "target_close": target_close,
        "target_change_percent": round((target_close / previous_close - 1) * 100, 2),
        "target_bar_low": float(bars[-1]["low"]), "target_bar_high": float(bars[-1]["high"]),
        "previous_day_change_percent": round(previous_day_change_percent, 2),
        "previous_day_green": previous_day_green, "today_red": today_red,
        "red_reversal": red_reversal,
        # 保留旧字段，避免旧页面或缓存读取失败；语义已改为“昨日绿、今日红”。
        "first_red": red_reversal, "crossed_red": today_red,
        "ma5_turned_up": ma5_turned_up, "ma5": round(ma5_now, 3),
        "ma5_previous": round(ma5_previous, 3),
        "ma5_slope_bp": round((ma5_now / ma5_previous - 1) * 10_000, 2) if ma5_previous else None,
        "ten_minute_amount": round(float(bars[-1]["amount"]), 2),
        "ten_minute_volume_ratio": round(float(bars[-1]["amount"]) / previous_amount, 2)
        if previous_amount else None,
        "current_price": _number(snapshot.get("f2")),
        "current_change_percent": _number(snapshot.get("f3"), -99),
        "amount": _number(snapshot.get("f6")), "turnover_rate": _number(snapshot.get("f8")),
    }


def waiting_late_red_screen(now: datetime | None = None) -> dict:
    now = china_time(now or datetime.now().astimezone())
    weekend = now.weekday() >= 5
    clock = now.hour * 100 + now.minute
    status = "closed" if weekend else "waiting"
    note = "非交易日不生成尾盘形态。" if weekend else (
        "14:40后使用已完成的14:31–14:40十分钟K线自动筛选。"
        if clock < SIGNAL_CLOCK else "正在等待后台扫描。"
    )
    return {
        "selected_date": now.date().isoformat(), "status": status,
        "stage": "等待14:40" if status == "waiting" else "休市",
        "generated_at": now.isoformat(timespec="seconds"), "candidates": [],
        "qualified_count": 0, "eligible_count": 0, "formal_recommendation": False,
        "recommendation_limit": MAX_RECOMMENDATIONS, "primary_code": None,
        "decision_window": "14:40–14:50", "frozen": False,
        "rule": late_red_rule(), "note": note,
    }


def late_red_rule() -> dict:
    return {
        "time": "14:40生成，14:50冻结",
        "universe": "沪深主板、非ST、上市满60日；当前仍保持红盘的股票",
        "first_red": "上一个交易日收绿；今日14:40十分钟K线收盘价高于昨收",
        "execution": "14:40–14:50动态确认Top 3，14:50冻结，预留尾盘下单时间",
        "ma5": "10分钟MA5本轮向上，上一轮MA5仍走平或向下",
        "liquidity": "当日成交额≥5000万元、换手率≥0.5%",
        "ranking": "入选逻辑不变；合格后按主力资金（最高权重）、换手率（第二）、流通市值（低权重参考）排序",
        "risk": "并购、收购、重大重组及其他重大事项风险不进入合格名单",
    }


def _prefilter(snapshot: dict, target: date) -> bool:
    previous_close = _number(snapshot.get("f18"))
    return bool(
        previous_close > 0 and _listed_long_enough(snapshot, target)
        and _number(snapshot.get("f3"), -99) > 0
        and _number(snapshot.get("f16")) < previous_close < _number(snapshot.get("f15"))
        and _number(snapshot.get("f6")) >= MIN_AMOUNT
        and _number(snapshot.get("f8")) >= MIN_TURNOVER
    )


def _event_excluded(item: dict) -> bool:
    event = item.get("corporate_event_risk") or {}
    return bool(
        event.get("level") in {"watch", "high", "unknown"}
        or event.get("is_restructuring") or event.get("is_merger_acquisition")
        or event.get("is_acquisition")
    )


def _late_red_potential(item: dict) -> dict:
    """对已满足严格形态的股票做相对排序，不把分数解释为上涨概率。"""
    score = 5.0
    reasons: list[str] = []
    volume_ratio = _number(item.get("ten_minute_volume_ratio"))
    slope = _number(item.get("ma5_slope_bp"))
    current_change = _number(item.get("current_change_percent"), -99)
    target_change = _number(item.get("target_change_percent"), -99)
    retention = current_change - target_change
    previous_change = _number(item.get("previous_day_change_percent"))
    amount = _number(item.get("amount"))
    turnover = _number(item.get("turnover_rate"))
    main_flow_available = bool(
        item.get("main_flow_available")
        and _finite_number(item.get("main_net")) is not None
        and _finite_number(item.get("main_ratio")) is not None
    )
    main_net = _number(item.get("main_net"))
    main_ratio = _number(item.get("main_ratio"))
    float_market_cap = _number(item.get("float_market_cap"))

    if volume_ratio >= 2:
        score += 18
        reasons.append(f"{TARGET_TIME}十分钟显著放量")
    elif volume_ratio >= 1.2:
        score += 13
        reasons.append(f"{TARGET_TIME}十分钟温和放量")
    elif volume_ratio >= 0.8:
        score += 8
        reasons.append(f"{TARGET_TIME}量能保持")
    else:
        score += 2

    if 2 <= slope <= 20:
        score += 12
        reasons.append("MA5温和拐头")
    elif 0 < slope <= 35:
        score += 8
        reasons.append("MA5确认向上")
    elif slope > 35:
        score += 3

    if retention >= -0.3:
        score += 12
        reasons.append("翻红后承接稳定")
    elif retention >= -0.8:
        score += 7
    else:
        score -= 5
        reasons.append("翻红后回落偏多")

    if 0.2 <= current_change <= 3.5:
        score += 10
        reasons.append("尾盘涨幅适中")
    elif 0 < current_change <= 5:
        score += 6
    elif current_change <= 7:
        score += 1
    else:
        score -= 8
        reasons.append("尾盘涨幅偏热")

    if -4 <= previous_change <= -0.5:
        score += 7
        reasons.append("昨日回调幅度适中")
    elif -7 <= previous_change < -4:
        score += 3

    if amount >= 300_000_000:
        score += 5
        reasons.append("成交活跃")
    elif amount >= 100_000_000:
        score += 3

    if 1.5 <= turnover <= 8:
        turnover_score = 10
        reasons.append("换手处于健康区间")
    elif 0.8 <= turnover < 1.5:
        turnover_score = 6
    elif MIN_TURNOVER <= turnover < 0.8 or 8 < turnover <= 12:
        turnover_score = 4
    elif turnover > 18:
        turnover_score = -8
        reasons.append("换手过热")
    else:
        turnover_score = 0
    score += turnover_score

    main_flow_score = 0
    if main_flow_available:
        if main_ratio >= 5:
            main_flow_score = 16
            reasons.append("主力资金强净流入")
        elif main_ratio >= 2:
            main_flow_score = 11
            reasons.append("主力资金净流入")
        elif main_ratio >= 0:
            main_flow_score = 4
        elif main_ratio > -2:
            main_flow_score = -4
        else:
            main_flow_score = -12
            reasons.append("主力资金明显流出")
        if main_net >= 100_000_000:
            main_flow_score += 3
        elif main_net <= -100_000_000:
            main_flow_score -= 3
    score += main_flow_score

    market_cap_score = 0
    if 3_000_000_000 <= float_market_cap < 10_000_000_000:
        market_cap_score = 3
        reasons.append("流通市值兼顾弹性与成交")
    elif 10_000_000_000 <= float_market_cap < 20_000_000_000:
        market_cap_score = 2
        reasons.append("流通市值适中")
    elif 20_000_000_000 <= float_market_cap < 50_000_000_000:
        market_cap_score = 1
    elif 0 < float_market_cap < 3_000_000_000:
        market_cap_score = -1
        reasons.append("流通市值偏小")
    elif float_market_cap >= 100_000_000_000:
        market_cap_score = -2
        reasons.append("流通市值偏大")
    score += market_cap_score

    # 同一评分档内保留少量连续差异，避免大量候选同分；不改变各主因子的方向。
    score += min(max(volume_ratio, 0), 3) * 0.7
    score += max(0, 1 - abs(slope - 10) / 20)
    score += max(-1, min(1, retention)) * 0.8
    score += max(0, 1 - abs(current_change - 1.5) / 3)
    score += min(max(amount, 0) / 1_000_000_000, 1)

    ranking_reasons = []
    if main_flow_available:
        ranking_reasons.append(f"主力净占比{main_ratio:+.2f}%（排名{main_flow_score:+}分）")
    else:
        ranking_reasons.append("主力资金数据缺失，资金项不计分")
    ranking_reasons.append(f"换手率{turnover:.2f}%（排名{turnover_score:+}分）")
    if float_market_cap > 0:
        ranking_reasons.append(f"流通市值仅参考（排名{market_cap_score:+}分）")
    else:
        ranking_reasons.append("流通市值缺失，市值项不计分")
    return {
        "potential_score": round(max(0, min(100, score)), 1),
        "potential_reasons": ranking_reasons + reasons[:3],
        "signal_retention_percent": round(retention, 2),
        "ranking_factor_scores": {
            "main_flow": main_flow_score,
            "turnover": turnover_score,
            "market_cap": market_cap_score,
        },
    }


def freeze_late_red_screen(payload: dict, now: datetime) -> dict:
    """只冻结已保存的排序；窗口后才生成的结果不能冒充及时推荐。"""
    now = china_time(now)
    late_reference = payload.get("snapshot_kind") == "late_reference"
    payload.update(
        frozen=True, decision_window="14:40–14:50",
        stage="错过确认窗口 · 事后参考" if late_reference else "14:50名单已冻结",
    )
    payload.setdefault("frozen_at", now.isoformat(timespec="seconds"))
    if late_reference:
        payload["timing_warning"] = "此结果未在14:40–14:50窗口内生成，仅供事后参考，不是当时可执行的推荐。"
    return payload


def refresh_late_red_screen(
    payload: dict, snapshots: list[dict], now: datetime | None = None,
) -> dict:
    now = china_time(now or datetime.now().astimezone())
    if payload.get("frozen"):
        return payload
    after_cutoff = now.hour * 100 + now.minute >= FREEZE_CLOCK
    if after_cutoff and payload.get("refreshed_at"):
        return freeze_late_red_screen(payload, now)
    latest = {str(row.get("f12") or ""): row for row in snapshots}
    matches = payload.get("matches") or []
    for item in matches:
        row = latest.get(str(item.get("code") or "")) or {}
        item.pop("rank", None)
        item["current_price"] = _number(row.get("f2"), _number(item.get("current_price")))
        item["current_change_percent"] = _number(
            row.get("f3"), item.get("current_change_percent", -99)
        )
        item["amount"] = _number(row.get("f6"), _number(item.get("amount")))
        item["turnover_rate"] = _number(row.get("f8"), _number(item.get("turnover_rate")))
        item["market_cap"] = _finite_number(row.get("f20"))
        item["float_market_cap"] = _finite_number(row.get("f21"))
        item["main_ratio"] = _finite_number(row.get("f184"))
        item["main_net"] = _finite_number(row.get("f62"))
        item["main_flow_available"] = (
            item["main_ratio"] is not None and item["main_net"] is not None
        )
        item["main_flow_source"] = "东方财富行情资金流口径" if item["main_flow_available"] else None
        item["signal_active"] = item["current_change_percent"] > 0
        item["risk_excluded"] = _event_excluded(item)
        item["status"] = (
            "重大事项风险排除" if item["risk_excluded"] else
            "翻绿失效" if not item["signal_active"] else "形态保持"
        )
        item["qualified"] = bool(item["signal_active"] and not item["risk_excluded"])
        if item["qualified"]:
            item.update(_late_red_potential(item))
    eligible = [item for item in matches if item.get("qualified")]
    eligible.sort(key=lambda item: (
        _number(item.get("potential_score")),
        (item.get("ranking_factor_scores") or {}).get("main_flow", 0),
        (item.get("ranking_factor_scores") or {}).get("turnover", 0),
        (item.get("ranking_factor_scores") or {}).get("market_cap", 0),
        _number(item.get("ten_minute_volume_ratio")),
        _number(item.get("signal_retention_percent")),
        _number(item.get("amount")), str(item.get("code") or ""),
    ), reverse=True)
    candidates = eligible[:MAX_RECOMMENDATIONS]
    for index, item in enumerate(candidates, start=1):
        item["rank"] = index
    payload["candidates"] = candidates
    payload["qualified_count"] = len(candidates)
    payload["eligible_count"] = len(eligible)
    payload["recommendation_limit"] = MAX_RECOMMENDATIONS
    payload["primary_code"] = candidates[0]["code"] if candidates else None
    payload["decision_window"] = "14:40–14:50"
    payload["frozen"] = False
    payload["snapshot_kind"] = "late_reference" if after_cutoff else "live_window"
    payload["stage"] = "14:40筛选完成，14:50前动态确认"
    payload["ranking_factor_limits"] = {"main_flow": 19, "turnover": 10, "market_cap": 3}
    payload["invalidated_count"] = sum(not item.get("signal_active") for item in matches)
    payload["risk_excluded_count"] = sum(item.get("risk_excluded") for item in matches)
    payload["refreshed_at"] = now.isoformat(timespec="seconds")
    payload["main_flow_missing_count"] = sum(not item.get("main_flow_available") for item in eligible)
    return freeze_late_red_screen(payload, now) if after_cutoff else payload


def build_late_red_screen(
    now: datetime | None = None, *, snapshots: list[dict] | None = None,
    fetcher: Callable[[str, date], list[str]] | None = None,
    previous_change_fetcher: Callable[[str, date], float] | None = None,
    event_attacher: Callable[[list[dict]], object] = attach_corporate_event_risks,
    progress_callback: Callable[[dict], None] | None = None,
) -> dict:
    now = china_time(now or datetime.now().astimezone())
    if now.weekday() >= 5 or now.hour * 100 + now.minute < SIGNAL_CLOCK:
        return waiting_late_red_screen(now)
    all_rows = snapshots if snapshots is not None else main_board_snapshots(cache_seconds=10)
    pool = [row for row in all_rows if _prefilter(row, now.date())]
    fetch = fetcher or _five_minute_klines
    fetch_previous_change = previous_change_fetcher or _previous_day_change
    matches: list[dict] = []
    errors: list[dict] = []
    completed = 0

    def load_candidate(row: dict) -> tuple[list[str], float]:
        code = str(row.get("f12") or "")
        previous_change = row.get("previous_day_change_percent")
        if previous_change is None:
            previous_change = fetch_previous_change(code, now.date())
        return fetch(code, now.date()), float(previous_change)

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(pool)))) as executor:
        futures = {
            executor.submit(load_candidate, row): row for row in pool
        }
        for future in as_completed(futures):
            row = futures[future]
            try:
                minute_rows, previous_change = future.result()
                result = evaluate_late_red_snapshot(row, minute_rows, previous_change)
                if result and result["red_reversal"] and result["ma5_turned_up"]:
                    matches.append(result)
            except Exception as exc:
                errors.append({"code": row.get("f12"), "name": row.get("f14"), "error": str(exc)})
            completed += 1
            if progress_callback and (completed == len(pool) or completed % 10 == 0):
                progress_callback({
                    "completed": completed, "total": len(pool),
                    "failed": len(errors), "matched": len(matches),
                })
    if matches:
        event_attacher(matches)
    coverage = (len(pool) - len(errors)) / len(pool) if pool else 1.0
    payload = {
        "selected_date": now.date().isoformat(), "status": "ready", "stage": "14:40严格筛选已完成",
        "generated_at": now.isoformat(timespec="seconds"), "target_time": TARGET_TIME,
        "scanned": len(all_rows), "prefiltered": len(pool), "evaluated": len(pool) - len(errors),
        "failed": len(errors), "coverage_percent": round(coverage * 100, 1),
        "data_degraded": coverage < 0.9, "matches": matches, "candidates": [],
        "matched_count": len(matches), "qualified_count": 0, "formal_recommendation": False,
        "errors": errors[:20], "rule": late_red_rule(),
        "note": "14:40生成、14:40–14:50动态确认并在14:50冻结；仅展示严格合格股票中的潜力前三，首位为唯一首选观察。潜力分是相对排序，不是次日上涨概率。计划次日开盘卖出仍可能遇到低开或跌停无法按预期成交。若扫描覆盖率不足90%，结果只能观察。",
    }
    return refresh_late_red_screen(payload, all_rows, now=now)


class LateRedRunner:
    """14:40–14:50主动生成并确认名单，不依赖浏览器打开页面。"""

    def __init__(self, callback: Callable[[], dict], *, interval: float = 5):
        self.callback, self.interval = callback, interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = {"running": False, "last_error": None, "last_triggered_at": None}

    def status(self) -> dict:
        return dict(self._state)

    def step(self, now: datetime | None = None) -> bool:
        now = china_time(now or datetime.now().astimezone())
        clock = now.hour * 100 + now.minute
        if now.weekday() >= 5 or not SIGNAL_CLOCK <= clock <= FREEZE_CLOCK:
            return False
        try:
            self.callback()
            self._state.update(last_triggered_at=now.isoformat(timespec="seconds"), last_error=None)
        except Exception as exc:
            self._state["last_error"] = str(exc)
        return True

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def run() -> None:
            self._state["running"] = True
            try:
                while not self._stop.is_set():
                    self.step()
                    self._stop.wait(self.interval)
            finally:
                self._state["running"] = False

        self._thread = threading.Thread(target=run, name="late-red-session", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
