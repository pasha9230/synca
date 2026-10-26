"""Owner, 2026-10-27: own tire photos only in the accounts chosen for them («2»: only «Основной» for now)."""
import test_feed_own_photos as f
from panel_helpers import login, make_app, post
from tires_sync import connections, photo_library as pl, store


def test_a_new_base_gives_own_photos_to_the_primary_account_only(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    first = connections.create_account(conn, "Основной", f.NOW, "o")
    second = connections.create_account(conn, "Второй", f.NOW, "o")
    assert connections.account_own_photos(conn, first) and not connections.account_own_photos(conn, second)


def test_an_account_without_own_photos_keeps_the_supplier_photo(tmp_path):
    conn, first, second = f.env(tmp_path)
    links = f.library(conn, first, second)
    connections.set_account_own_photos(conn, second, False, f.NOW, "o")
    conn.commit()
    f.run(tmp_path, conn)
    assert f.images(tmp_path, conn, first)["100"][:2] == [links[first]["tq"], links[first]["set4"]]
    assert f.images(tmp_path, conn, second)["100_a%d" % second] == [f.supplier(conn, second)]
    assert pl.own_photos_for(conn, second, f.BASE) is None


def test_copies_are_not_asked_or_taken_for_an_account_without_own_photos(tmp_path):
    import test_agent_api as t
    app, conn, client, auth = t.setup(tmp_path)
    key = t.add_set(app, conn)
    second = connections.create_account(conn, "Второй", t.NOW, "o")            # own photos off by default
    conn.commit()
    pid = t.approve(conn, app, key, "three_quarter", (5, 5, 5))
    todo = client.get("/api/photo-copies/todo", headers=auth).get_json()
    assert todo and {x["account_id"] for x in todo} == {connections.PRIMARY_ACCOUNT}
    up = client.post("/api/photo-copies", headers=auth, data={"account_id": str(second), "source_photo_id": str(pid), "kind": "crop", "variant": "",
                                                               "file": (__import__("io").BytesIO(t.jpeg((9, 9, 9))), "c.jpg")})
    assert up.status_code == 400


def test_panel_toggle_needs_login_and_csrf_and_switches(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(tmp_path / "t.db")
    acc = connections.create_account(conn, "Второй", f.NOW, "o") if not connections.list_feed_accounts(conn) else 2
    conn.commit()
    anon = app.test_client()
    assert anon.post("/connections/accounts/%d/own-photos" % acc, data={"on": "1"}).status_code in (302, 400)
    c = app.test_client()
    tok = login(c)
    assert c.post("/connections/accounts/%d/own-photos" % acc, data={"on": "1"}).status_code == 400
    before = connections.account_own_photos(conn, acc)
    assert post(c, "/connections/accounts/%d/own-photos" % acc, tok, on="0" if before else "1").status_code == 302
    assert connections.account_own_photos(conn, acc) is (not before)
    assert "Свои фото шин" in c.get("/connections/avito/%d" % acc).get_data(as_text=True)


def test_migration_of_a_live_base_turns_own_photos_on_for_the_primary_only(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    for name in ("Основной", "Второй", "Третий"):
        connections.create_account(conn, name, f.NOW, "o")
    conn.execute("ALTER TABLE account DROP COLUMN own_photos")             # a base from before the column
    conn.commit()
    conn.close()
    conn = store.connect(tmp_path / "t.db")
    assert dict(conn.execute("SELECT id, own_photos FROM account")) == {1: 1, 2: 0, 3: 0}
    connections.set_account_own_photos(conn, 3, True, f.NOW, "o")
    conn.commit()
    conn.close()
    conn = store.connect(tmp_path / "t.db")                                 # a second start does not reset the choice
    assert dict(conn.execute("SELECT id, own_photos FROM account")) == {1: 1, 2: 0, 3: 1}
