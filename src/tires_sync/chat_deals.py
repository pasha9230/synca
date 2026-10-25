"""Binding Avito chats to deals: a deal is made by the manager (the button in «Чаты»; the automatic one on a first message is off since 2026-10-15), a phone named in the chat joins chats of one client.

The phone is never in Avito's data: it is read from what the client wrote. Closed deals are never reused; a chat the owner unlinked is left alone.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import List, Optional

from . import chats as chats_store, clients, order_points as points_store, order_wait, orders, store
from .orders import OrderError

log = logging.getLogger("tires_sync.chats")
PHONE = re.compile(r"(?<!\d)(?<!\d[.,-])(?<!\d\s)(?:(\+?[78])[\s\-()]*)?\(?(9\d{2}|812)\)?([\s\-]*)(\d{3})([\s\-]*)(\d{2})([\s\-]*)(\d{2})(?!\d)")
NOT_A_PHONE_BEFORE = re.compile(r"(заказ\w*|артикул\w*|код\w*|№|номер заказа|цена|сумма)\W*$", re.I)
SYSTEM_AUTHOR = "чаты Авито"


def extract_phone(text, own=()) -> Optional[str]:
    """A Russian mobile (9xx) or St Petersburg (812) number in free text as +7XXXXXXXXXX, or None.

    A bare run of ten digits is not taken (an order number or an article looks the same); neither is a number that follows
    «заказ», «артикул», «цена» and the like, nor one of `own` (the shop's own numbers that a client quotes)."""
    text = str(text or "")
    for found in PHONE.finditer(text):
        prefix, area, sep1, middle, sep2, tens, sep3, ones = found.groups()
        if not (prefix or sep1 or sep2 or sep3):
            continue
        if NOT_A_PHONE_BEFORE.search(text[max(0, found.start() - 20):found.start()]):
            continue
        phone = "+7" + area + middle + tens + ones
        if _tail(phone) in own:
            continue
        return phone
    return None


def own_phones(conn) -> set:
    """The tails of the points' phones: the numbers a client quotes from our own ads must never become the client's."""
    return {t for t in (_tail(r[0]) for r in conn.execute("SELECT manager_phone FROM point")) if t}


def _tail(phone) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    return digits[-10:] if len(digits) >= 10 else ""


def ref(chat_row) -> str:
    return "Ч-%d" % chat_row


def _incoming_texts(conn, chat_row) -> List[str]:
    return [r[0] for r in conn.execute("SELECT text FROM avito_message WHERE chat_row = ? AND direction = 'in' AND kind = 'text' ORDER BY ts DESC, id DESC", (chat_row,))]


def _open_deals_with(conn, phone: str, skip: Optional[int] = None) -> List[int]:
    tail = _tail(phone)
    if not tail:
        return []
    out = []
    for card_id, stored in conn.execute("SELECT c.id, c.client_phone FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE s.kind = 'open' AND c.client_phone != '' ORDER BY c.id"):
        if card_id != skip and _tail(stored) == tail:
            out.append(card_id)
    return out


def _attach(conn, chat: dict, card_id: int, now: str, author: str) -> None:
    conn.execute("UPDATE avito_chat SET order_id = ?, unlinked = 0 WHERE id = ?", (card_id, chat["id"]))
    card = orders.get_card(conn, card_id)
    if not card["avito_chat"]:
        orders.update_card(conn, card_id, {"avito_chat": ref(chat["id"])}, now, author, commit=False)
    orders.add_event(conn, card_id, now, author, "chat", "Привязан чат %s: %s" % (ref(chat["id"]), chat["item_title"] or chat["peer_name"] or "без объявления"))
    bound = not card["point_id"] and _bind_point(conn, card_id, chat, now)
    conn.execute("SAVEPOINT client_link")                                 # the client of the Avito peer (or of the order); saved with the binding
    try:
        clients.attach_chat(conn, chat["id"], card_id, now, author)
        conn.execute("RELEASE client_link")
    except sqlite3.Error:                                                 # a client that cannot be found never breaks the deal; half of it is undone
        conn.execute("ROLLBACK TO client_link")
        conn.execute("RELEASE client_link")
        logging.getLogger("tires_sync.orders").exception("the client of chat %s could not be found", chat["id"])
    conn.commit()
    if bound:
        fire_point_rules(conn, card_id, now)


CLEARED_BY_HAND = "%s: (пусто)" % orders.LABELS["point_id"]             # the record update_card writes when a person takes the point off


def claim_point(conn, card_id, point_id, now: str, author: str, text: str, auto: bool = False) -> bool:
    """The order of no point becomes the point's: one UPDATE with the condition (as «Взять»), so a point set meanwhile is never changed;
    the record only when it was written. `auto` (the binding by a chat): only an open order whose point no person took off. The point's usual
    delivery time comes with it when no time is there, as when a person chooses the point (orders._default_time). Not saved."""
    extra = (" AND stage_id IN (SELECT id FROM order_stage WHERE kind = 'open') AND NOT EXISTS (SELECT 1 FROM order_event e WHERE "
             "e.order_id = order_card.id AND e.kind = 'field' AND e.text = ? AND e.author != ?)") if auto else ""
    args = (point_id, now, card_id, point_id) + ((CLEARED_BY_HAND, SYSTEM_AUTHOR) if auto else ())
    if conn.execute("UPDATE order_card SET point_id = ?, updated_at = ? WHERE id = ? AND point_id IS NULL AND EXISTS (SELECT 1 FROM point WHERE id = ?)"
                    + extra, args).rowcount != 1:
        return False
    orders.add_event(conn, card_id, now, author, "field", text)
    usual = conn.execute("SELECT default_time FROM point WHERE id = ?", (point_id,)).fetchone()[0]
    if usual and conn.execute("UPDATE order_card SET delivery_time = ? WHERE id = ? AND delivery_time = ''", (usual, card_id)).rowcount == 1:
        orders.add_event(conn, card_id, now, author, "field", "%s: %s" % (orders.LABELS["delivery_time"], str(usual)[:80]))     # as update_card says it
    return True


def fire_point_rules(conn, card_id, now: str) -> None:
    """The point was just bound by itself and saved: the rules of the stage set up for that point, which the order did not have when it entered
    the stage (the general ones ran then and are not run again). A task once per stay in the stage (order_task UNIQUE): a second binding makes
    nothing more. A message is only prepared, as always."""
    try:                                                                  # the binding is saved: nothing here may undo or break it
        point = orders.get_card(conn, card_id)["point_id"]
    except Exception:
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the rules of the point were not run for card %s", card_id)
        return
    if point is not None:
        orders._fire_rules(conn, card_id, now, (), of_point=point)


def _bind_point(conn, card_id: int, chat: dict, now: str) -> bool:
    """The client wrote to the Avito account of one point (the owner 2026-10-22): the order of no point is of that point."""
    point = points_store.point_by_account(conn, chat["account_id"])
    return point is not None and claim_point(conn, card_id, point["id"], now, SYSTEM_AUTHOR,
                                             "Привязан к точке «%s» по аккаунту Авито (чат %s)" % (point["name"], ref(chat["id"])), auto=True)


def _account_tag(conn, account_id) -> str:
    """The deal says which Avito account the client wrote to (one pipeline for all accounts; the board filters by this tag)."""
    row = conn.execute("SELECT name FROM account WHERE id = ?", (account_id,)).fetchone()
    return " ".join(str(row[0]).split())[:orders.MAX_TAG] if row else ""


PACK = re.compile(r"-x(\d+)$")
DEFAULT_QTY = 4                                                           # the minimum order: tires are sold by four


def ad_goods(conn, account_id, item_id):
    """(supplier code of the position, pack) behind the Avito ad of a chat, or None when we do not know the ad. The ad's Id is ours:
    `<code>` for a single tire, `<code>-x4` for the set of four, `<code>_<stamp>` for an ad of a mirrored feed."""
    text = str(item_id or "").strip()
    if not text.isdigit():
        return None
    row = conn.execute("SELECT ad_id FROM avito_ad WHERE account_id = ? AND avito_id = ?", (account_id, int(text))).fetchone()
    if not row or not row[0]:
        return None
    from . import mirror_feed
    inherited = mirror_feed.code_for_id(conn, account_id, str(row[0]))      # a lot that took the Id of the oldest ad of its tire
    ad_id = inherited or str(row[0]).split("_")[0]
    pack = PACK.search(ad_id)
    return (PACK.sub("", ad_id), int(pack.group(1)) if pack else 1) if ad_id else None


def _add_position(conn, chat: dict, card_id: int, provider, now: str, author: str) -> None:
    """The position of the ad the client wrote about, priced as the panel prices it today. Any trouble is only noted: the deal stands."""
    traced = ad_goods(conn, chat["account_id"], chat["item_id"])
    if provider is None:
        return
    if traced is None:
        if chat["item_id"]:
            orders.add_event(conn, card_id, now, author, "chat", "Позиция не определена: объявление Авито №%s не найдено среди наших объявлений" % chat["item_id"])
            conn.commit()
        return
    goods_id, pack = traced
    try:
        args = provider(goods_id, pack)
        if args:
            orders.add_item(conn, card_id, qty=DEFAULT_QTY, now=now, author=author, **args)
    except OrderError as err:
        orders.add_event(conn, card_id, now, author, "chat", "Позиция не добавлена: %s" % err)
    conn.commit()


def create_deal(conn, chat_row, now: str, author: str = SYSTEM_AUTHOR, position_provider=None) -> int:
    """A new deal for a chat. A chat that already has one gets no second: the deal it has is returned."""
    chat = chats_store.get_chat(conn, chat_row)
    if chat is None:
        raise OrderError("Такого чата нет.")
    claim = conn.execute("UPDATE avito_chat SET order_id = -1 WHERE id = ? AND order_id IS NULL", (chat_row,))
    conn.commit()
    if claim.rowcount != 1:                                                 # linked already, or another run is creating the deal this moment
        current = chats_store.get_chat(conn, chat_row)["order_id"]
        if current and current > 0:
            return current
        raise OrderError("Заказ для этого чата уже создаётся.")
    try:
        own = own_phones(conn)
        phone = next((p for p in (extract_phone(t, own) for t in _incoming_texts(conn, chat_row)) if p), "")
        comment = "Объявление: %s %s" % (chat["item_title"], chat["item_url"]) if chat["item_title"] else ""
        earlier = earlier_deals(conn, chat)
        name = (chat["peer_name"] or "")[:120]                                                          # the client's own name only; the ad stays in the comment
        tags = ",".join(t for t in (_account_tag(conn, chat["account_id"]), "повторный" if earlier else "") if t)
        card_id = orders.create_card(conn, {"client_name": name, "client_phone": phone, "avito_chat": ref(chat_row), "source": "avito",
                                            "comment": comment.strip(), "tags": tags, "responsible": orders.default_responsible(conn)},
                                    now, author, require_contact=False)
        _attach(conn, chat, card_id, now, author)
        if earlier:
            orders.add_event(conn, card_id, now, author, "chat", "Ранее писал: заказы %s" % ", ".join("№%d" % i for i in earlier))
            conn.commit()
        _add_position(conn, chat, card_id, position_provider, now, author)
    except BaseException:
        conn.rollback()
        conn.execute("UPDATE avito_chat SET order_id = NULL WHERE id = ? AND order_id = -1", (chat_row,))        # give the claim back
        conn.commit()
        raise
    return card_id


def earlier_deals(conn, chat: dict) -> List[int]:
    """The other deals of the same Avito user (any of our accounts): the client wrote before, about another ad."""
    if not chat.get("peer_id"):
        return []
    rows = conn.execute("SELECT DISTINCT order_id FROM avito_chat WHERE peer_id = ? AND id != ? AND order_id > 0 ORDER BY order_id", (chat["peer_id"], chat["id"])).fetchall()
    return [r[0] for r in rows]


def link_chat(conn, chat_row, order_id, now: str, author: str) -> None:
    chat = chats_store.get_chat(conn, chat_row)
    if chat is None:
        raise OrderError("Такого чата нет.")
    orders.get_card(conn, order_id)                                           # raises when there is no such deal
    old = chat["order_id"] if chat["order_id"] and chat["order_id"] > 0 else None
    if old and old != order_id:
        orders.add_event(conn, old, now, author, "chat", "Чат %s перенесён в заказ №%d" % (ref(chat_row), order_id))
    _attach(conn, chat, order_id, now, author)


def unlink_chat(conn, chat_row, now: str, author: str) -> None:
    chat = chats_store.get_chat(conn, chat_row)
    if chat is None:
        raise OrderError("Такого чата нет.")
    conn.execute("UPDATE avito_chat SET order_id = NULL, unlinked = 1 WHERE id = ?", (chat_row,))
    card = None
    if chat["order_id"] and chat["order_id"] > 0:
        try:
            card = orders.get_card(conn, chat["order_id"])
        except OrderError:
            card = None
    if card is not None:
        if card["avito_chat"] == ref(chat_row):
            orders.update_card(conn, card["id"], {"avito_chat": ""}, now, author, commit=False)
        orders.add_event(conn, card["id"], now, author, "chat", "Отвязан чат %s" % ref(chat_row))
    conn.commit()


# ---- the field «Чат Авито» of an order made by hand (the owner 2026-10-21) --------------------------------------------------------

AVITO_CHAT_PAGE = re.compile(r"^https://(?:www\.|m\.)?avito\.ru/[^\s?#]*/channel/([A-Za-z0-9_~.:-]{1,120})/?(?:[?#]\S*)?$")


def chat_of_field(conn, text) -> Optional[int]:
    """The chat the field «Чат Авито» names: our number «Ч-2346» or Avito's page of the chat (…/messenger/channel/<chat id>); None when it
    names no chat of the panel."""
    text = str(text or "").strip()
    found = orders.CHAT_REF.match(text)
    if found:
        row = conn.execute("SELECT id FROM avito_chat WHERE id = ?", (int(found.group(1)),)).fetchone()
        return row[0] if row else None
    found = AVITO_CHAT_PAGE.match(text)
    rows = conn.execute("SELECT id FROM avito_chat WHERE chat_id = ?", (found.group(1),)).fetchall() if found else []
    return rows[0][0] if len(rows) == 1 else None


def phone_chats(conn, card: dict) -> List[int]:
    """The free chats of the client with the order's phone (the client card joins them), newest first: offered, never bound by themselves.
    A chat the owner unlinked is left alone."""
    phone = clients.normalize(card.get("client_phone"))
    if not phone:
        return []
    return [r[0] for r in conn.execute(
        "SELECT c.id FROM client_phone p JOIN client_chat l ON l.client_id = p.client_id JOIN avito_chat c ON c.id = l.chat_row "
        "WHERE p.phone = ? AND (c.order_id IS NULL OR c.order_id = 0) AND c.unlinked = 0 ORDER BY c.last_at DESC, c.id DESC", (phone,))]


def _audit(conn, now: str, author: str, action: str, detail: dict) -> None:
    conn.execute("INSERT INTO audit (at, author, action, detail) VALUES (?,?,?,?)", (now, author, action, json.dumps(detail, ensure_ascii=False)))


def bind_chat(conn, card_id: int, chat_row: int, how: str, now: str, author: str, move: bool = False) -> dict:
    """Bind a chat to an order by a person: {"state": "linked" | "same" | "other" | "busy" | "missing", "chat": row, "order": the other order}.
    A chat of another order moves only with `move` (a press of «Перепривязать сюда»); a free chat is taken by one UPDATE, so a chat bound
    elsewhere meanwhile is never moved silently. how: field / phone (what named the chat) for the audit."""
    chat = chats_store.get_chat(conn, chat_row)
    if chat is None:
        return {"state": "missing", "chat": chat_row}
    orders.get_card(conn, card_id)
    current = chat["order_id"]
    if current == card_id:
        return {"state": "same", "chat": chat_row}
    if current == -1:
        return {"state": "busy", "chat": chat_row}
    if current and current > 0 and not move:
        return {"state": "other", "chat": chat_row, "order": current}
    conn.execute("SAVEPOINT chat_bind")                                  # a failed take undoes only its own rows, never the caller's
    if current and current > 0:
        orders.add_event(conn, current, now, author, "chat", "Чат %s перенесён в заказ №%d" % (ref(chat_row), card_id))
        old = orders.get_card(conn, current)
        if chat_of_field(conn, old["avito_chat"]) == chat_row:           # the old order no longer names the chat (it would call it back)
            orders.update_card(conn, current, {"avito_chat": ""}, now, author, commit=False)
        detail = {"order": card_id, "chat": chat_row, "how": "moved", "from": current}
        taken = conn.execute("UPDATE avito_chat SET order_id = ? WHERE id = ? AND order_id = ?", (card_id, chat_row, current)).rowcount
    else:
        detail = {"order": card_id, "chat": chat_row, "how": how}
        taken = conn.execute("UPDATE avito_chat SET order_id = ? WHERE id = ? AND (order_id IS NULL OR order_id = 0)", (card_id, chat_row)).rowcount
    if not taken:                                                         # bound elsewhere between the read and now: say so, move nothing
        conn.execute("ROLLBACK TO chat_bind")
        conn.execute("RELEASE chat_bind")
        again = chats_store.get_chat(conn, chat_row)
        owner = again["order_id"] if again else None
        return {"state": "other", "chat": chat_row, "order": owner} if owner and owner > 0 else {"state": "busy", "chat": chat_row}
    try:                                                                  # the take, its audit and its journal record are saved together
        _audit(conn, now, author, "order_chat_link", detail)
        _attach(conn, chat, card_id, now, author)                         # the event, the field, the client; its commit closes the savepoint
    except Exception:
        conn.execute("ROLLBACK TO chat_bind")
        conn.execute("RELEASE chat_bind")
        raise
    return {"state": "linked", "chat": chat_row}


def unbind_chat(conn, card_id: int, chat_row: int, now: str, author: str) -> bool:
    """Unlink a chat of this order (a chat of another order is not touched). True when unlinked."""
    chat = chats_store.get_chat(conn, chat_row)
    if chat is None or chat["order_id"] != card_id:
        return False
    _audit(conn, now, author, "order_chat_unlink", {"order": card_id, "chat": chat_row})
    field = orders.get_card(conn, card_id)["avito_chat"]
    if field != ref(chat_row) and chat_of_field(conn, field) == chat_row:   # an Avito link of the chat: it would offer the chat back at once
        orders.update_card(conn, card_id, {"avito_chat": ""}, now, author, commit=False)
    unlink_chat(conn, chat_row, now, author)                              # commits
    return True


def merge_chats(conn, from_order, into_order, now: str, author: str) -> int:
    """All chats of one deal move to another (for the duplicate mark). The first deal itself stays for the owner to close."""
    source, target = orders.get_card(conn, from_order), orders.get_card(conn, into_order)
    if source["id"] == target["id"]:
        raise OrderError("Выберите другой заказ.")
    moved = [{"id": r[0], "account_id": r[1]} for r in conn.execute("SELECT id, account_id FROM avito_chat WHERE order_id = ? ORDER BY id", (from_order,))]
    rows = [c["id"] for c in moved]
    conn.execute("UPDATE avito_chat SET order_id = ? WHERE order_id = ?", (into_order, from_order))
    if rows and not target["avito_chat"]:
        orders.update_card(conn, into_order, {"avito_chat": ref(rows[0])}, now, author, commit=False)
    bound = False
    for chat in moved if not target["point_id"] else ():                 # by the same rules; the first chat of a point's account decides
        if _bind_point(conn, into_order, chat, now):
            bound = True
            break
    if source["avito_chat"]:
        orders.update_card(conn, from_order, {"avito_chat": ""}, now, author, commit=False)
    orders.add_event(conn, from_order, now, author, "chat", "Чаты (%d) перенесены в заказ №%d" % (len(rows), into_order))
    orders.add_event(conn, into_order, now, author, "chat", "Получены чаты (%d) из заказа №%d" % (len(rows), from_order))
    conn.commit()
    if bound:
        fire_point_rules(conn, into_order, now)
    return len(rows)


def _note_written_again(conn, card_id: int, last_in: str, now: str) -> None:
    text = "Клиент написал после закрытия заказа (%s)" % last_in[:16].replace("T", " ")
    before = "Клиент написал после закрытия сделки (%s)" % last_in[:16].replace("T", " ")          # the same note, written before the word changed
    if not conn.execute("SELECT 1 FROM order_event WHERE order_id = ? AND text IN (?, ?)", (card_id, text, before)).fetchone():
        orders.add_event(conn, card_id, now, SYSTEM_AUTHOR, "chat", text)
        conn.commit()


def repair_claims(conn, now: str) -> int:
    """A deal claim (order_id = -1) that a killed process left. One seen in two runs in a row is repaired: the deal made for the chat is
    attached, otherwise the chat is freed. (A claim made right now by the panel is gone in a moment, so it is never seen twice.)"""
    import json
    stuck = [r[0] for r in conn.execute("SELECT id FROM avito_chat WHERE order_id = -1")]
    seen = set(json.loads(store.get_state(conn, "chats_stale_claims") or "[]"))
    fixed = 0
    for chat_row in stuck:
        if chat_row not in seen:
            continue
        card = conn.execute("SELECT id FROM order_card WHERE avito_chat = ? ORDER BY id DESC LIMIT 1", (ref(chat_row),)).fetchone()
        conn.execute("UPDATE avito_chat SET order_id = ? WHERE id = ? AND order_id = -1", (card[0] if card else None, chat_row))
        fixed += 1
    store.set_state(conn, "chats_stale_claims", json.dumps([c for c in stuck if c not in seen]))
    conn.commit()
    return fixed


def process_all(conn, now: str, position_provider=None, auto_create: bool = False) -> dict:
    """For every chat that is not unlinked: keep the phone and the «written again» note of its deal up to date. A deal is made for a chat only by the
    manager (the button in «Чаты»); `auto_create` (off, owner's decision 2026-10-15) would make one for every fresh first message.

    With `auto_create`: a chat that was already there when its account was first read (the past) gets a deal only when its client writes again.
    A chat whose deal is closed gets no new deal: the dialog goes on in it and a note says the client wrote again."""
    stats = {"created": 0, "linked": 0, "phones": 0}
    repair_claims(conn, now)
    own = own_phones(conn)
    for chat in chats_store.list_chats(conn):
        if chat["unlinked"] or chat["order_id"] == -1:                       # unlinked by the owner, or a deal is being made right now
            continue
        try:
            texts = _incoming_texts(conn, chat["id"])
            phone = next((p for p in (extract_phone(t, own) for t in texts) if p), "")
            last_in = conn.execute("SELECT MAX(at) FROM avito_message WHERE chat_row = ? AND direction = 'in' AND kind = 'text'", (chat["id"],)).fetchone()[0] or ""
            baseline = store.get_state(conn, "chats_baseline_%d" % chat["account_id"]) or ""
            fresh = bool(last_in) and (not baseline or last_in >= baseline)
            if chat["order_id"]:
                try:
                    card = orders.get_card(conn, chat["order_id"])
                except OrderError:
                    conn.execute("UPDATE avito_chat SET order_id = NULL WHERE id = ?", (chat["id"],))
                    conn.commit()
                    continue
                if card["stage"]["kind"] != "open" and last_in and last_in > card["entered_at"]:
                    # the client wrote again about the same ad after the deal was closed: no new deal, the dialog goes on in this one
                    _note_written_again(conn, card["id"], last_in, now)
                if phone and not card["client_phone"]:
                    orders.update_card(conn, card["id"], {"client_phone": phone}, now, SYSTEM_AUTHOR)
                    stats["phones"] += 1
                continue
            if not texts or not fresh:
                continue                                                      # only our own words, or an old chat nobody wrote to since
            if not auto_create:
                continue
            create_deal(conn, chat["id"], now, position_provider=position_provider)       # a deal for every chat: every ad of a client has its own
            stats["created"] += 1
        except Exception:                                                    # one bad chat must not stop the others
            conn.rollback()
            log.exception("binding of chat %s failed", chat["id"])
    return stats


SAME_LINE = ("brand", "model", "code", "width", "profile", "radius", "season", "studded", "qty")


def wait_from_chat(conn, chat_row, fields: dict, offers, now: str, author: str) -> int:
    """«Клиент ждёт эту шину»: the deal of the chat (made when there is none), a line «Что нужно клиенту» and the stage «Ожидает наличие».
    The form and the deal are checked first, so a bad one makes nothing; the same line pressed again is not added twice. Returns (id of the deal, a line was added,
    the deal was moved to the stage). A deal that is already further on (placed, moving, at the point) is never taken back."""
    chat = chats_store.get_chat(conn, chat_row)
    if chat is None:
        raise OrderError("Такого чата нет.")
    line = order_wait.resolve_line(offers, fields)
    clean = order_wait._clean(line)                                            # raises for a bad form before anything is made
    if chat["order_id"] and chat["order_id"] > 0:
        card_id = chat["order_id"]
        card = orders.get_card(conn, card_id)
        if card["stage"]["kind"] != "open":
            raise OrderError("Заказ по этому чату закрыт (этап «%s»): откройте его заново или создайте новый." % card["stage"]["name"])
        if card["stage"]["code"] not in ("new", "talk", "wait"):
            raise OrderError("Заказ уже в работе (этап «%s»): кнопка не возвращает его назад. Добавьте строку во вкладке «Что нужно» или создайте новый заказ." % card["stage"]["name"])
        if card["sale_ids"]:
            raise OrderError("По заказу записана продажа: сначала сторнируйте её.")
        if not (card["client_name"] or card["client_phone"]):
            raise OrderError("В заказе нет ни имени, ни телефона клиента: заполните их в карточке.")
    else:
        if not chat["peer_name"]:
            raise OrderError("У чата нет имени клиента: создайте заказ вручную и укажите клиента.")
        card_id = create_deal(conn, chat_row, now, author)
        card = orders.get_card(conn, card_id)
    same = [w for w in order_wait.list_wants(conn, card_id)
            if all(w[k] == clean[k] for k in SAME_LINE) and w["any_brand"] == bool(clean["any_brand"]) and w["any_size"] == bool(clean["any_size"])]
    if not same:
        order_wait.add_want(conn, card_id, line, now, author)
    stage = conn.execute("SELECT id FROM order_stage WHERE code = 'wait'").fetchone()
    if stage is None:
        raise OrderError("В воронке нет этапа «Ожидает наличие».")
    moved = orders.move_card(conn, card_id, stage[0], now, author)
    return card_id, not same, moved
