from panel_helpers import login, make_app, post
from tires_sync import store


def make(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(tmp_path / "t.db")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(photo)")]
    for i in (1, 2, 3):
        row = {"id": i, "sha256": "%064x" % i, "ext": "jpg"}
        for c in cols:
            row.setdefault(c, "x" if c not in ("width", "height", "bytes", "size") else 1)
        conn.execute("INSERT INTO photo (%s) VALUES (%s)" % (",".join(row), ",".join("?" * len(row))), list(row.values()))
    conn.execute("INSERT INTO photo_set (key, state, rank, brand_name, model_name) VALUES ('k','needed',1,'Brand','Model')")
    conn.execute("INSERT INTO photo_set (key, state, reason, brand_name, model_name) VALUES ('d','needs_decision','нет образца','B2','M2')")
    conn.execute("INSERT INTO set_frame (id, set_key, kind, photo_id, status, created) VALUES (1,'k','three_quarter',1,'approved','t')")
    conn.execute("INSERT INTO set_frame (id, set_key, kind, photo_id, status, created) VALUES (2,'k','three_quarter',2,'pending','t')")
    conn.commit()
    return app, conn


def status(conn, i):
    return conn.execute("SELECT status FROM set_frame WHERE id=?", (i,)).fetchone()[0]


def test_review_requires_login(tmp_path):
    app, _ = make(tmp_path)
    r = app.test_client().get("/photos/review")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert app.test_client().post("/photos/switch").status_code in (302, 400)


def test_approve_withdraws_previous(tmp_path):
    app, conn = make(tmp_path)
    c = app.test_client()
    tok = login(c)
    assert post(c, "/photos/frame/2/approve", tok).status_code == 302
    assert (status(conn, 1), status(conn, 2)) == ("withdrawn", "approved")


def test_regenerate_enqueues(tmp_path):
    app, conn = make(tmp_path)
    c = app.test_client()
    tok = login(c)
    post(c, "/photos/frame/2/regenerate", tok)
    assert status(conn, 2) == "rejected"
    assert conn.execute("SELECT COUNT(*) FROM image_job").fetchone()[0] >= 1


def test_switch_toggles_state(tmp_path):
    app, conn = make(tmp_path)
    c = app.test_client()
    tok = login(c)
    post(c, "/photos/switch", tok, on="1")
    assert store.get_state(conn, "own_photos") == "1"
    post(c, "/photos/switch", tok, on="0")
    assert store.get_state(conn, "own_photos") == "0"


def test_agent_token_shown_once(tmp_path):
    app, conn = make(tmp_path)
    c = app.test_client()
    tok = login(c)
    body = post(c, "/photos/agent-token", tok).get_data(as_text=True)
    assert "Новый токен агента" in body
    assert "Новый токен агента" not in c.get("/photos/review").get_data(as_text=True)


def test_tabs_list_sets(tmp_path):
    app, conn = make(tmp_path)
    c = app.test_client()
    login(c)
    assert "Brand" in c.get("/photos/review?tab=pending").get_data(as_text=True)
    page = c.get("/photos/review?tab=decision").get_data(as_text=True)
    assert "B2" in page and "нет образца" in page
    assert "Brand" not in c.get("/photos/review?tab=approved").get_data(as_text=True)


def test_withdraw_and_reject(tmp_path):
    app, conn = make(tmp_path)
    c = app.test_client()
    tok = login(c)
    post(c, "/photos/frame/1/withdraw", tok)
    post(c, "/photos/frame/2/reject", tok)
    assert (status(conn, 1), status(conn, 2)) == ("withdrawn", "rejected")


def test_stale_status_guard(tmp_path):
    app, conn = make(tmp_path)
    c = app.test_client()
    tok = login(c)
    r = post(c, "/photos/frame/1/approve", tok)            # already approved
    assert r.status_code == 302
    r = post(c, "/photos/frame/2/withdraw", tok)           # pending, not approved
    assert (status(conn, 1), status(conn, 2)) == ("approved", "pending")
    assert "уже обработан" in c.get("/photos/review").get_data(as_text=True)


def test_post_without_csrf_is_400(tmp_path):
    app, conn = make(tmp_path)
    c = app.test_client()
    login(c)
    assert c.post("/photos/frame/2/approve", data={}).status_code == 400
    assert c.post("/photos/switch", data={"on": "1"}).status_code == 400
    assert status(conn, 2) == "pending"


def test_approved_tab_needs_both_kinds(tmp_path):
    app, conn = make(tmp_path)
    conn.execute("INSERT INTO photo_set (key, state, brand_name, model_name) VALUES ('both','needed','BothBrand','M')")
    for i, k in ((10, "three_quarter"), (11, "set4")):
        conn.execute("INSERT INTO set_frame (id, set_key, kind, photo_id, status, created) VALUES (?,?,?,1,'approved','t')", (i, "both", k))
    conn.commit()
    c = app.test_client()
    login(c)
    page = c.get("/photos/review?tab=approved").get_data(as_text=True)
    assert "BothBrand" in page and "Brand Model" not in page


def set_id(conn, key):
    return conn.execute("SELECT rowid FROM photo_set WHERE key=?", (key,)).fetchone()[0]


def test_sample_shown_through_the_panel(tmp_path):
    app, conn = make(tmp_path)
    conn.execute("UPDATE photo_set SET ref_url='https://x.example/a.jpg' WHERE key='k'")
    conn.execute("UPDATE photo_set SET ref_url='http://evil.example/<b>' WHERE key='d'")
    conn.commit()
    c = app.test_client()
    login(c)
    page = c.get("/photos/review?tab=pending").get_data(as_text=True)
    assert 'src="/photos/ref/%d"' % set_id(conn, "k") in page and "x.example" not in page      # the supplier host is not reachable from the owner's browser
    dec = c.get("/photos/review?tab=decision").get_data(as_text=True)
    assert 'src="/photos/ref/%d"' % set_id(conn, "d") in dec and "evil.example" not in dec


def test_sample_proxy_requires_login(tmp_path):
    app, conn = make(tmp_path)
    conn.execute("UPDATE photo_set SET ref_url='https://x.example/a.jpg' WHERE key='k'")
    conn.commit()
    app.config["PHOTO_FETCH"] = lambda url: (_ for _ in ()).throw(AssertionError("fetched without login"))
    r = app.test_client().get("/photos/ref/%d" % set_id(conn, "k"))
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_sample_proxy_returns_the_supplier_image(tmp_path):
    app, conn = make(tmp_path)
    conn.execute("UPDATE photo_set SET ref_url='https://x.example/a.jpg' WHERE key='k'")
    conn.commit()
    seen = []
    app.config["PHOTO_FETCH"] = lambda url: (seen.append(url), (b"\x89PNGdata", "png"))[1]
    c = app.test_client()
    login(c)
    r = c.get("/photos/ref/%d" % set_id(conn, "k"))
    assert r.status_code == 200 and r.data == b"\x89PNGdata" and r.mimetype == "image/png"
    assert r.headers["Cache-Control"].startswith("private") and seen == ["https://x.example/a.jpg"]


def test_sample_proxy_unknown_set_or_no_sample_is_404(tmp_path):
    app, conn = make(tmp_path)
    app.config["PHOTO_FETCH"] = lambda url: (b"x", "jpg")
    c = app.test_client()
    login(c)
    assert c.get("/photos/ref/999").status_code == 404
    assert c.get("/photos/ref/%d" % set_id(conn, "k")).status_code == 404                   # set without ref_url


def test_switch_defaults_off(tmp_path):
    app, conn = make(tmp_path)
    c = app.test_client()
    login(c)
    assert store.get_state(conn, "own_photos") != "1"
    assert "выключены" in c.get("/photos/review").get_data(as_text=True)


def test_new_token_revokes_old(tmp_path):
    import re
    app, conn = make(tmp_path)
    c = app.test_client()
    tok = login(c)
    def issue():
        r = post(c, "/photos/agent-token", tok)
        assert r.headers["Cache-Control"] == "no-store"
        return re.search(r"<code>([^<]+)</code>", r.get_data(as_text=True)).group(1)
    old = issue()
    new = issue()
    agent = app.test_client()
    assert agent.get("/api/image-jobs", headers={"Authorization": "Bearer " + old}).status_code == 403
    assert agent.get("/api/image-jobs", headers={"Authorization": "Bearer " + new}).status_code == 200


def test_regenerate_queues_job_for_enqueueable_set(tmp_path):
    app, conn = make(tmp_path)
    conn.execute("INSERT INTO photo_set (key, state, ref_url, rank, brand_name, model_name) VALUES ('r','needed','https://x.example/r.jpg',0,'R','R')")
    conn.execute("INSERT INTO set_frame (id, set_key, kind, photo_id, status, created) VALUES (20,'r','three_quarter',3,'pending','t')")
    conn.commit()
    c = app.test_client()
    tok = login(c)
    post(c, "/photos/frame/20/regenerate", tok)
    assert status(conn, 20) == "rejected"
    assert conn.execute("SELECT status FROM image_job WHERE set_key='r' AND kind='three_quarter'").fetchone() == ("queued",)


def test_approve_queues_set4_at_once(tmp_path):
    app, conn = make(tmp_path)
    conn.execute("INSERT INTO photo_set (key, state, ref_url, rank, brand_name, model_name) VALUES ('r','needed','https://x.example/r.jpg',0,'R','R')")
    conn.execute("INSERT INTO set_frame (id, set_key, kind, photo_id, status, created) VALUES (20,'r','three_quarter',3,'pending','t')")
    conn.commit()
    c = app.test_client()
    tok = login(c)
    post(c, "/photos/frame/20/approve", tok)
    assert status(conn, 20) == "approved"
    assert conn.execute("SELECT status FROM image_job WHERE set_key='r' AND kind='set4'").fetchone() == ("queued",)


# --- «Генерация»: queue and spend tab, queue stop switch ---

def add_needed(conn, key, rank, brand, model, state="needed", cls="r15_18"):
    conn.execute("INSERT INTO photo_set (key, state, rank, brand_name, model_name, season, studded, size_class) VALUES (?,?,?,?,?,'winter',1,?)",
                 (key, state, rank, brand, model, cls))


def job(conn, key, kind="three_quarter", status="queued", cost=0.0, updated="2026-10-07T10:00:00"):
    conn.execute("INSERT INTO image_job (set_key, kind, status, cost_usd, created, updated) VALUES (?,?,?,?,?,?)", (key, kind, status, cost, updated, updated))
    conn.commit()


def queue_page(c):
    return c.get("/photos/review?tab=queue").get_data(as_text=True)


def test_nav_says_generation(tmp_path):
    app, _ = make(tmp_path)
    c = app.test_client()
    login(c)
    page = c.get("/photos/review").get_data(as_text=True)
    assert "Генерация" in page and ">Фото<" not in page and "Фото на проверке" not in page
    assert "Очередь и расходы" in page


def test_queue_tab_lists_needed_sets_in_rank_order(tmp_path):
    app, conn = make(tmp_path)
    add_needed(conn, "q1", 1, "SecondBrand", "M1")
    add_needed(conn, "q0", 0, "FirstBrand", "M0", cls="r19p")
    add_needed(conn, "gone", 0, "RetiredBrand", "MX", state="retired")
    conn.commit()
    c = app.test_client()
    login(c)
    page = queue_page(c)
    assert page.index("FirstBrand") < page.index("SecondBrand")
    assert "RetiredBrand" not in page and "B2" not in page
    assert "R19+" in page and "R15–R18" in page


def test_queue_tab_shows_frame_status_and_attempts(tmp_path):
    app, conn = make(tmp_path)                                  # set 'k': three_quarter approved + pending frames
    job(conn, "k", "set4", "running")
    conn.execute("INSERT INTO set_frame (set_key, kind, photo_id, status, created) VALUES ('k','three_quarter',3,'rejected','t')")
    conn.commit()
    c = app.test_client()
    login(c)
    page = queue_page(c)
    assert "одобрен" in page and "в работе" in page and "1 из 3" in page


def test_queue_tab_spend_today(tmp_path):
    app, conn = make(tmp_path)
    job(conn, "k", status="done", cost=1.25, updated="2026-10-07T08:00:00")
    job(conn, "k", status="done", cost=2.00, updated="2026-10-06T08:00:00")
    job(conn, "k", status="queued")
    job(conn, "k", kind="set4", status="running")
    c = app.test_client()
    login(c)
    page = queue_page(c)
    assert "$1.25 из $5.00" in page
    assert "В очереди: 1, в работе: 1" in page


def test_queue_tab_state_lines(tmp_path):
    app, conn = make(tmp_path)
    c = app.test_client()
    tok = login(c)
    assert "Очередь работает" in queue_page(c) and "Остановить очередь" in queue_page(c)
    store.set_state(conn, "image_queue_stopped", "1")
    conn.commit()
    page = queue_page(c)
    assert "Очередь остановлена владельцем" in page and "Запустить очередь" in page
    store.set_state(conn, "image_queue_stopped", "0")
    job(conn, "k", status="done", cost=5.0, updated="2026-10-07T08:00:00")
    assert "Лимит на сегодня исчерпан" in queue_page(c)
    store.set_state(conn, "image_queue_stopped", "1")
    conn.commit()
    page = queue_page(c)
    assert "остановлена владельцем" in page and "Лимит на сегодня исчерпан" not in page


def test_queue_switch_requires_login_and_csrf(tmp_path):
    app, conn = make(tmp_path)
    anon = app.test_client()
    r = anon.post("/photos/queue-switch", data={"on": "0"})
    assert r.status_code in (302, 400) and store.get_state(conn, "image_queue_stopped") is None
    c = app.test_client()
    tok = login(c)
    assert c.post("/photos/queue-switch", data={"on": "0"}).status_code == 400
    assert store.get_state(conn, "image_queue_stopped") is None
    assert post(c, "/photos/queue-switch", tok, on="0").status_code == 302
    assert store.get_state(conn, "image_queue_stopped") == "1"
    post(c, "/photos/queue-switch", tok, on="1")
    assert store.get_state(conn, "image_queue_stopped") == "0"


def test_tab_label_and_russian_season(tmp_path):
    app, conn = make(tmp_path)
    add_needed(conn, "w1", 0, "WinBrand", "M1")                                  # winter, studded
    conn.execute("INSERT INTO photo_set (key, state, rank, brand_name, model_name, season, studded) VALUES ('w2','needed',2,'PlainBrand','M2','winter',0)")
    conn.execute("INSERT INTO photo_set (key, state, rank, brand_name, model_name, season, studded) VALUES ('s1','needed',3,'SumBrand','M3','summer',0)")
    conn.execute("INSERT INTO photo_set (key, state, rank, brand_name, model_name, season, studded) VALUES ('a1','needed',4,'AllBrand','M4','all',0)")
    conn.execute("INSERT INTO photo_set (key, state, rank, brand_name, model_name) VALUES ('u1','needs_decision',5,'UnkBrand','M5')")
    conn.commit()
    c = app.test_client()
    login(c)
    q = queue_page(c)
    assert ">Одобрены (" in q and "Одобрено (" not in q
    for text in ("Зима · шипы", "Зима · без шипов", "Лето", "Всесезонные"):
        assert text in q
    assert "winter" not in q
    d = c.get("/photos/review?tab=decision").get_data(as_text=True)
    assert "Не указан" in d


def test_review_table_layout_texts(tmp_path):
    app, conn = make(tmp_path)
    conn.execute("UPDATE photo_set SET season='winter', studded=1, size_class='r15_18' WHERE key='k'")
    conn.commit()
    c = app.test_client()
    login(c)
    page = c.get("/photos/review?tab=pending").get_data(as_text=True)
    assert "Зима · шипы, R15–R18" in page and "r15_18" not in page and "Три четверти" not in page
    assert "Вполоборота" in page and "Комплект 4 шт." in page and 'class="tabs2"' in page and 'class="table-wrap"' in page
    assert 'target="_blank"' in page


# --- priority of sold models ---

def test_priority_requires_login_and_csrf(tmp_path):
    app, conn = make(tmp_path)
    r = app.test_client().post("/photos/priority", data={"sold": "x"})
    assert r.status_code in (302, 400) and store.get_state(conn, "photo_priority_sold") is None
    c = app.test_client()
    login(c)
    assert c.post("/photos/priority", data={"sold": "x"}).status_code == 400


def test_priority_saves_reranks_and_reports(tmp_path):
    app, conn = make(tmp_path)
    add_needed(conn, "a", 0, "Kumho", "WinterCraft Ice Wi32")
    add_needed(conn, "b", 5, "Ikon Tyres", "Character Ice 7")
    conn.commit()
    c = app.test_client()
    tok = login(c)
    sold = "Ikon Tyres 205/55/16 T 94 Ikon Character Ice 7 XL Ш.\nYokohama 225/60/17 V 99 AE61\n"
    assert post(c, "/photos/priority", tok, sold=sold).status_code == 302
    assert store.get_state(conn, "photo_priority_sold") == sold
    assert conn.execute("SELECT key FROM photo_set WHERE state='needed' ORDER BY rank LIMIT 1").fetchone() == ("b",)
    page = c.get("/photos/review?tab=queue").get_data(as_text=True)
    assert "Продано: 1" in page and "AE61" in page and "Character Ice 7 XL Ш." in page


def test_queue_tab_groups_rows_and_hides_zero_tries(tmp_path):
    app, conn = make(tmp_path)
    add_needed(conn, "busy", 0, "Kumho", "Busy")
    add_needed(conn, "idle", 1, "Kumho", "Idle")
    conn.execute("UPDATE photo_set SET ref_url='https://x.example/r.jpg' WHERE key='busy'")
    job(conn, "busy", status="queued")
    c = app.test_client()
    login(c)
    page = queue_page(c)
    assert "Сейчас в работе (1)" in page and "Ждут своей очереди (2)" in page   # k: approved three-quarter, no set4 yet
    assert page.index("Kumho Busy") < page.index("Ждут своей очереди") < page.index("Kumho Idle")
    assert "попыток 0 из" not in page and 'src="/photos/ref/' in page
    assert "на проверке: 1" in page                       # set 'k' has a pending frame


def test_queue_tab_shows_supplier_spellings_of_a_merged_set(tmp_path):
    app, conn = make(tmp_path)
    conn.execute("INSERT INTO photo_set (key, state, rank, brand_name, model_name, model_key, season, studded, size_class)"
                 " VALUES ('kumho|WS71|winter|n|r15_18','needed',0,'Kumho','WinterCraft WS71','WS71','winter',0,'r15_18')")
    conn.execute("INSERT INTO set_alias (alias_key, set_key) VALUES ('kumho|WS71SUV|winter|n|r15_18','kumho|WS71|winter|n|r15_18')")
    conn.commit()
    c = app.test_client()
    login(c)
    assert "У поставщика: WS71, WS71SUV" in queue_page(c)


# --- «По моделям»: what is generated and what is not, by brand and model ---

def models_page(c, show=""):
    return c.get("/photos/review?tab=models" + ("&show=" + show if show else "")).get_data(as_text=True)


def test_models_tab_splits_generated_and_not_by_brand(tmp_path):
    app, conn = make(tmp_path)
    for key, brand, model, stock in (("g", "Ikon Tyres", "Ready One", 40), ("n", "Ikon Tyres", "Fresh One", 90), ("k2", "Kumho", "Other", 12)):
        add_needed(conn, key, 0, brand, model)
        conn.execute("UPDATE photo_set SET positions=3, stock=? WHERE key=?", (stock, key))
    for kind, n in (("three_quarter", 1), ("set4", 2)):
        conn.execute("INSERT INTO set_frame (set_key, kind, photo_id, status, created) VALUES ('g',?,?,'approved','t')", (kind, n))
    conn.commit()
    c = app.test_client()
    login(c)
    todo = models_page(c)                                   # default: not generated yet
    assert "Fresh One" in todo and "Other" in todo and "Ready One" not in todo
    assert todo.index("Ikon Tyres") < todo.index("Fresh One") and "90 шт." in todo
    done = models_page(c, "done")
    assert "Ready One" in done and "Fresh One" not in done
    every = models_page(c, "all")
    assert "Ready One" in every and "Fresh One" in every and "готово 1 из 2" in every
