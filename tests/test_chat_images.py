"""A photo to the client of a chat: the client calls of Avito, the preparation of the picture, the send once per token (owner, 2026-10-19)."""
from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from http_fakes import ScriptedHttp
from test_chats_sync import FakeClient, TS, chat, db, msg
from tires_sync import chats, cli, connections, orders, store
from tires_sync.avito import AvitoClient, AvitoError, WriteDisabled
from tires_sync.cli import main
from tires_sync.secretbox import create_key_file, load_box

NOW = "2026-10-13T12:00:00"
TOKEN = (200, {"access_token": "t", "expires_in": 3600})


def jpeg(size=(640, 480), color=(200, 30, 30), exif=False) -> bytes:
    image = Image.new("RGB", size, color)
    out = io.BytesIO()
    if exif:
        data = Image.Exif()
        data[0x010F] = "SecretCameraMaker"                       # a tag that must not travel to the client
        image.save(out, "JPEG", exif=data)
    else:
        image.save(out, "JPEG")
    return out.getvalue()


def client_with(*replies):
    http = ScriptedHttp(TOKEN, *replies)
    return AvitoClient("cid", "secret", http=http, now=lambda: 0.0, allow_write=True), http


def test_the_client_uploads_a_picture_as_a_multipart_form_and_returns_its_id():
    client, http = client_with((200, {"img-777": {"640x480": "https://img.example/1.jpg"}}))
    assert client.upload_image(7, "photo.jpg", b"\xff\xd8bytes") == "img-777"
    method, url, headers, body = [c for c in http.calls if "messenger" in c[1]][0]
    assert method == "POST" and url.endswith("/messenger/v1/accounts/7/uploadImages")
    assert headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'name="uploadfile[]"; filename="photo.jpg"' in body and b"Content-Type: image/jpeg" in body and b"\xff\xd8bytes" in body
    assert headers["Content-Type"].split("boundary=")[1].encode() in body


@pytest.mark.parametrize("answer", [{}, [], {"a": 1, "b": 2}, "text", {"": {}}])
def test_a_strange_answer_to_the_upload_is_an_error_not_an_id(answer):
    client, _ = client_with((200, answer))
    with pytest.raises(AvitoError):
        client.upload_image(7, "photo.jpg", b"x")


def test_the_client_sends_the_uploaded_picture_to_a_chat_without_retries_and_only_when_writing_is_allowed():
    client, http = client_with((200, {"id": "m-img", "created": TS}))
    assert client.send_image(7, "u2i-abc", "img-777")["id"] == "m-img"
    method, url, _h, body = [c for c in http.calls if "messenger" in c[1]][0]
    assert method == "POST" and url.endswith("/messenger/v1/accounts/7/chats/u2i-abc/messages/image") and json.loads(body) == {"image_id": "img-777"}
    failing, calls = client_with((502, "bad gateway"), (200, {}))
    with pytest.raises(AvitoError):
        failing.send_image(7, "u2i-abc", "img-777")
    assert len([c for c in calls.calls if "messenger" in c[1]]) == 1                                                   # never retried: a retry could send it twice
    read_only = AvitoClient("cid", "secret", http=ScriptedHttp(), now=lambda: 0.0)
    for call in (lambda: read_only.upload_image(7, "p.jpg", b"x"), lambda: read_only.send_image(7, "c", "i")):
        with pytest.raises(WriteDisabled):
            call()


def test_the_picture_is_made_a_small_jpeg_without_the_data_of_the_camera_and_what_is_not_a_picture_is_refused():
    big = Image.new("RGBA", (4000, 3000), (10, 20, 30, 128))
    raw = io.BytesIO()
    big.save(raw, "PNG")
    out = chats.prepare_image(raw.getvalue())
    image = Image.open(io.BytesIO(out))
    assert image.format == "JPEG" and max(image.size) == 1600 and image.size == (1600, 1200)
    assert not Image.open(io.BytesIO(chats.prepare_image(jpeg(exif=True)))).getexif()                                  # nothing of the camera travels
    small = Image.open(io.BytesIO(chats.prepare_image(jpeg((300, 200)))))
    assert small.size == (300, 200)                                                                                    # a small one is not enlarged
    for bad in (b"", b"not a picture", b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"%PDF-1.4 ..."):
        with pytest.raises(chats.ChatError):
            chats.prepare_image(bad)
    tiff = io.BytesIO()
    Image.new("RGB", (10, 10)).save(tiff, "TIFF")                                                                      # a real picture of a kind we do not send
    with pytest.raises(chats.ChatError, match="Нужна фотография"):
        chats.prepare_image(tiff.getvalue())
    with pytest.raises(chats.ChatError, match="слишком большой"):
        chats.prepare_image(b"\xff\xd8" + b"0" * (chats.MAX_IMAGE_BYTES + 1))


class Sender:
    def __init__(self, upload_error=None, send_error=None):
        self.calls, self.upload_error, self.send_error = [], upload_error, send_error

    def get_self(self, retry=True):
        return {"id": 1}

    def upload_image(self, user_id, filename, data):
        self.calls.append(("upload", user_id, filename, len(data)))
        if self.upload_error:
            raise self.upload_error
        return "img-1"

    def send_image(self, user_id, chat_id, image_id):
        self.calls.append(("send", user_id, chat_id, image_id))
        if self.send_error:
            raise self.send_error
        return {"id": "m-img-1", "created": TS + 100}


def ready(tmp_path):
    conn, account = db(tmp_path)
    orders.ensure_pipeline(conn, NOW)
    fake = FakeClient([[chat("c1", text="Хочу заказать")]], {"c1": [msg("m1", "Хочу заказать")]})
    chats.sync_account(conn, fake, account, NOW)
    conn.execute("UPDATE account SET chats_send = 1, chats_enabled = 1 WHERE id = ?", (account,))
    conn.commit()
    return conn, chats.list_chats(conn)[0]["id"]


def test_a_photo_is_uploaded_and_sent_once_per_token_and_shown_in_the_chat(tmp_path):
    conn, row = ready(tmp_path)
    sender = Sender()
    chats.send_image(conn, sender, row, jpeg(), "tok-1", NOW, "pavel")
    chats.send_image(conn, sender, row, jpeg(), "tok-1", NOW, "pavel")                                                # a double press: the same token
    assert [c[0] for c in sender.calls] == ["upload", "send"] and sender.calls[1][1:] == (1, "c1", "img-1")
    last = chats.list_messages(conn, row)[-1]
    assert last["direction"] == "out" and "Фото" in last["text"]
    assert "Фото" in chats.get_chat(conn, row)["last_text"]


def test_a_photo_needs_the_sending_to_be_on_and_a_token_and_a_picture(tmp_path):
    conn, row = ready(tmp_path)
    sender = Sender()
    with pytest.raises(chats.ChatError, match="token|метки"):
        chats.send_image(conn, sender, row, jpeg(), "", NOW, "pavel")
    with pytest.raises(chats.ChatError):
        chats.send_image(conn, sender, row, b"junk", "tok-2", NOW, "pavel")
    conn.execute("UPDATE account SET chats_send = 0")
    conn.commit()
    with pytest.raises(chats.ChatError, match="Отправка ответов выключена"):
        chats.send_image(conn, sender, row, jpeg(), "tok-3", NOW, "pavel")
    assert sender.calls == []


def test_a_refused_upload_sends_nothing_and_may_be_tried_again_but_an_unsure_send_is_never_repeated(tmp_path):
    conn, row = ready(tmp_path)
    broken = Sender(upload_error=AvitoError(502, None, "bad gateway"))
    with pytest.raises(chats.ChatError):
        chats.send_image(conn, broken, row, jpeg(), "tok-up", NOW, "pavel")
    good = Sender()
    chats.send_image(conn, good, row, jpeg(), "tok-up", NOW, "pavel")                                                 # the same token again: nothing had left
    assert [c[0] for c in good.calls] == ["upload", "send"]
    unsure = Sender(send_error=AvitoError(502, None, "bad gateway"))
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_image(conn, unsure, row, jpeg(), "tok-un", NOW, "pavel")
    again = Sender()
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_image(conn, again, row, jpeg(), "tok-un", NOW, "pavel")
    assert again.calls == []
    refused = Sender(send_error=AvitoError(403, None, "no right"))
    with pytest.raises(chats.ChatError, match="не принял"):
        chats.send_image(conn, refused, row, jpeg(), "tok-no", NOW, "pavel")
    assert [m for m in chats.list_messages(conn, row) if "Фото" in m["text"]] and len([m for m in chats.list_messages(conn, row) if "Фото" in m["text"]]) == 1


def test_the_test_command_shows_the_plan_without_confirm_and_sends_one_photo_with_it(tmp_path, monkeypatch, capsys):
    conn = store.connect(tmp_path / "t.db")
    orders.ensure_pipeline(conn, NOW)
    account = connections.create_account(conn, "Основной", NOW, "o")
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    box = load_box(key_file)
    connections.set_secret(conn, box, "avito", account, "client_id", "CLIENTID-12345678", NOW, "o")
    connections.set_secret(conn, box, "avito", account, "client_secret", "SECRETVALUE-XYZ", NOW, "o")
    connections.set_modes(conn, account, True, False, NOW, "o")
    fake = FakeClient([[chat("c1", text="Хочу заказать")]], {"c1": [msg("m1", "Хочу заказать")]})
    chats.sync_account(conn, fake, account, NOW)
    row = chats.list_chats(conn)[0]["id"]
    conn.close()
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(jpeg())
    base = ["--db", str(tmp_path / "t.db"), "--key-file", str(key_file), "--chat", str(row), "--file", str(photo)]
    http = ScriptedHttp(TOKEN, (200, {"img-9": {}}), (200, {"id": "m-photo", "created": TS}))          # the cabinet of the account is already known
    monkeypatch.setattr(cli, "_http", lambda: http)
    assert main(["chat-test-send-image"] + base) == 0
    assert "Ничего не отправлено" in capsys.readouterr().out and http.calls == []
    assert main(["chat-test-send-image"] + base + ["--confirm"]) == 0
    assert "отправлено" in capsys.readouterr().out
    posts = [c for c in http.calls if c[0] == "POST" and "messenger" in c[1]]
    assert [p[1].rsplit("/", 1)[1] for p in posts] == ["uploadImages", "image"]


def test_a_turned_photo_is_set_upright_and_a_picture_of_too_many_points_is_refused_before_it_is_decoded(monkeypatch):
    image = Image.new("RGB", (300, 200), (10, 200, 10))
    data = Image.Exif()
    data[0x0112] = 6                                                          # the camera was held on its side
    raw = io.BytesIO()
    image.save(raw, "JPEG", exif=data)
    assert Image.open(io.BytesIO(chats.prepare_image(raw.getvalue()))).size == (200, 300)
    monkeypatch.setattr(chats, "MAX_IMAGE_PIXELS", 50_000)
    with pytest.raises(chats.ChatError, match="числу точек"):
        chats.prepare_image(jpeg((400, 300)))                                  # 120 000 points
    assert chats.prepare_image(jpeg((200, 200)))                               # 40 000 are fine


def test_a_picture_with_transparency_of_any_kind_gets_a_white_ground_and_a_broken_file_is_a_plain_refusal():
    for mode in ("RGBA", "LA", "P"):
        image = Image.new("RGBA", (40, 40), (0, 0, 0, 0)).convert(mode) if mode != "P" else Image.new("RGBA", (40, 40), (0, 0, 0, 0)).convert("P")
        raw = io.BytesIO()
        image.save(raw, "PNG", transparency=0) if mode == "P" else image.save(raw, "PNG")
        out = Image.open(io.BytesIO(chats.prepare_image(raw.getvalue()))).convert("RGB")
        assert min(out.getpixel((20, 20))) > 240                               # white, not black
    half = jpeg((800, 600))[:400]                                              # cut in the middle of the header data
    with pytest.raises(chats.ChatError):
        chats.prepare_image(half)


def test_an_unsure_send_and_a_failed_upload_are_told_apart_for_any_kind_of_trouble(tmp_path):
    conn, row = ready(tmp_path)
    timeout_on_upload = Sender(upload_error=OSError("timed out"))
    with pytest.raises(chats.ChatError):
        chats.send_image(conn, timeout_on_upload, row, jpeg(), "tok-a", NOW, "pavel")
    chats.send_image(conn, Sender(), row, jpeg(), "tok-a", NOW, "pavel")                                              # nothing had left: the same token goes again
    timeout_on_send = Sender(send_error=OSError("timed out"))
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_image(conn, timeout_on_send, row, jpeg(), "tok-b", NOW, "pavel")
    other = chats.get_chat(conn, row)["id"]
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status) VALUES ('tok-x', 9999, ?, 'o', 'sent')", (NOW,))
    conn.commit()
    with pytest.raises(chats.ChatError, match="другому чату"):
        chats.send_image(conn, Sender(), other, jpeg(), "tok-x", NOW, "pavel")
    conn.execute("UPDATE account SET chats_enabled = 0")
    conn.commit()
    with pytest.raises(chats.ChatError, match="выключено чтение"):
        chats.send_image(conn, Sender(), row, jpeg(), "tok-c", NOW, "pavel")


def test_a_photo_to_a_chat_with_a_deal_is_noted_in_the_deal(tmp_path):
    from tires_sync import chat_deals as deals
    conn, row = ready(tmp_path)
    deals.create_deal(conn, row, NOW, "pavel")
    chats.send_image(conn, Sender(), row, jpeg(), "tok-d", NOW, "pavel")
    card = orders.get_card(conn, chats.get_chat(conn, row)["order_id"])
    assert any("Отправлено фото клиенту" in e["text"] for e in orders.list_events(conn, card["id"]))


def test_the_body_of_the_upload_ends_with_the_closing_boundary_and_uses_crlf():
    client, http = client_with((200, {"img-1": {}}))
    client.upload_image(7, "photo.jpg", b"DATA")
    body = [c for c in http.calls if "uploadImages" in c[1]][0][3]
    boundary = [c for c in http.calls if "uploadImages" in c[1]][0][2]["Content-Type"].split("boundary=")[1].encode()
    assert body.endswith(b"DATA\r\n--" + boundary + b"--\r\n") and body.startswith(b"--" + boundary + b"\r\nContent-Disposition: form-data;")


def test_the_colour_profile_is_kept_only_when_it_fits_the_rgb_picture_we_write():
    def with_profile(space: bytes, mode="RGB"):
        profile = b"\x00" * 16 + space + b"\x00" * 108
        raw = io.BytesIO()
        Image.new(mode, (50, 50)).save(raw, "JPEG", icc_profile=profile)
        return Image.open(io.BytesIO(chats.prepare_image(raw.getvalue()))).info.get("icc_profile")
    assert with_profile(b"RGB ") is not None                                  # an ordinary profile (sRGB, Display P3) travels: the colours stay true
    assert with_profile(b"CMYK", "CMYK") is None and with_profile(b"GRAY", "L") is None
