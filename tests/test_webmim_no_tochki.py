"""«Подключения → WebMIM» does not list 4 точки (the owner 2026-10-21: «для них отдельный раздел»). Only the view: the supplier row,
its settings, the catalog, the feed and the loading of 4 точки are the same."""
from __future__ import annotations

import re

from test_tochki_panel import NOW, app_with
from tires_sync import connections, store, tochki_feed


def supplier_tabs(page: str):
    block = re.search(r'<div class="tabs2">(.*?)</div>', page, re.S)
    return re.findall(r'href="/connections/webmim/(\d+)"', block.group(1)) if block else []


def test_the_webmim_section_lists_no_4tochki_and_its_tab_is_unchanged(tmp_path):
    app, client, tok, sid = app_with(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    other = connections.create_supplier(conn, "Второй WebMIM", NOW, "o")
    page = client.get("/connections/webmim").get_data(as_text=True)
    assert supplier_tabs(page) == ["1", str(other)] and "4Точки" not in page
    r = client.get("/connections/webmim/%d" % sid)
    assert r.status_code == 302 and r.headers["Location"].endswith("/connections/tochki")
    tochki = client.get("/connections/tochki").get_data(as_text=True)
    assert "4Точки" in tochki                                                                  # its own tab still chooses its supplier row
    assert [s["id"] for s in connections.list_suppliers(conn)] == [1, sid, other]               # the data is the same
    assert tochki_feed.config(conn)["supplier_id"] == sid


def test_without_4tochki_chosen_every_supplier_is_listed(tmp_path):
    app, client, tok, sid = app_with(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    second = connections.create_supplier(conn, "4 точки (запас)", NOW, "o")        # two rows fit: none is chosen by itself
    connections.set_config(conn, "tochki", 0, {"login": "api-user", "supplier_id": None, "feed": False}, NOW, "o")
    conn.commit()
    assert supplier_tabs(client.get("/connections/webmim").get_data(as_text=True)) == ["1", str(sid), str(second)]
    assert client.get("/connections/webmim/%d" % sid).status_code == 200


def test_the_4tochki_tab_warns_while_no_supplier_row_is_chosen(tmp_path):
    app, client, tok, sid = app_with(tmp_path)
    assert "data-tochki-no-row" not in client.get("/connections/tochki").get_data(as_text=True)
    conn = store.connect(app.config["DB_PATH"])
    connections.create_supplier(conn, "4 точки (запас)", NOW, "o")                    # two rows fit: none is chosen by itself
    connections.set_config(conn, "tochki", 0, {"login": "api-user", "supplier_id": None, "feed": False}, NOW, "o")
    conn.commit()
    page = client.get("/connections/tochki").get_data(as_text=True)
    warn = re.search(r'<div class="flash flash--err"[^>]*data-tochki-no-row[^>]*>.*?</div>', page, re.S)
    assert warn and "не выбрана строка поставщика — каталог и остатки 4 точек не загружаются" in warn.group(0).lower()
