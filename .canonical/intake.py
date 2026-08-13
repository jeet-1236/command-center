"""commandcenter.intake — the request handler behind POST /api/notes on the Support surface.

An agent (or an integration) attaches a note to a ticket. The body is JSON:

    {"ticket_id": "TCK-4471", "notes": "customer called back, happy to wait"}

`notes` is OPTIONAL. Clients express "no note" in the two ways JSON allows — they omit the key, or they send
an explicit `null` (which is what every form binding produces for an empty text box). Both mean the same
thing: an empty note.

The contract this handler owes its callers is that a bad or incomplete request comes back as a **400 with a
field-level message the client can act on**. A 500 is a different promise entirely: it says the fault is
ours, it pages the on-call, and it tells the integration to retry a request that will never succeed.
"""
from __future__ import annotations

MAX_NOTE = 500


def handle_note(payload: dict) -> tuple:
    """Handle POST /api/notes. Returns `(status_code, body)`.

    201 — the note was accepted (the body echoes the stored note).
    400 — the request is not usable; the body carries `error` naming what to fix.

    This handler must never raise: whatever a client sends, it answers with one of those two.
    """
    ticket = payload.get("ticket_id")
    if not ticket:
        return 400, {"error": "ticket_id is required"}
    notes = payload.get("notes", "")
    text = (notes or "").strip()
    if len(text) > MAX_NOTE:
        return 400, {"error": f"notes must be at most {MAX_NOTE} characters"}
    return 201, {"ticket_id": ticket, "notes": text}
