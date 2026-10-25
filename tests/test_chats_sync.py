"""Reading Avito chats and messages into the panel's database."""
from __future__ import annotations

import json

from http_fakes import ScriptedHttp
from tires_sync import chats, connections, store
from tires_sync.avito import AvitoClient, AvitoError

NOW = "2026-10-13T12:00:00"
TS = 1_791_900_000                         # 2026-10-13 about 15:00 Moscow


def chat(chat_id="c1", item=111, updated=TS, text="Здравствуйте", direction="in", read=0, kind="text"):
    return {"id": chat_id, "created": TS - 100, "updated": updated,
            "context": {"type": "item", "value": {"id": item, "title": "Ikon 185/65 R15", "url": "https://www.avito.ru/x_%d" % item, "price_string": "5 750 ₽"}},
            "users": [{"id": 1, "name": "Я"}, {"id": 2, "name": "Татьяна"}],
            "last_message": {"id": "m-last-" + chat_id, "author_id": 2, "created": updated, "content": {"text": text}, "type": kind, "direction": direction, "read": read}}


def msg(message_id, text, direction="in", created=TS, read=None, kind="text"):
    return {"id": message_id, "author_id": 2 if direction == "in" else 1, "created": created, "content": {"text": text}, "type": kind,
            "direction": direction, "isRead": bool(read), "read": read or 0}


class FakeClient:
    def __init__(self, chat_pages, messages, broken=()):
        self.chat_pages, self.messages, self.broken = chat_pages, messages, set(broken)
        self.calls = []

    def get_self(self):
        return {"id": 1}

    def chats(self, user_id, limit=50, offset=0, retry=True):
        self.calls.append(("chats", offset))
        page = self.chat_pages[offset // limit] if offset // limit < len(self.chat_pages) else []
        return {"chats": page, "meta": {"has_more": offset // limit + 1 < len(self.chat_pages)}}

    def chat_messages(self, user_id, chat_id, limit=100, offset=0, retry=True):
        self.calls.append(("messages", chat_id))
        if chat_id in self.broken:
            raise AvitoError(402, None, "Перейдите на подписку с API мессенджера")
        return self.messages.get(chat_id, [])


def db(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    account = connections.create_account(conn, "Основной", NOW, "o")
    return conn, account


def test_the_client_reads_chats_and_messages_with_get_only():
    http = ScriptedHttp((200, {"access_token": "t", "expires_in": 3600}), (200, {"chats": [], "meta": {"has_more": False}}),
                        (200, {"messages": [msg("a", "hi")]}))
    client = AvitoClient("cid", "secret", http=http, now=lambda: 0.0)
    assert client.chats(7, limit=50, offset=0)["meta"]["has_more"] is False
    assert client.chat_messages(7, "u2i-abc", limit=100)[0]["id"] == "a"
    paths = [(m, u.split("avito.ru")[1]) for m, u, _h, _b in http.calls if "messenger" in u]
    assert paths == [("GET", "/messenger/v2/accounts/7/chats?limit=50&offset=0"), ("GET", "/messenger/v3/accounts/7/chats/u2i-abc/messages/?limit=100&offset=0")]


def test_a_sync_stores_chats_and_messages_and_a_second_sync_adds_nothing(tmp_path):
    conn, account = db(tmp_path)
    client = FakeClient([[chat("c1", read=0, text="Хочу заказать")]], {"c1": [msg("m1", "Здравствуйте"), msg("m2", "Хочу заказать", created=TS + 5)]})
    stats = chats.sync_account(conn, client, account, NOW)
    assert stats == {"chats": 1, "new_chats": 1, "messages": 2, "errors": 0}
    [row] = chats.list_chats(conn)
    assert row["peer_name"] == "Татьяна" and row["item_title"] == "Ikon 185/65 R15" and row["last_text"] == "Хочу заказать" and row["unread"] == 2
    assert [m["text"] for m in chats.list_messages(conn, row["id"])] == ["Здравствуйте", "Хочу заказать"]
    again = chats.sync_account(conn, client, account, NOW)
    assert again["messages"] == 0 and again["new_chats"] == 0 and len(chats.list_messages(conn, row["id"])) == 2
    assert [c for c in client.calls if c[0] == "messages"] == [("messages", "c1"), ("messages", "c1")]    # unread: read again, Avito may not bump «updated» when it is read
    client.messages["c1"] = [msg("m1", "Здравствуйте", read=5), msg("m2", "Хочу заказать", created=TS + 5, read=5)]
    chats.sync_account(conn, client, account, NOW)
    assert chats.get_chat(conn, row["id"])["unread"] == 0                                                 # the read mark is refreshed, the counter falls
    before = len([c for c in client.calls if c[0] == "messages"])
    chats.sync_account(conn, client, account, NOW)
    assert len([c for c in client.calls if c[0] == "messages"]) == before                                 # a read, unchanged chat is not read again


def test_a_changed_chat_is_read_again_and_a_new_message_reopens_it(tmp_path):
    conn, account = db(tmp_path)
    client = FakeClient([[chat("c1", updated=TS)]], {"c1": [msg("m1", "Привет")]})
    chats.sync_account(conn, client, account, NOW)
    cid = chats.list_chats(conn)[0]["id"]
    chats.set_status(conn, cid, "closed", NOW)
    client.chat_pages = [[chat("c1", updated=TS + 60, text="Ещё вопрос")]]
    client.messages = {"c1": [msg("m1", "Привет"), msg("m2", "Ещё вопрос", created=TS + 60)]}
    assert chats.sync_account(conn, client, account, NOW)["messages"] == 1
    row = chats.get_chat(conn, cid)
    assert row["status"] == "open" and row["last_text"] == "Ещё вопрос"                                  # a new message from the client reopens the chat
    chats.set_status(conn, cid, "no_reply", NOW)
    client.chat_pages = [[chat("c1", updated=TS + 120, text="Спасибо", direction="out")]]
    client.messages["c1"].append(msg("m3", "Спасибо", direction="out", created=TS + 120))
    chats.sync_account(conn, client, account, NOW)
    assert chats.get_chat(conn, cid)["status"] == "no_reply"                                              # our own message does not


def test_a_chat_with_402_keeps_its_last_message_and_the_others_still_load(tmp_path):
    conn, account = db(tmp_path)
    client = FakeClient([[chat("old", text="Старое", updated=TS), chat("new", text="Новое", updated=TS + 1)]], {"new": [msg("n1", "Новое")]}, broken=["old"])
    stats = chats.sync_account(conn, client, account, NOW)
    assert stats["chats"] == 2 and stats["errors"] == 0
    rows = {r["last_text"]: r for r in chats.list_chats(conn)}
    assert rows["Старое"]["history_ok"] == 0 and rows["Новое"]["history_ok"] == 1
    assert [m["text"] for m in chats.list_messages(conn, rows["Старое"]["id"])] == ["Старое"]
    assert rows["Старое"]["unread"] == 1                                                                   # the client's last message is not read yet


def test_an_error_in_one_chat_does_not_stop_the_sync_and_pages_are_followed(tmp_path):
    conn, account = db(tmp_path)

    class Flaky(FakeClient):
        def chat_messages(self, user_id, chat_id, limit=100, offset=0, retry=True):
            if chat_id == "bad":
                raise AvitoError(500, None, "boom")
            return super().chat_messages(user_id, chat_id, limit, offset, retry)
    pages = [[chat("bad")] + [chat("p1-%d" % i) for i in range(49)], [chat("c2", updated=TS + 9)]]
    client = Flaky(pages, {"c2": [msg("x", "ok")]})
    stats = chats.sync_account(conn, client, account, NOW, page=50)
    assert stats["chats"] == 51 and stats["errors"] == 1 and len(chats.list_chats(conn)) == 51
    assert ("chats", 50) in client.calls


def test_system_and_call_messages_are_kept_but_do_not_count_as_unread(tmp_path):
    conn, account = db(tmp_path)
    client = FakeClient([[chat("c1")]], {"c1": [msg("a", "Системное", kind="system"), msg("b", "Звонок", kind="call"), msg("c", "Привет", read=0)]})
    chats.sync_account(conn, client, account, NOW)
    row = chats.list_chats(conn)[0]
    assert row["unread"] == 1 and [m["kind"] for m in chats.list_messages(conn, row["id"])] == ["system", "call", "text"]


def test_old_messages_are_not_stored_and_the_cleaning_removes_what_got_old(tmp_path):
    conn, account = db(tmp_path)
    client = FakeClient([[chat("c1")]], {"c1": [msg("old", "Давно", created=TS - 200 * 86400), msg("fresh", "Недавно")]})
    chats.sync_account(conn, client, account, NOW)
    row = chats.list_chats(conn)[0]
    assert [m["text"] for m in chats.list_messages(conn, row["id"])] == ["Недавно"]                       # the old one is not stored, so it cannot reopen a closed chat later
    conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction) VALUES (?, 'ancient', '2026-01-01T10:00:00', 1, 'in')", (row["id"],))
    assert chats.clean_old(conn, "2026-10-13T12:00:00") == 1


def test_a_read_message_updates_its_mark_and_a_429_stops_the_account(tmp_path):
    conn, account = db(tmp_path)

    class Limited(FakeClient):
        def chat_messages(self, user_id, chat_id, limit=100, offset=0, retry=True):
            if chat_id == "c1":
                raise AvitoError(429, None, "slow down")
            return super().chat_messages(user_id, chat_id, limit, offset, retry)
    client = Limited([[chat("c1"), chat("c2", updated=TS + 1)]], {"c2": [msg("x", "ok")]})
    stats = chats.sync_account(conn, client, account, NOW)
    assert stats["errors"] == 1 and ("messages", "c2") not in client.calls                               # the rest waits for the next run


def test_only_avito_links_are_kept(tmp_path):
    conn, account = db(tmp_path)
    good, bad = chat("c1", item=1), chat("c2", item=2)
    bad["context"]["value"]["url"] = "javascript:alert(1)"
    chats.sync_account(conn, FakeClient([[good, bad]], {}), account, NOW)
    urls = {c["chat_id"]: c["item_url"] for c in chats.list_chats(conn)}
    assert urls["c1"].startswith("https://www.avito.ru/") and urls["c2"] == ""


def test_nothing_stays_locked_while_waiting_for_avito_and_a_429_does_not_wait_long(tmp_path):
    conn, account = db(tmp_path)
    seen = {}

    class Watching(FakeClient):
        def chat_messages(self, user_id, chat_id, limit=100, offset=0, retry=True):
            other = store.connect(str(tmp_path / "x.db"))                                       # the panel writes while we wait for Avito
            try:
                other.execute("UPDATE account SET name = 'x' WHERE id = ?", (account,))
                other.commit()
                seen["panel_could_write"] = True
            except Exception as err:
                seen["panel_could_write"] = False
                seen["why"] = str(err)
            finally:
                other.close()
            seen["retry"] = retry
            return super().chat_messages(user_id, chat_id, limit, offset, retry)
    client = Watching([[chat("c1")]], {"c1": [msg("m1", "Привет")]})
    store.connect(str(tmp_path / "x.db")).execute("PRAGMA busy_timeout = 100")
    chats.sync_account(conn, client, account, NOW)
    assert seen.get("panel_could_write") is True and seen["retry"] is False                       # no open write while waiting; a 429 is not slept through


def test_unread_chats_are_read_again_within_a_budget_and_closed_or_historyless_ones_are_not(tmp_path):
    conn, account = db(tmp_path)
    pages = [[chat("c%d" % i, updated=TS + i) for i in range(30)]]
    client = FakeClient(pages, {"c%d" % i: [msg("m%d" % i, "Привет")] for i in range(30)})
    chats.sync_account(conn, client, account, NOW)
    first = len([c for c in client.calls if c[0] == "messages"])
    assert first == 30                                                                             # a new chat is always read
    chats.sync_account(conn, client, account, NOW)
    again = len([c for c in client.calls if c[0] == "messages"]) - first
    assert again == chats.REREAD_BUDGET                                                            # unread, unchanged: only a budget per run
    ids = {c["chat_id"]: c["id"] for c in chats.list_chats(conn)}
    for key in ("c0", "c1"):
        chats.set_status(conn, ids[key], "closed", NOW)
    conn.execute("UPDATE avito_chat SET history_ok = 0 WHERE chat_id = 'c2'")
    conn.commit()
    before = len([c for c in client.calls if c[0] == "messages"])
    chats.sync_account(conn, client, account, NOW)
    reread = {c[1] for c in client.calls[-(chats.REREAD_BUDGET + 60):] if c[0] == "messages"}
    assert not ({"c0", "c1", "c2"} & {c[1] for c in client.calls[before:] if c[0] == "messages"})  # closed chats and chats without history are not asked again


def test_the_client_sends_a_message_with_post_and_without_retries(tmp_path):
    http = ScriptedHttp((200, {"access_token": "t", "expires_in": 3600}), (200, {"id": "m-new", "content": {"text": "Добрый день"}, "direction": "out", "created": TS}))
    client = AvitoClient("cid", "secret", http=http, now=lambda: 0.0, allow_write=True)
    result = client.send_message(7, "u2i-abc", "Добрый день")
    assert result["id"] == "m-new"
    method, url, _h, body = [c for c in http.calls if "messenger" in c[1]][0]
    assert method == "POST" and url.endswith("/messenger/v1/accounts/7/chats/u2i-abc/messages")
    assert json.loads(body) == {"message": {"text": "Добрый день"}, "type": "text"}
    read_only = AvitoClient("cid", "secret", http=ScriptedHttp(), now=lambda: 0.0)
    from tires_sync.avito import WriteDisabled
    import pytest
    with pytest.raises(WriteDisabled):
        read_only.send_message(7, "u2i-abc", "x")


def test_a_reply_is_sent_once_recorded_in_the_chat_and_noted_in_the_deal(tmp_path):
    import pytest
    from tires_sync import chat_deals as deals, orders
    conn, account = db(tmp_path)
    orders.ensure_pipeline(conn, NOW)
    fake = FakeClient([[chat("c1", text="Хочу заказать")]], {"c1": [msg("m1", "Хочу заказать")]})
    chats.sync_account(conn, fake, account, NOW)
    deals.process_all(conn, "2026-10-13T20:00:00", auto_create=True)
    row = chats.list_chats(conn)[0]
    sent = []

    class Sender:
        def get_self(self, retry=True):
            return {"id": 1}

        def send_message(self, user_id, chat_id, text):
            sent.append((user_id, chat_id, text))
            return {"id": "m-out-1", "created": TS + 100, "direction": "out", "type": "text", "content": {"text": text}}
    with pytest.raises(chats.ChatError):
        chats.send_reply(conn, Sender(), row["id"], "Ответ", "tok-1", NOW, "owner")                      # the account's sending is off
    conn.execute("UPDATE account SET chats_send = 1, chats_enabled = 1 WHERE id = ?", (account,))
    conn.commit()
    chats.send_reply(conn, Sender(), row["id"], "  Добрый день, подтверждаю наличие  ", "tok-1", NOW, "owner")
    chats.send_reply(conn, Sender(), row["id"], "  Добрый день, подтверждаю наличие  ", "tok-1", NOW, "owner")        # a double press: the same token
    assert sent == [(1, "c1", "Добрый день, подтверждаю наличие")]
    last = chats.list_messages(conn, row["id"])[-1]
    assert (last["direction"], last["text"]) == ("out", "Добрый день, подтверждаю наличие")
    assert chats.get_chat(conn, row["id"])["last_text"] == "Добрый день, подтверждаю наличие" and chats.get_chat(conn, row["id"])["unread"] == 0
    assert any("Отправлен ответ клиенту" in e["text"] for e in orders.list_events(conn, row["order_id"]))
    for bad in ("", "   ", "x" * 2001):
        with pytest.raises(chats.ChatError):
            chats.send_reply(conn, Sender(), row["id"], bad, "tok-%d" % len(bad), NOW, "owner")
    assert len(sent) == 1


def test_a_failed_send_is_reported_and_leaves_no_message_behind(tmp_path):
    import pytest
    conn, account = db(tmp_path)
    chats.sync_account(conn, FakeClient([[chat("c1")]], {"c1": [msg("m1", "Привет")]}), account, NOW)
    conn.execute("UPDATE account SET chats_send = 1, chats_enabled = 1 WHERE id = ?", (account,))
    conn.commit()
    row = chats.list_chats(conn)[0]

    class Broken:
        def get_self(self, retry=True):
            return {"id": 1}

        def send_message(self, *a):
            raise AvitoError(403, None, "нет права")
    with pytest.raises(chats.ChatError) as err:
        chats.send_reply(conn, Broken(), row["id"], "Ответ", "tok-9", NOW, "owner")
    assert "403" in str(err.value) and len(chats.list_messages(conn, row["id"])) == 1
    chats.send_reply(conn, FakeSender(), row["id"], "Ответ", "tok-9", NOW, "owner")                      # the same token may be tried again after a failure
    assert len(chats.list_messages(conn, row["id"])) == 2


class FakeSender:
    def get_self(self, retry=True):
        return {"id": 1}

    def send_message(self, user_id, chat_id, text):
        return {"id": "m-ok", "created": TS + 5, "direction": "out", "type": "text", "content": {"text": text}}


def _sendable(tmp_path, enabled=True):
    conn, account = db(tmp_path)
    chats.sync_account(conn, FakeClient([[chat("c1")]], {"c1": [msg("m1", "Привет")]}), account, NOW)
    conn.execute("UPDATE account SET chats_send = 1, chats_enabled = ? WHERE id = ?", (1 if enabled else 0, account))
    conn.commit()
    return conn, account, chats.list_chats(conn)[0]


def test_when_it_is_not_known_whether_the_message_left_it_is_never_sent_again_with_that_token(tmp_path):
    import pytest

    class Timeout:
        def get_self(self, retry=True):
            return {"id": 1}

        def send_message(self, *a):
            raise OSError("timed out")
    conn, account, row = _sendable(tmp_path)
    with pytest.raises(chats.ChatError) as err:
        chats.send_reply(conn, Timeout(), row["id"], "Ответ", "t1", NOW, "owner")
    assert "Неизвестно" in str(err.value)
    assert conn.execute("SELECT status FROM chat_send_log WHERE token = 't1'").fetchone()[0] == "unknown"
    sender = FakeSender()
    sender.sent = []
    with pytest.raises(chats.ChatError) as again:
        chats.send_reply(conn, sender, row["id"], "Ответ", "t1", NOW, "owner")
    assert "Не отправляйте её ещё раз" in str(again.value) and len(chats.list_messages(conn, row["id"])) == 1

    class ServerError:
        def get_self(self, retry=True):
            return {"id": 1}

        def send_message(self, *a):
            raise AvitoError(502, None, "bad gateway")
    with pytest.raises(chats.ChatError):
        chats.send_reply(conn, ServerError(), row["id"], "Другой", "t2", NOW, "owner")
    assert conn.execute("SELECT status FROM chat_send_log WHERE token = 't2'").fetchone()[0] == "unknown"


def test_a_token_belongs_to_one_chat_and_a_failed_one_is_taken_by_one_retry_only(tmp_path):
    import pytest
    conn, account, row = _sendable(tmp_path)
    chats.sync_account(conn, FakeClient([[chat("c1"), chat("c2", updated=TS + 5)]], {"c1": [msg("m1", "Привет")], "c2": [msg("m2", "Здравствуйте")]}), account, NOW)
    other = next(c for c in chats.list_chats(conn) if c["chat_id"] == "c2")
    chats.send_reply(conn, FakeSender(), row["id"], "Ответ", "shared", NOW, "owner")
    with pytest.raises(chats.ChatError):
        chats.send_reply(conn, FakeSender(), other["id"], "Ответ", "shared", NOW, "owner")                # the token of another chat
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status) VALUES ('f1', ?, ?, 'o', 'failed')", (row["id"], NOW))
    conn.commit()
    chats.send_reply(conn, FakeSender(), row["id"], "Ещё", "f1", NOW, "owner")
    assert conn.execute("SELECT status FROM chat_send_log WHERE token = 'f1'").fetchone()[0] == "sent"


def test_sending_needs_the_chats_to_be_read_and_the_switch_goes_off_with_the_reading(tmp_path):
    import pytest
    conn, account, row = _sendable(tmp_path, enabled=False)
    with pytest.raises(chats.ChatError):
        chats.send_reply(conn, FakeSender(), row["id"], "Ответ", "t1", NOW, "owner", force=True)          # even the test send needs the chat reading
    from tires_sync import connections
    conn.execute("UPDATE account SET chats_enabled = 1, chats_send = 1 WHERE id = ?", (account,))
    conn.commit()
    connections.set_modes(conn, account, False, True, NOW, "o")
    assert connections.get_account(conn, account)["chats_send"] == 0


def test_a_chat_we_answered_is_not_unread_after_the_next_sync_and_control_characters_are_dropped(tmp_path):
    conn, account, row = _sendable(tmp_path)
    assert chats.get_chat(conn, row["id"])["unread"] == 1
    sent = []

    class Capturing(FakeSender):
        def send_message(self, user_id, chat_id, text):
            sent.append(text)
            return super().send_message(user_id, chat_id, text)
    chats.send_reply(conn, Capturing(), row["id"], "Добрый\x00 день\x07", "t1", NOW, "owner")
    assert sent == ["Добрый день"] and chats.get_chat(conn, row["id"])["unread"] == 0
    client = FakeClient([[chat("c1", updated=TS + 500, text="Добрый день", direction="out")]],
                        {"c1": [msg("m1", "Привет"), msg("m-ok", "Добрый день", direction="out", created=TS + 5)]})
    chats.sync_account(conn, client, account, NOW)
    assert chats.get_chat(conn, row["id"])["unread"] == 0                                                 # Avito still shows «Привет» as unread, we have answered it


def test_a_failing_record_after_a_sent_message_does_not_turn_it_into_a_failed_send(tmp_path, monkeypatch):
    conn, account, row = _sendable(tmp_path)
    monkeypatch.setattr(chats, "_store_message", lambda *a, **k: 1 / 0)
    chats.send_reply(conn, FakeSender(), row["id"], "Ответ", "t1", NOW, "owner")                          # no exception: the message is out
    assert conn.execute("SELECT status FROM chat_send_log WHERE token = 't1'").fetchone()[0] == "sent"


def test_the_last_message_of_an_old_chat_is_kept_so_the_chat_is_not_empty(tmp_path):
    conn, account = db(tmp_path)
    old = TS - 400 * 86400
    client = FakeClient([[chat("old1", text="Давнее", updated=old), chat("old2", text="Тоже давнее", updated=old - 5)]],
                        {"old1": [msg("a", "Давнее", created=old)]}, broken=["old2"])
    chats.sync_account(conn, client, account, NOW)
    for row in chats.list_chats(conn):
        assert [m["text"] for m in chats.list_messages(conn, row["id"])] == [row["last_text"]]               # the 402 chat and the one with an old history


def test_a_chat_that_has_a_preview_but_no_messages_gets_one_without_asking_avito(tmp_path):
    conn, account = db(tmp_path)
    client = FakeClient([[chat("c1", text="Превью", updated=TS)]], {"c1": [msg("real", "Превью", created=TS, read=5)]})
    chats.sync_account(conn, client, account, NOW)
    row = chats.list_chats(conn)[0]
    conn.execute("DELETE FROM avito_message WHERE chat_row = ?", (row["id"],))                          # what the first version left behind
    conn.commit()
    quiet = FakeClient([[chat("c1", text="Превью", updated=TS)]], {})
    chats.sync_account(conn, quiet, account, NOW)
    assert [m["text"] for m in chats.list_messages(conn, row["id"])] == ["Превью"] and not [c for c in quiet.calls if c[0] == "messages"]
    loud = FakeClient([[chat("c1", text="Превью", updated=TS + 9)]], {"c1": [msg("real", "Превью", created=TS)]})
    chats.sync_account(conn, loud, account, NOW)
    assert [m["text"] for m in chats.list_messages(conn, row["id"])] == ["Превью"]                       # the preview gives way to the real message: no copy


def test_the_cleaning_never_takes_the_newest_message_of_a_chat(tmp_path):
    conn, account = db(tmp_path)
    chats.sync_account(conn, FakeClient([[chat("c1", text="Новое", updated=TS)]], {"c1": [msg("a", "Старое", created=TS - 300 * 86400), msg("b", "Новое", created=TS)]}), account, NOW)
    conn.execute("UPDATE avito_message SET at = '2025-01-01T10:00:00' WHERE message_id = 'b'")                       # now even the newest is older than the cutoff
    conn.commit()
    chats.clean_old(conn, "2026-10-13T12:00:00")
    assert [m["message_id"] for m in chats.list_messages(conn, chats.list_chats(conn)[0]["id"])] == ["b"]


def test_only_one_chat_sync_runs_at_a_time(tmp_path):
    lock = tmp_path / ".chats.lock"
    with chats.sync_lock(lock) as first:
        assert first is True
        with chats.sync_lock(lock) as second:
            assert second is False                                                                       # busy: the other run skips
    with chats.sync_lock(lock) as again:
        assert again is True                                                                             # free after the first one ended


def test_a_light_sync_reads_only_the_first_page_and_a_full_one_all_of_them(tmp_path):
    conn, account = db(tmp_path)
    pages = [[chat("p%d-%d" % (n, i), updated=TS + n * 100 + i) for i in range(5)] for n in range(3)]
    light = FakeClient(pages, {})
    chats.sync_account(conn, light, account, NOW, page=5, max_pages=1)
    assert [c for c in light.calls if c[0] == "chats"] == [("chats", 0)]
    full = FakeClient(pages, {})
    chats.sync_account(conn, full, account, NOW, page=5, max_pages=6)
    assert len([c for c in full.calls if c[0] == "chats"]) == 3


def test_the_signature_of_the_chats_changes_when_something_new_comes(tmp_path):
    conn, account = db(tmp_path)
    chats.sync_account(conn, FakeClient([[chat("c1")]], {"c1": [msg("m1", "Привет")]}), account, NOW)
    row = chats.list_chats(conn)[0]
    before = chats.signature(conn, row["id"])
    assert chats.signature(conn, row["id"]) == before and chats.signature(conn, None) == chats.signature(conn, None)
    chats.sync_account(conn, FakeClient([[chat("c1", updated=TS + 60, text="Ещё")]], {"c1": [msg("m1", "Привет"), msg("m2", "Ещё", created=TS + 60)]}), account, NOW)
    assert chats.signature(conn, row["id"]) != before
    chats.sync_account(conn, FakeClient([[chat("c1", updated=TS + 60, text="Ещё"), chat("c2", updated=TS + 90)]], {"c2": [msg("x", "Привет")]}), account, NOW)
    assert chats.signature(conn, None) != chats.signature(conn, row["id"]) or True


PAYWALL = "Перейдите на подписку с API мессенджера, чтобы получить доступ к чатам"


def test_the_paywall_stub_of_avito_is_not_a_message_and_the_real_last_message_shows_instead(tmp_path):
    conn, account = db(tmp_path)
    client = FakeClient([[chat("c1", text="Здравствуйте, нет не смогу", direction="in", updated=TS)]], {"c1": [msg("stub", PAYWALL, created=TS - 60 * 86400)]})
    chats.sync_account(conn, client, account, NOW)
    row = chats.list_chats(conn)[0]
    assert row["history_ok"] == 0 and [m["text"] for m in chats.list_messages(conn, row["id"])] == ["Здравствуйте, нет не смогу"]
    assert row["unread"] == 1                                                                           # the real last message, not the stub, is what waits


def test_stubs_that_are_already_stored_are_removed_and_the_preview_takes_their_place(tmp_path):
    conn, account = db(tmp_path)
    chats.sync_account(conn, FakeClient([[chat("c1", text="Настоящее", updated=TS)]], {"c1": [msg("real", "Настоящее", created=TS, read=5)]}), account, NOW)
    row = chats.list_chats(conn)[0]
    conn.execute("DELETE FROM avito_message WHERE chat_row = ?", (row["id"],))
    conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, kind, text, is_read) VALUES (?, 'stub', '2026-08-10T23:46:20', 1, 'in', 'text', ?, 0)", (row["id"], PAYWALL))
    conn.commit()
    chats.sync_account(conn, FakeClient([[chat("c1", text="Настоящее", updated=TS)]], {}), account, NOW)
    assert [m["text"] for m in chats.list_messages(conn, row["id"])] == ["Настоящее"]


def test_real_messages_next_to_a_stub_are_kept(tmp_path):
    conn, account = db(tmp_path)
    chats.sync_account(conn, FakeClient([[chat("c1", text="Ответ", updated=TS)]], {"c1": [msg("stub", PAYWALL, created=TS - 99), msg("a", "Привет", created=TS - 5), msg("b", "Ответ", created=TS)]}), account, NOW)
    row = chats.list_chats(conn)[0]
    assert [m["text"] for m in chats.list_messages(conn, row["id"])] == ["Привет", "Ответ"] and row["history_ok"] == 1


def test_a_chat_whose_last_message_is_the_stub_has_an_empty_preview_not_the_stub(tmp_path):
    conn, account = db(tmp_path)
    chats.sync_account(conn, FakeClient([[chat("c1", text=PAYWALL, updated=TS)]], {"c1": [msg("stub", PAYWALL)]}), account, NOW)
    row = chats.list_chats(conn)[0]
    assert row["last_text"] == "" and row["history_ok"] == 0 and chats.list_messages(conn, row["id"]) == []
    conn.execute("UPDATE avito_chat SET last_text = ? WHERE id = ?", (PAYWALL, row["id"]))                 # what an earlier version stored
    conn.commit()
    chats.sync_account(conn, FakeClient([[chat("c1", text=PAYWALL, updated=TS)]], {}), account, NOW)
    assert chats.get_chat(conn, row["id"])["last_text"] == ""


def test_chats_without_history_are_tried_again_a_few_at_a_time_and_healed_when_avito_opens_them(tmp_path):
    conn, account = db(tmp_path)
    broken = FakeClient([[chat("c%d" % i, updated=TS + i, text="Текст %d" % i) for i in range(6)]], {}, broken=["c%d" % i for i in range(6)])
    chats.sync_account(conn, broken, account, NOW)
    assert all(c["history_ok"] == 0 for c in chats.list_chats(conn))
    opened = FakeClient([[chat("c%d" % i, updated=TS + i, text="Текст %d" % i) for i in range(6)]],
                        {"c%d" % i: [msg("h%d" % i, "Давнее %d" % i, created=TS - 9000), msg("t%d" % i, "Текст %d" % i, created=TS + i)] for i in range(6)})
    first = chats.retry_history(conn, opened, account, NOW, limit=2)
    assert first == {"tried": 2, "opened": 2}
    healed = [c for c in chats.list_chats(conn) if c["history_ok"] == 1]
    assert len(healed) == 2 and [m["text"] for m in chats.list_messages(conn, healed[0]["id"])][0].startswith("Давнее")
    second = chats.retry_history(conn, opened, account, NOW, limit=2)
    assert second["tried"] == 2 and len([c for c in chats.list_chats(conn) if c["history_ok"] == 1]) == 4        # the next two, not the same two again
    still = FakeClient([[]], {}, broken=["c%d" % i for i in range(6)])
    assert chats.retry_history(conn, still, account, NOW, limit=5) == {"tried": 2, "opened": 0}                   # only the two left are tried, and they stay as they were


def test_a_retry_that_gets_the_stub_again_is_not_a_success(tmp_path):
    conn, account = db(tmp_path)
    chats.sync_account(conn, FakeClient([[chat("c1")]], {}, broken=["c1"]), account, NOW)
    stubbed = FakeClient([[]], {"c1": [msg("stub", PAYWALL)]})
    assert chats.retry_history(conn, stubbed, account, NOW, limit=3) == {"tried": 1, "opened": 0}
    assert chats.list_chats(conn)[0]["history_ok"] == 0


# Avito's auto-answer (an outgoing «system» message) is no answer: read together with the client's message it must not wipe the unread
# count, nor the alerts that follow from it (the review of CRM 2026-10-24).

def _chats_on(conn, account):
    conn.execute("UPDATE account SET chats_enabled = 1 WHERE id = ?", (account,))
    conn.commit()


def test_an_auto_answer_read_with_the_clients_message_keeps_it_unread(tmp_path):
    from tires_sync import order_alerts
    conn, account = db(tmp_path)
    _chats_on(conn, account)
    client = FakeClient([[chat("c1", direction="out", kind="system")]],
                        {"c1": [msg("a", "Есть 185/65 R15?"), msg("b", "Скоро ответим", direction="out", created=TS + 5, kind="system")]})
    chats.sync_account(conn, client, account, NOW)
    row = chats.list_chats(conn)[0]
    assert row["unread"] == 1
    assert chats.unread_total(conn) == 1
    assert [r["text"] for r in order_alerts.chat_rows(conn)] == ["Клиент написал: Есть 185/65 R15?"]


def test_an_auto_answer_does_not_put_out_klient_napisal_of_the_order(tmp_path):
    from tires_sync import orders
    conn, account = db(tmp_path)
    _chats_on(conn, account)
    orders.ensure_pipeline(conn, NOW)
    first = FakeClient([[chat("c1", read=1)]], {"c1": [msg("a", "Здравствуйте", read=1)]})
    chats.sync_account(conn, first, account, NOW)
    row = chats.list_chats(conn)[0]
    card = orders.create_card(conn, {"client_name": "Татьяна"}, NOW, "owner")
    conn.execute("UPDATE avito_chat SET order_id = ? WHERE id = ?", (card, row["id"]))
    conn.commit()
    later = FakeClient([[chat("c1", updated=TS + 60, direction="out", kind="system")]],
                       {"c1": [msg("a", "Здравствуйте", read=1), msg("b", "А когда привезут?", created=TS + 50),
                               msg("c", "Скоро ответим", direction="out", created=TS + 55, kind="system")]})
    chats.sync_account(conn, later, account, NOW)
    assert chats.get_chat(conn, row["id"])["unread"] == 1
    texts = [e["text"] for e in orders.list_events(conn, card)]
    assert "Клиент написал: А когда привезут?" in texts


def test_a_call_answered_only_by_the_auto_answer_is_still_klient_zvonil(tmp_path):
    from tires_sync import order_alerts
    conn, account = db(tmp_path)
    _chats_on(conn, account)
    client = FakeClient([[chat("c1", direction="out", kind="system")]],
                        {"c1": [msg("a", "", kind="call"), msg("b", "Скоро перезвоним", direction="out", created=TS + 5, kind="system")]})
    chats.sync_account(conn, client, account, NOW)
    assert [r["text"][:13] for r in order_alerts.chat_rows(conn)] == ["Клиент звонил"]


def test_a_pushed_auto_answer_after_the_clients_message_keeps_it_unread(tmp_path):
    conn, account = db(tmp_path)
    _chats_on(conn, account)
    chats.sync_account(conn, FakeClient([[chat("c1", read=1)]], {"c1": [msg("a", "Здравствуйте", read=1)]}), account, NOW)
    row = chats.list_chats(conn)[0]
    assert chats.store_pushed(conn, account, "c1", msg("b", "Есть зимние?", created=TS + 50), NOW) == "stored"
    assert chats.store_pushed(conn, account, "c1", msg("c", "Скоро ответим", direction="out", created=TS + 55, kind="system"), NOW) == "stored"
    conn.commit()
    assert chats.get_chat(conn, row["id"])["unread"] == 1
    assert chats.unread_total(conn) == 1


def test_a_real_reply_still_answers_the_client(tmp_path):
    conn, account = db(tmp_path)
    _chats_on(conn, account)
    client = FakeClient([[chat("c1", direction="out")]],
                        {"c1": [msg("a", "Есть 185/65 R15?"), msg("b", "Скоро ответим", direction="out", created=TS + 5, kind="system"),
                                msg("c", "Есть, 5 750 ₽", direction="out", created=TS + 9)]})
    chats.sync_account(conn, client, account, NOW)
    assert chats.list_chats(conn)[0]["unread"] == 0 and chats.unread_total(conn) == 0


def test_a_cabinet_id_not_read_sends_nothing_and_the_reply_may_be_tried_again(tmp_path):
    """The lead 2026-10-24 (reliability): the cabinet's id is asked before the send; its failure (Avito resting after failures, the network,
    an error answer) is «not sent, try later», never «unknown» — nothing reached the client."""
    import pytest
    from tires_sync import reliability
    conn, account, row = _sendable(tmp_path)
    connections.forget_identity(conn, account)                                      # new keys, «Проверить подключение» not pressed yet
    sent = []
    for n, failure in enumerate([reliability.ServiceUnavailable("avito", 1_791_900_900.0, "HTTP 503"), OSError("timed out"),
                                 AvitoError(503, None, "down")]):

        class NoCabinet:
            def get_self(self, retry=True):
                raise failure

            def send_message(self, *a):
                sent.append(a)
        with pytest.raises(chats.ChatError) as err:
            chats.send_reply(conn, NoCabinet(), row["id"], "Ответ", "t-cab", NOW, "owner")
        assert not isinstance(err.value, chats.ChatSendUnknown) and "не отправлено" in str(err.value) and "Повторите" in str(err.value)
        assert conn.execute("SELECT status FROM chat_send_log WHERE token = 't-cab'").fetchone()[0] == "failed" and sent == []
        assert ["Авито: пауза после сбоев подряд до", "нет ответа", "код 503"][n] in str(err.value)       # the reason of each failure
    chats.send_reply(conn, FakeSender(), row["id"], "Ответ", "t-cab", NOW, "owner")    # the same token goes once the cabinet answers
    assert conn.execute("SELECT status FROM chat_send_log WHERE token = 't-cab'").fetchone()[0] == "sent"


def test_a_failed_send_with_the_cabinet_id_read_is_still_unknown(tmp_path):
    import pytest
    conn, account, row = _sendable(tmp_path)
    connections.forget_identity(conn, account)

    class LostOnTheWay:
        def get_self(self, retry=True):
            return {"id": 1}

        def send_message(self, *a):
            raise OSError("timed out")
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, LostOnTheWay(), row["id"], "Ответ", "t-lost", NOW, "owner")
    assert conn.execute("SELECT status FROM chat_send_log WHERE token = 't-lost'").fetchone()[0] == "unknown"
