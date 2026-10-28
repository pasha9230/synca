"""The backstop of VK's Callback (the owner 2026-10-28, order 450): a «Приехали» of a point VK never called us with is read from the history of
the point's conversation and acted on exactly as if it had come; a message is acted on once whichever way it came, nothing older than a day
is answered, our own and the admins' messages are never read as the point's."""
from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

from http_fakes import ScriptedHttp
from test_vk_commands import GROUP, PEER, SENT, hook, ready, to_client, to_points
from tires_sync import cli, order_manager as mgr, orders, store, vk_callback, vk_catchup
from tires_sync.panel import routes_vk

RUN = datetime(2026, 10, 13, 9, 30, 0)                       # UTC, as the panel's now (the app of the tests says 09:00)


def at(minutes_ago: float) -> int:
    return int((RUN - timedelta(minutes=minutes_ago)).replace(tzinfo=timezone.utc).timestamp())


def item(mid, text, minutes_ago=20, sender=PEER, out=0, reply=None, admin=None):
    msg = {"id": mid, "conversation_message_id": mid + 1000, "date": at(minutes_ago), "out": out, "from_id": int(sender),
           "peer_id": int(PEER), "text": text, "attachments": []}
    if reply:
        msg["reply_message"] = reply
    if admin:
        msg["admin_author_id"] = admin
    return msg


class FakeHistory:
    """messages.getHistory of VK: the newest first, `count` a page at `offset`; records what was asked."""

    def __init__(self, items=None):
        self.by_peer = {PEER: list(items or [])}
        self.calls = []

    def __call__(self, peer, offset, count):
        self.calls.append((peer, offset, count))
        newest = sorted(self.by_peer.get(peer, []), key=lambda m: -m["id"])
        return newest[offset:offset + count]


def catchup(app, conn, history, now=RUN):
    return vk_catchup.run(conn, app.config["SECRET_BOX"], now, lambda db, local: routes_vk.channel(app, db, local), history=history)


def bot_line(no="5000100"):
    return {"id": 90, "from_id": -int(GROUP), "text": "Заказ №%s — Nokian Hakkapeliitta 10 × 4, клиент Татьяна" % no}


def test_a_missed_reply_arrived_moves_the_order_tells_the_client_once_and_is_written_in_the_journal(tmp_path, monkeypatch):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    said = []
    monkeypatch.setattr(cli, "_alert", lambda conn, box, text, **kw: said.append((text, kw)) or True)
    history = FakeHistory([item(101, "Приехали", reply=bot_line())])                 # a reply to the bot's order line, as at Янино
    got = catchup(app, conn, history)
    assert got["picked"] == 1 and orders.get_card(conn, card)["stage"]["code"] == "at_point"
    [text] = to_client(app)
    assert text.startswith("Заказ 5000100 приехал в пункт выдачи, можно забирать.")
    [(peer, answer)] = to_points(app)
    assert peer == PEER and answer.startswith("(бот) Заказ №5000100 — отмечено: приехал")
    row = conn.execute("SELECT detail FROM audit WHERE action = 'vk_catchup'").fetchone()
    detail = json.loads(row[0])
    assert detail["peer"] == PEER and detail["message_id"] == "101" and detail["point"] == "Бета"
    assert detail["text"] == vk_catchup.JOURNAL_LINE
    assert len(said) == 1 and "1" in said[0][0]                                     # the owner hears it, once a day
    for _ in range(2):                                                               # two more runs: nothing again
        assert catchup(app, conn, history)["picked"] == 0
    assert len(to_client(app)) == 1 and len(to_points(app)) == 1 and len(said) == 1
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'vk_catchup'").fetchone()[0] == 1


def test_a_message_that_came_by_callback_is_never_acted_on_again(tmp_path, monkeypatch):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    monkeypatch.setattr(cli, "_alert", lambda *a, **kw: pytest.fail("nothing was missed"))
    hook(app, conn, "Приехал 5000100", event="cb1", message_id=101)                   # Callback did its work
    assert len(to_client(app)) == 1 and len(to_points(app)) == 1
    assert catchup(app, conn, FakeHistory([item(101, "Приехал 5000100")]))["picked"] == 0
    assert len(to_client(app)) == 1 and len(to_points(app)) == 1
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'vk_catchup'").fetchone()[0] == 0


def test_a_message_known_only_from_the_events_of_before_the_backstop_is_not_redone(tmp_path, monkeypatch):
    """Callback took it before this version (no mark of the message) and the work was cut: «Входящие» shows it (vk_callback.sweep), the
    backstop never redoes it by itself."""
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    monkeypatch.setattr(cli, "_alert", lambda *a, **kw: pytest.fail("nothing was missed"))
    payload = {"type": "message_new", "group_id": int(GROUP), "event_id": "old1",
               "object": {"message": {"id": 101, "from_id": int(PEER), "peer_id": int(PEER), "text": "Приехал 5000100"}}}
    conn.execute("INSERT INTO vk_event (event_id, at, type, group_id, payload) VALUES ('old1', ?, 'message_new', ?, ?)",
                 ((RUN - timedelta(minutes=30)).isoformat(timespec="seconds"), GROUP, json.dumps(payload, ensure_ascii=False)))
    conn.commit()
    assert catchup(app, conn, FakeHistory([item(101, "Приехал 5000100")]))["picked"] == 0
    assert to_client(app) == [] and to_points(app) == []


def test_a_late_callback_after_the_backstop_does_nothing(tmp_path, monkeypatch):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    monkeypatch.setattr(cli, "_alert", lambda *a, **kw: True)
    assert catchup(app, conn, FakeHistory([item(101, "Приехал 5000100")]))["picked"] == 1
    hook(app, conn, "Приехал 5000100", event="late", message_id=101)                  # VK calls at last, with an event of its own
    assert len(to_client(app)) == 1 and len(to_points(app)) == 1                      # no «уже отмечен» either


def test_our_own_admin_and_strangers_messages_are_never_read_as_the_points(tmp_path, monkeypatch):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    monkeypatch.setattr(cli, "_alert", lambda *a, **kw: pytest.fail("nothing was missed"))
    history = FakeHistory([item(101, "Приехал 5000100", sender="-" + GROUP, out=1),                   # the bot
                           item(102, "приехал 5000100", sender="-" + GROUP, out=1, admin=777),       # an admin writing as the community
                           item(103, "приехал 5000100", out=1),                                      # outgoing whatever from_id says
                           item(104, "приехал 5000100", sender="999")])                              # not the point's peer
    assert catchup(app, conn, history)["picked"] == 0
    assert to_client(app) == [] and to_points(app) == [] and orders.get_card(conn, card)["stage"]["code"] == "moving"


def test_nothing_older_than_a_day_is_ever_answered(tmp_path, monkeypatch):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    monkeypatch.setattr(cli, "_alert", lambda *a, **kw: pytest.fail("nothing was missed"))
    history = FakeHistory([item(101, "Приехал 5000100", minutes_ago=24 * 60 + 5)])
    assert catchup(app, conn, history)["picked"] == 0
    assert catchup(app, conn, history)["picked"] == 0
    assert to_client(app) == [] and to_points(app) == [] and orders.get_card(conn, card)["stage"]["code"] == "moving"


def test_a_fresh_message_is_left_to_callback_and_taken_on_the_next_run(tmp_path, monkeypatch):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    monkeypatch.setattr(cli, "_alert", lambda *a, **kw: True)
    history = FakeHistory([item(101, "Приехал 5000100", minutes_ago=1)])               # Callback may still be on its way
    assert catchup(app, conn, history)["picked"] == 0 and to_client(app) == []
    assert catchup(app, conn, history, now=RUN + timedelta(minutes=10))["picked"] == 1
    assert len(to_client(app)) == 1


def test_only_messages_after_the_last_looked_at_are_read_and_the_pages_stop_there(tmp_path, monkeypatch):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    monkeypatch.setattr(cli, "_alert", lambda *a, **kw: True)
    history = FakeHistory([item(n, "спасибо", minutes_ago=200 - n) for n in range(1, 151)])
    catchup(app, conn, history)
    assert store.get_state(conn, vk_catchup.CURSOR_KEY % PEER) == "150"
    history.calls.clear()
    history.by_peer[PEER].append(item(151, "Приехал 5000100", minutes_ago=10))
    assert catchup(app, conn, history)["picked"] == 1
    assert len(history.calls) == 1                                                    # the first page already reaches the mark


def test_a_point_without_vk_reads_nothing(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    history = FakeHistory([item(101, "Приехал 5000100")])
    conn.execute("UPDATE point SET vk_peer_id = ''")
    conn.commit()
    assert catchup(app, conn, history)["picked"] == 0 and history.calls == []


def test_a_failure_of_vk_for_one_point_keeps_its_mark_and_raises_nothing(tmp_path, monkeypatch):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    monkeypatch.setattr(cli, "_alert", lambda *a, **kw: True)

    def broken(peer, offset, count):
        raise vk_catchup.vk_send.VkError("ВК недоступен: нет связи с сервером.")
    got = catchup(app, conn, broken)
    assert got["errors"] == 1 and got["picked"] == 0 and not store.get_state(conn, vk_catchup.CURSOR_KEY % PEER)
    assert catchup(app, conn, FakeHistory([item(101, "Приехал 5000100")]))["picked"] == 1


def test_the_owner_is_told_once_a_day(tmp_path, monkeypatch):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    said = []
    monkeypatch.setattr(cli, "_alert", lambda conn, box, text, **kw: said.append(text) or True)
    catchup(app, conn, FakeHistory([item(101, "спасибо")]))
    catchup(app, conn, FakeHistory([item(101, "спасибо"), item(102, "ок", minutes_ago=5)]), now=RUN + timedelta(minutes=10))
    assert len(said) == 1
    catchup(app, conn, FakeHistory([item(103, "привет", minutes_ago=5 - 24 * 60)]), now=RUN + timedelta(days=1))   # written the next day
    assert len(said) == 2


def test_the_history_is_asked_with_the_key_in_the_body_and_its_errors_are_refusals():
    http = ScriptedHttp((200, {"response": {"count": 1, "items": [{"id": 5}]}}), (200, {"error": {"error_code": 15, "error_msg": "Access denied"}}))
    history = vk_catchup.history_for("vk1.a.KEY", GROUP, http)
    assert history(PEER, 0, 200) == [{"id": 5}]
    method, url, headers, body = http.calls[0]
    fields = urllib.parse.parse_qs(body.decode())
    assert method == "POST" and url == vk_catchup.HISTORY_URL and "KEY" not in url
    assert fields["peer_id"] == [PEER] and fields["count"] == ["200"] and fields["offset"] == ["0"] and fields["group_id"] == [GROUP]
    assert fields["access_token"] == ["vk1.a.KEY"]
    with pytest.raises(vk_catchup.vk_send.VkError) as err:
        history(PEER, 0, 200)
    assert "15" in str(err.value) and "KEY" not in str(err.value)


def test_the_mark_of_a_message_is_an_additive_table(tmp_path):
    conn = store.connect(tmp_path / "s.db")
    assert vk_callback.claim(conn, "vk-1", PEER, "callback", RUN) is True
    assert vk_callback.claim(conn, "vk-1", PEER, "catchup", RUN) is False
    conn.close()
    conn = store.connect(tmp_path / "s.db")                                           # a second start keeps the marks
    assert conn.execute("SELECT source FROM vk_message WHERE message_id = 'vk-1'").fetchone()[0] == "callback"


def test_the_command_prints_what_it_did_and_needs_no_vk_when_not_set_up(tmp_path, capsys):
    db = tmp_path / "s.db"
    store.connect(db).close()
    assert cli.main(["vk-catchup", "--db", str(db), "--key-file", ""]) == 0
    assert "ВК" in capsys.readouterr().out
