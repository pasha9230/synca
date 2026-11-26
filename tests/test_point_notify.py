"""«Уведомления точек» and «Оповещение» of a point (the owner 2026-11-25, plan parts D and E; point_notify, routes_point_notify): on by
default; the point switches its own off only with the name of who does it, the office switches any; every switch and every action on
«Оповещение» is a record the office reads by point and days; «Разрешите уведомления» at most 3 times a calendar day per person, 2 hours
apart; «Оповещение» shows the last week, the return today («Есть возвраты!») and the office's tasks (a red «!» from their day on), both
views, its own point only."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from panel_helpers import login, make_app, post
from test_mypoint import manager
from test_point_new_view import flag
from tires_sync import order_points as points, point_notify as pn, store, tasks, users
from tires_sync.panel import routes_mypoint, routes_point_notify

NOW = "2026-11-26T10:00:00"                              # a Thursday: the last week is 16.11–22.11


@pytest.fixture
def clock(monkeypatch):
    moment = {"now": NOW}
    monkeypatch.setattr(routes_point_notify, "local_iso", lambda: moment["now"])
    monkeypatch.setattr(routes_mypoint, "local_iso", lambda: moment["now"])
    return moment


@pytest.fixture(params=["old", "new"])
def view(request):
    return request.param


def env(tmp_path, view="old"):
    app = make_app(tmp_path)
    owner = app.test_client()
    tok = login(owner)
    conn = store.connect(app.config["DB_PATH"])
    if view == "new":
        flag(conn, "all")
    a = points.save_point(conn, {"name": "Альфа", "channel": ""})
    b = points.save_point(conn, {"name": "Бета", "channel": ""})
    mine = manager(app, conn, a)                                   # gamma, the manager of Альфа
    return app, owner, tok, conn, a, b, mine


def office_task(conn, assignee="gamma", due="2026-11-26T15:00", author="admin", title="Пересчитать шины"):
    return tasks.add(conn, kind="other", title=title, due_at=due, note="", now="2026-11-25T09:00:00", author=author, assignee=assignee)


# ---- D: on by default, who switched them, the records -----------------------------------------------------------------------------------

def test_on_by_default_the_point_switches_off_only_with_a_name_and_both_switches_are_recorded(tmp_path, clock, view):
    app, owner, tok, conn, a, b, mine = env(tmp_path, view)
    assert pn.enabled(conn, a) and mine.get("/my-point/feed").get_json()["notify"]["on"] is True
    page = mine.get("/my-point").get_data(as_text=True)
    assert 'data-notify-switch-state="on"' in page and 'action="/my-point/notify"' in page and 'name="who" required' in page
    post(mine, "/my-point/notify", "tok", on="0", who="", back="/my-point")
    assert pn.enabled(conn, a) and not pn.history(conn, a)                         # no name: refused, nothing written
    assert "Впишите, кто выключает" in mine.get("/my-point").get_data(as_text=True)
    post(mine, "/my-point/notify", "tok", on="0", who="Иван", reason="мешает звук", back="/my-point")
    assert not pn.enabled(conn, a) and pn.enabled(conn, b)                          # the other point is untouched
    assert mine.get("/my-point/feed").get_json()["notify"] == {"on": False, "ask": 0}
    page = mine.get("/my-point").get_data(as_text=True)
    assert 'data-notify-switch-state="off"' in page and "Иван" in page
    post(mine, "/my-point/notify", "tok", on="1", who="", back="/my-point")
    rows = pn.history(conn, a)
    assert [r["kind"] for r in rows] == ["notify_on", "notify_off"]
    off = rows[1]
    assert off["who"] == "Иван" and off["text"] == "мешает звук" and off["author"] == "gamma" and off["point"] == "Альфа" and off["at"] == NOW


def test_the_office_switches_a_point_and_reads_the_history_by_point_days_and_kind(tmp_path, clock):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    page = owner.get("/points/notify").get_data(as_text=True)
    assert 'data-notify-point="%d"' % a in page and 'data-state="on"' in page and 'data-switch="off"' in page
    post(owner, "/points/notify", tok, p=str(b), on="0", reason="точка закрыта на ремонт", back="/points/notify")
    assert not pn.enabled(conn, b) and pn.enabled(conn, a)
    clock["now"] = "2026-11-28T12:00:00"
    post(owner, "/points/notify", tok, p=str(b), on="1", back="/points/notify")
    post(mine, "/my-point/notify", "tok", on="0", who="Иван")
    def history(url):
        page = owner.get(url).get_data(as_text=True)
        return page[page.index(">История</h2>"):]
    page = history("/points/notify?p=%d" % b)
    assert "точка закрыта на ремонт" in page and "Иван" not in page                 # one point
    assert page.count('data-history-kind="notify_') == 2
    page = history("/points/notify?from=2026-11-27&to=2026-11-30")
    assert "точка закрыта на ремонт" not in page and "Иван" in page                 # the days
    page = history("/points/notify?kind=notify_off")
    assert 'data-history-kind="notify_on"' not in page and page.count('data-history-kind="notify_off"') == 2


def test_who_may_look_and_switch(tmp_path, clock):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    assert mine.get("/points/notify").status_code == 403                           # a point: never the office's page
    assert post(mine, "/points/notify", "tok", p=str(b), on="0").status_code == 403
    assert not pn.history(conn)
    for role in ("chats", "viewer"):
        users.add_user(conn, role + "1", "pw-12345678", "2026-10-13T12:00:00", role=role)
    conn.commit()
    for login_ in ("chats1", "viewer1"):
        client = app.test_client()
        with client.session_transaction() as session:
            session.update(user=login_, epoch=store.get_session_epoch(conn, login_), csrf="tok")
        assert client.get("/points/notify").status_code == 200                      # the office looks with the usual rights
        status = post(client, "/points/notify", "tok", p=str(a), on="0").status_code
        assert (status == 302) == (login_ == "chats1")                              # the operator switches, the observer only looks
    assert [r["author"] for r in pn.history(conn, a)] == ["chats1"]
    assert post(owner, "/my-point/notify", tok, on="0", who="x").status_code == 403    # the owner not working as a point: the office's page


def test_the_owner_looking_at_a_point_sees_its_state_and_the_link_to_switch(tmp_path, clock, view):
    app, owner, tok, conn, a, b, mine = env(tmp_path, view)
    pn.switch(conn, a, False, NOW, "admin", "Офис")
    page = owner.get("/my-point?p=%d" % a).get_data(as_text=True)
    assert 'data-notify-switch-state="off"' in page and 'href="/points/notify?p=%d"' % a in page and 'action="/my-point/notify"' not in page


# ---- D: «Разрешите уведомления» 3 times a day -------------------------------------------------------------------------------------------

def test_three_asks_a_calendar_day_two_hours_apart_per_person(tmp_path, clock):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    ask = lambda: mine.post("/my-point/notify/ask", data={"csrf": "tok"}).get_json()["ok"]
    assert mine.get("/my-point/feed").get_json()["notify"]["ask"] == 3
    assert ask() is True and ask() is False                                         # the second one too soon
    assert mine.get("/my-point/feed").get_json()["notify"]["ask"] == 0
    for at in ("2026-11-26T12:00:00", "2026-11-26T14:30:00", "2026-11-26T18:00:00"):
        clock["now"] = at
        ask()
    assert conn.execute("SELECT shown FROM point_notify_ask WHERE login = 'gamma' AND day = '2026-11-26'").fetchone()[0] == 3
    assert pn.asks_left(conn, "gamma", "2026-11-26T23:00:00") == 0
    clock["now"] = "2026-11-27T08:00:00"                                            # a new calendar day: three again
    assert mine.get("/my-point/feed").get_json()["notify"]["ask"] == 3 and ask() is True
    users.add_user(conn, "delta", "pw-12345678", "2026-10-13T12:00:00", role="point", point_id=a)
    conn.commit()
    assert pn.asks_left(conn, "delta", "2026-11-27T08:00:00") == 3                  # per person: the other manager of the point


def test_no_ask_while_off_nor_for_the_owner_working_as_the_point(tmp_path, clock):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    pn.switch(conn, a, False, NOW, "admin", "Офис")
    assert mine.post("/my-point/notify/ask", data={"csrf": "tok"}).get_json()["ok"] is False
    assert not conn.execute("SELECT 1 FROM point_notify_ask").fetchone()
    pn.switch(conn, a, True, NOW, "admin", "Офис")
    post(owner, "/as-point", tok, point_id=str(a))
    assert owner.get("/my-point/feed").get_json()["notify"] == {"on": True, "ask": 0}   # working as the point: never asked
    assert owner.post("/my-point/notify/ask", data={"csrf": tok}).get_json()["ok"] is False


def test_the_browsers_answer_is_recorded_once_a_day(tmp_path, clock):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    for _ in range(2):
        mine.post("/my-point/notify/answer", data={"csrf": "tok", "permission": "granted"})
    mine.post("/my-point/notify/answer", data={"csrf": "tok", "permission": "default"})
    assert [r["kind"] for r in pn.history(conn, a)] == ["allowed"]


NOTIFY_JS = Path(routes_mypoint.__file__).parent / "static" / "notify.js"
HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const [file, on, ask, server, typing] = process.argv.slice(2);
let step = 0, tick = null, shown = 0, posts = [], timers = [], boxes = [], field = typing === "typing";
const feeds = [[], [{key: "order-1", title: "Новый заказ", text: "", href: "/my-point"}]];
function Note() { shown++; }
Note.permission = ask === "0" ? "granted" : "default";
Note.requestPermission = () => Promise.resolve("granted");
function el() { return {setAttribute() {}, appendChild() {}, addEventListener() {}, remove() {}}; }
const ctx = {console, JSON, Math, Number, String, Object, Date, Notification: Note, FormData: function () { this.append = () => {}; },
  document: {querySelector: () => null, addEventListener() {}, readyState: "complete", get activeElement() { return {matches: () => field}; },
             currentScript: {getAttribute: n => n === "data-csrf" ? "tok" : null}, createElement: el, body: {appendChild: b => boxes.push(b)}},
  localStorage: {data: {}, getItem(k) { return k in this.data ? this.data[k] : null; }, setItem(k, v) { this.data[k] = String(v); }}, sessionStorage: {getItem: () => null, setItem() {}, removeItem() {}},
  location: {pathname: "/chats", href: "https://example.com/chats"},
  fetch: (url, o) => { if (o && o.method === "POST") { posts.push(url); return Promise.resolve({ok: true, redirected: false, json: () => Promise.resolve({ok: server === "yes"})}); }
    return Promise.resolve({ok: true, redirected: false, json: () => Promise.resolve({who: "p", items: feeds[step++] || [], notify: {on: on === "on", ask: Number(ask)}})}); },
  setInterval: fn => { tick = fn; }, setTimeout: fn => { timers.push(fn); return timers.length; }, addEventListener() {}, focus() {}};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(file, "utf8"), ctx);
const settle = () => new Promise(r => setImmediate(r));
const later = () => { const t = timers; timers = []; t.forEach(fn => fn()); };
(async () => { await settle(); await settle(); tick(); await settle(); await settle();
  later(); await settle(); await settle();                          // the ask waits a little; typing: it waits more
  const before = posts.length; field = false; later(); await settle(); await settle();
  process.stdout.write(JSON.stringify({shown, posts, boxes: boxes.length, before})); })();
"""


def run_js(tmp_path, on="on", ask="0", server="no", typing=""):
    node = shutil.which("node")
    if not node:
        pytest.skip("no node to run notify.js")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    out = subprocess.run([node, str(harness), str(NOTIFY_JS), on, ask, server, typing or "-"], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


@pytest.mark.parametrize("on, shown", [("on", 1), ("off", 0)])
def test_notify_js_is_silent_while_the_notifications_are_off(tmp_path, on, shown):
    out = run_js(tmp_path, on)
    assert out["shown"] == shown and out["posts"] == [] and out["boxes"] == 0          # no ask: the server said none is due


@pytest.mark.parametrize("server, typing, boxes, before", [("yes", "", 1, 1), ("no", "", 0, 1), ("yes", "typing", 1, 0)])
def test_notify_js_shows_the_ask_only_when_the_server_counts_it_and_never_while_typing(tmp_path, server, typing, boxes, before):
    out = run_js(tmp_path, ask="2", server=server, typing=typing)
    assert out["posts"] == ["/my-point/notify/ask"] and out["boxes"] == boxes        # once per page load; the server's «ok» shows it
    assert out["before"] == before                                                   # typing: not asked until the field is left


def test_notify_js_asks_only_when_calm_and_by_the_server_s_count():
    js = NOTIFY_JS.read_text(encoding="utf-8")
    assert '"/my-point/notify/ask"' in js and "if (!got || !got.ok) return;" in js       # the server counts the asks
    assert "function calm() { return !typing() && !document.querySelector(BUSY); }" in js   # never while one types or a window is open
    assert js.index("if (quiet) return;") < js.index("ring();\n      added.slice")         # off: no sound, no notification
    assert "details.notify-switch[open]" in js                                             # the open switch is not wiped by a refresh


# ---- E: «Оповещение» ---------------------------------------------------------------------------------------------------------------------

def test_the_week_counts_what_came_was_handed_out_and_the_clients_handed_over(tmp_path, clock, monkeypatch, view):
    app, owner, tok, conn, a, b, mine = env(tmp_path, view)
    for order, at, pid, undone in ((1, "2026-11-16T10:00:00", a, None), (2, "2026-11-22T20:00:00", a, None), (3, "2026-11-23T09:00:00", a, None),
                                   (4, "2026-11-18T10:00:00", b, None), (5, "2026-11-19T10:00:00", a, "2026-11-19T11:00:00")):
        conn.execute("INSERT INTO order_arrival (order_id, arrived_at, point_id, pieces, undone_at) VALUES (?,?,?,4,?)", (order, at, pid, undone))
    for order, at, pid in ((7, "2026-11-17T10:00:00", a), (8, "2026-11-15T10:00:00", a), (9, "2026-11-17T10:00:00", b)):
        conn.execute("INSERT INTO order_handoff (order_id, message_id, point_id, created_at) VALUES (?,?,?,?)", (order, "m%d" % order, pid, at))
    conn.commit()
    monkeypatch.setattr(routes_mypoint, "_issued", lambda conn, pid, today: [{"at": "2026-11-20T10:00:00"}, {"at": "2026-11-24T10:00:00"}])
    page = mine.get("/my-point/notices").get_data(as_text=True)
    assert 'data-week="received">2<' in page and 'data-week="issued">1<' in page and 'data-week="handed">1<' in page
    assert "16.11" in page and "22.11" in page
    assert ("p2-notices" in page) == (view == "new")


def test_the_return_today_marks_the_button(tmp_path, monkeypatch, view):
    from test_point_oth_sell import setup
    from test_return_day import DAY, agreed
    app, owner, conn, pid, card = setup(tmp_path, view)
    mine = manager(app, conn, pid)
    for module in (routes_point_notify, routes_mypoint):
        monkeypatch.setattr(module, "local_iso", lambda: "2026-11-12T10:00:00")
    assert 'data-returns-mark' not in mine.get("/my-point").get_data(as_text=True)
    agreed(conn, card, DAY)
    for module in (routes_point_notify, routes_mypoint):
        monkeypatch.setattr(module, "local_iso", lambda: DAY + "T08:00:00")      # the day of the return, before the 09:00 notice too
    page = mine.get("/my-point").get_data(as_text=True)
    head = re.search(r"<header.*?</header>", page, re.S).group(0)
    assert "data-returns-mark" in head and pn.RETURNS_MARK in head
    notices = mine.get("/my-point/notices").get_data(as_text=True)
    assert "data-notices-returns" in notices and "5000100" in notices


def test_the_office_task_is_on_the_page_with_a_red_mark_from_its_day_while_open(tmp_path, clock, view):
    app, owner, tok, conn, a, b, mine = env(tmp_path, view)
    users.add_user(conn, "beta1", "pw-12345678", "2026-10-13T12:00:00", role="point", point_id=b)
    conn.commit()
    later = office_task(conn, due="2026-11-27T12:00", title="Завтрашняя")
    own = office_task(conn, author="gamma", title="Своя заметка")                  # set by the point itself: not the office's
    other = office_task(conn, assignee="beta1", title="Чужая точка")
    clock["now"] = "2026-11-25T10:00:00"
    page = mine.get("/my-point/notices").get_data(as_text=True)
    assert 'data-office-task="%d"' % later in page and "Своя заметка" not in page and "Чужая точка" not in page
    assert "data-task-mark" not in mine.get("/my-point").get_data(as_text=True)    # its day has not come
    clock["now"] = "2026-11-27T08:00:00"
    for url in ("/my-point", "/my-point/notices", "/chats"):
        assert "data-task-mark" in mine.get(url).get_data(as_text=True), url
    post(mine, "/my-point/notices/task/%d/read" % later, "tok")
    page = mine.get("/my-point/notices").get_data(as_text=True)
    assert "data-task-read" in page and "data-task-mark" in page                   # read: still open, still marked
    post(mine, "/my-point/notices/task/%d/done" % later, "tok", result="пересчитали")
    assert tasks.get(conn, later)["done_at"] and "data-task-mark" not in mine.get("/my-point").get_data(as_text=True)
    assert [r["kind"] for r in pn.history(conn, a)] == ["task_done", "task_read"]
    assert all(r["task_id"] == later and "Завтрашняя" in r["text"] for r in pn.history(conn, a))
    assert post(mine, "/my-point/notices/task/%d/done" % other, "tok").status_code == 404     # another point's
    assert post(mine, "/my-point/notices/task/%d/read" % own, "tok").status_code == 404       # the point's own
    assert not tasks.get(conn, other)["done_at"]


def test_the_point_sees_its_own_point_only_and_the_office_its_actions(tmp_path, clock):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    task = office_task(conn)
    page = mine.get("/my-point/notices?p=%d" % b).get_data(as_text=True)          # ?p= is the office's: the point stays on its own
    assert "Альфа" in page and "Бета" not in page
    post(mine, "/my-point/notices/task/%d/read" % task, "tok")
    office = owner.get("/points/notify?p=%d" % a).get_data(as_text=True)
    assert 'data-history-kind="task_read"' in office and "Пересчитать шины" in office
    seen = owner.get("/my-point/notices?p=%d" % a).get_data(as_text=True)          # the owner looks: no buttons of the point
    assert 'data-office-task="%d"' % task in seen and "data-task-read-btn" not in seen
    assert post(owner, "/my-point/notices/task/%d/done" % task, tok).status_code == 403
    for word in ("закуп", "Закуп", "прибыл"):
        assert word not in page


def test_the_nav_and_the_head_lead_to_the_notices_page(tmp_path, clock, view):
    app, owner, tok, conn, a, b, mine = env(tmp_path, view)
    page = mine.get("/my-point").get_data(as_text=True)
    head = re.search(r"<header.*?</header>", page, re.S).group(0)
    assert 'href="/my-point/notices"' in head and 'aria-label="Сводки' in head
    assert mine.get("/my-point/notices").status_code == 200 and owner.get("/my-point/notices?p=%d" % a).status_code == 200
    assert 'href="/points/notify"' in owner.get("/orders").get_data(as_text=True)


# ---- «Сводки» (the owner 2026-11-26): the name, «Требует действия сейчас», one block of counts, «Другие точки» ------------------------------

def staged(conn, point_id, code, number, delivery="", entered="2026-11-20T10:00:00", name="Клиент Тест"):
    from tires_sync import orders
    orders.ensure_pipeline(conn, "2026-11-01T10:00:00")
    cid = orders.create_card(conn, {"client_name": name, "point_id": str(point_id)}, "2026-11-01T10:00:00", "чаты")
    stage = conn.execute("SELECT id FROM order_stage WHERE code = ?", (code,)).fetchone()[0]
    conn.execute("UPDATE order_card SET stage_id = ?, entered_at = ?, delivery_date = ?, webmim_number = ? WHERE id = ?",
                 (stage, entered, delivery, number, cid))
    conn.commit()
    return cid


def test_the_page_is_called_summaries_everywhere_and_keeps_its_marks(tmp_path, clock, view):
    app, owner, tok, conn, a, b, mine = env(tmp_path, view)
    page = mine.get("/my-point/notices").get_data(as_text=True)
    assert "<h1>Сводки</h1>" in page and "<title>Сводки" in page and "<h1>Оповещение</h1>" not in page
    work = mine.get("/my-point").get_data(as_text=True)
    head = re.search(r"<header.*?</header>", work, re.S).group(0)
    assert 'aria-label="Сводки' in head and "Оповещения точки" not in work


def test_what_needs_an_action_now_links_the_late_orders_only(tmp_path, clock, view):
    app, owner, tok, conn, a, b, mine = env(tmp_path, view)
    late = staged(conn, a, "moving", "5100001", delivery="2026-11-24")
    staged(conn, a, "moving", "5100002", delivery="2026-11-26")                      # today: not late yet
    long_way = staged(conn, a, "moving", "5100006", entered="2026-11-25T03:00:00")   # no delivery day: 31 hours on the way
    staged(conn, a, "moving", "5100007", entered="2026-11-25T06:00:00")              # 28 hours: not yet
    staged(conn, a, "placed", "5100008", entered="2026-11-10T06:00:00")              # «Заказ оформлен» with no day: the supplier's, not late
    staged(conn, a, "at_point", "5100003", entered="2026-11-10T09:00:00")            # waiting at the point: the panel's own rule, not here
    staged(conn, b, "moving", "5100005", delivery="2026-11-20")                      # another point's
    page = mine.get("/my-point/notices").get_data(as_text=True)
    assert 'data-act-late="%d"' % late in page and 'href="/my-point?tab=moving#order-%d"' % late in page and "2 дня назад" in page
    assert 'data-act-late="%d"' % long_way in page and "31 час" in page
    for other in ("5100002", "5100007", "5100008", "5100003", "5100005"):
        assert other not in page, other
    assert "data-act-stale" not in page and "data-notices-no-returns" not in page


def test_one_block_of_counts_the_week_and_the_month_without_rubles_while_the_income_is_hidden(tmp_path, clock, view):
    app, owner, tok, conn, a, b, mine = env(tmp_path, view)
    for order, at in ((1, "2026-11-17T10:00:00"), (2, "2026-11-25T10:00:00"), (3, "2026-11-02T10:00:00"),
                      (4, "2026-11-01T09:00:00"), (6, "2026-11-01T07:59:00"), (9, "2026-10-31T10:00:00")):   # the month from the 1st, 08:00
        conn.execute("INSERT INTO order_arrival (order_id, arrived_at, point_id, pieces) VALUES (?,?,?,4)", (order, at, a))
    sold = staged(conn, a, "done", "5100010")
    conn.execute("INSERT INTO order_event (order_id, at, author, kind, text) VALUES (?,?,?,?,?)", (sold, "2026-11-18T10:00:00", "gamma", "point", pn_sold()))
    acting = staged(conn, a, "done", "5100011")                                          # the owner working as the point: not the point's sale
    conn.execute("INSERT INTO order_event (order_id, at, author, kind, text) VALUES (?,?,?,?,?)",
                 (acting, "2026-11-18T10:00:00", "Владелец за точку «Альфа»", "point", pn_sold()))
    conn.commit()
    page = mine.get("/my-point/notices").get_data(as_text=True)
    assert 'data-week="received">1<' in page and 'data-month="received">4<' in page and "08:00" in page
    assert 'data-week="oth">1<' in page and 'data-month="oth">1<' in page
    assert "data-summary-money" not in page and "₽" not in page.split("data-notices-week")[1].split("</section>")[0]
    assert "считается с 01.12" in page


def pn_sold():
    from tires_sync import order_report
    return order_report.OTH_SOLD_EVENT


def test_the_rubles_show_while_the_point_sees_its_income(tmp_path, clock):
    from tires_sync import order_report
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    order_report.set_income_shown(conn, True, NOW, "admin")
    conn.commit()
    page = mine.get("/my-point/notices").get_data(as_text=True)
    assert "data-summary-money" in page and 'href="/my-point/income"' in page


def test_other_points_show_names_and_counts_only_and_the_owner_hides_the_tab(tmp_path, clock, view):
    app, owner, tok, conn, a, b, mine = env(tmp_path, view)
    early = staged(conn, b, "at_point", "5100020", delivery="2026-11-10", name="Секретный Клиент")
    conn.execute("INSERT INTO order_arrival (order_id, arrived_at, point_id, pieces) VALUES (?,?,?,4)", (early, "2026-11-11T10:00:00", b))
    slow = staged(conn, b, "at_point", "5100021", delivery="2026-11-10")
    conn.execute("INSERT INTO order_arrival (order_id, arrived_at, point_id, pieces) VALUES (?,?,?,4)", (slow, "2026-11-14T10:00:00", b))
    conn.commit()
    assert "Бета" not in mine.get("/my-point/notices").get_data(as_text=True)          # the own tab: the own point only
    page = mine.get("/my-point/notices?tab=others").get_data(as_text=True)
    row = re.search(r'<tr data-compare-point="%d".*?</tr>' % b, page, re.S).group(0)
    assert "Бета" in row and "<td>2</td>" in row and "50%" in row
    assert 'data-compare-point="%d" data-mine' % a in page
    assert "Секретный" not in page and "5100020" not in page and "₽" not in page.split("data-compare")[1]
    week = mine.get("/my-point/notices?tab=others&period=week").get_data(as_text=True)
    assert "16.11–22.11" in week
    assert post(mine, "/points/notify", "tok", what="compare", on="0").status_code == 403    # never the point
    post(owner, "/points/notify", tok, what="compare", on="")
    hidden = mine.get("/my-point/notices?tab=others").get_data(as_text=True)
    assert "data-compare-table" not in hidden and "data-summary-tabs" not in hidden and "<h1>Сводки</h1>" in hidden
    post(owner, "/points/notify", tok, what="compare", on="1")
    assert "data-compare-table" in mine.get("/my-point/notices?tab=others").get_data(as_text=True)


def staff(app, conn, login_, role):
    users.add_user(conn, login_, "pw-12345678", "2026-10-13T12:00:00", role=role)
    conn.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = login_
        session["epoch"] = store.get_session_epoch(conn, login_)
        session["csrf"] = "tok"
    return client


def test_the_admin_and_the_operator_switch_other_points_and_the_income_the_viewer_does_not(tmp_path, clock):
    from tires_sync import order_report
    from tires_sync.point_summary import compare_shown
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    for login_, role in (("pavel", "partner"), ("chatty", "chats")):
        client = staff(app, conn, login_, role)
        page = client.get("/points/notify").get_data(as_text=True)
        assert "data-switch-compare" in page and "data-switch-income" in page, role
        post(client, "/points/notify", "tok", what="compare", on="")
        assert not compare_shown(conn), role
        post(client, "/points/notify", "tok", what="compare", on="1")
        assert compare_shown(conn), role
        post(client, "/points/notify", "tok", what="income", on="1")
        assert order_report.income_shown(conn), role
        post(client, "/points/notify", "tok", what="income", on="")
        assert not order_report.income_shown(conn), role
    looker = staff(app, conn, "vera", "viewer")
    assert "data-switch-compare" not in looker.get("/points/notify").get_data(as_text=True)
    assert post(looker, "/points/notify", "tok", what="income", on="1").status_code == 403
    assert post(mine, "/points/notify", "tok", what="income", on="1").status_code == 403
    assert not order_report.income_shown(conn) and compare_shown(conn)
