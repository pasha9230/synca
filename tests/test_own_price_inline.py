"""Своя цена прямо в графе «Наша цена» каталога (решение владельца 2026-10-30: «лучше давай будем менять базовую цену»): клик по цене →
поле «6990» (₽) или «+5 %» / «−5 %» (к закупке); одна правка на все наши аккаунты с фидом: первый (цена графы) получает её как есть,
остальные — со своей поправкой относительно первого (п. п. аккаунта + наценка бренда аккаунта), как процент бренда. В тот же черновик, что
карточка; «Сбросить» на всех; метка «своя»; ниже закупки или дальше 30 % от цены фида — усиленное подтверждение при «Применить».
Только владелец."""
from __future__ import annotations

import re

import pytest

from panel_helpers import login, post
from test_own_price import NOW, _app
from tires_sync import connections, staging, store
from tires_sync.settings_store import active_settings, confirm_draft

ROW_G = '<input type="hidden" name="g" value="100">'


def _owner(app):
    client = app.test_client()
    return client, login(client)


def _three(tmp_path):
    """Three feed accounts: the first without corrections, the second +2 points and +1 for IKON, the third −3 points."""
    app, conn, (first, second) = _app(tmp_path)
    third = connections.create_account(conn, "Третий", NOW, "owner")
    connections.set_account_pricing(conn, second, 2.0, None, NOW, "owner")
    connections.set_brand_deltas(conn, second, {"IKON TYRES": 1.0}, NOW, "owner")
    connections.set_account_pricing(conn, third, -3.0, None, NOW, "owner")
    conn.commit()
    return app, conn, (first, second, third)


def _inline(client, tok, value="", action="save", goods="100", **headers):
    data = dict(csrf=tok, g=goods, value=value, do=action, next="/catalog?q=Ikon")
    return client.post("/catalog/own-price/inline", data=data, headers=headers)


def _changes(conn):
    return staging.summary(conn)["changes"].get("price_overrides")


def _cell_form(page):
    return 'action="/catalog/own-price/inline"' in page


# --- кто видит ячейку ------------------------------------------------------------------------------------------------------------

def test_the_owner_sees_the_cell_form_one_for_all_accounts(tmp_path):
    app, conn, _ = _app(tmp_path)
    client, _ = _owner(app)
    page = client.get("/catalog").get_data(as_text=True)
    assert _cell_form(page) and "Своя цена, все аккаунты" in page and "со своей поправкой" in page
    assert 'name="acc"' not in page and "Цены аккаунта" not in page                                   # переключателя аккаунтов нет
    cell = page.split(ROW_G, 1)[1].split("</form>", 1)[0]
    assert "Сбросить</button>" not in cell and 'name="a"' not in cell                                 # своей цены нет — нечего сбрасывать


@pytest.mark.parametrize("role", ["partner", "chats", "viewer"])
def test_other_roles_see_the_price_but_not_the_cell_form(tmp_path, monkeypatch, role):
    from tires_sync import users
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    app, conn, (first, _) = _app(tmp_path)
    staging.set_own_price(conn, "100", [first], {"pct": 5}, NOW)
    users.add_user(conn, role + "1", "длинный-пароль-1", NOW, role=role)
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user=role + "1", epoch=store.get_session_epoch(conn, role + "1"), csrf="tok")
    page = client.get("/catalog").get_data(as_text=True)
    assert "Наша цена" in page and not _cell_form(page) and "data-own-cell" not in page and "data-purchase" not in page
    assert "data-own-mark" in page and ("к закупке" in page) == (role == "partner")                 # метка видна, процент к закупке — только с правом «деньги»


@pytest.mark.parametrize("role", ["partner", "chats", "viewer", "point", "client"])
def test_only_the_owner_may_post_the_cell(tmp_path, monkeypatch, role):
    from tires_sync import order_points as points, users
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    app, conn, _ = _app(tmp_path)
    pid = points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk"}) if role == "point" else None
    users.add_user(conn, role + "1", "длинный-пароль-1", NOW, role=role, point_id=pid)
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user=role + "1", epoch=store.get_session_epoch(conn, role + "1"), csrf="tok")
    r = _inline(client, "tok", "6990")
    assert r.status_code in (302, 403) and staging.summary(conn)["draft_id"] is None


# --- правка уходит в черновик ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value, own", [
    ("6990", {"price": 6990}), ("6 990 ₽", {"price": 6990}), ("+5 %", {"pct": 5.0}), ("−7,5 %", {"pct": -7.5}), ("-3%", {"pct": -3.0}),
    ("5%", {"pct": 5.0}),
])
def test_rubles_and_percents_go_into_the_draft_of_the_card_on_every_account(tmp_path, value, own):
    app, conn, (first, second) = _app(tmp_path)
    client, tok = _owner(app)
    r = _inline(client, tok, value)
    assert r.status_code == 302 and r.headers["Location"].endswith("/catalog?q=Ikon")
    assert _changes(conn) == {"%d:100" % first: own, "%d:100" % second: own}                         # без поправок — одно и то же
    assert active_settings(conn).price_overrides == {}                                               # до «Применить» ничего не применено


def test_one_edit_gives_three_accounts_each_its_correction(tmp_path):
    """Purchase 5 000. +5 %: the first 5, the second 5 + 2 + 1 = 8, the third 5 − 3 = 2 points. 6 990 ₽: + 5 000 × points / 100."""
    app, conn, (first, second, third) = _three(tmp_path)
    client, tok = _owner(app)
    _inline(client, tok, "+5 %")
    assert _changes(conn) == {"%d:100" % first: {"pct": 5.0}, "%d:100" % second: {"pct": 8.0}, "%d:100" % third: {"pct": 2.0}}
    _inline(client, tok, "6990")
    assert _changes(conn) == {"%d:100" % first: {"price": 6990}, "%d:100" % second: {"price": 7140}, "%d:100" % third: {"price": 6840}}
    assert staging.summary(conn)["count"] == 3                                                       # позиция × аккаунт


def test_the_first_accounts_correction_is_the_base(tmp_path):
    """The cell shows the first account's price: what is typed is its own price, the others are shifted by the difference of corrections."""
    app, conn, (first, second, third) = _three(tmp_path)
    connections.set_account_pricing(conn, first, 1.0, None, NOW, "owner")
    conn.commit()
    client, tok = _owner(app)
    _inline(client, tok, "+5 %")
    assert _changes(conn) == {"%d:100" % first: {"pct": 5.0}, "%d:100" % second: {"pct": 7.0}, "%d:100" % third: {"pct": 1.0}}


def test_a_shift_out_of_bounds_for_one_account_writes_nothing(tmp_path):
    """−49 % on the third (−3 points) would be −52 %, below the floor −50 %: no account gets anything."""
    app, conn, _ = _three(tmp_path)
    client, tok = _owner(app)
    r = _inline(client, tok, "−49 %", **{"X-Requested-With": "fetch"})
    assert r.get_json()["ok"] is False and "Процент к закупке" in r.get_json()["message"]
    assert staging.summary(conn)["draft_id"] is None


def test_the_ruble_shift_counts_from_the_anchor_of_the_supplier_price(tmp_path):
    """A fall of the supplier's price held by price_guard (anchor 6 000, today 5 000): the feed counts from 6 000, so does the shift."""
    app, conn, (first, second, third) = _three(tmp_path)
    conn.execute("INSERT INTO price_anchor (goods_id, base_price, set_at, how, brand) VALUES ('100', 6000, ?, 'first', '')", (NOW,))
    conn.commit()
    client, tok = _owner(app)
    _inline(client, tok, "6990")
    assert _changes(conn) == {"%d:100" % first: {"price": 6990}, "%d:100" % second: {"price": 7170}, "%d:100" % third: {"price": 6810}}


def test_the_cell_shows_the_mark_the_value_and_the_reset_and_the_price(tmp_path):
    app, conn, _ = _three(tmp_path)
    client, tok = _owner(app)
    _inline(client, tok, "7200")
    page = client.get("/catalog").get_data(as_text=True)
    form = page.split(ROW_G, 1)[1].split("</form>", 1)[0]
    assert 'value="7200"' in form and "Сбросить</button>" in form
    row = page.split(ROW_G, 1)[1].split("</tr>", 1)[0]
    summary = page.split(ROW_G, 1)[0].rsplit("<summary", 1)[1]
    assert "7\xa0200" in summary and "data-own-mark" in row and "не применено" in row                # цена первого аккаунта, уже с черновиком


def test_reset_takes_the_own_price_off_every_account_through_the_draft(tmp_path):
    app, conn, (first, second, third) = _three(tmp_path)
    client, tok = _owner(app)
    staging.set_own_price(conn, "100", [first, second, third], {"pct": 5}, NOW)
    confirm_draft(conn, staging.summary(conn)["draft_id"], NOW, "owner")
    assert len(active_settings(conn).price_overrides) == 3
    _inline(client, tok, action="clear")
    assert _changes(conn) == {}
    row = client.get("/catalog").get_data(as_text=True).split(ROW_G, 1)[1].split("</tr>", 1)[0]
    assert "сброс не применён" in row


def test_in_place_the_answer_is_json_names_the_accounts_and_the_draft_counts(tmp_path):
    app, conn, _ = _three(tmp_path)
    client, tok = _owner(app)
    r = _inline(client, tok, "+5 %", **{"X-Requested-With": "fetch"})
    body = r.get_json()
    assert body["ok"] and body["staged"] == 3 and "+5 % к закупке" in body["message"]
    assert "Шина-Колесо: +8 % к закупке" in body["message"] and "Третий: +2 % к закупке" in body["message"]
    r = _inline(client, tok, "abc", **{"X-Requested-With": "fetch"})
    assert r.get_json()["ok"] is False and len(_changes(conn)) == 3


@pytest.mark.parametrize("value", ["abc", "", "10.5", "+6990", "0", "-150 %", "6.990"])
def test_a_bad_cell_changes_nothing(tmp_path, value):
    app, conn, _ = _app(tmp_path)
    client, tok = _owner(app)
    _inline(client, tok, value)
    assert staging.summary(conn)["draft_id"] is None


def test_a_position_gone_from_the_unload_is_refused(tmp_path):
    app, conn, _ = _app(tmp_path)
    client, tok = _owner(app)
    _inline(client, tok, "6990", goods="nope")
    assert staging.summary(conn)["draft_id"] is None


# --- защиты при «Применить» --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value, asked", [("4500", True), ("−10 %", True), ("+40 %", True), ("5250", False), ("+5 %", False)])
def test_below_the_purchase_or_far_from_the_feed_needs_the_strong_confirmation(tmp_path, value, asked):
    """Purchase 5 000, the feed's price by the brand 4 810: 4 500 and −10 % are below the purchase, +40 % (7 000) is 46 % away."""
    app, conn, _ = _app(tmp_path)
    client, tok = _owner(app)
    _inline(client, tok, value)
    draft_id = staging.summary(conn)["draft_id"]
    page = client.get("/draft/%d" % draft_id).get_data(as_text=True)
    assert ("Подтверждаю значения вне диапазона" in page) == asked
    post(client, "/draft/%d/confirm" % draft_id, tok)                                                  # без отметки
    assert (active_settings(conn).price_overrides == {}) == asked
    if asked:
        post(client, "/draft/%d/confirm" % draft_id, tok, strong="on")
        assert len(active_settings(conn).price_overrides) == 2


def test_the_percent_hint_counts_from_the_anchor_of_the_supplier_price(tmp_path):
    """A fall of the supplier's price held by price_guard: the feed counts a percent from the anchor, so does the cell's hint."""
    app, conn, _ = _app(tmp_path)
    conn.execute("INSERT INTO price_anchor (goods_id, base_price, set_at, how, brand) VALUES ('100', 6000, ?, 'first', '')", (NOW,))
    conn.commit()
    client, _ = _owner(app)
    page = client.get("/catalog").get_data(as_text=True)
    purchases = re.findall(r'data-purchase="([^"]+)">(?:(?!</form>).)*?<input type="hidden" name="g" value="(\w+)"', page, re.S)
    assert ("6000.0", "100") in purchases and ("5000.0", "101") in purchases


def test_no_field_of_the_cell_is_named_action():
    """The catalog posts the cell in place (app.js: fetch(form.action)): a field named «action» would hide the address of the form."""
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "src/tires_sync/panel/templates/_catalog_table.html").read_text(encoding="utf-8")
    cell = text.split('action="/catalog/own-price/inline"', 1)[1].split("</form>", 1)[0]
    assert 'name="action"' not in cell and 'name="do"' in cell


@pytest.mark.parametrize("value, own", [("–5 %", {"pct": -5.0}), ("−0 %", {"pct": 0.0})])
def test_an_en_dash_is_a_minus_and_minus_zero_is_zero(tmp_path, value, own):
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    _inline(client, tok, value)
    got = _changes(conn)["%d:100" % first]
    assert got == own and str(got["pct"]) != "-0.0"
