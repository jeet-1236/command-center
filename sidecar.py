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

import json
import os
import threading

import requests
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

HERE = os.path.dirname(__file__)
DASHBOARD = os.path.join(HERE, "dashboard", "index.html")
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
    "code_pipeline":          False,   # cc-code-1 · pipeline.py: multi-currency deals double-counted → pipeline value overstated
    "code_sla":               False,   # cc-code-2 · sla.py: at-risk misses the boundary ticket → a breach slips
    "code_finops":            False,   # cc-code-3 · finops.py: projection off-by-one → false 'over budget' alert
    "code_access":            False,   # cc-code-4 · access.py: leaver not flagged → an access-review miss
    "code_accent":            False,   # cc-code-5 · theme.py: healthy status colour returns the danger red, not the brand green → a healthy system reads as critical
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
    code_sla = bool(s.get("code_sla"))
    code_finops = bool(s.get("code_finops"))
    code_access = bool(s.get("code_access"))
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
            # cc-code-2 (sla.py boundary): a ticket exactly at the warning threshold is missed → under-counted,
            # and one real breach slips the early-warning.
            "cc_sla_at_risk_count":           1 if code_sla else 2,
            "cc_sla_breach_missed":           1 if code_sla else 0,
            "cc_cloud_spend_usd":             48200,
            "cc_cloud_budget_usd":            60000,
            # cc-code-3 (finops.py projection off-by-one): early-month projection over-shoots the budget → false alert.
            "cc_cloud_projection_usd":        78000 if code_finops else 54000,
            "cc_access_creds_expired":        1 if cred_exp else 0,
            # cc-code-4 (access.py and/or): a departed employee who was recently active isn't flagged.
            "cc_access_review_missed":        1 if code_access else 0,
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
    if s.get("code_sla"):
        out.append(_err("commandcenter/sla.py", "SLA 'at risk' missed a ticket exactly at the warning threshold — a breach slipped the early warning (code defect cc-code-2)", 1))
    if s.get("code_finops"):
        out.append(_err("commandcenter/finops.py", "Cloud-spend month-end projection over-shoots early in the month — false 'over budget' alert (code defect cc-code-3)", 1))
    if s.get("code_access"):
        out.append(_err("commandcenter/access.py", "Access review missed a departed employee who was recently active (code defect cc-code-4)", 1))
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
    # PORT is injected by Railway / most PaaS; fall back to the local demo's COMMANDCENTER_SIDECAR_PORT, then 8092.
    port = int(os.getenv("PORT") or os.getenv("COMMANDCENTER_SIDECAR_PORT") or "8092")
    # Bind 0.0.0.0 so a PaaS router (Railway) or a tunnel can reach it; a co-located local run still works via
    # localhost. Set COMMANDCENTER_SIDECAR_HOST=127.0.0.1 to restrict to loopback.
    host = os.getenv("COMMANDCENTER_SIDECAR_HOST", "0.0.0.0")
    app.run(host=host, port=port, threaded=True)
