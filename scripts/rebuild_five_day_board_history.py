from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.stock_evaluator.board_plan import BOARD_STRATEGY_VERSION, build_board_plan
from src.stock_evaluator.history import (
    _board_review_view,
    _board_source_from_plan,
    _closing_outcome,
    _review_candidate,
)
from src.stock_evaluator.market import EastmoneyProvider
from src.stock_evaluator.review_metrics import matching_sources
from src.stock_evaluator.rule_audit import build_rule_audit


DATA_DIR = ROOT / "data"
REPORT_DIR = ROOT / "reports"
HISTORY_FILE = DATA_DIR / "candidate_history.json"
PLANS_FILE = DATA_DIR / "board_plan_snapshots.json"
DEFAULT_DATES = (
    date(2026, 8, 25),
    date(2026, 8, 26),
    date(2026, 8, 27),
    date(2026, 8, 28),
    date(2026, 8, 31),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _replay_snapshot(plan: dict, day_key: str) -> dict:
    snapshot = copy.deepcopy(plan)
    snapshot.update({
        "selected_date": day_key,
        "auction_phase": "historical",
        "snapshot_kind": "latest_strategy_replay",
        "snapshot_label": "最新版策略历史回放",
        "frozen": False,
    })
    return snapshot


def _review_source(source: dict, target: date, provider: EastmoneyProvider) -> dict:
    codes = {str(item.get("code") or "") for item in source.get("candidates") or []}
    codes.discard("")
    outcomes: dict[str, dict] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(codes)))) as executor:
        futures = {executor.submit(_closing_outcome, provider, code, target): code for code in codes}
        for future in as_completed(futures):
            code = futures[future]
            try:
                outcomes[code] = future.result()
            except Exception as exc:
                errors[code] = str(exc)
    reviewed = []
    for candidate in source.get("candidates") or []:
        code = str(candidate.get("code") or "")
        outcome = outcomes.get(code)
        if outcome:
            reviewed.append(_review_candidate(candidate, "board", outcome, False))
        else:
            reviewed.append({**candidate, "error": errors.get(code, "收盘数据不可用"), "counted": False})
    review = _board_review_view({
        "reviewed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sources": {"board": {**source, "candidates": reviewed}},
    })
    if not review:
        raise RuntimeError(f"{target.isoformat()} 复盘生成失败")
    return review


def _validate_plan(snapshot: dict, target: date) -> list[str]:
    errors: list[str] = []
    candidates = snapshot.get("candidates") or []
    if snapshot.get("selected_date") != target.isoformat():
        errors.append("selected_date不匹配")
    if snapshot.get("strategy_version") != BOARD_STRATEGY_VERSION:
        errors.append("策略版本不匹配")
    if snapshot.get("snapshot_kind") != "latest_strategy_replay" or not snapshot.get("historical"):
        errors.append("不是历史回放快照")
    if len(candidates) > 5:
        errors.append("主榜超过5只")
    if not (snapshot.get("screening") or {}).get("replay_warning"):
        errors.append("缺少历史代理警告")
    expected_cutoff = target.fromordinal(target.toordinal() - 1).isoformat()
    if (snapshot.get("screening") or {}).get("corporate_event_cutoff") != expected_cutoff:
        errors.append("公告核验截止日不正确")
    for candidate in candidates:
        event = candidate.get("corporate_event_risk") or {}
        if candidate.get("corporate_event_checked") is not True:
            errors.append(f"{candidate.get('code')}公告未完成核验")
        if event.get("level") == "high" or event.get("is_merger_acquisition") or event.get("is_restructuring"):
            errors.append(f"{candidate.get('code')}并购重组风险仍在主榜")
        if candidate.get("recommended") or candidate.get("actionable") or candidate.get("execution_ready"):
            errors.append(f"{candidate.get('code')}历史代理被误标为可执行")
    return errors


def _compact_history_days(history: dict, dates: tuple[date, ...]) -> list[dict]:
    result = []
    for target in dates:
        day = (history.get("days") or {}).get(target.isoformat()) or {}
        board = ((day.get("sources") or {}).get("board") or {})
        review = day.get("review") or {}
        metrics = review.get("metrics") or {}
        result.append({
            "date": target.isoformat(),
            "count": len(board.get("candidates") or []),
            "codes": [item.get("code") for item in board.get("candidates") or []],
            "names": [item.get("name") for item in board.get("candidates") or []],
            "t0_sealed": metrics.get("t0_sealed_count"),
            "t1_count": metrics.get("t1_close_count"),
            "t1_positive": metrics.get("t1_close_positive_count"),
            "t1_strong": metrics.get("t1_strong_close_count"),
            "t1_limit": metrics.get("t1_limit_up_count"),
        })
    return result


def _build_report(manifest: dict) -> str:
    audit = manifest["rule_audit"]
    summary = audit.get("summary") or {}
    lines = [
        "# 最近五个交易日打板规则重建与复盘",
        "",
        f"- 生成时间：{manifest['generated_at']}",
        f"- 策略版本：{manifest['strategy_version']}",
        f"- 日期：{'、'.join(manifest['dates'])}",
        "- 数据口径：目标日09:31首根分钟线作为09:25竞价代理；公告仅使用目标日前一日及更早数据。",
        "- 重要限制：历史回放不是当日真实发出的推荐，也不能证明当时可成交。",
        "",
        "## 每日结果",
        "",
        "| 日期 | 数量 | 候选 | T日封板 | T+1样本 | T+1正溢价 | T+1≥5% | T+1涨停 |",
        "|---|---:|---|---:|---:|---:|---:|---:|",
    ]
    for row in manifest["days"]:
        names = "、".join(row["names"]) or "空榜"
        values = [row.get(key) for key in ("t0_sealed", "t1_count", "t1_positive", "t1_strong", "t1_limit")]
        lines.append(f"| {row['date']} | {row['count']} | {names} | " + " | ".join("--" if value is None else str(value) for value in values) + " |")
    lines.extend([
        "",
        "## 五日汇总",
        "",
        f"- 候选记录：{summary.get('candidate_count', 0)} 条，去重 {summary.get('unique_stock_count', 0)} 只。",
        f"- T日封板：{summary.get('t0_sealed_count', 0)}/{summary.get('t0_count', 0)}（{summary.get('t0_sealed_percent')}%）。",
        f"- T+1收盘正溢价：{summary.get('t1_close_positive_count', 0)}/{summary.get('t1_close_count', 0)}（{summary.get('t1_close_positive_percent')}%），均值 {summary.get('t1_close_mean')}%。",
        f"- T+1收盘≥5%：{summary.get('t1_strong_close_count', 0)}/{summary.get('t1_close_count', 0)}（{summary.get('t1_strong_close_percent')}%）。",
        f"- T+1涨停：{summary.get('t1_limit_up_count', 0)}/{summary.get('t1_limit_count', 0)}（{summary.get('t1_limit_up_percent')}%）。",
        "",
        "## 审计说明",
        "",
        f"{audit.get('limitation')}",
        "",
        "详细候选、结构分组与延续分分组见同名 JSON 报告。",
    ])
    return "\n".join(lines) + "\n"


def rebuild(dates: tuple[date, ...], *, apply: bool) -> dict:
    for path in (HISTORY_FILE, PLANS_FILE):
        if path.resolve().parent != DATA_DIR.resolve():
            raise RuntimeError(f"拒绝写入工作区数据目录之外的路径：{path}")
    initial_hashes = {path.name: _sha256(path) for path in (HISTORY_FILE, PLANS_FILE)}
    provider = EastmoneyProvider(timeout=8)
    snapshots: dict[str, dict] = {}
    reviews: dict[str, dict] = {}
    sources: dict[str, dict] = {}
    print(f"开始生成 {len(dates)} 个交易日，策略版本 {BOARD_STRATEGY_VERSION}", flush=True)
    for index, target in enumerate(dates, start=1):
        print(f"[{index}/{len(dates)}] {target.isoformat()} 全市场历史回放...", flush=True)
        plan = build_board_plan(target_date=target)
        snapshot = _replay_snapshot(plan, target.isoformat())
        errors = _validate_plan(snapshot, target)
        if errors:
            raise RuntimeError(f"{target.isoformat()} 快照校验失败：{'；'.join(errors)}")
        source = _board_source_from_plan(snapshot)
        if source is None:
            raise RuntimeError(f"{target.isoformat()} 未生成可替换的回放来源")
        review = _review_source(source, target, provider)
        reviewed_source = ((review.get("sources") or {}).get("board") or {})
        if not matching_sources(reviewed_source, source):
            raise RuntimeError(f"{target.isoformat()} 复盘与候选来源不一致")
        snapshots[target.isoformat()] = snapshot
        sources[target.isoformat()] = source
        reviews[target.isoformat()] = review
        print(f"[{index}/{len(dates)}] 完成：{len(snapshot.get('candidates') or [])} 只", flush=True)

    # 生成完成后重新读取生产文件，只替换获批日期的 replay、board 和 board review。
    plans = _read_json(PLANS_FILE)
    history = _read_json(HISTORY_FILE)
    before = _compact_history_days(history, dates)
    plans.setdefault("days", {})
    history.setdefault("days", {})
    for target in dates:
        key = target.isoformat()
        plans["days"].setdefault(key, {})["replay"] = snapshots[key]
        day = history["days"].setdefault(key, {"date": key, "sources": {}})
        day.setdefault("sources", {})["board"] = sources[key]
        old_review_sources = copy.deepcopy(((day.get("review") or {}).get("sources") or {}))
        merged_review = copy.deepcopy(reviews[key])
        merged_review["sources"] = {**old_review_sources, "board": merged_review["sources"]["board"]}
        day["review"] = merged_review

    after = _compact_history_days(history, dates)
    history_view = {"rule_version": history.get("rule_version"), "days": [history["days"][item.isoformat()] for item in reversed(dates)]}
    audit = build_rule_audit(history_view, as_of=max(dates), closed_through=max(dates), limit=len(dates))
    if audit.get("dates") != [item.isoformat() for item in reversed(dates)]:
        raise RuntimeError("规则复盘日期覆盖不完整")

    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    manifest = {
        "generated_at": generated_at,
        "strategy_version": BOARD_STRATEGY_VERSION,
        "dates": [item.isoformat() for item in dates],
        "initial_hashes": initial_hashes,
        "before": before,
        "days": after,
        "rule_audit": audit,
        "applied": False,
        "backups": {},
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_json = REPORT_DIR / f"{stamp}-five-day-board-rebuild.json"
    report_md = REPORT_DIR / f"{stamp}-five-day-board-rebuild.md"

    if apply:
        backup_dir = DATA_DIR / "backups" / stamp
        backup_dir.mkdir(parents=True, exist_ok=False)
        backup_history = backup_dir / HISTORY_FILE.name
        backup_plans = backup_dir / PLANS_FILE.name
        shutil.copy2(HISTORY_FILE, backup_history)
        shutil.copy2(PLANS_FILE, backup_plans)
        old_history = HISTORY_FILE.read_bytes()
        old_plans = PLANS_FILE.read_bytes()
        try:
            _atomic_write(PLANS_FILE, plans)
            _atomic_write(HISTORY_FILE, history)
        except Exception:
            HISTORY_FILE.write_bytes(old_history)
            PLANS_FILE.write_bytes(old_plans)
            raise
        manifest["applied"] = True
        manifest["backups"] = {
            HISTORY_FILE.name: str(backup_history),
            PLANS_FILE.name: str(backup_plans),
        }
        manifest["final_hashes"] = {path.name: _sha256(path) for path in (HISTORY_FILE, PLANS_FILE)}
        print(f"已备份至 {backup_dir}", flush=True)
        print("已原子替换候选历史与回放快照", flush=True)

    report_json.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    report_md.write_text(_build_report(manifest), encoding="utf-8")
    manifest["report_json"] = str(report_json)
    manifest["report_md"] = str(report_md)
    print(json.dumps({
        "applied": manifest["applied"], "dates": manifest["dates"],
        "days": manifest["days"], "summary": audit.get("summary"),
        "report_json": str(report_json), "report_md": str(report_md),
        "backups": manifest["backups"],
    }, ensure_ascii=False, indent=2), flush=True)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="按最新规则重建最近五个已完成交易日的打板历史")
    parser.add_argument("--apply", action="store_true", help="校验通过后备份并替换生产历史数据")
    parser.add_argument("--dates", nargs="*", help="ISO日期；默认使用已确认的五个交易日")
    args = parser.parse_args()
    dates = tuple(date.fromisoformat(value) for value in args.dates) if args.dates else DEFAULT_DATES
    if len(dates) != 5 or len(set(dates)) != 5 or dates != tuple(sorted(dates)):
        raise SystemExit("必须提供5个互不重复且升序排列的交易日")
    if any(target >= date.today() for target in dates):
        raise SystemExit("历史重建日期必须早于今天")
    rebuild(dates, apply=args.apply)


if __name__ == "__main__":
    main()
