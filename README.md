# Company Command Center (demo "dummy website")

The buggy operations dashboard that **AgentForge** manages — it shows the wrong number/colour when a fault is
armed, and **heals on screen** the moment AgentForge's fix is approved. A single self-contained Flask app.

- **Dashboard** — `GET /` (polls `/state` every 2s and repaints).
- **Fault control** — `POST /admin/scenario {"fault":"<name>","on":true|false}`, `POST /admin/reset`.
- **State** — `GET /state`, `GET /health`, `GET /metrics` (Prometheus text).
- **Alert bridge** — `POST /ingest/grafana` → forwards to `PLATFORM_INGEST_URL`.

Fault flags: `code_pipeline`, `code_sla`, `code_finops`, `code_access`, `code_accent` (code defects) and
`crm_sync_stalled`, `crm_vendor_down`, `crm_credential_expired`, `monitor_alert_storm`, `crm_delivery_stuck`,
`account_mismatch` (operational).

## Run locally
```bash
pip install -r requirements.txt
python sidecar.py            # → http://localhost:8092
```

## Deploy to Railway
The app reads `$PORT` (Railway injects it) and binds `0.0.0.0`, so it works with **zero config**.
1. Push this repo to GitHub.
2. Railway → **New Project → Deploy from GitHub repo** → pick this repo. Railway builds the `Dockerfile`.
3. Railway → the service → **Settings → Networking → Generate Domain** → you get `https://<name>.up.railway.app`.
4. Open it — the dashboard loads. Arm a bug: `curl -X POST https://<name>.up.railway.app/admin/scenario -H content-type:application/json -d '{"fault":"code_pipeline","on":true}'`.

## Env vars (all optional)
| Var | Default | Meaning |
|---|---|---|
| `PORT` | (Railway sets it) | Port to listen on. |
| `COMMANDCENTER_SIDECAR_HOST` | `0.0.0.0` | Bind host. |
| `PLATFORM_INGEST_URL` | `http://localhost:8000/ingest/grafana` | Where `/ingest/grafana` forwards a fired alert (the AgentForge platform). |

## Wiring it to AgentForge
AgentForge (deployed separately) heals this dashboard by POSTing to its `/admin/scenario` on PR approval, so on
the **AgentForge platform** set:
```
SIDECAR_URL=https://<this-service>.up.railway.app
```
Then: arm a bug here → file a ticket in AgentForge → the agents fix it → approve → AgentForge POSTs
`/admin/scenario {on:false}` here → this dashboard heals live.
