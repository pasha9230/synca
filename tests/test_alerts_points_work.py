"""«Клиент написал» and «Срочно ответить» of the points are not the owner's nor the partner's (the owner 2026-11-03): neither of the orders and
chats of a point's Avito account (2026-10-26, for every staff role) nor of an order the point works now («Товар на точке», «На ОТХ»,
«Возврат передан», «Отправлен») and its chats (the owner and the partner only: the chats' manager still hears of them, the lead's B). The
failures, «после закрытия», the calls, the point user's rows and the work «за точку» stay as they were."""
from __future__ import annotations

from datetime import datetime

import pytest

from test_inquiry import message
from test_notify_by_point import OWN_ACCOUNT, POINT_ACCOUNT, card, chat, env, point
from tires_sync import daily_summary, order_alerts, order_points as points, orders, store, users

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
    for login in STAFF:
        assert mine["id"] not in seen(conn, login) and order_alerts.order_levels(conn, login, NOW).get(mine["id"]) == "green"
        assert order_alerts.counts(conn, login)["yellow"] == 0
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
    for login in STAFF:
        assert {failed["id"], closed["id"], called["id"], moving["id"], no_point["id"], handed["id"]} <= seen(conn, login)
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
        urgent = {r["chat_row"] for r in order_alerts.page_rows(conn, login, NOW) if r.get("urgent")}
        assert urgent == {waits}
        assert order_alerts.order_levels(conn, login, NOW)[there["id"]] == "green"            # no red dot of «Срочно ответить»
    assert {r["chat_row"] for r in order_alerts.page_rows(conn, "olga", NOW) if r.get("urgent")} == {late, waits}
    on_stage(conn, there["id"], "done")                                       # the point's work is over: the office's chat again
    assert late in {r["chat_row"] for r in order_alerts.page_rows(conn, "owner", NOW) if r.get("urgent")}


def test_the_morning_summary_does_not_count_the_points_chats(tmp_path):
    conn, pid = setup(tmp_path)
    for account, peer in ((POINT_ACCOUNT, "x"), (OWN_ACCOUNT, "y")):
        row = chat(conn, account, None, peer=peer, unread=1)
        message(conn, row, "2026-10-15T10:00:00", text="Есть шины?")
    text = daily_summary.text(conn, datetime(2026, 10, 15, 12, 0))
    assert "Чаты: непрочитанных 1, ждут ответа 1." in text
