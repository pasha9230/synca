"""«Выдал» from «Заказ оформлен» and «Перемещение на точку» (the owner 2026-10-21): the order closes as an ordinary issue (the point's report
counts it), the journal says it went past «Товар на точке», the client gets nothing, a repeat closes nothing twice. From «Новый заказ» it
stays «спорно»."""
from __future__ import annotations

import pytest

from panel_helpers import post
from test_mypoint import manager, press
from test_vk_commands import _card, _stage, hook, ready, to_client, to_points
from tires_sync import order_manager as mgr, order_report, orders

PAST = "минуя «Товар на точке»"


def _past(conn, card):
    return [e["text"] for e in orders.list_events(conn, card) if PAST in e["text"]]


@pytest.mark.parametrize("code", ["placed", "moving"])
def test_issued_by_vk_from_placed_or_moving_closes_once_and_tells_the_client_nothing(tmp_path, code):
    app, client, tok, conn, pid, card = ready(tmp_path)
    if code == "placed":
        conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (_stage(conn, "placed"), card))
        conn.commit()
    hook(app, conn, "выдал 5000100", event="i1")
    hook(app, conn, "забрали 5000100", event="i2")
    got = orders.get_card(conn, card)
    assert got["stage"]["code"] == "done" and to_client(app) == []
    assert len(_past(conn, card)) == 1
    assert [e["text"] for e in orders.list_events(conn, card)].count("Этап: «%s» → «%s»" % (orders.get_stage(conn, _stage(conn, code))["name"], got["stage"]["name"])) == 1
    answers = [a for _, a in to_points(app)]
    assert "отмечено: выдан" in answers[0] and "уже отмечен" in answers[1]


def test_issued_from_at_point_writes_no_past_note(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    orders.move_card(conn, card, _stage(conn, "at_point"), "2026-10-13T12:00:00", "o")
    hook(app, conn, "выдал 5000100")
    assert orders.get_card(conn, card)["stage"]["code"] == "done" and _past(conn, card) == []


def test_issued_of_a_new_order_is_still_doubtful(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    fresh = orders.create_card(conn, dict(client_name="Пётр", point_id=pid, webmim_number="600600"), "2026-10-13T12:00:00", "o")
    hook(app, conn, "выдал 600600")
    assert orders.get_card(conn, fresh)["stage"]["code"] == "new"
    assert mgr.list_inbox(conn)[0]["dispute"].startswith("слово «выдал» не подходит")


def test_the_owner_confirms_a_doubtful_issued_of_a_moving_order(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    hook(app, conn, "выдал")                                                                   # no number: doubtful
    item = mgr.list_inbox(conn)[0]
    post(client, "/orders/inbox/%d/confirm" % item["id"], tok, order_no="5000100")
    assert orders.get_card(conn, card)["stage"]["code"] == "done" and len(_past(conn, card)) == 1 and to_client(app) == []


def test_my_point_shows_issued_for_an_order_on_its_way_and_closes_it(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    page = mine.get("/my-point?tab=moving").get_data(as_text=True)                     # «Едут» (the owner 2026-10-30: no «Все»)
    assert "/my-point/%d/arrived" % card in page and "/my-point/%d/issued" % card in page
    assert "/my-point/%d/no_show" % card not in page                                        # «не пришёл» only at the point
    _t, page = press(mine, card, "issued")
    assert orders.get_card(conn, card)["stage"]["code"] == "done" and len(_past(conn, card)) == 1 and to_client(app) == []
    assert "отмечено: выдан" in page


def test_the_report_of_the_points_counts_an_issue_past_the_point(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    hook(app, conn, "выдал 5000100")
    orders.record_sales(conn, card, "2026-10-13", "2026-10-13T12:00:00", "o", "2026-10-13")              # «Продажа» of a closed order, as after any issue
    report = order_report.build(conn, "2026-01-01", "2030-12-31")
    [line] = report["lines"]
    assert line["orders"] == 1 and line["fee"] == order_report.POINT_FEE
