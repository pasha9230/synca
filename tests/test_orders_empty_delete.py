"""«Удалить пустые заказы» of «Ещё» in «Заказы» (the owner 2026-11-26): only the empty orders «Новый заказ» left (orders.EMPTY_SQL and nothing
else typed in the card), made 10 minutes ago or earlier, go to «Удалённые»; the confirmation names how many; a second press or a list changed
meanwhile removes nothing; one record of the journal says how many and who; the right is the right to delete an order (owner, partner)."""
from __future__ import annotations

import datetime as dt
import json

import pytest

from panel_helpers import login, make_app, post
from test_roles import person
from tires_sync import order_cleanup as cleanup, order_wait, orders, store, trash
from tires_sync.panel import texts
from tires_sync.panel.routes_design import UI_KEY

OLD = "2026-11-26T11:00:00"
NOW = "2026-11-26T12:00:00"                   # local (Moscow) time of the app's clock below
FRESH = "2026-11-26T11:55:00"                 # 5 minutes ago: may be open right now
APP_NOW = lambda: dt.datetime(2026, 11, 26, 9, 0, 0)    # noqa: E731 — UTC, 12:00 in Moscow


def blank(conn, made=OLD, **fields):
    return orders.create_card(conn, dict({"source": "avito", "deal_kind": "webmim", "responsible": "owner"}, **fields), made, "owner",
                              require_contact=False)


def base(conn):
    orders.ensure_pipeline(conn, OLD)
    cards = {"empty": blank(conn), "empty2": blank(conn), "fresh": blank(conn, FRESH), "name": blank(conn, client_name="Иван"),
             "phone": blank(conn, client_phone="+79990000000"), "comment": blank(conn, comment="Перезвонить"),
             "item": blank(conn), "want": blank(conn), "note": blank(conn), "tag": blank(conn), "date": blank(conn), "stage": blank(conn)}
    orders.add_item(conn, cards["item"], goods_id="1", name="Ikon 185/65 R15", brand_key="ikon", season="winter", diameter=15,
                    territory="1", qty=4, sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=OLD, author="owner")
    order_wait.add_want(conn, cards["want"], {"brand": "Ikon", "width": "185", "profile": "65", "radius": "15", "qty": "4"}, OLD, "owner")
    orders.add_note(conn, cards["note"], "Клиент перезвонит", OLD, "owner")
    conn.execute("UPDATE order_card SET tags = 'метка' WHERE id = ?", (cards["tag"],))
    conn.execute("UPDATE order_card SET delivery_date = '2026-11-27' WHERE id = ?", (cards["date"],))
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (orders.list_stages(conn)[1]["id"], cards["stage"]))
    conn.commit()
    return cards


def gone(conn, card_id) -> bool:
    try:
        orders.get_card(conn, card_id)
    except orders.OrderError:
        return True
    return False


def test_only_the_empty_orders_made_ten_minutes_ago_or_earlier_go_and_come_back(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    cards = base(conn)
    planned = cleanup.empty_to_delete(conn, NOW)
    assert planned == [cards["empty"], cards["empty2"]]
    done = cleanup.delete_empty(conn, NOW, "partner1", cleanup.empty_sign(planned))
    assert done == {"removed": 2, "skipped": 0, "left": 0}
    assert gone(conn, cards["empty"]) and gone(conn, cards["empty2"])
    for name in cards:
        if name not in ("empty", "empty2"):
            assert not gone(conn, cards[name]), name                                  # anything in it, or fresh: it stays
    [row] = conn.execute("SELECT author, detail FROM audit WHERE action = ?", (cleanup.EMPTY_ACTION,)).fetchall()
    assert row[0] == "partner1" and '"removed": 2' in row[1]
    records = trash.list_deleted(conn, "order")
    assert sorted(r["record_id"] for r in records) == [cards["empty"], cards["empty2"]]
    assert trash.restore_order(conn, records[0]["id"], NOW, "owner") in (cards["empty"], cards["empty2"])


def test_a_second_press_or_a_changed_list_removes_nothing(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    cards = base(conn)
    sign = cleanup.empty_sign(cleanup.empty_to_delete(conn, NOW))
    orders.update_card(conn, cards["empty2"], {"client_name": "Пётр"}, NOW, "owner")    # filled while the menu was open
    with pytest.raises(orders.OrderError, match="изменился"):
        cleanup.delete_empty(conn, NOW, "owner", sign)
    assert not gone(conn, cards["empty"])
    sign = cleanup.empty_sign(cleanup.empty_to_delete(conn, NOW))
    cleanup.delete_empty(conn, NOW, "owner", sign)
    with pytest.raises(orders.OrderError, match="Пустых заказов нет"):                  # the double press
        cleanup.delete_empty(conn, NOW, "owner", sign)
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = ?", (cleanup.EMPTY_ACTION,)).fetchone()[0] == 1


def test_a_card_filled_between_the_check_and_its_delete_stays(tmp_path, monkeypatch):
    conn = store.connect(tmp_path / "t.db")
    cards = base(conn)
    sign = cleanup.empty_sign(cleanup.empty_to_delete(conn, NOW))
    real = trash.delete_order

    def fill_then_delete(conn_, card_id, *args, **kw):
        if card_id == cards["empty2"]:
            conn_.execute("UPDATE order_card SET client_phone = '+79991112233' WHERE id = ?", (card_id,))
            conn_.commit()
        return real(conn_, card_id, *args, **kw)
    monkeypatch.setattr(trash, "delete_order", fill_then_delete)
    assert cleanup.delete_empty(conn, NOW, "owner", sign) == {"removed": 1, "skipped": 1, "left": 0}
    assert gone(conn, cards["empty"]) and not gone(conn, cards["empty2"])


@pytest.mark.parametrize("new_view", [False, True])
def test_the_menu_asks_with_the_count_and_the_button_deletes_once(tmp_path, new_view):
    app = make_app(tmp_path, NOW=APP_NOW)
    conn = store.connect(app.config["DB_PATH"])
    if new_view:
        store.set_state(conn, UI_KEY % "office", "all")
    cards = base(conn)
    client = app.test_client()
    tok = login(client)
    page = client.get("/orders").get_data(as_text=True)
    assert 'action="/orders/empty/delete"' in page and "Удалить 2 пустых заказа?" in page and "Удалить пустые заказы (2)" in page
    sign = cleanup.empty_sign(cleanup.empty_to_delete(conn, NOW))
    assert 'name="expected" value="%s"' % sign in page
    assert client.post("/orders/empty/delete", data={"expected": sign}).status_code == 400              # CSRF
    assert not gone(conn, cards["empty"])
    response = post(client, "/orders/empty/delete", tok, expected=sign)
    assert response.status_code == 302 and response.headers["Location"].endswith("/orders")
    assert gone(conn, cards["empty"]) and gone(conn, cards["empty2"]) and not gone(conn, cards["fresh"])
    post(client, "/orders/empty/delete", tok, expected=sign)                                              # the double press
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = ?", (cleanup.EMPTY_ACTION,)).fetchone()[0] == 1
    page = client.get("/orders").get_data(as_text=True)
    assert "Пустых заказов нет" in page and "Удалить пустые заказы — нет" in page
    [row] = conn.execute("SELECT at, author, action, detail FROM audit WHERE action = ?", (cleanup.EMPTY_ACTION,)).fetchall()
    text = texts.audit_text({"at": row[0], "author": row[1], "action": row[2], "detail": json.loads(row[3])})
    assert "Удалены пустые заказы" in text and "удалено 2" in text


def test_the_right_is_the_right_to_delete_an_order(tmp_path):
    app = make_app(tmp_path, NOW=APP_NOW)
    conn = store.connect(app.config["DB_PATH"])
    cards = base(conn)
    sign = cleanup.empty_sign(cleanup.empty_to_delete(conn, NOW))
    chats, _ = person(app, "chats")
    assert "/orders/empty/delete" not in chats.get("/orders").get_data(as_text=True)
    assert chats.post("/orders/empty/delete", data={"csrf": "tok", "expected": sign}).status_code == 403
    viewer, _ = person(app, "viewer")
    assert viewer.post("/orders/empty/delete", data={"csrf": "tok", "expected": sign}).status_code == 403
    assert not gone(conn, cards["empty"])
    partner, _ = person(app, "partner")
    assert "/orders/empty/delete" in partner.get("/orders").get_data(as_text=True)
    assert partner.post("/orders/empty/delete", data={"csrf": "tok", "expected": sign}).status_code == 302
    assert gone(conn, cards["empty"])
    [row] = conn.execute("SELECT author FROM audit WHERE action = ?", (cleanup.EMPTY_ACTION,)).fetchall()
    assert row[0] == "partner1"
