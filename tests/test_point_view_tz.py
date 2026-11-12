"""The owner 2026-11-12 (the task of the points' «Рабочее место», part one: the look only, no rule of the orders changes):
1. «Рабочее место» opens on «Едут»; a tab named in the address still opens, and a press returns to the tab it came from;
2. the texts under the tabs are the owner's new ones (word for word: test_point_return_tabs.NOTES);
3. «На возврат»: «Вернуть дд.мм.гггг» by the number of an order agreed for the return; «Скачать акт МХ-3» and «Распечатать» (the same PDF
   shown in the browser, the same rights) while the act is attached; one act of several orders: the same mark «1 Акт на несколько заказов»
   of one colour by each of them, another act another colour, an act of one order no mark."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from test_mypoint import manager
from test_point_oth_sell import setup
from test_point_return_tabs import to_return_stage
from test_return_act import DAY, NOW, PDF, PDF2, agreed, clock  # noqa: F401  (the fixture)
from test_return_day import second
from test_vk_commands import _card
from tires_sync import order_oth, order_points as points, orders, oth_return_act as acts
from tires_sync.panel import routes_mypoint

LATER = "2026-11-16"
MARK = "1 Акт на несколько заказов"


@pytest.fixture(params=["old", "new"])
def view(request):
    return request.param


def page_of(client, tab=None, p=None):
    args = [a for a in ("tab=%s" % tab if tab else "", "p=%d" % p if p else "") if a]
    return client.get("/my-point" + ("?" + "&".join(args) if args else "")).get_data(as_text=True)


def active(page) -> str:
    return re.search(r'class="mp-tab is-active[^"]*"[^>]*data-tab="(\w+)"', page).group(1)


def plaque(page, card) -> str:
    """The plaque of an order agreed for the return (above the tabs)."""
    return re.search(r'<article[^>]*data-return="%d".*?</article>' % card, page, re.S).group(0)


def marks(text) -> list:
    """[(the colour, the act)] of the marks «1 Акт на несколько заказов» of a piece of the page."""
    return re.findall(r'class="mp-act-mark mp-act-mark--(\d)" data-act-mark="(\d+)">%s<' % MARK, text)


# 1. the tab the page opens on

def test_the_workplace_opens_on_edut(tmp_path, clock, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    mine = manager(app, conn, pid)
    assert routes_mypoint.DEFAULT_TAB == "moving"
    assert active(page_of(mine)) == "moving"
    assert active(page_of(mine, "oth")) == "oth" and active(page_of(mine)) == "moving"       # the tab looked at before is not remembered
    assert active(page_of(mine, "nope")) == "moving"
    assert active(page_of(owner, p=pid)) == "moving"


@pytest.mark.parametrize("key", ["here", "oth", "agree", "return", "issued", "moving"])
def test_a_tab_named_in_the_address_still_opens(tmp_path, clock, view, key):
    app, owner, conn, pid, card = setup(tmp_path, view)
    assert active(page_of(manager(app, conn, pid), key)) == key


def test_a_press_returns_to_the_tab_it_came_from(tmp_path, clock):
    app, owner, conn, pid, card = setup(tmp_path)
    mine = manager(app, conn, pid)

    def back(came):                                                               # a press the server refuses (a bad token): only the way back
        r = mine.post("/my-point/%d/oth_here" % card, data={"csrf": "tok", "token": "x"}, headers={"Referer": "http://localhost" + came})
        assert r.status_code == 302
        return r.headers["Location"]

    assert back("/my-point?tab=here").endswith("/my-point?tab=here")              # «На точке» is no longer the page without a tab
    assert back("/my-point?tab=oth").endswith("/my-point?tab=oth")
    assert back("/my-point").endswith("/my-point") and back("/my-point?tab=moving").endswith("/my-point")


def test_the_links_of_the_page_keep_the_chosen_tab(tmp_path, clock, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    mine = manager(app, conn, pid)
    here, moving = page_of(mine, "here"), page_of(mine)
    assert '<input type="hidden" name="tab" value="here">' in here                # the search stays on «На точке»
    assert 'name="tab"' not in moving
    back = re.search(r'name="back" value="([^"]*)"', here).group(1)
    assert "tab=here" in back and "tab=" not in re.search(r'name="back" value="([^"]*)"', moving).group(1)


def test_the_sound_of_an_order_at_the_point_leads_to_its_tab(tmp_path, clock):
    """An order waiting at the point is on «На точке», no longer the tab the page opens on: its links name the tab."""
    app, owner, conn, pid, card = setup(tmp_path)
    here, coming = _card(conn, pid, code="at_point", webmim="5000200"), _card(conn, pid, webmim="5000300")
    items = {i["key"]: i["href"] for i in manager(app, conn, pid).get("/my-point/feed").get_json()["items"]}
    assert items["order-%d" % here] == "/my-point?tab=here" and items["order-%d" % coming] == "/my-point"


# 3. «На возврат»

def test_the_date_to_return_by_stands_by_the_number_of_an_agreed_order(tmp_path, clock, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    agreed(conn, card)
    mine = manager(app, conn, pid)
    box = plaque(page_of(mine, "return"), card)
    assert re.search(r'<b class="mp-return-by" data-return-by>Вернуть 13\.11\.2026</b>', box)
    assert box.index("№") < box.index("data-return-by")                          # by the number, after it


def test_no_date_no_words(tmp_path, clock, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    to_return_stage(conn, card)                                                   # on «На возврат», the return not agreed: no date yet
    mine = manager(app, conn, pid)
    page = page_of(mine, "agree") + page_of(mine, "return")
    assert 'id="order-%d"' % card in page and "data-return-by" not in page and "Вернуть " not in page


def test_the_act_has_two_buttons_download_and_print(tmp_path, clock, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    agreed(conn, card)
    mine = manager(app, conn, pid)
    page = page_of(mine, "return")
    assert "Скачать акт" not in page and "Распечатать" not in page                 # no act attached: neither button
    act, _ = acts.save(conn, pid, DAY, "act.pdf", PDF, NOW, "owner")
    box = plaque(page_of(mine, "return"), card)
    assert box.count(">Скачать акт МХ-3</a>") == 1 and box.count(">Распечатать</a>") == 1
    assert 'href="/my-point/return-act/%d"' % act in box
    link = re.search(r'<a [^>]*href="(/my-point/return-act/%d\?print=1)"[^>]*>Распечатать</a>' % act, box)
    assert link and 'target="_blank"' in link.group(0) and 'rel="noopener"' in link.group(0)
    assert 'target="_blank"' not in re.search(r"<a [^>]*>Скачать акт МХ-3</a>", box).group(0)
    office = plaque(page_of(owner, "return", pid), card)                              # the office looking at the point: its ?p= in both links
    assert 'href="/my-point/return-act/%d?p=%d"' % (act, pid) in office and 'href="/my-point/return-act/%d?print=1&amp;p=%d"' % (act, pid) in office
    assert "Скачать акт<" not in page_of(mine, "return")                           # the old name is gone everywhere


def test_the_print_link_shows_the_same_pdf_in_the_browser_with_the_same_rights(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    other = points.save_point(conn, dict(name="Гамма", channel="panel"))
    act, _ = acts.save(conn, pid, DAY, "act.pdf", PDF, NOW, "owner")
    foreign, _ = acts.save(conn, other, DAY, "gamma.pdf", PDF2, NOW, "owner")
    mine, theirs = manager(app, conn, pid), manager(app, conn, other, "delta")
    got = mine.get("/my-point/return-act/%d?print=1" % act)
    assert got.status_code == 200 and got.data == PDF
    assert got.headers["Content-Type"] == "application/pdf" and got.headers["X-Content-Type-Options"] == "nosniff"
    assert got.headers["Cache-Control"] == "no-store" and got.headers["Content-Disposition"].startswith("inline;")
    assert mine.get("/my-point/return-act/%d" % act).headers["Content-Disposition"].startswith("attachment;")     # the download as before
    assert mine.get("/my-point/return-act/%d?print=1" % foreign).status_code == 404  # another point's act
    assert theirs.get("/my-point/return-act/%d?print=1" % act).status_code == 404
    assert mine.get("/my-point/return-act/999999?print=1").status_code == 404
    assert app.test_client().get("/my-point/return-act/%d?print=1" % act).status_code == 302     # signed out: the login page
    assert owner.get("/my-point/return-act/%d?print=1&p=%d" % (foreign, other)).status_code == 200
    assert owner.get("/my-point/return-act/%d?print=1&p=%d" % (foreign, pid)).status_code == 404
    assert owner.get("/oth/return-act/%d" % act).headers["Content-Disposition"].startswith("attachment;")       # the office's file: a download


def test_one_act_of_several_orders_marks_each_of_them_in_one_colour(tmp_path, clock, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    agreed(conn, card)
    two = second(conn, pid)                                                       # the same date: the same act
    three, four = second(conn, pid, LATER, "5000300"), second(conn, pid, LATER, "5000400")
    alone = second(conn, pid, "2026-11-18", "5000500")
    mine = manager(app, conn, pid)
    assert MARK not in page_of(mine, "return")                                    # no act attached: no mark
    late, _ = acts.save(conn, pid, LATER, "later.pdf", PDF2, NOW, "owner")        # attached first, the later date
    act, _ = acts.save(conn, pid, DAY, "act.pdf", PDF, NOW, "owner")
    acts.save(conn, pid, "2026-11-18", "one.pdf", PDF, NOW, "owner")
    page = page_of(mine, "return")
    first, then = marks(plaque(page, card)), marks(plaque(page, three))
    assert first == marks(plaque(page, two)) == [("1", str(act))]                 # the colours go by the dates of the return
    assert then == marks(plaque(page, four)) == [("2", str(late))]
    assert MARK not in plaque(page, alone) and page.count(MARK) == 4              # an act of one order: no mark


def test_an_order_given_keeps_the_mark_of_the_act_it_shares(tmp_path, clock, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    agreed(conn, card)
    two = second(conn, pid)
    conn.execute("UPDATE order_item SET supplier_price = 0 WHERE order_id = ?", (card,))     # no refund counted: it stays on «На возврат»
    conn.commit()
    act, _ = acts.save(conn, pid, DAY, "act.pdf", PDF, NOW, "owner")
    assert order_oth.sign_act(conn, orders.get_card(conn, card), "Бета", DAY + "T10:00:00", "gamma") == "signed"
    conn.commit()
    page = page_of(manager(app, conn, pid), "return")
    given = re.search(r'<article[^>]*id="order-%d".*?</article>' % card, page, re.S).group(0)
    assert marks(given) == marks(plaque(page, two)) == [("1", str(act))]
    assert ">Скачать акт МХ-3</a>" in given and ">Распечатать</a>" in given
    assert "data-return-by" not in given                                          # given already: nothing to return by


def test_the_palette_is_small_and_goes_round(tmp_path):
    rows = [{"act_file": {"id": n // 2, "return_date": "2026-11-%02d" % (10 + n // 2)}} for n in range(2 * (routes_mypoint.ACT_COLOURS + 1))]
    routes_mypoint.mark_shared_acts(rows)
    assert [r["act_mark"] for r in rows[::2]] == list(range(1, routes_mypoint.ACT_COLOURS + 1)) + [1]
    assert 3 <= routes_mypoint.ACT_COLOURS <= 6
    for sheet in ("mypoint.css", "point2.css"):
        css = (Path(routes_mypoint.__file__).parent / "static" / sheet).read_text(encoding="utf-8")
        assert all(".mp-act-mark--%d" % n in css for n in range(1, routes_mypoint.ACT_COLOURS + 1)), sheet
