"""commandcenter.pagination — paging the audit export without losing or repeating rows.

Compliance exports run to tens of thousands of rows, so they are fetched a page at a time and stitched back
together. The property the whole export depends on is that stitching the pages gives you the table:

    every row appears EXACTLY ONCE across the pages, and the pages together are the whole set.

Offset paging cannot promise that on this data. Rows are ordered by `created_at`, audit rows are written in
batches, and a batch shares a timestamp to the second — so a page boundary that lands in the middle of a
tied group is free to order that group differently on the next query. A row that moves later is fetched
twice; a row that moves earlier is never fetched at all. The export is short by a handful of rows and long
by a handful of duplicates, and neither is visible without counting.

The fix is not a bigger page. It is (a) an ordering with NO ties, and (b) a cursor that says "resume after
this exact row" rather than "skip this many rows":

    ORDER BY created_at, id     — `id` is unique, so the order is total and stable across queries
    WHERE (created_at, id) > (last_created_at, last_id)    — resume from a position, not a count

The tuple comparison is the part worth being careful about: it is not `created_at > last AND id > last_id`,
which drops every remaining row in the boundary group. It is "a later timestamp, OR the same timestamp and
a later id".

`rows` are dicts with at least `id` (unique, comparable) and `created_at` (ISO-8601 string). The functions
here are pure — the real query does this in SQL, and this is the shape it has to produce.
"""
from __future__ import annotations


def _key(row: dict) -> tuple:
    """The total ordering: timestamp first, then the unique id to break every tie."""
    return (row.get("created_at", ""), row.get("id", ""))


def page(rows: list[dict], after: dict | None = None, limit: int = 100) -> list[dict]:
    """The next `limit` rows after the cursor row `after`, in the stable order.

    `after` is the LAST ROW of the previous page (or None for the first page) — a position, not a count.
    Raises ValueError on a non-positive limit: a limit of zero would return an empty page forever and the
    caller's loop would either spin or stop early with a partial export.
    """
    if limit <= 0:
        raise ValueError(f"limit must be positive, got {limit}")
    ordered = sorted(rows, key=_key)
    if after is None:
        return ordered[:limit]
    cursor = _key(after)
    # Strictly after the cursor in the TOTAL order — a later timestamp, or the same timestamp and a later
    # id. Comparing the tuples does exactly that; comparing the fields separately does not.
    return [r for r in ordered if _key(r) > cursor][:limit]


def export_all(rows: list[dict], limit: int = 100) -> list[dict]:
    """Page through everything the way the exporter does, and return the stitched result."""
    out: list[dict] = []
    cursor: dict | None = None
    while True:
        got = page(rows, after=cursor, limit=limit)
        if not got:
            return out
        out.extend(got)
        cursor = got[-1]


def export_is_complete(rows: list[dict], exported: list[dict]) -> bool:
    """The check the compliance job runs: same count, and every id exactly once."""
    ids = [r.get("id") for r in exported]
    return len(ids) == len(rows) and sorted(ids) == sorted(r.get("id") for r in rows)
