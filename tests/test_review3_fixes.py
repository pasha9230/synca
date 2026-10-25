from __future__ import annotations

import re

from panel_helpers import login, make_app
from tires_sync import store

T = "2026-10-07T10:00:00"


def test_new_count_is_a_cheap_count_with_an_index(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    assert store.count_new_since(conn, 0) == len(store.new_since(conn, 0)["goods"])
    names = {r[1] for r in conn.execute("PRAGMA index_list(seen_goods)")}
    assert "seen_goods_first_run" in names


def test_changing_a_mapping_drops_the_cached_menu_counter(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.set_state(conn, "nav_pending_pairs", "9")
    store.upsert_mapping(conn, "Ikon Tyres", "X", "Test Model")
    assert store.get_state(conn, "nav_pending_pairs") is None
    store.set_state(conn, "nav_pending_pairs", "9")
    store.upsert_brand_map(conn, "Z", "Pirelli")
    assert store.get_state(conn, "nav_pending_pairs") is None


def test_menu_counter_is_recomputed_on_any_page_without_visiting_home(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    store.set_state(conn, "nav_pending_pairs", "9")
    store.upsert_mapping(conn, "Ikon Tyres", "Whatever", "Other Model")
    client.get("/history")
    value = store.get_state(conn, "nav_pending_pairs")
    assert value is not None and value != "9"


def test_menu_is_not_computed_for_fragments_without_menu(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    calls = []
    real = store.new_since
    monkeypatch.setattr(store, "new_since", lambda *a, **k: calls.append(1) or real(*a, **k))                # the menu's counter reads the new goods
    client.get("/catalog/rows?brand=Ikon+Tyres")
    assert calls == []
    client.get("/history")
    assert calls


def test_check_uses_the_values_typed_in_the_same_form(tmp_path):
    from cryptography.fernet import Fernet
    from http_fakes import ScriptedHttp
    from panel_helpers import post
    from tires_sync import connections
    from tires_sync.secretbox import SecretBox

    token = (200, {"access_token": "t", "expires_in": 3600})
    me = (200, {"id": 7, "name": "N"})
    box = SecretBox(Fernet.generate_key())
    app = make_app(tmp_path, SECRET_BOX=box, AVITO_HTTP=ScriptedHttp(token, me, (200, {})))
    client = app.test_client()
    tok = login(client)
    post(client, "/connections/accounts", tok, name="A")
    # keys typed and "Проверить" pressed straight away: they must be saved and used
    post(client, "/connections/accounts/1/check", tok, client_id="CID-12345678", client_secret="SECRET-XYZ")
    conn = store.connect(app.config["DB_PATH"])
    assert connections.get_secret(conn, box, "avito", 1, "client_id") == "CID-12345678"
    assert "ключи не заданы" not in client.get("/connections/avito/1").get_data(as_text=True).lower()


def test_telegram_test_button_saves_typed_token_and_chat(tmp_path):
    from cryptography.fernet import Fernet
    from panel_helpers import post
    from tires_sync import connections
    from tires_sync.secretbox import SecretBox

    box = SecretBox(Fernet.generate_key())
    app = make_app(tmp_path, SECRET_BOX=box, TELEGRAM_HTTP=lambda *a, **k: (200, {"ok": True}))
    client = app.test_client()
    tok = login(client)
    post(client, "/connections/telegram/test", tok, chat_id="123", bot_token="123:ABC")
    conn = store.connect(app.config["DB_PATH"])
    assert connections.get_config(conn, "telegram", 0)["chat_id"] == "123"
    assert connections.get_secret(conn, box, "telegram", 0, "bot_token") == "123:ABC"


def test_webmim_check_button_saves_typed_form_first(tmp_path):
    from cryptography.fernet import Fernet
    from panel_helpers import post
    from tires_sync import connections
    from tires_sync.secretbox import SecretBox

    box = SecretBox(Fernet.generate_key())
    app = make_app(tmp_path, SECRET_BOX=box, WEBMIM_HTTP=lambda *a, **k: (500, ""))
    client = app.test_client()
    tok = login(client)
    post(client, "/connections/webmim/check", tok, base_url="https://mim.example", mode="apikey", login="u",
         password="", api_key="KEY-123456789")
    conn = store.connect(app.config["DB_PATH"])
    assert connections.get_config(conn, "webmim", 0)["base_url"] == "https://mim.example"
    assert connections.get_secret(conn, box, "webmim", 0, "api_key") == "KEY-123456789"


def test_logged_in_user_is_sent_home_from_the_login_page(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    assert client.get("/login").status_code == 200
    login(client)
    r = client.get("/login")
    assert r.status_code == 302 and r.headers["Location"].endswith("/")


def test_cancel_all_button_keeps_the_current_catalog_filters(tmp_path):
    from tires_sync import staging
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    staging.set_override(conn, "1", "off", T)
    html = client.get("/catalog?s=winter&q=ikon").get_data(as_text=True)
    assert re.search(r'name="next" value="/catalog\?s=winter&amp;q=ikon"', html)
    assert 'name="next" value="/history"' in client.get("/history").get_data(as_text=True)      # stays on the page it was pressed on


def test_stale_and_pending_bars_and_menu_counters_render(tmp_path):
    from tires_sync import staging
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    assert 'id="pendbar" hidden' in client.get("/history").get_data(as_text=True)
    staging.set_override(conn, "1", "off", T)
    html = client.get("/history").get_data(as_text=True)
    assert "Неподтверждённых изменений: 1" in html and 'sb-count sb-count--pend' in html
    from tires_sync.settings_store import confirm_draft, create_draft
    confirm_draft(conn, create_draft(conn, {"min_stock": 5}, T), T, "owner")     # settings moved on: the staged draft is stale
    html = client.get("/history").get_data(as_text=True)
    assert "Рабочий черновик устарел" in html and "Неподтверждённых изменений: 1" not in html


def test_csp_has_no_inline_scripts_and_templates_have_none(tmp_path):
    import pathlib
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    csp = client.get("/").headers["Content-Security-Policy"]
    script = [d for d in csp.split(";") if d.strip().startswith("script-src")][0]
    assert "unsafe-inline" not in script
    templates = pathlib.Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "templates"
    found = list(templates.glob("*.html"))
    assert len(found) > 10
    for path in found:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)", text) and not re.search(r"\son(change|click|submit)=", text), path.name


def test_rollback_dialog_clears_checkmarks_each_time_it_opens(tmp_path):
    import pathlib
    js = (pathlib.Path(__file__).resolve().parents[1] / "src/tires_sync/panel/static/app.js").read_text(encoding="utf-8")
    opener = js[js.index("data-rollback"):js.index("data-preview")]
    assert '"rb1"' in opener and '"rb2"' in opener and "checked = false" in opener
    assert "pageshow" in js


def test_new_positions_unseen_counter_does_not_depend_on_the_period_filter(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    r1 = store.save_run(conn, "2026-10-06T10:00:00", False, "a", "{}")
    r2 = store.save_run(conn, "2026-10-07T10:00:00", False, "b", "{}")
    for i in range(3):
        conn.execute("INSERT INTO seen_goods VALUES (?,?,?,?,?)", ("g%d" % i, "IKON TYRES", "M", "N", r2))
    conn.commit()
    a = client.get("/new").get_data(as_text=True)
    b = client.get("/new?days=7").get_data(as_text=True)
    count = lambda h: re.search(r"Не просмотренные <span class=\"cnt\">(\d+)</span>", h).group(1)
    assert count(a) == count(b) == "3"


# --- the real timer path (run-cycle --publish) must honour reviewed removals -------------------------------------------

def _cli_args(app, tmp_path):
    return ["--db", app.config["DB_PATH"], "--unload", app.config["UNLOAD_PATH"],
            "--reference", app.config["REFERENCE_PATH"], "--feed-root", str(tmp_path / "feed")]


def test_timer_cycle_publishes_a_confirmed_big_removal_without_manual_publish(tmp_path):
    from test_review_fixes import panel
    from panel_helpers import post
    from tires_sync import staging
    from tires_sync.cli import main
    app, client, tok, conn, folder = panel(tmp_path, count=10, guard_min=2)
    for g in range(1, 9):
        staging.set_override(conn, str(g), "off", T)
    d = staging.summary(conn)["draft_id"]
    post(client, "/draft/%d/confirm" % d, tok, understand="on")      # applied, not published
    assert main(["run-cycle", "--publish"] + _cli_args(app, tmp_path)) == 0
    conn2 = store.connect(app.config["DB_PATH"])
    assert set(store.load_published(conn2, account_id=1)) == {"9", "10"}
    # the acknowledgement is spent: an unreviewed shrink is refused again
    from tires_sync import publishing
    from tires_sync.settings_store import active_settings
    assert publishing.expected_removals(conn2, active_settings(conn2).version) == 0


def test_rollback_that_removes_many_ads_is_published_by_the_next_timer_cycle(tmp_path):
    from test_review_fixes import panel
    from panel_helpers import post
    from tires_sync import staging
    from tires_sync.cli import main
    app, client, tok, conn, folder = panel(tmp_path, count=10, guard_min=2)
    for g in range(1, 9):
        staging.set_override(conn, str(g), "off", T)
    d = staging.summary(conn)["draft_id"]
    post(client, "/draft/%d/confirm" % d, tok, understand="on", publish="1")   # 2 ads left, published
    assert len(store.load_published(conn, account_id=1)) == 2
    from tires_sync.settings_store import list_versions
    older = [v["id"] for v in list_versions(conn) if not v["active"]]
    # roll back to the version before the removal: everything comes back (growth, no guard needed)
    post(client, "/history/rollback/%d" % older[0], tok, confirm="on", understand="on")
    assert main(["run-cycle", "--publish"] + _cli_args(app, tmp_path)) == 0
    assert len(store.load_published(store.connect(app.config["DB_PATH"]), account_id=1)) == 10


def test_rollback_to_a_version_that_removes_many_ads_does_not_freeze_the_feed(tmp_path):
    from test_review_fixes import panel
    from panel_helpers import post
    from tires_sync import staging
    from tires_sync.cli import main
    from tires_sync.settings_store import list_versions
    app, client, tok, conn, folder = panel(tmp_path, count=10, guard_min=2)
    for g in range(1, 9):
        staging.set_override(conn, str(g), "off", T)
    d = staging.summary(conn)["draft_id"]
    post(client, "/draft/%d/confirm" % d, tok, understand="on", publish="1")   # active: 8 off, 2 ads
    removal_version = [v["id"] for v in list_versions(conn) if v["active"]][0]
    older = [v["id"] for v in list_versions(conn) if not v["active"]][0]
    post(client, "/history/rollback/%d" % older, tok, confirm="on", understand="on")
    assert main(["run-cycle", "--publish"] + _cli_args(app, tmp_path)) == 0     # back to 10
    post(client, "/history/rollback/%d" % removal_version, tok, confirm="on", understand="on")  # 10 -> 2 again
    assert main(["run-cycle", "--publish"] + _cli_args(app, tmp_path)) == 0
    assert len(store.load_published(store.connect(app.config["DB_PATH"]), account_id=1)) == 2


def test_expected_removals_are_counted_in_ads_already_and_not_doubled_again(tmp_path):
    from tires_sync import publishing
    from tires_sync.settings_store import active_settings, init_settings
    from helpers import settings_dict
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(pack4_rules=["*|*|*"]), T)
    s = active_settings(conn)
    publishing.remember_expected(conn, s.version, 3)
    assert publishing.expected_for(conn, s) == 3


def test_the_acknowledgement_survives_until_every_live_account_published(tmp_path):
    from test_review_fixes import panel
    from panel_helpers import post
    from tires_sync import connections, publishing, staging
    app, client, tok, conn, folder = panel(tmp_path, count=10, guard_min=2)
    connections.create_account(conn, "B", T, "owner")
    connections.set_live(conn, 2, True, T, "owner")
    from tires_sync import cycle
    from test_review_fixes import REF, TODAY, goods
    cycle.run_for_accounts(conn, goods(10), REF, TODAY, tmp_path / "feed")
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    for g in range(1, 9):
        staging.set_override(conn, str(g), "off", T)
    d = staging.summary(conn)["draft_id"]
    post(client, "/draft/%d/confirm" % d, tok, understand="on")
    cycle.run_for_accounts(conn, goods(10), REF, TODAY, tmp_path / "feed")
    out = publishing.publish_live_accounts(conn, tmp_path / "feed")
    assert [o["ok"] for o in out] == [True, True], out
