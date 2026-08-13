"""commandcenter.refunds — splitting a refund across the lines of an order.

When a customer is refunded for a multi-line order, the refund has to be attributed back to the individual
lines: Finance reconciles per line, tax is reclaimed per line at that line's rate, and the marketplace
commission is clawed back per line. So one number in has to become several numbers out.

The rule those downstream systems rely on is exact and unforgiving:

    THE PARTS MUST SUM TO THE WHOLE. sum(split(total, lines)) == total, always, for every input.

That is harder than it looks, because the natural split — each line's share of the order value — almost
never divides evenly into whole cents. A £100.00 refund across three lines of £33.33, £33.33 and £33.34
gives shares of 3333.0, 3333.0 and 3334.0 cents, which is fine; but across three EQUAL lines it gives
3333.33… each, and there is no way to write that in cents. Something has to absorb the remainder.

Rounding each share on its own does not work, and it fails in both directions: three shares of 3333.33
round to 3333 each and lose a penny, while three of 3333.67 round to 3334 each and invent one. A lost
penny is a reconciliation break somebody chases by hand; an invented one is a refund of money that was
never charged.

The method used here is LARGEST REMAINDER. Give every line the whole-cent part of its exact share, count
how many cents are left over, and hand those out one each to the lines with the largest discarded
fraction. The result sums exactly by construction, and the cents land where the customer has the strongest
claim to them. Ties are broken by line order so the same order always splits the same way — Finance
re-runs these and expects the same answer twice.

Amounts are integer CENTS. `lines` are the per-line amounts of the ORIGINAL order, and they are what the
proportions are taken from.
"""
from __future__ import annotations


def split_refund(total_cents: int, line_cents: list[int]) -> list[int]:
    """Split `total_cents` across `line_cents` in proportion, in whole cents, summing EXACTLY to the total.

    Returns one amount per line, in the same order. Raises ValueError on a negative total, a negative line,
    or an empty line list — all three mean the caller has a bug and a silent zero would hide it.

    An order whose lines sum to zero (a fully comped order that is now being refunded) cannot be split
    proportionally at all, so the refund is spread as evenly as whole cents allow, earliest lines first.
    """
    if total_cents < 0:
        raise ValueError(f"refund total must not be negative: {total_cents}")
    if not line_cents:
        raise ValueError("cannot split a refund across no lines")
    if any(c < 0 for c in line_cents):
        raise ValueError(f"line amounts must not be negative: {line_cents}")

    base = sum(line_cents)
    n = len(line_cents)

    if base == 0:
        # No proportions to take. Spread evenly and give the remainder to the earliest lines, so the split
        # is still exact and still deterministic.
        each, extra = divmod(total_cents, n)
        return [each + (1 if i < extra else 0) for i in range(n)]

    # The whole-cent part of each exact share, and the fraction each line had to give up to get there.
    # Kept as integers (numerator over the common denominator `base`) so there is no float anywhere near
    # the money — a float share of 33.33333333333333 compares unpredictably against another.
    floors: list[int] = []
    remainders: list[int] = []
    for c in line_cents:
        whole, rem = divmod(total_cents * c, base)
        floors.append(whole)
        remainders.append(rem)

    # Hand out the leftover cents, largest discarded fraction first, ties by line order.
    leftover = total_cents - sum(floors)
    order = sorted(range(n), key=lambda i: (-remainders[i], i))
    for i in order[:leftover]:
        floors[i] += 1
    return floors


def refund_is_balanced(total_cents: int, allocations: list[int]) -> bool:
    """The check Finance runs on the way in: does this split actually add up?"""
    return sum(allocations) == total_cents
