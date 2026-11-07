"""Метки системных сообщений клиенту — системные (ветка lane/token-guard, находка ревью autosend-guard): менеджер точки своим запросом
не занимает метку «можно забирать» (arrived-…), «срок резерва» (reserve-…), «появилось в наличии» (wait-…), сообщения по правилу
этапа (task-…), автосообщения (autosend-…) и формы офиса «Позиция появилась» (o<заказ>t<задача>), не жмёт по ним «Отправить снова» и
«Отправил вручную»; 403, ничего не уходит и не меняется. Одна общая проверка: chats.is_system_mark (форма метки, которую ставит система:
слово, номер и цифры). Свои сообщения точки (случайная метка страницы) и повторы по ним — как раньше; офис не меняется.
Авито поддельное: ничего не уходит наружу."""
from __future__ import annotations

import io
import secrets

import pytest

from test_autosend_guard import AT, calls, env, status  # noqa: F401  (the fixture: the office, the point «gamma», its chat and order)
from panel_helpers import post
from tires_sync import chats, order_autosend as auto, order_engine, order_manager, unsent

SYSTEM = {
    "arrived": "arrived-%d-20261013080000",
    "reserve": "reserve-%d-20261013080000",
    "wait": "wait-%d-20261013080000",
    "task": "task-%d-20261013080000",
    "autosend": "autosend-%d-7",
    "office_form": "o%dt9",
}


def marks(card):
    return {name: shape % card for name, shape in SYSTEM.items()}


def lost(env, token, text="Можно забирать заказ"):
    env["conn"].execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, text) VALUES (?, ?, ?, 'система', 'lost', ?)",
                        (token, env["row"], AT, text))
    env["conn"].commit()


def photo():
    return (io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 64), "a.png")


# ---- the one rule ------------------------------------------------------------------------------------------------------------------------

def test_the_marks_the_system_makes_are_system_marks_and_the_page_s_are_not():
    card = 41
    for name, token in marks(card).items():
        assert chats.is_system_mark(token), name
    assert chats.is_system_mark(order_manager.arrived_token({"id": card, "entered_at": "2026-10-13T08:00:00"}, AT))
    assert chats.is_system_mark(order_engine.task_token({"id": 5, "created_at": "2026-10-13T08:00:00", "rule_id": 1}))
    assert chats.is_system_mark(auto.TOKEN % (card, 3))
    for own in (secrets.token_hex(12), "lost-own", "point-own-reply-token", "t-own", "", None):     # the page's random mark; the tests' own
        assert not chats.is_system_mark(own), own


# ---- a send of the point under a system mark -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(SYSTEM))
def test_the_point_cannot_send_under_a_system_mark(env, name):
    token = marks(env["card"])[name]
    before = calls(env)
    r = post(env["point"], "/chats/%d/send" % env["row"], "tok", text="Свой текст", reply_token=token)
    assert r.status_code == 403
    for path in ("/chats/%d/send", "/chats/%d/send-image"):
        r = env["point"].post(path % env["row"], data={"csrf": "tok", "image_token": token, "photo": photo()}, content_type="multipart/form-data")
        assert r.status_code == 403, path
    assert status(env, token) is None and calls(env) == before                     # the mark stays free for the system


@pytest.mark.parametrize("name", sorted(SYSTEM))
def test_the_point_cannot_send_again_a_lost_system_message(env, name):
    token = marks(env["card"])[name]
    lost(env, token)
    before = calls(env)
    assert token not in env["point"].get("/chats/%d" % env["row"]).get_data(as_text=True)      # not offered on its screen either
    r = post(env["point"], "/chats/%d/send" % env["row"], "tok", again=token)
    assert r.status_code == 403 and status(env, token) == "lost" and calls(env) == before


def test_the_office_still_sends_a_lost_system_message_again(env):
    token = marks(env["card"])["arrived"]
    lost(env, token)
    r = post(env["office"], "/chats/%d/send" % env["row"], env["tok"], again=token)
    assert r.status_code == 302 and status(env, token) == "sent"


def test_the_point_s_own_message_goes_as_before(env):
    own = secrets.token_hex(12)                                                    # the mark the chat page gives (routes_chats)
    r = post(env["point"], "/chats/%d/send" % env["row"], "tok", text="Шины на точке, ждём вас", reply_token=own)
    assert r.status_code == 302 and status(env, own) == "sent"


def test_the_point_s_own_retry_goes_as_before(env):
    retry = secrets.token_hex(12)
    env["conn"].execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, text) VALUES (?, ?, ?, 'gamma', 'lost', 'Ждём вас')",
                        (retry, env["row"], AT))
    env["conn"].commit()
    r = post(env["point"], "/chats/%d/send" % env["row"], "tok", again=retry)
    assert r.status_code == 302 and status(env, retry) == "sent"


# ---- «Отправил вручную» of «Не дошло клиенту» ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(SYSTEM))
def test_the_point_cannot_mark_a_system_message_sent_by_hand(env, name):
    conn, card = env["conn"], env["card"]
    token = marks(card)[name]
    unsent.record(conn, card, token, AT, "Авито не принял сообщение (код 402)", "Можно забирать заказ")
    conn.commit()
    row = conn.execute("SELECT id FROM order_unsent WHERE token = ?", (token,)).fetchone()[0]
    page = env["point"].get("/orders/%d" % card).get_data(as_text=True)
    assert "/unsent/send/%d" % row not in page                                    # not offered on its card
    assert post(env["point"], "/orders/%d/unsent/send/%d" % (card, row), "tok").status_code == 403
    assert conn.execute("SELECT closed_at FROM order_unsent WHERE id = ?", (row,)).fetchone()[0] is None
    assert post(env["office"], "/orders/%d/unsent/send/%d" % (card, row), env["tok"]).status_code == 302      # the office: as before
    assert conn.execute("SELECT closed_at FROM order_unsent WHERE id = ?", (row,)).fetchone()[0] is not None


def test_the_point_still_marks_its_own_message_sent_by_hand(env):
    conn, card = env["conn"], env["card"]
    own = secrets.token_hex(12)
    unsent.record(conn, card, own, AT, "Авито не принял сообщение (код 402)", "Ждём вас")
    conn.commit()
    row = conn.execute("SELECT id FROM order_unsent WHERE token = ?", (own,)).fetchone()[0]
    assert "/unsent/send/%d" % row in env["point"].get("/orders/%d" % card).get_data(as_text=True)
    assert post(env["point"], "/orders/%d/unsent/send/%d" % (card, row), "tok").status_code == 302
    assert conn.execute("SELECT closed_by FROM order_unsent WHERE id = ?", (row,)).fetchone()[0] == "gamma"
