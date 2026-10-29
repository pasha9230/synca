"""Every position the catalog lists has a card and «Просмотр», even without stock (the owner 2026-10-29): «нет в наличии», the last known prices
(the purchase only with the right «money») and when it was last in stock; the positions of 4tochki too."""
from __future__ import annotations

import json

import pytest

from helpers import catalog_xml, commodity_xml, make_offer
from panel_helpers import login, make_app
from tires_sync import catalog_archive, connections, store, users
from tires_sync.webmim import ParseResult

T = "2026-10-07T10:00:00"
ROWS = (commodity_xml(NNOMMODIF="1", SMNFCODE="C1", NREST="12"),
        commodity_xml(NNOMMODIF="2", SMNFCODE="C0", NREST="0", SWIDTH="215"))


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


def setup(tmp_path):
    app = make_app(tmp_path)
    owner = app.test_client()
    login(owner)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*ROWS))
    conn = store.connect(app.config["DB_PATH"])
    gone = make_offer(goods_id="900", code="GONE1", name="IKON 175/70/13 T 82 Old Model", name_orig="IKON 175/70/13 T 82 Old Model",
                      model_raw="Old Model", width=175, height=70, diameter=13, stock=7, base_price=3100.0)
    catalog_archive.record(conn, ParseResult(offers={"900": gone, "901": make_offer(goods_id="901", code="K")}), "2026-10-10T10:00:00")
    catalog_archive.record(conn, ParseResult(offers={"901": make_offer(goods_id="901", code="K")}), "2026-10-15T10:00:00")
    return app, owner, conn


def viewer(app, conn):
    users.add_user(conn, "viewer1", "длинный-пароль-1", T, role="viewer")
    c = app.test_client()
    with c.session_transaction() as session:
        session.update(user="viewer1", epoch=store.get_session_epoch(conn, "viewer1"), csrf="tok")
    return c


def test_a_position_the_supplier_took_away_has_a_card_with_its_last_prices_and_the_day_it_was_last_in_stock(tmp_path):
    app, owner, conn = setup(tmp_path)
    r = owner.get("/catalog/item/900")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "data-out-of-stock" in html and "Нет в наличии." in html and "нет у поставщика с 15.10.2026" in html
    assert "Последний раз в наличии" in html and "10.10.2026 10:00:00" in html
    assert "Последняя цена продажи (за 1 шт.)" in html
    assert "Последняя закупка (за 1 шт.)" in html and "3 100 ₽" in html
    assert "Своя цена по аккаунтам" not in html                      # an own price is for a position of the feed only
    view = viewer(app, conn).get("/catalog/item/900").get_data(as_text=True)
    assert "Нет в наличии." in view and "Последняя закупка" not in view and "3 100" not in view


def test_the_catalog_row_of_a_gone_position_has_a_preview_and_the_preview_opens(tmp_path):
    app, owner, conn = setup(tmp_path)
    page = owner.get("/catalog").get_data(as_text=True)
    before, after = page.split(">GONE1<", 1)
    row = before.rsplit("<tr", 1)[1] + after.split("</tr>")[0]
    assert 'data-preview="900"' in row
    preview = owner.get("/catalog/preview/900")
    assert preview.status_code == 200 and "нет в наличии" in preview.get_data(as_text=True)


def test_a_position_in_the_unload_with_stock_0_says_so_and_its_last_in_stock_day_is_unknown(tmp_path):
    app, owner, conn = setup(tmp_path)
    html = owner.get("/catalog/item/2").get_data(as_text=True)
    assert "data-out-of-stock" in html and "Последний раз в наличии" in html
    assert "Остаток" in html and "нет в наличии" in html
    live = owner.get("/catalog/item/1").get_data(as_text=True)
    assert "data-out-of-stock" not in live and "12 шт." in live and "Цена продажи (за 1 шт.)" in live


def test_a_position_of_4tochki_before_its_switch_has_a_card_and_a_preview(tmp_path):
    from test_tochki_panel import row
    app, owner, conn = setup(tmp_path)
    sid = connections.create_supplier(conn, "4Точки", T, "o")
    connections.set_config(conn, "tochki", 0, {"login": "api-user", "supplier_id": sid, "feed": False}, T, "o")
    conn.commit()
    (tmp_path / "tochki-snapshot.json").write_text(json.dumps({"version": 1, "fetched_at": "2026-10-07T11:00:00", "warehouse_id": 7,
                                                               "items": [row("A1"), row("A0", rest=0)]}), encoding="utf-8")
    page = owner.get("/catalog?q=A1").get_data(as_text=True)
    ids = [g for g in ("s%d-A1" % sid,) if 'data-preview="%s"' % g in page]
    assert ids, "the 4tochki row has «Просмотр»"
    assert owner.get("/catalog/preview/" + ids[0]).status_code == 200
    card = owner.get("/catalog/item/" + ids[0])
    assert card.status_code == 200 and "A1" in card.get_data(as_text=True)
    assert owner.get("/catalog/item/nothing-like-it").status_code == 404
