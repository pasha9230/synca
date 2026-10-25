"""Avito refuses a send to the client (24.10: a second account got a 402 a few times, the same text went seconds later): the person sees Avito's
own answer and what to do, the hint about the app's right stays for a 403 only, and Avito's whole answer stays in chat_send_log, the
order's journal and the service log — never a key. A 402 of the send is sent again by the minute's sync (the owner 2026-10-25,
tests/test_send_settle.py); another 4xx is «failed» (tried again by a press), anything else «unknown»."""
from __future__ import annotations

import io
import json
import logging

import pytest

from test_stuck_sending import NOW, Sender, env, journal, status  # noqa: F401  (env is a fixture)
from tires_sync import chats, order_engine as engine, order_templates as tpl, orders
from tires_sync.avito import AvitoError, _error_from

AGAIN = "Повторите через минуту; если повторяется — напишите владельцу."
SELF = "повторю сам через минуту (попытка 1 из 3)."
RIGHT = "Проверьте право приложения на отправку сообщений."
KEY = "eyJhbGciOiJIUzI1NiJ9abcdefghijklmnopqrstuvwxyz0123456789"


def refusal(status_, body):
    """The error the client raises for Avito's answer, made the way avito._request makes it."""
    raw = json.dumps(body).encode() if isinstance(body, dict) else body.encode()
    return _error_from(status_, {}, raw)


class Refusing(Sender):
    def __init__(self, *errors):
        super().__init__()
        self.errors = list(errors)

    def send_message(self, user_id, chat_id, text):
        if self.errors:
            raise self.errors.pop(0)
        return super().send_message(user_id, chat_id, text)

    def upload_image(self, user_id, name, data):
        if self.errors and getattr(self, "at_upload", False):
            raise self.errors.pop(0)
        return "img-1"

    def send_image(self, user_id, chat_id, image_id):
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append(image_id)
        return {"id": "p-out", "created": 1_791_900_700}


def picture():
    from PIL import Image
    data = io.BytesIO()
    Image.new("RGB", (10, 10)).save(data, "JPEG")
    return data.getvalue()


def error_of(conn, token="tok-1"):
    return conn.execute("SELECT error FROM chat_send_log WHERE token = ?", (token,)).fetchone()[0]


def test_a_402_shows_avitos_text_and_what_to_do_not_the_right_of_the_app(env, caplog):
    conn, chat_row, card = env
    sender = Refusing(refusal(402, {"error": {"code": 402, "message": "Лимит сообщений исчерпан"}}))
    with caplog.at_level(logging.WARNING, logger=chats.log.name):
        with pytest.raises(chats.ChatSendRetry) as raised:
            chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    said = str(raised.value)
    assert said == "Авито отказал (код 402: Лимит сообщений исчерпан), " + SELF                # 2026-10-25: the sync sends it again by itself
    assert isinstance(raised.value, chats.ChatSendUnknown) and RIGHT not in said             # to every caller: never by hand meanwhile
    assert status(conn)[0] == "retry" and error_of(conn) == "HTTP 402: Лимит сообщений исчерпан"
    assert any("Лимит сообщений исчерпан" in line and "402" in line for line in journal(conn, card))
    assert any("402" in r.getMessage() and "Лимит сообщений исчерпан" in r.getMessage() for r in caplog.records)
    assert not any("Добрый день" in r.getMessage() for r in caplog.records)                      # the text of the reply never goes to the logs
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")          # «retry»: the press does not send it, the sync does
    assert sender.sent == [] and status(conn)[0] == "retry"


def test_a_403_keeps_the_hint_about_the_right(env):
    conn, chat_row, card = env
    with pytest.raises(chats.ChatError) as raised:
        chats.send_reply(conn, Refusing(refusal(403, {"error": {"code": 403, "message": "Forbidden"}})), chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert str(raised.value) == "Авито не принял сообщение (код 403: Forbidden). " + RIGHT


def test_a_body_that_is_not_avitos_json_still_reaches_the_person(env):
    conn, chat_row, card = env
    err = refusal(402, "Payment Required: messenger quota")
    assert err.message == "HTTP 402" and err.body == "Payment Required: messenger quota"
    with pytest.raises(chats.ChatError) as raised:
        chats.send_reply(conn, Refusing(err), chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert "(код 402: Payment Required: messenger quota)" in str(raised.value)
    assert error_of(conn) == "HTTP 402: Payment Required: messenger quota"


def test_a_refusal_without_any_text_shows_only_the_code(env):
    conn, chat_row, card = env
    with pytest.raises(chats.ChatError) as raised:
        chats.send_reply(conn, Refusing(refusal(402, "")), chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert str(raised.value) == "Авито отказал (код 402), " + SELF
    assert error_of(conn) == "HTTP 402"


def test_avitos_own_code_is_kept_when_it_differs(env):
    conn, chat_row, card = env
    with pytest.raises(chats.ChatError):
        chats.send_reply(conn, Refusing(refusal(400, {"error": {"code": 1012, "message": "bad chat"}})), chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert error_of(conn) == "HTTP 400 (код Авито 1012): bad chat"


def test_a_key_in_avitos_answer_never_reaches_the_person_the_journal_or_the_log(env, caplog):
    conn, chat_row, card = env
    err = refusal(402, {"error": {"message": "token %s rejected, Bearer abc.def" % KEY}})
    with caplog.at_level(logging.WARNING, logger=chats.log.name):
        with pytest.raises(chats.ChatError) as raised:
            chats.send_reply(conn, Refusing(err), chat_row, "Добрый день", "tok-1", NOW, "owner")
    seen = [str(raised.value), error_of(conn)] + journal(conn, card) + [r.getMessage() for r in caplog.records]
    assert not any(KEY[:20] in line or "abc.def" in line for line in seen)
    assert "token … rejected, …" in str(raised.value)


def test_a_long_answer_is_cut(env):
    conn, chat_row, card = env
    with pytest.raises(chats.ChatError) as raised:
        chats.send_reply(conn, Refusing(refusal(402, {"error": {"message": "очень длинно " * 100}})), chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert len(str(raised.value)) < 300 and "…)" in str(raised.value)


def test_an_unsure_answer_shows_avitos_text_and_is_never_sent_again(env):
    conn, chat_row, card = env
    sender = Refusing(refusal(502, {"message": "upstream timeout"}))
    with pytest.raises(chats.ChatSendUnknown) as raised:
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert "(ответ Авито: код 502: upstream timeout)" in str(raised.value) and chats.CHECKING in str(raised.value)
    assert status(conn)[0] == "unknown" and error_of(conn) == "HTTP 502: upstream timeout"
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert sender.sent == []


def test_no_answer_at_all_is_recorded_by_its_kind_only(env):
    conn, chat_row, card = env
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, Refusing(TimeoutError("secret detail")), chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert error_of(conn) == "нет ответа (TimeoutError)"


def test_the_cabinet_asked_before_the_send_shows_avitos_text(env, monkeypatch):
    conn, chat_row, card = env

    def refused(*a, **k):
        raise refusal(402, {"error": {"message": "нет подписки"}})
    monkeypatch.setattr(chats.connections, "cabinet_user_id", refused)
    with pytest.raises(chats.ChatError) as raised:
        chats.send_reply(conn, Sender(), chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert "(код 402: нет подписки)" in str(raised.value)
    assert status(conn)[0] == "failed" and error_of(conn) == "HTTP 402: нет подписки"


def test_a_photo_refused_at_the_send_or_the_upload_shows_avitos_text(env):
    conn, chat_row, card = env
    with pytest.raises(chats.ChatError) as raised:
        chats.send_image(conn, Refusing(refusal(402, {"error": {"message": "Лимит"}})), chat_row, picture(), "img-1", NOW, "owner")
    assert str(raised.value) == "Авито отказал (код 402: Лимит), " + SELF
    assert status(conn, "img-1")[0] == "retry" and error_of(conn, "img-1") == "HTTP 402: Лимит"
    upload = Refusing(refusal(403, {"error": {"message": "Forbidden"}}))
    upload.at_upload = True
    with pytest.raises(chats.ChatError) as raised:
        chats.send_image(conn, upload, chat_row, picture(), "img-2", NOW, "owner")
    assert str(raised.value) == "Авито не принял фото (код 403: Forbidden). " + RIGHT
    assert error_of(conn, "img-2") == "HTTP 403: Forbidden"


def test_an_order_message_keeps_avitos_text_whole(env):
    conn, chat_row, card = env
    template = tpl.add_template(conn, "Клиенту: готов", "client", "Заказ готов")
    conn.execute("INSERT INTO order_task (order_id, rule_id, entered_at, kind, arg, title, body, status, created_at) VALUES (?, 0, ?, 'message', ?, 'Клиенту', 'Заказ готов', 'pending', ?)",
                 (card, NOW, str(template), NOW))
    conn.commit()
    task_id = conn.execute("SELECT MAX(id) FROM order_task").fetchone()[0]
    sender = Refusing(refusal(402, {"error": {"message": "Лимит сообщений исчерпан"}}))
    with pytest.raises(chats.ChatError) as raised:
        engine.send_task(conn, task_id, "Заказ готов", lambda row, text, token: chats.send_reply(conn, sender, row, text, token, NOW, "owner"), NOW, "owner")
    assert "(код 402: Лимит сообщений исчерпан)" in str(raised.value)
    assert engine.get_task(conn, task_id)["status"] == "pending"                                 # a refused send leaves the task waiting


def test_the_automatic_order_message_does_not_cut_avitos_text(tmp_path):
    from test_order_placed import Deliver, placed, ready
    from test_webmim_create import FakeWebmim, db, place
    conn = db(tmp_path)
    mine = ready(conn)
    place(conn, mine, FakeWebmim())
    long = chats.ChatError("Авито не принял сообщение (код 402: %s…). %s" % ("т" * 159, AGAIN))
    assert len(str(long)) > 200
    [line] = placed(conn, mine, Deliver(fail=long))
    assert AGAIN in line
    assert any(AGAIN in e["text"] for e in orders.list_events(conn, mine))


def test_avito_quoting_our_message_never_puts_it_in_the_journals(env, caplog):
    conn, chat_row, card = env
    text = "Иван, ваш заказ ждёт по адресу Новая ул., 1, телефон 8 900 000-00-00"
    err = refusal(400, {"error": {"message": "message.text '%s' is invalid" % text}})
    with caplog.at_level(logging.WARNING, logger=chats.log.name):
        with pytest.raises(chats.ChatError) as raised:
            chats.send_reply(conn, Refusing(err), chat_row, text, "tok-1", NOW, "owner")
    seen = [str(raised.value), error_of(conn)] + journal(conn, card) + [r.getMessage() for r in caplog.records]
    assert not any("Новая ул" in line or "ваш заказ" in line for line in seen)
    assert "(код 400: текст ответа скрыт, в нём наше сообщение)" in str(raised.value)


def test_a_link_and_a_request_id_of_avito_stay_readable():
    err = refusal(402, {"error": {"message": "see https://developers.avito.ru/api-catalog/messenger/documentation request 3f2b8c1e-9a7d-4e21-b0c3-7d1e2f3a4b5c"}})
    said = chats._avito_said(err)
    assert "developers.avito.ru/api-catalog/messenger/documentation" in said and "3f2b8c1e-9a7d-4e21-b0c3-7d1e2f3a4b5c" in said


def test_an_html_page_instead_of_avitos_json_shows_its_words_only():
    said = chats._avito_said(refusal(502, "<html><head><title>502 Bad Gateway</title></head><body><center>nginx</center></body></html>"))
    assert "<" not in said and "502 Bad Gateway" in said


def test_avitos_own_code_is_cleaned_like_its_text(env, caplog):
    conn, chat_row, card = env
    with caplog.at_level(logging.WARNING, logger=chats.log.name):
        with pytest.raises(chats.ChatError):
            chats.send_reply(conn, Refusing(refusal(400, {"error": {"code": "x\nWARNING forged", "message": "bad"}})), chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert "\n" not in error_of(conn) and not any("\n" in r.getMessage() for r in caplog.records)


def test_a_base64_key_with_a_slash_is_hidden_whole():
    key = "Ab12Cd34Ef56Gh78/Ij90Kl12Mn34Op56Qr78St90"
    said = chats._avito_said(refusal(402, {"error": {"message": "bad key %s and 3q2+7w8/Ab12Cd34Ef56Gh78Ij90Kl12Mn34Op56Qr78/St90Uv==" % key}}))
    assert "Ab12" not in said and "St90" not in said and "Ij90" not in said
