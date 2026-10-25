"""The life of a position (published, waiting, returned, removed) and how full the supplier's catalog is, hour by hour."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from helpers import catalog_xml, commodity_xml, settings_dict
from panel_helpers import login, make_app
from tires_sync import catalog_probe, cli, connections, cycle, publishing, store
from tires_sync.catalog import parse_reference
from tires_sync.secretbox import create_key_file
from tires_sync.settings_store import init_settings
import pytest

pytestmark = pytest.mark.usefixtures("morning_wait")  # these tests describe the wait for the morning refill, which is switched off by default

NOW = "2026-10-14T10:00:00"
TODAY = date(2026, 10, 14)
REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')


def goods(n, start=1):
    return catalog_xml(*[commodity_xml(NNOMMODIF=str(i), NREST="12", SMNFCODE="C%d" % i) for i in range(start, start + n)])


def env(tmp_path, **settings):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(**settings), NOW)
    connections.create_account(conn, "A", NOW, "owner")
    connections.set_live(conn, 1, True, NOW, "owner")
    return conn


def step(conn, tmp_path, xml, moment):
    """One cycle at a Moscow moment, published."""
    results = cycle.run_for_accounts(conn, xml, REF, moment.date(), tmp_path / "feed", now=moment)
    assert results[0].ok, results[0].error
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    return results[0]


def events(conn):
    return [(e, g, r) for g, e, r in conn.execute("SELECT goods_id, event, reason FROM ad_event ORDER BY id")]


def test_events_follow_a_position_from_publication_through_the_wait_to_its_removal(tmp_path):
    conn = env(tmp_path, guard_min=50)
    step(conn, tmp_path, goods(3), datetime(2026, 10, 14, 6, 11))
    assert sorted(e for e in events(conn)) == [("published", "1", ""), ("published", "2", ""), ("published", "3", "")]
    names = [r[0] for r in conn.execute("SELECT name FROM ad_event")]
    assert all("Ikon" in n or "205/55" in n or n for n in names)
    step(conn, tmp_path, goods(2, start=2), datetime(2026, 10, 14, 12, 11))                                  # position 1 left at noon
    assert events(conn)[3:] == [("waiting", "1", "cart_wait:absent_from_supplier")]
    step(conn, tmp_path, goods(2, start=2), datetime(2026, 10, 14, 18, 11))                                  # still waiting: nothing new is written
    step(conn, tmp_path, goods(2, start=2), datetime(2026, 10, 15, 0, 11))
    assert len(events(conn)) == 4
    step(conn, tmp_path, goods(2, start=2), datetime(2026, 10, 15, 6, 45))                                   # the morning: still gone
    assert events(conn)[4:] == [("removed", "1", "absent_from_supplier")]
    assert "1" not in store.load_published(conn, 1)


def test_a_position_that_comes_back_is_logged_and_its_wait_is_forgotten_so_the_next_absence_waits_again(tmp_path):
    conn = env(tmp_path, guard_min=50)
    step(conn, tmp_path, goods(3), datetime(2026, 10, 14, 6, 11))
    step(conn, tmp_path, goods(2, start=2), datetime(2026, 10, 14, 12, 11))
    assert store.load_published(conn, 1)["1"].first_hold is not None
    step(conn, tmp_path, goods(3), datetime(2026, 10, 15, 6, 45))                                              # back after the refill
    assert events(conn)[-1] == ("returned", "1", "")
    assert store.load_published(conn, 1)["1"].first_hold is None                                              # the old wait is not carried over
    step(conn, tmp_path, goods(2, start=2), datetime(2026, 10, 15, 12, 11))                                    # gone again at noon
    assert events(conn)[-1] == ("waiting", "1", "cart_wait:absent_from_supplier")
    assert "1" in store.load_published(conn, 1)                                                                # it waits again instead of going at once


def test_a_guard_hold_is_logged_with_its_own_reason(tmp_path):
    conn = env(tmp_path, guard_min=2, guard_fraction=0.0)
    step(conn, tmp_path, goods(6), datetime(2026, 10, 14, 6, 11))
    step(conn, tmp_path, goods(2), datetime(2026, 10, 14, 12, 11))                                             # four leave: they wait
    assert [e for e in events(conn) if e[0] == "waiting"] and all(r.startswith("cart_wait") for e, g, r in events(conn) if e == "waiting")
    step(conn, tmp_path, goods(2), datetime(2026, 10, 15, 6, 45))                                              # the morning: removal would be mass, the guard holds
    held = [r for e, g, r in events(conn) if e == "removed"]
    assert held == []                                                                                          # nothing removed behind the owner's back
    assert len(store.load_published(conn, 1)) == 6


def test_the_event_log_is_written_in_the_same_transaction_as_the_state(tmp_path):
    conn = env(tmp_path, guard_min=50)
    cycle.run_for_accounts(conn, goods(2), REF, TODAY, tmp_path / "feed", now=datetime(2026, 10, 14, 6, 11))
    assert conn.execute("SELECT COUNT(*) FROM ad_event").fetchone()[0] == 0                                    # planned, not published: no events yet
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    assert conn.execute("SELECT COUNT(*) FROM ad_event").fetchone()[0] == 2


# ---- how full the catalog is

def test_a_probe_counts_our_positions_in_the_unload(tmp_path):
    conn = env(tmp_path, guard_min=50)
    step(conn, tmp_path, goods(5), datetime(2026, 10, 14, 6, 11))
    from tires_sync.webmim import parse_unload
    parsed = parse_unload(catalog_xml(commodity_xml(NNOMMODIF="1", NREST="12"), commodity_xml(NNOMMODIF="2", NREST="2"),
                                      commodity_xml(NNOMMODIF="9", NREST="12")), settings_dict()["territory_rn"])
    row = catalog_probe.record(conn, parsed, 4, "2026-10-15T00:40:00", "probe")
    assert row == {"items": 3, "published": 5, "present": 1, "absent": 3, "low": 1, "waiting": 0}
    assert catalog_probe.list_probes(conn, "2026-10-15T00:00:00")[0]["present"] == 1


def test_the_summary_names_the_first_probe_after_midnight_with_more_positions_in_place():
    def p(at, present, published=10):
        return {"at": at, "source": "probe", "items": 1800, "published": published, "present": present, "absent": published - present, "low": 0, "waiting": 0}
    probes = [p("2026-10-14T12:11:00", 7), p("2026-10-14T18:11:00", 4), p("2026-10-15T00:11:00", 4), p("2026-10-15T00:40:00", 4),
              p("2026-10-15T03:00:00", 9), p("2026-10-15T06:11:00", 10),
              p("2026-10-15T18:11:00", 6), p("2026-10-16T00:40:00", 6), p("2026-10-16T06:11:00", 6)]
    summary = catalog_probe.refill_summary(probes)
    assert summary[0] == {"day": "2026-10-15", "before": 4, "before_at": "2026-10-14T18:11:00", "first_at": "2026-10-15T03:00:00", "after": 9, "published": 10}
    assert summary[1]["day"] == "2026-10-16" and summary[1]["first_at"] == "" and summary[1]["after"] is None       # nothing came back that night


def test_the_night_probe_command_downloads_counts_and_leaves_no_file(tmp_path, monkeypatch, capsys):
    conn = env(tmp_path, guard_min=50)
    step(conn, tmp_path, goods(4), datetime(2026, 10, 14, 6, 11))
    conn.close()
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    work = tmp_path / "probe-unload.xml"

    def fake_fetch(conn_, box, dest, now, http=None, supplier_id=1):
        dest.write_bytes(goods(2))
        dest.with_name(dest.name + ".meta.json").write_text("{}")
        return {"items": 2}

    monkeypatch.setattr(cli, "fetch_for_connection", fake_fetch)
    monkeypatch.setattr(cli, "moscow_now", lambda: datetime(2026, 10, 15, 0, 40))
    assert cli.main(["catalog-probe", "--db", str(tmp_path / "t.db"), "--key-file", str(key_file), "--work-file", str(work)]) == 0
    assert "на месте 2" in capsys.readouterr().out
    assert not work.exists() and not (tmp_path / "probe-unload.xml.meta.json").exists()
    conn = store.connect(tmp_path / "t.db")
    assert conn.execute("SELECT at, source, present, absent FROM catalog_probe").fetchall() == [("2026-10-15T00:40:00", "probe", 2, 2)]
    assert conn.execute("SELECT COUNT(*) FROM published_state").fetchone()[0] == 4                              # a probe publishes and removes nothing


def test_a_cycle_leaves_a_probe_too(tmp_path, monkeypatch):
    conn = env(tmp_path, guard_min=50)
    conn.close()
    (tmp_path / "unload.xml").write_bytes(goods(3))
    (tmp_path / "reference.txt").write_text('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>', encoding="utf-8")
    monkeypatch.setattr(cli, "moscow_now", lambda: datetime(2026, 10, 15, 6, 11))
    assert cli.main(["run-cycle", "--db", str(tmp_path / "t.db"), "--unload", str(tmp_path / "unload.xml"),
                     "--reference", str(tmp_path / "reference.txt"), "--feed-root", str(tmp_path / "feed")]) == 0
    conn = store.connect(tmp_path / "t.db")
    assert conn.execute("SELECT at, source, items FROM catalog_probe").fetchall() == [("2026-10-15T06:11:00", "cycle", 3)]


# ---- the pages

def page_client(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    return app, client, store.connect(app.config["DB_PATH"])


def test_the_history_has_three_sections_and_an_empty_state_for_each(tmp_path):
    app, client, conn = page_client(tmp_path)
    for url, text in (("/history", "Журнал и версии"), ("/history/ads", "Событий пока нет"), ("/history/catalog", "Проверок пока нет")):
        html = client.get(url).get_data(as_text=True)
        assert text in html and 'href="/history/ads"' in html and 'href="/history/catalog"' in html
    assert app.test_client().get("/history/ads").status_code == 302 and app.test_client().get("/history/catalog").status_code == 302


def test_the_events_page_filters_searches_and_pages(tmp_path):
    app, client, conn = page_client(tmp_path)
    for i in range(120):
        store.record_event(conn, "2026-10-15T06:11:%02d" % (i % 60), 1, "G%d" % i, "Шина %d Pirelli" % i, "published")
    store.record_event(conn, "2026-10-15T07:00:00", 1, "X1", "Шина 100% зимняя_Ice", "waiting", "cart_wait:absent_from_supplier")
    store.record_event(conn, "2026-10-15T07:01:00", 1, "X2", "Шина Kumho", "removed", "absent_from_supplier")
    store.record_event(conn, "2026-10-15T07:02:00", 1, "X3", "Шина Nokian", "waiting", "guard:low_stock")
    conn.commit()
    html = client.get("/history/ads").get_data(as_text=True)
    assert html.count('class="gname"') == 100 and "страница" not in html and 'aria-label="Страницы"' in html
    assert html.index("Kumho") < html.index("Шина 119") and "Ждёт" in html and "Снята" in html
    assert "ждём утреннего наполнения каталога" in html and "Удержана защитой от массового снятия" in html
    only = client.get("/history/ads?f=removed").get_data(as_text=True)
    assert "Kumho" in only and "Nokian" not in only and "Шина 5 " not in only
    assert "Kumho" in client.get("/history/ads?q=kumho").get_data(as_text=True)
    assert client.get("/history/ads?q=100%25").get_data(as_text=True).count('class="gname"') == 1             # % is text, not a wildcard
    assert client.get("/history/ads?q=_").get_data(as_text=True).count('class="gname"') == 1                 # so is _
    assert client.get("/history/ads?f=zzz&pg=abc").status_code == 200
    second = client.get("/history/ads?pg=2").get_data(as_text=True)
    assert second.count('class="gname"') == 23


def test_the_catalog_page_shows_the_probes_and_when_the_refill_was_seen(tmp_path):
    app, client, conn = page_client(tmp_path)          # the panel clock is 2026-10-07 12:00 UTC = 15:00 Moscow
    def p(at, present, source="probe"):
        conn.execute("INSERT INTO catalog_probe (at, source, items, published, present, absent, low, waiting) VALUES (?,?,1800,10,?,?,0,0)",
                     (at, source, present, 10 - present))
    p("2026-10-06T18:11:00", 4, "cycle")
    p("2026-10-07T00:40:00", 4)
    p("2026-10-07T03:00:00", 9)
    p("2026-10-07T06:11:00", 10, "cycle")
    conn.commit()
    html = client.get("/history/catalog").get_data(as_text=True)
    assert "наполнение замечено в 03:00: стало 9 из 10" in html and "Накануне вечером на месте было 4" in html
    assert html.count("<tr>") >= 5 and ">ночная<" in html and ">цикл<" in html


def test_a_position_that_returns_keeps_its_set_of_four(tmp_path):
    conn = env(tmp_path, guard_min=50, pack4_rules=["*|*|*"])
    step(conn, tmp_path, goods(3), datetime(2026, 10, 14, 6, 11))
    assert store.get_snapshot4(conn, "1", 1)
    step(conn, tmp_path, goods(2, start=2), datetime(2026, 10, 14, 12, 11))
    step(conn, tmp_path, goods(3), datetime(2026, 10, 15, 6, 11))
    assert events(conn)[-1] == ("returned", "1", "")
    assert store.get_snapshot4(conn, "1", 1) and store.load_published(conn, 1)["1"].first_hold is None


def test_search_ignores_the_case_of_cyrillic_names(tmp_path):
    app, client, conn = page_client(tmp_path)
    store.record_event(conn, "2026-10-15T07:00:00", 1, "A1", "Зимняя шина Ikon", "published")
    conn.commit()
    assert "Зимняя шина" in client.get("/history/ads?q=зимняя").get_data(as_text=True)
    assert "Зимняя шина" in client.get("/history/ads?q=IKON").get_data(as_text=True)
    assert "Зимняя шина" not in client.get("/history/ads?q=летняя").get_data(as_text=True)


def test_a_position_waiting_for_the_morning_does_not_keep_its_set_of_four(tmp_path):
    conn = env(tmp_path, guard_min=50, pack4_rules=["*|*|*"])
    step(conn, tmp_path, goods(3), datetime(2026, 10, 14, 6, 11))
    feed = lambda: next((tmp_path / "feed").iterdir()) / "avito_feed.xml"
    assert "<Id>1-x4</Id>" in feed().read_text(encoding="utf-8")
    step(conn, tmp_path, goods(2, start=2), datetime(2026, 10, 14, 12, 11))                                    # position 1 left the unload
    text = feed().read_text(encoding="utf-8")
    assert "<Id>1</Id>" in text and "<Id>1-x4</Id>" not in text                                                 # the single ad waits, the set is gone
    assert "<Id>2-x4</Id>" in text
    conn.execute("UPDATE published_state SET first_hold = NULL")
    conn.commit()
    step(conn, tmp_path, goods(3), datetime(2026, 10, 15, 6, 11))
    assert "<Id>1-x4</Id>" in feed().read_text(encoding="utf-8")                                                # back: the set returns


def test_the_draft_review_counts_the_waiting_positions_as_staying(tmp_path):
    from tires_sync.panel import service
    from tires_sync.settings_store import create_draft
    from tires_sync.webmim import parse_unload
    conn = env(tmp_path, guard_min=50, min_stock=4)
    step(conn, tmp_path, goods(4), datetime(2026, 10, 14, 6, 11))
    four = catalog_xml(*[commodity_xml(NNOMMODIF=str(i), NREST="4", SMNFCODE="C%d" % i) for i in range(1, 5)])      # the unload now has 4 of each
    parsed = parse_unload(four, settings_dict()["territory_rn"])
    draft = create_draft(conn, {"min_stock": 5}, NOW)                                                              # the draft raises the threshold to 5
    at_once = service.draft_review(conn, draft, parsed, REF, date(2026, 10, 14))
    waiting = service.draft_review(conn, draft, parsed, REF, date(2026, 10, 14), datetime(2026, 10, 14, 15, 0))
    assert at_once["guard"]["gone"] == 4                                  # without the clock the four would go at once
    assert waiting["guard"]["gone"] == 0 and waiting["guard"]["over"] is False        # in fact they wait for the morning


def test_a_burst_of_positions_leaving_at_once_raises_an_alarm_even_though_nothing_is_removed(tmp_path, monkeypatch):
    from http_fakes import ScriptedHttp
    from tires_sync.secretbox import load_box
    conn = env(tmp_path, guard_min=2, guard_fraction=0.0)
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    box = load_box(key_file)
    connections.set_config(conn, "telegram", 0, {"chat_id": "42"}, NOW, "owner")
    connections.set_secret(conn, box, "telegram", 0, "bot_token", "123456:ABCdef-SECRET", NOW, "owner")
    step(conn, tmp_path, goods(8), datetime(2026, 10, 14, 6, 11))
    conn.close()
    (tmp_path / "unload.xml").write_bytes(goods(2))
    (tmp_path / "reference.txt").write_text('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>', encoding="utf-8")
    http = ScriptedHttp((200, {"ok": True, "result": {}}), (200, {"ok": True, "result": {}}))
    monkeypatch.setattr(cli, "_http", lambda: http)
    monkeypatch.setattr(cli, "moscow_now", lambda: datetime(2026, 10, 14, 12, 11))
    code = cli.main(["run-cycle", "--db", str(tmp_path / "t.db"), "--unload", str(tmp_path / "unload.xml"), "--reference", str(tmp_path / "reference.txt"),
                     "--feed-root", str(tmp_path / "feed"), "--key-file", str(key_file)])
    assert code == 0
    import json
    texts = [json.loads(c[3])["text"] for c in http.calls]
    assert any("ждут утра" in t and "6 позиций" in t for t in texts)
