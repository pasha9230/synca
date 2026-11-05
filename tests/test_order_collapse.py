"""The order card folds three parts (the owner 2026-11-05: «в заказе сверни: Доставка в другой город; WebMIM Боевой сервер (webmim); Продажа и
расчёты»): they open by a click, both in the old view and in «Новый вид». A link to a part (?tab=<part> or #<part>) opens it; the content and
the forms are the same as before, only folded."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from panel_helpers import login, make_app, post
from test_roles import PASSWORD, conn_of, sign_in
from tires_sync import connections, order_intercity as intercity, order_points as points, orders, store, users

NOW = "2026-10-07T12:00:00"
FOLDED = ("intercity", "webmim", "money")
APP_JS = Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static" / "app.js"


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


@pytest.fixture
def w(tmp_path):
    app = make_app(tmp_path)
    conn = conn_of(app)
    orders.ensure_pipeline(conn, NOW)
    connections.set_config(conn, "webmim_orders", 0, {"server": "prod"}, NOW, "owner")
    pid = points.save_point(conn, {"name": "Янино", "manager_name": "А", "channel": "vk"})
    users.add_user(conn, "pt1", PASSWORD, NOW, role="point", point_id=pid)
    card = orders.create_card(conn, dict(client_name="Анна", client_phone="+7 900 000-00-01", point_id=pid), NOW, "o")
    intercity.save(conn, card, dict(enabled="1", city="Псков", carrier="ПЭК", track=""), NOW, "owner")
    conn.commit()
    return {"app": app, "conn": conn, "card": card}


def page(client, url):
    r = client.get(url)
    assert r.status_code == 200, url
    return r.get_data(as_text=True)


def tag(text, part):
    """The opening tag of the element id=<part>."""
    m = re.search(r'<(\w+)\s[^>]*\bid="%s"[^>]*>' % part, text)
    assert m, part
    return m.group(0)


def block(text, part):
    """The whole <details id=part>…</details> (the parts hold no nested <details> of their own)."""
    start = text.index(tag(text, part))
    return text[start:text.index("</details>", start)]


def is_folded(text, part):
    t = tag(text, part)
    return t.startswith("<details") and not re.search(r"\sopen(\s|>|=)", t)


def is_open(text, part):
    t = tag(text, part)
    return t.startswith("<details") and re.search(r"\sopen(\s|>|=)", t)


def owner(w, new_view=False):
    store.set_state(w["conn"], "ui_new.office", "all" if new_view else "off")
    w["conn"].commit()
    client = w["app"].test_client()
    tok = login(client)
    return client, tok


@pytest.mark.parametrize("new_view", [False, True], ids=["old", "new"])
def test_the_three_parts_are_folded_when_the_card_opens(w, new_view):
    client, _ = owner(w, new_view)
    text = page(client, "/orders/%d" % w["card"])
    assert ("office2.js" in text) == new_view
    for part in FOLDED:
        assert is_folded(text, part), part
    assert "Доставка в другой город" in block(text, "intercity").split("</summary>")[0]
    assert "Боевой сервер (webmim)" in block(text, "webmim").split("</summary>")[0]
    assert "Продажа и расчёты" in block(text, "money").split("</summary>")[0]
    assert 'name="city" value="Псков"' in block(text, "intercity")                # folded, not dropped: the forms are all there
    assert 'action="/orders/%d/save"' % w["card"] in block(text, "money") and 'name="tab" value="money"' in block(text, "money")


@pytest.mark.parametrize("new_view", [False, True], ids=["old", "new"])
@pytest.mark.parametrize("part", FOLDED)
def test_a_link_to_a_part_opens_it_and_only_it(w, new_view, part):
    client, _ = owner(w, new_view)
    text = page(client, "/orders/%d?tab=%s" % (w["card"], part))
    assert is_open(text, part)
    for other in FOLDED:
        if other != part:
            assert is_folded(text, other), other


def test_an_anchor_opens_a_folded_part_also_on_a_click_in_the_page():
    js = APP_JS.read_text(encoding="utf-8")
    assert "hashchange" in js and 'part.tagName === "DETAILS"' in js     # #intercity of the tag «В другой город» in the window, #money after a save


def test_a_folded_form_still_saves_and_comes_back_to_its_part(w):
    client, tok = owner(w)
    r = post(client, "/orders/%d/intercity" % w["card"], tok, enabled="1", city="Псков", carrier="КИТ", track="")
    assert r.status_code == 302 and r.headers["Location"].endswith("#intercity")
    assert intercity.of(w["conn"], w["card"])["carrier"] == "КИТ"
    r = post(client, "/orders/%d/intercity" % w["card"], tok, enabled="1", city="Псков", carrier="СДЭК", track="")    # refused: back to the part
    assert r.status_code == 302 and r.headers["Location"].endswith("#intercity")


def test_a_point_sees_the_same_folded_content(w):
    client = w["app"].test_client()
    sign_in(client, w["conn"], "pt1")
    text = page(client, "/orders/%d" % w["card"])
    assert is_folded(text, "intercity") and "Город: <b>Псков</b>" in block(text, "intercity")
    assert 'id="webmim"' not in text                                      # a point never had WebMIM: still not
    assert 'name="city"' not in text                                      # nor the office's form
