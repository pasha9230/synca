"""One «Отправить» for the answer of a chat: the text, the photo or both in one press; the photo goes first and a text never follows a photo that did not."""
from __future__ import annotations

import io
import re

from test_chats_pages import _image_token, _photo, _sendable_page, _token, setup

UPLOAD = (200, {"img-9": {"1600x1200": "https://img.example/9.jpg"}})
IMAGE_SENT = (200, {"id": "m-img", "created": 1_791_900_500})
TEXT_SENT = (200, {"id": "m-out", "created": 1_791_900_600, "direction": "out", "type": "text", "content": {"text": "Вот фото"}})


def _press(client, row, tok, text="", photo=None, reply_token=None, image_token=None, **extra):
    data = {"csrf": tok, "text": text, "reply_token": reply_token or _token(client, row), "image_token": image_token or _image_token(client, row), **extra}
    if photo is not None:
        data["photo"] = (io.BytesIO(photo), "photo.jpg")
    return client.post("/chats/%d/send" % row["id"], data=data, content_type="multipart/form-data")


def _avito(app):
    """What reached Avito, in order: «upload», «image» (the photo message) and «text»."""
    out = []
    for method, url, _h, _b in app.config["AVITO_HTTP"].calls:
        if url.endswith("/uploadImages"):
            out.append("upload")
        elif url.endswith("/messages/image"):
            out.append("image")
        elif method == "POST" and url.endswith("/messages"):
            out.append("text")
    return out


def _textarea(page):
    return re.search(r"<textarea[^>]*>(.*?)</textarea>", page, re.S).group(1)


def test_the_chat_has_one_form_with_the_text_the_clip_and_one_send_button(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path)
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    forms = re.findall(r'<form method="post" action="/chats/%d/send[^"]*"[^>]*>.*?</form>' % tanya["id"], page, re.S)
    assert len(forms) == 1                                                                                   # no second form for the photo
    form = forms[0]
    assert 'action="/chats/%d/send"' % tanya["id"] in form and 'enctype="multipart/form-data"' in form and "data-once" in form
    assert 'name="text"' in form and 'name="photo"' in form and 'accept="image/*"' in form
    assert re.search(r'chat-photo-pick"><svg class="i i-sm"[^>]*><path d="M21 11.5[^"]*"/></svg> Фото<input', form) and "📎" not in page   # the set's paperclip, not an emoji
    assert 'name="reply_token"' in form and 'name="image_token"' in form and 'name="csrf"' in form
    assert re.search(r'name="photo"[^>]*required', form) is None                                           # a text alone is a valid press
    assert re.findall(r"<button(?![^>]*type=\"button\")[^>]*>([^<]*)</button>", form) == ["Отправить"]           # the one button that sends
    assert "Отправить клиенту" not in page and "Отправить фото клиенту" not in page
    assert re.search(r'<textarea[^>]*rows="2"', form)
    assert "Уйдёт сразу, от имени «Основной»" in form and re.search(r"<span data-photo-note>[^<]*</span>", form)   # the account line is not in the note the photo rewrites


def test_text_and_photo_go_in_one_press_the_photo_first(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path, UPLOAD, IMAGE_SENT, TEXT_SENT)
    assert _press(client, tanya, tok, text="Вот фото", photo=_photo((800, 600))).status_code == 302
    assert _avito(app) == ["upload", "image", "text"]
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert "Фото и ответ отправлены клиенту" in page and "Вот фото" not in _textarea(page)


def test_a_double_press_of_text_and_photo_sends_each_once(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path, UPLOAD, IMAGE_SENT, TEXT_SENT)
    reply, image = _token(client, tanya), _image_token(client, tanya)
    _press(client, tanya, tok, text="Вот фото", photo=_photo((800, 600)), reply_token=reply, image_token=image)
    _press(client, tanya, tok, text="Вот фото", photo=_photo((800, 600)), reply_token=reply, image_token=image)
    assert _avito(app) == ["upload", "image", "text"]


def test_a_refused_photo_keeps_the_text_unsent_in_the_field_and_says_what_did_not_go(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path, (403, {"error": {"message": "forbidden"}}))
    _press(client, tanya, tok, text="Текст после фото", photo=_photo((800, 600)))
    assert _avito(app) == ["upload"]                                                                         # the upload was refused: no message at all
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert "Фото не отправлено" in page and "Текст не отправлен" in page
    assert "Текст после фото" in _textarea(page)


def test_an_unsure_photo_keeps_the_text_unsent_and_warns_not_to_send_the_photo_again(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path, UPLOAD, (502, {"error": {"message": "bad gateway"}}))
    _press(client, tanya, tok, text="Текст после фото", photo=_photo((800, 600)))
    assert _avito(app) == ["upload", "image"]
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert "Неизвестно, ушло ли фото" in page and "Текст не отправлен" in page
    assert "Фото не отправлено" not in page                                                                 # it may have gone: never say it did not
    assert "Текст после фото" in _textarea(page)                                                           # the text itself surely did not go


def test_an_empty_file_with_a_text_sends_nothing_and_says_so(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path)
    _press(client, tanya, tok, text="Текст после фото", photo=b"")
    assert _avito(app) == []                                                                                # the text does not go without the chosen photo
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert "Файл фото пустой" in page and "Текст после фото" in _textarea(page)


def test_a_photo_alone_clears_an_old_draft(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path, (403, {"error": {"message": "forbidden"}}),
                                                   (200, {"access_token": "t", "expires_in": 3600}), UPLOAD, IMAGE_SENT)   # each press has its own client
    _press(client, tanya, tok, text="Старый черновик")                                                     # refused: kept as the draft
    assert "Старый черновик" in _textarea(client.get("/chats/%d" % tanya["id"]).get_data(as_text=True))
    _press(client, tanya, tok, text="", photo=_photo((800, 600)))                                           # the field was cleared, a photo alone went
    assert "Старый черновик" not in _textarea(client.get("/chats/%d" % tanya["id"]).get_data(as_text=True))


def test_a_file_that_is_not_a_photo_sends_nothing(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path)
    _press(client, tanya, tok, text="Текст после фото", photo=b"%PDF-1.4 not a picture")
    assert _avito(app) == []
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert "не открывается как фотография" in page and "Текст после фото" in _textarea(page)


def test_a_sent_photo_and_a_refused_text_say_so_and_keep_the_text(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path, UPLOAD, IMAGE_SENT, (403, {"error": {"message": "forbidden"}}))
    _press(client, tanya, tok, text="Текст после фото", photo=_photo((800, 600)))
    assert _avito(app) == ["upload", "image", "text"]
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert "Фото отправлено клиенту, а текст не ушёл" in page and "Текст после фото" in _textarea(page)


def test_a_sent_photo_and_an_unsure_text_do_not_bring_the_text_back(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path, UPLOAD, IMAGE_SENT, (502, {"error": {"message": "bad gateway"}}))
    _press(client, tanya, tok, text="Текст после фото", photo=_photo((800, 600)))
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert "Фото отправлено клиенту" in page and "Неизвестно, ушло ли сообщение" in page
    assert "Текст после фото" not in _textarea(page)


def test_an_empty_press_sends_nothing_and_says_so(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path)
    assert _press(client, tanya, tok, text="  \n ").status_code == 302
    assert _avito(app) == []
    assert "Ничего не отправлено: напишите ответ или выберите фото." in client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)


def test_a_photo_alone_goes_from_the_same_form(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path, UPLOAD, IMAGE_SENT)
    _press(client, tanya, tok, text=" ", photo=_photo())
    assert _avito(app) == ["upload", "image"]                                                                # the blank text is not a message
    assert "Фото отправлено клиенту." in client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)


def test_a_text_alone_goes_as_before(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path, TEXT_SENT)
    _press(client, tanya, tok, text="Добрый день")
    assert _avito(app) == ["text"]
    assert "Ответ отправлен клиенту." in client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)


def test_a_text_over_the_limit_with_a_photo_sends_nothing(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path)
    _press(client, tanya, tok, text="я" * 2001, photo=_photo((800, 600)))
    assert _avito(app) == []
    assert "Ничего не отправлено" in client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)


def test_the_send_needs_csrf_and_an_allowed_account(tmp_path):
    from http_fakes import ScriptedHttp
    app, client, tok, conn, account = setup(tmp_path)
    app.config["AVITO_HTTP"] = ScriptedHttp()
    tanya = next(c for c in __import__("tires_sync.chats", fromlist=["x"]).list_chats(conn) if c["peer_name"] == "Татьяна")
    assert client.post("/chats/%d/send" % tanya["id"], data={"text": "x", "photo": (io.BytesIO(_photo((80, 60))), "p.jpg")},
                       content_type="multipart/form-data").status_code == 400
    _press(client, tanya, tok, text="x", photo=_photo((80, 60)), reply_token="a" * 24, image_token="b" * 24)    # sending is off for the account
    assert _avito(app) == []


def test_a_chosen_photo_can_be_removed_without_a_reload_and_the_text_stays(tmp_path):
    """Owner's word 2026-10-20: «×» at the chosen photo. The button never sends; app.js empties the file and the note, never the text.
    (Checked in a real browser: choose, «×», the note back, the text kept, «Отправить» sends the text alone.)"""
    app, client, tok, conn, tanya = _sendable_page(tmp_path)
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    form = re.search(r'<form method="post" action="/chats/%d/send"[^>]*>.*?</form>' % tanya["id"], page, re.S).group(0)
    clear = re.search(r"<button[^>]*data-photo-clear[^>]*>(.*?)</button>", form)                    # «×» is the cross of the icon set (UX review 1.0i)
    assert clear and clear.group(1).startswith('<svg class="i i-sm"') and 'type="button"' in clear.group(0) and " hidden" in clear.group(0) and 'aria-label="Убрать фото"' in clear.group(0)
    js = client.get("/static/app.js").get_data(as_text=True)
    block = js[js.index("[data-photo-clear]"):][:1500]
    assert 'input.value = ""' in block and "textarea" not in block                                         # the file goes, the text is not touched
    assert block.index("form.dataset.sent") < block.index('input.value = ""')                            # during a send «×» does nothing: the photo is already on its way
    assert "input.focus()" in block                                                                         # the keyboard stays in the form when «×» hides
