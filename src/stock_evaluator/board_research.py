"""追加式盘中留痕与静默实验。绝不调用正式首选账本或生成交易权限。"""
from __future__ import annotations

import copy
import json
import os
import re
from datetime import date, datetime
from math import isfinite
from pathlib import Path
from threading import RLock

from .board_selection import _number, _quality_failure, intraday_selection_window
from .quote_sampling import china_time, quote_freshness

RESEARCH_VERSION = "sealed-shadow-v1"
RESEARCH_ROOT = Path(__file__).resolve().parents[2] / "data" / "board_research"
RULE = "独立静默对照：①原70–74分实验；②早封连板55–74分实验（昨日连续≥2板且最终封板早于11:30，原早封通道条件齐备）。均要求盘中分≥90、真实封板及风险/资金核验，4次新采样间隔≥20秒且跨度≥60秒，间断>90秒或失效重置。两组分别计数，不改变正式门槛、不产生买点。"
CHAIN_EXPERIMENT = "early-chain-55-74-v1"
NOTE = "只记录实际观察；采样跨度不代表期间连续封板，炸板次数是观测下限。假设触发价不代表成交，不回填开盘收益；资金源时间未提供时仅记录交易日与获取时间。"
MAX_FILE_BYTES = 128 * 1024 * 1024


class ResearchError(RuntimeError):
    pass


def _clean(value):
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, float) and not isfinite(value):
        return None
    return value


def _day_key(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("日期格式应为YYYY-MM-DD")
    date.fromisoformat(value)
    return value


def _chain_experiment(row: dict, previous: dict, now: datetime, quote_epoch, *,
                      market: str, failure: str | None, duplicate: bool, sealed: bool, gap: bool) -> dict:
    """结构分层实验，独立于原70–74通道和正式首选；不能串用确认次数。"""
    state = dict(previous)
    reason = failure or _quality_failure(row, market, minimum_continuation=55)
    if row.get("book_available") is not True or str((row.get("funds") or {}).get("date") or "")[:10] != now.date().isoformat():
        reason = reason or "盘口或当日资金未核验"
    if row.get("quote_source") == "全市场批量行情降级":
        reason = reason or "降级行情仅留痕"
    if not 55 <= _number(row.get("continuation_score")) < 75:
        reason = reason or "不在早封连板55–74分实验范围"
    seal_time = _number(row.get("previous_final_seal_time"))
    structure = (row.get("early_final_seal_chain_matched") is True
                 and row.get("previous_day_limit_up") is True
                 and _number(row.get("consecutive_limit_up_days")) >= 2
                 and 92500 <= seal_time < 113000
                 and 1 <= _number(row.get("auction_gap_percent")) < 9.8
                 and _number(row.get("auction_amount")) > 30_000_000
                 and _number(row.get("auction_volume_percent")) > 1)
    if not structure:
        reason = reason or "缺少可核验的昨日早封连板及竞价结构"
    if _number(row.get("open_score")) < 90 or not sealed:
        reason = reason or "盘中分未达90或未实际封板"
    if reason or gap:
        state["count"] = 0
    if not reason and not duplicate:
        if not state.get("count") or quote_epoch - state["last_quote"] > 90 or now.timestamp() - state["last_collected"] > 90:
            state.update(count=1, started=quote_epoch, last_quote=quote_epoch, last_collected=now.timestamp())
        elif quote_epoch - state["last_quote"] >= 20 and now.timestamp() - state["last_collected"] >= 20:
            state.update(count=min(4, state["count"] + 1), last_quote=quote_epoch, last_collected=now.timestamp())
    span = max(0, quote_epoch - state["started"]) if quote_epoch is not None and state.get("count") else 0
    matched = not reason and not duplicate and state.get("count", 0) >= 4 and span >= 60
    first_event = bool(matched and not state.get("first_at"))
    if first_event:
        state.update(first_at=now.isoformat(timespec="seconds"), first_quote_time=row.get("quote_time"), first_price=row.get("price"))
    state.update(version=CHAIN_EXPERIMENT)
    return {"version": CHAIN_EXPERIMENT, "matched": bool(matched), "first_event": first_event,
            "reason": reason or ("重复源时间，不推进实验" if duplicate else "结构静默条件通过" if matched else "等待4次新采样且跨度≥60秒"),
            "span_seconds": span, "state": state}


def _sample(row: dict, previous: dict, now: datetime, market: str, baseline: dict) -> dict:
    state = dict(previous)
    at = now.isoformat(timespec="seconds")
    timestamp, failure = quote_freshness(row, now)
    quote_epoch = timestamp.timestamp() if timestamp else None
    highwater = state.get("last_quote_epoch")
    duplicate = not failure and highwater is not None and quote_epoch == highwater
    if not failure and highwater is not None and quote_epoch < highwater:
        failure = "行情时间倒退"
    quality_failure = _quality_failure(row, market)
    if row.get("book_available") is not True:
        quality_failure = quality_failure or "完整五档数据缺失"
    funds = row.get("funds") or {}
    if str(funds.get("date") or "")[:10] != now.date().isoformat():
        quality_failure = quality_failure or "资金交易日未核验为今天"
    if row.get("quote_source") == "全市场批量行情降级":
        quality_failure = quality_failure or "批量降级行情仅留痕"
    sealed = bool(
        row.get("sealed") and _number(row.get("limit_up_price")) > 0
        and abs(_number(row.get("price")) - _number(row.get("limit_up_price"))) < .005
        and row.get("book_available") is True and row.get("ask_volume5") == 0
        and _number(row.get("bid_volume5")) > 0
    )
    known_unsealed = (row.get("book_available") is True
                      and 0 < _number(row.get("price")) < _number(row.get("limit_up_price")) - .005)
    gap = highwater is not None and quote_epoch is not None and quote_epoch - highwater > 90
    if not failure and not duplicate:
        if gap:
            state.update(was_sealed=None, seal_since=None, shadow_count=0)
        was_sealed = state.get("was_sealed")
        if was_sealed is True and known_unsealed:
            state["observed_breaks"] = state.get("observed_breaks", 0) + 1
        if sealed and was_sealed is False and state.get("seen_seal"):
            state["observed_reseals"] = state.get("observed_reseals", 0) + 1
        if sealed and was_sealed is not True:
            state["seal_since"] = quote_epoch
        if not sealed:
            state["seal_since"] = None
        # 缺失盘口/价格、只触及涨停价都不是已观测的炸板，不能据此推导回封。
        state.update(was_sealed=True if sealed else False if known_unsealed else None,
                     seen_seal=bool(state.get("seen_seal") or sealed),
                     last_quote_epoch=quote_epoch)
    if failure:
        state.update(was_sealed=None, seal_since=None)
    shadow_failure = failure or quality_failure
    if not 70 <= _number(row.get("continuation_score")) < 75:
        shadow_failure = shadow_failure or "不在70–74分实验范围"
    if _number(row.get("open_score")) < 90:
        shadow_failure = shadow_failure or "盘中评分未达90"
    if not sealed:
        shadow_failure = shadow_failure or "未确认实际封板"
    if shadow_failure:
        state["shadow_count"] = 0
    elif not duplicate:
        last = state.get("shadow_last_quote")
        last_collected = state.get("shadow_last_collected")
        if not state.get("shadow_count") or last is None or quote_epoch - last > 90 or now.timestamp() - last_collected > 90:
            state.update(shadow_count=1, shadow_started=quote_epoch,
                         shadow_last_quote=quote_epoch, shadow_last_collected=now.timestamp())
        elif quote_epoch - last >= 20 and now.timestamp() - last_collected >= 20:
            state.update(shadow_count=state["shadow_count"] + 1,
                         shadow_last_quote=quote_epoch, shadow_last_collected=now.timestamp())
    span = max(0, quote_epoch - state.get("shadow_started", quote_epoch)) if quote_epoch and state.get("shadow_count") else 0
    shadow_match = not shadow_failure and not duplicate and state.get("shadow_count", 0) >= 4 and span >= 60
    first_event = bool(shadow_match and not state.get("first_shadow_at"))
    if first_event:
        state.update(first_shadow_at=at, first_shadow_quote_time=row.get("quote_time"),
                     first_shadow_price=row.get("price"))
    chain = _chain_experiment(row, state.get("early_chain") or {}, now, quote_epoch, market=market,
                              failure=failure, duplicate=duplicate, sealed=sealed, gap=gap)
    state["early_chain"] = chain["state"]
    if baseline.get("recommended") and not state.get("first_formal_at"):
        state.update(first_formal_at=at, first_formal_price=row.get("price"))
    state.setdefault("first_seen_at", at)
    state["last_seen_at"] = at
    state["last_quality_failure"] = quality_failure
    state["last_time_failure"] = failure
    state["sample_count"] = state.get("sample_count", 0) + 1
    status = "rejected" if failure else "duplicate" if duplicate else "fresh"
    counter = status + "_count"
    state[counter] = state.get(counter, 0) + 1
    state["seal_span_seconds"] = max(0, quote_epoch - state["seal_since"]) if quote_epoch and state.get("seal_since") else 0
    bid1_price, bid1_volume = row.get("bid1_price"), row.get("bid1_volume")
    seal_amount = None
    if sealed and bid1_price is not None and bid1_volume is not None and abs(_number(bid1_price) - _number(row.get("limit_up_price"))) < .005:
        seal_amount = round(_number(bid1_price) * _number(bid1_volume) * 100, 2)
    fields = ("code", "name", "quote_time", "quote_source", "quote_provider", "quote_error", "price", "open_price", "change_percent", "amount",
              "limit_up_price", "bid1_price", "bid1_volume", "bid_volume5", "ask_volume5", "book_available",
              "order_imbalance", "continuation_score", "open_score", "regulatory_risk", "corporate_event_checked",
              "risk_veto", "discovered_at", "discovery_source", "opening_dip", "late_final_seal_watch",
              "auction_price", "auction_gap_percent", "auction_amount", "auction_turnover_percent", "strategy_mode", "listed_sessions", "float_market_cap",
              "consecutive_limit_up_days", "early_final_seal_chain_matched", "previous_day_limit_up", "previous_final_seal_time", "auction_volume_percent")
    return _clean({
        **{key: row.get(key) for key in fields}, "collected_at": at, "status": status,
        "corporate_event_risk": {key: (row.get("corporate_event_risk") or {}).get(key) for key in ("available", "level", "label", "summary", "is_restructuring", "is_merger_acquisition")},
        "data_reason": failure or ("同一行情源时间，不重复计数" if duplicate else None),
        "quality_failure": quality_failure, "funds": funds, "sealed": sealed,
        "seal_amount": seal_amount, "coverage_gap": bool(gap), "market_state": market,
        "baseline": baseline, "shadow_match": bool(shadow_match), "first_shadow_event": first_event,
        "early_chain_experiment": {key: value for key, value in chain.items() if key != "state"},
        "shadow_reason": shadow_failure or ("重复快照，不推进实验" if duplicate else "静默条件通过" if shadow_match else "等待4次有效采样且跨度≥60秒"),
        "shadow_span_seconds": span, "state": state,
    })


class BoardResearchStore:
    """单进程使用；按日JSONL追加，读取接口不会采样或请求行情。"""

    def __init__(self, root: Path = RESEARCH_ROOT):
        self.root = Path(root)
        self.lock = RLock()
        self._key = None
        self._signature = None
        self._samples = []
        self._states = {}

    def _path(self, day: str) -> Path:
        return self.root / (_day_key(day) + ".jsonl")

    def _load(self, day: str) -> Path:
        path = self._path(day)
        info = path.stat() if path.exists() else None
        signature = (info.st_mtime_ns, info.st_size) if info else None
        if self._key == day and self._signature == signature:
            return path
        if info and info.st_size > MAX_FILE_BYTES:
            raise ResearchError("当日研究记录超过128MB，停止追加；不自动清理原文件")
        samples, states = [], {}
        if info:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.endswith("\n"):
                        raise ResearchError("研究记录尾行不完整，停止追加；原文件保留")
                    batch = json.loads(line)
                    if not isinstance(batch, dict) or batch.get("version") != RESEARCH_VERSION or batch.get("date") != day or not isinstance(batch.get("samples"), list):
                        raise ResearchError("研究记录版本或日期不匹配")
                    for row in batch["samples"]:
                        if not isinstance(row, dict) or not re.fullmatch(r"\d{6}", str(row.get("code") or "")) or not isinstance(row.get("state"), dict) or row.get("id") != len(samples) + 1:
                            raise ResearchError("研究记录结构损坏，停止追加")
                        if row.get("collected_at") != row["state"].get("last_seen_at") or not isinstance(row.get("collected_at"), str) or row.get("status") not in {"fresh", "duplicate", "rejected"}:
                            raise ResearchError("研究记录时间或状态损坏")
                        samples.append(row)
                        states[row["code"]] = row["state"]
        self._key, self._signature = day, signature
        self._samples, self._states = samples, states
        return path

    def record(self, rows: list[dict], now: datetime, *, snapshot: dict, baseline_rows: list[dict]) -> dict:
        now = china_time(now)
        day = now.date().isoformat()
        if snapshot.get("historical") or snapshot.get("selected_date") != day or not intraday_selection_window(now):
            return {"available": True, "recording": False, "message": "非当日盘中扫描，不追加历史数据", "version": RESEARCH_VERSION}
        try:
            with self.lock:
                path = self._load(day)
                pending, codes = [], set()
                baseline = {str(row.get("code")): row for row in baseline_rows}
                for row in rows:
                    code = str(row.get("code") or "")
                    if not re.fullmatch(r"\d{6}", code) or code in codes:
                        continue
                    codes.add(code)
                    prior = self._states.get(code, {})
                    base = baseline.get(code, {})
                    baseline_view = {key: base.get(key) for key in ("recommended", "actionable", "primary_pick", "selection_reason", "confirmation_samples", "potential_score")}
                    sample = _sample(row, prior, now, (snapshot.get("market") or {}).get("state", "未知"), baseline_view)
                    sample["strategy_version"] = snapshot.get("strategy_version")
                    sample["snapshot_kind"] = snapshot.get("snapshot_kind")
                    # 并发页面对同一源快照的重复请求不重复写入；缺失/过期每20秒最多一条。
                    last_at = datetime.fromisoformat(prior["last_seen_at"]) if prior.get("last_seen_at") else None
                    if last_at and (now - last_at).total_seconds() < 0:
                        continue
                    if (last_at and (now - last_at).total_seconds() < 20 and sample["status"] != "fresh"
                            and sample["quality_failure"] == prior.get("last_quality_failure")
                            and sample["state"].get("first_formal_at") == prior.get("first_formal_at")
                            and sample["state"].get("early_chain", {}).get("count") == prior.get("early_chain", {}).get("count", 0)
                            and sample["state"].get("last_time_failure") == prior.get("last_time_failure")):
                        continue
                    sample["id"] = len(self._samples) + len(pending) + 1
                    pending.append(sample)
                if pending:
                    batch = {"version": RESEARCH_VERSION, "date": day, "strategy_version": snapshot.get("strategy_version"),
                             "snapshot_generated_at": snapshot.get("generated_at"), "snapshot_kind": snapshot.get("snapshot_kind"), "samples": pending}
                    encoded = (json.dumps(batch, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
                    if (path.stat().st_size if path.exists() else 0) + len(encoded) > MAX_FILE_BYTES:
                        raise ResearchError("当日研究记录达到128MB上限，停止追加")
                    self.root.mkdir(parents=True, exist_ok=True)
                    with path.open("ab") as handle:
                        handle.write(encoded)
                        handle.flush()
                        os.fsync(handle.fileno())
                    self._samples.extend(pending)
                    self._states.update({row["code"]: row["state"] for row in pending})
                    info = path.stat()
                    self._signature = (info.st_mtime_ns, info.st_size)
                return {"available": True, "recording": True, "written": len(pending), **self._summary(), "version": RESEARCH_VERSION}
        except (OSError, ValueError, TypeError, KeyError, ResearchError) as exc:
            self._key = None
            return {"available": False, "recording": False, "message": f"研究留痕暂停：{exc}", "version": RESEARCH_VERSION}

    def _summary(self) -> dict:
        return {"sample_count": len(self._samples), "stock_count": len(self._states),
                "shadow_stock_count": sum(bool(s.get("first_shadow_at")) for s in self._states.values()),
                "early_chain_stock_count": sum(bool((s.get("early_chain") or {}).get("first_at")) for s in self._states.values()),
                "formal_stock_count": sum(bool(s.get("first_formal_at")) for s in self._states.values()),
                "last_collected_at": self._samples[-1]["collected_at"] if self._samples else None}

    def query(self, day: str, *, code: str | None = None, limit: int = 100, before: int | None = None) -> dict:
        _day_key(day)
        if code is not None and not re.fullmatch(r"\d{6}", code):
            raise ValueError("股票代码应为6位数字")
        if not 1 <= limit <= 500 or before is not None and before < 1:
            raise ValueError("limit范围1–500，before须为正整数")
        try:
            with self.lock:
                self._load(day)
                latest = {row["code"]: row for row in self._samples}
                stocks = [{"code": key, "name": latest[key].get("name"), **state,
                           "latest_status": latest[key]["status"], "latest_reason": latest[key]["shadow_reason"]}
                          for key, state in sorted(self._states.items()) if code is None or key == code]
                matches = [row for row in reversed(self._samples) if (code is None or row["code"] == code) and (before is None or row["id"] < before)]
                page = [{key: val for key, val in row.items() if key != "state"} for row in matches[:limit]]
                return copy.deepcopy({"available": True, "date": day, "version": RESEARCH_VERSION,
                                      "rule": RULE, "note": NOTE, "summary": self._summary(), "stocks": stocks,
                                      "samples": page, "next_before": page[-1]["id"] if len(matches) > limit else None})
        except (OSError, ValueError, TypeError, KeyError, ResearchError) as exc:
            raise ResearchError(f"研究记录读取失败：{exc}") from exc


BOARD_RESEARCH_STORE = BoardResearchStore()
