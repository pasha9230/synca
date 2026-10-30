"""«Передать под заказ» (the owner 2026-10-23, п. 6, 7, 20–24): the manager of a point hands the client of a chat of the point's account to
the person on duty in one window (phone, size, season, brand, comment; only the phone or the chat is needed); the staff see «Клиент хочет
заказать шины…» and the client is «взят в работу» when they open the alert or the order, answer in the chat, or by hand — the stage stays;
the point sees what became of the client on «Рабочее место»; in VK the bot's button asks for the client in one message; the point gets +50 ₽ for
an issued order of such a client from 2026-10-24."""
from __future__ import annotations

import json
import re
import secrets
import urllib.parse

import pytest

from panel_helpers import post
from test_chat_deals import Fake
from test_point_rights import bind, world
from test_roles import person
from test_vk_commands import PEER, hook, ready
from tires_sync import chats, order_manager as mgr, order_points as points, order_report, order_wait, orders, sales, store, users, vk_send
from tires_sync.panel import routes_alerts

NOW = "2026-10-21T12:00:00"


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


@pytest.fixture
def w(tmp_path):
    w = world(tmp_path)
    bind(w)                                                        # the point Альфа works with the chats of its Avito account (Пётр)
    w["manager"], _ = person(w["app"], "point", w["rust"])
    return w


def token() -> str:
    return secrets.token_hex(12)


def hand(w, client=None, **form):
    data = dict({"phone": "", "size": "", "season": "", "brand": "", "comment": "", "token": token()}, **form)
    return (client or w["manager"]).post("/chats/%d/handoff" % w["petr"]["id"], data=dict(data, csrf="tok"))


def handed(conn, order_id=None):
    rows = conn.execute("SELECT h.order_id, h.point_id, h.phone, h.need, s.kind, s.chat_row, s.size, s.season, s.brand, s.taken_at "
                        "FROM order_handoff h LEFT JOIN order_handoff_source s ON s.order_id = h.order_id ORDER BY h.order_id").fetchall()
    found = [dict(zip(("order", "point", "phone", "need", "kind", "chat", "size", "season", "brand", "taken"), r)) for r in rows]
    return [f for f in found if order_id is None or f["order"] == order_id]


# ---- the window in «Чаты» ----------------------------------------------------------------------------------------------------------------

def test_the_point_hands_over_a_chat_with_what_the_client_wants(w):
    conn = w["conn"]
    page = w["manager"].get("/chats/%d" % w["petr"]["id"]).get_data(as_text=True)
    assert "Передать под заказ" in page and "Клиент хочет шины под заказ?" in page and 'action="/chats/%d/handoff"' % w["petr"]["id"] in page
    tok = token()
    r = hand(w, phone="8 (900) 111-22-33", size="205/55 R16", season="winter_studded", brand="Nokian", comment="к пятнице", token=tok)
    assert r.status_code == 302
    [h] = handed(conn)
    card = orders.get_card(conn, h["order"])
    assert (h["point"], h["phone"], h["kind"], h["chat"], h["size"], h["season"], h["brand"]) == (
        w["rust"], "+79001112233", "chat", w["petr"]["id"], "205/55 R16", "winter_studded", "Nokian")
    assert h["need"] == "205/55 R16, зима, шипы, Nokian. к пятнице"
    assert card["stage"]["code"] == "from_point" and card["point_id"] == w["rust"] and card["client_name"] == "Пётр"
    assert mgr.HANDOFF_TAG in card["tags"]
    assert chats.get_chat(conn, w["petr"]["id"])["order_id"] == card["id"]                       # the chat goes with the client
    [want] = order_wait.list_wants(conn, card["id"])
    assert (want["width"], want["profile"], want["radius"], want["season"], want["studded"], want["brand"], want["qty"]) == (205, 55, 16, "winter", "yes", "Nokian", 4)
    ask = [e for e in orders.list_events(conn, card["id"]) if e["text"].startswith(mgr.ASK)]
    assert len(ask) == 1 and ask[0]["level"] == "yellow" and "Альфа" in ask[0]["text"] and "чат Авито" in ask[0]["text"]
    again = hand(w, phone="89001112233", token=tok)                                               # the same press twice
    assert again.status_code == 302 and len(handed(conn)) == 1
    assert "уже передан" in w["manager"].get("/chats/%d" % w["petr"]["id"]).get_data(as_text=True)
    assert len(handed(conn)) == 1 and hand(w, phone="89001112233").status_code == 302 and len(handed(conn)) == 1    # a new press: the chat is handed already


def test_without_a_phone_the_chat_is_handed_over_and_a_bad_phone_makes_nothing(w):
    conn = w["conn"]
    hand(w, phone="12345")
    assert handed(conn) == []
    assert "Телефон не распознан" in w["manager"].get("/chats/%d" % w["petr"]["id"]).get_data(as_text=True)    # the window comes back open
    hand(w, comment="хочет летние")
    [h] = handed(conn)
    assert h["phone"] == "" and h["need"] == "хочет летние"
    assert any("телефона нет, есть чат" in e["text"] for e in orders.list_events(conn, h["order"]))


def test_the_phone_of_the_window_comes_from_the_chat(tmp_path):
    w = world(tmp_path)
    conn = w["conn"]
    fake = Fake()
    fake.add("c9", "Пётр", [("in", "Есть зимние 205/55?"), ("in", "мой номер 8 921 555-44-33")])
    chats.sync_account(conn, fake, w["rust_acc"], NOW)
    assert mgr.chat_phone(conn, w["petr"]["id"]) == "+79215554433"
    orders.update_card(conn, w["mine"], {"client_phone": "+7 900 111-22-33"}, NOW, "o")
    conn.execute("UPDATE avito_chat SET order_id = ? WHERE id = ?", (w["mine"], w["petr"]["id"]))
    conn.commit()
    assert mgr.chat_phone(conn, w["petr"]["id"]) == "+79001112233"                                # the order's phone first


def test_only_the_manager_of_the_point_of_the_chat_hands_it_over(w):
    owner_page = w["owner"].get("/chats/%d" % w["petr"]["id"]).get_data(as_text=True)
    assert "Передать под заказ" not in owner_page
    assert hand(w, client=w["owner"], phone="89001112233").status_code == 403                    # the staff take clients, they do not hand them
    other, _ = person(w["app"], "point", w["gamma"], login_="gamma1")
    assert hand(w, client=other, phone="89001112233").status_code == 403                         # a chat of another point's account
    assert handed(w["conn"]) == []


def test_an_early_order_of_the_point_is_handed_over_itself_a_later_one_is_not_doubled(w):
    conn = w["conn"]
    conn.execute("UPDATE avito_chat SET order_id = ? WHERE id = ?", (w["mine"], w["petr"]["id"]))
    conn.commit()
    mgr.set_duty(conn, {d: "worker2" for d in range(7)}, (0, 24), "", "2026-10-13")
    cards = conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0]
    hand(w, phone="89001112233", season="summer")
    [h] = handed(conn)
    card = orders.get_card(conn, w["mine"])
    assert h["order"] == w["mine"] and card["stage"]["code"] == "from_point" and card["responsible"] == "worker2" and card["point_id"] == w["rust"]
    assert card["client_phone"] == "+79001112233" and mgr.HANDOFF_TAG in card["tags"]
    assert conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == cards                  # no second card
    hand(w, phone="89001112233")
    assert len(handed(conn)) == 1                                                                 # handed already: said so, nothing new


def test_a_chat_whose_order_went_further_gets_a_new_order_and_stays_where_it_is(w):
    conn = w["conn"]
    conn.execute("UPDATE avito_chat SET order_id = ? WHERE id = ?", (w["mine"], w["petr"]["id"]))
    conn.execute("UPDATE order_card SET stage_id = (SELECT id FROM order_stage WHERE code = 'placed') WHERE id = ?", (w["mine"],))
    conn.commit()
    hand(w, phone="89001112233")
    [h] = handed(conn)
    assert h["order"] != w["mine"] and h["chat"] == w["petr"]["id"]
    assert chats.get_chat(conn, w["petr"]["id"])["order_id"] == w["mine"]
    assert orders.get_card(conn, w["mine"])["stage"]["code"] == "placed"


# ---- «взяли в работу» ----------------------------------------------------------------------------------------------------------------

def _event(conn, order_id):
    return next(e for e in orders.list_events(conn, order_id) if e["text"].startswith(mgr.ASK))


def test_the_alert_leads_the_staff_to_the_chat_and_takes_the_client_the_point_does_not(w):
    conn = w["conn"]
    hand(w, phone="89001112233")
    [h] = handed(conn)
    event = _event(conn, h["order"])
    rows = w["owner"].get("/alerts").get_data(as_text=True)
    assert 'href="/alerts/%d/go"' % event["id"] in rows and "Взять в работу" in rows
    feed = w["owner"].get("/alerts/feed").get_json()
    assert any(i["href"] == "/alerts/%d/go" % event["id"] for i in feed["items"])
    assert mgr.ASK not in w["manager"].get("/alerts").get_data(as_text=True)                    # the point sees its clients on «Рабочее место»
    r = w["manager"].get("/alerts/%d/go" % event["id"])
    assert r.status_code == 302 and handed(conn)[0]["taken"] is None
    assert w["manager"].get("/orders/%d" % h["order"]).status_code == 200 and handed(conn)[0]["taken"] is None
    r = w["owner"].get("/alerts/%d/go" % event["id"])
    assert r.status_code == 302 and r.headers["Location"].endswith("/chats/%d" % w["petr"]["id"])
    taken = conn.execute("SELECT taken_at, taken_by, taken_how FROM order_handoff_source WHERE order_id = ?", (h["order"],)).fetchone()
    assert taken[0] and taken[2] == "открыл уведомление"
    assert orders.get_card(conn, h["order"])["stage"]["code"] == "from_point"                    # the stage stays (the lead 2026-10-23)
    assert any(e["text"].startswith("Клиент взят в работу: открыл уведомление") for e in orders.list_events(conn, h["order"]))


def test_opening_the_order_and_the_button_take_the_client_once(w):
    conn = w["conn"]
    hand(w, phone="89001112233")
    first = handed(conn)[0]["order"]
    assert w["owner"].get("/orders/%d" % first).status_code == 200
    assert conn.execute("SELECT taken_how FROM order_handoff_source WHERE order_id = ?", (first,)).fetchone()[0] == "открыл заказ"
    conn.execute("UPDATE order_card SET stage_id = (SELECT id FROM order_stage WHERE code = 'cancel') WHERE id = ?", (first,))   # closed: a new client
    conn.commit()
    hand(w, phone="89002223344")
    newer = max(h["order"] for h in handed(conn))
    assert newer != first
    event = _event(conn, newer)
    post(w["owner"], "/alerts/%d/take" % event["id"], w["tok"])
    post(w["owner"], "/alerts/%d/take" % event["id"], w["tok"])
    assert sum(e["text"].startswith("Клиент взят в работу") for e in orders.list_events(conn, newer)) == 1
    assert w["manager"].post("/alerts/%d/take" % event["id"], data={"csrf": "tok"}).status_code == 403


def test_an_answer_in_the_chat_takes_the_client_and_a_moved_order_is_not_taken_again(w):
    conn = w["conn"]
    hand(w, phone="89001112233")
    order = handed(conn)[0]["order"]
    assert mgr.take_chat(conn, w["petr"]["id"], "ответил в чате", "owner", NOW) == [order]
    assert mgr.take_chat(conn, w["petr"]["id"], "ответил в чате", "owner", NOW) == []
    other = orders.create_card(conn, {"client_name": "y"}, NOW, "o")
    assert mgr.take(conn, other, "вручную", "owner", NOW) is False                                # not a handed-over client


def test_the_mark_is_taken_for_the_point_and_stops_the_fifteen_minutes(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    pid = points.save_point(conn, {"name": "Бета", "manager_name": "Алексей", "channel": "vk", "vk_peer_id": PEER})
    conn.execute("UPDATE point SET vk_peer_id = ? WHERE id = ?", (PEER, pid))
    conn.commit()
    mgr.dispatch(conn, "vk-1", PEER, "клиент 89001112233 шины", None, NOW)
    order = handed(conn)[0]["order"]
    assert mgr.take(conn, order, "позвонил", "worker2", NOW)
    made = mgr.handoff_tick(conn, "2026-10-21T13:00:00")
    assert made == {"overdue": 0}                                                                # nothing to the point in VK (2026-10-23)
    assert mgr.point_handoffs(conn, pid, "2026-10-01")[0]["status_name"] == "взяли в работу"


# ---- «Рабочее место» -------------------------------------------------------------------------------------------------------------------

def test_the_point_sees_what_became_of_its_clients_and_the_bonus(w):
    conn = w["conn"]
    hand(w, phone="89001112233", size="205/55 R16")
    page = w["manager"].get("/my-point?tab=handed").get_data(as_text=True)                 # «Переданные клиенты»: its own tab (2026-10-30)
    assert "Переданные клиенты" in page and "+7 900 111-22-33" in page and "передан, ждёт" in page
    order = handed(conn)[0]["order"]
    orders.move_card(conn, order, next(s["id"] for s in orders.list_stages(conn) if s["code"] == "cancel"), NOW, "o", {"cancel_reason": "нет"})
    assert "отказ" in w["manager"].get("/my-point?tab=handed").get_data(as_text=True)
    other, _ = person(w["app"], "point", w["gamma"], login_="gamma1")
    assert "+7 900 111-22-33" not in other.get("/my-point?tab=handed").get_data(as_text=True)


def test_the_form_of_my_point_takes_the_season_size_and_brand(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    page = client.get("/my-point?tab=handed&p=%d" % pid).get_data(as_text=True)
    form_token = re.search(r'name="token" value="([0-9a-f]{24})"', page).group(1)
    post(client, "/my-point", tok, p=str(pid), token=form_token, phone="89002223344", need="4 шт", size="185/65 R15", season="winter_soft", brand="Kumho")
    [h] = handed(conn)
    assert (h["kind"], h["size"], h["season"], h["brand"], h["need"]) == ("page", "185/65 R15", "winter_soft", "Kumho", "185/65 R15, зима, без шипов, Kumho. 4 шт")


# ---- VK: the bot's button ------------------------------------------------------------------------------------------------------------

def test_the_button_asks_for_the_client_and_the_next_message_hands_it_over(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    pid = points.save_point(conn, {"name": "Бета", "manager_name": "Алексей", "channel": "vk", "vk_peer_id": PEER})
    conn.execute("UPDATE point SET vk_peer_id = ? WHERE id = ?", (PEER, pid))
    conn.commit()
    assert mgr.dispatch(conn, "vk-1", PEER, "Передать под заказ", None, NOW) == mgr.BUTTON_ANSWER
    assert mgr.dispatch(conn, "vk-1", PEER, "Передать под заказ", None, NOW) == ""                # VK repeats the event
    answer = mgr.dispatch(conn, "vk-2", PEER, "+7 900 111-22-33 зима шипы 205/55 R16", None, "2026-10-21T12:10:00")
    assert answer.startswith("Клиент передан") and handed(conn)[0]["phone"] == "+79001112233"
    mgr.dispatch(conn, "vk-3", PEER, "+7 900 222-33-44 лето", None, "2026-10-21T12:11:00")
    assert len(handed(conn)) == 1                                                                 # one press, one client
    mgr.dispatch(conn, "vk-4", PEER, "передать под заказ", None, "2026-10-21T13:00:00")
    mgr.dispatch(conn, "vk-5", PEER, "+7 900 333-44-55 лето", None, "2026-10-21T13:31:00")         # after 30 minutes: not a client
    assert len(handed(conn)) == 1
    assert mgr.dispatch(conn, "vk-6", "999", "Передать под заказ", None, NOW) != mgr.BUTTON_ANSWER    # a stranger
    assert mgr.dispatch(conn, "vk-7", PEER, "клиент 89004445566 шины", None, NOW).startswith("Клиент передан")     # the command stays


def _sent(app):
    return [urllib.parse.parse_qs(c[3].decode("utf-8")) for c in app.config["VK_HTTP"].calls]


def test_the_answer_to_a_point_carries_the_keyboard_and_goes_without_it_while_vk_refuses_it(tmp_path):
    from test_vk_commands import ScriptedHttp
    app, client, tok, conn, pid, card = ready(tmp_path)
    hook(app, conn, "Передать под заказ", event="b1")
    [first] = _sent(app)
    assert first["message"][0] == mgr.signed(mgr.BUTTON_ANSWER) and json.loads(first["keyboard"][0])["buttons"][0][0]["action"]["label"] == mgr.BUTTON
    app.config["VK_HTTP"] = ScriptedHttp((200, {"error": {"error_code": 912, "error_msg": "This is a chat bot feature"}}), (200, {"response": 7}),
                                         *[(200, {"response": n}) for n in range(8, 20)])
    hook(app, conn, "+7 900 222-33-44 летние", event="b2")              # not the phone of the open order of ready()
    calls = _sent(app)
    assert "keyboard" in calls[0] and "keyboard" not in calls[1] and calls[1]["message"][0].startswith("(бот) Клиент передан")


def test_vk_send_puts_the_keyboard_in_the_body_only_when_given():
    from test_vk_commands import ScriptedHttp
    http = ScriptedHttp((200, {"response": 1}), (200, {"response": 2}))
    vk_send.send_message("key", "555", "текст", http=http, random_id=1)
    vk_send.send_message("key", "555", "текст", http=http, random_id=2, keyboard=mgr.KEYBOARD)
    bodies = [urllib.parse.parse_qs(c[3].decode("utf-8")) for c in http.calls]
    assert "keyboard" not in bodies[0] and bodies[1]["keyboard"][0] == mgr.KEYBOARD


# ---- the money: +50 ₽ from 2026-10-24 ------------------------------------------------------------------------------------------------

def _sell(conn, order_id, point, day):
    sales.add_sale(conn, day=day, goods_id="g%d" % order_id, brand_key="nokian", season="winter", diameter=16, name="Nokian", ad_kind="single",
                   pieces=4, sale_total=20000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, account_id=None, point=point,
                   note="Заказ №%d" % order_id, now=NOW, author="o", today=day, source="order")


def test_fifty_rubles_for_an_issued_order_of_a_handed_over_client_from_the_24th(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    for order_id in (1, 2, 3):
        conn.execute("INSERT INTO order_handoff (order_id, message_id, created_at) VALUES (?, ?, ?)", (order_id, "m%d" % order_id, NOW))
    conn.commit()
    _sell(conn, 1, "Бета", "2026-10-23")                                                         # the day before: as before
    _sell(conn, 2, "Бета", "2026-10-24")                                                         # the first day: 75 + 50
    _sell(conn, 4, "Бета", "2026-10-24")                                                         # not handed over: 75
    assert order_report.rate_on([], "handoff_fee", "2026-10-23") == 0 and order_report.rate_on([], "handoff_fee", order_report.HANDOFF_FEE_FROM) == 50
    before = order_report.build(conn, "2026-10-01", "2026-10-23")
    assert before["lines"][0]["fee"] == 75 and before["handoff"]["fee"] == 0
    rep = order_report.build(conn, "2026-10-01", "2026-10-31")
    line = rep["lines"][0]
    assert line["fee"] == 75 * 3 + 50 and rep["handoff"] == {"lines": [{"point": "Бета", "clients": 2, "fee": 50.0}], "clients": 2, "fee": 50.0}
    assert line["net"] == line["margin"] - 275 and line["owner"] == round(line["net"] / 2, 2)
    order_report.set_rates(conn, "2026-10-25", "2026-10-25", NOW, "owner", handoff_fee=70)          # a saved rate wins from its day
    _sell(conn, 3, "Бета", "2026-10-25")
    assert order_report.build(conn, "2026-10-25", "2026-10-25")["lines"][0]["fee"] == 75 + 70
    assert order_report.build(conn, "2026-10-24", "2026-10-24")["lines"][0]["fee"] == 75 * 2 + 50


def test_the_staff_answer_in_the_chat_takes_the_client(tmp_path):
    from test_chats_pages import _sendable_page, _token
    from test_vk_commands import SENT
    app, client, tok, conn, tanya = _sendable_page(tmp_path, SENT)
    orders.ensure_pipeline(conn, NOW)
    pid = points.save_point(conn, {"name": "Бета", "manager_name": "А", "channel": "vk"})
    conn.execute("UPDATE point SET avito_account_id = ? WHERE id = ?", (tanya["account_id"], pid))     # the chat is of the point's account
    conn.commit()
    order, _ = mgr.hand_over(conn, pid, {"kind": "chat", "chat_row": tanya["id"]}, "89001112233", mgr.handoff_fields({}), token(), "point1", NOW)
    post(client, "/chats/%d/send" % tanya["id"], tok, text="Здравствуйте, подберём", reply_token=_token(client, tanya))
    assert conn.execute("SELECT taken_how FROM order_handoff_source WHERE order_id = ?", (order,)).fetchone()[0] == "ответил в чате"


# ---- review round 1 ------------------------------------------------------------------------------------------------------------------

def test_a_viewer_never_takes_the_client(w):
    """Д1: «взяли» is the work of who works the orders (owner, partner, the chats' manager), not of «Наблюдатель»."""
    conn = w["conn"]
    hand(w, phone="89001112233")
    order = handed(conn)[0]["order"]
    event = _event(conn, order)
    viewer, _ = person(w["app"], "viewer")
    assert viewer.get("/orders/%d" % order).status_code == 200
    viewer.get("/alerts/%d/go" % event["id"])
    assert "Взять в работу" not in viewer.get("/alerts").get_data(as_text=True)
    assert viewer.post("/alerts/%d/take" % event["id"], data={"csrf": "tok"}).status_code == 403
    assert handed(conn)[0]["taken"] is None


def test_a_chat_whose_handed_order_is_closed_is_handed_again_an_open_one_is_not(w):
    """Д2, Д7: a second press (another tab) while the client is in work makes nothing; after the order is closed the client is new."""
    conn = w["conn"]
    hand(w, phone="89001112233")
    first = handed(conn)[0]["order"]
    conn.execute("UPDATE avito_chat SET order_id = NULL WHERE id = ?", (w["petr"]["id"],))        # the chat let go, the order still open
    conn.commit()
    hand(w, phone="89001112233")
    assert [h["order"] for h in handed(conn)] == [first]
    conn.execute("UPDATE avito_chat SET order_id = ? WHERE id = ?", (first, w["petr"]["id"]))
    conn.execute("UPDATE order_card SET stage_id = (SELECT id FROM order_stage WHERE code = 'done') WHERE id = ?", (first,))
    conn.commit()
    hand(w, phone="89001112233", size="185/65 R15")
    assert len(handed(conn)) == 2 and chats.get_chat(conn, w["petr"]["id"])["order_id"] == first   # a new order; the chat stays


def test_the_button_is_not_shown_for_a_chat_of_another_points_order_and_hand_over_checks_the_chat(w):
    """Д8, Д10: the page offers what the POST allows; hand_over itself refuses a chat of another account or of another point's order."""
    conn = w["conn"]
    conn.execute("UPDATE avito_chat SET order_id = ? WHERE id = ?", (w["other"], w["petr"]["id"]))    # Пётр's chat, an order of Гамма
    conn.commit()
    assert "Передать под заказ" not in w["manager"].get("/chats/%d" % w["petr"]["id"]).get_data(as_text=True)
    with pytest.raises(orders.OrderError):
        mgr.hand_over(conn, w["rust"], {"kind": "chat", "chat_row": w["petr"]["id"]}, "89001112233", mgr.handoff_fields({}), token(), "point1", NOW)
    with pytest.raises(orders.OrderError):                                                          # Татьяна: the chat of the main account
        mgr.hand_over(conn, w["rust"], {"kind": "chat", "chat_row": w["tanya"]["id"]}, "89001112233", mgr.handoff_fields({}), token(), "point1", NOW)
    assert handed(conn) == []


def test_the_alert_opened_is_seen_and_an_alert_without_an_order_is_404(w):
    """Д9: the row «Клиент хочет…» goes from «Уведомления» once the staff opened it."""
    conn = w["conn"]
    hand(w, phone="89001112233")
    event = _event(conn, handed(conn)[0]["order"])
    w["owner"].get("/alerts/%d/go" % event["id"])
    assert 'href="/alerts/%d/go"' % event["id"] not in w["owner"].get("/alerts").get_data(as_text=True)
    assert w["owner"].get("/alerts/999999/go").status_code == 404


def test_an_early_order_keeps_its_responsible_without_anybody_on_duty_and_no_line_twice(w):
    """Д5 and the review's question 3: nobody on duty never wipes the responsible; the line «Что нужно» already there is not added twice."""
    conn = w["conn"]
    orders.update_card(conn, w["mine"], {"responsible": "pavel"}, NOW, "o")
    order_wait.add_want(conn, w["mine"], {"width": "205", "profile": "55", "radius": "16", "qty": "4"}, NOW, "o")
    conn.execute("UPDATE avito_chat SET order_id = ? WHERE id = ?", (w["mine"], w["petr"]["id"]))
    conn.commit()
    hand(w, phone="89001112233", size="205/55 R16")
    assert orders.get_card(conn, w["mine"])["responsible"] == "pavel" and len(order_wait.list_wants(conn, w["mine"])) == 1


def test_a_race_says_what_happened(w, monkeypatch):
    """Д4: the order of the chat moved meanwhile: the manager reads that, not «напишите владельцу»."""
    conn = w["conn"]
    conn.execute("UPDATE avito_chat SET order_id = ? WHERE id = ?", (w["mine"], w["petr"]["id"]))
    conn.commit()
    real = orders.update_card

    def moved(c, card_id, changes, now, author, commit=True):                                     # somebody moves the order in between
        c.execute("UPDATE order_card SET stage_id = (SELECT id FROM order_stage WHERE code = 'talk') WHERE id = ?", (card_id,))
        return real(c, card_id, changes, now, author, commit=commit)
    monkeypatch.setattr(orders, "update_card", moved)
    with pytest.raises(orders.OrderError, match="только что изменили"):
        mgr.hand_over(conn, w["rust"], {"kind": "chat", "chat_row": w["petr"]["id"]}, "89001112233", mgr.handoff_fields({}), token(), "point1", NOW)
    assert handed(conn) == []


def test_any_refusal_of_the_keyboard_still_answers_the_point(tmp_path):
    """Д6: VK refuses the keyboard with another code (911): the answer goes without it."""
    from test_vk_commands import ScriptedHttp
    app, client, tok, conn, pid, card = ready(tmp_path)
    app.config["VK_HTTP"] = ScriptedHttp((200, {"error": {"error_code": 911, "error_msg": "Keyboard format is invalid"}}), (200, {"response": 7}))
    hook(app, conn, "Передать под заказ", event="k1")
    calls = _sent(app)
    assert len(calls) == 2 and "keyboard" not in calls[1] and calls[1]["message"][0] == mgr.signed(mgr.BUTTON_ANSWER)


def test_the_fifty_go_to_the_point_that_handed_the_client_over_the_75_to_the_point_that_issued(tmp_path):
    """The lead 2026-10-23 (the owner to confirm): the fixed sum to the point that handed the client over (order_report.bonus_point)."""
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    beta = points.save_point(conn, {"name": "Бета", "manager_name": "А", "channel": "vk"})
    points.save_point(conn, {"name": "Гамма", "manager_name": "Б", "channel": "vk"})
    conn.execute("INSERT INTO order_handoff (order_id, message_id, point_id, created_at) VALUES (1, 'm1', ?, ?)", (beta, NOW))
    conn.commit()
    _sell(conn, 1, "Гамма", "2026-10-24")                                                         # handed by Бета, issued at Гамма
    rep = order_report.build(conn, "2026-10-01", "2026-10-31")
    by = {l["point"]: l for l in rep["lines"]}
    assert by["Гамма"]["fee"] == 75 and by["Бета"]["fee"] == 50 and by["Бета"]["orders"] == 0 and by["Бета"]["net"] == -50
    assert rep["handoff"]["lines"] == [{"point": "Бета", "clients": 1, "fee": 50.0}] and rep["total"]["fee"] == 125
    assert rep["total"]["owner"] + rep["total"]["partner"] == rep["total"]["net"] == by["Гамма"]["margin"] - 125
    assert [l["point"] for l in order_report.build(conn, "2026-10-01", "2026-10-31", "Бета")["lines"]] == ["Бета"]
    assert order_report.build(conn, "2026-10-01", "2026-10-31", "Гамма")["lines"][0]["fee"] == 75
    same = order_report.bonus_point
    try:
        order_report.BONUS_TO = "issued"                                                          # the other choice: one line to change
        assert same("Гамма", "Бета") == "Гамма"
        assert {l["point"]: l["fee"] for l in order_report.build(conn, "2026-10-01", "2026-10-31")["lines"]} == {"Гамма": 125}
    finally:
        order_report.BONUS_TO = "handed"


# ---- from a call (the calls lane): the same window in «Звонки» ---------------------------------------------------------------------

from test_calls import FakeClient, call as avito_call                                              # noqa: E402
from test_calls_panel import person as call_person, setup                                         # noqa: E402,F401
from tires_sync import calls, connections                                                         # noqa: E402


def hand_call(client, row, **form):
    data = dict({"phone": "", "size": "", "season": "", "brand": "", "comment": "", "token": token(), "csrf": "tok"}, **form)
    return client.post("/calls/%d/handoff" % row, data=data)


def test_the_point_hands_over_the_client_of_its_call_and_the_list_says_so(setup):
    app, conn, ids = setup
    orders.ensure_pipeline(conn, NOW)                                                             # the stages of the panel (cancel)
    manager = call_person(app, conn, "point", ids["point"])
    row = ids["rows"][3]
    page = manager.get("/calls").get_data(as_text=True)
    assert 'data-handoff-action="/calls/%d/handoff"' % row in page and 'data-handoff-phone="+7 911 333-00-00"' in page
    body = page[page.index("</title>"):]                                                        # Д11: the window is in the page, not in its title
    assert 'id="handoff"' in body and "handoff.js" in body and "handoff" not in page[:page.index("</title>")]
    assert hand_call(manager, row, phone="+7 911 333-00-00", size="205/55 R16", season="summer").status_code == 302
    [h] = handed(conn)
    assert (h["kind"], h["phone"], h["point"]) == ("call", "+79113330000", ids["point"])
    assert conn.execute("SELECT call_id FROM order_handoff_source").fetchone()[0] == str(row)
    assert any(e["text"].startswith(mgr.ASK) and "(звонок)" in e["text"] for e in orders.list_events(conn, h["order"]))
    hand_call(manager, row, phone="+7 911 333-00-00")
    assert len(handed(conn)) == 1                                                                 # the client of the call is in work already
    page = manager.get("/calls").get_data(as_text=True)
    assert "передан точкой · №%d" % h["order"] in page and 'data-handoff-action="/calls/%d/handoff"' % row not in page
    conn.execute("UPDATE order_card SET stage_id = (SELECT id FROM order_stage WHERE code = 'cancel') WHERE id = ?", (h["order"],))
    conn.commit()                                                                                # Д13: closed — the client may be handed over again
    page = manager.get("/calls").get_data(as_text=True)
    assert "передан точкой · №%d" % h["order"] in page and 'data-handoff-action="/calls/%d/handoff"' % row in page


def test_after_the_handoff_the_calls_page_keeps_its_filter(setup):
    """Д14: back to the list as it was (the period, the phone), on the row."""
    app, conn, ids = setup
    manager = call_person(app, conn, "point", ids["point"])
    page = manager.get("/calls?days=30&q=3330").get_data(as_text=True)
    assert 'name="back" value="/calls?days=30&amp;q=3330"' in page
    r = hand_call(manager, ids["rows"][3], phone="89113330000", back="/calls?days=30&q=3330")
    assert r.headers["Location"].endswith("/calls?days=30&q=3330#call-%d" % ids["rows"][3])
    r = hand_call(manager, ids["rows"][3], phone="89113330000", back="https://evil.example/")
    assert r.headers["Location"].endswith("/calls#call-%d" % ids["rows"][3])                    # only a page of «Звонки»


def test_only_the_point_of_the_calls_account_hands_it_over(setup):
    app, conn, ids = setup
    manager = call_person(app, conn, "point", ids["point"])
    assert hand_call(manager, ids["rows"][1], phone="89111234567").status_code == 403             # a call of the main account
    owner = call_person(app, conn, "owner")
    assert hand_call(owner, ids["rows"][3], phone="89113330000").status_code == 403               # the staff take, they do not hand over
    assert 'data-handoff-action' not in owner.get("/calls").get_data(as_text=True)
    with pytest.raises(orders.OrderError):
        mgr.hand_over(conn, ids["point"], {"kind": "call", "call_id": ids["rows"][1]}, "89111234567", mgr.handoff_fields({}), token(), "p", NOW)
    assert handed(conn) == []


def test_the_alert_of_a_call_leads_to_the_clients_card_and_listening_takes_the_client(setup):
    app, conn, ids = setup
    manager = call_person(app, conn, "point", ids["point"])
    hand_call(manager, ids["rows"][3], phone="89113330000")
    order = handed(conn)[0]["order"]
    event = _event(conn, order)
    assert manager.get("/calls/%d/record" % ids["rows"][3]).status_code == 200 and handed(conn)[0]["taken"] is None   # the point listens: not taken
    owner = call_person(app, conn, "owner")
    client = conn.execute("SELECT client_id FROM client_call WHERE call_row = ?", (ids["rows"][3],)).fetchone()[0]
    r = owner.get("/alerts/%d/go" % event["id"])
    assert r.status_code == 302 and r.headers["Location"].endswith("/clients/%d" % client)
    assert "передан точкой · №%d" % order in owner.get("/clients/%d" % client).get_data(as_text=True)
    conn.execute("UPDATE order_handoff_source SET taken_at = NULL")                               # once more: by listening
    conn.commit()
    assert owner.get("/calls/%d/record" % ids["rows"][3]).status_code == 200
    assert conn.execute("SELECT taken_how FROM order_handoff_source").fetchone()[0] == "прослушал звонок"


def test_a_call_of_the_client_to_the_owner_or_the_partner_after_the_handoff_takes_it(setup):
    app, conn, ids = setup
    manager = call_person(app, conn, "point", ids["point"])
    hand_call(manager, ids["rows"][3], phone="89113330000")
    order = handed(conn)[0]["order"]
    created = conn.execute("SELECT created_at FROM order_handoff").fetchone()[0]
    calls.sync_account(conn, FakeClient([[avito_call(9, at="2026-10-07T23:00:00+03:00", buyer="+79113330000")]]), ids["gamma"], NOW, lambda s: None)
    mgr.handoff_tick(conn, "2026-10-07T23:10:00")
    assert handed(conn)[0]["taken"] is None                                                       # a call to the point itself is not «взяли»
    other_acc = connections.create_account(conn, "Альфа", NOW, "o")                         # Д12: another point's account is no call of ours
    points.save_point(conn, {"name": "Альфа", "manager_name": "Б", "channel": "vk", "avito_account_id": other_acc})
    calls.sync_account(conn, FakeClient([[avito_call(11, at="2026-10-07T23:20:00+03:00", buyer="+79113330000")]]), other_acc, NOW, lambda s: None)
    mgr.handoff_tick(conn, "2026-10-07T23:25:00")
    assert handed(conn)[0]["taken"] is None
    calls.sync_account(conn, FakeClient([[avito_call(10, at="2026-10-07T23:30:00+03:00", buyer="+79113330000")]]), ids["main"], NOW, lambda s: None)
    mgr.handoff_tick(conn, "2026-10-07T23:40:00")
    how = conn.execute("SELECT taken_how, taken_by FROM order_handoff_source WHERE order_id = ?", (order,)).fetchone()
    assert how[0].startswith("позвонил") and how[1] == mgr.CALLED and created < "2026-10-07T23:30:00"


def test_the_point_hears_only_the_confirmation_in_vk(tmp_path):
    """The owner 2026-10-23: «Точка по-факту не должна ничего получать, просто после передачи подтверждение от бота получать и все»."""
    app, client, tok, conn, pid, card = ready(tmp_path)
    hook(app, conn, "клиент 89002223344 шины", event="c1")                 # not the phone of the open order of ready()
    order = handed(conn)[0]["order"]
    orders.move_card(conn, order, next(s["id"] for s in orders.list_stages(conn) if s["code"] == "talk"), NOW, "worker2")
    hook(app, conn, "привет", event="c2")                                                       # the look after the message: no status
    orders.move_card(conn, order, next(s["id"] for s in orders.list_stages(conn) if s["code"] == "cancel"), NOW, "worker2", {"cancel_reason": "нет"})
    hook(app, conn, "ау", event="c3")
    messages = [m["message"][0] for m in _sent(app)]
    assert messages[0] == "(бот) Клиент передан: +7 900 222-33-44, заказ №%d." % order and messages[1:] == [mgr.signed(mgr.HINT)]


def test_the_report_offers_a_point_paid_only_for_the_clients_it_handed_over(tmp_path):
    """Хвост З8: «Бета» sold nothing in the period, it only handed a client over: it is in the list of points of the report."""
    from datetime import datetime
    from panel_helpers import make_app
    from test_share_labels import login
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 25, 9, 0, 0))
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    beta = points.save_point(conn, {"name": "Бета", "manager_name": "А", "channel": "vk"})
    points.save_point(conn, {"name": "Гамма", "manager_name": "Б", "channel": "vk"})
    conn.execute("INSERT INTO order_handoff (order_id, message_id, point_id, created_at) VALUES (1, 'm1', ?, ?)", (beta, NOW))
    conn.commit()
    _sell(conn, 1, "Гамма", "2026-10-24")
    for chosen in ("", "Гамма", "Бета"):
        html = client.get("/orders/report", query_string={"from": "2026-10-01", "to": "2026-10-31", "pt": chosen}).get_data(as_text=True)
        assert '<option value="Бета"' in html and '<option value="Гамма"' in html
    html = client.get("/orders/report", query_string={"from": "2026-10-01", "to": "2026-10-31", "pt": "Бета"}).get_data(as_text=True)
    assert '<option value="Бета" selected>' in html and "<td>Бета</td>" in html
    assert "командой ВК, с «Рабочего места», из чатов и звонков" in html and "той точки, которая передала клиента" in html   # хвост З9
    conn.execute("INSERT INTO order_handoff (order_id, message_id, point_id, created_at) VALUES (2, 'm2', NULL, ?)", (NOW,))   # its point is gone
    conn.commit()
    _sell(conn, 2, "", "2026-10-24")                                                             # issued without a point
    html = client.get("/orders/report", query_string={"from": "2026-10-01", "to": "2026-10-31"}).get_data(as_text=True)
    assert '<option value="%s"' % order_report.NO_POINT not in html                             # review 1: no made-up point in the filter


# ---- хвост З20: a GET that does something does it only inside the panel or opened directly (Sec-Fetch-Site) ------------------------------

SITES = [({"Sec-Fetch-Site": "same-origin"}, True), ({"Sec-Fetch-Site": "none"}, True), ({}, True), ({"Sec-Fetch-Site": "cross-site"}, False),
         ({"Sec-Fetch-Site": "same-site"}, False)]


@pytest.mark.parametrize("headers, acts", SITES)
def test_the_alert_takes_the_client_only_from_the_panel_or_opened_directly(w, headers, acts):
    conn = w["conn"]
    hand(w, phone="89001112233")
    [h] = handed(conn)
    r = w["owner"].get("/alerts/%d/go" % _event(conn, h["order"])["id"], headers=headers)
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/chats/%d" % w["petr"]["id"] if acts else "/orders/%d" % h["order"])
    assert bool(handed(conn)[0]["taken"]) is acts


@pytest.mark.parametrize("headers, acts", SITES)
def test_the_order_page_takes_the_client_and_marks_the_journal_seen_only_from_the_panel_or_directly(w, headers, acts):
    conn = w["conn"]
    hand(w, phone="89001112233")
    order = handed(conn)[0]["order"]
    page = w["owner"].get("/orders/%d" % order, headers=headers)
    assert page.status_code == 200 and "№%d" % order in page.get_data(as_text=True)              # the page itself always opens
    assert bool(handed(conn)[0]["taken"]) is acts
    seen = conn.execute("SELECT COUNT(*) FROM order_event_seen WHERE order_id = ?", (order,)).fetchone()[0]
    assert bool(seen) is acts


@pytest.mark.parametrize("headers, acts", SITES)
def test_listening_takes_the_client_and_is_journaled_only_from_the_panel_or_directly(setup, headers, acts):
    app, conn, ids = setup
    manager = call_person(app, conn, "point", ids["point"])
    hand_call(manager, ids["rows"][3], phone="89113330000")
    owner = call_person(app, conn, "owner")
    listened = lambda: conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'call_listened'").fetchone()[0]       # noqa: E731
    before = listened()
    assert owner.get("/calls/%d/record" % ids["rows"][3], headers=headers).status_code == 200      # the recording always plays
    assert bool(handed(conn)[0]["taken"]) is acts and (listened() > before) is acts


# ---- хвост З10b (match3, lane misc-tails): two tabs pressed at once make one order --------------------------------------------------

def _the_other_tab_first(monkeypatch, db_path, point, source, phone):
    """The other tab's press runs whole (its own connection) after this press has checked the chat or the call and before it writes."""
    real, done = mgr.on_duty, []

    def between(conn, now):
        if not done:
            done.append(True)
            other = store.connect(db_path)
            done.append(mgr.hand_over(other, point, source, phone, mgr.handoff_fields({}), token(), "point1", NOW)[0])
            other.close()
        return real(conn, now)
    monkeypatch.setattr(mgr, "on_duty", between)
    return done


def test_z10b_two_tabs_of_a_chat_without_an_order_make_one_order(w, monkeypatch):
    conn, chat_row = w["conn"], w["petr"]["id"]
    done = _the_other_tab_first(monkeypatch, w["app"].config["DB_PATH"], w["rust"], {"kind": "chat", "chat_row": chat_row}, "89001112233")
    first = token()
    with pytest.raises(orders.OrderError, match="У клиента уже есть открытый заказ №"):      # the other tab's order holds the phone (2026-10-27)
        mgr.hand_over(conn, w["rust"], {"kind": "chat", "chat_row": chat_row}, "89001112233", mgr.handoff_fields({}), first, "point1", NOW)
    other = done[1]
    assert [h["order"] for h in handed(conn)] == [other] and chats.get_chat(conn, chat_row)["order_id"] == other
    assert conn.execute("SELECT COUNT(*) FROM order_card WHERE tags LIKE ?", ("%" + mgr.HANDOFF_TAG + "%",)).fetchone()[0] == 1
    outcome = conn.execute("SELECT outcome, order_id FROM order_command_log WHERE message_id = ?", ("chat-" + first,)).fetchone()
    assert outcome[0] == "не передан: У клиента уже есть открытый заказ №%d — откройте его." % other and outcome[1] is None


def test_z10b_two_tabs_of_a_chat_without_a_phone_make_one_order_by_the_chat_itself(w, monkeypatch):
    """Without a phone the rule of one open order per phone says nothing: the second look by the chat (open_handoff_of_chat) still holds."""
    conn, chat_row = w["conn"], w["petr"]["id"]
    done = _the_other_tab_first(monkeypatch, w["app"].config["DB_PATH"], w["rust"], {"kind": "chat", "chat_row": chat_row}, "")
    with pytest.raises(orders.OrderError, match="Клиент по этому чату уже передан, заказ №"):
        mgr.hand_over(conn, w["rust"], {"kind": "chat", "chat_row": chat_row}, "", mgr.handoff_fields({}), token(), "point1", NOW)
    assert [h["order"] for h in handed(conn)] == [done[1]]


def test_z10b_two_tabs_of_a_call_make_one_order(setup, monkeypatch):
    app, conn, ids = setup
    orders.ensure_pipeline(conn, NOW)
    row = ids["rows"][3]
    done = _the_other_tab_first(monkeypatch, app.config["DB_PATH"], ids["point"], {"kind": "call", "call_id": row}, "89113330000")
    with pytest.raises(orders.OrderError, match="У клиента уже есть открытый заказ №"):      # the other tab's order holds the phone (2026-10-27)
        mgr.hand_over(conn, ids["point"], {"kind": "call", "call_id": row}, "89113330000", mgr.handoff_fields({}), token(), "p", NOW)
    assert [h["order"] for h in handed(conn)] == [done[1]]


def test_z10b_an_early_order_of_the_chat_and_a_closed_one_are_as_before(w):
    """Regression: the second look is only for a new order; the chat's own early order is handed over itself, a closed one is no hindrance."""
    conn, chat_row = w["conn"], w["petr"]["id"]
    conn.execute("UPDATE avito_chat SET order_id = ? WHERE id = ?", (w["mine"], chat_row))
    conn.commit()
    order, _ = mgr.hand_over(conn, w["rust"], {"kind": "chat", "chat_row": chat_row}, "89001112233", mgr.handoff_fields({}), token(), "point1", NOW)
    assert order == w["mine"] and [h["order"] for h in handed(conn)] == [w["mine"]]
    conn.execute("UPDATE order_card SET stage_id = (SELECT id FROM order_stage WHERE code = 'done') WHERE id = ?", (w["mine"],))
    conn.commit()
    again, _ = mgr.hand_over(conn, w["rust"], {"kind": "chat", "chat_row": chat_row}, "89001112233", mgr.handoff_fields({}), token(), "point1", NOW)
    assert again != w["mine"] and len(handed(conn)) == 2
