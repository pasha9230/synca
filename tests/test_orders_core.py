"""Orders: stages, cards, positions, transitions, board (no Flask)."""
from __future__ import annotations

import pytest

from order_fixtures import card_of_phone
from tires_sync import orders, store

NOW = "2026-10-12T12:00:00"


def db(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    return conn


def test_the_default_pipeline_is_seeded_once_in_the_owners_order(tmp_path):
    conn = db(tmp_path)
    orders.ensure_pipeline(conn, NOW)                                           # a second call changes nothing
    stages = orders.list_stages(conn)
    assert [s["code"] for s in stages] == ["new", "talk", "wait", "placed", "moving", "at_point", "oth", "return_given", "done", "cancel", "refused", "return"]   # «Ожидает наличие» stands after the talks (owner, 2026-10-15); «Отказ/невыкуп» after «Отмена» (2026-10-23); «Возврат передан» after «На ОТХ» (2026-10-29)
    assert [s["name"] for s in stages][:4] == ["Новый заказ", "Переговоры", "Ожидает наличие", "Заказ оформлен"]
    assert {s["code"]: s["kind"] for s in stages}["done"] == "won" and {s["code"]: s["kind"] for s in stages}["cancel"] == "lost"
    assert [s["position"] for s in stages] == list(range(len(orders.DEFAULT_STAGES)))


def test_the_refused_stage_is_added_once_to_an_old_pipeline_after_cancel_and_kept_as_a_system_stage(tmp_path):
    """«Отказ/невыкуп» (the owner 2026-10-23, п. 14): a closed stage of its own, apart from «Отмена»; an old pipeline gets it once, the
    owner's own stages keep their order around it."""
    conn = store.connect(str(tmp_path / "old.db"))
    for position, (code, name, color, kind) in enumerate(c for c in orders.DEFAULT_STAGES if c[0] != "refused"):
        conn.execute("INSERT INTO order_stage (code, name, color, position, kind) VALUES (?,?,?,?,?)", (code, name, color, position, kind))
    conn.commit()
    mine = orders.add_stage(conn, "Ждём клиента", "#7F56D9", "open", "", 0)
    orders.ensure_pipeline(conn, NOW)
    orders.ensure_pipeline(conn, NOW)
    stages = orders.list_stages(conn)
    codes = [s["code"] for s in stages]
    assert codes.count("refused") == 1 and codes[codes.index("cancel") + 1] == "refused" and stages[-1]["id"] == mine
    assert [s["position"] for s in stages] == list(range(len(stages)))
    refused = next(s for s in stages if s["code"] == "refused")
    assert refused["name"] == "Отказ/невыкуп" and refused["kind"] == "lost"
    with pytest.raises(orders.OrderError, match="Системный"):
        orders.delete_stage(conn, refused["id"])


def test_a_card_at_the_point_moves_to_refused_without_new_data_and_not_with_a_sale(tmp_path):
    conn = db(tmp_path)
    refused = next(s for s in orders.list_stages(conn) if s["code"] == "refused")
    card_id = orders.create_card(conn, {"client_name": "Иван"}, NOW, "owner")
    assert orders.move_card(conn, card_id, refused["id"], NOW, "owner") is True               # no reason, no data of the placing needed
    assert orders.get_card(conn, card_id)["stage"]["name"] == "Отказ/невыкуп"
    other = orders.create_card(conn, {"client_name": "Пётр"}, NOW, "owner")
    conn.execute("UPDATE order_card SET sale_ids = '[7]' WHERE id = ?", (other,))
    conn.commit()
    with pytest.raises(orders.OrderError, match="продажа"):
        orders.move_card(conn, other, refused["id"], NOW, "owner")


def test_stages_can_be_added_renamed_moved_and_only_empty_user_stages_deleted(tmp_path):
    conn = db(tmp_path)
    mine = orders.add_stage(conn, "Ждём клиента", "#7F56D9", "open", "Позвоните", 48)
    assert orders.list_stages(conn)[-1]["id"] == mine and orders.get_stage(conn, mine)["limit_hours"] == 48
    orders.update_stage(conn, mine, "Ждём ответа", "#F79009", "open", "", 24)
    assert orders.get_stage(conn, mine)["name"] == "Ждём ответа"
    orders.shift_stage(conn, mine, -1)
    names = [s["name"] for s in orders.list_stages(conn)]
    assert names[-2] == "Ждём ответа" and names[-1] == "Возврат"
    orders.shift_stage(conn, orders.list_stages(conn)[0]["id"], -1)             # the first one cannot go up: nothing happens
    assert orders.list_stages(conn)[0]["code"] == "new"
    orders.delete_stage(conn, mine)
    assert "Ждём ответа" not in [s["name"] for s in orders.list_stages(conn)]
    assert [s["position"] for s in orders.list_stages(conn)] == list(range(len(orders.DEFAULT_STAGES)))


def test_system_stages_and_stages_with_cards_are_never_deleted_and_bad_input_is_refused(tmp_path):
    conn = db(tmp_path)
    system = orders.list_stages(conn)[2]["id"]
    with pytest.raises(orders.OrderError):
        orders.delete_stage(conn, system)
    mine = orders.add_stage(conn, "Мой", "#7F56D9", "open", "", 0)
    conn.execute("INSERT INTO order_card (stage_id, created_at, created_by, entered_at, updated_at) VALUES (?, ?, 'o', ?, ?)", (mine, NOW, NOW, NOW))
    with pytest.raises(orders.OrderError):
        orders.delete_stage(conn, mine)
    with pytest.raises(orders.OrderError):
        orders.delete_stage(conn, 9999)
    for bad in (dict(name="", color="#7F56D9", kind="open"), dict(name="x" * 61, color="#7F56D9", kind="open"),
                dict(name="ok", color="red", kind="open"), dict(name="ok", color="#7F56D9", kind="weird")):
        with pytest.raises(orders.OrderError):
            orders.add_stage(conn, bad["name"], bad["color"], bad["kind"], "", 0)
    with pytest.raises(orders.OrderError):
        orders.add_stage(conn, "ok", "#7F56D9", "open", "h" * 301, 0)
    with pytest.raises(orders.OrderError):
        orders.add_stage(conn, "ok", "#7F56D9", "open", "", -1)
    with pytest.raises(orders.OrderError):
        orders.add_stage(conn, "ok", "#7F56D9", "open", "", 24 * 365 + 1)


from tires_sync import order_points as points


def card(conn, **fields):
    return orders.create_card(conn, dict({"client_name": "Иван"}, **fields), NOW, "owner")


def test_a_card_starts_on_the_first_stage_and_needs_some_contact(tmp_path):
    conn = db(tmp_path)
    cid = card(conn, client_phone="+7 (900) 000-00-15", tags="срочно, vip, срочно")
    c = orders.get_card(conn, cid)
    assert c["stage"]["code"] == "new" and c["client_name"] == "Иван" and c["tags"] == ["срочно", "vip"] and c["entered_at"] == NOW
    assert [e["kind"] for e in orders.list_events(conn, cid)] == ["created"]
    with pytest.raises(orders.OrderError):
        orders.create_card(conn, {"comment": "просто заметка"}, NOW, "owner")
    assert orders.get_card(conn, orders.create_card(conn, {}, NOW, "owner", require_contact=False))["client_name"] == ""
    with pytest.raises(orders.OrderError):
        orders.get_card(conn, 999)


def test_fields_are_validated_changed_and_every_change_lands_in_the_feed_once(tmp_path):
    conn = db(tmp_path)
    cid = card(conn)
    pid = points.save_point(conn, {"name": "Гамма"})
    changed = orders.update_card(conn, cid, {"client_phone": "+7 900 000 00 15", "point_id": str(pid), "delivery_date": "2026-10-14",
                                             "delivery_time": "до 18:00", "comment": "позвонить", "return_amount": "1 500,50"}, NOW, "owner")
    assert set(changed) == {"client_phone", "point_id", "delivery_date", "delivery_time", "comment", "return_amount"}
    c = orders.get_card(conn, cid)
    assert c["point"]["name"] == "Гамма" and c["delivery_date"] == "2026-10-14" and c["return_amount"] == 1500.5
    assert orders.update_card(conn, cid, {"delivery_time": "до 18:00"}, NOW, "owner") == []                     # nothing changed: nothing written
    kinds = [e["kind"] for e in orders.list_events(conn, cid)]
    assert kinds.count("field") == 6 and kinds[-1] == "created"


@pytest.mark.parametrize("field,value", [
    ("client_phone", "12345"), ("client_phone", "9" * 20), ("client_name", "n" * 121), ("delivery_date", "14.10.2026"),
    ("delivery_date", "2026-13-40"), ("delivery_date", "2019-01-01"), ("delivery_time", "t" * 61), ("point_id", "999"), ("point_id", "abc"),
    ("comment", "c" * 2001), ("return_kind", "elsewhere"), ("return_amount", "-5"), ("return_amount", "abc"), ("return_amount", "1e12"),
    ("tags", ",".join("t%d" % i for i in range(11))), ("tags", "x" * 31), ("cancel_reason", "r" * 301), ("webmim_id", "w" * 61)])
def test_bad_field_values_are_refused_and_change_nothing(tmp_path, field, value):
    conn = db(tmp_path)
    cid = card(conn)
    with pytest.raises(orders.OrderError):
        orders.update_card(conn, cid, {field: value}, NOW, "owner")
    assert orders.get_card(conn, cid)["client_name"] == "Иван" and len(orders.list_events(conn, cid)) == 1


def test_unknown_fields_are_ignored_and_notes_are_kept_with_their_author(tmp_path):
    conn = db(tmp_path)
    cid = card(conn)
    assert orders.update_card(conn, cid, {"stage_id": 5, "created_by": "hacker", "id": 7}, NOW, "owner") == []
    assert orders.get_card(conn, cid)["created_by"] == "owner"
    orders.add_note(conn, cid, "  Клиент просил позвонить  ", "2026-10-12T12:05:00", "owner")
    note = orders.list_events(conn, cid)[0]
    assert note["kind"] == "note" and note["text"] == "Клиент просил позвонить" and note["author"] == "owner" and note["at"] == "2026-10-12T12:05:00"
    for bad in ("", "   ", "x" * 2001):
        with pytest.raises(orders.OrderError):
            orders.add_note(conn, cid, bad, NOW, "owner")


TERMS = {"discount_pct": 5.0, "bonus_pct": 2.0, "monthly_pct": 10.0}


def item(conn, cid, **over):
    args = dict(goods_id="G1", name="Pirelli 225/55 R17", brand_key="PIRELLI", season="winter", diameter=17, territory="103261489424",
                qty=4, sale_unit=9600.0, supplier_price=10000.0, terms=TERMS, fee_pct=3.0, promo_pct=2.0, now=NOW, author="owner")
    args.update(over)
    return orders.add_item(conn, cid, **args)


def test_money_of_a_position_follows_the_unit_economics_and_is_fixed_when_added(tmp_path):
    conn = db(tmp_path)
    cid = card(conn)
    iid = item(conn, cid)
    got = orders.get_card(conn, cid)["items"][0]
    from tires_sync import economics
    assert got["money"] == economics.unit(10000.0, 9600.0, TERMS, 3.0, 2.0, 4)
    assert got["money"]["margin"] == round(4 * (9600 - 9500 + 200 + 1000 - 288 - 192), 2)
    assert orders.totals(orders.get_card(conn, cid)["items"]) == {"sale": 38400.0, "cost": 38000.0, "margin": got["money"]["margin"], "no_purchase": [],
                                                                  "qty": 4}           # хвост З7: the sums of a known purchase are as before
    assert [e["kind"] for e in orders.list_events(conn, cid)][0] == "item"
    orders.update_item(conn, iid, 2, 9000.0, NOW, "owner")
    again = orders.get_card(conn, cid)["items"][0]
    assert again["qty"] == 2 and again["sale_unit"] == 9000.0 and again["supplier_price"] == 10000.0 and again["terms"] == TERMS
    orders.remove_item(conn, iid, NOW, "owner")
    assert orders.get_card(conn, cid)["items"] == [] and orders.totals([]) == {"sale": 0.0, "cost": 0.0, "margin": 0.0, "no_purchase": [], "qty": 0}


def test_a_position_without_terms_still_counts_and_bad_numbers_are_refused(tmp_path):
    conn = db(tmp_path)
    cid = card(conn)
    item(conn, cid, terms=None)
    assert orders.get_card(conn, cid)["items"][0]["money"]["has_terms"] is False
    for bad in (dict(qty=0), dict(qty=101), dict(qty=1.5), dict(qty="x"), dict(sale_unit=0), dict(sale_unit=-1), dict(sale_unit=1_000_001),
                dict(sale_unit=float("nan")), dict(supplier_price=0), dict(supplier_price="abc"), dict(goods_id=""), dict(name="")):
        with pytest.raises(orders.OrderError):
            item(conn, cid, **bad)
    with pytest.raises(orders.OrderError):
        item(conn, 999)
    with pytest.raises(orders.OrderError):
        orders.update_item(conn, 999, 1, 100.0, NOW, "owner")
    with pytest.raises(orders.OrderError):
        orders.remove_item(conn, 999, NOW, "owner")


def ready(conn, cid):
    """The data «Заказ оформлен» needs (every later stage needs it too)."""
    pid = points.save_point(conn, {"name": "Точка %d" % cid})
    orders.update_card(conn, cid, {"point_id": str(pid), "delivery_date": "2026-10-14", "client_phone": "+7 900 000 00 15"}, NOW, "owner")


def stage_id(conn, code):
    return next(s["id"] for s in orders.list_stages(conn) if s["code"] == code)


def test_a_card_moves_and_the_move_is_in_the_feed_with_a_new_entry_time(tmp_path):
    conn = db(tmp_path)
    cid = card(conn)
    assert orders.move_card(conn, cid, stage_id(conn, "talk"), "2026-10-12T13:00:00", "owner") is True
    c = orders.get_card(conn, cid)
    assert c["stage"]["code"] == "talk" and c["entered_at"] == "2026-10-12T13:00:00"
    assert orders.list_events(conn, cid)[0]["text"] == "Этап: «Новый заказ» → «Переговоры»"


def test_a_repeated_move_to_the_same_stage_changes_and_writes_nothing(tmp_path):
    conn = db(tmp_path)
    cid = card(conn)
    orders.move_card(conn, cid, stage_id(conn, "talk"), "2026-10-12T13:00:00", "owner")
    before = len(orders.list_events(conn, cid))
    assert orders.move_card(conn, cid, stage_id(conn, "talk"), "2026-10-12T15:00:00", "owner") is False
    assert len(orders.list_events(conn, cid)) == before and orders.get_card(conn, cid)["entered_at"] == "2026-10-12T13:00:00"


def test_placing_an_order_needs_positions_point_date_and_phone_and_a_refusal_changes_nothing(tmp_path):
    conn = db(tmp_path)
    cid = card(conn)
    events = len(orders.list_events(conn, cid))
    with pytest.raises(orders.OrderError) as err:
        orders.move_card(conn, cid, stage_id(conn, "placed"), NOW, "owner")
    for part in ("позиции", "точка", "дата доставки", "телефон клиента"):
        assert part in str(err.value)
    assert orders.get_card(conn, cid)["stage"]["code"] == "new" and len(orders.list_events(conn, cid)) == events + 1
    refusal = orders.list_events(conn, cid)[0]                                                              # the only new record: the red refusal
    assert (refusal["kind"], refusal["level"]) == ("refused", "red") and str(err.value) in refusal["text"]
    pid = points.save_point(conn, {"name": "Гамма"})
    item(conn, cid)
    changes = {"point_id": str(pid), "delivery_date": "2026-10-14", "client_phone": "+7 900 000 00 15"}
    assert orders.move_card(conn, cid, stage_id(conn, "placed"), NOW, "owner", changes) is True              # the fields come with the move
    assert orders.get_card(conn, cid)["stage"]["code"] == "placed"


def test_cancel_and_return_need_their_own_data(tmp_path):
    conn = db(tmp_path)
    cid = card(conn)
    with pytest.raises(orders.OrderError):
        orders.move_card(conn, cid, stage_id(conn, "cancel"), NOW, "owner")
    assert orders.move_card(conn, cid, stage_id(conn, "cancel"), NOW, "owner", {"cancel_reason": "клиент передумал"}) is True
    other = card(conn)
    with pytest.raises(orders.OrderError) as err:
        orders.move_card(conn, other, stage_id(conn, "return"), NOW, "owner", {"return_kind": "client"})
    assert "причина возврата" in str(err.value) and "сумма возврата" in str(err.value) and "тип возврата" not in str(err.value)
    assert orders.move_card(conn, other, stage_id(conn, "return"), NOW, "owner",
                            {"return_kind": "supplier", "return_reason": "брак", "return_amount": "9600"}) is True
    with pytest.raises(orders.OrderError):
        orders.move_card(conn, other, 999, NOW, "owner")
    with pytest.raises(orders.OrderError):
        orders.move_card(conn, 999, stage_id(conn, "talk"), NOW, "owner")


from datetime import datetime


def test_the_board_shows_columns_counts_sums_days_and_overdue_marks(tmp_path):
    conn = db(tmp_path)
    orders.update_stage(conn, stage_id(conn, "new"), "Новый заказ", "#7F56D9", "open", "", 24)           # a day is the limit
    a = card(conn, client_name="Иван")
    b = orders.create_card(conn, {"client_name": "Ольга", "tags": "vip"}, "2026-10-11T09:00:00", "owner")
    item(conn, a)
    board = orders.board(conn, datetime(2026, 10, 12, 12, 0, 0))
    assert [c["stage"]["code"] for c in board["columns"]][:2] == ["new", "talk"]
    new = board["columns"][0]
    assert new["count"] == 2 and new["sum"] == 38400.0
    by_id = {c["id"]: c for c in new["cards"]}
    assert by_id[a]["days"] == 0 and by_id[a]["overdue"] is False and by_id[a]["items_text"] == "Pirelli 225/55 R17 × 4"
    assert by_id[b]["days"] == 1 and by_id[b]["overdue"] is True and by_id[b]["tags"] == ["vip"]
    assert board["summary"] == {"open": 2, "overdue": 1, "new_today": 1, "sum_open": 38400.0}


def test_board_filters_by_point_tag_and_search_and_survives_nothing_matching(tmp_path):
    conn = db(tmp_path)
    pid = points.save_point(conn, {"name": "Гамма"})
    a = card(conn, client_name="Иван", client_phone="+7 900 000 00 15", point_id=str(pid), tags="срочно")
    card(conn, client_name="Ольга")
    ids = lambda **kw: [c["id"] for col in orders.board(conn, datetime(2026, 10, 12, 12), **kw)["columns"] for c in col["cards"]]
    assert ids(point_id=pid) == [a] and ids(tag="срочно") == [a] and ids(query="иван") == [a] and ids(query="9000000015") == [a]
    assert ids(query=str(a)) == [a] and ids(query="нет такого") == [] and len(ids()) == 2
    assert orders.board(conn, datetime(2026, 10, 12, 12), query="нет такого")["summary"]["open"] == 0


def test_the_board_works_on_an_empty_database(tmp_path):
    conn = store.connect(str(tmp_path / "e.db"))
    board = orders.board(conn, datetime(2026, 10, 12, 12))
    assert board["columns"] == [] and board["summary"] == {"open": 0, "overdue": 0, "new_today": 0, "sum_open": 0.0}


def test_recording_a_sale_makes_one_sale_per_position_once_and_a_return_reverses_it_once(tmp_path):
    from tires_sync import sales
    conn = db(tmp_path)
    cid = card(conn)
    item(conn, cid)
    item(conn, cid, goods_id="G2", name="Kumho 205/55 R16", qty=2, sale_unit=5000.0, supplier_price=5600.0, diameter=16, season="summer")
    ready(conn, cid)
    orders.move_card(conn, cid, stage_id(conn, "done"), NOW, "owner")
    ids = orders.record_sales(conn, cid, "2026-10-12", NOW, "owner", "2026-10-12")
    assert len(ids) == 2 and orders.get_card(conn, cid)["sale_ids"] == ids
    rows = sales.list_sales(conn, "2026-10-12", "2026-10-12")
    assert sorted(r["pieces"] for r in rows) == [2, 4] and sum(r["sale_total"] for r in rows) == 38400.0 + 10000.0
    with pytest.raises(orders.OrderError):
        orders.record_sales(conn, cid, "2026-10-12", NOW, "owner", "2026-10-12")                              # a second press changes nothing
    assert len(sales.list_sales(conn, "2026-10-12", "2026-10-12")) == 2
    assert orders.reverse_sales(conn, cid, NOW, "owner") == 2
    assert sales.summarize(sales.list_sales(conn, "2026-10-12", "2026-10-12"))["pieces"] == 0
    assert orders.get_card(conn, cid)["sale_ids"] == [] and orders.reverse_sales(conn, cid, NOW, "owner") == 0
    empty = card(conn)
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage_id(conn, "done"), empty))             # a card without positions on the successful stage
    conn.commit()
    with pytest.raises(orders.OrderError):
        orders.record_sales(conn, empty, "2026-10-12", NOW, "owner", "2026-10-12")                            # no positions: nothing to record


# ---- the fix pass after the final review

def test_a_refused_move_leaves_the_fields_and_the_feed_exactly_as_they_were(tmp_path):
    conn = db(tmp_path)
    cid = card(conn)
    events = len(orders.list_events(conn, cid))
    with pytest.raises(orders.OrderError):
        orders.move_card(conn, cid, stage_id(conn, "placed"), NOW, "owner", {"client_phone": "+7 900 000 00 15", "comment": "срочно"})
    after = orders.get_card(conn, cid)
    assert after["client_phone"] == "" and after["comment"] == "" and len(orders.list_events(conn, cid)) == events + 1 and after["stage"]["code"] == "new"
    assert [(e["kind"], e["level"]) for e in orders.list_events(conn, cid)[:1]] == [("refused", "red")]         # the refusal only, no field record
    assert orders.move_card(conn, cid, stage_id(conn, "new"), NOW, "owner", {"comment": "сохранить"}) is False       # no move, but the fields are kept
    assert orders.get_card(conn, cid)["comment"] == "сохранить"


def test_a_placed_order_cannot_be_stripped_of_what_placing_needed(tmp_path):
    conn = db(tmp_path)
    cid = card(conn)
    pid = points.save_point(conn, {"name": "Гамма"})
    iid = item(conn, cid)
    orders.move_card(conn, cid, stage_id(conn, "placed"), NOW, "owner",
                     {"point_id": str(pid), "delivery_date": "2026-10-14", "client_phone": "+7 900 000 00 15"})
    for bad in ({"client_phone": ""}, {"point_id": ""}, {"delivery_date": ""}):
        with pytest.raises(orders.OrderError):
            orders.update_card(conn, cid, bad, NOW, "owner")
    with pytest.raises(orders.OrderError):
        orders.remove_item(conn, iid, NOW, "owner")
    c = orders.get_card(conn, cid)
    assert c["client_phone"] and c["point_id"] and c["delivery_date"] and len(c["items"]) == 1
    orders.update_card(conn, cid, {"client_phone": "+7 911 111 11 11", "comment": "ok"}, NOW, "owner")              # a correction is fine
    orders.move_card(conn, cid, stage_id(conn, "talk"), NOW, "owner")                                              # back to an earlier stage: editable again
    orders.remove_item(conn, iid, NOW, "owner")
    assert orders.get_card(conn, cid)["items"] == []


def test_a_sale_is_recorded_only_on_the_successful_stage_and_only_once_even_if_a_second_request_races(tmp_path):
    from tires_sync import sales
    conn = db(tmp_path)
    cid = card(conn)
    item(conn, cid)
    with pytest.raises(orders.OrderError):
        orders.record_sales(conn, cid, "2026-10-12", NOW, "owner", "2026-10-12")                                  # «Новый заказ» is not a sale
    ready(conn, cid)
    orders.move_card(conn, cid, stage_id(conn, "done"), NOW, "owner")
    conn.execute("UPDATE order_card SET sale_ids = '[-1]' WHERE id = ?", (cid,))                                  # another request has just claimed the order
    conn.commit()
    with pytest.raises(orders.OrderError):
        orders.record_sales(conn, cid, "2026-10-12", NOW, "owner", "2026-10-12")
    assert sales.list_sales(conn, "2026-10-12", "2026-10-12") == []
    conn.execute("UPDATE order_card SET sale_ids = '[]' WHERE id = ?", (cid,))
    conn.commit()
    conn.execute("UPDATE order_item SET qty = 100, sale_unit = 1000000 WHERE order_id = ?", (cid,))             # 100 × 1 000 000 > the limit of one sale
    conn.commit()
    with pytest.raises(orders.OrderError):
        orders.record_sales(conn, cid, "2026-10-12", NOW, "owner", "2026-10-12")
    assert orders.get_card(conn, cid)["sale_ids"] == []                                                          # a failure gives the claim back


def test_an_order_with_a_recorded_sale_cannot_be_cancelled_before_the_sale_is_reversed(tmp_path):
    conn = db(tmp_path)
    cid = card(conn)
    item(conn, cid)
    ready(conn, cid)
    orders.move_card(conn, cid, stage_id(conn, "done"), NOW, "owner")
    orders.record_sales(conn, cid, "2026-10-12", NOW, "owner", "2026-10-12")
    with pytest.raises(orders.OrderError) as err:
        orders.move_card(conn, cid, stage_id(conn, "cancel"), NOW, "owner", {"cancel_reason": "передумал"})
    assert "сторнируйте" in str(err.value) and orders.get_card(conn, cid)["stage"]["code"] == "done"
    assert orders.get_card(conn, cid)["cancel_reason"] == ""                                                     # the refused move saved nothing
    orders.reverse_sales(conn, cid, NOW, "owner")
    assert orders.move_card(conn, cid, stage_id(conn, "cancel"), NOW, "owner", {"cancel_reason": "передумал"}) is True


def test_the_kind_of_a_system_stage_is_fixed(tmp_path):
    conn = db(tmp_path)
    done = orders.get_stage(conn, stage_id(conn, "done"))
    with pytest.raises(orders.OrderError):
        orders.update_stage(conn, done["id"], done["name"], done["color"], "open", done["hint"], done["limit_hours"])
    orders.update_stage(conn, done["id"], "Готово", "#079455", "won", "", 0)                                       # a rename is fine
    assert orders.get_stage(conn, done["id"])["name"] == "Готово"


# ---- the second review

def sold_card(conn):
    cid = card(conn)
    item(conn, cid)
    ready(conn, cid)
    orders.move_card(conn, cid, stage_id(conn, "done"), NOW, "owner")
    orders.record_sales(conn, cid, "2026-10-12", NOW, "owner", "2026-10-12")
    return cid


def test_positions_are_frozen_while_a_sale_is_recorded(tmp_path):
    conn = db(tmp_path)
    cid = sold_card(conn)
    iid = orders.get_card(conn, cid)["items"][0]["id"]
    for call in (lambda: item(conn, cid, goods_id="G9"), lambda: orders.update_item(conn, iid, 1, 100.0, NOW, "owner"),
                 lambda: orders.remove_item(conn, iid, NOW, "owner")):
        with pytest.raises(orders.OrderError) as err:
            call()
        assert "сторнируйте" in str(err.value)
    assert orders.get_card(conn, cid)["items"][0]["qty"] == 4
    orders.reverse_sales(conn, cid, NOW, "owner")
    orders.update_item(conn, iid, 2, 9000.0, NOW, "owner")                                                        # free again after the reversal
    assert orders.get_card(conn, cid)["items"][0]["qty"] == 2


def test_with_a_live_sale_a_card_may_leave_the_successful_stage_only_for_a_closing_one(tmp_path):
    conn = db(tmp_path)
    cid = sold_card(conn)
    refusal = orders.add_stage(conn, "Отказ клиента", "#D92D20", "lost", "", 0)
    for target in (stage_id(conn, "talk"), refusal, stage_id(conn, "moving")):
        with pytest.raises(orders.OrderError) as err:
            orders.move_card(conn, cid, target, NOW, "owner")
        assert "сторнируйте" in str(err.value)
    assert orders.get_card(conn, cid)["stage"]["code"] == "done"
    assert orders.move_card(conn, cid, stage_id(conn, "return"), NOW, "owner",
                            {"return_kind": "client", "return_reason": "не подошли", "return_amount": "38400"}) is True    # a return is allowed: it ends with the reversal


def test_the_sale_claim_needs_the_card_to_still_be_on_the_successful_stage(tmp_path):
    from tires_sync import sales
    conn = db(tmp_path)
    cid = card(conn)
    item(conn, cid)
    ready(conn, cid)
    orders.move_card(conn, cid, stage_id(conn, "done"), NOW, "owner")
    real = orders.get_card

    def stale(conn_, card_id):                       # another worker moved the card between the read and the claim
        c = real(conn_, card_id)
        conn_.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage_id(conn_, "cancel"), card_id))
        conn_.commit()
        return c

    orders.get_card = stale
    try:
        with pytest.raises(orders.OrderError):
            orders.record_sales(conn, cid, "2026-10-12", NOW, "owner", "2026-10-12")
    finally:
        orders.get_card = real
    assert sales.list_sales(conn, "2026-10-12", "2026-10-12") == [] and real(conn, cid)["sale_ids"] == []


def test_a_hanging_claim_is_released_and_the_sales_written_before_the_crash_are_reversed(tmp_path):
    from tires_sync import sales
    conn = db(tmp_path)
    cid = card(conn)
    item(conn, cid)
    ready(conn, cid)
    orders.move_card(conn, cid, stage_id(conn, "done"), NOW, "owner")
    sales.add_sale(conn, day="2026-10-12", goods_id="G1", brand_key="PIRELLI", season="winter", diameter=17, name="x", ad_kind="single", pieces=4,
                   sale_total=38400.0, supplier_price=10000.0, terms=TERMS, fee_pct=3.0, promo_pct=2.0, account_id=None, point="", note="Заказ №%d" % cid,
                   now=NOW, author="owner", today="2026-10-12", source="order")                          # written, then the process died
    conn.execute("UPDATE order_card SET sale_ids = '[-1]' WHERE id = ?", (cid,))
    conn.commit()
    assert sales.summarize(sales.list_sales(conn, "2026-10-12", "2026-10-12"))["pieces"] == 4
    assert orders.reverse_sales(conn, cid, NOW, "owner") == 1
    assert sales.summarize(sales.list_sales(conn, "2026-10-12", "2026-10-12"))["pieces"] == 0 and orders.get_card(conn, cid)["sale_ids"] == []


def test_the_data_of_placing_is_required_for_every_later_stage_not_only_for_the_placing_itself(tmp_path):
    conn = db(tmp_path)
    cid = card(conn)
    for code in ("moving", "at_point", "oth", "done"):
        with pytest.raises(orders.OrderError) as err:
            orders.move_card(conn, cid, stage_id(conn, code), NOW, "owner")
        assert "точка" in str(err.value) and orders.get_card(conn, cid)["stage"]["code"] == "new"
    mine = orders.add_stage(conn, "Мой поздний", "#7F56D9", "open", "", 0)                                         # a user stage after «Заказ оформлен»
    with pytest.raises(orders.OrderError):
        orders.move_card(conn, cid, mine, NOW, "owner")
    assert orders.move_card(conn, cid, stage_id(conn, "talk"), NOW, "owner") is True                              # an earlier stage needs nothing
    item(conn, cid)
    ready(conn, cid)
    assert orders.move_card(conn, cid, stage_id(conn, "at_point"), NOW, "owner") is True


def test_the_protection_of_a_placed_order_does_not_depend_on_where_the_placed_stage_stands(tmp_path):
    conn = db(tmp_path)
    cid = card(conn)
    item(conn, cid)
    ready(conn, cid)
    orders.move_card(conn, cid, stage_id(conn, "moving"), NOW, "owner")
    for _ in range(8):
        orders.shift_stage(conn, stage_id(conn, "placed"), 1)                                                     # «Заказ оформлен» pushed to the end
    with pytest.raises(orders.OrderError):
        orders.update_card(conn, cid, {"point_id": ""}, NOW, "owner")


def test_the_reason_of_a_cancelled_card_and_the_data_of_a_return_cannot_be_emptied(tmp_path):
    conn = db(tmp_path)
    cid = card(conn)
    orders.move_card(conn, cid, stage_id(conn, "cancel"), NOW, "owner", {"cancel_reason": "передумал"})
    with pytest.raises(orders.OrderError):
        orders.update_card(conn, cid, {"cancel_reason": ""}, NOW, "owner")
    orders.update_card(conn, cid, {"cancel_reason": "другая причина"}, NOW, "owner")
    other = card(conn)
    orders.move_card(conn, other, stage_id(conn, "return"), NOW, "owner", {"return_kind": "client", "return_reason": "брак", "return_amount": "500"})
    for bad in ({"return_kind": ""}, {"return_reason": ""}, {"return_amount": "0"}):
        with pytest.raises(orders.OrderError):
            orders.update_card(conn, other, bad, NOW, "owner")


def test_card_fields_for_the_sheet_are_saved_checked_and_logged(tmp_path):
    conn = db(tmp_path)
    cid = orders.create_card(conn, dict(client_name="Иван", source="pvz", deal_kind="stock", transfer_number="26R-922352"), NOW, "o")
    card = orders.get_card(conn, cid)
    assert (card["source"], card["deal_kind"], card["transfer_number"]) == ("pvz", "stock", "26R-922352")
    plain = orders.get_card(conn, orders.create_card(conn, dict(client_name="Пётр"), NOW, "o"))
    assert (plain["source"], plain["deal_kind"], plain["settled_point_at"]) == ("", "webmim", "")           # a deal goes through WebMIM unless said otherwise
    orders.update_card(conn, cid, dict(settled_point_at="2026-10-14", codes_written_off_at="2026-10-15", settled_supplier_at="2026-10-16"), NOW, "o")
    card = orders.get_card(conn, cid)
    assert (card["settled_point_at"], card["codes_written_off_at"], card["settled_supplier_at"]) == ("2026-10-14", "2026-10-15", "2026-10-16")
    texts = [e["text"] for e in orders.list_events(conn, cid)]
    assert any("Расчёт с точкой" in t and "2026-10-14" in t for t in texts) and any("Списание кодов" in t for t in texts)
    orders.update_card(conn, cid, dict(settled_point_at=""), NOW, "o")                                      # taking the mark back is allowed
    assert orders.get_card(conn, cid)["settled_point_at"] == ""
    for bad in (dict(source="radio"), dict(deal_kind="barter"), dict(transfer_number="x" * 41), dict(settled_point_at="завтра"),
                dict(codes_written_off_at="2026-13-40"), dict(settled_supplier_at="1999-01-01")):
        with pytest.raises(orders.OrderError):
            orders.update_card(conn, cid, bad, NOW, "o")


def test_a_position_keeps_the_suppliers_article_and_falls_back_to_the_internal_code(tmp_path):
    conn = db(tmp_path)
    cid = orders.create_card(conn, dict(client_name="Иван"), NOW, "o")
    orders.add_item(conn, cid, goods_id="126496625866", code="T743172", name="Ikon 175/70/14", brand_key="ikon", season="winter", diameter=14,
                    territory="1", qty=4, sale_unit=3550, supplier_price=3488, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")
    orders.add_item(conn, cid, goods_id="777", name="Без артикула", brand_key="x", season="all", diameter=15, territory="1", qty=1,
                    sale_unit=100, supplier_price=90, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")
    first, second = orders.get_card(conn, cid)["items"]
    assert first["code"] == "T743172" and second["code"] == ""
    from tires_sync import order_templates as tpl
    text, _ = tpl.render(conn, orders.get_card(conn, cid), "{items_full}")
    assert "Код T743172" in text and "Код 777" in text and "126496625866" not in text


def test_the_delivery_time_of_the_point_is_filled_in_when_the_point_is_chosen_and_never_over_a_typed_one(tmp_path):
    from tires_sync import order_points as points
    conn = db(tmp_path)
    pid = points.save_point(conn, dict(name="Бета", default_time="с 10 до 20"))
    cid = orders.create_card(conn, dict(client_name="Иван", point_id=pid), NOW, "o")
    assert orders.get_card(conn, cid)["delivery_time"] == "с 10 до 20"                                   # a new deal on a point
    other = orders.create_card(conn, dict(client_name="Пётр"), NOW, "o")
    orders.update_card(conn, other, dict(point_id=str(pid), delivery_time=""), NOW, "o")                 # the card form sends an empty time with the point
    assert orders.get_card(conn, other)["delivery_time"] == "с 10 до 20"
    typed = orders.create_card(conn, dict(client_name="Анна", point_id=pid, delivery_time="до 18:00"), NOW, "o")
    assert orders.get_card(conn, typed)["delivery_time"] == "до 18:00"
    orders.update_card(conn, other, dict(delivery_time="завтра"), NOW, "o")
    orders.update_card(conn, other, dict(point_id=""), NOW, "o")
    assert orders.get_card(conn, other)["delivery_time"] == "завтра"                                      # nothing is wiped when the point is taken away


def test_the_search_by_number_finds_that_order_and_not_every_order_that_has_the_digit(tmp_path):
    conn = db(tmp_path)
    ids = [card_of_phone(conn, dict(client_name="Клиент %d" % n, client_phone="+7 900 000 01 3%d" % (n % 10)), NOW, "o") for n in range(1, 13)]
    import datetime as dt
    def found(q):
        data = orders.board(conn, dt.datetime(2026, 10, 12, 13, 0, 0), query=q)
        return sorted(c["id"] for col in data["columns"] for c in col["cards"])
    assert found("1") == [ids[0]] and found("№12") == [ids[11]] and found("#2") == [ids[1]] and found("заказ 3") == [ids[2]]
    assert found("клиент 5") == [ids[4]] and len(found("000 01")) == 12 and found("9999") == []              # a name and a phone fragment still work


def test_a_sale_of_an_absurd_amount_is_refused_before_anything_is_written(tmp_path):
    conn = db(tmp_path)
    cid = orders.create_card(conn, dict(client_name="Иван"), NOW, "o")
    orders.add_item(conn, cid, goods_id="1", name="Шина", brand_key="x", season="all", diameter=15, territory="1", qty=100, sale_unit=999_999,
                    supplier_price=1, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")
    done = next(s["id"] for s in orders.list_stages(conn) if s["code"] == "done")
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (done, cid))
    conn.commit()
    with pytest.raises(orders.OrderError):
        orders.record_sales(conn, cid, "2026-10-12", NOW, "o", "2026-10-12")
    assert orders.get_card(conn, cid)["sale_ids"] == []


def test_the_chat_field_becomes_a_link_only_for_our_chat_or_an_avito_page():
    assert orders.chat_link("Ч-12") == ("/chats/12", False)
    assert orders.chat_link(" https://www.avito.ru/profile/messenger/channel/u2i-abc ") == ("https://www.avito.ru/profile/messenger/channel/u2i-abc", True)
    assert orders.chat_link("javascript:alert(1)") is None and orders.chat_link("https://evil.example/x") is None and orders.chat_link("") is None


def test_board_search_finds_by_webmim_number_and_any_written_phone(tmp_path):
    conn = db(tmp_path)
    a = card(conn, client_name="Иван", client_phone="+7 900 000-00-11", webmim_number="7700001")
    b = card(conn, client_name="Ольга", client_phone="8 (900) 000-00-15")
    ids = lambda q: [c["id"] for col in orders.board(conn, datetime(2026, 10, 12, 12), query=q)["columns"] for c in col["cards"]]
    assert ids("7700001") == [a] and ids("№7700001") == [a] and ids("77000") == []
    for written in ("+7 900 000-00-11", "89000000011", "8 (900) 000 00 11", "9000000011"):
        assert ids(written) == [a], written
    assert ids("+79000000015") == [b] and ids("нет такого") == [] and ids(str(b)) == [b]


def test_board_phone_search_stays_inside_the_point(tmp_path):
    conn = db(tmp_path)
    mine, other = points.save_point(conn, {"name": "Гамма"}), points.save_point(conn, {"name": "Мурино"})
    a = card(conn, client_name="Иван", client_phone="+7 900 000-00-11", point_id=str(mine))
    card_of_phone(conn, dict(client_name="Пётр", client_phone="8 900 000 00 11", point_id=str(other), webmim_number="7700001"), NOW, "owner")
    ids = lambda q: [c["id"] for col in orders.board(conn, datetime(2026, 10, 12, 12), point_id=mine, query=q)["columns"] for c in col["cards"]]
    assert ids("89000000011") == [a] and ids("7700001") == []


def test_a_position_without_its_purchase_makes_the_margin_unknown_not_bigger(tmp_path):
    """Хвост З7: the purchase 0 (a point tyre before it is typed) is «не указана»: no margin of 4 × the sale price; the sale sum stays."""
    conn = db(tmp_path)
    cid = card(conn, deal_kind="stock")
    item(conn, cid)
    item(conn, cid, goods_id=orders.POINT_GOODS, name="Шина с точки 205/55 R16", supplier_price=0, terms={}, qty=4, sale_unit=5000.0)
    money = orders.totals(orders.get_card(conn, cid)["items"])
    assert money["margin"] is None and money["no_purchase"] == ["Шина с точки 205/55 R16"]
    assert money["sale"] == 38400.0 + 20000.0 and money["qty"] == 8
    point_item = next(i for i in orders.get_card(conn, cid)["items"] if i["goods_id"] == orders.POINT_GOODS)
    orders.update_item(conn, point_item["id"], 4, 5000.0, NOW, "owner", supplier_price="4000")
    known = orders.totals(orders.get_card(conn, cid)["items"])
    assert known["no_purchase"] == [] and known["margin"] == round(sum(i["money"]["margin"] for i in orders.get_card(conn, cid)["items"]), 2)
