"""Своя цена любой позиции каталога (слово владельца 2026-11-09: «я хочу ставить свою цену даже если нет у поставщика», «пусть работает во
всех»): только что пропавшей из выгрузки (ещё не отмеченной), отмеченной пропавшей, показанной только для просмотра (4 точки до включения
«в фид», старая вторая выгрузка). В рублях всегда, в процентах — от известной закупки. Не меняется: без товара у поставщика на Авито уходит
только со «Своим остатком»; ручное «вкл/выкл» массово таким позициям не ставится (03.11); ставят владелец и партнёр (право «деньги»)."""
from __future__ import annotations

import math
from datetime import datetime

from werkzeug.security import generate_password_hash

from helpers import make_offer, make_settings
from panel_helpers import login, post
from test_manual_prices import BLIND, GONE, _draft, _flash, _gone_app, _owner
from test_oth_manual import manual, price_of
from test_roles import person
from test_stock_pass import AFTER, FIVE, ads_of, prepared, public, run
from tires_sync import catalog_archive, connections, staging, store, tochki_feed
from tires_sync.panel import create_app
from tires_sync.settings_store import confirm_draft
from tires_sync.sync import _manual_gone
from tires_sync.webmim import ParseResult

NOW = "2026-11-09T10:00:00"


def _changes(conn):
    return staging.summary(conn)["changes"].get("price_overrides") or {}


def _panel(tmp_path):
    """The panel on the database and unload of the cycle's tests (test_stock_pass.prepared)."""
    return create_app({"TESTING": True, "SECRET_KEY": "test-secret", "DB_PATH": str(tmp_path / "t.db"),
                       "PASSWORD_HASH": generate_password_hash("pw", method="pbkdf2:sha256:1000"),
                       "UNLOAD_PATH": str(tmp_path / "unload.xml"), "REFERENCE_PATH": str(tmp_path / "reference.txt"),
                       "NOW": lambda: datetime(2026, 11, 9, 12, 0, 0)})


def _unmark(tmp_path, goods_id):
    conn = store.connect(tmp_path / "t.db")
    conn.execute("UPDATE catalog_item SET gone_since = '' WHERE goods_id = ?", (goods_id,))     # пропала, но ещё не отмечена (обвал не принят)
    conn.commit()
    conn.close()


def _apply(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    confirm_draft(conn, staging.summary(conn)["draft_id"], NOW, "owner", strong=True)
    conn.close()


# --- фид ---------------------------------------------------------------------------------------------------------------------------

def test_a_price_set_on_a_position_just_gone_reaches_the_feed_with_the_own_stock_only(tmp_path):
    prepared(tmp_path, accounts=("A",))
    assert run(tmp_path, FIVE) == 0
    assert run(tmp_path, AFTER) == 0                                     # 102 ушла из выгрузки
    _unmark(tmp_path, "102")
    client = _panel(tmp_path).test_client()
    tok = login(client)
    assert "data-own-gone" in client.get("/catalog/item/102").get_data(as_text=True)
    r = post(client, "/catalog/item/102/own-price", tok, kind="price", value="6900", a=["1"], action="save")
    assert "в черновике" in _flash(client, r)
    _apply(tmp_path)
    _unmark(tmp_path, "102")
    assert run(tmp_path, AFTER) == 0                                     # своего остатка нет: цена одна объявление не выпускает
    assert "102" not in ads_of(public(tmp_path))
    manual(tmp_path, "102", 2)
    _unmark(tmp_path, "102")
    assert run(tmp_path, AFTER) == 0
    assert price_of(public(tmp_path), "102") == "6900"


def test_a_percent_on_a_position_just_gone_is_counted_from_its_last_purchase_in_the_feed(tmp_path):
    prepared(tmp_path, accounts=("A",))
    assert run(tmp_path, FIVE) == 0
    assert run(tmp_path, AFTER) == 0
    _unmark(tmp_path, "102")
    client = _panel(tmp_path).test_client()
    tok = login(client)
    post(client, "/catalog/item/102/own-price", tok, kind="pct", value="10", a=["1"], action="save")
    _apply(tmp_path)
    conn = store.connect(tmp_path / "t.db")
    last = conn.execute("SELECT last_price FROM catalog_item WHERE goods_id = '102'").fetchone()[0]
    conn.close()
    manual(tmp_path, "102", 2)
    assert run(tmp_path, AFTER) == 0
    assert price_of(public(tmp_path), "102") == str(int(math.ceil(last * 1.1 / 10) * 10))


def test_the_own_price_of_a_view_only_position_is_this_accounts_and_waits_for_it_to_enter_the_feed(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    for code, price in (("s3-1", 2000.0), ("s2-5", 3000.0)):
        catalog_archive.record_prices(conn, {code: make_offer(goods_id=code, base_price=price)}, NOW)
    settings = make_settings(price_overrides={"1:s3-1": {"price": 4500}, "1:s2-5": {"price": 4700}, "2:s3-1": {"price": 9999}})
    eff = connections.effective_settings(conn, 1, settings)
    assert eff.own_prices["s3-1"] == ("price", 4500) and eff.own_prices["s2-5"] == ("price", 4700)    # чужого аккаунта не видно
    from tires_sync.pricing import position_price
    assert position_price(make_offer(goods_id="s3-1", base_price=2000.0), eff) == 4500       # вошла в фид (4 точки «в фид»): со своей ценой
    on_oth = {"s3-1": 2, "s2-5": 2}
    offers, prices, drop, no_price = _manual_gone(conn, ParseResult(offers={}, unavailable={2}), on_oth, {}, eff, 3)
    assert offers == {} and prices == {} and drop == {"s3-1"} and no_price == set()   # 4 точки вне пробы и старая вторая выгрузка: не на Авито


# --- карточка: 4 точки до включения «в фид» -------------------------------------------------------------------------------------------

def _tochki_app(tmp_path, monkeypatch, price=2000.0):
    app, conn, ids = _gone_app(tmp_path)

    def view(conn_, parsed, base):
        only = {"s3-1": make_offer(goods_id="s3-1", code="T1", base_price=price)}
        return ParseResult(offers={**parsed.offers, **only}), frozenset(only)
    monkeypatch.setattr(tochki_feed, "catalog_view", view)
    return app, conn, ids


def test_the_card_of_4tochki_before_its_switch_sets_rubles_always_and_a_percent_of_its_purchase(tmp_path, monkeypatch):
    app, conn, (first, second) = _tochki_app(tmp_path, monkeypatch)
    client, tok = _owner(app)
    page = client.get("/catalog/item/s3-1").get_data(as_text=True).replace("\xa0", " ")
    assert "data-own-view" in page and "от закупки 2 000 ₽" in page and 'action="/catalog/item/s3-1/own-price"' in page
    post(client, "/catalog/item/s3-1/own-price", tok, kind="pct", value="20", a=[str(first)], action="save")
    post(client, "/catalog/item/s3-1/own-price", tok, kind="price", value="2990", a=[str(second)], action="save")
    assert _changes(conn) == {"%d:s3-1" % first: {"pct": 20.0}, "%d:s3-1" % second: {"price": 2990}}


def test_a_percent_of_a_view_only_position_without_a_purchase_is_refused(tmp_path, monkeypatch):
    app, conn, (first, _) = _tochki_app(tmp_path, monkeypatch, price=0.0)
    client, tok = _owner(app)
    assert "нельзя: закупка неизвестна" in client.get("/catalog/item/s3-1").get_data(as_text=True)
    r = post(client, "/catalog/item/s3-1/own-price", tok, kind="pct", value="5", a=[str(first)], action="save")
    assert "Поставьте цену продажи в рублях" in _flash(client, r) and _changes(conn) == {}


def test_a_price_below_the_purchase_is_a_warning_not_a_refusal(tmp_path):
    app, conn, (first, _) = _gone_app(tmp_path)
    conn.execute("UPDATE catalog_item SET gone_since = '' WHERE goods_id = ?", (GONE,))
    conn.commit()
    client, tok = _owner(app)
    post(client, "/catalog/item/%s/own-price" % GONE, tok, kind="price", value="3000", a=[str(first)], action="save")    # закупка 4 000
    assert _changes(conn) == {"%d:%s" % (first, GONE): {"price": 3000}}
    assert "ниже закупки" in client.get("/catalog/item/%s" % GONE).get_data(as_text=True)


# --- права -------------------------------------------------------------------------------------------------------------------------

def test_rights_are_as_before_the_owner_sets_the_partner_sees_a_role_without_money_neither(tmp_path, monkeypatch):
    """Права не меняются: ставит владелец (раздел «Товар и фид» на правку и право «деньги»); партнёр с «деньгами» видит блок, раздел у него
    только на просмотр; без «денег» блока нет."""
    app, conn, (first, _) = _tochki_app(tmp_path, monkeypatch)
    conn.execute("UPDATE catalog_item SET gone_since = '' WHERE goods_id = ?", (GONE,))
    conn.commit()
    owner, tok = _owner(app)
    for goods_id in ("s3-1", GONE):
        post(owner, "/catalog/item/%s/own-price" % goods_id, tok, kind="price", value="4500", a=[str(first)], action="save")
    assert set(_changes(conn)) == {"%d:s3-1" % first, "%d:%s" % (first, GONE)}
    form = {"csrf": "tok", "kind": "price", "value": "1", "a": str(first), "action": "save"}
    partner = person(app, "partner")[0]
    for goods_id in ("s3-1", GONE):
        page = partner.get("/catalog/item/%s" % goods_id).get_data(as_text=True)
        assert "Своя цена по аккаунтам" in page and 'action="/catalog/item/%s/own-price"' % goods_id not in page
        assert partner.post("/catalog/item/%s/own-price" % goods_id, data=form).status_code == 403
    for role in ("chats", "viewer"):
        other = person(app, role)[0]
        for goods_id in ("s3-1", GONE):
            page = other.get("/catalog/item/%s" % goods_id).get_data(as_text=True)
            assert "Своя цена по аккаунтам" not in page and "own-price" not in page
            assert other.post("/catalog/item/%s/own-price" % goods_id, data=form).status_code == 403
    assert _changes(conn) == {"%d:s3-1" % first: {"price": 4500}, "%d:%s" % (first, GONE): {"price": 4500}}


# --- массово -----------------------------------------------------------------------------------------------------------------------

def test_the_bulk_own_price_takes_positions_without_the_supplier_and_the_manual_rule_still_skips_them(tmp_path, monkeypatch):
    app, conn, (first, _) = _tochki_app(tmp_path, monkeypatch)
    conn.execute("UPDATE catalog_item SET gone_since = '' WHERE goods_id = ?", (BLIND,))     # BLIND: только что пропала, закупка неизвестна
    conn.commit()
    client, tok = _owner(app)
    r = _draft(client, tok, ["100", GONE, BLIND, "s3-1"], [first], base="purchase", unit="pct", side="up", value="5")
    assert _changes(conn) == {"%d:%s" % (first, g): {"pct": 5.0} for g in ("100", GONE, "s3-1")}
    assert "закупка неизвестна: 1" in _flash(client, r)
    _draft(client, tok, [BLIND], [first], base="purchase", unit="rub", side="up", value="100")
    assert "%d:%s" % (first, BLIND) not in _changes(conn)                    # «от закупки» без закупки — нет
    staging.set_own_price(conn, "s3-1", [first], {"price": 2500}, NOW)
    assert 'name="g" value="s3-1"' in client.get("/catalog?own=1").get_data(as_text=True)      # отмечается под фильтром «Ручные цены»
    assert 'name="g" value="s3-1"' not in client.get("/catalog").get_data(as_text=True)
    for mode in ("on", "off"):                                               # ручное правило таким позициям не ставится (03.11)
        post(client, "/catalog/stage", tok, bulk=mode, g=["100", GONE, BLIND, "s3-1"])
        assert staging.summary(conn)["changes"].get("overrides") == {"1:100": mode}


def test_the_card_of_a_position_unknown_everywhere_is_still_refused(tmp_path):
    app, conn, (first, _) = _gone_app(tmp_path)
    client, tok = _owner(app)
    assert client.get("/catalog/item/nope").status_code == 404
    r = post(client, "/catalog/item/nope/own-price", tok, kind="price", value="4500", a=[str(first)], action="save")
    assert "нет в выгрузке и не было в ней раньше" in _flash(client, r) and _changes(conn) == {}


def test_all_by_the_filter_takes_a_gone_position_but_never_one_shown_for_viewing_only(tmp_path, monkeypatch):
    """Ревью: «все по фильтру» по бренду или размеру не должны молча ставить цену позициям только для просмотра — их отмечают руками."""
    import re
    app, conn, (first, _) = _tochki_app(tmp_path, monkeypatch)
    client, tok = _owner(app)
    page = client.get("/catalog").get_data(as_text=True)
    fp = re.search(r'name="fp" value="([^"]*)"', page).group(1)
    form = dict(all="1", filter="", fp=fp, base="purchase", unit="rub", side="up", value="100")
    r = post(client, "/catalog/own-price/bulk", tok, a=[str(first)], **form)
    assert r.status_code == 200 and "только просмотр" not in r.get_data(as_text=True)
    plan = re.search(r'name="plan" value="([0-9a-f]+)"', r.get_data(as_text=True)).group(1)
    r = post(client, "/catalog/own-price/bulk", tok, a=[str(first)], step="draft", plan=plan, **form)
    assert "только для просмотра (по фильтру не берутся" in _flash(client, r)
    assert "%d:s3-1" % first not in _changes(conn) and "%d:%s" % (first, GONE) in _changes(conn)
    r = _draft(client, tok, ["s3-1"], [first], base="purchase", unit="rub", side="up", value="100")
    assert _changes(conn)["%d:s3-1" % first] == {"price": 2100}
