"""Trading session helpers (Asia/Seoul wall-clock)."""
from __future__ import annotations

from datetime import datetime, time, date
from zoneinfo import ZoneInfo


SEOUL = ZoneInfo("Asia/Seoul")


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def in_session(now: datetime, start: str, end: str, tz: str = "Asia/Seoul") -> bool:
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(tz))
    else:
        now = now.astimezone(ZoneInfo(tz))
    t = now.time()
    return parse_hhmm(start) <= t <= parse_hhmm(end)


def session_date(now: datetime, tz: str = "Asia/Seoul") -> date:
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(tz))
    else:
        now = now.astimezone(ZoneInfo(tz))
    return now.date()


def ensure_aware(now: datetime, tz: str = "Asia/Seoul") -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=ZoneInfo(tz))
    return now.astimezone(ZoneInfo(tz))
