"""The client's filters (/shop, the owner 2026-11-09 by the office's catalog): one card of two rows as in office2/catalog.html — the width,
the profile, the diameter, «Размеры», «Поиск»; the season and the studs as segments, «Сбросить» — in both views; nothing of the office
(«Статус», «Ручные цены», «Бренды») and no supplier; each filter narrows the rows."""
from __future__ import annotations

import re

import pytest

from test_shop import make_shop, page, tochki_row
from tires_sync import store


def view(conn, state):
    store.set_state(conn, "ui_new.client", state)
    conn.commit()


def found(html) -> int:
    return int(re.search(r"[Нн]айдено (\d+)", html).group(1))


def filters(html) -> str:
    start = html.index('class="o2 shop-o2f"')
    return html[start:html.index("</form>", start)]


@pytest.mark.parametrize("state", ["off", "all"])
def test_the_client_has_the_filters_of_the_office_and_nothing_of_the_office(tmp_path, state):
    app, client, conn, _ = make_shop(tmp_path, tochki=[tochki_row("T1", rest=3)])
    view(conn, state)
    html = page(client)
    box = filters(html)
    assert box.count('class="o2-frow') == 2 and 'class="filters o2-filters"' in box
    for label in ("Ширина", "Профиль", "Диаметр", "Размеры", "Поиск", "Сезон", "Шипы", "Без шипов", "Сбросить", "Показать"):
        assert label in box, label
    for field in ('name="w"', 'name="h"', 'name="d"', 'name="size"', 'name="q"', 'name="s" value="summer"', 'name="s" value="winter"',
                  'name="t" value="studded"', 'name="t" value="friction"'):
        assert field in box, field
    assert 'class="input-icon"' in box and 'class="ico-sun"' in box and 'class="ico-snow"' in box and "o2-stud" in box
    assert 'placeholder="205/55R16 225/45R18"' in box
    assert "Статус" not in box and "Цены" not in box
    for word in ("Ручные цены", "Бренды", 'name="st"', 'name="own"', 'name="b"', 'name="xb"', "WebMIM", "4Точки", "4 Точки"):
        assert word not in html, word
    assert "shop-side" not in html and "<script" not in html


@pytest.mark.parametrize("state", ["off", "all"])
def test_each_filter_narrows_the_rows(tmp_path, state):
    app, client, conn, _ = make_shop(tmp_path, tochki=[tochki_row("T1", rest=3)])
    view(conn, state)                     # 205/55 R16 ×2 (one studded), 245/45 R18, 195/65 R15 summer
    assert found(page(client)) == 4
    assert found(page(client, "/shop?w=205")) == 2 and found(page(client, "/shop?h=45")) == 1 and found(page(client, "/shop?d=15")) == 1
    assert found(page(client, "/shop?w=205+245")) == 3 and found(page(client, "/shop?d=16,18")) == 3      # several values, as the office's
    assert found(page(client, "/shop?w=205&h=55&d=16")) == 2 and found(page(client, "/shop?w=205&d=18")) == 0
    assert found(page(client, "/shop?size=205/55R16+195/65R15")) == 3
    assert found(page(client, "/shop?s=summer")) == 1 and found(page(client, "/shop?s=winter")) == 3
    assert found(page(client, "/shop?t=studded")) == 1 and found(page(client, "/shop?t=friction")) == 3
    html = page(client, "/shop?w=205+245&s=winter&t=friction")
    assert found(html) == 2 and 'value="205 245"' in filters(html)            # the filters keep what was typed
    assert 'name="s" value="winter" checked' in filters(html) and 'name="t" value="friction" checked' in filters(html)
    for junk in ("/shop?w=abc", "/shop?d=R", "/shop?w=" + "9" * 500, "/shop?h=%00"):
        assert found(page(client, junk)) in (0, 4)


def test_reset_keeps_the_view_and_the_sort_and_drops_the_filters(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    view(conn, "off")
    box = filters(page(client, "/shop?w=205&s=winter&v=tiles&o=price"))
    assert re.search(r'class="btn btn--secondary o2-reset" href="/shop\?(v=tiles&amp;o=price|o=price&amp;v=tiles)"', box)
