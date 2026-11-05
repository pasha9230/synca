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
    _t, page = refuse(mine, card, "year")
    assert "«ОТКАЗ»" in page
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
