from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


UTC = timezone.utc


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"datetime must be timezone-aware before UTC conversion: {value!r}")
    return value.astimezone(UTC)


def to_beijing(value: datetime, timezone_name: str = "Asia/Shanghai") -> datetime:
    return ensure_utc(value).astimezone(ZoneInfo(timezone_name))


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime) -> str:
    return ensure_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    # Some public APIs (including Bluesky) emit nanosecond precision while
    # Python 3.9's datetime parser accepts at most six fractional digits.
    value = re.sub(r"(\.\d{6})\d+(?=(?:Z|[+-]\d{2}:\d{2})$)", r"\1", value)
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return ensure_utc(datetime.fromisoformat(value))


def floor_to_minutes(value: datetime, minutes: int) -> datetime:
    value = ensure_utc(value)
    discard = timedelta(
        minutes=value.minute % minutes,
        seconds=value.second,
        microseconds=value.microsecond,
    )
    return value - discard


def ceil_to_minutes(value: datetime, minutes: int) -> datetime:
    floored = floor_to_minutes(value, minutes)
    if floored == ensure_utc(value):
        return floored
    return floored + timedelta(minutes=minutes)
