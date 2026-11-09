"""«Настройки заказов › Расчёты» (п. 28 of the audit, 2026-11-08): the forms «Ставки» and «Подписи долей» moved from «Отчёт по заказам»
with the same rights — who sees the money sees the tab (the owner and the partner), only who may change «Настройка» gets the forms."""
from __future__ import annotations

from datetime import datetime

import pytest

from panel_helpers import login, make_app
from tires_sync import order_report, store, users

NOW = "2026-10-20T09:00:00"
PASSWORD = "Long-test-pass-2026"


def _as(app, conn, role):
    users.add_user(conn, "someone", PASSWORD, NOW, role=role, point_id=1 if role == "point" else None)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user"], s["epoch"], s["csrf"] = "someone", store.get_session_epoch(conn, "someone"), "tok"
    return client


def test_the_owner_has_the_tab_with_both_forms_and_the_report_has_none(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 20, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    tabs = client.get("/orders/settings").get_data(as_text=True)
    assert '<a href="/orders/settings/money">Расчёты</a>' in tabs
    page = client.get("/orders/settings/money").get_data(as_text=True)
    assert 'class="is-active" aria-current="page">Расчёты</a>' in page and "<h2>Ставки</h2>" in page and "<h2>Подписи долей</h2>" in page
    assert page.count('action="/orders/report/rates"') == 2                    # the same POST as on the report
    report = client.get("/orders/report").get_data(as_text=True)
    assert "<h2>Ставки</h2>" not in report and "<h2>Подписи долей</h2>" not in report and 'action="/orders/report/rates"' not in report
    r = client.post("/orders/report/rates", data={"csrf": tok, "point_fee": "80", "point_fee_was": "75", "owner_pct": "50", "owner_pct_was": "50",
                                                  "handoff_fee": "0", "handoff_fee_was": "0", "valid_from": "2026-10-21"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/orders/settings/money")
    assert [(x["name"], x["value"]) for x in order_report.list_rates(store.connect(app.config["DB_PATH"]))] == [("point_fee", 80.0)]
    r = client.post("/orders/report/rates", data={"csrf": tok, "what": "labels", "owner_label": "А", "partner_label": "Б"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/orders/settings/money")


def test_the_partner_sees_the_rates_without_the_forms(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 20, 9, 0, 0))
    conn = store.connect(app.config["DB_PATH"])
    client = _as(app, conn, "partner")
    page = client.get("/orders/settings/money")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Ставки не менялись" in html and 'action="/orders/report/rates"' not in html
    assert '<a href="/orders/settings/money">Расчёты</a>' in client.get("/orders/settings").get_data(as_text=True)
    assert client.post("/orders/report/rates", data={"csrf": "tok", "point_fee": "1", "valid_from": "2026-10-21"}).status_code == 403


@pytest.mark.parametrize("role", ["viewer", "chats", "point"])
def test_who_does_not_see_the_money_does_not_open_the_tab(tmp_path, role):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 20, 9, 0, 0))
    conn = store.connect(app.config["DB_PATH"])
    client = _as(app, conn, role)
    assert client.get("/orders/settings/money").status_code == 403
    assert client.get("/orders/report").status_code == 403                    # as the report itself
