"""«Работать как точка» (the owner 2026-11-02, base-plan part 3): the owner and the partner work as the manager of a point — exactly its
pages, its orders only, the banner and «Выйти»; every action's author «<name> за точку «X»»; the mode is checked on every request and ends
with «Выйти», a new password, a sign-out, a role taken away or the point deleted. Nobody else can set it; a forged point is refused."""
from __future__ import annotations

import json

import pytest

from panel_helpers import login, make_app, post
from test_roles import PASSWORD, conn_of, sign_in
from order_fixtures import card_of_phone
from tires_sync import order_points as points, orders, store, tasks, users
from tires_sync.panel import common

NOW = "2026-10-07T12:00:00"


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


@pytest.fixture
def w(tmp_path):
    app = make_app(tmp_path)
    conn = conn_of(app)
    orders.ensure_pipeline(conn, NOW)
    pa = points.save_point(conn, {"name": "Янино", "manager_name": "А", "channel": "vk"})
    pb = points.save_point(conn, {"name": "Юнона", "manager_name": "Б", "channel": "vk"})
    mine = card_of_phone(conn, {"client_name": "Свой", "client_phone": "+79001110001", "point_id": str(pa)}, NOW, "o")
    other = card_of_phone(conn, {"client_name": "Чужой", "client_phone": "+79002220002", "point_id": str(pb)}, NOW, "o")
    users.add_user(conn, "zaman", PASSWORD, NOW, role="partner", display_name="Заман")
    return {"app": app, "conn": conn, "pa": pa, "pb": pb, "mine": mine, "other": other}


def owner(w):
    client = w["app"].test_client()
    login(client)
    return client


def partner(w):
    client = w["app"].test_client()
    sign_in(client, w["conn"], "zaman")
    return client


def as_point(client, point_id) -> int:
    return post(client, "/as-point", "tok", point_id=str(point_id)).status_code


def session_point(client):
    with client.session_transaction() as session:
        return session.get("as_point")


def audits(conn, action):
    return [(r[0], json.loads(r[1])) for r in conn.execute("SELECT author, detail FROM audit WHERE action = ? ORDER BY id", (action,))]


# --- who may --------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("who", ["owner", "partner"])
def test_the_owner_and_the_partner_start_it_and_land_on_the_points_page(w, who):
    client = owner(w) if who == "owner" else partner(w)
    response = post(client, "/as-point", "tok", point_id=str(w["pa"]))
    assert response.status_code == 302 and response.headers["Location"].endswith("/my-point")
    assert session_point(client) == w["pa"]
    login_ = "owner" if who == "owner" else "zaman"
    assert audits(w["conn"], "as_point_start") == [(login_, {"point": w["pa"], "name": "Янино"})]


@pytest.mark.parametrize("role", ["chats", "viewer", "point", "client"])
def test_nobody_else_can_set_it(w, role):
    conn = w["conn"]
    users.add_user(conn, role + "1", PASSWORD, NOW, role=role, point_id=w["pb"] if role == "point" else None)
    client = w["app"].test_client()
    sign_in(client, conn, role + "1")
    assert as_point(client, w["pa"]) == 403
    assert session_point(client) is None
    assert post(client, "/as-point/stop", "tok").status_code == 403
    assert audits(conn, "as_point_start") == []


@pytest.mark.parametrize("forged", ["999", "abc", "", "-1", "1.5", "True"])
def test_a_forged_point_is_refused(w, forged):
    client = owner(w)
    assert post(client, "/as-point", "tok", point_id=forged).status_code == 400
    assert session_point(client) is None and audits(w["conn"], "as_point_start") == []


def test_it_needs_the_csrf_token_and_a_post(w):
    client = owner(w)
    assert client.post("/as-point", data={"point_id": str(w["pa"])}).status_code == 400
    assert client.post("/as-point", data={"point_id": str(w["pa"]), "csrf": "bad"}).status_code == 400
    assert client.get("/as-point").status_code == 405
    assert session_point(client) is None


def test_signed_out_nothing(w):
    client = w["app"].test_client()
    with client.session_transaction() as session:
        session["csrf"] = "tok"
    response = post(client, "/as-point", "tok", point_id=str(w["pa"]))
    assert response.status_code == 302 and "/login" in response.headers["Location"]
    assert session_point(client) is None


def test_a_planted_session_point_of_a_viewer_gives_nothing(w):
    """A session as_point of a role that may not have it (a role changed meanwhile): the person is their own role, the mode is gone."""
    conn = w["conn"]
    users.add_user(conn, "view1", PASSWORD, NOW, role="viewer")
    client = w["app"].test_client()
    sign_in(client, conn, "view1")
    with client.session_transaction() as session:
        session["as_point"] = w["pa"]
    assert client.get("/my-point").status_code == 403                    # the viewer has no «Рабочее место»
    assert client.get("/orders/%d" % w["mine"]).status_code == 200      # its own right: the orders, all of them
    assert session_point(client) is None
    assert audits(conn, "as_point_stop") == [("view1", {"point": w["pa"], "name": "Янино", "why": "invalid"})]


# --- exactly the point ---------------------------------------------------------------------------------------------------------------

def test_as_a_point_the_panel_is_the_points_own(w):
    client = owner(w)
    assert as_point(client, w["pa"]) == 302
    page = client.get("/my-point")
    text = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "Вы работаете как точка «Янино»" in text and 'action="/as-point/stop"' in text
    assert "Работать как точка ▾" not in text                            # no second point while in one
    assert 'href="/connections"' not in text and 'href="/users"' not in text   # the point's menu
    assert client.get("/").headers["Location"].endswith("/my-point")
    for closed in ("/connections/webmim", "/users", "/catalog", "/economics", "/ui-new", "/settings", "/orders/%d" % w["other"]):
        assert client.get(closed).status_code == 403, closed
    assert client.get("/orders/%d" % w["mine"]).status_code == 200
    assert post(client, "/orders/%d/note" % w["other"], "tok", text="x").status_code == 403
    assert post(client, "/as-point", "tok", point_id=str(w["pb"])).status_code == 409    # «Выйти» first
    assert session_point(client) == w["pa"]


def test_without_the_mode_the_owner_sees_the_list_and_no_banner(w):
    text = owner(w).get("/orders").get_data(as_text=True)
    assert "Работать как точка ▾" in text and "Янино" in text and "Вы работаете как точка" not in text


def test_the_point_and_the_viewer_see_no_list(w):
    conn = w["conn"]
    users.add_user(conn, "pt1", PASSWORD, NOW, role="point", point_id=w["pa"])
    users.add_user(conn, "view1", PASSWORD, NOW, role="viewer")
    for who, page in (("pt1", "/my-point"), ("view1", "/orders")):
        client = w["app"].test_client()
        sign_in(client, conn, who)
        assert "Работать как точка" not in client.get(page).get_data(as_text=True)


def test_current_user_as_a_point(w):
    client = partner(w)
    as_point(client, w["pa"])
    with w["app"].test_request_context("/my-point"):
        from flask import session
        session.update(user="zaman", as_point=w["pa"])
        user = common.current_user()
        assert (user["role"], user["point_id"], user["real_role"], user["login"]) == ("point", w["pa"], "partner", "zaman")
        assert common.own_point() == w["pa"] and common.can("setup") is False and common.has_right("supplier") is False
        assert common.author() == "Заман за точку «Янино»"


# --- the author -----------------------------------------------------------------------------------------------------------------------

def test_every_action_names_the_person_for_the_point(w):
    conn = w["conn"]
    users.set_main_name(conn, "Павел", NOW, "owner")
    client = owner(w)
    as_point(client, w["pa"])
    assert post(client, "/orders/%d/note" % w["mine"], "tok", text="позвонить").status_code == 302
    note = conn.execute("SELECT author FROM order_event WHERE order_id = ? AND kind = 'note' ORDER BY id DESC LIMIT 1", (w["mine"],)).fetchone()
    assert note[0] == "Павел за точку «Янино»"
    stage = conn.execute("SELECT id FROM order_stage WHERE id != (SELECT stage_id FROM order_card WHERE id = ?) ORDER BY position LIMIT 1",
                         (w["mine"],)).fetchone()[0]
    assert post(client, "/orders/%d/move" % w["mine"], "tok", stage_id=str(stage)).status_code == 302
    logged = conn.execute("SELECT author FROM audit WHERE action = 'point_action' ORDER BY id DESC LIMIT 1").fetchone()
    assert logged[0] == "Павел за точку «Янино»"
    moved = conn.execute("SELECT author FROM order_event WHERE order_id = ? AND kind = 'stage' ORDER BY id DESC LIMIT 1", (w["mine"],)).fetchone()
    assert moved[0] == "Павел за точку «Янино»"


def test_the_partner_is_named_by_the_display_name(w):
    client = partner(w)
    as_point(client, w["pa"])
    post(client, "/orders/%d/note" % w["mine"], "tok", text="x")
    row = w["conn"].execute("SELECT author FROM order_event WHERE order_id = ? AND kind = 'note'", (w["mine"],)).fetchone()
    assert row[0] == "Заман за точку «Янино»"


def test_out_of_the_mode_the_author_is_the_login(w):
    client = partner(w)
    post(client, "/orders/%d/note" % w["mine"], "tok", text="x")
    assert w["conn"].execute("SELECT author FROM order_event WHERE kind = 'note'").fetchone()[0] == "zaman"


# --- the end of it ----------------------------------------------------------------------------------------------------------------------

def test_exit_ends_it_and_the_owner_is_back(w):
    client = owner(w)
    as_point(client, w["pa"])
    response = post(client, "/as-point/stop", "tok")
    assert response.status_code == 302 and session_point(client) is None
    assert client.get("/connections/webmim").status_code == 200 and client.get("/orders/%d" % w["other"]).status_code == 200
    assert audits(w["conn"], "as_point_stop") == [("owner", {"point": w["pa"], "name": "Янино", "why": "exit"})]
    assert client.post("/as-point/stop", data={}).status_code == 400       # CSRF as everywhere


def test_logout_ends_it(w):
    client = owner(w)
    as_point(client, w["pa"])
    post(client, "/logout", "tok")
    assert session_point(client) is None


def test_a_new_epoch_ends_it(w):
    client = partner(w)
    as_point(client, w["pa"])
    store.bump_session_epoch(w["conn"], "zaman")                         # «Выйти везде», another sign-out, a new password elsewhere
    assert client.get("/my-point").status_code == 302
    assert session_point(client) is None


def test_a_new_password_ends_it_and_the_session_stays(w):
    client = partner(w)
    as_point(client, w["pa"])
    response = post(client, "/account/password", "tok", old=PASSWORD, new="другой-пароль-22", new2="другой-пароль-22")
    assert response.status_code == 302
    assert session_point(client) is None
    assert client.get("/orders/%d" % w["other"]).status_code == 200      # the partner again, still signed in
    assert audits(w["conn"], "as_point_stop") == [("zaman", {"point": w["pa"], "name": "Янино", "why": "password"})]


def test_a_role_taken_away_ends_it_on_the_next_request(w):
    conn = w["conn"]
    client = partner(w)
    as_point(client, w["pa"])
    conn.execute("UPDATE panel_user_role SET role = 'chats' WHERE login = 'zaman'")      # without an epoch: the check of every request
    conn.commit()
    assert client.get("/my-point").status_code == 403                   # the chats' manager, not the point
    assert session_point(client) is None
    assert audits(conn, "as_point_stop")[-1] == ("zaman", {"point": w["pa"], "name": "Янино", "why": "invalid"})


def test_the_point_deleted_ends_it(w):
    conn = w["conn"]
    client = owner(w)
    as_point(client, w["pa"])
    conn.execute("DELETE FROM point WHERE id = ?", (w["pa"],))
    conn.commit()
    assert client.get("/connections/webmim").status_code == 200          # the owner again
    assert session_point(client) is None


# --- the places that compare the login with the owner or the assignee -----------------------------------------------------------------

def test_tasks_as_a_point_are_the_persons_own_of_that_point_only(w):
    conn = w["conn"]
    common_args = dict(kind="other", note="", now=NOW, author="owner", assignee="owner", owner_login="owner", allowed={"owner"})
    tasks.add(conn, title="своей точки", due_at="2026-10-07T13:00", order_id=w["mine"], **common_args)
    tasks.add(conn, title="чужой точки", due_at="2026-10-07T13:00", order_id=w["other"], **common_args)
    tasks.add(conn, title="без заказа", due_at="2026-10-07T13:00", **common_args)
    client = owner(w)
    office = client.get("/tasks").get_data(as_text=True)
    assert "своей точки" in office and "чужой точки" in office and "без заказа" in office
    as_point(client, w["pa"])
    text = client.get("/tasks").get_data(as_text=True)
    assert "своей точки" in text and "без заказа" in text and "чужой точки" not in text
    assert tasks.counter(conn, "owner", "owner", NOW, True, as_point=w["pa"])["count"] == 2


def test_the_bell_as_a_point_is_the_points(w):
    """The owner's own bell leaves out the orders of the points' accounts (order_alerts._staff); as a point it is the point's, and what
    the person pressed as a point is their own record, never an alert to themselves."""
    from tires_sync import order_alerts
    conn = w["conn"]
    orders.add_event(conn, w["mine"], NOW, "Павел за точку «Янино»", "point", "сам нажал", level="yellow")
    orders.add_event(conn, w["mine"], NOW, "pt1", "point", "нажала точка", level="yellow")
    users.set_main_name(conn, "Павел", NOW, "owner")
    texts = [r["text"] for r in order_alerts.page_rows(conn, "owner", None, own_prefix="Павел за точку «")]
    assert "нажала точка" in texts and "сам нажал" not in texts
    assert "сам нажал" in [r["text"] for r in order_alerts.page_rows(conn, "owner", None)]


# --- the review (2026-11-02): tasks, the end by a sign-out, the person's own settings -------------------------------------------------

def test_a_task_set_and_closed_as_a_point_names_the_person_for_the_point(w):
    conn = w["conn"]
    users.set_main_name(conn, "Павел", NOW, "owner")
    client = owner(w)
    as_point(client, w["pa"])
    assert post(client, "/tasks/new", "tok", kind="other", title="позвонить", date="2026-10-08", time="10:00",
                order_id=str(w["mine"])).status_code == 302
    task = conn.execute("SELECT id, created_by, assignee FROM callback_task").fetchone()
    assert task[1:] == ("owner", "owner")                                 # the person's own: «Поставил я», «Мне»
    assert post(client, "/tasks/%d/done" % task[0], "tok", result="ок").status_code == 302
    journal = [r[0] for r in conn.execute("SELECT author FROM order_event WHERE order_id = ? AND kind = 'task' ORDER BY id", (w["mine"],))]
    assert journal == ["Павел за точку «Янино»"] * 2
    assert [r[0] for r in conn.execute("SELECT author FROM audit WHERE action IN ('callback', 'callback_done') ORDER BY id")] == \
        ["Павел за точку «Янино»"] * 2
    assert conn.execute("SELECT done_by FROM callback_task").fetchone()[0] == "owner"


def test_as_a_point_a_task_of_another_point_is_not_closed(w):
    conn = w["conn"]
    tasks.add(conn, kind="other", title="чужой точки", due_at="2026-10-07T13:00", note="", now=NOW, author="owner", assignee="owner",
              owner_login="owner", order_id=w["other"], allowed={"owner"})
    client = owner(w)
    as_point(client, w["pa"])
    assert post(client, "/tasks/1/done", "tok").status_code == 403
    assert conn.execute("SELECT done_at FROM callback_task").fetchone()[0] is None


def test_a_sign_out_while_a_point_is_in_the_journal(w):
    client = owner(w)
    as_point(client, w["pa"])
    post(client, "/logout", "tok")
    other = partner(w)
    as_point(other, w["pb"])
    store.bump_session_epoch(w["conn"], "zaman")
    other.get("/my-point")
    assert audits(w["conn"], "as_point_stop") == [("owner", {"point": w["pa"], "name": "Янино", "why": "logout"}),
                                                  ("zaman", {"point": w["pb"], "name": "Юнона", "why": "signed_out"})]


def test_the_stop_names_the_point(w):
    from tires_sync.panel import texts
    client = owner(w)
    as_point(client, w["pa"])
    post(client, "/as-point/stop", "tok")
    detail = audits(w["conn"], "as_point_stop")[0][1]
    assert texts.audit_text({"action": "as_point_stop", "detail": detail}) == "Закончил работать как точка «Янино»"


def test_the_persons_own_settings_are_not_changed_from_the_point(w):
    """The columns of the list and the mark of the tour are the person's own (user_pref): as a point they stay as the office left them."""
    from tires_sync import user_prefs
    conn = w["conn"]
    client = owner(w)
    as_point(client, w["pa"])
    before = conn.execute("SELECT COUNT(*) FROM user_pref").fetchone()[0]
    post(client, "/orders/list/columns", "tok", order="Имя", shown="Имя")                # a real column: a save would write it
    post(client, "/tour/done", "tok", how="skip")
    post(client, "/tour/again", "tok")
    assert conn.execute("SELECT COUNT(*) FROM user_pref").fetchone()[0] == before
    assert 'data-tour-root' not in client.get("/my-point").get_data(as_text=True)
