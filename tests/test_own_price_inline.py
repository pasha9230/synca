"""Своя цена прямо в графе «Наша цена» каталога (решение владельца 2026-10-30): клик по цене → поле «6990» (₽) или «+5 %» / «−5 %» (к закупке),
для аккаунта, выбранного в каталоге («Цены аккаунта»), с галочкой «то же на …»; в тот же черновик, что карточка, «Сбросить», метка «своя»;
ниже закупки или дальше 30 % от цены фида — усиленное подтверждение при «Применить». Только владелец."""
from __future__ import annotations

import re

import pytest

from panel_helpers import login, post
from test_own_price import NOW, _app
from tires_sync import staging, store
from tires_sync.settings_store import active_settings, confirm_draft


def _owner(app):
    client = app.test_client()
    return client, login(client)


def _inline(client, tok, acc, value="", others=(), action="save", goods="100", **headers):
    data = dict(csrf=tok, g=goods, acc=str(acc), a=[str(a) for a in others], value=value, do=action, next="/catalog?acc=%s" % acc)
    return client.post("/catalog/own-price/inline", data=data, headers=headers)


def _changes(conn):
    return staging.summary(conn)["changes"].get("price_overrides")


def _cell_form(page):
    return 'action="/catalog/own-price/inline"' in page


# --- кто видит ячейку ------------------------------------------------------------------------------------------------------------

def test_the_owner_sees_the_cell_form_for_the_account_chosen(tmp_path):
    app, conn, (first, second) = _app(tmp_path)
    client, _ = _owner(app)
    page = client.get("/catalog").get_data(as_text=True)
    assert _cell_form(page) and "Цена на «Основной»" in page and "то же на «Шина-Колесо»" in page
    assert 'name="acc" value="%d"' % first in page and "Цены аккаунта" in page
    page = client.get("/catalog?acc=%d" % second).get_data(as_text=True)
    assert "Цена на «Шина-Колесо»" in page and "то же на «Основной»" in page and 'name="acc" value="%d"' % second in page
    assert "Сбросить</button>" not in page.split('action="/catalog/own-price/inline"', 1)[1].split("</form>", 1)[0]   # своей цены нет — нечего сбрасывать


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
    app, conn, (first, _) = _app(tmp_path)
    pid = points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk"}) if role == "point" else None
    users.add_user(conn, role + "1", "длинный-пароль-1", NOW, role=role, point_id=pid)
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user=role + "1", epoch=store.get_session_epoch(conn, role + "1"), csrf="tok")
    r = _inline(client, "tok", first, "6990")
    assert r.status_code in (302, 403) and staging.summary(conn)["draft_id"] is None


# --- правка уходит в черновик ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value, own", [
    ("6990", {"price": 6990}), ("6 990 ₽", {"price": 6990}), ("+5 %", {"pct": 5.0}), ("−7,5 %", {"pct": -7.5}), ("-3%", {"pct": -3.0}),
    ("5%", {"pct": 5.0}),
])
def test_rubles_and_percents_go_into_the_draft_of_the_card(tmp_path, value, own):
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    r = _inline(client, tok, first, value)
    assert r.status_code == 302 and r.headers["Location"].endswith("/catalog?acc=%d" % first)
    assert _changes(conn) == {"%d:100" % first: own}
    assert active_settings(conn).price_overrides == {}                                               # до «Применить» ничего не применено


def test_the_second_account_and_the_tick_for_the_other(tmp_path):
    app, conn, (first, second) = _app(tmp_path)
    client, tok = _owner(app)
    _inline(client, tok, second, "7200")
    assert _changes(conn) == {"%d:100" % second: {"price": 7200}}
    _inline(client, tok, second, "+4 %", others=[first], goods="101")
    assert _changes(conn) == {"%d:100" % second: {"price": 7200}, "%d:101" % first: {"pct": 4.0}, "%d:101" % second: {"pct": 4.0}}


def test_the_cell_shows_the_mark_the_value_and_the_reset_and_the_price_of_the_account_chosen(tmp_path):
    app, conn, (first, second) = _app(tmp_path)
    client, tok = _owner(app)
    _inline(client, tok, second, "7200")
    on_second = client.get("/catalog?acc=%d" % second).get_data(as_text=True)
    form = on_second.split('<input type="hidden" name="g" value="100">', 1)[1].split("</form>", 1)[0]
    assert 'value="7200"' in form and "Сбросить</button>" in form
    row = on_second.split('<input type="hidden" name="g" value="100">', 1)[1].split("</tr>", 1)[0]
    summary = on_second.split('<input type="hidden" name="g" value="100">', 1)[0].rsplit("<summary", 1)[1]
    assert "7\xa0200" in summary and "data-own-mark" in row and "не применено" in row                  # цена этого аккаунта, уже с черновиком
    on_first = client.get("/catalog?acc=%d" % first).get_data(as_text=True)
    row = on_first.split('<input type="hidden" name="g" value="100">', 1)[1].split("</tr>", 1)[0]
    summary = on_first.split('<input type="hidden" name="g" value="100">', 1)[0].rsplit("<summary", 1)[1]
    assert "7\xa0200" not in summary and "4\xa0810" in summary
    assert "data-own-mark" not in row and "своя цена</span>" in row                                 # на этом аккаунте своей нет, на другом есть


def test_reset_takes_the_own_price_off_through_the_draft(tmp_path):
    app, conn, (first, second) = _app(tmp_path)
    client, tok = _owner(app)
    staging.set_own_price(conn, "100", [first, second], {"pct": 5}, NOW)
    confirm_draft(conn, staging.summary(conn)["draft_id"], NOW, "owner")
    assert set(active_settings(conn).price_overrides) == {"%d:100" % first, "%d:100" % second}
    _inline(client, tok, first, action="clear")
    assert _changes(conn) == {"%d:100" % second: {"pct": 5}}
    row = client.get("/catalog?acc=%d" % first).get_data(as_text=True).split('<input type="hidden" name="g" value="100">', 1)[1].split("</tr>", 1)[0]
    assert "сброс не применён" in row


def test_in_place_the_answer_is_json_and_the_draft_counts(tmp_path):
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    r = _inline(client, tok, first, "+5 %", **{"X-Requested-With": "fetch"})
    body = r.get_json()
    assert body["ok"] and body["staged"] == 1 and "+5 % к закупке" in body["message"] and "Основной" in body["message"]
    r = _inline(client, tok, first, "abc", **{"X-Requested-With": "fetch"})
    assert r.get_json()["ok"] is False and _changes(conn) == {"%d:100" % first: {"pct": 5.0}}


@pytest.mark.parametrize("value, acc, others", [
    ("abc", None, ()), ("", None, ()), ("10.5", None, ()), ("+6990", None, ()), ("0", None, ()), ("6990", "99", ()), ("6990", None, ("99",)),
    ("6990", "", ()), ("-150 %", None, ()),
])
def test_a_bad_cell_changes_nothing(tmp_path, value, acc, others):
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    _inline(client, tok, first if acc is None else acc, value, others=others)
    assert staging.summary(conn)["draft_id"] is None


def test_a_position_gone_from_the_unload_is_refused(tmp_path):
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    _inline(client, tok, first, "6990", goods="nope")
    assert staging.summary(conn)["draft_id"] is None


# --- защиты при «Применить» --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value, asked", [("4500", True), ("−10 %", True), ("+40 %", True), ("5250", False), ("+5 %", False)])
def test_below_the_purchase_or_far_from_the_feed_needs_the_strong_confirmation(tmp_path, value, asked):
    """Purchase 5 000, the feed's price by the brand 4 810: 4 500 and −10 % are below the purchase, +40 % (7 000) is 46 % away."""
    app, conn, (first, _) = _app(tmp_path)
    client, tok = _owner(app)
    _inline(client, tok, first, value)
    draft_id = staging.summary(conn)["draft_id"]
    page = client.get("/draft/%d" % draft_id).get_data(as_text=True)
    assert ("Подтверждаю значения вне диапазона" in page) == asked
    post(client, "/draft/%d/confirm" % draft_id, tok)                                                  # без отметки
    assert (active_settings(conn).price_overrides == {}) == asked
    if asked:
        post(client, "/draft/%d/confirm" % draft_id, tok, strong="on")
        assert set(active_settings(conn).price_overrides) == {"%d:100" % first}


def test_the_percent_hint_counts_from_the_anchor_of_the_supplier_price(tmp_path):
    """A fall of the supplier's price held by price_guard: the feed counts a percent from the anchor, so does the cell's hint."""
    app, conn, (first, _) = _app(tmp_path)
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
