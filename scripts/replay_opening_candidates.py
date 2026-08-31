"""核验当日存档候选的开盘事实，不写推荐账本，不用收盘盘口补造早盘信号。"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.stock_evaluator.funds import _fetch_tencent_page
from src.stock_evaluator.market import EastmoneyProvider

CHINA = ZoneInfo("Asia/Shanghai")
CUTOFF = "09:35:00"
FIELDS = (
    "code", "name", "previous_close", "auction_time", "auction_price",
    "auction_amount", "auction_gap_percent", "auction_turnover_percent",
    "auction_volume_percent", "continuation_score", "priority_tier",
    "early_final_seal_chain_matched", "high_turnover_chain_matched",
    "previous_day_limit_up", "previous_final_seal_time", "consecutive_limit_up_days",
    "risk_veto", "regulatory_risk", "corporate_event_checked", "corporate_event_risk",
    "listed_sessions", "float_market_cap", "strategy_mode", "risks",
)


def percent(value: float, reference: float) -> float:
    return round((value / reference - 1) * 100, 2)


def opening_facts(trades: list[dict], previous_close: float, auction_price: float) -> dict:
    """只计算截至09:35的成交事实；触板不等于封板或可成交买点。"""
    rows = sorted((row for row in trades if "09:30:00" <= row["time"] <= CUTOFF),
                  key=lambda row: (row["time"], row["id"]))
    if not rows:
        return {}
    limit = float((Decimal(str(previous_close)) * Decimal("1.1")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP))
    touches = [row for row in rows if abs(row["price"] - limit) < 0.005]
    first = touches[0] if touches else None
    below_after_touch = [row for row in rows if first and row["id"] > first["id"]
                         and row["price"] < limit - 0.005]
    last = rows[-1]
    return {
        "first_trade_time": rows[0]["time"], "first_trade_price": rows[0]["price"],
        "last_trade_time": last["time"], "last_trade_price": last["price"],
        "last_change_percent": percent(last["price"], previous_close),
        "last_vs_auction_percent": percent(last["price"], auction_price),
        "min_print_price": min(row["price"] for row in rows),
        "max_print_price": max(row["price"] for row in rows),
        "min_vs_auction_percent": percent(min(row["price"] for row in rows), auction_price),
        "reported_trade_amount_yuan": round(sum(row["amount"] for row in rows), 2),
        "reported_trade_count": len(rows), "reference_limit_up_price": limit,
        "first_limit_price_print": first["time"] if first else None,
        "below_limit_prints_after_first_touch": len(below_after_touch),
        "first_below_limit_after_touch": below_after_touch[0]["time"] if below_after_touch else None,
        "last_print_at_limit": abs(last["price"] - limit) < 0.005,
        "sealed": None,
        "note": "价格为行情商汇总成交记录；未提供五档挂单，不能认定稳定封板、排队成交或资金净流入。",
    }


def collect(candidate: dict, trade_date: str) -> dict:
    code = candidate["code"]
    symbol = ("sh" if code.startswith("6") else "sz") + code
    result = {"code": code, "name": candidate["name"],
              "saved_candidate_context": {key: candidate.get(key) for key in FIELDS},
              "errors": [], "trades": [], "minute_bars": []}
    # 此处最新报价仅用于交易日期校验，其价格、盘口与资金绝不参与开盘判断。
    try:
        quote = EastmoneyProvider(timeout=6).quote(code)
        result["date_check_quote"] = {"quote_time": quote.quote_time, "source": quote.quote_source}
    except Exception as exc:
        result["errors"].append(f"报价日期校验失败：{exc}")
    page_urls = []
    seen = {}
    reached_cutoff = False
    try:
        for page in range(6):
            page_urls.append("https://stock.gtimg.cn/data/index.php?" + urlencode({
                "appn": "detail", "action": "data", "c": symbol, "p": page,
                "d": trade_date.replace("-", "")}))
            raw = _fetch_tencent_page(symbol, page, timeout=6, trade_date=trade_date)
            for values in raw:
                if len(values) < 7:
                    continue
                row = {"id": int(values[0]), "time": values[1], "price": float(values[2]),
                       "volume_hands": float(values[4]), "amount": float(values[5]),
                       "side": values[6]}
                seen[row["id"]] = row
            if raw and raw[-1][1] >= CUTOFF:
                reached_cutoff = True
                break
            if not raw:
                break
    except Exception as exc:
        result["errors"].append(f"开盘成交明细获取失败：{exc}")
    result["trades"] = sorted((row for row in seen.values() if "09:25:00" <= row["time"] <= CUTOFF),
                              key=lambda row: (row["time"], row["id"]))
    minute_url = (
        "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20auction=/"
        "CN_MarketDataService.getKLineData?" + urlencode({
            "symbol": symbol, "scale": 1, "ma": "no", "datalen": 1970}))
    try:
        req = Request(minute_url, headers={"User-Agent": "Mozilla/5.0",
                                          "Referer": "https://finance.sina.com.cn/"})
        with urlopen(req, timeout=8) as response:
            body = response.read().decode("utf-8", errors="replace")
        match = re.search(r"=\((\[.*\])\)\s*;?\s*$", body, re.S)
        bars = json.loads(match.group(1)) if match else []
        result["minute_bars"] = [bar for bar in bars
                                 if f"{trade_date} 09:31:00" <= str(bar.get("day", ""))
                                 <= f"{trade_date} {CUTOFF}"]
    except Exception as exc:
        result["errors"].append(f"带日期分钟线获取失败：{exc}")
    auction = next((row for row in result["trades"] if row["time"].startswith("09:25:")), None)
    result["auction_trade"] = auction
    checks = {
        "current_quote_date_matches": str((result.get("date_check_quote") or {}).get("quote_time", "")).startswith(trade_date),
        "dated_five_minute_bars_present": {bar["day"] for bar in result["minute_bars"]}
        == {f"{trade_date} 09:{minute}:00" for minute in range(31, 36)},
        "trade_pages_reach_cutoff": reached_cutoff,
        "auction_price_matches_saved": bool(auction and abs(auction["price"] - candidate["auction_price"]) < 0.005),
        "auction_amount_matches_saved": bool(auction and abs(auction["amount"] - candidate["auction_amount"]) <= 1),
        "minute_open_matches_auction": bool(auction and result["minute_bars"] and
            abs(float(result["minute_bars"][0]["open"]) - auction["price"]) < 0.005),
    }
    result["data_checks"] = checks
    result["opening_data_cross_checked"] = all(checks.values()) and not result["errors"]
    result["sources"] = {"trade_urls": page_urls, "minute_url": minute_url,
                         "date_caveat": "腾讯成交明细行本身不含交易日期；以当日报价日期、存档竞价及带日期分钟线交叉校验。"}
    result["opening"] = opening_facts(result["trades"], candidate["previous_close"], candidate["auction_price"])
    result.update(recommended=False, actionable=False, execution_ready=False,
                  formal_confirmation_status="unverifiable_missing_opening_book_and_funds",
                  missing_evidence=["开盘五档盘口及封单", "同一时点资金数据", "两次间隔20秒的有效连续采样",
                                    "完整盘前留存候选池与风险核验快照"])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=datetime.now(CHINA).date().isoformat())
    args = parser.parse_args()
    now = datetime.now(CHINA)
    if args.date != now.date().isoformat():
        parser.error("成交服务未可靠证明支持历史日期，本工具仅允许核验今天；不将当前成交伪装成历史。")
    source = ROOT / "data" / "board_plan_snapshots.json"
    day = json.loads(source.read_text(encoding="utf-8"))["days"][args.date]
    snapshot = day["replay"]
    unique = {item["code"]: item for item in snapshot.get("candidates", []) + snapshot.get("watch_candidates", [])}
    with ThreadPoolExecutor(max_workers=3) as executor:
        rows = list(executor.map(lambda item: collect(item, args.date), unique.values()))
    output = {
        "trade_date": args.date, "generated_at": datetime.now(CHINA).isoformat(),
        "strategy_version": snapshot.get("strategy_version"),
        "kind": "opening_fact_replay_not_live_recommendations", "cutoff": CUTOFF,
        "candidate_pool_snapshot_generated_at": snapshot.get("generated_at"),
        "candidate_pool_source": str(source), "candidate_count": len(rows),
        "formal_recommendations": [], "primary_code": None,
        "limitations": ["范围仅为收盘后复盘池中的主榜及折叠候选，不能代表全市场盘前选股结果。",
                        "历史结构评分引用最新规则存档；池来源、风险与流通市值上下文可能包含盘后数据。",
                        "未用09:35之后的价格表现、资金或盘口筛选；最新报价仅校验交易日期。",
                        "缺少开盘连续盘口和资金，不能补造正式推荐或静默实验触发，不写首选账本。",
                        "未提供下一交易日结果，不能判断次日大涨概率。"],
        "assessments": rows,
    }
    path = ROOT / "reports" / f"{args.date}-opening-replay-{now:%H%M%S%f}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(output, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"path": str(path), "rows": [
        {"code": row["code"], "name": row["name"], "checks": row["data_checks"],
         "opening": row["opening"], "minutes": row["minute_bars"], "errors": row["errors"]}
        for row in rows]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
