"""commandcenter/theme.py — Command Center dashboard brand + status colour tokens.

The dashboard's status colours (healthy / warning / critical) are single brand tokens. Keeping the colour here —
as one function, not scattered as CSS literals — is what lets a "wrong status colour" report be reproduced and
fixed like any other code defect: one place to test, one place to change.
"""
from __future__ import annotations

BRAND_ACCENT = "#3b82f6"      # the Command Center brand blue (the dashboard --accent)
BRAND_OK = "#3fb950"          # the HEALTHY / "all good" green (the dashboard --ok)


def status_ok_color() -> str:
    """The colour of every HEALTHY status signal — the "Operational" pill, the green service-health dots and the
    ok-state KPI stripes. It must be the brand green so a healthy system reads as calm/green.

    KNOWN-ISSUE (cc-code-5): returns the danger RED instead of the healthy green, so every healthy signal reads as
    critical — the whole dashboard looks like it is on fire even though every underlying status is OK. The fix
    returns BRAND_OK so healthy signals show green again.
    """
    return BRAND_OK          # BUG: should be BRAND_OK — healthy signals render red (they read as critical)
