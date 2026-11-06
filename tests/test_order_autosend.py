"""«Автосообщение клиенту» (order_autosend, the owner 2026-11-05/06): the manager sets a date, a time and a text in the order or its chat; at
that time the system writes it in the order's Avito chat by itself. «Спросить, решил ли брать» is cancelled by an answer of the client after it
was set or by a closed order; «Договорённость» by a closed order only. One send mark per message: never a second one; what did not go (a
refusal, a 402, a busy base) is a task of the responsible; a manager of a point never sees any of it. A fake Avito only: nothing goes out."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from test_chat_send_refusal import Refusing
from test_chats_sync import FakeClient, chat, db, msg
from test_send_settle import refused402
from test_stuck_sending import Sender
from tires_sync import chats, order_alerts, order_autosend as auto, order_templates as tpl, orders, store, tasks

SET = "2026-11-10T10:00:00"
DUE = "2026-11-11T15:00"
BEFORE = "2026-11-11T14:59:00"
AT = "2026-11-11T15:00:20"
LATER = "2026-11-11T15:01:30"


@pytest.fixture
def env(tmp_path):
    conn, account = db(tmp_path)
    orders.ensure_pipeline(conn, SET)
    conn.execute("UPDATE account SET chats_enabled = 1, chats_send = 1 WHERE id = ?", (account,))
    conn.commit()
    chats.sync_account(conn, FakeClient([[chat("c1", text="Здравствуйте")]], {"c1": [msg("m1", "Здравствуйте", read=1)]}), account, SET)
    row = chats.list_chats(conn)[0]["id"]
    card = orders.create_card(conn, {"client_name": "Иван"}, SET, "worker2")
    conn.execute("UPDATE avito_chat SET order_id = ? WHERE id = ?", (card, row))
    conn.commit()
    return conn, row, card


def stage(conn, code):
    return next(s["id"] for s in orders.list_stages(conn) if s["code"] == code)


def deliver_by(conn, sender, now):
    return lambda row, text, token: chats.send_reply(conn, sender, row, text, token, now, "система")


def journal(conn, card):
    return [(e["level"], e["text"]) for e in orders.list_events(conn, card) if e["kind"] == auto.EVENT]


def open_tasks(conn, card):
    return [t for t in conn.execute("SELECT title, assignee, note, done_at, cancelled_at FROM callback_task WHERE order_id = ?", (card,))]


def client_wrote(conn, row, when: str, text="Ещё думаю"):
    ts = int(datetime.fromisoformat(when).replace(tzinfo=timezone(timedelta(hours=3))).timestamp())
    conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, kind, text) VALUES (?, ?, ?, ?, 'in', 'text', ?)",
                 (row, "in-%d" % ts, when, ts, text))
    conn.commit()


@pytest.mark.parametrize("kind", ("ask", "deal"))
def test_it_goes_at_its_time_once_with_the_journal_of_who_when_and_kind(env, kind):
    conn, _row, card = env
    sender = Sender()
    aid = auto.add(conn, card, kind, DUE, "Добрый день, как договаривались", SET, "worker2")
    assert auto.run(conn, BEFORE, deliver_by(conn, sender, BEFORE))["sent"] == 0 and sender.sent == []
    assert auto.run(conn, AT, deliver_by(conn, sender, AT))["sent"] == 1
    assert sender.sent == ["Добрый день, как договаривались"]
    auto.run(conn, LATER, deliver_by(conn, sender, LATER))
    auto.run(conn, LATER, deliver_by(conn, sender, LATER))                    # two runs: still one message
    assert len(sender.sent) == 1 and auto.get(conn, aid)["status"] == "sent"
    lines = journal(conn, card)
    assert lines[-1][0] == "grey" and "поставлено: «%s» на 11.11.2026 15:00" % auto.KINDS[kind] in lines[-1][1]
    assert lines[0][0] == "green" and "отправлено («%s» на 11.11.2026 15:00, поставил(а) worker2)" % auto.KINDS[kind] in lines[0][1]
    assert conn.execute("SELECT status FROM chat_send_log WHERE token = ?", (auto.TOKEN % (card, aid),)).fetchone()[0] == "sent"


def test_ask_is_cancelled_by_an_answer_of_the_client_after_it_was_set(env):
    conn, row, card = env
    aid = auto.add(conn, card, "ask", DUE, "Решили брать?", SET, "worker2")
    client_wrote(conn, row, "2026-11-10T09:00:00")                            # before it was set: no answer to it
    auto.run(conn, "2026-11-10T12:00:00", deliver_by(conn, Sender(), "2026-11-10T12:00:00"))
    assert auto.get(conn, aid)["status"] == "pending"
    client_wrote(conn, row, "2026-11-10T18:00:00")
    sender = Sender()
    assert auto.run(conn, AT, deliver_by(conn, sender, AT))["cancelled"] == 1
    item = auto.get(conn, aid)
    assert sender.sent == [] and item["status"] == "cancelled" and item["reason"] == "клиент ответил"
    assert any("отменено" in t and "клиент ответил" in t for _l, t in journal(conn, card))


def test_deal_is_not_cancelled_by_an_answer(env):
    conn, row, card = env
    aid = auto.add(conn, card, "deal", DUE, "Напоминаем о договорённости", SET, "worker2")
    client_wrote(conn, row, "2026-11-10T18:00:00")
    sender = Sender()
    auto.run(conn, AT, deliver_by(conn, sender, AT))
    assert sender.sent == ["Напоминаем о договорённости"] and auto.get(conn, aid)["status"] == "sent"


@pytest.mark.parametrize("kind", ("ask", "deal"))
@pytest.mark.parametrize("code", ("cancel", "done"))
def test_a_closed_order_cancels_both_kinds(env, kind, code):
    conn, _row, card = env
    aid = auto.add(conn, card, kind, DUE, "Текст", SET, "worker2")
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, code), card))      # closed, whatever the way
    conn.commit()
    sender = Sender()
    auto.run(conn, AT, deliver_by(conn, sender, AT))
    item = auto.get(conn, aid)
    assert sender.sent == [] and item["status"] == "cancelled" and item["reason"].startswith("заказ закрыт")
    with pytest.raises(auto.AutosendError):
        auto.add(conn, card, kind, DUE, "Текст", SET, "worker2")               # nor can a new one be set on it


def test_a_run_stopped_after_the_send_never_sends_it_again(env):
    conn, _row, card = env
    sender = Sender()
    aid = auto.add(conn, card, "deal", DUE, "Текст", SET, "worker2")
    deliver = deliver_by(conn, sender, AT)

    def then_die(row, text, token):
        deliver(row, text, token)
        raise KeyboardInterrupt                                              # the process killed right after Avito took it
    with pytest.raises(KeyboardInterrupt):
        auto.run(conn, AT, then_die)
    conn.rollback()
    assert auto.get(conn, aid)["status"] == "sending"
    auto.run(conn, "2026-11-11T15:20:00", deliver_by(conn, sender, "2026-11-11T15:20:00"))     # the next runs settle it by its mark
    assert sender.sent == ["Текст"] and auto.get(conn, aid)["status"] == "sent"


def test_a_change_and_a_cancel_before_it_goes(env):
    conn, _row, card = env
    sender = Sender()
    aid = auto.add(conn, card, "ask", DUE, "Старый текст", SET, "worker2")
    auto.update(conn, aid, "2026-11-11T16:00", "Новый текст", "2026-11-10T11:00:00", "worker3", order_id=card)
    auto.run(conn, AT, deliver_by(conn, sender, AT))
    assert sender.sent == []                                                 # moved to 16:00
    auto.run(conn, "2026-11-11T16:00:05", deliver_by(conn, sender, "2026-11-11T16:00:05"))
    assert sender.sent == ["Новый текст"]
    with pytest.raises(auto.AutosendError):
        auto.update(conn, aid, "2026-11-12T16:00", "Ещё", "2026-11-11T17:00:00", "worker3")
    with pytest.raises(auto.AutosendError):
        auto.cancel(conn, aid, "2026-11-11T17:00:00", "worker3")
    other = auto.add(conn, card, "deal", "2026-11-12T10:00", "Не надо", "2026-11-11T17:00:00", "worker2")
    auto.cancel(conn, other, "2026-11-11T17:01:00", "worker3", order_id=card)
    auto.run(conn, "2026-11-12T10:00:30", deliver_by(conn, sender, "2026-11-12T10:00:30"))
    assert sender.sent == ["Новый текст"] and auto.get(conn, other)["reason"] == "отменено вручную"
    assert any("изменено" in t and "Новый текст" in t for _l, t in journal(conn, card))


def test_the_form_is_checked(env):
    conn, _row, card = env
    for kind, due, text in (("x", DUE, "т"), ("ask", "2026-11-10T09:00", "т"), ("ask", "завтра", "т"), ("ask", DUE, " "),
                            ("ask", DUE, "Заказ [Номер заказа WebMIM]")):
        with pytest.raises(auto.AutosendError):
            auto.add(conn, card, kind, due, text, SET, "worker2")
    assert auto.default_text(conn, "ask") == tpl.AUTOSEND_ASK_SAMPLE[2] and auto.default_text(conn, "deal") == tpl.AUTOSEND_DEAL_SAMPLE[2]


def test_a_failed_send_is_a_task_of_the_responsible_and_never_goes_again(env):
    conn, _row, card = env
    aid = auto.add(conn, card, "deal", DUE, "Текст", SET, "worker2")

    def refuse(row, text, token):
        raise chats.ChatError("Авито отказал")
    assert auto.run(conn, AT, refuse)["failed"] == 1
    item = auto.get(conn, aid)
    assert item["status"] == "failed" and "Авито отказал" in item["reason"]
    [task] = open_tasks(conn, card)
    assert task[0] == "%s: заказ №%d" % (auto.TASK_TITLE, card) and "Текст" in task[2] and task[3] is None
    assert task[1] == "owner"                                                # nobody of the office answers for it: the owner
    assert any(level == "red" and "не отправлено" in t for level, t in journal(conn, card))
    auto.run(conn, LATER, lambda *a: pytest.fail("never again"))
    assert len(open_tasks(conn, card)) == 1


def test_a_402_is_a_task_the_sync_retries_once_and_a_delivery_closes_it(env):
    conn, row, card = env
    sender = Refusing(refused402())
    aid = auto.add(conn, card, "deal", DUE, "Текст", SET, "worker2")
    assert auto.run(conn, AT, deliver_by(conn, sender, AT))["waiting"] == 1
    item = auto.get(conn, aid)
    assert item["status"] == "waiting" and "402" in item["reason"]
    [task] = open_tasks(conn, card)
    assert "402" in task[2] and task[3] is None
    assert any(level == "yellow" and "402" in t for level, t in journal(conn, card))
    auto.run(conn, "2026-11-11T15:00:40", lambda *a: pytest.fail("no send of ours around the mark"))
    assert chats.settle_sends(conn, sender, lambda: sender, 1, "2026-11-11T15:02:00") == {"sent": 1}
    auto.run(conn, "2026-11-11T15:02:30", lambda *a: pytest.fail("no second one"))
    assert auto.get(conn, aid)["status"] == "sent" and sender.sent == ["Текст"]
    assert open_tasks(conn, card)[0][3] is not None                          # the task closed: it went by itself


def test_a_402_of_ask_is_not_retried_after_the_client_answered(env):
    conn, row, card = env
    sender = Refusing(refused402())
    aid = auto.add(conn, card, "ask", DUE, "Решили?", SET, "worker2")
    auto.run(conn, AT, deliver_by(conn, sender, AT))
    client_wrote(conn, row, "2026-11-11T15:01:00")
    assert chats.settle_sends(conn, sender, lambda: sender, 1, "2026-11-11T15:02:00") != {"sent": 1}
    assert sender.sent == []
    auto.run(conn, "2026-11-11T15:02:30", lambda *a: pytest.fail("no send"))
    item = auto.get(conn, aid)
    assert item["status"] == "cancelled" and "клиент ответил" in item["reason"]
    [task] = open_tasks(conn, card)
    assert task[4] is not None                                               # its task is cancelled with it: nobody writes it by hand
    halted = [e for e in orders.list_events(conn, card) if e["author"] == "система" and e["level"] == "yellow"]
    assert halted and not any(order_alerts.bell_row(e) for e in halted)     # «не отправлено повторно»: no bell, as for any send


def test_a_402_for_good_ends_failed_with_its_task(env):
    conn, _row, card = env
    sender = Refusing(*[refused402() for _ in range(10)])
    aid = auto.add(conn, card, "deal", DUE, "Текст", SET, "worker2")
    auto.run(conn, AT, deliver_by(conn, sender, AT))
    when = datetime.fromisoformat(AT)
    for minute in range(2, 40):
        chats.settle_sends(conn, sender, lambda: sender, 1, (when + timedelta(minutes=minute)).isoformat(timespec="seconds"))
    auto.run(conn, "2026-11-11T15:45:00", lambda *a: pytest.fail("never by us"))
    assert auto.get(conn, aid)["status"] == "failed" and sender.sent == [] and len(open_tasks(conn, card)) == 1


def test_a_busy_base_leaves_it_for_the_next_run_and_sends_it_once(tmp_path, env):
    conn, _row, card = env
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    aid = auto.add(conn, card, "deal", DUE, "Текст", SET, "worker2")
    quick = store.connect(path, timeout=0.05)
    holder = sqlite3.connect(path)
    holder.execute("BEGIN IMMEDIATE")                                        # another writer holds the base
    sender = Sender()
    auto.run(quick, AT, deliver_by(quick, sender, AT))
    assert sender.sent == [] and auto.get(conn, aid)["status"] == "pending"
    holder.rollback()
    auto.run(quick, LATER, deliver_by(quick, sender, LATER))
    assert sender.sent == ["Текст"] and auto.get(conn, aid)["status"] == "sent"


def test_too_late_does_not_go_and_is_a_task(env):
    conn, _row, card = env
    aid = auto.add(conn, card, "deal", DUE, "Текст", SET, "worker2")
    auto.run(conn, "2026-11-11T18:00:00", lambda *a: pytest.fail("not hours late"))
    assert auto.get(conn, aid)["status"] == "failed" and len(open_tasks(conn, card)) == 1


def test_the_sync_cycle_runs_it_after_the_chats_are_read(env, monkeypatch):
    from tires_sync import cli
    conn, _row, card = env
    auto.add(conn, card, "deal", DUE, "Текст", SET, "worker2")
    sent = []
    monkeypatch.setattr(cli, "_chat_deliver", lambda conn_, box, now: lambda row, text, token: sent.append(token))
    cli._autosend(conn, None, AT)
    assert len(sent) == 1 and sent[0].startswith("autosend-%d-" % card)


def test_a_point_sees_nothing_of_it(env):
    conn, _row, card = env
    aid = auto.add(conn, card, "deal", DUE, "Текст", SET, "worker2")
    auto.run(conn, AT, lambda *a: (_ for _ in ()).throw(chats.ChatError("нет")))
    events = orders.list_events(conn, card)
    assert [e for e in events if auto.for_office(e)] and auto.get(conn, aid)["task_id"]
    assert any(e["kind"] == "task" and auto.for_office(e) for e in events)   # the «Задачи» line too
    quiet = order_alerts._quiet(conn, "pointuser", False)
    assert all(order_alerts._hidden(dict(e, order_id=card), quiet) for e in events if auto.for_office(e))


# --- the panel --------------------------------------------------------------------------------------------------------------------------

def test_the_card_and_the_chat_set_it_and_a_point_sees_nothing(tmp_path):
    from panel_helpers import login, make_app, post
    from test_mypoint import manager
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, SET)
    alpha = conn.execute("INSERT INTO point (name) VALUES ('Альфа')").lastrowid
    conn.commit()
    card = orders.create_card(conn, {"client_name": "Иван", "client_phone": "+79001112233", "point_id": str(alpha)}, SET, "owner")
    conn.commit()
    html = client.get("/orders/%d" % card).get_data(as_text=True)
    assert 'id="autosend"' in html and "Поставить: Спросить, решил ли брать" in html and tpl.AUTOSEND_DEAL_SAMPLE[2] in html
    due = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    r = post(client, "/orders/%d/autosend" % card, tok, kind="deal", due_at=due, text="Договорились на завтра")
    assert r.status_code == 302
    [item] = auto.of_order(conn, card)
    assert item["kind"] == "deal" and item["text"] == "Договорились на завтра" and item["created_by"] == app.config["AUTHOR"]
    assert "Договорились на завтра" in client.get("/orders/%d" % card).get_data(as_text=True)
    point = manager(app, conn, alpha)
    seen = point.get("/orders/%d" % card)
    assert seen.status_code == 200
    page = seen.get_data(as_text=True)
    assert 'id="autosend"' not in page and "Договорились на завтра" not in page and "Автосообщение" not in page
    assert post(point, "/orders/%d/autosend" % card, "tok", kind="deal", due_at=due, text="x").status_code == 403
    assert post(point, "/orders/%d/autosend/%d/cancel" % (card, item["id"]), "tok").status_code == 403
    assert auto.get(conn, item["id"])["status"] == "pending"
    r = post(client, "/orders/%d/autosend/%d/cancel" % (card, item["id"]), tok)
    assert r.status_code == 302 and auto.get(conn, item["id"])["status"] == "cancelled"
    from tires_sync import connections
    account = connections.create_account(conn, "Основной", SET, "o")
    conn.execute("UPDATE account SET chats_enabled = 1, chats_send = 1 WHERE id = ?", (account,))
    row = conn.execute("INSERT INTO avito_chat (account_id, chat_id, order_id, last_at) VALUES (?, 'c-9', ?, ?)", (account, card, SET)).lastrowid
    conn.commit()
    chat_page = client.get("/chats/%d" % row).get_data(as_text=True)
    assert 'id="autosend"' in chat_page and 'name="chat" value="%d"' % row in chat_page
    r = post(client, "/orders/%d/autosend" % card, tok, kind="ask", due_at=due, text="Из чата", chat=str(row))
    assert r.status_code == 302 and r.headers["Location"].endswith("/chats/%d#autosend" % row)
    assert [i["text"] for i in auto.of_order(conn, card) if i["status"] == "pending"] == ["Из чата"]


# --- the review of 2026-11-06 ----------------------------------------------------------------------------------------------------------

def point_visible(conn, card):
    """The records of the order's journal a manager of a point is shown (the card's filter, panel/routes_orders.card_page)."""
    return [e["text"] for e in orders.list_events(conn, card) if not auto.for_office(e)]


def test_a_send_writes_one_green_record_and_none_a_point_sees(env):
    conn, _row, card = env
    before = point_visible(conn, card)
    auto.add(conn, card, "deal", DUE, "Текст", SET, "worker2")
    auto.run(conn, AT, deliver_by(conn, Sender(), AT))
    greens = [e for e in orders.list_events(conn, card) if e["level"] == "green" and e["kind"] in ("chat", auto.EVENT)]
    assert len(greens) == 1 and greens[0]["kind"] == auto.EVENT
    assert point_visible(conn, card) == before


def test_through_a_402_and_its_retry_a_point_sees_nothing_either(env):
    conn, _row, card = env
    before = point_visible(conn, card)
    sender = Refusing(refused402())
    auto.add(conn, card, "deal", DUE, "Текст", SET, "worker2")
    auto.run(conn, AT, deliver_by(conn, sender, AT))
    chats.settle_sends(conn, sender, lambda: sender, 1, "2026-11-11T15:02:00")
    auto.run(conn, "2026-11-11T15:02:30", lambda *a: pytest.fail("no second one"))
    assert point_visible(conn, card) == before
    reds = [e for e in orders.list_events(conn, card) if e["level"] == "red"]
    assert all(e["kind"] == auto.EVENT for e in reds)                        # the office still has the send's own red record of the 402


def test_a_lost_one_ends_failed_and_never_goes_by_itself(env):
    conn, _row, card = env
    aid = auto.add(conn, card, "deal", DUE, "Текст", SET, "worker2")
    auto.run(conn, AT, lambda row, text, token: (_ for _ in ()).throw(chats.ChatSendUnknown("таймаут")))
    assert auto.get(conn, aid)["status"] == "waiting"
    conn.execute("INSERT OR REPLACE INTO chat_send_log (token, chat_row, at, author, status) VALUES (?, 1, ?, 'система', 'lost')",
                 (auto.TOKEN % (card, aid), AT))                              # the sync did not find it in the chat
    conn.commit()
    auto.run(conn, LATER, lambda *a: pytest.fail("never by itself"))
    item = auto.get(conn, aid)
    assert item["status"] == "failed" and "Отправить снова" in item["reason"] and len(open_tasks(conn, card)) == 1


def test_a_stopped_run_after_a_refusal_does_not_try_again(env):
    conn, _row, card = env
    aid = auto.add(conn, card, "deal", DUE, "Текст", SET, "worker2")
    conn.execute("UPDATE order_autosend SET status = 'sending', updated_at = ? WHERE id = ?", (AT, aid))      # the run died after the refusal
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, error) VALUES (?, 1, ?, 'система', 'failed', 'HTTP 400')",
                 (auto.TOKEN % (card, aid), AT))
    conn.commit()
    auto.run(conn, "2026-11-11T15:30:00", lambda *a: pytest.fail("no new try"))
    assert auto.get(conn, aid)["status"] == "failed"


def test_it_goes_and_comes_back_with_the_order(env):
    from tires_sync import trash
    conn, _row, card = env
    aid = auto.add(conn, card, "deal", DUE, "Текст", SET, "worker2")
    record = trash.delete_order(conn, card, "2026-11-10T11:00:00", "owner")
    auto.run(conn, "2026-11-10T11:01:00", lambda *a: pytest.fail("no order"))
    trash.restore_order(conn, record, "2026-11-10T12:00:00", "owner")
    assert auto.get(conn, aid)["status"] == "pending"


# --- the second review of 2026-11-06 ---------------------------------------------------------------------------------------------------

def test_a_lost_one_sent_again_by_a_press_is_sent_with_its_task_closed(env):
    conn, row, card = env
    aid = auto.add(conn, card, "deal", DUE, "Текст", SET, "worker2")
    auto.run(conn, AT, lambda r, text, token: (_ for _ in ()).throw(chats.ChatSendUnknown("таймаут")))
    token = auto.TOKEN % (card, aid)
    conn.execute("INSERT OR REPLACE INTO chat_send_log (token, chat_row, at, author, status, text) VALUES (?, ?, ?, 'система', 'lost', 'Текст')",
                 (token, row, AT))
    conn.commit()
    auto.run(conn, LATER, lambda *a: pytest.fail("never by itself"))
    assert auto.get(conn, aid)["status"] == "failed" and "Отправить снова" in open_tasks(conn, card)[0][2]   # the task says what to do now
    sender = Sender()
    chats.send_reply(conn, sender, row, "Текст", token, "2026-11-11T15:10:00", "worker2", again=True)        # «Отправить снова» in the chat
    auto.run(conn, "2026-11-11T15:11:00", lambda *a: pytest.fail("never by itself"))
    assert sender.sent == ["Текст"] and auto.get(conn, aid)["status"] == "sent"
    assert open_tasks(conn, card)[0][3] is not None
    greens = [e for e in orders.list_events(conn, card) if e["level"] == "green" and e["kind"] in ("chat", auto.EVENT)]
    assert len(greens) == 1


def test_through_a_402_one_green_record_and_the_bell_hears_only_the_modules(env):
    conn, _row, card = env
    sender = Refusing(refused402())
    auto.add(conn, card, "deal", DUE, "Текст", SET, "worker2")
    auto.run(conn, AT, deliver_by(conn, sender, AT))
    chats.settle_sends(conn, sender, lambda: sender, 1, "2026-11-11T15:02:00")
    auto.run(conn, "2026-11-11T15:02:30", lambda *a: pytest.fail("no second one"))
    events = orders.list_events(conn, card)
    greens = [e for e in events if e["level"] == "green" and e["kind"] in ("chat", auto.EVENT)]
    assert len(greens) == 1 and greens[0]["author"] == auto.AUTHOR
    rung = [e for e in events if e["kind"] == auto.EVENT and order_alerts.bell_row(e)]
    assert rung and all(e["author"] != "система" or e["level"] == "red" for e in rung)    # the send mechanism's records: as for any send


def test_the_unsettled_line_above_the_chat_is_not_a_points(env):
    conn, row, card = env
    sender = Refusing(refused402())
    aid = auto.add(conn, card, "deal", DUE, "Текст", SET, "worker2")
    auto.run(conn, AT, deliver_by(conn, sender, AT))
    sends = chats.pending_sends(conn, [row], "2026-11-11T15:00:30")
    assert [s["token"] for s in auto.office_sends(sends, False)] == [auto.TOKEN % (card, aid)]
    assert auto.office_sends(sends, True) == []
