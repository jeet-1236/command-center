"""commandcenter — the business-logic backend behind the Company Command Center dashboard.

The "system under management" for the AgentForge Command Center demo: the small, readable modules that compute
the numbers on the dashboard (pipeline value, SLA risk, cloud-spend projection, access review). AgentForge's L3
code lane clones this package, reproduces a reported defect against its test suite, patches the one module, and
opens a PR. Each module carries exactly ONE planted, isolated defect with a dedicated reproduction test:

    pipeline.py  (cc-code-1) — Revenue/Deals: multi-currency deals double-counted in the pipeline rollup
    sla.py       (cc-code-2) — Support:       a ticket exactly at the warning threshold isn't flagged at-risk
    finops.py    (cc-code-3) — Cloud Spend:   month-end projection divides by the wrong day count → false alert
    access.py    (cc-code-4) — Access/Security: a departed employee isn't flagged for access review (and vs or)

The operational L1/L2 incidents are diagnosed from the sidecar's runtime telemetry, not from source here.
"""
__version__ = "1.0.0"
