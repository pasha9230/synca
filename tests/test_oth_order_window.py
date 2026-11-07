"""The window of an order on «На ОТХ» (the owner 2026-11-07, «А»), in the old view and «Новый вид»: no «Автосообщение клиенту» (no client on
«На ОТХ»); the title «№<MIM> · <transfer> от <invoice>» without the inner number; on top of the return one row «№<MIM> · <transfer> от <invoice> ·
На ОТХ с <day> · резерв до <day>», the buttons of the return under it as before; no row «Товар на точке · Крайняя дата резерва», no table
«Заказы WebMIM» of one part (several: the table stays). Other stages and the point are not changed."""
from __future__ import annotations

import re

import pytest

from test_mypoint import manager
from test_oth_board_card import TYRE, cheap_hashes, order, w  # noqa: F401  (the fixtures)
from tires_sync import orders

HEAD = "№5316244 · 26R-1506015 от 03.11.2026"
LINE = HEAD + " · На ОТХ с 07.11 · резерв до 07.11"


def window(w, cid, client=None):
    r = (client or w["client"]).get("/orders/%d" % cid)
    assert r.status_code == 200
    text = r.get_data(as_text=True)
    if client is None:
        assert ("office2.css" in text) == (w["view"] == "new")
    return text


def plain(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


def title(w, text):
    if w["view"] == "new":
        return text[text.index('<div class="o2c-title">'):text.index("</div>", text.index('<div class="o2c-title">'))]
    found = re.search(r'<p class="strong" data-oth-title>.*?</p>', text, re.S)
    return found.group(0) if found else ""


def part(kind, number):
    return dict(kind=kind, label="Заказ " + number, number=number, transfer="26R-" + number, day="2026-11-03", items=[dict(id=1, name=TYRE, qty=4)],
                highlight=False, typed=False, needs_numbers=False)


def test_the_window_on_oth_has_no_autosend(w):
    cid = order(w)
    assert 'id="autosend"' not in window(w, cid)
    other = order(w, code="at_point", webmim="5316245")
    if w["view"] == "new":          # «Новый вид»: the block is in the column «Чат Авито», with a bound chat only (the owner 2026-11-07, ui-audit 6)
        assert 'id="autosend"' not in window(w, other) and "data-autosend-nochat" in window(w, other)
    else:
        assert 'id="autosend"' in window(w, other)                               # other stages: the block stays


def test_the_title_is_as_on_the_board_without_the_inner_number(w):
    cid = order(w)
    head = title(w, window(w, cid))
    assert plain(head) == HEAD
    assert "внутр." not in head and "№%d" % cid not in head and "Иван" not in head


def test_one_row_on_top_of_the_return_and_the_buttons_under_it(w):
    cid = order(w)
    text = window(w, cid)
    assert plain(text).count(LINE) == 1
    row = text.index("data-oth-head")
    assert row < text.index("data-to-return") and row < text.index('action="/orders/%d/oth-agreed"' % cid)
    assert text.index("data-to-return") < text.index('action="/orders/%d/oth-agreed"' % cid)
    assert "Товар на точке:" not in text and "Крайняя дата резерва" not in text


@pytest.mark.parametrize("transfer, invoice, reserve, want", [
    ("", "2026-11-03T09:00:00", "2026-11-07", "№5316244 · счёт от 03.11.2026 · На ОТХ с 07.11 · резерв до 07.11"),
    ("26R-1506015", "", "", "№5316244 · 26R-1506015 · На ОТХ с 07.11"),
])
def test_what_is_missing_leaves_no_empty_piece(w, transfer, invoice, reserve, want):
    cid = order(w, transfer=transfer, invoice=invoice, reserve=reserve)
    text = window(w, cid)
    head = text[text.index("data-oth-head"):]
    assert plain(head[head.index(">") + 1:head.index("</p>")]) == want


def test_the_table_of_webmim_shows_only_for_several_parts(w):
    cid = order(w)
    assert "Заказы WebMIM" not in window(w, cid)
    w["app"].jinja_env.globals["webmim_parts_for"] = lambda card_id: [part("own", "5316244")]
    assert "Заказы WebMIM" not in window(w, cid)
    w["app"].jinja_env.globals["webmim_parts_for"] = lambda card_id: [part("own", "5316244"), part("extra", "5316300")]
    text = window(w, cid)
    assert "Заказы WebMIM" in text and "5316300" in text
    other = order(w, code="at_point", webmim="5316245")
    w["app"].jinja_env.globals["webmim_parts_for"] = lambda card_id: [part("own", "5316245")]
    assert "Заказы WebMIM" in window(w, other)                                   # other stages: as before


def test_other_stages_keep_their_title(w):
    cid = order(w, code="at_point")
    text = window(w, cid)
    assert "data-oth-head" not in text
    if w["view"] == "new":
        assert "внутр. № %d" % cid in title(w, text)


def test_the_point_sees_no_webmim_nor_transfer_in_the_window(w):
    cid = order(w, webmim="", deal="stock", goods=orders.POINT_GOODS)
    text = window(w, cid, manager(w["app"], w["conn"], w["pid"]))
    assert "без № MIM" not in text and "data-oth-head" not in text and "data-oth-title" not in text
    assert text.count("26R-1506015") == text.count('name="transfer_number" value="26R-1506015"') <= 1     # only the point's own field «Номер перемещения», as before
