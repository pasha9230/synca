"""«Аналитика»: the money of the period with the right only, the funnel whole and by a slice, the stages; bad dates fall back to 30 days."""
from __future__ import annotations

from datetime import datetime

import pytest

from panel_helpers import login, make_app
from test_dashboard import UTC_NOW, fill
from tires_sync import store, users


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


def test_the_page_with_and_without_the_money_right(tmp_path):
    app = make_app(tmp_path, NOW=lambda: UTC_NOW)
    conn = store.connect(app.config["DB_PATH"])
    fill(conn)
    owner = app.test_client()
    login(owner)
    page = owner.get("/analytics?from=2026-10-01&to=2026-10-21").get_data(as_text=True)
    assert "Продажи и прибыль" in page and "30 000 ₽" in page and "Воронка" in page and "Время на этапах" in page
    assert "с 2026-10-01 по 2026-10-21" in page
    for by in ("source", "account", "responsible", "point"):
        assert owner.get("/analytics?by=%s" % by).status_code == 200
    assert "с 2026-09-22 по 2026-10-21" in owner.get("/analytics?from=bad&to=worse").get_data(as_text=True)
    assert "с 2026-10-01 по 2026-10-21" in owner.get("/analytics?from=2026-10-21&to=2026-10-01").get_data(as_text=True)   # swapped
    users.add_user(conn, "viewer1", "длинный-пароль-1", "2026-10-21T10:00:00", role="viewer")
    viewer = app.test_client()
    with viewer.session_transaction() as s:
        s["user"], s["epoch"] = "viewer1", store.get_session_epoch(conn, "viewer1")
    page = viewer.get("/analytics?from=2026-10-01&to=2026-10-21").get_data(as_text=True)
    assert "Воронка" in page and "<h3>Продажи и прибыль</h3>" not in page and "30\u00a0000" not in page and 'href="/analytics"' in page
    assert 'href="/economics"' not in page
