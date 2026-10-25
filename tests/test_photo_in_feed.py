"""Owner, 2026-10-22: own photos go to the feed only for the models the owner sent there (one by one or many at once);
the switch `own_photos` stays the emergency stop for all of them."""
import test_feed_own_photos as f
from panel_helpers import login, make_app, post
from tires_sync import photo_library as pl
from tires_sync import store


def test_a_set_not_sent_to_the_feed_keeps_the_supplier_photo(tmp_path):
    conn, first, second = f.env(tmp_path)
    f.library(conn, first, second)
    conn.execute("UPDATE photo_set SET in_feed = 0")
    conn.commit()
    f.run(tmp_path, conn)
    assert f.images(tmp_path, conn, first)["100"] == [f.supplier(conn, first)]


def test_a_set_sent_to_the_feed_gets_its_photos(tmp_path):
    conn, first, second = f.env(tmp_path)
    links = f.library(conn, first, second)[first]
    f.run(tmp_path, conn)
    assert f.images(tmp_path, conn, first)["100"][:2] == [links["tq"], links["set4"]]


def test_set_in_feed_changes_only_the_given_sets(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    for key in ("a", "b", "c"):
        conn.execute("INSERT INTO photo_set (key, state) VALUES (?, 'needed')", (key,))
    ids = {k: r for r, k in conn.execute("SELECT rowid, key FROM photo_set")}
    assert pl.set_in_feed(conn, [ids["a"], ids["b"]], True) == 2
    assert pl.set_in_feed(conn, [ids["a"]], True) == 0                       # already there
    assert pl.set_in_feed(conn, [ids["b"]], False) == 1
    assert dict(conn.execute("SELECT key, in_feed FROM photo_set")) == {"a": 1, "b": 0, "c": 0}


def _panel(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(tmp_path / "t.db")
    for key, model in (("x", "One"), ("y", "Two")):
        conn.execute("INSERT INTO photo_set (key, state, brand_name, model_name, season, studded, size_class) VALUES (?, 'needed', 'Kumho', ?, 'winter', 1, 'r15_18')", (key, model))
        for kind in ("three_quarter", "set4"):
            conn.execute("INSERT INTO set_frame (set_key, kind, photo_id, status, created) VALUES (?, ?, 1, 'approved', 't')", (key, kind))
    conn.commit()
    ids = {k: r for r, k in conn.execute("SELECT rowid, key FROM photo_set")}
    return app, conn, ids


def _flags(conn):
    return dict(conn.execute("SELECT key, in_feed FROM photo_set"))


def test_panel_sends_one_or_many_and_takes_them_out(tmp_path):
    app, conn, ids = _panel(tmp_path)
    c = app.test_client()
    tok = login(c)
    assert post(c, "/photos/feed", tok, one="%d:1" % ids["x"]).status_code == 302
    assert _flags(conn) == {"x": 1, "y": 0}
    c.post("/photos/feed", data={"csrf": tok, "on": "1", "set": [str(ids["x"]), str(ids["y"])]})
    assert _flags(conn) == {"x": 1, "y": 1}
    c.post("/photos/feed", data={"csrf": tok, "on": "0", "set": [str(ids["y"])]})
    assert _flags(conn) == {"x": 1, "y": 0}
    page = c.get("/photos/review?tab=models&show=done").get_data(as_text=True)
    assert "в фиде" in page and "Отправить выбранные в фид" in page
    assert "выключатель выключен" in page                                  # the switch is off: a warning, the marks are kept


def test_panel_feed_needs_login_and_csrf(tmp_path):
    app, conn, ids = _panel(tmp_path)
    assert app.test_client().post("/photos/feed", data={"one": "%d:1" % ids["x"]}).status_code in (302, 400)
    c = app.test_client()
    login(c)
    assert c.post("/photos/feed", data={"one": "%d:1" % ids["x"]}).status_code == 400
    assert _flags(conn) == {"x": 0, "y": 0}


def test_a_sold_out_model_in_the_feed_stays_on_the_tab_and_can_be_taken_out(tmp_path):
    app, conn, ids = _panel(tmp_path)
    conn.execute("UPDATE photo_set SET in_feed = 1, state = 'retired' WHERE key = 'x'")
    conn.commit()
    c = app.test_client()
    tok = login(c)
    page = c.get("/photos/review?tab=models&show=done").get_data(as_text=True)
    assert "One" in page and "нет в наличии" in page and "готово 1 из 1" in page     # not counted among the models in stock
    assert "One" not in c.get("/photos/review?tab=models").get_data(as_text=True)
    post(c, "/photos/feed", tok, one="%d:0" % ids["x"])
    assert _flags(conn)["x"] == 0
    assert "One" not in c.get("/photos/review?tab=models&show=done").get_data(as_text=True)


def test_feed_ids_must_be_plain_digits(tmp_path):
    app, conn, ids = _panel(tmp_path)
    c = app.test_client()
    tok = login(c)
    assert post(c, "/photos/feed", tok, one="²:1").status_code == 400
