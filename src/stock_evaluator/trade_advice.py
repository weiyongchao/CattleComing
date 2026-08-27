from __future__ import annotations


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _event_level(candidate: dict) -> str:
    return str((candidate.get("corporate_event_risk") or {}).get("level") or "normal")


def _limit_up_price(previous_close: float) -> float | None:
    return round(previous_close * 1.1 + 1e-8, 2) if previous_close > 0 else None


def auction_entry_plan(candidate: dict, auction_phase: str, market_state: str) -> dict:
    """为09:17–09:25榜单生成未持有者操作口径，不把竞价候选直接写成已确认买点。"""
    previous_close = _number(candidate.get("previous_close"))
    auction_price = _number(candidate.get("auction_price"))
    limit_price = _limit_up_price(previous_close)
    event = candidate.get("corporate_event_risk") or {}
    regulation = candidate.get("regulatory_risk") or {}
    action_text = str(candidate.get("action") or "")
    event_high = _event_level(candidate) == "high"
    event_unknown = event.get("available") is False
    regulation_high = regulation.get("level") == "high"
    hard_reject = bool(candidate.get("risk_veto") or action_text == "取消候选")
    preliminary = auction_phase in {"cancelable", "indicative"}
    historical = auction_phase == "historical"

    conditions = [
        "09:25最终撮合价量不弱于当前结构",
        "09:30后不跌破竞价价且五档卖压不占优",
        "所属题材或同方向个股保持共振",
    ]
    invalidation = [
        "开盘后跌破竞价价约1%且20秒刷新仍未收回",
        "触及涨停后炸板且不能快速回封",
        "当日主力明显流出或卖盘持续扩大",
    ]
    if event_high:
        action, tone = "重组风险观望", "negative"
        timing = "先核对最新重组公告、审批进度和复牌波动；不因题材涨停直接排队"
        reference_text = "当前不设置买入价，公告风险解除或充分消化后重新评估"
    elif historical:
        action, tone = "仅作历史复盘", "neutral"
        timing = "历史数据不生成当前买点"
        reference_text = "不可按历史竞价价追溯下单"
    elif preliminary:
        action, tone = "竞价观察", "neutral"
        timing = "09:17–09:20仅预选；09:20–09:25每20秒重排，等09:25最终撮合"
        reference_text = f"当前竞价参考¥{auction_price:.2f}，尚不是买入价" if auction_price else "竞价价待确认"
    elif market_state == "空仓":
        action, tone = "观望", "negative"
        timing = "市场总开关为空仓，不执行单票信号"
        reference_text = "等待市场环境与候选强度重新达到门槛"
    elif hard_reject or regulation_high:
        action, tone = "放弃", "negative"
        timing = "高位透支、异动监管或硬否决条件已触发"
        reference_text = "不因盘中急拉重新追入，等待下一交易日重评"
    elif candidate.get("board_entry_allowed"):
        action, tone = "排队打板", "positive"
        timing = "09:25后可按涨停价小单排队；未成交不追改价，开板成交只接受快速回封"
        reference_text = f"涨停参考价¥{limit_price:.2f}" if limit_price else "涨停价待交易所行情确认"
    elif candidate.get("actionable"):
        action, tone = "开盘确认后买入", "positive"
        timing = "09:30后等承接、盘口和成交确认；快速拉升至8.5%以上时改为等封板，不直接追高"
        reference_text = f"先看竞价支撑¥{auction_price:.2f}，确认不破后再小仓" if auction_price else "等待开盘承接价确认"
    elif "反核" in str(candidate.get("strategy_mode") or ""):
        action, tone = "高风险观望", "neutral"
        timing = "先等开盘低点止跌、收复昨收和竞价价，再考虑极小仓试错"
        reference_text = f"竞价支撑参考¥{auction_price:.2f}" if auction_price else "等待止跌价形成"
    else:
        action, tone = "观望", "neutral"
        timing = "当前仅为B级/观察候选；等20秒动态榜单升级为开盘确认或封板确认"
        reference_text = f"竞价参考¥{auction_price:.2f}，不提前追价" if auction_price else "等待实时买点"

    if event_unknown:
        conditions.append("公告接口当前不可用，人工核对交易所最新公告后才允许执行")
    if event.get("risks"):
        invalidation.extend(event["risks"][:2])
    return {
        "action": action,
        "tone": tone,
        "timing": timing,
        "reference_price": limit_price if action == "排队打板" else auction_price or None,
        "reference_text": reference_text,
        "conditions": conditions,
        "invalidation": list(dict.fromkeys(invalidation)),
        "basis": [
            f"T+1结构分{candidate.get('continuation_score', candidate.get('score', '--'))}",
            f"竞价涨幅{_number(candidate.get('auction_gap_percent')):+.2f}%",
            f"竞价成交额{_number(candidate.get('auction_amount')) / 1e8:.2f}亿",
            f"近3/5/10日{_number(candidate.get('three_day_change_percent')):+.2f}%/"
            f"{_number(candidate.get('five_day_change_percent')):+.2f}%/"
            f"{_number(candidate.get('ten_day_change_percent')):+.2f}%",
        ],
    }


def live_entry_plan(item: dict) -> dict:
    """根据盘中实时确认结果给未持有者生成可执行时机，20秒刷新时动态升降级。"""
    event = item.get("corporate_event_risk") or {}
    event_high = _event_level(item) == "high"
    price = _number(item.get("price"))
    auction_price = _number(item.get("auction_price"))
    limit_price = _number(item.get("limit_up_price"))
    change = _number(item.get("change_percent"), -99)
    tone = str(item.get("tone") or "watch")
    sealed = bool(item.get("sealed"))
    failed_board = bool(item.get("failed_board"))
    conditions: list[str] = []
    invalidation = [
        "下一轮20秒刷新转为放弃买入",
        "五档卖压明显占优或主力净占比低于-3%",
        "跌破日内低点后不能快速收回",
    ]

    if event_high:
        action, action_tone = "重组风险观望", "negative"
        timing = "不因涨停、回封或急拉直接买入，先核对重组审批与最新风险公告"
        reference_text = "当前不设置买点"
    elif sealed and item.get("board_entry_allowed"):
        action, action_tone = "排队打板", "positive"
        timing = "可按涨停价小单排队；未成交不追改价，开板成交必须快速回封"
        reference_text = f"涨停价¥{limit_price:.2f}" if limit_price else "涨停价待确认"
        conditions = ["封单保持稳定", "开板后短时间内重新封住", "板块强度没有同步转弱"]
    elif sealed:
        action, action_tone = "观望（已封板）", "neutral"
        timing = "当前不追价；只有策略明确允许的一字板才排队，普通封板等下一次机会"
        reference_text = f"现价/涨停价¥{price:.2f}" if price else "已封板"
    elif failed_board:
        action, action_tone = "等待回封", "neutral"
        timing = "炸板后不接第一波回落，重新封板并连续两轮保持买盘后才恢复打板资格"
        reference_text = f"重新封住¥{limit_price:.2f}后再评估" if limit_price else "等待重新封板"
        invalidation.append("炸板后跌幅扩大或反抽不封板")
    elif tone == "reject":
        action, action_tone = "放弃", "negative"
        timing = "当前实时价格、承接或资金已破坏竞价逻辑，今天不再追入"
        reference_text = "不设置买点"
    elif tone == "confirm" and item.get("rebound_confirmed") and change < 8.5:
        action, action_tone = "小仓买入", "positive"
        timing = "深回踩后已收复昨收与竞价价；等回踩不再创新低时小仓，不在直线拉升段追"
        reference_text = f"竞价支撑¥{auction_price:.2f}附近确认不破" if auction_price else f"现价¥{price:.2f}附近只等回踩确认"
        conditions = ["价格保持在昨收与竞价价上方", "盘口卖压不占优", "从日内低点修复后不再创新低"]
    elif tone == "confirm" and change < 8.5:
        action, action_tone = "确认后小仓买入", "positive"
        timing = "连续两轮20秒刷新保持开盘确认，回踩竞价价/开盘价不破时小仓"
        reference_text = f"竞价支撑¥{auction_price:.2f}，现价¥{price:.2f}" if auction_price else f"现价¥{price:.2f}，等待回踩确认"
        conditions = ["成交继续放大", "盘口与当日资金不转弱", "不在快速拉升段追高"]
    elif tone == "confirm":
        action, action_tone = "等待封板打板", "positive"
        timing = "涨幅已高，不直接扫单；等触及涨停并确认封单后再决定是否排队"
        reference_text = f"涨停参考价¥{limit_price:.2f}" if limit_price else "等待涨停价确认"
    else:
        action, action_tone = "观望", "neutral"
        timing = "等待收复竞价价、买盘转强或封板确认；未升级前不买"
        reference_text = f"竞价支撑¥{auction_price:.2f}，现价¥{price:.2f}" if auction_price else f"现价¥{price:.2f}"

    if event.get("risks"):
        invalidation.extend(event["risks"][:2])
    return {
        "action": action,
        "tone": action_tone,
        "timing": timing,
        "reference_price": limit_price if action in {"排队打板", "等待回封", "等待封板打板"} else auction_price or price or None,
        "reference_text": reference_text,
        "conditions": conditions,
        "invalidation": list(dict.fromkeys(invalidation)),
    }
