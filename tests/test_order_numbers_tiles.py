"""The number of an order always seen (the owner 25.11: «№ заказов МИМ не вижу на самой плитке»): a tile of «Заказы» (the board, «Список»,
«Ожидает наличие» — the waiting list is a column of the board) shows the WebMIM number; without it, small, the panel's one («панель №…»), so an
order without WebMIM is still known (the old view keeps its «№N клиент» and adds «MIM №X» small); the dark head of the card shows «№ панели N · № MIM X» top left (MIM only when there is one), in both views.
A point keeps what it saw: its own number (point_entity), no WebMIM number added to its pages."""
from __future__ import annotations

import re

import pytest

from panel_helpers import login, make_app
from test_roles import PASSWORD, conn_of, sign_in
from tires_sync import order_points as points, orders, store, users

NOW = "2026-11-25T12:00:00"


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


@pytest.fixture
def w(tmp_path):
    app = make_app(tmp_path)
    conn = conn_of(app)
    orders.ensure_pipeline(conn, NOW)
    pid = points.save_point(conn, {"name": "Янино", "manager_name": "А", "channel": "vk"})
    users.add_user(conn, "pt1", PASSWORD, NOW, role="point", point_id=pid)
    stages = {s["code"]: s for s in orders.list_stages(conn)}
    mim = orders.create_card(conn, dict(client_name="Анна", client_phone="+7 900 000-00-01"), NOW, "o")
    orders.update_card(conn, mim, dict(webmim_number="7712345"), NOW, "o")
    wait = orders.create_card(conn, dict(client_name="Борис", client_phone="+7 900 000-00-02"), NOW, "o")
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stages["wait"]["id"], wait))     # the waiting list: no WebMIM
    own = orders.create_card(conn, dict(client_name="Вера", client_phone="+7 900 000-00-03", point_id=str(pid), deal_kind="stock"), NOW, "o")
    conn.commit()
    return dict(app=app, conn=conn, pid=pid, mim=mim, wait=wait, own=own, wait_stage=stages["wait"]["id"])


def view(w, state):
    store.set_state(w["conn"], "ui_new.office", state)
    w["conn"].commit()


def owner(w):
    client = w["app"].test_client()
    login(client)
    return client


def html(client, page):
    r = client.get(page)
    assert r.status_code == 200, page
    return r.get_data(as_text=True)


def tile(text, cid):
    return re.search(r'<a class="ocard[^"]*"[^>]*data-card="%d".*?</a>' % cid, text, re.S).group(0)


def head(text):
    return re.search(r'<div class="owin-head">.*?</h1>', text, re.S).group(0)


@pytest.mark.parametrize("state", ["all", "off"])
def test_the_waiting_tile_shows_the_panel_number_and_a_webmim_tile_its_own(w, state):
    view(w, state)
    text = html(owner(w), "/orders")
    bare, both = tile(text, w["wait"]), tile(text, w["mim"])
    assert 'data-stage="%d"' % w["wait_stage"] in text                                  # «Ожидает наличие» is a column of the board
    if state == "all":                                                                   # «Новый вид»: № WebMIM, the panel's small
        assert '<span class="xs muted" data-panel-no>панель №%d</span>' % w["wait"] in bare and "o2-wm" not in bare
        assert '<span class="o2-wm">№ 7712345</span>' in both and "data-panel-no" not in both    # with WebMIM no inner number (08.11)
    else:                                                                                # the old view: «№N клиент», MIM small under it
        assert "№%d Борис" % w["wait"] in bare and "MIM №" not in bare
        assert "№%d Анна" % w["mim"] in both and '<div class="xs muted" data-mim-no>MIM №7712345</div>' in both


@pytest.mark.parametrize("state", ["all", "off"])
def test_the_list_shows_both_numbers(w, state):
    view(w, state)
    text = html(owner(w), "/orders?v=list")
    row = re.search(r'<tr data-href="/orders/%d".*?</tr>' % w["mim"], text, re.S).group(0)
    assert "7712345" in row and str(w["mim"]) in row
    bare = re.search(r'<tr data-href="/orders/%d".*?</tr>' % w["wait"], text, re.S).group(0)
    assert str(w["wait"]) in bare and "MIM №" not in bare


@pytest.mark.parametrize("state", ["all", "off"])
def test_the_dark_head_of_the_card_names_the_panel_and_the_webmim_numbers(w, state):
    view(w, state)
    client = owner(w)
    both = head(html(client, "/orders/%d" % w["mim"]))
    assert '<span class="entity-kind" id="ow-title-kind">№ панели %d · № MIM 7712345</span>' % w["mim"] in both
    bare = head(html(client, "/orders/%d" % w["wait"]))
    assert '<span class="entity-kind" id="ow-title-kind">№ панели %d</span>' % w["wait"] in bare and "MIM" not in bare
    assert both.index("entity-kind") < both.index("owin-title")                          # top left, above the client's name


def test_the_client_page_names_the_webmim_number_of_a_purchase(w):
    row = w["conn"].execute("SELECT client_id FROM client_order WHERE order_id = ?", (w["mim"],)).fetchone()
    if row is None:
        pytest.skip("the order is not linked to a client here")
    for state in ("all", "off"):
        view(w, state)
        text = html(owner(w), "/clients/%d" % row[0])
        assert '<span class="xs muted" data-mim-no>MIM №7712345</span>' in text


def test_the_point_keeps_its_numbers(w):
    """A point: its own sale's card is headed by point_entity («Заказ № панель N»), no «№ панели» of the office; its «Заказы» get no
    WebMIM number and no panel mark of the office."""
    point = w["app"].test_client()
    sign_in(point, w["conn"], "pt1")
    assert w["own"] in points.own_sales(w["conn"], w["pid"])
    card = head(html(point, "/orders/%d" % w["own"]))
    assert "Заказ № панель %d" % w["own"] in card and "№ панели" not in card
    board = html(point, "/orders")
    assert "data-mim-no" not in board and "data-panel-no" not in board and "7712345" not in board
