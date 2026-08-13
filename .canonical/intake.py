"""commandcenter.intake — recording a payment against an invoice.

Finance records payments as they clear the bank. The form has an invoice reference and an amount, and the
amount is the whole problem: it arrives as whatever the browser sent, and the browser sends whatever the
assistant typed.

    1200          a JSON number — the field is a number input, and a round amount has no separators
    "1200"        a string, when the value came from a paste or an older browser
    "1,200.00"    typed the way it is written on the remittance advice
    "₹1,200.00"   pasted straight from the bank statement, symbol and all
    "  1200 "     pasted from a spreadsheet cell

All five are the same payment and all five must record it. Money is integer CENTS from here on, because
the ledger reconciles against the bank to the paisa and a float does not survive that comparison.

What must NOT be accepted is a value that is not an amount at all — a blank field, a reference typed into
the wrong box, a negative number. Those get a 400 naming the field, so the assistant sees which box to fix.
A 500 tells them nothing, pages the on-call, and loses the payment.
"""
from __future__ import annotations

# What a human legitimately types or pastes around a number. Anything else means it is not an amount.
_AMOUNT_NOISE = " \t ,₹$£€"


def parse_amount(value) -> int:
    """The amount in integer CENTS, from any of the shapes the form actually sends.

    Raises ValueError with a human-readable reason when the value is not an amount. The caller turns that
    into a 400 against the field; it must never become a 500.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError("an amount is required")
    if isinstance(value, bool):                       # bool is an int in Python; a tick-box is not an amount
        raise ValueError("that is not an amount")
    if isinstance(value, int):
        cents = value * 100
    elif isinstance(value, float):
        cents = round(value * 100)
    elif isinstance(value, str):
        cleaned = value.strip()
        for ch in _AMOUNT_NOISE:
            cleaned = cleaned.replace(ch, "")
        if not cleaned:
            raise ValueError("an amount is required")
        try:
            cents = round(float(cleaned) * 100)
        except ValueError:
            raise ValueError(f"{value!r} is not an amount") from None
    else:
        raise ValueError("that is not an amount")
    if cents < 0:
        raise ValueError("an amount cannot be negative")
    return int(cents)


def record_payment(payload: dict) -> tuple:
    """(status, body) for POST /api/payments. Never raises — a bad field is a 400 that names it."""
    invoice = str((payload or {}).get("invoice_id") or "").strip()
    if not invoice:
        return 400, {"error": "invoice_id is required", "field": "invoice_id"}
    try:
        cents = parse_amount((payload or {}).get("amount"))
    except ValueError as e:
        return 400, {"error": str(e), "field": "amount"}
    return 201, {"invoice_id": invoice, "amount_cents": cents,
                 "recorded": f"₹{cents / 100:,.2f} against {invoice}"}
