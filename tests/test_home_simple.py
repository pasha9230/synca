"""The simpler home page (owner's word 2026-10-21): no catalog button and no settings version tile, one tile per feed, readiness only
when a step is not ready, the problem count the catalog filter shows, 4tochki out of readiness while its switch is off, and the last
publication with the latest changes on the history page."""
from __future__ import annotations

import json
import re
from datetime import datetime

from helpers import catalog_xml, commodity_xml
from panel_helpers import login, make_app
from tires_sync import connections, store
from tires_sync.panel import service
from tires_sync.panel.data import load_merged, load_reference
from tires_sync.settings_store import active_settings

NOW = "2026-10-07T10:00:00"


def home(client):
    return client.get("/").get_data(as_text=True)


def attention_of(html):
    return html.split("Требует внимания")[1].split('<div class="tiles">')[0]


def test_the_home_header_keeps_the_feed_button_and_has_no_catalog_button(tmp_path):
    client = make_app(tmp_path).test_client()
    login(client)
    head = home(client).split('<header class="page-head">')[1].split("</header>")[0]
    assert 'href="/feed/update"' in head and "Обновить фид" in head
    assert 'href="/catalog"' not in head and "Каталог" not in head


def test_there_is_no_settings_version_tile(tmp_path):
    client = make_app(tmp_path).test_client()
    login(client)
    html = home(client)
    assert "Версия настроек" not in html and "Активная версия настроек" not in html


def _catalog_problems(client):
    return set(re.findall(r'name="g" value="([^"]+)"', client.get("/catalog?st=problem").get_data(as_text=True)))


def test_the_problem_count_is_what_the_catalog_problem_filter_shows(tmp_path):
    """Rows of the unload that never became a position (a size that is not a number, a duplicate Id) are counted by the evaluation but
    the catalog cannot show them: the home page counted them, and «Разобрать» opened an empty list."""
    app = make_app(tmp_path)
    (tmp_path / "unload.xml").write_bytes(catalog_xml(
        commodity_xml(NNOMMODIF="100"),
        commodity_xml(NNOMMODIF="104", NPRICE_DELAYED="0"),               # a real position with a problem: no price
        commodity_xml(NNOMMODIF="105", SDIAMETR="abc"),                   # not a position: the size is not a number
        commodity_xml(NNOMMODIF="106"), commodity_xml(NNOMMODIF="106"),   # not a position: the same Id twice
    ))
    client = app.test_client()
    login(client)
    shown = _catalog_problems(client)
    assert shown == {"104"}
    attention = attention_of(home(client))
    assert "Позиции с проблемами" in attention and "1: ошибка цены" in attention


def test_no_problem_item_when_only_rows_that_are_not_positions_have_problems(tmp_path):
    app = make_app(tmp_path)
    (tmp_path / "unload.xml").write_bytes(catalog_xml(commodity_xml(NNOMMODIF="100"), commodity_xml(NNOMMODIF="105", SDIAMETR="abc")))
    client = app.test_client()
    login(client)
    assert _catalog_problems(client) == set()
    assert "Позиции с проблемами" not in attention_of(home(client))


def test_the_problem_count_follows_the_catalog_view_of_unconfirmed_changes(tmp_path):
    """The catalog shows the settings with the changes not yet confirmed: the home count is the same list."""
    from tires_sync import staging
    app = make_app(tmp_path)
    (tmp_path / "unload.xml").write_bytes(catalog_xml(commodity_xml(NNOMMODIF="100"), commodity_xml(NNOMMODIF="104", NPRICE_DELAYED="0")))
    conn = store.connect(app.config["DB_PATH"])
    staging.set_override(conn, "104", "off", NOW)
    conn.close()
    client = app.test_client()
    login(client)
    assert _catalog_problems(client) == set()
    assert "Позиции с проблемами" not in attention_of(home(client))


def _tochki(app, feed):
    conn = store.connect(app.config["DB_PATH"])
    sid = connections.create_supplier(conn, "4Точки", NOW, "o")
    connections.set_config(conn, "tochki", 0, {"login": "api-user", "supplier_id": sid, "feed": feed}, NOW, "o")
    conn.commit()
    return conn


def _snapshot(tmp_path, fetched_at, *rows):
    (tmp_path / "tochki-snapshot.json").write_text(
        json.dumps({"version": 1, "fetched_at": fetched_at, "warehouse_id": 7, "items": list(rows)}), encoding="utf-8")


def _readiness(app, conn):
    settings = active_settings(conn)
    parsed = load_merged(conn, settings, app.config["UNLOAD_PATH"])
    return service.readiness(conn, app.config["UNLOAD_PATH"], parsed, load_reference(app.config["REFERENCE_PATH"]), settings,
                             now=datetime(2026, 10, 7, 12, 0, 0))


def test_4tochki_with_its_switch_off_is_not_a_readiness_step(tmp_path):
    app = make_app(tmp_path)
    conn = _tochki(app, feed=False)
    assert not [s for s in _readiness(app, conn) if "4Точки" in s["title"]]
    client = app.test_client()
    login(client)
    html = home(client)
    assert "4Точки" not in html and "нет файла выгрузки" not in html


def test_4tochki_with_its_switch_on_is_checked_by_its_own_load_not_by_a_webmim_file(tmp_path):
    app = make_app(tmp_path)
    conn = _tochki(app, feed=True)
    [step] = [s for s in _readiness(app, conn) if "4Точки" in s["title"]]
    assert step["level"] == "bad" and "нет загруженных позиций 4 точек" in step["detail"] and "файла выгрузки" not in step["detail"]
    from test_tochki_panel import row
    _snapshot(tmp_path, "2026-10-07T09:00:00", row("A1"), row("A2", width="215"))
    [step] = [s for s in _readiness(app, conn) if "4Точки" in s["title"]]
    assert step["level"] == "ok" and "2 позиции" in step["detail"] and "07.10.2026 12:00:00" in step["detail"]                    # UTC shown in Moscow time (run6c)
    _snapshot(tmp_path, "2026-10-01T09:00:00", row("A1"))
    [step] = [s for s in _readiness(app, conn) if "4Точки" in s["title"]]
    assert step["level"] == "warn" and "устарела" in step["detail"]
    _snapshot(tmp_path, "2026-10-07T12:00:00+03:00", row("A1"))                     # a time with a zone: no error, compared in UTC
    [step] = [s for s in _readiness(app, conn) if "4Точки" in s["title"]]
    assert step["level"] == "ok"


def test_readiness_shows_only_steps_that_are_not_ready_and_names_them_in_attention(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    html = home(client)
    block = html.split('id="readiness"')[1].split("</section>")[0]
    assert "Включённые сочетания" not in block and "Готово" not in block and "готово" not in block      # the one ready step is not listed
    assert "Подключение к WebMIM" in block and "Справочник Авито" in block
    attention = attention_of(html)
    assert "проверки ниже" not in attention
    assert "Не готово: Подключение к WebMIM" in attention and "Справочник Авито" in attention


def test_no_readiness_block_when_every_step_is_ready(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    monkeypatch.setattr(service, "readiness", lambda *a, **k: [service._step("ok", "Подключение к WebMIM", "ключ получен", "/connections")])
    html = home(client)
    assert 'id="readiness"' not in html and "Готовность к публикации" not in html and "Подготовка к публикации" not in html


def _feed(app, public_at=None, draft_at=None, live=False, avito_id=None):
    import os
    conn = store.connect(app.config["DB_PATH"])
    account = connections.create_account(conn, "Основной", NOW, "owner")
    if avito_id:
        connections.set_avito_identity(conn, account, avito_id, "Шины")
    connections.set_live(conn, account, live, NOW, "owner")
    for gid in ("100", "101"):
        conn.execute("INSERT INTO published_state (account_id, goods_id, price, base_price, point, snapshot) VALUES (?, ?, 5000, 5000, 'P1', '{}')",
                     (account, gid))
    conn.commit()
    root = __import__("pathlib").Path(app.config["FEED_ROOT"]) / connections.get_account(conn, account)["feed_token"]
    (root / "next").mkdir(parents=True, exist_ok=True)
    for path, at in ((root / "avito_feed.xml", public_at), (root / "next" / "avito_feed.xml", draft_at)):
        if at:
            path.write_text("<Ads/>", encoding="utf-8")
            stamp = (at - datetime(1970, 1, 1)).total_seconds()
            os.utime(path, (stamp, stamp))
    return conn


def _tiles(html):
    return html.split('<div class="tiles">')[1].split("</div>")[0]


def test_one_tile_per_feed_with_the_cabinet_id_the_account_name_the_time_and_the_ads(tmp_path):
    app = make_app(tmp_path, FEED_ROOT=str(tmp_path / "feed"))               # the test clock says 2026-10-07 12:00 UTC
    _feed(app, public_at=datetime(2026, 10, 7, 11, 0, 0), draft_at=datetime(2026, 10, 7, 11, 0, 0), live=True, avito_id=80000001)
    client = app.test_client()
    login(client)
    tiles = _tiles(home(client))
    assert "Фид Авито · ID 80000001 · Основной" in tiles
    assert "Фид для Авито" not in tiles and "В фиде ·" not in tiles
    assert tiles.count("Фид Авито ·") == 1 and "07.10 14:00" in tiles and ">2<" in tiles and "Ждут сопоставления" in tiles
    assert "tile--warn" not in tiles.split('<a class="tile tile--warn" href="/mappings">')[0] and "свежий собран" not in tiles


def test_the_feed_tile_keeps_its_warnings(tmp_path):
    app = make_app(tmp_path, FEED_ROOT=str(tmp_path / "feed"))
    conn = _feed(app, public_at=datetime(2026, 10, 6, 12, 0, 0), draft_at=datetime(2026, 10, 7, 11, 18, 0))
    client = app.test_client()
    login(client)
    tile = _tiles(home(client)).split('<a class="tile tile--warn" href="/mappings">')[0]
    assert "Фид Авито · Основной" in tile and "tile--warn" in tile
    assert "публикация выключена" in tile and "свежий собран 07.10 14:18" in tile
    connections.set_live(conn, connections.list_accounts(conn)[0]["id"], True, NOW, "owner")
    conn.commit()
    tile = _tiles(home(client)).split('<a class="tile tile--warn" href="/mappings">')[0]
    assert "публикация выключена" not in tile and "tile--warn" in tile                         # the public feed is a day old (over 13 hours)


def test_last_publication_and_the_journal_of_changes_open_the_history_page_and_left_the_home_page(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    store.set_state(conn, "last_publish", json.dumps({"at": "2026-10-08T10:00:00", "lines": ["Опубликовано: Основной, 5 объявлений"]}))
    conn.commit()
    client = app.test_client()
    login(client)
    html = home(client)
    assert "Последняя публикация</h3>" not in html and "Последние изменения" not in html
    page = client.get("/history").get_data(as_text=True)
    assert page.index("Последняя публикация</h3>") < page.index("Журнал действий</h3>") < page.index("Версии настроек</h3>")      # the latest changes = the journal
    assert "Опубликовано: Основной, 5 объявлений" in page and 'href="/publish/result">Подробнее<' in page
    assert 'class="tabs"' in page and page.index('class="tabs"') < page.index("Последняя публикация</h3>")
    assert "Опубликовано: Основной, 5 объявлений" in client.get("/history?f=system").get_data(as_text=True)


def test_history_without_a_publication_has_no_publication_block(tmp_path):
    client = make_app(tmp_path).test_client()
    login(client)
    page = client.get("/history").get_data(as_text=True)
    assert "Последняя публикация</h3>" not in page and "Журнал действий</h3>" in page


def _home_problem_count(client):
    found = re.search(r"Позиции с проблемами</div><div class=\"cl-text\">(\d+):", home(client))
    return int(found.group(1)) if found else 0


def test_the_problem_count_includes_4tochki_positions_the_catalog_shows_for_viewing(tmp_path):
    from test_tochki_panel import row
    from tires_sync.settings_store import confirm_draft, create_draft
    app = make_app(tmp_path)
    conn = _tochki(app, feed=False)
    sid = connections.list_suppliers(conn)[-1]["id"]
    brand = "S%d:IKON TYRES" % sid
    confirm_draft(conn, create_draft(conn, {"enabled": [["IKON TYRES", "winter"], [brand, "winter"]],
                                            "pricing": {"IKON TYRES|winter": {"pct_small": -3.82, "pct_large": -4.28},
                                                        brand + "|winter": {"pct_small": 0, "pct_large": 0}}}, NOW), NOW, "owner")
    conn.commit()
    _snapshot(tmp_path, "2026-10-07T09:00:00", row("A1", price=0), row("A2", width="215"))
    client = app.test_client()
    login(client)
    page = client.get("/catalog?st=problem").get_data(as_text=True)                  # rows shown for viewing carry no Id in the page
    assert "Ikon Tyres Test Model 205/55 R16 94T" in page and "215/55" not in page        # A1 has no price, A2 is fine
    assert _home_problem_count(client) == 1


def test_a_position_of_an_excluded_brand_is_not_counted_even_with_a_problem_of_the_unload(tmp_path):
    """An Id met twice between suppliers stays a position and also gets a problem; with its brand excluded by the owner the catalog
    hides it, and the home page does not count it."""
    from helpers import make_offer
    from tires_sync.panel.routes_main import catalog_scope
    from tires_sync.settings_store import confirm_draft, create_draft
    from tires_sync.webmim import ParseResult
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    confirm_draft(conn, create_draft(conn, {"excluded_brands": ["IKON TYRES"]}, NOW), NOW, "owner")
    conn.commit()
    parsed = ParseResult(offers={"100": make_offer(goods_id="100")}, issues={"100": ["duplicate_id_between_suppliers"]})
    ref = load_reference(app.config["REFERENCE_PATH"])
    with app.test_request_context("/"):
        settings, view = catalog_scope(app, conn, parsed)
    assert service.catalog_problems(conn, active_settings(conn), parsed, ref) == ["100"]          # without the catalog's view it counted
    assert service.catalog_problems(conn, settings, view, ref) == []
