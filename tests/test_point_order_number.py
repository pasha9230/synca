"""A point knows the order by its WebMIM number, never the panel's inner number (the owner 08.11, the prototype of the audit: «у точки заказ по
№ MIM»); the office keeps «Заказ №N». Since the owner 13.11 a point opens no card of our order: «Рабочее место» shows the number, and the
card sends the point there, searching by the WebMIM number (the id of the order when it has none)."""
from __future__ import annotations

import re
from datetime import datetime

import pytest

from panel_helpers import assert_card_closed_to_point, login, make_app
from test_mypoint import manager
from tires_sync import order_points as points, orders, store, users

NOW = "2026-11-08T12:00:00"


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


@pytest.fixture
def w(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 11, 8, 12, 0, 0))
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    pid = points.save_point(conn, {"name": "Юнона", "manager_name": "А", "channel": "panel"})
    stages = {s["code"]: s for s in orders.list_stages(conn)}
    cid = orders.create_card(conn, {"client_name": "Иван Клиентов", "point_id": str(pid)}, NOW, "o")
    conn.execute("UPDATE order_card SET stage_id = ?, webmim_number = '7712345' WHERE id = ?", (stages["at_point"]["id"], cid))
    bare = orders.create_card(conn, {"client_name": "Пётр", "point_id": str(pid)}, NOW, "o")
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stages["at_point"]["id"], bare))
    conn.commit()
    return dict(app=app, conn=conn, pid=pid, cid=cid, bare=bare)


def page(client, cid):
    r = client.get("/orders/%d" % cid)
    assert r.status_code == 200
    return r.get_data(as_text=True)


def test_the_point_sees_the_webmim_number_and_no_inner_one(w):
    point = manager(w["app"], w["conn"], w["pid"])
    where = assert_card_closed_to_point(point, w["cid"], query="7712345", hidden=("Иван Клиентов",))      # the search is by the WebMIM number
    text = point.get(where).get_data(as_text=True)                                                  # «Рабочее место», where the card sends
    assert "№ 7712345" in text
    assert not re.search(r"№\s?%d\b" % w["cid"], text)


def test_without_a_webmim_number_the_point_sees_the_panel_mark(w):
    point = manager(w["app"], w["conn"], w["pid"])
    where = assert_card_closed_to_point(point, w["bare"], query=str(w["bare"]), hidden=("Пётр",))     # no number: the search is by the id
    assert "№ панель %d" % w["bare"] in point.get(where).get_data(as_text=True)


def test_the_office_keeps_its_number(w):
    client = w["app"].test_client()
    login(client)
    assert "<title>Заказ №%d · Tires Sync</title>" % w["cid"] in page(client, w["cid"])
