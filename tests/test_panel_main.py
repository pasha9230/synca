from datetime import date

from tires_sync import store
from tires_sync.catalog import parse_reference
from tires_sync.settings_store import active_settings
from tires_sync.sync import run_cycle
from panel_helpers import REF_TEXT, UNLOAD, login, make_app, post


def test_dashboard_shows_pending_pairs_and_no_run_yet(tmp_path):
    client = make_app(tmp_path).test_client()
    login(client)
    page = client.get("/").get_data(as_text=True)
    assert "1 модель (1 позиция)" in page
    assert "Запусков ещё не было" in page


def test_dashboard_shows_last_run_counts(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    run_cycle(UNLOAD, active_settings(conn), parse_reference(REF_TEXT), conn, date(2026, 10, 7), out_dir=tmp_path)
    conn.close()
    client = app.test_client()
    login(client)
    assert '<b class="text-ok">1</b><span>к публикации</span>' in client.get("/").get_data(as_text=True)


def test_missing_data_files_render_a_no_data_message_not_an_error(tmp_path):
    client = make_app(tmp_path, with_data=False).test_client()
    login(client)
    assert "Нет данных от поставщика" in client.get("/").get_data(as_text=True)
    assert client.get("/new").status_code == 200


def test_corrupted_unload_does_not_crash(tmp_path):
    app = make_app(tmp_path)
    (tmp_path / "unload.xml").write_text("<not-xml", encoding="utf-8")
    client = app.test_client()
    login(client)
    assert client.get("/").status_code == 200


def test_uninitialized_settings_explain_what_to_do(tmp_path):
    app = make_app(tmp_path)
    import sqlite3
    conn = sqlite3.connect(app.config["DB_PATH"])
    conn.execute("DELETE FROM settings_active")
    conn.commit()
    conn.close()
    client = app.test_client()
    login(client)
    r = client.get("/")
    assert r.status_code == 503 and "settings init" in r.get_data(as_text=True)


def test_new_positions_page_lists_goods_of_the_last_run(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    ref = parse_reference(REF_TEXT)
    run_cycle(UNLOAD, active_settings(conn), ref, conn, date(2026, 10, 7), out_dir=tmp_path)
    conn.close()
    client = app.test_client()
    login(client)
    page = client.get("/new").get_data(as_text=True)
    assert "первый запуск" in page.lower()


def test_unreadable_data_files_do_not_cause_server_errors(tmp_path):
    import shutil
    app = make_app(tmp_path)
    (tmp_path / "reference.txt").write_bytes(b"\xff\xfe\x00bad-encoding")
    shutil.rmtree(tmp_path / "unload.xml", ignore_errors=True)
    (tmp_path / "unload.xml").unlink()
    (tmp_path / "unload.xml").mkdir()
    client = app.test_client()
    login(client)
    for url in ("/", "/mappings", "/catalog", "/new"):
        assert client.get(url).status_code == 200, url


def test_new_positions_page_accumulates_until_marked_and_can_look_back_by_days(tmp_path):
    from helpers import make_offer
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    r1 = store.save_run(conn, "2026-10-01T10:00:00", True, "a", "{}")
    store.record_seen(conn, r1, [make_offer(goods_id="1")])
    r2 = store.save_run(conn, "2026-10-05T10:00:00", True, "b", "{}")
    store.record_seen(conn, r2, [make_offer(goods_id="2", brand_key="PIRELLI", name_orig="OLD ONE")])
    r3 = store.save_run(conn, "2026-10-06T10:00:00", True, "c", "{}")
    store.record_seen(conn, r3, [make_offer(goods_id="3", name_orig="NEW ONE")])
    conn.close()
    client = app.test_client()
    tok = login(client)
    page = client.get("/new").get_data(as_text=True)
    assert "OLD ONE" in page and "NEW ONE" in page and "Новые бренды" in page
    assert "Отметить просмотренными" in page
    post(client, "/new/seen", tok)
    after = client.get("/new").get_data(as_text=True)
    assert "OLD ONE" not in after and "NEW ONE" not in after and "новых позиций нет" in after.lower()
    assert "NEW ONE" in client.get("/new?days=3650").get_data(as_text=True)


def test_dashboard_leads_with_what_needs_attention_and_links_to_where_it_is_solved(tmp_path):
    from tires_sync import staging
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    html = client.get("/").get_data(as_text=True)
    assert html.index("Требует внимания") < html.index("Ждут сопоставления")
    assert "Модели ждут сопоставления" in html and 'href="/mappings">Сопоставить<' in html
    assert "Есть неподтверждённые изменения" not in html
    staging.set_override(store.connect(app.config["DB_PATH"]), "100", "off", "2026-10-07T10:00:00")
    html = client.get("/").get_data(as_text=True)
    assert "Есть неподтверждённые изменения" in html and 'href="/catalog/review">Посмотреть и применить<' in html


def test_dashboard_shows_ads_in_the_feed_per_account(tmp_path):
    from tires_sync import connections
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    connections.create_account(conn, "Основной", "2026-10-07T10:00:00", "owner")
    client = app.test_client()
    login(client)
    assert "Фид Авито · Основной" in client.get("/").get_data(as_text=True)                       # one tile per feed (2026-10-21)


def test_dashboard_says_all_is_fine_when_nothing_needs_attention(tmp_path, monkeypatch):
    from tires_sync.panel import service
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    real = service.dashboard_data
    monkeypatch.setattr(service, "dashboard_data", lambda *a, **k: dict(real(*a, **k), attention=[]))
    html = client.get("/").get_data(as_text=True)
    assert "Всё в порядке" in html and "можно не заходить" in html


def test_one_unmatched_model_is_one_item_not_three(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    html = client.get("/").get_data(as_text=True)
    attention = html.split("Требует внимания")[1].split('<div class="tiles">')[0]
    assert attention.count("Модели ждут сопоставления") == 1 and "Позиции с проблемами" not in attention
    assert attention.count("Подготовка к публикации") <= 1 and 'href="#readiness"' in attention


def test_a_publication_switched_off_on_our_side_is_not_called_a_failure(tmp_path):
    import json
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    store.set_state(conn, "last_publish", json.dumps({"at": "2026-10-08T10:00:00", "lines": ["Не публикуется: A, публикация выключена"]}))
    client = app.test_client()
    login(client)
    assert "Последняя публикация прошла не полностью" not in client.get("/").get_data(as_text=True)
    assert "Пропущено" in client.get("/history").get_data(as_text=True)                              # the last publication is on «История» (2026-10-21)


def test_new_positions_show_the_latin_name_of_the_goods_not_the_suppliers_russian_spelling(tmp_path):
    from helpers import make_offer
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    r1 = store.save_run(conn, "2026-10-01T10:00:00", True, "a", "{}")
    store.record_seen(conn, r1, [make_offer(goods_id="1")])
    r2 = store.save_run(conn, "2026-10-05T10:00:00", True, "b", "{}")
    store.record_seen(conn, r2, [make_offer(goods_id="200", name="Лауфенн 195/65 R15 G Fit", name_orig="LAUFENN 195/65 R15 G Fit")])
    conn.execute("INSERT INTO seen_goods VALUES (?,?,?,?,?)", ("100", "IKON TYRES", "Test Model", "Икон Тайрс 205/55/16 Тест", r2))   # saved before the fix
    conn.commit()
    conn.close()
    client = app.test_client()
    login(client)
    page = client.get("/new").get_data(as_text=True)
    assert "LAUFENN 195/65 R15 G Fit" in page and "Лауфенн" not in page                  # new rows keep the Latin name
    assert "Ikon  205/55/16  T 94 Test Model" in page and "Икон Тайрс" not in page          # an old row is shown with the catalog's Latin name


def _feed_files(app, public_at=None, draft_at=None):
    import os
    from datetime import datetime as _dt
    from tires_sync import connections
    conn = store.connect(app.config["DB_PATH"])
    account = connections.create_account(conn, "Основной", "2026-10-07T10:00:00", "owner")
    token = connections.get_account(conn, account)["feed_token"]
    root = __import__("pathlib").Path(app.config["FEED_ROOT"]) / token
    (root / "next").mkdir(parents=True, exist_ok=True)
    for path, at in ((root / "avito_feed.xml", public_at), (root / "next" / "avito_feed.xml", draft_at)):
        if at:
            path.write_text("<Ads/>", encoding="utf-8")
            stamp = (at - _dt(1970, 1, 1)).total_seconds()
            os.utime(path, (stamp, stamp))
    return conn


def test_home_shows_when_the_feed_avito_reads_was_updated_and_warns_if_publication_is_off(tmp_path):
    from datetime import datetime as _dt
    app = make_app(tmp_path, FEED_ROOT=str(tmp_path / "feed"))                  # the test clock says 2026-10-07 12:00 UTC
    conn = _feed_files(app, public_at=_dt(2026, 10, 5, 12, 17, 47), draft_at=_dt(2026, 10, 7, 11, 18, 23))
    client = app.test_client()
    login(client)
    html = client.get("/").get_data(as_text=True)
    assert "Фид Авито · Основной" in html and "05.10 15:17" in html and "07.10 14:18" in html     # Moscow time = UTC + 3
    assert "Публикация фида выключена" in html and "Авито читает фид" in html                     # the account is not live by default
    from tires_sync import connections
    connections.set_live(conn, connections.list_accounts(conn)[0]["id"], True, "2026-10-07T12:00:00", "owner")
    conn.commit()
    html = client.get("/").get_data(as_text=True)
    assert "Публикация фида выключена" not in html
    assert "Фид давно не обновлялся" in html                                                      # the public file is two days old


def test_home_says_nothing_about_an_old_feed_when_it_is_fresh_and_live_and_when_there_is_no_public_file_it_says_so(tmp_path):
    from datetime import datetime as _dt
    from tires_sync import connections
    app = make_app(tmp_path, FEED_ROOT=str(tmp_path / "feed"))
    conn = _feed_files(app, public_at=_dt(2026, 10, 7, 9, 0, 0), draft_at=_dt(2026, 10, 7, 9, 0, 0))
    connections.set_live(conn, connections.list_accounts(conn)[0]["id"], True, "2026-10-07T12:00:00", "owner")
    conn.commit()
    client = app.test_client()
    login(client)
    html = client.get("/").get_data(as_text=True)
    assert "Фид давно не обновлялся" not in html and "Публикация фида выключена" not in html and "07.10 12:00" in html
    (tmp_path / "feed").joinpath(connections.list_accounts(conn)[0]["feed_token"], "avito_feed.xml").unlink()
    html = client.get("/").get_data(as_text=True)
    assert "ещё не публиковался" in html
