from __future__ import annotations

from panel_helpers import login, make_app, post
from tires_sync import store
from tires_sync.settings_store import active_settings, confirm_draft, create_draft

T = "2026-10-07T10:00:00"


def setup(tmp_path, guard_min):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    confirm_draft(conn, create_draft(conn, {"guard_min": guard_min, "guard_fraction": 0.0}, T), T, "owner")
    return app, client, tok, conn


def test_review_shows_the_removal_count_per_brand_and_asks_to_understand_when_over_the_limit(tmp_path):
    app, client, tok, conn = setup(tmp_path, 0)
    d = create_draft(conn, {"enabled": []}, T)
    html = client.get("/draft/%d" % d).get_data(as_text=True)
    assert "Исчезнет" in html and "IKON TYRES" in html
    assert 'name="understand"' in html and "Я понимаю" in html


def test_confirming_a_large_removal_without_the_checkbox_is_refused_and_with_it_applied(tmp_path):
    app, client, tok, conn = setup(tmp_path, 0)
    d = create_draft(conn, {"enabled": []}, T)
    refused = post(client, "/draft/%d/confirm" % d, tok)
    assert refused.status_code == 302 and refused.headers["Location"].endswith("/draft/%d" % d)
    assert active_settings(conn).enabled != frozenset()
    assert "Подтвердите снятие" in client.get("/draft/%d" % d).get_data(as_text=True)
    post(client, "/draft/%d/confirm" % d, tok, understand="on")
    assert active_settings(conn).enabled == frozenset()


def test_small_removals_under_the_limit_need_no_extra_checkbox(tmp_path):
    app, client, tok, conn = setup(tmp_path, 50)
    d = create_draft(conn, {"enabled": []}, T)
    assert 'name="understand"' not in client.get("/draft/%d" % d).get_data(as_text=True)
    post(client, "/draft/%d/confirm" % d, tok)
    assert active_settings(conn).enabled == frozenset()
