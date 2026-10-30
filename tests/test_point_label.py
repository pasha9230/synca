"""The orders a point only receives and hands over (the owner 2026-10-22): the manager of a point sees the orders of the own point only —
never one of no point, on no page — and an order of the point that did not come from the point's own Avito account carries a badge with
the name of the Avito account it came from, «Без аккаунта» without a chat (the card, the board, «Рабочее место»); the chat of another account is not shown in the
card, nor the client's words in the journal and in the alerts. The owner, the partner and the manager of the chats see everything as before."""
from __future__ import annotations

import pytest

from test_chats_pages import NOW
from test_point_rights import bind, world
from test_roles import person
from tires_sync import chats, orders, users


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


LABEL = "data-origin"


def chat_of(conn, account, order_id, chat_id) -> int:
    row = conn.execute("INSERT INTO avito_chat (account_id, chat_id, peer_id, order_id) VALUES (?, ?, ?, ?)",
                       (account, chat_id, "p-" + chat_id, order_id)).lastrowid
    conn.commit()
    return row


def stage(conn, card_id, code):
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (next(s["id"] for s in orders.list_stages(conn) if s["code"] == code), card_id))
    conn.commit()


@pytest.fixture
def w(tmp_path):
    w = world(tmp_path)
    conn = w["conn"]
    w["main_name"] = conn.execute("SELECT name FROM account WHERE id = ?", (w["main"],)).fetchone()[0]
    conn.execute("UPDATE avito_chat SET order_id = ? WHERE id = ?", (w["mine"], w["petr"]["id"]))     # «Свой клиент»: the point's account
    w["away"] = orders.create_card(conn, {"client_name": "Клиент Основного", "point_id": str(w["rust"])}, NOW, "o")
    w["away_chat"] = chat_of(conn, w["main"], w["away"], "c-away")                                     # the chat of «Основной»
    w["bare"] = orders.create_card(conn, {"client_name": "Клиент Без Чата", "point_id": str(w["rust"])}, NOW, "o")
    w["free"] = orders.create_card(conn, {"client_name": "Ничей Клиент", "client_phone": "+7 900 333-44-55", "webmim_number": "6600001"},
                                   NOW, "o")
    orders.add_event(conn, w["free"], NOW, "система", "point", "Красная запись заказа №%d" % w["free"], level="red")
    conn.commit()
    for card in (w["mine"], w["away"], w["bare"], w["free"]):
        stage(conn, card, "placed")
    return w


def text(client, url) -> str:
    response = client.get(url)
    assert response.status_code == 200, url
    return response.get_data(as_text=True)


# --- an order of no point: no point's, on no page --------------------------------------------------------------------------------------

def test_an_order_of_no_point_is_seen_by_no_point_on_any_page(w):
    bind(w)
    conn = w["conn"]
    chat_of(conn, w["rust_acc"], w["free"], "c-free")                    # even with a chat of the point's account
    manager, _ = person(w["app"], "point", w["rust"])
    for view in ("/orders", "/orders?v=list", "/my-point", "/alerts"):
        assert "Ничей Клиент" not in text(manager, view) and "Красная запись заказа №%d" % w["free"] not in text(manager, view), view
    assert manager.get("/orders/%d" % w["free"]).status_code == 403
    found = " ".join(i["title"] for q in ("Ничей", "6600001", "+7 900 333-44-55", "№%d" % w["free"])
                     for g in manager.get("/search.json", query_string={"q": q}).get_json()["groups"] for i in g["items"])
    assert "Ничей Клиент" not in found and "Заказ №%d" % w["free"] not in found
    assert not chats.find(conn, "6600001", point=w["rust"]) & {r[0] for r in conn.execute("SELECT id FROM avito_chat WHERE order_id = ?", (w["free"],))}
    assert w["free"] not in {i["order_id"] for i in manager.get("/alerts/feed").get_json()["items"]}
    event = conn.execute("SELECT id FROM order_event WHERE order_id = ? AND level = 'red'", (w["free"],)).fetchone()[0]
    assert manager.post("/alerts/%d/ack" % event, data={"csrf": "tok"}).status_code == 403
    assert manager.post("/orders/%d/note" % w["free"], data={"csrf": "tok", "text": "x"}).status_code == 403


def test_the_excel_file_of_a_point_has_no_order_of_no_point(w):
    pytest.importorskip("openpyxl")
    import io
    import openpyxl
    manager, _ = person(w["app"], "point", w["rust"])
    book = openpyxl.load_workbook(io.BytesIO(manager.get("/orders/export.xlsx").data))
    cells = " ".join(str(c.value) for row in book.active.iter_rows() for c in row if c.value is not None)
    assert "Свой клиент" in cells and "Ничей Клиент" not in cells and "Чужой клиент" not in cells


# --- the badge of the account ----------------------------------------------------------------------------------------------------

def test_origin_labels_name_the_account_an_order_came_from(w):
    bind(w)
    conn = w["conn"]
    labels = orders.origin_labels(conn, w["rust"], [w["mine"], w["away"], w["bare"]])
    assert w["mine"] not in labels                                                              # a chat of the point's account
    assert labels[w["away"]] == {"account": "", "text": orders.ORIGIN_OTHER, "hint": orders.ORIGIN_HINT}   # never the name (2026-10-29)
    assert labels[w["bare"]] == {"account": "", "text": "Без аккаунта", "hint": orders.ORIGIN_HINT}
    chat_of(conn, w["rust_acc"], w["away"], "c-away-own")                                       # a chat of the point's account too: its own
    assert w["away"] not in orders.origin_labels(conn, w["rust"], [w["away"]])
    assert orders.origin_labels(conn, w["rust"], []) == {}


def test_a_point_sees_the_label_on_the_card_the_board_and_my_point_its_own_orders_have_none(w):
    bind(w)
    manager, _ = person(w["app"], "point", w["rust"])
    page = "".join(text(manager, "/my-point?tab=" + t) for t in ("here", "moving", "oth", "return"))    # «Рабочее место»: our orders, each on its tab
    assert orders.ORIGIN_OTHER in page and "Без аккаунта" in page and w["main_name"] not in page           # no account's name (2026-10-29)
    assert page.count(LABEL) == 2                                                           # «Свой клиент» is «Заказы»'s (2026-10-30)
    for url in ("/orders", "/orders?v=list"):                   # «Заказы» of a point: its own sales only (the owner 2026-10-30), no label
        page = text(manager, url)
        assert 'href="/orders/%d"' % w["mine"] in page and 'href="/orders/%d"' % w["away"] not in page and LABEL not in page, url
    away = text(manager, "/orders/%d" % w["away"])
    assert ">%s</span>" % orders.ORIGIN_OTHER in away and orders.ORIGIN_HINT in away and w["main_name"] not in away
    assert ">Без аккаунта</span>" in text(manager, "/orders/%d" % w["bare"])
    assert LABEL not in text(manager, "/orders/%d" % w["mine"])


def test_a_point_without_an_avito_account_sees_every_order_labelled(w):
    manager, _ = person(w["app"], "point", w["rust"])                     # not bound: the point has no account of its own
    page = text(manager, "/orders/%d" % w["mine"])
    assert 'data-origin>%s</span>' % orders.ORIGIN_OTHER in page and 'data-origin>Альфа</span>' not in page   # never the account's name


def test_the_other_roles_see_no_label(w):
    bind(w)
    for role in ("owner", "partner", "chats"):
        client = w["owner"] if role == "owner" else person(w["app"], role)[0]
        for url in ("/orders/%d" % w["away"], "/orders", "/orders?v=list") + (("/my-point?p=%d" % w["rust"],) if role == "owner" else ()):
            assert LABEL not in text(client, url), (role, url)


# --- the chat of another account in the card ----------------------------------------------------------------------------------------

def test_the_chat_of_another_account_and_the_clients_words_are_hidden_from_a_point(w):
    bind(w)
    conn = w["conn"]
    orders.add_event(conn, w["away"], NOW, chats.CLIENT_WROTE_AUTHOR, "chat", "Клиент написал: секретные слова", level="yellow")
    orders.add_event(conn, w["away"], NOW, chats.CLIENT_WROTE_AUTHOR, "chat", "Клиент звонил: тайный звонок", level="yellow")
    orders.update_card(conn, w["away"], {"avito_chat": "Ч-%d" % w["away_chat"]}, NOW, "o")
    conn.commit()
    manager, _ = person(w["app"], "point", w["rust"])
    for page in (text(manager, "/alerts"), str(manager.get("/alerts/feed").get_json())):            # before the card: it marks them seen
        assert "секретные слова" not in page and "тайный звонок" not in page
    card = text(manager, "/orders/%d" % w["away"])
    assert 'href="/chats/%d"' % w["away_chat"] not in card and "Чаты Авито" not in card
    assert "секретные слова" not in card and "тайный звонок" not in card
    assert "тайный звонок" in text(w["owner"], "/alerts")              # the owner's alerts as before (one row per order: the newest record)
    for role in ("owner", "partner", "chats"):                           # nobody else: as before
        client = w["owner"] if role == "owner" else person(w["app"], role)[0]
        page = text(client, "/orders/%d" % w["away"])
        assert 'href="/chats/%d"' % w["away_chat"] in page and "секретные слова" in page, role


def test_the_chat_of_the_points_own_account_and_its_words_are_shown(w):
    bind(w)
    conn = w["conn"]
    orders.add_event(conn, w["mine"], NOW, chats.CLIENT_WROTE_AUTHOR, "chat", "Клиент написал: свои слова", level="yellow")
    conn.commit()
    manager, _ = person(w["app"], "point", w["rust"])
    assert "свои слова" in text(manager, "/alerts")                                              # before the card: it marks them seen
    card = text(manager, "/orders/%d" % w["mine"])
    assert 'href="/chats/%d"' % w["petr"]["id"] in card and "свои слова" in card


def test_the_chat_of_another_account_named_in_the_field_is_not_offered_to_a_point(w):
    bind(w)
    conn = w["conn"]
    ivan = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Иван")                    # «Основной», no order
    manager, _ = person(w["app"], "point", w["rust"])
    for field, said in (("Ч-%d" % ivan["id"], "Найден чат"), ("Ч-%d" % w["tanya"]["id"], "уже в заказе №")):
        orders.update_card(conn, w["bare"], {"avito_chat": field}, NOW, "o")
        conn.commit()
        assert said in text(w["owner"], "/orders/%d" % w["bare"]), field                         # the owner: as before
        page = text(manager, "/orders/%d" % w["bare"])
        assert said not in page and 'href="/chats/%d"' % ivan["id"] not in page, field


def test_the_message_to_the_client_through_a_chat_of_another_account_is_not_offered_to_a_point(tmp_path):
    from test_order_wait_pages import chat_with_order
    from tires_sync import order_points as points
    client, conn, card_id, chat = chat_with_order(tmp_path)                                         # the chat of «Основной»
    rust = points.save_point(conn, {"name": "Альфа", "manager_name": "А", "channel": "vk"})
    orders.update_card(conn, card_id, {"point_id": str(rust)}, NOW, "o")
    conn.commit()
    assert 'action="/chats/%d/send"' % chat["id"] in text(client, "/orders/%d" % card_id)        # the owner: as before
    manager, _ = person(client.application, "point", rust)
    page = text(manager, "/orders/%d" % card_id)
    assert 'action="/chats/%d/send"' % chat["id"] not in page and "уйдёт в чат Ч-%d" % chat["id"] not in page
