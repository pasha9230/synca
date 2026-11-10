"""«На ОТХ»: the point chosen stays until the person takes it off (the owner 2026-11-10) — over «На возврат» / «Вернуть поставщику», over a
walk to an order and back; «Все» forgets it; each person has their own; an unknown or emptied point shows the whole page, as before."""
from __future__ import annotations

import pytest

from panel_helpers import login, make_app
from tests.test_oth_manual import a_point
from tests.test_order_reserve import card_at as order_on
from tires_sync import oth_manual, order_oth, orders, store, user_prefs, users

T = "2026-11-03T09:00:00"
NOW = "2026-11-03T10:00:00"
PAGE = {"Referer": "http://localhost/oth"}          # a request made from the page itself: its form «Точка», its buttons


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


def setup(tmp_path):
    """An order on «На ОТХ» at «Бета», our own pieces at «Юнона»: two points to choose from."""
    app = make_app(tmp_path)
    owner = app.test_client()
    login(owner)
    with owner.session_transaction() as session:
        session["csrf"] = "tok"
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, T)
    cid = order_on(conn, "oth", T)
    beta, yunona = a_point(conn, "Бета"), a_point(conn)
    oth_manual.set_qty(conn, "100", yunona, 3, NOW, "owner", "витрина")
    conn.commit()
    return app, owner, conn, cid, beta, yunona


def another(app, conn, login_, role="partner", point_id=None):
    users.add_user(conn, login_, "длинный-пароль-1", T, role=role, point_id=point_id)
    conn.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user=login_, epoch=store.get_session_epoch(conn, login_), csrf="tok")
    return client


def whole(page: str, cid: int) -> bool:
    return "data-oth-manual-list" in page and "/orders/%d" % cid in page


def only_beta(page: str, cid: int) -> bool:
    return "data-oth-manual-list" not in page and "/orders/%d" % cid in page


def test_the_point_survives_na_vozvrat_and_the_bare_address(tmp_path):
    app, owner, conn, cid, beta, _ = setup(tmp_path)
    page = owner.get("/oth?point=%d" % beta, headers=PAGE).get_data(as_text=True)
    assert only_beta(page, cid) and '<input type="hidden" name="point" value="%d">' % beta in page    # every form of a row carries the point
    assert user_prefs.get(conn, "owner", "f.oth_page") == {"point": [str(beta)]}
    item = orders.get_card(conn, cid)["items"][0]["id"]
    r = owner.post("/oth/to-return", data={"csrf": "tok", "item": str(item), "qty": "1", "point": str(beta)}, headers=PAGE)
    assert r.status_code == 302 and r.headers["Location"].endswith("/oth?point=%d" % beta)
    assert [x["qty"] for x in order_oth.stock(conn)["returned"] if x["item_id"] == item] == [1]
    assert only_beta(owner.get(r.headers["Location"], headers=PAGE).get_data(as_text=True), cid)
    r = owner.post("/oth/return", data={"csrf": "tok", "item": str(item), "qty": "1", "point": str(beta)}, headers=PAGE)   # «Вернуть поставщику»
    assert r.status_code == 302 and r.headers["Location"].endswith("/oth?point=%d" % beta)
    owner.get("/orders/%d" % cid)                                                     # into the order and back by the menu
    r = owner.get("/oth", headers={"Referer": "http://localhost/orders/%d" % cid})
    assert r.status_code == 302 and r.headers["Location"].endswith("/oth?point=%d" % beta)
    r = owner.get("/oth")                                                             # a bookmark, the address typed
    assert r.status_code == 302 and only_beta(owner.get(r.headers["Location"]).get_data(as_text=True), cid)


def test_a_form_without_the_point_comes_back_to_the_remembered_one(tmp_path):
    app, owner, conn, cid, beta, _ = setup(tmp_path)
    owner.get("/oth?point=%d" % beta, headers=PAGE)
    item = orders.get_card(conn, cid)["items"][0]["id"]
    r = owner.post("/oth/to-return", data={"csrf": "tok", "item": str(item), "qty": "1"}, headers=PAGE)      # a page opened before the update
    assert r.status_code == 302 and r.headers["Location"].endswith("/oth")
    r = owner.get("/oth", headers=PAGE)                                               # the redirect of the form is no «Все»
    assert r.status_code == 302 and r.headers["Location"].endswith("/oth?point=%d" % beta)
    for bad in ("0", "-3", "abc"):
        r = owner.post("/oth/return", data={"csrf": "tok", "item": "0", "point": bad}, headers=PAGE)
        assert r.status_code == 302 and r.headers["Location"].endswith("/oth")


def test_all_points_forgets_the_point(tmp_path):
    app, owner, conn, cid, beta, yunona = setup(tmp_path)
    owner.get("/oth?point=%d" % beta, headers=PAGE)
    page = owner.get("/oth?point=", headers=PAGE)                                     # «Точка: Все» → «Показать»
    assert page.status_code == 200 and whole(page.get_data(as_text=True), cid)
    assert user_prefs.get(conn, "owner", "f.oth_page") is None
    r = owner.get("/oth")
    assert r.status_code == 200 and whole(r.get_data(as_text=True), cid)
    owner.get("/oth?point=%d" % yunona, headers=PAGE)
    r = owner.get("/oth?reset=1")                                                     # «Сбросить фильтры» of the mark «Фильтр включён»
    assert r.status_code == 302 and r.headers["Location"].endswith("/oth") and user_prefs.get(conn, "owner", "f.oth_page") is None
    assert whole(owner.get("/oth").get_data(as_text=True), cid)


def test_each_person_has_their_own_point(tmp_path):
    app, owner, conn, cid, beta, yunona = setup(tmp_path)
    partner = another(app, conn, "zaman")
    owner.get("/oth?point=%d" % beta, headers=PAGE)
    r = partner.get("/oth")
    assert r.status_code == 200 and whole(r.get_data(as_text=True), cid)              # the owner's point is not the partner's
    partner.get("/oth?point=%d" % yunona, headers=PAGE)
    assert owner.get("/oth").headers["Location"].endswith("/oth?point=%d" % beta)
    assert partner.get("/oth").headers["Location"].endswith("/oth?point=%d" % yunona)


def test_an_unknown_or_emptied_point_shows_the_whole_page(tmp_path):
    app, owner, conn, cid, beta, yunona = setup(tmp_path)
    assert whole(owner.get("/oth?point=99999", headers=PAGE).get_data(as_text=True), cid)
    owner.get("/oth?point=%d" % yunona, headers=PAGE)
    oth_manual.set_qty(conn, "100", yunona, 0, NOW, "owner", None)                    # nothing left at «Юнона»
    conn.commit()
    r = owner.get("/oth")
    page = owner.get(r.headers["Location"]).get_data(as_text=True)
    assert "/orders/%d" % cid in page and 'name="point" value="%d"' % yunona not in page


def test_a_point_has_no_page_and_keeps_no_filter(tmp_path):
    app, owner, conn, cid, beta, _ = setup(tmp_path)
    point = another(app, conn, "tochka", role="point", point_id=beta)
    assert point.get("/oth?point=%d" % beta, headers=PAGE).status_code == 403          # as before: «На ОТХ» is the office's page
    assert point.post("/oth/to-return", data={"csrf": "tok", "item": "0", "point": str(beta)}, headers=PAGE).status_code == 403
    assert user_prefs.get(conn, "tochka", "f.oth_page") is None
