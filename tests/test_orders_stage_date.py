"""«Заказы» (the owner 2026-11-04): the date next to the status, «На точке 04.11», on the list, the board and the card, and the tyres' sum under the
client's name on the card, in the old view and «Новый вид». Display only: an order with no moment of its stage shows the status alone."""
from __future__ import annotations

import re
from datetime import datetime

import pytest

from panel_helpers import login, make_app
from test_mypoint import manager
from tires_sync import order_points as points, orders, store, users

NOW = "2026-11-05T12:00:00"
ENTERED = "2026-11-04T23:30:00"                     # late in the evening, Moscow time: the day stays the 4th, no shift


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


@pytest.fixture(params=["old", "new"])
def w(tmp_path, request):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 11, 5, 9, 0, 0))
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    pid = points.save_point(conn, {"name": "Альфа", "manager_name": "А", "channel": "panel"})
    stages = {s["code"]: s for s in orders.list_stages(conn)}
    cid = orders.create_card(conn, {"client_name": "Анна", "point_id": str(pid)}, NOW, "o")
    conn.execute("UPDATE order_card SET stage_id = ?, entered_at = ? WHERE id = ?", (stages["at_point"]["id"], ENTERED, cid))
    store.set_state(conn, "ui_new.office", "all" if request.param == "new" else "off")
    conn.commit()
    client = app.test_client()
    login(client)
    return dict(app=app, conn=conn, client=client, cid=cid, pid=pid, stages=stages, name=stages["at_point"]["name"], view=request.param)


def page(w, url):
    r = w["client"].get(url)
    assert r.status_code == 200, url
    text = r.get_data(as_text=True)
    assert ("office2.css" in text) == (w["view"] == "new")           # the view the test is about
    return text


def entered(w, value):
    w["conn"].execute("UPDATE order_card SET entered_at = ? WHERE id = ?", (value, w["cid"]))
    w["conn"].commit()


def add_item(w):
    orders.add_item(w["conn"], w["cid"], goods_id="1", name="Nokian Hakka 10", brand_key="nokian", season="winter", diameter=16, territory="1", qty=4,
                    sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")
    w["conn"].commit()


def list_row(text, cid):
    return text[text.index('data-href="/orders/%d"' % cid):text.index("</tr>", text.index('data-href="/orders/%d"' % cid))]


def board_card(text, cid):
    return text[text.index('data-card="%d"' % cid):text.index("</a>", text.index('data-card="%d"' % cid))]


@pytest.mark.parametrize("value, day", [(ENTERED, "2026-11-04"), ("2026-11-04", "2026-11-04"), ("", ""), ("не дата", ""), (None, "")])
def test_the_day_of_the_stage_is_the_stored_moment_or_nothing(value, day):
    assert orders.stage_day(value) == day


def test_the_list_shows_the_date_next_to_the_status(w):
    assert "%s 04.11" % w["name"] in list_row(page(w, "/orders?v=list"), w["cid"])


def test_the_board_shows_the_date_on_the_order(w):
    card = board_card(page(w, "/orders"), w["cid"])
    assert re.search(r"data-stage-date>%s 04\.11</div>" % re.escape(w["name"]), card)


def test_the_card_shows_the_date_next_to_the_status(w):
    text = page(w, "/orders/%d" % w["cid"])
    assert re.search(r'<span class="badge badge--\w+">%s 04\.11</span>' % re.escape(w["name"]), text)
    if w["view"] == "new":
        assert re.search(r'class="o2c-stage"[^>]*>%s 04\.11 · 0 дн\.</span>' % re.escape(w["name"]), text)


def test_a_date_of_another_year_names_the_year(w):
    entered(w, "2025-12-31T10:00:00")
    assert "%s 31.12.2025" % w["name"] in list_row(page(w, "/orders?v=list"), w["cid"])
    assert "%s 31.12.2025" % w["name"] in board_card(page(w, "/orders"), w["cid"])
    assert "%s 31.12.2025</span>" % w["name"] in page(w, "/orders/%d" % w["cid"])


@pytest.mark.parametrize("value", ["", "не дата"])
def test_an_order_with_no_moment_shows_the_status_without_a_date(w, value):
    entered(w, value)
    row = list_row(page(w, "/orders?v=list"), w["cid"])
    assert re.search(r">%s</span>" % re.escape(w["name"]), row)
    assert "data-stage-date" not in board_card(page(w, "/orders"), w["cid"])
    text = page(w, "/orders/%d" % w["cid"])
    assert re.search(r'<span class="badge badge--\w+">%s</span>' % re.escape(w["name"]), text)
    if w["view"] == "new":
        assert re.search(r'class="o2c-stage"[^>]*>%s · 0 дн\.</span>' % re.escape(w["name"]), text)


def test_the_card_shows_the_tyres_sum_under_the_clients_name_the_same_as_in_the_money_part(w):
    add_item(w)
    text = page(w, "/orders/%d" % w["cid"]).replace("\xa0", " ")
    head = re.search(r'id="ow-title"[^>]*>Анна</h1><div class="entity-under">Итого <b data-order-sum>([^<]+)</b></div>', text)
    assert head and head.group(1).startswith("20 000")
    assert '<p class="ocard-sum">Итого <b>%s</b>' % head.group(1) in text
    assert "Итого: <b>%s</b>" % head.group(1) in text                 # the line under the positions: the same number, the same word
    assert "Сумма шин" not in text and "Сумма заказа" not in text      # one word for the one sum (the owner 2026-11-06)


def test_an_order_with_no_positions_has_no_sum_under_the_name(w):
    text = page(w, "/orders/%d" % w["cid"])
    assert "data-order-sum" not in text and "entity-under" not in text
    assert '<p class="ocard-sum">Итого <b>' in text                  # the money part is as it was, «Итого» (2026-11-06)


def test_the_new_order_window_has_no_sum_and_no_date(w):
    r = w["client"].get("/orders/new")
    assert r.status_code == 200
    assert "data-order-sum" not in r.get_data(as_text=True)


def test_nothing_of_the_order_changes(w):
    add_item(w)
    before = orders.get_card(w["conn"], w["cid"])
    page(w, "/orders/%d" % w["cid"])
    page(w, "/orders?v=list")
    page(w, "/orders")
    assert orders.get_card(w["conn"], w["cid"]) == before


def test_a_points_phone_list_and_board_name_the_real_stage_with_its_date(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 11, 5, 9, 0, 0))
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    pid = points.save_point(conn, {"name": "Альфа", "manager_name": "А", "channel": "panel"})
    stages = {s["code"]: s for s in orders.list_stages(conn)}
    own = orders.create_card(conn, {"client_name": "Свой", "point_id": str(pid)}, NOW, "o")
    other = orders.create_card(conn, {"client_name": "Без даты", "point_id": str(pid)}, NOW, "o")
    conn.execute("UPDATE order_card SET stage_id = ?, deal_kind = 'stock', entered_at = ? WHERE id = ?", (stages["talk"]["id"], ENTERED, own))
    conn.execute("UPDATE order_card SET stage_id = ?, deal_kind = 'stock', entered_at = '' WHERE id = ?", (stages["new"]["id"], other))
    conn.commit()
    text = manager(app, conn, pid).get("/orders").get_data(as_text=True)
    phone = text[text.index("data-point-orders"):text.index('class="pt-desk"')]
    assert re.search(r'data-real-stage>%s</span><span class="mp-sub" data-stage-date>04\.11 · 0 дн\.' % stages["talk"]["name"], phone)   # next to the badge of the real stage
    assert re.search(r'data-pcard="%d".*?data-stage-date>\s*0 дн\.' % other, phone, re.S)         # no moment: the days alone, as before
    desk = board_card(text[text.index('class="pt-desk"'):], own)
    assert "data-stage-date>%s 04.11" % stages["talk"]["name"] in desk
    assert 'data-order-sum' not in text
