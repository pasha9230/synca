"""«Передано с точки» is the first stage of the pipeline, before «Новый заказ» (the owner 2026-10-23: «Передано точкой должно стоять в воронке
до нового заказа»); the quick add of the board stays with «Новый заказ», and a new order from the board or the window is made there."""
from __future__ import annotations

from test_order_window import created, setup  # noqa: F401
from panel_helpers import post
from tires_sync import orders


def _column(html: str, stage_id: int) -> str:
    start = html.index('data-stage="%d"' % stage_id)
    nxt = html.find('<section class="col', start)
    return html[start:nxt if nxt > 0 else len(html)]


def test_the_stage_of_the_points_comes_first(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    orders.ensure_from_point_stage(conn)
    assert [s["code"] for s in orders.list_stages(conn)][:2] == ["from_point", "new"]


def test_the_quick_add_is_in_the_column_of_the_new_orders_and_new_orders_go_there(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    stage = {s["code"]: s["id"] for s in [orders.ensure_from_point_stage(conn)] + orders.list_stages(conn)}
    html = client.get("/orders").get_data(as_text=True)
    assert html.count('class="quick"') == 1
    assert 'class="quick"' in _column(html, stage["new"]) and 'class="quick"' not in _column(html, stage["from_point"])
    quick = orders.get_card(conn, created(post(client, "/orders/new", tok, client_name="Иван")))                       # the board
    window = orders.get_card(conn, created(post(client, "/orders/new", tok, client_name="Антон", item_goods_id=["100"], item_query=[""],
                                                item_qty=["4"], item_sale_unit=["5000"])))                                 # the window
    assert quick["stage"]["code"] == "new" and window["stage"]["code"] == "new"


def test_without_the_new_orders_the_quick_add_stays_in_the_first_column(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    conn.execute("UPDATE order_stage SET code = '' WHERE code = 'new'")                     # an old base: no system «Новый заказ»
    conn.commit()
    first = orders.list_stages(conn)[0]["id"]
    html = client.get("/orders").get_data(as_text=True)
    assert html.count('class="quick"') == 1 and 'class="quick"' in _column(html, first)
