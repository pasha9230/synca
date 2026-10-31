"""«Мои заказы» of a client, the tails of the new design (the owner 2026-10-31): «Получение» (the point / the delivery and its address),
«храним до» of an order waiting at the point (the reservation the point and the managers see), «Отменить заказ» while the order is early
(before it is placed with the supplier) with a confirmation, the manager told, a line of the audit; later — «напишите менеджеру»."""
from __future__ import annotations

import json

from panel_helpers import login, post
from test_shop import page
from test_shop_cart import add, checkout, keys, second_client, shop as _bare_shop
from tires_sync import orders, shop_cart
from tires_sync.panel import texts

NOW = "2026-10-07T12:00:00"


def shop(tmp_path):
    """test_shop_cart.shop with the whole pipeline of the orders (the stages of placing, the point, «Отмена / невыкуп»)."""
    app, client, conn, tg, pid = _bare_shop(tmp_path)
    orders.ensure_pipeline(conn, NOW)
    return app, client, conn, tg, pid


def _order(client, **form):
    add(client, keys(page(client))[0], qty="2")
    return int(checkout(client, **form).headers["Location"].rsplit("/", 1)[1])


def _stage(conn, code):
    return conn.execute("SELECT id FROM order_stage WHERE code = ?", (code,)).fetchone()[0]


def _sent(tg):
    return [json.loads(c[3])["text"] for c in tg.calls if "sendMessage" in c[1]]


# ---- «Получение» and «храним до» ---------------------------------------------------------------------------------------------------

def test_the_list_says_how_the_client_receives_each_order(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    delivery = _order(client)
    pickup = _order(client, way="point", point_id=str(pid))
    html = page(client, "/shop/orders")
    assert "<th>Получение</th>" in html
    assert "Доставка: г. Тест, ул. Первая, 1" in html and "Самовывоз: Бета" in html
    assert "Самовывоз: Бета" in page(client, "/shop/orders/%d" % pickup) and "Доставка: г. Тест" in page(client, "/shop/orders/%d" % delivery)
    assert "к пятнице" not in html                                                     # the comment itself is never shown


def test_the_delivery_address_is_the_one_of_the_order_not_of_the_profile_now(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    order = _order(client)
    post(client, "/shop/profile", "tok", name="Покупатель", phone="+7 900 111-22-33", address="г. Другой, ул. Новая, 9")
    html = page(client, "/shop/orders/%d" % order)
    assert "Доставка: г. Тест, ул. Первая, 1" in html and "Новая" not in html


def test_an_order_before_the_choice_was_kept_reads_it_from_the_first_words_of_its_comment(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    order = _order(client, way="point", point_id=str(pid))
    conn.execute("UPDATE shop_order SET way = '', place = '' WHERE order_id = ?", (order,))           # an order of before 2026-10-31
    conn.commit()
    assert shop_cart.my_order(conn, "buyer", order)["receive"] == "Самовывоз: Бета"


def test_an_order_at_the_point_says_until_when_it_is_kept(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    order = _order(client, way="point", point_id=str(pid))
    assert "храним до" not in page(client, "/shop/orders")
    orders.update_card(conn, order, {"delivery_date": "2026-10-08", "point_id": str(pid)}, NOW, "zaman1")
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (_stage(conn, "moving"), order))
    conn.commit()
    orders.move_card(conn, order, _stage(conn, "at_point"), "2026-10-08T10:00:00", "zaman1")
    until = orders.get_card(conn, order)["reserve_until"]
    assert until == "2026-10-11"                                                       # the point's reservation: three days (orders.RESERVE_DAYS)
    shown = "%s.%s" % (until[8:10], until[5:7])
    assert "храним до %s" % shown in page(client, "/shop/orders")
    assert "Храним до:</span> <b>%s" % shown in page(client, "/shop/orders/%d" % order)
    orders.update_card(conn, order, {"reserve_until": "2026-10-20"}, NOW, "zaman1")    # the managers moved it: the client sees the same date
    assert "храним до 20.10" in page(client, "/shop/orders")


# ---- «Отменить заказ» ---------------------------------------------------------------------------------------------------------------

def test_the_client_cancels_an_early_order_after_a_confirmation(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    order = _order(client)
    html = page(client, "/shop/orders/%d" % order)
    assert 'href="/shop/orders/%d?cancel=1#cancel"' % order in html and "/cancel\"" not in html       # first a link, not a form
    assert 'action="/shop/orders/%d/cancel"' % order in page(client, "/shop/orders/%d?cancel=1" % order)
    r = post(client, "/shop/orders/%d/cancel" % order, "tok")                                      # without «Да»: back to the question
    assert r.status_code == 302 and r.headers["Location"].endswith("?cancel=1#cancel")
    assert orders.get_card(conn, order)["stage"]["code"] == "from_point"
    before = len(_sent(tg))
    r = post(client, "/shop/orders/%d/cancel" % order, "tok", confirm="1")
    assert r.status_code == 302 and r.headers["Location"].endswith("/shop/orders/%d" % order)
    card = orders.get_card(conn, order)
    assert card["stage"]["code"] == "cancel" and card["cancel_reason"] == "отменил клиент: сам, в каталоге клиента"
    assert any("«Передано с точки» → «Отмена / невыкуп»" in e["text"] and e["author"] == "buyer" for e in orders.list_events(conn, order))
    told = _sent(tg)[before:]
    assert len(told) == 1 and "Клиент отменил заказ №%d" % order in told[0] and "https://panel.example.test/orders/%d" % order in told[0]
    audit = conn.execute("SELECT author, detail FROM audit WHERE action = 'shop_order_cancel'").fetchall()
    assert len(audit) == 1 and audit[0][0] == "buyer"
    line = texts.audit_text({"action": "shop_order_cancel", "detail": json.loads(audit[0][1]), "author": "buyer", "at": NOW})
    assert "buyer" in line and str(order) in line and "Передано с точки" in line
    html = page(client, "/shop/orders/%d" % order)
    assert "Отменён" in html and "Отменить заказ" not in html and "напишите менеджеру" not in html


def test_a_placed_order_has_no_button_and_the_post_changes_nothing(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    order = _order(client)
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (_stage(conn, "placed"), order))
    conn.commit()
    html = page(client, "/shop/orders/%d" % order)
    assert "Отменить заказ" not in html and "Чтобы отменить, напишите менеджеру." in html
    assert "/cancel" not in page(client, "/shop/orders/%d?cancel=1" % order)
    before = len(_sent(tg))
    r = post(client, "/shop/orders/%d/cancel" % order, "tok", confirm="1")
    assert r.status_code == 302 and orders.get_card(conn, order)["stage"]["code"] == "placed"
    assert len(_sent(tg)) == before and conn.execute("SELECT 1 FROM audit WHERE action = 'shop_order_cancel'").fetchone() is None


def test_an_early_order_already_placed_with_the_supplier_is_not_cancelled(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    for field in ("webmim_id", "webmim_number", "supplier_number", "transfer_number"):
        order = _order(client)
        conn.execute("UPDATE order_card SET %s = '777' WHERE id = ?" % field, (order,))
        conn.commit()
        assert not shop_cart.my_order(conn, "buyer", order)["can_cancel"], field
        post(client, "/shop/orders/%d/cancel" % order, "tok", confirm="1")
        assert orders.get_card(conn, order)["stage"]["code"] == "from_point", field


def test_a_sale_a_press_of_webmim_or_tyres_of_oth_keep_the_order(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    order = _order(client)
    conn.execute("UPDATE order_card SET sale_ids = '[5]' WHERE id = ?", (order,))
    conn.commit()
    assert not shop_cart.my_order(conn, "buyer", order)["can_cancel"]
    conn.execute("UPDATE order_card SET sale_ids = '[]' WHERE id = ?", (order,))
    for state, may in (("running", False), ("unknown", False), ("partial", False), ("done", False), ("refused", True)):
        conn.execute("INSERT OR REPLACE INTO webmim_create (order_id, server, state, first_at, started_at, author, updated_at) "
                     "VALUES (?, 's', ?, ?, ?, 'zaman1', ?)", (order, state, NOW, NOW, NOW))
        conn.commit()
        assert shop_cart.my_order(conn, "buyer", order)["can_cancel"] is may, state
    post(client, "/shop/orders/%d/cancel" % order, "tok", confirm="1")                 # «не оформлен»: the client may cancel
    assert orders.get_card(conn, order)["stage"]["code"] == "cancel"
    other = _order(client)
    conn.execute("INSERT INTO webmim_create (order_id, server, state, first_at, started_at, author, updated_at) VALUES (?, 's', 'running', ?, ?, 'z', ?)",
                 (other, NOW, NOW, NOW))
    conn.commit()
    post(client, "/shop/orders/%d/cancel" % other, "tok", confirm="1")                 # «оформляется» this moment: kept
    assert orders.get_card(conn, other)["stage"]["code"] == "from_point"


def test_tyres_of_oth_hide_the_button(tmp_path, monkeypatch):
    from tires_sync import oth_sale
    app, client, conn, tg, pid = shop(tmp_path)
    order = _order(client)
    monkeypatch.setattr(oth_sale, "holds", lambda c, card_id: [99] if card_id == order else [])
    html = page(client, "/shop/orders/%d" % order)
    assert "Отменить заказ" not in html and "Чтобы отменить, напишите менеджеру." in html and "ОТХ" not in html
    post(client, "/shop/orders/%d/cancel" % order, "tok", confirm="1")
    assert orders.get_card(conn, order)["stage"]["code"] == "from_point"


def test_a_real_refusal_of_the_move_shows_the_client_no_words_of_the_staff(tmp_path, monkeypatch):
    app, client, conn, tg, pid = shop(tmp_path)
    order = _order(client)
    monkeypatch.setattr(orders, "missing_for", lambda *a, **k: ["причина отмены"])   # move_card's own refusal: rollback, red record
    r = post(client, "/shop/orders/%d/cancel" % order, "tok", confirm="1")
    follow = client.get(r.headers["Location"]).get_data(as_text=True)
    assert "Отменить не получилось. Напишите менеджеру." in follow and "заполните" not in follow
    assert orders.get_card(conn, order)["stage"]["code"] == "from_point"
    assert any(e["kind"] == "refused" for e in orders.list_events(conn, order))
    assert conn.execute("SELECT 1 FROM audit WHERE action = 'shop_order_cancel'").fetchone() is None


def test_the_stages_the_client_may_cancel_on(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    order = _order(client)
    for stage in orders.list_stages(conn):
        conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage["id"], order))
        assert shop_cart.my_order(conn, "buyer", order)["can_cancel"] == (stage["code"] in ("from_point", "new", "talk", "wait")), stage["code"]
    conn.rollback()


def test_one_client_never_cancels_another_order(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    order = _order(client)
    other = second_client(app, conn)
    assert post(other, "/shop/orders/%d/cancel" % order, "tok", confirm="1").status_code == 404
    assert other.get("/shop/orders/%d?cancel=1" % order).status_code == 404
    assert orders.get_card(conn, order)["stage"]["code"] == "from_point"


def test_the_staff_and_a_point_cannot_use_the_client_cancel(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    order = _order(client)
    owner = app.test_client()
    login(owner)
    assert post(owner, "/shop/orders/%d/cancel" % order, "tok", confirm="1").status_code == 403
    assert orders.get_card(conn, order)["stage"]["code"] == "from_point"


def test_the_cancel_keeps_the_strict_frame(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    order = _order(client)
    for r in (client.get("/shop/orders/%d?cancel=1" % order), post(client, "/shop/orders/%d/cancel" % order, "tok", confirm="1")):
        csp = r.headers["Content-Security-Policy"]
        assert "default-src 'none'" in csp and "script-src" not in csp and "<script" not in r.get_data(as_text=True)


def test_a_refused_move_tells_the_client_to_write_to_the_manager(tmp_path, monkeypatch):
    app, client, conn, tg, pid = shop(tmp_path)
    order = _order(client)

    def refuse(*a, **k):
        raise orders.OrderError("По заказу записана продажа.")
    monkeypatch.setattr(orders, "move_card", refuse)
    r = post(client, "/shop/orders/%d/cancel" % order, "tok", confirm="1")
    follow = client.get(r.headers["Location"]).get_data(as_text=True)
    assert "Напишите менеджеру" in follow and orders.get_card(conn, order)["stage"]["code"] == "from_point"
    assert conn.execute("SELECT 1 FROM audit WHERE action = 'shop_order_cancel'").fetchone() is None
