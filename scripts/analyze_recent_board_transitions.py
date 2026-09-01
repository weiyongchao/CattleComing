from __future__ import annotations

import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.stock_evaluator.market import EastmoneyProvider
from src.stock_evaluator.universe import previous_limit_up_pool


TARGET_DATES = (
    date(2026, 8, 25), date(2026, 8, 26), date(2026, 8, 27),
    date(2026, 8, 28), date(2026, 8, 31),
)
SEQUENCE = (date(2026, 8, 24),) + TARGET_DATES
REPORT_DIR = ROOT / "reports"


def number(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def pool_on(trade_date: date) -> list[dict]:
    rows = previous_limit_up_pool(trade_date + timedelta(days=1))
    actual = {str(row.get("trade_date") or "") for row in rows}
    if actual != {trade_date.isoformat()}:
        raise RuntimeError(f"涨停池日期错位：期望{trade_date.isoformat()}，实际{sorted(actual)}")
    return rows


def group_stats(name: str, rows: list[dict]) -> dict:
    advanced = [row for row in rows if row["advanced"]]
    t1 = [row for row in advanced if row["t1_close_return"] is not None]
    strong = [row for row in t1 if row["t1_close_return"] >= 5]
    negative = [row for row in t1 if row["t1_close_return"] < 0]
    return {
        "name": name,
        "candidate_count": len(rows),
        "advance_count": len(advanced),
        "advance_percent": round(len(advanced) / len(rows) * 100, 1) if rows else None,
        "t1_count": len(t1),
        "t1_strong_count": len(strong),
        "t1_strong_percent": round(len(strong) / len(t1) * 100, 1) if t1 else None,
        "t1_negative_count": len(negative),
        "t1_negative_percent": round(len(negative) / len(t1) * 100, 1) if t1 else None,
    }


def analyze() -> dict:
    pools = {day: pool_on(day) for day in SEQUENCE}
    pool_maps = {day: {str(row.get("c") or ""): row for row in rows} for day, rows in pools.items()}
    candidates: list[tuple[date, dict]] = []
    for index, target in enumerate(TARGET_DATES, start=1):
        previous_day = SEQUENCE[index - 1]
        candidates.extend((target, row) for row in pools[previous_day] if int(number(row.get("lbc"))) >= 2)

    provider = EastmoneyProvider(timeout=8)
    histories: dict[str, list] = {}
    codes = sorted({str(row.get("c") or "") for _, row in candidates})
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(provider.history, code, 160): code for code in codes}
        for future in as_completed(futures):
            histories[futures[future]] = future.result()

    rows: list[dict] = []
    target_index = {day: index for index, day in enumerate(TARGET_DATES)}
    for target, previous in candidates:
        code = str(previous.get("c") or "")
        previous_boards = int(number(previous.get("lbc")))
        current = pool_maps[target].get(code)
        current_boards = int(number((current or {}).get("lbc")))
        advanced = bool(current and current_boards == previous_boards + 1)
        bars = histories.get(code) or []
        bar_index = next((index for index, bar in enumerate(bars) if bar.trade_date == target), None)
        if bar_index is None or bar_index == 0:
            continue
        bar = bars[bar_index]
        previous_close = bars[bar_index - 1].close
        gap = (bar.open / previous_close - 1) * 100 if previous_close else 0.0
        t1_close_return = None
        if bar_index + 1 < len(bars) and bars[bar_index + 1].trade_date <= TARGET_DATES[-1]:
            next_bar = bars[bar_index + 1]
            t1_close_return = (next_bar.close / bar.close - 1) * 100 if bar.close else None
        previous_amount = number(previous.get("amount"))
        previous_seal = number(previous.get("fund"))
        row = {
            "date": target.isoformat(),
            "code": code,
            "name": str(previous.get("n") or code),
            "transition": f"{previous_boards}进{previous_boards + 1}",
            "previous_boards": previous_boards,
            "advanced": advanced,
            "current_boards": current_boards if advanced else None,
            "gap_percent": round(gap, 2),
            "previous_first_seal_time": int(number(previous.get("fbt"))) or None,
            "previous_final_seal_time": int(number(previous.get("lbt"))) or None,
            "previous_breaks": int(number(previous.get("zbc"))),
            "previous_turnover_percent": round(number(previous.get("hs")), 2),
            "previous_amount": previous_amount,
            "previous_seal_amount": previous_seal,
            "previous_seal_to_amount_percent": round(previous_seal / previous_amount * 100, 2) if previous_amount else None,
            "float_market_cap": number(previous.get("ltsz")),
            "industry": str(previous.get("hybk") or ""),
            "current_first_seal_time": int(number((current or {}).get("fbt"))) or None,
            "current_final_seal_time": int(number((current or {}).get("lbt"))) or None,
            "current_breaks": int(number((current or {}).get("zbc"))) if current else None,
            "current_turnover_percent": round(number((current or {}).get("hs")), 2) if current else None,
            "current_seal_to_amount_percent": (
                round(number(current.get("fund")) / number(current.get("amount")) * 100, 2)
                if current and number(current.get("amount")) else None
            ),
            "t1_close_return": round(t1_close_return, 2) if t1_close_return is not None else None,
        }
        rows.append(row)

    board_groups = [
        group_stats(f"{boards}进{boards + 1}", [row for row in rows if row["previous_boards"] == boards])
        for boards in (2, 3, 4)
    ]
    board_groups.append(group_stats("5板及以上晋级", [row for row in rows if row["previous_boards"] >= 5]))

    feature_groups = [
        group_stats("昨日最终封板<11:30", [row for row in rows if 0 < (row["previous_final_seal_time"] or 0) < 113000]),
        group_stats("昨日最终封板≥11:30", [row for row in rows if (row["previous_final_seal_time"] or 0) >= 113000]),
        group_stats("昨日零炸板", [row for row in rows if row["previous_breaks"] == 0]),
        group_stats("昨日有炸板", [row for row in rows if row["previous_breaks"] > 0]),
        group_stats("昨日换手≤10%", [row for row in rows if row["previous_turnover_percent"] <= 10]),
        group_stats("昨日换手10%–20%", [row for row in rows if 10 < row["previous_turnover_percent"] <= 20]),
        group_stats("昨日换手>20%", [row for row in rows if row["previous_turnover_percent"] > 20]),
        group_stats("今日低开", [row for row in rows if row["gap_percent"] < 0]),
        group_stats("今日高开0%–3%", [row for row in rows if 0 <= row["gap_percent"] < 3]),
        group_stats("今日高开3%–5%", [row for row in rows if 3 <= row["gap_percent"] < 5]),
        group_stats("今日高开5%–9.8%", [row for row in rows if 5 <= row["gap_percent"] < 9.8]),
        group_stats("今日接近一字≥9.8%", [row for row in rows if row["gap_percent"] >= 9.8]),
    ]
    strategy_groups = [
        group_stats("早封连板基础形态", [
            row for row in rows
            if 0 < (row["previous_final_seal_time"] or 0) < 113000
            and 1 <= row["gap_percent"] < 9.8
        ]),
        group_stats("早封+昨日换手≤20%+非一字", [
            row for row in rows
            if 0 < (row["previous_final_seal_time"] or 0) < 113000
            and row["previous_turnover_percent"] <= 20
            and 1 <= row["gap_percent"] < 9.8
        ]),
        group_stats("早封+零炸板+昨日换手≤10%+非一字", [
            row for row in rows
            if 0 < (row["previous_final_seal_time"] or 0) < 113000
            and row["previous_breaks"] == 0
            and row["previous_turnover_percent"] <= 10
            and 1 <= row["gap_percent"] < 9.8
        ]),
        group_stats("二进三：早封+换手≤20%+高开1%–9.8%", [
            row for row in rows
            if row["previous_boards"] == 2
            and 0 < (row["previous_final_seal_time"] or 0) < 113000
            and row["previous_turnover_percent"] <= 20
            and 1 <= row["gap_percent"] < 9.8
        ]),
        group_stats("三板及以上：高开<5%", [
            row for row in rows if row["previous_boards"] >= 3 and -3 <= row["gap_percent"] < 5
        ]),
        group_stats("三板及以上：高开≥5%", [
            row for row in rows if row["previous_boards"] >= 3 and row["gap_percent"] >= 5
        ]),
    ]
    advanced = [row for row in rows if row["advanced"]]
    execution_groups = [
        group_stats("晋级且T日最终封板<11:30", [
            row for row in advanced if 0 < (row["current_final_seal_time"] or 0) < 113000
        ]),
        group_stats("晋级且T日最终封板≥11:30", [
            row for row in advanced if (row["current_final_seal_time"] or 0) >= 113000
        ]),
        group_stats("晋级且T日零炸板", [row for row in advanced if row["current_breaks"] == 0]),
        group_stats("晋级且T日炸板1次", [row for row in advanced if row["current_breaks"] == 1]),
        group_stats("晋级且T日炸板≥2次", [row for row in advanced if (row["current_breaks"] or 0) >= 2]),
        group_stats("晋级且T日换手≤10%", [
            row for row in advanced if (row["current_turnover_percent"] or 0) <= 10
        ]),
        group_stats("晋级且T日换手10%–20%", [
            row for row in advanced if 10 < (row["current_turnover_percent"] or 0) <= 20
        ]),
        group_stats("晋级且T日换手>20%", [
            row for row in advanced if (row["current_turnover_percent"] or 0) > 20
        ]),
    ]
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dates": [day.isoformat() for day in TARGET_DATES],
        "sample_count": len(rows),
        "summary": group_stats("全部≥2连板晋级样本", rows),
        "board_groups": board_groups,
        "feature_groups": feature_groups,
        "strategy_groups": strategy_groups,
        "execution_groups": execution_groups,
        "rows": rows,
        "limitations": [
            "样本仅覆盖5个晋级日，同一股票会跨日重复出现，不能视为独立样本。",
            "晋级率来自收盘涨停池；开盘缺口来自日K开盘价，不等于09:25逐笔竞价成交额。",
            "T+1收盘涨幅以T日收盘为基准，未假设成交价格，也未扣除费用。",
        ],
    }


def markdown(report: dict) -> str:
    lines = [
        "# 最近五日连板晋级分析", "",
        f"样本日期：{'、'.join(report['dates'])}；全市场样本：{report['sample_count']}。", "",
        "## 板高分组", "",
        "| 分组 | 样本 | 晋级 | 晋级率 | 晋级后T+1样本 | T+1≥5% | T+1负溢价 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["board_groups"]:
        lines.append(
            f"| {row['name']} | {row['candidate_count']} | {row['advance_count']} | {row['advance_percent']}% | "
            f"{row['t1_count']} | {row['t1_strong_count']}（{row['t1_strong_percent']}%） | "
            f"{row['t1_negative_count']}（{row['t1_negative_percent']}%） |"
        )
    lines.extend(["", "## 条件分组", ""])
    for row in report["feature_groups"] + report["strategy_groups"] + report["execution_groups"]:
        lines.append(
            f"- {row['name']}：{row['advance_count']}/{row['candidate_count']}晋级（{row['advance_percent']}%）；"
            f"晋级后T+1≥5% {row['t1_strong_count']}/{row['t1_count']}，负溢价{row['t1_negative_count']}/{row['t1_count']}。"
        )
    lines.extend(["", "## 限制", ""] + [f"- {item}" for item in report["limitations"]])
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    result = analyze()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    json_path = REPORT_DIR / f"{stamp}-board-transition-audit.json"
    md_path = REPORT_DIR / f"{stamp}-board-transition-audit.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(markdown(result), encoding="utf-8")
    print(json.dumps({
        "summary": result["summary"], "board_groups": result["board_groups"],
        "feature_groups": result["feature_groups"], "strategy_groups": result["strategy_groups"],
        "execution_groups": result["execution_groups"],
        "json": str(json_path), "markdown": str(md_path),
    }, ensure_ascii=False, indent=2))
