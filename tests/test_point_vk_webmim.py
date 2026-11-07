"""Тексты точке без WebMIM (решение владельца 30.10; ветка lane/point-vk-webmim, находка ревью point-webmim-hide): ответы бота ВК точке и
журнал её карточки не называют WebMIM / MIM / МИМ. Номер заказа для точки (order_label: «Рабочее место», команды ВК) остаётся, только
без слова WebMIM; что записано офису (исход команды, журнал офиса) — как раньше. Тексты клиентам не трогаются."""
from __future__ import annotations

import re

from test_arrived_stage import outcome
from test_order_manager import card_at, panel_numbers_from, setup
from test_point_webmim_hide import world  # noqa: F401  (the point's card with WebMIM values, old and new view)
from test_vk_commands import SENT, _stage, hook, ready, to_points
from tires_sync import order_manager as mgr, order_placed, orders

WORDS = re.compile(r"WebMIM|\bMIM\b|\bМИМ\b")
NOW = "2026-10-12T15:00:00"


def test_the_hint_of_the_panel_number_names_the_point_s_number_without_webmim(tmp_path):
    conn, pid, _other = setup(tmp_path)
    panel_numbers_from(conn, 429)
    card = card_at(conn, pid)
    orders.update_card(conn, card, {"webmim_number": "5000200"}, NOW, "o")
    answer = mgr.handle_command(conn, "h1", "111", "приехал %d" % card, None, NOW)
    assert not WORDS.search(answer), answer
    assert "5000200" in answer and "«приехал 5000200»" in answer                 # the number the point knows the order by
    assert conn.execute("SELECT outcome FROM order_command_log WHERE message_id = 'h1'").fetchone()[0] == \
        "внутренний номер заказа: подсказан номер WebMIM"                          # the office's record: as before


def test_arrived_on_an_order_not_placed_yet_tells_the_point_without_webmim(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    orders.move_card(conn, card, _stage(conn, "new"), "2026-10-13T12:00:00", "o")
    hook(app, conn, "приехали 5000100")
    texts = [text for _peer, text in to_points(app)]
    assert any("Заказ №5000100 ещё не оформлен" in text for text in texts), texts
    assert not any(WORDS.search(text) for text in texts), texts
    assert outcome(conn)[-1].startswith("спорно: ")                                 # the office's record: as before


def test_the_point_s_card_journal_names_no_webmim_author(world):
    conn, cid = world["conn"], world["here"]
    orders.add_event(conn, cid, "2026-10-13T12:07:00", order_placed.AUTHOR, "chat", "Клиенту ушло сообщение «заказ оформлен»", level="grey")
    conn.commit()
    page = world["point"].get("/orders/%d" % cid).get_data(as_text=True)
    assert "Клиенту ушло сообщение «заказ оформлен»" in page                       # the record stays the point's
    assert order_placed.AUTHOR not in page
    office = world["office"].get("/orders/%d" % cid).get_data(as_text=True)
    assert order_placed.AUTHOR in office                                           # the office: as before
