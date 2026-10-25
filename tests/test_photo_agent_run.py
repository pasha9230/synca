"""Generation from the panel: the run request, a model first, the agent's «is there work» and «run done», the status line."""
from __future__ import annotations

import json

from panel_helpers import login, make_app, post
from tires_sync import photo_agent, photo_library, store
from tires_sync.panel import routes_agent

NOW = "2026-10-07T12:00:00"          # the panel clock of make_app


def make(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    for i in (1, 2):
        add_photo(conn, i)
    conn.commit()
    return app, conn


def add_set(conn, key, rank, state="needed", ref="https://x.example/r.jpg"):
    conn.execute("INSERT INTO photo_set (key, state, ref_url, rank, brand_name, model_name, season, studded, size_class)"
                 " VALUES (?,?,?,?,?,?,'winter',1,'r15_18')", (key, state, ref, rank, "Brand " + key, "Model " + key))
    conn.commit()
    return conn.execute("SELECT rowid FROM photo_set WHERE key=?", (key,)).fetchone()[0]


def add_photo(conn, i):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(photo)")]
    row = {"id": i, "sha256": "%064x" % i, "ext": "jpg"}
    for c in cols:
        row.setdefault(c, "x" if c not in ("width", "height", "bytes", "size") else 1)
    conn.execute("INSERT INTO photo (%s) VALUES (%s)" % (",".join(row), ",".join("?" * len(row))), list(row.values()))


def frame(conn, key, kind, status, photo=1, frame_id=None):
    conn.execute("INSERT INTO set_frame (id, set_key, kind, photo_id, status, created) VALUES (?,?,?,?,?,'t')", (frame_id, key, kind, photo, status))
    conn.commit()


def jobs(conn):
    return conn.execute("SELECT set_key, kind, status FROM image_job ORDER BY id").fetchall()


def agent(app, conn):
    token = routes_agent.issue_agent_token(conn)
    return app.test_client(), {"Authorization": "Bearer " + token}


# --- the module: request, a model first, work and done ---

def test_request_all_wins_over_rush_and_keeps_the_newest_stamp(tmp_path):
    _, conn = make(tmp_path)
    photo_agent.request_run(conn, "2026-10-07T10:00:00", "rush")
    photo_agent.request_run(conn, "2026-10-07T10:01:00", "all")
    photo_agent.request_run(conn, "2026-10-07T10:02:00", "rush")
    assert photo_agent.pending_request(conn) == {"at": "2026-10-07T10:02:00", "scope": "all"}


def test_rush_model_queues_its_job_first_even_past_the_queue_depth(tmp_path):
    _, conn = make(tmp_path)
    for i in range(12):
        add_set(conn, "s%02d" % i, i)
    photo_library.enqueue(conn, NOW)
    assert ("s11", "three_quarter", "queued") not in jobs(conn)
    ok, _ = photo_agent.rush_model(conn, "s11", NOW)
    assert ok
    queued = [r[0] for r in jobs(conn) if r[2] == "queued"]
    assert "s11" in queued and len(queued) == photo_library.MAX_OPEN_JOBS
    assert photo_agent.rush_keys(conn) == ["s11"]
    assert photo_agent.pending_request(conn)["scope"] == "rush"


def test_rush_model_says_why_nothing_is_queued(tmp_path):
    _, conn = make(tmp_path)
    add_set(conn, "p", 0)
    frame(conn, "p", "three_quarter", "pending")
    ok, why = photo_agent.rush_model(conn, "p", NOW)
    assert not ok and "проверк" in why
    add_set(conn, "a", 1)
    for _ in range(photo_library.MAX_ATTEMPTS):
        conn.execute("INSERT INTO image_job (set_key, kind, status, created, updated) VALUES ('a','three_quarter','failed','t','t')")
    conn.commit()
    ok, why = photo_agent.rush_model(conn, "a", NOW)
    assert not ok and "попыт" in why
    add_set(conn, "r", 2, state="retired")
    ok, why = photo_agent.rush_model(conn, "r", NOW)
    assert not ok and "не нужна" in why
    store.set_state(conn, photo_library.QUEUE_STOP_KEY, "1")
    add_set(conn, "s", 3)
    ok, why = photo_agent.rush_model(conn, "s", NOW)
    assert not ok and "остановлена" in why
    assert photo_agent.pending_request(conn) is None


def test_rush_model_at_the_daily_cap_is_refused(tmp_path):
    _, conn = make(tmp_path)
    add_set(conn, "s", 0)
    conn.execute("INSERT INTO image_job (set_key, kind, status, cost_usd, created, updated) VALUES ('s','three_quarter','done',5.0,?,?)", (NOW, NOW))
    conn.commit()
    ok, why = photo_agent.rush_model(conn, "s", NOW)
    assert not ok and "Лимит" in why


def test_work_hands_the_request_and_done_clears_only_that_request(tmp_path):
    _, conn = make(tmp_path)
    assert photo_agent.work(conn, NOW) == {"run": False, "scope": "", "request_at": "", "copies": False, "copies_at": ""}
    photo_agent.request_run(conn, "2026-10-07T11:00:00", "all")
    photo_agent.request_copies(conn, "2026-10-07T11:00:30")
    w = photo_agent.work(conn, NOW)
    assert w == {"run": True, "scope": "all", "request_at": "2026-10-07T11:00:00", "copies": True, "copies_at": "2026-10-07T11:00:30"}
    assert photo_agent.status(conn)["state"] == "running"
    photo_agent.request_run(conn, "2026-10-07T11:59:00", "rush")              # pressed again during the run
    photo_agent.finish(conn, NOW, w["request_at"], w["copies_at"], 2, 1, "")
    assert photo_agent.pending_request(conn) == {"at": "2026-10-07T11:59:00", "scope": "all"}
    st = photo_agent.status(conn)
    assert (st["state"], st["done"], st["failed"]) == ("done", 2, 1)
    assert photo_agent.work(conn, NOW)["copies"] is False
    photo_agent.finish(conn, NOW, "2026-10-07T11:59:00", "", 0, 0, "OpenAI HTTP 403")
    assert photo_agent.pending_request(conn) is None
    assert photo_agent.status(conn)["state"] == "error"


def test_rush_list_drops_finished_and_retired_models(tmp_path):
    _, conn = make(tmp_path)
    for key in ("done", "gone", "open"):
        add_set(conn, key, 0)
    for key in ("gone", "open"):
        conn.execute("INSERT INTO image_job (set_key, kind, status, created, updated) VALUES (?,'three_quarter','queued','t','t')", (key,))
    store.set_state(conn, photo_agent.RUSH_KEY, json.dumps(["done", "gone", "open"]))
    frame(conn, "done", "three_quarter", "approved")
    frame(conn, "done", "set4", "approved")
    conn.execute("UPDATE photo_set SET state='retired' WHERE key='gone'")
    conn.commit()
    photo_agent.work(conn, NOW)
    assert photo_agent.rush_keys(conn) == ["open"]


def test_status_view_texts(tmp_path):
    _, conn = make(tmp_path)
    assert "кнопк" in photo_agent.status_view(conn, NOW)["text"]
    photo_agent.request_run(conn, "2026-10-07T11:58:00", "all")
    v = photo_agent.status_view(conn, NOW)
    assert "ждёт агента" in v["text"] and not v["silent"]
    photo_agent.request_run(conn, "2026-10-07T11:00:00", "all")
    assert photo_agent.status_view(conn, "2026-10-07T11:20:00")["silent"]       # asked 20 min ago, the agent never came
    photo_agent.work(conn, "2026-10-07T11:21:00")
    v = photo_agent.status_view(conn, "2026-10-07T11:30:00")
    assert v["state"] == "running" and "Идёт генерация" in v["text"]
    v = photo_agent.status_view(conn, "2026-10-07T15:00:00")                   # running past the stuck time (200 min): not «идёт»
    assert v["state"] != "running" and v["silent"]


# --- the agent API ---

def test_work_and_done_need_the_agent_token(tmp_path):
    app, conn = make(tmp_path)
    c, auth = agent(app, conn)
    assert c.get("/api/image-jobs/work").status_code == 403
    assert c.post("/api/image-jobs/run-done", data={}).status_code == 403
    assert c.get("/api/image-jobs/work", headers=auth).get_json()["run"] is False


def test_done_validates_and_clears(tmp_path):
    app, conn = make(tmp_path)
    c, auth = agent(app, conn)
    photo_agent.request_run(conn, "2026-10-07T11:00:00", "all")
    w = c.get("/api/image-jobs/work", headers=auth).get_json()
    assert w["run"] and w["scope"] == "all"
    assert c.post("/api/image-jobs/run-done", headers=auth, data={"done": "-1"}).status_code == 400
    assert c.post("/api/image-jobs/run-done", headers=auth, data={"done": "x"}).status_code == 400
    r = c.post("/api/image-jobs/run-done", headers=auth, data={"request_at": w["request_at"], "copies_at": "", "done": "3", "failed": "0",
                                                               "message": "m" * 2000})
    assert r.status_code == 200
    assert photo_agent.pending_request(conn) is None
    assert len(photo_agent.status(conn)["message"]) <= photo_agent.MAX_MESSAGE


def test_handout_only_rush_and_rush_first(tmp_path):
    app, conn = make(tmp_path)
    c, auth = agent(app, conn)
    add_set(conn, "first", 0)
    add_set(conn, "later", 5)
    photo_library.enqueue(conn, NOW)
    photo_agent.rush_model(conn, "later", NOW)
    got = c.get("/api/image-jobs?limit=1&only=rush", headers=auth).get_json()
    assert [j["set_key"] for j in got] == ["later"]
    assert c.get("/api/image-jobs?limit=5&only=rush", headers=auth).get_json() == []
    assert [j["set_key"] for j in c.get("/api/image-jobs?limit=5", headers=auth).get_json()] == ["first"]


def test_handout_rush_first_without_only(tmp_path):
    app, conn = make(tmp_path)
    c, auth = agent(app, conn)
    add_set(conn, "first", 0)
    add_set(conn, "later", 5)
    photo_library.enqueue(conn, NOW)
    photo_agent.rush_model(conn, "later", NOW)
    assert [j["set_key"] for j in c.get("/api/image-jobs?limit=2", headers=auth).get_json()] == ["later", "first"]


# --- the panel ---

def test_generate_queue_button_requests_a_run(tmp_path):
    app, conn = make(tmp_path)
    add_set(conn, "s", 0)
    c = app.test_client()
    tok = login(c)
    r = post(c, "/photos/generate", tok)
    assert r.status_code == 302 and "tab=queue" in r.headers["Location"]
    assert photo_agent.pending_request(conn)["scope"] == "all"
    assert jobs(conn) == [("s", "three_quarter", "queued")]
    page = c.get("/photos/review?tab=queue").get_data(as_text=True)
    assert "Сгенерировать очередь" in page and "ждёт агента" in page


def test_generate_queue_button_with_an_empty_queue_says_so(tmp_path):
    app, conn = make(tmp_path)
    c = app.test_client()
    tok = login(c)
    post(c, "/photos/generate", tok)
    assert photo_agent.pending_request(conn) is None
    assert "генерировать нечего" in c.get("/photos/review?tab=queue").get_data(as_text=True)


def test_generate_model_button(tmp_path):
    app, conn = make(tmp_path)
    sid = add_set(conn, "m", 0)
    c = app.test_client()
    tok = login(c)
    page = c.get("/photos/review?tab=models").get_data(as_text=True)
    assert 'formaction="/photos/generate"' in page and 'value="%d"' % sid in page
    r = post(c, "/photos/generate", tok, set=str(sid), show="todo")
    assert r.status_code == 302 and "tab=models" in r.headers["Location"]
    assert photo_agent.rush_keys(conn) == ["m"] and photo_agent.pending_request(conn)["scope"] == "rush"
    assert post(c, "/photos/generate", tok, set="abc").status_code == 400
    assert post(c, "/photos/generate", tok, set="99999").status_code == 404


def test_generate_requires_login_and_csrf(tmp_path):
    app, conn = make(tmp_path)
    anon = app.test_client()
    r = anon.post("/photos/generate")
    assert r.status_code in (302, 400) and photo_agent.pending_request(conn) is None
    c = app.test_client()
    login(c)
    assert c.post("/photos/generate").status_code == 400


def test_approve_asks_for_copies_and_runs_set4_of_a_rushed_model(tmp_path):
    app, conn = make(tmp_path)
    add_set(conn, "m", 0)
    add_set(conn, "plain", 1)
    store.set_state(conn, photo_agent.RUSH_KEY, json.dumps(["m"]))
    frame(conn, "m", "three_quarter", "pending", frame_id=10)
    frame(conn, "plain", "three_quarter", "pending", frame_id=11)
    c = app.test_client()
    tok = login(c)
    post(c, "/photos/frame/11/approve", tok)
    assert photo_agent.pending_request(conn) is None                         # a plain model: its set4 waits for a press
    assert photo_agent.work(conn, NOW)["copies"] is True
    post(c, "/photos/frame/10/approve", tok)
    assert photo_agent.pending_request(conn)["scope"] == "rush"
    assert ("m", "set4", "queued") in jobs(conn)


# --- the agent on the server: the tick wrapper, the one-time token, «Подключения → OpenAI (фото)» ---

import subprocess
from types import SimpleNamespace

from cryptography.fernet import Fernet

from tires_sync import connections
from tires_sync.secretbox import SecretBox

KEY = "sk-proj-TESTKEY0123456789abcdef"
PROXY = "socks5://user:PROXYPW@127.0.0.1:1080"


def server(tmp_path):
    box = SecretBox(Fernet.generate_key())
    app = make_app(tmp_path, SECRET_BOX=box)
    conn = store.connect(app.config["DB_PATH"])
    store.set_state(conn, "public_base", "https://panel.example")
    return app, conn, box


def keys(conn, box, key=KEY, proxy=PROXY):
    for name, value in (("api_key", key), ("proxy", proxy)):
        if value:
            connections.set_secret(conn, box, photo_agent.SECRET_KIND, 0, name, value, NOW, "t")
    conn.commit()


def test_tick_server_without_a_request_starts_nothing(tmp_path):
    _, conn, box = server(tmp_path)
    calls = []
    assert photo_agent.tick_server(conn, lambda: box, "/bin/agent", str(tmp_path), NOW, run=lambda *a, **k: calls.append(a)) == 0
    assert calls == [] and photo_agent.server_token_digest(conn) == ""


def test_tick_server_gives_secrets_on_stdin_only_and_revokes_the_token(tmp_path):
    app, conn, box = server(tmp_path)
    keys(conn, box)
    photo_agent.request_run(conn, "2026-10-07T11:00:00", "all")
    seen = {}

    def fake_run(argv, input, text, env, timeout):
        sent = json.loads(input)
        seen.update(argv=argv, env=env, sent=sent)
        c = app.test_client()                                     # the agent's calls with the one-time token work during the run
        w = c.get("/api/image-jobs/work", headers={"Authorization": "Bearer " + sent["token"]})
        seen["work"] = w.status_code
        c.post("/api/image-jobs/run-done", headers={"Authorization": "Bearer " + sent["token"]},
               data={"request_at": w.get_json()["request_at"], "done": "1"})
        return SimpleNamespace(returncode=0)

    assert photo_agent.tick_server(conn, lambda: box, "/opt/agent", str(tmp_path / "a"), NOW, run=fake_run) == 0
    assert seen["work"] == 200 and seen["argv"] == ["/opt/agent", "tick", "--stdin-secrets"]
    assert (seen["sent"]["openai_key"], seen["sent"]["proxy"]) == (KEY, PROXY)
    flat = json.dumps(seen["argv"]) + json.dumps(seen["env"])
    assert KEY not in flat and "PROXYPW" not in flat and seen["sent"]["token"] not in flat
    assert seen["env"]["TIRES_PANEL_URL"] == "https://panel.example" and seen["env"]["TIRES_AGENT_DIR"] == str(tmp_path / "a")
    assert "TIRES_SECRET_KEY_FILE" not in seen["env"]
    assert photo_agent.server_token_digest(conn) == ""
    r = app.test_client().get("/api/image-jobs/work", headers={"Authorization": "Bearer " + seen["sent"]["token"]})
    assert r.status_code == 403                                   # revoked after the run
    assert photo_agent.pending_request(conn) is None and photo_agent.status(conn)["done"] == 1


def test_tick_server_crash_shows_in_the_panel_and_keeps_the_request(tmp_path):
    _, conn, box = server(tmp_path)
    keys(conn, box)
    photo_agent.request_run(conn, "2026-10-07T11:00:00", "all")
    assert photo_agent.tick_server(conn, lambda: box, "/opt/agent", str(tmp_path), NOW, run=lambda *a, **k: SimpleNamespace(returncode=2)) == 1
    st = photo_agent.status(conn)
    assert st["state"] == "error" and "код 2" in st["message"]
    assert photo_agent.pending_request(conn) is not None and photo_agent.server_token_digest(conn) == ""
    assert "код 2" in photo_agent.status_view(conn, NOW)["text"]                # the reason is shown, not «ждёт агента»
    assert "1 из 3" in st["message"]

    def hang(*a, **k):
        raise subprocess.TimeoutExpired("agent", 1)

    photo_agent.tick_server(conn, lambda: box, "/opt/agent", str(tmp_path), NOW, run=hang)
    assert "не уложился" in photo_agent.status(conn)["message"] and photo_agent.server_token_digest(conn) == ""
    assert photo_agent.pending_request(conn) is not None

    def missing(*a, **k):
        raise FileNotFoundError("no such file")

    photo_agent.tick_server(conn, lambda: box, "/opt/agent", str(tmp_path), NOW, run=missing)
    st = photo_agent.status(conn)                                  # the third death in a row: no more retries
    assert "не запускается" in st["message"] and "3 раза" in st["message"]
    assert photo_agent.pending_request(conn) is None
    calls = []
    photo_agent.tick_server(conn, lambda: box, "/opt/agent", str(tmp_path), NOW, run=lambda *a, **k: calls.append(1))
    assert calls == []


def test_tick_server_without_base_or_master_key_does_not_start(tmp_path):
    _, conn, box = server(tmp_path)
    photo_agent.request_run(conn, NOW, "all")
    started = []
    assert photo_agent.tick_server(conn, lambda: None, "/opt/agent", str(tmp_path), NOW, run=lambda *a, **k: started.append(1)) == 1
    assert "мастер-ключ" in photo_agent.status(conn)["message"] and photo_agent.pending_request(conn) is None   # not retried
    photo_agent.request_run(conn, NOW, "all")
    store.set_state(conn, "public_base", "")
    photo_agent.tick_server(conn, lambda: box, "/opt/agent", str(tmp_path), NOW, run=lambda *a, **k: started.append(1))
    assert "public_base" in photo_agent.status(conn)["message"] and started == []


def test_tick_server_without_an_openai_key_still_runs_for_copies(tmp_path):
    _, conn, box = server(tmp_path)
    photo_agent.request_copies(conn, NOW)
    sent = []
    photo_agent.tick_server(conn, lambda: box, "/opt/agent", str(tmp_path), NOW,
                            run=lambda argv, input, **k: sent.append(json.loads(input)) or SimpleNamespace(returncode=0))
    assert sent and sent[0]["openai_key"] == "" and sent[0]["proxy"] == ""


def test_check_proxy():
    for good in ("socks5://127.0.0.1:1080", "http://u:p@proxy.example:3128", "socks5://h.example:1080/"):
        assert photo_agent.check_proxy(" %s " % good) == good.strip()
    for bad in ("ftp://h:1", "socks5://h", "127.0.0.1:1080", "http://h:1/path", "socks5://a b:1", ""):
        try:
            photo_agent.check_proxy(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_openai_connection_page_saves_encrypted_and_never_shows(tmp_path):
    app, conn, box = server(tmp_path)
    c = app.test_client()
    tok = login(c)
    page = c.get("/connections/openai").get_data(as_text=True)
    assert "OpenAI (фото)" in page and "не задан" in page
    post(c, "/connections/openai", tok, api_key=KEY, proxy=PROXY)
    raw = conn.execute("SELECT group_concat(ciphertext) FROM secret WHERE kind='openai'").fetchone()[0]
    assert KEY not in raw and "PROXYPW" not in raw
    assert connections.get_secret(conn, box, "openai", 0, "api_key") == KEY
    page = c.get("/connections/openai").get_data(as_text=True)
    assert KEY not in page and "PROXYPW" not in page and "127.0.0.1" not in page and "••••" in page
    audit = json.dumps(conn.execute("SELECT * FROM audit").fetchall(), ensure_ascii=False) if conn.execute(
        "SELECT name FROM sqlite_master WHERE name='audit'").fetchone() else ""
    assert KEY not in audit and "PROXYPW" not in audit
    post(c, "/connections/openai", tok, api_key="", proxy="ftp://x:1")
    assert "Прокси" in c.get("/connections/openai").get_data(as_text=True)
    assert connections.get_secret(conn, box, "openai", 0, "proxy") == PROXY        # a bad value changes nothing
    post(c, "/connections/openai/clear", tok, what="proxy")
    assert connections.get_secret(conn, box, "openai", 0, "proxy") is None
    assert connections.get_secret(conn, box, "openai", 0, "api_key") == KEY
    assert post(c, "/connections/openai/clear", tok, what="x").status_code == 400


def test_openai_connection_requires_login_and_csrf(tmp_path):
    app, conn, box = server(tmp_path)
    anon = app.test_client()
    assert anon.get("/connections/openai").status_code == 302
    anon.post("/connections/openai", data={"api_key": KEY})
    c = app.test_client()
    login(c)
    assert c.post("/connections/openai", data={"api_key": KEY}).status_code == 400
    assert connections.get_secret(conn, box, "openai", 0, "api_key") is None


def test_tick_server_reads_the_master_key_only_with_work(tmp_path):
    _, conn, box = server(tmp_path)
    loads = []
    photo_agent.tick_server(conn, lambda: loads.append(1) or box, "/opt/agent", str(tmp_path), NOW, run=lambda *a, **k: None)
    assert loads == []


def test_tick_server_drops_a_request_older_than_a_day(tmp_path):
    _, conn, box = server(tmp_path)
    photo_agent.request_run(conn, "2026-10-06T11:00:00", "all")
    calls = []
    photo_agent.tick_server(conn, lambda: box, "/opt/agent", str(tmp_path), NOW, run=lambda *a, **k: calls.append(1))
    assert calls == [] and photo_agent.pending_request(conn) is None and "старше суток" in photo_agent.status(conn)["message"]


def test_tick_server_one_at_a_time(tmp_path):
    _, conn, box = server(tmp_path)
    photo_agent.request_run(conn, NOW, "all")
    held = photo_agent._lock(str(tmp_path))
    calls = []
    assert photo_agent.tick_server(conn, lambda: box, "/opt/agent", str(tmp_path), NOW, run=lambda *a, **k: calls.append(1)) == 0
    assert calls == [] and photo_agent.pending_request(conn) is not None
    held.close()
    photo_agent.tick_server(conn, lambda: box, "/opt/agent", str(tmp_path), NOW, run=lambda *a, **k: calls.append(1) or SimpleNamespace(returncode=0))
    assert calls == [1]


def test_a_run_that_recovers_resets_the_count(tmp_path):
    _, conn, box = server(tmp_path)
    photo_agent.request_run(conn, NOW, "all")
    for code in (2, 0, 2, 2):
        photo_agent.tick_server(conn, lambda: box, "/opt/agent", str(tmp_path), NOW, run=lambda *a, **k: SimpleNamespace(returncode=code))
    assert photo_agent.pending_request(conn) is not None             # 2, ok, 2, 2: never three in a row


def test_copies_alone_do_not_hide_the_last_result(tmp_path):
    _, conn = make(tmp_path)
    photo_agent.request_run(conn, "2026-10-07T11:00:00", "all")
    w = photo_agent.work(conn, NOW)
    photo_agent.finish(conn, NOW, w["request_at"], "", 0, 1, "OpenAI HTTP 401: bad key")
    photo_agent.request_copies(conn, NOW)
    w = photo_agent.work(conn, NOW)
    assert photo_agent.status(conn)["state"] == "error"
    photo_agent.finish(conn, NOW, "", w["copies_at"], 0, 0, "")
    assert "401" in photo_agent.status_view(conn, NOW)["text"]
    assert photo_agent.work(conn, NOW)["copies"] is False


def test_rush_drops_a_model_out_of_attempts(tmp_path):
    _, conn = make(tmp_path)
    add_set(conn, "a", 0)
    store.set_state(conn, photo_agent.RUSH_KEY, json.dumps(["a"]))
    for _ in range(photo_library.MAX_ATTEMPTS):
        conn.execute("INSERT INTO image_job (set_key, kind, status, created, updated) VALUES ('a','three_quarter','failed','t','t')")
    conn.commit()
    photo_agent.work(conn, NOW)
    assert photo_agent.rush_keys(conn) == []


def test_regenerate_of_a_rushed_model_runs_at_once(tmp_path):
    app, conn = make(tmp_path)
    add_set(conn, "m", 0)
    store.set_state(conn, photo_agent.RUSH_KEY, json.dumps(["m"]))
    frame(conn, "m", "three_quarter", "pending", frame_id=10)
    c = app.test_client()
    tok = login(c)
    post(c, "/photos/frame/10/regenerate", tok)
    assert photo_agent.pending_request(conn)["scope"] == "rush"
    assert photo_agent.work(conn, NOW)["copies"] is False               # a rejected frame makes no copies


def test_generate_queue_with_an_empty_queue_still_refreshes_copies(tmp_path):
    app, conn = make(tmp_path)
    c = app.test_client()
    tok = login(c)
    post(c, "/photos/generate", tok)
    assert photo_agent.pending_request(conn) is None and photo_agent.work(conn, NOW)["copies"] is True


def test_the_third_death_keeps_a_press_made_during_it_and_approvals_do_not_reset_the_count(tmp_path):
    _, conn, box = server(tmp_path)
    photo_agent.request_run(conn, "2026-10-07T11:00:00", "all")
    dead = lambda *a, **k: SimpleNamespace(returncode=2)
    for i in range(2):
        photo_agent.request_copies(conn, "2026-10-07T11:0%d:30" % i)       # an approval between the deaths
        photo_agent.tick_server(conn, lambda: box, "/opt/agent", str(tmp_path), NOW, run=dead)
    assert "2 из 3" in photo_agent.status(conn)["message"]

    def dead_and_pressed(*a, **k):
        photo_agent.request_run(conn, "2026-10-07T11:59:00", "all")        # pressed again during the third run
        return SimpleNamespace(returncode=2)

    photo_agent.tick_server(conn, lambda: box, "/opt/agent", str(tmp_path), NOW, run=dead_and_pressed)
    assert "3 раза" in photo_agent.status(conn)["message"]
    assert photo_agent.pending_request(conn) == {"at": "2026-10-07T11:59:00", "scope": "all"}
