"""«Список» of «Заказы» as the owner's Google table (the owner 2026-10-29): a row per position with the values of order_sheet, the totals
of the whole filter, the person's own columns (user_pref), and no purchase nor profit for a person without the right «money» (a point)."""
from __future__ import annotations

import pytest

from panel_helpers import make_app, shared_mark_of
from test_roles import person
from test_webmim_create import NOW, card, point
from tires_sync import order_sheet as sheet, orders, orders_list, store, user_prefs, users

MONEY_HEADS = ("<th class=\"num\">Цена MIM</th>", "<th class=\"num\">Сумма MIM</th>", "<th class=\"num\">Прибыль %</th>", "<th class=\"num\">Прибыль ₽</th>")


def rub(value):
    return "{:,.0f}".format(value).replace(",", " ") + " ₽"


@pytest.fixture
def panel(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    conn.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = "owner", 0, "tok"
        session["pw"] = shared_mark_of(client.application)
    return app, conn, client


def heads(html):
    part = html.split('<table class="table table--plain orders-sheet">', 1)[1].split("</thead>", 1)[0]
    found = [h.rsplit(">", 1)[1] for h in part.split("</th>")[:-1]]
    assert found[0] == "№"                                                 # the order of the panel with its dot: always first, not a column to choose
    return found[1:]


def sold(conn, card_id):
    stage = next(s for s in orders.list_stages(conn) if s["code"] == "done")
    orders.move_card(conn, card_id, stage["id"], NOW, "owner")
    orders.record_sales(conn, card_id, "2026-10-23", NOW, "owner", "2026-10-23")


def test_the_default_columns_in_the_owners_order(panel):
    app, conn, client = panel
    assert orders_list.COLUMNS == ("Дата счета", "№ заказа MIM", "Перемещение MIM", "Артикул", "Наименование", "Кол-во", "Цена MIM", "Сумма MIM",
                                   "Цена продажи", "Сумма продажи", "Имя", "Телефон", "ПВЗ", "Этап", "Реализация", "Списание", "Прибыль %",
                                   "Прибыль ₽", "Примечание")
    assert set(orders_list.COLUMNS) <= set(sheet.COLUMNS)                          # the names of the Google table, none of the panel's own
    card(conn)
    assert heads(client.get("/orders?v=list").get_data(as_text=True)) == list(orders_list.COLUMNS)
    assert [c["name"] for c in orders_list.choice(conn, "owner", False)] == [c for c in orders_list.COLUMNS if c not in orders_list.MONEY]


def test_a_row_is_what_goes_to_the_google_table(panel):
    app, conn, client = panel
    mine = card(conn, goods=(("111", 4), ("222", 2)), comment="Хочу сегодня")
    orders.update_card(conn, mine, {"webmim_number": "1234567", "transfer_number": "26R-922352"}, NOW, "owner")
    sold(conn, mine)
    expected = list(sheet.rows_of(conn, mine, NOW).values())
    got = orders_list.rows(conn, [mine], True)
    assert [r["order"] for r in got] == [mine, mine]
    assert [r["values"] for r in got] == [{c: e[c] for c in orders_list.COLUMNS} for e in expected]
    assert all(isinstance(r["values"]["Прибыль ₽"], float) for r in got)                       # the sale is written: the analytics' profit
    html = client.get("/orders?v=list").get_data(as_text=True)
    assert 'data-href="/orders/%d"' % mine in html and "26R-922352" in html and "Шина 222" in html
    assert rub(expected[0]["Сумма MIM"]) in html and rub(expected[0]["Прибыль ₽"]) in html


def test_an_order_without_positions_still_has_its_row(panel):
    app, conn, client = panel
    mine = card(conn, goods=(), client_name="Пётр")
    got = orders_list.rows(conn, [mine], True)
    assert len(got) == 1 and got[0]["values"]["Имя"] == "Пётр" and got[0]["values"]["Артикул"] == "" and got[0]["values"]["Сумма MIM"] == ""
    assert 'data-href="/orders/%d"' % mine in client.get("/orders?v=list").get_data(as_text=True)


def test_the_totals_count_every_page_of_the_filter(panel, monkeypatch):
    app, conn, client = panel
    monkeypatch.setattr(orders_list, "PAGE", 1)
    first = card(conn, goods=(("111", 4),), client_name="Анна")                # 16 000 / 20 000
    second = card(conn, goods=(("222", 2),), client_name="Борис")              # 8 000 / 10 000
    sold(conn, first)
    card(conn, goods=(("333", 1),), client_name="Зоя", tags="другой")           # out of the filter
    html = client.get("/orders?v=list&t=тест").get_data(as_text=True)
    assert 'data-href="/orders/%d"' % second in html and 'data-href="/orders/%d"' % first not in html          # newest first, one per page
    profit = sum(r["values"]["Прибыль ₽"] for r in orders_list.rows(conn, [first], True))
    line = html.split('class="small muted orders-total-line">', 1)[1].split("</p>", 1)[0]
    assert rub(24000) in line and rub(30000) in line and rub(profit) in line
    assert ("%.1f" % (profit * 100.0 / 20000)).replace(".", ",") + " %" in line                # over the sale of the positions sold
    assert "page=2" in html and "t=%D1%82%D0%B5%D1%81%D1%82" in html                             # the pages keep the filter
    assert 'data-href="/orders/%d"' % first in client.get("/orders?v=list&t=тест&page=2").get_data(as_text=True)
    assert orders_list.totals([], True) == {"Сумма MIM": 0, "Сумма продажи": 0, "Прибыль ₽": 0, "Прибыль %": None}


def test_the_columns_are_saved_and_reset(panel):
    app, conn, client = panel
    card(conn)
    order = ["Имя", "Этап"] + [c for c in orders_list.COLUMNS if c not in ("Имя", "Этап")]
    r = client.post("/orders/list/columns", data={"csrf": "tok", "order": order, "shown": [c for c in order if c != "Телефон"], "q": "Иван"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/orders?v=list&q=%D0%98%D0%B2%D0%B0%D0%BD")
    shown = heads(client.get("/orders?v=list").get_data(as_text=True))
    assert shown[:2] == ["Имя", "Этап"] and "Телефон" not in shown and len(shown) == len(orders_list.COLUMNS) - 1
    assert client.post("/orders/list/columns", data={"csrf": "tok", "order": order}).status_code == 302          # nothing shown: refused
    assert heads(client.get("/orders?v=list").get_data(as_text=True)) == shown
    client.post("/orders/list/columns", data={"csrf": "tok", "reset": "1"})
    assert heads(client.get("/orders?v=list").get_data(as_text=True)) == list(orders_list.COLUMNS)
    assert client.post("/orders/list/columns", data={"order": order, "shown": order}).status_code == 400        # no CSRF token


def test_a_column_the_saved_order_does_not_name_comes_at_its_place(panel):
    app, conn, client = panel
    user_prefs.put(conn, "owner", orders_list.PREF_KEY, {"order": ["Примечание", "Имя", "чужое"], "hidden": ["Имя"]}, NOW)
    got = orders_list.choice(conn, "owner", True)
    assert [c["name"] for c in got][:2] == ["Дата счета", "№ заказа MIM"] and len(got) == len(orders_list.COLUMNS)
    assert [c["name"] for c in got].index("Примечание") < [c["name"] for c in got].index("Имя")
    assert {c["name"] for c in got if not c["shown"]} == {"Имя"}
    user_prefs.put(conn, "owner", orders_list.PREF_KEY, ["не словарь"], NOW)
    assert [c["name"] for c in orders_list.choice(conn, "owner", True)] == list(orders_list.COLUMNS)


def test_a_point_never_gets_the_purchase_nor_the_profit(panel):
    app, conn, client = panel
    pid, other = point(conn), point(conn, name="Дельта")
    mine = card(conn, client_name="Свой", point_id=pid)
    card(conn, client_name="Чужой", point_id=other)
    sold(conn, mine)
    manager, _ = person(app, "point", point_id=pid)
    html = manager.get("/orders?v=list").get_data(as_text=True)
    assert "Свой" in html and "Чужой" not in html
    assert not any(h in html for h in MONEY_HEADS) and "Итого" not in html and "orders-total-line" not in html
    assert rub(16000) not in html and "Цена MIM" not in html and "Прибыль" not in html
    assert "Цена продажи" not in html and "Сумма продажи" not in html and "₽" not in html     # no sums for a point (the owner 2026-10-26)
    every = list(orders_list.COLUMNS)                                     # a saved choice with the money: not taken, not shown
    assert manager.post("/orders/list/columns", data={"csrf": "tok", "order": every, "shown": every}).status_code == 302
    saved = user_prefs.get(conn, "point1", orders_list.PREF_KEY)
    assert not (orders_list.MONEY | orders_list.SALE) & set(saved["order"])
    user_prefs.put(conn, "point1", orders_list.PREF_KEY, {"order": every, "hidden": []}, NOW)
    html = manager.get("/orders?v=list").get_data(as_text=True)
    assert "Цена MIM" not in html and "Прибыль" not in html and "Сумма MIM" not in html and "Сумма продажи" not in html
    assert all(not (orders_list.MONEY | orders_list.SALE) & set(r["values"]) for r in orders_list.rows(conn, [mine], False, point=True))
    chats, _ = person(app, "chats")                                       # no right «money»: no purchase either
    page = chats.get("/orders?v=list").get_data(as_text=True)
    assert "Цена MIM" not in page and "Прибыль ₽" not in page and "Свой" in page and "Сумма продажи" in page
    partner, _ = person(app, "partner")
    assert all(h in partner.get("/orders?v=list").get_data(as_text=True) for h in MONEY_HEADS)


def test_the_note_a_point_reads_has_no_account_name(panel):
    app, conn, client = panel
    pid = point(conn)
    conn.execute("INSERT INTO account (name, feed_token, created) VALUES ('Шинный Двор', 't-1', 'x')")
    conn.commit()
    mine = card(conn, point_id=pid, comment="из Шинный Двор")
    got = orders_list.rows(conn, [mine], False, point=True)
    assert "Шинный Двор" not in got[0]["values"]["Примечание"] and "аккаунт Авито" in got[0]["values"]["Примечание"]
    manager, _ = person(app, "point", point_id=pid)
    assert "Шинный Двор" not in manager.get("/orders?v=list").get_data(as_text=True)
    assert "Шинный Двор" in client.get("/orders?v=list").get_data(as_text=True)


def test_a_setting_is_the_persons_own(panel):
    app, conn, client = panel
    user_prefs.put(conn, "anna", "orders.list.columns", {"order": ["Имя"]}, NOW)
    assert user_prefs.get(conn, "boris", "orders.list.columns") is None
    assert user_prefs.get(conn, "", "orders.list.columns", 1) == 1
    assert user_prefs.get(conn, "anna", "orders.list.columns") == {"order": ["Имя"]}
    user_prefs.drop(conn, "boris", "orders.list.columns")
    assert user_prefs.get(conn, "anna", "orders.list.columns") == {"order": ["Имя"]}
    with pytest.raises(user_prefs.PrefError):
        user_prefs.put(conn, "anna", "x", "я" * user_prefs.PREF_BYTES, NOW)
    with pytest.raises(user_prefs.PrefError):
        user_prefs.get(conn, "anna", "'; DROP TABLE user_pref; --")
    partner, _ = person(app, "partner")
    client.post("/orders/list/columns", data={"csrf": "tok", "order": ["Имя"], "shown": ["Имя"]})       # the owner's choice
    card(conn)
    assert heads(partner.get("/orders?v=list").get_data(as_text=True)) == list(orders_list.COLUMNS)
    assert heads(client.get("/orders?v=list").get_data(as_text=True))[0] == "Имя"


def test_a_total_shows_under_its_column_even_first(panel):
    app, conn, client = panel
    card(conn, goods=(("111", 4),))
    order = ["Сумма продажи"] + [c for c in orders_list.COLUMNS if c != "Сумма продажи"]
    client.post("/orders/list/columns", data={"csrf": "tok", "order": order, "shown": order})
    foot = client.get("/orders?v=list").get_data(as_text=True).split("<tfoot>", 1)[1].split("</tfoot>", 1)[0]
    cells = foot.split("</td>")
    assert "Итого" in cells[0] and rub(20000) in cells[1]
