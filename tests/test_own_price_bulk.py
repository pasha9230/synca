"""Массовая своя цена из каталога (решение владельца 2026-10-30): галочками или всё по фильтру — одна наценка или уценка в % к закупке
(`{"pct": ±x}`, как своя цена позиции), на отмеченные аккаунты с фидом, через черновик и «Применить». Позиции, у которых своя цена уже
есть, по умолчанию не трогаются. Не больше 1000 позиций и 50 % за раз; ниже закупки или дальше 30 % от цены фида — усиленное подтверждение."""
from __future__ import annotations

import re

import pytest

from panel_helpers import login, post
from test_own_price import NOW, _app
from tires_sync import staging, store
from tires_sync.settings_store import active_settings

ALL = ["100", "101", "102", "103"]


def _owner(app):
    client = app.test_client()
    return client, login(client)


def _bulk(client, tok, goods, accounts, **form):
    data = dict(dict(side="up", value="5"), **form)
    return post(client, "/catalog/own-price/bulk", tok, g=list(goods), a=[str(a) for a in accounts], **data)


def _changes(conn):
    return staging.summary(conn)["changes"].get("price_overrides")


# --- запись в черновик ---------------------------------------------------------------------------------------------------------------

def test_a_markup_of_the_ticked_positions_goes_into_the_draft_as_a_percent_to_the_purchase(tmp_path):
    app, conn, (first, second) = _app(tmp_path)
    client, tok = _owner(app)
    r = _bulk(client, tok, ["100", "101"], [first, second], side="up", value="5")
    assert r.status_code == 302
    assert _changes(conn) == {"%d:%s" % (a, g): {"pct": 5.0} for a in (first, second) for g in ("100", "101")}
    assert staging.summary(conn)["count"] == 4                                                      # позиции × аккаунты


def test_a_markdown_is_a_negative_percent_to_the_purchase_and_a_comma_is_a_point(tmp_path):
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    _bulk(client, tok, ["100"], [first], side="down", value="7,5")
    assert _changes(conn) == {"%d:100" % first: {"pct": -7.5}}


def test_a_position_with_its_own_price_is_kept_unless_asked(tmp_path):
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    staging.set_own_price(conn, "101", [first], {"price": 9000}, NOW)
    _bulk(client, tok, ["100", "101"], [first], value="5")
    assert _changes(conn) == {"%d:100" % first: {"pct": 5.0}, "%d:101" % first: {"price": 9000}}
    _bulk(client, tok, ["100", "101"], [first], value="6", redo="1")
    assert _changes(conn) == {"%d:100" % first: {"pct": 6.0}, "%d:101" % first: {"pct": 6.0}}


def test_an_own_price_only_in_the_draft_counts_as_already_set(tmp_path):
    """Kept means kept as it stands now, applied or in the draft: one press never undoes another's own price."""
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    _bulk(client, tok, ["100"], [first], value="5")
    _bulk(client, tok, ["100", "101"], [first], value="9")
    assert _changes(conn) == {"%d:100" % first: {"pct": 5.0}, "%d:101" % first: {"pct": 9.0}}


def test_take_off_the_own_price_of_the_ticked_positions(tmp_path):
    app, conn, (first, second) = _app(tmp_path)
    client, tok = _owner(app)
    staging.set_own_price(conn, "100", [first, second], {"pct": 5}, NOW)
    post(client, "/catalog/own-price/bulk", tok, g=["100"], a=[str(first)], action="clear")
    assert _changes(conn) == {"%d:100" % second: {"pct": 5}}


def test_a_position_gone_from_the_unload_is_skipped_and_said(tmp_path):
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    r = _bulk(client, tok, ["100", "nope"], [first])
    assert _changes(conn) == {"%d:100" % first: {"pct": 5.0}}
    page = client.get(r.headers["Location"]).get_data(as_text=True)
    assert "только для просмотра): 1" in page


@pytest.mark.parametrize("form", [
    dict(value="0"), dict(value="51"), dict(value="-5"), dict(value="abc"), dict(value=""), dict(side="sideways"),
    dict(accounts=[]), dict(accounts=[99]), dict(goods=[]),
])
def test_a_bad_form_changes_nothing(tmp_path, form):
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    goods, accounts = form.pop("goods", ["100"]), form.pop("accounts", [first])
    r = _bulk(client, tok, goods, accounts, **form)
    assert r.status_code == 302 and staging.summary(conn)["draft_id"] is None


def test_more_than_the_limit_is_refused_whole(tmp_path, monkeypatch):
    from tires_sync.panel import routes_catalog
    monkeypatch.setattr(routes_catalog, "BULK_PRICE_LIMIT", 2)
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    r = _bulk(client, tok, ["100", "101", "102"], [first])
    assert staging.summary(conn)["draft_id"] is None
    assert "Не больше 2 позиций" in client.get(r.headers["Location"]).get_data(as_text=True)


def test_all_by_the_filter_takes_what_the_filter_finds_and_refuses_when_it_changed(tmp_path):
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    page = client.get("/catalog?q=Test").get_data(as_text=True)
    total = int(re.search(r'name="total" value="(\d+)"', page).group(1))
    fp = re.search(r'name="fp" value="([0-9a-f]+)"', page).group(1)
    assert re.search(r'name="filter" value="q=Test"', page) and total >= 1
    _bulk(client, tok, [], [first], all="1", filter="q=Test", total=str(total), fp="0" * len(fp))      # the filter finds other positions now
    assert staging.summary(conn)["draft_id"] is None
    _bulk(client, tok, [], [first], all="1", filter="q=Test", total=str(total), fp=fp)
    assert len(_changes(conn)) == total and all(k.startswith("%d:" % first) for k in _changes(conn))


# --- «было → стало» и подтверждение -----------------------------------------------------------------------------------------------

def test_the_draft_says_the_percent_is_to_the_purchase_and_shows_the_price(tmp_path):
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    _bulk(client, tok, ["100"], [first], value="5")
    draft = client.get("/draft/%d" % staging.summary(conn)["draft_id"]).get_data(as_text=True)
    assert "+5 % к закупке → 5 250 ₽" in draft                                                        # закупка 5 000


def test_many_positions_get_a_summary_and_a_table_with_the_risky_ones_first(tmp_path):
    app, conn, (first, second) = _app(tmp_path)
    client, tok = _owner(app)
    _bulk(client, tok, ALL, [first, second], side="down", value="10")
    draft = client.get("/draft/%d" % staging.summary(conn)["draft_id"]).get_data(as_text=True)
    assert "data-own-summary" in draft and "−10 % к закупке" in draft
    assert "ниже закупки: 8" in draft and "Подтверждаю значения вне диапазона" in draft
    assert draft.count("data-own-row") == 8


def test_a_percent_below_the_purchase_or_far_from_the_feed_needs_the_strong_confirmation(tmp_path):
    """Purchase 5 000, the feed's price by the brand 4 810: −10 % → 4 500 (below the purchase), +40 % → 7 000 (+46 %), +5 % → 5 250 (+9 %)."""
    for value, side, asked in (("10", "down", True), ("40", "up", True), ("5", "up", False)):
        (tmp_path / (side + value)).mkdir()
        app, conn, (first, _) = _app(tmp_path / (side + value))
        client, tok = _owner(app)
        _bulk(client, tok, ["100"], [first], side=side, value=value)
        draft_id = staging.summary(conn)["draft_id"]
        page = client.get("/draft/%d" % draft_id).get_data(as_text=True)
        assert ("Подтверждаю значения вне диапазона" in page) == asked, (side, value)
        post(client, "/draft/%d/confirm" % draft_id, tok)
        assert (active_settings(conn).price_overrides == {}) == asked, (side, value)


# --- каталог и права -------------------------------------------------------------------------------------------------------------

def test_the_catalog_offers_the_bulk_price_to_the_owner_and_says_it_is_to_the_purchase(tmp_path):
    app, conn, _ = _app(tmp_path)
    client, _ = _owner(app)
    page = client.get("/catalog").get_data(as_text=True)
    assert "Своя цена выбранным" in page and 'formaction="/catalog/own-price/bulk"' in page and "% к закупке" in page


@pytest.mark.parametrize("role", ["partner", "chats", "viewer", "point", "client"])
def test_only_the_owner_may_set_a_bulk_price(tmp_path, monkeypatch, role):
    from tires_sync import order_points as points, users
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    app, conn, (first, _) = _app(tmp_path)
    pid = points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk"}) if role == "point" else None
    users.add_user(conn, role + "1", "длинный-пароль-1", NOW, role=role, point_id=pid)
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user=role + "1", epoch=store.get_session_epoch(conn, role + "1"), csrf="tok")
    r = client.post("/catalog/own-price/bulk", data=dict(csrf="tok", g=["100"], a=[str(first)], side="up", value="5"))
    assert r.status_code in (302, 403) and staging.summary(conn)["draft_id"] is None
    assert "Своя цена выбранным" not in client.get("/catalog").get_data(as_text=True)


def test_enter_in_the_percent_field_puts_the_price_never_takes_the_positions_off(tmp_path):
    """Enter in a field presses the first button of its form: in «bulk» it must be «В черновик» of the price, not «Снять выбранные»."""
    app, conn, _ = _app(tmp_path)
    client, _ = _owner(app)
    page = client.get("/catalog").get_data(as_text=True)
    form = page[page.index('<form id="bulk"'):]
    first = re.search(r"<button(?![^>]*type=\"button\")[^>]*>", form).group(0)
    assert 'formaction="/catalog/own-price/bulk"' in first and 'value="save"' in first


def test_many_risky_rows_are_counted_above_not_listed(tmp_path):
    app, conn, (first, second) = _app(tmp_path)
    client, tok = _owner(app)
    _bulk(client, tok, ALL, [first, second], side="down", value="10")
    draft = client.get("/draft/%d" % staging.summary(conn)["draft_id"]).get_data(as_text=True)
    plaque = draft.split("data-own-summary")[0]
    assert "8 строк" in plaque and "закупка 5 000 ₽" not in plaque                                   # the rows are in the table


def test_nothing_changed_is_said_so(tmp_path):
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    staging.set_own_price(conn, "100", [first], {"price": 9000}, NOW)
    r = _bulk(client, tok, ["100"], [first], value="5")
    assert "Ничего не изменилось" in client.get(r.headers["Location"]).get_data(as_text=True)
