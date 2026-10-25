"""Sending a message to a person through the VK community (the managers of the points): messages.send."""
from __future__ import annotations

import urllib.parse

import pytest
from cryptography.fernet import Fernet

from http_fakes import ScriptedHttp
from tires_sync import connections, store, vk_send
from tires_sync.secretbox import SecretBox

NOW = "2026-10-15T12:00:00"
KEY = "vk1.a.SECRETKEYOFTHECOMMUNITY"


def form(call) -> dict:
    return {k: v[0] for k, v in urllib.parse.parse_qs(call[3].decode("utf-8")).items()}


def test_a_message_goes_to_the_vk_api_with_the_key_in_the_body_not_in_the_address():
    http = ScriptedHttp((200, {"response": 4711}))
    assert vk_send.send_message(KEY, "123456", "Заказ №7: появилась позиция", http=http, random_id=99) == 4711
    method, url = http.calls[0][0], http.calls[0][1]
    assert method == "POST" and url == "https://api.vk.com/method/messages.send" and KEY not in url
    sent = form(http.calls[0])
    assert sent["access_token"] == KEY and sent["peer_id"] == "123456" and sent["random_id"] == "99" and sent["v"] == "5.131"
    assert sent["message"] == "Заказ №7: появилась позиция"


def test_a_refusal_of_vk_is_an_error_without_the_key():
    http = ScriptedHttp((200, {"error": {"error_code": 901, "error_msg": "Can't send messages for users without permission"}}))
    with pytest.raises(vk_send.VkError) as err:
        vk_send.send_message(KEY, "123456", "текст", http=http)
    assert "901" in str(err.value) and KEY not in str(err.value)


def test_a_failed_request_is_an_error(monkeypatch):
    with pytest.raises(vk_send.VkError, match="ВК"):
        vk_send.send_message(KEY, "1", "текст", http=ScriptedHttp((500, {})))
    def down(*a):
        raise OSError("no route")
    with pytest.raises(vk_send.VkError, match="недоступен"):
        vk_send.send_message(KEY, "1", "текст", http=down)
    with pytest.raises(vk_send.VkError):
        vk_send.send_message(KEY, "1", "текст", http=ScriptedHttp((200, "not json at all")))
    for odd in ({"foo": 1}, {"response": "yes"}, {"response": None}, [1, 2]):
        with pytest.raises(vk_send.VkError):
            vk_send.send_message(KEY, "1", "текст", http=ScriptedHttp((200, odd)))


def test_wrong_input_is_refused_before_anything_is_sent():
    http = ScriptedHttp((200, {"response": 1}))
    for token, peer, text in (("", "1", "т"), (KEY, "", "т"), (KEY, "abc", "т"), (KEY, "-5", "т"), (KEY, "1", "  ")):
        with pytest.raises(vk_send.VkError):
            vk_send.send_message(token, peer, text, http=http)
    assert http.calls == []
    vk_send.send_message(KEY, "1", "я" * 5000, http=http)
    assert len(form(http.calls[0])["message"]) == vk_send.VK_LIMIT


def test_the_sender_exists_only_when_the_community_key_is_saved_and_uses_it(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    box = SecretBox(Fernet.generate_key())
    assert vk_send.sender_for(conn, box) is None and vk_send.sender_for(conn, None) is None
    connections.set_secret(conn, box, "vk", 0, "token", KEY, NOW, "owner")
    http = ScriptedHttp((200, {"response": 5}))
    send = vk_send.sender_for(conn, box, http=http)
    send("42", "привет")
    assert form(http.calls[0])["access_token"] == KEY and form(http.calls[0])["peer_id"] == "42"
