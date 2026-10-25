from tires_sync import store
from tires_sync.settings_store import active_settings, get_active
from panel_helpers import login, make_app, post


def stage_summer(client, tok, small="-3", large="-3", enabled="on", brand="IKON TYRES", season="summer"):
    fields = {season + "_form": "1", season + "_small": small, season + "_large": large}
    if enabled == "on":
        fields[season + "_on"] = "on"
    return post(client, "/settings/pricing", tok, scope="brand", supplier="1", brand=brand, **fields)



def test_staged_brand_change_becomes_a_draft_that_confirms_into_a_new_version(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    assert stage_summer(client, tok).status_code == 302
    r = client.get("/catalog/review")
    assert r.status_code == 302 and r.headers["Location"].endswith("/draft/1")
    page = client.get("/draft/1").get_data(as_text=True)
    assert "enabled" in page and "pricing" in page
    assert post(client, "/draft/1/confirm", tok).status_code == 302
    conn = store.connect(app.config["DB_PATH"])
    assert any(s.value == "summer" for b, s in active_settings(conn).enabled if b == "IKON TYRES")
    assert get_active(conn)[0] == 2


def test_a_change_that_changes_nothing_creates_no_draft(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    r = stage_summer(client, tok, small="-3.82", large="-4.28", season="winter")
    assert r.status_code == 302
    assert client.get("/catalog/review").headers["Location"].endswith("/catalog")
    assert "Неподтверждённых изменений нет" in client.get("/catalog").get_data(as_text=True)
    conn = store.connect(app.config["DB_PATH"])
    assert conn.execute("SELECT COUNT(*) FROM draft").fetchone()[0] == 0


def test_malformed_percent_never_creates_a_draft_or_crashes(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    for small, large in (("abc", "-3"), ("-3", ""), ("nan", "-3"), ("inf", "-3")):
        assert stage_summer(client, tok, small=small, large=large).status_code == 302
    conn = store.connect(app.config["DB_PATH"])
    assert conn.execute("SELECT COUNT(*) FROM draft").fetchone()[0] == 0
    assert get_active(conn)[0] == 1


def test_percent_outside_range_needs_the_strong_checkbox(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    stage_summer(client, tok, small="-20", large="-3")
    assert "усиленное подтверждение" in client.get("/draft/1").get_data(as_text=True)
    r = post(client, "/draft/1/confirm", tok)
    page = client.get(r.headers["Location"]).get_data(as_text=True)
    assert "усиленное подтверждение" in page
    conn = store.connect(app.config["DB_PATH"])
    assert get_active(conn)[0] == 1
    post(client, "/draft/1/confirm", tok, strong="on")
    assert get_active(store.connect(app.config["DB_PATH"]))[0] == 2


def test_stale_draft_is_refused_with_a_clear_message(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    post(client, "/settings/draft", tok, **{"min_stock": "6"})
    post(client, "/settings/draft", tok, **{"min_stock": "7"})
    post(client, "/draft/1/confirm", tok)
    r = post(client, "/draft/2/confirm", tok)
    assert "устарел" in client.get(r.headers["Location"]).get_data(as_text=True)
    assert active_settings(store.connect(app.config["DB_PATH"])).min_stock == 6


def test_settings_form_changes_only_differing_fields_and_cancel_closes_the_draft(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    form = {"min_stock": "5", "hysteresis": "2", "min_diameter": "15", "portion_limit": "100", "stale_days": "3",
            "date_end_days": "29", "point_mode": "percent", "pv|P1": "40", "pv|P2": "40", "pv|P3": "20",
            "point_rest": ""}
    post(client, "/settings/draft", tok, **form)
    page = client.get("/draft/1").get_data(as_text=True)
    assert "Минимальный остаток" in page and "Запас гистерезиса" not in page
    assert post(client, "/draft/1/cancel", tok).status_code == 302
    post(client, "/draft/1/confirm", tok)
    assert get_active(store.connect(app.config["DB_PATH"]))[0] == 1


def test_unknown_draft_returns_404(tmp_path):
    client = make_app(tmp_path).test_client()
    login(client)
    assert client.get("/draft/99").status_code == 404


def test_non_finite_percents_and_point_shares_never_create_a_draft(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    for bad in ("nan", "inf", "-inf", "1e400"):
        stage_summer(client, tok, small=bad, large="-3")
        post(client, "/settings/draft", tok, **{"pv|P1": bad, "pv|P2": "50", "pv|P3": "50", "point_mode": "percent"})
    conn = store.connect(app.config["DB_PATH"])
    assert conn.execute("SELECT COUNT(*) FROM draft").fetchone()[0] == 0
    assert client.get("/").status_code == 200


def test_settings_default_points_show_the_address_name_next_to_the_id(tmp_path):
    from tires_sync import connections, store
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    a = connections.create_account(conn, "A", "2026-10-07T10:00:00", "owner")
    connections.save_addresses(conn, a, "percent", None, [
        {"name": "Бета", "address_id": "101", "enabled": True, "share": 100, "address_text": ""}], "2026-10-07T10:00:00", "owner")
    from tires_sync.settings_store import confirm_draft, create_draft
    confirm_draft(conn, create_draft(conn, {"point_values": {"101": 60, "102": 40}}, "2026-10-07T10:01:00"), "2026-10-07T10:01:00", "owner")
    page = client.get("/settings").get_data(as_text=True)
    assert "101 · Бета" in page and "102 ·" not in page


def test_zero_days_before_taking_off_can_be_set_in_the_settings_form(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    form = {"min_stock": "4", "min_diameter": "15", "portion_limit": "100", "stale_days": "0", "date_end_days": "29",
            "point_mode": "percent", "pv|P1": "40", "pv|P2": "40", "pv|P3": "20", "point_rest": ""}
    post(client, "/settings/draft", tok, **form)
    assert "Дней «пропуска» до снятия" in client.get("/draft/1").get_data(as_text=True)
    post(client, "/draft/1/confirm", tok)
    assert get_active(store.connect(app.config["DB_PATH"]))[1]["stale_days"] == 0
