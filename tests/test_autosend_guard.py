"""«Автосообщение клиенту» is the office's (order_autosend; ветка lane/autosend-guard, ревью autotask-date): a manager of a point never sees it, and a
request past the screen is refused on the server too — 403 and nothing changes: «Отправить снова» of its lost send, a send of the point under its
mark (reply_token, image_token), «Отправил вручную» of its «Не дошло клиенту», the task «Автосообщение клиенту не ушло», set / change / cancel.
The office does as before; the point's own messages in its chats go as before. A fake Avito only: nothing goes out."""
from __future__ import annotations

import io

import pytest

from test_chats_pages import _sendable_page, post
from test_mypoint import manager
from tires_sync import order_autosend as auto, tasks, unsent

SENT = (200, {"id": "m-1", "created": 1_791_900_800})
LOOK = (200, {"messages": []})
AT = "2026-10-13T08:00:00"


@pytest.fixture
def env(tmp_path):
    """The office's client, the point's manager «gamma» whose point has the chat's account, the chat's order of that point."""
    app, office, tok, conn, chat = _sendable_page(tmp_path, LOOK, SENT, LOOK, SENT)
    card = chat["order_id"]
    pid = conn.execute("INSERT INTO point (name, avito_account_id) VALUES ('Альфа', ?)", (chat["account_id"],)).lastrowid
    conn.execute("UPDATE order_card SET point_id = ? WHERE id = ?", (pid, card))
    conn.commit()
    point = manager(app, conn, pid)
    return dict(app=app, office=office, tok=tok, conn=conn, row=chat["id"], card=card, point=point)


def calls(env):
    return len(env["app"].config["AVITO_HTTP"].calls)


def lost(env, aid=1, text="Договорились на завтра"):
    token = auto.TOKEN % (env["card"], aid)
    env["conn"].execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, text) VALUES (?, ?, ?, 'система', 'lost', ?)",
                        (token, env["row"], AT, text))
    env["conn"].commit()
    return token


def status(env, token):
    row = env["conn"].execute("SELECT status FROM chat_send_log WHERE token = ?", (token,)).fetchone()
    return row[0] if row else None


# ---- «Отправить снова» -------------------------------------------------------------------------------------------------------------------

def test_the_point_cannot_send_again_a_lost_auto_message(env):
    token = lost(env)
    before = calls(env)
    page = env["point"].get("/chats/%d" % env["row"]).get_data(as_text=True)
    assert token not in page                                                         # the screen hides it (as before)
    r = post(env["point"], "/chats/%d/send" % env["row"], "tok", again=token)
    assert r.status_code == 403
    assert status(env, token) == "lost" and calls(env) == before                      # nothing went to Avito
    r = post(env["point"], "/chats/%d/send" % env["row"], "tok", again=token, back="/orders/%d" % env["card"])   # from the card's form too
    assert r.status_code == 403 and status(env, token) == "lost"


def test_the_office_still_sends_it_again(env):
    token = lost(env)
    r = post(env["office"], "/chats/%d/send" % env["row"], env["tok"], again=token)
    assert r.status_code == 302 and status(env, token) == "sent"


def test_the_point_still_sends_again_its_own_lost_message(env):
    env["conn"].execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, text) VALUES ('lost-own', ?, ?, 'gamma', 'lost', 'Можно забирать')",
                        (env["row"], AT))
    env["conn"].commit()
    r = post(env["point"], "/chats/%d/send" % env["row"], "tok", again="lost-own")
    assert r.status_code == 302 and status(env, "lost-own") == "sent"


# ---- a send of the point under the auto message's mark -------------------------------------------------------------------------------------

def test_the_point_cannot_take_the_mark_of_a_pending_auto_message(env):
    aid = auto.add(env["conn"], env["card"], "deal", "2026-11-11T15:00", "Договорились на завтра", "2026-11-10T10:00:00", "worker2")
    token = auto.TOKEN % (env["card"], aid)
    before = calls(env)
    r = post(env["point"], "/chats/%d/send" % env["row"], "tok", text="Свой текст", reply_token=token)
    assert r.status_code == 403
    assert status(env, token) is None and calls(env) == before and auto.get(env["conn"], aid)["status"] == "pending"
    photo = (io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 64), "a.png")
    for path in ("/chats/%d/send", "/chats/%d/send-image"):
        r = env["point"].post(path % env["row"], data={"csrf": "tok", "image_token": token, "photo": photo}, content_type="multipart/form-data")
        assert r.status_code == 403, path
        photo = (io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 64), "a.png")
    assert status(env, token) is None and calls(env) == before


def test_the_point_s_own_message_goes_as_before(env):
    r = post(env["point"], "/chats/%d/send" % env["row"], "tok", text="Шины на точке, ждём вас", reply_token="point-own-reply-token")
    assert r.status_code == 302 and status(env, "point-own-reply-token") == "sent"


# ---- «Отправил вручную» of «Не дошло клиенту» ----------------------------------------------------------------------------------------------

def test_the_point_cannot_mark_an_auto_message_sent_by_hand(env):
    conn, card = env["conn"], env["card"]
    unsent.record(conn, card, auto.TOKEN % (card, 1), AT, "Авито не принял сообщение (код 402)", "Договорились на завтра")
    unsent.record(conn, card, "t-own", AT, "Авито не принял сообщение (код 402)", "Можно забирать")
    conn.commit()
    auto_row, own_row = [r[0] for r in conn.execute("SELECT id FROM order_unsent ORDER BY id")]
    r = post(env["point"], "/orders/%d/unsent/send/%d" % (card, auto_row), "tok")
    assert r.status_code == 403
    assert conn.execute("SELECT closed_at FROM order_unsent WHERE id = ?", (auto_row,)).fetchone()[0] is None
    assert post(env["point"], "/orders/%d/unsent/send/%d" % (card, own_row), "tok").status_code == 302       # its own: as before
    assert conn.execute("SELECT closed_by FROM order_unsent WHERE id = ?", (own_row,)).fetchone()[0] == "gamma"
    assert post(env["office"], "/orders/%d/unsent/send/%d" % (card, auto_row), env["tok"]).status_code == 302
    assert conn.execute("SELECT closed_at FROM order_unsent WHERE id = ?", (auto_row,)).fetchone()[0] is not None


# ---- the task «Автосообщение клиенту не ушло» ---------------------------------------------------------------------------------------------

def test_the_point_cannot_close_the_task_of_an_auto_message_even_if_given_to_it(env):
    conn, card = env["conn"], env["card"]
    task = tasks.add(conn, kind="other", title="%s: заказ №%d" % (auto.TASK_TITLE, card), due_at="2026-10-13T09:00", assignee="gamma", order_id=card, note="",
                     author="o", now=AT)
    conn.commit()
    for how in ("done", "cancel"):
        assert post(env["point"], "/tasks/%d/%s" % (task, how), "tok", result="отправил").status_code == 403
    assert tasks.get(conn, task)["done_at"] is None and tasks.get(conn, task)["cancelled_at"] is None
    assert post(env["office"], "/tasks/%d/done" % task, env["tok"], result="отправил").status_code == 302
    assert tasks.get(conn, task)["done_at"] is not None


# ---- set, change, cancel ------------------------------------------------------------------------------------------------------------------

def test_the_point_cannot_set_change_or_cancel_one(env):
    conn, card = env["conn"], env["card"]
    aid = auto.add(conn, card, "deal", "2026-11-11T15:00", "Договорились на завтра", "2026-11-10T10:00:00", "worker2")
    base = "/orders/%d/autosend" % card
    assert post(env["point"], base, "tok", kind="ask", due_at="2026-11-12T10:00", body="x").status_code == 403
    assert post(env["point"], "%s/%d/update" % (base, aid), "tok", due_at="2026-11-12T10:00", body="Чужой текст").status_code == 403
    assert post(env["point"], "%s/%d/cancel" % (base, aid), "tok").status_code == 403
    [item] = auto.of_order(conn, card)
    assert (item["status"], item["text"], item["due_at"]) == ("pending", "Договорились на завтра", "2026-11-11T15:00")
