"""«Предоплата» of the card (the owner 2026-11-28): a checkbox and its sum in «Дополнительно» (the sum required while ticked), apart from
«Особые условия»; «Заказы» → «Фильтры»: «Отправка ТК» (order_intercity ticked) and «Предоплата» (ticked or a sum), with the other filters."""
from __future__ import annotations

from datetime import datetime

import pytest
from panel_helpers import login, make_app, post

from tires_sync import order_intercity, orders, oth_swap, store

NOW = "2026-11-28T12:00:00"


def db(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    return conn


def test_the_columns_come_with_the_migration_and_default_to_none(tmp_path):
    conn = db(tmp_path)
    cols = {r[1]: (r[2], r[4]) for r in conn.execute("PRAGMA table_info(order_card)")}
    assert cols["prepay"] == ("INTEGER", "0") and cols["prepay_amount"] == ("REAL", "0")
    store.connect(str(tmp_path / "x.db"))                                       # twice: nothing breaks
    cid = orders.create_card(conn, dict(client_name="Иван"), NOW, "o")
    card = orders.get_card(conn, cid)
    assert (card["prepay"], card["prepay_amount"]) == (0, 0)


def test_the_prepay_needs_its_sum_and_is_journaled(tmp_path):
    conn = db(tmp_path)
    cid = orders.create_card(conn, dict(client_name="Иван"), NOW, "o")
    with pytest.raises(orders.OrderError, match="укажите сумму предоплаты"):
        orders.update_card(conn, cid, {"prepay": "1", "prepay_amount": ""}, NOW, "o")
    with pytest.raises(orders.OrderError, match="укажите сумму предоплаты"):
        orders.update_card(conn, cid, {"prepay": "1"}, NOW, "o")                 # only the box changed (the window sends what changed)
    assert orders.get_card(conn, cid)["prepay"] == 0
    orders.update_card(conn, cid, {"prepay": "1", "prepay_amount": "5 000"}, NOW, "o")
    card = orders.get_card(conn, cid)
    assert (card["prepay"], card["prepay_amount"]) == (1, 5000.0)
    texts = [e["text"] for e in orders.list_events(conn, cid)]
    assert "Предоплата: да" in texts and "Сумма предоплаты: 5000" in texts
    with pytest.raises(orders.OrderError, match="укажите сумму предоплаты"):
        orders.update_card(conn, cid, {"prepay_amount": ""}, NOW, "o")           # the sum taken away while ticked
    for bad in ("много", "-1"):
        with pytest.raises(orders.OrderError):
            orders.update_card(conn, cid, {"prepay_amount": bad}, NOW, "o")
    orders.update_card(conn, cid, {"prepay": "", "prepay_amount": ""}, NOW, "o")
    card = orders.get_card(conn, cid)
    assert (card["prepay"], card["prepay_amount"]) == (0, 0)
    with pytest.raises(orders.OrderError, match="укажите сумму предоплаты"):
        orders.create_card(conn, dict(client_name="Пётр", prepay="1"), NOW, "o")
    new = orders.create_card(conn, dict(client_name="Пётр", prepay="1", prepay_amount="1.500"), NOW, "o")
    assert orders.get_card(conn, new)["prepay_amount"] == 1500.0


def test_a_prepaid_order_is_never_swapped_on_oth(tmp_path):
    conn = db(tmp_path)
    cid = orders.create_card(conn, dict(client_name="Иван"), NOW, "o")
    orders.update_card(conn, cid, {"prepay": "1", "prepay_amount": "2000"}, NOW, "o")
    assert oth_swap._client_terms(conn, orders.get_card(conn, cid)) == "В заказе предоплата клиента."


def window(tmp_path, new_view=False):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 11, 28, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    client.get("/orders")
    if new_view:
        store.set_state(conn, "ui_new.office", "all")
        conn.commit()
    return client, tok, conn


@pytest.mark.parametrize("new_view", [False, True])
def test_the_card_form_has_the_box_the_sum_and_refuses_the_box_alone(tmp_path, new_view):
    client, tok, conn = window(tmp_path, new_view)
    cid = orders.create_card(conn, dict(client_name="Иван"), NOW, "o")
    html = client.get("/orders/%d?tab=main" % cid).get_data(as_text=True)
    assert 'name="prepay"' in html and 'name="prepay_amount"' in html and "Сумма предоплаты" in html
    assert "Особые условия (перевод на карту" in html and "(предоплата," not in html
    r = post(client, "/orders/%d/save" % cid, tok, tab="main", client_name="Иван", prepay=["1", ""], prepay_amount="")
    assert orders.get_card(conn, cid)["prepay"] == 0
    page = r.get_data(as_text=True) if r.status_code == 200 else client.get(r.headers["Location"]).get_data(as_text=True)
    assert "укажите сумму предоплаты" in page and 'data-field-error="prepay_amount"' in page
    post(client, "/orders/%d/save" % cid, tok, tab="main", client_name="Иван", prepay=["1", ""], prepay_amount="3000")
    card = orders.get_card(conn, cid)
    assert (card["prepay"], card["prepay_amount"]) == (1, 3000.0)
    html = client.get("/orders/%d?tab=main" % cid).get_data(as_text=True)
    assert "Предоплата: " in html and 'value="3000"' in html
    post(client, "/orders/%d/save" % cid, tok, tab="main", client_name="Иван", prepay=[""], prepay_amount="")
    assert orders.get_card(conn, cid)["prepay"] == 0


def ids_on(html, cards):
    return {c for c in cards if "/orders/%d" % c in html}


@pytest.mark.parametrize("new_view", [False, True])
def test_the_filters_tk_and_prepay_keep_their_orders_and_combine(tmp_path, new_view):
    client, tok, conn = window(tmp_path, new_view)
    plain = orders.create_card(conn, dict(client_name="Анна", client_phone="+7 900 000 00 01"), NOW, "o")
    tk = orders.create_card(conn, dict(client_name="Борис", client_phone="+7 900 000 00 02"), NOW, "o")
    paid = orders.create_card(conn, dict(client_name="Вера", client_phone="+7 900 000 00 03", prepay="1", prepay_amount="1000"), NOW, "o")
    both = orders.create_card(conn, dict(client_name="Глеб", client_phone="+7 900 000 00 04", prepay_amount="500"), NOW, "o")   # a sum, no box
    for card in (tk, both):
        order_intercity.save(conn, card, dict(enabled="1", city="Тест", carrier="ПЭК"), NOW, "o")
    conn.commit()
    cards = {plain, tk, paid, both}
    for view in ("board", "list"):
        html = client.get("/orders?v=%s" % view).get_data(as_text=True)
        assert ids_on(html, cards) == cards
        assert 'name="tk"' in html and 'name="pp"' in html
        assert ids_on(client.get("/orders?v=%s&tk=1" % view).get_data(as_text=True), cards) == {tk, both}
        assert ids_on(client.get("/orders?v=%s&pp=1" % view).get_data(as_text=True), cards) == {paid, both}
        html = client.get("/orders?v=%s&tk=1&pp=1" % view).get_data(as_text=True)
        assert ids_on(html, cards) == {both}
        assert ids_on(client.get("/orders?v=%s&pp=1&q=%s" % (view, "Вера")).get_data(as_text=True), cards) == {paid}
    html = client.get("/orders?v=list&tk=1&pp=1").get_data(as_text=True)
    assert 'option value="1" selected' in html and "tk=1" in html and "pp=1" in html        # chosen and kept in the links


def test_the_excel_file_follows_the_flags(tmp_path):
    pytest.importorskip("openpyxl")
    client, tok, conn = window(tmp_path)
    orders.create_card(conn, dict(client_name="Анна", client_phone="+7 900 000 00 01"), NOW, "o")
    orders.create_card(conn, dict(client_name="Вера", client_phone="+7 900 000 00 03", prepay="1", prepay_amount="1000"), NOW, "o")
    import io
    import openpyxl
    for args, names in (("", {"Анна", "Вера"}), ("?pp=1", {"Вера"}), ("?tk=1", set())):
        book = openpyxl.load_workbook(io.BytesIO(client.get("/orders/export.xlsx" + args).data))
        cells = {str(c.value) for row in book.active.iter_rows() for c in row if c.value}
        assert {n for n in ("Анна", "Вера") if any(n in x for x in cells)} == names
