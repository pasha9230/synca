"""«Объединить заказы» (the owner 2026-10-30, plan docs/runs/2026-10-30-order-merge): two and more orders of one point become one new order;
the sources go to «Отмена» («Объединён в заказ №N»), their tyres are given to the new one at their purchase, the revenue is counted once,
nothing is sent to anybody, «Разъединить» gives everything back. A source with a sale or on a closed stage takes part «справочно»."""
from __future__ import annotations

import json
import re

import pytest

from panel_helpers import post
from test_mypoint import manager
from test_oth_sale import LATER, T, done_with_sale, new_order, panel, point_id, stage, take
from tests.test_order_reserve import card_at, db
from tests import test_webmim_create as wc
from tires_sync import order_merge, order_oth, order_report, order_rules as rules, order_sheet, orders, oth_sale, oth_stock, trash

NOW = "2026-10-29T12:00:00"
LATEST = "2026-10-29T13:00:00"


def _card(conn, cid):
    return orders.get_card(conn, cid)


def _qty(conn, cid):
    return [(i["goods_id"], int(i["qty"])) for i in _card(conn, cid)["items"]]


def _client(conn, cid):
    row = conn.execute("SELECT client_id, how FROM client_order WHERE order_id = ?", (cid,)).fetchone()
    return tuple(row) if row else None


def _second(conn, code="moving", goods="2"):
    """Another placed order of Бета with its own WebMIM number, two tyres of `goods`."""
    cid = card_at(conn, code, T)
    item = _card(conn, cid)["items"][0]["id"]
    conn.execute("UPDATE order_item SET goods_id = ?, qty = 2, supplier_price = 3000 WHERE id = ?", (goods, item))
    conn.execute("UPDATE order_card SET transfer_number = 'П-77', delivery_date = '2026-11-02' WHERE id = ?", (cid,))
    conn.commit()
    return cid


def _merge(conn, ids, code="moving", client_from=None, **kw):
    return order_merge.merge(conn, ids, stage(conn, code), NOW, "o", client_from=ids[0] if client_from is None else client_from, **kw)


# ---- the merge ----------------------------------------------------------------------------------------------------------------------

def test_two_orders_become_one_new_order_and_the_sources_are_cancelled_with_their_tyres_given(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "moving", T)
    b = _second(conn)
    first = _card(conn, a)
    new = _merge(conn, [a, b])
    card = _card(conn, new)
    assert card["stage"]["code"] == "moving" and new not in (a, b)
    assert sorted((i["goods_id"], int(i["qty"]), float(i["supplier_price"])) for i in card["items"]) == [("1", 4, 4000.0), ("2", 2, 3000.0)]
    assert all(r[0] == 1 for r in conn.execute("SELECT purchase_manual FROM order_item WHERE order_id = ?", (new,)))   # WebMIM never rewrites it
    for src in (a, b):
        got = _card(conn, src)
        assert got["stage"]["code"] == "cancel" and got["cancel_reason"] == order_merge.REASON % new
        assert [q for _g, q in _qty(conn, src)] == [0] and order_merge.into(conn, src) == new
    assert (card["client_name"], card["client_phone"], card["delivery_date"], card["transfer_number"]) == (
        first["client_name"], first["client_phone"], first["delivery_date"], first["transfer_number"])
    assert _client(conn, new) == (_client(conn, a)[0], "manual")                   # the client of the chosen order
    assert card["webmim_number"] == "" and card["webmim_id"] == ""                 # one WebMIM number is never on two orders
    assert oth_sale.supplier_items(conn, card) == []                               # both were placed: nothing is ordered again
    assert [s["order_id"] for s in order_merge.sources(conn, new)] == [a, b]


def test_nothing_is_sent_and_no_rule_wakes(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "moving", T)
    b = _second(conn)
    rules.add_rule(conn, stage(conn, "moving"), "enter", 0, "remind", None, "Позвоните", "ask", None)
    rules.add_rule(conn, stage(conn, "cancel"), "enter", 0, "remind", None, "Отмена", "ask", None)
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM order_task").fetchone()[0]
    new = _merge(conn, [a, b])
    assert conn.execute("SELECT COUNT(*) FROM order_task").fetchone()[0] == before
    loud = conn.execute("SELECT order_id, kind, text FROM order_event WHERE at = ? AND level != 'grey'", (NOW,)).fetchall()
    assert loud == []                                                              # no alert in the bell: every record of the merge is grey
    assert any("Объединён в заказ №%d" % new in e["text"] for e in orders.list_events(conn, a))


def test_the_new_order_keeps_the_history_of_the_sources(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "moving", T)
    b = _second(conn)
    orders.add_note(conn, a, "Клиент просил после обеда", T, "zaman")
    orders.update_card(conn, b, {"comment": "Позвонить перед доставкой"}, T, "o")
    new = _merge(conn, [a, b])
    texts = [e["text"] for e in orders.list_events(conn, new)]
    label_a, label_b = _card(conn, a)["webmim_number"], _card(conn, b)["webmim_number"]
    assert any(t.startswith("Создан объединением заказов") and label_a in t and label_b in t for t in texts)
    assert any("[№%s]" % label_a in t and "Клиент просил после обеда" in t for t in texts)
    assert any("[№%s]" % label_b in t and "Позвонить перед доставкой" in t for t in texts)
    assert any("[№%s]" % label_a in t and "создан" in t.lower() for t in texts)
    assert not [e for e in orders.list_events(conn, new) if e["kind"] == "stage"]     # «дата оформления» is never taken from a copy


def test_a_new_client_is_written_and_the_rule_of_one_open_order_does_not_stop_it(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "moving", T)
    b = _second(conn)
    phone = _card(conn, a)["client_phone"]                                         # the chosen phone is the open order's own
    new = _merge(conn, [a, b], client_from=b)
    assert _card(conn, new)["client_phone"] == _card(conn, b)["client_phone"]
    c = card_at(conn, "moving", T)
    d = _second(conn)
    other = order_merge.merge(conn, [c, d], stage(conn, "moving"), NOW, "o", client_name="Олег", client_phone="+7 900 333 44 55")
    assert (_card(conn, other)["client_name"], _card(conn, other)["client_phone"]) == ("Олег", "+7 900 333 44 55")
    assert phone
    e = card_at(conn, "moving", T)
    f = _second(conn)
    with pytest.raises(orders.OrderError, match="уже есть открытый заказ №%d" % f):
        order_merge.merge(conn, [c, e][1:] + [_second(conn)], stage(conn, "moving"), NOW, "o", client_name="Ф", client_phone=_card(conn, f)["client_phone"])
    assert _card(conn, e)["stage"]["code"] == "moving"                             # nothing is kept of it


def test_the_stage_chosen_needs_its_data_and_at_the_point_starts_a_reservation(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "at_point", T)
    b = _second(conn, "at_point")
    conn.execute("UPDATE order_card SET reserve_until = '2026-11-05' WHERE id = ?", (a,))      # the client asked to wait longer
    conn.commit()
    reserve = "2026-11-05"
    new = _merge(conn, [a, b], code="at_point")
    assert _card(conn, new)["reserve_until"] == reserve                            # the reservation of the sources, not a new one
    c = card_at(conn, "moving", T)
    d = _second(conn)
    with pytest.raises(orders.OrderError, match="телефон"):
        order_merge.merge(conn, [c, d], stage(conn, "moving"), NOW, "o", client_name="Олег", client_phone="")
    assert _card(conn, c)["stage"]["code"] == "moving" and order_merge.into(conn, c) is None   # nothing is kept of a refused merge


def test_what_cannot_be_merged(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "moving", T)
    with pytest.raises(orders.OrderError, match="два"):
        _merge(conn, [a])
    other = card_at(conn, "moving", T)
    conn.execute("UPDATE order_card SET point_id = ? WHERE id = ?", (wc.point(conn, name="Гамма"), other))
    conn.commit()
    with pytest.raises(orders.OrderError, match="одной точки"):
        _merge(conn, [a, other])
    b = _second(conn)
    new = _merge(conn, [a, b])
    c = _second(conn)
    with pytest.raises(orders.OrderError, match="уже объединён"):
        _merge(conn, [a, c])
    with pytest.raises(orders.OrderError, match="уже объединён"):
        _merge(conn, [new, c])
    d = _second(conn)
    conn.execute("INSERT INTO webmim_create (order_id, server, state, first_at, started_at, author, updated_at) VALUES (?, 'prod', 'running', ?, ?, 'o', ?)",
                 (d, T, T, T))
    conn.commit()
    with pytest.raises(orders.OrderError, match="оформляется в WebMIM"):
        _merge(conn, [c, d])


def test_tyres_of_oth_are_given_as_a_take_and_an_order_holding_tyres_of_oth_is_not_merged(tmp_path):
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)                                                 # the owner's example: 4 on OTH, the client wants them
    cid = new_order(conn, qty=2, goods="2")
    new = _merge(conn, [cid, old], code="new")
    assert oth_stock.by_point(conn) == {}                                         # the stock of OTH (and the ad) is gone
    taken = oth_sale.taken(conn, new)
    assert [(t["from_order"], t["qty"]) for t in taken.values()] == [(old, 4)]     # «с ОТХ»: its chain, its purchase, «Б» in the table
    assert [i["goods_id"] for i in oth_sale.supplier_items(conn, _card(conn, new))] == ["2"]     # not placed: ordered from the new order
    assert _card(conn, old)["stage"]["code"] == "cancel"
    holder = new_order(conn, qty=2)
    card_at(conn, "oth", T)
    take(conn, holder)
    with pytest.raises(orders.OrderError, match="с ОТХ"):
        _merge(conn, [holder, new_order(conn, qty=1, goods="3")])
    third = card_at(conn, "oth", T)
    conn.execute("INSERT INTO oth_return_plan (order_id, agreed_at, agreed_by, return_date) VALUES (?,?,?,?)", (third, T, "o", "2026-10-29"))
    conn.commit()
    with pytest.raises(orders.OrderError, match="возврат"):
        _merge(conn, [third, new_order(conn, qty=1, goods="4")])


def test_a_source_with_a_sale_takes_part_for_reference_only_and_its_sale_and_tyres_stay(tmp_path):
    conn = db(tmp_path)
    sold = new_order(conn, qty=2)
    done_with_sale(conn, sold)
    before = _card(conn, sold)
    cid = new_order(conn, qty=2, goods="2")
    new = _merge(conn, [cid, sold], code="new")
    after = _card(conn, sold)
    assert (after["stage"]["code"], after["sale_ids"], _qty(conn, sold)) == ("done", before["sale_ids"], [("1", 2)])
    assert _qty(conn, new) == [("2", 2)]                                           # nothing of the sold order is sold again
    assert [(s["order_id"], s["ref"]) for s in order_merge.sources(conn, new)] == [(cid, False), (sold, True)]
    one, other = new_order(conn, qty=1, goods="5"), new_order(conn, qty=1, goods="6")
    for done in (one, other):
        done_with_sale(conn, done)
    with pytest.raises(orders.OrderError, match="Нечего объединять"):
        _merge(conn, [one, other], code="new")
    assert order_merge.why_not_unmerge(conn, new) is None
    order_merge.unmerge(conn, new, LATEST, "o")
    assert _card(conn, sold)["stage"]["code"] == "done" and _card(conn, sold)["sale_ids"] == before["sale_ids"]
    assert _card(conn, cid)["stage"]["code"] == "new" and _qty(conn, cid) == [("2", 2)]


def test_the_revenue_is_counted_once_in_the_new_order_at_the_purchase_of_the_sources(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "moving", T)
    b = _second(conn)
    new = _merge(conn, [a, b])
    for step in ("at_point", "done"):
        orders.move_card(conn, new, stage(conn, step), LATEST, "o")
    orders.record_sales(conn, new, "2026-10-29", LATEST, "o", "2026-10-29")
    sales = conn.execute("SELECT note, pieces, price_total FROM sale WHERE reversed_at IS NULL ORDER BY id").fetchall()
    assert sorted(sales) == sorted([("Заказ №%d" % new, 4, 16000.0), ("Заказ №%d" % new, 2, 6000.0)])


def test_the_point_gets_its_fee_for_a_handed_client_once(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "moving", T)
    b = _second(conn)
    conn.execute("INSERT INTO order_handoff (order_id, message_id, point_id, created_at) VALUES (?, 'm1', ?, ?)", (a, point_id(conn), T))
    conn.commit()
    new = _merge(conn, [a, b])
    assert order_merge.handed(conn) == {new: a}
    for step in ("at_point", "done"):
        orders.move_card(conn, new, stage(conn, step), LATEST, "o")
    orders.record_sales(conn, new, "2026-10-29", LATEST, "o", "2026-10-29")
    report = order_report.build(conn, "2026-10-01", "2026-10-31")
    assert report["handoff"]["clients"] == 1


# ---- the Google table ---------------------------------------------------------------------------------------------------------------

def test_the_table_keeps_the_old_rows_and_adds_the_new_ones_with_their_webmim_numbers(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "moving", T)
    b = _second(conn)
    label_a, label_b = _card(conn, a)["webmim_number"], _card(conn, b)["webmim_number"]
    written = {src: order_sheet.rows_of(conn, src, T) for src in (a, b)}
    new = _merge(conn, [a, b])
    rows = order_sheet.rows_of(conn, new, NOW)
    assert sorted((r["№ заказа MIM"], r["Перемещение MIM"], r["Кол-во"]) for r in rows.values()) == sorted(
        [(label_a, _card(conn, a)["transfer_number"], 4), (label_b, "П-77", 2)])
    assert all("клиент уже есть в заказе №" in r["Примечание"] for r in rows.values())
    for src in (a, b):
        now_rows = order_sheet.rows_of(conn, src, T)
        ops = order_sheet.changes(now_rows, written[src], *order_sheet.followed(conn, src))
        assert len(ops) == 1 and ops[0]["fields"]["Кол-во"] == 0 and ops[0]["fields"]["Этап"] == "Отмена / невыкуп"
        assert ops[0]["fields"]["Примечание"].startswith("объединён в заказ №%d" % new)


# ---- «Разъединить» ------------------------------------------------------------------------------------------------------------------

def test_unmerge_gives_everything_back_and_sends_nothing(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "at_point", T)
    b = _second(conn, "moving")
    chat = conn.execute("INSERT INTO avito_chat (account_id, chat_id, order_id) VALUES (1, 'c1', ?)", (a,)).lastrowid
    conn.commit()
    kept = {src: {k: _card(conn, src)[k] for k in ("stage_id", "entered_at", "cancel_reason", "reserve_until")} for src in (a, b)}
    client_a = _client(conn, a)
    new = _merge(conn, [a, b])
    assert conn.execute("SELECT order_id FROM avito_chat WHERE id = ?", (chat,)).fetchone()[0] == new
    tasks = conn.execute("SELECT COUNT(*) FROM order_task").fetchone()[0]
    order_merge.unmerge(conn, new, LATEST, "o")
    for src in (a, b):
        assert {k: _card(conn, src)[k] for k in kept[src]} == kept[src]
    assert _qty(conn, a) == [("1", 4)] and _qty(conn, b) == [("2", 2)] and _client(conn, a) == client_a
    assert conn.execute("SELECT order_id FROM avito_chat WHERE id = ?", (chat,)).fetchone()[0] == a
    got = _card(conn, new)
    assert got["stage"]["code"] == "cancel" and got["cancel_reason"].startswith("другое: Разъединён")
    assert [q for _g, q in _qty(conn, new)] == [0, 0]
    assert conn.execute("SELECT COUNT(*) FROM order_task").fetchone()[0] == tasks
    assert conn.execute("SELECT COUNT(*) FROM order_event WHERE at = ? AND level != 'grey'", (LATEST,)).fetchone()[0] == 0
    assert order_merge.into(conn, a) is None
    with pytest.raises(orders.OrderError):
        order_merge.unmerge(conn, new, LATEST, "o")                                # twice: nothing more
    again = _merge(conn, [a, b])                                                   # the sources may be merged again
    assert again != new


def test_tyres_of_oth_go_back_to_oth_on_unmerge(tmp_path):
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    cid = new_order(conn, qty=2, goods="2")
    new = _merge(conn, [cid, old], code="new")
    order_merge.unmerge(conn, new, LATEST, "o")
    assert _card(conn, old)["stage"]["code"] == "oth" and oth_stock.by_point(conn) == {("1", point_id(conn)): 4}
    assert oth_sale.taken(conn, new) == {}


def test_when_unmerge_is_refused(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "moving", T)
    b = _second(conn)
    new = _merge(conn, [a, b])
    item = _card(conn, new)["items"][0]["id"]
    orders.update_item(conn, item, 3, 5000, NOW, "o")
    assert "изменили" in order_merge.why_not_unmerge(conn, new)
    orders.update_item(conn, item, 4, 5000, NOW, "o")
    assert order_merge.why_not_unmerge(conn, new) is None
    conn.execute("UPDATE order_card SET webmim_number = '999' WHERE id = ?", (new,))
    conn.commit()
    assert "WebMIM" in order_merge.why_not_unmerge(conn, new)
    conn.execute("UPDATE order_card SET webmim_number = '' WHERE id = ?", (new,))
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, "new"), a))      # a source taken off «Отмена» by hand
    conn.commit()
    assert "№" in order_merge.why_not_unmerge(conn, new)
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, "cancel"), a))
    conn.commit()
    for step in ("at_point", "done"):
        orders.move_card(conn, new, stage(conn, step), LATEST, "o")
    orders.record_sales(conn, new, "2026-10-29", LATEST, "o", "2026-10-29")
    assert "продажа" in order_merge.why_not_unmerge(conn, new)
    with pytest.raises(orders.OrderError, match="продажа"):
        order_merge.unmerge(conn, new, LATEST, "o")


def test_the_bin_refuses_an_order_of_a_merge(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "moving", T)
    b = _second(conn)
    new = _merge(conn, [a, b])
    for cid in (a, new):
        with pytest.raises(orders.OrderError, match="(?i)разъедините"):
            trash.delete_order(conn, cid, LATEST, "o")


# ---- the panel ----------------------------------------------------------------------------------------------------------------------

def test_the_panel_merges_and_unmerges_and_a_point_cannot(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    a = card_at(conn, "moving", T)
    b = _second(conn)
    page = owner.get("/orders/%d/merge" % a).get_data(as_text=True)
    assert 'name="orders" value="%d"' % b in page and "Объединить" in page
    point = manager(app, conn, point_id(conn))
    assert point.post("/orders/%d/merge" % a, data={"csrf": "tok", "orders": [a, b], "client_from": a, "stage_id": stage(conn, "moving")}).status_code == 403
    r = post(owner, "/orders/%d/merge" % a, tok, orders=[str(a), str(b)], client_from=str(a), stage_id=str(stage(conn, "moving")))
    assert r.status_code == 302
    new = order_merge.into(conn, a)
    assert new and r.headers["Location"].endswith("/orders/%d" % new)
    card = owner.get("/orders/%d" % new).get_data(as_text=True)
    assert "Объединено из" in card and 'href="/orders/%d"' % a in card and "Разъединить" in card
    assert "Объединён в заказ" in owner.get("/orders/%d" % a).get_data(as_text=True)
    assert point.post("/orders/%d/unmerge" % new, data={"csrf": "tok"}).status_code == 403
    assert post(owner, "/orders/%d/unmerge" % new, tok).status_code == 302
    assert _card(conn, a)["stage"]["code"] == "moving"


def test_the_reconciliation_wants_no_pair_for_a_merged_order_of_placed_sources_only(tmp_path):
    from datetime import date
    from tires_sync import webmim_match
    conn = db(tmp_path)
    a = card_at(conn, "moving", T)
    b = _second(conn)
    new = _merge(conn, [a, b])
    c = new_order(conn, qty=2, goods="7")                                          # not placed: its tyres are the new order's to order
    d = card_at(conn, "moving", T)
    mixed = _merge(conn, [c, d], code="new")
    unpaired = {r["id"] for r in webmim_match.reconcile(conn, date(2026, 10, 30))["unpaired"]}
    assert new not in unpaired and mixed in unpaired


# ---- the review of 2026-10-30 -----------------------------------------------------------------------------------------------------------

def test_no_unmerge_once_the_new_order_is_on_oth(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "moving", T)
    b = _second(conn)
    new = _merge(conn, [a, b])
    for step in ("at_point", "oth"):                                               # the new client refused: the tyres are our stock
        orders.move_card(conn, new, stage(conn, step), LATEST, "o")
    assert "На ОТХ" in order_merge.why_not_unmerge(conn, new)


def test_no_unmerge_when_a_position_of_a_source_was_changed_or_deleted(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "moving", T)
    b = _second(conn)
    new = _merge(conn, [a, b])
    home = _card(conn, a)["items"][0]["id"]
    conn.execute("UPDATE order_item SET qty = 2 WHERE id = ?", (home,))           # a hand POST on the cancelled source
    conn.commit()
    assert "изменили" in order_merge.why_not_unmerge(conn, new)
    conn.execute("DELETE FROM order_item WHERE id = ?", (home,))
    conn.commit()
    assert "изменили" in order_merge.why_not_unmerge(conn, new)
    with pytest.raises(orders.OrderError):
        order_merge.unmerge(conn, new, LATEST, "o")
    assert _qty(conn, b) == [("2", 0)]                                             # nothing given back of a refused unmerge


def test_tyres_ordered_by_a_placed_source_are_never_replaced_by_tyres_of_oth(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "moving", T)
    b = _second(conn)
    new = _merge(conn, [a, b], code="new")
    card_at(conn, "oth", T)                                                        # the same tyre lies on OTH at the point
    card = _card(conn, new)
    assert oth_sale.offers(conn, card) == {}
    line = next(i for i in card["items"] if i["goods_id"] == "1")
    stock = next(r for r in oth_stock._rows(conn))
    with pytest.raises(orders.OrderError):
        oth_sale.take(conn, new, line["id"], stock["item_id"], LATEST, "o")


def test_a_source_placed_in_part_is_not_merged(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "moving", T)
    b = _second(conn)
    conn.execute("INSERT INTO webmim_create (order_id, server, state, first_at, started_at, author, updated_at) VALUES (?, 'prod', 'partial', ?, ?, 'o', ?)",
                 (b, T, T, T))
    conn.commit()
    with pytest.raises(orders.OrderError, match="не полностью"):
        _merge(conn, [a, b])


def test_tyres_not_ordered_yet_keep_the_purchase_webmim_gives_and_hold_the_new_order_before_placed(tmp_path):
    conn = db(tmp_path)
    cid = new_order(conn, qty=2, goods="2")
    other = new_order(conn, qty=1, goods="3")
    with pytest.raises(orders.OrderError, match="ещё не заказаны"):
        _merge(conn, [cid, other], code="moving")
    new = _merge(conn, [cid, other], code="new")
    assert [r[0] for r in conn.execute("SELECT purchase_manual FROM order_item WHERE order_id = ?", (new,))] == [0, 0]


def test_the_reservation_goes_on_and_the_point_and_the_client_are_not_asked_again_today(tmp_path):
    from tires_sync import order_reserve, store
    conn = db(tmp_path)
    a = card_at(conn, "at_point", T)
    b = _second(conn, "at_point")
    conn.execute("UPDATE order_card SET reserve_until = ? WHERE id IN (?, ?)", (NOW[:10], a, b))
    conn.commit()
    card = _card(conn, a)
    store.set_state(conn, order_reserve._ask_key(card), "asked|" + T)
    orders.add_event(conn, a, T, "система", "reserve", "Клиенту отправлено [резерв:клиенту:%s]" % card["entered_at"])
    conn.commit()
    new = _merge(conn, [a, b], code="at_point")
    got = _card(conn, new)
    assert got["reserve_until"] == NOW[:10] and order_reserve.was_asked(conn, got)
    assert order_reserve._done(conn, new, "[резерв:клиенту:%s]" % got["entered_at"])
