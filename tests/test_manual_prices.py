"""«Ручные цены» каталога (решение владельца 2026-11-03): фильтр позиций со своей ценой, массовая правка от закупки или от своей цены в % или ₽
с показом «было → стало» до черновика, массовый сброс к настройкам фида, своя цена позиции, которой нет у поставщика (₽ всегда, % — только
от известной последней закупки). Всё через черновик и «Применить» с прежними подтверждениями; в журнале — «было → стало»."""
from __future__ import annotations

import json
import re
from datetime import datetime

import pytest

from helpers import make_offer
from panel_helpers import login, post
from test_own_price import NOW, _app
from tires_sync import catalog_archive, own_bulk, staging, store
from tires_sync.settings_store import active_settings, confirm_draft
from tires_sync.webmim import ParseResult

GONE, BLIND = "900", "901"          # нет у поставщика: последняя закупка 4 000 ₽ / закупка неизвестна


def _gone_app(tmp_path):
    app, conn, ids = _app(tmp_path)
    offers = {g: make_offer(goods_id=g, code="G" + g, name="Ушедшая %s" % g, name_orig="Ушедшая %s" % g, base_price=price)
              for g, price in ((GONE, 4000.0), (BLIND, 0.0))}
    catalog_archive.record(conn, ParseResult(offers=offers), "2026-10-01T10:00:00")
    catalog_archive.record(conn, ParseResult(offers={"100": make_offer()}), "2026-10-02T10:00:00")
    return app, conn, ids


def _owner(app):
    client = app.test_client()
    return client, login(client)


def _preview(client, tok, goods, accounts, **form):
    return post(client, "/catalog/own-price/bulk", tok, g=list(goods), a=[str(a) for a in accounts], **form)


def _draft(client, tok, goods, accounts, **form):
    r = _preview(client, tok, goods, accounts, **form)
    if r.status_code != 200:
        return r
    plan = re.search(r'name="plan" value="([0-9a-f]+)"', r.get_data(as_text=True)).group(1)
    return _preview(client, tok, goods, accounts, step="draft", plan=plan, **form)


def _changes(conn):
    return staging.summary(conn)["changes"].get("price_overrides")


def _flash(client, r):
    return client.get(r.headers["Location"]).get_data(as_text=True)


# --- расчёт ------------------------------------------------------------------------------------------------------------------------

def test_the_plan_counts_each_account_from_its_own_base_and_rounds_up_to_tens():
    prices = {"1:a": {"price": 7000}, "2:a": {"pct": 10}, "1:b": {"pct": 5}}
    purchases = {"a": 5000.0, "b": 0.0}
    down = own_bulk.plan(prices, purchases, [1, 2], {"base": "own", "unit": "pct", "delta": -5})
    assert down["set"] == {"1:a": {"price": 6650}, "2:a": {"price": 5230}}         # 7 000 − 5 % = 6 650; 5 500 − 5 % = 5 225 → 5 230
    assert down["skipped"] == {"no_purchase": ["1:b"], "no_own": ["2:b"]}           # процент без закупки не считается; своей цены нет
    up = own_bulk.plan(prices, purchases, [1], {"base": "purchase", "unit": "rub", "delta": 305}, redo=True)
    assert up["set"] == {"1:a": {"price": 5310}} and up["skipped"] == {"no_purchase": ["1:b"]}
    assert own_bulk.plan(prices, purchases, [1], {"base": "purchase", "unit": "pct", "delta": 5})["kept"] == ["1:a", "1:b"]
    assert own_bulk.plan(prices, purchases, [1, 2], None)["set"] == {"1:a": None, "1:b": None, "2:a": None}
    low = own_bulk.plan({"1:a": {"price": 100}}, {"a": 5000.0}, [1], {"base": "own", "unit": "rub", "delta": -200})
    assert low["set"] == {} and low["skipped"] == {"out": ["1:a"]}


# --- фильтр «Ручные цены» ------------------------------------------------------------------------------------------------------------

def test_the_filter_shows_positions_with_an_own_price_in_any_account_applied_or_in_the_draft_with_the_accounts(tmp_path):
    app, conn, (first, second) = _gone_app(tmp_path)
    client, tok = _owner(app)
    staging.set_own_price(conn, "100", [first], {"price": 7000}, NOW)
    confirm_draft(conn, staging.summary(conn)["draft_id"], NOW, "owner")
    staging.set_own_price(conn, "101", [second], {"pct": 5}, NOW)
    staging.set_own_price(conn, GONE, [first], {"price": 4500}, NOW)
    page = client.get("/catalog?own=1").get_data(as_text=True)
    shown = set(re.findall(r'name="g" value="([^"]+)"', page))
    assert shown == {"100", "101", GONE}
    assert "Основной: цена 7 000 ₽" in page and "Шина-Колесо: +5 % к закупке" in page and 'name="own" value="1" checked' in page
    page = client.get("/catalog?own=1&st=gone").get_data(as_text=True)
    assert set(re.findall(r'name="g" value="([^"]+)"', page)) == {GONE}
    assert set(re.findall(r'name="g" value="([^"]+)"', client.get("/catalog?own=1&q=Ушедшая").get_data(as_text=True))) == {GONE}
    staging.set_own_price(conn, "100", [first], None, NOW)                               # сброс в черновике: позиция ещё в фильтре, со сбросом
    page = client.get("/catalog?own=1").get_data(as_text=True)
    assert 'name="g" value="100"' in page and "Основной: сброс" in page
    assert len(set(re.findall(r'name="g" value="([^"]+)"', client.get("/catalog").get_data(as_text=True)))) > 3


# --- массовая правка ---------------------------------------------------------------------------------------------------------------

def test_the_first_press_shows_was_and_will_be_and_writes_nothing(tmp_path):
    app, conn, (first, second) = _app(tmp_path)
    client, tok = _owner(app)
    staging.set_own_price(conn, "100", [first], {"price": 7000}, NOW)
    before = _changes(conn)
    r = _preview(client, tok, ["100", "101"], [first, second], base="own", unit="pct", side="down", value="5")
    page = r.get_data(as_text=True).replace("\xa0", " ")
    assert r.status_code == 200 and _changes(conn) == before
    assert 'data-own-bulk-row="%d:100"' % first in page and "7 000 ₽" in page and "6 650 ₽" in page
    assert page.count("data-own-bulk-row=") == 1 and 'data-own-bulk-skip="no_own"' in page        # остальные три — без своей цены


def test_from_the_own_price_writes_a_fixed_price_per_account_and_skips_those_without_one(tmp_path):
    app, conn, (first, second) = _app(tmp_path)
    client, tok = _owner(app)
    staging.set_own_price(conn, "100", [first], {"price": 7000}, NOW)
    staging.set_own_price(conn, "100", [second], {"pct": 10}, NOW)
    r = _draft(client, tok, ["100", "101"], [first, second], base="own", unit="rub", side="up", value="300")
    assert _changes(conn) == {"%d:100" % first: {"price": 7300}, "%d:100" % second: {"price": 5800}}
    assert "нет своей цены на этом аккаунте: 2" in _flash(client, r)


def test_from_the_purchase_in_rubles_is_a_fixed_price_and_in_percent_stays_a_percent(tmp_path):
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    _draft(client, tok, ["100"], [first], base="purchase", unit="rub", side="down", value="7")
    assert _changes(conn) == {"%d:100" % first: {"price": 5000}}                           # 5 000 − 7 = 4 993 → вверх до 5 000
    _draft(client, tok, ["101"], [first], base="purchase", unit="pct", side="up", value="5")
    assert _changes(conn)["%d:101" % first] == {"pct": 5.0}


def test_a_draft_changed_after_the_preview_writes_nothing(tmp_path):
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    staging.set_own_price(conn, "100", [first], {"price": 7000}, NOW)
    r = _preview(client, tok, ["100"], [first], base="own", unit="pct", side="down", value="5")
    plan = re.search(r'name="plan" value="([0-9a-f]+)"', r.get_data(as_text=True)).group(1)
    staging.set_own_price(conn, "100", [first], {"price": 8000}, NOW)                      # другая вкладка
    r = _preview(client, tok, ["100"], [first], base="own", unit="pct", side="down", value="5", step="draft", plan=plan)
    assert _changes(conn) == {"%d:100" % first: {"price": 8000}}
    assert "посмотрите ещё раз" in _flash(client, r)


def test_reset_to_the_feed_settings_takes_the_own_price_off_also_of_a_position_gone(tmp_path):
    app, conn, (first, second) = _gone_app(tmp_path)
    client, tok = _owner(app)
    staging.set_own_price(conn, "100", [first, second], {"price": 7000}, NOW)
    staging.set_own_price(conn, GONE, [first], {"price": 4500}, NOW)
    r = _preview(client, tok, ["100", GONE], [first], action="clear")
    assert "Своя цена снята — цена по настройкам фида" in r.get_data(as_text=True)
    _draft(client, tok, ["100", GONE], [first], action="clear")
    assert _changes(conn) == {"%d:100" % second: {"price": 7000}}


def test_all_by_the_own_filter_takes_just_the_positions_with_an_own_price(tmp_path):
    app, conn, (first, _) = _gone_app(tmp_path)
    client, tok = _owner(app)
    staging.set_own_price(conn, "101", [first], {"price": 6000}, NOW)
    staging.set_own_price(conn, GONE, [first], {"price": 4500}, NOW)
    page = client.get("/catalog?own=1").get_data(as_text=True)
    fp = re.search(r'name="fp" value="([0-9a-f]+)"', page).group(1)
    _draft(client, tok, [], [first], all="1", filter="own=1", fp=fp, base="own", unit="rub", side="down", value="500")
    assert _changes(conn) == {"%d:101" % first: {"price": 5500}, "%d:%s" % (first, GONE): {"price": 4000}}


def test_a_gone_position_gets_a_percent_only_from_a_known_purchase(tmp_path):
    app, conn, (first, _) = _gone_app(tmp_path)
    client, tok = _owner(app)
    r = _draft(client, tok, [GONE, BLIND], [first], base="purchase", unit="pct", side="up", value="5")
    assert _changes(conn) == {"%d:%s" % (first, GONE): {"pct": 5.0}}
    assert "закупка неизвестна: 1" in _flash(client, r)
    _draft(client, tok, [BLIND], [first], base="purchase", unit="rub", side="up", value="500")
    assert "%d:%s" % (first, BLIND) not in _changes(conn)


@pytest.mark.parametrize("form", [
    dict(unit="rub", value="0"), dict(unit="rub", value="50001"), dict(unit="rub", value="10.5"), dict(unit="pct", value="51"),
    dict(base="feed"), dict(unit="usd"), dict(side=""),
])
def test_a_bad_press_changes_nothing(tmp_path, form):
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    staging.set_own_price(conn, "100", [first], {"price": 7000}, NOW)
    data = dict(dict(base="own", unit="pct", side="down", value="5"), **form)
    r = _draft(client, tok, ["100"], [first], **data)
    assert r.status_code == 302 and _changes(conn) == {"%d:100" % first: {"price": 7000}}


def test_the_limit_of_positions_stays(tmp_path, monkeypatch):
    from tires_sync.panel import routes_catalog
    monkeypatch.setattr(routes_catalog, "BULK_PRICE_LIMIT", 1)
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    r = _draft(client, tok, ["100", "101"], [first], base="purchase", unit="rub", side="up", value="100")
    assert staging.summary(conn)["draft_id"] is None and "Не больше 1 позиций" in _flash(client, r)


def test_the_preview_marks_what_needs_the_strong_confirmation_and_apply_asks_it(tmp_path):
    """Закупка 5 000, предел процента 15: «−40 % от своей» 7 000 → 4 200 (ниже закупки, −40 %), «+20 % от закупки» — процент выше предела."""
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    staging.set_own_price(conn, "100", [first], {"price": 7000}, NOW)
    confirm_draft(conn, staging.summary(conn)["draft_id"], NOW, "owner")
    page = _preview(client, tok, ["100"], [first], base="own", unit="pct", side="down", value="40").get_data(as_text=True)
    assert "data-own-bulk-risky" in page and "ниже закупки" in page and "−40 % к цене сейчас" in page.replace("-", "−")
    page = _preview(client, tok, ["101"], [first], base="purchase", unit="pct", side="up", value="20").get_data(as_text=True)
    assert "процент больше 15" in page
    _draft(client, tok, ["100"], [first], base="own", unit="pct", side="down", value="40")
    draft_id = staging.summary(conn)["draft_id"]
    assert "Подтверждаю значения вне диапазона" in client.get("/draft/%d" % draft_id).get_data(as_text=True)
    post(client, "/draft/%d/confirm" % draft_id, tok, rev=staging.rev(staging.summary(conn)["changes"]))
    assert active_settings(conn).price_overrides == {"%d:100" % first: ("price", 7000)}       # без усиленного — не применено


# --- позиция, которой нет у поставщика: карточка --------------------------------------------------------------------------------------

def test_the_card_of_a_gone_position_sets_a_price_always_and_a_percent_only_from_a_known_purchase(tmp_path):
    app, conn, (first, _) = _gone_app(tmp_path)
    client, tok = _owner(app)
    page = client.get("/catalog/item/%s" % GONE).get_data(as_text=True).replace("\xa0", " ")
    assert "Своя цена по аккаунтам" in page and "data-own-gone" in page and "от последней известной закупки 4 000 ₽" in page
    post(client, "/catalog/item/%s/own-price" % GONE, tok, kind="price", value="4500", a=[str(first)], action="save")
    post(client, "/catalog/item/%s/own-price" % GONE, tok, kind="pct", value="5", a=[str(first)], action="save")
    assert _changes(conn) == {"%d:%s" % (first, GONE): {"pct": 5.0}}
    assert "последняя закупка неизвестна" in client.get("/catalog/item/%s" % BLIND).get_data(as_text=True)
    r = post(client, "/catalog/item/%s/own-price" % BLIND, tok, kind="pct", value="5", a=[str(first)], action="save")
    assert "процент не от чего считать" in _flash(client, r) and "%d:%s" % (first, BLIND) not in _changes(conn)
    post(client, "/catalog/item/%s/own-price" % BLIND, tok, kind="price", value="3990", a=[str(first)], action="save")
    assert _changes(conn)["%d:%s" % (first, BLIND)] == {"price": 3990}
    r = post(client, "/catalog/item/nope/own-price", tok, kind="price", value="3990", a=[str(first)], action="save")
    assert "Такой позиции нет в выгрузке" in _flash(client, r)


def test_the_draft_counts_a_gone_position_from_its_last_purchase_and_asks_below_it(tmp_path):
    app, conn, (first, _) = _gone_app(tmp_path)
    client, tok = _owner(app)
    staging.set_own_price(conn, GONE, [first], {"price": 3500}, NOW)                      # ниже последней закупки 4 000
    draft_id = staging.summary(conn)["draft_id"]
    page = client.get("/draft/%d" % draft_id).get_data(as_text=True)
    assert "нет у поставщика, от последней закупки" in page and "3 500 ₽ — ниже закупки" in page
    assert "Подтверждаю значения вне диапазона" in page


# --- журнал ---------------------------------------------------------------------------------------------------------------------------

def test_apply_writes_was_and_will_be_of_the_own_prices_into_the_audit(tmp_path):
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    _draft(client, tok, ["100"], [first], base="purchase", unit="pct", side="up", value="5")
    draft_id = staging.summary(conn)["draft_id"]
    post(client, "/draft/%d/confirm" % draft_id, tok, rev=staging.rev(staging.summary(conn)["changes"]))
    assert active_settings(conn).price_overrides == {"%d:100" % first: ("pct", 5.0)}
    [detail] = [json.loads(d) for (d,) in conn.execute("SELECT detail FROM audit WHERE action = 'own_price_confirmed'")]
    assert detail["draft"] == draft_id and len(detail["lines"]) == 1 and "→ 5 250 ₽" in detail["lines"][0]


@pytest.mark.parametrize("role", ["partner", "viewer"])
def test_only_the_owner_previews_or_writes(tmp_path, monkeypatch, role):
    from tires_sync import users
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    app, conn, (first, _) = _app(tmp_path)
    users.add_user(conn, role + "1", "длинный-пароль-1", NOW, role=role)
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user=role + "1", epoch=store.get_session_epoch(conn, role + "1"), csrf="tok")
    for step in ("", "draft"):
        r = client.post("/catalog/own-price/bulk", data=dict(csrf="tok", g=["100"], a=[str(first)], base="purchase", unit="rub",
                                                               side="up", value="5", step=step, plan="0"))
        assert r.status_code in (302, 403) and "было → стало" not in r.get_data(as_text=True)
    assert staging.summary(conn)["draft_id"] is None


# --- тексты возврата (слово владельца 2026-11-03: понятно, ЧТО возвращается) -----------------------------------------------------------

def test_the_reset_button_says_the_price_comes_back_by_the_feed_settings_with_was_and_will_be(tmp_path):
    """Своя 7 000 ₽: в подсказке «было 7 000 ₽ → станет <цена по бренду>» — в карточке по аккаунтам, в ячейке каталога по первому."""
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    staging.set_own_price(conn, "100", [first], {"price": 7000}, NOW)
    card = client.get("/catalog/item/100").get_data(as_text=True).replace("\xa0", " ")
    button = re.search(r"<button[^>]*data-own-reset[^>]*>[^<]*</button>", card).group(0)
    assert ">Вернуть цену по настройкам фида</button>" in button and "К цене бренда" not in card
    assert "цена снова считается по % бренда и сезона" in button and "Основной: было 7 000 ₽ → станет" in button
    page = client.get("/catalog").get_data(as_text=True).replace("\xa0", " ")
    cell = re.search(r"<button[^>]*name=\"do\" value=\"clear\"[^>]*>[^<]*</button>", page).group(0)
    assert ">Вернуть цену по настройкам фида</button>" in cell and "было 7 000 ₽ → станет " in cell and "станет —" not in cell
    modal = re.search(r"<button[^>]*value=\"clear\" class=\"btn btn--secondary\"[^>]*>[^<]*</button>", page).group(0)
    assert ">Вернуть цену по настройкам фида</button>" in modal and "Сбросить" not in modal


def test_the_bulk_reset_shows_was_and_will_be_and_the_draft_and_audit_say_it_is_by_the_feed_settings(tmp_path):
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    staging.set_own_price(conn, "100", [first], {"price": 7000}, NOW)
    confirm_draft(conn, staging.summary(conn)["draft_id"], NOW, "owner")
    page = _preview(client, tok, ["100"], [first], action="clear").get_data(as_text=True).replace("\xa0", " ")
    row = re.search(r'<tr data-own-bulk-row="%d:100">.*?</tr>' % first, page, re.S).group(0)
    assert "7 000 ₽" in row and "по настройкам фида" in row and "Своя цена снята — цена по настройкам фида" in page
    r = _draft(client, tok, ["100"], [first], action="clear")
    assert "Своя цена снята — цена по настройкам фида в черновике: 1" in _flash(client, r)
    draft_id = staging.summary(conn)["draft_id"]
    draft = client.get("/draft/%d" % draft_id).get_data(as_text=True)
    assert "Своя цена снята — цена по настройкам фида · Основной" in draft and "Как у бренда" not in draft
    post(client, "/draft/%d/confirm" % draft_id, tok, rev=staging.rev(staging.summary(conn)["changes"]))
    [detail] = [json.loads(d) for (d,) in conn.execute("SELECT detail FROM audit WHERE action = 'own_price_confirmed'")]
    assert detail["lines"][0].startswith("Своя цена снята — цена по настройкам фида · Основной") and "7 000 ₽ →" in detail["lines"][0]


def test_the_card_reset_says_so_in_the_flash(tmp_path):
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    staging.set_own_price(conn, "100", [first], {"price": 7000}, NOW)
    r = post(client, "/catalog/item/100/own-price", tok, a=[str(first)], action="clear")
    assert "Своя цена снята — цена по настройкам фида — в черновике" in _flash(client, r)


def test_the_audit_page_reads_the_own_price_lines():
    from tires_sync.panel.texts import audit_text
    text = audit_text({"action": "own_price_confirmed", "detail": {"draft": 7, "lines": ["a: 1 → 2"] * 6, "more": 3}})
    assert text.startswith("Свои цены, черновик №7: a: 1 → 2;") and text.endswith("ещё 4")


# --- по ревью ---------------------------------------------------------------------------------------------------------------------------

def test_a_gone_position_is_ticked_only_under_the_own_filter_and_never_gets_a_manual_rule(tmp_path):
    app, conn, (first, _) = _gone_app(tmp_path)
    client, tok = _owner(app)
    staging.set_own_price(conn, GONE, [first], {"price": 4500}, NOW)
    assert 'name="g" value="%s"' % GONE not in client.get("/catalog").get_data(as_text=True)
    assert 'name="g" value="%s"' % GONE in client.get("/catalog?own=1").get_data(as_text=True)
    for mode in ("on", "off"):
        post(client, "/catalog/stage", tok, bulk=mode, g=["100", GONE])
        assert staging.summary(conn)["changes"].get("overrides") == {"1:100": mode}
    r = client.post("/catalog/stage", data=dict(csrf=tok, bulk="on", g=[GONE]), headers={"X-Requested-With": "fetch"})
    assert r.get_json()["ok"] is False and "ничего не записано" in r.get_json()["message"]


def test_a_position_shown_for_viewing_only_gets_an_own_price_in_rubles_and_a_percent_of_a_known_purchase(tmp_path, monkeypatch):
    """Владелец 09.11: «пусть работает во всех» — старая вторая выгрузка держит позицию только для просмотра, своя цена ей ставится."""
    from tires_sync.panel import routes_catalog
    app, conn, (first, _) = _gone_app(tmp_path)
    client, tok = _owner(app)
    held, blind = catalog_archive.gone_offer(conn, GONE), catalog_archive.gone_offer(conn, BLIND)
    monkeypatch.setattr(routes_catalog, "_held_offers", lambda *a: [(held, datetime(2026, 10, 1, 10)), (blind, datetime(2026, 10, 1, 10))])
    page = client.get("/catalog/item/%s" % GONE).get_data(as_text=True).replace("\xa0", " ")
    assert "data-own-view" in page and "data-own-gone" not in page and "от закупки 4 000 ₽" in page
    r = post(client, "/catalog/item/%s/own-price" % GONE, tok, kind="price", value="4500", a=[str(first)], action="save")
    assert "показана только для просмотра: цена начнёт действовать" in _flash(client, r)
    assert _changes(conn) == {"%d:%s" % (first, GONE): {"price": 4500}}
    r = post(client, "/catalog/item/%s/own-price" % BLIND, tok, kind="pct", value="5", a=[str(first)], action="save")
    assert "процент не от чего считать" in _flash(client, r) and "%d:%s" % (first, BLIND) not in _changes(conn)
    _draft(client, tok, [GONE, BLIND], [first], base="purchase", unit="rub", side="up", value="100", redo="1")
    assert _changes(conn) == {"%d:%s" % (first, GONE): {"price": 4100}}       # закупка неизвестна — «от закупки» пропущено


def test_from_the_purchase_counts_from_the_anchor_and_below_is_said_as_apply_asks_it(tmp_path, monkeypatch):
    """Поставщик снизил 5 000 → держим якорь 6 000 (price_guard): «−500 ₽ от закупки» = 5 500, не ниже цены поставщика — не «ниже закупки»."""
    from tires_sync import price_guard
    monkeypatch.setattr(price_guard, "base_for", lambda guard, gid, price, inherited=None: (6000.0, "held") if gid == "100" else (price, ""))
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    page = _preview(client, tok, ["100"], [first], base="purchase", unit="rub", side="down", value="500").get_data(as_text=True)
    row = re.search(r'<tr data-own-bulk-row="%d:100">.*?</tr>' % first, page.replace("\xa0", " "), re.S).group(0)
    assert "6 000 ₽" in row and "5 500 ₽" in row and "ниже закупки" not in row
    _draft(client, tok, ["100"], [first], base="purchase", unit="rub", side="down", value="500")
    assert _changes(conn) == {"%d:100" % first: {"price": 5500}}


def test_the_rub_limit_itself_is_accepted_and_a_live_position_without_a_purchase_gets_no_percent():
    assert own_bulk.plan({}, {"a": 5000.0}, [1], {"base": "purchase", "unit": "rub", "delta": 50000})["set"] == {"1:a": {"price": 55000}}
    assert own_bulk.plan({}, {"a": 0.0}, [1], {"base": "purchase", "unit": "pct", "delta": 5})["skipped"] == {"no_purchase": ["1:a"]}
