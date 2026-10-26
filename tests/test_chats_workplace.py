"""«Чаты» as a workplace (lane 5B, 2026-10-21): three columns, one word «заказ», the switch-off plaque, the search, the person who answers
and the clock of the waiting client, a position added to the order from the chat, the client's photos and calls."""
from __future__ import annotations

import re

from panel_helpers import post
from test_chats_pages import NOW, _sendable_page, setup
from tires_sync import chats, connections, orders, store, users

PASSWORD = "password-123"


def as_role(app, conn, role, login_="someone"):
    users.add_user(conn, login_, PASSWORD, NOW, role=role)
    conn.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = login_, store.get_session_epoch(conn, login_), "tok"
    return client


def chat_of(conn, name):
    return next(c for c in chats.list_chats(conn) if c["peer_name"] == name)


# --- 1, 4, 6: three columns, «заказ», the plaque ---------------------------------------------------------------------------------

def test_the_chat_has_three_columns_and_the_order_column_folds(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    page = client.get("/chats/%d" % chat_of(conn, "Татьяна")["id"]).get_data(as_text=True)
    layout = page.split('<div class="chats-layout">')[1]
    assert layout.index('class="card chats-list"') < layout.index('class="card chats-main"') < layout.index('class="card chats-deal"')
    assert 'data-deal-fold aria-controls="chats-deal"' in page and "data-deal-unfold" in page
    css = client.get("/static/panel.css").get_data(as_text=True)
    narrow = re.search(r"@media \(max-width: 1599px\) \{ \.chats-layout \{([^}]*)\}", css).group(1)
    assert "240px minmax(0, 1fr) 288px" in narrow                                           # at 1280 the order stays on the right
    assert ".chats-layout.is-deal-folded .chats-deal { display: none; }" in css


def test_one_word_for_the_order_in_the_chats(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    for url in ("/chats", "/chats/%d" % chat_of(conn, "Иван")["id"], "/chats/%d" % chat_of(conn, "Татьяна")["id"]):
        page = client.get(url).get_data(as_text=True)
        assert "делк" not in page.split('<div class="chats-layout">')[0].split("<h1>")[1] and "делк" not in page.split('class="chats-layout"')[1], url
    assert "Без заказа" in client.get("/chats").get_data(as_text=True)


def test_sending_off_is_a_plaque_with_the_way_to_switch_it_on_for_who_may(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    row = chat_of(conn, "Татьяна")
    page = client.get("/chats/%d" % row["id"]).get_data(as_text=True)
    composer = page.split('<div class="chat-composer">')[1].split("</div></div>")[0]
    assert "flash--warn" in composer and "Отправка ответов выключена" in composer
    assert '<a href="/connections/avito/%d">Включить</a>' % account in composer and "<textarea" not in composer
    manager = as_role(app, conn, "chats")
    composer = manager.get("/chats/%d" % row["id"]).get_data(as_text=True).split('<div class="chat-composer">')[1]
    assert "Отправка ответов выключена" in composer and "/connections/avito/" not in composer.split("</div></div>")[0]


# --- 3: the search ------------------------------------------------------------------------------------------------------------------

def _listed(client, q, **extra):
    from urllib.parse import urlencode
    page = client.get("/chats?" + urlencode(dict(q=q, **extra))).get_data(as_text=True)
    return re.findall(r'class="strong chat-name">([^<]*)<', page)


def test_the_search_finds_by_name_text_phone_and_order_number(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    tanya, ivan = chat_of(conn, "Татьяна"), chat_of(conn, "Иван")
    orders.update_card(conn, tanya["order_id"], {"client_phone": "+7 900 111-22-33", "webmim_number": "5000100"}, NOW, "o")
    conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, kind, text, is_read) VALUES (?,?,?,?,?,?,?,1)",
                 (ivan["id"], "m-phone", NOW, 1, "in", "text", "мой номер 8 (921) 555-44-33, перезвоните"))
    conn.commit()
    assert _listed(client, "иван") == ["Иван"]                                                    # the name, any case
    assert _listed(client, "АКТУАЛЬНО") == ["Татьяна"]                                            # the text of a message
    assert _listed(client, "89001112233") == ["Татьяна"] and _listed(client, "900 111 22") == ["Татьяна"]   # the order's phone, written another way
    assert _listed(client, "+7 921 555 44 33") == ["Иван"]                                        # a phone in the messages
    assert _listed(client, "5000100") == ["Татьяна"] and _listed(client, "№%d" % tanya["order_id"]) == ["Татьяна"]
    assert _listed(client, "нет такого") == [] and "<h3>По запросу «нет такого» чатов нет</h3>" in client.get("/chats?q=нет+такого").get_data(as_text=True)
    assert _listed(client, "100%_") == []                                                         # the wildcards of LIKE are text
    assert _listed(client, "иван", f="closed") == [] and _listed(client, "иван", f="nodeal") == ["Иван"]   # the filter still works with the search
    page = client.get("/chats?q=%D0%B8%D0%B2%D0%B0%D0%BD").get_data(as_text=True)
    assert 'href="/chats/%d?q=%%D0%%B8%%D0%%B2%%D0%%B0%%D0%%BD"' % ivan["id"] in page and 'href="/chats">Сбросить</a>' in page


# --- 5: who answers the chat and how long the client waits ---------------------------------------------------------------------------

def test_the_chat_is_given_to_a_person_and_the_journal_says_so(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    users.add_user(conn, "worker2", PASSWORD, NOW, role="partner", display_name="Партнёр Тестов")
    conn.commit()
    row = chat_of(conn, "Татьяна")
    page = client.get("/chats/%d" % row["id"]).get_data(as_text=True)
    assert '<option value="worker2">Отвечает: Партнёр Тестов</option>' in page and '<option value="owner">Отвечает: Владелец</option>' in page
    post(client, "/chats/%d/assign" % row["id"], tok, responsible="worker2")
    assert chats.get_chat(conn, row["id"])["responsible"] == "worker2"
    [entry] = [a for a in store.list_audit(conn, 10) if a["action"] == "chat_assign"]
    assert entry["detail"] == {"chat": row["id"], "from": "", "to": "worker2"} and entry["author"] == "owner"
    assert ">Партнёр Тестов</span>" in client.get("/chats").get_data(as_text=True)                       # the list shows who answers
    post(client, "/chats/%d/assign" % row["id"], tok, responsible="worker2")                       # the same again: nothing new in the journal
    post(client, "/chats/%d/assign" % row["id"], tok, responsible="stranger")                    # not one who answers chats: refused
    assert chats.get_chat(conn, row["id"])["responsible"] == "worker2"
    post(client, "/chats/%d/assign" % row["id"], tok, responsible="")
    assert chats.get_chat(conn, row["id"])["responsible"] == ""
    assert [a["detail"]["to"] for a in store.list_audit(conn, 10) if a["action"] == "chat_assign"] == ["", "worker2"]
    history = client.get("/history").get_data(as_text=True)
    assert "Чат Ч-%d: ответственный — worker2" % row["id"] in history and "Чат Ч-%d: ответственный снят" % row["id"] in history
    viewer = as_role(app, conn, "viewer")
    assert viewer.post("/chats/%d/assign" % row["id"], data={"csrf": "tok", "responsible": "worker2"}).status_code == 403


def test_the_clock_of_the_client_who_waits_for_an_answer(tmp_path):
    from datetime import datetime
    app, client, tok, conn, account = setup(tmp_path)                                        # the panel clock: 2026-10-13 12:00 Moscow
    tanya, ivan = chat_of(conn, "Татьяна"), chat_of(conn, "Иван")

    def say(chat_row, at, direction="in", kind="text"):                                    # the clock reads the messages (wait_starts)
        ts = int(datetime.fromisoformat(at).timestamp())
        conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, kind, text) VALUES (?,?,?,?,?,?,'x')",
                     (chat_row, "w-%s-%s" % (at, direction), at, ts, direction, kind))
        conn.execute("UPDATE avito_chat SET last_dir = ?, last_at = ? WHERE id = ?", (direction, at, chat_row))
    for row in (tanya, ivan):
        conn.execute("DELETE FROM avito_message WHERE chat_row = ?", (row["id"],))
    say(tanya["id"], "2026-10-13T11:35:00")
    say(tanya["id"], "2026-10-13T11:36:00", "out", "system")                                  # Avito's auto-answer is no answer
    say(ivan["id"], "2026-10-13T09:50:00")
    store.set_state(conn, "chats_baseline_%d" % account, "2026-10-13T09:00:00")              # the account was first read at 09:00
    conn.commit()
    listing = client.get("/chats").get_data(as_text=True)
    assert "badge--warn badge--dot\" title=\"Клиент ждёт ответа\">ждёт 25 мин" in listing
    assert "badge--err badge--dot\" title=\"Клиент ждёт ответа\">ждёт 2 ч 10 мин" in listing           # an hour and more: red
    assert "Клиент ждёт ответа 25 мин" in client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    say(tanya["id"], "2026-10-13T11:50:00", "out")                                            # we answered: nobody waits
    conn.execute("UPDATE avito_chat SET status = 'no_reply' WHERE id = ?", (ivan["id"],))         # «не требует ответа»: no clock
    conn.commit()
    assert "ждёт" not in client.get("/chats?f=noreply").get_data(as_text=True).split('class="card chats-list"')[1].split("</section>")[0]
    store.set_state(conn, "chats_baseline_%d" % account, "2026-10-13T10:00:00")              # Иван's chat is older than the first read: the past
    conn.execute("UPDATE avito_chat SET status = 'open' WHERE id = ?", (ivan["id"],))
    conn.commit()
    assert "ждёт" not in client.get("/chats").get_data(as_text=True).split('class="card chats-list"')[1].split("</section>")[0]
    assert chats.waiting_minutes({"status": "open", "last_dir": "in", "last_at": "2026-10-13T12:30:00"}, datetime(2026, 10, 13, 12, 0)) == 0
    assert chats.waiting_minutes({"status": "open", "last_dir": "in", "last_at": "вчера"}, datetime(2026, 10, 13, 12, 0)) is None
    answered = {"status": "open", "last_dir": "in", "last_at": "2026-10-13T11:00:00", "wait_since": None}                     # the messages say: answered
    assert chats.waiting_minutes(answered, datetime(2026, 10, 13, 12, 0)) is None
    assert chats.waiting_minutes(dict(answered, last_dir="out", wait_since="2026-10-13T11:30:00"), datetime(2026, 10, 13, 12, 0)) == 30
    assert chats.wait_starts(conn, [tanya["id"], ivan["id"]]) == {tanya["id"]: None, ivan["id"]: "2026-10-13T09:50:00"}


# --- 2: a position from the chat into its order --------------------------------------------------------------------------------------

def test_a_position_is_found_and_added_to_the_order_from_the_chat(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    tanya, ivan = chat_of(conn, "Татьяна"), chat_of(conn, "Иван")
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    deal = page.split('class="card chats-deal"')[1]
    assert 'data-item-find="/orders/items/find?chat=%d"' % tanya["id"] in deal and 'action="/chats/%d/items/add"' % tanya["id"] in deal and "Добавить в заказ" in deal
    found = client.get("/orders/items/find?q=205/55").get_json()["items"]
    assert found
    before = len(orders.get_card(conn, tanya["order_id"])["items"])
    r = post(client, "/chats/%d/items/add" % tanya["id"], tok, goods_id=found[0]["goods_id"], qty="4", query=found[0]["name"])
    assert r.headers["Location"].endswith("/chats/%d" % tanya["id"])
    items = orders.get_card(conn, tanya["order_id"])["items"]
    assert len(items) == before + 1 and items[-1]["goods_id"] == found[0]["goods_id"] and items[-1]["qty"] == 4
    assert "В заказ №%d добавлено" % tanya["order_id"] in client.get(r.headers["Location"]).get_data(as_text=True)
    post(client, "/chats/%d/items/add" % tanya["id"], tok, goods_id=found[0]["goods_id"], qty="2", sale_unit="4990")
    assert orders.get_card(conn, tanya["order_id"])["items"][-1]["sale_unit"] == 4990                 # the price typed by hand
    r = post(client, "/chats/%d/items/add" % tanya["id"], tok, goods_id="", query="что-то", qty="4")
    assert "Выберите позицию" in client.get(r.headers["Location"]).get_data(as_text=True)
    r = post(client, "/chats/%d/items/add" % ivan["id"], tok, goods_id=found[0]["goods_id"], qty="4")   # a chat without an order
    assert "У чата нет заказа" in client.get(r.headers["Location"]).get_data(as_text=True)
    assert len(orders.get_card(conn, tanya["order_id"])["items"]) == before + 2


# --- 7: the client's photos and calls ------------------------------------------------------------------------------------------------

def _photo(message_id, created, sizes, direction="in"):
    return {"id": message_id, "author_id": 2, "created": created, "type": "image", "direction": direction, "isRead": False, "read": 0,
            "content": {"image": {"sizes": sizes}}}


def _call(message_id, created, status="missed"):
    return {"id": message_id, "author_id": 2, "created": created, "type": "call", "direction": "in", "isRead": False, "read": 0,
            "content": {"call": {"status": status, "target_user_id": 1}}}


def test_a_photo_of_the_client_shows_in_the_conversation_and_only_from_avitos_storage(tmp_path):
    from test_chats_sync import FakeClient, TS, chat, db, msg
    conn, account = db(tmp_path)
    good = {"140x105": "https://00.img.avito.st/140x105/1.jpg", "1280x960": "https://00.img.avito.st/1280x960/1.jpg", "640x480": "https://00.img.avito.st/640x480/1.jpg"}
    client = FakeClient([[chat("c1", text="")]], {"c1": [msg("m1", "Вот такие"), _photo("p1", TS + 1, good),
                                                        _photo("p2", TS + 2, {"1280x960": "javascript:alert(1)", "640x480": "https://evil.example/x.jpg"})]})
    chats.sync_account(conn, client, account, NOW)
    row = chats.list_chats(conn)[0]
    shown = {m["message_id"]: (m["media"], m["text"]) for m in chats.list_messages(conn, row["id"])}
    assert shown["p1"] == ("https://00.img.avito.st/1280x960/1.jpg", "") and shown["p2"] == ("", "")     # the biggest; nothing but Avito's own links
    assert chats.photo_url({"type": "image", "content": {}}) == ""
    assert not conn.execute("SELECT 1 FROM avito_message WHERE text LIKE '%avito.st%'").fetchone()


def test_the_page_shows_the_photo_and_the_call(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    row = chat_of(conn, "Татьяна")
    for mid, kind, text, media in (("p1", "image", "", "https://00.img.avito.st/1280x960/1.jpg"), ("p2", "image", "", ""),
                                   ("k1", "call", "Звонок через Авито: пропущен", "")):
        conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, kind, text, is_read, media) VALUES (?,?,?,?,?,?,?,1,?)",
                     (row["id"], mid, NOW, 1_791_900_500, "in", kind, text, media))
    conn.commit()
    page = client.get("/chats/%d" % row["id"]).get_data(as_text=True)
    assert '<img class="msg-photo" src="https://00.img.avito.st/1280x960/1.jpg" alt="Фото клиента"' in page and 'referrerpolicy="no-referrer"' in page
    assert "Фото (Авито не отдал ссылку)" in page and "📞 Клиент звонил: пропущен" in page


def test_a_call_of_the_client_is_a_line_and_an_alert_in_the_open_order(tmp_path):
    from test_m2_tails import _chat_order
    from tires_sync import order_alerts
    from test_chats_sync import TS
    conn, account, client, chat_row, card, chat, msg = _chat_order(tmp_path)
    client.chat_pages = [[chat("c1", updated=TS + 10, text="")]]
    client.messages["c1"] = [msg("m1", "Здравствуйте"), _call("k1", TS + 10)]
    chats.sync_account(conn, client, account, "2026-10-13T12:05:00")
    events = [(e["text"], e["level"]) for e in orders.list_events(conn, card) if e["text"].startswith("Клиент звонил")]
    assert events == [("Клиент звонил: Звонок через Авито: пропущен", "yellow")]
    assert "Клиент звонил: Звонок через Авито: пропущен" in [a["text"] for a in order_alerts.page_rows(conn, "owner")]
    chats.sync_account(conn, client, account, "2026-10-13T12:06:00")                            # read again: no second record of the same call
    assert len([e for e in orders.list_events(conn, card) if e["text"].startswith("Клиент звонил")]) == 1


# --- the guard: sending to the client is not changed by this lane ----------------------------------------------------------------------

SEND_CODE = {"send_reply": "67a2e20061cd2c7d", "send_image": "246067c5cbb76d35", "_not_delivered": "a8956eb6a6144a6f",
             "_definitely_not_sent": "54fccf3440320712", "prepare_image": "49124b261ee79487", "_ours_at_avito": "b314973f9e146a88",
             "_resume_interrupted": "6dafc631f5931af4", "_avito_said": "c548573094ccd636", "_refused_hint": "43286dbe7edeb825",
             "_send_record": "31f9efceaa31c6ea", "_quotes": "fd5e8049fdeab435", "_deliver": "cffc525c2f695af0",
             "_mark": "d70d75c8ce7b5088", "_alert_not_sent": "74efc5b821020db2", "_retry_refused": "fb82d89ea741b009",
             "_check_unknown": "3aed91f55d552351", "settle_sends": "c527b46d7c743787", "_lost_is_there": "a5c7e7ea4c1266ec",
             "_claim_by_sync": "12aa42afe40970a8", "_same_text": "3f655b3cb9d97bfe", "_since": "6194713c717a8ee2",
             "_others": "2a2ad9a520585a3f", "_retry_late": "322e8e40b2498e09", "_stopped": "252620f625d49f18", "_halt": "bba4642d8e4cd798",
             "_close_task": "d2a8bb9dfb018951",
             "_inbox_done": "0d5006e110a24a0b", "_again_said": "1f6b922254a554a8", "_failed_is_there": "f9de17b3a9f270aa",
             "_inbox_close": "480dcd9ae2041726"}       # run8, lane stuck-sending: a mark «sending» older than 5 minutes is resolved (tests/test_stuck_sending.py)
# 2026-10-24 reliability, lane rel-core (RED, reviewed): send_reply asks the cabinet's id before the send; its failure is «failed», not «unknown»
# 2026-10-24 open1, lane chat-402 (RED, reviewed): a refusal shows and keeps Avito's own answer (tests/test_chat_send_refusal.py); what is repeated is unchanged
# 2026-10-25 tails1, lane tails-chats (reviewed): Ч2 a failure of our own is not «нет ответа»; Ч3 a pause of Avito named, 400/404 «повтор не поможет»; Ч4 an escaped quote of ours is found (tests/test_tails_chats.py); what is repeated is unchanged
# 2026-10-25 tails1, lane tails-orders (RED, reviewed): «Срок резерва истекает» (token reserve-…) writes no second record in the order's journal;
# what is sent and how is unchanged (tests/test_order_reserve.py::test_the_reserve_message_is_one_record_of_the_journal_not_two)
# 2026-10-25 match3, lane misc-tails (reviewed): Q3 _ours_at_avito — the cabinet's id asked in its own try; «нет Id кабинета», 429/5xx, the network
# at the check of an interrupted send are «попробуйте через минуту» (the mark stays), a refusal stays «unknown» (tests/test_stuck_sending.py q3)
# 2026-10-25 send-retry, lane A (RED, reviewed; the owner: «проверка чата и автоповтор при коде 402»): send_reply/send_image share _deliver/_mark;
# a 402 of the send is «retry» (settle_sends sends it again, 3 times, then «failed» + «Входящие»), an unknown one is looked up in the chat
# («sent» / «lost», sent again by «Отправить снова» only / a verdict), «sent» clears `error`; _not_delivered says «не отправлено» for a 402
# (tests/test_send_settle.py). The new functions are guarded too. Round 2 (the owner 25.10): a lost text is sent again by the sync itself, once
# (again_at), under the same mark; a 402 ends 30 minutes after the first refusal, stops on another message of ours; «Отправить снова» looks first.
# Round 3: our own sends before this one (a photo of the same press) are not «another message» (_others); a late retry looks first.
# Round 4: another send started after the first refusal (a retelling by hand, refused too) stops the retry and the send again.
# 2026-10-25 tails3, lane send-retry2 (RED, reviewed): send_reply/send_image keep the first start (orig_at); _check_unknown, _retry_refused and the
# button look at the chat from it (M1); _others counts another send by its first start (L1); a send of the sync stops when its task was skipped,
# ticked off or is gone or the order is closed (_stopped/_halt, L4); a task whose message the sync delivered is done (_close_task); settle_sends
# reads orig_at (tests/test_send_settle2.py).
# 2026-10-25 tails5, lane chats-tails (reviewed): _resume_interrupted looks at the chat from the first start (orig_at); _check_unknown and
# _retry_refused close the caller's line of «Входящие» once the sync settled the send as sent (_inbox_done); _check_unknown says no «напишите
# вручную» for a skipped task or a closed order it cannot check, and who sent it again (_again_said); what is sent and when is unchanged
# (tests/test_send_settle3.py).
# 2026-10-25 tails6, lane no-copy (RED, reviewed): a new press of a «failed» send (send_reply/send_image) looks at the chat from its first
# start first (_failed_is_there): the same text of ours there is «sent», nothing goes; another message of ours stops it («проверьте чат»);
# an unreadable chat does not hold it back; either outcome closes the open lines of «Входящие» with a text to copy, the sync's alerts too.
# _check_unknown's cannot() closes the caller's line of «Входящие» for a skipped task or a closed order (_inbox_close). What is sent
# otherwise is unchanged (tests/test_send_settle4.py).
# 2026-10-27 tails-answers (RED, reviewed): a new press of a send that goes (send_reply/send_image: a failed one, an interrupted or a lost one
# found) answers the open lines of «Входящие» about it, the sync's alerts too («дошло»); no line about a send is written once it went
# (_alert_not_sent, order_manager._tell_arrived, order_reserve: NOT_SENT_MEANWHILE); a send the sync stopped for a skipped task or a closed
# order answers its open lines «писать клиенту не нужно» (_halt); the sync's delivery answers the sync's alerts of an earlier try of the
# same mark too (_inbox_done, review D1). What is sent is unchanged (tests/test_send_settle5.py).


def test_the_functions_that_send_to_the_client_are_as_they_were():
    """The lane 5B changes how chats are read and shown, never how a reply or a photo leaves for the client: a change of these functions
    is a separate RED change with its own review (then this guard is updated with it)."""
    import hashlib
    import inspect
    now = {name: hashlib.sha256(inspect.getsource(getattr(chats, name)).encode()).hexdigest()[:16] for name in SEND_CODE}
    assert now == SEND_CODE
    sent = {"id": "m-out", "created": 1_791_900_600, "direction": "out", "type": "text", "content": {"text": "Добрый день"}}
    assert chats.message_text(sent) == "Добрый день"                                   # what send_reply stores of its own message is the same text


def test_a_call_in_a_chat_without_an_order_is_a_row_until_we_write(tmp_path):
    from test_chats_sync import FakeClient, TS, chat, db, msg
    from tires_sync import order_alerts
    conn, account = db(tmp_path)
    conn.execute("UPDATE account SET chats_enabled = 1 WHERE id = ?", (account,))
    conn.commit()
    client = FakeClient([[chat("c1", text="Здравствуйте")]], {"c1": [msg("m1", "Здравствуйте", read=1)]})
    chats.sync_account(conn, client, account, "2026-10-13T10:00:00")                  # the first read: the past
    store.set_state(conn, "chats_baseline_%d" % account, "2026-10-13T10:00:00")
    client.chat_pages = [[chat("c1", updated=TS + 10, text="")]]
    client.messages["c1"] = [msg("m1", "Здравствуйте", read=1), _call("k1", TS + 10)]
    chats.sync_account(conn, client, account, "2026-10-13T16:00:00")
    conn.execute("UPDATE avito_message SET at = '2026-10-13T15:00:00' WHERE message_id = 'k1'")
    conn.commit()
    [row] = order_alerts.chat_rows(conn)
    assert row["text"] == "Клиент звонил: пропущен" and row["level"] == "yellow" and row["chat_row"] == chats.list_chats(conn)[0]["id"]
    client.chat_pages = [[chat("c1", updated=TS + 20, text="Перезвоню", direction="out")]]
    client.messages["c1"] = client.messages["c1"] + [msg("m2", "Перезвоню", direction="out", created=TS + 20)]
    chats.sync_account(conn, client, account, "2026-10-13T16:05:00")                  # we wrote back: the call is answered
    assert order_alerts.chat_rows(conn) == []


# --- review round 1 ----------------------------------------------------------------------------------------------------------------

def test_a_search_finds_closed_chats_too_and_is_kept_after_an_action(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    tanya = chat_of(conn, "Татьяна")
    chats.set_status(conn, tanya["id"], "closed", NOW)
    assert _listed(client, "татьяна") == ["Татьяна"]                                             # the default filter hides closed chats, a search does not
    page = client.get("/chats/%d?q=%%D1%%82%%D0%%B0%%D1%%82%%D1%%8C%%D1%%8F%%D0%%BD%%D0%%B0" % tanya["id"]).get_data(as_text=True)
    assert 'action="/chats/%d" class="chats-search"' % tanya["id"] in page and '<input type="hidden" name="q" value="татьяна">' in page
    r = post(client, "/chats/%d/status" % tanya["id"], tok, status="open", q="татьяна")
    assert r.headers["Location"].endswith("/chats/%d?q=%%D1%%82%%D0%%B0%%D1%%82%%D1%%8C%%D1%%8F%%D0%%BD%%D0%%B0" % tanya["id"])


def test_a_position_goes_only_to_the_order_the_page_showed(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    tanya = chat_of(conn, "Татьяна")
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert '<input type="hidden" name="order_id" value="%d">' % tanya["order_id"] in page
    found = client.get("/orders/items/find?q=205/55").get_json()["items"]
    before = len(orders.get_card(conn, tanya["order_id"])["items"])
    r = post(client, "/chats/%d/items/add" % tanya["id"], tok, goods_id=found[0]["goods_id"], qty="4", order_id=str(tanya["order_id"] + 100))
    assert "уже привязан к другому заказу" in client.get(r.headers["Location"]).get_data(as_text=True)
    assert len(orders.get_card(conn, tanya["order_id"])["items"]) == before


def test_the_live_update_keeps_the_fold_buttons_and_the_position_picker_working():
    """The open page redraws the chat's head and the order column when a message comes (app.js, «Чаты» live update): the fold buttons
    are found by a click on the document, their state comes back after the redraw, and «Подобрать позицию» is carried over as it is."""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static" / "app.js").read_text(encoding="utf-8")
    fold = js[js.index("the order column folds away"):js.index("an open page asks for fresh messages")]
    assert 'document.addEventListener("click"' in fold and 'closest("[data-deal-fold]")' in fold and 'addEventListener("chats:refilled", show)' in fold
    assert ".addEventListener(" not in fold.replace('document.addEventListener("click"', "").replace('document.addEventListener("chats:refilled"', "")
    refill = js[js.index("an open page asks for fresh messages"):js.index("// 11) Заказ: поиск позиции")]
    assert 'querySelector(".chat-pick")' in refill and "replaceChild(picker, slot)" in refill and 'new Event("chats:refilled")' in refill


def test_a_chat_with_only_the_stand_in_of_its_preview_waits_by_its_preview(tmp_path):
    """backfill_previews keeps a stand-in (message_id 'preview', ts 0) for a chat whose history is not read: the preview is all there is."""
    from datetime import datetime
    app, client, tok, conn, account = setup(tmp_path)
    tanya = chat_of(conn, "Татьяна")
    conn.execute("DELETE FROM avito_message WHERE chat_row = ?", (tanya["id"],))
    conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, kind, text) VALUES (?, 'preview', '2026-10-13T11:35:00', 0, 'in', "
                 "'text', 'Есть?')", (tanya["id"],))
    conn.execute("UPDATE avito_chat SET last_dir = 'in', last_at = '2026-10-13T11:35:00' WHERE id = ?", (tanya["id"],))
    store.set_state(conn, "chats_baseline_%d" % account, "2026-10-13T09:00:00")
    conn.commit()
    assert chats.wait_starts(conn, [tanya["id"]]) == {}
    assert "Клиент ждёт ответа 25 мин" in client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert chats.waiting_minutes(chats.get_chat(conn, tanya["id"]), datetime(2026, 10, 13, 12, 0)) == 25
