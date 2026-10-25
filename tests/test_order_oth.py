"""«На ОТХ»: seven days to sell the goods we bought out, the alerts about the end of that time."""
from __future__ import annotations

from tests.test_order_reserve import card_at, db, stage
from tires_sync import order_reserve as reserve, orders


def test_entering_oth_gives_seven_days_and_replaces_the_reserve_date(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    orders.update_card(conn, cid, {"reserve_until": "2026-10-25"}, "2026-10-16T10:00:00", "o")        # extended earlier, says nothing about the ОТХ stay
    orders.move_card(conn, cid, stage(conn, "oth"), "2026-10-17T10:00:00", "o")
    assert orders.get_card(conn, cid)["reserve_until"] == "2026-10-24"
    assert any("На ОТХ: срок продажи до 2026-10-24" in e["text"] for e in orders.list_events(conn, cid))


def test_oth_alerts_come_the_day_before_and_on_the_day_once_each_with_their_own_text(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "oth", "2026-10-17T10:00:00")
    sent = []
    notify = lambda card, text: (sent.append(text), "отправлено")[1]
    assert reserve.check(conn, "2026-10-22T12:00:00", notify) == 0
    assert reserve.check(conn, "2026-10-23T12:00:00", notify) == 1 and "продажи на ОТХ заканчивается завтра (24.10.2026)" in sent[0]
    assert reserve.check(conn, "2026-10-23T13:00:00", notify) == 0
    assert reserve.check(conn, "2026-10-24T12:00:00", notify) == 1 and "заканчивается сегодня" in sent[1] and "верните поставщику" in sent[1]
    assert reserve.check(conn, "2026-10-26T12:00:00", notify) == 0                     # the day passed and it was told already
    assert any("конце срока на ОТХ" in e["text"] for e in orders.list_events(conn, cid))


def test_a_card_that_left_oth_gets_no_alert_and_the_old_reserve_alert_does_not_block_oth(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")                             # reserve until 18.10
    assert reserve.check(conn, "2026-10-18T12:00:00", lambda c, t: "ok") == 1
    orders.move_card(conn, cid, stage(conn, "oth"), "2026-10-18T13:00:00", "o")        # until 25.10
    assert reserve.check(conn, "2026-10-24T12:00:00", lambda c, t: "ok") == 1          # not mistaken for the reserve alerts already made
    orders.move_card(conn, cid, stage(conn, "done"), "2026-10-24T13:00:00", "o")
    assert reserve.check(conn, "2026-10-25T12:00:00", lambda c, t: "ok") == 0


def test_cards_that_stood_in_oth_before_the_seven_days_get_no_alert_and_going_back_resets_the_date(tmp_path):
    conn = db(tmp_path)
    old = card_at(conn, "oth", "2026-10-17T10:00:00")
    conn.execute("DELETE FROM order_event WHERE order_id = ? AND text LIKE 'На ОТХ: срок%'", (old,))      # as if moved before this change
    conn.execute("UPDATE order_card SET reserve_until = '2026-10-18' WHERE id = ?", (old,))
    assert reserve.check(conn, "2026-10-20T12:00:00", lambda c, t: "ok") == 0
    cid = card_at(conn, "oth", "2026-10-17T10:00:00")                                    # until 24.10
    orders.move_card(conn, cid, stage(conn, "at_point"), "2026-10-18T10:00:00", "o")
    assert orders.get_card(conn, cid)["reserve_until"] == "2026-10-21"


def test_an_order_returned_to_the_supplier_in_full_gets_no_alert_of_the_seven_days(tmp_path):
    """Хвост З14: every position is marked «Вернуть поставщику»: nothing is left to sell, no «конец 7 дней»; a part returned still alerts."""
    from tires_sync import order_oth
    conn = db(tmp_path)
    full, part = card_at(conn, "oth", "2026-10-17T10:00:00"), card_at(conn, "oth", "2026-10-17T10:00:00")
    orders.add_item(conn, part, goods_id="2", name="Ikon 195/65 R15", brand_key="ikon", season="winter", diameter=15, territory="1",
                    qty=2, sale_unit=4000, supplier_price=3000, terms=None, fee_pct=0, promo_pct=0, now="2026-10-17T11:00:00", author="o")
    for item in orders.get_card(conn, full)["items"] + orders.get_card(conn, part)["items"][:1]:
        order_oth.mark_returned(conn, item["id"], "2026-10-18T10:00:00", "o")
    told = []
    assert reserve.check(conn, "2026-10-23T12:00:00", lambda card, text: (told.append(card["id"]), "ok")[1]) == 1 and told == [part]
    order_oth.unmark_returned(conn, orders.get_card(conn, full)["items"][0]["id"], "2026-10-23T12:30:00", "o")
    assert reserve.check(conn, "2026-10-23T13:00:00", lambda card, text: (told.append(card["id"]), "ok")[1]) == 1 and told == [part, full]


def test_a_part_of_one_position_returned_still_alerts_and_the_whole_quantity_does_not(tmp_path):
    """A return by quantity (the owner 2026-10-24): what is left is still to sell; every piece back, nothing to sell."""
    from tires_sync import order_oth
    conn = db(tmp_path)
    cid = card_at(conn, "oth", "2026-10-17T10:00:00")
    item = orders.get_card(conn, cid)["items"][0]["id"]
    order_oth.mark_returned(conn, item, "2026-10-18T10:00:00", "o", qty="3")
    assert not reserve._all_returned(conn, cid)
    told = []
    assert reserve.check(conn, "2026-10-23T12:00:00", lambda card, text: (told.append(card["id"]), "ok")[1]) == 1 and told == [cid]
    order_oth.mark_returned(conn, item, "2026-10-23T12:10:00", "o", qty="1")
    assert reserve._all_returned(conn, cid)
