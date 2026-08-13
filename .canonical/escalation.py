"""commandcenter.escalation — how long a ticket has been open, in BUSINESS hours.

The support queue escalates on elapsed time, and the clock the SLA is written against is the one the desk
actually works: 09:00 to 17:00, Monday to Friday. Nights and weekends are not time the customer was kept
waiting by us, and counting them makes Monday morning's queue read as a wall of breaches that nobody can
act on — which is worse than no escalation at all, because the team learns to ignore the colour.

    A ticket raised Friday 16:30 and still open Monday 09:30 has been open for 65 wall-clock hours
    and for ONE business hour: thirty minutes on Friday afternoon, thirty on Monday morning.

Both timestamps are UTC ISO-8601 with a trailing Z, and the business day is defined in UTC — the desk is a
single region, and moving to per-region calendars is a bigger change than this module.

The rules, in the order they bite:

    * only Monday–Friday count (weekday() 0–4);
    * within a day, only the part of the interval that overlaps 09:00–17:00 counts;
    * an interval that starts before the window starts counts from 09:00, and one that ends after the
      window closes counts to 17:00;
    * an interval entirely outside the window on a given day contributes nothing, not a negative;
    * `end` before `start` is a caller bug, not zero — a silently-zero elapsed time reads as "just raised"
      and parks a breaching ticket at the bottom of the queue.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

DAY_START_HOUR = 9
DAY_END_HOUR = 17
BUSINESS_MINUTES_PER_DAY = (DAY_END_HOUR - DAY_START_HOUR) * 60


def _parse(ts: str) -> datetime:
    """Parse a UTC ISO-8601 timestamp. Accepts the trailing 'Z' the feed actually sends."""
    if not ts:
        raise ValueError("timestamp is required")
    cleaned = ts.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(cleaned)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def business_minutes_between(start_ts: str, end_ts: str) -> int:
    """Business minutes between two UTC timestamps, counting only Mon–Fri 09:00–17:00."""
    start = _parse(start_ts)
    end = _parse(end_ts)
    if end < start:
        raise ValueError(f"end {end_ts} is before start {start_ts}")

    total = 0
    day = start.date()
    last_day = end.date()
    while day <= last_day:
        if day.weekday() < 5:                       # Saturday is 5, Sunday is 6
            window_open = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) \
                .replace(hour=DAY_START_HOUR)
            window_shut = window_open.replace(hour=DAY_END_HOUR)
            # The part of THIS day's window that the interval actually covers.
            covered_from = max(start, window_open)
            covered_to = min(end, window_shut)
            if covered_to > covered_from:
                total += int((covered_to - covered_from).total_seconds() // 60)
        day += timedelta(days=1)
    return total


def is_breached(start_ts: str, now_ts: str, sla_business_minutes: int) -> bool:
    """Has this ticket used up its SLA, measured on the working clock?"""
    return business_minutes_between(start_ts, now_ts) >= sla_business_minutes
