"""行情源时间校验；获取时间不能代替行情时间。"""
from datetime import datetime, timedelta, timezone
from math import isfinite

CHINA_TZ = timezone(timedelta(hours=8))
MAX_QUOTE_AGE_SECONDS = 60


def china_time(value: datetime) -> datetime:
    return value.replace(tzinfo=CHINA_TZ) if value.tzinfo is None else value.astimezone(CHINA_TZ)


def parse_quote_time(value: object) -> datetime | None:
    try:
        if isinstance(value, datetime):
            return china_time(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not isfinite(value) or value <= 0:
                return None
            return datetime.fromtimestamp(value, CHINA_TZ)
        if isinstance(value, str) and len(value) == 14 and value.isdigit():
            return china_time(datetime.strptime(value, "%Y%m%d%H%M%S"))
        if isinstance(value, str) and len(value) >= 19:
            return china_time(datetime.fromisoformat(value))
    except (ValueError, OverflowError, OSError):
        pass
    return None


def quote_time_text(value: object) -> str | None:
    parsed = parse_quote_time(value)
    return parsed.isoformat(timespec="seconds") if parsed else None


def quote_freshness(item: dict, now: datetime) -> tuple[datetime | None, str | None]:
    timestamp = parse_quote_time(item.get("quote_time"))
    now = china_time(now)
    if timestamp is None:
        return None, "行情源时间缺失，不能作为有效采样"
    age = (now - timestamp).total_seconds()
    if timestamp.date() != now.date():
        return timestamp, "行情不是今天的数据"
    if age < -5:
        return timestamp, "行情时间超前，等待时钟核验"
    if age > MAX_QUOTE_AGE_SECONDS:
        return timestamp, "行情已超过60秒，等待新数据"
    return timestamp, None
