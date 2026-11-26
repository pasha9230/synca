"""«Переданные точкой» (ТЗ 95): the funnel of the clients the points handed over and the slice of «Отчёт по заказам»."""
from __future__ import annotations

import re

from test_point_income import END, NOW, START, handed, panel, sell

from tires_sync import analytics, order_report, orders


def test_the_slice_of_the_report(tmp_path):
    app, owner, tok, conn, a, b = panel(tmp_path)                 # Альфа handed order 2 (issued by Бета)
    only = order_report.build(conn, START, END, handed_slice="only")
    assert {r["point"]: r["orders"] for r in only["lines"]} == {"Бета": 1, "Альфа": 0}
    assert only["handoff"]["fee"] == 200
    none = order_report.build(conn, START, END, handed_slice="none")
    assert {r["point"]: r["orders"] for r in none["lines"]} == {"Альфа": 4} and none["handoff"]["fee"] == 0
    assert order_report.build(conn, START, END, handed_slice="only", handed_by=b)["lines"] == []
    page = owner.get("/orders/report?from=%s&to=%s&hs=only" % (START, END)).get_data(as_text=True)
    assert "Только переданные точкой" in page and re.search(r"<td>Бета</td><td>1</td>", page)
    page = owner.get("/orders/report?from=%s&to=%s&hp=%d" % (START, END, b)).get_data(as_text=True)
    assert "За период продаж по заказам нет." in page
    assert owner.get("/orders/report/export.csv?from=%s&to=%s&hs=none" % (START, END)).status_code == 200


def test_the_funnel_of_the_handed_clients(tmp_path):
    app, owner, tok, conn, a, b = panel(tmp_path)
    stages = {s["kind"]: s["id"] for s in orders.list_stages(conn)}
    for _ in range(10):                                           # the ids of the orders of base() are taken by their sales
        orders.create_card(conn, {"client_name": "-"}, NOW, "o")
    done = orders.create_card(conn, {"client_name": "А", "point_id": b}, NOW, "o")
    work = orders.create_card(conn, {"client_name": "Б", "point_id": b}, NOW, "o")
    lost = orders.create_card(conn, {"client_name": "В", "point_id": b}, NOW, "o")
    for card in (done, work):
        handed(conn, card, a)
    conn.execute("INSERT INTO order_source_point (order_id, point_id, at, author) VALUES (?, ?, ?, 'o')", (lost, a, NOW))   # by phone (ТЗ 92)
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stages["won"], done))
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stages["lost"], lost))
    conn.commit()
    sell(conn, done, b, pieces=4)
    found = analytics.handed_funnel(conn, START, END)
    [row] = [r for r in found["rows"] if r["point"] == "Альфа"]
    assert (row["handed"], row["work"], row["done"], row["lost"], row["tyres"]) == (4, 2, 1, 1, 4)          # + order 2 of base(), its card open
    page = owner.get("/analytics?from=%s&to=%s" % (START, END)).get_data(as_text=True)
    assert "Переданные точкой" in page and 'data-handed-point="%d"><td>Альфа</td><td>4</td><td>2</td><td>1</td><td>1</td><td>4</td>' % a in page
