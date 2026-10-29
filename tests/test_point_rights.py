"""The rights of the points and of the partner (the owner 2026-10-21): the partner looks at everything (changes the sales only, never sees
a key); the manager of a point sees the communications, the orders and the alerts of the own point only — the chats of the Avito account
of the point (none while it is not chosen), the orders of the point, the alerts of those. The filter is in the query; a foreign one is 403."""
from __future__ import annotations

import re

from panel_helpers import post
from test_chat_deals import Fake
from test_chats_pages import NOW, setup
from test_roles import person
from tires_sync import chats, connections, order_alerts, order_points as points, orders, store, users


def world(tmp_path):
    """The owner's panel: the account «Основной» (Татьяна, Иван), the account of Альфа (Пётр), the point Альфа and the point Гамма,
    an order of each point with a red record."""
    app, client, tok, conn, main = setup(tmp_path)
    rust_acc = connections.create_account(conn, "Альфа", NOW, "o")
    connections.set_modes(conn, rust_acc, True, True, NOW, "o")
    fake = Fake()
    fake.add("c9", "Пётр", [("in", "Есть зимние 205/55?")])
    chats.sync_account(conn, fake, rust_acc, NOW)
    rust = points.save_point(conn, {"name": "Альфа", "manager_name": "А", "channel": "vk"})
    gamma = points.save_point(conn, {"name": "Гамма", "manager_name": "Б", "channel": "vk"})
    mine = orders.create_card(conn, {"client_name": "Свой клиент", "point_id": str(rust)}, NOW, "o")
    other = orders.create_card(conn, {"client_name": "Чужой клиент", "point_id": str(gamma)}, NOW, "o")
    for card in (mine, other):
        orders.add_event(conn, card, NOW, "система", "point", "Красная запись заказа №%d" % card, level="red")
    conn.commit()
    by_name = {c["peer_name"]: c for c in chats.list_chats(conn)}
    return dict(app=app, owner=client, tok=tok, conn=conn, main=main, rust_acc=rust_acc, rust=rust, gamma=gamma, mine=mine, other=other,
                petr=by_name["Пётр"], tanya=by_name["Татьяна"])


def bind(w):
    post(w["owner"], "/connections/points/save", w["tok"], id=str(w["rust"]), name="Альфа", manager_name="А", channel="vk",
         avito_account_id=str(w["rust_acc"]))
    assert points.get_point(w["conn"], w["rust"])["avito_account_id"] == w["rust_acc"]


def test_the_matrix_partner_looks_at_everything_point_has_sales_of_its_own():
    assert all(users.level("partner", s) >= users.VIEW for s in users.SECTIONS if s != "shop")   # «Каталог для клиента»: the client's only
    assert users.level("partner", "setup") == users.level("partner", "users") == users.VIEW and not users.has_right("partner", "connections")
    assert users.level("point", "chats") >= users.VIEW and users.level("point", "alerts") >= users.VIEW
    for section in ("home", "catalog", "analytics", "setup", "users"):
        assert users.level("point", section) == users.NONE, section
    assert users.level("point", "clients") == users.NONE              # «CRM»: none (the owner 2026-10-29; its own part since 2026-10-26 before)
    assert users.has_right("point", "send") and not users.has_right("point", "delete") and not users.has_right("point", "bulk")   # the owner: they answer


def test_the_partner_opens_the_settings_and_the_users_but_changes_nothing_and_sees_no_key(tmp_path):
    w = world(tmp_path)
    partner, _ = person(w["app"], "partner")
    for url in ("/connections/webmim", "/connections/avito/%d" % w["main"], "/connections/points", "/connections/telegram", "/users", "/orders/settings"):
        assert partner.get(url).status_code == 200, url
    token = store.connect(w["app"].config["DB_PATH"]).execute("SELECT feed_token FROM account WHERE id = ?", (w["main"],)).fetchone()[0]
    assert token and token not in partner.get("/connections/avito/%d" % w["main"]).get_data(as_text=True)
    assert token in w["owner"].get("/connections/avito/%d" % w["main"]).get_data(as_text=True)                 # the owner still sees it
    for url, data in (("/connections/telegram", {"bot_token": "x"}), ("/users/invite", {"role": "viewer"}),
                      ("/connections/points/save", {"name": "Новая", "channel": "vk"})):
        assert partner.post(url, data=dict(data, csrf="tok")).status_code == 403, url


def test_a_point_without_its_avito_account_sees_no_chat(tmp_path):
    w = world(tmp_path)
    manager, _ = person(w["app"], "point", w["rust"])
    page = manager.get("/chats").get_data(as_text=True)
    assert "Пётр" not in page and "Татьяна" not in page
    assert manager.get("/chats/%d" % w["petr"]["id"]).status_code == 403


def test_a_point_sees_and_works_with_the_chats_of_its_account_only(tmp_path):
    """The owner 2026-10-21: «менеджеры точек отвечают клиентам и работают с чатами своего аккаунта»; another account: neither a form nor a POST."""
    w = world(tmp_path)
    bind(w)
    manager, conn = person(w["app"], "point", w["rust"])
    for url in ("/chats", "/chats?acc=%d" % w["main"]):
        page = manager.get(url).get_data(as_text=True)
        assert "Пётр" in page and "Татьяна" not in page and "Иван" not in page, url
    assert manager.get("/chats/%d" % w["petr"]["id"]).status_code == 200
    assert manager.get("/chats/%d" % w["tanya"]["id"]).status_code == 403
    for action, data in (("send", {"text": "Здравствуйте"}), ("send-image", {}), ("status", {"status": "closed"}), ("create-deal", {}),
                         ("link", {"order_id": str(w["mine"])}), ("stage", {"stage_id": "1"})):
        assert manager.post("/chats/%d/%s" % (w["tanya"]["id"], action), data=dict(data, csrf="tok")).status_code == 403, action
    assert manager.post("/chats/%d/status" % w["petr"]["id"], data={"csrf": "tok", "status": "no_reply"}).status_code == 302
    assert chats.get_chat(conn, w["petr"]["id"])["status"] == "no_reply"
    r = manager.post("/chats/%d/send" % w["petr"]["id"], data={"csrf": "tok", "text": "Здравствуйте"})
    assert r.status_code != 403                                                               # the own chat: the send itself is the chats' business
    manager.post("/chats/%d/link" % w["petr"]["id"], data={"csrf": "tok", "order_id": str(w["other"])})
    assert chats.get_chat(conn, w["petr"]["id"])["order_id"] != w["other"]                    # an order of another point: refused
    manager.post("/chats/%d/create-deal" % w["petr"]["id"], data={"csrf": "tok"})
    made = chats.get_chat(conn, w["petr"]["id"])["order_id"]
    assert made and orders.get_card(conn, made)["point_id"] == w["rust"]                       # the order made from the chat is of the point
    assert manager.post("/chats/refresh", data={"csrf": "tok", "chat": str(w["petr"]["id"])}).status_code == 200
    assert manager.post("/chats/%d/delete" % w["petr"]["id"], data={"csrf": "tok"}).status_code == 403
    assert manager.get("/shares").status_code == 403                                           # the links are made in the catalog: not theirs


def test_a_chat_of_the_own_account_bound_to_another_points_order_is_looked_at_not_acted_on(tmp_path):
    w = world(tmp_path)
    bind(w)
    manager, conn = person(w["app"], "point", w["rust"])
    conn.execute("UPDATE avito_chat SET order_id = ? WHERE id = ?", (w["other"], w["petr"]["id"]))
    conn.commit()
    page = manager.get("/chats/%d" % w["petr"]["id"]).get_data(as_text=True)
    assert "Чужой клиент" not in page
    assert manager.post("/chats/%d/status" % w["petr"]["id"], data={"csrf": "tok", "status": "closed"}).status_code == 403


def test_a_point_sees_the_alerts_of_its_orders_and_chats_only(tmp_path):
    w = world(tmp_path)
    bind(w)
    manager, conn = person(w["app"], "point", w["rust"])
    page = manager.get("/alerts").get_data(as_text=True)
    assert "Красная запись заказа №%d" % w["mine"] in page and "Красная запись заказа №%d" % w["other"] not in page
    assert "Татьяна" not in page and "Иван" not in page                                    # the waiting chats of the other account
    feed = manager.get("/alerts/feed").get_json()
    assert {i["order_id"] for i in feed["items"]} <= {w["mine"], None} and all(i.get("title") != "Заказ №%d" % w["other"] for i in feed["items"])
    other_event = conn.execute("SELECT id FROM order_event WHERE order_id = ? AND level = 'red'", (w["other"],)).fetchone()[0]
    assert manager.post("/alerts/%d/ack" % other_event, data={"csrf": "tok"}).status_code == 403
    own_event = conn.execute("SELECT id FROM order_event WHERE order_id = ? AND level = 'red'", (w["mine"],)).fetchone()[0]
    assert manager.post("/alerts/%d/ack" % own_event, data={"csrf": "tok"}).status_code == 302
    owner_page = w["owner"].get("/alerts").get_data(as_text=True)
    assert "Красная запись заказа №%d" % w["other"] in owner_page                           # the others see as before


def test_the_menu_counts_the_unread_chats_of_the_point_only(tmp_path):
    w = world(tmp_path)
    bind(w)
    manager, conn = person(w["app"], "point", w["rust"])
    counter = re.search(r'href="/chats">.*?<span class="sb-count">(\d+)</span>', manager.get("/orders").get_data(as_text=True), re.S)
    own = conn.execute("SELECT COUNT(*) FROM avito_chat WHERE account_id = ? AND unread > 0 AND status = 'open'", (w["rust_acc"],)).fetchone()[0]
    assert (int(counter.group(1)) if counter else 0) == own
    assert chats.unread_total(conn) > own                                                   # the whole panel has more


def test_the_point_setting_takes_an_existing_account_or_nothing(tmp_path):
    w = world(tmp_path)
    bind(w)
    post(w["owner"], "/connections/points/save", w["tok"], id=str(w["rust"]), name="Альфа", manager_name="А", channel="vk", avito_account_id="")
    assert points.get_point(w["conn"], w["rust"])["avito_account_id"] is None
    post(w["owner"], "/connections/points/save", w["tok"], id=str(w["rust"]), name="Альфа", manager_name="А", channel="vk", avito_account_id="999")
    assert points.get_point(w["conn"], w["rust"])["avito_account_id"] is None                # no such account: refused
    points.save_point(w["conn"], {"name": "Альфа", "manager_name": "А", "channel": "vk"}, w["rust"])          # a form without the field keeps it
    assert points.get_point(w["conn"], w["rust"])["avito_account_id"] is None
    assert "Аккаунт Авито точки" in w["owner"].get("/connections/points").get_data(as_text=True)


# ---- the review, round 1 ---------------------------------------------------------------------------------------------------------------

def test_the_partner_sees_neither_the_vk_hook_address_nor_the_binding_code(tmp_path):
    w = world(tmp_path)
    post(w["owner"], "/connections/points/%d/code" % w["rust"], w["tok"])
    code = points.get_point(w["conn"], w["rust"])["bind_code"]
    assert code and code in w["owner"].get("/connections/points").get_data(as_text=True)
    partner, _ = person(w["app"], "partner")
    assert code not in partner.get("/connections/points").get_data(as_text=True)
    assert "hook/vk/" in w["owner"].get("/connections/vk").get_data(as_text=True)
    assert "hook/vk/" not in partner.get("/connections/vk").get_data(as_text=True)


def test_a_chat_of_the_own_account_whose_order_has_no_point_is_the_points_to_work_with(tmp_path):
    w = world(tmp_path)
    bind(w)
    manager, conn = person(w["app"], "point", w["rust"])
    nowhere = orders.create_card(conn, {"client_name": "Без Точки"}, NOW, "o")
    conn.execute("UPDATE avito_chat SET order_id = ? WHERE id = ?", (nowhere, w["petr"]["id"]))
    conn.commit()
    assert manager.post("/chats/%d/status" % w["petr"]["id"], data={"csrf": "tok", "status": "no_reply"}).status_code == 302
    assert chats.get_chat(conn, w["petr"]["id"])["status"] == "no_reply"


def test_a_point_adds_no_positions_through_a_chat_and_the_viewer_reads_no_avito(tmp_path):
    w = world(tmp_path)
    bind(w)
    manager, conn = person(w["app"], "point", w["rust"])
    for action in ("items/add", "wait"):
        assert manager.post("/chats/%d/%s" % (w["petr"]["id"], action), data={"csrf": "tok"}).status_code == 403, action
    page = manager.get("/chats/%d" % w["petr"]["id"]).get_data(as_text=True)
    assert "/items/add" not in page and 'action="/chats/%d/wait"' % w["petr"]["id"] not in page
    viewer, _ = person(w["app"], "viewer")
    assert viewer.post("/chats/refresh", data={"csrf": "tok"}).status_code == 403                # as before: the point only reads with «смотреть»


def test_the_point_sees_no_number_of_another_points_earlier_order(tmp_path):
    w = world(tmp_path)
    bind(w)
    manager, conn = person(w["app"], "point", w["rust"])
    conn.execute("UPDATE avito_chat SET order_id = ? WHERE id = ?", (w["mine"], w["petr"]["id"]))
    other_chat = conn.execute("INSERT INTO avito_chat (account_id, chat_id, peer_id, order_id) VALUES (?, 'c-old', ?, ?)",
                              (w["rust_acc"], w["petr"]["peer_id"], w["other"])).lastrowid
    conn.commit()
    assert w["petr"]["peer_id"]
    page = manager.get("/chats/%d" % w["petr"]["id"]).get_data(as_text=True)
    assert 'href="/orders/%d"' % w["other"] not in page and other_chat


def test_the_point_moves_no_stage_of_an_order_without_a_point_through_the_chat(tmp_path):
    w = world(tmp_path)
    bind(w)
    manager, conn = person(w["app"], "point", w["rust"])
    nowhere = orders.create_card(conn, {"client_name": "Без Точки"}, NOW, "o")
    conn.execute("UPDATE avito_chat SET order_id = ? WHERE id = ?", (nowhere, w["petr"]["id"]))
    conn.commit()
    before = orders.get_card(conn, nowhere)["stage_id"]
    target = next(s["id"] for s in orders.list_stages(conn) if s["id"] != before and s["kind"] == "open")
    manager.post("/chats/%d/stage" % w["petr"]["id"], data={"csrf": "tok", "stage_id": str(target)})
    assert orders.get_card(conn, nowhere)["stage_id"] == before                              # the owner's order: not the point's to move
