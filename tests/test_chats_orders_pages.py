"""The pages of the owner's 2026-10-29 decisions: «Ответственный» of an account (its form, «Чаты» by default), the reason of «Отмена /
невыкуп» on the card and through the question «на ОТХ?», a red line of the history, the rows of the orders opened by a click anywhere."""
from __future__ import annotations

import re

from panel_helpers import post
from test_chats_pages import NOW, setup
from tires_sync import connections, orders, users


def test_the_account_page_saves_its_responsible_from_the_staff_only(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    users.add_user(conn, "masha", "pass-word-1", NOW, role="chats")
    users.add_user(conn, "buyer", "pass-word-1", NOW, role="client")
    page = client.get("/connections/avito/%d" % account).get_data(as_text=True)
    form = page.split('id="responsible"')[1].split("</section>")[0]
    assert '<option value="masha"' in form and '<option value="buyer"' not in form and "Ответственный" in form
    post(client, "/connections/accounts/%d/responsible" % account, tok, responsible="buyer")
    assert connections.get_responsible(conn, account) == ""                                # a client is refused
    post(client, "/connections/accounts/%d/responsible" % account, tok, responsible="masha")
    assert connections.get_responsible(conn, account) == "masha"


def test_the_chats_open_the_account_of_its_responsible_and_all_on_asking(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    other = connections.create_account(conn, "Второй", NOW, "o")
    connections.set_modes(conn, other, True, True, NOW, "o")
    page = client.get("/chats").get_data(as_text=True)
    assert "Аккаунт: Все аккаунты" in page
    connections.set_responsible(conn, account, "owner", ["owner"], NOW, "owner")
    page = client.get("/chats").get_data(as_text=True)
    assert "Аккаунт: Основной" in page and "Татьяна" in page
    assert "acc=all" in page                                                              # «Все аккаунты» keeps «all» over the default
    page = client.get("/chats?acc=all").get_data(as_text=True)
    assert "Аккаунт: Все аккаунты" in page and 'name="acc" value="all"' in page


def test_a_manual_order_with_the_chat_of_the_account_gets_its_responsible(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    users.add_user(conn, "masha", "pass-word-1", NOW, role="chats")
    connections.set_responsible(conn, account, "masha", ["masha"], NOW, "owner")
    tanya = next(r for r in conn.execute("SELECT id FROM avito_chat WHERE peer_name = 'Татьяна'"))[0]
    conn.execute("UPDATE avito_chat SET order_id = NULL WHERE id = ?", (tanya,))
    conn.execute("DELETE FROM order_card")
    conn.commit()
    post(client, "/orders/new", tok, client_name="Татьяна", client_phone="+7 900 000-11-22", avito_chat="Ч-%d" % tanya, responsible="owner")
    card = conn.execute("SELECT id, responsible FROM order_card ORDER BY id DESC").fetchone()
    assert card[1] == "masha"
    post(client, "/orders/new", tok, client_name="Пётр", client_phone="+7 900 000-11-33", responsible="owner")
    assert conn.execute("SELECT responsible FROM order_card ORDER BY id DESC").fetchone()[0] == "owner"   # no chat of the account: as typed


def test_a_manual_order_with_the_chat_of_a_points_account_gets_the_points_manager(tmp_path):
    """The owner 2026-10-30: the manager of the point comes before the account's responsible; the person picked by hand stays."""
    from tires_sync import order_points as points
    app, client, tok, conn, account = setup(tmp_path)
    point = points.save_point(conn, {"name": "Юнона", "manager_name": "А", "channel": "panel", "avito_account_id": account})
    users.add_user(conn, "masha", "pass-word-1", NOW, role="chats")
    users.add_user(conn, "yun", "pass-word-1", NOW, role="point", point_id=point)
    connections.set_responsible(conn, account, "masha", ["masha"], NOW, "owner")
    tanya = next(r for r in conn.execute("SELECT id FROM avito_chat WHERE peer_name = 'Татьяна'"))[0]
    conn.execute("UPDATE avito_chat SET order_id = NULL WHERE id = ?", (tanya,))
    conn.execute("DELETE FROM order_card")
    conn.commit()
    post(client, "/orders/new", tok, client_name="Татьяна", client_phone="+7 900 000-11-22", avito_chat="Ч-%d" % tanya, responsible="owner")
    assert conn.execute("SELECT responsible FROM order_card ORDER BY id DESC").fetchone()[0] == "yun"
    conn.execute("UPDATE avito_chat SET order_id = NULL WHERE id = ?", (tanya,))
    conn.execute("DELETE FROM order_card")
    conn.commit()
    post(client, "/orders/new", tok, client_name="Татьяна", client_phone="+7 900 000-11-22", avito_chat="Ч-%d" % tanya, responsible="masha")
    assert conn.execute("SELECT responsible FROM order_card ORDER BY id DESC").fetchone()[0] == "masha"   # picked by hand: as typed


def stage_id(conn, code):
    return conn.execute("SELECT id FROM order_stage WHERE code = ?", (code,)).fetchone()[0]


def test_the_cancel_window_asks_the_reason_and_saves_it(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    card = orders.create_card(conn, {"client_name": "Иван", "client_phone": "+7 900 000-22-33"}, NOW, "owner")
    page = client.get("/orders/%d" % card).get_data(as_text=True)
    assert 'name="cancel_kind" value="не выкупил"' in page and 'value="другое"' in page
    post(client, "/orders/%d/move" % card, tok, stage_id=str(stage_id(conn, "cancel")), cancel_kind="другое", cancel_reason="")
    assert orders.get_card(conn, card)["stage"]["code"] == "new"                           # «другое» needs a text
    post(client, "/orders/%d/move" % card, tok, stage_id=str(stage_id(conn, "cancel")), cancel_kind="не выкупил", cancel_reason="не пришёл")
    got = orders.get_card(conn, card)
    assert got["stage"]["code"] == "cancel" and got["cancel_reason"] == "не выкупил: не пришёл"


def test_the_question_at_the_point_carries_the_chosen_reason(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    card = orders.create_card(conn, {"client_name": "Иван", "client_phone": "+7 900 000-22-44"}, NOW, "owner")
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage_id(conn, "at_point"), card))
    conn.commit()
    post(client, "/orders/%d/move" % card, tok, stage_id=str(stage_id(conn, "cancel")), cancel_kind="отменил клиент", cancel_reason="")
    page = client.get("/orders/%d" % card).get_data(as_text=True)
    assert "Шины остаются на точке" in page and 'name="cancel_reason" value="отменил клиент"' in page
    post(client, "/orders/%d/move" % card, tok, stage_id=str(stage_id(conn, "cancel")), oth_answer="no", cancel_reason="отменил клиент")
    assert orders.get_card(conn, card)["cancel_reason"] == "отменил клиент"


def test_a_message_not_sent_is_red_in_the_history_with_its_reason(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    card = orders.create_card(conn, {"client_name": "Иван"}, NOW, "owner")
    orders.add_event(conn, card, NOW, "система", "chat", "Сообщение клиенту не отправлено («Клиенту: заказ оформлен»): у заказа нет чата Авито", level="red")
    orders.add_event(conn, card, NOW, "owner", "note", "обычная заметка")
    conn.commit()
    page = client.get("/orders/%d" % card).get_data(as_text=True)
    red = re.findall(r'<li class="[^"]*is-red[^"]*" data-red>(.*?)</li>', page, re.S)
    assert len(red) == 1 and "у заказа нет чата Авито" in red[0]


def test_the_rows_of_orders_open_by_a_click_anywhere(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    card = orders.create_card(conn, {"client_name": "Иван", "client_phone": "+7 900 000-55-66"}, NOW, "owner")
    board = client.get("/orders").get_data(as_text=True)
    assert 'class="ocard' in board and 'href="/orders/%d"' % card in board                  # the board: the card is a link
    listing = client.get("/orders?v=list").get_data(as_text=True)
    assert 'data-href="/orders/%d" class="is-link"' % card in listing
    client_id = conn.execute("SELECT client_id FROM client_order WHERE order_id = ?", (card,)).fetchone()
    assert client_id is not None
    page = client.get("/clients/%d" % client_id[0]).get_data(as_text=True)
    assert 'data-href="/orders/%d" class="is-link"' % card in page
    js = open(app.static_folder + "/app.js", encoding="utf-8").read()
    assert 't.closest("[data-href]")' in js


def test_all_the_accounts_stay_chosen_through_the_filters_and_the_list(tmp_path):
    """D4: «Все аккаунты» over the person's own account is kept by the filter of the state and by the links of the list."""
    app, client, tok, conn, account = setup(tmp_path)
    other = connections.create_account(conn, "Второй", NOW, "o")
    connections.set_modes(conn, other, True, True, NOW, "o")
    connections.set_responsible(conn, account, "owner", ["owner"], NOW, "owner")
    page = client.get("/chats?acc=all").get_data(as_text=True)
    state_links = re.findall(r'href="(/chats\?f=[^"]*)"', page)
    assert state_links and all("acc=all" in h for h in state_links)
    assert 'data-place="?acc=all"' in page

