"""One look of dates, money, phones and percents; empty states, errors under the field, the panel's own confirmation, «Нет связи»
(lane 6B, 2026-10-21; docs/design.md §4, §5, §6, §7)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from panel_helpers import login, make_app, post
from tires_sync import order_points as points, orders, store

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-10-21T12:00:00"


@pytest.fixture
def env(tmp_path):
    from datetime import datetime
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 21, 9, 0, 0))          # 12:00 in Moscow
    return app, app.jinja_env


def f(env, name, *args):
    with env[0].app_context():                                                    # the dates ask the panel's clock what «this year» is
        return env[1].filters[name](*args)


# --- the filters on the edges ------------------------------------------------------------------------------------------------------

def test_money_and_its_delta_on_empty_zero_and_negative(env):
    nb = " "
    assert f(env, "money", 12345.6) == "12" + nb + "346" + nb + "₽"
    assert f(env, "money", 0) == "0" + nb + "₽" and f(env, "money", "0") == "0" + nb + "₽"
    assert f(env, "money", -1200) == "−1" + nb + "200" + nb + "₽"                          # a real minus
    for empty in (None, "", "   ", "abc", float("nan"), True):
        assert f(env, "money", empty) == "—", empty
    assert f(env, "money_delta", 1200) == "+1" + nb + "200" + nb + "₽" and f(env, "money_delta", -1200) == "−1" + nb + "200" + nb + "₽"
    assert f(env, "money_delta", 0.4) == "0" + nb + "₽" and f(env, "money_delta", None) == "—"


def test_pct_and_phone_on_empty(env):
    assert f(env, "pct", 12.345) == "12,3" and f(env, "pct", 5, 2) == "5,00" and f(env, "pct", -3.5) == "−3,5"
    assert f(env, "pct", None) == "—" and f(env, "pct", "") == "—" and f(env, "pct", 0) == "0,0"
    assert f(env, "phone", "89001112233") == "+7 900 111-22-33" and f(env, "phone", "+79001112233") == "+7 900 111-22-33"
    assert f(env, "phone", None) == "—" and f(env, "phone", "") == "—" and f(env, "phone", "доб. 12") == "доб. 12"


def test_dates_in_moscow_time_by_one_rule(env):
    assert f(env, "dt", "2026-10-20T12:00:05") == "20.10.2026 12:00:05"
    assert f(env, "dt", "2026-10-20 12:00") == "20.10.2026 12:00"
    assert f(env, "dt_utc", "2026-10-20T21:30:00") == "21.10.2026 00:30:00"                # UTC → Moscow across midnight
    assert f(env, "dt_utc", "2025-12-31T22:00:00") == "01.01.2026 01:00:00"                # and across a year
    assert f(env, "short_dt", "2026-10-20T12:00:05") == "20.10 12:00" and f(env, "short_dt", "2025-10-20T12:00:05") == "20.10.2025 12:00"
    assert f(env, "short_dt_utc", "2026-10-20T21:30:00") == "21.10 00:30"
    assert f(env, "day", "2026-10-20") == "20.10" and f(env, "day", "2025-10-20") == "20.10.2025"
    assert f(env, "dt", "2026-10-20") == "20.10.2026" and f(env, "dt_utc", "2026-10-20") == "20.10.2026"   # a calendar day is never shifted
    for name in ("dt", "dt_utc", "short_dt", "short_dt_utc", "day"):
        assert f(env, name, None) == "—" and f(env, name, "") == "—", name
        assert f(env, name, "когда-то") == "когда-то", name                                # not a date: shown as it is
    assert f(env, "hm", "2026-10-20T07:05:00") == "07:05" and f(env, "hm", "2026-10-20") == ""


def test_the_journal_shows_both_clocks_in_moscow_time(env):
    with env[0].app_context():
        audit_dt = env[1].globals["audit_dt"]
        assert audit_dt({"action": "settings_confirm", "author": "owner", "at": "2026-10-20T21:30:00"}) == "21.10.2026 00:30:00"     # UTC
        assert audit_dt({"action": "chat_assign", "author": "owner", "at": "2026-10-20T21:30:00"}) == "20.10.2026 21:30:00"         # Moscow
        assert audit_dt({"action": "ids_bound", "author": "ids-heal", "at": "2026-10-20T10:00:00"}) == "20.10.2026 10:00:00"
        assert audit_dt({"action": "ids_bound", "author": "same_tire", "at": "2026-10-20T10:00:00"}) == "20.10.2026 13:00:00"
        assert audit_dt({"action": "account_live", "author": "mirror-go-live", "at": "2026-10-20T10:00:00"}) == "20.10.2026 10:00:00"
        assert audit_dt({"action": "account_live", "author": "owner", "at": "2026-10-20T10:00:00"}) == "20.10.2026 13:00:00"
        assert audit_dt({"action": "x", "author": "", "at": None}) == "—"


def test_no_template_slices_a_date_by_hand():
    found = []
    for path in (ROOT / "src" / "tires_sync" / "panel" / "templates").glob("*.html"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"(_date|_at|\.at|created|started)\s*\)?\[\s*\d*\s*:\s*\d+\s*\]", line) and path.name not in ("dashboard.html", "feed_update.html"):
                found.append("%s:%d" % (path.name, n))
    assert found == []


def test_the_history_shows_utc_records_in_moscow_time(tmp_path):
    from datetime import datetime
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 20, 21, 30, 0))
    conn = store.connect(app.config["DB_PATH"])
    store.write_audit(conn, "2026-10-20T21:30:00", "owner", "settings_confirm", {"draft": 1, "to": 2})
    conn.commit()
    client = app.test_client()
    login(client)
    page = client.get("/history").get_data(as_text=True)
    assert "<time>21.10.2026 00:30:00</time>" in page and "2026-10-20 / 21:30" not in page


# --- empty states ----------------------------------------------------------------------------------------------------------------

def test_empty_states_are_one_macro(tmp_path):
    app = make_app(tmp_path)
    with app.test_request_context("/"):
        from flask import render_template_string
        html = render_template_string('{% from "_ui.html" import empty %}{{ empty("inbox", "Пусто здесь", "Почему и что сделать", "/x", "Сделать") }}')
    assert '<div class="empty"><div class="empty-icon">' in html and "<h3>Пусто здесь</h3>" in html and "<p>Почему и что сделать</p>" in html
    assert '<a class="btn btn--secondary btn--sm" href="/x">Сделать</a>' in html
    client = app.test_client()
    login(client)
    for url, title in (("/alerts", "Новых уведомлений нет"), ("/deleted", "Удалённых нет")):
        page = client.get(url).get_data(as_text=True)
        assert 'class="empty"' in page and "<h3>%s</h3>" % title in page, url


def test_the_public_page_of_a_selection_says_so_when_it_is_empty(tmp_path):
    app = make_app(tmp_path)
    with app.test_request_context("/"):
        from flask import render_template
        html = render_template("share_public.html", link={"note": "", "created_label": "21.10.2026 12:00", "show_price": False}, items=[], token="t")
    assert "<h3>Сейчас в подборке нет шин</h3>" in html and "Напишите менеджеру" in html
    css = (ROOT / "src" / "tires_sync" / "panel" / "static" / "share.css").read_text(encoding="utf-8")
    assert ".empty{" in css and ".empty-icon{" in css


# --- errors under the field, the green «Готово» -----------------------------------------------------------------------------------

def test_a_wrong_phone_of_an_order_is_named_under_its_field(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    card = orders.create_card(conn, {"client_name": "Иван"}, NOW, "owner")
    conn.commit()
    client = app.test_client()
    tok = login(client)
    page = post(client, "/orders/%d/save" % card, tok, client_name="Иван", client_phone="12").get_data(as_text=True)
    assert 'data-field-error="client_phone"' in page and "Телефон клиента: от 10 до 15 цифр." in page
    page = client.get("/orders/%d" % card).get_data(as_text=True)
    r = post(client, "/orders/%d/save" % card, tok, client_name="Иван", client_phone="+7 900 111-22-33")
    assert "flash flash--ok" in client.get(r.headers["Location"]).get_data(as_text=True)


def test_a_point_error_names_the_field_of_its_own_form(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    pid = points.save_point(conn, {"name": "Бета", "channel": "vk"})
    conn.commit()
    client = app.test_client()
    tok = login(client)
    r = post(client, "/connections/points/save", tok, id=str(pid), name="Бета", channel="vk", manager_phone="12")
    page = client.get(r.headers["Location"]).get_data(as_text=True)
    assert 'data-field-error="manager_phone@%d"' % pid in page
    r = post(client, "/connections/points/save", tok, name="", channel="vk")
    assert 'data-field-error="name@new"' in client.get(r.headers["Location"]).get_data(as_text=True)


# --- the confirmation window, «Нет связи», the word «заказ» -----------------------------------------------------------------------

JS_HARNESS = r"""
const fs = require("fs");
const handlers = {doc: {}, win: {}}, created = [];
const on = (box) => (t, f, cap) => (box[t] = box[t] || []).push(f);
const node = (tag) => { const n = {tag, children: [], attrs: {}, classList: {set: new Set(), add(c) { this.set.add(c); }, remove(c) { this.set.delete(c); },
  contains(c) { return this.set.has(c); }, toggle(c, v) { v ? this.set.add(c) : this.set.delete(c); }}, set innerHTML(v) { this._html = v; },
  setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k]; }, appendChild(c) { this.children.push(c); },
  querySelector(s) { return (this._q = this._q || {})[s] = (this._q[s] || node(s)); }, focus() {}}; created.push(n); return n; };
global.document = {addEventListener: on(handlers.doc), querySelectorAll: () => [], querySelector: () => null, getElementById: () => null,
  body: node("body"), documentElement: node("html"), createElement: node, hidden: false, cookie: ""};
global.window = {addEventListener: on(handlers.win), location: {}, matchMedia: () => ({matches: false, addEventListener() {}}), fetch: () => new Promise(() => {}), FormData: function () {}, confirm: () => { throw new Error("window.confirm"); }};
global.location = {}; global.localStorage = {getItem: () => null, setItem() {}}; global.navigator = {};
global.MutationObserver = function () { return {observe() {}}; }; global.Event = function (t) { this.type = t; };
global.setTimeout = () => 0; global.clearTimeout = () => {};
try { eval(fs.readFileSync(process.argv[2], "utf8")); } catch (e) {}
const form = {dataset: {}, attrs: {"data-confirm": "Заказ уйдёт в «Удалённые».", "data-confirm-title": "Удалить заказ №7?", "data-confirm-ok": "Удалить заказ"},
  matches: (s) => s === "form[data-confirm]", getAttribute(k) { return this.attrs[k]; }, hasAttribute(k) { return k in this.attrs || k === "data-confirm-danger"; },
  querySelector: () => null, requestSubmit() { form.sent = true; }};
let stopped = false;
const ev = {target: form, submitter: null, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() { stopped = true; }};
(handlers.doc.submit || []).forEach((f) => { try { f(ev); } catch (e) { console.log(JSON.stringify({error: e.message})); } });
const win = created.find((n) => n.classList.contains("is-open"));
const out = {held: ev.defaultPrevented && stopped && !form.sent, opened: !!win};
const go = win && win.querySelector("[data-confirm-go]");
out.labels = !!win && win.querySelector("h2").textContent === "Удалить заказ №7?" && go.textContent === "Удалить заказ" && go.className.indexOf("btn--danger") >= 0;
if (go) go.onclick();
out.sent_after_yes = form.sent === true && form.dataset.confirmed === "1";
console.log(JSON.stringify(out));
"""


def test_the_confirmation_is_the_panels_window_not_the_browsers():
    node = shutil.which("node")
    if not node:
        pytest.skip("no node")
    harness = ROOT / "tests" / ".confirm_harness.js"
    try:
        harness.write_text(JS_HARNESS, encoding="utf-8")
        out = subprocess.run([node, str(harness), str(ROOT / "src" / "tires_sync" / "panel" / "static" / "app.js")], capture_output=True, text=True, timeout=30, check=True)
    finally:
        harness.unlink(missing_ok=True)
    assert json.loads(out.stdout.strip().splitlines()[-1]) == {"held": True, "opened": True, "labels": True, "sent_after_yes": True}
    js = (ROOT / "src" / "tires_sync" / "panel" / "static" / "app.js").read_text(encoding="utf-8")
    assert "window.confirm" not in js


def test_no_connection_is_said_in_the_chats_the_bell_and_the_suggestions(tmp_path):
    js = (ROOT / "src" / "tires_sync" / "panel" / "static" / "app.js").read_text(encoding="utf-8")
    bell = (ROOT / "src" / "tires_sync" / "panel" / "static" / "bell.js").read_text(encoding="utf-8")
    live = js[js.index("an open page asks for fresh messages"):js.index("// 11) Заказ: поиск позиции")]
    assert "misses >= 2" in live and "offline(true)" in live and "r.redirected" in live
    assert "Нет связи с панелью: подсказки не загрузились" in js
    assert "Загружаю…" in bell and "Нет связи с панелью" in bell and "is-offline" in bell
    from test_chats_pages import setup
    app, client, tok, conn, account = setup(tmp_path)
    page = client.get("/chats").get_data(as_text=True)
    assert 'id="chats-offline"' in page and "Нет связи с панелью" in page


def test_the_chat_deals_say_order_not_deal():
    text = (ROOT / "src" / "tires_sync" / "chat_deals.py").read_text(encoding="utf-8")
    shown = re.findall(r'(?:OrderError|add_event)\([^)]*?"([^"]*)"', text) + re.findall(r'text = "([^"]*)"', text)
    assert shown and not [s for s in shown if "делк" in s]


def test_the_error_goes_into_the_fields_own_block():
    """Checked in the browser: put after the block, the message took the next cell of the grid (under «Имя клиента», not «Телефон»)."""
    js = (ROOT / "src" / "tires_sync" / "panel" / "static" / "app.js").read_text(encoding="utf-8")
    part = js[js.index("5a) Ошибка формы под полем"):js.index("5b) Форма с data-confirm")]
    assert 'closest(".field")' in part and "box.appendChild(hint)" in part and "aria-invalid" in part and "field.focus()" in part


# --- review round 1 ----------------------------------------------------------------------------------------------------------------

def test_a_note_written_before_the_word_changed_is_not_written_again(tmp_path):
    """«…после закрытия сделки (…)» is in the base already: the same message of the client must not add «…после закрытия заказа (…)»."""
    from tires_sync import chat_deals
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    card = orders.create_card(conn, {"client_name": "Иван"}, NOW, "o")
    orders.add_event(conn, card, NOW, "чаты Авито", "chat", "Клиент написал после закрытия сделки (2026-10-21 11:00)")
    conn.commit()
    chat_deals._note_written_again(conn, card, "2026-10-21T11:00:00", NOW)
    assert len([e for e in orders.list_events(conn, card) if e["text"].startswith("Клиент написал после закрытия")]) == 1
    chat_deals._note_written_again(conn, card, "2026-10-21T11:30:00", NOW)                       # a new message: a new note, the new word
    assert [e["text"] for e in orders.list_events(conn, card) if e["text"].startswith("Клиент написал после закрытия")][0] == (
        "Клиент написал после закрытия заказа (2026-10-21 11:30)")


def test_the_green_ready_on_my_point_and_on_the_sign_in_page(tmp_path):
    app = make_app(tmp_path)
    with app.test_request_context("/"):
        from flask import flash, render_template
        flash("Передано, ответственный — Партнёр. Заказ №4.", "ok")
        html = render_template("mypoint.html", point=None, cards=[], choose=True, points=[], user={"role": "owner"}, handoff_token="a" * 24,
                               handoff_error="", handoff_form={})
    assert 'class="flash flash--ok"' in html
    client = app.test_client()
    with client.session_transaction() as session:
        session["_flashes"] = [("ok", "Готово: войдите с логином «ivan» и своим паролем.")]
    assert 'class="flash flash--ok"' in client.get("/login").get_data(as_text=True)


def test_the_error_of_a_folded_field_opens_its_block_and_the_cancel_of_a_danger_has_the_focus():
    js = (ROOT / "src" / "tires_sync" / "panel" / "static" / "app.js").read_text(encoding="utf-8")
    part = js[js.index("5a) Ошибка формы под полем"):js.index("5b) Форма с data-confirm")]
    assert 'field.closest("details")' in part and ".open = true" in part
    confirm = js[js.index("5b) Форма с data-confirm"):js.index("// 6) Форма с data-once")]
    assert "data-close-modal" not in confirm                                      # its «Отмена» closes this window only
    assert "danger ? cancel : go" in confirm and "delete form.dataset.confirmed" in confirm and "setTimeout" in confirm
