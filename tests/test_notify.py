from __future__ import annotations

import json

import pytest

from http_fakes import ScriptedHttp
from tires_sync.notify import NotifyError, send_message

TOKEN = "123456:ABCdef-SECRET"


def test_message_goes_to_the_bot_api_with_the_chat_id():
    http = ScriptedHttp((200, {"ok": True, "result": {}}))
    send_message(TOKEN, "42", "Привет", http=http)
    method, url, headers, body = http.calls[0]
    assert (method, url) == ("POST", "https://api.telegram.org/bot%s/sendMessage" % TOKEN)
    assert json.loads(body) == {"chat_id": "42", "text": "Привет", "disable_web_page_preview": True}


@pytest.mark.parametrize("reply", [(401, {"ok": False, "description": "Unauthorized"}), (400, "chat not found"),
                                   (200, {"ok": False, "description": "bad"}), (200, "not json")])
def test_failures_raise_notify_error_without_the_token(reply):
    with pytest.raises(NotifyError) as info:
        send_message(TOKEN, "42", "x", http=ScriptedHttp(reply))
    assert TOKEN not in str(info.value) and "SECRET" not in str(info.value)


def test_network_error_is_reported_without_the_url():
    def down(*args):
        raise OSError("failed to reach https://api.telegram.org/bot%s/sendMessage" % TOKEN)

    with pytest.raises(NotifyError) as info:
        send_message(TOKEN, "42", "x", http=down)
    assert TOKEN not in str(info.value) and "недоступен" in str(info.value)


def test_long_text_is_cut_to_the_telegram_limit():
    http = ScriptedHttp((200, {"ok": True}))
    send_message(TOKEN, "42", "я" * 5000, http=http)
    assert len(json.loads(http.calls[0][3])["text"]) <= 4000


def test_missing_settings_are_rejected_before_any_request():
    http = ScriptedHttp()
    with pytest.raises(NotifyError):
        send_message("", "42", "x", http=http)
    with pytest.raises(NotifyError):
        send_message(TOKEN, "  ", "x", http=http)
    assert http.calls == []


def test_send_photo_multipart():
    from tires_sync.notify import send_photo
    http = ScriptedHttp((200, {"ok": True, "result": {}}))
    send_photo(TOKEN, "42", "я" * 2000, b"\xff\xd8\xffJPEGDATA", http=http)
    method, url, headers, body = http.calls[0]
    assert (method, url) == ("POST", "https://api.telegram.org/bot%s/sendPhoto" % TOKEN)
    assert headers["Content-Type"].startswith("multipart/form-data; boundary=")
    boundary = headers["Content-Type"].split("boundary=")[1].encode()
    assert body.count(b"--" + boundary) == 4 and body.rstrip().endswith(b"--" + boundary + b"--")
    assert b'name="chat_id"\r\n\r\n42\r\n' in body
    assert b'name="photo"; filename="sheet.jpg"\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8\xffJPEGDATA\r\n' in body
    assert ("я" * 1024).encode() in body and ("я" * 1025).encode() not in body
    for reply in ((400, "bad"), (200, {"ok": False}), (200, "no")):
        with pytest.raises(NotifyError) as info:
            send_photo(TOKEN, "42", "x", b"j", http=ScriptedHttp(reply))
        assert TOKEN not in str(info.value)
    with pytest.raises(NotifyError):
        send_photo("bad token", "42", "x", b"j", http=http)
