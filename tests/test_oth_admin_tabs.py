"""«На ОТХ» of the office in four tabs (the owner 2026-11-22): «На точке», «На согласовании», «На возврат», «Вернули поставщику» (a year);
«Согласованы» (the act may come later) and «Вернули поставщику» by checkboxes; the search, the filters, the point and the sort on every tab."""
from __future__ import annotations

import re

from test_oth_returns_eg import ACT_DAY, act_post, office_clock, two_orders  # noqa: F401  (office_clock: a fixture)
from test_point_oth_sell import setup
from tires_sync import order_oth, orders


def tab_ids(client, tab="", **q):
    query = dict(q, **({"tab": tab} if tab else {}))
    page = client.get("/oth", query_string=query).get_data(as_text=True)
    return sorted({int(x) for x in re.findall(r'data-href="/orders/(\d+)"', page)} | {int(x) for x in re.findall(r'data-returned-row="\d+"[^>]*>.*?<a href="/orders/(\d+)"', page, re.S)})


def agree(client, ids, day=ACT_DAY, tab="agree"):
    return client.post("/oth/return-act/orders", data={"csrf": "tok", "order": [str(i) for i in ids], "return_date": day, "tab": tab},
                       content_type="multipart/form-data")


def test_the_orders_move_through_the_tabs_by_the_two_mass_buttons(tmp_path, office_clock):
    app, owner, conn, pid, card = setup(tmp_path)
    ids = two_orders(conn, pid)                                                           # both on the agreement of their return
    assert tab_ids(owner) == [] and tab_ids(owner, "agree") == ids and tab_ids(owner, "return") == []
    page = owner.get("/oth?tab=agree").get_data(as_text=True)
    assert all('data-agree-one="%d"' % i in page for i in ids) and ">Согласованы</button>" in page and 'name="file"' in page
    r = agree(owner, ids)                                                                 # no act: agreed all the same (2026-11-22)
    assert r.status_code == 302 and r.headers["Location"].endswith("/oth?tab=agree")
    assert tab_ids(owner, "agree") == [] and tab_ids(owner, "return") == ids
    assert all(order_oth.return_plan(conn, i)["return_date"] == ACT_DAY for i in ids)
    page = owner.get("/oth?tab=return").get_data(as_text=True)
    assert 'id="oth-sent-back"' in page and all('form="oth-sent-back" data-act-order="%d"' % i in page for i in ids)
    r = owner.post("/oth/sent-back", data={"csrf": "tok", "order": [str(ids[0])], "tab": "return"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/oth?tab=return")
    assert order_oth.return_plan(conn, ids[0])["signed_at"] and order_oth.return_plan(conn, ids[1])["signed_at"] is None
    assert tab_ids(owner, "return") == [ids[1]] and ids[0] in tab_ids(owner, "returned")
    owner.post("/oth/sent-back", data={"csrf": "tok", "order": [str(ids[0])]})              # again: nothing more, told why
    assert order_oth.return_plan(conn, ids[1])["signed_at"] is None


def test_one_order_agreed_by_its_own_button(tmp_path, office_clock):
    app, owner, conn, pid, card = setup(tmp_path)
    ids = two_orders(conn, pid)
    assert agree(owner, [ids[0]]).status_code == 302
    assert tab_ids(owner, "agree") == [ids[1]] and tab_ids(owner, "return") == [ids[0]]


def test_with_an_act_the_orders_must_be_of_one_point(tmp_path, office_clock):
    app, owner, conn, pid, card = setup(tmp_path)
    ids = two_orders(conn, pid)
    from tires_sync import oth_return_act
    assert act_post(owner, ids).status_code == 302                                        # one point, the act: as before (E3)
    assert tab_ids(owner, "return") == ids and oth_return_act.find(conn, pid, ACT_DAY) is not None


def test_the_returns_are_kept_a_year_and_every_tab_counts_and_filters(tmp_path, office_clock):
    from test_order_oth_stock import _signed
    from tests.test_order_reserve import card_at
    app, owner, conn, pid, card = setup(tmp_path)
    old, fresh = card_at(conn, "oth", "2025-01-10T10:00:00"), card_at(conn, "oth", "2026-11-01T10:00:00")
    _signed(conn, old, "5300001", "26R-1", "2025-10-01T12:00:00")                         # more than a year ago (the clock: 2026-11-1x)
    _signed(conn, fresh, "5300002", "26R-2", "2026-11-02T12:00:00")
    assert tab_ids(owner, "returned") == [fresh]
    page = owner.get("/oth?tab=returned").get_data(as_text=True)
    assert re.search(r'data-oth-tab="returned">Вернули поставщику <span class="badge badge--neutral" data-returned-pieces>4 шт\.', page)
    ids = two_orders(conn, pid)
    assert tab_ids(owner, "agree", q="5000200") == [ids[1]]                               # the search on the tab of the agreement too
    assert tab_ids(owner, "agree", q="нет такого") == []


def test_the_sort_turns_and_keeps_the_filters(tmp_path, office_clock):
    from test_order_oth_stock import _signed
    from tests.test_order_reserve import card_at
    app, owner, conn, pid, card = setup(tmp_path)
    a, b = card_at(conn, "oth", T1 := "2026-11-01T10:00:00"), card_at(conn, "oth", T1)
    _signed(conn, a, "5300001", "26R-1", "2026-11-02T12:00:00")
    _signed(conn, b, "5300002", "26R-2", "2026-11-05T12:00:00")
    order = lambda html: [int(x) for x in re.findall(r'data-returned-row="\d+"[^>]*>.*?<a href="/orders/(\d+)"', html, re.S)]
    assert order(owner.get("/oth?tab=returned").get_data(as_text=True)) == [b, a]          # the newest return first
    assert order(owner.get("/oth?tab=returned&s=date&d=asc").get_data(as_text=True)) == [a, b]
    assert order(owner.get("/oth?tab=returned&s=order&d=desc").get_data(as_text=True)) == [b, a]
    page = owner.get("/oth?tab=returned&q=5300&s=date&d=desc").get_data(as_text=True)
    link = re.search(r'<a href="(/oth\?[^"]*s=date[^"]*)" class="is-active"', page).group(1)
    assert "q=5300" in link and "d=asc" in link and "tab=returned" in link                 # the chosen key again turns, the filters stay


def test_a_filter_that_hides_the_first_row_of_an_order_keeps_its_checkbox_and_its_button(tmp_path, office_clock):
    """Review 2026-11-22: the checkbox (and «Возврат согласован») stood on the first row of the order before the search."""
    app, owner, conn, pid, card = setup(tmp_path)
    ids = two_orders(conn, pid)
    first = orders.get_card(conn, ids[0])
    orders.add_item(conn, ids[0], goods_id="9", name="Вторая шина K-2", brand_key="nokian", season="winter", diameter=16, territory="1", qty=1,
                    sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now="2026-11-12T10:00:00", author="o")
    page = owner.get("/oth?tab=agree&q=Вторая").get_data(as_text=True)
    assert 'data-act-order="%d"' % ids[0] in page and 'data-agree-one="%d"' % ids[0] in page
    assert first


def test_the_point_chosen_keeps_the_tab_and_the_search_keeps_the_sort(tmp_path, office_clock):
    """Review 2026-11-22: the form of the point sent the page back to «На точке»; «Найти» dropped the sort."""
    app, owner, conn, pid, card = setup(tmp_path)
    two_orders(conn, pid)
    page = owner.get("/oth?tab=agree&s=qty&d=desc").get_data(as_text=True)
    point_form = re.search(r'<form method="get" action="/oth" class="filters">.*?</form>', page, re.S).group(0)
    assert '<input type="hidden" name="tab" value="agree">' in point_form
    search = re.search(r'<form method="get" action="/oth" class="filters" data-returned-filters>.*?</form>', page, re.S).group(0)
    assert 'name="s" value="qty"' in search and 'name="d" value="desc"' in search


def test_the_office_s_sent_back_closes_the_order_with_its_refund(tmp_path, office_clock):
    app, owner, conn, pid, card = setup(tmp_path)
    ids = two_orders(conn, pid)
    agree(owner, ids)
    owner.post("/oth/sent-back", data={"csrf": "tok", "order": [str(i) for i in ids]})
    for i in ids:
        card = orders.get_card(conn, i)
        assert card["stage"]["code"] == "return" and card["return_amount"] > 0 and order_oth.return_plan(conn, i)["signed_at"], i


def test_a_button_of_a_row_comes_back_to_its_tab(tmp_path, office_clock):
    app, owner, conn, pid, card = setup(tmp_path)
    two_orders(conn, pid)
    page = owner.get("/oth?tab=agree").get_data(as_text=True)
    form = re.search(r'<form method="post" action="/oth/cancel-return".*?</form>', page, re.S)
    assert form is None or '<input type="hidden" name="tab" value="agree">' in form.group(0)
    agree_one = re.search(r'<form method="post" action="/oth/return-act/orders"[^>]*data-agree-one=.*?</form>', page, re.S).group(0)
    assert agree_one.count('name="tab"') == 1
