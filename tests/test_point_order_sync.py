"""«Рабочее место» follows the order (the owner 2026-11-25): what the office changes in the order's window — the positions, the client, the
dates, «Комментарий» — is what the point sees; the comment shows in the card only when typed, never its automatic part nor an Avito
account's name; another point sees nothing of it; the open page reloads itself on a change (the print of /my-point/feed)."""
from __future__ import annotations

import re

import pytest

from test_mypoint import manager
from test_point_new_view import flag
from test_vk_commands import NOW, _card, ready
from tires_sync import connections, order_points as points, orders
from tires_sync.panel import routes_mypoint

LATER = "2026-10-14T10:00:00"


@pytest.fixture(params=["old", "new"])
def view(request):
    return request.param


def _setup(tmp_path, view):
    app, owner, tok, conn, pid, card = ready(tmp_path)
    if view == "new":
        flag(conn, "all")
    conn.commit()
    return app, conn, pid, card, manager(app, conn, pid)


def _page(client, tab="moving"):
    return client.get("/my-point?tab=%s" % tab).get_data(as_text=True)


def _card_html(page, card):
    found = re.search(r'<article[^>]*data-order="%d".*?</article>' % card, page, re.S)
    assert found, "the card %d is not on the page" % card
    return found.group(0)


def test_office_changes_of_the_positions_and_the_client_are_seen_by_the_point(tmp_path, view):
    app, conn, pid, card, mine = _setup(tmp_path, view)
    before = _card_html(_page(mine), card)
    assert "Nokian Hakkapeliitta 10" in before and "Татьяна" in before
    item = orders.list_items(conn, card)[0]
    orders.update_item(conn, item["id"], 2, 5500, LATER, "o")
    orders.add_item(conn, card, goods_id="2", name="Michelin X-Ice North 4", brand_key="michelin", season="winter", diameter=16, territory="1",
                    qty=4, sale_unit=6000, supplier_price=5000, terms=None, fee_pct=0, promo_pct=0, now=LATER, author="o")
    orders.update_card(conn, card, dict(client_name="Ольга", client_phone="+7 900 444 55 66"), LATER, "o")
    after = _card_html(_page(mine), card)
    assert "Michelin X-Ice North 4" in after and "Ольга" in after and "Татьяна" not in after
    assert re.search(r'Hakkapeliitta 10(?:<span class="p2-q">| × )2\b', after)              # the pieces changed
    assert re.search(r"Цена ед\. 5\D?500", after)                                         # the price written in the order («Подробнее»)
    assert "900 444" in after or "79004445566" in after                                   # the phone in «Подробнее»


def test_the_comment_shows_only_when_typed_with_its_lines_and_escaped(tmp_path, view):
    app, conn, pid, card, mine = _setup(tmp_path, view)
    assert "data-point-comment" not in _card_html(_page(mine), card)                       # none typed: no line at all
    orders.update_card(conn, card, dict(comment="Клиент приедет в пятницу\n<b>после 18:00</b>"), LATER, "o")
    html = _card_html(_page(mine), card)
    assert "data-point-comment" in html and "Комментарий" in html
    assert "Клиент приедет в пятницу\n&lt;b&gt;после 18:00&lt;/b&gt;" in html and "<b>после" not in html
    orders.update_card(conn, card, dict(comment=""), LATER, "o")
    assert "data-point-comment" not in _card_html(_page(mine), card)                       # cleared: gone


def test_the_comment_has_no_automatic_part_nor_an_account_name(tmp_path, view):
    app, conn, pid, card, mine = _setup(tmp_path, view)
    connections.create_account(conn, "Шинный Двор", NOW, "o")
    orders.update_card(conn, card, dict(comment="Объявление: Шины R16 https://www.avito.ru/123 — звонить после обеда, Шинный Двор"), LATER, "o")
    html = _card_html(_page(mine), card)
    assert "звонить после обеда" in html and "avito.ru" not in html and "Объявление:" not in html and "Шинный Двор" not in html
    orders.update_card(conn, card, dict(comment="Объявление: Шины R16 https://www.avito.ru/123"), LATER, "o")
    assert "data-point-comment" not in _card_html(_page(mine), card)                       # only the automatic part: nothing to show


def test_another_point_sees_neither_the_order_nor_its_comment(tmp_path, view):
    app, conn, pid, card, mine = _setup(tmp_path, view)
    orders.update_card(conn, card, dict(comment="Секрет для Беты"), LATER, "o")
    other = points.save_point(conn, dict(name="Гамма", channel="vk", vk_peer_id="777"))
    theirs = manager(app, conn, other, login_="delta")
    for tab in ("moving", "here", "oth", "return", "issued"):
        page = _page(theirs, tab)
        assert "Секрет для Беты" not in page and 'data-order="%d"' % card not in page
    assert "Секрет для Беты" not in theirs.get("/my-point/feed").get_data(as_text=True)


def test_the_feed_print_changes_with_the_order_and_the_page_carries_it(tmp_path, view):
    app, conn, pid, card, mine = _setup(tmp_path, view)
    page = _page(mine)
    shown = re.search(r'data-point-rev="([0-9a-f]+)"', page).group(1)
    rev = mine.get("/my-point/feed").get_json()["rev"]
    assert rev == shown and mine.get("/my-point/feed").get_json()["rev"] == rev          # nothing changed: the same print, no reload
    orders.update_card(conn, card, dict(comment="Новый комментарий"), LATER, "o")
    assert mine.get("/my-point/feed").get_json()["rev"] != rev
    rev2 = mine.get("/my-point/feed").get_json()["rev"]
    orders.add_item(conn, card, goods_id="3", name="Ikon Nordman 7", brand_key="ikon", season="winter", diameter=16, territory="1", qty=1,
                    sale_unit=4000, supplier_price=3000, terms=None, fee_pct=0, promo_pct=0, now=LATER, author="o")
    assert mine.get("/my-point/feed").get_json()["rev"] != rev2                          # a position added
    rev3 = mine.get("/my-point/feed").get_json()["rev"]
    orders.update_card(conn, card, dict(special_amount="100"), LATER, "o")                # the office's own field: not the point's, no reload
    assert mine.get("/my-point/feed").get_json()["rev"] == rev3


def test_the_owner_looking_at_a_point_gets_no_print(tmp_path, view):
    app, owner, tok, conn, pid, card = ready(tmp_path)
    if view == "new":
        flag(conn, "all")
    conn.commit()
    assert "data-point-rev" not in owner.get("/my-point?p=%d" % pid).get_data(as_text=True)     # its feed is another point's: no reload loop
    assert "rev" not in owner.get("/my-point/feed").get_json()


def test_the_reload_waits_while_a_field_holds_typed_text():
    """notify.js: a field typed in and left (the search, «Другой» of «ОТКАЗ») keeps the page busy, the reload never wipes the text."""
    from pathlib import Path
    script = (Path(routes_mypoint.__file__).parent / "static" / "notify.js").read_text(encoding="utf-8")
    edited = re.search(r"function edited\(\) \{.*?\n  \}", script, re.S).group(0)
    assert 'input:not([type=hidden]), textarea' in edited and "f.value !== f.defaultValue" in edited and "f.checked !== f.defaultChecked" in edited
    assert "|| edited()) return false" in re.search(r"function idle\(\) \{.*?\n  \}", script, re.S).group(0)


def test_the_feed_reads_the_cards_and_the_account_names_once(tmp_path, monkeypatch):
    app, conn, pid, card, mine = _setup(tmp_path, "old")
    orders.update_card(conn, card, dict(comment="Позвонить заранее"), LATER, "o")
    calls = {"cards": 0, "oth": 0, "names": 0}
    real_cards, real_oth, real_names = routes_mypoint._cards, routes_mypoint._oth_cards, orders.account_names

    def count(key, real):
        def wrapped(*args, **kw):
            calls[key] += 1
            return real(*args, **kw)
        return wrapped
    monkeypatch.setattr(routes_mypoint, "_cards", count("cards", real_cards))
    monkeypatch.setattr(routes_mypoint, "_oth_cards", count("oth", real_oth))
    monkeypatch.setattr(orders, "account_names", count("names", real_names))
    assert mine.get("/my-point/feed").get_json()["rev"]
    assert calls == {"cards": 1, "oth": 1, "names": 1}