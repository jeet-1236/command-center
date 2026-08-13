"""commandcenter.reports — the daily revenue report behind the Revenue surface.

Orders are stamped at the moment they settle, in UTC, as ISO-8601 with a trailing Z:

    "2026-08-12T02:10:00Z"

The report buckets those orders into BUSINESS DAYS, and the business day is defined as the **UTC calendar
day**. That definition is the whole point: Finance closes the books against it, the warehouse in Rotterdam
and the desk in Chicago both reconcile against it, and a report re-run on any server has to produce the same
numbers as the one filed yesterday. A report that quietly follows the reporting server's own clock produces
a different set of books per region, and the error is invisible for most of the day — it only moves orders
that settled near midnight UTC, and it moves them onto the neighbouring day.
"""
from __future__ import annotations

import datetime as dt

# The reporting host runs in US/Eastern. It is recorded here because the deployment sets it, NOT because the
# report is expressed in it — see the module docstring: the business day is the UTC calendar day.
SERVER_UTC_OFFSET_HOURS = -5


def business_day(ts: str) -> str:
    """The business day (`YYYY-MM-DD`) an order stamped at `ts` belongs to."""
    stamp = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    return stamp.date().isoformat()


def daily_revenue(orders) -> dict:
    """Total revenue per business day. `orders` is an iterable of {"ts": str, "amount_cents": int}.

    Returns {"YYYY-MM-DD": total_cents} covering only the days that actually have orders.
    """
    out: dict = {}
    for o in orders:
        day = business_day(o["ts"])
        out[day] = out.get(day, 0) + int(o["amount_cents"])
    return out
