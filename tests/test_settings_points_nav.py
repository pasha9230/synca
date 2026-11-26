"""«Уведомления точек» inside «Настройки → Уведомления», the journal of the points in «Настройки → История → Точки» with its filters, the
tab «История» of «Настройки», and «Пункты выдачи → Аналитика точек» (the owner 2026-11-26)."""
from __future__ import annotations

import re

from panel_helpers import post
from test_point_notify import NOW, clock, env, staff, view  # noqa: F401 - fixtures
from tires_sync import point_log, point_metrics, point_notify as pn


def _sales_menu(page: str) -> str:
    return re.search(r'data-sec="sales".*?</nav>', page, re.S).group(0)


def test_the_item_is_not_in_the_sales_menu_and_the_block_is_inside_settings(tmp_path, clock, view):
    app, owner, tok, conn, a, b, mine = env(tmp_path, view)
    page = owner.get("/orders").get_data(as_text=True)
    assert "Уведомления точек" not in _sales_menu(page) and 'href="/points/notify"' not in page
    assert 'href="/settings"' in page and '>История</a>' not in re.search(r'data-sec="setup".*?</nav>', page, re.S).group(0)
    page = owner.get("/settings/notify").get_data(as_text=True)
    assert "data-points-notify-block" in page and 'data-notify-point="%d"' % a in page and "data-point-switches" in page
    assert "data-points-notify-link" not in page and "Утренняя сводка" in page
    assert '<a href="/settings/notify" class="is-active" aria-current="page">Уведомления</a>' in page and 'href="/history"' in page
    post(owner, "/points/notify", tok, p=str(a), on="0", reason="ремонт", back="/settings/notify")
    assert not pn.enabled(conn, a)
    assert 'data-state="off"' in owner.get("/settings/notify").get_data(as_text=True)


def test_the_old_address_leads_to_the_settings_and_its_history_to_the_journal(tmp_path, clock):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    got = owner.get("/points/notify")
    assert got.status_code == 302 and got.headers["Location"].endswith("/settings/notify#points-notify")
    got = owner.get("/points/notify?p=%d&from=2026-11-01&kind=notify_off" % a)
    assert got.headers["Location"].endswith("/history/points?p=%d&from=2026-11-01&k=notify_off" % a)
    for url in ("/history", "/history/ads", "/history/catalog", "/history/points", "/points/history"):
        assert owner.get(url, follow_redirects=True).status_code == 200, url


def test_the_history_tabs_are_under_the_settings_tabs(tmp_path, clock):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    for url, tab in (("/history", "Журнал и версии"), ("/history/ads", "События позиций"), ("/history/catalog", "Каталог по часам"),
                     ("/history/points", "Точки")):
        page = owner.get(url).get_data(as_text=True)
        assert '<a href="/history" class="is-active" aria-current="page">История</a>' in page, url
        tabs = page[page.index("data-history-tabs"):]
        assert 'class="is-active" aria-current="page">%s</a>' % tab in tabs and "Уведомления точек" not in tabs, url


def test_the_chats_manager_switches_the_points_from_the_settings_the_point_never(tmp_path, clock):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    chats = staff(app, conn, "chatty", "chats")
    page = chats.get("/settings/notify").get_data(as_text=True)
    assert "data-points-notify-block" in page and "data-switch-compare" in page and "Утренняя сводка" not in page
    assert 'href="/settings/notify"' in page                                           # «Настройки» in its menu
    looker = staff(app, conn, "vera", "viewer")
    page = looker.get("/settings/notify").get_data(as_text=True)
    assert "data-points-notify-block" in page and 'data-switch="off"' not in page and "data-switch-compare" not in page
    assert mine.get("/settings/notify").status_code == 403 and mine.get("/history/points").status_code == 403
    assert "Уведомления точек" not in mine.get("/my-point").get_data(as_text=True).split("<main")[0]


def test_the_journal_filters_by_point_days_and_action_and_shows_the_notifications(tmp_path, clock):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    point_log.record(conn, "2026-11-20T10:00:00", "gamma", a, "arrived", 11)
    point_log.record(conn, "2026-11-24T10:00:00", "gamma", a, "issued", 11)
    point_log.record(conn, "2026-11-24T11:00:00", "delta", b, "arrived", 12)
    pn.switch(conn, a, False, "2026-11-25T09:00:00", "admin", "Офис", "ремонт")
    kinds = {r["kind"] for r in point_log.journal(conn)}
    assert kinds == {"arrived", "issued", "notify_off"}
    assert [r["kind"] for r in point_log.journal(conn, a, "2026-11-24", "2026-11-25")] == ["notify_off", "issued"]
    assert [r["order_id"] for r in point_log.journal(conn, kind="arrived")] == [12, 11]
    assert [r["kind"] for r in point_log.journal(conn, kind="notify_off")] == ["notify_off"]

    def rows(url):
        page = owner.get(url).get_data(as_text=True)
        return re.findall(r'data-history-kind="(\w+)"', page), page
    found, page = rows("/history/points")
    assert sorted(found) == ["arrived", "arrived", "issued", "notify_off"]
    assert "data-journal-from" in page and "data-journal-to" in page and 'name="k"' in page and "ремонт" in page
    assert '<option value="arrived">' in page and '<option value="notify_off">' in page
    assert rows("/history/points?p=%d" % b)[0] == ["arrived"]
    assert rows("/history/points?from=2026-11-24&to=2026-11-24")[0] == ["arrived", "issued"]
    assert rows("/history/points?k=issued")[0] == ["issued"]
    assert rows("/history/points?k=nonsense&from=bad")[0] == found                    # an unknown value: no filter


def _order(conn, point_id, created, code, entered, arrived, moves=()):
    from tires_sync import orders
    orders.ensure_pipeline(conn, "2026-11-01T10:00:00")
    cid = orders.create_card(conn, {"client_name": "Клиент Тест", "point_id": str(point_id)}, created, "чаты")
    stage = conn.execute("SELECT id FROM order_stage WHERE code = ?", (code,)).fetchone()[0]
    conn.execute("UPDATE order_card SET stage_id = ?, entered_at = ?, created_at = ? WHERE id = ?", (stage, entered, created, cid))
    if arrived:
        conn.execute("INSERT INTO order_arrival (order_id, arrived_at, point_id, pieces) VALUES (?,?,?,4)", (cid, arrived, point_id))
    for at, text in moves:
        conn.execute("INSERT INTO order_event (order_id, at, author, kind, text) VALUES (?,?,?,?,?)", (cid, at, "x", "stage", text))
    conn.commit()
    return cid


def test_the_metrics_count_the_hours_and_the_shares_from_the_recorded_moments(tmp_path, clock):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    # Альфа: 24 h to come, issued 2 h later (journal); 48 h to come, not bought: on «На ОТХ» 10 h later (journal); 12 h, still there
    _order(conn, a, "2026-11-10T10:00:00", "done", "2026-11-11T12:00:00", "2026-11-11T10:00:00",
           [("2026-11-11T12:00:00", "Этап: «На точке, ждём клиента» → «Выполнено»")])
    _order(conn, a, "2026-11-10T10:00:00", "oth", "2026-11-12T20:00:00", "2026-11-12T10:00:00",
           [("2026-11-12T09:00:00", "Этап: «Едет на точку» → «На точке, ждём клиента»"), ("2026-11-12T20:00:00", "Этап: «На точке, ждём клиента» → «На ОТХ»")])
    _order(conn, a, "2026-11-13T10:00:00", "at_point", "2026-11-13T22:00:00", "2026-11-13T22:00:00")
    # Бета: no move in the journal, the present stage «Отмена» since 5 h after the arrival; one order never arrived
    _order(conn, b, "2026-11-14T00:00:00", "cancel", "2026-11-14T11:00:00", "2026-11-14T06:00:00")
    _order(conn, b, "2026-11-14T00:00:00", "moving", "2026-11-14T00:00:00", "")
    conn.execute("INSERT INTO order_handoff (order_id, message_id, point_id, created_at) VALUES (99, 'm', ?, '2026-11-15T10:00:00')", (a,))
    conn.commit()
    got = point_metrics.report(conn, "2026-11-01", "2026-11-30")
    alpha, beta = (next(r for r in got["rows"] if r["id"] == p) for p in (a, b))
    assert alpha["received"] == 3 and alpha["pieces"] == 12 and alpha["issued"] == 1 and alpha["handed"] == 1
    assert alpha["to_arrive"] == {"n": 3, "avg": 28.0, "med": 24.0}
    assert alpha["to_end"] == {"n": 2, "avg": 6.0, "med": 6.0}
    assert alpha["outcomes"] == {"issued": 1, "oth": 1, "cancel": 0, "return": 0, "open": 1} and alpha["share"]["oth"] == 33.3
    assert beta["received"] == 1 and beta["to_end"]["med"] == 5.0 and beta["share"]["cancel"] == 100.0
    assert got["total"]["received"] == 4 and got["total"]["to_arrive"]["n"] == 4 and got["total"]["outcomes"]["cancel"] == 1
    empty = point_metrics.report(conn, "2026-12-01", "2026-12-31", a)
    assert empty["rows"][0]["to_arrive"]["med"] is None and empty["rows"][0]["share"]["oth"] is None
    page = owner.get("/points/payouts/analytics?from=2026-11-01&to=2026-11-30").get_data(as_text=True)
    assert 'class="is-active" aria-current="page">Аналитика точек</a>' in page and "data-metrics-total" in page
    assert re.search(r'data-point="%d">.*?data-to-arrive>.*?24\.0 ч' % a, page, re.S)
    page = owner.get("/points/payouts/analytics?from=2026-12-01&to=2026-12-31&p=%d" % a).get_data(as_text=True)
    assert "нет данных" in page and "data-metrics-total" not in page
    for word in ("закуп", "Закуп", "₽"):
        assert word not in page.split('<div class="page')[1]
    assert 'href="/points/payouts/analytics"' in owner.get("/points/payouts").get_data(as_text=True)


def test_only_the_office_with_the_money_right_sees_the_analytics(tmp_path, clock):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    assert staff(app, conn, "pavel", "partner").get("/points/payouts/analytics").status_code == 200
    for login_, role in (("chatty", "chats"), ("vera", "viewer")):
        assert staff(app, conn, login_, role).get("/points/payouts/analytics").status_code == 403, role
    assert mine.get("/points/payouts/analytics").status_code == 403
