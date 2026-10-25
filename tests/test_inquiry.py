"""The inquiry of a client (the owner 2026-10-23, answers 8–12; the lead's answers of the same day): новое → в работе → заказал / отказ,
moved by the look of every minute (inquiry.tick) and by the pages the staff open; and «Перезвонить»."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from tires_sync import clients, inquiry, orders, store, users

NOW = "2026-10-23T12:00:00"


def at(minutes=0, base=NOW) -> str:
    return (datetime.fromisoformat(base) + timedelta(minutes=minutes)).isoformat(timespec="seconds")


@pytest.fixture
def conn(tmp_path):
    c = store.connect(str(tmp_path / "t.db"))
    orders.ensure_pipeline(c, "2026-10-01T10:00:00")
    c.execute("INSERT INTO account (id, name, feed_token, created, chats_enabled) VALUES (1, 'Альфа', 't1', '2026-10-01', 1), "
              "(2, 'Бета', 't2', '2026-10-01', 1)")
    c.commit()
    return c


def chat(conn, peer="p1", name="Татьяна", account=1, order_id=None):
    return conn.execute("INSERT INTO avito_chat (account_id, chat_id, peer_id, peer_name, order_id) VALUES (?,?,?,?,?)",
                        (account, "c-%s-%d" % (peer, account), peer, name, order_id)).lastrowid


def message(conn, chat_row, when, direction="in", text="Есть 205/55 R16?", kind="text"):
    ts = int(datetime.fromisoformat(when).timestamp())
    conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, kind, text) VALUES (?,?,?,?,?,?,?)",
                 (chat_row, "m%d-%s-%s" % (chat_row, when, direction), when, ts, direction, kind, text))
    conn.execute("UPDATE avito_chat SET last_at = ?, last_dir = ?, last_text = ? WHERE id = ?", (when, direction, text, chat_row))
    conn.commit()


def call(conn, when, phone="+79001112233", answered=0, account=1):
    row = conn.execute("INSERT INTO avito_call (account_id, call_id, call_at, buyer_phone, answered) VALUES (?,?,?,?,?)",
                       (account, abs(hash((when, phone))) % 10 ** 9, when, phone, answered)).lastrowid
    clients.attach_call(conn, row, when, "звонки")
    conn.commit()
    return row


def status(conn, client_id):
    row = inquiry.get(conn, client_id)
    return row["status"] if row else None


def client_of_chat(conn, chat_row):
    return conn.execute("SELECT client_id FROM client_chat WHERE chat_row = ?", (chat_row,)).fetchone()[0]


def stage(conn, code):
    return next(s for s in orders.list_stages(conn) if s["code"] == code)


def order(conn, client_chat=None, phone="", code="new", when=NOW, by="owner"):
    card_id = orders.create_card(conn, {"client_name": "Татьяна", "client_phone": phone}, when, by)
    move(conn, card_id, code, when)
    if client_chat is not None:
        clients._link_order(conn, client_of_chat(conn, client_chat), card_id, "chat", when)
    conn.commit()
    return card_id


def move(conn, card_id, code, when):
    conn.execute("UPDATE order_card SET stage_id = ?, entered_at = ?, updated_at = ? WHERE id = ?", (stage(conn, code)["id"], when, when, card_id))
    conn.commit()


# ---- новое / в работе ------------------------------------------------------------------------------------------------------------------

def test_a_message_of_a_client_makes_a_new_inquiry_and_links_the_chat_to_a_client(conn):
    h = chat(conn)
    message(conn, h, at(-5))
    assert inquiry.tick(conn, NOW)["new"] == 1
    client_id = client_of_chat(conn, h)                                          # the lead 2026-10-23: lane B links the chat
    row = inquiry.get(conn, client_id)
    assert row["status"] == "new" and row["new_at"] == at(-5) and row["round_at"] == at(-5)
    [audit] = [json.loads(r[0]) for r in conn.execute("SELECT detail FROM audit WHERE action = 'inquiry'")]
    assert audit == {"client": client_id, "from": "", "to": "new", "why": "клиент написал"}
    assert inquiry.tick(conn, at(1)) == {"new": 0, "work": 0, "ordered": 0, "refused": 0}    # a repeat of the look changes nothing


def test_our_answer_in_the_chat_puts_it_in_work(conn):
    h = chat(conn)
    message(conn, h, at(-5))
    message(conn, h, at(-2), direction="out", text="Есть, 4 шт.")
    inquiry.tick(conn, NOW)
    assert status(conn, client_of_chat(conn, h)) == "work"


def test_an_answer_before_the_inquiry_is_not_work(conn):
    h = chat(conn)
    message(conn, h, at(-60), direction="out", text="Здравствуйте")
    message(conn, h, at(-5))
    inquiry.tick(conn, NOW)
    assert status(conn, client_of_chat(conn, h)) == "new"


def test_a_missed_call_is_new_and_an_answered_one_is_in_work(conn):
    missed = call(conn, at(-10))
    talked = call(conn, at(-10), phone="+79005556677", answered=1)
    inquiry.tick(conn, NOW)
    of = lambda row: conn.execute("SELECT client_id FROM client_call WHERE call_row = ?", (row,)).fetchone()[0]     # noqa: E731
    assert status(conn, of(missed)) == "new" and status(conn, of(talked)) == "work"


def test_the_chats_and_calls_of_a_point_account_are_not_our_inquiries(conn):
    conn.execute("INSERT INTO point (name, avito_account_id) VALUES ('Бета', 2)")
    h = chat(conn, account=2)
    message(conn, h, at(-5))
    call(conn, at(-5), account=2)
    inquiry.tick(conn, NOW)
    assert conn.execute("SELECT COUNT(*) FROM client_inquiry").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM client_chat").fetchone()[0] == 0          # nor is the chat linked to a client


def test_the_past_is_not_looked_at(conn):
    store.set_state(conn, "chats_baseline_1", at(-60 * 24))
    old = chat(conn, peer="old")
    message(conn, old, at(-60 * 24 * 4))                                          # older than three days
    before = chat(conn, peer="before")
    message(conn, before, at(-60 * 25))                                           # before the account was first read
    inquiry.tick(conn, NOW)
    assert conn.execute("SELECT COUNT(*) FROM client_inquiry").fetchone()[0] == 0


def test_opening_a_page_puts_a_new_inquiry_in_work_once(conn):
    h = chat(conn)
    message(conn, h, at(-5))
    inquiry.tick(conn, NOW)
    client_id = client_of_chat(conn, h)
    assert inquiry.touch(conn, client_id, at(1), "worker2", "открыл чат") is True
    assert inquiry.touch(conn, client_id, at(2), "worker2", "открыл чат") is False
    row = inquiry.get(conn, client_id)
    assert row["status"] == "work" and row["worked_at"] == at(1)
    assert inquiry.touch(conn, 999, at(2), "worker2", "открыл чат") is False             # no inquiry: nothing


# ---- заказал ---------------------------------------------------------------------------------------------------------------------------

def test_a_deal_made_by_an_operator_is_work_and_a_placed_order_is_ordered(conn):
    h = chat(conn)
    message(conn, h, at(-5))
    inquiry.tick(conn, NOW)
    card_id = order(conn, client_chat=h, code="talk", when=at(1))
    inquiry.tick(conn, at(2))
    client_id = client_of_chat(conn, h)
    assert status(conn, client_id) == "work"
    move(conn, card_id, "placed", at(3))
    assert inquiry.tick(conn, at(4))["ordered"] == 1
    assert status(conn, client_id) == "ordered"


def test_ordered_after_refused_and_a_cancelled_order_is_refused(conn):
    h = chat(conn)
    message(conn, h, at(-5))
    inquiry.tick(conn, NOW)
    inquiry.tick(conn, at(60 * 24 * 3))
    client_id = client_of_chat(conn, h)
    assert status(conn, client_id) == "refused"
    card_id = order(conn, client_chat=h, code="placed", when=at(60 * 24 * 4))         # the owner: «заказал» before and after «отказ»
    inquiry.tick(conn, at(60 * 24 * 4 + 1))
    assert status(conn, client_id) == "ordered"
    move(conn, card_id, "cancel", at(60 * 24 * 5))
    inquiry.tick(conn, at(60 * 24 * 5 + 1))
    assert status(conn, client_id) == "refused"


def test_na_oth_is_not_an_order(conn):
    h = chat(conn)
    message(conn, h, at(-5))
    inquiry.tick(conn, NOW)
    order(conn, client_chat=h, code="oth", when=at(1))
    inquiry.tick(conn, at(2))
    assert status(conn, client_of_chat(conn, h)) == "new"


# ---- отказ -----------------------------------------------------------------------------------------------------------------------------

def test_three_days_without_an_order_is_refused_and_a_message_after_it_is_work(conn):
    h = chat(conn)
    message(conn, h, at(-5))
    inquiry.tick(conn, NOW)
    client_id = client_of_chat(conn, h)
    inquiry.tick(conn, at(60 * 24 * 3 - 10))
    assert status(conn, client_id) == "new"
    assert inquiry.tick(conn, at(60 * 24 * 3 - 4))["refused"] == 1
    message(conn, h, at(60 * 24 * 10), text="А сейчас есть?")                         # any time later
    inquiry.tick(conn, at(60 * 24 * 10 + 1))
    row = inquiry.get(conn, client_id)
    assert row["status"] == "work" and row["round_at"] == at(60 * 24 * 10)
    inquiry.tick(conn, at(60 * 24 * 13 + 1))                                          # three days of the new round
    assert status(conn, client_id) == "refused"


def test_an_open_deal_holds_the_refusal(conn):
    h = chat(conn)
    message(conn, h, at(-5))
    inquiry.tick(conn, NOW)
    card_id = order(conn, client_chat=h, code="wait", when=at(1))
    inquiry.tick(conn, at(60 * 24 * 5))
    client_id = client_of_chat(conn, h)
    assert status(conn, client_id) == "work"
    move(conn, card_id, "cancel", at(60 * 24 * 5 + 1))
    inquiry.tick(conn, at(60 * 24 * 5 + 2))
    assert status(conn, client_id) == "refused"


def test_a_message_after_a_pause_starts_a_new_round_of_three_days(conn):
    h = chat(conn)
    message(conn, h, at(-5))
    message(conn, h, at(-1), direction="out")
    message(conn, h, at(60 * 24 * 2))                                                 # two days later: a pause longer than the gap
    inquiry.tick(conn, at(60 * 24 * 2 + 1))
    client_id = client_of_chat(conn, h)
    assert inquiry.get(conn, client_id)["round_at"] == at(60 * 24 * 2)
    inquiry.tick(conn, at(60 * 24 * 4))
    assert status(conn, client_id) == "work"


def test_after_a_finished_purchase_a_message_after_a_pause_is_a_new_inquiry(conn, monkeypatch):
    h = chat(conn)
    message(conn, h, at(-5))
    inquiry.tick(conn, NOW)
    card_id = order(conn, client_chat=h, code="placed", when=at(1))
    inquiry.tick(conn, at(2))
    client_id = client_of_chat(conn, h)
    message(conn, h, at(60), text="Спасибо!")                                        # within the gap: still «заказал»
    inquiry.tick(conn, at(61))
    assert status(conn, client_id) == "ordered"
    move(conn, card_id, "done", at(60 * 24))
    inquiry.tick(conn, at(60 * 24 + 1))
    assert status(conn, client_id) == "ordered"
    message(conn, h, at(60 * 24 * 30), text="Нужны летние")
    inquiry.tick(conn, at(60 * 24 * 30 + 1))
    row = inquiry.get(conn, client_id)
    assert row["status"] == "new" and row["new_at"] == at(60 * 24 * 30)               # the old purchase is the past
    monkeypatch.setattr(clients, "NEW_INQUIRY_GAP_HOURS", 24 * 60, raising=False)      # lane A's constant is the gap
    assert inquiry.gap_hours() == 24 * 60


def test_a_client_handed_over_by_a_point_is_new_at_once(conn):
    pid = conn.execute("INSERT INTO point (name, avito_account_id) VALUES ('Бета', 2)").lastrowid
    card_id = orders.create_card(conn, {"client_name": "Иван", "client_phone": "+79001112233"}, at(-3), "система")
    orders.ensure_from_point_stage(conn)
    move(conn, card_id, orders.FROM_POINT[0], at(-3))
    conn.execute("INSERT INTO order_handoff (order_id, message_id, point_id, phone, created_at) VALUES (?, 'm1', ?, '+79001112233', ?)",
                 (card_id, pid, at(-3)))
    conn.commit()
    inquiry.tick(conn, NOW)
    client_id = conn.execute("SELECT client_id FROM client_order WHERE order_id = ?", (card_id,)).fetchone()[0]
    assert status(conn, client_id) == "new"
    inquiry.tick(conn, at(60 * 24 * 4))                                               # «Передано с точки» is an open deal: no refusal
    assert status(conn, client_id) == "new"
    conn.execute("INSERT INTO order_handoff_source (order_id, kind, taken_at, taken_by) VALUES (?, 'vk', ?, 'worker2')", (card_id, at(60 * 24 * 4 + 1)))
    conn.commit()
    inquiry.tick(conn, at(60 * 24 * 4 + 2))
    assert status(conn, client_id) == "work"                                          # «взяли в работу»


def test_the_inquiry_of_a_merged_client_goes_to_the_one_it_was_merged_into(conn):
    a, b = chat(conn, peer="a"), chat(conn, peer="b", name="Таня")
    message(conn, a, at(-5))
    message(conn, b, at(-4))
    inquiry.tick(conn, NOW)
    keep, gone = client_of_chat(conn, a), client_of_chat(conn, b)
    clients.merge(conn, keep, gone, at(1), "owner")
    conn.commit()
    inquiry.tick(conn, at(2))
    assert inquiry.get(conn, gone) is None and status(conn, keep) == "new"


# ---- the statuses in the panel -------------------------------------------------------------------------------------------------------

def test_statuses_of_chats_and_calls(conn):
    h = chat(conn)
    message(conn, h, at(-5))
    k = call(conn, at(-5), phone="+79005556677")
    inquiry.tick(conn, NOW)
    assert inquiry.of_chats(conn, [h, 999]) == {h: {"code": "new", "name": "новое"}}
    assert inquiry.of_calls(conn, [k]) == {k: {"code": "new", "name": "новое"}}


# ---- «Перезвонить» ---------------------------------------------------------------------------------------------------------------------

def test_a_callback_goes_to_the_responsible_of_the_deal_and_is_due_at_its_time(conn):
    users.add_user(conn, "worker2", "пароль-для-партнёра-1", NOW)
    h = chat(conn)
    message(conn, h, at(-5))
    inquiry.tick(conn, NOW)
    card_id = order(conn, client_chat=h, code="talk", when=at(1))
    conn.execute("UPDATE order_card SET responsible = 'worker2' WHERE id = ?", (card_id,))
    task = inquiry.add_callback(conn, at(60)[:16], "Подумает", NOW, "owner", order_id=card_id)
    got = inquiry.get_callback(conn, task)
    assert got["assignee"] == "worker2" and got["client_id"] == client_of_chat(conn, h) and got["due_at"] == at(60)[:16]
    logins = {"owner", "worker2"}
    assert inquiry.due_rows(conn, "worker2", at(59), "owner", logins) == []
    [row] = inquiry.due_rows(conn, "worker2", at(60), "owner", logins)
    assert row["level"] == "yellow" and row["text"].startswith("Перезвонить") and "Подумает" in row["text"] and row["href"] == "/orders/%d" % card_id
    assert inquiry.due_rows(conn, "owner", at(60), "owner", logins) == []             # not the owner's task
    assert inquiry.due_rows(conn, "worker2", at(121), "owner", logins)[0]["level"] == "red"
    assert [t["id"] for t in inquiry.open_callbacks(conn, client_id=client_of_chat(conn, h))] == [task]
    inquiry.close_callback(conn, task, "done", at(61), "worker2")
    assert inquiry.due_rows(conn, "worker2", at(200), "owner", logins) == [] and inquiry.open_callbacks(conn, order_id=card_id) == []
    with pytest.raises(inquiry.InquiryError):
        inquiry.close_callback(conn, task, "cancel", at(62), "worker2")


def test_a_callback_without_a_deal_goes_to_the_chat_responsible_else_the_author_and_a_removed_user_to_the_owner(conn):
    h = chat(conn)
    task = inquiry.add_callback(conn, at(10)[:16], "", NOW, "owner", chat_row=h)
    assert inquiry.get_callback(conn, task)["assignee"] == "owner"
    users.add_user(conn, "worker2", "пароль-для-партнёра-1", NOW)
    conn.execute("UPDATE avito_chat SET responsible = 'worker2' WHERE id = ?", (h,))
    other = inquiry.add_callback(conn, at(10)[:16], "", NOW, "owner", chat_row=h)
    assert inquiry.get_callback(conn, other)["assignee"] == "worker2"
    conn.execute("UPDATE callback_task SET assignee = 'gone' WHERE id = ?", (other,))          # the user left the panel afterwards
    assert [r["callback"] for r in inquiry.due_rows(conn, "owner", at(10), "owner", {"owner", "worker2"})] == [task, other]


def test_the_assignee_is_the_deal_maker_when_nobody_is_named_and_never_a_point_or_an_observer(conn):
    users.add_user(conn, "worker2", "пароль-для-партнёра-1", NOW)
    users.add_user(conn, "gamma", "пароль-для-точки-1", NOW, role="point", point_id=1)
    users.add_user(conn, "viewer", "пароль-наблюдателя", NOW, role="viewer")
    h = chat(conn)
    card_id = orders.create_card(conn, {"client_name": "Иван"}, NOW, "worker2")                  # nobody named: the maker answers for it
    conn.commit()
    assert inquiry.get_callback(conn, inquiry.add_callback(conn, at(10)[:16], "", NOW, "owner", order_id=card_id))["assignee"] == "worker2"
    robot = orders.create_card(conn, {"client_name": "Иван"}, NOW, "чаты Авито")                # the robot is nobody: the chat, then the author
    conn.execute("UPDATE avito_chat SET responsible = 'gamma' WHERE id = ?", (h,))             # a point that took its own chat is not the one
    conn.commit()
    task = inquiry.add_callback(conn, at(10)[:16], "", NOW, "owner", order_id=robot, chat_row=h)
    assert inquiry.get_callback(conn, task)["assignee"] == "owner"
    conn.execute("UPDATE order_card SET responsible = 'viewer' WHERE id = ?", (card_id,))
    assert inquiry.get_callback(conn, inquiry.add_callback(conn, at(10)[:16], "", NOW, "worker2", order_id=card_id))["assignee"] == "worker2"


@pytest.mark.parametrize("due, error", [("", "Укажите дату"), ("2026-10-23T25:00", "Укажите дату"), ("2026-10-23T11:00", "уже прошло")])
def test_a_callback_needs_a_time_to_come(conn, due, error):
    h = chat(conn)
    with pytest.raises(inquiry.InquiryError, match=error):
        inquiry.add_callback(conn, due, "", NOW, "owner", chat_row=h)
    with pytest.raises(inquiry.InquiryError, match="кому"):
        inquiry.add_callback(conn, at(10)[:16], "", NOW, "owner")


# ---- the look of every minute in chats-sync ---------------------------------------------------------------------------------------------

def test_the_minute_look_starts_urgent_moves_statuses_and_never_breaks_the_chats(conn, monkeypatch, capsys):
    from tires_sync import cli, order_alerts
    h = chat(conn)
    message(conn, h, at(-5))
    cli._inquiry_look(conn, None, NOW)
    assert store.get_state(conn, order_alerts.URGENT_START_KEY) == NOW and status(conn, client_of_chat(conn, h)) == "new"
    assert "обращения: новое 1" in capsys.readouterr().out
    cli._inquiry_look(conn, None, at(5))
    assert store.get_state(conn, order_alerts.URGENT_START_KEY) == NOW                # set once
    monkeypatch.setattr(inquiry, "tick", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    from tires_sync import order_notify
    nets = []
    monkeypatch.setattr(order_notify, "escalate_unseen", lambda *a, **k: nets.append(1) or 0)
    cli._inquiry_look(conn, None, at(6))
    assert "обращения не проверены (RuntimeError: boom)" in capsys.readouterr().err
    assert nets == [1]                                                               # the safety net of the points still ran


def test_one_broken_event_does_not_stop_the_others(conn, monkeypatch):
    a, b = chat(conn, peer="a"), chat(conn, peer="b")
    message(conn, a, at(-5))
    message(conn, b, at(-4))
    real = clients.attach_chat

    def broken(c, chat_row, *args):
        if chat_row == a:
            raise RuntimeError("broken")
        return real(c, chat_row, *args)
    monkeypatch.setattr(clients, "attach_chat", broken)
    inquiry.tick(conn, NOW)
    assert status(conn, client_of_chat(conn, b)) == "new"



# ---- the review of round 1 ---------------------------------------------------------------------------------------------------------------

def handoff(conn, card_id, when, pid=1):
    conn.execute("INSERT OR REPLACE INTO order_handoff (order_id, message_id, point_id, phone, created_at) VALUES (?, ?, ?, '+79001112233', ?)",
                 (card_id, "m-%s" % when, pid, when))
    conn.commit()


def test_a_handoff_on_an_older_order_is_not_lost(conn):
    """A point may hand a client over on an order the chat already had: its number is older than the last handoff looked at."""
    old = orders.create_card(conn, {"client_name": "Иван", "client_phone": "+79001112233"}, at(-60), "owner")
    new = orders.create_card(conn, {"client_name": "Пётр", "client_phone": "+79005556677"}, at(-30), "owner")
    conn.commit()
    handoff(conn, new, at(-5))
    inquiry.tick(conn, NOW)
    handoff(conn, old, at(1))
    inquiry.tick(conn, at(2))
    client_id = conn.execute("SELECT client_id FROM client_order WHERE order_id = ?", (old,)).fetchone()[0]
    assert status(conn, client_id) == "new"
    inquiry.touch(conn, client_id, at(3), "owner", "открыл")
    inquiry.tick(conn, at(4))                                                         # a handoff looked at is not looked at again
    assert status(conn, client_id) == "work"


def test_a_client_who_ordered_before_and_is_handed_over_again_is_new(conn):
    h = chat(conn)
    message(conn, h, at(-5))
    inquiry.tick(conn, NOW)
    card_id = order(conn, client_chat=h, code="done", when=at(1))
    inquiry.tick(conn, at(2))
    client_id = client_of_chat(conn, h)
    assert status(conn, client_id) == "ordered"
    orders.ensure_from_point_stage(conn)
    again = orders.create_card(conn, {"client_name": "Татьяна"}, at(60), "система")
    move(conn, again, orders.FROM_POINT[0], at(60))
    clients._link_order(conn, client_id, again, "phone", at(60))
    handoff(conn, again, at(60))
    inquiry.tick(conn, at(61))
    row = inquiry.get(conn, client_id)
    assert row["status"] == "new" and row["new_at"] == at(60)
    assert card_id


def test_avito_notes_in_a_chat_are_not_the_client(conn):
    h = chat(conn)
    message(conn, h, at(-5), kind="system", text="Покупатель оставил отзыв")
    message(conn, h, at(-4), kind="deleted", text="")
    inquiry.tick(conn, NOW)
    assert conn.execute("SELECT COUNT(*) FROM client_inquiry").fetchone()[0] == 0


def test_a_placed_order_gone_back_with_a_deal_still_open_is_work(conn):
    h = chat(conn)
    message(conn, h, at(-5))
    inquiry.tick(conn, NOW)
    card_id = order(conn, client_chat=h, code="placed", when=at(1))
    inquiry.tick(conn, at(2))
    move(conn, card_id, "wait", at(3))
    inquiry.tick(conn, at(4))
    assert status(conn, client_of_chat(conn, h)) == "work"


def test_ordered_and_refused_clients_are_looked_at_again_only_when_an_order_changes(conn, monkeypatch):
    h = chat(conn)
    message(conn, h, at(-5))
    inquiry.tick(conn, NOW)
    card_id = order(conn, client_chat=h, code="placed", when=at(1))
    inquiry.tick(conn, at(2))
    move(conn, card_id, "moving", at(3))                                              # the order goes on: still «заказал»
    looked = []
    real = inquiry.look_orders
    monkeypatch.setattr(inquiry, "look_orders", lambda c, cid, *a: looked.append(cid) or real(c, cid, *a))
    inquiry.tick(conn, at(4))
    assert looked == [client_of_chat(conn, h)]
    inquiry.tick(conn, at(5))                                                         # nothing changed since: not looked at again
    assert looked == [client_of_chat(conn, h)] and status(conn, client_of_chat(conn, h)) == "ordered"


def test_a_change_made_meanwhile_is_not_overwritten(conn):
    h = chat(conn)
    message(conn, h, at(-5))
    inquiry.tick(conn, NOW)
    client_id = client_of_chat(conn, h)
    stale = inquiry.get(conn, client_id)
    inquiry.touch(conn, client_id, at(1), "owner", "открыл чат")                     # the panel, between the look's read and write
    assert inquiry._put(conn, client_id, stale, at(2), "клиент написал", inquiry.AUTHOR, last_in_at=at(2)) is False
    assert status(conn, client_id) == "work"


def test_a_task_of_a_switched_off_user_goes_to_the_owner_and_avito_notes_are_not_our_answer(conn):
    users.add_user(conn, "worker2", "пароль-для-партнёра-1", NOW)
    h = chat(conn)
    conn.execute("UPDATE avito_chat SET responsible = 'worker2' WHERE id = ?", (h,))
    task = inquiry.add_callback(conn, at(10)[:16], "", NOW, "owner", chat_row=h)
    users.set_active(conn, "worker2", False, NOW, "owner")
    conn.commit()
    from tires_sync import order_alerts
    assert [r["callback"] for r in order_alerts.callback_rows(conn, "owner", at(10))] == [task]
    other = chat(conn, peer="p2")
    message(conn, other, at(-5))
    message(conn, other, at(-4), direction="out", kind="system", text="Автоматическое уведомление")
    inquiry.tick(conn, NOW)
    assert status(conn, client_of_chat(conn, other)) == "new"


# ---- «3 дня тишины» (the owner 2026-10-23) ----------------------------------------------------------------------------------------------

DAY = 60 * 24


def test_a_client_writing_every_day_is_never_refused(conn):
    h = chat(conn)
    message(conn, h, at(-5))
    message(conn, h, at(-1), direction="out", text="Есть")
    for n in range(1, 9):
        message(conn, h, at(n * 20 * 60), text="Ещё думаю")                      # every 20 hours: within the gap (no new round), not silence
        inquiry.tick(conn, at(n * 20 * 60 + 1))
        assert status(conn, client_of_chat(conn, h)) == "work"
    inquiry.tick(conn, at(160 * 60 + 3 * DAY - 1))
    assert status(conn, client_of_chat(conn, h)) == "work"
    inquiry.tick(conn, at(160 * 60 + 3 * DAY))                                        # three days after the last word
    assert status(conn, client_of_chat(conn, h)) == "refused"


def test_our_answer_by_a_person_holds_the_silence_and_the_auto_answer_does_not(conn):
    h = chat(conn)
    message(conn, h, at(-5))
    inquiry.tick(conn, NOW)
    client_id = client_of_chat(conn, h)
    message(conn, h, at(2 * DAY), direction="out", text="Пришли шины, ждём вас")    # a person answered on the second day
    inquiry.tick(conn, at(2 * DAY + 1))
    inquiry.tick(conn, at(4 * DAY))
    assert status(conn, client_id) == "work"
    message(conn, h, at(4 * DAY + 10), direction="out", kind="system", text="[Системное сообщение] Спасибо за обращение")
    inquiry.tick(conn, at(5 * DAY + 1))                                               # three days after the person's answer
    assert status(conn, client_id) == "refused"


def test_a_call_holds_the_silence(conn):
    k = call(conn, at(-5), answered=1)
    inquiry.tick(conn, NOW)
    client_id = conn.execute("SELECT client_id FROM client_call WHERE call_row = ?", (k,)).fetchone()[0]
    call(conn, at(20 * 60), answered=0)                                               # within the gap: no new round, but not silence
    inquiry.tick(conn, at(20 * 60 + 1))
    inquiry.tick(conn, at(3 * DAY))
    assert status(conn, client_id) == "work"
    inquiry.tick(conn, at(20 * 60 + 3 * DAY))
    assert status(conn, client_id) == "refused"


def test_an_inquiry_table_of_the_first_release_gets_the_new_columns(tmp_path):
    path = str(tmp_path / "old.db")
    old = store.connect(path)
    old.execute("DROP TABLE client_inquiry")
    old.execute("CREATE TABLE client_inquiry (client_id INTEGER PRIMARY KEY, status TEXT NOT NULL, new_at TEXT NOT NULL DEFAULT '', "
                "round_at TEXT NOT NULL DEFAULT '', last_in_at TEXT NOT NULL DEFAULT '', worked_at TEXT NOT NULL DEFAULT '', "
                "ordered_at TEXT NOT NULL DEFAULT '', refused_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '')")
    old.execute("INSERT INTO client_inquiry (client_id, status, new_at, round_at) VALUES (1, 'new', ?, ?)", (NOW, NOW))
    old.commit()
    old.close()
    conn = store.connect(path)
    assert inquiry.get(conn, 1)["active_at"] == "" and inquiry.get(conn, 1)["looked_at"] == ""


def test_a_merge_keeps_the_last_word_of_either_client(conn):
    a, b = chat(conn, peer="a"), chat(conn, peer="b", name="Таня")
    message(conn, a, at(-60))
    message(conn, b, at(-5))
    inquiry.tick(conn, NOW)
    keep, gone = client_of_chat(conn, a), client_of_chat(conn, b)
    conn.execute("UPDATE client_inquiry SET updated_at = ? WHERE client_id = ?", (at(1), keep))   # the kept row is the newer one
    clients.merge(conn, keep, gone, at(2), "owner")
    conn.commit()
    inquiry.tick(conn, at(3))
    assert inquiry.get(conn, keep)["active_at"] == at(-5)                              # Таня wrote later: three days from her word


def test_a_lost_write_still_moves_the_silence(conn, monkeypatch):
    h = chat(conn)
    message(conn, h, at(-5))
    inquiry.tick(conn, NOW)
    client_id = client_of_chat(conn, h)
    monkeypatch.setattr(inquiry, "_put", lambda *a, **k: False)                       # the panel wrote meanwhile
    inquiry.on_inbound(conn, client_id, at(2 * DAY), at(2 * DAY))
    assert inquiry.get(conn, client_id)["active_at"] == at(2 * DAY)


def test_a_message_read_late_whose_answer_was_seen_first_is_in_work_not_new(conn):
    """Хвост З13 (b): the history read later brings the client's message older than our answer the look has seen already: «в работе»."""
    h = chat(conn)
    message(conn, h, at(-5), direction="out", text="Добрый день, есть")          # our answer, seen first (no client yet: nothing to change)
    inquiry.tick(conn, at(-4))
    message(conn, h, at(-20), text="Есть 205/55 R16?")                           # the client's message, read later with an earlier time
    inquiry.tick(conn, NOW)
    client_id = client_of_chat(conn, h)
    row = inquiry.get(conn, client_id)
    assert row["status"] == "work" and row["worked_at"] == at(-5) and row["new_at"] == at(-20)
    message(conn, h, at(1), text="А цена?")                                      # a new message after the answer: the inquiry goes on as usual
    inquiry.tick(conn, at(2))
    assert status(conn, client_id) == "work"


def test_a_message_without_a_later_answer_is_new_as_before(conn):
    h = chat(conn)
    message(conn, h, at(-20), direction="out", text="Здравствуйте")              # an answer before the message is no answer to it
    message(conn, h, at(-5), text="Есть 205/55 R16?")
    inquiry.tick(conn, NOW)
    assert status(conn, client_of_chat(conn, h)) == "new"


def test_an_event_whose_look_failed_is_looked_at_again_next_time(conn, monkeypatch):
    """Хвост З13 (a): a message whose look broke (a race with calls-sync, the panel writing at once) is looked at in the next look."""
    h = chat(conn)
    message(conn, h, at(-5))
    real, broken = inquiry.on_inbound, [1]

    def once(*args, **kwargs):
        if broken:
            broken.pop()
            raise RuntimeError("database is locked")
        return real(*args, **kwargs)
    monkeypatch.setattr(inquiry, "on_inbound", once)
    inquiry.tick(conn, NOW)
    assert conn.execute("SELECT COUNT(*) FROM client_inquiry").fetchone()[0] == 0                # undone: no status yet
    assert json.loads(store.get_state(conn, inquiry.RETRY_MARK))[0][::2] == ["message", 1]
    inquiry.tick(conn, at(1))
    assert status(conn, client_of_chat(conn, h)) == "new" and json.loads(store.get_state(conn, inquiry.RETRY_MARK)) == []


def test_an_event_that_keeps_failing_is_tried_three_times_more_then_is_a_record_of_the_journal(conn, monkeypatch):
    h = chat(conn)
    message(conn, h, at(-5))
    tried = []

    def always(*args, **kwargs):
        tried.append(1)
        raise RuntimeError("broken client")
    monkeypatch.setattr(inquiry, "on_inbound", always)
    for minute in range(6):
        inquiry.tick(conn, at(minute))
    assert len(tried) == 1 + inquiry.RETRIES and json.loads(store.get_state(conn, inquiry.RETRY_MARK)) == []
    [(detail,)] = conn.execute("SELECT detail FROM audit WHERE action = 'inquiry_failed'").fetchall()
    assert json.loads(detail)["event"] == "message" and json.loads(detail)["tries"] == 4 and "broken client" in json.loads(detail)["why"]
