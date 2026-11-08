"""The look of the audit of 2026-11-08 on the pages but the orders (lane proto-rest): one «Ждут ответа», no waiting time, the order panel of
a chat, the top bar and the menu."""
from __future__ import annotations

import re

from test_chats_pages import _say, setup
from tires_sync import chats, orders, store
from tires_sync.panel.routes_design import UI_KEY


def _new_view(conn):
    store.set_state(conn, UI_KEY % "office", "all")
    conn.commit()


def _menu_chats(page):
    link = re.search(r'<a class="sb-link[^"]*" href="/chats">.*?</a>', page, re.S).group(0)
    found = re.search(r'<span class="sb-count">(\d+)</span>', link)
    return int(found.group(1)) if found else 0


def _waiting_world(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    tanya = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Татьяна")
    ivan = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Иван")
    for row in (tanya, ivan):
        conn.execute("DELETE FROM avito_message WHERE chat_row = ?", (row["id"],))
    _say(conn, tanya["id"], "2026-10-13T11:35:00")                                 # Татьяна waits
    _say(conn, ivan["id"], "2026-10-13T11:00:00")
    _say(conn, ivan["id"], "2026-10-13T11:10:00", "out")                           # Иван is answered
    conn.execute("UPDATE avito_chat SET unread = 3")                               # unread is no waiting
    store.set_state(conn, "chats_baseline_%d" % account, "2026-10-13T09:00:00")
    conn.commit()
    return app, client, tok, conn, account, tanya, ivan


def test_the_menu_and_the_filter_count_the_same_waiting_chats_and_no_time_is_shown(tmp_path):
    app, client, tok, conn, account, tanya, ivan = _waiting_world(tmp_path)
    _new_view(conn)
    assert chats.waiting(conn, chats.list_chats(conn)) == {tanya["id"]}
    assert chats.waiting_total(conn) == 1 and chats.waiting_total(conn, account_id=account) == 1
    page = client.get("/chats").get_data(as_text=True)
    assert _menu_chats(page) == 1 and "Ждут ответа <b>1</b>" in page
    assert "Всего: 1" in client.get("/chats?f=wait").get_data(as_text=True)
    assert "Не прочитанные" not in page and "o2-cnt" not in page                   # no unread counters (п. 7)
    opened = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert "Клиент ждёт ответа</span>" in opened and not re.search(r"ждёт[^<]*\d+ (мин|ч|дн)", opened)   # a label, no time (п. 8)
    _say(conn, tanya["id"], "2026-10-13T11:50:00", "out")                         # answered: nobody waits, unread or not
    conn.commit()
    page = client.get("/chats").get_data(as_text=True)
    assert _menu_chats(page) == 0 and chats.waiting_total(conn) == 0 and "Ждут ответа <b>" not in page


def test_closed_and_old_chats_do_not_wait(tmp_path):
    app, client, tok, conn, account, tanya, ivan = _waiting_world(tmp_path)
    conn.execute("UPDATE avito_chat SET status = 'no_reply' WHERE id = ?", (tanya["id"],))
    conn.commit()
    assert chats.waiting_total(conn) == 0
    conn.execute("UPDATE avito_chat SET status = 'open' WHERE id = ?", (tanya["id"],))
    store.set_state(conn, "chats_baseline_%d" % account, "2026-10-13T12:00:00")  # older than the first read: the past
    conn.commit()
    assert chats.waiting_total(conn) == 0


def test_pick_and_wait_for_a_tyre_only_before_the_order_is_placed(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    _new_view(conn)
    tanya = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Татьяна")
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert "Подобрать позицию" in page and "Клиент ждёт эту шину" in page          # a new order
    placed = next(s for s in orders.list_stages(conn) if s["code"] == "placed")
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (placed["id"], tanya["order_id"]))
    conn.commit()
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert "Заказ №%d" % tanya["order_id"] in page
    assert "Подобрать позицию" not in page and "Клиент ждёт эту шину" not in page
    ivan = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Иван")      # no order: «Заказ по чату», create or link
    page = client.get("/chats/%d" % ivan["id"]).get_data(as_text=True)
    assert "Заказ по чату" in page and "Создать заказ" in page and "Привязать" in page and "Клиент ждёт эту шину" in page


def test_no_search_on_top_and_no_number_on_the_bell_and_no_templates_in_the_menu(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    for url in ("/", "/chats", "/orders", "/catalog", "/orders/templates"):
        page = client.get(url).get_data(as_text=True)
        assert 'id="topsearch"' not in page and "bell-count" not in page, url
        assert 'href="/orders/templates">' not in page.split("</aside>")[0], url   # «Шаблоны сообщений» is a tab of «Настройки заказов» (п. 19)
    menu = client.get("/orders/templates").get_data(as_text=True).split("</aside>")[0]
    assert re.search(r'class="sb-link is-active" href="/orders/settings"', menu)
