# Company Command Center — the "buggy website that heals on approval".
# A tiny Flask app (sidecar.py) that serves the dashboard, exposes /state + /admin/* + /metrics, and RUNS
# the commandcenter package so the Live app checks report what the application actually does.
# The app reads $PORT (Railway injects it) and binds 0.0.0.0 — no EXPOSE needed; the PaaS routes to $PORT.
FROM python:3.11-slim

WORKDIR /app

# `patch` applies the approved fix that /admin/deploy receives; without it an approval cannot heal this
# service, because the platform runs on a different machine and can only send the diff over HTTP.
RUN apt-get update \
 && apt-get install -y --no-install-recommends patch git \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir flask requests

COPY sidecar.py livechecks.py cc_incidents.py ./
COPY commandcenter/ ./commandcenter/
COPY dashboard/ ./dashboard/
# The support web UI — the surface the two FRONT-END incidents live on. Without it those two defects have
# nowhere to be seen on the deployed site, and their fix patches (webui/app.js, webui/styles.css) have no
# file to apply to, so /admin/deploy rejects them and the approval heals nothing.
COPY webui/ ./webui/
# The canonical sources: `reset` restores from these, and planting an incident starts from them. Without
# them a module the agents have already fixed cannot be re-armed and the demo cannot be run twice.
COPY .canonical/ ./.canonical/

CMD ["python", "sidecar.py"]
