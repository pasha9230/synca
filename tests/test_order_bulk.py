"""«Закрыть заказы разом»: the picked cards move to one closing stage, a copy of the base comes first, the rules of the stage stay asleep,
and a card that cannot move (a sale, missing data, changed meanwhile) stays where it is."""
from __future__ import annotations

import datetime
import sqlite3

import pytest

from panel_helpers import login, make_app, post
from tires_sync import order_bulk as bulk, orders, store

NOW = "2026-10-19T12:00:00"


def base(path):
    conn = store.connect(path)
    orders.ensure_pipeline(conn, NOW)
    cards = [orders.create_card(conn, {"client_name": "Клиент %d" % n}, NOW, "manager") for n in range(4)]
    conn.execute("UPDATE order_card SET sale_ids = '[7]' WHERE id = ?", (cards[3],))
    conn.execute("INSERT INTO order_task (order_id, rule_id, entered_at, kind, status, title, created_at) VALUES (?, 1, ?, 'notify', 'pending', 'Сообщить', ?)",
                 (cards[0], NOW, NOW))
    conn.commit()
    return conn, cards


def stage(conn, code):
    return next(s for s in orders.list_stages(conn) if s["code"] == code)


def test_the_check_says_what_moves_and_why_the_rest_does_not(tmp_path):
    conn, cards = base(tmp_path / "t.db")
    plan = bulk.check(conn, cards, stage(conn, "cancel")["id"], "")
    assert [c["id"] for c in plan["ok"]] == [] and all("причина отмены" in why for _c, why in plan["skip"][:3])
    plan = bulk.check(conn, cards, stage(conn, "cancel")["id"], bulk.DEFAULT_REASON)
    assert [c["id"] for c in plan["ok"]] == cards[:3]
    assert plan["webmim"] == []
    orders.update_card(conn, cards[1], {"webmim_number": "555"}, NOW, "manager")
    assert [c["id"] for c in bulk.check(conn, cards, stage(conn, "cancel")["id"], "x")["webmim"]] == [cards[1]]
    [(card, why)] = plan["skip"]
    assert card["id"] == cards[3] and "продажа" in why
    assert "return" not in {s["code"] for s in bulk.targets(conn)} and "new" not in {s["code"] for s in bulk.targets(conn)}
    with pytest.raises(orders.OrderError):
        bulk.check(conn, cards, stage(conn, "new")["id"], "")


def test_the_run_takes_a_copy_moves_with_the_reason_and_wakes_no_rule(tmp_path):
    db = tmp_path / "t.db"
    conn, cards = base(db)
    cancel = stage(conn, "cancel")
    plan = bulk.check(conn, cards, cancel["id"], "дубль")
    done = bulk.run(conn, db, cards, cancel["id"], "дубль", plan["sign"], NOW, "owner")
    assert done["moved"] == 3 and done["skipped"] == 0 and done["backup"] == "pre-bulk-cancel-20261019-120000.db"
    copy = sqlite3.connect(str(tmp_path / done["backup"]))
    assert copy.execute("SELECT COUNT(*) FROM order_card WHERE stage_id = ?", (cancel["id"],)).fetchone()[0] == 0          # the copy is from before
    copy.close()
    for card_id in cards[:3]:
        card = orders.get_card(conn, card_id)
        assert card["stage"]["code"] == "cancel" and card["cancel_reason"] == "дубль"
    assert orders.get_card(conn, cards[3])["stage"]["code"] == "new"
    assert conn.execute("SELECT status FROM order_task WHERE order_id IN (?,?,?)", tuple(cards[:3])).fetchall() == [("skipped",)]   # no new task, the old one ticked off
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'orders_bulk_move'").fetchone()[0] == 1
    assert orders.move_card(conn, cards[0], stage(conn, "new")["id"], NOW, "owner")                                          # and it goes back


def test_rules_of_the_stage_stay_asleep_only_in_the_bulk_move(tmp_path, monkeypatch):
    conn, cards = base(tmp_path / "t.db")
    fired = []
    monkeypatch.setattr(orders, "_fire_rules", lambda conn_, card_id, now, chain: fired.append(card_id))
    cancel = stage(conn, "cancel")
    plan = bulk.check(conn, cards[:1], cancel["id"], "x")
    bulk.run(conn, tmp_path / "t.db", cards[:1], cancel["id"], "x", plan["sign"], NOW, "owner")
    assert fired == []
    orders.move_card(conn, cards[1], cancel["id"], NOW, "owner", changes={"cancel_reason": "y"})
    assert fired == [cards[1]]


def test_nothing_moves_when_the_cards_changed_since_the_check(tmp_path):
    db = tmp_path / "t.db"
    conn, cards = base(db)
    cancel = stage(conn, "cancel")
    plan = bulk.check(conn, cards, cancel["id"], "x")
    orders.move_card(conn, cards[0], cancel["id"], NOW, "manager", changes={"cancel_reason": "сам"})
    with pytest.raises(orders.OrderError):
        bulk.run(conn, db, cards, cancel["id"], "x", plan["sign"], NOW, "owner")
    assert orders.get_card(conn, cards[1])["stage"]["code"] == "new"
    assert not list(tmp_path.glob("pre-bulk-*.db"))


def test_the_panel_lists_checks_and_moves(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime.datetime(2026, 10, 19, 9, 0, 0))
    conn, cards = base(app.config["DB_PATH"])
    client = app.test_client()
    tok = login(client)
    assert "/orders/bulk" in client.get("/orders").get_data(as_text=True)
    page = client.get("/orders/bulk").get_data(as_text=True)
    assert all('name="id" value="%d" checked' % c in page for c in cards) and bulk.DEFAULT_REASON in page
    cancel = stage(conn, "cancel")
    check = post(client, "/orders/bulk/check", tok, id=[str(c) for c in cards], to=str(cancel["id"]), reason="дубль").get_data(as_text=True)
    assert "Будет переведено: <b>3</b>" in check and "продажа" in check and "WebMIM" not in check.split("Не будут")[0].split("Будет переведено")[1]
    sign = bulk.check(conn, cards, cancel["id"], "дубль")["sign"]
    assert 'value="%s"' % sign in check
    response = post(client, "/orders/bulk/run", tok, ids=",".join(map(str, cards)), to=str(cancel["id"]), reason="дубль", expected=sign)
    assert response.status_code == 302
    assert orders.get_card(conn, cards[0])["stage"]["code"] == "cancel"


def _placed_cancelled(tmp_path, keep_from=True):
    """Two cards on «Новый заказ», one moved by hand to «Ожидает наличие», then all cancelled in one go."""
    from tires_sync import order_wait
    db = tmp_path / "t.db"
    conn, cards = base(db)
    order_wait.add_want(conn, cards[2], {"brand": "Ikon", "width": "185", "profile": "65", "radius": "15", "qty": "4"}, NOW, "manager")
    orders.move_card(conn, cards[2], stage(conn, "wait")["id"], "2026-10-19T11:00:00", "manager")
    conn.execute("INSERT INTO order_task (order_id, rule_id, entered_at, kind, status, title, created_at) VALUES (?, 1, ?, 'notify', 'pending', 'Сообщить', ?)",
                 (cards[2], NOW, NOW))
    conn.commit()
    cancel = stage(conn, "cancel")
    plan = bulk.check(conn, cards[:3], cancel["id"], bulk.DEFAULT_REASON)
    bulk.run(conn, db, cards[:3], cancel["id"], bulk.DEFAULT_REASON, plan["sign"], NOW, "owner")
    if not keep_from:                                                                            # a run recorded before the stages were kept
        audit_id, detail = conn.execute("SELECT id, detail FROM audit WHERE action = 'orders_bulk_move'").fetchone()
        data = __import__("json").loads(detail)
        data.pop("from")
        conn.execute("UPDATE audit SET detail = ? WHERE id = ?", (__import__("json").dumps(data), audit_id))
        conn.commit()
    return conn, db, cards


@pytest.mark.parametrize("keep_from", [True, False])
def test_a_bulk_move_is_undone_to_the_stage_each_card_stood_on(tmp_path, keep_from):
    conn, db, cards = _placed_cancelled(tmp_path, keep_from)
    [run] = bulk.runs(conn)
    plan = bulk.undo_check(conn, run["id"])
    backs = {line["card"]["id"]: (line["back"]["code"], line["first"]) for line in plan["ok"]}
    assert backs == {cards[0]: ("new", True), cards[1]: ("new", True), cards[2]: ("wait", False)}
    done = bulk.undo(conn, db, run["id"], [cards[2]], "2026-10-19T13:00:00", "owner")
    assert done["back"] == 1 and done["backup"] == "pre-bulk-undo-20261019-130000.db"
    card = orders.get_card(conn, cards[2])
    assert card["stage"]["code"] == "wait" and card["cancel_reason"] == ""
    assert conn.execute("SELECT status FROM order_task WHERE order_id = ?", (cards[2],)).fetchall() == [("pending",)]   # waits again
    assert orders.get_card(conn, cards[0])["stage"]["code"] == "cancel"                                                 # not picked: stays
    assert [c["id"] for c, _why in bulk.undo_check(conn, run["id"])["skip"]] == [cards[2]]                              # not twice


def test_the_bulk_page_shows_only_new_orders_unless_all_are_asked_for(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime.datetime(2026, 10, 19, 9, 0, 0))
    conn, cards = base(app.config["DB_PATH"])
    orders.move_card(conn, cards[0], stage(conn, "talk")["id"], NOW, "manager")
    client = app.test_client()
    tok = login(client)
    page = client.get("/orders/bulk").get_data(as_text=True)
    assert 'value="%d" checked' % cards[0] not in page and 'value="%d" checked' % cards[1] in page
    assert 'value="%d" checked' % cards[0] in client.get("/orders/bulk?from=all").get_data(as_text=True)
    cancel = stage(conn, "cancel")
    sign = bulk.check(conn, cards[:2], cancel["id"], "x")["sign"]
    post(client, "/orders/bulk/run", tok, ids="%d,%d" % tuple(cards[:2]), to=str(cancel["id"]), reason="x", expected=sign)
    [run] = bulk.runs(conn)
    undo_page = client.get("/orders/bulk/undo/%d" % run["id"]).get_data(as_text=True)
    assert 'name="id" value="%d" checked' % cards[0] in undo_page and 'name="id" value="%d" checked' % cards[1] not in undo_page
    assert post(client, "/orders/bulk/undo/%d" % run["id"], tok, id=[str(cards[0])]).status_code == 302
    assert orders.get_card(conn, cards[0])["stage"]["code"] == "talk"
