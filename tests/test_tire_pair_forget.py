"""«Забыть пару» (run «match3»): the owner takes a remembered pair out of the memory — a mark, never a deletion — with the journal and undo;
the command `match-pair-forget`; the page «Пары поставщиков» shows what `match-report` shows and writes nothing; only the owner forgets."""
from __future__ import annotations

import json
import re

import pytest
from werkzeug.security import generate_password_hash

import test_tire_match_feed_unchanged as golden
from panel_helpers import login, post
from test_tire_match_feed_unchanged import UNLOAD, UNLOAD_WITHOUT_WEBMIM, prepare
from tires_sync import connections, store, tire_match, users
from tires_sync.cli import main
from tires_sync.panel import create_app
from tires_sync.panel.routes_pairs import pairs_view

AT = "2026-10-25T09:00:00"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    from tires_sync import cli
    from datetime import datetime
    monkeypatch.setattr(cli, "date", golden.GoldenDay)
    monkeypatch.setattr(cli, "moscow_now", lambda: datetime(2026, 10, 25, 3, 0, 0))


def feeds(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    out = {a["name"]: (tmp_path / "feed" / a["feed_token"] / "next" / "avito_feed.xml").read_text(encoding="utf-8")
           for a in connections.list_accounts(conn)}
    conn.close()
    return out


def published_pair(tmp_path):
    """Cycle 1 with the switch on: the pair 100 | s2-TCH1 is one WebMIM ad, published and remembered."""
    args = prepare(tmp_path, True)
    assert main(["run-cycle"] + args) == 0
    conn = store.connect(tmp_path / "t.db")
    for account in connections.list_accounts(conn):
        store.commit_pending(conn, account["id"], (tmp_path / "feed" / account["feed_token"] / "next" / "avito_feed.xml").read_bytes())
    assert [r[:2] for r in store.load_tire_pairs(conn)] == [("100", "s2-TCH1")]
    conn.close()
    return args


def counts(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    out = {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in ("tire_pair", "tire_pair_forget", "audit")}
    conn.close()
    return out


def test_a_forgotten_pair_is_not_used_by_the_memory_and_undo_brings_it_back(tmp_path):
    args = published_pair(tmp_path)
    conn = store.connect(tmp_path / "t.db")
    assert tire_match.forget_pair(conn, "s2-TCH1", "100", "owner", AT) is True              # the order of the codes does not matter
    assert tire_match.forget_pair(conn, "100", "s2-TCH1", "owner", AT) is False             # forgotten already
    assert store.load_tire_pairs(conn)[0][:2] == ("100", "s2-TCH1")                        # the row stays
    assert tire_match.usable_pairs(conn) == []
    conn.close()
    (tmp_path / "unload.xml").write_bytes(UNLOAD_WITHOUT_WEBMIM)
    assert main(["run-cycle"] + args) == 0
    built = feeds(tmp_path)
    assert "<Id>s2-TCH1</Id>" in built["Основной"] and "<Model>Nordman 7</Model>" in built["Основной"]    # as with no memory at all
    conn = store.connect(tmp_path / "t.db")
    assert tire_match.forget_pair(conn, "100", "s2-TCH1", "owner", AT, undo=True) is True
    assert tire_match.forget_pair(conn, "100", "s2-TCH1", "owner", AT, undo=True) is False
    actions = [r[0] for r in conn.execute("SELECT action FROM audit WHERE action LIKE 'match_pair%' ORDER BY id")]
    assert actions == ["match_pair_forget", "match_pair_unforget"]
    assert json.loads(conn.execute("SELECT detail FROM audit WHERE action = 'match_pair_forget'").fetchone()[0]) == {
        "code_a": "100", "code_b": "s2-TCH1"}
    conn.close()
    assert main(["run-cycle"] + args) == 0
    built = feeds(tmp_path)
    assert "<Id>100</Id>" in built["Основной"] and "<Id>s2-TCH1</Id>" not in built["Основной"]       # the memory again
    assert "<Model>Character Ice 7</Model>" in built["Основной"]
    assert counts(tmp_path)["tire_pair"] == 1 and counts(tmp_path)["tire_pair_forget"] == 2


def test_a_cycle_that_finds_a_forgotten_pair_again_does_not_bring_it_back(tmp_path):
    args = published_pair(tmp_path)
    conn = store.connect(tmp_path / "t.db")
    tire_match.forget_pair(conn, "100", "s2-TCH1", "owner", AT)
    conn.close()
    assert main(["run-cycle"] + args) == 0                                  # both sides here, the switch on: the pair is found again
    conn = store.connect(tmp_path / "t.db")
    assert store.forgotten_tire_pairs(conn) == {("100", "s2-TCH1"): (AT, "owner")} and tire_match.usable_pairs(conn) == []
    conn.close()


def test_a_pair_that_is_not_remembered_cannot_be_forgotten(tmp_path):
    prepare(tmp_path)
    conn = store.connect(tmp_path / "t.db")
    with pytest.raises(ValueError):
        tire_match.forget_pair(conn, "100", "s2-TCH1", "owner", AT)
    assert store.mark_tire_pair(conn, "100", "s2-TCH1", True, AT, "owner") is False
    conn.close()
    assert counts(tmp_path)["tire_pair_forget"] == 0


def test_the_command_forgets_brings_back_and_refuses_an_unknown_pair(tmp_path, capsys):
    published_pair(tmp_path)
    db = str(tmp_path / "t.db")
    capsys.readouterr()
    assert main(["match-pair-forget", "--db", db, "100", "s2-TCH1"]) == 0
    assert "пара забыта" in capsys.readouterr().out
    assert main(["match-pair-forget", "--db", db, "100", "s2-TCH1"]) == 0
    assert "пара уже забыта" in capsys.readouterr().out
    report = ["match-report", "--db", db, "--unload", str(tmp_path / "unload.xml"), "--reference", str(tmp_path / "reference.txt")]
    assert main(report) == 0
    out = capsys.readouterr().out
    assert "сохранённых пар (запомнены циклом при включённой склейке): 1, используется 0 из 1" in out
    assert re.search(r"  100 \| s2-TCH1, найдена \S+ \(забыта \S+ UTC, владелец\)", out)
    assert main(["match-pair-forget", "--db", db, "100", "s2-TCH1", "--undo"]) == 0
    assert "пара возвращена" in capsys.readouterr().out
    assert main(["match-pair-forget", "--db", db, "100", "s2-TCH9"]) == 2
    assert counts(tmp_path)["tire_pair"] == 1 and counts(tmp_path)["tire_pair_forget"] == 2


def page_app(tmp_path):
    return create_app({"TESTING": True, "SECRET_KEY": "test-secret", "PASSWORD_HASH": generate_password_hash("pw", method="pbkdf2:sha256:1000"),
                       "DB_PATH": str(tmp_path / "t.db"), "UNLOAD_PATH": str(tmp_path / "unload.xml"),
                       "REFERENCE_PATH": str(tmp_path / "reference.txt"), "NOW": lambda: __import__("datetime").datetime(2026, 10, 25, 9, 0, 0)})


def signed_in(app, role):
    client = app.test_client()
    if role == "owner":
        login(client)
        return client
    conn = store.connect(app.config["DB_PATH"])
    users.add_user(conn, role + "1", "длинный-пароль-1", AT, role=role, point_id=None)
    with client.session_transaction() as session:
        session.update(user=role + "1", epoch=store.get_session_epoch(conn, role + "1"), csrf="tok")
    conn.close()
    return client


def test_the_page_shows_what_match_report_shows_and_writes_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    args = published_pair(tmp_path)
    (tmp_path / "unload.xml").write_bytes(UNLOAD_WITHOUT_WEBMIM)
    assert main(["run-cycle"] + args) == 0                                  # the WebMIM row left: the memory goes on, the change is noted
    (tmp_path / "unload.xml").write_bytes(UNLOAD)
    capsys.readouterr()
    report = ["match-report", "--db", args[1], "--unload", args[3], "--reference", args[5]]
    assert main(report) == 0
    out = capsys.readouterr().out
    before = counts(tmp_path)
    app = page_app(tmp_path)
    from pathlib import Path
    from tires_sync.catalog import parse_reference
    from tires_sync.settings_store import active_settings
    from tires_sync.suppliers import load_all_parsed
    conn = store.connect(tmp_path / "t.db")
    settings = active_settings(conn)
    parsed, _digest, _notes = load_all_parsed(conn, settings, Path(app.config["UNLOAD_PATH"]))
    view = pairs_view(conn, parsed, parse_reference((tmp_path / "reference.txt").read_text(encoding="utf-8")), settings, app.config["UNLOAD_PATH"])
    conn.close()
    assert [[s["code"] for s in g["sides"]] for g in view["groups"]] == [["100", "s2-TCH1"]]
    apart = int(re.search(r"пар \(групп одной шины у разных поставщиков\): 1, из них модели Авито расходятся: (\d+)", out).group(1))
    assert sum(1 for g in view["groups"] if g["apart"]) == apart, (view["groups"], out)
    assert [(p["a"]["code"], p["b"]["code"], p["forgotten"]) for p in view["stored"]] == [("100", "s2-TCH1", None)]
    assert "сохранённых пар (запомнены циклом при включённой склейке): 1, используется 1 из 1" in out and view["used"] == 1
    assert len(view["ambiguous"]) == int(re.search(r"неоднозначных групп \(не склеиваются\): (\d+)", out).group(1))
    assert len(view["doubtful"]) == int(re.search(r"сомнительных позиций .*?: (\d+)", out).group(1))
    switches = {a["id"]: [(s["ad_id"], s["from"], s["to"]) for s in a["switches"]] for a in view["accounts"]}
    assert switches == {1: [("100", "100", "s2-TCH1")], 2: [("100_a2", "100", "s2-TCH1")]}
    assert "   Id 100: 100 → s2-TCH1, " in out
    assert all(a["waiting"] == [] for a in view["accounts"]) and "ждёт смены поставщика" not in out
    owner = signed_in(app, "owner")
    page = owner.get("/pairs")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Пары поставщиков" in html and "s2-TCH1" in html and 'action="/pairs/forget"' in html and "используется 1 из 1" in html
    assert counts(tmp_path) == before                                       # the page and the report wrote nothing


def test_only_the_owner_forgets_the_others_see(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    published_pair(tmp_path)
    app = page_app(tmp_path)
    for role in ("partner", "chats", "viewer"):
        client = signed_in(app, role)
        page = client.get("/pairs")
        assert page.status_code == 200, role
        assert 'action="/pairs/forget"' not in page.get_data(as_text=True), role
        assert post(client, "/pairs/forget", "tok", a="100", b="s2-TCH1").status_code == 403, role
    assert counts(tmp_path)["tire_pair_forget"] == 0
    owner = signed_in(app, "owner")
    assert owner.post("/pairs/forget", data={"a": "100", "b": "s2-TCH1"}).status_code in (400, 403)        # no CSRF token
    assert counts(tmp_path)["tire_pair_forget"] == 0
    r = post(owner, "/pairs/forget", "tok", a="100", b="s2-TCH1")
    assert r.status_code == 302 and r.headers["Location"].endswith("/pairs")
    html = owner.get("/pairs").get_data(as_text=True)
    assert "забыта" in html and "Вернуть пару" in html and "используется 0 из 1" in html
    journal = owner.get("/history").get_data(as_text=True)
    assert "Пара поставщиков забыта: 100 | s2-TCH1" in journal
    r = post(owner, "/pairs/forget", "tok", a="100", b="s2-TCH1", undo="1")
    assert r.status_code == 302
    conn = store.connect(tmp_path / "t.db")
    assert store.forgotten_tire_pairs(conn) == {} and len(tire_match.usable_pairs(conn)) == 1
    conn.close()
    r = post(owner, "/pairs/forget", "tok", a="100", b="s2-TCH9")
    assert r.status_code == 302 and counts(tmp_path)["tire_pair_forget"] == 2              # an unknown pair: a message, nothing written


def test_the_page_is_a_tab_of_the_mappings_for_who_sees_the_catalog(tmp_path, monkeypatch):
    """The owner 2026-10-25: «Пары поставщиков» lives in «Сопоставления» (a tab), not in the side menu; the address /pairs stays."""
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    prepare(tmp_path)
    app = page_app(tmp_path)
    for role in ("owner", "viewer"):
        client = signed_in(app, role)
        mappings = client.get("/mappings").get_data(as_text=True)
        assert '<a href="/pairs">Пары поставщиков</a>' in mappings and '<a class="is-active" aria-current="page" href="/mappings">Модели</a>' in mappings, role
        pairs = client.get("/pairs").get_data(as_text=True)
        assert '<a class="is-active" aria-current="page" href="/pairs">Пары поставщиков</a>' in pairs, role
        assert re.search(r'class="sb-link is-active" href="/mappings">(?:<svg.*?</svg>)?Сопоставления', pairs, re.S), role
        assert 'class="sb-link" href="/pairs"' not in pairs and 'class="sb-link is-active" href="/pairs"' not in pairs, role
    conn = store.connect(tmp_path / "t.db")
    from tires_sync import order_points
    pid = order_points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk"})
    users.add_user(conn, "point1", "длинный-пароль-1", AT, role="point", point_id=pid)
    point = app.test_client()
    with point.session_transaction() as session:
        session.update(user="point1", epoch=store.get_session_epoch(conn, "point1"), csrf="tok")
    conn.close()
    assert point.get("/pairs").status_code == 403


def test_a_code_that_waits_for_the_day_is_on_the_page_as_in_match_report(tmp_path, capsys, monkeypatch):
    """Cycle 2 hands the ad 100 over to 4tochki and is published; WebMIM comes back the same day: it waits (the pause of a day)."""
    from datetime import datetime
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    args = published_pair(tmp_path)
    (tmp_path / "unload.xml").write_bytes(UNLOAD_WITHOUT_WEBMIM)
    assert main(["run-cycle"] + args) == 0
    conn = store.connect(tmp_path / "t.db")
    for account in connections.list_accounts(conn):
        store.commit_pending(conn, account["id"], (tmp_path / "feed" / account["feed_token"] / "next" / "avito_feed.xml").read_bytes())
    conn.close()
    app = page_app(tmp_path)
    owner = signed_in(app, "owner")
    html = owner.get("/pairs").get_data(as_text=True)
    assert "держит объявление" in html and "Повторное размещение" in html               # one side gone: the pair carries the ad
    (tmp_path / "unload.xml").write_bytes(UNLOAD)
    capsys.readouterr()
    assert main(["match-report", "--db", args[1], "--unload", args[3], "--reference", args[5]]) == 0
    out = capsys.readouterr().out
    waits = re.findall(r"ждёт смены поставщика: (\S+) \(Id (\S+),", out)
    assert waits and ("100", "100") in waits
    from pathlib import Path
    from tires_sync.catalog import parse_reference
    from tires_sync.settings_store import active_settings
    from tires_sync.suppliers import load_all_parsed
    conn = store.connect(tmp_path / "t.db")
    settings = active_settings(conn)
    parsed, _digest, _notes = load_all_parsed(conn, settings, Path(app.config["UNLOAD_PATH"]))
    view = pairs_view(conn, parsed, parse_reference((tmp_path / "reference.txt").read_text(encoding="utf-8")), settings,
                      app.config["UNLOAD_PATH"], now=datetime(2026, 10, 25, 3, 0, 0))             # the clock of match-report here
    conn.close()
    shown = {(a["id"], w["code"], w["ad_id"]) for a in view["accounts"] for w in a["waiting"]}
    assert (1, "100", "100") in shown and {(c, i) for _a, c, i in shown} == set(waits)


def test_the_page_says_when_the_unload_cannot_be_read(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    prepare(tmp_path)
    (tmp_path / "unload.xml").write_bytes(b"<broken")
    page = signed_in(page_app(tmp_path), "owner").get("/pairs")
    assert page.status_code == 200 and "Выгрузка поставщика не читается" in page.get_data(as_text=True)


def test_a_page_that_would_write_fails_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    prepare(tmp_path)
    real = tire_match.for_feed

    def writing(conn, offers, *a, **kw):
        conn.execute("INSERT INTO audit (at, author, action, detail) VALUES ('x', 'x', 'x', '{}')")
        return real(conn, offers, *a, **kw)
    monkeypatch.setattr(tire_match, "for_feed", writing)
    owner = signed_in(page_app(tmp_path), "owner")
    before = counts(tmp_path)["audit"]
    assert owner.get("/pairs").status_code == 500
    assert counts(tmp_path)["audit"] == before


def test_the_forgotten_mark_shows_moscow_time_and_the_journal_has_both_texts(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    published_pair(tmp_path)
    owner = signed_in(page_app(tmp_path), "owner")
    post(owner, "/pairs/forget", "tok", a="s2-TCH1", b="100")
    assert "25.10.2026 12:00:00" in owner.get("/pairs").get_data(as_text=True)            # 09:00 UTC of the panel's clock
    post(owner, "/pairs/forget", "tok", a="100", b="s2-TCH1", undo="1")
    journal = owner.get("/history").get_data(as_text=True)
    assert "Пара поставщиков забыта: 100 | s2-TCH1" in journal and "Пара поставщиков возвращена: 100 | s2-TCH1" in journal
