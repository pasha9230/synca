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


def test_the_main_page_of_a_chats_manager_has_no_feed_and_no_line_to_a_closed_page(tmp_path):
    from panel_helpers import make_app
    from tires_sync import users
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    users.add_user(conn, "olya", "пароль-для-теста-1", "2026-10-21T10:00:00", role="chats")
    client = app.test_client()
    with client.session_transaction() as s:
        s["user"], s["epoch"], s["csrf"] = "olya", store.get_session_epoch(conn, "olya"), "tok"
    page = client.get("/").get_data(as_text=True)
    assert "Главная" in page and "<h3>Фид</h3>" not in page and "Последний запуск" not in page
    for href in re.findall(r'<a class="btn btn--secondary btn--sm" href="([^"]+)"', page):
        assert client.get(href).status_code == 200, href                           # every «Открыть» / «Сопоставить» opens (Б1)


def test_the_templates_are_a_tab_of_the_order_settings_text_left_variables_right(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    page = client.get("/orders/templates").get_data(as_text=True)
    assert "<h1>Настройки заказов" in page and 'href="/orders/settings">Воронка</a>' in page and "Шаблоны сообщений</a>" in page
    assert page.count('class="tpl2-grid"') >= 5 and 'class="tpl-var" data-insert="{client_name}"' in page
    assert "<strong>Клиенту:" not in page and "<strong>Менеджеру:" not in page
    settings = client.get("/orders/settings").get_data(as_text=True)
    assert "<h1>Настройки заказов" in settings and 'href="/orders/templates">Шаблоны сообщений</a>' in settings


def test_the_catalog_tab_all_counts_what_is_found(tmp_path):
    """п. 9: «Все N» = «Найдено N позиций»; a supplier's tab counts what the filters find there too."""
    from test_panel_suppliers import setup as suppliers
    app, client, tok, conn = suppliers(tmp_path)

    def tabs(html):
        return {name: int(n) for name, n in re.findall(r'>(Все|WebMIM|Второй) <span class="badge badge--neutral">(\d+)</span>', html)}

    def found(html):
        return int(re.search(r"Найдено (\d+) позиц", html).group(1))
    html = client.get("/catalog").get_data(as_text=True)
    assert tabs(html)["Все"] == found(html) == tabs(html)["WebMIM"] + tabs(html)["Второй"]
    second = client.get("/catalog?sup=2").get_data(as_text=True)
    assert tabs(second) == tabs(html) and tabs(second)["Второй"] == found(second)
    none = client.get("/catalog?q=нет-такой-шины").get_data(as_text=True)
    assert tabs(none)["Все"] == found(none) == 0


def test_the_menu_counts_the_account_the_chats_open_with(tmp_path):
    """Review 08.11 (59 in the menu, 14 in the filter): the menu counts what its link opens — the one account the person answers for."""
    from tires_sync import connections
    app, client, tok, conn, account, tanya, ivan = _waiting_world(tmp_path)
    _new_view(conn)
    other = connections.create_account(conn, "Второй", "2026-10-13T09:00:00", "o")
    connections.set_modes(conn, other, True, True, "2026-10-13T09:00:00", "o")
    conn.execute("UPDATE account SET chats_enabled = 1")
    conn.execute("UPDATE avito_chat SET account_id = ? WHERE id = ?", (other, ivan["id"]))
    conn.execute("DELETE FROM avito_message WHERE chat_row = ? AND direction = 'out'", (ivan["id"],))   # Иван waits too, on the other account
    store.set_state(conn, "chats_baseline_%d" % other, "2026-10-13T09:00:00")
    conn.commit()
    assert chats.waiting_total(conn) == 2
    page = client.get("/chats").get_data(as_text=True)
    assert _menu_chats(page) == 2 and "Ждут ответа <b>2</b>" in page              # nobody answers for an account: all of them, both
    connections.set_responsible(conn, account, app.config["AUTHOR"], [app.config["AUTHOR"]], "2026-10-13T09:00:00", "o")
    page = client.get("/chats").get_data(as_text=True)
    assert _menu_chats(page) == 1 and "Ждут ответа <b>1</b>" in page              # the person's account: the same one in both
