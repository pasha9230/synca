"""The filters of the list pages are kept in the person's account (the owner 2026-10-29): they survive a reload and a walk to another page, a
person's are never another's, «Сбросить фильтры» clears them, the page shows that a filter is on."""
from __future__ import annotations

import pytest

from helpers import catalog_xml, commodity_xml
from panel_helpers import login, make_app
from tires_sync import order_points as points, store, user_prefs, users
from tires_sync.panel import page_prefs

T = "2026-10-07T10:00:00"
ROWS = (commodity_xml(NNOMMODIF="1", SMNFCODE="C1", NREST="12"),
        commodity_xml(NNOMMODIF="3", SMNFCODE="C3", SDIAMETR="13", NREST="9"))


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


def setup(tmp_path):
    app = make_app(tmp_path)
    owner = app.test_client()
    login(owner)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*ROWS))
    return app, owner, store.connect(app.config["DB_PATH"])


def on(path):
    """The headers of a request made from the page `path` itself (its filter form, its links)."""
    return {"Referer": "http://localhost" + path}


CAT = on("/catalog")


def as_user(app, conn, login_, role, point_id=None):
    users.add_user(conn, login_, "длинный-пароль-1", T, role=role, point_id=point_id)
    c = app.test_client()
    with c.session_transaction() as session:
        session.update(user=login_, epoch=store.get_session_epoch(conn, login_), csrf="tok")
    return c


def test_every_remembered_page_is_a_page_of_the_panel(tmp_path):
    app, _, _ = setup(tmp_path)
    assert set(page_prefs.PAGES) <= set(app.view_functions)
    assert {e for pages in page_prefs.TABS.values() for e in pages} <= set(app.view_functions)


def test_the_catalog_filters_survive_a_reload_and_a_walk_to_another_page(tmp_path):
    app, owner, conn = setup(tmp_path)
    page = owner.get("/catalog?d=13&o=stock&p=1", headers=CAT).get_data(as_text=True)
    assert "data-filter-on" in page and "Фильтр включён: 1" in page and "/catalog?reset=1" in page    # the sort and the page are no filter
    assert user_prefs.get(conn, "owner", "f.catalog") == {"d": ["13"], "o": ["stock"], "p": ["1"]}
    owner.get("/orders")                                                        # another page
    r = owner.get("/catalog", headers={"Referer": "http://localhost/orders"})   # the menu link
    assert r.status_code == 302 and r.headers["Location"].endswith("/catalog?d=13&o=stock&p=1")
    r = owner.get("/catalog")                                                    # a bookmark, the address typed
    assert r.status_code == 302 and "d=13" in r.headers["Location"]
    shown = owner.get(r.headers["Location"]).get_data(as_text=True)
    assert ">C3<" in shown and ">C1<" not in shown


def test_the_reset_and_a_clear_link_of_the_page_itself_forget_the_filters(tmp_path):
    app, owner, conn = setup(tmp_path)
    owner.get("/catalog?d=13", headers=CAT)
    r = owner.get("/catalog?reset=1")
    assert r.status_code == 302 and r.headers["Location"].endswith("/catalog")
    assert user_prefs.get(conn, "owner", "f.catalog") is None
    page = owner.get("/catalog")
    assert page.status_code == 200 and "data-filter-on" not in page.get_data(as_text=True)
    owner.get("/catalog?d=13", headers=CAT)
    r = owner.get("/catalog", headers={"Referer": "http://localhost/catalog?d=13"})      # «Сбросить» of the page: the bare address from itself
    assert r.status_code == 200 and user_prefs.get(conn, "owner", "f.catalog") is None
    owner.get("/catalog?d=13", headers=CAT)
    owner.get("/catalog?d=&q=&s=", headers=CAT)                                              # the form sent with every field cleared
    assert user_prefs.get(conn, "owner", "f.catalog") is None


def test_the_filters_of_one_person_are_never_anothers(tmp_path):
    app, owner, conn = setup(tmp_path)
    other = as_user(app, conn, "partner1", "partner")
    owner.get("/catalog?d=13", headers=CAT)
    r = other.get("/catalog")
    assert r.status_code == 200 and "data-filter-on" not in r.get_data(as_text=True)
    other.get("/catalog?d=16", headers=CAT)
    assert user_prefs.get(conn, "owner", "f.catalog") == {"d": ["13"]}
    assert user_prefs.get(conn, "partner1", "f.catalog") == {"d": ["16"]}
    assert owner.get("/catalog").headers["Location"].endswith("d=13")


def test_the_orders_view_and_its_page_are_kept_and_a_point_never_keeps_another_point(tmp_path):
    app, owner, conn = setup(tmp_path)
    owner.get("/orders?v=list&q=Иван&page=2", headers=on("/orders"))
    assert user_prefs.get(conn, "owner", "f.orders_page") == {"v": ["list"], "q": ["Иван"], "page": ["2"]}
    assert "v=list" in owner.get("/orders").headers["Location"]
    pid = points.save_point(conn, {"name": "Юнона", "manager_name": "А", "channel": "panel"})
    manager = as_user(app, conn, "point1", "point", point_id=pid)
    manager.get("/orders?v=list&p=999", headers=on("/orders?v=list"))
    assert user_prefs.get(conn, "point1", "f.orders_page") == {"v": ["list"]}


def test_a_script_request_and_a_one_off_address_are_not_touched(tmp_path):
    app, owner, conn = setup(tmp_path)
    owner.get("/catalog?d=13", headers=CAT)
    r = owner.get("/catalog", headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200
    owner.get("/chats?f=unread", headers=on("/chats"))
    assert owner.get("/chats/12345").status_code != 302 or "f=unread" not in owner.get("/chats/12345").headers.get("Location", "")
    assert user_prefs.get(conn, "owner", "f.chats_page") == {"f": ["unread"]}


def test_settings_open_the_tab_last_looked_at_and_the_tabs_move_freely(tmp_path):
    app, owner, conn = setup(tmp_path)
    owner.get("/settings/exclusions")
    r = owner.get("/settings", headers={"Referer": "http://localhost/catalog"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/settings/exclusions")
    r = owner.get("/settings", headers={"Referer": "http://localhost/settings/exclusions"})     # the tab «Общие»
    assert r.status_code == 200
    assert owner.get("/settings", headers={"Referer": "http://localhost/catalog"}).status_code == 200


def test_a_link_with_its_own_parameters_from_elsewhere_opens_exactly_them_and_keeps_the_saved_choice(tmp_path):
    app, owner, conn = setup(tmp_path)
    owner.get("/orders?v=list&q=Иван", headers=on("/orders"))
    r = owner.get("/orders?v=list&s=3", headers=on("/my-point"))               # a tile of another page: its one-off view
    assert r.status_code == 200 and "data-filter-on" in r.get_data(as_text=True)
    assert user_prefs.get(conn, "owner", "f.orders_page") == {"v": ["list"], "q": ["Иван"]}
    assert owner.get("/orders?v=list&s=3").status_code == 200                    # a bookmark with its parameters: the same
    assert user_prefs.get(conn, "owner", "f.orders_page") == {"v": ["list"], "q": ["Иван"]}
    assert "q=" in owner.get("/orders", headers=on("/")).headers["Location"]    # the bare address: the saved choice


def test_a_page_without_a_saved_choice_opens_with_its_default(tmp_path, monkeypatch):
    app, owner, conn = setup(tmp_path)
    assert owner.get("/chats", headers=on("/")).status_code == 200             # all the accounts: no default of the person yet
    monkeypatch.setattr(page_prefs, "default", lambda conn, login, endpoint: {"acc": ["7"]} if endpoint == "chats_page" else {})
    r = owner.get("/chats", headers=on("/"))
    assert r.status_code == 302 and r.headers["Location"].endswith("/chats?acc=7")
