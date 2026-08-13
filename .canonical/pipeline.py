"""commandcenter.pipeline — the Revenue/Deals pipeline-value rollup shown on the dashboard.

The deals feed is a JOIN of deals with their settlement-currency legs, so a deal that settles in more than one
currency arrives as MULTIPLE rows — one per currency — each row carrying the deal's full converted value in
`amount_usd`. A row is:  {"id": str, "name": str, "amount_usd": int, "currency": str, "stage": str}.
Money is integer USD for readability.
"""
from __future__ import annotations

OPEN_STAGES = ("prospect", "qualified", "proposal", "negotiation")


def is_open(row) -> bool:
    """True for a deal still in the open pipeline (not closed_won / closed_lost)."""
    return row.get("stage") in OPEN_STAGES


def pipeline_total(rows) -> int:
    """Total OPEN-pipeline value = the sum of each distinct open deal's converted value.

    KNOWN-ISSUE (cc-code-1): the feed is a per-currency JOIN, so a multi-currency deal appears as several rows
    that each carry the deal's FULL `amount_usd`. Summing `amount_usd` across rows counts such a deal once per
    currency, inflating the pipeline total. The rollup must count each distinct deal `id` exactly once.
    """
    total = 0
    seen_ids = set()
    for r in rows:
        if is_open(r):
            deal_id = r.get("id")
            if deal_id not in seen_ids:
                seen_ids.add(deal_id)
                total += int(r["amount_usd"])
    return total   # BUG: multi-currency deals counted N times
