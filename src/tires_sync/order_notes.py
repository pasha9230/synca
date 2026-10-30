"""«Есть примечание» of the board and the list of orders (the owner 2026-10-30): an order has a note of a person — the comment of the order
or a note of the journal — as long as it has one; the hint of the badge is the latest of them, its first NOTE_HINT characters. Not a note:
what the robot, the system, the bot or an automatic record wrote. Read only."""
from __future__ import annotations

import re
from typing import Callable, Dict, Iterable, Optional

from .users import LOGIN, RESERVED

NOTE_HINT = 100
# the notes of the journal the panel writes by itself under the name of the person who pressed (order_bulk, routes_mypoint)
AUTO_NOTES = ("Возвращён после массового перевода", "Клиент передан через «Рабочее место»")
# the comments a new order gets from its source and nobody typed (chat_deals.create_deal, calls, order_manager.VOICE_NOTE)
AUTO_COMMENT = re.compile(r"^(Объявление: |Звонок \d{4}-\d{2}-\d{2} \d{2}:\d{2}[ (]|голосовое сообщение, нужна расшифровка$)")
COMMENT_EVENT = "Комментарий: "                     # orders.update_card: the record of a changed comment


POINT_MANAGER = "менеджер "                         # order_manager: the manager of a point acting by VK or «Рабочее место» («менеджер Альфа»)


def is_person(author: str) -> bool:
    """A login of the panel (latin letters, not a name of the server: «cli», «server», «system») or the manager of a point — not
    «система», «чаты Авито», «бот ВК» and the like."""
    author = str(author or "")
    return (bool(LOGIN.match(author)) and author not in RESERVED - {"owner"}) or author.startswith(POINT_MANAGER)


def hint(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= NOTE_HINT else text[:NOTE_HINT].rstrip() + "…"


def latest(conn, card_ids: Iterable[int], clean: Optional[Callable[[str], str]] = None) -> Dict[int, str]:
    """Order id → the hint of its latest note of a person (the orders without one are not in it). `clean` (the manager of a point: no
    account's name) works on the whole text, before it is cut."""
    ids = sorted({int(i) for i in card_ids})
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    found: Dict[int, tuple] = {}                                   # id → (at, record id, text)
    edited: Dict[int, tuple] = {}                                  # id → (at, author) of the last change of the comment
    for oid, eid, at, author, kind, text in conn.execute(
            "SELECT order_id, id, at, author, kind, text FROM order_event WHERE order_id IN (%s) AND (kind = 'note' OR (kind = 'field' AND text LIKE ?)) "
            "ORDER BY id" % marks, ids + [COMMENT_EVENT + "%"]):
        if kind == "field":
            edited[oid] = (at, author)
        elif is_person(author) and not str(text).startswith(AUTO_NOTES):
            found[oid] = (at, eid, text)
    for oid, comment, created in conn.execute("SELECT id, comment, created_at FROM order_card WHERE id IN (%s) AND comment != ''" % marks, ids):
        if oid in edited:
            at, author = edited[oid]
            if not is_person(author):
                continue
        elif AUTO_COMMENT.match(comment):                          # as the source left it
            continue
        else:
            at = created
        if oid not in found or at >= found[oid][0]:
            found[oid] = (at, 0, comment)
    return {oid: hint(clean(v[2]) if clean else v[2]) for oid, v in found.items()}
