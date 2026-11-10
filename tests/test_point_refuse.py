"""«ОТКАЗ» of the point (the owner 2026-11-05): instead of «Клиент не пришёл» the point marks the client's refusal with a reason («Старый год
производства», «Ошиблись размером», «Другой» typed); the order goes to «На ОТХ» by the usual move, with a red record and the reason. The VK
command is «отказ <номер> <причина>»; without a reason it asks and changes nothing. The client hears nothing."""
from __future__ import annotations

import pytest

from panel_helpers import post
from test_mypoint import journal, manager, token, workplace
from test_point_new_view import flag
from test_vk_commands import PEER, _card, _stage, hook, ready, to_client, to_points
from tires_sync import order_manager as mgr, orders, point_log

REASONS = [("year", "", "Старый год производства"), ("size", "", "Ошиблись размером"), ("other", "  клиент   передумал ", "клиент передумал")]


def at_point(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    orders.move_card(conn, card, _stage(conn, "at_point"), "2026-10-13T12:00:00", "o")
    conn.commit()
    return app, client, conn, pid, card


def refuse(client, card, reason, other="", tok=None):
    page = workplace(client)
    data = dict(token=tok or token(page, card, "refuse"), reason=reason, other=other)
    r = post(client, "/my-point/%d/refuse" % card, "tok", **data)
    assert r.status_code == 302, r.status_code
    return data["token"], client.get(r.headers["Location"]).get_data(as_text=True)


def linked(conn, card):
    return conn.execute("SELECT 1 FROM client_order WHERE order_id = ?", (card,)).fetchone() is not None


@pytest.mark.parametrize("new_view", [False, True])
def test_the_card_at_the_point_has_refuse_with_three_reasons_and_no_client_did_not_come(tmp_path, new_view):
    app, _client, conn, pid, card = at_point(tmp_path)
    if new_view:
        flag(conn, "all")
    page = workplace(manager(app, conn, pid))
    assert '/my-point/%d/refuse' % card in page and "ОТКАЗ" in page
    for value, label in (("year", "Старый год производства"), ("size", "Ошиблись размером"), ("other", "Другой")):
        assert 'value="%s"' % value in page and label in page
    assert 'name="other"' in page
    assert "/no_show" not in page and "Клиент не пришёл" not in page and "/to_oth" not in page


@pytest.mark.parametrize("value,other,reason", REASONS)
def test_the_button_sends_the_order_to_oth_with_the_reason_and_the_client_hears_nothing(tmp_path, value, other, reason):
    app, _client, conn, pid, card = at_point(tmp_path)
    assert linked(conn, card)
    mine = manager(app, conn, pid)
    _t, page = refuse(mine, card, value, other)
    got = orders.get_card(conn, card)
    assert got["stage"]["code"] == "oth" and got["cancel_reason"] == mgr.REFUSE_REASON % reason
    assert "отказ" in page.lower() and reason in page
    red = [t for k, t, lvl in journal(conn, card) if lvl == "red" and k == "point"]
    assert len(red) == 1 and reason in red[0] and "Отказ клиента" in red[0]
    assert not linked(conn, card)                                             # unlinked on the way to «На ОТХ» (order_oth.detach)
    assert to_client(app) == []
    [row] = [r for r in point_log.history(conn, pid) if r["kind"] == "refuse"]
    assert row["order_id"] == card


@pytest.mark.parametrize("value,other", [("", ""), ("other", ""), ("other", "   "), ("bogus", "x")])
def test_without_a_reason_nothing_changes_and_the_press_can_be_sent_again(tmp_path, value, other):
    app, _client, conn, pid, card = at_point(tmp_path)
    mine = manager(app, conn, pid)
    tok, page = refuse(mine, card, value, other)
    assert "Отказ не отмечен: укажите причину отказа" in page
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point" and linked(conn, card)
    assert not [e for e in orders.list_events(conn, card) if e["level"] == "red"]
    _t, page = refuse(mine, card, "year", tok=tok)                            # the same form, now with a reason: it goes
    assert orders.get_card(conn, card)["stage"]["code"] == "oth"


def test_a_refuse_that_cannot_move_the_order_changes_nothing_and_says_why(tmp_path):
    app, _client, conn, pid, card = at_point(tmp_path)
    conn.execute("UPDATE order_card SET client_phone = '' WHERE id = ?", (card,))   # «На ОТХ» needs the phone: the move is refused
    conn.commit()
    _t, page = refuse(manager(app, conn, pid), card, "size")
    got = orders.get_card(conn, card)
    assert got["stage"]["code"] == "at_point" and got["cancel_reason"] == ""
    assert "Не получилось" in page and "телефон клиента" in page
    red = [t for _k, t, lvl in journal(conn, card) if lvl == "red"]
    assert any("Ошиблись размером" in t and "не выполнен" in t for t in red), red
    assert to_client(app) == []
    assert not [r for r in point_log.history(conn, pid) if r["kind"] == "refuse"]


def test_refuse_does_not_fit_an_order_still_on_its_way(tmp_path):
    app, _client, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    r = post(mine, "/my-point/%d/refuse" % card, "tok", token="a" * 24, reason="year", other="")
    page = mine.get(r.headers["Location"]).get_data(as_text=True)
    assert "отмечают, когда заказ на точке" in page and orders.get_card(conn, card)["stage"]["code"] == "moving"


def test_the_old_buttons_are_gone_from_the_panel(tmp_path):
    app, _client, conn, pid, card = at_point(tmp_path)
    mine = manager(app, conn, pid)
    for action in ("no_show", "to_oth"):
        assert post(mine, "/my-point/%d/%s" % (card, action), "tok", token="b" * 24).status_code == 404
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point"


def test_refuse_can_be_undone_and_the_client_is_linked_again(tmp_path):
    app, _client, conn, pid, card = at_point(tmp_path)
    mine = manager(app, conn, pid)
    refuse(mine, card, "year")
    oth = mine.get("/my-point?tab=oth").get_data(as_text=True)                          # «Отменить «ОТКАЗ»» in the card on «На ОТХ» (no list
    assert 'data-undo-card="%d"' % card in oth and "Отменить «ОТКАЗ»" in oth            # over the orders: the owner 2026-11-10)
    r = post(mine, "/my-point/%d/undo" % card, "tok")
    assert r.status_code == 302
    got = orders.get_card(conn, card)
    assert got["stage"]["code"] == "at_point" and got["cancel_reason"] == "" and linked(conn, card)


# --- VK -------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,reason", [("отказ 5000100 старый год производства", "Старый год производства"),
                                         ("Отказ 5000100 Ошиблись размером", "Ошиблись размером"),
                                         ("отказ №5000100: клиент нашёл дешевле", "клиент нашёл дешевле")])
def test_the_vk_command_sends_the_order_to_oth_with_the_reason(tmp_path, text, reason):
    app, _client, conn, _pid, card = at_point(tmp_path)
    hook(app, conn, text)
    got = orders.get_card(conn, card)
    assert got["stage"]["code"] == "oth" and got["cancel_reason"] == mgr.REFUSE_REASON % reason
    [red] = [t for k, t, lvl in journal(conn, card) if lvl == "red" and k == "point"]
    assert reason in red
    [(peer, answer)] = to_points(app)
    assert peer == PEER and "отказ" in answer.lower() and reason in answer and "На ОТХ" in answer
    assert to_client(app) == [] and not linked(conn, card)


def test_the_vk_command_in_reply_to_the_bots_line_needs_no_number(tmp_path):
    app, _client, conn, _pid, card = at_point(tmp_path)
    answer = mgr.dispatch(conn, "r1", PEER, "отказ ошиблись размером", 5000100, "2026-10-13T13:00:00", None, None)
    got = orders.get_card(conn, card)
    assert got["stage"]["code"] == "oth" and got["cancel_reason"] == mgr.REFUSE_REASON % "Ошиблись размером", answer


@pytest.mark.parametrize("text", ["отказ 5000100", "отказ 5000100 ", "Отказ №5000100."])
def test_the_vk_command_without_a_reason_asks_for_it_and_changes_nothing(tmp_path, text):
    app, _client, conn, _pid, card = at_point(tmp_path)
    answer = mgr.dispatch(conn, "n1", PEER, text, None, "2026-10-13T13:00:00", None, None)
    assert "причину" in answer and "отказ 5000100 старый год производства" in answer
    got = orders.get_card(conn, card)
    assert got["stage"]["code"] == "at_point" and got["cancel_reason"] == "" and linked(conn, card)
    assert not [e for e in orders.list_events(conn, card) if e["level"] == "red"] and mgr.list_inbox(conn) == []


def test_the_vk_command_that_cannot_move_the_order_says_why(tmp_path):
    app, _client, conn, _pid, card = at_point(tmp_path)
    conn.execute("UPDATE order_card SET client_name = '' WHERE id = ?", (card,))
    conn.commit()
    told = []
    answer = mgr.dispatch(conn, "f1", PEER, "отказ 5000100 старый год производства", None, "2026-10-13T13:00:00", None,
                          lambda c, t: told.append(t) or "отправлено")
    assert "Не получилось" in answer and "имя клиента" in answer
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point" and to_client(app) == []
    assert told and "Старый год производства" in told[0]                     # the office is told: the point said «отказ», nothing moved


def test_the_vk_command_does_not_fit_an_order_on_its_way(tmp_path):
    app, _client, tok, conn, _pid, card = ready(tmp_path)
    answer = mgr.dispatch(conn, "w1", PEER, "отказ 5000100 ошиблись размером", None, "2026-10-13T13:00:00", None, None)
    assert orders.get_card(conn, card)["stage"]["code"] == "moving" and "на этапе" in answer
    assert mgr.list_inbox(conn)[0]["dispute"].startswith("«отказ», а заказ на этапе")


def test_the_binding_names_the_new_command_and_not_the_old_one(tmp_path):
    text = mgr.BOUND % "Бета"
    assert "отказ 123456" in text and "не пришёл" not in text


def test_the_hint_of_the_card_names_refuse():
    from tires_sync import onboarding_texts
    assert "[ОТКАЗ]" in onboarding_texts.POINT["here"] and "не пришёл" not in onboarding_texts.POINT["here"]


# --- review round 1: a doubtful «отказ» never moves the order; «Отменить» closes the red record -----------------------------------

@pytest.mark.parametrize("text,reply", [("отказ 5000100?", None), ("Отказ?", 5000100), ("отказ не нужен, клиент приедет завтра", 5000100),
                                        ("Отказ от заказа 5999999, передумал", 5000100), ("отказ 5000100 5000200 размер", None),
                                        ("отказ 5000100 нет, приедет", None)])
def test_a_doubtful_vk_refusal_waits_for_the_owner_and_moves_nothing(tmp_path, text, reply):
    app, _client, conn, _pid, card = at_point(tmp_path)
    answer = mgr.dispatch(conn, "d1", PEER, text, reply, "2026-10-13T13:00:00", None, None)
    got = orders.get_card(conn, card)
    assert got["stage"]["code"] == "at_point" and got["cancel_reason"] == "" and linked(conn, card), answer
    assert mgr.list_inbox(conn) and mgr.list_inbox(conn)[0]["dispute"], answer
    assert "владельцу" in answer


@pytest.mark.parametrize("text", ["отказ 5000100 —", "отказ 5000100 ?!", "отказ 5000100 123"])
def test_a_reason_without_letters_is_no_reason(tmp_path, text):
    app, _client, conn, _pid, card = at_point(tmp_path)
    mgr.dispatch(conn, "l1", PEER, text, None, "2026-10-13T13:00:00", None, None)
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point"
    assert mgr.refuse_reason("—") == "" and mgr.refuse_reason(" – ? ") == "" and mgr.refuse_reason("12") == ""


def test_a_reason_with_a_negation_inside_is_still_a_reason(tmp_path):
    app, _client, conn, _pid, card = at_point(tmp_path)
    mgr.dispatch(conn, "g1", PEER, "отказ 5000100 клиенту не подошёл год", None, "2026-10-13T13:00:00", None, None)
    assert orders.get_card(conn, card)["cancel_reason"] == mgr.REFUSE_REASON % "клиенту не подошёл год"


def test_several_forwarded_lines_and_a_number_not_among_them_waits_for_the_owner(tmp_path):
    app, _client, conn, pid, card = at_point(tmp_path)
    other = _card(conn, pid, webmim="5999999", code="at_point")
    mgr.dispatch(conn, "m1", PEER, "отказ 5999999 ошиблись размером", None, "2026-10-13T13:00:00", None, None, reply_nos=(5000100, 5000200))
    assert orders.get_card(conn, other)["stage"]["code"] == "at_point" and orders.get_card(conn, card)["stage"]["code"] == "at_point"
    assert mgr.list_inbox(conn)[0]["dispute"]


def test_the_undo_of_refuse_closes_its_red_record(tmp_path):
    from tires_sync import order_alerts
    app, _client, conn, pid, card = at_point(tmp_path)
    mine = manager(app, conn, pid)
    refuse(mine, card, "size")
    [red] = [e for e in orders.list_events(conn, card) if e["level"] == "red" and e["kind"] == "point"]
    assert red["id"] in {a["id"] for a in order_alerts.open_alerts(conn, "owner", card)}
    post(mine, "/my-point/%d/undo" % card, "tok")
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point"
    assert red["id"] not in {a["id"] for a in order_alerts.open_alerts(conn, "owner", card)}


# --- review round 2 ---------------------------------------------------------------------------------------------------------------

def test_the_vk_refusal_of_an_order_to_another_city_waits_for_the_owner(tmp_path):
    from tires_sync import order_intercity
    app, _client, conn, _pid, card = at_point(tmp_path)
    order_intercity.save(conn, card, {"enabled": "1", "city": "Казань", "carrier": "ПЭК", "track": ""}, "2026-10-13T12:30:00", "o")
    conn.commit()
    answer = mgr.dispatch(conn, "c1", PEER, "отказ 5000100 старый год производства", None, "2026-10-13T13:00:00", None, None)
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point" and "в другой город" in answer and mgr.list_inbox(conn)


@pytest.mark.parametrize("text", ["отказ 2019 года", "отказ 2019 год выпуска", "Отказ 2021г старый"])
def test_a_year_is_never_taken_for_the_order(tmp_path, text):
    """«отказ 2019 года» names the year of the tyres, not the order 2019 (review round 2): no order is moved, the point is asked which."""
    app, _client, conn, pid, card = at_point(tmp_path)
    year = orders.create_card(conn, dict(client_name="Год"), "2026-10-13T12:00:00", "o")
    conn.execute("UPDATE order_card SET id = 2019 WHERE id = ?", (year,))
    conn.commit()
    assert mgr.parse_refuse(text)[0] is None
    answer = mgr.dispatch(conn, "y1", PEER, text, None, "2026-10-13T13:00:00", None, None)
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point" and "О каком заказе" in answer
    assert mgr.parse_refuse("отказ 5000100 2019 года")[0] == 5000100 and mgr.parse_refuse("отказ №2019 размер")[0] == 2019


# --- review round 3 ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["отказ 5000100 год выпуска старый", "отказ 5000100 года 2019", "отказ 5000100 г.в. 2019"])
def test_the_order_number_before_the_word_year_is_still_the_order(tmp_path, text):
    app, _client, conn, _pid, card = at_point(tmp_path)
    assert mgr.parse_refuse(text)[0] == 5000100
    mgr.dispatch(conn, "y2", PEER, text, None, "2026-10-13T13:00:00", None, None)
    assert orders.get_card(conn, card)["stage"]["code"] == "oth"


def test_a_busy_base_after_the_move_still_says_moved(tmp_path, monkeypatch):
    """The move is saved first; a failure of its red record after it must not tell the point «не получилось» (review round 3)."""
    import sqlite3
    app, _client, conn, _pid, card = at_point(tmp_path)
    real = orders.add_event

    def busy(c, order_id, now, author, kind, text, **k):
        if kind == "point" and text.startswith("Отказ клиента на точке"):
            raise sqlite3.OperationalError("database is locked")
        return real(c, order_id, now, author, kind, text, **k)
    monkeypatch.setattr(orders, "add_event", busy)
    outcome, answer, ok = mgr.refuse(conn, orders.get_card(conn, card), "размер", "менеджер Бета", "2026-10-13T13:00:00", None)
    assert ok and "отказ отмечен" in answer and orders.get_card(conn, card)["stage"]["code"] == "oth" and "запись не сохранена" in outcome


# --- review round 4: a size or a year range after «отказ» is never an order number --------------------------------------------------

@pytest.mark.parametrize("number,text", [(205, "отказ 205/55 R16 мал"), (205, "отказ 205 55 16 размер мал"), (2018, "отказ 2018/2019 год"),
                                         (2019, "отказ 2019-2020 года"), (2019, "отказ 2019 выпуск"), (205, "отказ 205.55 r16")])
def test_a_size_or_a_year_after_the_word_moves_no_order(tmp_path, number, text):
    app, _client, conn, pid, card = at_point(tmp_path)
    panel = orders.create_card(conn, dict(client_name="Панель"), "2026-10-13T12:00:00", "o")
    conn.execute("UPDATE order_card SET id = ? WHERE id = ?", (number, panel))
    conn.commit()
    _card(conn, pid, card=number, code="at_point", webmim="")                     # the point's order known by its panel number
    answer = mgr.dispatch(conn, "s1", PEER, text, None, "2026-10-13T13:00:00", None, None)
    assert orders.get_card(conn, number)["stage"]["code"] == "at_point", answer
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point"


def test_a_short_panel_number_with_a_plain_reason_still_works(tmp_path):
    app, _client, conn, pid, card = at_point(tmp_path)
    panel = orders.create_card(conn, dict(client_name="Панель"), "2026-10-13T12:00:00", "o")
    conn.execute("UPDATE order_card SET id = 423 WHERE id = ?", (panel,))
    conn.commit()
    _card(conn, pid, card=423, code="at_point", webmim="")
    mgr.dispatch(conn, "s2", PEER, "отказ 423 ошиблись размером", None, "2026-10-13T13:00:00", None, None)
    assert orders.get_card(conn, 423)["stage"]["code"] == "oth"


# --- review round 5: a partial refusal or «забрал» in the reason waits for the owner; «отказ по заказу N» -----------------------------

@pytest.mark.parametrize("text", ["отказ 5000100 забрали 2 из 4, две не подошли", "отказ 5000100 одну шину, остальные выдал",
                                  "отказ 5000100 взял только 2", "отказ 5000100 частично, 2 шины", "отказ 5000100 клиент забрал",
                                  "отказ 5000100 старый год, но забрал", "отказ 5000100 клиент не пришёл", "отказ 5000100 на точке",
                                  "отказ 5000100 передал курьеру", "отказ 5000100 возврат", "отказ 5000100 невыкуп"])
def test_a_partial_refusal_or_another_command_in_the_reason_waits_for_the_owner(tmp_path, text):
    from tires_sync import order_rules as rules
    app, _client, conn, _pid, card = at_point(tmp_path)
    rules.add_command(conn, _stage(conn, "at_point"), "невыкуп", _stage(conn, "cancel"))     # a word of the owner's «Команды»
    answer = mgr.dispatch(conn, "p1", PEER, text, None, "2026-10-13T13:00:00", None, None)
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point" and linked(conn, card), answer
    assert mgr.list_inbox(conn) and mgr.list_inbox(conn)[0]["dispute"]


@pytest.mark.parametrize("text", ["Отказ по заказу 5000100 старый год производства", "отказ по 5000100 старый год производства"])
def test_refuse_by_the_order_works(tmp_path, text):
    app, _client, conn, _pid, card = at_point(tmp_path)
    mgr.dispatch(conn, "p2", PEER, text, None, "2026-10-13T13:00:00", None, None)
    assert orders.get_card(conn, card)["cancel_reason"] == mgr.REFUSE_REASON % "Старый год производства"


@pytest.mark.parametrize("reason", ["брак", "порез на боковине", "клиент нашёл дешевле", "ошиблись размером", "клиенту не подошёл год"])
def test_ordinary_reasons_are_not_doubtful(reason):
    assert mgr.refuse_doubt("отказ 5000100 " + reason, reason) == ""


# --- review round 6: the office alert names the order -------------------------------------------------------------------------------

def test_the_office_alert_names_the_order_when_moved_and_when_not(tmp_path):
    app, _client, conn, _pid, card = at_point(tmp_path)
    told = []
    notify = lambda c, t: told.append(t) or "отправлено"
    outcome, _a, ok = mgr.refuse(conn, orders.get_card(conn, card), "размер", "менеджер Бета", "2026-10-13T13:00:00", notify)
    assert ok and told[-1].startswith("Заказ №%d: Отказ клиента" % card) and outcome.endswith("отправлено") and "None" not in outcome
    orders.move_card(conn, card, _stage(conn, "at_point"), "2026-10-13T13:10:00", "o")
    conn.execute("UPDATE order_card SET client_phone = '' WHERE id = ?", (card,))
    conn.commit()
    outcome, _a, ok = mgr.refuse(conn, orders.get_card(conn, card), "размер", "менеджер Бета", "2026-10-13T13:20:00", notify)
    assert not ok and told[-1].startswith("Заказ №%d: Точка «Бета» отметила отказ" % card) and "None" not in outcome
