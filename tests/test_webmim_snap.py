"""Снимок количества, когда номер WebMIM вписан автоматически (ответ владельца «А» 07.11, ветка lane/webmim-snap): «Это он» (webmim_match.link)
и «Заказ есть: да, это наш заказ» (webmim_create.resolve) сразу берут снимок по позициям поставщика (order_webmim.snapshot, order_mim_base),
как открытие карточки в офисе и «Оформить» / «Досоздать». Шины, добавленные после, — вторая часть, даже если карточку между этим не открывали.
Уже снятый снимок не перезаписывается; номер, вписанный человеком в карточке, — как раньше (снимок при открытии карточки); правило
списания шин между частями не меняется. Реальных заказов WebMIM нет."""
from __future__ import annotations

from tests import test_webmim_create as wc
from tests import test_webmim_match as wm
from tires_sync import order_webmim, orders, webmim_create, webmim_match

AT = "2026-10-20T15:00:00"


def base(conn, cid):
    return {item: qty for item, qty in conn.execute("SELECT item_id, qty FROM order_mim_base WHERE order_id = ?", (cid,))}


def parts_unseen(conn, cid):
    """The block as it is, the card not looked at (no snapshot taken by the look)."""
    return [(p["kind"], [x["qty"] for x in p["items"]]) for p in order_webmim.parts(conn, orders.get_card(conn, cid), take_snapshot=False, now=AT)]


def test_this_is_it_takes_the_snapshot_so_pieces_added_later_are_a_second_part(tmp_path):
    conn = wm.db(tmp_path)
    wm.snap(conn, (wm.wm(51, 951, "19.10.26 10:00:00", phone="89111112233"), [("A", 4, 4000)]))
    cid = wm.card(conn, at="2026-10-19T09:00:00", goods=[("A", 4)])
    item = orders.list_items(conn, cid)[0]["id"]
    webmim_match.link(conn, cid, "51", AT, "manager1")
    assert base(conn, cid) == {0: 0, item: 4}                                       # taken at once, the card never opened
    orders.update_item(conn, item, 6, 5000, AT, "owner")                            # two more of the same tyre afterwards
    assert parts_unseen(conn, cid) == [("supplier", [4]), ("extra", [2])]


def test_this_is_it_never_overwrites_a_snapshot_taken_before(tmp_path):
    conn = wm.db(tmp_path)
    wm.snap(conn, (wm.wm(51, 951, "19.10.26 10:00:00", phone="89111112233"), [("A", 4, 4000)]))
    cid = wm.card(conn, at="2026-10-19T09:00:00", goods=[("A", 4)])
    item = orders.list_items(conn, cid)[0]["id"]
    orders.update_card(conn, cid, {"webmim_number": "777"}, AT, "owner")           # a number typed once, the card looked at
    order_webmim.parts(conn, orders.get_card(conn, cid), take_snapshot=True, now=AT)
    orders.update_card(conn, cid, {"webmim_number": ""}, AT, "owner")              # cleared again
    orders.update_item(conn, item, 6, 5000, AT, "owner")
    webmim_match.link(conn, cid, "51", AT, "manager1")
    assert base(conn, cid) == {0: 0, item: 4}                                       # the old snapshot stays
    assert conn.execute("SELECT number FROM order_mim_base WHERE order_id = ? AND item_id = 0", (cid,)).fetchone()[0] == "951"


def test_a_found_order_takes_the_snapshot_so_pieces_added_later_are_a_second_part(tmp_path):
    conn = wc.db(tmp_path)
    cid = wc.card(conn)
    fake = wc.FakeWebmim()
    fake.fail[("POST", "/api/v2/orders")] = ["made-then-timeout"]
    wc.place(conn, cid, fake)                                                       # the answer lost: the button shut, no number on the card
    assert base(conn, cid) == {}
    seen = webmim_create.check(conn, cid, wc.client_of(fake), wc.NOW, webmim_id="1001", sleep=lambda _s: None)
    webmim_create.resolve(conn, cid, True, wc.NOW, "owner", webmim_id="1001", client=wc.client_of(fake), confirm=seen["token"],
                          sleep=lambda _s: None)
    held = {i["id"]: int(i["qty"]) for i in orders.list_items(conn, cid)}
    assert base(conn, cid) == {0: 0, **held}                                         # taken at once
    first = orders.list_items(conn, cid)[0]
    orders.update_item(conn, first["id"], int(first["qty"]) + 1, 5000, wc.NOW, "owner")
    assert ("extra", [1]) in parts_unseen(conn, cid)


def test_a_number_typed_by_a_person_still_waits_for_the_card(tmp_path):
    """The rule of lane order-webmim-list stays: the number typed in the card with the pieces is no snapshot until the card is looked at."""
    conn = wm.db(tmp_path)
    cid = wm.card(conn, at="2026-10-19T09:00:00", goods=[("A", 4)])
    orders.update_card(conn, cid, {"webmim_number": "777"}, AT, "owner")
    assert base(conn, cid) == {}
