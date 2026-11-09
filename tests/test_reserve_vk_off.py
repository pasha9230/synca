"""The owner 2026-11-09 «Когда срок заканчивается, бот шлёт сообщение точке, это надо отключить»: on the last day of the reservation the bot
asks no point in VK («ещё на точке? выдал / на точке»), and no yellow «точку не спросить» either; the client's reminder from 12:30 and the
alert of the responsible go as before."""
from __future__ import annotations

from test_reserve_ask_point import at, ok, ready
from tires_sync import order_reserve as reserve, orders


def test_the_point_is_not_asked_in_vk_the_client_still_gets_the_reminder(tmp_path):
    assert reserve.VK_ASK_OFF
    conn, cid, chat, deliver, vk = ready(tmp_path)
    for hour in (10, 11, 15, 19):
        reserve.check(conn, at(hour), ok, deliver, vk)
    assert vk.sent == []                                                                # no question to the point
    assert len(deliver.sent) == 1                                                       # the client's reminder, once
    texts = [e["text"] for e in orders.list_events(conn, cid)]
    assert not any(t.startswith("Точке «") and "вопрос" in t for t in texts)
    assert not any("Точку не спросить" in t for t in texts)
