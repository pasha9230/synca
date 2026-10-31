"""«Не дошло клиенту» in the order card (the owner 2026-10-31; unsent.py, panel/routes_unsent.py): a message Avito refused for good (a 402
after the retries, another refusal of a press) is kept with its whole text and the reason; a message the panel cannot send because the
order has no chat of Avito is shown with its text too; «Скопировать» takes that text; «Отправил вручную» closes it once with a line in the
order's journal and the audit; a send that went after all is never shown; one token is one row; a failure to keep it never touches the
send; a point sees and marks only its own orders'; the rows go and come back with the order and a merged order shows its sources'."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import pytest

from panel_helpers import login, make_app, post
from test_chat_send_refusal import Refusing, refusal
from test_stuck_sending import NOW, Sender, env, journal, status  # noqa: F401  (env is a fixture)
from tires_sync import chats, order_engine as engine, orders, store, unsent, users


def at(seconds: int) -> str:
    return (datetime.fromisoformat(NOW) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def refused402():
    return refusal(402, {"error": {"code": 402, "message": "Перейдите на подписку с API мессенджера"}})


def settle(conn, sender, when, account=1):
    return chats.settle_sends(conn, sender, lambda: sender, account, when)


def exhaust(conn, chat_row, text="Добрый день, шины на точке", token="tok-1"):
    """A 402 and three more by the sync: «не отправлено» for good."""
    sender = Refusing(*[refused402() for _ in range(4)])
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, text, token, NOW, "owner")
    for step in (61, 122, 183):
        settle(conn, sender, at(step))
    assert status(conn, token)[0] == "failed"


def test_a_402_after_the_retries_is_kept_with_its_text_and_shown_once(env):
    conn, chat_row, card = env
    exhaust(conn, chat_row)
    [row] = unsent.of_order(conn, card)
    assert row["kind"] == "send" and row["text"] == "Добрый день, шины на точке" and "4 раза подряд" in row["why"]
    assert conn.execute("SELECT COUNT(*) FROM order_unsent").fetchone()[0] == 1                       # one token, one row
    unsent.record(conn, card, "tok-1", at(500), "ещё раз", "другой текст")                             # the same send failed again: the row
    conn.commit()                                                                                    # keeps its last text and reason
    assert [(r["text"], r["why"]) for r in unsent.of_order(conn, card)] == [("другой текст", "ещё раз")]
    assert conn.execute("SELECT COUNT(*) FROM order_unsent").fetchone()[0] == 1


def test_a_refusal_of_a_press_is_kept_too(env):
    conn, chat_row, card = env
    with pytest.raises(chats.ChatError):
        chats.send_reply(conn, Refusing(refusal(403, {"error": {"code": 403, "message": "Forbidden"}})), chat_row, "Ваш заказ готов", "tok-1", NOW, "owner")
    [row] = unsent.of_order(conn, card)
    assert row["text"] == "Ваш заказ готов" and "403" in row["why"]


def test_a_send_that_went_after_all_is_never_shown(env):
    conn, chat_row, card = env
    exhaust(conn, chat_row)
    conn.execute("UPDATE chat_send_log SET status = 'sent' WHERE token = 'tok-1'")                 # «Отправить снова» delivered it later
    conn.commit()
    assert unsent.of_order(conn, card) == []


def test_a_402_still_retried_or_a_photo_is_no_row(env):
    conn, chat_row, card = env
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, Refusing(refused402()), chat_row, "Ждём", "tok-1", NOW, "owner")
    assert unsent.of_order(conn, card) == []                                                         # the sync sends it again by itself


def test_a_failure_to_keep_it_never_touches_the_send(env, monkeypatch):
    conn, chat_row, card = env

    def broken(*args, **kwargs):
        raise RuntimeError("database is locked")
    real = conn.execute

    class Conn:                                                                                      # the insert into order_unsent fails
        def __getattr__(self, name):
            return getattr(conn, name)

        def execute(self, sql, *args):
            if "order_unsent" in sql:
                raise RuntimeError("database is locked")
            return real(sql, *args)
    sender = Refusing(*[refused402() for _ in range(4)])
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    for step in (61, 122):
        settle(conn, sender, at(step))
    assert settle(Conn(), sender, at(183)) == {"failed": 1}                                          # the end is as before
    assert status(conn)[0] == "failed" and conn.execute("SELECT COUNT(*) FROM manager_message").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM order_unsent").fetchone()[0] == 0
    assert any("4 раза подряд" in line for line in journal(conn, card))


@pytest.fixture
def arrived(tmp_path, monkeypatch):
    """An order at the point without a chat of Avito (tests/test_arrived_wait.py)."""
    from test_chats_sync import db
    from tires_sync import order_manager as mgr
    monkeypatch.setattr(mgr.templates, "render", lambda *a: ("Заказ 5000777 приехал, можно забирать", []))
    conn, account = db(tmp_path)
    orders.ensure_pipeline(conn, NOW)
    card = orders.create_card(conn, dict(client_name="Татьяна", webmim_number="5000777"), NOW, "o")
    at_point = conn.execute("SELECT id FROM order_stage WHERE code = 'at_point'").fetchone()[0]
    conn.execute("UPDATE order_card SET stage_id = ?, entered_at = ? WHERE id = ?", (at_point, NOW, card))
    conn.commit()
    return conn, card


def test_an_order_without_a_chat_shows_the_waiting_message_with_its_text(arrived):
    from tires_sync import order_manager as mgr
    conn, card = arrived
    sent, _note = mgr.tell_client_arrived(conn, card, None, NOW, "точка")
    assert not sent
    [row] = unsent.of_order(conn, card)
    assert row["kind"] == "task" and "нет чата Авито" in row["why"] and row["text"] == "Заказ 5000777 приехал, можно забирать"
    assert conn.execute("SELECT COUNT(*) FROM order_unsent").fetchone()[0] == 0                      # one place, never twice
    unsent.sent_by_hand(conn, card, "task", row["id"], at(60), "owner")
    assert unsent.of_order(conn, card) == [] and engine.get_task(conn, row["id"])["status"] == "done"
    sent, _note = mgr.tell_client_arrived(conn, card, None, at(120), "точка")                         # «приехал» again: never back
    assert unsent.of_order(conn, card) == []


# ---- the card: the block, «Скопировать», «Отправил вручную», rights ------------------------------------------------------------------

def panel(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    alpha = conn.execute("INSERT INTO point (name) VALUES ('Альфа')").lastrowid
    beta = conn.execute("INSERT INTO point (name) VALUES ('Бета')").lastrowid
    conn.commit()
    return app, client, tok, conn, alpha, beta


def card_with_unsent(conn, point_id, phone, token, text="Можно забирать заказ"):
    card = orders.create_card(conn, {"client_name": "Иван", "client_phone": phone, "point_id": str(point_id)}, NOW, "owner")
    conn.commit()
    unsent.record(conn, card, token, NOW, "Авито не принял сообщение (код 402)", text)
    conn.commit()
    return card


def test_the_card_shows_the_block_and_by_hand_closes_it_once(tmp_path):
    app, client, tok, conn, alpha, beta = panel(tmp_path)
    card = card_with_unsent(conn, alpha, "+79001112233", "t-1")
    html = client.get("/orders/%d" % card).get_data(as_text=True)
    block = html[html.index('class="order-unsent'):].split("</section>")[0]
    [row_id] = [r[0] for r in conn.execute("SELECT id FROM order_unsent")]
    assert "Не дошло клиенту" in block and "Можно забирать заказ" in block and "код 402" in block
    assert 'data-copy="#unsent-send-%d"' % row_id in block and 'id="unsent-send-%d"' % row_id in block and "readonly" in block
    assert html.index("order-unsent") < html.index("ocard-layout")                                  # at the top of the card
    r = post(client, "/orders/%d/unsent/send/%d" % (card, row_id), tok)
    assert r.status_code == 302 and "order-unsent" not in client.get("/orders/%d" % card).get_data(as_text=True)
    assert conn.execute("SELECT closed_by FROM order_unsent").fetchone()[0] == app.config["AUTHOR"]
    assert any("отправлено вручную" in t for t in journal(conn, card))
    assert json.loads(conn.execute("SELECT detail FROM audit WHERE action = 'unsent_by_hand'").fetchone()[0])["order"] == card
    post(client, "/orders/%d/unsent/send/%d" % (card, row_id), tok)                                  # a second press: nothing more
    assert sum("отправлено вручную" in t for t in journal(conn, card)) == 1
    assert unsent.of_order(conn, card) == []                                                         # marked: gone
    unsent.record(conn, card, "t-1", at(60), "снова отказ", "Можно забирать заказ")                   # the same send pressed and refused again:
    conn.commit()                                                                                    # back with its last reason (never lost)
    assert [r["why"] for r in unsent.of_order(conn, card)] == ["снова отказ"]


def test_a_point_sees_and_marks_only_its_own_orders(tmp_path):
    from test_mypoint import manager
    app, client, tok, conn, alpha, beta = panel(tmp_path)
    gamma = manager(app, conn, alpha)
    own = card_with_unsent(conn, alpha, "+79001112233", "t-own", "Своей точке")
    other = card_with_unsent(conn, beta, "+79005556677", "t-other", "Чужой точке")
    own_id, other_id = [r[0] for r in conn.execute("SELECT id FROM order_unsent ORDER BY id")]
    html = gamma.get("/orders/%d" % own).get_data(as_text=True)
    assert "Своей точке" in html and 'action="/orders/%d/unsent/send/%d"' % (own, own_id) in html
    assert gamma.get("/orders/%d" % other).status_code == 403
    assert post(gamma, "/orders/%d/unsent/send/%d" % (other, other_id), "tok").status_code == 403    # another point's order: refused
    assert post(gamma, "/orders/%d/unsent/send/%d" % (own, other_id), "tok").status_code == 302      # its own order, another's row: not closed
    assert conn.execute("SELECT closed_at FROM order_unsent WHERE id = ?", (other_id,)).fetchone()[0] is None
    assert post(gamma, "/orders/%d/unsent/send/%d" % (own, own_id), "tok").status_code == 302
    assert conn.execute("SELECT closed_by FROM order_unsent WHERE id = ?", (own_id,)).fetchone()[0] == "gamma"


def test_the_observer_sees_it_and_marks_nothing(tmp_path):
    app, client, tok, conn, alpha, beta = panel(tmp_path)
    card = card_with_unsent(conn, alpha, "+79001112233", "t-1")
    users.add_user(conn, "watcher", "пароль-для-теста-1", NOW, role="viewer")
    conn.commit()
    watcher = app.test_client()
    with watcher.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = "watcher", store.get_session_epoch(conn, "watcher"), "tok"
    html = watcher.get("/orders/%d" % card).get_data(as_text=True)
    assert "Можно забирать заказ" in html and "/unsent/send/" not in html and "Скопировать" in html
    [row_id] = [r[0] for r in conn.execute("SELECT id FROM order_unsent")]
    assert post(watcher, "/orders/%d/unsent/send/%d" % (card, row_id), "tok").status_code == 403


def test_a_task_in_error_is_marked_by_hand_and_the_window_copies_it(tmp_path):
    app, client, tok, conn, alpha, beta = panel(tmp_path)
    card = orders.create_card(conn, {"client_name": "Иван", "client_phone": "+79001112233", "point_id": str(alpha)}, NOW, "owner")
    task_id = conn.execute("INSERT INTO order_task (order_id, rule_id, entered_at, kind, arg, status, title, body, result, created_at) "
                           "VALUES (?, 0, ?, 'message', '', 'error', 'Клиенту: заказ оформлен', 'Ваш заказ оформлен', 'Авито отказал', ?)",
                           (card, NOW, NOW)).lastrowid
    conn.commit()
    engine_audience = engine.audience
    try:
        engine.audience = lambda conn_, task: "client"
        html = client.get("/orders/%d" % card).get_data(as_text=True)
        assert "Ваш заказ оформлен" in html[html.index('class="order-unsent'):].split("</section>")[0]
        assert post(client, "/orders/%d/unsent/task/%d" % (card, task_id), tok).status_code == 302
        assert engine.get_task(conn, task_id)["status"] == "done" and unsent.of_order(conn, card) == []
    finally:
        engine.audience = engine_audience


def test_the_window_of_messages_copies_a_task_in_error_too(tmp_path):
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "templates" / "_order_messages.html").read_text(encoding="utf-8")
    assert '{% if t.body %}<button type="button" class="btn btn--secondary btn--sm" data-copy="#message-{{ t.id }}">' in source
    assert re.search(r'\{% if t.status != "error" or t.body %\}<label', source)


# ---- with the order: the trash and the merge ------------------------------------------------------------------------------------------

def test_the_rows_go_and_come_back_with_the_order_and_a_merged_order_shows_its_sources(tmp_path):
    from tires_sync import order_merge, trash
    app, client, tok, conn, alpha, beta = panel(tmp_path)
    first = card_with_unsent(conn, alpha, "+79001112233", "t-1", "Первому")
    second = orders.create_card(conn, {"client_name": "Пётр", "client_phone": "+79005556677", "point_id": str(alpha)}, NOW, "owner")
    conn.commit()
    record = trash.delete_order(conn, first, NOW, "owner")
    assert conn.execute("SELECT COUNT(*) FROM order_unsent").fetchone()[0] == 0
    trash.restore_order(conn, record, NOW, "owner")
    assert [r["text"] for r in unsent.of_order(conn, first)] == ["Первому"]
    stage = conn.execute("SELECT id FROM order_stage WHERE code = 'new'").fetchone()[0]
    merged = order_merge.merge(conn, [first, second], stage, NOW, "owner", client_from=first)
    assert [r["text"] for r in unsent.of_order(conn, merged)] == ["Первому"] and unsent.of_order(conn, first) == []
    [row_id] = [r[0] for r in conn.execute("SELECT id FROM order_unsent")]
    assert "Первому" in client.get("/orders/%d" % merged).get_data(as_text=True)
    order_merge.unmerge(conn, merged, NOW, "owner")
    assert [r["text"] for r in unsent.of_order(conn, first)] == ["Первому"] and row_id


def test_by_hand_ticks_off_the_rules_message_of_that_send_and_answers_the_inbox(tmp_path):
    """The review: «Отправил вручную» of a refused send of a rule's message must leave nothing that sends the same text again — the task
    is done «отправлено вручную», the line of «Входящие» about it is answered."""
    app, client, tok, conn, alpha, beta = panel(tmp_path)
    card = orders.create_card(conn, {"client_name": "Иван", "client_phone": "+79001112233", "point_id": str(alpha)}, NOW, "owner")
    task_id = conn.execute("INSERT INTO order_task (order_id, rule_id, entered_at, kind, arg, status, title, body, result, created_at) "
                           "VALUES (?, 0, ?, 'message', '', 'pending', 'Клиенту: заказ оформлен', 'Ваш заказ оформлен', '', ?)",
                           (card, NOW, NOW)).lastrowid
    token = "task-%d-%s" % (task_id, re.sub(r"\D", "", NOW))
    conn.execute("INSERT INTO manager_message (message_id, at, point_id, sender, text, status) VALUES (?, ?, NULL, 'система', ?, 'new')",
                 ("send-%s-1" % token, NOW, "Заказ №%d: клиенту не отправлено — Отправьте вручную." % card))
    conn.commit()
    unsent.record(conn, card, token, NOW, "Авито отказал 4 раза подряд", "Ваш заказ оформлен")
    conn.commit()
    from tires_sync import order_engine
    assert order_engine.task_of_token(conn, token)["id"] == task_id
    [row] = unsent.of_order(conn, card)
    assert post(client, "/orders/%d/unsent/send/%d" % (card, row["id"]), tok).status_code == 302
    assert engine.get_task(conn, task_id)["status"] == "done" and engine.get_task(conn, task_id)["result"] == "отправлено вручную"
    assert conn.execute("SELECT status FROM manager_message").fetchone()[0] != "new"


def test_a_point_never_ticks_off_a_message_the_block_does_not_show(tmp_path):
    from test_mypoint import manager
    app, client, tok, conn, alpha, beta = panel(tmp_path)
    gamma = manager(app, conn, alpha)
    card = orders.create_card(conn, {"client_name": "Иван", "client_phone": "+79001112233", "point_id": str(alpha)}, NOW, "owner")
    task_id = conn.execute("INSERT INTO order_task (order_id, rule_id, entered_at, kind, arg, status, title, body, result, created_at) "
                           "VALUES (?, 0, ?, 'message', '', 'pending', 'Менеджеру', 'Текст менеджеру', '', ?)", (card, NOW, NOW)).lastrowid
    conn.commit()
    assert post(gamma, "/orders/%d/unsent/task/%d" % (card, task_id), "tok").status_code == 302
    assert engine.get_task(conn, task_id)["status"] == "pending"                                     # not shown in the block: not touched
