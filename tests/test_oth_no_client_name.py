"""Orders on «На ОТХ», «На возврат» and in «Вернули поставщику» after them are shown without the client (the owner 2026-11-09: «мы же
отвязываем клиентов и храним только в журнале»): «Мои дела на сегодня», the board, the search, the notifications, the tasks. The look only:
the order keeps client_name."""
from __future__ import annotations

from datetime import datetime

from tires_sync import analytics, inquiry, orders, search, store
from tires_sync.panel.routes_alerts import with_orders

NOW = "2026-11-09T12:00:00"
NAME, PHONE = "Илья Тестов", "+79000000001"


def db(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    return conn


def card(conn, code, detached=False):
    n = conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0]
    cid = orders.create_card(conn, {"client_name": NAME, "client_phone": PHONE[:-1] + str(n + 1)}, NOW, "olya")
    stage = next(s for s in orders.list_stages(conn) if s["code"] == code)
    conn.execute("UPDATE order_card SET stage_id = ?, responsible = 'olya' WHERE id = ?", (stage["id"], cid))
    if detached:
        conn.execute("INSERT INTO order_oth_client (order_id, client_id, how, linked_at, detached_at, detached_by) VALUES (?, 1, '', ?, ?, 'o')", (cid, NOW, NOW))
    conn.commit()
    return cid


def test_which_orders_are_shown_without_the_client(tmp_path):
    conn = db(tmp_path)
    oth, ret, back, back_plain, talk = card(conn, "oth"), card(conn, "return_given"), card(conn, "return", True), card(conn, "return"), card(conn, "talk")
    assert orders.unlinked_orders(conn) == {oth, ret, back}
    assert orders.unlinked_orders(conn, [ret, talk]) == {ret} and orders.unlinked_orders(conn, []) == set()
    assert orders.get_card(conn, oth)["client_name"] == NAME                                    # the data stays
    assert back_plain not in orders.unlinked_orders(conn)


def test_my_day_shows_the_order_without_the_name(tmp_path):
    conn = db(tmp_path)
    oth, ret, talk = card(conn, "oth"), card(conn, "return_given"), card(conn, "talk")
    mine = {o["id"]: o["client"] for o in analytics.my_day(conn, "olya", datetime(2026, 11, 9, 12))["orders"]}
    assert mine == {oth: "", ret: "", talk: NAME}


def test_the_board_shows_no_client_on_the_return_and_the_mim_title_on_oth(tmp_path):
    conn = db(tmp_path)
    oth, ret, talk = card(conn, "oth"), card(conn, "return_given"), card(conn, "talk")
    shown = {c["id"]: c["client"] for col in orders.board(conn, datetime(2026, 11, 9, 12))["columns"] for c in col["cards"]}
    assert shown[ret] == orders.NO_CLIENT and shown[talk] == NAME
    assert NAME not in shown[oth] and "MIM" in shown[oth]


def test_the_search_finds_the_order_and_shows_no_client(tmp_path):
    conn = db(tmp_path)
    oth = card(conn, "oth")
    found = search._orders(conn, search.parse("№%d" % oth), search.scope_for("owner"), 10)
    assert [f["title"] for f in found] == ["Заказ №%d · клиента нет" % oth]


def test_the_notifications_and_the_tasks_show_no_client(tmp_path):
    conn = db(tmp_path)
    oth, talk = card(conn, "oth"), card(conn, "talk")
    rows = with_orders(conn, [{"order_id": oth}, {"order_id": talk}])
    assert [r["order"]["client"] for r in rows] == ["", NAME]
    assert inquiry._who_called(conn, {"client_id": None, "order_id": oth, "chat_row": None}) == ("", "")
    assert inquiry._who_called(conn, {"client_id": None, "order_id": talk, "chat_row": None}) == (NAME, PHONE[:-1] + "2")


def test_the_main_page_writes_the_number_without_the_name(tmp_path):
    from panel_helpers import login, make_app
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 11, 9, 9, 0, 0))
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    oth = card(conn, "oth")
    conn.execute("UPDATE order_card SET responsible = 'owner' WHERE id = ?", (oth,))
    conn.commit()
    client = app.test_client()
    login(client)
    page = client.get("/").get_data(as_text=True)
    assert "Заказ №%d</div>" % oth in page and NAME not in page
