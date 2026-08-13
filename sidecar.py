"""samples/commandcenter/sidecar/sidecar.py — the Company Command Center demo sidecar.

Same pattern as the ShopFront sidecar, adapted to the Command Center's five business surfaces:
  1. Holds the demo FAULT STATE (one flag per flagship incident; default OFF = clean slate).
  2. Derives the LIVE TELEMETRY the agents read — services + `cc_*` metrics (incl. the DERIVED freshness gauge
     `cc_crm_sync_lag_seconds`, so the numeric-only KEDB predicate parser can evaluate it) — written atomically
     to OBS_FILE (the agents' file backend) and exposed at /metrics (Prometheus).
  3. Serves the Command Center DASHBOARD (the "app being managed") which polls /state and HEALS on screen.
  4. Bridges an alert webhook to the platform's /ingest so a fired alert auto-files a ticket.

Metric naming/schema is deliberate: `{services:{name:{ok,error_rate,p95_ms}}, metrics:{name:value}, errors:[…]}`
— byte-for-byte the shape agents/observability.py reads.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import sys
import threading

import requests
from flask import Flask, Response, jsonify, request

# `python sidecar/sidecar.py` puts this directory on sys.path, but a WSGI/PaaS entrypoint may not — be
# explicit so the live checks import the same way under every runner.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import livechecks          # noqa: E402 — the LIVE app checks (they RUN commandcenter/*.py, not a fault flag)

app = Flask(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
# Two layouts run this file: the monorepo's `samples/commandcenter/sidecar/sidecar.py`, where the dashboard
# is a sibling of this directory, and the deployed Command Center repository, where `sidecar.py` sits at the
# root beside `dashboard/`. Pick whichever exists rather than keeping two copies of the file in step by hand.
DASHBOARD = next((p for p in (os.path.join(HERE, "..", "dashboard", "index.html"),
                              os.path.join(HERE, "dashboard", "index.html"))
                  if os.path.isfile(p)), os.path.join(HERE, "..", "dashboard", "index.html"))
OBS_FILE = os.getenv("COMMANDCENTER_OBS_FILE", "/tmp/commandcenter-obs.json")
STATE_FILE = os.path.join(HERE, "state.json")
PLATFORM_INGEST_URL = os.getenv("PLATFORM_INGEST_URL", "http://localhost:8000/ingest/grafana")

# ── demo faults — one per flagship incident. Default OFF: the CC opens on a CLEAN SLATE (all surfaces green). ──
_DEFAULT_STATE = {
    "crm_sync_stalled":       False,   # UC1 · CRM sync worker wedged → data STALE while the monitor stays green
    "crm_vendor_down":        False,   # UC3 · the CRM vendor's API is 5xx-ing (their side)
    "crm_credential_expired": False,   # UC3 · OUR stored CRM credential expired (our side → rotate)
    "monitor_alert_storm":    False,   # UC2 · many synthetic monitor alerts + ONE real outage
    "crm_delivery_stuck":     False,   # UC6 · ONE account's outbound delivery dead-lettered (others fine)
    "account_mismatch":       False,   # UC1 · ONE account's stored balance is wrong while the aggregate is fine
    # ── L3 CODE DEFECTS — real bugs in the commandcenter/*.py backend (not an armed runtime fault; the defect
    #    lives in the repo). Armed here so the dashboard SHOWS the wrong number the bug produces + an engineering
    #    incident; AgentForge's L3 lane reproduces → fixes → PR → (deploy) heals it. Toggle for the demo. ──
    "code_pipeline":          False,   # pipeline.py: multi-currency deals double-counted → pipeline value overstated
    "code_accent":            False,   # theme.py: the healthy status colour returns the danger red → a healthy system reads as critical
}

# UC6 · the one account whose outbound sync delivery is dead-lettered while everyone else's flows. Naming the
# specific item is the whole point — we retry JUST this one, not the whole batch.
STUCK_DELIVERY_ITEM = "ACME-4021"

# UC1 · a small book of customer accounts. Normally every STORED balance equals the AUTHORITATIVE (system-of-
# record) value. Arming `account_mismatch` makes ONE account's stored balance wrong (understated) while the
# portfolio AGGREGATE stays within tolerance and every service reads green — the healthy-status trap at the
# RECORD level: overall looks fine, one customer's number is off. Reconciling looks up THAT one record and
# corrects it to the authoritative value, not a batch re-run.
ACCOUNTS = [
    {"id": "ACME-1001", "name": "Acme Robotics",     "expected": 4820000},
    {"id": "ACC-4471",  "name": "Northwind Traders", "expected": 10000000},
    {"id": "ACC-3300",  "name": "Globex Capital",    "expected": 2650000},
    {"id": "ACC-5582",  "name": "Initech Holdings",  "expected": 780000},
    {"id": "ACC-6120",  "name": "Umbrella Group",    "expected": 5310000},
    {"id": "ACC-7788",  "name": "Wayne Enterprises", "expected": 9040000},
]
MISMATCH_ACCOUNT = "ACC-4471"     # the one customer whose number is wrong
MISMATCH_DELTA = -150000          # its stored balance is understated by 150k when the fault is armed


def account_rows(s: dict) -> list:
    """Per-account STORED-vs-AUTHORITATIVE records. Stored == expected everywhere EXCEPT the one mismatched
    account when the fault is armed (its stored balance is understated). This is the book AgentForge looks up a
    single record in and diffs — not a service health signal."""
    off = bool((s or {}).get("account_mismatch"))
    rows = []
    for a in ACCOUNTS:
        stored = a["expected"] + MISMATCH_DELTA if (off and a["id"] == MISMATCH_ACCOUNT) else a["expected"]
        rows.append({"id": a["id"], "name": a["name"], "stored": stored,
                     "expected": a["expected"], "matches": stored == a["expected"]})
    return rows


def account_mismatch_count(s: dict) -> int:
    return sum(0 if r["matches"] else 1 for r in account_rows(s))


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return {**_DEFAULT_STATE, **(json.load(f) or {})}
        except Exception:  # noqa: BLE001
            pass
    return dict(_DEFAULT_STATE)


state = load_state()

_OBS_LOCK = threading.Lock()


def save_state(s: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f)
    except Exception:  # noqa: BLE001
        pass
    write_obs_snapshot(s)


def _svc(ok, error_rate=0.0, p95_ms=200.0) -> dict:
    return {"ok": bool(ok), "error_rate": float(error_rate), "p95_ms": float(p95_ms)}


def build_snapshot(s: dict) -> dict:
    """Live Command-Center telemetry derived from the armed fault flags. Shared by the file snapshot AND the
    Prometheus /metrics endpoint so both always agree."""
    s = s or {}
    stalled = bool(s.get("crm_sync_stalled"))
    vendor_down = bool(s.get("crm_vendor_down"))
    cred_exp = bool(s.get("crm_credential_expired"))
    storm = bool(s.get("monitor_alert_storm"))
    sync_failing = vendor_down or cred_exp     # the CRM sync connector is erroring (either owner)
    # L3 code defects — the wrong number each planted bug produces on the dashboard when armed.
    code_pipeline = bool(s.get("code_pipeline"))
    return {
        "_comment": "Live Company Command Center telemetry — written by the sidecar from the armed fault flags.",
        "services": {
            # CRM sync connector errors when the vendor is down OR our credential expired; a STALE-but-not-erroring
            # sync (UC1) leaves it 'ok' — the tell is freshness, not an error rate (the healthy-status trap).
            "crm-sync-connector": _svc(not sync_failing, 0.08 if sync_failing else 0.0, 900 if sync_failing else 180),
            "twenty-crm-vendor":  _svc(not vendor_down, 0.11 if vendor_down else 0.0, 1200 if vendor_down else 210),
            "payments-api":       _svc(not storm, 0.14 if storm else 0.0, 2200 if storm else 190),  # the ONE real down in the storm
            "uptime-monitors":    _svc(True, 0.0, 40),                    # the monitors themselves are fine
            "support-desk":       _svc(True, 0.0, 120),                   # Freshdesk healthy
            "billing-service":    _svc(True, 0.0, 160),
        },
        "metrics": {
            # UC1 · freshness as a DERIVED numeric gauge (not a timestamp) so the predicate parser can read it.
            "cc_crm_sync_lag_seconds":        2700 if stalled else 45,
            "cc_crm_monitor_up":              1,          # the healthy-status trap: monitor stays green even when stale
            # UC3 · owner split from the exact signal.
            "cc_crm_vendor_up":               0 if vendor_down else 1,
            "cc_crm_credential_valid":        0 if cred_exp else 1,
            # KEDB flag guardrail (KE-CC-3 flags="crm-vendor-up:on"): the credential-rotation record is filtered
            # out unless this reads 1, so an our-side rotation can NEVER fire during a vendor outage. Emitted as
            # a `flag_<name>` gauge because an UNREADABLE flag doesn't disambiguate — it must be present.
            "flag_crm_vendor_up":             0 if vendor_down else 1,
            "cc_crm_sync_error_rate":         0.08 if sync_failing else 0.0,
            # UC2 · the real outage count vs. the synthetic-alert volume.
            "cc_monitor_real_down_count":     1 if storm else 0,
            "cc_monitor_synthetic_alert_count": 18 if storm else 1,
            # UC6 · the dead-letter depth. ONE account's delivery is stuck while thousands flow — the aggregate
            # (>0.5 → the flip reads GREEN at 0) is how a single stuck item is caught without a per-record scan.
            "cc_dead_letter_count":           1 if s.get("crm_delivery_stuck") else 0,
            # UC1 · per-account data-integrity gauge: how many customer records disagree with the system of
            # record. ONE off account reads 1 here while every service is green and the portfolio aggregate
            # (cc_pipeline_usd) is unchanged — the record-level healthy-status trap.
            "cc_account_mismatch_count":      account_mismatch_count(s),
            # panel summaries (mocked business surfaces). Some carry an L3 CODE-DEFECT skew when armed: the wrong
            # value the planted bug produces, which AgentForge's L3 fix corrects.
            "cc_deals_open":                  37,
            # cc-code-1 (pipeline.py double-count): a $200k multi-currency deal is counted twice → overstated.
            "cc_pipeline_usd":                2040000 if code_pipeline else 1840000,
            "cc_pipeline_usd_correct":        1840000,
            "cc_open_tickets":                12,
            "cc_sla_at_risk_count":           2,
            "cc_cloud_spend_usd":             48200,
            "cc_cloud_budget_usd":            60000,
            "cc_cloud_projection_usd":        54000,
            "cc_access_creds_expired":        1 if cred_exp else 0,
        },
        "errors": _errors(s),
    }


import datetime as _dt


def _err(service: str, message: str, count: int, entity: str = "") -> dict:
    """One error record with a real TIMESTAMP and a trace-to-SOURCE deep-link, so an agent that cites this log
    can show WHEN it happened and click through to WHERE it came from (the observability backend). `entity`
    (an account/order id) powers the 'reporter's own log line' correlation boost when present."""
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rec = {"service": service, "message": message, "count": count, "ts": now,
           "source_url": f"https://grafana.internal/explore?service={service}&from=now-1h"}
    if entity:
        rec["entity"] = entity
    return rec


def _errors(s: dict) -> list:
    out = []
    if s.get("crm_sync_stalled"):
        out.append(_err("crm-sync-connector", "sync worker heartbeat stale — last commit 45m ago", 1))
    if s.get("crm_vendor_down"):
        out.append(_err("twenty-crm-vendor", "GET /rest/opportunities -> 503 Service Unavailable (vendor)", 9))
    if s.get("crm_credential_expired"):
        out.append(_err("crm-sync-connector", "POST /rest/sync -> 401 Unauthorized: api key expired", 6))
    if s.get("monitor_alert_storm"):
        out.append(_err("payments-api", "monitor 'payments-api' DOWN (real) amid 17 synthetic check alerts", 18))
    if s.get("crm_delivery_stuck"):
        out.append(_err("crm-sync-connector",
                        f"outbound sync delivery for account {STUCK_DELIVERY_ITEM} dead-lettered (downstream 409) "
                        f"— 1 item stuck; all other accounts delivering normally", 1, entity=STUCK_DELIVERY_ITEM))
    if s.get("account_mismatch"):
        out.append(_err("crm-sync-connector",
                        f"data integrity: account {MISMATCH_ACCOUNT} stored balance disagrees with the system of "
                        f"record (off by {MISMATCH_DELTA:,}) — portfolio aggregate within tolerance, all other "
                        f"accounts reconcile", 1, entity=MISMATCH_ACCOUNT))
    # L3 code-defect symptoms — named to the source file so the investigation points at the module to fix.
    if s.get("code_pipeline"):
        out.append(_err("commandcenter/pipeline.py", "Revenue rollup overstates pipeline value — multi-currency deals counted once per currency (code defect cc-code-1)", 1))
    return out


def write_obs_snapshot(s: dict) -> None:
    """Reflect the fault flags into the snapshot AgentForge's L1/L2 agents read (file backend). Atomic; never raises."""
    snapshot = build_snapshot(s)
    try:
        with _OBS_LOCK:
            tmp = f"{OBS_FILE}.tmp.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f)
            os.replace(tmp, OBS_FILE)
    except Exception:  # noqa: BLE001
        pass


# An L2 runbook action → the fault flag it clears (the "heal on approve" beat). Accepts both the CC-specific
# names and the generic action_class the executor may send.
_ACTIONS = {
    "requeue_crm_sync":       "crm_sync_stalled",
    "job_requeue":            "crm_sync_stalled",
    "rotate_crm_credential":  "crm_credential_expired",
    "secret_rotation":        "crm_credential_expired",
    "replay_delivery":        "crm_delivery_stuck",
    "delivery_replay":        "crm_delivery_stuck",
    "reconcile_account":      "account_mismatch",
    "account_reconcile":      "account_mismatch",
    "data_reconcile":         "account_mismatch",
}


# ── routes ───────────────────────────────────────────────────────────────────
@app.get("/")
def dashboard():
    try:
        with open(DASHBOARD, encoding="utf-8") as f:
            return Response(f.read(), mimetype="text/html")
    except FileNotFoundError:
        return Response("<h1>Command Center dashboard not found</h1>", mimetype="text/html", status=404)


@app.get("/health")
def health():
    return jsonify({"ok": True, "armed": sum(1 for v in state.values() if v)})


@app.get("/state")
def get_state():
    return jsonify(state)


@app.get("/accounts")
def accounts():
    """UC1 · the per-account book (stored vs authoritative). AgentForge reads ONE record from here by the
    account ID it pulled from the ticket, and diffs its stored value against the authoritative one."""
    return jsonify({"accounts": account_rows(state)})


@app.get("/accounts/<account_id>")
def account_one(account_id):
    for r in account_rows(state):
        if r["id"].lower() == account_id.lower():
            return jsonify(r)
    return jsonify({"error": f"unknown account {account_id}"}), 404


@app.get("/webui/")
@app.get("/webui/<path:asset>")
def webui(asset: str = "index.html"):
    """The Support surface's own web UI — the page the two FRONT-END incidents live in.

    Served from here so the defect can be looked at (and resized) in a browser during the demo, and so the
    dashboard can reach it same-origin. Confined to the webui directory: the path is resolved and checked to
    be inside it, so a traversal cannot read the rest of the checkout."""
    root = os.path.realpath(os.path.join(livechecks.APP_ROOT, "webui"))
    full = os.path.realpath(os.path.join(root, asset))
    if not (full == root or full.startswith(root + os.sep)) or not os.path.isfile(full):
        return Response("not found", status=404, mimetype="text/plain")
    kind = ("text/html" if full.endswith(".html") else
            "text/css" if full.endswith(".css") else
            "application/javascript" if full.endswith(".js") else "text/plain")
    with open(full, encoding="utf-8") as fh:
        return Response(fh.read(), mimetype=kind)


@app.get("/api/checks")
def api_checks():
    """The LIVE application checks behind the "Live app checks" tab — each one RUNS a real function in
    `commandcenter/` and reports what came back. Unlike every other panel here, nothing about this endpoint
    is derived from a fault flag: it is red because the served code is wrong and green because it is right,
    which is what makes the post-approval heal something the audience can verify rather than take on
    trust. See sidecar/livechecks.py."""
    rows = livechecks.run_all()
    # The dashboard paints its "healthy" green with whatever commandcenter/theme.py returns, so the visual
    # defect is visible as the dashboard itself turning red rather than as a card that says it did. Same
    # principle as every check here: the app's own code decides, not a flag.
    try:
        ok_color = livechecks._module("theme").status_ok_color()
    except Exception:  # noqa: BLE001
        ok_color = "#3fb950"
    return jsonify({"checks": rows, "failing": sum(0 if r["ok"] else 1 for r in rows),
                    "ok_color": ok_color})


@app.post("/api/contacts")
def api_contacts():
    """The "New CRM contact" form posts here. Runs the app's real validation rule."""
    body = request.get_json(silent=True) or {}
    return jsonify(livechecks.try_contact(str(body.get("name", "")), str(body.get("phone", ""))))


@app.post("/api/payments")
def api_payments():
    """The "Record a payment" form posts here. Returns whatever the handler answered — including the 500
    it is currently answering with, which is the whole point of the incident."""
    body = request.get_json(silent=True) or {}
    payload = {"invoice_id": body.get("invoice_id")}
    if "amount" in body:
        payload["amount"] = body["amount"]      # may be a NUMBER — that IS the reported case
    res = livechecks.try_payment(payload)
    return jsonify(res), res["status"]


@app.post("/api/notes")
def api_notes():
    """The "Add note to ticket" form posts here. Returns whatever the handler answered — including the 500
    it is currently answering with, which is the whole point of the incident."""
    body = request.get_json(silent=True) or {}
    payload = {"ticket_id": body.get("ticket_id")}
    if "notes" in body:
        payload["notes"] = body["notes"]          # may be an explicit JSON null — that IS the reported case
    res = livechecks.try_note(payload)
    return jsonify(res), res["status"]


# ── driving the L2 code demo from the dashboard ──────────────────────────────────────────────────────────
# "Plant the bug, then watch it get fixed", as four clicks on the page the audience is already looking at,
# instead of a terminal beside it. Every one of these does the SAME thing the CLI driver does — plant edits
# the served module, file raises a real ticket on the platform, approve answers a real gate. Nothing here is
# a rehearsal of the demo; it IS the demo, with the buttons where people can see them.
#
# They live on the sidecar rather than being called from the page directly because the dashboard is served
# from here: same origin, no CORS, and it keeps working when the platform is on another host.

@app.get("/api/incidents")
def api_incidents():
    """Every plantable incident, with whether it is currently planted and where its ticket has got to."""
    return jsonify({"incidents": livechecks.incident_list()})


@app.post("/api/incident/<slug>/plant")
def api_incident_plant(slug):
    """Put the defect into the served app. The matching live check goes red on the next poll.

    ONE AT A TIME: every other planted incident is cleared first, so the board shows exactly one red card
    and it is this one. POST {"exclusive": false} to plant alongside what is already there.
    """
    body = request.get_json(silent=True) or {}
    res = livechecks.plant_incident(slug, exclusive=body.get("exclusive", True) is not False)
    return jsonify(res), (200 if res.get("ok") else 400)


@app.post("/api/incident/<slug>/clear")
def api_incident_clear(slug):
    """Take the defect back out, from the canonical snapshot — the reset between rehearsals."""
    res = livechecks.clear_incident(slug)
    return jsonify(res), (200 if res.get("ok") else 400)


@app.post("/api/incident/<slug>/file")
def api_incident_file(slug):
    """Raise the incident's ticket on the platform, in the reporter's words. AgentForge picks it up from
    there — the page then just watches the ticket's stage."""
    res = livechecks.file_incident(slug)
    return jsonify(res), (200 if res.get("ok") else 400)


@app.post("/api/incident/<slug>/approve")
def api_incident_approve(slug):
    """Answer the open approval gate. The gate refuses a bare approval, so a written justification goes with
    it — after which the reviewed patch deploys and the red card turns green on its own."""
    res = livechecks.approve_incident(slug)
    return jsonify(res), (200 if res.get("ok") else 400)


@app.get("/metrics")
def metrics():
    snap = build_snapshot(state)
    lines = []
    for name, val in snap["metrics"].items():
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {float(val)}")
    for svc, h in snap["services"].items():
        lines.append(f'cc_service_up{{service="{svc}"}} {1 if h["ok"] else 0}')
    return Response("\n".join(lines) + "\n", mimetype="text/plain")


@app.post("/admin/scenario")
def admin_scenario():
    body = request.get_json(silent=True) or {}
    fault, on = body.get("fault"), bool(body.get("on", True))
    if fault not in _DEFAULT_STATE:
        return jsonify({"ok": False, "error": f"Unknown fault: {fault}"}), 400
    state[fault] = on
    save_state(state)
    return jsonify({"ok": True, "fault": fault, "on": on})


@app.post("/admin/action")
def admin_action():
    """The L2 executor calls this on human approval; clears the fault the runbook action targets → the panel heals."""
    body = request.get_json(silent=True) or {}
    action, fixes = body.get("action"), bool(body.get("fix", True))
    account_id = body.get("account_id")     # UC1 · the specific record to reconcile (pulled from the ticket)
    fault = _ACTIONS.get(action)
    if fault is None:
        return jsonify({"ok": False, "error": f"Unknown action: {action}"}), 400
    if fixes:
        state[fault] = False
        save_state(state)
        if fault == "account_mismatch":
            who = account_id or MISMATCH_ACCOUNT
            return jsonify({"ok": True, "message": f"{action}: corrected the stored record for account {who} to "
                                                   f"its authoritative value; that one account now reconciles "
                                                   f"(no other records touched)."})
        return jsonify({"ok": True, "message": f"{action}: {fault} cleared; the surface heals."})
    return jsonify({"ok": True, "message": f"{action} ran (dry-run, {fault} unchanged)."})


@app.post("/admin/deploy")
def admin_deploy():
    """Deploy an approved fix INTO the served app: apply a unified diff to `commandcenter/`.

    Co-located, the platform writes the patch straight onto this tree. Deployed, the Command Center is its
    own service on its own disk, so the platform cannot — and without this the live checks would stay red
    after an approval, or would have to be "healed" by restoring our own copy of the answer, which is not
    the same claim at all. This applies the RUN'S OWN DIFF, so the deployed heal means what the local one
    means: the code the reviewer approved is now the code running here.

    Forward-only (`git apply` never reverse-applies, and the `patch` fallback is `--forward`), so
    re-approving cannot re-break the surface, and confined to `commandcenter/` so a diff for anything else
    is refused rather than written."""
    import subprocess
    body = request.get_json(silent=True) or {}
    patch = body.get("patch") or ""
    if not patch.strip():
        return jsonify({"ok": False, "error": "no patch"}), 400
    # A unified diff is newline-terminated data, not a string to tidy. Stripping it removes the final
    # newline and `git apply` answers "corrupt patch"/"unexpectedly ends in middle of line" — which reads
    # as a bad diff from the fix agent rather than as us having damaged it in transit.
    if not patch.endswith("\n"):
        patch += "\n"
    targets = {ln.split(" b/")[-1].strip() for ln in patch.splitlines() if ln.startswith("diff --git ")}
    stray = sorted(t for t in targets if not t.startswith("commandcenter/"))
    if stray:
        return jsonify({"ok": False, "error": f"refusing a patch that touches {stray}"}), 400
    root = livechecks.APP_ROOT

    def _digests():
        out = {}
        for t in targets:
            try:
                with open(os.path.join(root, t), "rb") as fh:
                    out[t] = hashlib.sha256(fh.read()).hexdigest()
            except OSError:
                out[t] = ""
        return out

    before = _digests()
    try:
        # `patch` FIRST, because its paths are relative to -d and nothing else. `git apply` resolves a
        # patch against the REPOSITORY root, not against -C, so when the app is a subdirectory of a larger
        # checkout — which it is in the monorepo — the diff lands somewhere else entirely and git still
        # exits 0. Forward-only in both, so re-approving cannot reverse a deployed fix.
        r = subprocess.run(["patch", "-p1", "--forward", "--batch", "-d", root],
                           input=patch.encode(), capture_output=True, timeout=15)
        why = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
        if r.returncode != 0:
            g = subprocess.run(["git", "-C", root, "apply", "--recount", "--whitespace=nowarn"],
                               input=patch.encode(), capture_output=True, timeout=15)
            why = (why + " | " + g.stderr.decode("utf-8", "replace").strip())[:300]
    except Exception as e:  # noqa: BLE001 — a deploy must never take the dashboard down
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    # `applied` is decided by whether the FILES CHANGED, not by an exit code. A patch tool that reports
    # success having written nothing here (or having written somewhere else) would otherwise be reported as
    # a deploy, and the dashboard would sit red underneath a message saying it had healed.
    applied = _digests() != before
    for f in glob.glob(os.path.join(root, "commandcenter", "*.rej")) + \
            glob.glob(os.path.join(root, "commandcenter", "*.orig")):
        try:
            os.remove(f)                 # a rejected hunk leaves litter in the served tree; don't keep it
        except OSError:
            pass
    # Re-run the checks now so the answer says what the app does, not just that a file was written.
    rows = livechecks.run_all()
    return jsonify({"ok": True, "applied": applied, "files": sorted(targets),
                    "failing": sum(0 if x["ok"] else 1 for x in rows),
                    "message": ("deployed — the served app now runs the approved fix" if applied
                                else f"patch did not apply (already deployed?) — {why}")})


@app.post("/admin/reset")
def admin_reset():
    for k in _DEFAULT_STATE:
        state[k] = False
    save_state(state)
    return jsonify({"ok": True, "message": "clean slate — every surface healthy"})


@app.post("/ingest/grafana")
def ingest_bridge():
    """Relay a fired alert to the platform /ingest (the Docker→:8000 localhost-forward workaround, same as ShopFront)."""
    hdrs = {"content-type": "application/json"}
    for h in ("x-ingest-secret", "authorization"):
        if request.headers.get(h):
            hdrs[h] = request.headers.get(h)
    try:
        r = requests.post(PLATFORM_INGEST_URL, data=request.get_data(), headers=hdrs, timeout=6)
        return Response(r.content, status=r.status_code, mimetype="application/json")
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 502


if __name__ == "__main__":
    write_obs_snapshot(state)          # seed the obs file on boot so the agents read a clean slate immediately
    # PORT is injected by Railway / most PaaS. Its presence is also the signal that we are BEHIND a router
    # that reaches the container on its external interface, so bind 0.0.0.0 there and stay on loopback
    # locally — which keeps the safer default a dev machine wants without breaking the deployed service.
    # COMMANDCENTER_SIDECAR_HOST overrides either way (both sides of this merge wanted that knob).
    port = int(os.getenv("PORT") or os.getenv("COMMANDCENTER_SIDECAR_PORT") or "8092")
    host = os.getenv("COMMANDCENTER_SIDECAR_HOST") or ("0.0.0.0" if os.getenv("PORT") else "127.0.0.1")
    app.run(host=host, port=port, threaded=True)
