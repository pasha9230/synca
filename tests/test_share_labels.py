"""Who the two shares of the net profit go to (the analytics tile, the report of the orders, its CSV): the display names of the active owners and
partners of the panel, else «Владельцу» / «Партнёру»; the label the owner writes on «Отчёт по заказам» comes first. A label only: the money keys (`owner`, `partner`) and the split stay as they were."""
from __future__ import annotations

from datetime import datetime

import pytest

import order_report_fixture as fixture
from panel_helpers import login, make_app
from test_dashboard import UTC_NOW, fill
from tires_sync import order_report, store, users

NOW = "2026-10-20T09:00:00"
PASSWORD = "Long-test-pass-2026"


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


def team(conn):
    users.add_user(conn, "anna", PASSWORD, NOW, role="owner", display_name="Анна")
    users.add_user(conn, "boris", PASSWORD, NOW, role="partner", display_name="Борис & <b>Ко</b>")
    users.add_user(conn, "vera", PASSWORD, NOW, role="partner", display_name="  Вера  ")
    users.add_user(conn, "gleb", PASSWORD, NOW, role="partner", display_name="Глеб")
    users.set_active(conn, "gleb", False, NOW, "test")                                   # switched off: not a partner of the report
    users.add_user(conn, "dina", PASSWORD, NOW, role="viewer", display_name="Дина")      # another role: never a share


def test_without_names_the_labels_are_the_neutral_ones(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    assert order_report.share_labels(conn) == {"owner": "Владельцу", "partner": "Партнёру"}
    conn.execute("INSERT INTO panel_user VALUES ('old', 'x', ?)", (NOW,))                # made before the roles: an owner without a name
    users.add_user(conn, "boris", PASSWORD, NOW, role="partner")
    assert order_report.share_labels(conn) == {"owner": "Владельцу", "partner": "Партнёру"}


def test_the_labels_are_the_names_of_the_active_owners_and_partners(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    team(conn)
    assert order_report.share_labels(conn) == {"owner": "Анна", "partner": "Борис & <b>Ко</b>, Вера"}


def test_the_csv_header_takes_the_labels_and_keeps_a_formula_out(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    fixture.fill(conn)
    rep = order_report.build(conn, "2026-08-01", "2026-10-31")
    header = order_report.export_csv(rep, {"owner": "Анна", "partner": "=Борис"}).split("\n")[0]
    assert header.endswith(";Чистая прибыль;Анна;'=Борис")
    assert order_report.export_csv(rep).split("\n")[0].endswith(";Владельцу;Партнёру")


def test_the_pages_and_the_csv_show_the_names(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 20, 9, 0, 0))
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    fixture.fill(conn)
    html = client.get("/orders/report?from=2026-08-01&to=2026-10-31").get_data(as_text=True)
    assert "<th>Владельцу</th><th>Партнёру</th>" in html and "делится на две доли: «Владельцу» и «Партнёру»" in html
    team(conn)
    html = client.get("/orders/report?from=2026-08-01&to=2026-10-31").get_data(as_text=True)
    assert "<th>Анна</th><th>Борис &amp; &lt;b&gt;Ко&lt;/b&gt;, Вера</th>" in html and "«Анна» и «Борис &amp; &lt;b&gt;Ко&lt;/b&gt;, Вера»" in html
    assert "<b>Ко</b>" not in html                                                       # escaped: a name is text, never markup
    csv = client.get("/orders/report/export.csv?from=2026-08-01&to=2026-10-31").get_data(as_text=True)
    assert csv.split("\n")[0].endswith(";Анна;Борис & <b>Ко</b>, Вера")


def test_the_analytics_tile_shows_the_names(tmp_path):
    app = make_app(tmp_path, NOW=lambda: UTC_NOW)
    conn = store.connect(app.config["DB_PATH"])
    fill(conn)
    client = app.test_client()
    login(client)
    page = client.get("/analytics?from=2026-10-01&to=2026-10-21").get_data(as_text=True)
    assert '<span class="tile-label">Владельцу / Партнёру</span>' in page and '<span class="tile-sub">Партнёру ' in page
    team(conn)
    page = client.get("/analytics?from=2026-10-01&to=2026-10-21").get_data(as_text=True)
    assert '<span class="tile-label">Анна / Борис &amp; &lt;b&gt;Ко&lt;/b&gt;, Вера</span>' in page and '<span class="tile-sub">Борис &amp; &lt;b&gt;Ко&lt;/b&gt;, Вера ' in page


def test_a_saved_label_comes_first_and_an_empty_one_falls_back(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    team(conn)
    assert order_report.set_share_labels(conn, "  Собственнику ", "", NOW, "owner") is True
    assert order_report.share_labels(conn) == {"owner": "Собственнику", "partner": "Борис & <b>Ко</b>, Вера"}
    assert order_report.set_share_labels(conn, "Собственнику", "", NOW, "owner") is False                   # the same: nothing written
    order_report.set_share_labels(conn, "Собственнику", "П" * 100, NOW, "owner")
    assert order_report.share_labels(conn)["partner"] == "П" * order_report.SHARE_LABEL_MAX
    order_report.set_share_labels(conn, " ", "", NOW, "owner")                                              # both empty: the key is gone
    assert conn.execute("SELECT 1 FROM app_state WHERE key = 'share_labels'").fetchone() is None
    assert order_report.share_labels(conn) == {"owner": "Анна", "partner": "Борис & <b>Ко</b>, Вера"}
    audit = [a["detail"] for a in store.list_audit(conn) if a["action"] == "share_labels"]
    assert len(audit) == 3 and {"from": {"owner": "", "partner": ""}, "to": {"owner": "Собственнику", "partner": ""}} in audit
    for broken in ("[1, 2]", "not json"):                                                                   # a broken value: as if not written
        conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES ('share_labels', ?)", (broken,))
        assert order_report.share_labels(conn) == {"owner": "Анна", "partner": "Борис & <b>Ко</b>, Вера"}


def test_the_owner_saves_the_labels_on_the_report_page(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 20, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    fixture.fill(conn)
    html = client.get("/orders/report").get_data(as_text=True)
    assert "<h2>Подписи долей</h2>" in html and 'name="owner_label"' in html and 'placeholder="Владельцу"' in html
    assert client.post("/orders/report/rates", data={"csrf": tok, "what": "labels", "owner_label": "Собственнику",
                                                     "partner_label": "Второму"}).status_code == 302
    assert order_report.list_rates(conn) == []                                                               # a label: no rate written
    html = client.get("/orders/report?from=2026-08-01&to=2026-10-31").get_data(as_text=True)
    assert "<th>Собственнику</th><th>Второму</th>" in html and 'value="Собственнику"' in html and 'value="Второму"' in html
    csv = client.get("/orders/report/export.csv?from=2026-08-01&to=2026-10-31").get_data(as_text=True)
    assert csv.split("\n")[0].endswith(";Собственнику;Второму")
    assert [(a["author"], a["at"][:16]) for a in store.list_audit(conn) if a["action"] == "share_labels"] == [("owner", "2026-10-20T09:00")]  # UTC, as the journal reads it
    assert client.post("/orders/report/rates", data={"what": "labels", "owner_label": "X", "partner_label": "Y"}).status_code in (400, 403)
    assert order_report.saved_share_labels(conn) == {"owner": "Собственнику", "partner": "Второму"}           # no CSRF: nothing written


@pytest.mark.parametrize("role", ["viewer", "partner", "chats", "point"])
def test_without_the_right_the_labels_are_not_saved(tmp_path, role):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 20, 9, 0, 0))
    conn = store.connect(app.config["DB_PATH"])
    users.add_user(conn, "someone", PASSWORD, NOW, role=role, point_id=1 if role == "point" else None)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user"], s["epoch"], s["csrf"] = "someone", store.get_session_epoch(conn, "someone"), "tok"
    assert client.post("/orders/report/rates", data={"csrf": "tok", "what": "labels", "owner_label": "X", "partner_label": "Y"}).status_code == 403
    assert order_report.saved_share_labels(conn) == {"owner": "", "partner": ""}
    assert 'name="owner_label"' not in client.get("/orders/report").get_data(as_text=True)
