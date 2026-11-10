"""The owner 2026-11-10 of «Рабочее место» of a point (both views): «Включить уведомления» in the head beside «Оповещения» (its state a small
line under the head, none when empty), and the page refreshing itself on news while the point is idle (no «Есть новое. Обновить»)."""
from __future__ import annotations

import re

import pytest

from test_mypoint import manager
from test_point_issued import setup


@pytest.fixture(params=["old", "new"])
def view(request):
    return request.param


def head(page: str) -> str:
    return re.search(r"<header class=\"(?:pt-top mp-head|p2-head)\">.*?</header>", page, re.S).group(0)


def test_notify_button_is_in_the_head_beside_the_alerts(tmp_path, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    page = manager(app, conn, pid).get("/my-point").get_data(as_text=True)
    top = head(page)
    assert page.count("data-notify-enable") == 1 and "data-notify-enable" in top
    alerts, button = top.index('aria-label="Оповещения'), top.index("data-notify-enable")
    between = top[alerts:button]
    assert alerts < button and between.count("<a ") == 0                                    # right after «Оповещения», nothing between
    assert re.search(r'<button type="button" class="[^"]*(btn--secondary|p2-ico)[^"]*" data-notify-enable hidden>Включить уведомления</button>', top)
    assert "data-notify-box" not in page and 'class="mp-notify"' not in page                 # no line of its own over the search
    after = page[page.index("</header>"):]
    assert re.match(r'</header>\n<p class="[^"]*(mp|p2)-notify-state" data-notify-state></p>', after)   # small, under the head


def test_no_notify_button_for_the_owner(tmp_path, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    page = owner.get("/my-point").get_data(as_text=True)
    assert "data-notify-enable" not in page and "data-notify-state" not in page


def test_the_state_line_takes_no_room_when_empty():
    from pathlib import Path
    static = Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static"
    assert ".mp-notify-state:empty{display:none}" in (static / "mypoint.css").read_text(encoding="utf-8")
    css = (static / "point2.css").read_text(encoding="utf-8")
    assert ".p2-notify-state:empty{display:none}" in css
    assert ".p2-head-acts:has(.p2-notify-on:not([hidden])){flex-wrap:wrap}" in css           # a phone: the head wraps, it does not break


# --- the page refreshes itself, only while the point is idle (the owner 2026-11-10) -------------------------------------------------------

NOTIFY_JS = __import__("pathlib").Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static" / "notify.js"


def test_no_line_there_is_news_refresh_on_the_page(tmp_path, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    page = manager(app, conn, pid).get("/my-point").get_data(as_text=True)
    assert "data-notify-fresh" not in page and "Есть новое." not in page and "mp-fresh" not in page
    assert "notify.js" in page


def test_notify_js_looks_for_idleness_before_it_refreshes():
    js = NOTIFY_JS.read_text(encoding="utf-8")
    assert "data-notify-fresh" not in js and "fresh.hidden" not in js
    for event in ('"click"', '"keydown"', '"touchstart"', '"scroll"'):
        assert event in js                                                                   # what counts as the point at work
    assert ".modal-backdrop.is-open" in js and "document.activeElement" in js and '"submit"' in js
    assert "IDLE = 10000" in js and "Date.now() - last < EVERY" in js                       # ~10 s of quiet; once in 30 s at most
    assert js.index("function idle()") < js.index("window.location.replace(url)")
    assert "if (idle() && !soon()) { reload(); return; }" in js
    assert 'window.location.pathname === "/my-point"' in js                                 # the other pages of the point: no refresh


HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const [file, how, path] = process.argv.slice(2);
let clock = 100000, step = 0, timers = [], tick = null, busy = how === "modal", field = how === "focus";
const on = {}, won = {}, session = {}, local = {}, replaced = [];
const feeds = [[], [{key: "order-1", title: "Новый заказ", text: "", href: "/my-point"}]];
if (how === "soon") session["tires.mypoint.reloaded"] = String(clock + 6000);
const ctx = {
  console, JSON, Math, Number, String,
  Date: {now: () => clock},
  document: {querySelector: s => (s.includes(".modal-backdrop") && busy ? {} : null), readyState: "complete",
             activeElement: {matches: () => field}, addEventListener: (n, fn) => { on[n] = fn; }},
  localStorage: {getItem: k => (k in local ? local[k] : null), setItem: (k, v) => { local[k] = String(v); }},
  sessionStorage: {getItem: k => (k in session ? session[k] : null), setItem: (k, v) => { session[k] = String(v); }, removeItem: k => { delete session[k]; }},
  location: {pathname: path, href: "https://example.com" + path + "?tab=moving&q=7001#order-1", replace: u => replaced.push(u)},
  scrollY: 640, scrollTo() {},
  fetch: () => Promise.resolve({ok: true, redirected: false, json: () => Promise.resolve({who: "p", items: feeds[step++] || []})}),
  setInterval: fn => { tick = fn; }, setTimeout: fn => { timers.push(fn); return timers.length; },
  addEventListener: (n, fn) => { won[n] = fn; },
  AudioContext: function () { return {state: "running", currentTime: 0, destination: {}, resume: () => Promise.resolve(),
    createOscillator: () => ({frequency: {}, connect() {}, start() {}, stop() {}}),
    createGain: () => ({gain: {setValueAtTime() {}, exponentialRampToValueAtTime() {}}, connect() {}})}; },
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(file, "utf8"), ctx);
const settle = () => new Promise(r => setTimeout(r, 0));
const later = () => { const t = timers; timers = []; t.forEach(fn => fn()); };
(async () => {
  await settle(); await settle();
  clock += 11000;                                                     // the point has done nothing for 11 s
  if (how === "click") { clock -= 3000; on.click(); clock += 3000; }  // ... but pressed 3 s ago
  if (how === "sending") won.submit({defaultPrevented: false});
  if (how === "asked") won.submit({defaultPrevented: true});          // the panel's window asks first: nothing sent yet
  tick(); await settle(); await settle();
  const first = replaced.length;
  busy = false; field = false; clock += 31000; later();               // the point is idle again
  process.stdout.write(JSON.stringify({first, after: replaced, scroll: session["tires.mypoint.scroll"] || null}));
})();
"""


def run(tmp_path, how, path="/my-point"):
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("no node to run notify.js")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    out = subprocess.run([node, str(harness), str(NOTIFY_JS), how, path], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def test_news_on_an_idle_point_refreshes_the_page_keeping_the_tab_search_and_scroll(tmp_path):
    got = run(tmp_path, "idle")
    assert got["first"] == 1 and got["after"] == ["https://example.com/my-point?tab=moving&q=7001"]   # the same address, a GET
    assert '"y":640' in got["scroll"] and "tab=moving&q=7001" in got["scroll"]


@pytest.mark.parametrize("how", ["click", "modal", "focus", "soon"])
def test_a_busy_point_is_not_refreshed_until_it_is_idle(tmp_path, how):
    got = run(tmp_path, how)
    assert got["first"] == 0 and got["after"] == ["https://example.com/my-point?tab=moving&q=7001"]  # waited, then once


def test_a_form_being_sent_is_never_cut_by_a_refresh(tmp_path):
    assert run(tmp_path, "sending")["after"] == []
    assert run(tmp_path, "asked")["first"] == 1                                              # a window that only asks: not a form sent


def test_other_pages_of_the_point_do_not_refresh(tmp_path):
    assert run(tmp_path, "idle", "/chats")["after"] == []
