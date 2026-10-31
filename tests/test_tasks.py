"""«Задачи» (the owner 2026-10-31; tasks.py, panel/routes_tasks.py): the kinds and the text, to whom (the owner, the office, a manager of
a point), with an order or without; the page with «Мне», «Поставил я», «Все» (the office only), the filters, a late task red, the counter of
the menu; a point sees, sets and closes only its own — checked on the server; the observer and the client set none; the order's journal
gets a grey line when a task is set, done or cancelled; a merge keeps the kind and the text; the old rows are «перезвонить»; a task is no
alert and sends nothing."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from panel_helpers import login, make_app, post
from test_mypoint import manager
from tires_sync import order_alerts, orders, store, tasks, users

NOW = "2026-10-23T12:00:00"                                       # Moscow; the panel clock below is UTC
PASSWORD = "пароль-для-теста-1"


def setup(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 23, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, "2026-10-01T10:00:00")
    users.add_user(conn, "zaman", PASSWORD, NOW, role="partner", display_name="Заман")
    alpha = conn.execute("INSERT INTO point (name) VALUES ('Альфа')").lastrowid
    beta = conn.execute("INSERT INTO point (name) VALUES ('Бета')").lastrowid
    conn.commit()
    return app, client, tok, conn, alpha, beta


def as_user(app, conn, login_):
    other = app.test_client()
    with other.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = login_, store.get_session_epoch(conn, login_), "tok"
    return other


def card(conn, point_id=None, phone="+79001112233"):
    card_id = orders.create_card(conn, {"client_name": "Иван", "client_phone": phone, "point_id": str(point_id) if point_id else ""},
                                 NOW, "owner")
    conn.commit()
    return card_id


def new(client, tok="tok", **form):
    data = dict(kind="other", title="Проверить остаток", date="2026-10-24", time="10:00", back="/tasks")
    data.update(form)
    return post(client, "/tasks/new", tok, **data)


def open_ids(conn):
    return [r[0] for r in conn.execute("SELECT id FROM callback_task WHERE done_at IS NULL AND cancelled_at IS NULL ORDER BY id")]


# ---- set: the kinds, to whom, with an order or without ------------------------------------------------------------------------------

def test_a_task_to_myself_to_zaman_and_to_a_point_with_an_order_and_without(tmp_path):
    app, client, tok, conn, alpha, beta = setup(tmp_path)
    manager(app, conn, alpha)
    order_id = card(conn, alpha)
    assert new(client, tok).status_code == 302                                                  # without an order, to myself
    assert new(client, tok, kind="order", title="Оформить у поставщика", assignee="zaman", order_id=str(order_id)).status_code == 302
    assert new(client, tok, kind="oth", title="", assignee="gamma", order_id=str(order_id)).status_code == 302
    rows = conn.execute("SELECT kind, title, assignee, order_id, created_by FROM callback_task ORDER BY id").fetchall()
    assert rows == [("other", "Проверить остаток", "owner", None, "owner"), ("order", "Оформить у поставщика", "zaman", order_id, "owner"),
                    ("oth", "", "gamma", order_id, "owner")]
    assert new(client, tok, assignee="stranger").status_code == 302 and len(open_ids(conn)) == 3       # nobody of the panel: refused
    assert new(client, tok, kind="nonsense").status_code == 302 and len(open_ids(conn)) == 3
    assert new(client, tok, kind="other", title="").status_code == 302 and len(open_ids(conn)) == 3    # «Другое» needs its text
    assert new(client, tok, kind="callback").status_code == 302 and len(open_ids(conn)) == 3           # whom to call: nobody


def test_the_page_tabs_filters_late_and_the_counter_of_the_menu(tmp_path):
    app, client, tok, conn, alpha, beta = setup(tmp_path)
    zaman = as_user(app, conn, "zaman")
    new(client, tok, title="Моя на завтра")                                                     # to me, tomorrow
    tasks.add(conn, kind="other", title="Моя просроченная", due_at="2026-10-23T12:30", note="", now=NOW, author="zaman", assignee="owner")
    new(client, tok, title="Заману", assignee="zaman", kind="order")
    html = client.get("/tasks").get_data(as_text=True)
    assert "Моя на завтра" in html and "Моя просроченная" not in html.split("Моя на завтра")[1] and "Заману" not in html   # «Мне», by time
    app.config["NOW"] = lambda: datetime(2026, 10, 23, 9, 45, 0)                                # 12:45 in Moscow: 12:30 is late
    html = client.get("/tasks").get_data(as_text=True)
    assert 'class="is-late"' in html and html.index("Моя просроченная") < html.index("Моя на завтра")
    assert '<span class="sb-count sb-count--red" title="Мои: просроченные и на сегодня">1</span>' in html     # late + today: 1
    assert "Заману" in client.get("/tasks?tab=by_me").get_data(as_text=True) and "Моя просроченная" not in client.get("/tasks?tab=by_me").get_data(as_text=True)
    everything = client.get("/tasks?tab=all").get_data(as_text=True)
    assert all(t in everything for t in ("Моя на завтра", "Моя просроченная", "Заману"))
    assert "Заману" in client.get("/tasks?tab=all&to=zaman").get_data(as_text=True) and "Моя на завтра" not in client.get("/tasks?tab=all&to=zaman").get_data(as_text=True)
    assert "Моя на завтра" not in client.get("/tasks?tab=all&due=today").get_data(as_text=True)
    assert "Заману" in client.get("/tasks?tab=all&kind=order").get_data(as_text=True) and "Моя на завтра" not in client.get("/tasks?tab=all&kind=order").get_data(as_text=True)
    assert "Моя просроченная" in client.get("/tasks?tab=all&due=overdue").get_data(as_text=True)
    assert "Заману" in zaman.get("/tasks").get_data(as_text=True)


def test_done_with_a_result_and_cancel_close_it_once(tmp_path):
    app, client, tok, conn, alpha, beta = setup(tmp_path)
    order_id = card(conn)
    new(client, tok, order_id=str(order_id), kind="callback", title="")
    new(client, tok, title="Вторая")
    first, second = open_ids(conn)
    assert post(client, "/tasks/%d/done" % first, tok, result="Перезвонил, берёт").status_code == 302
    assert post(client, "/tasks/%d/cancel" % second, tok).status_code == 302 and open_ids(conn) == []
    assert post(client, "/tasks/%d/cancel" % first, tok).status_code == 302                     # closed stays closed
    assert conn.execute("SELECT done_by, cancelled_at FROM callback_task WHERE id = ?", (first,)).fetchone() == ("owner", None)
    assert post(client, "/tasks/999/done", tok).status_code == 404


# ---- the order's journal; no alert; nothing sent -------------------------------------------------------------------------------------

def test_the_order_journal_gets_a_grey_line_when_set_done_and_cancelled_and_nothing_is_an_alert(tmp_path):
    app, client, tok, conn, alpha, beta = setup(tmp_path)
    order_id = card(conn)
    new(client, tok, order_id=str(order_id), kind="order", title="Оформить", assignee="zaman")
    new(client, tok, order_id=str(order_id), kind="callback", title="")
    first, second = open_ids(conn)
    post(client, "/tasks/%d/done" % first, tok, result="Оформлено")
    post(client, "/tasks/%d/cancel" % second, tok)
    lines = [(e["kind"], e["level"], e["text"]) for e in orders.list_events(conn, order_id) if e["kind"] == "task"]
    texts = [t for _k, _l, t in lines]
    assert all(level == "grey" for _k, level, _t in lines) and len(lines) == 4
    assert any(t.startswith("Задача: Оформить заказ: Оформить — Заман, до 24.10 10:00") for t in texts)
    assert "Задача выполнена: Оформить заказ: Оформить — Оформлено" in texts and "Задача отменена: Перезвонить" in texts
    app.config["NOW"] = lambda: datetime(2026, 10, 25, 9, 0, 0)
    assert not [r for r in order_alerts.page_rows(conn, "owner", "2026-10-25T12:00:00") if r.get("callback") or "Задача" in (r.get("text") or "")]
    assert conn.execute("SELECT COUNT(*) FROM chat_send_log").fetchone()[0] == 0


# ---- a point: its own only, checked on the server -----------------------------------------------------------------------------------

def test_a_point_sees_sets_and_closes_only_its_own(tmp_path):
    app, client, tok, conn, alpha, beta = setup(tmp_path)
    gamma = manager(app, conn, alpha)
    delta = manager(app, conn, beta, "delta")
    own, other = card(conn, alpha), card(conn, beta, "+79005556677")
    new(client, tok, title="Офису", assignee="zaman")
    new(client, tok, title="Точке Бета", assignee="delta", order_id=str(other))
    new(client, tok, title="Точке Альфа", assignee="gamma", order_id=str(own))
    office_task, beta_task, alpha_task = open_ids(conn)
    page = gamma.get("/tasks").get_data(as_text=True)
    assert "Точке Альфа" in page and "Точке Бета" not in page and "Офису" not in page
    for tab in ("by_me", "all"):
        page = gamma.get("/tasks?tab=%s" % tab).get_data(as_text=True)
        assert "Точке Бета" not in page and "Офису" not in page, tab
    assert '?tab=all"' not in gamma.get("/tasks").get_data(as_text=True)                     # «Все» is the office's
    assert "Точке Альфа" in gamma.get("/orders/%d" % own).get_data(as_text=True)
    assert gamma.get("/orders/%d" % other).status_code == 403                                  # another point's order: closed as before
    for task_id in (office_task, beta_task):                                                    # another's task by a direct POST: refused
        assert post(gamma, "/tasks/%d/done" % task_id, "tok").status_code == 403
        assert post(gamma, "/tasks/%d/cancel" % task_id, "tok").status_code == 403
    assert sorted(open_ids(conn)) == sorted([office_task, beta_task, alpha_task])
    assert post(gamma, "/tasks/%d/done" % alpha_task, "tok", result="Сделал").status_code == 302 and alpha_task not in open_ids(conn)
    assert new(gamma, assignee="zaman").status_code == 403                                     # to another: refused
    assert new(gamma, order_id=str(other)).status_code == 403                                  # of another point's order: refused
    assert new(gamma, client_id="1").status_code == 403                                        # of a client or a chat: not the point's
    assert new(gamma, title="Себе без заказа").status_code == 302
    assert new(gamma, kind="callback", title="", order_id=str(own)).status_code == 302
    mine = conn.execute("SELECT assignee, created_by, order_id FROM callback_task WHERE created_by = 'gamma' ORDER BY id").fetchall()
    assert mine == [("gamma", "gamma", None), ("gamma", "gamma", own)]
    assert "Себе без заказа" not in delta.get("/tasks").get_data(as_text=True)
    form = gamma.get("/tasks").get_data(as_text=True)
    assert 'value="zaman"' not in form and 'value="gamma"' in form                             # «Кому»: itself only
    assert "/tasks" in gamma.get("/my-point").get_data(as_text=True)                          # the point's menu has «Задачи»


def test_the_observer_and_the_client_set_no_task(tmp_path):
    app, client, tok, conn, alpha, beta = setup(tmp_path)
    users.add_user(conn, "watcher", PASSWORD, NOW, role="viewer")
    users.add_user(conn, "buyer", PASSWORD, NOW, role="client")
    conn.commit()
    new(client, tok, title="Чья-то")
    [task_id] = open_ids(conn)
    watcher, buyer = as_user(app, conn, "watcher"), as_user(app, conn, "buyer")
    for who in (watcher, buyer):
        assert new(who).status_code == 403
        assert post(who, "/tasks/%d/done" % task_id, "tok").status_code == 403
    assert buyer.get("/tasks").status_code == 403
    page = watcher.get("/tasks").get_data(as_text=True)
    assert 'action="/tasks/new"' not in page and "/done" not in page                         # looks, never sets nor closes
    assert open_ids(conn) == [task_id]


# ---- the old rows, the merge -----------------------------------------------------------------------------------------------------

def test_the_old_rows_are_callbacks_and_the_migration_is_idempotent(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE callback_task (id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER, order_id INTEGER, chat_row INTEGER, "
                "due_at TEXT NOT NULL, assignee TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, "
                "created_by TEXT NOT NULL, done_at TEXT, done_by TEXT NOT NULL DEFAULT '', cancelled_at TEXT)")
    old.execute("INSERT INTO callback_task (client_id, due_at, assignee, note, created_at, created_by) VALUES (1, '2026-10-24T10:00', 'owner', "
                "'Подумает', ?, 'owner')", (NOW,))
    old.commit()
    old.close()
    conn = store.connect(path)
    store.connect(path).close()                                                                 # twice: nothing doubles
    task = tasks.get(conn, 1)
    assert (task["kind"], task["title"], task["kind_label"], task["note"]) == ("callback", "", "Перезвонить", "Подумает")
    assert {r[1] for r in conn.execute("PRAGMA table_info(callback_task)")} >= {"kind", "title"}


def test_a_merge_keeps_the_kind_and_the_text_of_the_tasks(tmp_path):
    from tires_sync import order_merge
    app, client, tok, conn, alpha, beta = setup(tmp_path)
    first, second = card(conn, alpha), card(conn, alpha, "+79005556677")
    new(client, tok, order_id=str(first), kind="oth", title="Вернуть на ОТХ")
    stage = conn.execute("SELECT id FROM order_stage WHERE code = 'new'").fetchone()[0]
    merged = order_merge.merge(conn, [first, second], stage, NOW, "owner", client_from=first)
    [task] = tasks.of_place(conn, order_id=merged)
    assert (task["kind"], task["title"]) == ("oth", "Вернуть на ОТХ")
    order_merge.unmerge(conn, merged, NOW, "owner")
    [task] = tasks.of_place(conn, order_id=first)
    assert (task["kind"], task["title"]) == ("oth", "Вернуть на ОТХ")


def test_the_point_gets_the_page_for_a_phone_and_the_office_the_wide_one(tmp_path):
    """The lead 2026-10-31: the point works from a phone — /tasks is device-width, one column, big «Выполнено», «Задачи» in its bottom
    sections with the counter; the office keeps width=1280 and the table."""
    app, client, tok, conn, alpha, beta = setup(tmp_path)
    gamma = manager(app, conn, alpha)
    new(gamma, title="Себе", date="2026-10-23", time="13:00")                                # due today: counted
    page = gamma.get("/tasks").get_data(as_text=True)
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in page and "pt-tasks-page" in page
    assert 'class="card pt-task"' in page and 'class="btn btn--primary btn--lg">Выполнено' in page and "<table" not in page.split("pt-tasks")[1].split("</article>")[0]
    nav = page[page.index("data-point-nav"):].split("</nav>")[0]
    assert 'href="/tasks" class="is-active"' in nav and "<b>1</b>" in nav
    office = client.get("/tasks").get_data(as_text=True)
    assert '<meta name="viewport" content="width=1280">' in office and 'class="table"' in office and "pt-tasks-page" not in office


def test_the_review_tails_whom_to_call_a_points_task_of_another_point_the_deleted_order_and_the_journal(tmp_path):
    from tires_sync import trash
    from tires_sync.panel import texts
    app, client, tok, conn, alpha, beta = setup(tmp_path)
    manager(app, conn, alpha)
    own, other = card(conn, alpha), card(conn, beta, "+79005556677")
    new(client, tok, order_id=str(own), kind="callback", title="")
    assert "Иван" in client.get("/tasks").get_data(as_text=True) and "111-22-33" in client.get("/tasks").get_data(as_text=True)   # whom to call
    new(client, tok, order_id=str(other), assignee="gamma", title="Чужой заказ")                    # a point's manager: its point's orders only
    assert conn.execute("SELECT COUNT(*) FROM callback_task WHERE assignee = 'gamma'").fetchone()[0] == 0
    [task_id] = open_ids(conn)
    record = trash.delete_order(conn, own, NOW, "owner")
    assert open_ids(conn) == []                                                                     # the order's tasks go with it
    trash.restore_order(conn, record, NOW, "owner")
    assert open_ids(conn) == [task_id]                                                              # and come back with it
    audit = conn.execute("SELECT action, detail FROM audit WHERE action LIKE 'callback%' ORDER BY id").fetchall()
    import json
    line = texts.audit_text({"action": "callback", "detail": json.loads(audit[0][1])})
    assert line.startswith("Задача №%d «Перезвонить»: заказ №%d" % (task_id, own))
    assert texts.audit_text({"action": "callback", "detail": {"task": 9, "due": "2026-10-24T10:00", "to": "owner", "kind": "other"}}).startswith(
        "Задача №9 «Другое»: без заказа")
    assert texts.audit_text({"action": "callback_done", "detail": {"task": 9, "result": "Готово"}}) == "Задача №9 выполнена: Готово"


def test_the_office_never_gives_a_points_manager_a_chat_a_client_nor_another_points_order(tmp_path):
    """The lead's review: through the office a point would see the name and the phone of a client out of its own orders (CRM is closed to
    the points, the owner 2026-10-29) — a point's manager gets an order of its own point or none, never a chat or a client."""
    from test_inquiry import chat
    app, client, tok, conn, alpha, beta = setup(tmp_path)
    manager(app, conn, alpha)
    own, other = card(conn, alpha), card(conn, beta, "+79005556677")
    h = chat(conn)
    client_id = conn.execute("INSERT INTO client (name, created_at) VALUES ('Чужой клиент', ?)", (NOW,)).lastrowid
    conn.commit()
    for form in ({"chat_row": str(h)}, {"client_id": str(client_id)}, {"order_id": str(other)},
                 {"order_id": str(own), "client_id": str(client_id)}):                           # a client slipped in beside its own order
        assert new(client, tok, assignee="gamma", **form).status_code == 302, form
        assert conn.execute("SELECT COUNT(*) FROM callback_task WHERE assignee = 'gamma'").fetchone()[0] == 0, form
    assert new(client, tok, assignee="gamma", order_id=str(own)).status_code == 302
    assert new(client, tok, assignee="gamma").status_code == 302
    assert conn.execute("SELECT order_id, client_id FROM callback_task WHERE assignee = 'gamma' ORDER BY id").fetchall()[1] == (None, None)
    assert new(client, tok, assignee="zaman", chat_row=str(h), kind="callback", title="").status_code == 302    # the office: as before
    assert conn.execute("SELECT COUNT(*) FROM callback_task WHERE assignee = 'zaman' AND chat_row = ?", (h,)).fetchone()[0] == 1


def test_a_direct_post_of_a_point_with_a_chat_is_refused(tmp_path):
    from test_inquiry import chat
    app, client, tok, conn, alpha, beta = setup(tmp_path)
    gamma = manager(app, conn, alpha)
    h = chat(conn)
    assert new(gamma, chat_row=str(h), kind="callback", title="").status_code == 403
    assert conn.execute("SELECT COUNT(*) FROM callback_task").fetchone()[0] == 0
