"""«Клиент написал» and «Срочно ответить» of the points are not the owner's nor the partner's (the owner 2026-11-03): neither of the orders and
chats of a point's Avito account (2026-10-26, for every staff role) nor of an order the point works now («Товар на точке», «На ОТХ»,
«Возврат передан», «Отправлен») and its chats (the owner and the partner only: the chats' manager still hears of them, the lead's B). The
failures, «после закрытия», the calls, the point user's rows and the work «за точку» stay as they were."""
from __future__ import annotations

from datetime import datetime

import pytest

from test_inquiry import message
from test_notify_by_point import OWN_ACCOUNT, POINT_ACCOUNT, card, chat, env, point
from tires_sync import chat_handover, chats, daily_summary, order_alerts, order_points as points, orders, store, users

NOW = "2026-10-15T12:00:00"
STAFF = ("owner", "zamantyres", "partner1")       # «owner» has no role row (an owner made before the roles); Заман is an owner by role
WROTE = "Клиент написал: Когда можно забрать?"


def setup(tmp_path):
    conn, box = env(tmp_path)
    users.add_user(conn, "zamantyres", "пароль-для-замана-1", NOW, role="owner")
    users.add_user(conn, "olga", "пароль-для-ольги-1", NOW, role="chats")      # the chats' manager: hears of the points' work (the lead, B)
    pid = point(conn, channel="panel")
    users.add_user(conn, "gamma1", "пароль-для-точки-1", NOW, role="point", point_id=pid)
    store.set_state(conn, order_alerts.URGENT_START_KEY, "2026-10-01T00:00:00")
    conn.commit()
    assert conn.execute("SELECT 1 FROM panel_user_role WHERE login = 'owner'").fetchone() is None
    return conn, pid


def on_stage(conn, card_id, code):
    conn.execute("UPDATE order_card SET stage_id = (SELECT id FROM order_stage WHERE code = ?) WHERE id = ?", (code, card_id))
    conn.commit()


def wrote(conn, card_id, text=WROTE, level="yellow"):
    orders.add_event(conn, card_id, NOW, "чаты Авито", "chat", text, level=level)
    conn.commit()


def answers(conn, login, *card_ids):
    """Make `login` the one who answers for the orders: since 2026-11-03 only they hear of them."""
    for card_id in card_ids:
        conn.execute("UPDATE order_card SET responsible = ? WHERE id = ?", (login, card_id))
    conn.commit()


def seen(conn, login, **kw):
    """The orders with a row of the client's or a failure on the page (not the green «Заказ создан» every order has)."""
    return {r["order_id"] for r in order_alerts.page_rows(conn, login, NOW, **kw) if r["order_id"] is not None and r["kind"] == "chat"}


def test_the_orders_and_chats_of_a_point_account_stay_away_from_both_owners_and_the_partner(tmp_path):
    conn, pid = setup(tmp_path)
    theirs = card(conn, "owner", pid)
    chat(conn, POINT_ACCOUNT, theirs["id"])
    wrote(conn, theirs["id"])
    lone = chat(conn, POINT_ACCOUNT, None, peer="x", unread=1)                 # a chat of the point's account without an order
    message(conn, lone, "2026-10-15T10:00:00", text="Есть шины?")
    for login in STAFF + ("olga",):
        rows = order_alerts.page_rows(conn, login, NOW)
        assert theirs["id"] not in seen(conn, login) and lone not in {r.get("chat_row") for r in rows}
        assert order_alerts.counts(conn, login)["yellow"] == 0
    assert theirs["id"] in seen(conn, "gamma1") and lone in {r.get("chat_row") for r in order_alerts.page_rows(conn, "gamma1", NOW)}


@pytest.mark.parametrize("code", points.WORK_STAGES)
def test_the_client_words_of_an_order_the_point_works_are_the_points_only(tmp_path, code):
    conn, pid = setup(tmp_path)
    mine = card(conn, "owner", pid)
    chat(conn, OWN_ACCOUNT, mine["id"])                                         # the chat of our account: not (a)
    on_stage(conn, mine["id"], code)
    wrote(conn, mine["id"])
    for login in STAFF:                                                         # even when they answer for it
        answers(conn, login, mine["id"])
        assert mine["id"] not in seen(conn, login) and order_alerts.order_levels(conn, login, NOW).get(mine["id"]) == "green"
        assert order_alerts.counts(conn, login)["yellow"] == 0
    answers(conn, "olga", mine["id"])
    assert mine["id"] in seen(conn, "olga") and order_alerts.counts(conn, "olga")["yellow"] == 1      # the chats' manager: as before
    assert mine["id"] in seen(conn, "gamma1")                                   # the point user's rows: not touched by this rule
    assert mine["id"] in seen(conn, "owner", staff=False)                       # the owner working as the point: the point's rows


def test_failures_after_closing_calls_and_other_orders_stay_with_the_staff(tmp_path):
    conn, pid = setup(tmp_path)
    failed = card(conn, "owner", pid)
    on_stage(conn, failed["id"], "at_point")
    wrote(conn, failed["id"], "Сообщение клиенту не отправлено: Авито 403", level="red")
    closed = card(conn, "owner", pid)
    on_stage(conn, closed["id"], "shipped")
    wrote(conn, closed["id"], "Клиент написал после закрытия заказа (2026-10-15 11:00)")
    called = card(conn, "owner", pid)
    on_stage(conn, called["id"], "at_point")
    wrote(conn, called["id"], "Клиент звонил: 2 мин")
    moving = card(conn, "owner", pid)                                         # not yet at the point
    on_stage(conn, moving["id"], "moving")
    wrote(conn, moving["id"])
    no_point = card(conn, "owner")
    on_stage(conn, no_point["id"], "at_point")
    wrote(conn, no_point["id"])
    handed = card(conn, "owner", pid)                                         # the point handed the client over: the staff's
    on_stage(conn, handed["id"], "at_point")
    conn.execute("INSERT INTO order_handoff (order_id, message_id, point_id, created_at) VALUES (?, 'h1', ?, ?)", (handed["id"], pid, NOW))
    wrote(conn, handed["id"])
    every = (failed["id"], closed["id"], called["id"], moving["id"], no_point["id"], handed["id"])
    for login in STAFF:
        answers(conn, login, *every)
        assert set(every) <= seen(conn, login)
        assert order_alerts.counts(conn, login)["red"] == 1


@pytest.mark.parametrize("code", points.WORK_STAGES)
def test_srochno_otvetit_of_an_order_the_point_works_is_not_the_owners_nor_the_partners(tmp_path, code):
    conn, pid = setup(tmp_path)
    there = card(conn, "owner", pid)
    on_stage(conn, there["id"], code)
    late = chat(conn, OWN_ACCOUNT, there["id"], peer="a")
    message(conn, late, "2026-10-15T10:30:00")
    before = card(conn, "owner", pid)
    on_stage(conn, before["id"], "placed")
    waits = chat(conn, OWN_ACCOUNT, before["id"], peer="b")
    message(conn, waits, "2026-10-15T10:30:00")
    for login in STAFF:
        answers(conn, login, there["id"], before["id"])
        urgent = {r["chat_row"] for r in order_alerts.page_rows(conn, login, NOW) if r.get("urgent")}
        assert urgent == {waits}
        assert order_alerts.order_levels(conn, login, NOW)[there["id"]] == "green"            # no red dot of «Срочно ответить»
    answers(conn, "olga", there["id"], before["id"])
    shipped = code == "shipped"                                               # «Отправлен» closes the order (won): never «срочно» (2026-11-03)
    assert {r["chat_row"] for r in order_alerts.page_rows(conn, "olga", NOW) if r.get("urgent")} == ({waits} if shipped else {late, waits})
    answers(conn, "owner", there["id"])
    on_stage(conn, there["id"], "placed")                                     # the point's work is over: the office's chat again
    assert late in {r["chat_row"] for r in order_alerts.page_rows(conn, "owner", NOW) if r.get("urgent")}


def test_the_morning_summary_does_not_count_the_points_chats(tmp_path):
    conn, pid = setup(tmp_path)
    for account, peer in ((POINT_ACCOUNT, "x"), (OWN_ACCOUNT, "y")):
        row = chat(conn, account, None, peer=peer, unread=1)
        message(conn, row, "2026-10-15T10:00:00", text="Есть шины?")
    text = daily_summary.text(conn, datetime(2026, 10, 15, 12, 0))
    assert "Чаты: непрочитанных 1, ждут ответа 1." in text


def test_the_client_words_written_while_at_the_point_stay_quiet_after_the_order_leaves_it(tmp_path):
    conn, pid = setup(tmp_path)
    mine = card(conn, "owner", pid)
    row = chat(conn, OWN_ACCOUNT, mine["id"], unread=1)
    message(conn, row, "2026-10-15T11:30:00", text="Спасибо, забрал")   # not «Срочно» yet
    on_stage(conn, mine["id"], "at_point")
    chats._note_client_wrote(conn, row, NOW)                                   # the real writer of «Клиент написал»
    conn.commit()
    on_stage(conn, mine["id"], "done")                                         # the point's work is over: still not the owner's (review D5)
    for login in STAFF:
        answers(conn, login, mine["id"])
        assert mine["id"] not in seen(conn, login)
    answers(conn, "olga", mine["id"])
    assert mine["id"] in seen(conn, "olga")
    later = card(conn, "owner", pid)                                           # written while not at the point: as before
    row2 = chat(conn, OWN_ACCOUNT, later["id"], peer="q", unread=1)
    message(conn, row2, "2026-10-15T11:30:00")
    on_stage(conn, later["id"], "moving")
    chats._note_client_wrote(conn, row2, NOW)
    conn.commit()
    assert later["id"] in seen(conn, "owner")


def test_a_client_the_owner_or_the_partner_took_from_the_point_is_told_as_usual(tmp_path):
    conn, pid = setup(tmp_path)
    lone = chat(conn, POINT_ACCOUNT, None, peer="x", unread=1)                 # a point's chat handed over and taken by Заман
    message(conn, lone, "2026-10-15T10:30:00", text="Есть шины?")
    conn.execute("INSERT INTO chat_handover (chat_row, point_id, point_name, at, handed_by) VALUES (?, ?, 'Гамма', ?, 'gamma1')", (lone, pid, NOW))
    conn.commit()
    assert lone not in {r.get("chat_row") for r in order_alerts.page_rows(conn, "owner", NOW)}
    assert chat_handover.take(conn, lone, "zamantyres", NOW)
    assigned = card(conn, "gamma1", pid)                                       # an order at the point the owner made the partner answer for
    on_stage(conn, assigned["id"], "at_point")
    orders.update_card(conn, assigned["id"], {"responsible": "partner1"}, NOW, "owner")
    wrote(conn, assigned["id"])
    answered = card(conn, "gamma1", pid)                                       # a chat of an order at the point the owner answers
    on_stage(conn, answered["id"], "oth")
    row = chat(conn, OWN_ACCOUNT, answered["id"], peer="a")
    message(conn, row, "2026-10-15T10:30:00")
    chats.set_responsible(conn, row, "owner", NOW, "owner")
    by_default = card(conn, "owner", pid)                                      # «owner» only by default (no one named them): still quiet
    on_stage(conn, by_default["id"], "at_point")
    wrote(conn, by_default["id"])
    for login in STAFF:                                     # the point answers for `answered`: the taken client is the office's as before;
        rows = order_alerts.page_rows(conn, login, NOW)     # `assigned` has a person: theirs only (2026-11-03)
        assert {r["chat_row"] for r in rows if r.get("urgent")} == {lone, row}
        assert (assigned["id"] in seen(conn, login)) == (login == "partner1") and by_default["id"] not in seen(conn, login)
    orders.update_card(conn, answered["id"], {"responsible": "zamantyres"}, NOW, "owner")           # a person answers for it now: theirs only
    assert row in {r["chat_row"] for r in order_alerts.page_rows(conn, "zamantyres", NOW) if r.get("urgent")}
    assert row not in {r["chat_row"] for r in order_alerts.page_rows(conn, "owner", NOW) if r.get("urgent")}
    olga = order_alerts.page_rows(conn, "olga", NOW)                           # the chats' manager: the point's account stays the point's
    assert lone not in {r.get("chat_row") for r in olga}
    gamma = order_alerts.page_rows(conn, "gamma1", NOW)                         # the point's rows: no «Срочно ответить» of its account
    assert lone not in {r["chat_row"] for r in gamma if r.get("urgent")}


def test_an_order_taken_from_the_point_brings_back_its_client_words_of_every_kind(tmp_path):
    conn, pid = setup(tmp_path)
    theirs = card(conn, "gamma1", pid)                                         # an order of the point's account (a), taken by the owner
    chat(conn, POINT_ACCOUNT, theirs["id"], peer="t")
    wrote(conn, theirs["id"])
    working = card(conn, "gamma1", pid)                                        # «Клиент написал» written at the point (POINT_WROTE_AUTHOR)
    row = chat(conn, OWN_ACCOUNT, working["id"], peer="w", unread=1)
    message(conn, row, "2026-10-15T11:30:00")
    on_stage(conn, working["id"], "at_point")
    chats._note_client_wrote(conn, row, NOW)
    on_stage(conn, working["id"], "done")
    of_chat = card(conn, "gamma1", pid)                                        # an order of a chat the partner answers
    on_stage(conn, of_chat["id"], "oth")
    chats.set_responsible(conn, chat(conn, OWN_ACCOUNT, of_chat["id"], peer="c"), "partner1", NOW, "owner")
    wrote(conn, of_chat["id"])
    conn.commit()
    for login in STAFF:                                     # the point answers for all three; of_chat is taken (its chat): as before
        assert {theirs["id"], working["id"], of_chat["id"]} & seen(conn, login) == {of_chat["id"]}
    orders.update_card(conn, theirs["id"], {"responsible": "owner"}, NOW, "partner1")
    orders.update_card(conn, working["id"], {"responsible": "zamantyres"}, NOW, "owner")
    for login, mine in (("owner", {theirs["id"], of_chat["id"]}), ("zamantyres", {working["id"], of_chat["id"]}), ("partner1", {of_chat["id"]})):
        assert {theirs["id"], working["id"], of_chat["id"]} & seen(conn, login) == mine         # a person answers: theirs only (2026-11-03)
        assert order_alerts.counts(conn, login)["yellow"] == len(mine)
    assert theirs["id"] not in seen(conn, "olga")                               # the chats' manager: the point's account stays the point's
