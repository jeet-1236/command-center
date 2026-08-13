"""commandcenter.contacts — CRM contact intake validation for the Revenue surface.

A sales rep adds a contact from the Command Center's "New CRM contact" form. Every contact must carry a
name we can address them by and a phone number we can dial, and the same rule runs twice: once in the
browser to give the rep immediate feedback, and once here before the record reaches the CRM.

The book of record is Indian, and increasingly international. Names are free text: most carry no
punctuation (Ananya Krishnamurthy), but plenty do — an initial with a full stop is how a South Indian name
is ordinarily written (R. Venkataraman), and apostrophes, hyphens and accents all occur (D'Souza,
Anne-Marie, José).

Phone numbers arrive however the rep typed them. The shapes the desk actually sees are the mobile with a
country code (+91 98765 43210), the same number without it (98765 43210), a landline with an STD code in
parentheses ((022) 2654 0000), and the hyphenated form (080-2345-6789).
"""
from __future__ import annotations

# The punctuation a human legitimately types inside a phone number. Everything else is a typo.
PHONE_SEPARATORS = " ()-."

# ── field rules ───────────────────────────────────────────────────────────────────────────────────────────
# The two predicates below ARE the validation rule. Keeping them as named functions (rather than inlining a
# pattern into validate_contact) is what lets a "the form rejects my customer" report be reproduced against
# one function and fixed in one place.


def _name_ok(name: str) -> bool:
    """A name is 2–60 characters of letters plus ordinary name punctuation (space, apostrophe, hyphen, dot).

    The full stop matters: "R. Venkataraman" is how the name is written, not a typo. Accented and
    non-Latin letters ARE letters. A digit is never part of a person's name.
    """
    if not 2 <= len(name) <= 60:
        return False
    return all(c.isalpha() or c in " '-." for c in name)


def _phone_ok(phone: str) -> bool:
    """A phone number is 7–15 digits once the separators are removed, with an optional leading '+'.

    7–15 is the E.164 range: a national number is never shorter, and no country's number is longer.
    """
    body = phone[1:] if phone.startswith("+") else phone
    if any(not (c.isdigit() or c in PHONE_SEPARATORS) for c in body):
        return False
    return 7 <= sum(1 for c in body if c.isdigit()) <= 15


def validate_contact(contact: dict) -> list:
    """Validate a contact for the CRM. Returns the list of FIELD NAMES that failed, in field order.

    An empty list means the contact is valid and gets saved. Anything in the list is shown against that
    field in the form and the record is NOT saved, so a rule that is stricter than the book of record locks
    a real customer out of the CRM entirely.
    """
    errors = []
    name = (contact.get("name") or "").strip()
    phone = (contact.get("phone") or "").strip()
    if not _name_ok(name):
        errors.append("name")
    if not _phone_ok(phone):
        errors.append("phone")
    return errors
