"""samples/commandcenter/sidecar/livechecks.py — the Command Center's live application checks.

The four L2 code incidents are shown on the dashboard by RUNNING the application's own code, not by reading
a fault flag. Each check calls a real function in `commandcenter/` with a real input and reports what came
back next to what should have come back.

That distinction is the point of this file. A flag-driven panel can only ever show a rehearsal: it goes red
because someone set a boolean and green because someone cleared it. These checks go red because the code in
the served tree is genuinely wrong, and they go green the moment a correct version of that code is on disk —
which, in the demo, is when AgentForge's reviewed patch is deployed to the served tree on approval. Nobody
has to be trusted for the audience to believe the heal; they can read the input and add it up themselves.

Modules are re-imported when their file changes on disk (`_module`), so a deployed fix takes effect on the
next poll without restarting the sidecar.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import threading
import time
import traceback
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def _app_root() -> str:
    """The directory that holds the `commandcenter` package.

    Two layouts ship this file: the monorepo's `samples/commandcenter/sidecar/livechecks.py`, where the
    package is one level up, and the deployed Command Center repository, where `sidecar.py` sits at the root
    beside `commandcenter/`. Assuming either one breaks the other, and the failure is silent — the checks
    just report that they could not import the module they are supposed to be running.
    """
    for cand in (os.path.dirname(HERE), HERE):
        if os.path.isdir(os.path.join(cand, "commandcenter")):
            return cand
    return os.path.dirname(HERE)


APP_ROOT = _app_root()
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)

_DIGESTS: dict = {}


def _module(name: str):
    """Import `commandcenter.<name>`, reloading it if the file changed since we last looked.

    This is what makes "approve the PR → the app corrects itself" real: the deploy writes the patched file
    into the served tree and the very next poll runs the new code.

    The change is detected by hashing the file rather than reading its mtime. These modules are a couple of
    kilobytes so the hash costs nothing, and it removes the one way this could go quietly wrong: two writes
    inside the same filesystem timestamp tick, where an mtime check would keep serving the stale module and
    the dashboard would show a fix that had already been deployed as still broken.
    """
    mod_name = f"commandcenter.{name}"
    path = os.path.join(APP_ROOT, "commandcenter", f"{name}.py")
    try:
        with open(path, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        digest = ""
    mod = sys.modules.get(mod_name)
    if mod is None:
        mod = importlib.import_module(mod_name)
    elif _DIGESTS.get(mod_name) != digest:
        mod = importlib.reload(mod)
    _DIGESTS[mod_name] = digest
    return mod


# ── the checks ────────────────────────────────────────────────────────────────────────────────────────────
# Each returns (actual_repr, ok, error). `expected` is a fixed string on the descriptor: it is what the
# module's own docstring and its test suite say the answer is, written out so the audience can check it.


def _check_contacts():
    validate = _module("contacts").validate_contact
    control = validate({"name": "Dana White", "phone": "555-018-2231"})
    real = validate({"name": "Siobhán O'Brien", "phone": "+44 20 7946 0958"})
    if control:
        return f"rejected fields {control} (even the US control case)", False, ""
    if real:
        return f"REJECTED — invalid: {', '.join(real)}", False, ""
    return "accepted — contact saved", True, ""


def _check_intake():
    handle = _module("intake").handle_note
    try:
        status, body = handle({"ticket_id": "TCK-4471", "notes": None})
    except Exception:  # noqa: BLE001 — an uncaught exception IS the symptom under test
        line = traceback.format_exc().strip().splitlines()[-1]
        return f"500 Internal Server Error — {line}", False, line
    if status != 201:
        return f"{status} {body.get('error', body)}", False, ""
    return f"201 Created — note stored as {body.get('notes', '')!r}", True, ""


def _check_orders():
    total = _module("orders").order_total
    got = total(10000, discount_pct=10, tax_pct=20)
    return f"${got / 100:,.2f} charged", got == 10800, ""


def _check_reports():
    reports = _module("reports")
    orders = [
        {"ts": "2026-08-11T23:40:00Z", "amount_cents": 12000},
        {"ts": "2026-08-12T02:10:00Z", "amount_cents": 48000},
        {"ts": "2026-08-12T15:00:00Z", "amount_cents": 30000},
    ]
    got = reports.daily_revenue(orders)
    want = {"2026-08-11": 12000, "2026-08-12": 78000}
    shown = " · ".join(f"{d} ${v / 100:,.0f}" for d, v in sorted(got.items()))
    return shown, got == want, ""


def _check_theme():
    theme = _module("theme")
    got = theme.status_ok_color()
    return f"healthy renders {got}", got == theme.BRAND_OK, ""


# The open deals the Pipeline value is computed from. The feed is a join with the settlement-currency legs,
# so a deal that settles in two currencies arrives as TWO rows each carrying its full converted value —
# which is the thing the rollup has to not count twice. Unique total $1.84M; counting per currency $2.04M.
PIPE_DEALS = [
    ("ACME-1001", 100000, ("USD", "EUR")), ("GLOBEX-2044", 60000, ("USD", "GBP")),
    ("INITECH-330", 40000, ("USD", "JPY")), ("HOOLI-4110", 820000, ("USD",)),
    ("STARK-5001", 520000, ("USD",)), ("WAYNE-6200", 300000, ("USD",)),
]
PIPE_ROWS = [{"id": did, "name": did, "amount_usd": usd, "currency": ccy, "stage": "proposal"}
             for did, usd, ccys in PIPE_DEALS for ccy in ccys]


def _check_pipeline():
    total = _module("pipeline").pipeline_total(PIPE_ROWS)
    return f"${total / 1e6:.2f}M pipeline value", total == 1840000, ""


INCIDENT_KEYS = ("contacts", "intake", "orders", "reports", "theme", "pipeline")

CHECKS = [
    {
        "key": "contacts", "module": "commandcenter/contacts.py",
        "surface": "Revenue · New CRM contact", "category": "Broken validation logic",
        "title": "Saving an international contact",
        "what": "The form validates the contact before it reaches the CRM.",
        "input": "Siobhán O'Brien · +44 20 7946 0958",
        "expected": "accepted — contact saved",
        "run": _check_contacts,
    },
    {
        "key": "intake", "module": "commandcenter/intake.py",
        "surface": "Support · Add note to ticket", "category": "Uncaught null / exception handling",
        "title": "Attaching a note with the note box left empty",
        "what": "POST /api/notes with notes = null, which is what an empty box sends.",
        "input": '{"ticket_id": "TCK-4471", "notes": null}',
        "expected": "201 Created — note stored as ''",
        "run": _check_intake,
    },
    {
        "key": "orders", "module": "commandcenter/orders.py",
        "surface": "Revenue · Order totals", "category": "Incorrect calculation logic",
        "title": "Charging a discounted international order",
        "what": "$100.00 subtotal, 10% promo, 20% VAT — the amount the processor settles.",
        "input": "subtotal $100.00 · 10% promo · 20% VAT",
        "expected": "$108.00 charged",
        "run": _check_orders,
    },
    {
        "key": "reports", "module": "commandcenter/reports.py",
        "surface": "Revenue · Daily revenue (UTC)", "category": "Timezone / localization mismatch",
        "title": "Filing a late-night order on the right business day",
        "what": "Three orders either side of midnight UTC, bucketed into UTC business days.",
        "input": "23:40 on the 11th · 02:10 and 15:00 on the 12th",
        "expected": "2026-08-11 $120 · 2026-08-12 $780",
        "run": _check_reports,
    },
    {
        "key": "theme", "module": "commandcenter/theme.py",
        "surface": "Every surface · status colours", "category": "Visual / CSS regression",
        "title": "The colour a healthy status renders in",
        "what": "Every green on this dashboard is painted with whatever this function returns.",
        "input": "theme.status_ok_color()",
        "expected": "healthy renders #3fb950",
        "run": _check_theme,
    },
    {
        "key": "pipeline", "module": "commandcenter/pipeline.py",
        "surface": "Revenue · Pipeline value", "category": "Incorrect rollup logic",
        "title": "Rolling up open deals that settle in two currencies",
        "what": "Six open deals, three of them settling in two currencies — so nine rows, six deals.",
        "input": "3 multi-currency deals among 6 open deals",
        "expected": "$1.84M pipeline value",
        "run": _check_pipeline,
    },
]


# ── who is working on it ──────────────────────────────────────────────────────────────────────────────────
# A card that only says PASS/FAIL leaves out the half of the story the demo is about. These read the
# platform's own ticket queue so each card can also say: nobody has reported this yet / it is reported and
# AgentForge is working on it / there is a pull request waiting for a human / it is resolved.
#
# Read-only and best-effort in every direction: no platform, no token, a slow API — the card falls back to
# its PASS/FAIL, which is the part that must never depend on anything.
PLATFORM_URL = os.getenv("PLATFORM_URL", "http://localhost:8000").rstrip("/")
PLATFORM_USER = os.getenv("DEMO_USER", "developer")
PLATFORM_PASS = os.getenv("DEMO_PASS", "developer")
REPO_PREFIX = "samples/commandcenter-"

_TICKETS: dict = {}          # slug -> the newest ticket for that incident's repo
_TICKETS_AT = 0.0
_TICKET_TTL = 3.0            # the dashboard polls every 2s; do not hammer the API on every poll
_TOKEN = {"v": ""}
_LOCK = threading.Lock()

# ticket status → what a person watching should understand from it
_STAGE = {
    "new":            ("reported", "Reported — queued for triage"),
    "queued":         ("reported", "Reported — queued for triage"),
    "triaging":       ("working", "AgentForge is triaging the report"),
    "reproducing":    ("working", "AgentForge is reproducing it"),
    "fixing":         ("working", "AgentForge is writing the fix"),
    "testing":        ("working", "AgentForge is running the tests"),
    "in_review":      ("working", "Under review"),
    "pr_open":        ("review", "Pull request open — waiting for a human to approve"),
    "awaiting_approval": ("review", "Waiting for a human to approve"),
    "resolved":       ("resolved", "Fixed and deployed"),
    "needs_triage":   ("escalated", "Escalated to a human"),
    "failed":         ("escalated", "AgentForge could not complete this"),
}


def _api(path: str, body: dict = None, token: str = ""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(PLATFORM_URL + path, data=data,
                                 method="POST" if data is not None else "GET")
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=4) as r:      # noqa: S310 — a fixed localhost demo URL
        return json.loads(r.read() or "{}")


def _token() -> str:
    if not _TOKEN["v"]:
        _TOKEN["v"] = _api("/login", {"user": PLATFORM_USER, "password": PLATFORM_PASS}).get("token", "")
    return _TOKEN["v"]


def _refresh_tickets() -> dict:
    """slug → {status, stage, stage_label, id, pr_url, title}. Cached for _TICKET_TTL seconds."""
    global _TICKETS_AT
    now = time.time()
    with _LOCK:
        if now - _TICKETS_AT < _TICKET_TTL:
            return _TICKETS
        _TICKETS_AT = now
    try:
        rows = _api("/tickets", token=_token())
        rows = rows if isinstance(rows, list) else rows.get("tickets", [])
    except Exception:  # noqa: BLE001
        _TOKEN["v"] = ""                       # a stale token is the likeliest cause; re-login next time
        return _TICKETS
    found: dict = {}
    for t in rows:                             # /tickets is newest-first, so the first match per repo wins
        repo = (t.get("repo") or "")
        if not repo.startswith(REPO_PREFIX):
            continue
        slug = repo[len(REPO_PREFIX):]
        if slug in found or slug not in INCIDENT_KEYS:
            continue
        status = t.get("status", "")
        stage, label = _STAGE.get(status, ("working", f"In progress ({status})"))
        found[slug] = {"id": t.get("id", ""), "status": status, "stage": stage, "stage_label": label,
                       "pr_url": t.get("pr_url", "") or ""}
    with _LOCK:
        _TICKETS.clear()
        _TICKETS.update(found)
    return _TICKETS


def run_all() -> list:
    """Every check, evaluated now against the code currently on disk, plus who is working on it."""
    tickets = _refresh_tickets()
    out = []
    for c in CHECKS:
        row = {k: v for k, v in c.items() if k != "run"}
        try:
            actual, ok, error = c["run"]()
        except Exception as e:  # noqa: BLE001 — a broken check must not take the dashboard down
            actual, ok, error = f"check could not run: {type(e).__name__}: {e}", False, str(e)
        row.update({"actual": actual, "ok": bool(ok), "error": error})
        t = tickets.get(c["key"])
        # A ticket describes the CURRENT failure only while it is still open, or once the check passes. A
        # FAILING check whose newest ticket is already resolved or escalated belongs to a previous cycle —
        # the incident was armed again after it — and showing "Fixed and deployed" next to a red card is
        # worse than showing nothing, because it is the exact claim the demo is asking to be believed.
        if t and not ok and t.get("stage") in ("resolved", "escalated"):
            t = None
        if t:
            row["ticket"] = t
        elif not ok:
            row["ticket"] = {"stage": "unreported", "stage_label": "Not reported yet", "id": "", "pr_url": ""}
        out.append(row)
    return out


# ── the two interactive surfaces (the audience types into these) ──────────────────────────────────────────
def try_contact(name: str, phone: str) -> dict:
    errors = _module("contacts").validate_contact({"name": name, "phone": phone})
    return {"saved": not errors, "errors": errors,
            "message": ("Contact saved to the CRM." if not errors
                        else "Not saved — invalid " + " and ".join(errors) + ".")}


def try_note(payload: dict) -> dict:
    """Call the notes handler exactly as the HTTP layer would, and report what the caller would receive."""
    try:
        status, body = _module("intake").handle_note(payload)
        return {"status": int(status), "body": body, "crashed": False}
    except Exception:  # noqa: BLE001 — this is the 500 the reporter is complaining about
        line = traceback.format_exc().strip().splitlines()[-1]
        return {"status": 500, "crashed": True, "exception": line,
                "body": {"error": "Internal Server Error"}}


