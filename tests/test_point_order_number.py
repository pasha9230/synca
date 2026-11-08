"""The order's window of a point shows the order by its WebMIM number, never the panel's inner number (the owner 08.11, the prototype of the
audit: «у точки заказ по № MIM»); the office keeps «Заказ №N»."""
from __future__ import annotations

import re
from datetime import datetime

import pytest

from panel_helpers import login, make_app
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
    text = page(manager(w["app"], w["conn"], w["pid"]), w["cid"])
    assert "<title>Заказ № 7712345 · Tires Sync</title>" in text
    assert "№ 7712345" in text
    assert not re.search(r"№\s?%d\b" % w["cid"], text)


def test_without_a_webmim_number_the_point_sees_the_panel_mark(w):
    text = page(manager(w["app"], w["conn"], w["pid"]), w["bare"])
    assert "<title>Заказ № панель %d · Tires Sync</title>" % w["bare"] in text


def test_the_office_keeps_its_number(w):
    client = w["app"].test_client()
    login(client)
    assert "<title>Заказ №%d · Tires Sync</title>" % w["cid"] in page(client, w["cid"])
