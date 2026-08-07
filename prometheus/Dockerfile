# Prometheus for the AgentForge demo — scrapes the Command Center's /metrics so the platform's
# `prometheus` observability backend can confirm faults from live telemetry.
# The base image already reads /etc/prometheus/prometheus.yml by default; we just bake ours in.
FROM prom/prometheus:latest
COPY prometheus.yml /etc/prometheus/prometheus.yml
EXPOSE 9090
