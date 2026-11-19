"""«Цепочка заказов» (the owner 2026-11-19, task #83): the orders of one client tied, not merged; «Добавить заказ» makes a tied second order."""
from __future__ import annotations

import pytest

from tires_sync import order_chain as chain, orders, store

NOW = "2026-11-19T12:00:00"
PHONE = "+7 900 111-22-33"


def db(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    return conn


def card(conn, phone=PHONE, name="Иван", **extra):
    return orders.create_card(conn, dict(client_name=name, client_phone=phone, **extra), NOW, "o", many_open=True)


def ids(conn, order_id):
    return [o["id"] for o in chain.others(conn, order_id)]


def texts(conn, order_id):
    return [e["text"] for e in orders.list_events(conn, order_id)]


def test_two_orders_tied_name_each_other_and_stay_two_cards(tmp_path):
    conn = db(tmp_path)
    a, b = card(conn), card(conn)
    conn.execute("UPDATE order_card SET webmim_number = '5367654' WHERE id = ?", (b,))
    chain.link(conn, a, b, NOW, "o")
    assert ids(conn, a) == [b] and ids(conn, b) == [a]
    assert chain.others(conn, a)[0]["label"] == "5367654" and chain.others(conn, b)[0]["label"] == str(a)
    assert "Связан с заказом №5367654 (цепочка заказов клиента)" in texts(conn, a)
    assert "Связан с заказом №%d (цепочка заказов клиента)" % a in texts(conn, b)
    assert conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 2


def test_tying_into_a_chain_joins_the_whole_chain_and_two_chains_become_one(tmp_path):
    conn = db(tmp_path)
    a, b, c, d = (card(conn) for _ in range(4))
    chain.link(conn, a, b, NOW, "o")
    chain.link(conn, c, d, NOW, "o")
    chain.link(conn, b, d, NOW, "o")
    assert ids(conn, a) == [b, c, d] and ids(conn, d) == [a, b, c]


def test_the_same_order_or_one_already_in_the_chain_is_refused(tmp_path):
    conn = db(tmp_path)
    a, b = card(conn), card(conn)
    with pytest.raises(orders.OrderError, match="тот же заказ"):
        chain.link(conn, a, a, NOW, "o")
    chain.link(conn, a, b, NOW, "o")
    with pytest.raises(orders.OrderError, match="уже в этой цепочке"):
        chain.link(conn, b, a, NOW, "o")
    with pytest.raises(orders.OrderError):
        chain.link(conn, a, 999, NOW, "o")


def test_untying_leaves_the_rest_and_a_chain_of_one_is_no_chain(tmp_path):
    conn = db(tmp_path)
    a, b, c = card(conn), card(conn), card(conn)
    chain.link(conn, a, b, NOW, "o")
    chain.link(conn, a, c, NOW, "o")
    assert chain.unlink(conn, c, NOW, "o") is True
    assert ids(conn, a) == [b] and ids(conn, c) == []
    assert chain.unlink(conn, b, NOW, "o") is True
    assert ids(conn, a) == [] and conn.execute("SELECT COUNT(*) FROM order_chain").fetchone()[0] == 0
    assert chain.unlink(conn, b, NOW, "o") is False
    assert "Заказ №%d отвязан от цепочки" % c in texts(conn, a)


def test_find_takes_webmim_s_number_first_then_the_panel_s(tmp_path):
    conn = db(tmp_path)
    a, b = card(conn), card(conn)
    conn.execute("UPDATE order_card SET webmim_number = ? WHERE id = ?", (str(a), b))     # a WebMIM number equal to a panel number
    assert chain.find(conn, "№ %d" % a) == b and chain.find(conn, str(b)) == b
    assert chain.find(conn, "") is None and chain.find(conn, "нет такого") is None


def test_a_deleted_order_drops_out_of_the_chain(tmp_path):
    conn = db(tmp_path)
    a, b = card(conn), card(conn)
    chain.link(conn, a, b, NOW, "o")
    conn.execute("DELETE FROM order_card WHERE id = ?", (b,))
    assert ids(conn, a) == []


def test_add_order_copies_the_client_ties_it_and_passes_the_one_open_order_guard(tmp_path):
    conn = db(tmp_path)
    first = orders.create_card(conn, dict(client_name="Иван", client_phone=PHONE, responsible="manager", source="avito"), NOW, "o")
    with pytest.raises(orders.OpenOrderExists):                    # the guard of an accidental double stays on the usual way
        orders.create_card(conn, dict(client_name="Иван", client_phone=PHONE), NOW, "o")
    new = chain.again(conn, first, NOW, "o")
    made = orders.get_card(conn, new)
    assert (made["client_name"], made["client_phone"], made["responsible"], made["source"]) == ("Иван", PHONE, "manager", "avito")
    assert made["items"] == [] and made["stage"]["kind"] == "open"
    assert ids(conn, first) == [new] and ids(conn, new) == [first]
    assert "Создан из заказа №%d: тот же клиент" % first in texts(conn, new)


def test_add_order_after_a_closed_one(tmp_path):
    conn = db(tmp_path)
    first = card(conn)
    done = next(s for s in orders.list_stages(conn) if s["code"] == "done")
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (done["id"], first))     # closed (the way there is not what is tested)
    new = chain.again(conn, first, NOW, "o")
    assert ids(conn, new) == [first] and orders.get_card(conn, new)["stage"]["kind"] == "open"


def test_add_order_of_a_client_ties_to_the_newest_order_or_makes_the_first(tmp_path):
    conn = db(tmp_path)
    first = card(conn)
    [(client_id,)] = conn.execute("SELECT client_id FROM client_order WHERE order_id = ?", (first,)).fetchall()
    second = chain.for_client(conn, client_id, NOW, "o")
    assert ids(conn, second) == [first] and orders.get_card(conn, second)["client_phone"] == PHONE
    cur = conn.execute("INSERT INTO client (name, created_at) VALUES ('Пётр', ?)", (NOW,))
    conn.execute("INSERT INTO client_phone (phone, client_id) VALUES ('+79001234567', ?)", (cur.lastrowid,))
    conn.commit()
    lone = chain.for_client(conn, cur.lastrowid, NOW, "o")
    assert orders.get_card(conn, lone)["client_name"] == "Пётр" and ids(conn, lone) == []
    with pytest.raises(orders.OrderError, match="Такого клиента нет"):
        chain.for_client(conn, 999, NOW, "o")


def test_the_card_shows_the_chain_and_its_buttons_and_the_routes_work(tmp_path):
    from panel_helpers import login, make_app, post
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    a, b = card(conn), card(conn)
    conn.execute("UPDATE order_card SET webmim_number = '5367654' WHERE id = ?", (b,))
    conn.commit()
    page = client.get("/orders/%d" % a).get_data(as_text=True)
    assert "Другие заказы клиента" in page and "Этот заказ ни с чем не связан" in page and "data-chain-unlink" not in page
    post(client, "/orders/%d/chain" % a, tok, other="5367654")
    page = client.get("/orders/%d" % a).get_data(as_text=True)
    assert 'data-chain-order="%d"' % b in page and "№ 5367654" in page and "data-chain-unlink" in page
    post(client, "/orders/%d/chain" % a, tok, other="77777")
    assert "Заказа с номером «77777» нет" in client.get("/orders/%d" % a).get_data(as_text=True)
    reply = post(client, "/orders/%d/again" % a, tok)
    new = int(reply.headers["Location"].rsplit("/", 1)[1])
    assert sorted(ids(conn, new)) == [a, b]
    post(client, "/orders/%d/chain-unlink" % new, tok)
    assert ids(conn, new) == [] and ids(conn, a) == [b]
    [(client_id,)] = conn.execute("SELECT client_id FROM client_order WHERE order_id = ?", (a,)).fetchall()
    reply = post(client, "/clients/%d/new-order" % client_id, tok)
    assert reply.status_code == 302 and "/orders/" in reply.headers["Location"]


def test_the_client_card_offers_add_order(tmp_path):
    from panel_helpers import login, make_app
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    a = card(conn)
    [(client_id,)] = conn.execute("SELECT client_id FROM client_order WHERE order_id = ?", (a,)).fetchall()
    page = client.get("/clients/%d" % client_id).get_data(as_text=True)
    assert 'action="/clients/%d/new-order"' % client_id in page and "data-client-new-order" in page


def test_a_double_press_of_add_order_makes_one_order_a_later_one_another(tmp_path):
    conn = db(tmp_path)
    first = card(conn)
    new = chain.again(conn, first, NOW, "o")
    assert chain.again(conn, first, "2026-11-19T12:00:30", "o") == new
    assert conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 2
    later = chain.again(conn, first, "2026-11-19T12:05:00", "o")
    assert later != new and sorted(ids(conn, first)) == [new, later]


def test_a_point_cannot_tie_nor_add_orders(tmp_path):
    from test_mypoint import manager
    from test_vk_commands import ready
    app, client, tok, conn, pid, made = ready(tmp_path)
    a, b = card(conn), card(conn)
    mine = manager(app, conn, pid)
    for url in ("/orders/%d/chain" % a, "/orders/%d/again" % a, "/orders/%d/chain-unlink" % a):
        assert mine.post(url, data={"csrf": "tok", "other": str(b)}).status_code == 403
    assert ids(conn, a) == [] and conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 2 + (1 if made else 0)
