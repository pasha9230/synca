"""Test orders under the rule of one open order per phone (the owner 2026-10-27, orders.OpenOrderExists)."""
from __future__ import annotations

from tires_sync import connections, orders


def free_phone(conn, phone: str) -> str:
    """The phone itself, or, when an open order has it already, the next number with no open order: a helper that makes many orders of
    «the same» client whose phone is not what the test is about."""
    digits = connections.normalize_phone(phone)
    if orders.open_card_by_phone(conn, digits) is None:
        return phone
    number = int(digits[2:])
    while orders.open_card_by_phone(conn, "+7%010d" % number) is not None:
        number += 1
    return "+7%010d" % number


def legacy_card(conn, fields: dict, now: str, author: str, **kwargs) -> int:
    """An order of a phone that has another open order, as the base holds them from before the rule: made without the phone, which is then
    written by an edit (orders.update_card joins it to its client, as a phone typed into the card does)."""
    fields = dict(fields)
    phone = fields.pop("client_phone", "")
    card_id = orders.create_card(conn, fields, now, author, **dict(kwargs, require_contact=False))
    if phone:
        orders.update_card(conn, card_id, {"client_phone": phone}, now, author)
    return card_id


def card_of_phone(conn, fields: dict, now: str, author: str, **kwargs) -> int:
    """orders.create_card, or legacy_card when the phone has an open order already (the test is about several orders of one client)."""
    if fields.get("client_phone") and orders.open_card_by_phone(conn, fields["client_phone"]) is not None:
        return legacy_card(conn, fields, now, author, **kwargs)
    return orders.create_card(conn, fields, now, author, **kwargs)
