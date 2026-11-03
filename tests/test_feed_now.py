"""«Собрать фид сейчас» (feed_now): the usual cycle with publication in a process of its own; refused while a cycle builds, within
5 minutes after one started, while the button's own still runs, and just before the hourly cycle; the owner and the partner only."""
from __future__ import annotations

import json
from datetime import datetime

from panel_helpers import login, make_app, post
from tires_sync import feed_now, order_points, store, users
from tires_sync.publishing import try_feed_lock

QUIET = datetime(2026, 11, 3, 9, 5, 0)          # 12:05 in Moscow: far from the hourly :40


class Popen:
    def __init__(self, pid=4242):
        self.started, self.pid = [], pid

    def __call__(self, args, **kwargs):
        self.started.append((args, kwargs))
        return type("P", (), {"pid": self.pid, "poll": lambda self: None})()


def setup(tmp_path, at=QUIET):
    popen = Popen()
    app = make_app(tmp_path, FEED_ROOT=str(tmp_path / "feed"), POPEN=popen, NOW=lambda: at, SECRET_KEY_FILE="/etc/k")
    client = app.test_client()
    return app, client, login(client), popen


def audit(app, action):
    conn = store.connect(app.config["DB_PATH"])
    try:
        return [json.loads(r[0]) for r in conn.execute("SELECT detail FROM audit WHERE action = ?", (action,))]
    finally:
        conn.close()


def test_the_button_starts_the_usual_cycle_with_publication_apart_and_says_so(tmp_path):
    app, client, tok, popen = setup(tmp_path)
    assert "Собрать фид сейчас" in client.get("/feed/update").get_data(as_text=True)
    r = post(client, "/feed/update/now", tok)
    assert r.status_code == 302 and r.headers["Location"].endswith("/feed/update")
    args, kwargs = popen.started[0]
    assert args[1:] == ["-m", "tires_sync", "run-cycle", "--fetch", "--publish", "--db", app.config["DB_PATH"], "--unload", app.config["UNLOAD_PATH"],
                        "--reference", app.config["REFERENCE_PATH"], "--feed-root", app.config["FEED_ROOT"], "--key-file", "/etc/k"]
    assert kwargs["start_new_session"] is True
    page = client.get("/feed/update").get_data(as_text=True)
    assert "Цикл запущен" in page and "Сбор по кнопке" in page
    assert audit(app, "feed_now_started") == [{"pid": 4242}]


def test_a_second_press_within_5_minutes_is_refused(tmp_path):
    app, client, tok, popen = setup(tmp_path)
    post(client, "/feed/update/now", tok)
    popen.pid = 999999                                                      # the first one may have ended: the 5 minutes still hold
    conn = store.connect(app.config["DB_PATH"])
    store.set_state(conn, feed_now.STATE_KEY, json.dumps({"at": "x", "pid": 999999}))
    conn.close()
    post(client, "/feed/update/now", tok)
    assert len(popen.started) == 1
    assert "запускался 0 мин назад" in client.get("/feed/update").get_data(as_text=True)
    assert len(audit(app, "feed_now_refused")) == 1


def test_a_cycle_started_by_the_timer_minutes_ago_or_one_building_now_refuses_it(tmp_path):
    app, client, tok, popen = setup(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    store.set_state(conn, feed_now.CYCLE_STARTED_KEY, "2026-11-03T09:02:00")     # run-cycle writes it at its start
    post(client, "/feed/update/now", tok)
    assert popen.started == []
    store.set_state(conn, feed_now.CYCLE_STARTED_KEY, "2026-11-03T08:40:00")
    held = try_feed_lock(app.config["FEED_ROOT"])                                 # a publication of the panel, the stock pass
    try:
        post(client, "/feed/update/now", tok)
    finally:
        held.close()
    assert popen.started == [] and "другой процесс" in client.get("/feed/update").get_data(as_text=True)
    post(client, "/feed/update/now", tok)
    assert len(popen.started) == 1
    conn.close()


def test_the_buttons_own_cycle_still_running_refuses_it(tmp_path):
    import os
    app, client, tok, popen = setup(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    store.set_state(conn, feed_now.STATE_KEY, json.dumps({"at": "2026-11-03T08:30:00", "pid": os.getpid()}))
    conn.close()
    post(client, "/feed/update/now", tok)
    assert popen.started == [] and "уже собирается" in client.get("/feed/update").get_data(as_text=True)


def test_just_before_the_hourly_cycle_it_is_refused(tmp_path):
    app, client, tok, popen = setup(tmp_path, at=datetime(2026, 11, 3, 9, 33, 0))   # 12:33 in Moscow
    post(client, "/feed/update/now", tok)
    assert popen.started == [] and "Скоро плановый цикл" in client.get("/feed/update").get_data(as_text=True)


def test_the_partner_may_press_it_a_manager_or_an_observer_may_not(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    app, _, _, popen = setup(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    point = order_points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk"})
    codes = {}
    for role in ("chats", "point", "viewer", "partner"):
        users.add_user(conn, role + "1", "длинный-пароль-1", "2026-10-24T12:00:00", role=role, point_id=point if role == "point" else None)
        client = app.test_client()
        with client.session_transaction() as session:
            session["user"] = role + "1"
            session["epoch"] = store.get_session_epoch(conn, role + "1")
            session["csrf"] = "tok"
        codes[role] = client.post("/feed/update/now", data={"csrf": "tok"}).status_code
    conn.close()
    assert codes == {"chats": 403, "point": 403, "viewer": 403, "partner": 302}
    assert len(popen.started) == 1


def test_run_cycle_marks_its_start(tmp_path, monkeypatch):
    from tires_sync import cli
    conn = store.connect(tmp_path / "t.db")
    conn.close()
    monkeypatch.setattr(cli, "load_box", lambda path: None)
    monkeypatch.setattr(cli, "parse_reference", lambda text: (_ for _ in ()).throw(RuntimeError("stop here")))
    (tmp_path / "ref.txt").write_text("", encoding="utf-8")
    try:
        cli.main(["run-cycle", "--db", str(tmp_path / "t.db"), "--unload", str(tmp_path / "u.xml"), "--reference", str(tmp_path / "ref.txt"),
                  "--feed-root", str(tmp_path / "feed")])
    except RuntimeError:
        pass
    conn = store.connect(tmp_path / "t.db")
    assert store.get_state(conn, feed_now.CYCLE_STARTED_KEY)
    conn.close()
