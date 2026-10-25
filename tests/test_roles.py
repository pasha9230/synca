"""Users and roles: one right per page, the role × section table, money hidden without the right, invitations, «off», the emergency login,
the manager of a point sees only the point."""
from __future__ import annotations

import re
from datetime import datetime

import pytest

from panel_helpers import login, make_app, post, shared_mark_of
from tires_sync import order_points as points, orders, store, users
from tires_sync.panel import common

NOW = "2026-10-07T12:00:00"
PASSWORD = "длинный-пароль-1"


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


def conn_of(app):
    return store.connect(app.config["DB_PATH"])


def sign_in(client, conn, login_, ) -> str:
    with client.session_transaction() as session:
        session["user"] = login_
        session["epoch"] = store.get_session_epoch(conn, login_)
        session["csrf"] = "tok"
        if login_ == "owner":                                          # the shared password: its sessions carry its mark
            session["pw"] = shared_mark_of(client.application)
    return "tok"


def person(app, role, point_id=None, login_=None):
    conn = conn_of(app)
    login_ = login_ or role + "1"
    users.add_user(conn, login_, PASSWORD, NOW, role=role, point_id=point_id)
    client = app.test_client()
    sign_in(client, conn, login_)
    return client, conn


# --- every page has a right ---------------------------------------------------------------------------------------------------------

def test_every_route_is_open_by_design_or_has_a_section_and_the_tables_name_no_ghosts(tmp_path):
    app = make_app(tmp_path)
    endpoints = {r.endpoint for r in app.url_map.iter_rules()}
    missing = sorted(e for e in endpoints if e not in common.OPEN and e not in common.SECTION_OF)
    assert missing == [], "a page without a right: add it to panel/common.SECTION_OF (or OPEN, with a reason)"
    assert set(common.SECTION_OF) <= endpoints and set(common.NEEDS) <= set(common.SECTION_OF) and common.POINT_PAGES <= set(common.SECTION_OF)
    assert not set(common.OPEN) & set(common.SECTION_OF)
    assert set(common.SECTION_OF.values()) <= set(users.SECTIONS) and set(common.NEEDS.values()) <= set(users.RIGHTS)


def test_a_page_nobody_gave_a_right_to_is_closed_even_for_the_owner(tmp_path):
    app = make_app(tmp_path)

    @app.route("/zz-new-page")
    def zz_new_page():
        return "secret"
    client = app.test_client()
    assert client.get("/zz-new-page").status_code == 302                                   # signed out: to the login page
    login(client)
    page = client.get("/zz-new-page")
    assert page.status_code == 403 and "Нет доступа" in page.get_data(as_text=True) and "secret" not in page.get_data(as_text=True)


# --- role × section ---------------------------------------------------------------------------------------------------------------

PAGES = {"home": "/", "orders": "/orders", "chats": "/chats", "calls": "/calls", "alerts": "/alerts", "catalog": "/catalog", "analytics": "/economics",
         "setup": "/connections/webmim", "users": "/users", "self": "/account/password"}      # /connections itself only leads there


@pytest.mark.parametrize("role", ["partner", "chats", "viewer", "point"])
def test_each_role_opens_exactly_its_sections(tmp_path, role):
    app = make_app(tmp_path)
    pid = points.save_point(conn_of(app), {"name": "Гамма", "manager_name": "А", "channel": "vk"}) if role == "point" else None
    client, conn = person(app, role, pid)
    for section, url in PAGES.items():
        got = client.get(url).status_code
        allowed = users.level(role, section) >= users.VIEW and (section != "analytics" or users.has_right(role, "money"))
        if role == "point" and section == "home":
            assert got == 302                                                                # the point goes straight to its orders
        else:
            assert got == (200 if allowed else 403), (role, section, got)
    assert client.get("/orders/report").status_code == (200 if role == "partner" else 403)
    assert client.get("/economics/sales/export.csv").status_code == (200 if role == "partner" else 403)


def test_the_owner_and_the_emergency_password_open_everything(tmp_path):
    app = make_app(tmp_path)
    users.add_user(conn_of(app), "worker2", PASSWORD, NOW, role="partner")                   # roles of others change nothing for the owner
    client = app.test_client()
    assert client.post("/login", data={"csrf": _csrf(client), "login": "", "password": "pw"}).status_code == 302
    for url in PAGES.values():
        got = client.get(url)
        assert got.status_code in (200, 302) and "/login" not in got.headers.get("Location", ""), url
    client2, conn = person(app, "owner", login_="pavel")
    assert client2.get("/users").status_code == 200


def _csrf(client) -> str:
    with client.session_transaction() as session:
        session["csrf"] = "tok"
    return "tok"


def test_view_is_not_change_and_the_dangerous_rights_are_apart(tmp_path):
    app = make_app(tmp_path)
    viewer, _ = person(app, "viewer")
    assert viewer.post("/orders/new", data={"csrf": "tok", "client_name": "Иван"}).status_code == 403
    assert viewer.post("/alerts/1/ack", data={"csrf": "tok"}).status_code != 403            # the own alerts: allowed
    chats, _ = person(app, "chats")
    assert chats.post("/orders/1/note", data={"csrf": "tok", "text": "x"}).status_code != 403
    for url in ("/orders/1/delete", "/orders/bulk/run", "/orders/cleanup", "/chats/merge", "/draft/1/confirm", "/connections/telegram"):
        assert chats.post(url, data={"csrf": "tok"}).status_code == 403, url
    partner, _ = person(app, "partner")
    assert partner.post("/orders/1/delete", data={"csrf": "tok"}).status_code != 403
    for url in ("/draft/1/confirm", "/feed/update/confirm", "/connections/telegram", "/orders/report/rates", "/users/invite",
                "/connections/accounts/1/live"):
        assert partner.post(url, data={"csrf": "tok"}).status_code == 403, url


# --- money --------------------------------------------------------------------------------------------------------------------------

def _supplier_cells(html: str):
    return re.findall(r'<td class="num col-sp">([^<]*)</td>', html)


def test_the_supplier_price_is_not_even_in_the_html_without_the_money_right(tmp_path):
    app = make_app(tmp_path)
    login_client = app.test_client()
    login(login_client)
    owner_cells = _supplier_cells(login_client.get("/catalog").get_data(as_text=True))
    assert owner_cells and any(c.strip("—  ₽") for c in owner_cells)
    chats, _ = person(app, "chats")
    html = chats.get("/catalog").get_data(as_text=True)
    assert _supplier_cells(html) and all(c == "—" for c in _supplier_cells(html))
    table = chats.get("/catalog/table").get_data(as_text=True)
    assert all(c == "—" for c in _supplier_cells(table))
    partner, _ = person(app, "partner")
    assert _supplier_cells(partner.get("/catalog").get_data(as_text=True)) == owner_cells


def test_the_menu_shows_only_what_the_role_may_open(tmp_path):
    app = make_app(tmp_path)
    chats, _ = person(app, "chats")
    html = chats.get("/orders").get_data(as_text=True)
    assert 'href="/orders"' in html and 'href="/chats"' in html
    for hidden in ('href="/economics"', 'href="/connections"', 'href="/users"', 'href="/settings"', 'href="/orders/report"', 'href="/orders/bulk"',
                   'href="/orders/settings"'):
        assert hidden not in html, hidden
    owner = app.test_client()
    login(owner)
    html = owner.get("/orders").get_data(as_text=True)
    assert all(h in html for h in ("Продажи", "Товар и фид", "Аналитика", "Настройка", 'href="/users"', 'href="/orders/report"'))


# --- the manager of a point ---------------------------------------------------------------------------------------------------------

def test_the_manager_of_a_point_sees_only_the_orders_of_the_point(tmp_path):
    app = make_app(tmp_path)
    conn = conn_of(app)
    orders.ensure_pipeline(conn, NOW)
    mine = points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk"})
    other = points.save_point(conn, {"name": "Бета", "manager_name": "Б", "channel": "vk"})
    my_card = orders.create_card(conn, {"client_name": "Свой Клиент", "point_id": str(mine)}, NOW, "owner")
    their_card = orders.create_card(conn, {"client_name": "Чужой Клиент", "point_id": str(other)}, NOW, "owner")
    nowhere = orders.create_card(conn, {"client_name": "Без Точки"}, NOW, "owner")
    client, _ = person(app, "point", mine)
    board = client.get("/orders?p=%d" % other).get_data(as_text=True)                      # asking for the other point gives the own one
    assert "Свой Клиент" in board and "Чужой Клиент" not in board and "Без Точки" not in board
    assert client.get("/orders/%d" % my_card).status_code == 200
    assert client.get("/orders/%d" % their_card).status_code == 403
    assert client.get("/orders/%d" % nowhere).status_code == 403
    assert client.post("/orders/%d/note" % their_card, data={"csrf": "tok", "text": "x"}).status_code == 403
    for url in ("/orders/new", "/orders/inbox", "/orders/settings", "/catalog", "/clients", "/shares"):
        assert client.get(url).status_code == 403, url
    assert client.get("/chats").status_code == client.get("/alerts").status_code == 200     # the owner 2026-10-21: the point's own (tests/test_point_rights.py)
    with pytest.raises(users.UserError):
        users.add_user(conn, "nopoint", PASSWORD, NOW, role="point")


def test_the_excel_file_of_a_point_manager_has_only_the_point(tmp_path):
    pytest.importorskip("openpyxl")
    import io
    import openpyxl
    app = make_app(tmp_path)
    conn = conn_of(app)
    orders.ensure_pipeline(conn, NOW)
    mine = points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk"})
    other = points.save_point(conn, {"name": "Бета", "manager_name": "Б", "channel": "vk"})
    orders.create_card(conn, {"client_name": "Свой Клиент", "point_id": str(mine)}, NOW, "owner")
    orders.create_card(conn, {"client_name": "Чужой Клиент", "point_id": str(other)}, NOW, "owner")
    client, _ = person(app, "point", mine)
    book = openpyxl.load_workbook(io.BytesIO(client.get("/orders/export.xlsx?p=%d" % other).data))
    text = " ".join(str(c.value) for row in book.active.iter_rows() for c in row if c.value is not None)
    assert "Свой Клиент" in text and "Чужой Клиент" not in text


# --- users, invitations, «off», logins ----------------------------------------------------------------------------------------------

def test_a_user_made_before_the_roles_is_an_owner_and_keeps_the_session(tmp_path):
    app = make_app(tmp_path)
    conn = conn_of(app)
    conn.execute("INSERT INTO panel_user VALUES (?,?,?)", ("old", users._hash(PASSWORD), NOW))   # as the old code wrote it: no role row
    conn.commit()
    assert users.get_role(conn, "old") == {"role": "owner", "active": True, "point_id": None, "display_name": ""}
    assert [u["role"] for u in users.list_users(conn)] == ["owner"]
    client = app.test_client()
    with client.session_transaction() as session:                                          # a session opened before the update
        session["user"], session["epoch"] = "old", 0
    assert client.get("/users").status_code == 200


def test_the_invitation_is_hashed_one_time_and_expires_in_48_hours(tmp_path):
    app = make_app(tmp_path)
    conn = conn_of(app)
    pid = points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk"})
    token = users.create_invite(conn, "point", pid, "Алексей", NOW, "owner")
    dump = "\n".join(conn.iterdump())
    assert token not in dump and users._token_hash(token) in dump
    assert users.find_invite(conn, token, "2026-10-09T11:59:59") is not None
    assert users.find_invite(conn, token, "2026-10-09T12:00:00") is None                    # 48 hours later
    with pytest.raises(users.UserError):
        users.accept_invite(conn, token, "late", PASSWORD, "2026-10-09T12:00:01")
    assert users.accept_invite(conn, token, "alexey", PASSWORD, NOW) == "alexey"
    assert users.get_role(conn, "alexey") == {"role": "point", "active": True, "point_id": pid, "display_name": "Алексей"}
    with pytest.raises(users.UserError):
        users.accept_invite(conn, token, "second", PASSWORD, NOW)
    assert not users.exists(conn, "second")
    other = users.create_invite(conn, "viewer", None, "", NOW, "owner")
    users.revoke_invite(conn, users.list_invites(conn, NOW)[0]["id"], NOW, "owner")
    assert users.find_invite(conn, other, NOW) is None


def test_the_invitation_through_the_pages(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 7, 12, 0, 0))
    owner = app.test_client()
    tok = login(owner)
    assert post(owner, "/users/invite", tok, role="chats", display_name="Оля").status_code == 302
    page = owner.get("/users").get_data(as_text=True)
    link = re.search(r'value="(http://localhost/invite/[^"]+)"', page).group(1)
    assert link not in owner.get("/users").get_data(as_text=True)                          # shown once
    guest = app.test_client()
    path = link.replace("http://localhost", "")
    assert "Менеджер чатов" in guest.get(path).get_data(as_text=True)
    tok2 = _csrf(guest)
    assert guest.post(path, data={"csrf": tok2, "login": "olya", "password": PASSWORD, "password2": "другой-пароль-2"}).status_code == 400
    assert guest.post(path, data={"csrf": tok2, "login": "olya", "password": PASSWORD, "password2": PASSWORD}).status_code == 302
    assert guest.get(path).status_code == 404
    assert guest.post("/login", data={"csrf": _csrf(guest), "login": "olya", "password": PASSWORD}).status_code == 302
    assert guest.get("/chats").status_code == 200 and guest.get("/users").status_code == 403
    assert guest.get("/invite/not-a-token").status_code == 404


def test_a_switched_off_user_cannot_sign_in_and_the_open_session_ends(tmp_path):
    app = make_app(tmp_path)
    client, conn = person(app, "chats", login_="olya")
    assert client.get("/chats").status_code == 200
    users.set_active(conn, "olya", False, NOW, "owner")
    assert client.get("/chats").status_code == 302
    fresh = app.test_client()
    assert fresh.post("/login", data={"csrf": _csrf(fresh), "login": "olya", "password": PASSWORD}).status_code == 401
    users.set_active(conn, "olya", True, NOW, "owner")
    assert fresh.post("/login", data={"csrf": _csrf(fresh), "login": "olya", "password": PASSWORD}).status_code == 302
    assert [u["login"] for u in users.list_users(conn)] == ["olya"]                         # the history and the user stay
    with pytest.raises(users.UserError):
        users.set_active(conn, "olya", False, NOW, "olya")                                   # not oneself


def test_the_owner_page_changes_roles_and_switches_off(tmp_path):
    app = make_app(tmp_path)
    conn = conn_of(app)
    users.add_user(conn, "olya", PASSWORD, NOW, role="viewer")
    owner = app.test_client()
    tok = login(owner)
    post(owner, "/users/olya/role", tok, role="partner", display_name="Ольга", point_id="")
    assert users.get_role(conn, "olya")["role"] == "partner" and users.get_role(conn, "olya")["display_name"] == "Ольга"
    post(owner, "/users/olya/active", tok, active="0")
    assert users.get_role(conn, "olya")["active"] is False
    actions = [a["action"] for a in store.list_audit(conn)]
    assert "user_role" in actions and "user_off" in actions


def test_logins_and_failed_logins_are_in_the_journal_without_the_password(tmp_path):
    app = make_app(tmp_path)
    conn = conn_of(app)
    users.add_user(conn, "olya", PASSWORD, NOW, role="viewer")
    client = app.test_client()
    client.post("/login", data={"csrf": _csrf(client), "login": "olya", "password": "неверный-пароль"})
    client.post("/login", data={"csrf": _csrf(client), "login": "olya", "password": PASSWORD})
    journal = store.list_audit(conn)
    failed = next(a for a in journal if a["action"] == "login_failed")
    ok = next(a for a in journal if a["action"] == "login")
    assert failed["detail"]["login"] == "olya" and failed["detail"]["ip"] and ok["author"] == "olya" and ok["detail"]["ip"]
    assert "неверный-пароль" not in str(journal) and PASSWORD not in str(journal)


def test_the_own_password(tmp_path):
    app = make_app(tmp_path)
    client, conn = person(app, "chats", login_="olya")
    assert client.post("/account/password", data={"csrf": "tok", "old": "не-тот-пароль", "new": "новый-пароль-22", "new2": "новый-пароль-22"}).status_code == 302
    assert users.verify(conn, "olya", PASSWORD) == "olya"
    client.post("/account/password", data={"csrf": "tok", "old": PASSWORD, "new": "новый-пароль-22", "new2": "новый-пароль-22"})
    assert users.verify(conn, "olya", "новый-пароль-22") == "olya" and client.get("/chats").status_code == 200   # this session stays


def test_the_error_pages_are_in_the_panel(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    page = client.get("/no-such-page")
    assert page.status_code == 404 and "Страница не найдена" in page.get_data(as_text=True) and "sb-link" in page.get_data(as_text=True)
    chats, _ = person(app, "chats")
    denied = chats.get("/users")
    assert denied.status_code == 403 and "спросите владельца" in denied.get_data(as_text=True)


def test_drafts_with_markups_are_closed_without_the_owner_setup(tmp_path):
    """The partner looks at the settings since 2026-10-21 (the owner: «смотрит всё»; the partner has the money right): the others do not."""
    app = make_app(tmp_path)
    for role in ("chats", "viewer"):
        client, _ = person(app, role)
        for url in ("/draft/1", "/catalog/review", "/publish/result"):
            assert client.get(url).status_code == 403, (role, url)


def test_moving_a_card_does_not_let_a_point_manager_change_its_fields(tmp_path):
    app = make_app(tmp_path)
    conn = conn_of(app)
    orders.ensure_pipeline(conn, NOW)
    mine = points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk"})
    other = points.save_point(conn, {"name": "Бета", "manager_name": "Б", "channel": "vk"})
    card_id = orders.create_card(conn, {"client_name": "Свой Клиент", "client_phone": "+79001112233", "point_id": str(mine)}, NOW, "owner")
    stage = orders.get_card(conn, card_id)["stage_id"]
    target = next(s["id"] for s in orders.list_stages(conn) if s["id"] != stage and s["kind"] == "open")
    client, _ = person(app, "point", mine)
    assert client.post("/orders/%d/move" % card_id, data={"csrf": "tok", "stage_id": str(target), "point_id": str(other),
                                                          "client_phone": "+79990000000"}).status_code in (200, 302)
    card = orders.get_card(conn, card_id)
    assert card["stage_id"] == target and card["point_id"] == mine and card["client_phone"] == "+79001112233"


def test_the_money_operations_of_an_order_and_the_partner_analytics(tmp_path):
    app = make_app(tmp_path)
    chats, _ = person(app, "chats")
    assert chats.post("/orders/1/sale/reverse", data={"csrf": "tok"}).status_code == 403
    assert chats.post("/orders/1/sale", data={"csrf": "tok"}).status_code != 403
    partner, _ = person(app, "partner")
    assert partner.get("/economics").status_code == 200 and partner.post("/economics/sales/add", data={"csrf": "tok"}).status_code == 403
    assert partner.post("/orders/1/sale/reverse", data={"csrf": "tok"}).status_code != 403


def test_analytics_needs_the_money_right_even_if_a_role_is_given_the_section(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    viewer, _ = person(app, "viewer")
    monkeypatch.setitem(users.MATRIX["viewer"]["sections"], "analytics", users.VIEW)          # a slip in the table of rights
    assert viewer.get("/economics").status_code == 403 and viewer.get("/orders/report").status_code == 403


def test_moving_a_card_still_saves_the_fields_for_the_other_roles(tmp_path):
    app = make_app(tmp_path)
    conn = conn_of(app)
    orders.ensure_pipeline(conn, NOW)
    card_id = orders.create_card(conn, {"client_name": "Клиент"}, NOW, "owner")
    stage = orders.get_card(conn, card_id)["stage_id"]
    target = next(s["id"] for s in orders.list_stages(conn) if s["id"] != stage and s["kind"] == "open")
    owner = app.test_client()
    tok = login(owner)
    post(owner, "/orders/%d/move" % card_id, tok, stage_id=str(target), client_phone="+79001112233", comment="позвонить вечером")
    card = orders.get_card(conn, card_id)
    assert card["stage_id"] == target and card["client_phone"] == "+79001112233" and card["comment"] == "позвонить вечером"
