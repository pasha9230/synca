"""The card of an order on «На ОТХ» on the board «Заказы» (the owner 2026-11-07), in the old view and «Новый вид»: «№<WebMIM>», «<transfer> от
<invoice>», «На ОТХ с <day>», «Резерв до <day>», the point, every tyre and «Кол-во: N» — no inner number of the panel, no «На точке», no «× N».
Display only; the bottom row (sum, tags, days) stays; other stages and the point's board are not changed.
«Новый вид» since the prototype of the audit 08.11 (audit 40, the owner 08.11): the card of «На ОТХ» is as every other card — № WebMIM, the
tyres, «4 шт.», the sum, the point, the days; no inner number."""
from __future__ import annotations

import re
from datetime import datetime

import pytest

from panel_helpers import login, make_app
from test_mypoint import manager
from tires_sync import order_points as points, orders, store, users

NOW = "2026-11-07T12:00:00"
TYRE = "Nexen 275/55/20 T 113 WINGUARD WINSPIKE 3 Ш."


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


@pytest.fixture(params=["old", "new"])
def w(tmp_path, request):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 11, 7, 9, 0, 0))
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    pid = points.save_point(conn, {"name": "Юнона", "manager_name": "А", "channel": "panel"})
    stages = {s["code"]: s for s in orders.list_stages(conn)}
    store.set_state(conn, "ui_new.office", "all" if request.param == "new" else "off")
    conn.commit()
    client = app.test_client()
    login(client)
    return dict(app=app, conn=conn, client=client, pid=pid, stages=stages, view=request.param)


def order(w, code="oth", webmim="5316244", transfer="26R-1506015", invoice="2026-11-03T09:00:00", reserve="2026-11-07", items=((TYRE, 4),), deal="", goods="1"):
    conn = w["conn"]
    cid = orders.create_card(conn, {"client_name": "Иван Клиентов", "point_id": str(w["pid"])}, NOW, "o")
    if deal:
        conn.execute("UPDATE order_card SET deal_kind = ? WHERE id = ?", (deal, cid))
    for name, qty in items:
        orders.add_item(conn, cid, goods_id=goods, name=name, brand_key="nexen", season="winter", diameter=20, territory="1", qty=qty,
                        sale_unit=15300, supplier_price=12000, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")
    conn.execute("UPDATE order_card SET stage_id = ?, entered_at = '2026-11-07T10:00:00', webmim_number = ?, transfer_number = ?, reserve_until = ? WHERE id = ?",
                 (w["stages"][code]["id"], webmim, transfer, reserve, cid))
    if invoice:
        conn.execute("INSERT INTO webmim_create (order_id, server, state, first_at, started_at, updated_at, author) VALUES (?, 'x', 'done', ?, ?, ?, 'o')",
                     (cid, NOW, NOW, invoice))
    conn.commit()
    return cid


def board(w, client=None):
    r = (client or w["client"]).get("/orders")
    assert r.status_code == 200
    text = r.get_data(as_text=True)
    if client is None:
        assert ("office2.css" in text) == (w["view"] == "new")
    return text


def card_html(text, cid):
    start = text.index('data-card="%d"' % cid)
    return text[start:text.index("</a>", start)]


def lines(text, cid):
    """The visible rows of the card above its bottom row (sum, tags, days)."""
    html = card_html(text, cid)
    html = re.split(r'<div class="o2-foot"|<div class="row" style=', html)[0]
    html = html[html.index(">") + 1:]
    rows = [re.sub(r"\s+", " ", x).strip() for x in re.sub(r"<[^>]+>", "\n", re.sub(r"</div>|<br>", "\n\n", html)).split("\n\n")]
    return [re.sub(r"\s*\n\s*", " ", x).strip() for x in rows if x.strip()]


NEW = ["№ 5316244", TYRE, "4 шт.", "61 200 ₽", "Юнона", "0 дн."]


def test_the_card_on_oth_has_exactly_the_owners_rows(w):
    cid = order(w)
    text = board(w)
    if w["view"] == "new":                                       # as every other card (audit 40)
        assert lines(text, cid) == NEW
        card = card_html(text, cid)
        assert "внутр." not in card and "№ %d" % cid not in card and "Иван" not in card and "Кол-во" not in card
        return
    assert lines(text, cid) == ["№5316244", "26R-1506015 от 03.11.2026", "На ОТХ с 07.11", "Резерв до 07.11", "Юнона", TYRE, "Кол-во: 4"]
    card = card_html(text, cid)
    assert "№%d" % cid not in card and "№ %d" % cid not in card and "внутр." not in card       # no inner number of the panel
    assert "× 4" not in card and "На точке" not in card and "Иван" not in card
    assert "61\u00a0200" in card and "дн." in card                                                       # the bottom row stays


def test_every_position_has_its_name_and_its_count(w):
    cid = order(w, items=((TYRE, 4), ("Ikon Nordman 7 205/55 R16", 2)))
    if w["view"] == "new":                                       # two positions: «2 позиции · 6 шт.» (the prototype of the audit)
        assert lines(board(w), cid)[:2] == ["№ 5316244", "2 позиции · 6 шт."]
        return
    assert lines(board(w), cid)[-4:] == [TYRE, "Кол-во: 4", "Ikon Nordman 7 205/55 R16", "Кол-во: 2"]


@pytest.mark.parametrize("transfer, invoice, reserve, want", [
    ("", "2026-11-03T09:00:00", "2026-11-07", ["№5316244", "Счёт от 03.11.2026", "На ОТХ с 07.11", "Резерв до 07.11", "Юнона"]),
    ("26R-1506015", "", "2026-11-07", ["№5316244", "26R-1506015", "На ОТХ с 07.11", "Резерв до 07.11", "Юнона"]),
    ("", "", "", ["№5316244", "На ОТХ с 07.11", "Юнона"]),
])
def test_what_is_missing_leaves_no_empty_row(w, transfer, invoice, reserve, want):
    cid = order(w, transfer=transfer, invoice=invoice, reserve=reserve)
    got = lines(board(w), cid)
    if w["view"] == "new":                                       # no rows of the transfer and the reserve at all (audit 40)
        assert got == NEW
        return
    assert got == want + [TYRE, "Кол-во: 4"]
    assert not any(x.startswith("от") or x.endswith("от") or x == "Резерв до" for x in got)


def test_other_stages_keep_their_card(w):
    cid = order(w, code="at_point")
    card = card_html(board(w), cid)
    if w["view"] == "new":                                       # the card of the prototype: no client, no inner number (the owner 08.11)
        assert lines(board(w), cid) == NEW and "внутр." not in card
        return
    assert "Иван Клиентов" in card and "× 4" in card and "Кол-во" not in card and "data-oth-transfer" not in card
    assert ("внутр. № %d" % cid in card) if w["view"] == "new" else ("№%d Иван Клиентов" % cid in card)


def test_the_point_still_sees_no_webmim_nor_transfer(w):
    cid = order(w, webmim="", deal="stock", goods=orders.POINT_GOODS)                      # the point's own sale (no WebMIM) that went to «На ОТХ»
    assert cid in points.own_sales(w["conn"], w["pid"])
    text = board(w, manager(w["app"], w["conn"], w["pid"]))
    card = card_html(text, cid)
    assert "26R-1506015" not in card and "без № MIM" not in card and "data-oth-transfer" not in card and "Кол-во" not in card
    assert "На ОТХ" in card
