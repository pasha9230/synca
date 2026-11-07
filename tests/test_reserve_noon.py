"""The owner 2026-11-02: on the last day of the reservation the client gets «срок резерва истекает» by itself from 12:30, whatever the point
answered (the point is still asked as before); when the client writes nothing in that chat for 12 hours after it, the order goes to «На ОТХ»
by itself — not when the point marked «выдан», the order left «Товар на точке», the date was moved, or the reminder did not go."""
from __future__ import annotations

from test_order_reserve import PointVk, card_at, chat_of, db, ok, stage
from tires_sync import chats, order_reserve as reserve, orders

DAY = "2026-10-18"                                     # the last day of a card that came to the point on 2026-10-15
NEXT = "2026-10-19"


class Deliver:
    """chats.send_reply as order_reserve.check gets it: records the message and its send mark at the moment of the tick (`at`)."""

    def __init__(self, conn, error=None):
        self.conn, self.error, self.sent, self.at = conn, error, [], ""

    def __call__(self, chat_row, text, token):
        if self.error is not None:
            raise self.error
        self.sent.append((chat_row, text, token))
        self.conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status) VALUES (?, ?, ?, 'система', 'sent')", (token, chat_row, self.at))


def ready(tmp_path, error=None):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    return conn, cid, chat_of(conn, cid), Deliver(conn, error), PointVk()


def tick(conn, when, deliver, vk=None):
    deliver.at = when
    return reserve.check(conn, when, ok, deliver, vk if vk is not None else PointVk())


def code(conn, cid):
    return orders.get_card(conn, cid)["stage"]["code"]


def wrote(conn, chat, at, direction="in", kind="text"):
    """A message of the chat as the sync keeps it (chats._store_message: `at` is Moscow time)."""
    conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, kind, text) VALUES (?, ?, ?, 0, ?, ?, 'продлите, пожалуйста')",
                 (chat, "m-%s-%s" % (direction, at), at, direction, kind))
    conn.commit()


# ---- 1. the reminder at 12:30 -----------------------------------------------------------------------------------------------------

def test_the_reminder_goes_at_12_30_not_at_12_29_and_once(tmp_path):
    conn, cid, chat, deliver, vk = ready(tmp_path)
    tick(conn, DAY + "T10:07:00", deliver, vk)
    assert len(vk.sent) == 1 and deliver.sent == []                                     # the point is asked as before
    tick(conn, DAY + "T12:29:00", deliver, vk)
    assert deliver.sent == []
    tick(conn, DAY + "T12:30:00", deliver, vk)
    assert [(c, t.split("!")[0]) for c, t, _ in deliver.sent] == [(chat, "Здравствуйте")]  # nobody answered: it goes all the same
    for when in (DAY + "T13:07:00", DAY + "T19:07:00"):
        tick(conn, when, deliver, vk)
    assert len(deliver.sent) == 1 and len(vk.sent) == 1


def test_no_reminder_after_the_working_hours_nor_the_next_day(tmp_path):
    conn, _cid, _chat, deliver, vk = ready(tmp_path)
    tick(conn, DAY + "T20:07:00", deliver, vk)                                          # the working hours are 10–20
    tick(conn, NEXT + "T12:30:00", deliver, vk)                                         # «истекает» is no longer true
    assert deliver.sent == []


def test_issued_before_12_30_tells_the_client_nothing(tmp_path):
    conn, cid, _chat, deliver, vk = ready(tmp_path)
    tick(conn, DAY + "T10:07:00", deliver, vk)
    orders.move_card(conn, cid, stage(conn, "done"), DAY + "T11:00:00", "точка")
    tick(conn, DAY + "T12:30:00", deliver, vk)
    assert deliver.sent == []


def test_the_point_saying_here_sends_nothing_and_the_tick_sends_it(tmp_path):
    conn, cid, _chat, deliver, vk = ready(tmp_path)
    tick(conn, DAY + "T10:07:00", deliver, vk)
    assert reserve.here(conn, orders.get_card(conn, cid), "менеджер Бета", DAY + "T10:30:00", deliver) == (
        "на точке", "Заказ №5200001 — отмечено: на точке.")
    assert deliver.sent == []
    tick(conn, DAY + "T12:30:00", deliver, vk)
    assert len(deliver.sent) == 1


def test_a_reminder_that_did_not_go_is_alerted_as_before(tmp_path):
    conn, cid, _chat, deliver, vk = ready(tmp_path, chats.ChatError("У аккаунта не заданы ключи Авито."))
    tick(conn, DAY + "T12:30:00", deliver, vk)
    [red] = [e for e in orders.list_events(conn, cid) if e["level"] == "red"]
    assert red["text"].startswith("Сообщение клиенту не отправлено («Клиенту: срок резерва истекает»)") and "не заданы ключи" in red["text"]
    assert [r[0] for r in conn.execute("SELECT text FROM manager_message")][0].startswith("Заказ №5200001: клиенту не отправлено")


# ---- 2. no answer of the client for 12 hours: «На ОТХ» -----------------------------------------------------------------------------

def read_chats(conn, at):
    """The sync read the chats of the accounts at `at` (avito_chat.synced_at, Moscow time, as chats.sync_account)."""
    conn.execute("UPDATE avito_chat SET synced_at = ?", (at,))
    conn.commit()


def told(tmp_path, error=None):
    conn, cid, chat, deliver, vk = ready(tmp_path, error)
    tick(conn, DAY + "T12:30:00", deliver, vk)
    read_chats(conn, "2099-01-01T00:00:00")                                             # the chats are read (the guard of the review)
    return conn, cid, chat, deliver, vk


def test_twelve_hours_of_silence_move_the_order_to_oth_not_a_second_earlier(tmp_path):
    conn, cid, _chat, _deliver, _vk = told(tmp_path)
    assert not reserve._no_answer_step(conn, orders.get_card(conn, cid), NEXT + "T00:29:59")
    assert code(conn, cid) == "at_point"
    assert reserve._no_answer_step(conn, orders.get_card(conn, cid), NEXT + "T00:30:00")
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == "oth" and card["cancel_reason"] == reserve.NO_ANSWER_REASON
    [record] = [e for e in orders.list_events(conn, cid) if "не ответил за 12 часов" in e["text"]]
    assert record["text"].startswith("Клиент не ответил за 12 часов на напоминание о сроке резерва: заказ переведён на «На ОТХ»")
    assert record["author"] == "система"


def test_the_tick_moves_it_at_night_once(tmp_path):
    conn, cid, _chat, deliver, vk = told(tmp_path)                                      # the lead 2026-11-04: on time; before: from 09:00
    tick(conn, NEXT + "T00:07:00", deliver, vk)                                         # not yet 12 hours
    assert code(conn, cid) == "at_point"
    tick(conn, NEXT + "T01:07:00", deliver, vk)
    assert code(conn, cid) == "oth"
    tick(conn, NEXT + "T09:07:00", deliver, vk)
    assert code(conn, cid) == "oth"
    tick(conn, NEXT + "T10:07:00", deliver, vk)
    assert sum("не ответил за 12 часов" in e["text"] for e in orders.list_events(conn, cid)) == 1


def test_a_message_of_the_client_after_the_reminder_keeps_the_order_at_the_point(tmp_path):
    conn, cid, chat, deliver, vk = told(tmp_path)
    wrote(conn, chat, DAY + "T23:59:00")
    tick(conn, NEXT + "T09:07:00", deliver, vk)
    tick(conn, NEXT + "T10:07:00", deliver, vk)
    assert code(conn, cid) == "at_point"
    assert sum("Клиент ответил на напоминание о сроке резерва: на «На ОТХ» не переводим" in e["text"] for e in orders.list_events(conn, cid)) == 1
    assert sum(e["text"].startswith("Ответственному: «Клиент ответил") for e in orders.list_events(conn, cid)) == 1    # no date: 2026-11-06


def test_our_own_messages_and_the_clients_before_the_reminder_do_not_count(tmp_path):
    conn, cid, chat, deliver, vk = told(tmp_path)
    wrote(conn, chat, DAY + "T11:00:00")                                                # before the reminder
    wrote(conn, chat, DAY + "T14:00:00", direction="out")                               # ours
    wrote(conn, chat, DAY + "T15:00:00", kind="system")                                 # Avito's
    tick(conn, NEXT + "T09:07:00", deliver, vk)
    assert code(conn, cid) == "oth"


def test_a_reminder_that_did_not_go_never_moves_the_order(tmp_path):
    conn, cid, _chat, deliver, vk = told(tmp_path, chats.ChatError("Авито не принял сообщение (403)."))
    tick(conn, NEXT + "T09:07:00", deliver, vk)
    tick(conn, "2026-10-20T12:07:00", deliver, vk)
    assert code(conn, cid) == "at_point"


def test_a_moved_date_or_an_issue_after_the_reminder_moves_nothing(tmp_path):
    conn, cid, _chat, deliver, vk = told(tmp_path)
    orders.update_card(conn, cid, {"reserve_until": "2026-10-21"}, DAY + "T15:00:00", "o")      # the client asked for more time by phone
    tick(conn, NEXT + "T09:07:00", deliver, vk)
    assert code(conn, cid) == "at_point"
    (tmp_path / "b").mkdir()
    conn, cid, _chat, deliver, vk = told(tmp_path / "b")
    orders.move_card(conn, cid, stage(conn, "done"), DAY + "T16:00:00", "точка")        # «выдан» after the reminder
    tick(conn, NEXT + "T09:07:00", deliver, vk)
    assert code(conn, cid) == "done"


def test_a_refused_move_is_one_red_record(tmp_path, monkeypatch):
    conn, cid, _chat, deliver, vk = told(tmp_path)

    def refuse(*a, **k):
        raise orders.OrderError("нельзя")
    monkeypatch.setattr(reserve.orders, "move_card", refuse)
    tick(conn, NEXT + "T09:07:00", deliver, vk)
    tick(conn, NEXT + "T10:07:00", deliver, vk)
    red = [e for e in orders.list_events(conn, cid) if e["level"] == "red" and "не перевести" in e["text"]]
    assert len(red) == 1 and "нельзя" in red[0]["text"] and code(conn, cid) == "at_point"


# ---- the review of 2026-11-02 -----------------------------------------------------------------------------------------------------

def test_an_old_reminder_never_moves_the_order(tmp_path):
    """D1: a reminder of days ago (the order forgotten at the point, or sent before the rule came) is not a reason to move it now."""
    conn, cid, _chat, deliver, vk = told(tmp_path)
    tick(conn, "2026-10-21T09:07:00", deliver, vk)
    assert code(conn, cid) == "at_point"


def test_chats_not_read_lately_move_nothing(tmp_path):
    """D2: our copy of the chat may be behind (keys, Avito blocking): no move until the chats of the account were read lately."""
    conn, cid, _chat, deliver, vk = told(tmp_path)
    read_chats(conn, NEXT + "T08:00:00")                                                # an hour before the tick: stale
    tick(conn, NEXT + "T09:07:00", deliver, vk)
    assert code(conn, cid) == "at_point"
    read_chats(conn, NEXT + "T09:59:00")
    tick(conn, NEXT + "T10:07:00", deliver, vk)
    assert code(conn, cid) == "oth"


def test_an_answer_in_another_chat_of_the_order_or_only_in_the_chats_preview_keeps_it(tmp_path):
    conn, cid, chat, deliver, vk = told(tmp_path)
    account = conn.execute("SELECT account_id FROM avito_chat WHERE id = ?", (chat,)).fetchone()[0]
    other = conn.execute("INSERT INTO avito_chat (account_id, chat_id, order_id, last_at, synced_at) VALUES (?, 'c-other', ?, ?, ?)",
                         (account, cid, DAY, "2099-01-01T00:00:00")).lastrowid
    wrote(conn, other, DAY + "T18:00:00")
    tick(conn, NEXT + "T09:07:00", deliver, vk)
    assert code(conn, cid) == "at_point"
    (tmp_path / "b").mkdir()
    conn, cid, chat, deliver, vk = told(tmp_path / "b")
    conn.execute("UPDATE avito_chat SET last_dir = 'in', last_at = ? WHERE id = ?", (DAY + "T18:00:00", chat))   # the list says so, the messages not yet
    conn.commit()
    tick(conn, NEXT + "T09:07:00", deliver, vk)
    assert code(conn, cid) == "at_point"


def test_the_reminder_never_goes_to_an_order_that_left_the_point_meanwhile(tmp_path):
    """D4: the card read again after the question may be on another stage (a «Выдал» in between)."""
    conn, cid, _chat, deliver, _vk = ready(tmp_path)
    orders.move_card(conn, cid, stage(conn, "done"), DAY + "T12:31:00", "точка")
    reserve._client_step(conn, orders.get_card(conn, cid), orders.date.fromisoformat(DAY), DAY + "T12:31:00", (10, 20), deliver)
    assert deliver.sent == []


# ---- the answers of 2026-11-02 (the owner, the lead) --------------------------------------------------------------------------------

def test_the_move_is_told_to_the_point_and_to_the_owner_and_the_partner(tmp_path):
    from tires_sync import order_notify, users
    conn, cid, _chat, deliver, vk = told(tmp_path)
    users.add_user(conn, "zaman", "password-123", DAY + "T10:00:00", role="partner")
    conn.commit()
    got = []
    deliver.at = NEXT + "T09:07:00"
    reserve.check(conn, NEXT + "T09:07:00", lambda card, text: (got.append((card.get("responsible"), bool(card.get("to_staff")), text)), "ok")[1],
                  deliver, vk)
    note = "Заказ №5200001 переведён на «На ОТХ»: клиент не ответил на напоминание о сроке резерва"
    assert [g for g in got if g[2] == note] == [("", False, note)]                        # the point: the note
    assert [g[:2] for g in got if g[2].startswith("Позвонить клиенту")] == [("owner", True)]   # the order's responsible only: «Позвонить клиенту» (2026-11-04)
    card = orders.get_card(conn, cid)
    assert order_notify.route(conn, dict(card, responsible="", to_staff=False))["channel"] == "vk"          # the point's own way
    assert order_notify.route(conn, dict(card, responsible="zaman", to_staff=True))["why"] in ("zaman", "у zaman нет чата Telegram")


def test_chats_not_read_for_a_day_after_the_window_are_one_yellow_record(tmp_path):
    conn, cid, _chat, deliver, vk = told(tmp_path)
    read_chats(conn, DAY + "T13:00:00")                                                 # read last on the day of the reminder
    tick(conn, NEXT + "T09:07:00", deliver, vk)                                         # 20 h after the reminder: not yet a day after its window
    assert not [e for e in orders.list_events(conn, cid) if "не читаются" in e["text"]]
    tick(conn, "2026-10-20T09:07:00", deliver, vk)
    tick(conn, "2026-10-20T10:07:00", deliver, vk)
    [stale] = [e for e in orders.list_events(conn, cid) if "не читаются" in e["text"]]
    assert stale["level"] == "yellow" and code(conn, cid) == "at_point"
