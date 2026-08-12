"""commandcenter.contacts — CRM contact intake validation for the Revenue surface.

A sales rep adds a contact from the Command Center's "New CRM contact" form. Every contact must carry a
name we can address them by and a phone number we can dial, and the same rule runs twice: once in the
browser to give the rep immediate feedback, and once here before the record reaches the CRM.

The book of record is international. Names are free text and routinely carry an apostrophe, a hyphen or an
accent (O'Brien, Anne-Marie, José). Phone numbers arrive however the rep typed them — E.164 with a country
code and spaces (+44 20 7946 0958), the domestic dashed form (555-018-2231), or with an area code in
parentheses ((212) 555-0147).
"""
from __future__ import annotations

# The punctuation a human legitimately types inside a phone number. Everything else is a typo.
PHONE_SEPARATORS = " ()-."

# ── field rules ───────────────────────────────────────────────────────────────────────────────────────────
# The two predicates below ARE the validation rule. Keeping them as named functions (rather than inlining a
# pattern into validate_contact) is what lets a "the form rejects my customer" report be reproduced against
# one function and fixed in one place.


def _name_ok(name: str) -> bool:
    """A name is 2–60 characters of letters plus ordinary name punctuation (space, apostrophe, hyphen).

    Accented and non-Latin letters ARE letters. A digit is never part of a person's name.
    """
    if not 2 <= len(name) <= 60:
        return False
    return all(("a" <= c.lower() <= "z") or c == " " for c in name)


def _phone_ok(phone: str) -> bool:
    """A phone number is 7–15 digits once the separators are removed, with an optional leading '+'.

    7–15 is the E.164 range: a national number is never shorter, and no country's number is longer.
    """
    parts = phone.split("-")
    return [len(p) for p in parts] == [3, 3, 4] and all(p.isdigit() for p in parts)


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
