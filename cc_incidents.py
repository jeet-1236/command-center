"""scripts/cc_incidents.py — the registry of the Command Center's L2 CODE incidents.

One entry per demo incident. Everything that has to agree about an incident lives here exactly once: the
module it lives in, the source edit that arms and heals it, the isolated repo AgentForge is pointed at, and
the words the reporter uses when they file it.

Three consumers read this file, which is the reason it exists:

    scripts/make_cc_incident_repos.py   builds one single-defect git repo per incident
    scripts/demo_l2_incident.py         arms the served app, files the ticket, follows the run, heals
    tests/test_cc_l2_incidents.py       proves each arm/heal pair still applies and still flips the tests

ARMING IS A SOURCE EDIT, not a feature flag. `--arm` rewrites the served module to the version that carries
the defect and `--heal` rewrites it back, so the dashboard's live checks fail because the running code is
genuinely wrong — and pass again because the code is genuinely fixed. The heal an audience actually watches
is neither of those: it is AgentForge's own reviewed patch, applied to the served tree on approval
(platform/worker._deploy_patch_to_surface).
"""
from __future__ import annotations

import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _served_root() -> str:
    """The directory that holds the `commandcenter` package.

    Two layouts ship this file. In the monorepo it is scripts/cc_incidents.py and the app is at
    samples/commandcenter. In the DEPLOYED Command Center this module sits at the root beside the package
    it describes, because the whole point of publishing it there is that the dashboard's plant / report /
    approve buttons need a registry — without one the tab can only say "incident list unavailable", which
    is what the deployed site did.

    Assuming either layout breaks the other silently: every path resolves, nothing raises, and the buttons
    just never find a module to edit. So it is detected, and SERVED is overridable for a third layout.
    """
    env = os.getenv("COMMANDCENTER_ROOT", "").strip()
    if env:
        return env
    monorepo = os.path.join(ROOT, "samples", "commandcenter")
    if os.path.isdir(os.path.join(monorepo, "commandcenter")):
        return monorepo
    here = os.path.dirname(os.path.abspath(__file__))     # deployed: beside the package
    if os.path.isdir(os.path.join(here, "commandcenter")):
        return here
    return monorepo


SERVED = _served_root()

# Every module the isolated per-incident repo carries. An incident's own module is swapped to its BUGGY
# variant; every other module is copied at its correct version, so the repo holds exactly one defect and its
# suite is green apart from that one defect's cases.
PACKAGE_MODULES = ("__init__.py", "contacts.py", "intake.py", "orders.py", "reports.py",
                   "pipeline.py", "sla.py", "finops.py", "access.py", "theme.py")


INCIDENTS = {
    # ── 1 · broken validation logic ───────────────────────────────────────────────────────────────────────
    "contacts": {
        "title": "CRM contact form rejects valid international numbers and accented names",
        "module": "commandcenter/contacts.py",
        "category": "Broken validation logic",
        "surface": "Revenue · New CRM contact",
        "symptom": "a rep cannot save a real customer — the form marks a valid phone and a valid name invalid",
        # The validation rule was written for the US launch and never revisited: letters means A–Z, and a
        # phone means the domestic dashed form. Both predicates are swapped, because the reporter hits both.
        "edits": [
            ("""    if not 2 <= len(name) <= 60:
        return False
    return all(c.isalpha() or c in " '-." for c in name)""",
             """    if not 2 <= len(name) <= 60:
        return False
    return all(("a" <= c.lower() <= "z") or c == " " for c in name)"""),
            ("""    body = phone[1:] if phone.startswith("+") else phone
    if any(not (c.isdigit() or c in PHONE_SEPARATORS) for c in body):
        return False
    return 7 <= sum(1 for c in body if c.isdigit()) <= 15""",
             """    digits = phone.replace(" ", "")
    return len(digits) == 10 and digits.isdigit()"""),
        ],
                "story": (
            "Kavya on the Bengaluru desk is adding a customer she has just got off the phone with — Meera D'Souza, landline (022) 2654 0000. The form marks the number invalid. She tries the mobile with the country code, +91 98765 43210, and that is refused too. She drops the country code and types 98765 43210 and it saves."
        ),
        "oracle": (
            "The customer is on the phone on that number, so the number is real. And the same number saves once the country code is removed — so it is the form's rule, not the number."
        ),
        "ticket": {
            "title": "Sales can't save contacts — the CRM form rejects valid Indian numbers and names",
            "details": (
                "The Bengaluru and Mumbai desks cannot add contacts to the CRM. The New CRM contact form "
                "marks the phone number invalid for anything except a plain ten-digit mobile: "
                "+91 98765 43210 is rejected, (022) 2654 0000 is rejected, and 080-2345-6789 is rejected. "
                "Typing the same mobile without the country code — 98765 43210 — saves fine, so the rule "
                "appears to accept only the bare ten digits.\n\n"
                "Names are being rejected too. Meera D'Souza and R. Venkataraman both come back as an "
                "invalid name; Ananya Krishnamurthy saves without trouble. An initial with a full stop is "
                "how a great many of our customers write their name, so this is not an edge case for us.\n\n"
                "The CRM itself holds these contacts fine — there are thousands of +91 numbers in it "
                "already — so this is the form's validation rule, not the CRM. It is in the contact "
                "validation (commandcenter/contacts.py). Both desks are blocked; please accept real "
                "customer names and the number formats above, without letting genuine rubbish through "
                "(a name with digits in it, a five-digit phone number, free text)."
            ),
        },
    },

    # ── 2 · uncaught null → 500 where a 400 was owed ──────────────────────────────────────────────────────
    "intake": {
        "title": "Recording a payment fails when the amount is typed as a plain number",
        "module": "commandcenter/intake.py",
        "category": "Unhandled input type",
        "surface": "Finance · Record a payment",
        "symptom": "a round payment amount crashes the form; the same amount with a comma saves",
        "edits": [('    if value is None or (isinstance(value, str) and not value.strip()):\n        raise ValueError("an amount is required")\n    if isinstance(value, bool):                       # bool is an int in Python; a tick-box is not an amount\n        raise ValueError("that is not an amount")\n    if isinstance(value, int):\n        cents = value * 100\n    elif isinstance(value, float):\n        cents = round(value * 100)\n    elif isinstance(value, str):\n        cleaned = value.strip()\n        for ch in _AMOUNT_NOISE:\n            cleaned = cleaned.replace(ch, "")\n        if not cleaned:\n            raise ValueError("an amount is required")\n        try:\n            cents = round(float(cleaned) * 100)\n        except ValueError:\n            raise ValueError(f"{value!r} is not an amount") from None\n    else:\n        raise ValueError("that is not an amount")\n    if cents < 0:\n        raise ValueError("an amount cannot be negative")\n    return int(cents)', '    cleaned = value.strip().replace(",", "")\n    return int(round(float(cleaned) * 100))')],
        "story": (
            "Priya in Finance is recording yesterday's cleared payments against their invoices. She types "
            "1200 into the amount box for INV-2291, hits save, and the page says something went wrong. She "
            "tries again with 1,200.00 — exactly as it is written on the remittance advice — and it saves "
            "immediately. She has forty payments to record and about half of them are round numbers."
        ),
        "oracle": (
            "The same payment saves when it is typed with a comma and fails when it is not, so it is the "
            "form and not the payment. Nothing reached the ledger either time it failed."
        ),
        "ticket": {
            "title": "Recording a payment fails unless the amount is typed with a comma",
            "details": (
                "Finance cannot record payments with round amounts. Typing 1200 into the amount box and "
                "saving returns a server error; typing 1,200.00 for the same payment saves immediately. "
                "It is the same money either way, so it is the form, not the payment.\n\n"
                "It is happening on roughly half of yesterday's batch, because round amounts are common, "
                "and every failure pages the on-call with a 500. Nothing reaches the ledger when it fails, "
                "so the payment is simply lost until somebody retypes it.\n\n"
                "The amount box should take the amount however it is written — 1200, \"1200\", "
                "\"1,200.00\", the value pasted from the bank statement with the currency symbol on it, or "
                "a spreadsheet cell with spaces around it. Something that is genuinely not an amount (an "
                "empty box, an invoice reference typed into the wrong field) should say which field is "
                "wrong, not return a server error. The handler is commandcenter/intake.py."
            ),
        },
    },
    "orders": {
        "title": "Discount applied twice — VAT charged on an already-discounted amount, discounted again",
        "module": "commandcenter/orders.py",
        "category": "Incorrect calculation logic",
        "surface": "Revenue · Order totals",
        "symptom": "discounted international orders are undercharged; the ledger disagrees with the processor",
        "edits": [
            ("""    discounted = discounted_subtotal(subtotal_cents, discount_pct)
    tax = round(discounted * tax_pct / 100.0)
    return discounted + tax + shipping_cents""",
             """    discounted = discounted_subtotal(subtotal_cents, discount_pct)
    taxable = discounted_subtotal(discounted, discount_pct)
    tax = round(taxable * tax_pct / 100.0)
    return discounted + tax + shipping_cents"""),
        ],
                "story": (
            "An order for ₹10,000 with a 10% festive promo and 20% GST was placed yesterday. The customer's invoice shows ₹10,800. Accounts pulled the settlement file from the payment processor in the morning and that order settled at ₹10,620."
        ),
        "oracle": (
            'Two independent records of the same order disagree by ₹180: the invoice we issued the customer and the settlement the processor actually took. Orders without a promo code reconcile exactly, so it is the promo path.'
        ),
        "ticket": {
            "title": "International orders with a promo code are undercharging — VAT is short on every one",
            "details": (
                "Finance reconciled yesterday's settlement against our order ledger and every international "
                "order that used a promo code is short. Worked example from order INT-88214: subtotal "
                "$100.00, 10% promo, 20% VAT. We charged $106.20. It should be $108.00 — 10% off $100 is "
                "$90, and 20% VAT on $90 is $18. The $1.80 gap is exactly 10% of the VAT, on every "
                "discounted order, which is why it looks like the discount is being taken off twice: once "
                "off the goods and again off the amount we work the VAT out on.\n\n"
                "Domestic orders reconcile perfectly, and international orders WITHOUT a promo code "
                "reconcile perfectly, so it only shows up when both a discount and a tax rate are present. "
                "It is in the order total calculation (commandcenter/orders.py). This is a tax "
                "under-collection so please treat it as urgent."
            ),
        },
    },

    # ── 4 · timezone / localization mismatch ──────────────────────────────────────────────────────────────
    "reports": {
        "title": "Daily revenue report buckets orders by server-local time instead of UTC",
        "module": "commandcenter/reports.py",
        "category": "Timezone / localization mismatch",
        "surface": "Revenue · Daily revenue (UTC)",
        "symptom": "late-night orders are reported on the wrong day, so two days' books are both wrong",
        "edits": [
            ("""    stamp = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    return stamp.date().isoformat()""",
             """    stamp = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    local = stamp + dt.timedelta(hours=SERVER_UTC_OFFSET_HOURS)
    return local.date().isoformat()"""),
        ],
                "story": (
            "The Monday revenue report was ₹1.2 lakh lower than the team expected and Tuesday's was higher by the same amount. Finance checked the orders behind it: three orders placed after 11pm on Monday are counted on Tuesday."
        ),
        "oracle": (
            "The payment processor's own daily settlement puts those three orders on Monday. Two systems, the same orders, different days — and the two days are wrong by exactly the same amount, which is what a boundary being drawn in the wrong place looks like."
        ),
        "ticket": {
            # Worded to describe the COMPUTATION, not the numbers. An earlier draft led with "the report
            # disagrees with the settlement file" and "the books are wrong", and the planner reasonably read
            # that as a data-repair request — an L2 tier with no code lane and no workspace. The defect is in
            # what the code computes on every run, and the report says so.
            "title": "Daily revenue report assigns late-night orders to the wrong day",
            "details": (
                "The daily revenue report puts orders on the wrong business day, and it does it every time "
                "it runs — the stored orders are all correct, it is what the report computes from them that "
                "is wrong.\n\n"
                # Deliberately avoids the word "reconcile": it is a `data-repair` keyword in triage's class
                # table, and it routed this ticket to the L2 data-action tier — which has no code lane.
                "The business day for this report is the UTC calendar day: that is what Finance closes "
                "against and what the Rotterdam and Chicago desks both report against. Order EU-5512 is "
                "stamped 02:10 UTC on 2026-08-12 and the report files it under 2026-08-11. An order stamped "
                "15:00 UTC on the 12th is filed correctly under the 12th. Re-running the report gives the "
                "same wrong answer, so nothing needs correcting in the data — the day it derives from a "
                "timestamp is wrong for anything before about 05:00 UTC.\n\n"
                "That is the offset of the reporting host (it runs 5 hours behind UTC), so it looks like the "
                "day bucketing converts the timestamp to the server's local time before taking the date "
                "instead of using UTC. It is in the report's day bucketing "
                "(commandcenter/reports.py). Please make it bucket on the UTC day so any server produces "
                "the same report."
            ),
        },
    },

    # ── 5 · the visual defect (kept from the earlier demo set) ────────────────────────────────────────────
    # A wrong colour is still a code defect: the dashboard's status colours come from ONE function, so a
    # "the whole dashboard is red" report is reproduced against a test and fixed in a line, like any other.
    "theme": {
        "title": "Every healthy status renders in the danger red",
        "module": "commandcenter/theme.py",
        "category": "Visual / CSS regression",
        "surface": "Every surface · status colours",
        "symptom": "a fully healthy system reads as critical — the whole dashboard looks like it is on fire",
        "legacy": True,
        "edits": [("    return BRAND_OK", '    return "#f85149"')],
                "story": (
            'The operations lead opened the Command Center on Monday and every status indicator on the board was red — services, KPIs, the lot — so he started an incident call. Nothing was actually down.'
        ),
        "oracle": (
            'The monitoring underneath the board reports every service healthy, and the services themselves are answering. The board is the only thing saying otherwise.'
        ),
        "ticket": {
            "title": "The whole dashboard shows red even though nothing is wrong",
            "details": (
                "Since this morning the Command Center looks like everything is on fire. The Operational "
                "pill at the top is red, all the service dots are red, and the KPI stripes are red — but "
                "every service underneath is genuinely fine and no incident is open. Two people have already "
                "escalated a 'major outage' that isn't happening.\n\n"
                "The red is the same colour we use for critical (#f85149), so it looks like the healthy "
                "colour is coming back as the danger colour. Our status colours come from one place "
                "(commandcenter/theme.py) rather than being scattered through the CSS. Please fix it so a "
                "healthy signal is the brand green again — people are ignoring the dashboard because they "
                "can't tell a real incident from this."
            ),
        },
    },

    # ── 6 · the rollup defect (kept from the earlier demo set) ────────────────────────────────────────────
    "refunds": {
        "title": "Refund split across order lines does not add up",
        "module": "commandcenter/refunds.py",
        "category": "Incorrect calculation logic",
        "surface": "Revenue · Refunds",
        "symptom": "a multi-line refund allocates a penny more or less than the customer was refunded",
        "edits": [('    base = sum(line_cents)\n    n = len(line_cents)\n\n    if base == 0:\n        # No proportions to take. Spread evenly and give the remainder to the earliest lines, so the split\n        # is still exact and still deterministic.\n        each, extra = divmod(total_cents, n)\n        return [each + (1 if i < extra else 0) for i in range(n)]\n\n    # The whole-cent part of each exact share, and the fraction each line had to give up to get there.\n    # Kept as integers (numerator over the common denominator `base`) so there is no float anywhere near\n    # the money — a float share of 33.33333333333333 compares unpredictably against another.\n    floors: list[int] = []\n    remainders: list[int] = []\n    for c in line_cents:\n        whole, rem = divmod(total_cents * c, base)\n        floors.append(whole)\n        remainders.append(rem)\n\n    # Hand out the leftover cents, largest discarded fraction first, ties by line order.\n    leftover = total_cents - sum(floors)\n    order = sorted(range(n), key=lambda i: (-remainders[i], i))\n    for i in order[:leftover]:\n        floors[i] += 1\n    return floors', '    base = sum(line_cents)\n    # Each line gets its share of the refund, rounded to the nearest cent.\n    return [round(total_cents * c / base) for c in line_cents]')],
                "story": (
            'A customer returned two items from a three-line order and was refunded ₹10,000. Their bank statement shows ₹10,000 credited. Our ledger shows three line credits of ₹3,333 — ₹9,999.'
        ),
        "oracle": (
            "The customer's statement and our ledger differ by ₹1 on a refund we made ourselves. The refund left our account in one number and landed in our books as three that do not add up to it."
        ),
        "ticket": {
            "title": "Refunds do not reconcile — the per-line split is a penny out on multi-line orders",
            "details": (
                "Finance reconciles refunds per order line and is getting breaks on any refund across "
                "more than one line. A £100.00 refund on three equal lines allocates £33.33 to each — "
                "£99.99 — so a penny is unaccounted for; on other orders it allocates a penny MORE than "
                "was refunded, which is worse, because we have credited money that was never charged.\n\n"
                "Over a month it is a few pounds, but every one is a manual journal, and the "
                "reconciliation cannot be signed off with an unexplained difference. The rule is that the "
                "per-line allocations must add up to the refund total EXACTLY, for every input — the "
                "split is in commandcenter/refunds.py. Please make the parts sum to the whole."
            ),
        },
    },
    "escalation": {
        "title": "SLA clock counts nights and weekends",
        "module": "commandcenter/escalation.py",
        "category": "Timezone / working-calendar logic",
        "surface": "Support · Escalation queue",
        "symptom": "Monday's queue shows every weekend ticket as breached",
        "edits": [("    total = 0\n    day = start.date()\n    last_day = end.date()\n    while day <= last_day:\n        if day.weekday() < 5:                       # Saturday is 5, Sunday is 6\n            window_open = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) \\\n                .replace(hour=DAY_START_HOUR)\n            window_shut = window_open.replace(hour=DAY_END_HOUR)\n            # The part of THIS day's window that the interval actually covers.\n            covered_from = max(start, window_open)\n            covered_to = min(end, window_shut)\n            if covered_to > covered_from:\n                total += int((covered_to - covered_from).total_seconds() // 60)\n        day += timedelta(days=1)\n    return total", '    # Elapsed time between the two timestamps, in whole minutes.\n    return int((end - start).total_seconds() // 60)')],
                "story": (
            'A customer raised a ticket at 16:30 on Friday. The desk answered it at 09:30 on Monday, half an hour after opening. The escalation queue had it flagged as breached all weekend and it is still red on Monday morning, along with everything else raised after Friday lunchtime.'
        ),
        "oracle": (
            'The support contract says four WORKING hours, and the desk works 09:00-17:00 Monday to Friday. Between Friday 16:30 and Monday 09:30 the desk was open for one hour. One is not four.'
        ),
        "ticket": {
            "title": "Every ticket raised on a Friday shows as breached on Monday",
            "details": (
                "The escalation queue is unusable on Monday mornings. Everything raised after Friday "
                "afternoon is red, including tickets nobody could have been expected to answer — a "
                "ticket raised Friday 16:30 and picked up Monday 09:30 shows as 65 hours old against a "
                "4-hour SLA.\n\n"
                "The desk works 09:00-17:00 Monday to Friday, and the SLA is written against those "
                "hours: that Friday ticket has been open for ONE business hour, not 65. The elapsed "
                "time is computed in commandcenter/escalation.py and appears to be counting wall-clock "
                "time. The team has started ignoring the colour entirely, which is the real damage."
            ),
        },
    },
    "pagination": {
        "title": "Paged audit export loses and duplicates rows",
        "module": "commandcenter/pagination.py",
        "category": "Data-integrity / boundary logic",
        "surface": "Compliance · Audit export",
        "symptom": "the export is short a few rows and contains a few twice",
        "edits": [('    if limit <= 0:\n        raise ValueError(f"limit must be positive, got {limit}")\n    ordered = sorted(rows, key=_key)\n    if after is None:\n        return ordered[:limit]\n    cursor = _key(after)\n    # Strictly after the cursor in the TOTAL order — a later timestamp, or the same timestamp and a later\n    # id. Comparing the tuples does exactly that; comparing the fields separately does not.\n    return [r for r in ordered if _key(r) > cursor][:limit]', '    ordered = sorted(rows, key=lambda r: r.get("created_at", ""))\n    if after is None:\n        return ordered[:limit]\n    # Resume after the cursor\'s timestamp.\n    return [r for r in ordered if r.get("created_at", "") > after.get("created_at", "")][:limit]')],
                "story": (
            'The quarterly audit export was sent to the external auditor. They counted the rows against a direct query on the audit table and sent it back: 41,318 rows in the export, 41,326 in the table, and eight of the rows in the export appear twice.'
        ),
        "oracle": (
            "The auditor's own count of the source table is the reference, and the export does not match it. The missing rows are all in batches written within the same second, which is where the export's page boundaries fall."
        ),
        "ticket": {
            "title": "The audit export does not match the table — rows missing, others duplicated",
            "details": (
                "Our auditor rejected the quarterly audit export. It has 41,318 rows where the table has "
                "41,326, and 8 of the rows it does have appear twice. The missing ones are not random: "
                "they are all in batches written within the same second.\n\n"
                "Audit rows are written in batches, so a batch shares a created_at to the second, and the "
                "export is fetched a page at a time. It looks like a page boundary landing inside one of "
                "those tied groups loses the rest of the group and re-fetches rows it already had. The "
                "paging is in commandcenter/pagination.py. Every row must appear exactly once across the "
                "pages — this is a compliance artefact and it has to be exact."
            ),
        },
    },
    "pipeline": {
        "title": "Pipeline value overstated — multi-currency deals counted once per currency",
        "module": "commandcenter/pipeline.py",
        "category": "Incorrect rollup logic",
        "surface": "Revenue · Pipeline value",
        "symptom": "the headline revenue number is overstated by every multi-currency deal",
        "legacy": True,
        "edits": [("""    total = 0
    seen_ids = set()
    for r in rows:
        if is_open(r):
            deal_id = r.get("id")
            if deal_id not in seen_ids:
                seen_ids.add(deal_id)
                total += int(r["amount_usd"])
    return total""",
                   '    return sum(int(r["amount_usd"]) for r in rows if is_open(r))')],
                "story": (
            'The board pack quotes Pipeline value at ₹2.04 crore. The sales director exported the same open deals from the CRM and added them up: ₹1.84 crore.'
        ),
        "oracle": (
            'The CRM export is the system of record for deals and it totals ₹1.84 crore. The ₹20 lakh gap is exactly the three deals that settle in more than one currency.'
        ),
        "ticket": {
            "title": "Pipeline value is overstating — multi-currency deals are counted twice",
            "details": (
                "Finance reconciled the Revenue 'Pipeline value' against the CRM export and it is "
                "overstating. Any deal that settles in more than one currency is counted once per currency — "
                "a $100k deal that settles in USD and EUR shows up as $200k. The dashboard reads $2.04M; the "
                "CRM export adds up to $1.84M, and the $200k gap is exactly the three multi-currency deals.\n\n"
                "The deals feed is a join with the settlement-currency legs, so a multi-currency deal arrives "
                "as several rows that each carry the deal's full converted value. The rollup should count "
                "each distinct deal once (commandcenter/pipeline.py). This is the number that goes in the "
                "board pack, so please fix it."
            ),
        },
    },

    # ── 7 · state management: the UI does not re-render after the action ────────────────────────────────
    # A FRONT-END defect, in front-end code, gated by a real browser. `state.notes.push(...)` succeeds
    # whether or not anything reaches the screen, so only the DOM can tell you whether the agent saw it.
    "uistate": {
        "title": "A saved note only appears after a page refresh",
        "module": "webui/app.js",
        "category": "State management glitch",
        "surface": "Support \u00b7 Add note to ticket (web UI)",
        "symptom": "the submitted state never reaches the screen, so agents re-submit the same note",
        "browser": True,
        # Both variants carry the closing `});`. Without it the buggy text is a PREFIX of the correct text,
        # and a prefix breaks both directions silently: `is_armed` sees the buggy string inside the healed
        # file and reports armed, while arming finds its target, rewrites it, and leaves the file exactly as
        # it was. The repo built green and the defect was not there. See _no_overlap below.
        "edits": [("""    state.submitting = false;
    input.value = "";
    render();
  });""",
                   """    state.submitting = false;
    input.value = "";
  });""")],
        "ticket": {
            "title": "Saved notes don't show up until you refresh the ticket",
            "details": (
                "When we add a note to a ticket, nothing on the page changes. The note box clears, but the "
                "list underneath still says 'No notes yet' and the note we just wrote is not in it. If you "
                "refresh the page the note IS there, so it is being saved \u2014 it just never appears until "
                "you reload.\n\n"
                "The desk lives in one tab all day, so nobody refreshes, and the agents assume the save "
                "failed and press it again. We have customers with the same note three times on their "
                "record now. It's the notes panel on the ticket page (webui/app.js). Please make a saved "
                "note show up straight away, and keep the counter underneath in step with it."
            ),
        },
    },

    # ── 8 · responsive layout: the dialog does not fit a phone ───────────────────────────────────────────
    "uilayout": {
        "title": "The confirmation dialog overflows a phone viewport and its button cannot be tapped",
        "module": "webui/styles.css",
        "category": "Responsive layout break",
        "surface": "Support \u00b7 Close ticket dialog (web UI)",
        "symptom": "on a phone the dialog is wider than the screen and the confirm button is off it",
        "browser": True,
        # `max-width: 100%` -> `min-width: 520px`. Removing max-width alone is NOT a defect: the dialog is a
        # flex item, so it simply shrinks to fit. A floor is what actually overflows a small viewport, and it
        # is the plausible mistake — someone pinning a minimum so the dialog "stops collapsing".
        "edits": [("""  width: 520px;
  max-width: 100%;""", """  width: 520px;
  min-width: 520px;""")],
        "ticket": {
            "title": "Can't close a ticket on a phone \u2014 the confirm button is off the edge of the screen",
            "details": (
                "On an iPhone, tapping 'Close ticket' opens the confirmation dialog but the dialog is wider "
                "than the screen. The 'Close ticket' button inside it sits off the right-hand edge and "
                "cannot be tapped at all, and the page scrolls sideways. Cancel is only half visible. On a "
                "laptop it looks perfectly fine, which is presumably why it shipped.\n\n"
                "Our field team works entirely on phones and they cannot close tickets, so they are calling "
                "the desk to do it for them. It is the dialog styling (webui/styles.css). Please make it "
                "fit a small screen with both buttons reachable \u2014 without shrinking it on desktop, "
                "where the current size is right."
            ),
        },
    },
}

# The four incidents built and armed as a set by the demo driver + repo builder.
ORDER = ("contacts", "intake", "orders", "reports", "refunds", "escalation", "pagination")
# Every code defect on the Command Center's checklist: the four above plus the two carried over from the
# earlier demo set. `theme` and `pipeline` keep their own long-standing repositories
# (samples/commandcenter-{theme,pipeline}), so the repo builder leaves them alone — they are `legacy`.
# The two FRONT-END incidents. Their defect is in DOM/CSS, so their gate is a real browser measuring the
# rendered page (samples/commandcenter/tests/test_ui_*.py) rather than an assertion over a return value.
# Kept out of ORDER because the per-incident repo builder ships the Python package; these live in the
# served app's own web UI and are worked there.
BROWSER = ("uistate", "uilayout")
# The incidents the dashboard POLLS. A live check runs on every 2-second tick, so it has to be cheap — the
# browser incidents are gated by a real browser instead, which takes seconds, and are shown on the page
# itself rather than as a polled card. Naming the set beats each consumer guessing at it.
LIVE_CHECKED = ORDER + ("theme", "pipeline")

# The set the Command Center actually SHOWS. Everything in LIVE_CHECKED works; this is a narrower choice
# about what a demo has time to tell properly. Each of these five has a person behind it doing something
# recognisable — entering a customer, recording a payment, checking a total, chasing an SLA, sending an
# export to an auditor — and a surface on the board where you can do that thing yourself and watch it go
# wrong. The others stay in the registry, keep their repos, and can be put back by adding them here.
DEMO = ("contacts", "intake", "orders", "escalation", "pagination")
ALL = ORDER + ("theme", "pipeline") + BROWSER


def mirror_dir(slug: str) -> str:
    """The isolated single-defect git repo AgentForge is pointed at for `slug`."""
    return os.path.join(ROOT, "samples", f"commandcenter-{slug}")


# The OWNER the per-incident repos are published under. A deployed platform has no local `samples/` tree, so
# a ticket it can actually work has to name a real repository — see scripts/publish_cc_incident_repos.py.
GITHUB_OWNER = os.getenv("GITHUB_CC_OWNER", "jeet-1236")


def github_repo(slug: str) -> str:
    """The published GitHub repo for this incident: one defect, three commits, its own suite."""
    return f"{GITHUB_OWNER}/cc-{slug}"



# The CLIENT-worded version of each incident, for the reporter surface. Symptom-only ON PURPOSE:
# it names no file, so localization and root cause stay the agents' work. A demo that hands over the
# filename has skipped the part worth watching.
REPORTER = {
    'contacts': {
        "label": "Sales can't save customers with a +91 number", "area": 'CRM & deals sync', "impact": 'blocked',
        "title": 'Sales cannot add customers to the CRM',
        "details": "The Bengaluru and Mumbai desks cannot add contacts. The form rejects the phone number for anything except a plain ten-digit mobile — +91 98765 43210, (022) 2654 0000 and 080-2345-6789 are all refused, while 98765 43210 saves. It rejects names too: Meera D'Souza and R. Venkataraman both come back as invalid, though Ananya Krishnamurthy is fine. Both desks are blocked.",
    },
    'intake': {
        "label": "Payments won't save unless you type a comma", "area": 'Billing & revenue', "impact": 'blocked',
        "title": 'Recording a payment fails when the amount is a round number',
        "details": "I am entering yesterday's cleared payments and about half of them will not save. If I type the amount as a plain number the page throws an error and nothing is recorded. If I type the same amount with a comma and decimals it saves straight away. It is the same payment either way.",
    },
    'orders': {
        "label": 'International orders are undercharged', "area": 'Billing & revenue', "impact": 'blocked',
        "title": 'Orders with a promo code are being undercharged',
        "details": "Finance reconciled yesterday's settlement against the order ledger. Every international order that used a promo code collected less tax than it should have. Orders without a promo code reconcile exactly, and domestic orders are fine.",
    },
    'reports': {
        "label": 'Daily revenue lands on the wrong day', "area": 'Billing & revenue', "impact": 'affecting',
        "title": 'The daily revenue report puts orders on the wrong day',
        "details": 'The daily revenue figures do not agree with the ledger. Orders placed late in the evening are being counted against the following day, so one day is understated and the next overstated by the same amount. It happens every time the report runs.',
    },
    'refunds': {
        "label": 'Refunds are a penny out on multi-line orders', "area": 'Billing & revenue', "impact": 'blocked',
        "title": 'Refunds do not reconcile on orders with more than one line',
        "details": 'Finance reconciles refunds per line and is getting breaks on every refund across more than one line. A 100.00 refund on three equal lines pays out 99.99; on other orders it pays out a penny MORE than was refunded. Each one is a manual journal and the reconciliation cannot be signed off.',
    },
    'escalation': {
        "label": 'Friday tickets all breach over the weekend', "area": 'Support & tickets', "impact": 'affecting',
        "title": 'Everything raised on a Friday shows as breached on Monday',
        "details": 'The escalation queue is unusable on Monday mornings — everything raised after Friday afternoon is red, including tickets nobody could have answered. A ticket raised Friday 16:30 and picked up Monday 09:30 shows as 65 hours old against a 4-hour target. The desk works 09:00-17:00 Monday to Friday. The team has started ignoring the colour.',
    },
    'pagination': {
        "label": "The audit export doesn't match the table", "area": 'Compliance & audit', "impact": 'blocked',
        "title": 'The audit export is missing rows and repeating others',
        "details": 'Our auditor rejected the quarterly export. It has 41,318 rows where the table has 41,326, and 8 of the rows it does have appear twice. The missing ones are not random — they are all in batches written within the same second.',
    },
}


def reporter_text(slug: str) -> str:
    """What the CLIENT writes: what they were doing, what happened, and how they know it is wrong. Names no
    file and asks for no particular fix — on that surface, working that out is the agents' job."""
    inc = INCIDENTS[slug]
    r = REPORTER.get(slug, {})
    parts = [p for p in (inc.get("story"), r.get("details")) if p]
    if inc.get("oracle"):
        parts.append("How we know this is wrong: " + inc["oracle"])
    return "\n\n".join(parts)


def report_text(slug: str) -> str:
    """The ticket body as a reporter would actually write it: what happened, how we know it is wrong, then
    what is being asked for. Built from the incident's own story/oracle so the two cannot drift apart."""
    inc = INCIDENTS[slug]
    parts = []
    if inc.get("story"):
        parts.append(inc["story"])
    if inc.get("oracle"):
        parts.append("How we know this is wrong: " + inc["oracle"])
    parts.append(inc["ticket"]["details"])
    return "\n\n".join(parts)


def mirror_repo(slug: str) -> str:
    """The repo path as a ticket carries it (relative to the AgentForge root)."""
    return f"samples/commandcenter-{slug}"


def served_module(slug: str) -> str:
    return os.path.join(SERVED, INCIDENTS[slug]["module"])


def _swap(text: str, pairs, arm: bool) -> tuple:
    """Apply every (fixed, buggy) pair in one direction. Returns (new_text, applied, already)."""
    applied = already = 0
    for fixed, buggy in pairs:
        src, dst = (fixed, buggy) if arm else (buggy, fixed)
        if src in text:
            text = text.replace(src, dst, 1)
            applied += 1
        elif dst in text:
            already += 1
    return text, applied, already


def set_state(slug: str, arm: bool, path: str = "") -> str:
    """Arm (`arm=True`) or heal a module in place. Returns a one-line human-readable outcome.

    Idempotent in both directions, and it never half-applies: a file that matches neither variant is left
    exactly as it is and says so, because a partially-rewritten module is worse than an un-armed demo.
    """
    inc = INCIDENTS[slug]
    p = path or served_module(slug)
    with open(p, encoding="utf-8") as fh:
        text = fh.read()
    new, applied, already = _swap(text, inc["edits"], arm)
    want = len(inc["edits"])
    verb = "armed" if arm else "healed"
    if applied == 0 and already == want:
        return f"{slug}: already {verb} ({inc['module']} unchanged)"
    if applied + already != want:
        return (f"{slug}: ✗ {inc['module']} matches neither variant "
                f"({applied} of {want} edits applied) — left untouched")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(new)
    return f"{slug}: {verb} · {inc['module']}"


# The KNOWN-GOOD source of every incident module, kept as plain files. `--reset` restores from here.
#
# This exists because of what a SUCCESSFUL demo does to the working tree. AgentForge's fix is a real edit
# written by a model: correct, and textually nothing like ours. Once it is deployed, the served module
# matches neither variant in `edits`, `set_state` refuses (loudly, by design) — and the incident cannot be
# re-armed. So the second rehearsal of the day would quietly have nothing to show.
#
# Neither git HEAD nor the incident repo can stand in for this. The two carried-over incidents are committed
# in their ARMED state (that is how those demos have always shipped), and the incident repos exist to hold a
# defect. A file whose only job is "the correct version" is the one thing that is unambiguous.
CANONICAL_DIR = os.path.join(SERVED, ".canonical")


def canonical_path(slug: str) -> str:
    return os.path.join(CANONICAL_DIR, os.path.basename(INCIDENTS[slug]["module"]))


def snapshot_canonical(slug: str = "") -> list:
    """Record the CURRENT served source as canonical. Only valid when the module is correct — the caller is
    responsible for that, which is why it is a deliberate command and not something a run does."""
    os.makedirs(CANONICAL_DIR, exist_ok=True)
    out = []
    for s in ([slug] if slug else list(ALL)):
        with open(served_module(s), encoding="utf-8") as fh:
            src = fh.read()
        if any(buggy in src for _f, buggy in INCIDENTS[s]["edits"]):
            out.append(f"{s}: ✗ REFUSED — the served module is armed; heal it before snapshotting")
            continue
        with open(canonical_path(s), "w", encoding="utf-8") as fh:
            fh.write(src)
        out.append(f"{s}: canonical source recorded")
    return out


def canonical_source(slug: str) -> str:
    try:
        with open(canonical_path(slug), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def reset(slug: str) -> str:
    """Restore the served module to its canonical correct source. Returns a one-line outcome."""
    src = canonical_source(slug)
    if not src:
        return (f"{slug}: ✗ no canonical source recorded — run "
                f"`scripts/demo_l2_incident.py --snapshot` while the app is healthy")
    p = served_module(slug)
    with open(p, encoding="utf-8") as fh:
        if fh.read() == src:
            return f"{slug}: already at the canonical source"
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(src)
    return f"{slug}: reset to the canonical source · {INCIDENTS[slug]['module']}"


def is_armed(slug: str, path: str = "") -> bool:
    """True when the module carries the defect (every buggy variant present)."""
    inc = INCIDENTS[slug]
    with open(path or served_module(slug), encoding="utf-8") as fh:
        text = fh.read()
    return all(buggy in text for _fixed, buggy in inc["edits"])


def _no_overlap() -> None:
    """Refuse an arm/heal pair where one variant CONTAINS the other.

    `set_state` and `is_armed` both work by substring, so an overlapping pair fails silently in both
    directions: the healed file contains the buggy string (so `is_armed` says armed when it is not), and
    arming replaces its target with a string the file already effectively has (so nothing changes). The
    result is a demo repository that builds green with no defect in it — which is the one failure mode
    worth an import-time check, because everything downstream reports success.
    """
    for slug, inc in INCIDENTS.items():
        for i, (fixed, buggy) in enumerate(inc["edits"]):
            if fixed in buggy or buggy in fixed:
                raise AssertionError(
                    f"cc_incidents[{slug!r}].edits[{i}]: one variant contains the other, so arming and "
                    f"is_armed are both unreliable — extend both sides until they differ at each end")


_no_overlap()
