"""4 точки: the supplier row chooses itself (the owner 2026-10-21: «4 точки сделай»). No row saved and exactly one row fits (not the first, no
WebMIM address, named like 4tochki): that row is used for the load, the catalog and the tab, the WebMIM section hides it. Reading writes nothing;
the choice is saved only when the owner saves the tab. The feeds do not change: a row chosen by itself never opens «в фид»."""
from __future__ import annotations

import json
from datetime import datetime

import test_tochki_feed_unchanged as golden_case
from panel_helpers import post
from test_tochki_feed_unchanged import fixed_clock  # noqa: F401  (autouse: the clock of the golden feeds)
from test_tochki_panel import NOW, app_with, row
from test_webmim_no_tochki import supplier_tabs
from tires_sync import connections, store, tochki_feed
from tires_sync.cli import main


def unsaved(app_or_conn):
    conn = store.connect(app_or_conn.config["DB_PATH"]) if hasattr(app_or_conn, "config") else app_or_conn
    connections.set_config(conn, "tochki", 0, {"login": "api-user", "supplier_id": None, "feed": False}, NOW, "o")
    conn.commit()
    return conn


def stored(conn):
    return (conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0], json.dumps(connections.get_config(conn, "tochki", 0), sort_keys=True))


def test_the_only_row_that_fits_is_used_and_reading_writes_nothing(tmp_path):
    app, client, tok, sid = app_with(tmp_path)
    conn = unsaved(app)
    before = stored(conn)
    cfg = tochki_feed.config(conn)
    assert cfg["supplier_id"] == sid and cfg["auto"] and not cfg["feed"]
    page = client.get("/connections/tochki").get_data(as_text=True)
    assert "Выбрана автоматически по имени: №%d · 4Точки" % sid in page and "можно выбрать вручную" in page
    assert "data-tochki-no-row" not in page and "не выбран (каталог не загружается)" not in page                # it would not stop the load
    assert "сотрите логин" in page                                                                            # how to stop it
    assert "Поставщик «4Точки»" not in client.get("/").get_data(as_text=True)                             # not a WebMIM supplier on the home page
    assert 'value="%d" selected' % sid in page                                                  # saving the tab keeps it
    client.get("/connections/webmim")
    client.get("/catalog")
    assert stored(conn) == before                                                               # nothing written by reading


def test_the_webmim_section_hides_the_row_chosen_by_itself(tmp_path):
    app, client, tok, sid = app_with(tmp_path)
    conn = unsaved(app)
    other = connections.create_supplier(conn, "Второй WebMIM", NOW, "o")
    connections.set_config(conn, "webmim", other, {"base_url": "https://b.example", "mode": "apikey"}, NOW, "o")
    conn.commit()
    assert supplier_tabs(client.get("/connections/webmim").get_data(as_text=True)) == ["1", str(other)]
    r = client.get("/connections/webmim/%d" % sid)
    assert r.status_code == 302 and r.headers["Location"].endswith("/connections/tochki")


def test_saving_the_tab_stores_the_row(tmp_path):
    app, client, tok, sid = app_with(tmp_path)
    conn = unsaved(app)
    post(client, "/connections/tochki", tok, login="api-user", supplier_id=str(sid))
    assert connections.get_config(conn, "tochki", 0)["supplier_id"] == sid
    assert not tochki_feed.config(conn)["auto"]
    assert "Выбрана автоматически" not in client.get("/connections/tochki").get_data(as_text=True)


def test_none_two_or_an_unlike_row_choose_nothing_and_a_saved_row_wins(tmp_path):
    app, client, tok, sid = app_with(tmp_path, supplier=False)
    conn = store.connect(app.config["DB_PATH"])
    assert tochki_feed.config(conn)["supplier_id"] is None                                     # no row
    plain = connections.create_supplier(conn, "Новый поставщик", NOW, "o")                    # a new WebMIM row without its address yet
    many = connections.create_supplier(conn, "14 точек продаж", NOW, "o")                     # «4 точ» inside another number
    conn.commit()
    assert tochki_feed.config(conn)["supplier_id"] is None
    assert client.get("/connections/webmim/%d" % many).status_code == 200
    assert client.get("/connections/webmim/%d" % plain).status_code == 200                    # its WebMIM page stays open
    one = connections.create_supplier(conn, "4 точки", NOW, "o")
    two = connections.create_supplier(conn, "4Точки-2", NOW, "o")
    conn.commit()
    assert tochki_feed.config(conn) == {"login": "api-user", "supplier_id": None, "feed": False, "auto": False,    # two: none
                                        "accounts": [], "mode": "off", "allow": []}
    page = client.get("/connections/tochki").get_data(as_text=True)
    assert "data-tochki-no-row" in page and "Выбрана автоматически" not in page
    connections.set_config(conn, "tochki", 0, {"login": "api-user", "supplier_id": two, "feed": False}, NOW, "o")
    conn.commit()
    assert tochki_feed.config(conn)["supplier_id"] == two and not tochki_feed.config(conn)["auto"] and one != two


def test_the_load_and_the_catalog_take_the_row_chosen_by_itself(tmp_path):
    app, client, tok, sid = app_with(tmp_path, rows=[row("T1", rest=41)])
    conn = unsaved(app)
    result = tochki_feed.refresh(conn, app.config["SECRET_BOX"], app.config["UNLOAD_PATH"], datetime(2026, 10, 21, 9, 0, 0))
    assert result["status"] == "off" and "пароль" in result["message"]                        # not «поставщик не выбран»: the row is there
    only = client.get("/catalog?sup=%d" % sid).get_data(as_text=True)
    assert "Ikon Tyres Test Model 205/55 R16 94T" in only                                       # its positions are in the catalog, view only


def test_the_feeds_are_byte_for_byte_the_golden_ones_even_with_the_switch_on(tmp_path):
    """The worst case: nothing saved, the switch «в фид» stored on. The row chosen by itself loads and shows 4tochki, never publishes it."""
    golden_case.SNAPSHOT["fetched_at"] = datetime.utcnow().isoformat(timespec="seconds")      # fresh: it would be used if it leaked
    try:
        args = golden_case.prepare(tmp_path, feed_switch=True)
    finally:
        golden_case.SNAPSHOT["fetched_at"] = "2026-10-14T09:30:00"
    conn = store.connect(tmp_path / "t.db")
    connections.set_config(conn, "tochki", 0, {"login": "api-user", "supplier_id": None, "feed": True}, NOW, "owner")
    conn.commit()
    assert tochki_feed.config(conn)["auto"] and not tochki_feed.config(conn)["feed"]
    conn.close()
    assert main(["run-cycle"] + args) == 0
    conn = store.connect(tmp_path / "t.db")
    golden = json.loads(golden_case.GOLDEN.read_text(encoding="utf-8"))
    for account in connections.list_accounts(conn):
        token = account["feed_token"]
        built = (tmp_path / "feed" / token / "next" / "avito_feed.xml").read_text(encoding="utf-8").replace(token, "TOKEN")
        assert built == golden[account["name"]], account["name"]
