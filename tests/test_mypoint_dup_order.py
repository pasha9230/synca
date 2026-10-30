"""«Рабочее место»: a second order for a phone that has an open order is refused with that order named (the owner 2026-10-27; it was a warning
with «Всё равно создать» since 2026-10-25)."""
from __future__ import annotations

from panel_helpers import post
from test_mypoint import handed, handoff_token, manager
from test_vk_commands import NOW, _card, _stage, ready
from tires_sync import order_points as points, orders

PHONE = "8 (900) 111-22-33"                      # the card of ready() has «+7 900 111 22 33»: the same number typed otherwise


def send(client, phone, need="", tok_=None, force=False, p=None):
    page = client.get("/my-point/handed").get_data(as_text=True)
    data = dict(token=tok_ or handoff_token(page), phone=phone, need=need)
    if force:
        data["force"] = force if isinstance(force, str) else "+79001112233"
    if p:
        data["p"] = str(p)
    return data["token"], post(client, "/my-point", "tok", **data)


def test_an_open_order_of_the_phone_refuses_and_nothing_is_made(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    _t, r = send(mine, PHONE, "второй комплект 205/55 R16")
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and 'id="mp-dup"' in page
    assert "У клиента уже есть открытый заказ № 5000100 от 13.10 — откройте его." in page and 'href="/my-point?tab=moving#order-%d"' % card in page          # its card on its tab
    assert "второй комплект 205/55 R16</textarea>" in page and 'value="%s"' % PHONE in page          # the form is not lost
    assert handed(conn) == [] and conn.execute("SELECT COUNT(*) FROM order_command_log").fetchone()[0] == 0


def test_the_force_of_an_old_page_makes_no_order(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    first, _r = send(mine, PHONE, "другой размер")
    _t, r = send(mine, PHONE, "другой размер", tok_=first, force=True)
    assert r.status_code == 200 and 'id="mp-dup"' in r.get_data(as_text=True) and handed(conn) == []


def test_a_stale_second_tab_is_refused_when_the_first_tab_made_the_order(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    stale = handoff_token(mine.get("/my-point/handed").get_data(as_text=True))
    send(mine, "+7 900 222-33-44", "зима")                                                    # the first tab hands the client over
    _t, r = send(mine, "8 900 222 33 44", "ещё", tok_=stale)
    assert r.status_code == 200 and "уже есть открытый заказ" in r.get_data(as_text=True) and len(handed(conn)) == 1


def test_a_double_press_of_the_same_order_says_passed_not_warning(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    first, _r = send(mine, "+7 900 222-33-44", "зима")
    _t, r = send(mine, "+7 900 222-33-44", "зима", tok_=first)
    assert r.status_code == 302 and "уже передан" in mine.get(r.headers["Location"]).get_data(as_text=True)


def test_a_closed_order_and_another_phone_do_not_warn(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    _t, r = send(mine, "+7 900 555-66-77", "зима")
    assert r.status_code == 302
    orders.move_card(conn, card, _stage(conn, "done"), NOW, "o")
    conn.commit()
    assert conn.execute("SELECT s.kind FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE c.id = ?", (card,)).fetchone()[0] != "open"
    _t, r = send(mine, PHONE, "зима")
    assert r.status_code == 302


def test_an_open_order_of_another_point_does_not_stop_the_handoff(tmp_path):
    """The rule stays inside the point (the lead 2026-10-27): the client's order at another point is that point's business."""
    app, client, tok, conn, pid, card = ready(tmp_path)
    other = points.save_point(conn, dict(name="Секретная точка", channel="vk", vk_peer_id="777"))
    conn.execute("UPDATE order_card SET point_id = ? WHERE id = ?", (other, card))
    conn.commit()
    mine = manager(app, conn, pid)
    _t, r = send(mine, PHONE, "зима")
    assert r.status_code == 302 and len(handed(conn)) == 1


def test_an_open_order_of_no_point_is_named_by_number_and_date_only_and_no_link(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    conn.execute("UPDATE order_card SET point_id = NULL WHERE id = ?", (card,))
    conn.commit()
    mine = manager(app, conn, pid)
    page = send(mine, PHONE)[1].get_data(as_text=True)
    assert "уже есть открытый заказ № 5000100" in page and "Татьяна" not in page
    assert "data-dup-open" not in page and "data-dup-force" not in page and handed(conn) == []


def test_the_owner_gets_a_link_to_the_order_card(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    page = send(client, PHONE, p=pid)[1].get_data(as_text=True)
    assert 'href="/orders/%d"' % card in page


def test_open_card_by_phone_reads_only_open_orders_by_the_normalized_phone(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    assert orders.open_card_by_phone(conn, "89001112233")["id"] == card
    assert orders.open_card_by_phone(conn, "") is None and orders.open_card_by_phone(conn, "123") is None
    assert orders.open_card_by_phone(conn, "+7 900 000-00-00") is None


def test_the_refusal_has_no_button_around_it_and_another_phone_still_goes(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    page = send(mine, PHONE)[1].get_data(as_text=True)
    assert 'name="force"' not in page and "Всё равно создать" not in page
    send(mine, "+7 900 111-22-34", "x", force="+79001112233")                              # no open order there: made, whatever the old field says
    assert len(handed(conn)) == 1
    _t, r = send(mine, PHONE, "x", force="+79990000000")
    assert r.status_code == 200 and 'id="mp-dup"' in r.get_data(as_text=True)
