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
        action, tone = "等待盘中封板确认", "neutral"
        timing = "竞价只建观察池，不直接排队；盘中通过两次有效采样并进入前五后再评估"
        reference_text = f"涨停参考价¥{limit_price:.2f}" if limit_price else "涨停价待交易所行情确认"
    elif candidate.get("actionable"):
        action, tone = "等待盘中封板确认", "neutral"
        timing = "普通开盘承接不再作为买点；以封板确认精选为准，极强开盘例外也须盘中复核"
        reference_text = f"竞价支撑参考¥{auction_price:.2f}，不是买点" if auction_price else "等待开盘承接价确认"
    elif "反核" in str(candidate.get("strategy_mode") or ""):
        action, tone = "高风险观望", "neutral"
        timing = "反核与修复仅保留观察，等待封板及风险复核，不因急拉直接买入"
        reference_text = f"竞价支撑参考¥{auction_price:.2f}" if auction_price else "等待止跌价形成"
    else:
        action, tone = "观望", "neutral"
        timing = "当前只属观察池；等待实际封板或极强开盘例外通过两次采样，并成为唯一首选"
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
    elif item.get("focus_locked"):
        action, action_tone = "只看风险（今日已选定）", "neutral"
        timing = "已锁定今日关注对象，不再新增买入提示；仍按原仓位与风险纪律处理，不代表已经成交"
        reference_text = "此操作仅管理提示，不下单、不加仓"
    elif sealed and item.get("recommended") and item.get("recommendation_kind") == "sealed" and not failed_board and tone != "reject":
        action, action_tone = "排队打板", "positive"
        timing = "已成为唯一首选且两次采样通过；仅按涨停价小单排队，撤单或炸板即撤销资格，不保证成交"
        reference_text = f"涨停价¥{limit_price:.2f}" if limit_price else "涨停价待确认"
        conditions = ["封单保持稳定", "开板即取消资格，回封后重新采样", "板块强度没有同步转弱"]
    elif sealed:
        action, action_tone = "观望（已封板）", "neutral"
        timing = "一次触板不等于确认；等待两次间隔20秒的盘口和资金采样通过并进入前五"
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
    elif item.get("recommended") and item.get("recommendation_kind") == "strong_open" and change < 8.5:
        action, action_tone = "极强开盘小仓观察", "positive"
        timing = "仅高换手强竞价、资金买盘同步强势且两次采样通过的开盘例外；不追快速拉升，转弱即取消"
        reference_text = f"现价¥{price:.2f}，竞价支撑¥{auction_price:.2f}"
        conditions = ["09:30–09:35有效", "盘口失衡≥35%且主力占比≥3%", "当前唯一首选"]
    elif tone == "confirm":
        action, action_tone = "等待封板打板", "neutral"
        timing = "开盘转强或回踩修复仅用于观察；等实际封板并进入当前前五，不提前扫单"
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
