"""What Avito says about our last upload, shown on the home page: «pay the advance» and real errors apart."""
from __future__ import annotations

import json
from datetime import datetime

from cryptography.fernet import Fernet

from helpers import catalog_xml, commodity_xml
from http_fakes import ScriptedHttp
from panel_helpers import login, make_app, post
from tires_sync import avito_report, cli, connections, store
from tires_sync.avito import AvitoClient
from tires_sync.secretbox import SecretBox

PAY = 'Нужно <a href="https://pro.avito.ru/cpa">пополнить аванс</a>. Как только на счёте появятся деньги, объявление станет активно автоматически'
TOKEN = (200, {"access_token": "t", "expires_in": 3600})


def item(ad_id, status, *messages):
    return {"ad_id": ad_id, "avito_status": status, "messages": [{"title": t, "description": d} for t, d in messages]}


def test_summary_groups_by_reason_cleans_the_markup_and_counts_the_silent_ones():
    items = [item("1", "active"), item("2", "active"),
             item("3", "unknown", (PAY, "")), item("4", "unknown", (PAY, "")), item("5", "old", (PAY, "")),
             item("6", "rejected", ("Ошибка параметра: поле Quantity", "Укажите значение")),
             item("7", "old")]                                                                  # closed by us, Avito says nothing
    s = avito_report.summarize(items)
    assert s["counts"] == {"active": 2, "unknown": 2, "old": 2, "rejected": 1} and s["total"] == 7
    assert [g["kind"] for g in s["groups"]] == ["payment", "error"]                              # the money problem first
    pay = s["groups"][0]
    assert pay["count"] == 3 and pay["examples"] == ["3", "4", "5"] and "<a" not in pay["text"] and pay["text"].startswith("Нужно пополнить аванс.")
    assert s["groups"][1]["text"] == "Ошибка параметра: поле Quantity: Укажите значение" and s["groups"][1]["count"] == 1


def test_only_a_few_examples_are_kept():
    s = avito_report.summarize([item(str(i), "unknown", (PAY, "")) for i in range(40)])
    assert s["groups"][0]["count"] == 40 and len(s["groups"][0]["examples"]) == avito_report.EXAMPLES


def test_collect_reads_the_last_upload_and_stores_the_summary(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    http = ScriptedHttp(
        TOKEN, (200, {"upload_id": 9, "status": "success_warning", "started_at": "2026-10-12T11:22:59Z", "stats": {"count": 2}}),   # last_successful
        (200, {"upload_id": 10, "status": "processing"}),                                                                          # current
        (200, {"items": [item("1", "active"), item("2", "unknown", (PAY, ""))], "meta": {"pages": 1}}))
    client = AvitoClient("id", "secret", http=http, now=lambda: 0.0, sleep=lambda s: None)
    summary = avito_report.collect(conn, client, 1, "2026-10-12T15:00:00")
    assert summary["upload_id"] == 9 and summary["running"] == 10 and summary["total"] == 2
    assert avito_report.load(conn, 1)["groups"][0]["count"] == 1
    assert all(c[0] in ("GET", "POST") and "/autoload/v1/upload" not in c[1] for c in http.calls)              # reads only


def test_no_upload_yet_stores_nothing(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    http = ScriptedHttp(TOKEN, (404, {"error": {"code": 404, "message": "none"}}), (404, {"error": {"code": 404, "message": "none"}}))
    client = AvitoClient("id", "secret", http=http, now=lambda: 0.0, sleep=lambda s: None)
    assert avito_report.collect(conn, client, 1, "2026-10-12T15:00:00") is None and avito_report.load(conn, 1) is None


def home(tmp_path, report=None):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    connections.create_account(conn, "Основной", "2026-10-07T10:00:00", "owner")
    if report is not None:
        store.set_state(conn, "avito_report:1", json.dumps(report, ensure_ascii=False))
    return client


def report(groups):
    return {"at": "2026-10-12T15:00:00", "counts": {}, "total": 55, "upload_id": 9, "upload_status": "success_warning", "running": None, "groups": groups}


def test_home_page_asks_to_top_up_the_advance_with_a_link_to_avito(tmp_path):
    html = home(tmp_path, report([{"text": "Нужно пополнить аванс.", "kind": "payment", "count": 22, "examples": ["1", "2"]}])).get("/").get_data(as_text=True)
    assert "Авито не активировал 22 объявления: нужно пополнить аванс" in html and "Нужно сделать" in html
    assert 'href="https://pro.avito.ru/cpa" target="_blank" rel="noopener noreferrer"' in html and "Данные Авито на 12.10 15:00 МСК" in html


def test_home_page_shows_a_real_error_with_examples_and_no_external_link(tmp_path):
    html = home(tmp_path, report([{"text": "Ошибка параметра: поле Quantity", "kind": "error", "count": 2, "examples": ["10", "11", "12", "13"]}])).get("/").get_data(as_text=True)
    assert "Авито не принял 2 объявления" in html and "Ошибка параметра: поле Quantity" in html and "10, 11, 12" in html and "13" not in html.split("Например")[1][:40]


def test_home_page_has_no_such_card_when_all_is_fine_or_nothing_was_collected(tmp_path):
    assert "Авито не" not in home(tmp_path).get("/").get_data(as_text=True)
    (tmp_path / "b").mkdir()
    assert "Авито не" not in home(tmp_path / "b", report([])).get("/").get_data(as_text=True)


def test_a_cycle_collects_the_report_and_survives_avito_being_down(tmp_path, monkeypatch, capsys):
    from helpers import settings_dict
    from tires_sync.secretbox import create_key_file, load_box
    from tires_sync.settings_store import init_settings
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(guard_min=50), "2026-10-14T10:00:00")
    connections.create_account(conn, "A", "2026-10-14T10:00:00", "owner")
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    box = load_box(key_file)
    connections.set_secret(conn, box, "avito", 1, "client_id", "cid", "2026-10-14T10:00:00", "owner")
    connections.set_secret(conn, box, "avito", 1, "client_secret", "csecret", "2026-10-14T10:00:00", "owner")
    connections.set_avito_identity(conn, 1, None, "A", "79000000000")                            # as «Проверить подключение» records it
    conn.close()
    (tmp_path / "unload.xml").write_bytes(catalog_xml(commodity_xml(NNOMMODIF="1", NREST="12")))
    (tmp_path / "reference.txt").write_text('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>', encoding="utf-8")
    args = ["run-cycle", "--db", str(tmp_path / "t.db"), "--unload", str(tmp_path / "unload.xml"), "--reference", str(tmp_path / "reference.txt"),
            "--feed-root", str(tmp_path / "feed"), "--key-file", str(key_file)]
    monkeypatch.setattr(cli, "moscow_now", lambda: datetime(2026, 10, 15, 6, 11))

    def down(*a):
        raise OSError("avito is down")

    monkeypatch.setattr(cli, "_http", lambda: down)
    assert cli.main(args) == 0                                                                                    # the cycle goes on
    assert "отчёт Авито не получен" in capsys.readouterr().err and avito_report.load(store.connect(tmp_path / "t.db"), 1) is None
    http = ScriptedHttp(TOKEN, (200, {"upload_id": 9, "status": "success"}), (404, {"error": {"code": 404, "message": "x"}}),
                        (200, {"items": [item("1", "unknown", (PAY, ""))], "meta": {"pages": 1}}))
    monkeypatch.setattr(cli, "_http", lambda: http)
    assert cli.main(args) == 0
    stored = avito_report.load(store.connect(tmp_path / "t.db"), 1)
    assert stored["at"] == "2026-10-15T06:11:00" and stored["groups"][0]["kind"] == "payment"


def test_several_kinds_of_errors_of_one_account_make_one_card_not_one_per_kind(tmp_path):
    groups = [{"text": "Ошибка: Количество", "kind": "error", "count": 5, "examples": ["10", "11", "12"]},
              {"text": "Ошибка: Модель", "kind": "error", "count": 2, "examples": ["20"]},
              {"text": "Снято с публикации", "kind": "error", "count": 1, "examples": ["30"]},
              {"text": "Ещё что-то", "kind": "error", "count": 1, "examples": ["40"]}]
    html = home(tmp_path, report(groups)).get("/").get_data(as_text=True)
    assert html.count("Авито не принял") == 1 and "Авито не принял 9 объявлений" in html
    assert "5× Ошибка: Количество" in html and "2× Ошибка: Модель" in html and "и ещё 1 вид" in html and "10, 11, 12" in html


def test_the_report_of_a_mirrored_account_that_is_not_in_the_air_yet_is_not_shown(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    mine = connections.create_account(conn, "Основной", "2026-10-07T10:00:00", "owner")
    other = connections.create_account(conn, "Чужой", "2026-10-07T10:00:00", "owner")
    conn.execute("UPDATE account SET feed_mode = 'mirror' WHERE id = ?", (other,))
    conn.commit()
    errors = [{"text": "Чужая ошибка", "kind": "error", "count": 3, "examples": ["1"]}]
    store.set_state(conn, "avito_report:%d" % other, json.dumps(report(errors), ensure_ascii=False))
    assert "Чужая ошибка" not in client.get("/").get_data(as_text=True)                                   # the errors of somebody else's file
    connections.set_live(conn, other, True, "2026-10-13T10:00:00", "o")
    conn.commit()
    assert "Чужая ошибка" in client.get("/").get_data(as_text=True)                                       # in the air with our link: now it is ours
