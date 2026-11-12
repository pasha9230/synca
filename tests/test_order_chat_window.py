"""The column «Чат Авито» of the order window (the owner 2026-11-09: «Доработай чат авито в заказе, чтобы я мог прямо из этого окна видеть
диалог и писать клиенту. Автосообщения убери»), in the old view and «Новый вид»: the whole thread of the order's chat and the reply field of
the page «Чаты» (the same files, the same form to /chats/<id>/send), the send comes back to the window (#order-chat), once per press; the
rights of the page «Чаты» (a point: its own account; the observer reads only; another account's chat is never shown); no chat: as before."""
from __future__ import annotations

import io
import re

import pytest

from panel_helpers import post
from test_chats_pages import NOW, _photo, _sendable_page
from tires_sync import chats, connections, orders, store, users

TEXT_SENT = (200, {"id": "m-out", "created": 1_791_900_600, "direction": "out", "type": "text", "content": {"text": "Есть в наличии"}})
UPLOAD = (200, {"img-9": {"1600x1200": "https://img.example/9.jpg"}})
IMAGE_SENT = (200, {"id": "m-img", "created": 1_791_900_500})


@pytest.fixture(params=["old", "new"])
def view(request):
    return request.param


def page(tmp_path, view, *replies):
    """The chat «Татьяна» of the account «Основной» (sending on) with its order, a photo and a link from the client in its thread."""
    app, client, tok, conn, tanya = _sendable_page(tmp_path, *replies)
    store.set_state(conn, "ui_new.office", "all" if view == "new" else "off")
    conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, kind, text, media) VALUES (?, 'p-1', '2026-10-13T08:30:00', 1791900300, 'in', 'image', '', 'https://img.example/tyre.jpg')", (tanya["id"],))
    conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, kind, text) VALUES (?, 'p-2', '2026-10-13T08:31:00', 1791900400, 'in', 'text', 'Вот такие: https://example.com/tyre')", (tanya["id"],))
    conn.commit()
    tanya = chats.get_chat(conn, tanya["id"])
    assert tanya["order_id"]
    return app, client, tok, conn, tanya


def window(client, card, status=200):
    r = client.get("/orders/%d" % card)
    assert r.status_code == status
    return r.get_data(as_text=True)


def column(html):
    """The chat's own block in the window (_order_chat.html)."""
    found = re.search(r'<div class="order-chat" data-order-chat="\d+"[^>]*>.*?<div class="chat-composer[^"]*" id="order-chat">.*?</div>\n</div>', html, re.S)
    return found.group(0) if found else ""


def form_of(html, chat_row):
    found = re.search(r'<form method="post" action="/chats/%d/send"[^>]*>.*?</form>' % chat_row, column(html), re.S)
    return found.group(0) if found else ""


def press(client, chat_row, html, tok, text="", photo=None):
    form = form_of(html, chat_row)
    data = {k: v for k, v in re.findall(r'<input type="hidden" name="([a-z_]+)" value="([^"]*)"', form)}
    data.update(csrf=tok, text=text)
    if photo is not None:
        data["photo"] = (io.BytesIO(photo), "photo.jpg")
    return client.post("/chats/%d/send" % chat_row, data=data, content_type="multipart/form-data")


def sent(app):
    return [url for method, url, _h, _b in app.config["AVITO_HTTP"].calls if method == "POST" and (url.endswith("/messages") or url.endswith("/messages/image"))]


def other_user(app, conn, login_, role, **extra):
    users.add_user(conn, login_, "длинный-пароль-1", NOW, role=role, **extra)
    conn.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = login_
        session["epoch"] = store.get_session_epoch(conn, login_)
        session["csrf"] = "tok"
    return client


def test_the_window_shows_the_whole_thread_with_the_photo_and_the_link_as_the_chats_page(tmp_path, view):
    app, client, tok, conn, tanya = page(tmp_path, view)
    html = window(client, tanya["order_id"])
    assert ('office2.css' in html) == (view == "new")
    block = column(html)
    assert block, "no thread in the column «Чат Авито»"
    chat_page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    mine = block[block.index('<div class="chat-thread order-chat-thread">') + len('<div class="chat-thread order-chat-thread">'):block.index('<div class="chat-composer')]
    theirs = chat_page[chat_page.index('<div class="chat-thread'):]
    theirs = theirs[theirs.index(">") + 1:theirs.index('<div class="chat-composer')]
    def norm(t):
        return re.sub(r"\s*</div>\s*$", "", t.strip())
    assert norm(mine) and norm(mine) == norm(theirs)                                                 # the same messages, the same markup
    assert "Здравствуйте, это актуально?" in mine and 'class="msg msg--in' in mine and "&lt;b&gt;жирный&lt;/b&gt;" in mine   # the client's text never markup
    assert 'src="https://img.example/tyre.jpg"' in mine and "https://example.com/tyre" in mine and "msg-meta" in mine     # the photo, the link, the time
    live = re.search(r'<div id="chats-live" data-sig="([0-9a-f]+)" data-chat="%d" data-page="/chats/%d"' % (tanya["id"], tanya["id"]), block)
    assert live and live.group(1) == chats.signature(conn, tanya["id"])                            # the live update of «Чаты» reads the chat's page
    assert html.count('id="chats-live"') == 1
    assert html.count('id="chats-offline"') == 1 and re.search(r'id="chats-offline" role="status" hidden', block)   # a cut connection is said, as in «Чаты»


def test_the_reply_field_is_the_one_of_the_chats_page_and_comes_back_to_the_window(tmp_path, view):
    app, client, tok, conn, tanya = page(tmp_path, view)
    html = window(client, tanya["order_id"])
    form = form_of(html, tanya["id"])
    assert form and 'enctype="multipart/form-data"' in form and "data-once" in form and "data-photo-form" in form
    assert 'name="text"' in form and 'aria-label="Ответ клиенту"' in form and 'name="photo"' in form and 'accept="image/*"' in form
    assert 'name="reply_token"' in form and 'name="image_token"' in form and '<input type="hidden" name="to" value="order">' in form
    assert 'data-back="/orders/%d#order-chat"' % tanya["order_id"] in form                         # a photo goes by app.js and comes back here
    assert re.findall(r"<button(?![^>]*type=\"button\")[^>]*>(?:<svg.*?</svg>)?([^<]*)</button>", form) == ["Отправить"]
    assert 'name="back"' not in form                                                               # no address from the page: the server picks it


def test_a_send_goes_the_chats_way_once_and_comes_back_to_the_window(tmp_path, view):
    app, client, tok, conn, tanya = page(tmp_path, view, TEXT_SENT)
    card = tanya["order_id"]
    html = window(client, card)
    before = len(orders.list_events(conn, card))
    r = press(client, tanya["id"], html, tok, text="Есть в наличии")
    assert r.status_code == 302 and r.headers["Location"].endswith("/orders/%d#order-chat" % card)
    again = press(client, tanya["id"], html, tok, text="Есть в наличии")                                # a double press: the same marks
    assert again.status_code == 302 and again.headers["Location"].endswith("/orders/%d#order-chat" % card)
    assert len([u for u in sent(app) if u.endswith("/messages")]) == 1                               # one message to Avito
    rows = conn.execute("SELECT status, author FROM chat_send_log WHERE chat_row = ?", (tanya["id"],)).fetchall()
    assert rows == [("sent", app.config["AUTHOR"])]
    added = orders.list_events(conn, card)[: len(orders.list_events(conn, card)) - before]
    assert [e for e in added if "Есть в наличии" in e["text"]] and len([e for e in added if "Есть в наличии" in e["text"]]) == 1   # one record in the journal
    back = window(client, card)
    assert "Ответ отправлен клиенту" in back and "Есть в наличии" in column(back)                    # the answer is in the thread of the window
    assert not chats.waiting(conn, [chats.get_chat(conn, tanya["id"])], {})                         # «отвечено»: the chat waits for nobody


def test_a_photo_from_the_window_goes_once_and_comes_back(tmp_path):
    app, client, tok, conn, tanya = page(tmp_path, "new", UPLOAD, IMAGE_SENT)
    card = tanya["order_id"]
    html = window(client, card)
    for _ in range(2):
        r = press(client, tanya["id"], html, tok, photo=_photo((800, 600)))
        assert r.status_code == 302 and r.headers["Location"].endswith("/orders/%d#order-chat" % card)
    assert [u for u in sent(app) if u.endswith("/messages/image")] and len([u for u in sent(app) if u.endswith("/messages/image")]) == 1


def test_a_chats_page_send_still_comes_back_to_the_chat(tmp_path):
    app, client, tok, conn, tanya = page(tmp_path, "new", TEXT_SENT)
    chat_page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    token = re.search(r'name="reply_token" value="([0-9a-f]+)"', chat_page).group(1)
    r = post(client, "/chats/%d/send" % tanya["id"], tok, text="Есть в наличии", reply_token=token)
    assert r.status_code == 302 and "/chats/%d" % tanya["id"] in r.headers["Location"] and "/orders/" not in r.headers["Location"]


def test_to_order_never_leads_to_another_order(tmp_path):
    app, client, tok, conn, tanya = page(tmp_path, "new", TEXT_SENT)
    other = orders.create_card(conn, {"client_name": "Другой"}, NOW, "owner")
    conn.commit()
    html = window(client, tanya["order_id"])
    form = form_of(html, tanya["id"])
    data = dict(re.findall(r'<input type="hidden" name="([a-z_]+)" value="([^"]*)"', form), csrf=tok, text="Тест", back="/orders/%d" % other, order="%d" % other)
    r = client.post("/chats/%d/send" % tanya["id"], data=data, content_type="multipart/form-data")
    assert r.headers["Location"].endswith("/orders/%d#order-chat" % tanya["order_id"])            # the chat's own order, whatever the form says


def test_the_observer_reads_the_thread_without_a_reply_field(tmp_path, view):
    app, client, tok, conn, tanya = page(tmp_path, view)
    viewer = other_user(app, conn, "look1", "viewer")
    html = window(viewer, tanya["order_id"])
    block = column(html)
    assert "Здравствуйте, это актуально?" in block and 'src="https://img.example/tyre.jpg"' in block
    assert not form_of(html, tanya["id"]) and 'name="text"' not in block and "Только просмотр" in block
    assert press(viewer, tanya["id"], '<div class="order-chat" data-order-chat="1"></div>', "tok", text="x").status_code == 403
    assert sent(app) == []


def test_a_point_sees_and_answers_its_own_account_s_chat_only(tmp_path):
    app, client, tok, conn, tanya = page(tmp_path, "old", TEXT_SENT)
    card = tanya["order_id"]
    alpha = conn.execute("INSERT INTO point (name, avito_account_id) VALUES ('Альфа', ?)", (tanya["account_id"],)).lastrowid
    conn.execute("UPDATE order_card SET point_id = ? WHERE id = ?", (alpha, card))
    conn.commit()
    point = other_user(app, conn, "gamma", "point", point_id=alpha)
    html = window(point, card)
    block = column(html)
    assert "Здравствуйте, это актуально?" in block and form_of(html, tanya["id"])
    assert "Основной" not in block                                                                   # never an account's name for a point
    r = press(point, tanya["id"], html, "tok", text="Есть в наличии")
    assert r.status_code == 302 and r.headers["Location"].endswith("/orders/%d#order-chat" % card)
    assert len([u for u in sent(app) if u.endswith("/messages")]) == 1
    # the point's account is another one now: the chat of «Основной» is neither shown nor answerable from the window
    other = connections.create_account(conn, "Второй", NOW, "o")
    conn.execute("UPDATE point SET avito_account_id = ? WHERE id = ?", (other, alpha))
    conn.commit()
    html = window(point, card)
    assert not column(html) and "Здравствуйте, это актуально?" not in html and 'action="/chats/%d/send"' % tanya["id"] not in html
    assert press(point, tanya["id"], '<div class="order-chat" data-order-chat="1"></div>', "tok", text="x").status_code == 403


def test_a_chat_of_another_order_is_not_shown(tmp_path):
    app, client, tok, conn, tanya = page(tmp_path, "new")
    ivan = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Иван")
    other = orders.create_card(conn, {"client_name": "Без чата"}, NOW, "owner")
    conn.commit()
    html = window(client, other)
    assert not column(html) and "Здравствуйте, это актуально?" not in html and 'id="chats-live"' not in html
    assert "К заказу не привязан чат Авито" in html                                                # no chat: as before
    if ivan["order_id"] and ivan["order_id"] != tanya["order_id"]:
        assert "Здравствуйте, это актуально?" not in window(client, ivan["order_id"])              # Татьяна's words never in Иван's order


def test_no_chat_in_the_old_view_is_as_before(tmp_path):
    app, client, tok, conn, tanya = page(tmp_path, "old")
    other = orders.create_card(conn, {"client_name": "Без чата"}, NOW, "owner")
    conn.commit()
    html = window(client, other)
    assert "Чаты Авито" not in html and 'id="order-chat"' not in html and 'id="chats-live"' not in html


def test_the_auto_message_block_is_gone_and_a_waiting_one_is_a_line_with_cancel(tmp_path, view):
    from datetime import datetime, timedelta
    from tires_sync import order_autosend as auto
    app, client, tok, conn, tanya = page(tmp_path, view)
    card = tanya["order_id"]
    html = window(client, card)
    assert 'id="autosend"' not in html and "Поставить:" not in html and 'action="/orders/%d/autosend"' % card not in html
    due = (datetime.now() + timedelta(days=1)).replace(second=0, microsecond=0)
    aid = auto.add(conn, card, "deal", due.strftime("%Y-%m-%dT%H:%M"), "Ждём вас завтра в десять, адрес на карте", NOW, "owner")
    conn.commit()
    html = window(client, card)
    line = re.search(r'<div class="row" data-autosend="%d".*?</div>' % aid, html, re.S).group(0)
    assert "Автосообщение запланировано на %s" % due.strftime("%d.%m %H:%M") in line and "«Ждём вас завтра в десять, адрес на карте»" in line
    assert 'action="/orders/%d/autosend/%d/cancel"' % (card, aid) in line and ">Отменить</button>" in line
    r = post(client, "/orders/%d/autosend/%d/cancel" % (card, aid), tok)
    assert r.status_code == 302 and auto.get(conn, aid)["status"] == "cancelled"
    assert 'data-autosend="%d"' % aid not in window(client, card)


# The partner 2026-11-12: «из заказа не видно, с какого аккаунта Авито диалог, и «Чаты» открываются на последнем выбранном аккаунте».

ACC_BADGE = r'<span class="badge badge--neutral order-chat-acc"[^>]*>%s</span>'


def test_the_window_names_the_chat_s_account_and_its_links_open_the_chats_on_it(tmp_path, view):
    app, client, tok, conn, tanya = page(tmp_path, view)
    html = window(client, tanya["order_id"])
    assert re.search(ACC_BADGE % "Основной", html)                                 # one account in the whole system: named all the same
    address = "/chats/%d?acc=%d" % (tanya["id"], tanya["account_id"])
    assert html.count('href="%s"' % address) >= 2                                  # the chat's line and «Открыть»
    assert 'href="/chats/%d"' % tanya["id"] not in html                            # no link of the window opens the kept account


def test_the_link_opens_the_dialog_on_its_account_and_keeps_the_person_s_choice_of_the_chats(tmp_path, view):
    from tires_sync import user_prefs
    app, client, tok, conn, tanya = page(tmp_path, view)
    other = connections.create_account(conn, "Второй", NOW, "o")
    conn.execute("UPDATE account SET chats_enabled = 1 WHERE id = ?", (other,))
    conn.execute("INSERT INTO avito_chat (account_id, chat_id, peer_name, last_at, last_text) VALUES (?, 'u2u-other', 'Чужой', '2026-10-13T09:00:00', 'Про другое')", (other,))
    conn.commit()
    client.get("/chats?acc=%d" % other, headers={"Referer": "http://localhost/chats"})        # the person's own choice on the page: kept
    login = conn.execute("SELECT login FROM user_pref WHERE key = 'f.chats_page'").fetchone()[0]
    kept = user_prefs.get(conn, login, "f.chats_page")
    assert kept == {"acc": [str(other)]}
    html = window(client, tanya["order_id"])
    address = re.search(r'href="(/chats/%d\?acc=\d+)"' % tanya["id"], html).group(1)
    assert address == "/chats/%d?acc=%d" % (tanya["id"], tanya["account_id"])
    r = client.get(address, headers={"Referer": "http://localhost/orders/%d" % tanya["order_id"]})
    assert r.status_code == 200
    chats_html = r.get_data(as_text=True)
    assert "Здравствуйте, это актуально?" in chats_html and 'name="acc" value="%d"' % tanya["account_id"] in chats_html   # the dialog, the list of its account
    assert "Чужой" not in chats_html                                                            # not the kept account's list
    assert user_prefs.get(conn, login, "f.chats_page") == kept                                  # the walk from the order saved nothing
    assert client.get("/chats", headers={"Referer": "http://localhost/"}).headers["Location"].endswith("/chats?acc=%d" % other)


def test_the_field_s_open_chat_link_goes_to_the_chat_s_account_too(tmp_path):
    app, client, tok, conn, tanya = page(tmp_path, "old")
    conn.execute("UPDATE order_card SET avito_chat = ? WHERE id = ?", ("Ч-%d" % tanya["id"], tanya["order_id"]))
    conn.commit()
    html = window(client, tanya["order_id"])
    assert '<a class="small" href="/chats/%d?acc=%d">Открыть чат</a>' % (tanya["id"], tanya["account_id"]) in html


def test_a_point_sees_no_account_badge_and_its_links_stay_bare(tmp_path, view):
    app, client, tok, conn, tanya = page(tmp_path, view)
    card = tanya["order_id"]
    alpha = conn.execute("INSERT INTO point (name, avito_account_id) VALUES ('Альфа', ?)", (tanya["account_id"],)).lastrowid
    conn.execute("UPDATE order_card SET point_id = ? WHERE id = ?", (alpha, card))
    conn.commit()
    html = window(other_user(app, conn, "gamma", "point", point_id=alpha), card)
    assert "Здравствуйте, это актуально?" in html
    assert "order-chat-acc" not in html and "Основной" not in column(html)
    assert 'href="/chats/%d"' % tanya["id"] in html and "?acc=" not in html


def test_an_order_without_a_chat_opens_without_the_badge(tmp_path, view):
    app, client, tok, conn, tanya = page(tmp_path, view)
    other = orders.create_card(conn, {"client_name": "Без чата"}, NOW, "owner")
    conn.commit()
    html = window(client, other)
    assert "order-chat-acc" not in html and "/chats/%d" % tanya["id"] not in html
