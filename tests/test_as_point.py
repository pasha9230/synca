"""«Работать как точка» (the owner 2026-11-02, base-plan part 3): the owner and the partner work as the manager of a point — exactly its
pages, its orders only, the banner and «Выйти»; every action's author «<name> за точку «X»»; the mode is checked on every request and ends
with «Выйти», a new password, a sign-out, a role taken away or the point deleted. Nobody else can set it; a forged point is refused."""
from __future__ import annotations

import json

import pytest

from panel_helpers import assert_card_closed_to_point, login, make_app, post
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
    assert_card_closed_to_point(client, w["mine"])                       # the owner 13.11: as a point, no card of our order (its own sale opens)
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
    users.add_user(conn, "pt1", PASSWORD, NOW, role="point", point_id=w["pa"])
    client = owner(w)
    as_point(client, w["pa"])
    assert post(client, "/tasks/new", "tok", kind="other", title="позвонить", date="2026-10-08", time="10:00",
                order_id=str(w["mine"])).status_code == 302
    task = conn.execute("SELECT id, created_by, assignee FROM callback_task").fetchone()
    assert task[1:] == ("owner", "pt1")                                   # set by the person, to the point's manager
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
    """The columns of the list, the mark of the tour and the filters of the lists are the person's own (user_pref): as a point they stay as
    the office left them."""
    conn = w["conn"]
    client = owner(w)
    client.get("/orders?t=1", headers={"Referer": "http://localhost/orders"})          # the office's filter of the list
    as_point(client, w["pa"])
    before = conn.execute("SELECT login, key, value FROM user_pref ORDER BY login, key").fetchall()
    assert before == [("owner", "f.orders_page", '{"t":["1"]}')]
    post(client, "/orders/list/columns", "tok", order="Имя", shown="Имя")                # a real column: a save would write it
    post(client, "/tour/done", "tok", how="skip")
    post(client, "/tour/again", "tok")
    client.get("/orders?t=2", headers={"Referer": "http://localhost/orders"})
    assert conn.execute("SELECT login, key, value FROM user_pref ORDER BY login, key").fetchall() == before
    client.get("/orders?reset=1", headers={"Referer": "http://localhost/orders"})
    assert conn.execute("SELECT login, key, value FROM user_pref ORDER BY login, key").fetchall() == before
    assert 'data-tour-root' not in client.get("/my-point").get_data(as_text=True)


# --- the lead's decisions (2026-11-02): «Разобрался», Telegram to oneself, the point's tasks, the responsible ---------------------------

def test_ack_as_a_point_names_the_person_for_the_point_and_closes_it_for_them(w):
    conn = w["conn"]
    users.set_main_name(conn, "Павел", NOW, "owner")
    event = orders.add_event(conn, w["mine"], NOW, "система", "notify", "Оповещение не отправлено", level="red")
    conn.commit()
    event = event or conn.execute("SELECT MAX(id) FROM order_event").fetchone()[0]
    client = owner(w)
    as_point(client, w["pa"])
    assert post(client, "/alerts/%d/ack" % event, "tok").status_code == 302
    assert conn.execute("SELECT login, author FROM order_event_ack WHERE event_id = ?", (event,)).fetchall() == \
        [("owner", "Павел за точку «Янино»")]                             # the mark is the person's own; who pressed: for the point


def _chats(w):
    from cryptography.fernet import Fernet
    from tires_sync import connections, order_notify
    from tires_sync.secretbox import SecretBox
    conn, box = w["conn"], SecretBox(Fernet.generate_key())
    w["app"].config["SECRET_BOX"] = box
    connections.set_config(conn, "telegram", 0, {"chat_id": "1000"}, NOW, "owner")       # the owner's chat
    connections.set_secret(conn, box, "telegram", 0, "bot_token", "123456:ABCdef-TEST", NOW, "owner")
    order_notify.set_user_chat(conn, "zaman", "2000", NOW, "owner")


@pytest.mark.usefixtures("telegram_for_everything")       # the mechanics of Telegram: the setting of critical.py off (test_critical.py: the rule)
def test_telegram_about_ones_own_press_as_a_point_is_not_sent_to_oneself(w):
    from flask import session
    from http_fakes import ScriptedHttp
    from tires_sync.panel import routes_mypoint, routes_vk
    conn = w["conn"]
    _chats(w)
    tg = ScriptedHttp(*[(200, {"ok": True, "result": {}})] * 10)
    w["app"].config["TG_HTTP"] = tg

    def told():
        found = {str(json.loads(c[3]).get("chat_id")) for c in tg.calls}
        tg.calls.clear()
        return found
    with w["app"].test_request_context("/my-point"):
        session.update(user="owner", as_point=w["pa"])
        routes_mypoint._tell(conn, NOW)(["owner", "zaman"], "точка отметила")
        assert told() == {"2000"}                                         # the partner hears; the owner who pressed does not
        _send, _deliver, notify = routes_vk.channel(w["app"], conn, NOW)
        card = orders.get_card(conn, w["mine"])
        answer = notify(dict(card, responsible="owner", to_staff=True), "заказ")
        assert "нажал сам" in answer and told() == set()
        note = conn.execute("SELECT text FROM order_event WHERE order_id = ? AND kind = 'notify' ORDER BY id DESC LIMIT 1", (w["mine"],)).fetchone()
        assert note[0] == "Оповещение в свой Telegram не отправлено: нажал сам, работая за точку"     # the journal says why, no failure
        notify(dict(card, responsible="zaman", to_staff=True), "заказ")
        assert told() == {"2000"}
    with w["app"].test_request_context("/my-point"):                      # out of the mode: as before, the owner is told
        session.update(user="owner")
        routes_mypoint._tell(conn, NOW)(["owner"], "точка отметила снова")
        assert told() == {"1000"}


@pytest.fixture
def staff(w):
    conn = w["conn"]
    users.add_user(conn, "pt1", PASSWORD, NOW, role="point", point_id=w["pa"])
    users.add_user(conn, "pt2", PASSWORD, NOW, role="point", point_id=w["pb"])
    args = dict(kind="other", note="", now=NOW, author="owner", owner_login="owner", allowed={"owner", "pt1", "pt2"})
    ids = {
        "pt1_order": tasks.add(conn, title="менеджеру точки по заказу", due_at="2026-10-07T13:00", assignee="pt1", order_id=w["mine"], **args),
        "pt1_free": tasks.add(conn, title="менеджеру точки без заказа", due_at="2026-10-07T13:00", assignee="pt1", **args),
        "owner": tasks.add(conn, title="владельцу по заказу точки", due_at="2026-10-07T13:00", assignee="owner", order_id=w["mine"], **args),
        "pt2": tasks.add(conn, title="другой точке", due_at="2026-10-07T13:00", assignee="pt2", order_id=w["other"], **args),
    }
    return ids


def test_tasks_as_a_point_are_exactly_the_points_managers(w, staff):
    conn = w["conn"]
    client = owner(w)
    as_point(client, w["pa"])
    text = client.get("/tasks").get_data(as_text=True)
    assert "менеджеру точки по заказу" in text and "менеджеру точки без заказа" in text
    assert "владельцу по заказу точки" not in text and "другой точке" not in text
    assert tasks.counter(conn, "owner", "owner", NOW, True, as_point=w["pa"])["count"] == 2
    assert_card_closed_to_point(client, w["mine"], hidden=("менеджеру точки по заказу",))     # the owner 13.11: no card of our order, the task is in /tasks
    assert post(client, "/tasks/%d/cancel" % staff["owner"], "tok").status_code == 403         # the person's own: after «Выйти»
    assert post(client, "/tasks/%d/cancel" % staff["pt2"], "tok").status_code == 403
    assert post(client, "/tasks/%d/done" % staff["pt1_free"], "tok", result="ок").status_code == 302
    assert conn.execute("SELECT done_by FROM callback_task WHERE id = ?", (staff["pt1_free"],)).fetchone()[0] == "owner"
    assert post(client, "/tasks/new", "tok", kind="other", title="себе", date="2026-10-08", time="10:00", assignee="owner").status_code == 403
    assert post(client, "/tasks/new", "tok", kind="other", title="точке", date="2026-10-08", time="10:00", assignee="pt1").status_code == 302
    assert conn.execute("SELECT assignee, created_by FROM callback_task WHERE title = 'точке'").fetchone() == ("pt1", "owner")
    post(client, "/as-point/stop", "tok")
    assert "владельцу по заказу точки" in client.get("/tasks").get_data(as_text=True)          # back after «Выйти»


def test_the_done_tasks_as_a_point_are_exactly_the_points_managers(w, staff):
    conn = w["conn"]
    client = owner(w)
    conn.execute("UPDATE callback_task SET done_at = ?, done_by = 'owner'", (NOW,))
    conn.commit()
    as_point(client, w["pa"])
    for tab in ("mine", "by_me", "all"):
        text = client.get("/tasks?tab=%s&view=done" % tab).get_data(as_text=True)
        assert "менеджеру точки по заказу" in text and "менеджеру точки без заказа" in text, tab
        assert "владельцу по заказу точки" not in text and "другой точке" not in text, tab
    post(client, "/as-point/stop", "tok")
    assert "владельцу по заказу точки" in client.get("/tasks?view=done").get_data(as_text=True)     # back after «Выйти»


def test_a_point_without_a_manager_has_no_tasks_and_sets_none(w):
    client = owner(w)
    as_point(client, w["pa"])
    assert "Задач нет" in client.get("/tasks").get_data(as_text=True)
    post(client, "/tasks/new", "tok", kind="other", title="никому", date="2026-10-08", time="10:00")
    assert w["conn"].execute("SELECT COUNT(*) FROM callback_task").fetchone()[0] == 0


def test_an_order_made_for_a_point_never_has_the_text_as_its_responsible(w):
    """created_by is the journal's «<name> за точку «X»»; the responsible is a login or empty (the rules of the point then), never that text."""
    conn = w["conn"]
    users.set_main_name(conn, "Павел", NOW, "owner")
    with w["app"].test_request_context("/"):
        from flask import session
        session.update(user="owner", as_point=w["pa"])
        who = common.author()
    assert orders.ACTING_MARK in who
    card_id = orders.create_card(conn, {"client_name": "Новый", "client_phone": "+79005550001", "point_id": str(w["pa"])}, NOW, who)
    card = orders.get_card(conn, card_id)
    assert card["responsible"] == "" and orders.ACTING_MARK not in card["responsible"]
    from tires_sync import order_notify
    way = order_notify.route(conn, card)
    assert way["channel"] != "telegram" and "Янино" in way["why"]          # as a point manager's order: through the point
    made = orders.create_card(conn, {"client_name": "Другой", "client_phone": "+79005550002"}, NOW, "zaman")
    assert orders.get_card(conn, made)["responsible"] == "zaman"           # a login: as before


@pytest.mark.usefixtures("telegram_for_everything")       # the mechanics of Telegram: the setting of critical.py off (test_critical.py: the rule)
def test_a_chat_shared_with_another_person_still_hears_of_the_press(w):
    """The skip is by the person, not by the chat: the partner who shares the owner's chat of the alerts still gets it."""
    from flask import session
    from http_fakes import ScriptedHttp
    from tires_sync import order_notify
    from tires_sync.panel import routes_mypoint, routes_vk
    conn = w["conn"]
    _chats(w)
    order_notify.set_user_chat(conn, "zaman", "1000", NOW, "owner")       # one group chat for both
    tg = ScriptedHttp(*[(200, {"ok": True, "result": {}})] * 10)
    w["app"].config["TG_HTTP"] = tg
    with w["app"].test_request_context("/my-point"):
        session.update(user="owner", as_point=w["pa"])
        routes_mypoint._tell(conn, NOW)(["owner", "zaman"], "точка отметила")
        assert {str(json.loads(c[3]).get("chat_id")) for c in tg.calls} == {"1000"}
        tg.calls.clear()
        _send, _deliver, notify = routes_vk.channel(w["app"], conn, NOW)
        notify(dict(orders.get_card(conn, w["mine"]), responsible="zaman", to_staff=True), "заказ")
        assert {str(json.loads(c[3]).get("chat_id")) for c in tg.calls} == {"1000"}


def test_a_switched_off_manager_is_listed_but_never_given_a_task(w):
    conn = w["conn"]
    users.add_user(conn, "pa_old", PASSWORD, NOW, role="point", point_id=w["pa"])
    users.add_user(conn, "pb_new", PASSWORD, NOW, role="point", point_id=w["pa"])
    tasks.add(conn, kind="other", title="старому менеджеру", due_at="2026-10-07T13:00", note="", now=NOW, author="owner", assignee="pa_old",
              owner_login="owner", allowed={"pa_old"})
    users.set_active(conn, "pa_old", False, NOW, "owner")
    client = owner(w)
    as_point(client, w["pa"])
    page = client.get("/tasks").get_data(as_text=True)
    assert "старому менеджеру" in page                                      # its open task is still the point's
    assert 'value="pa_old"' not in page and 'value="pb_new"' in page        # «Кому»: who can sign in
    assert post(client, "/tasks/new", "tok", kind="other", title="без кому", date="2026-10-08", time="10:00").status_code == 302
    assert conn.execute("SELECT assignee FROM callback_task WHERE title = 'без кому'").fetchone()[0] == "pb_new"
    assert post(client, "/tasks/new", "tok", kind="other", title="выключенному", date="2026-10-08", time="10:00",
                assignee="pa_old").status_code == 403
