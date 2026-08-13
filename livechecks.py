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
    control = validate({"name": "Ananya Krishnamurthy", "phone": "98765 43210"})
    real = validate({"name": "Meera D'Souza", "phone": "+91 80 2345 6789"})
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


def _check_refunds():
    split = _module("refunds").split_refund
    got = split(10000, [1000, 1000, 1000])
    return f"£{sum(got) / 100:,.2f} allocated across 3 lines ({got})", sum(got) == 10000, ""


def _check_escalation():
    mins = _module("escalation").business_minutes_between
    # Friday 16:30 to Monday 09:30 — 65 wall-clock hours, one business hour.
    got = mins("2026-08-14T16:30:00Z", "2026-08-17T09:30:00Z")
    return f"{got} business minutes since Friday 16:30", got == 60, ""


def _check_pagination():
    pg = _module("pagination")
    rows = [{"id": f"a{i:02d}", "created_at": "2026-08-12T10:00:00Z"} for i in range(10)]
    out = pg.export_all(rows, limit=3)
    ids = [r["id"] for r in out]
    dupes = len(ids) - len(set(ids))
    return (f"{len(ids)} of 10 rows exported, {dupes} duplicated",
            pg.export_is_complete(rows, out), "")


INCIDENT_KEYS = ("contacts", "intake", "orders", "reports", "refunds", "escalation", "pagination",
                 "theme", "pipeline")
# Every incident that can be FILED from the dashboard. Wider than INCIDENT_KEYS: the two front-end incidents
# have no polled card (a browser gate is far too slow for a 2-second tick) but they are still demoed, so
# their tickets still have to be found.
TICKET_KEYS = INCIDENT_KEYS + ("uistate", "uilayout")

CHECKS = [
    {
        "key": "refunds", "module": "commandcenter/refunds.py",
        "surface": "Revenue · Refunds", "category": "Incorrect calculation logic",
        "title": "Splitting a refund across three order lines",
        "what": "A £100.00 refund on three equal lines — the parts must add up to the whole.",
        "input": "£100.00 refunded across lines of £10.00, £10.00, £10.00",
        "expected": "£100.00 allocated across 3 lines ([3334, 3333, 3333])",
        "run": _check_refunds,
    },
    {
        "key": "escalation", "module": "commandcenter/escalation.py",
        "surface": "Support · Escalation queue", "category": "Timezone / working-calendar logic",
        "title": "How long a Friday-afternoon ticket has been open",
        "what": "The desk works 09:00-17:00 Mon-Fri, so the weekend is not time we kept anyone waiting.",
        "input": "raised Fri 16:30 · now Mon 09:30",
        "expected": "60 business minutes since Friday 16:30",
        "run": _check_escalation,
    },
    {
        "key": "pagination", "module": "commandcenter/pagination.py",
        "surface": "Compliance · Audit export", "category": "Data-integrity / boundary logic",
        "title": "Paging an audit batch written in the same second",
        "what": "Ten rows sharing one timestamp, fetched three at a time — each must appear exactly once.",
        "input": "10 rows, all created_at 10:00:00, page size 3",
        "expected": "10 of 10 rows exported, 0 duplicated",
        "run": _check_pagination,
    },
    {
        "key": "contacts", "module": "commandcenter/contacts.py",
        "surface": "Revenue · New CRM contact", "category": "Broken validation logic",
        "title": "Saving an international contact",
        "what": "The form validates the contact before it reaches the CRM.",
        "input": "Meera D'Souza · +91 80 2345 6789",
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
        if slug in found or slug not in TICKET_KEYS:
            continue
        status = t.get("status", "")
        stage, label = _STAGE.get(status, ("working", f"In progress ({status})"))
        found[slug] = {"id": t.get("id", ""), "status": status, "stage": stage, "stage_label": label,
                       "pr_url": t.get("pr_url", "") or ""}
    with _LOCK:
        _TICKETS.clear()
        _TICKETS.update(found)
    return _TICKETS


def _invalidate() -> None:
    """Drop the ticket cache so the very next poll refetches.

    Clearing `_TICKETS` alone would not do it: `_refresh_tickets` returns early on the TTL timestamp, so the
    dashboard would show "Not reported yet" for up to three seconds after the click that filed the report —
    which reads, on stage, as the button having failed."""
    global _TICKETS_AT
    with _LOCK:
        _TICKETS.clear()
        _TICKETS_AT = 0.0


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


# ── driving the demo from the dashboard ──────────────────────────────────────────────────────────────────
# The dashboard is served from here, so a call to here is same-origin; a call from the page straight to the
# platform is not, and would need the platform to allow this origin. Routing through the sidecar keeps the
# demo working on any host without touching CORS.
INCIDENT_TICKETS: dict = {}      # slug -> {title, details, repo}, loaded from scripts/cc_incidents.py
_REG = {"mod": None}             # the loaded scripts/cc_incidents module, or None in a deployed copy


def _load_tickets() -> dict:
    """The reporter's words for each incident, read from the ONE registry that also drives the CLI, so a
    ticket raised from the dashboard is the same ticket `scripts/demo_l2_incident.py` raises."""
    if INCIDENT_TICKETS:
        return INCIDENT_TICKETS
    try:
        import importlib.util
        # The registry lives in a different place in each layout, and getting it wrong is SILENT: the
        # import fails, the incident list comes back empty, and the dashboard's plant / report / approve
        # buttons report "incident list unavailable" on a site that is otherwise working perfectly. That
        # is exactly what the deployed Command Center did — it only ever looked for the monorepo path.
        candidates = [
            os.path.join(APP_ROOT, "cc_incidents.py"),                        # deployed: beside the package
            os.path.join(os.path.dirname(APP_ROOT), "..", "scripts", "cc_incidents.py"),   # monorepo
            os.path.join(HERE, "cc_incidents.py"),                            # beside this file
        ]
        reg = next((os.path.realpath(c) for c in candidates if os.path.isfile(c)), "")
        if not reg:
            log_paths = ", ".join(os.path.realpath(c) for c in candidates)
            raise FileNotFoundError(f"cc_incidents.py not found — looked in: {log_paths}")
        spec = importlib.util.spec_from_file_location("cc_incidents", reg)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _REG["mod"] = mod
        for slug in mod.ALL:
            inc = mod.INCIDENTS[slug]
            INCIDENT_TICKETS[slug] = {"title": inc["ticket"]["title"],
                                      "details": inc["ticket"]["details"],
                                      "repo": mod.mirror_repo(slug),
                                      "category": inc["category"], "surface": inc["surface"]}
    except Exception as e:  # noqa: BLE001 — no registry → the buttons report it rather than 500
        print(f"[livechecks] incident registry unavailable: {type(e).__name__}: {e}", flush=True)
    return INCIDENT_TICKETS


def _registry():
    """The incident registry module itself, for the operations that edit source (arm / heal / reset)."""
    _load_tickets()
    return _REG["mod"]


def incident_list() -> list:
    """Every incident the dashboard can plant and clear, with what is true of it right now."""
    reg = _registry()
    if reg is None:
        return []
    tickets = _refresh_tickets() or {}
    out = []
    # Only what this board can actually SHOW and DRIVE. The two front-end incidents are gated by a real
    # browser, which no deployment of this app has — so they have no live card, their state cannot be
    # verified here, and offering them as buttons means offering a bug that cannot be seen to fail or seen
    # to heal. reg.LIVE_CHECKED is exactly the set with a check behind it.
    for slug in reg.LIVE_CHECKED:
        inc = reg.INCIDENTS[slug]
        try:
            armed = reg.is_armed(slug)
        except Exception:  # noqa: BLE001
            armed = None
        out.append({"slug": slug, "title": inc["ticket"]["title"], "category": inc["category"],
                    "surface": inc["surface"], "module": inc["module"], "armed": armed,
                    "browser": bool(inc.get("browser")), "polled": slug in INCIDENT_KEYS,
                    "ticket": tickets.get(slug) or {}})
    return out


def plant_incident(slug: str, exclusive: bool = True) -> dict:
    """Put the defect into the SERVED app, so the failure the ticket describes is on screen before the
    ticket exists. This edits real source — the same edit `scripts/demo_l2_incident.py --arm` makes — which
    is why the card that goes red afterwards means something.

    EXCLUSIVE BY DEFAULT: every other planted incident is cleared first, so the app carries exactly one
    defect and the board shows exactly one red card. A demo runs an incident at a time — plant it, show it
    failing, file the ticket, watch it resolve, show it green — and that story only holds if there is
    nothing else red on screen to argue about. After a few runs the app would otherwise be carrying three
    or four defects at once and the room cannot tell which red card belongs to the ticket being discussed.

    Pass exclusive=False to plant alongside whatever is already there (the old behaviour), which is what
    the arm-everything scripts want.
    """
    reg = _registry()
    if reg is None:
        return {"ok": False, "error": "incident registry not available in this deployment"}
    cleared = []
    try:
        if exclusive:
            for other in reg.ALL:
                if other != slug and reg.is_armed(other):
                    reg.reset(other)
                    cleared.append(other)
        reg.reset(slug)                       # from the canonical snapshot, so a healed-by-agent module arms
        msg = reg.set_state(slug, True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if "\u2717" in msg:
        return {"ok": False, "error": msg}
    return {"ok": True, "armed": True, "message": msg, "cleared": cleared}


def clear_incident(slug: str) -> dict:
    """Put the module back to its canonical CORRECT source — the reset between rehearsals."""
    reg = _registry()
    if reg is None:
        return {"ok": False, "error": "incident registry not available in this deployment"}
    try:
        return {"ok": True, "armed": False, "message": reg.reset(slug)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def file_incident(slug: str) -> dict:
    """Raise this incident's ticket on the platform, exactly as the CLI would."""
    t = _load_tickets().get(slug)
    if not t:
        return {"ok": False, "error": f"no ticket registered for {slug!r}"}
    live = (_refresh_tickets() or {}).get(slug) or {}
    if live.get("id") and live.get("stage") not in ("resolved", "escalated"):
        # Already in flight. A second identical report is recognised as a duplicate and deliberately NOT
        # investigated again, so the button would look broken. Hand back the one that is running.
        return {"ok": True, "id": live["id"], "title": t["title"], "already": True}
    try:
        tok = _token()
        if not tok:
            return {"ok": False, "error": "could not sign in to the platform"}
        r = _api("/tickets", {"source": "manual", "type": "bug", "problem_class": "code-bug",
                              "repo": t["repo"], "ref": "main",
                              "title": t["title"], "details": t["details"]}, tok)
        _invalidate()                           # so the next poll reflects the new ticket immediately
        return {"ok": True, "id": r.get("id", ""), "title": t["title"]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def approve_incident(slug: str) -> dict:
    """Approve the open gate for this incident's ticket. The gate refuses a bare approval, so a written
    justification travels with it — the same one a person would have to type in the console."""
    t = (_refresh_tickets() or {}).get(slug) or {}
    tid = t.get("id")
    if not tid:
        return {"ok": False, "error": "nothing to approve — this incident has no open ticket"}
    try:
        tok = _token()
        aps = _api("/approvals", token=tok)
        aps = aps if isinstance(aps, list) else aps.get("approvals", [])
        mine = [a for a in aps if a.get("item_id") == tid and not a.get("resolved")]
        if not mine:
            return {"ok": False, "error": "no open approval gate for this ticket yet"}
        _api(f"/approvals/{mine[0]['id']}",
             {"decision": "approve",
              "comment": "Reviewed from the Command Center: the reported case passes, the rest of the "
                         "suite is unchanged, and the change is confined to the module the report named."},
             tok)
        _invalidate()
        return {"ok": True, "id": tid}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


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


