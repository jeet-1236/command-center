"""commandcenter.orders — order totals for the Revenue surface.

The number this module returns is the number the payment processor charges the customer, so it has to agree
with the processor's own settlement to the cent. Money is integer CENTS throughout; a float would round
differently on the two sides of that comparison.

An order total is built in a fixed order, and the order matters because each step feeds the next:

    1. the promotional discount comes off the subtotal
    2. tax is charged on what the customer actually pays for the goods — the DISCOUNTED amount
    3. shipping is added last, after tax (it is quoted tax-inclusive by the carrier)

Step 2 is where international orders differ from domestic ones: a domestic order carries 0% tax at this
layer (it is added downstream by the state-tax service), so any error in the tax base is invisible at home
and shows up only on orders that carry a VAT/GST rate.
"""
from __future__ import annotations


def discounted_subtotal(subtotal_cents: int, discount_pct: float) -> int:
    """The subtotal after the promotional discount, in cents."""
    return round(subtotal_cents * (1 - discount_pct / 100.0))


def order_total(subtotal_cents: int, discount_pct: float = 0.0, tax_pct: float = 0.0,
                shipping_cents: int = 0) -> int:
    """The amount to charge, in cents: discounted subtotal + tax on that discounted amount + shipping."""
    discounted = discounted_subtotal(subtotal_cents, discount_pct)
    tax = round(discounted * tax_pct / 100.0)
    return discounted + tax + shipping_cents
