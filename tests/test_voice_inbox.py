"""A voice of a point without VK's transcript is a question in «Входящие», never an order (the owner 2026-10-21, docs/DECISIONS.md):
no «Передано с точки», no 15-minute timer, no alert to the person on duty. A transcript «клиент …» still hands a client over."""
from __future__ import annotations

import json
import urllib.parse

from test_handoff import NOW, base  # noqa: F401  (the fixture)
from test_vk_commands import PEER, ready
from tires_sync import order_manager as mgr, orders

LINK = "https://psv4.userapi.com/c1/u1/audiomsg/d1/a1b2c3.mp3"


def handoffs(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM order_handoff").fetchone()[0]


def test_a_voice_without_a_transcript_is_a_line_of_the_inbox_and_no_order(base):  # noqa: F811
    conn, _ = base
    told = []
    cards = conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0]
    answer = mgr.dispatch(conn, "vk-1", PEER, "", None, NOW, notify=lambda card, text: told.append(text), voice="pending", voice_link=LINK)
    [row] = mgr.list_inbox(conn)
    assert row["text"] == "(голосовое, расшифровки нет) %s" % LINK and row["status"] == "new" and row["point"] == "Бета"
    assert handoffs(conn) == 0 and conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == cards and told == []
    assert "заказ" not in answer.lower()
    mgr.dispatch(conn, "vk-1", PEER, "", None, NOW, voice="pending", voice_link=LINK)                         # a repeat of the event
    assert len(mgr.list_inbox(conn)) == 1


def test_a_voice_without_a_transcript_keeps_the_words_typed_with_it_and_drops_a_strange_link(base):  # noqa: F811
    conn, _ = base
    mgr.dispatch(conn, "vk-2", PEER, "по Ивану", None, NOW, voice="pending", voice_link="javascript:alert(1)")
    assert mgr.list_inbox(conn)[0]["text"] == "(голосовое, расшифровки нет) по Ивану" and handoffs(conn) == 0


def test_a_typed_handoff_with_a_voice_keeps_what_was_typed(base):  # noqa: F811
    conn, _ = base
    mgr.dispatch(conn, "vk-3", PEER, "клиент 8 900 111 22 33 нужны летние шины", None, NOW, voice="pending")
    card = orders.get_card(conn, conn.execute("SELECT order_id FROM order_handoff").fetchone()[0])
    assert card["comment"] == "нужны летние шины"


def test_a_transcript_klient_still_hands_the_client_over(base):  # noqa: F811
    conn, _ = base
    mgr.dispatch(conn, "vk-4", PEER, "", None, NOW, voice="done", transcript="клиент 89001112233 нужны шины")
    assert handoffs(conn) == 1 and mgr.list_inbox(conn) == []


def _voice_event(app, conn, event, audio):
    from tires_sync import vk_callback
    msg = {"id": 900 + len(event), "from_id": int(PEER), "peer_id": int(PEER), "text": "", "attachments": [{"type": "audio_message", "audio_message": audio}]}
    body = {"type": "message_new", "group_id": 220945892, "secret": "Sup3r-Secret_key", "event_id": event, "object": {"message": msg}}
    url = "/hook/vk/%s" % vk_callback.path_token(conn)
    return app.test_client().post(url, data=json.dumps(body), content_type="application/json").get_data(as_text=True)


def test_through_the_vk_hook_a_voice_without_a_transcript_lands_in_the_inbox_once_with_its_link(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    before = handoffs(conn)
    for _ in range(2):                                                                                         # VK repeats the event
        assert _voice_event(app, conn, "ev-v1", {"link_mp3": LINK, "link_ogg": LINK.replace(".mp3", ".ogg"), "transcript_state": "in_progress"}) == "ok"
    rows = [r for r in mgr.list_inbox(conn) if r["text"].startswith("(голосовое")]
    assert len(rows) == 1 and LINK in rows[0]["text"] and handoffs(conn) == before
    sent = [urllib.parse.parse_qs(c[3].decode("utf-8"))["message"][0] for c in app.config["VK_HTTP"].calls]
    assert not any("Принято голосовое" in s for s in sent)
    assert sent.count(mgr.signed(mgr.VOICE_ANSWER)) == 1                                                                  # the point is asked to type, once (VK repeats)
    page = client.get("/orders/inbox").get_data(as_text=True)
    assert "расшифровки нет" in page


# ---- the review 2026-10-21 -------------------------------------------------------------------------------------------------------------

def test_the_auto_answer_looks_only_at_what_was_typed_and_the_link_comes_first(base):  # noqa: F811
    conn, _ = base
    mgr.add_bot_answer(conn, "нет", "Автоответ про «нет»")
    assert mgr.dispatch(conn, "vk-5", PEER, "", None, NOW, voice="pending", voice_link=LINK) == mgr.VOICE_ANSWER    # «расшифровки нет» is ours: not «нет»
    mgr.dispatch(conn, "vk-6", PEER, "х" * 3000, None, NOW, voice="pending", voice_link=LINK)
    row = [r for r in mgr.list_inbox(conn) if r["message_id"] == "vk-6"][0]
    assert row["text"].startswith("(голосовое, расшифровки нет) %s " % LINK)                            # a long text never cuts the record off


def test_a_typed_handoff_keeps_the_record_of_its_voice_in_the_journal(base):  # noqa: F811
    conn, _ = base
    mgr.dispatch(conn, "vk-7", PEER, "клиент 8 900 111 22 33 нужны летние шины", None, NOW, voice="pending", voice_link=LINK)
    card_id = conn.execute("SELECT order_id FROM order_handoff").fetchone()[0]
    assert any(LINK in e["text"] and "голосовое" in e["text"] for e in orders.list_events(conn, card_id))


def test_a_lost_voice_without_a_transcript_reads_the_same_with_its_link(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    from tires_sync import vk_callback
    from datetime import datetime
    msg = {"id": 902, "from_id": int(PEER), "peer_id": int(PEER), "text": "", "attachments": [{"type": "audio_message", "audio_message": {"link_mp3": LINK}}]}
    data = {"type": "message_new", "group_id": 220945892, "event_id": "ev-lost", "object": {"message": msg}}
    vk_callback.started(conn, datetime(2026, 10, 21, 8, 0, 0))
    conn.execute("INSERT INTO vk_event (event_id, at, type, group_id, payload) VALUES (?,?,?,?,?)",
                 ("ev-lost", "2026-10-21T08:30:00", "message_new", "220945892", json.dumps(data)))
    conn.commit()
    assert vk_callback.sweep(conn, datetime(2026, 10, 21, 9, 0, 0)) == 1
    text = conn.execute("SELECT text FROM manager_message ORDER BY id DESC").fetchone()[0]
    assert "(голосовое, расшифровки нет)" in text and LINK in text


# ---- the answer to the point: on by default (the owner 2026-10-21: «включи»), the switch in app_state turns it off --------------------

def test_the_point_gets_no_answer_when_the_switch_is_turned_off(base):  # noqa: F811
    conn, _ = base
    conn.execute("INSERT OR REPLACE INTO app_state VALUES (?, ?)", (mgr.VOICE_ANSWER_KEY, "false"))
    conn.commit()
    assert not mgr.voice_answer_on(conn)
    assert mgr.dispatch(conn, "vk-8", PEER, "", None, NOW, voice="pending", voice_link=LINK) == ""


def test_by_default_the_point_is_asked_to_type_once_per_event(base):  # noqa: F811
    conn, _ = base
    assert mgr.voice_answer_on(conn)                                                                         # nothing in app_state: on
    assert mgr.dispatch(conn, "vk-9", PEER, "", None, NOW, voice="pending", voice_link=LINK) == mgr.VOICE_ANSWER
    assert mgr.VOICE_ANSWER == "Голосовое получено, расшифровки нет — напишите текстом: клиент <телефон> <что нужно>"
    assert mgr.dispatch(conn, "vk-9", PEER, "", None, NOW, voice="pending", voice_link=LINK) == ""             # a repeat of the event
    assert len(mgr.list_inbox(conn)) == 1 and handoffs(conn) == 0


def test_a_repeat_of_a_typed_handoff_with_a_voice_writes_the_record_once(base):  # noqa: F811
    conn, _ = base
    for _ in range(2):
        mgr.dispatch(conn, "vk-10", PEER, "клиент 8 900 111 22 33 нужны шины", None, NOW, voice="pending", voice_link=LINK)
    card_id = conn.execute("SELECT order_id FROM order_handoff").fetchone()[0]
    assert sum("приложено голосовое" in e["text"] for e in orders.list_events(conn, card_id)) == 1


def test_the_answer_goes_only_to_a_point_and_only_for_a_voice_without_a_transcript(base):  # noqa: F811
    conn, _ = base
    assert mgr.dispatch(conn, "vk-20", "555000111", "", None, NOW, voice="pending", voice_link=LINK) == ""       # not a point: nothing
    handed = mgr.dispatch(conn, "vk-21", PEER, "клиент 8 900 111 22 33 нужны шины", None, NOW, voice="pending", voice_link=LINK)
    assert handed and handed != mgr.VOICE_ANSWER                                                                  # «клиент …»: the handoff's answer
    mgr.set_bot_answer_enabled(conn, mgr.add_bot_answer(conn, "шины", "Автоответ про шины"), True)
    assert mgr.dispatch(conn, "vk-22", PEER, "шины", None, NOW, voice="pending", voice_link=LINK) == "Автоответ про шины"
    assert mgr.dispatch(conn, "vk-23", PEER, "", None, NOW, voice="done", transcript="есть летние?") != mgr.VOICE_ANSWER   # a transcript
