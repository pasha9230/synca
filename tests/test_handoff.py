"""Clients handed over by the points: «клиент <телефон> <что нужно>» in VK makes an order on «Передано с точки» for the person on duty;
15 working minutes to take it, then a red record; the point hears «взят», «оформлен», «выдан», «отказ» once each; the fixed sum in the report."""
from __future__ import annotations

import json
import urllib.parse

import pytest

from test_vk_commands import PEER, hook, ready
from tires_sync import order_manager as mgr, order_points as points, order_report, orders, sales, store

NOW = "2026-10-21T12:00:00"                     # a Wednesday, Moscow time


@pytest.fixture
def base(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    pid = points.save_point(conn, {"name": "Бета", "manager_name": "Алексей", "channel": "vk", "vk_peer_id": PEER})
    conn.execute("UPDATE point SET vk_peer_id = ? WHERE id = ?", (PEER, pid))
    conn.commit()
    return conn, pid


def _stage(conn, code):
    return next(s for s in orders.list_stages(conn) if s["code"] == code)


def test_the_command_makes_an_order_for_the_person_on_duty_and_a_repeat_makes_nothing(base):
    conn, pid = base
    mgr.set_duty(conn, {2: "worker2", 3: "pavel"}, (10, 20), "", NOW[:10])
    told = []
    answer = mgr.dispatch(conn, "vk-1", PEER, "Клиент 8 (900) 111-22-33 нужны зимние 205/55 R16, 4 шт", None, NOW,
                          notify=lambda card, text: told.append((card["id"], text)) or "отправлено")
    card_id = conn.execute("SELECT order_id FROM order_handoff").fetchone()[0]
    card = orders.get_card(conn, card_id)
    assert answer == "Клиент передан: +7 900 111-22-33, заказ №%d." % card_id                # only the confirmation (2026-10-23)
    assert card["stage"]["code"] == "from_point" and card["client_phone"] == "+79001112233"
    assert card["source"] == "pvz" and card["point_id"] == pid and card["responsible"] == "worker2" and "205/55 R16" in card["comment"]
    assert told and told[0][0] == card_id and "Бета" in told[0][1]
    assert any(e["level"] == "yellow" and e["text"].startswith(mgr.ASK) and "Бета" in e["text"]           # the owner's words 2026-10-23 (п. 20)
               for e in orders.list_events(conn, card_id))
    assert mgr.HANDOFF_TAG in card["tags"]
    assert mgr.dispatch(conn, "vk-1", PEER, "Клиент 89001112233 шины", None, NOW) == "Это сообщение уже обработано."
    assert conn.execute("SELECT COUNT(*) FROM order_handoff").fetchone()[0] == 1
    assert [s["code"] for s in orders.list_stages(conn)][:2] == ["from_point", "new"]          # before «Новый заказ» (the owner 2026-10-23)
    mgr.dispatch(conn, "vk-2", PEER, "клиент +7 900 222-33-44 летние", None, NOW)
    assert [s["code"] for s in orders.list_stages(conn)].count("from_point") == 1           # the stage is made once


def test_what_is_not_a_handoff_goes_as_before(base):
    conn, _ = base
    assert mgr.parse_handoff("клиент не пришёл 5000100") is None                             # the old command keeps working
    assert mgr.parse_handoff("клиент приедет завтра в 10") is None
    assert mgr.parse_handoff("клиент 12345 шины") is None                                     # not a phone
    assert mgr.parse_handoff("клиент: +7 (900) 111-22-33") == {"phone": "+79001112233", "need": ""}
    assert mgr.dispatch(conn, "vk-9", "999", "клиент 89001112233 шины", None, NOW) == ""          # a stranger: no order
    assert conn.execute("SELECT COUNT(*) FROM order_handoff").fetchone()[0] == 0


def test_the_person_on_duty_by_the_day_the_change_for_today_and_the_fallback(base):
    conn, _ = base
    orders.set_default_responsible(conn, "owner")
    assert mgr.on_duty(conn, NOW) == "owner"                                                  # nobody set: the usual responsible
    mgr.set_duty(conn, {2: "worker2", 3: "pavel"}, (10, 20), "", NOW[:10])
    assert mgr.on_duty(conn, NOW) == "worker2" and mgr.on_duty(conn, "2026-10-22T09:00:00") == "pavel"
    mgr.set_duty(conn, {2: "worker2", 3: "pavel"}, (10, 20), "pavel", NOW[:10])
    assert mgr.on_duty(conn, NOW) == "pavel" and mgr.on_duty(conn, "2026-10-28T12:00:00") == "worker2"   # the change is for that day only
    with pytest.raises(orders.OrderError):
        mgr.set_duty(conn, {}, (20, 10), "", NOW[:10])


def test_fifteen_working_minutes(base):
    assert str(mgr.take_deadline("2026-10-21T12:00:00", (10, 20))) == "2026-10-21 12:15:00"
    assert str(mgr.take_deadline("2026-10-21T19:50:00", (10, 20))) == "2026-10-22 10:05:00"   # the clock stops at 20:00
    assert str(mgr.take_deadline("2026-10-21T23:30:00", (10, 20))) == "2026-10-22 10:15:00"
    assert str(mgr.take_deadline("2026-10-21T07:00:00", (10, 20))) == "2026-10-21 10:15:00"


def test_not_taken_in_fifteen_minutes_is_one_red_record_and_a_telegram(base):
    conn, _ = base
    mgr.dispatch(conn, "vk-1", PEER, "клиент 89001112233 шины", None, NOW)
    card_id = conn.execute("SELECT order_id FROM order_handoff").fetchone()[0]
    told = []
    notify = lambda card, text: told.append(text) or "отправлено"                              # noqa: E731
    assert mgr.handoff_tick(conn, "2026-10-21T12:14:59", notify=notify)["overdue"] == 0
    assert mgr.handoff_tick(conn, "2026-10-21T12:15:00", notify=notify)["overdue"] == 1
    assert mgr.handoff_tick(conn, "2026-10-21T12:40:00", notify=notify)["overdue"] == 0          # once
    red = [e for e in orders.list_events(conn, card_id) if e["level"] == "red"]
    assert len(red) == 1 and "15 минут" in red[0]["text"] and len(told) == 1
    mgr.dispatch(conn, "vk-2", PEER, "клиент 89002223344 шины", None, NOW)
    other = max(r[0] for r in conn.execute("SELECT order_id FROM order_handoff"))
    orders.move_card(conn, other, _stage(conn, "new")["id"], NOW, "worker2")                    # taken in time: no red record
    assert mgr.handoff_tick(conn, "2026-10-21T13:00:00")["overdue"] == 0


def test_nothing_goes_to_the_point_in_vk_and_each_status_is_only_marked(base):
    """The owner 2026-10-23: the point gets in VK only the confirmation of the handoff (it replaces the statuses of 2026-10-21); the status
    is marked once in order_handoff_status, so that a finished client is not looked at again."""
    conn, _ = base
    mgr.dispatch(conn, "vk-1", PEER, "клиент 89001112233 шины", None, NOW)
    card_id = conn.execute("SELECT order_id FROM order_handoff").fetchone()[0]

    def marked(order_id):
        return dict(conn.execute("SELECT status, outcome FROM order_handoff_status WHERE order_id = ?", (order_id,)).fetchall())
    mgr.handoff_tick(conn, NOW)
    assert marked(card_id) == {}                                                                  # still on «Передано с точки»
    orders.move_card(conn, card_id, _stage(conn, "talk")["id"], NOW, "worker2")
    mgr.handoff_tick(conn, NOW)
    mgr.handoff_tick(conn, NOW)
    assert marked(card_id) == {"taken": mgr.NOT_SENT}
    orders.move_card(conn, card_id, _stage(conn, "cancel")["id"], NOW, "worker2", {"cancel_reason": "передумал"})
    mgr.handoff_tick(conn, NOW)
    assert marked(card_id) == {"taken": mgr.NOT_SENT, "refused": mgr.NOT_SENT}
    mgr.dispatch(conn, "vk-2", PEER, "клиент 89002223344 шины", None, NOW)
    jumped = max(r[0] for r in conn.execute("SELECT order_id FROM order_handoff"))
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (_stage(conn, "done")["id"], jumped))   # straight to «Выполнено»
    conn.commit()
    mgr.handoff_tick(conn, NOW)
    assert marked(jumped) == {"done": mgr.NOT_SENT}
    assert not any("точке в ВК" in e["text"].lower() for o in (card_id, jumped) for e in orders.list_events(conn, o))


def test_a_voice_message_with_and_without_vks_transcript(base):
    """Without a transcript: a line of «Входящие», no order (the owner 2026-10-21; was an order «нужна расшифровка», tests/test_voice_inbox.py)."""
    conn, _ = base
    mgr.dispatch(conn, "vk-1", PEER, "", None, NOW, voice="pending")
    assert conn.execute("SELECT COUNT(*) FROM order_handoff").fetchone()[0] == 0 and mgr.list_inbox(conn)[0]["text"].startswith("(голосовое, расшифровки нет)")
    mgr.dispatch(conn, "vk-2", PEER, "клиент 8 900 111 22 33 нужны летние шины", None, NOW, voice="done")
    voiced = max(r[0] for r in conn.execute("SELECT order_id FROM order_handoff"))
    assert orders.get_card(conn, voiced)["comment"] == "нужны летние шины (из голосового)"


def test_through_the_vk_hook_with_a_transcript_the_answer_and_no_status(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    msg = {"id": 777, "from_id": int(PEER), "peer_id": int(PEER), "text": "",
           "attachments": [{"type": "audio_message", "audio_message": {"transcript": "клиент 89001112233 нужны шины", "transcript_state": "done"}}]}
    body = {"type": "message_new", "group_id": 220945892, "secret": "Sup3r-Secret_key", "event_id": "ev-voice", "object": {"message": msg}}
    from tires_sync import vk_callback
    url = "/hook/vk/%s" % vk_callback.path_token(conn)
    assert app.test_client().post(url, data=json.dumps(body), content_type="application/json").get_data(as_text=True) == "ok"
    assert app.test_client().post(url, data=json.dumps(body), content_type="application/json").get_data(as_text=True) == "ok"   # VK repeats
    handed = conn.execute("SELECT order_id FROM order_handoff").fetchall()
    assert len(handed) == 1
    sent = [urllib.parse.parse_qs(c[3].decode("utf-8"))["message"][0] for c in app.config["VK_HTTP"].calls]
    assert any(s.startswith("(бот) Клиент передан") for s in sent)                        # the bot signs its answers (the owner 2026-10-24)
    orders.move_card(conn, handed[0][0], _stage(conn, "talk")["id"], NOW, "o")
    hook(app, conn, "спасибо", event="ev-next")                                              # any later event looks at the handed-over clients
    sent = [urllib.parse.parse_qs(c[3].decode("utf-8"))["message"][0] for c in app.config["VK_HTTP"].calls]
    assert not any("взят в работу" in s for s in sent)                                       # nothing but the confirmation (2026-10-23)


# --- the report --------------------------------------------------------------------------------------------------------------------

def _sell(conn, order_id, point, day):
    sales.add_sale(conn, day=day, goods_id="g%d" % order_id, brand_key="nokian", season="winter", diameter=16, name="Nokian", ad_kind="single",
                   pieces=4, sale_total=20000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, account_id=None, point=point,
                   note="Заказ №%d" % order_id, now=NOW, author="o", today="2026-10-21", source="order")


def test_the_fixed_sum_is_zero_by_default_and_follows_the_dated_rate(base):
    conn, _ = base
    mgr.dispatch(conn, "vk-1", PEER, "клиент 89001112233 шины", None, NOW)
    handed = conn.execute("SELECT order_id FROM order_handoff").fetchone()[0]
    plain = orders.create_card(conn, {"client_name": "Свой"}, NOW, "o")
    _sell(conn, handed, "Бета", "2026-10-10")
    _sell(conn, plain, "Бета", "2026-10-10")
    line = order_report.build(conn, "2026-10-01", "2026-10-31")["lines"][0]
    assert (line["fee"], line["net"]) == (150, 8000 - 150)                                    # 0 by default: the report as before
    rep = order_report.build(conn, "2026-10-01", "2026-10-31")
    assert rep["handoff"] == {"lines": [{"point": "Бета", "clients": 1, "fee": 0.0}], "clients": 1, "fee": 0.0}
    assert order_report.set_rates(conn, "2026-10-21", "2026-10-21", NOW, "owner", handoff_fee=300) == ["handoff_fee"]
    _sell(conn, handed + 100, "Бета", "2026-10-21")                                          # not a handed-over client
    conn.execute("INSERT INTO order_handoff (order_id, message_id, created_at) VALUES (?, 'vk-x', ?)", (handed + 200, NOW))
    _sell(conn, handed + 200, "Бета", "2026-10-21")
    rep = order_report.build(conn, "2026-10-01", "2026-10-31")
    assert rep["lines"][0]["fee"] == 75 * 4 + 300 and rep["handoff"]["clients"] == 2 and rep["handoff"]["fee"] == 300   # only from the date
    assert order_report.build(conn, "2026-10-01", "2026-10-20")["lines"][0]["fee"] == 150


def test_the_duty_is_set_on_the_orders_settings_page(tmp_path):
    from datetime import datetime
    from panel_helpers import login, make_app, post
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 21, 9, 0, 0))                      # 12:00 in Moscow, a Wednesday
    client = app.test_client()
    tok = login(client)
    page = client.get("/orders/settings").get_data(as_text=True)
    assert "Клиенты с точек: дежурный" in page and 'name="day6"' in page
    post(client, "/orders/settings/duty", tok, day2="owner", start="9", end="21", today="")
    conn = store.connect(app.config["DB_PATH"])
    assert mgr.duty_settings(conn)["week"] == {2: "owner"} and mgr.duty_settings(conn)["hours"] == [9, 21]
    post(client, "/orders/settings/duty", tok, day2="owner", start="9", end="21", today="worker2")
    assert mgr.on_duty(conn, "2026-10-21T12:00:00") == "worker2" and mgr.on_duty(conn, "2026-10-28T12:00:00") == "owner"
    post(client, "/orders/settings/duty", tok, start="21", end="9")
    assert mgr.duty_settings(conn)["hours"] == [9, 21]                                        # refused: nothing changed


# --- review round 1 -------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, phone, need", [
    ("клиент 89001234567 205/55 R16 4 шт", "+79001234567", "205/55 R16 4 шт"),
    ("клиент +79001234567 4 колеса", "+79001234567", "4 колеса"),
    ("Клиент 8 (900) 123-45-67 2 шины", "+79001234567", "2 шины"),
    ("клиент 9001234567, R17", "+79001234567", "R17"),
])
def test_the_phone_is_read_when_the_need_starts_with_a_number(text, phone, need):
    assert mgr.parse_handoff(text) == {"phone": phone, "need": need}


def test_without_anybody_on_duty_the_staff_alerts_go_to_the_owner_never_to_the_point(base):
    conn, _ = base
    from tires_sync import order_notify
    routes = []
    notify = lambda card, text: routes.append(order_notify.route(conn, card)) or "отправлено"   # noqa: E731
    mgr.dispatch(conn, "vk-1", PEER, "клиент 89001112233 шины", None, NOW, notify=notify)          # nobody on duty, no usual responsible
    mgr.handoff_tick(conn, "2026-10-21T13:00:00", notify=notify)
    assert len(routes) == 2 and all(r["channel"] != "vk" for r in routes)


def test_a_transcript_is_used_only_for_a_handoff_never_as_an_order_command(base):
    conn, _ = base
    answer = mgr.dispatch(conn, "vk-1", PEER, "", None, NOW, voice="done", transcript="приехал 5000100")
    assert "Принято" not in answer and conn.execute("SELECT COUNT(*) FROM order_command_log WHERE word = 'приехал'").fetchone()[0] == 0
    assert conn.execute("SELECT text FROM manager_message").fetchone()[0].startswith("(голосовое)")
    mgr.dispatch(conn, "vk-2", PEER, "", None, NOW, voice="done", transcript="клиент 89001112233 шины")
    assert conn.execute("SELECT COUNT(*) FROM order_handoff").fetchone()[0] == 1


def test_the_stage_goes_right_before_the_new_orders_and_a_user_stage_keeps_its_place(base):
    """The owner 2026-10-23: «Передано с точки» first, before «Новый заказ» (it was right after it)."""
    conn, _ = base
    orders.add_stage(conn, "Ждём звонка", "#123456", "open", "", "")
    before = [s["code"] or s["name"] for s in orders.list_stages(conn)]
    orders.ensure_from_point_stage(conn)
    after = [s["code"] or s["name"] for s in orders.list_stages(conn)]
    assert after == ["from_point"] + before
    assert [s["position"] for s in orders.list_stages(conn)] == list(range(len(after)))


def test_the_look_skips_the_clients_already_finished(base, monkeypatch):
    conn, _ = base
    mgr.dispatch(conn, "vk-1", PEER, "клиент 89001112233 шины", None, NOW)
    card_id = conn.execute("SELECT order_id FROM order_handoff").fetchone()[0]
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (_stage(conn, "done")["id"], card_id))
    conn.commit()
    mgr.handoff_tick(conn, NOW)
    looked = []
    real = orders.get_card
    monkeypatch.setattr(orders, "get_card", lambda c, i: looked.append(i) or real(c, i))
    mgr.handoff_tick(conn, NOW)
    assert looked == []


def test_a_voice_message_cut_before_it_was_handled_shows_in_the_inbox(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    from tires_sync import vk_callback
    from datetime import datetime
    msg = {"id": 901, "from_id": int(PEER), "peer_id": int(PEER), "text": "", "attachments": [{"type": "audio_message", "audio_message": {}}]}
    data = {"type": "message_new", "group_id": 220945892, "event_id": "ev-cut", "object": {"message": msg}}
    vk_callback.started(conn, datetime(2026, 10, 21, 8, 0, 0))
    conn.execute("INSERT INTO vk_event (event_id, at, type, group_id, payload) VALUES (?,?,?,?,?)",
                 ("ev-cut", "2026-10-21T08:30:00", "message_new", "220945892", json.dumps(data)))
    conn.commit()
    assert vk_callback.sweep(conn, datetime(2026, 10, 21, 9, 0, 0)) == 1
    assert "голосовое" in conn.execute("SELECT text FROM manager_message ORDER BY id DESC").fetchone()[0]


def test_chats_sync_looks_at_the_handed_over_clients_and_a_failure_there_never_fails_it(tmp_path, monkeypatch):
    from tires_sync import cli
    from tires_sync.secretbox import create_key_file
    conn = store.connect(tmp_path / "t.db")
    orders.ensure_pipeline(conn, NOW)
    conn.close()
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    calls = []
    monkeypatch.setattr(mgr, "handoff_tick", lambda *a, **k: calls.append(a[1]) or {})
    assert cli.main(["chats-sync", "--db", str(tmp_path / "t.db"), "--key-file", str(key_file)]) == 0 and len(calls) == 1

    def broken(*a, **k):
        raise RuntimeError("сломалось")
    monkeypatch.setattr(mgr, "handoff_tick", broken)
    assert cli.main(["chats-sync", "--db", str(tmp_path / "t.db"), "--key-file", str(key_file)]) == 0


def test_the_report_page_shows_the_handed_over_clients_and_saves_their_rate(tmp_path):
    from datetime import datetime
    from panel_helpers import login, make_app, post
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 21, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    pid = points.save_point(conn, {"name": "Бета", "manager_name": "А", "channel": "vk"})
    conn.execute("UPDATE point SET vk_peer_id = ? WHERE id = ?", (PEER, pid))
    conn.commit()
    mgr.dispatch(conn, "vk-1", PEER, "клиент 89001112233 шины", None, NOW)
    _sell(conn, conn.execute("SELECT order_id FROM order_handoff").fetchone()[0], "Бета", "2026-10-10")
    page = client.get("/orders/report?from=2026-10-01&to=2026-10-31").get_data(as_text=True)
    assert "Переданные клиенты" in page and 'name="handoff_fee" inputmode="decimal" required value="0"' in page
    post(client, "/orders/report/rates", tok, point_fee="75", point_fee_was="75", owner_pct="50", owner_pct_was="50", handoff_fee="300",
         handoff_fee_was="0", valid_from="2026-10-22")
    assert [(r["name"], r["value"]) for r in order_report.list_rates(conn)] == [("handoff_fee", 300.0)]
    page = client.get("/orders/report").get_data(as_text=True)
    assert "За переданного клиента" in page and "300 ₽" in page


def test_only_the_word_klient_starts_a_handoff_and_a_cut_voice_keeps_its_transcript(tmp_path):
    assert mgr.parse_handoff("Клиенту 89001234567 перезвонить") is None
    assert mgr.parse_handoff("клиента 89001234567 нет") is None
    app, client, tok, conn, pid, card = ready(tmp_path)
    from tires_sync import vk_callback
    from datetime import datetime
    msg = {"id": 902, "from_id": int(PEER), "peer_id": int(PEER), "text": "",
           "attachments": [{"type": "audio_message", "audio_message": {"transcript": "где заказ", "transcript_state": "done"}}]}
    data = {"type": "message_new", "group_id": 220945892, "event_id": "ev-cut2", "object": {"message": msg}}
    vk_callback.started(conn, datetime(2026, 10, 21, 8, 0, 0))
    conn.execute("INSERT INTO vk_event (event_id, at, type, group_id, payload) VALUES (?,?,?,?,?)",
                 ("ev-cut2", "2026-10-21T08:30:00", "message_new", "220945892", json.dumps(data)))
    conn.commit()
    vk_callback.sweep(conn, datetime(2026, 10, 21, 9, 0, 0))
    assert "где заказ" in conn.execute("SELECT text FROM manager_message ORDER BY id DESC").fetchone()[0]


# --- lane 4A2: the tails ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["клиент — 89001234567 шины", "клиент – 89001234567 шины", "клиент - 89001234567 шины", "клиент: 89001234567 шины",
                                  "Клиентка 89001234567 шины", "клиентка — 8 900 123-45-67 шины"])
def test_dashes_colons_and_klientka_start_a_handoff(text):
    assert mgr.parse_handoff(text) == {"phone": "+79001234567", "need": "шины"}


def test_a_ten_digit_number_with_a_missing_digit_is_not_taken_silently(base):
    conn, _ = base
    assert mgr.parse_handoff("клиент 8900123456 шины") == {"phone": "", "typo": "8900123456", "need": "шины"}
    assert mgr.parse_handoff("клиент 8121234567 шины")["phone"] == "+78121234567"                 # a city number of St Petersburg is fine
    assert mgr.parse_handoff("клиент 4951234567 шины")["phone"] == "+74951234567"                 # Moscow without «8»
    for typo in ("7495123456", "8812123456"):                                                       # «7»/«88» and a city code with a digit missing
        assert mgr.parse_handoff("клиент %s шины" % typo)["typo"] == typo
    answer = mgr.dispatch(conn, "vk-1", PEER, "клиент 8900123456 шины", None, NOW)
    card_id = conn.execute("SELECT order_id FROM order_handoff").fetchone()[0]
    card = orders.get_card(conn, card_id)
    assert "проверьте номер" in answer.lower() and "клиент <телефон> <что нужно>" in answer and card["client_phone"] == ""
    assert any("Проверьте телефон" in e["text"] and "8900123456" in e["text"] for e in orders.list_events(conn, card_id))


def test_an_order_back_from_the_refusal_is_looked_at_again_and_issued_ends_it(base, monkeypatch):
    conn, _ = base
    mgr.dispatch(conn, "vk-1", PEER, "клиент 89001112233 шины", None, NOW)
    card_id = conn.execute("SELECT order_id FROM order_handoff").fetchone()[0]
    orders.move_card(conn, card_id, _stage(conn, "talk")["id"], NOW, "worker2")
    mgr.handoff_tick(conn, NOW)
    orders.move_card(conn, card_id, _stage(conn, "cancel")["id"], NOW, "worker2", {"cancel_reason": "ошибся"})
    mgr.handoff_tick(conn, NOW)
    orders.move_card(conn, card_id, _stage(conn, "talk")["id"], NOW, "worker2")                   # back from the refusal by a person
    looked = []
    real = orders.get_card
    monkeypatch.setattr(orders, "get_card", lambda c, i: looked.append(i) or real(c, i))
    mgr.handoff_tick(conn, NOW)
    assert looked == [card_id]                                                                   # a refused order brought back: looked at
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (_stage(conn, "done")["id"], card_id))   # issued
    conn.commit()
    mgr.handoff_tick(conn, NOW)
    looked.clear()
    mgr.handoff_tick(conn, NOW)
    assert looked == []                                                                          # issued: never again
    assert {r[0] for r in conn.execute("SELECT status FROM order_handoff_status WHERE order_id = ?", (card_id,))} == {"taken", "refused", "done"}


def test_the_rules_of_the_stage_run_when_the_order_is_made_and_nothing_goes_to_the_client(base):
    conn, _ = base
    from tires_sync import order_rules as rules
    stage = orders.ensure_from_point_stage(conn)
    from tires_sync import order_engine, order_templates
    rules.add_rule(conn, stage["id"], "enter", 0, "tag_add", None, "с точки", "auto", None)
    template = order_templates.add_template(conn, "Здравствуйте", "client", "Здравствуйте!")
    rules.add_rule(conn, stage["id"], "enter", 0, "message", template, "", "auto", None)              # even «сразу»: only prepared
    mgr.dispatch(conn, "vk-1", PEER, "клиент 89001112233 шины", None, NOW)
    card_id = conn.execute("SELECT order_id FROM order_handoff").fetchone()[0]
    assert "с точки" in orders.get_card(conn, card_id)["tags"]
    tasks = order_engine.list_tasks(conn, card_id)
    assert [t["kind"] for t in tasks if t["kind"] == "message"] == ["message"] and all(t["status"] == "pending" for t in tasks if t["kind"] == "message")
    assert conn.execute("SELECT COUNT(*) FROM chat_send_log").fetchone()[0] == 0


def test_the_csv_of_the_report_has_the_handed_over_clients(base):
    conn, _ = base
    mgr.dispatch(conn, "vk-1", PEER, "клиент 89001112233 шины", None, NOW)
    _sell(conn, conn.execute("SELECT order_id FROM order_handoff").fetchone()[0], "Бета", "2026-10-10")
    text = order_report.export_csv(order_report.build(conn, "2026-10-01", "2026-10-31"))
    assert "\nПереданные клиенты (входят в строки выше);1;;;;;0,00;;;\n" in text
