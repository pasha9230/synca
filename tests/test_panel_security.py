import re

import pytest
from werkzeug.security import check_password_hash

from tires_sync.panel.__main__ import build_app, main
from panel_helpers import login, make_app


def concrete(rule):
    return re.sub(r"<[^>]+>", "1", rule.rule)


def test_every_page_except_login_requires_a_session(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    public = {"share_public", "share_photo", "account_photo", "stored_photo"}   # the client link and the photos: open by design, guarded by an unguessable token / content hash
    status = {"health"}                                          # the outside watchdog: only ok / fail and codes, no data (tests/test_health.py)
    invite = {"invite_page"}                                     # the one-time invitation: open by design, an unknown token is a 404 (tests/test_roles.py)
    agent = {r.endpoint for r in app.url_map.iter_rules() if r.rule.startswith(("/api/image-jobs", "/api/photo-copies", "/api/review-sheet"))}
    assert len(agent) == 9                                       # the photo agent: a bearer token instead of a session, 403 without it (work, run-done: test_photo_agent_run)
    for rule in app.url_map.iter_rules():
        if "GET" in rule.methods and rule.rule != "/login" and rule.endpoint != "static" and rule.endpoint not in public | agent | status | invite:
            r = client.get(concrete(rule))
            assert r.status_code == 302 and "/login" in r.headers["Location"], rule.rule
    assert {r.endpoint for r in app.url_map.iter_rules() if r.rule.startswith(("/s/", "/img/"))} == public - {"stored_photo"}
    assert [r.rule for r in app.url_map.iter_rules() if r.endpoint in status] == ["/health"]
    assert [r.endpoint for r in app.url_map.iter_rules() if r.rule.startswith("/photos/<")] == ["stored_photo"]
    for rule in app.url_map.iter_rules():
        if rule.endpoint in agent and "GET" in rule.methods:
            assert client.get(concrete(rule)).status_code == 403, rule.rule
    for rule in app.url_map.iter_rules():
        if rule.endpoint in public:
            assert client.get(concrete(rule)).status_code == 404, rule.rule


def test_every_post_route_rejects_a_missing_csrf_token(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    for rule in app.url_map.iter_rules():
        if rule.rule.startswith(("/api/image-jobs", "/api/photo-copies", "/api/review-sheet")):     # the photo agent: no CSRF, a bearer token (tests/test_agent_api.py)
            assert "POST" not in rule.methods or client.post(concrete(rule), data={}).status_code == 403, rule.rule
        elif "POST" in rule.methods and rule.endpoint not in ("vk_hook", "avito_hook"):   # VK's callback and Avito's webhook: their own secrets (tests/test_vk_callback.py, test_avito_webhook.py)
            assert client.post(concrete(rule), data={}).status_code == 400, rule.rule
    assert sorted(r.endpoint for r in app.url_map.iter_rules() if r.rule.startswith("/hook/")) == ["avito_hook", "vk_hook"]


def test_build_app_requires_secrets_from_the_environment(tmp_path):
    with pytest.raises(RuntimeError):
        build_app({}, str(tmp_path / "t.db"), "u.xml", "r.txt")
    app = build_app({"PANEL_PASSWORD_HASH": "x", "PANEL_SECRET_KEY": "y"}, str(tmp_path / "t.db"), "u.xml", "r.txt")
    assert app.config["SESSION_COOKIE_SECURE"] is True                  # https by default (docs/runs/2026-10-24-open2: no line in panel.env needed)
    local = build_app({"PANEL_PASSWORD_HASH": "x", "PANEL_SECRET_KEY": "y", "PANEL_SECURE_COOKIES": "0"},
                      str(tmp_path / "t.db"), "u.xml", "r.txt")
    assert local.config["SESSION_COOKIE_SECURE"] is False


def test_hash_password_command_prints_a_verifiable_pbkdf2_hash(monkeypatch, capsys):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "secret-pw")
    assert main(["hash-password"]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed.startswith("pbkdf2:sha256") and check_password_hash(printed, "secret-pw")


def test_static_assets_are_public_and_confined_to_the_static_folder(tmp_path):
    client = make_app(tmp_path).test_client()
    css = client.get("/static/panel.css")
    assert css.status_code == 200 and css.headers["X-Content-Type-Options"] == "nosniff"
    assert client.get("/static/fonts/inter-cyrillic.woff2").status_code == 200
    assert client.get("/static/../__init__.py").status_code == 404
    assert client.get("/static/%2e%2e/__init__.py").status_code == 404


def test_nav_highlights_only_the_current_section(tmp_path):
    client = make_app(tmp_path).test_client()
    login(client)
    page = client.get("/catalog").get_data(as_text=True)
    assert 'class="sb-link is-active" href="/catalog"' in page
    assert 'class="sb-link is-active" href="/"' not in page


def test_responses_carry_a_content_security_policy_and_other_hardening_headers(tmp_path):
    client = make_app(tmp_path).test_client()
    r = client.get("/login")
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    assert "img-src" in csp and "https:" in csp
    assert r.headers["Permissions-Policy"].startswith("camera=()")
