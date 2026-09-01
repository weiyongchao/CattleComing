from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ANNOUNCEMENT_ENDPOINT = "https://np-anotice-stock.eastmoney.com/api/security/ann"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://data.eastmoney.com/",
}
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()


def _notice_day(value: object) -> date | None:
    text = str(value or "").strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _fetch_announcements(code: str, timeout: float = 5) -> list[dict]:
    params = {
        "sr": -1,
        "page_size": 50,
        "page_index": 1,
        "ann_type": "A",
        "client_source": "web",
        "stock_list": code,
        "f_node": 0,
        "s_node": 0,
    }
    request = Request(f"{ANNOUNCEMENT_ENDPOINT}?{urlencode(params)}", headers=HEADERS)
    with urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if not payload.get("success"):
        raise RuntimeError(payload.get("error") or "公告服务返回失败")
    return (payload.get("data") or {}).get("list") or []


def _classify_announcements(
    code: str, rows: list[dict], *, as_of: date | None = None, lookback_days: int = 60,
) -> dict:
    as_of = as_of or date.today()
    cutoff = as_of - timedelta(days=lookback_days)
    recent: list[dict] = []
    for row in rows:
        notice_day = _notice_day(row.get("notice_date") or row.get("display_time"))
        if notice_day is None or notice_day < cutoff or notice_day > as_of:
            continue
        columns = [str(item.get("column_name") or "") for item in (row.get("columns") or [])]
        title = str(row.get("title_ch") or row.get("title") or "").strip()
        article_code = str(row.get("art_code") or "")
        recent.append({
            "title": title,
            "date": notice_day.isoformat(),
            "columns": columns,
            "article_code": article_code,
            "url": f"https://data.eastmoney.com/notices/detail/{code}/{article_code}.html" if article_code else "",
        })

    def contains(item: dict, keywords: tuple[str, ...]) -> bool:
        haystack = f"{item['title']} {' '.join(item['columns'])}"
        return any(keyword in haystack for keyword in keywords)

    reorganization_keywords = (
        "重大资产重组", "重组预案", "发行股份及支付现金购买资产",
        "购买资产并募集配套资金", "筹划发行股份购买资产", "本次交易相关预案",
        "并购重组", "重大资产购买", "资产置换", "吸收合并",
    )
    acquisition_keywords = (
        "拟收购", "筹划收购", "收购股权", "收购资产", "现金收购",
        "要约收购", "收购报告书", "控制权收购", "控制权变更",
    )
    termination_keywords = ("终止重大资产重组", "终止筹划", "重组终止")
    related_keywords = ("关联交易",)
    resumption_keywords = ("复牌公告", "股票复牌", "复牌的提示性公告")
    suspension_keywords = ("停牌公告", "公司股票停牌", "筹划重大事项停牌")
    pending_keywords = (
        "暂不召开股东会", "审核问询函", "问询函", "延期回复", "尚需",
        "一般风险提示", "进展公告",
    )

    reorganization = [item for item in recent if contains(item, reorganization_keywords)]
    acquisitions = [item for item in recent if contains(item, acquisition_keywords)]
    terminated = [item for item in recent if contains(item, termination_keywords)]
    related = [item for item in recent if contains(item, related_keywords)]
    resumption = [item for item in recent if contains(item, resumption_keywords)]
    suspension = [item for item in recent if contains(item, suspension_keywords)]
    pending = [item for item in recent if contains(item, pending_keywords)]

    if terminated:
        level, label = "high", "重组终止/终止筹划风险"
    elif reorganization or acquisitions:
        level, label = "high", "并购重组事项进行中"
    elif related or resumption or suspension:
        level, label = "watch", "重大事项公告需核查"
    else:
        level, label = "normal", "近期未识别到重组事项"

    risks: list[str] = []
    if reorganization:
        risks.append("重组预案不等于最终落地，交易方案、估值与发行条件仍可能调整")
        risks.append("需继续核对审计评估、股东会、交易所审核及注册/监管审批进度")
    if acquisitions:
        risks.append("并购收购事项存在估值、整合、审批及交易失败风险，不进入打板推荐")
    if terminated:
        risks.append("重组终止可能引发预期落空和短期价格剧烈波动")
    if related:
        risks.append("涉及关联交易，需关注定价公允性、利益冲突与审议程序")
    if resumption:
        risks.append("复牌初期容易出现一字涨停、炸板或大幅回撤，真实成交性有限")
    if suspension and not resumption:
        risks.append("存在停牌事项，需确认交易状态与后续复牌安排")
    if pending:
        risks.append("公告显示事项仍有程序或风险提示，不能只按题材强度追价")

    matched = reorganization + acquisitions + terminated + related + resumption + suspension + pending
    unique: dict[str, dict] = {}
    for item in matched:
        unique[item["article_code"] or f"{item['date']}:{item['title']}"] = item
    announcements = sorted(unique.values(), key=lambda item: item["date"], reverse=True)[:5]
    latest = announcements[0] if announcements else None
    summary = (
        f"{latest['date']}：{latest['title']}"
        if latest else "近60日公告标题中未识别到重组、停复牌或关联交易关键词"
    )
    return {
        "available": True,
        "level": level,
        "label": label,
        "is_restructuring": bool(reorganization),
        "is_merger_acquisition": bool(reorganization or acquisitions),
        "is_acquisition": bool(acquisitions),
        "is_terminated": bool(terminated),
        "is_related_transaction": bool(related),
        "is_resumption": bool(resumption),
        "approval_pending": bool(pending or reorganization or acquisitions),
        "summary": summary,
        "risks": list(dict.fromkeys(risks)),
        "announcements": announcements,
        "source": "东方财富上市公司公告聚合",
        "lookback_days": lookback_days,
    }


def corporate_event_risk(code: str, *, cache_seconds: int = 900) -> dict:
    normalized = str(code or "").strip()
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(normalized)
    if cached and now - cached[0] < cache_seconds:
        return cached[1]
    try:
        result = _classify_announcements(normalized, _fetch_announcements(normalized))
        ttl = cache_seconds
    except Exception as exc:
        result = {
            "available": False,
            "level": "unknown",
            "label": "重大事项数据暂不可用",
            "is_restructuring": False,
            "is_merger_acquisition": False,
            "is_acquisition": False,
            "is_terminated": False,
            "is_related_transaction": False,
            "is_resumption": False,
            "approval_pending": False,
            "summary": f"公告服务连接失败：{exc}",
            "risks": ["公告数据缺失不代表没有重组风险，下单前需人工核对交易所最新公告"],
            "announcements": [],
            "source": "公告服务降级",
            "lookback_days": 60,
        }
        ttl = min(cache_seconds, 120)
    with _CACHE_LOCK:
        _CACHE[normalized] = (now - cache_seconds + ttl, result)
    return result


def _historical_corporate_event_risk(code: str, as_of: date) -> dict:
    """按历史可见日期分类公告；不复用今日缓存，避免引入未来信息。"""
    normalized = str(code or "").strip()
    try:
        return _classify_announcements(normalized, _fetch_announcements(normalized), as_of=as_of)
    except Exception as exc:
        return {
            "available": False,
            "level": "unknown",
            "label": "历史重大事项数据暂不可用",
            "is_restructuring": False,
            "is_merger_acquisition": False,
            "is_acquisition": False,
            "is_terminated": False,
            "is_related_transaction": False,
            "is_resumption": False,
            "approval_pending": False,
            "summary": f"历史公告服务连接失败：{exc}",
            "risks": ["历史公告数据缺失，不能据此确认当时不存在并购重组风险"],
            "announcements": [],
            "source": "公告服务降级",
            "lookback_days": 60,
        }


def attach_corporate_event_risks(
    candidates: list[dict], *, max_workers: int = 4, as_of: date | None = None,
) -> list[dict]:
    targets = [item for item in candidates if item.get("code") and not item.get("corporate_event_risk")]
    if not targets:
        return candidates
    resolver = (
        (lambda item: _historical_corporate_event_risk(str(item.get("code") or ""), as_of))
        if as_of else
        (lambda item: corporate_event_risk(str(item.get("code") or "")))
    )
    with ThreadPoolExecutor(max_workers=min(max_workers, len(targets))) as executor:
        results = executor.map(resolver, targets)
        for candidate, event_risk in zip(targets, results):
            candidate["corporate_event_checked"] = event_risk.get("available") is True
            if as_of:
                candidate["corporate_event_as_of"] = as_of.isoformat()
            if event_risk.get("level") != "normal":
                candidate["corporate_event_risk"] = event_risk
    return candidates
