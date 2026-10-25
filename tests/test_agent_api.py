"""Token-protected API for the photo agent on the owner's Mac."""
from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from http_fakes import ScriptedHttp
from panel_helpers import login, make_app
from tires_sync import connections, photo_library, store
from tires_sync.panel import routes_agent
from tires_sync.secretbox import SecretBox
from cryptography.fernet import Fernet

NOW = "2026-10-07T12:00:00"
BOT = "123456:ABCdef-SECRET-token"


def jpeg(color=(200, 10, 10)):
    buf = io.BytesIO()
    Image.new("RGB", (8, 6), color).save(buf, "JPEG")
    return buf.getvalue()


def setup(tmp_path, **cfg):
    app = make_app(tmp_path, PHOTO_FETCH=lambda url: (jpeg((1, 2, 3)), "jpg"), **cfg)
    conn = store.connect(app.config["DB_PATH"])
    assert connections.create_account(conn, "Главный", NOW, "o") == connections.PRIMARY_ACCOUNT
    conn.commit()
    token = routes_agent.issue_agent_token(conn)
    return app, conn, app.test_client(), {"Authorization": "Bearer " + token}


def the_key(app, conn):
    positions = routes_agent.set_positions(conn, app.config)
    assert len(positions) >= 1
    return sorted(positions)[0]


def add_set(app, conn):
    key = the_key(app, conn)
    brand, model, season, st, cls = key.split("|")
    conn.execute("INSERT INTO photo_set (key, brand_key, model_key, season, studded, size_class, ref_url, size_hint, state, rank, brand_name, model_name)"
                 " VALUES (?,?,?,?,?,?,?,?,?,0,?,?)", (key, brand, model, season, int(st == "s"), cls, "https://example.test/1.jpg", "205/55 R16", "needed",
                                                       "Ikon Tyres", "Test Model"))
    conn.commit()
    return key


def queue(conn, key, kind="three_quarter", updated=NOW, cost=0.0, status="queued"):
    job = conn.execute("INSERT INTO image_job (set_key, kind, status, cost_usd, created, updated) VALUES (?,?,?,?,?,?)",
                       (key, kind, status, cost, NOW, updated)).lastrowid
    conn.commit()
    return job


def test_agent_token_scope(tmp_path):
    app, conn, client, auth = setup(tmp_path)
    assert client.get("/api/image-jobs").status_code == 403
    assert client.get("/api/image-jobs", headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert client.get("/api/image-jobs", headers={"Authorization": "Basic abc"}).status_code == 403
    ok = client.get("/api/image-jobs", headers=auth)
    assert ok.status_code == 200 and ok.get_json() == [] and "Set-Cookie" not in ok.headers
    home = client.get("/", headers=auth)                                # the token opens nothing else
    assert home.status_code == 302 and home.headers["Location"].endswith("/login")
    login(client)                                                    # an owner's session is no substitute for the token
    assert client.get("/api/image-jobs").status_code == 403
    assert client.post("/api/image-jobs/1/fail", data={"error": "x"}).status_code == 403
    assert client.get("/api/photo-copies/todo").status_code == 403
    assert client.post("/api/review-sheet").status_code == 403
    assert client.post("/api/image-jobs/1/fail", data={"error": "x"}, headers=auth).status_code == 404     # no csrf needed with the token


def test_missing_stored_token_is_always_403(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    for header in ({}, {"Authorization": "Bearer "}, {"Authorization": "Bearer null"}, {"Authorization": "Bearer " + "0" * 64}):
        assert client.get("/api/image-jobs", headers=header).status_code == 403


def test_token_stored_only_as_hash(tmp_path):
    app, conn, client, auth = setup(tmp_path)
    raw = auth["Authorization"].split()[1]
    stored = store.get_state(conn, "image_agent_token")
    assert stored != raw and len(stored) == 64 and raw not in json.dumps(dict(conn.execute("SELECT key, value FROM app_state").fetchall()))


def test_reissue_token_revokes_old(tmp_path):
    app, conn, client, auth = setup(tmp_path)
    assert client.get("/api/image-jobs", headers=auth).status_code == 200
    new = routes_agent.issue_agent_token(conn)
    assert client.get("/api/image-jobs", headers=auth).status_code == 403
    assert client.get("/api/image-jobs", headers={"Authorization": "Bearer " + new}).status_code == 200


def test_jobs_handout_marks_running(tmp_path):
    app, conn, client, auth = setup(tmp_path)
    key = add_set(app, conn)
    job = queue(conn, key)
    queue(conn, "other|set", status="queued")                        # a job of an unknown set is never handed out
    data = client.get("/api/image-jobs?limit=50", headers=auth).get_json()
    assert [j["id"] for j in data] == [job]
    item = data[0]
    assert item["kind"] == "three_quarter" and item["set_key"] == key and item["season"] == "winter"
    assert item["studded"] is False and item["size_class"] == "r15_18" and item["size_hint"] == "205/55 R16"
    assert item["ref_url"] == "http://localhost/api/image-jobs/%d/ref" % job and item["approved_three_quarter_url"] is None
    row = conn.execute("SELECT status, attempts FROM image_job WHERE id=?", (job,)).fetchone()
    assert tuple(row) == ("running", 1)
    assert client.get("/api/image-jobs", headers=auth).get_json() == []


def test_jobs_skip_sets_not_needed_and_respect_cap(tmp_path):
    app, conn, client, auth = setup(tmp_path)
    key = add_set(app, conn)
    queue(conn, key)
    queue(conn, key, kind="set4", status="done", updated="2026-10-07T01:00:00", cost=5.0)    # spent today: the cap is reached
    assert client.get("/api/image-jobs", headers=auth).get_json() == []
    conn.execute("UPDATE image_job SET updated='2026-10-06T23:00:00' WHERE status='done'")
    conn.commit()
    assert len(client.get("/api/image-jobs", headers=auth).get_json()) == 1
    conn.execute("UPDATE image_job SET status='queued' WHERE status='running'")
    conn.execute("UPDATE photo_set SET state='retired'")
    conn.commit()
    assert client.get("/api/image-jobs", headers=auth).get_json() == []


def test_approved_three_quarter_url_for_set4(tmp_path):
    app, conn, client, auth = setup(tmp_path)
    key = add_set(app, conn)
    pid = photo_library.save_photo(conn, app.config["PHOTOS_ROOT"], jpeg(), "agent", NOW)
    conn.execute("INSERT INTO set_frame (set_key, kind, photo_id, status, created) VALUES (?,?,?,?,?)", (key, "three_quarter", pid, "approved", NOW))
    conn.commit()
    queue(conn, key, kind="set4")
    sha, ext = conn.execute("SELECT sha256, ext FROM photo WHERE id=?", (pid,)).fetchone()
    item = client.get("/api/image-jobs", headers=auth).get_json()[0]
    assert item["approved_three_quarter_url"] == "http://localhost/photos/%s.%s" % (sha, ext)


def test_ref_proxy_fetches_server_side(tmp_path):
    app, conn, client, auth = setup(tmp_path)
    key = add_set(app, conn)
    job = queue(conn, key)
    assert client.get("/api/image-jobs/%d/ref" % job).status_code == 403
    reply = client.get("/api/image-jobs/%d/ref" % job, headers=auth)
    assert reply.status_code == 200 and reply.mimetype == "image/jpeg" and reply.data == jpeg((1, 2, 3))
    assert client.get("/api/image-jobs/9999/ref", headers=auth).status_code == 404


def test_result_creates_pending_frame(tmp_path):
    app, conn, client, auth = setup(tmp_path)
    key = add_set(app, conn)
    job = queue(conn, key)
    client.get("/api/image-jobs", headers=auth)
    data = jpeg((9, 9, 9))
    reply = client.post("/api/image-jobs/%d/result" % job, headers=auth, data={"cost_usd": "0.07", "file": (io.BytesIO(data), "a.jpg")})
    assert reply.status_code == 201
    frame = conn.execute("SELECT f.set_key, f.kind, f.status, p.source FROM set_frame f JOIN photo p ON p.id=f.photo_id").fetchone()
    assert tuple(frame) == (key, "three_quarter", "pending", "agent")
    assert tuple(conn.execute("SELECT status, cost_usd FROM image_job WHERE id=?", (job,)).fetchone()) == ("done", 0.07)
    again = client.post("/api/image-jobs/%d/result" % job, headers=auth, data={"cost_usd": "0.07", "file": (io.BytesIO(data), "a.jpg")})
    assert again.status_code == 409 and conn.execute("SELECT COUNT(*) FROM set_frame").fetchone()[0] == 1


@pytest.mark.parametrize("cost,payload", [("abc", None), ("-1", None), ("nan", None), ("0.1", b"not an image")])
def test_result_rejects_bad_input(tmp_path, cost, payload):
    app, conn, client, auth = setup(tmp_path)
    job = queue(conn, add_set(app, conn))
    client.get("/api/image-jobs", headers=auth)
    reply = client.post("/api/image-jobs/%d/result" % job, headers=auth, data={"cost_usd": cost, "file": (io.BytesIO(payload or jpeg()), "a.jpg")})
    assert reply.status_code == 400
    assert conn.execute("SELECT status FROM image_job WHERE id=?", (job,)).fetchone()[0] == "running"
    assert conn.execute("SELECT COUNT(*) FROM set_frame").fetchone()[0] == 0


def test_fail_records_error(tmp_path):
    app, conn, client, auth = setup(tmp_path)
    job = queue(conn, add_set(app, conn))
    client.get("/api/image-jobs", headers=auth)
    assert client.post("/api/image-jobs/%d/fail" % job, headers=auth, data={"error": "x" * 900}).status_code == 200
    row = conn.execute("SELECT status, error FROM image_job WHERE id=?", (job,)).fetchone()
    assert row[0] == "failed" and 0 < len(row[1]) <= 500


def approve(conn, app, key, kind, color):
    pid = photo_library.save_photo(conn, app.config["PHOTOS_ROOT"], jpeg(color), "agent", NOW)
    conn.execute("INSERT INTO set_frame (set_key, kind, photo_id, status, created) VALUES (?,?,?,?,?)", (key, kind, pid, "approved", NOW))
    conn.commit()
    return pid


def test_copies_todo_lists_missing_only(tmp_path):
    app, conn, client, auth = setup(tmp_path)
    key = add_set(app, conn)
    second = connections.create_account(conn, "Второй", NOW, "o")
    conn.commit()
    pid = approve(conn, app, key, "three_quarter", (5, 5, 5))
    todo = client.get("/api/photo-copies/todo", headers=auth).get_json()
    kinds = sorted((t["account_id"], t["kind"], t["variant"]) for t in todo if t["kind"] != "card")
    assert kinds == [(1, "crop", ""), (second, "copy", ""), (second, "crop", "")]
    assert all(t["source_photo_id"] == pid and t["source_url"].startswith("http://localhost/photos/") for t in todo)
    cards = [t for t in todo if t["kind"] == "card"]
    assert {t["account_id"] for t in cards} == {1, second}
    one = next(t for t in cards if t["account_id"] == 1)
    assert one["variant"] == "205/55 R16 94T|x1" and one["card"] == {"size": "205/55 R16 94T", "season_line": "Зима · без шипов", "set_line": None}
    assert not any(t["variant"].endswith("|x4") for t in cards)                      # pack4 is off
    up = client.post("/api/photo-copies", headers=auth, data={"account_id": str(second), "source_photo_id": str(pid), "kind": "copy", "variant": "",
                                                              "file": (io.BytesIO(jpeg((7, 7, 7))), "c.jpg")})
    assert up.status_code == 201
    after = client.get("/api/photo-copies/todo", headers=auth).get_json()
    assert len(after) == len(todo) - 1 and not any(t["account_id"] == second and t["kind"] == "copy" for t in after)


def test_copies_todo_pack4_cards(tmp_path):
    from tires_sync import settings_store
    from helpers import settings_dict
    app, conn, client, auth = setup(tmp_path)
    key = add_set(app, conn)
    approve(conn, app, key, "three_quarter", (5, 5, 5))
    vid = settings_store._insert_version(conn, settings_dict(pack4=True, pack4_rules=["*|*|*"]), "2026-10-07T13:00:00", "t", "")
    conn.execute("UPDATE settings_active SET version_id=?", (vid,))
    conn.commit()
    todo = client.get("/api/photo-copies/todo", headers=auth).get_json()
    x4 = [t for t in todo if t["variant"].endswith("|x4")]
    assert x4 and x4[0]["card"]["set_line"] == "Комплект 4 шт."


def test_copy_upload_validation(tmp_path):
    app, conn, client, auth = setup(tmp_path)
    key = add_set(app, conn)
    pid = approve(conn, app, key, "three_quarter", (5, 5, 5))
    base = {"account_id": "1", "source_photo_id": str(pid), "kind": "crop", "variant": ""}
    def post(**over):
        data = dict(base, **over)
        data["file"] = (io.BytesIO(jpeg((3, 3, 3))), "c.jpg")
        return client.post("/api/photo-copies", headers=auth, data=data).status_code
    assert post() == 201 and post() == 201                          # re-upload is idempotent
    assert conn.execute("SELECT COUNT(*) FROM account_photo").fetchone()[0] == 1
    assert post(kind="copy") == 400                                  # the primary account gets originals, not copies
    assert post(kind="nope") == 400 and post(account_id="99") == 400 and post(source_photo_id="999") == 400 and post(account_id="x") == 400


def telegram(conn, box):
    connections.set_config(conn, "telegram", 0, {"chat_id": "1000"}, NOW, "owner")
    connections.set_secret(conn, box, "telegram", 0, "bot_token", BOT, NOW, "owner")


def test_review_sheet_sends_to_telegram(tmp_path):
    http = ScriptedHttp((200, {"ok": True, "result": {}}))
    box = SecretBox(Fernet.generate_key())
    app, conn, client, auth = setup(tmp_path, SECRET_BOX=box, TELEGRAM_HTTP=http)
    key = add_set(app, conn)
    telegram(conn, box)
    sheet = jpeg()
    reply = client.post("/api/review-sheet", headers=auth, data={"set_key": key, "file": (io.BytesIO(sheet), "s.jpg")})
    assert reply.status_code == 200
    method, url, headers, body = http.calls[0]
    assert url == "https://api.telegram.org/bot%s/sendPhoto" % BOT and sheet in body
    assert "Фото на проверке: ".encode() in body and b"http://localhost/photos/review" in body and b"1000" in body


def test_review_sheet_without_telegram_is_204(tmp_path):
    app, conn, client, auth = setup(tmp_path, SECRET_BOX=SecretBox(Fernet.generate_key()))
    key = add_set(app, conn)
    reply = client.post("/api/review-sheet", headers=auth, data={"set_key": key, "file": (io.BytesIO(jpeg()), "s.jpg")})
    assert reply.status_code == 204
    assert client.post("/api/review-sheet", headers=auth, data={"set_key": "nope", "file": (io.BytesIO(jpeg()), "s.jpg")}).status_code == 404


def test_display_names_from_a_second_supplier_set(tmp_path):
    from tires_sync.candidates import Candidate
    from helpers import make_offer
    http = ScriptedHttp((200, {"ok": True, "result": {}}))
    box = SecretBox(Fernet.generate_key())
    app, conn, client, auth = setup(tmp_path, SECRET_BOX=box, TELEGRAM_HTTP=http)
    telegram(conn, box)
    offer = make_offer(goods_id="S2:7", brand_key="S2:NOKIAN", brand_raw="nokian", model_raw="hakka 10", diameter=17)
    plans = photo_library.needed_sets({"S2:7": Candidate("S2:7", offer, 1, "Nokian Tyres", "Hakkapeliitta 10")}, 4)
    assert (plans[0].brand_name, plans[0].model_name) == ("Nokian Tyres", "Hakkapeliitta 10")
    photo_library.sync_sets(conn, plans, NOW)
    photo_library.enqueue(conn, NOW)
    item = client.get("/api/image-jobs", headers=auth).get_json()[0]
    assert (item["brand"], item["model"]) == ("Nokian Tyres", "Hakkapeliitta 10")
    reply = client.post("/api/review-sheet", headers=auth, data={"set_key": item["set_key"], "file": (io.BytesIO(jpeg()), "s.jpg")})
    assert reply.status_code == 200
    assert "Фото на проверке: Nokian Tyres Hakkapeliitta 10, ".encode() in http.calls[0][3] and b"s2:" not in http.calls[0][3].lower()


def test_stuck_running_jobs_fail_with_timeout(tmp_path):
    app, conn, client, auth = setup(tmp_path)
    key = add_set(app, conn)
    stuck = queue(conn, key, status="running", updated="2026-10-07T09:00:00")
    fresh = queue(conn, key, kind="set4", status="running", updated="2026-10-07T11:30:00")
    client.get("/api/image-jobs", headers=auth)
    assert tuple(conn.execute("SELECT status, error FROM image_job WHERE id=?", (stuck,)).fetchone()) == ("failed", "timeout")
    assert conn.execute("SELECT status FROM image_job WHERE id=?", (fresh,)).fetchone()[0] == "running"


def test_stuck_jobs_are_swept_even_when_the_cap_is_reached(tmp_path):
    app, conn, client, auth = setup(tmp_path)
    key = add_set(app, conn)
    queue(conn, key, kind="set4", status="done", updated="2026-10-07T01:00:00", cost=5.0)    # the cap is reached
    stuck = queue(conn, key, status="running", updated="2026-10-07T09:00:00")
    assert client.get("/api/image-jobs", headers=auth).get_json() == []
    assert tuple(conn.execute("SELECT status, error FROM image_job WHERE id=?", (stuck,)).fetchone()) == ("failed", "timeout")


def test_ref_proxy_accepts_webp(tmp_path):
    app, conn, client, auth = setup(tmp_path)
    app.config["PHOTO_FETCH"] = lambda url: (b"RIFFxxxxWEBP", "webp")
    job = queue(conn, add_set(app, conn))
    reply = client.get("/api/image-jobs/%d/ref" % job, headers=auth)
    assert reply.status_code == 200 and reply.mimetype == "image/webp"


def test_csrf_exemption_is_exact_prefix(tmp_path):
    from tires_sync.panel.common import is_agent_path
    assert is_agent_path("/api/image-jobs") and is_agent_path("/api/image-jobs/3/fail") and is_agent_path("/api/review-sheet")
    assert not is_agent_path("/api/image-jobs-x") and not is_agent_path("/api/photo-copiesX/1") and not is_agent_path("/x/api/image-jobs")
    app = make_app(tmp_path)
    login(app.test_client())
    assert app.test_client().post("/api/image-jobs-x", data={}).status_code == 400


def test_fail_stores_cost(tmp_path):
    app, conn, client, auth = setup(tmp_path)
    job = queue(conn, add_set(app, conn))
    client.get("/api/image-jobs", headers=auth)
    assert client.post("/api/image-jobs/%d/fail" % job, headers=auth, data={"error": "upload", "cost_usd": "0.07"}).status_code == 200
    assert tuple(conn.execute("SELECT status, cost_usd FROM image_job WHERE id=?", (job,)).fetchone()) == ("failed", 0.07)


@pytest.mark.parametrize("cost", ["abc", "-1", "nan", "101"])
def test_fail_rejects_bad_cost(tmp_path, cost):
    app, conn, client, auth = setup(tmp_path)
    job = queue(conn, add_set(app, conn))
    client.get("/api/image-jobs", headers=auth)
    assert client.post("/api/image-jobs/%d/fail" % job, headers=auth, data={"error": "x", "cost_usd": cost}).status_code == 400
    assert conn.execute("SELECT status FROM image_job WHERE id=?", (job,)).fetchone()[0] == "running"


def test_stopped_queue_hands_out_nothing_but_sweeps(tmp_path):
    app, conn, client, auth = setup(tmp_path)
    key = add_set(app, conn)
    store.set_state(conn, "image_queue_stopped", "1")
    queued = queue(conn, key)
    stuck = queue(conn, key, kind="set4", status="running", updated="2026-10-07T09:00:00")
    assert client.get("/api/image-jobs", headers=auth).get_json() == []
    assert tuple(conn.execute("SELECT status, error FROM image_job WHERE id=?", (stuck,)).fetchone()) == ("failed", "timeout")
    assert conn.execute("SELECT status FROM image_job WHERE id=?", (queued,)).fetchone()[0] == "queued"


def test_fail_with_requeue_and_no_cost_is_not_an_attempt(tmp_path):
    app, conn, client, auth = setup(tmp_path)
    key = add_set(app, conn)
    job = queue(conn, key, status="running")
    assert client.post("/api/image-jobs/%d/fail" % job, headers=auth, data={"error": "OpenAI недоступен", "cost_usd": "0", "requeue": "1"}).status_code == 200
    assert conn.execute("SELECT status FROM image_job WHERE id=?", (job,)).fetchone() == ("queued",)
    assert photo_library.attempts_used(conn, key, "three_quarter") == 0
    job2 = queue(conn, key, kind="set4", status="running")              # money spent: it is an attempt whatever the agent says
    client.post("/api/image-jobs/%d/fail" % job2, headers=auth, data={"error": "upload lost", "cost_usd": "0.07", "requeue": "1"})
    assert conn.execute("SELECT status FROM image_job WHERE id=?", (job2,)).fetchone() == ("failed",)
