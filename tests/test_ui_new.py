"""«Новый вид» (the owner 2026-11-02, base-plan parts 1-2): the style «Строгий» lives under body.ui-new only; the owner's page sets per role
group (клиент, точка, офис) off / «только у владельца» / «у всех» in app_state ui_new.<group>, audited; who gets the class."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from panel_helpers import login, make_app, post
from test_roles import PASSWORD, conn_of, sign_in
from tires_sync import order_points as points, orders, store, users

NOW = "2026-10-07T12:00:00"
STATIC = Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static"


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


@pytest.fixture
def w(tmp_path):
    app = make_app(tmp_path)
    conn = conn_of(app)
    orders.ensure_pipeline(conn, NOW)
    pid = points.save_point(conn, {"name": "Янино", "manager_name": "А", "channel": "vk"})
    for login_, role in (("zaman", "partner"), ("chat1", "chats"), ("view1", "viewer"), ("pt1", "point"), ("cl1", "client")):
        users.add_user(conn, login_, PASSWORD, NOW, role=role, point_id=pid if role == "point" else None)
    return {"app": app, "conn": conn, "pid": pid}


def client_of(w, who):
    client = w["app"].test_client()
    if who == "owner":
        login(client)
    else:
        sign_in(client, w["conn"], who)
    return client


def has_class(client, page) -> bool:
    response = client.get(page)
    assert response.status_code == 200, page
    body = re.search(r"<body class=\"([^\"]*)\"", response.get_data(as_text=True))
    return "ui-new" in body.group(1).split()


def send(who, **form):
    return who.post("/ui-new", data=dict(form, csrf="tok"))


def set_states(w, **states):
    for group, state in states.items():
        store.set_state(w["conn"], "ui_new.%s" % group, state)


PAGES = {"owner": "/orders", "zaman": "/orders", "chat1": "/orders", "view1": "/orders", "pt1": "/my-point", "cl1": "/shop"}


def test_off_by_default_nobody_gets_it(w):
    for who, page in dict(PAGES, owner="/orders").items():
        assert not has_class(client_of(w, who), page), who


def test_off_written_is_the_same_page_as_nothing_written(w):
    client = client_of(w, "owner")
    before = client.get("/orders").get_data(as_text=True)
    set_states(w, office="off", point="off", client="off")
    assert client.get("/orders").get_data(as_text=True) == before


@pytest.mark.parametrize("state, who_sees", [("owner_only", {"owner", "zaman"}), ("all", {"owner", "zaman", "chat1", "view1"})])
def test_the_office(w, state, who_sees):
    set_states(w, office=state)
    for who in ("owner", "zaman", "chat1", "view1"):
        assert has_class(client_of(w, who), "/orders") == (who in who_sees), who
    assert not has_class(client_of(w, "pt1"), "/my-point") and not has_class(client_of(w, "cl1"), "/shop")


def test_the_point_owner_only_is_the_owner_working_as_the_point(w):
    set_states(w, point="owner_only")
    assert not has_class(client_of(w, "pt1"), "/my-point")
    owner = client_of(w, "owner")
    assert not has_class(owner, "/orders")                              # the owner's own pages: the office's state
    assert post(owner, "/as-point", "tok", point_id=str(w["pid"])).status_code == 302
    assert has_class(owner, "/my-point")
    set_states(w, point="all")
    assert has_class(client_of(w, "pt1"), "/my-point")


def test_the_client(w):
    set_states(w, client="owner_only")
    assert not has_class(client_of(w, "cl1"), "/shop")
    assert has_class(client_of(w, "owner"), "/client-terms/c/cl1/preview")      # «Каталог глазами клиента»: the client's state
    set_states(w, client="all")
    assert has_class(client_of(w, "cl1"), "/shop")


def test_a_wrong_value_in_the_store_is_off(w):
    set_states(w, office="yes")
    assert not has_class(client_of(w, "owner"), "/orders")


# --- the page -------------------------------------------------------------------------------------------------------------------------

def test_the_owner_sets_it_and_the_journal_keeps_it(w):
    client = client_of(w, "owner")
    page = client.get("/ui-new").get_data(as_text=True)
    assert "Новый вид" in page and 'name="office" value="off" checked' in page
    response = send(client, office="owner_only", point="off", client="all")
    assert response.status_code == 302
    assert store.get_state(w["conn"], "ui_new.office") == "owner_only" and store.get_state(w["conn"], "ui_new.client") == "all"
    assert store.get_state(w["conn"], "ui_new.point") is None           # unchanged: not written
    rows = [(r[0], json.loads(r[1])) for r in w["conn"].execute("SELECT author, detail FROM audit WHERE action = 'ui_new' ORDER BY id")]
    assert rows == [("owner", {"group": "client", "from": "off", "to": "all"}), ("owner", {"group": "office", "from": "off", "to": "owner_only"})]
    from tires_sync.panel import texts
    assert texts.audit_text({"action": "ui_new", "detail": rows[1][1]}) == \
        "Новый вид, Офис: партнёр, менеджер чатов, наблюдатель: Выкл → Только у владельца"


def test_a_wrong_state_is_refused_and_nothing_written(w):
    client = client_of(w, "owner")
    assert send(client, client="all", point="maybe", office="off").status_code == 400     # the first group fine, the second not: none
    assert w["conn"].execute("SELECT COUNT(*) FROM audit WHERE action = 'ui_new'").fetchone()[0] == 0
    assert store.get_state(w["conn"], "ui_new.client") is None
    assert send(client, office="all").status_code == 400     # every group is sent


def test_the_partner_looks_nobody_else_sets(w):
    partner = client_of(w, "zaman")
    page = partner.get("/ui-new")
    assert page.status_code == 200 and "<fieldset disabled" in page.get_data(as_text=True)
    assert send(partner, office="all", point="all", client="all").status_code == 403
    for who in ("chat1", "view1", "pt1", "cl1"):
        client = client_of(w, who)
        assert client.get("/ui-new").status_code == 403, who
        assert send(client, office="all", point="all", client="all").status_code == 403, who
    assert store.get_state(w["conn"], "ui_new.office") is None
    assert client_of(w, "owner").post("/ui-new", data={"office": "all", "point": "all", "client": "all"}).status_code == 400   # CSRF


def test_the_menu_shows_it_to_the_owner(w):
    assert 'href="/ui-new"' in client_of(w, "owner").get("/orders").get_data(as_text=True)
    assert 'href="/ui-new"' not in client_of(w, "chat1").get("/orders").get_data(as_text=True)


# --- the styles: only under the class ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sheet", ["panel.css", "point.css", "shop.css"])
def test_every_rule_of_the_new_style_is_under_the_class(sheet):
    css = re.sub(r"/\*.*?\*/", "", (STATIC / sheet).read_text(encoding="utf-8"), flags=re.S)
    rules = [selector for selector in re.findall(r"([^{}]+)\{", css) if "ui-new" in selector]
    assert rules, sheet
    for selector in rules:
        for part in selector.split(","):
            assert "ui-new" in part, (sheet, part)                         # «a.ui-new, b» would change b for everybody
