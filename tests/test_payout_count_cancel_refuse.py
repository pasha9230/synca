"""The owner 2026-11-26: a correction of the payout by the tyres counted by hand (75 ₽ a tyre × the count), and «Отмена» (before the tyres
came: «О») apart from «Невыкуп» (the tyres came, the client refused: «Н») in «Реализация» of the Google table."""
from __future__ import annotations

import pytest

from panel_helpers import post
from test_order_sheet import placed, stage, table  # noqa: F401 - the fixture
from test_point_income import panel, point

from tires_sync import order_report, order_sheet as sheet, orders, point_payouts

NOW = "2026-12-15T12:00:00"


# ---- 1. «по ручному подсчёту» -----------------------------------------------------------------------------------------------------------

def test_a_correction_by_the_count_is_the_rate_a_tyre_times_the_count(tmp_path):
    from test_point_income import db
    conn = db(tmp_path)
    a = point(conn, "Альфа")
    adj = point_payouts.adjust(conn, a, None, "пересчёт по накладным", "2026-12-05", NOW, "owner", pieces=6)
    row = conn.execute("SELECT amount, reason FROM point_adjust WHERE id = ?", (adj,)).fetchone()
    assert row == (450.0, "по ручному подсчёту 6 шт.: пересчёт по накладным")
    assert order_report.build(conn, "2026-12-01", "2026-12-31")["fees"]["adj"] == 450
    order_report.set_point_rates(conn, a, "2026-12-10", "2026-12-10", NOW, "owner", {"point_fee": 80})
    assert conn.execute("SELECT amount FROM point_adjust WHERE id = ?",
                        (point_payouts.adjust(conn, a, None, "x", "2026-12-10", NOW, "owner", pieces=2),)).fetchone()[0] == 160   # its own rate
    assert point_payouts.adjust(conn, a, 160, "x", "2026-12-10", NOW, "owner", pieces=2)                  # the same sum typed: ok
    for amount, pieces in ((100, 2), (None, 0), (None, 10001), (None, None)):
        with pytest.raises(point_payouts.PayoutError):
            point_payouts.adjust(conn, a, amount, "x", "2026-12-10", NOW, "owner", pieces=pieces)
    with pytest.raises(point_payouts.PayoutError):                                  # the day counted once an order: a sum only
        point_payouts.adjust(conn, a, None, "x", "2026-10-20", NOW, "owner", pieces=4)
    with pytest.raises(point_payouts.PayoutError):                                  # a reason as always
        point_payouts.adjust(conn, a, None, "", "2026-12-10", NOW, "owner", pieces=4)


def test_the_ledger_page_takes_the_count(tmp_path):
    app, owner, tok, conn, a, b = panel(tmp_path)
    page = owner.get("/points/payouts").get_data(as_text=True)
    assert "data-adjust-pieces" in page and "по ручному подсчёту" in page
    keep = {"from": "2026-12-01", "to": "2026-12-31", "pay": "all"}
    post(owner, "/points/payouts/adjust", tok, point_id=str(a), amount="", pieces="4", reason="сверка", day="2026-12-10", **keep)
    assert conn.execute("SELECT amount, reason FROM point_adjust").fetchall() == [(300.0, "по ручному подсчёту 4 шт.: сверка")]
    post(owner, "/points/payouts/adjust", tok, point_id=str(a), amount="", pieces="4.5", reason="сверка", day="2026-12-10", **keep)
    post(owner, "/points/payouts/adjust", tok, point_id=str(a), amount="", pieces="", reason="сверка", day="2026-12-10", **keep)
    assert conn.execute("SELECT COUNT(*) FROM point_adjust").fetchone()[0] == 1                       # no number: nothing written
    shown = owner.get("/points/payouts?from=2026-12-01&to=2026-12-31&pay=all").get_data(as_text=True)
    assert "по ручному подсчёту 4 шт.: сверка" in shown
    audit = conn.execute("SELECT detail FROM audit WHERE action = 'point_adjust'").fetchone()[0]
    assert '"pieces": 4' in audit


# ---- 2. «Отмена» and «Невыкуп» -----------------------------------------------------------------------------------------------------------

def _mark(conn, mine):
    return {r["Реализация"] for r in sheet.rows_of(conn, mine, NOW).values()}


def test_the_mark_follows_whether_the_tyres_came(table):
    conn, box, fake = table
    before = placed(conn)                                                         # cancelled on the way: «Отмена»
    orders.move_card(conn, before, stage(conn, "cancel")["id"], NOW, "owner", {"cancel_reason": "отменил клиент"})
    assert _mark(conn, before) == {sheet.CANCELLED} == {"О"}
    came = placed(conn)                                                           # came, the client did not buy: «Невыкуп»
    for code in ("moving", "at_point"):
        orders.move_card(conn, came, stage(conn, code)["id"], NOW, "owner")
    orders.move_card(conn, came, stage(conn, "cancel")["id"], NOW, "owner", {"cancel_reason": "не выкупил"}, oth_split=False)
    assert _mark(conn, came) == {sheet.NOT_BOUGHT} == {"Н"}
    none = placed(conn)                                                           # «шины физически не приезжали»: «Отмена»
    for code in ("moving", "at_point"):
        orders.move_card(conn, none, stage(conn, code)["id"], NOW, "owner")
    orders.move_card(conn, none, stage(conn, "cancel")["id"], NOW, "owner", {"cancel_reason": "отменил клиент"}, oth_split=False)
    from tires_sync import point_arrival
    point_arrival.mark_none(conn, none, NOW, "owner")
    assert _mark(conn, none) == {"О"}
    oth = placed(conn)                                                            # on «На ОТХ»: «Невыкуп»
    for code in ("moving", "at_point", "oth"):
        orders.move_card(conn, oth, stage(conn, code)["id"], NOW, "owner", oth_split=False)
    assert _mark(conn, oth) == {"Н"}


def test_an_arrival_of_before_the_table_is_read_from_the_journal(table):
    conn, box, fake = table
    came = placed(conn)
    for code in ("moving", "at_point"):
        orders.move_card(conn, came, stage(conn, code)["id"], "2026-11-10T10:00:00", "owner")
    orders.move_card(conn, came, stage(conn, "cancel")["id"], "2026-11-12T10:00:00", "owner", {"cancel_reason": "не выкупил"}, oth_split=False)
    undone = placed(conn)
    for code in ("moving", "at_point", "moving", "cancel"):
        orders.move_card(conn, undone, stage(conn, code)["id"], "2026-11-10T10:00:00", "owner", {"cancel_reason": "отменил клиент"})
    conn.execute("DELETE FROM order_arrival")
    conn.commit()
    assert (_mark(conn, came), _mark(conn, undone)) == ({"Н"}, {"О"})
