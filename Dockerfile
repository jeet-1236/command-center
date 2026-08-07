# Company Command Center — the "buggy website that heals on approval".
# A tiny Flask app (sidecar.py) that serves the dashboard and exposes /state + /admin/scenario|reset + /metrics.
# The app reads $PORT (Railway injects it) and binds 0.0.0.0 — no EXPOSE needed; the PaaS routes to $PORT.
FROM python:3.11-slim

WORKDIR /app
RUN pip install --no-cache-dir flask requests

COPY sidecar.py .
COPY dashboard/ ./dashboard/

CMD ["python", "sidecar.py"]
