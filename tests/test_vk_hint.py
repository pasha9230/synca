"""The bot answers any plain message of a point (the owner 2026-10-23: «Кнопка не появилась» after «привет»): a short hint with the commands,
and with it the keyboard «Передать под заказ»; once in 30 minutes per point, never to a stranger, never twice for one VK event. The message
still lands in «Входящие»; a voice goes as before."""
from __future__ import annotations

from test_handoff import NOW, base  # noqa: F401  (the fixture)
from test_vk_commands import PEER
from tires_sync import order_manager as mgr, order_points as points


def inbox(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM manager_message").fetchone()[0]


def test_a_plain_message_of_a_point_gets_the_hint_once_in_half_an_hour(base):  # noqa: F811
    conn, _ = base
    assert mgr.dispatch(conn, "vk-1", PEER, "привет", None, NOW) == mgr.HINT
    assert inbox(conn) == 1                                                                    # the owner still sees it
    assert mgr.dispatch(conn, "vk-2", PEER, "как дела", None, "2026-10-21T12:29:00") == ""     # within 30 minutes: no spam
    assert inbox(conn) == 2
    assert mgr.dispatch(conn, "vk-3", PEER, "ау", None, "2026-10-21T12:30:00") == mgr.HINT     # half an hour later: again


def test_the_hint_says_the_commands_and_the_button(base):  # noqa: F811
    assert "приехал" in mgr.HINT and "выдал" in mgr.HINT and "клиент <телефон>" in mgr.HINT and mgr.BUTTON in mgr.HINT


def test_an_auto_answer_comes_first_and_does_not_spend_the_hint(base):  # noqa: F811
    conn, _ = base
    mgr.set_bot_answer_enabled(conn, mgr.add_bot_answer(conn, "шины", "Автоответ про шины"), True)
    assert mgr.dispatch(conn, "vk-1", PEER, "есть шины?", None, NOW) == "Автоответ про шины"
    assert mgr.dispatch(conn, "vk-2", PEER, "привет", None, NOW) == mgr.HINT


def test_no_hint_to_a_stranger_nor_for_a_repeat_of_the_event(base):  # noqa: F811
    conn, _ = base
    assert mgr.dispatch(conn, "vk-1", "555000111", "привет", None, NOW) == ""
    assert mgr.dispatch(conn, "vk-2", PEER, "привет", None, NOW) == mgr.HINT
    store_hint = conn.execute("SELECT value FROM app_state WHERE key LIKE 'vk_hint_%'").fetchall()
    assert len(store_hint) == 1
    conn.execute("DELETE FROM app_state WHERE key LIKE 'vk_hint_%'")                          # even with the pause over, a repeat says nothing
    conn.commit()
    assert mgr.dispatch(conn, "vk-2", PEER, "привет", None, NOW) == ""


def test_the_pause_is_per_point(base):  # noqa: F811
    conn, _ = base
    other = points.save_point(conn, {"name": "Гамма", "manager_name": "Б", "channel": "vk"})
    conn.execute("UPDATE point SET vk_peer_id = '222333444' WHERE id = ?", (other,))
    conn.commit()
    assert mgr.dispatch(conn, "vk-1", PEER, "привет", None, NOW) == mgr.HINT
    assert mgr.dispatch(conn, "vk-2", "222333444", "привет", None, NOW) == mgr.HINT           # another point: its own pause
    assert mgr.dispatch(conn, "vk-3", PEER, "привет", None, NOW) == ""


def test_a_voice_goes_as_before(base):  # noqa: F811
    conn, _ = base
    assert mgr.dispatch(conn, "vk-1", PEER, "", None, NOW, voice="pending", voice_link="https://psv4.userapi.com/c1/a.mp3") == mgr.VOICE_ANSWER
    assert mgr.dispatch(conn, "vk-2", PEER, "", None, NOW, voice="done", transcript="есть летние?") == ""


def test_the_hint_goes_to_the_point_in_vk_with_the_keyboard(tmp_path):
    import json
    import urllib.parse
    from test_vk_commands import hook, ready
    app, client, tok, conn, pid, card = ready(tmp_path)
    hook(app, conn, "привет", event="h1")
    [sent] = [urllib.parse.parse_qs(c[3].decode("utf-8")) for c in app.config["VK_HTTP"].calls]
    assert sent["message"][0] == mgr.signed(mgr.HINT) and json.loads(sent["keyboard"][0])["buttons"][0][0]["action"]["label"] == mgr.BUTTON
