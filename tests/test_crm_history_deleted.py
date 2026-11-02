"""CRM, the client's history keeps what was deleted (the owner 2026-11-02): a position removed from its order and an order in «Удалённые»
stay in «Заказы и покупки», marked «удалён», never counted in a sum; a restored order is shown as a normal one again; the link of a deleted
order (client_order) breaks no count."""
from __future__ import annotations

import pytest

from order_fixtures import card_of_phone
from panel_helpers import login, make_app
from tires_sync import clients, orders, store, trash, users

NOW = "2026-11-02T12:00:00"
LATER = "2026-11-02T13:00:00"


@pytest.fixture
def conn(tmp_path):
    c = store.connect(str(tmp_path / "t.db"))
    orders.ensure_pipeline(c, NOW)
    return c


def _order(conn, phone="+79001112233", name="Иван"):
    card = card_of_phone(conn, {"client_name": name, "client_phone": phone}, NOW, "o")
    conn.commit()
    return card


def _item(conn, order, name, qty, unit, goods="g1"):
    row = conn.execute("INSERT INTO order_item (order_id, goods_id, name, qty, supplier_price, sale_unit) VALUES (?, ?, ?, ?, 1000, ?)",
                       (order, goods, name, qty, unit)).lastrowid
    conn.commit()
    return row


def _view(conn, order):
    client = clients.client_of_order(conn, order)
    return clients.purchases_view(conn, clients.get_client(conn, client["id"])), client["id"]


def test_a_removed_position_stays_in_the_history_marked_and_out_of_the_sum(conn):
    order = _order(conn)
    _item(conn, order, "Nokian Hakkapeliitta 10 205/55 R16 94T", 4, 6250)
    gone = _item(conn, order, "Ikon Character Ice 7 195/65 R15 95T", 2, 5000, goods="g2")
    orders.remove_item(conn, gone, LATER, "manager")
    assert conn.execute("SELECT COUNT(*) FROM order_item WHERE order_id = ?", (order,)).fetchone()[0] == 1
    kept = conn.execute("SELECT item_id, order_id, name, qty, sale_unit, removed_at, removed_by FROM order_item_removed").fetchall()
    assert kept == [(gone, order, "Ikon Character Ice 7 195/65 R15 95T", 2, 5000.0, LATER, "manager")]
    view, _ = _view(conn, order)
    p = view[0]
    assert not p["deleted"] and p["total"] == 25000                          # the removed position is not in the sum
    assert [(i["name"], i["size"], i["qty"], i["sum"], i["deleted"]) for i in p["items"]] == [
        ("Nokian Hakkapeliitta 10 205/55 R16 94T", "205/55 R16", 4, 25000, False),
        ("Ikon Character Ice 7 195/65 R15 95T", "195/65 R15", 2, 10000, True)]


def test_a_removal_refused_by_its_guard_keeps_no_record(conn, monkeypatch):
    order = _order(conn)
    item = _item(conn, order, "Ikon 205/55 R16", 4, 6000)

    def refuse(*_a, **_k):
        raise orders.OrderError("нельзя")
    monkeypatch.setattr(orders, "_guard_stage", refuse)
    with pytest.raises(orders.OrderError):
        orders.remove_item(conn, item, LATER, "manager")
    assert conn.execute("SELECT COUNT(*) FROM order_item_removed").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM order_item WHERE id = ?", (item,)).fetchone()[0] == 1


def test_a_position_back_under_the_same_id_is_shown_once_as_it_is(conn):
    order = _order(conn)
    item = _item(conn, order, "Ikon 205/55 R16", 4, 6000)
    orders.remove_item(conn, item, LATER, "manager")
    conn.execute("INSERT INTO order_item (id, order_id, goods_id, name, qty, supplier_price, sale_unit) VALUES (?, ?, 'g1', 'Ikon 205/55 R16', 4, 1000, 6000)",
                 (item, order))
    conn.commit()
    view, _ = _view(conn, order)
    assert [(i["name"], i["deleted"]) for i in view[0]["items"]] == [("Ikon 205/55 R16", False)] and view[0]["total"] == 24000


def test_a_deleted_order_stays_in_the_history_and_comes_back_normal_when_restored(conn):
    pid = conn.execute("INSERT INTO point (name) VALUES ('Бета')").lastrowid
    order = _order(conn)
    conn.execute("UPDATE order_card SET point_id = ? WHERE id = ?", (pid, order))
    _item(conn, order, "Nokian 205/55 R16", 4, 6250)
    removed = _item(conn, order, "Шиномонтаж", 1, 1000, goods="g2")
    orders.remove_item(conn, removed, NOW, "manager")
    other = _order(conn)                                                    # the same phone: the same client, a live order
    _item(conn, other, "Ikon 195/65 R15", 2, 5000)
    cid = clients.client_of_order(conn, order)["id"]
    record = trash.delete_order(conn, order, LATER, "owner")
    client = clients.get_client(conn, cid)
    assert [o["id"] for o in client["orders"]] == [other]                    # the live orders: what the rest of the card counts
    view = clients.purchases_view(conn, client)
    assert [(p["id"], p["deleted"]) for p in view] == [(other, False), (order, True)]
    gone = view[1]
    assert gone["point"] == "Бета" and gone["total"] == 0 and not gone["won"] and not gone["lost"] and gone["stage"]
    assert [(i["name"], i["qty"], i["deleted"]) for i in gone["items"]] == [("Nokian 205/55 R16", 4, True), ("Шиномонтаж", 1, True)]
    trash.restore_order(conn, record, "2026-11-02T14:00:00", "owner")
    view = clients.purchases_view(conn, clients.get_client(conn, cid))
    back = [p for p in view if p["id"] == order][0]
    assert not back["deleted"] and back["total"] == 25000 and len(view) == 2
    assert [(i["name"], i["deleted"]) for i in back["items"]] == [("Nokian 205/55 R16", False), ("Шиномонтаж", True)]


def test_a_deleted_order_again_after_its_restore_is_shown_once(conn):
    order = _order(conn)
    _item(conn, order, "Ikon 205/55 R16", 4, 6000)
    cid = clients.client_of_order(conn, order)["id"]
    first = trash.delete_order(conn, order, NOW, "owner")
    trash.restore_order(conn, first, LATER, "owner")
    trash.delete_order(conn, order, "2026-11-02T15:00:00", "owner")
    gone = clients.get_client(conn, cid)["deleted_orders"]
    assert [(o["id"], o["deleted_at"]) for o in gone] == [(order, "2026-11-02T15:00:00")]


def test_the_link_of_a_deleted_order_breaks_no_count(conn):
    order = _order(conn)
    _item(conn, order, "Ikon 205/55 R16", 4, 6000)
    cid = clients.client_of_order(conn, order)["id"]
    trash.delete_order(conn, order, LATER, "owner")
    assert conn.execute("SELECT client_id FROM client_order WHERE order_id = ?", (order,)).fetchone() == (cid,)   # the link is kept
    row = [r for r in clients.list_clients(conn) if r["id"] == cid][0]
    assert row["orders"] == 0
    client = clients.get_client(conn, cid)
    card = clients.card_view(conn, client, {}, clients.purchases(conn, cid), False)
    assert {f["label"]: f["value"] for f in card["fields"]}["Заказов"] == 0 and card["orders"] == []
    assert clients.purchases(conn, cid) == 0
    conn.execute("DELETE FROM deleted_record")                                # a link whose order is nowhere: nothing in the history
    conn.commit()
    assert clients.purchases_view(conn, clients.get_client(conn, cid)) == []


def test_the_card_page_marks_the_deleted_order_and_position(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    live = _order(conn)
    _item(conn, live, "Nokian 205/55 R16", 4, 6250)
    orders.remove_item(conn, _item(conn, live, "Шиномонтаж", 1, 1000, goods="g2"), NOW, "manager")
    gone = _order(conn)
    _item(conn, gone, "Ikon 195/65 R15", 2, 5000)
    cid = clients.client_of_order(conn, live)["id"]
    trash.delete_order(conn, gone, LATER, "owner")
    conn.close()
    owner = app.test_client()
    login(owner)
    page = owner.get("/clients/%d" % cid).get_data(as_text=True)
    assert page.count(">удалён</span>") == 3                                 # the deleted order, its position, the removed position
    assert '<a href="/orders/%d">' % live in page and '<a href="/orders/%d">' % gone not in page
    assert "split-order/%d" % gone not in page
