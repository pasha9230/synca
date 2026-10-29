"""Своя цена позиции (решение владельца 2026-10-29): % к закупке или готовая цена, на каждый аккаунт отдельно, через черновик настроек."""
from __future__ import annotations

import dataclasses
import json
from datetime import date

import pytest

from helpers import catalog_xml, commodity_xml, make_offer, make_settings, settings_dict
from panel_helpers import login, make_app, post
from tires_sync import connections, cycle, mirror_feed, own_price, staging, store
from tires_sync.catalog import parse_reference
from tires_sync.config import settings_from_dict
from tires_sync.pricing import position_price
from tires_sync.settings_store import PercentLimitError, active_settings, confirm_draft, get_active, init_settings, rollback

NOW = "2026-10-29T10:00:00"
REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')


def _db(tmp_path, accounts=("Основной", "Шина-Колесо")):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    ids = [connections.create_account(conn, name, NOW, "owner") for name in accounts]
    return conn, ids


def _confirm(conn, strong=False):
    return confirm_draft(conn, staging.summary(conn)["draft_id"], NOW, "owner", strong=strong)


# --- настройки и расчёт ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    {"1:100": {}}, {"1:100": {"pct": 1, "price": 100}}, {"1:100": {"pct": "5"}}, {"1:100": {"pct": float("nan")}},
    {"1:100": {"pct": 301}}, {"1:100": {"pct": -51}}, {"1:100": {"price": 0}}, {"1:100": {"price": -5}}, {"1:100": {"price": 10.5}},
    {"1:100": {"price": True}}, {"1:100": {"cost": 5}}, {"100": {"price": 100}}, {"0:100": {"price": 100}}, {"x:100": {"price": 100}},
    ["1:100"],
])
def test_bad_own_prices_are_refused(bad):
    with pytest.raises(ValueError):
        settings_from_dict(settings_dict(price_overrides=bad))


def test_old_settings_without_the_key_have_no_own_prices():
    s = make_settings()
    assert s.price_overrides == {} and s.own_prices == {}


def test_position_price_own_percent_and_fixed_price_go_up_to_tens_brand_otherwise():
    offer = make_offer(base_price=5000.0)
    s = make_settings()
    assert position_price(offer, s) == 4810                                                            # −3,82 % бренда
    assert position_price(offer, dataclasses.replace(s, own_prices={"100": ("pct", 10.0)})) == 5500
    assert position_price(offer, dataclasses.replace(s, own_prices={"100": ("pct", 10.01)})) == 5510   # вверх до десятков
    assert position_price(offer, dataclasses.replace(s, own_prices={"100": ("price", 6995)})) == 7000
    assert position_price(offer, dataclasses.replace(s, own_prices={"100": ("price", 6990)})) == 6990
    assert position_price(offer, dataclasses.replace(s, own_prices={"999": ("price", 1)})) == 4810     # чужая позиция не влияет


def test_general_settings_never_apply_an_own_price_only_the_accounts_effective_ones_do(tmp_path):
    conn, (first, second) = _db(tmp_path)
    general = settings_from_dict(settings_dict(price_overrides={"%d:100" % first: {"price": 9000}}))
    assert general.own_prices == {}
    assert connections.effective_settings(conn, first, general).own_prices == {"100": ("price", 9000)}
    assert connections.effective_settings(conn, second, general).own_prices == {}


def test_own_percent_replaces_the_brand_percent_and_the_account_corrections(tmp_path):
    conn, (first, _) = _db(tmp_path)
    connections.set_account_pricing(conn, first, 3.0, None, NOW, "owner")
    connections.set_brand_deltas(conn, first, {"IKON TYRES": 2.0}, NOW, "owner")
    general = settings_from_dict(settings_dict(price_overrides={"%d:100" % first: {"pct": 10}}))
    assert position_price(make_offer(), connections.effective_settings(conn, first, general)) == 5500


# --- фиды ----------------------------------------------------------------------------------------------------------------------

def _feed_text(tmp_path, conn, account):
    token = connections.get_account(conn, account)["feed_token"]
    return (tmp_path / "feed" / token / "next" / "avito_feed.xml").read_text(encoding="utf-8")


def test_own_price_goes_only_into_the_feed_of_its_account_and_the_pack_costs_four(tmp_path):
    conn, (first, second) = _db(tmp_path)
    staging.set_own_price(conn, "100", [second], {"price": 6995}, NOW)
    _confirm(conn)
    raw = get_active(conn)[1]                                                                          # «за 4 шт.» для всех позиций
    vid = conn.execute("INSERT INTO settings_version (created, author, note, json) VALUES (?,?,?,?)",
                       (NOW, "t", "pack4", json.dumps({**raw, "pack4_rules": ["*|*|*"]}))).lastrowid
    conn.execute("UPDATE settings_active SET version_id = ? WHERE id = 1", (vid,))
    conn.commit()
    cycle.run_for_accounts(conn, catalog_xml(commodity_xml(NREST="10")), REF, date(2026, 10, 29), tmp_path / "feed")
    one, two = _feed_text(tmp_path, conn, first), _feed_text(tmp_path, conn, second)
    assert "<Price>4810</Price>" in one and "<Price>19240</Price>" in one and "<Price>7000</Price>" not in one
    assert "<Price>7000</Price>" in two and "<Price>28000</Price>" in two and "<Price>4810</Price>" not in two


def test_mirror_ads_take_the_own_price_of_their_account_a_pack_its_count(tmp_path):
    conn, (first, second) = _db(tmp_path)
    ads = "".join('<Ad><Id>%s</Id><Price>1</Price><Title>T</Title><Description>D</Description></Ad>' % i for i in ("100_1", "100-x4", "101_1"))
    mirror_feed.import_feed(conn, second, ('<Ads formatVersion="3" target="Avito.ru">%s</Ads>' % ads).encode(), NOW)
    general = settings_from_dict(settings_dict(price_overrides={"%d:100" % second: {"pct": 10}, "%d:101" % first: {"price": 100}}))
    offers = {"100": make_offer(), "101": make_offer(goods_id="101", code="TS2")}
    prices = mirror_feed.prices_for(conn, second, offers, connections.effective_settings(conn, second, general))
    assert prices == {"100_1": 5500, "100-x4": 22000, "101_1": 4810}                                   # 101: своя цена другого аккаунта


# --- черновик, «Применить», откат ----------------------------------------------------------------------------------------------

def test_an_own_price_waits_in_the_draft_and_counts_once_per_account_until_applied(tmp_path):
    conn, (first, second) = _db(tmp_path)
    staging.set_own_price(conn, "100", [first, second], {"pct": 5}, NOW)
    assert staging.summary(conn)["count"] == 2
    assert active_settings(conn).price_overrides == {}                                                 # фид ещё не знает
    vid = _confirm(conn)
    assert active_settings(conn).price_overrides == {"%d:100" % first: ("pct", 5.0), "%d:100" % second: ("pct", 5.0)}
    staging.set_own_price(conn, "100", [second], None, NOW)                                            # «Как у бренда»
    _confirm(conn)
    assert set(active_settings(conn).price_overrides) == {"%d:100" % first}
    rollback(conn, vid - 1, NOW, "owner")                                                              # версия до своей цены
    assert active_settings(conn).price_overrides == {}


def test_taking_off_a_price_that_is_only_in_the_draft_cancels_the_draft(tmp_path):
    conn, (first, _) = _db(tmp_path)
    staging.set_own_price(conn, "100", [first], {"price": 5000}, NOW)
    staging.set_own_price(conn, "100", [first], None, NOW)
    assert staging.summary(conn)["draft_id"] is None


@pytest.mark.parametrize("value", [{"pct": "x"}, {"price": 0}, {"pct": 1, "price": 2}, {}])
def test_a_bad_value_leaves_the_draft_untouched(tmp_path, value):
    conn, (first, _) = _db(tmp_path)
    with pytest.raises(ValueError):
        staging.set_own_price(conn, "100", [first], value, NOW)
    with pytest.raises(ValueError):
        staging.set_own_price(conn, "100", [], {"pct": 1}, NOW)
    assert staging.summary(conn)["draft_id"] is None


def test_an_own_percent_beyond_the_limit_needs_the_strong_confirmation(tmp_path):
    conn, (first, _) = _db(tmp_path)
    staging.set_own_price(conn, "100", [first], {"pct": 20}, NOW)                                     # pct_limit 15
    with pytest.raises(PercentLimitError):
        _confirm(conn)
    _confirm(conn, strong=True)
    staging.set_own_price(conn, "100", [first], {"price": 100000}, NOW)                               # готовая цена — не процент
    _confirm(conn)


# --- ниже закупки ---------------------------------------------------------------------------------------------------------------

def test_a_fixed_price_below_the_purchase_is_told_once_and_again_only_after_a_change(tmp_path):
    conn, (first, second) = _db(tmp_path)
    general = settings_from_dict(settings_dict(price_overrides={"%d:100" % first: {"price": 4000}, "%d:101" % second: {"pct": -20}}))
    offers = {"100": make_offer(), "101": make_offer(goods_id="101", code="TS2")}
    lines, keep = own_price.fresh_events(conn, general, offers)
    assert len(lines) == 1 and "Основной" in lines[0] and "4 000 ₽" in lines[0] and "5 000 ₽" in lines[0]    # процент — не событие
    own_price.remember(conn, keep)
    assert own_price.fresh_events(conn, general, offers)[0] == []                                      # следующий цикл молчит
    offers["100"] = make_offer(base_price=6000.0)
    assert own_price.fresh_events(conn, general, offers)[0] == []                                      # закупка выросла ещё: то же событие
    changed = settings_from_dict(settings_dict(price_overrides={"%d:100" % first: {"price": 4500}}))
    assert len(own_price.fresh_events(conn, changed, offers)[0]) == 1                                  # цену поправили — снова
    above = {"100": make_offer(base_price=3000.0)}
    own_price.remember(conn, own_price.fresh_events(conn, general, above)[1])                          # вернулась выше закупки
    assert len(own_price.fresh_events(conn, general, offers)[0]) == 1                                  # и снова ниже — новое событие


# --- панель ---------------------------------------------------------------------------------------------------------------------

def _app(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    ids = [connections.create_account(conn, name, NOW, "owner") for name in ("Основной", "Шина-Колесо")]
    return app, conn, ids


def test_the_owner_puts_an_own_price_into_the_draft_from_the_card_and_sees_was_and_will_be(tmp_path):
    app, conn, (first, second) = _app(tmp_path)
    client = app.test_client()
    tok = login(client)
    page = client.get("/catalog/item/100").get_data(as_text=True)
    assert "Своя цена по аккаунтам" in page and 'action="/catalog/item/100/own-price"' in page and "Шина-Колесо" in page
    r = post(client, "/catalog/item/100/own-price", tok, kind="price", value="6 995", a=[str(second)], action="save")
    assert r.status_code == 302
    assert staging.summary(conn)["changes"]["price_overrides"] == {"%d:100" % second: {"price": 6995}}
    page = client.get("/catalog/item/100").get_data(as_text=True)
    assert "цена 6 995 ₽" in page and "не применено" in page
    draft = client.get("/draft/%d" % staging.summary(conn)["draft_id"]).get_data(as_text=True)
    assert "Своя цена · Шина-Колесо" in draft and "→ 7 000 ₽" in draft
    r = post(client, "/catalog/item/100/own-price", tok, a=[str(second)], action="clear")
    assert staging.summary(conn)["draft_id"] is None


@pytest.mark.parametrize("form", [
    dict(kind="price", value="abc", a=["1"]), dict(kind="pct", value="", a=["1"]), dict(kind="pct", value="5", a=[]),
    dict(kind="pct", value="5", a=["99"]), dict(kind="weird", value="5", a=["1"]), dict(kind="price", value="10.5", a=["1"]),
])
def test_a_bad_form_changes_nothing(tmp_path, form):
    app, conn, _ = _app(tmp_path)
    client = app.test_client()
    tok = login(client)
    r = post(client, "/catalog/item/100/own-price", tok, action="save", **form)
    assert r.status_code == 302 and staging.summary(conn)["draft_id"] is None


def test_an_unknown_position_is_refused(tmp_path):
    app, conn, (first, _) = _app(tmp_path)
    client = app.test_client()
    tok = login(client)
    post(client, "/catalog/item/nope/own-price", tok, kind="pct", value="5", a=[str(first)], action="save")
    assert staging.summary(conn)["draft_id"] is None


@pytest.mark.parametrize("role", ["partner", "chats", "viewer", "point", "client"])
def test_only_the_owner_may_set_an_own_price(tmp_path, monkeypatch, role):
    from tires_sync import order_points as points, users
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    app, conn, (first, _) = _app(tmp_path)
    pid = points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk"}) if role == "point" else None
    users.add_user(conn, role + "1", "длинный-пароль-1", NOW, role=role, point_id=pid)
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user=role + "1", epoch=store.get_session_epoch(conn, role + "1"), csrf="tok")
    r = client.post("/catalog/item/100/own-price", data=dict(csrf="tok", kind="pct", value="5", a=[str(first)], action="save"))
    assert r.status_code in (302, 403) and staging.summary(conn)["draft_id"] is None
    if r.status_code == 302:
        assert "/catalog/item/" not in r.headers["Location"]                                          # отправлен прочь, не «сохранено»


def test_the_cycle_tells_the_owner_once_in_the_journal(tmp_path):
    from tires_sync import cli
    conn, (first, _) = _db(tmp_path)
    general = settings_from_dict(settings_dict(price_overrides={"%d:100" % first: {"price": 4000}}))
    for _ in range(2):
        cli._alert_own_below_cost(conn, None, {"100": make_offer()}, general)
    told = [e for e in store.list_audit(conn, 50) if "своя цена ниже закупки" in str(e.get("detail"))]
    assert len(told) == 1


def test_the_client_catalog_never_takes_the_owners_own_prices():
    """The client's catalog prices from the purchase by the client's own terms (the lead 2026-10-29): it must not read price_overrides."""
    from pathlib import Path
    source = Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "client_catalog.py"
    if not source.exists():
        pytest.skip("клиентского каталога ещё нет в этой ветке")
    text = source.read_text(encoding="utf-8")
    assert "position_price" not in text and "own_prices" not in text and "price_overrides" not in text


def test_an_own_price_of_the_owner_never_changes_the_price_in_the_clients_shop(tmp_path):
    """The client's catalog (/shop) prices from the purchase by the client's terms (the lead 2026-10-29): the owner's own price stays out."""
    from test_shop import make_shop, page, prices
    app, client, conn, _ = make_shop(tmp_path)
    accounts = [connections.create_account(conn, name, NOW, "owner") for name in ("Основной", "Шина-Колесо")]
    before = prices(page(client))
    assert before
    staging.set_own_price(conn, "77001", accounts, {"price": 999}, NOW)
    staging.set_own_price(conn, "77002", accounts, {"pct": -40}, NOW)
    _confirm(conn, strong=True)
    assert set(active_settings(conn).price_overrides) == {"%d:%s" % (a, g) for a in accounts for g in ("77001", "77002")}
    assert prices(page(client)) == before


def test_a_broken_below_cost_check_never_stops_the_cycle(tmp_path, monkeypatch, capsys):
    from tires_sync import cli
    conn, _ = _db(tmp_path)

    def broken(*args):
        raise RuntimeError("boom")
    monkeypatch.setattr(own_price, "fresh_events", broken)
    cli._alert_own_below_cost(conn, None, {}, make_settings())                                        # no exception: publishing goes on
    assert "boom" in capsys.readouterr().err
    monkeypatch.undo()
    monkeypatch.setattr(own_price, "remember", broken)                                                # a busy base when the marks are kept
    cli._alert_own_below_cost(conn, None, {})                                                          # the settings read inside too
    assert "boom" in capsys.readouterr().err


def test_a_position_away_from_the_unload_for_a_while_is_not_told_again(tmp_path):
    conn, (first, _) = _db(tmp_path)
    general = settings_from_dict(settings_dict(price_overrides={"%d:100" % first: {"price": 4000}}))
    offers = {"100": make_offer()}
    own_price.remember(conn, own_price.fresh_events(conn, general, offers)[1])
    own_price.remember(conn, own_price.fresh_events(conn, general, {})[1])                            # ушла из выгрузки на цикл
    assert own_price.fresh_events(conn, general, offers)[0] == []                                      # вернулась той же — молчим


def test_the_catalog_marks_an_own_price_set_on_any_account(tmp_path):
    app, conn, (first, second) = _app(tmp_path)
    client = app.test_client()
    tok = login(client)
    post(client, "/catalog/item/100/own-price", tok, kind="pct", value="5", a=[str(second)], action="save")
    assert "своя цена</span>" in client.get("/catalog").get_data(as_text=True)


def _same_tire_ad(app, conn, ad_id):
    from tires_sync import same_tire
    from tires_sync.panel import service
    from tires_sync.panel.data import load_merged, load_reference
    settings = active_settings(conn)
    parsed = load_merged(conn, settings, app.config["UNLOAD_PATH"])
    key = same_tire.identity(service._unbuilt_candidate(conn, settings, load_reference(app.config["REFERENCE_PATH"]), parsed.offers["100"])[0])
    tags = ("Brand", "Model", "TireSectionWidth", "TireAspectRatio", "RimDiameter", "TireType", "LoadIndex", "SpeedIndex")
    return "<Ad><Id>%s</Id>%s<RunFlat>Нет</RunFlat></Ad>" % (ad_id, "".join("<%s>%s</%s>" % (t, v, t) for t, v in zip(tags, key)))


def _publish(conn, account, goods_id, ad):
    conn.execute("INSERT INTO published_state (account_id, goods_id, price, base_price, point, snapshot) VALUES (?,?,?,?,?,?)",
                 (account, goods_id, 5000, 4000.0, "P1", ad))
    conn.commit()


def _card(app):
    client = app.test_client()
    login(client)
    return client.get("/catalog/item/100").get_data(as_text=True)


def test_the_card_says_when_another_lot_of_the_same_tire_carries_the_ad(tmp_path):
    app, conn, (first, second) = _app(tmp_path)
    _publish(conn, second, "101", _same_tire_ad(app, conn, "101"))                                    # 101 в выгрузке, та же шина
    page = _card(app)
    assert page.count("Объявление держит другая партия") == 1 and "Шина-Колесо: объявление держит другая партия" in page
    assert 'href="/catalog/item/101"' in page
    _publish(conn, second, "100", _same_tire_ad(app, conn, "100"))
    assert "держит другая партия" not in _card(app)                                                    # своё объявление есть — пометки нет


def test_a_holder_gone_from_the_unload_is_no_holder(tmp_path):
    app, conn, (first, second) = _app(tmp_path)
    _publish(conn, second, "GONE1", _same_tire_ad(app, conn, "GONE1"))                                # удерживаемая, нет в выгрузке
    assert "держит другая партия" not in _card(app)


def test_a_mirrored_ad_counts_only_while_the_account_is_a_mirror(tmp_path):
    app, conn, (first, second) = _app(tmp_path)
    ad = _same_tire_ad(app, conn, "101_1")
    mirror_feed.import_feed(conn, second, ('<Ads formatVersion="3" target="Avito.ru">%s</Ads>' % ad).encode(), NOW)
    assert "Шина-Колесо: объявление держит другая партия" in _card(app)
    conn.execute("UPDATE account SET feed_mode = 'generated' WHERE id = ?", (second,))                # перевод на нашу сборку: файл остался в базе
    conn.commit()
    assert "держит другая партия" not in _card(app)


@pytest.mark.parametrize("value, asked", [("4000", True), ("6990", True), ("3000", True), ("5200", False), ("6200", False)])
def test_a_fixed_price_below_the_purchase_or_far_from_the_feed_is_asked_once_more(tmp_path, value, asked):
    """Purchase 5 000, feed price by the brand 4 810: 4 000 is below the purchase, 6 990 is +45 %, 3 000 both; 5 200 and 6 200 neither."""
    app, conn, (first, _) = _app(tmp_path)
    client = app.test_client()
    tok = login(client)
    post(client, "/catalog/item/100/own-price", tok, kind="price", value=value, a=[str(first)], action="save")
    draft_id = staging.summary(conn)["draft_id"]
    page = client.get("/draft/%d" % draft_id).get_data(as_text=True)
    assert ("Подтверждаю значения вне диапазона" in page) == asked
    if asked:
        assert "закупка 5 000 ₽" in page and "было 4 810 ₽" in page
    post(client, "/draft/%d/confirm" % draft_id, tok)                                                  # без отметки
    assert (active_settings(conn).price_overrides == {}) == asked
    if asked:
        post(client, "/draft/%d/confirm" % draft_id, tok, strong="on")
        assert active_settings(conn).price_overrides == {"%d:100" % first: ("price", int(value))}


def test_the_feeds_price_is_the_published_one_when_there_is_one(tmp_path):
    from tires_sync.panel import service
    app, conn, (first, _) = _app(tmp_path)
    conn.execute("INSERT INTO published_state (account_id, goods_id, price, base_price, point, snapshot) VALUES (?,?,?,?,?,?)",
                 (first, "100", 9000, 5000.0, "P1", "<Ad/>"))
    conn.commit()
    raw = get_active(conn)[1]
    offers = {"100": make_offer()}
    changes = {"price_overrides": {"%d:100" % first: {"price": 6990}}}
    lines = service.own_price_risky(conn, raw, changes, offers, settings_from_dict(raw))
    assert lines == []                                                                                  # 6 990 к 9 000 — −22 %
