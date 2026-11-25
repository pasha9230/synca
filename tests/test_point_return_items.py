"""ТЗ 84 (2026-11-25, «раздел на возврат не содержит данных по позициям в панели точек»): on «На возврат» of the point every record shows its
tyres — the supplier's name (brand, model, size as the supplier writes them), the code and «Кол-во: N шт.» — in the old and the new view, as
production runs (OTH_SPLIT: an order put on «На ОТХ» becomes a record of storage): agreed while on «На ОТХ», agreed on its own stage
«На возврат», and one position of several pieces returned on its own (its own order with its pieces)."""
from __future__ import annotations

import re
from datetime import date

import pytest

from test_mypoint import manager
from test_point_oth_sell import setup
from test_point_return_tabs import to_return_stage
from tires_sync import order_oth, orders

OTH_SPLIT = True
TYRE = "Nokian Hakkapeliitta 10"


def record(conn) -> int:
    """The record of storage the order became on «На ОТХ»."""
    return conn.execute("SELECT c.id FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE s.code = 'oth' ORDER BY c.id").fetchone()[0]


def plaque(page, card) -> str:
    found = re.search(r'<article class="mp-return-row" data-return="%d">(.*?)</article>' % card, page, re.S)
    assert found, "no record of the order %d on «На возврат»" % card
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", found.group(1)))


@pytest.mark.parametrize("view", ["old", "new"])
@pytest.mark.parametrize("how", ["on_oth", "own_stage"])
def test_every_return_shows_its_tyres_with_the_pieces(tmp_path, view, how):
    app, owner, conn, pid, card = setup(tmp_path, view)
    rec = record(conn)
    if how == "own_stage":
        to_return_stage(conn, rec)
    order_oth.agree(conn, orders.get_card(conn, rec), date.fromisoformat("2026-10-14"), "2026-10-14T10:00:00", "owner", "2026-10-16")
    conn.commit()
    page = manager(app, conn, pid).get("/my-point?tab=return").get_data(as_text=True)
    text = plaque(page, rec)
    assert "%s Код 1 Кол-во: 4 шт." % TYRE in text and "шины не указаны" not in text
    assert page.count("data-return-item") == 1


@pytest.mark.parametrize("view", ["old", "new"])
def test_one_position_returned_on_its_own_shows_its_own_pieces(tmp_path, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    rec = record(conn)
    item = conn.execute("SELECT id FROM order_item WHERE order_id = ?", (rec,)).fetchone()[0]
    order_oth.item_to_return(conn, item, "2026-10-14T10:00:00", "owner", 2)                       # 2 of 4 go back
    order_oth.agree(conn, orders.get_card(conn, rec), date.fromisoformat("2026-10-14"), "2026-10-14T10:00:00", "owner", "2026-10-16")
    conn.commit()
    back = [c for (c,) in conn.execute("SELECT c.id FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE s.code = ?", (orders.RETURN_GIVEN,))]
    assert len(back) == 1
    mine = manager(app, conn, pid)
    assert "%s Код 1 Кол-во: 2 шт." % TYRE in plaque(mine.get("/my-point?tab=return").get_data(as_text=True), back[0])
    oth = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", mine.get("/my-point?tab=oth").get_data(as_text=True)))
    assert "%s Кол-во: 2 шт." % TYRE in oth                                                        # the rest stays on «На ОТХ» with its pieces
