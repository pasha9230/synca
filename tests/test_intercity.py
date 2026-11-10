"""«Доставка в другой город» (order_intercity, the owner 2026-11-02): the checkbox, the city, the transport company, the track number and the
labels of an order; no reservation, «можно забирать», reminder, move to «На ОТХ», question of the seventh day nor «Клиент не пришёл» for it;
«Передал курьеру ТК» (the button, «отправил N», «передал курьеру N») moves it to «Отправлен» (a successful stage), tells the client, the owner
and the partner, and asks them for the track number when it is empty; «Отменить» puts it back. Fakes only: nothing leaves the tests."""
from __future__ import annotations

import io
import re

import pytest

from panel_helpers import post
from test_mypoint import manager, press, token, workplace
from test_point_undo import T1, T2, clock, told  # noqa: F401  (fixtures)
from test_vk_commands import SENT, _card, _stage, hook, ready, to_client, to_points
from tires_sync import (order_engine as engine, order_intercity as intercity, order_manager as mgr, order_notify, order_oth, order_reserve,
                        order_rules as rules, order_sheet, order_templates as templates, orders, point_log, store, users)

NOW = "2026-10-13T12:00:00"
PDF = b"%PDF-1.4\n% labels\n" + b"0" * 100


def city(conn, card, track="", **more):
    """The order is to another city: Псков, ПЭК (and the track number when given)."""
    templates.add_samples(conn)
    return intercity.save(conn, card, dict(dict(enabled="1", city="Псков", carrier="ПЭК", track=track), **more), NOW, "owner")


def code(conn, card):
    return orders.get_card(conn, card)["stage"]["code"]


@pytest.fixture
def tg(monkeypatch):
    sent = []
    monkeypatch.setattr(order_notify, "tell_people", lambda conn, box, logins, text, **k: sent.append((logins, text)) or "ok")
    return sent


def track_tasks(conn, card):
    return conn.execute("SELECT assignee, title, done_at, cancelled_at FROM callback_task WHERE order_id = ? ORDER BY id", (card,)).fetchall()


# ---- the stage, the fields --------------------------------------------------------------------------------------------------------------

def test_the_stage_shipped_stands_after_at_point_and_is_successful(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    stages = orders.list_stages(conn)
    codes = [s["code"] for s in stages]
    assert codes[codes.index("at_point") + 1] == "shipped"
    shipped = next(s for s in stages if s["code"] == "shipped")
    assert shipped["name"] == "Отправлен в ТК" and shipped["kind"] == "won"
    with pytest.raises(orders.OrderError):                                       # a system stage: renamed, never deleted
        orders.delete_stage(conn, shipped["id"])
    orders.ensure_pipeline(conn, NOW)                                            # once
    assert [s["code"] for s in orders.list_stages(conn)].count("shipped") == 1


def test_an_order_not_to_another_city_never_goes_to_shipped(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    with pytest.raises(orders.OrderError, match="доставка в другой город"):
        orders.move_card(conn, card, _stage(conn, "shipped"), NOW, "owner")
    assert code(conn, card) == "moving"


def test_the_fields_are_checked_and_journaled(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    with pytest.raises(orders.OrderError, match="ПЭК или КИТ"):
        intercity.save(conn, card, dict(enabled="1", carrier="СДЭК"), NOW, "owner")
    with pytest.raises(orders.OrderError, match="Трек-номер"):
        intercity.save(conn, card, dict(enabled="1", track="<script>"), NOW, "owner")
    assert not intercity.is_on(conn, card)
    assert city(conn, card) == ["Доставка в другой город", "Город", "ТК"]
    assert intercity.of(conn, card) == {"on": True, "city": "Псков", "carrier": "ПЭК", "track": ""}
    assert city(conn, card) == []                                                # nothing changed: nothing written
    texts = [e["text"] for e in orders.list_events(conn, card)]
    assert "Доставка в другой город: да" in texts and "ТК: ПЭК" in texts


def test_the_owner_saves_the_fields_from_the_card_and_sees_the_tag(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    r = post(client, "/orders/%d/intercity" % card, tok, enabled="1", city="Псков", carrier="КИТ", track="")
    assert r.status_code == 302 and intercity.of(conn, card)["carrier"] == "КИТ"
    page = client.get("/orders/%d" % card).get_data(as_text=True)
    assert "data-intercity-tag" in page and "Загрузить этикетки" in page
    board = client.get("/orders").get_data(as_text=True)
    assert board.count("data-intercity-tag") == 1


# ---- the labels -----------------------------------------------------------------------------------------------------------------------

def upload(client, tok, card, data=PDF, name="labels.pdf"):
    return client.post("/orders/%d/labels" % card, data={"csrf": tok, "file": (io.BytesIO(data), name)}, content_type="multipart/form-data")


def test_the_office_uploads_a_pdf_within_the_limits(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    assert upload(client, tok, card).status_code == 302 and intercity.labels(conn, card) == []      # not to another city yet
    city(conn, card)
    assert upload(client, tok, card).status_code == 302
    [label] = intercity.labels(conn, card)
    assert label["name"] == "labels.pdf" and label["size"] == len(PDF)
    upload(client, tok, card, data=b"MZ not a pdf", name="labels.pdf")
    upload(client, tok, card, name="labels.exe")
    upload(client, tok, card, data=PDF + b"0" * intercity.LABEL_MAX)
    assert len(intercity.labels(conn, card)) == 1
    for _ in range(intercity.LABELS_MAX):
        upload(client, tok, card)
    assert len(intercity.labels(conn, card)) == intercity.LABELS_MAX
    r = client.get("/orders/%d/labels/%d" % (card, label["id"]))
    assert r.status_code == 200 and r.data == PDF and r.mimetype == "application/pdf" and "attachment" in r.headers["Content-Disposition"]
    r = post(client, "/orders/%d/labels/%d/delete" % (card, label["id"]), tok)
    assert r.status_code == 302 and len(intercity.labels(conn, card)) == intercity.LABELS_MAX - 1
    assert client.get("/orders/%d/labels/%d" % (card, label["id"])).status_code == 404


def test_a_point_downloads_the_labels_of_its_order_only_and_writes_nothing(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    city(conn, card)
    label = intercity.add_label(conn, card, "a.pdf", PDF, NOW, "owner")
    mine = manager(app, conn, pid)
    assert mine.get("/orders/%d/labels/%d" % (card, label)).data == PDF
    assert upload(mine, "tok", card).status_code == 403
    assert post(mine, "/orders/%d/labels/%d/delete" % (card, label), "tok").status_code == 403
    assert post(mine, "/orders/%d/intercity" % card, "tok", enabled="").status_code == 403
    assert len(intercity.labels(conn, card)) == 1 and intercity.is_on(conn, card)
    from tires_sync import order_points as points
    other = points.save_point(conn, dict(name="Дельта", channel="panel"))
    theirs = manager(app, conn, other, login_="delta")
    assert theirs.get("/orders/%d/labels/%d" % (card, label)).status_code == 403
    users.add_user(conn, "watch", "password-123", NOW, role="viewer")
    conn.commit()
    viewer = app.test_client()
    with viewer.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = "watch", store.get_session_epoch(conn, "watch"), "tok"
    assert upload(viewer, "tok", card).status_code == 403


# ---- at the point: no pickup ------------------------------------------------------------------------------------------------------------

def test_the_arrival_sets_no_reservation_and_tells_the_client_nothing(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    city(conn, card)
    hook(app, conn, "Приехал 5000100")
    got = orders.get_card(conn, card)
    assert got["stage"]["code"] == "at_point" and got["reserve_until"] == ""
    assert to_client(app) == []
    assert "клиенту «можно забирать» не отправляется" in to_points(app)[-1][1]
    from tires_sync import order_arrived
    assert order_arrived.untold(conn, "2026-10-14T12:00:00") == []
    assert any("Заказ в другой город" in e["text"] for e in orders.list_events(conn, card) if e["kind"] == "point")


def test_no_reminder_no_question_and_no_oth_for_an_order_to_another_city(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    assert orders.get_card(conn, card)["reserve_until"] == "2026-10-16"         # an ordinary order has its three days
    city(conn, card)                                                             # the checkbox came after the arrival
    alerts = []
    for moment in ("2026-10-16T09:30:00", "2026-10-16T12:45:00", "2026-10-17T02:00:00", "2026-10-17T10:00:00"):
        order_reserve.check(conn, moment, lambda c, text: alerts.append(text) or "ok", deliver=lambda *a: alerts.append(a), point_send=lambda *a, **k: alerts.append(a))
    assert alerts == [] and code(conn, card) == "at_point"
    assert not order_reserve.question_open(conn, orders.get_card(conn, card), "2026-10-16T10:00:00")


def test_the_seventh_day_on_oth_asks_nothing_for_an_order_to_another_city(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    order_oth.ask_seventh_day(conn, "2026-10-01T00:00:00")                       # the rule's first tick
    plain = _card(conn, pid, webmim="700300", code="at_point")
    city(conn, card)
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    for c in (card, plain):
        orders.move_card(conn, c, _stage(conn, "oth"), NOW, "o", changes={"cancel_reason": "Клиент не пришёл"})
    assert order_oth.ask_seventh_day(conn, "2026-10-19T13:00:00") == 1
    assert not order_oth.question_open(conn, orders.get_card(conn, card)) and order_oth.question_open(conn, orders.get_card(conn, plain))


# ---- «Передал курьеру ТК» from VK --------------------------------------------------------------------------------------------------------

def test_vk_handed_to_the_courier_ships_tells_the_client_the_office_and_asks_for_the_track(tmp_path, tg):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    users.add_user(conn, "zaman", "password-123", NOW, role="partner")
    conn.commit()
    city(conn, card)
    hook(app, conn, "передал курьеру 5000100")
    assert code(conn, card) == "done" and intercity.was_shipped(conn, orders.get_card(conn, card))     # through «Отправлен» to «Выполнено»
    assert to_client(app) == ["Заказ 5000100 отправлен в город Псков транспортной компанией ПЭК.\n\nСпасибо за покупку!"]
    assert "передан курьеру ТК" in to_points(app)[-1][1]
    assert tg and tg[0][0] == ["owner"] and "передала заказ №5000100 курьеру ТК ПЭК в Псков" in tg[0][1]
    assert [(a, t) for a, t, _d, _c in track_tasks(conn, card)] == [("owner", "Внести трек-номер заказа №5000100"),
                                                                    ("zaman", "Внести трек-номер заказа №5000100")]
    [line] = [e for e in orders.list_events(conn, card) if "курьеру ТК" in e["text"] and e["kind"] == "point"]
    assert line["level"] == "yellow" and "трек-номер не внесён" in line["text"]
    hook(app, conn, "отправил 5000100", event="e2")                              # again: nothing more
    assert len(to_client(app)) == 1 and "уже отмечен" in to_points(app)[-1][1]
    intercity.save(conn, card, dict(enabled="1", city="Псков", carrier="ПЭК", track="ПЭК-123"), "2026-10-13T13:00:00", "owner")
    assert all(d for _a, _t, d, _c in track_tasks(conn, card))                   # the number came: the tasks are done


def test_vk_sent_with_the_track_number_says_it_and_asks_nothing(tmp_path, tg):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    city(conn, card, track="12-3456")
    hook(app, conn, "Отправил заказ 5000100")
    assert code(conn, card) == "done"
    assert to_client(app) == ["Заказ 5000100 отправлен в город Псков транспортной компанией ПЭК.\nТрек-номер: 12-3456\n\nСпасибо за покупку!"]
    assert track_tasks(conn, card) == []


@pytest.mark.parametrize("text", ["отправил на возврат 5000100", "передал курьеру деньги 5000100", "отправил 5000100?", "отдал курьеру 5000100"])
def test_vk_words_that_are_not_strictly_the_command_are_a_question_for_the_owner(tmp_path, text):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    city(conn, card)
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    hook(app, conn, text)
    assert code(conn, card) == "at_point" and mgr.new_count(conn) == 1


def test_vk_issued_or_no_show_of_an_order_to_another_city_goes_to_the_owner(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    city(conn, card)
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    hook(app, conn, "выдал 5000100")
    hook(app, conn, "клиент не пришёл 5000100", event="e2")
    assert code(conn, card) == "at_point" and mgr.new_count(conn) == 2
    assert "передал курьеру 5000100" in to_points(app)[0][1]
    assert not any(e["level"] == "red" and "не пришёл" in e["text"] for e in orders.list_events(conn, card))


def test_vk_handed_to_the_courier_of_an_ordinary_order_is_a_question(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    hook(app, conn, "передал курьеру 5000100")
    assert code(conn, card) == "at_point" and mgr.new_count(conn) == 1 and "не в другой город" in to_points(app)[-1][1]


def test_the_owner_confirms_a_disputed_courier_word(tmp_path, tg):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    city(conn, card)
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    hook(app, conn, "передал курьеру")                                           # no number
    item_id = conn.execute("SELECT id FROM manager_message ORDER BY id DESC LIMIT 1").fetchone()[0]
    reply = mgr.confirm(conn, item_id, "5000100", "2026-10-13T13:00:00", "owner", deliver=lambda *a: None)
    assert code(conn, card) == "done" and "подтвердил владелец" in reply


# ---- «Рабочее место» -----------------------------------------------------------------------------------------------------------------

def test_the_workplace_card_shows_the_tag_the_labels_and_the_courier_button(tmp_path, clock, told):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    city(conn, card)
    label = intercity.add_label(conn, card, "a.pdf", PDF, NOW, "owner")
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    mine = manager(app, conn, pid)
    page = workplace(mine)
    block = page[page.index('id="order-%d"' % card):]
    block = block[:block.index("</article>")]
    assert "В другой город" in block and intercity.HINT in block and "/orders/%d/labels/%d" % (card, label) in block
    assert "Скачать этикетки" in block and "Передал курьеру ТК" in block
    assert "/my-point/%d/issued" % card not in block and "/my-point/%d/refuse" % card not in block
    r = post(mine, "/my-point/%d/issued" % card, "tok", token="a" * 24)            # a crafted «Выдал»: refused
    assert r.status_code == 302 and code(conn, card) == "at_point"
    r = post(mine, "/my-point/%d/refuse" % card, "tok", token="b" * 24, reason="year")   # a crafted «ОТКАЗ» (2026-11-05): refused
    assert code(conn, card) == "at_point" and not any(e["level"] == "red" and e["kind"] == "point" for e in orders.list_events(conn, card))
    press(mine, card, "shipped")
    assert code(conn, card) == "done" and len(to_client(app)) == 1
    assert told and "курьеру ТК" in told[0][1]
    assert [r["kind"] for r in point_log.history(conn, pid)][0] == "shipped"


def test_the_courier_mark_is_undone_and_the_track_tasks_are_cancelled(tmp_path, clock, told):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    city(conn, card)
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    before = orders.get_card(conn, card)
    mine = manager(app, conn, pid)
    press(mine, card, "shipped")
    assert code(conn, card) == "done" and track_tasks(conn, card)
    page = workplace(mine)              # the owner 2026-11-10: no list «Отменить отметку» on the page; the order is gone from it,
    assert 'action="/my-point/%d/undo"' % card not in page          # the office corrects it; the undo itself still works (below)
    clock["now"] = T2
    r = post(mine, "/my-point/%d/undo" % card, "tok")
    assert r.status_code == 302
    after = orders.get_card(conn, card)
    assert (after["stage"]["code"], after["entered_at"], after["reserve_until"]) == ("at_point", before["entered_at"], "")
    assert all(c for _a, _t, _d, c in track_tasks(conn, card))                   # cancelled
    assert len(to_client(app)) == 1                                              # what went stays, nothing new


# ---- the client's messages ---------------------------------------------------------------------------------------------------------------

def test_the_placed_message_of_an_order_to_another_city_is_its_own(tmp_path):
    from test_order_placed import Deliver, db, ready as placed_ready
    from test_webmim_create import FakeWebmim, place
    from tires_sync import order_placed
    conn = db(tmp_path)
    mine = placed_ready(conn)
    city(conn, mine)
    place(conn, mine, FakeWebmim())
    deliver = Deliver()
    [line] = [order_placed.tell_client(conn, mine, "1001", NOW, "owner", deliver)]
    assert line.startswith("Клиенту отправлено «Клиенту: заказ в другой город оформлен»")
    [(_chat, text, _tok)] = deliver.sent
    assert text.startswith("Заказ 5001 оформлен с доставкой в город Псков транспортной компанией ПЭК.") and "сообщим здесь в чате" in text
    order_placed.tell_client(conn, mine, "1001", "2026-10-23T12:10:00", "owner", deliver)
    assert len(deliver.sent) == 1                                                # one of the two, once


def test_a_rule_of_the_placed_stage_with_the_usual_text_is_skipped(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    templates.add_samples(conn)
    usual = next(t["id"] for t in templates.list_templates(conn) if t["name"] == "Клиенту: заказ оформлен")
    other = _card(conn, pid, webmim="700300", code="moving")
    for c in (card, other):
        orders.move_card(conn, c, _stage(conn, "talk"), NOW, "o")
    rules.add_rule(conn, _stage(conn, "placed"), "enter", 0, "message", usual, "", "ask", None)
    city(conn, card)
    for c in (card, other):
        orders.move_card(conn, c, _stage(conn, "placed"), "2026-10-13T13:00:00", "o")
    status = lambda c: [t["status"] for t in engine.list_tasks(conn, c) if t["title"] == "Клиенту: заказ оформлен"]
    assert status(card) == ["skipped"] and status(other) == ["pending"]


def test_a_lacking_city_leaves_the_shipped_message_waiting(tmp_path, tg):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    city(conn, card, city="")
    hook(app, conn, "передал курьеру 5000100")
    assert code(conn, card) == "done" and to_client(app) == []
    [task] = [t for t in engine.list_tasks(conn, card) if t["rule_id"] == intercity.SHIPPED_RULE]
    assert task["status"] == "error" and "Город доставки" in task["result"]
    assert "не отправлено" in to_points(app)[-1][1]


def test_the_samples_are_at_the_end_and_render_without_a_track(tmp_path):
    names = [s[0] for s in templates.SAMPLES]
    assert names[-2:] == [intercity.PLACED_TEMPLATE, intercity.SHIPPED_TEMPLATE]
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    city(conn, card)
    text, missing = templates.render(conn, orders.get_card(conn, card), templates.INTERCITY_SHIPPED_SAMPLE[2])
    assert missing == [] and "Трек-номер" not in text


# ---- done for the table, the sales ----------------------------------------------------------------------------------------------------

def test_shipped_is_realized_in_the_sheet_and_may_record_a_sale(tmp_path, tg):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    city(conn, card)
    hook(app, conn, "передал курьеру 5000100")
    shipped = orders.get_card(conn, card)
    assert order_sheet.realization(shipped) == order_sheet.REALIZED
    assert orders.record_sales(conn, card, "2026-10-13", NOW, "owner", "2026-10-13")
    with pytest.raises(orders.OrderError, match="галочку не снять"):
        intercity.save(conn, card, dict(enabled=""), NOW, "owner")


# ---- after the review of 2026-11-02 ------------------------------------------------------------------------------------------------------

def test_the_messages_go_on_a_base_where_nobody_pressed_add_samples(tmp_path, tg):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    intercity.save(conn, card, dict(enabled="1", city="Псков", carrier="КИТ"), NOW, "owner")     # no templates.add_samples
    hook(app, conn, "передал курьеру 5000100")
    assert code(conn, card) == "done"
    assert to_client(app) == ["Заказ 5000100 отправлен в город Псков транспортной компанией КИТ.\n\nСпасибо за покупку!"]
    assert intercity.SHIPPED_TEMPLATE in [t["name"] for t in templates.list_templates(conn)]       # editable from now on


def test_the_owner_cannot_confirm_issued_or_no_show_of_an_order_to_another_city(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    city(conn, card)
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    hook(app, conn, "выдал 5000100")
    hook(app, conn, "клиент не пришёл 5000100", event="e2")
    for (item_id,) in conn.execute("SELECT id FROM manager_message ORDER BY id").fetchall():
        with pytest.raises(orders.OrderError, match="в другой город"):
            mgr.confirm(conn, item_id, "5000100", "2026-10-13T13:00:00", "owner", deliver=lambda *a: None)
    assert code(conn, card) == "at_point"
    assert not any(e["level"] == "red" and "не пришёл" in e["text"] for e in orders.list_events(conn, card))


def test_vk_here_of_an_order_to_another_city_goes_to_the_owner(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    city(conn, card)
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    hook(app, conn, "на точке 5000100")
    assert mgr.new_count(conn) == 1 and to_client(app) == []


def test_the_undo_skips_a_shipped_message_still_waiting(tmp_path, clock, told):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    city(conn, card, city="")                                                    # lacking: the message waits
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    mine = manager(app, conn, pid)
    press(mine, card, "shipped")
    [task] = [t for t in engine.list_tasks(conn, card) if t["rule_id"] == intercity.SHIPPED_RULE]
    assert task["status"] == "error"
    clock["now"] = T2
    post(mine, "/my-point/%d/undo" % card, "tok")
    assert code(conn, card) == "at_point"
    assert [t["status"] for t in engine.list_tasks(conn, card) if t["rule_id"] == intercity.SHIPPED_RULE] == ["skipped"]


def test_after_the_undo_a_second_courier_mark_still_tells_the_client_once(tmp_path, clock, told):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    city(conn, card, city="")                                                    # lacking: the first message waits
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    mine = manager(app, conn, pid)
    press(mine, card, "shipped")
    clock["now"] = T2
    post(mine, "/my-point/%d/undo" % card, "tok")
    intercity.save(conn, card, dict(enabled="1", city="Псков", carrier="ПЭК"), T2, "owner")
    clock["now"] = "2026-10-16T14:00:00"
    press(mine, card, "shipped")
    assert code(conn, card) == "done"
    assert to_client(app) == ["Заказ 5000100 отправлен в город Псков транспортной компанией ПЭК.\n\nСпасибо за покупку!"]


# ---- the owner's answers of 2026-11-02 (evening) ---------------------------------------------------------------------------------------

def test_the_office_moving_it_to_shipped_does_what_the_point_does(tmp_path, tg):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    users.add_user(conn, "zaman", "password-123", NOW, role="partner")
    conn.commit()
    city(conn, card)
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    r = post(client, "/orders/%d/move" % card, tok, stage_id=str(_stage(conn, "shipped")))
    assert r.status_code == 302
    assert code(conn, card) == "done" and intercity.was_shipped(conn, orders.get_card(conn, card))
    assert to_client(app) == ["Заказ 5000100 отправлен в город Псков транспортной компанией ПЭК.\n\nСпасибо за покупку!"]
    assert tg and tg[0][0] == ["owner"] and "Офис" in tg[0][1] # the order's responsible only (the owner 2026-11-03)
    assert len(track_tasks(conn, card)) == 2


def test_the_rules_of_done_run_after_shipped(tmp_path, tg):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    rules.add_rule(conn, _stage(conn, "done"), "enter", 0, "record_sale", None, "", "auto", None)
    city(conn, card, track="1")
    hook(app, conn, "передал курьеру 5000100")
    assert code(conn, card) == "done" and orders.get_card(conn, card)["sale_ids"]        # the owner's rule of «Выполнено» recorded the sale


def test_the_office_moving_it_from_the_chat_or_the_window_does_it_too(tmp_path, tg):
    for how in ("chat", "window"):
        (tmp_path / how).mkdir()
        app, client, tok, conn, pid, card = ready(tmp_path / how, SENT, SENT)
        city(conn, card)
        orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
        if how == "chat":
            row = conn.execute("SELECT id FROM avito_chat WHERE order_id = ?", (card,)).fetchone()[0]
            post(client, "/chats/%d/stage" % row, tok, stage_id=str(_stage(conn, "shipped")))
        else:
            post(client, "/orders/%d/save" % card, tok, stage_id=str(_stage(conn, "shipped")))
        assert code(conn, card) == "done" and len(to_client(app)) == 1, how


def test_moving_a_shipped_order_back_into_shipped_repeats_nothing(tmp_path, tg):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    city(conn, card)
    hook(app, conn, "передал курьеру 5000100")
    tasks_before, told_before = len(track_tasks(conn, card)), len(tg)
    post(client, "/orders/%d/move" % card, tok, stage_id=str(_stage(conn, "shipped")))
    assert code(conn, card) == "done" and len(to_client(app)) == 1
    assert len(track_tasks(conn, card)) == tasks_before and len(tg) == told_before


def test_a_renamed_stage_still_knows_the_order_was_shipped(tmp_path, tg):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    city(conn, card)
    hook(app, conn, "передал курьеру 5000100")
    conn.execute("UPDATE order_stage SET name = 'Уехал в ТК' WHERE code = 'shipped'")
    conn.commit()
    assert intercity.was_shipped(conn, orders.get_card(conn, card))
    hook(app, conn, "отправил 5000100", event="e9")
    assert "уже отмечен" in to_points(app)[-1][1]


# ---- the owner's answers of 2026-11-02 (night): Q3, Q4, Q5; «Шаблоны сообщений» ------------------------------------------------------

def test_the_two_texts_are_on_the_page_of_the_texts_that_go_by_themselves(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    page = client.get("/orders/templates").get_data(as_text=True)
    auto = page[page.index("Сообщения, которые уходят сами"):]
    shown = [name.split(": ", 1)[1] for name in (intercity.PLACED_TEMPLATE, intercity.SHIPPED_TEMPLATE)]   # without «Клиенту:» (2026-11-08)
    assert all("<strong>" + name[:1].upper() + name[1:] + "</strong>" in auto for name in shown)
    assert "<strong>Клиенту:" not in auto and "<strong>Менеджеру:" not in auto


def test_the_checkbox_skips_the_waiting_pickup_messages(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    templates.add_samples(conn)
    arrived = next(t["id"] for t in templates.list_templates(conn) if t["name"] == templates.ARRIVED_SAMPLE[0])
    other = next(t["id"] for t in templates.list_templates(conn) if t["name"] == "Менеджеру: заказ оформлен")
    for template_id in (arrived, other):
        rules.add_rule(conn, _stage(conn, "at_point"), "enter", 0, "message", template_id, "", "ask", None)
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    status = lambda name: [t["status"] for t in engine.list_tasks(conn, card) if t["title"] == name]
    assert status(templates.ARRIVED_SAMPLE[0]) == ["pending"]
    city(conn, card)
    assert status(templates.ARRIVED_SAMPLE[0]) == ["skipped"] and status("Менеджеру: заказ оформлен") == ["pending"]


def test_a_recreated_template_leaves_a_grey_record(tmp_path, tg):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    intercity.save(conn, card, dict(enabled="1", city="Псков", carrier="КИТ"), NOW, "owner")       # no templates.add_samples
    hook(app, conn, "передал курьеру 5000100")
    [line] = [e for e in orders.list_events(conn, card) if "создан из образца" in e["text"]]
    assert line["level"] == "grey" and intercity.SHIPPED_TEMPLATE in line["text"]


def test_the_client_rules_of_done_are_not_prepared_for_an_order_to_another_city(tmp_path, tg):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    templates.add_samples(conn)
    thanks = templates.add_template(conn, "Клиенту: спасибо", "client", "Спасибо за покупку, {client_name}!")
    point_note = next(t["id"] for t in templates.list_templates(conn) if t["name"] == "Менеджеру: заказ оформлен")
    for template_id in (thanks, point_note):
        rules.add_rule(conn, _stage(conn, "done"), "enter", 0, "message", template_id, "", "ask", None)
    other = _card(conn, pid, webmim="700300", code="at_point")
    city(conn, card)
    hook(app, conn, "передал курьеру 5000100")
    orders.move_card(conn, other, _stage(conn, "done"), NOW, "o")
    status = lambda c, name: [t["status"] for t in engine.list_tasks(conn, c) if t["title"] == name]
    assert status(card, "Клиенту: спасибо") == ["skipped"] and status(card, "Менеджеру: заказ оформлен") == ["pending"]
    assert status(other, "Клиенту: спасибо") == ["pending"]


# ---- the review of f52d0b6 -----------------------------------------------------------------------------------------------------------

def test_the_checkbox_closes_the_tails_of_a_failed_pickup_message(tmp_path):
    from tires_sync import chats, unsent
    app, client, tok, conn, pid, card = ready(tmp_path, (402, {"error": {"message": "Перейдите на подписку с API мессенджера"}}))
    conn.execute("UPDATE avito_chat SET order_id = NULL WHERE order_id = ?", (card,))           # no chat: «можно забирать» cannot go
    conn.commit()
    hook(app, conn, "Приехал 5000100")
    arrived = [t for t in engine.list_tasks(conn, card) if t["rule_id"] == engine.ARRIVED_RULE]
    assert [t["status"] for t in arrived] == ["pending"]
    token = engine.task_token(arrived[0])
    unsent.record(conn, card, token, NOW, "нет чата Авито", "Заказ приехал", source="arrived")
    conn.execute("INSERT INTO manager_message (message_id, sender, text, at, status) VALUES (?, ?, 'клиенту не отправлено «можно забирать»', ?, 'new')",
                 (token, chats.SYSTEM, NOW))
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, next_at) VALUES ('reserve-%d-1', 1, ?, 'o', 'retry', ?)" % card, (NOW, NOW))
    conn.commit()
    intercity.save(conn, card, dict(enabled="1", city="Псков", carrier="ПЭК"), NOW, "owner")
    assert [t["status"] for t in engine.list_tasks(conn, card) if t["rule_id"] == engine.ARRIVED_RULE] == ["skipped"]
    assert unsent.of_order(conn, card) == []
    assert conn.execute("SELECT status, answer FROM manager_message WHERE message_id = ?", (token,)).fetchone() == ("answered", intercity.NOT_NEEDED)
    assert conn.execute("SELECT status, next_at FROM chat_send_log WHERE token = 'reserve-%d-1'" % card).fetchone() == ("failed", "")


def test_the_checkbox_skips_a_waiting_reserve_reminder_and_one_in_error(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    templates.add_samples(conn)
    reserve = next(t["id"] for t in templates.list_templates(conn) if t["name"] == templates.RESERVE_SAMPLE[0])
    arrived = next(t["id"] for t in templates.list_templates(conn) if t["name"] == templates.ARRIVED_SAMPLE[0])
    rules.add_rule(conn, _stage(conn, "at_point"), "enter", 0, "message", reserve, "", "ask", None)
    rules.add_rule(conn, _stage(conn, "at_point"), "enter", 0, "message", arrived, "", "ask", None)
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    conn.execute("UPDATE order_task SET status = 'error', result = 'Не хватает: Точка.' WHERE order_id = ? AND title = ?", (card, templates.ARRIVED_SAMPLE[0]))
    conn.commit()
    city(conn, card)
    status = {t["title"]: t["status"] for t in engine.list_tasks(conn, card) if t["kind"] == "message"}
    assert status[templates.RESERVE_SAMPLE[0]] == "skipped" and status[templates.ARRIVED_SAMPLE[0]] == "skipped"


def test_the_owners_text_of_shipped_goes_and_reset_brings_the_sample_back(tmp_path, tg):
    from tires_sync import message_texts as mt
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    city(conn, card)
    post(client, "/orders/templates/text", tok, key="intercity_shipped", body="Ваш заказ {webmim_number} уехал в {city} ({carrier}).{track_line}")
    assert mt.body(conn, "intercity_shipped") == "Ваш заказ {webmim_number} уехал в {city} ({carrier}).{track_line}"
    hook(app, conn, "передал курьеру 5000100")
    assert to_client(app) == ["Ваш заказ 5000100 уехал в Псков (ПЭК)."]
    post(client, "/orders/templates/text/reset", tok, key="intercity_shipped")
    assert mt.body(conn, "intercity_shipped") == templates.INTERCITY_SHIPPED_SAMPLE[2]
    post(client, "/orders/templates/text", tok, key="intercity_placed", body="Оформили в {city}, {carrier}.")
    assert mt.body(conn, "intercity_placed") == "Оформили в {city}, {carrier}."


# ---- «Работать как точка» (the base of the design, 2026-11-02) --------------------------------------------------------------------------

def test_the_owner_working_as_the_point_ships_downloads_and_undoes_but_never_writes_labels(tmp_path, clock, monkeypatch):
    sent = []
    monkeypatch.setattr(order_notify, "tell_people", lambda conn, box, logins, text, skip_logins=(), **k: sent.append((list(logins), set(skip_logins), text)) or "ok")
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    users.add_user(conn, "zaman", "password-123", NOW, role="partner")
    conn.commit()
    city(conn, card)
    label = intercity.add_label(conn, card, "a.pdf", PDF, NOW, "owner")
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    assert post(client, "/as-point", tok, point_id=str(pid)).status_code == 302                  # the owner works as the point
    assert client.get("/orders/%d/labels/%d" % (card, label)).data == PDF
    assert upload(client, tok, card).status_code == 403
    assert post(client, "/orders/%d/labels/%d/delete" % (card, label), tok).status_code == 403
    assert post(client, "/orders/%d/intercity" % card, tok, enabled="").status_code == 403
    page = workplace(client)
    assert "Передал курьеру ТК" in page and "Скачать этикетки" in page
    press(client, card, "shipped", tok=tok)
    assert code(conn, card) == "done" and len(to_client(app)) == 1
    [line] = [e for e in orders.list_events(conn, card) if e["kind"] == "point" and "курьеру ТК" in e["text"]]
    assert " за точку «" in line["author"]
    assert sent and sent[0][0] == ["owner"] and "owner" in sent[0][1]                    # not to the one who pressed
    clock["now"] = T2
    assert post(client, "/my-point/%d/undo" % card, tok).status_code == 302
    assert code(conn, card) == "at_point"


def test_a_board_move_to_shipped_working_as_the_point_tells_the_others_only(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(order_notify, "tell_people", lambda conn, box, logins, text, skip_logins=(), **k: sent.append((list(logins), set(skip_logins))) or "ok")
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    city(conn, card)
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    assert post(client, "/as-point", tok, point_id=str(pid)).status_code == 302
    assert post(client, "/orders/%d/move" % card, tok, stage_id=str(_stage(conn, "shipped"))).status_code == 302
    assert code(conn, card) == "done" and sent and "owner" in sent[0][1]


def test_the_checkbox_closes_the_unsent_row_of_a_rule_task_too(tmp_path):
    from tires_sync import unsent
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    templates.add_samples(conn)
    arrived = next(t["id"] for t in templates.list_templates(conn) if t["name"] == templates.ARRIVED_SAMPLE[0])
    rules.add_rule(conn, _stage(conn, "at_point"), "enter", 0, "message", arrived, "", "ask", None)
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    [task] = [t for t in engine.list_tasks(conn, card) if t["title"] == templates.ARRIVED_SAMPLE[0] and t["rule_id"] != engine.ARRIVED_RULE]
    conn.execute("UPDATE order_task SET status = 'error', result = 'Авито отказал' WHERE id = ?", (task["id"],))
    unsent.record(conn, card, engine.task_token(task), NOW, "Авито отказал", "Заказ приехал")
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, next_at) VALUES ('arrived-%d-7', 1, ?, 'o', 'retry', ?)" % card, (NOW, NOW))
    conn.commit()
    city(conn, card)
    assert [u for u in unsent.of_order(conn, card) if u["kind"] == "send"] == []
    assert any("не будет отправлено" in e["text"] for e in orders.list_events(conn, card))           # the stopped 402 is in the journal
