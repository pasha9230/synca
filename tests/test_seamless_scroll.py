"""Бесшовность (the owner 2026-11-28): the pages keep where one was and what one did. «Рабочее место» keeps its place after «Принял/Выдал»
(app.js 13d) and after its quiet refresh on a computer, where .main scrolls and not the window (notify.js); a press there comes back with
the tab and the search; the board of orders keeps its columns after a move; <details data-remember> stays open or folded; the reply typed
in a chat comes back after a reload and goes away after «Отправить» (app.js 13f). The scripts run in node where it is installed."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_workplace import _text, manager, ready

STATIC = Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
NOTIFY_JS = STATIC / "notify.js"


def node():
    found = shutil.which("node")
    if not found:
        pytest.skip("no node")
    return found


def block(start, end):
    return APP_JS[APP_JS.index(start):APP_JS.index(end, APP_JS.index(start))]


# --- notify.js: the quiet refresh of «Рабочее место» keeps the place of .main on a computer -------------------------------------------

NOTIFY_HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const [file, how, kept] = process.argv.slice(2);
let clock = 100000, tick = null, scrolledTo = null, timers = [];
const session = kept ? {"tires.mypoint.scroll": kept} : {}, replaced = [];
const main = {scrollTop: how === "save" ? 900 : 0, scrollHeight: 3000, clientHeight: 700};
const holder = {getAttribute: () => "old"};
const ctx = {
  console, JSON, Math, Number, String,
  Date: {now: () => clock},
  document: {querySelector: s => (s === ".main" && how !== "phone" ? main : (s === "[data-point-rev]" ? holder : null)), readyState: "complete",
             activeElement: {matches: () => false}, addEventListener() {}, querySelectorAll: () => []},
  getComputedStyle: el => ({overflowY: el === main ? "auto" : "visible"}),
  localStorage: {getItem: () => null, setItem() {}},
  sessionStorage: {getItem: k => (k in session ? session[k] : null), setItem: (k, v) => { session[k] = String(v); }, removeItem: k => { delete session[k]; }},
  location: {pathname: "/my-point", href: "https://example.com/my-point?tab=here", replace: u => replaced.push(u)},
  scrollY: how === "phone" ? 640 : 0, scrollTo: (x, y) => { scrolledTo = y; },
  fetch: () => Promise.resolve({ok: true, redirected: false, json: () => Promise.resolve({who: "p", items: [], rev: "new"})}),
  setInterval: fn => { tick = fn; }, setTimeout: fn => { timers.push(fn); return timers.length; }, addEventListener() {},
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(file, "utf8"), ctx);
const restored = {main: main.scrollTop, window: scrolledTo};
const settle = () => new Promise(r => setTimeout(r, 0));
(async () => {
  await settle(); await settle();
  clock += 60000;                                                     // the point has done nothing for a minute: the refresh goes
  const t = timers; timers = []; t.forEach(fn => fn());
  if (tick) { tick(); await settle(); await settle(); }
  process.stdout.write(JSON.stringify({restored, replaced: replaced.length, scroll: session["tires.mypoint.scroll"] || null}));
})();
"""


def run_notify(tmp_path, how, kept=""):
    harness = tmp_path / "notify_harness.js"
    harness.write_text(NOTIFY_HARNESS, encoding="utf-8")
    out = subprocess.run([node(), str(harness), str(NOTIFY_JS), how, kept], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def test_the_refresh_keeps_the_place_of_the_main_box_on_a_computer(tmp_path):
    js = NOTIFY_JS.read_text(encoding="utf-8")
    assert "function mainBox()" in js and "box ? box.scrollTop : (window.scrollY" in js
    got = run_notify(tmp_path, "main", json.dumps({"url": "https://example.com/my-point?tab=here", "y": 900, "main": True}))
    assert got["restored"] == {"main": 900, "window": None}                    # .main put back, the window left alone
    got = run_notify(tmp_path, "phone", json.dumps({"url": "https://example.com/my-point?tab=here", "y": 640, "main": False}))
    assert got["restored"] == {"main": 0, "window": 640}                       # a phone: the window, as before
    got = run_notify(tmp_path, "main", json.dumps({"url": "https://example.com/my-point?tab=here", "y": 500, "main": True}))
    assert got["restored"]["main"] == 500


def test_the_refresh_saves_the_place_of_main_and_of_the_window(tmp_path):
    got = run_notify(tmp_path, "save")
    assert got["replaced"] == 1 and json.loads(got["scroll"])["y"] == 900 and json.loads(got["scroll"])["main"] is True
    got = run_notify(tmp_path, "phone")
    assert got["replaced"] == 1 and json.loads(got["scroll"])["y"] == 640 and json.loads(got["scroll"])["main"] is False


def test_a_saved_place_of_main_is_put_on_the_window_when_main_no_longer_scrolls(tmp_path):
    got = run_notify(tmp_path, "phone", json.dumps({"url": "https://example.com/my-point?tab=here", "y": 700, "main": True}))
    assert got["restored"] == {"main": 0, "window": 700}


# --- app.js 13d: «Рабочее место» keeps its place after a press -------------------------------------------------------------------

def test_the_workplace_takes_the_common_place_after_a_press():
    keep = block("13d) Списки и разделы", "13b) Новый заказ")
    assert '"/my-point") return' not in keep and "«Рабочее место» точки — тоже" in keep


def test_a_press_comes_back_with_the_tab_and_the_search(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    token = re.search(r'action="/my-point/%d/arrived".*?name="token" value="([0-9a-f]+)"' % card, _text(mine.get("/my-point?tab=moving")), re.S).group(1)
    r = mine.post("/my-point/%d/arrived" % card, data={"csrf": "tok", "token": token},
                  headers={"Referer": "http://localhost/my-point?tab=oth&q=26R-14+47"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/my-point?tab=oth&q=26R-14+47")


def test_a_press_from_another_site_or_page_takes_no_search(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    token = re.search(r'action="/my-point/%d/arrived".*?name="token" value="([0-9a-f]+)"' % card, _text(mine.get("/my-point?tab=moving")), re.S).group(1)
    r = mine.post("/my-point/%d/arrived" % card, data={"csrf": "tok", "token": token}, headers={"Referer": "http://evil.example.com/my-point?q=x"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/my-point")


# --- app.js 9: the board keeps its columns after a move ----------------------------------------------------------------------------

def test_the_board_keeps_its_side_scroll_after_a_move():
    board = block("9) Заказы: перетаскивание", 'btn.dataset.copy')
    assert '"tires.board-left"' in board and "board.scrollLeft = was.left" in board
    assert "keepLeft = true;" in board and board.index("keepLeft = true;") < board.index('fetch("/orders/" + card.dataset.card + "/move"')
    assert '(was.keep || (navBoard && navBoard.type === "reload"))' in board and "was.path === window.location.pathname" in board
    for line in board.splitlines():                                             # no storage: the board as before
        assert "sessionStorage." not in line or ("try {" in line and "catch (e)" in line), line


# --- app.js 13f: open blocks and the chat's draft --------------------------------------------------------------------------------

REMEMBER_HARNESS = r"""
const fs = require("fs");
const [file, stored] = process.argv.slice(2);
const session = JSON.parse(stored), doc = {};
const details = (key, open) => ({tagName: "DETAILS", open, getAttribute: k => (k === "data-remember" ? key : null)});
const stages = details("point-stages", false), extra = details("order-extra", false), other = details("", true);
const form = {matches: s => s === "form[data-photo-form]", getAttribute: k => (k === "action" ? "/chats/7/send" : null)};
const area = {tagName: "TEXTAREA", name: "text", form, value: session.server || ""};
global.sessionStorage = {getItem: k => (k in session ? session[k] : null), setItem: (k, v) => { session[k] = String(v); }, removeItem: k => { delete session[k]; }};
global.document = {
  querySelectorAll: s => (s === "details[data-remember]" ? [stages, extra] : (s.includes("textarea") ? [area] : [])),
  addEventListener: (n, fn, capture) => { doc[n] = {fn, capture: !!capture}; },
};
eval(fs.readFileSync(file, "utf8"));
const out = {stages: stages.open, extra: extra.open, typed: area.value};
extra.open = true; doc.toggle.fn({target: extra});
doc.toggle.fn({target: other});
out.kept = JSON.parse(session["tires.details-open"]);
area.value = "Здравствуйте, шины"; doc.input.fn({target: area});
out.draft = session["tires.chat-draft:/chats/7/send"] || null;
doc.submit.fn({target: form});
out.after_send = session["tires.chat-draft:/chats/7/send"] || null;
out.capture = doc.toggle.capture && doc.submit.capture;
console.log(JSON.stringify(out));
"""


def run_remember(tmp_path, session):
    js = tmp_path / "remember.js"
    js.write_text("// " + block("13f) Бесшовность", "13c) Карточка заказа").rsplit("//", 1)[0], encoding="utf-8")   # the block alone, its comments too
    harness = tmp_path / "remember_harness.js"
    harness.write_text(REMEMBER_HARNESS, encoding="utf-8")
    out = subprocess.run([node(), str(harness), str(js), json.dumps(session)], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_open_blocks_and_the_draft_come_back_and_the_draft_goes_after_the_send(tmp_path):
    got = run_remember(tmp_path, {"tires.details-open": json.dumps({"point-stages": True}), "tires.chat-draft:/chats/7/send": "Добрый день"})
    assert got["stages"] is True and got["extra"] is False                     # the key kept opens its block, the other one as the page shows
    assert got["typed"] == "Добрый день"                                       # the typed reply came back
    assert got["kept"] == {"point-stages": True, "order-extra": True}          # a block without a key is not kept
    assert got["draft"] == "Здравствуйте, шины" and got["after_send"] is None and got["capture"] is True


def test_the_draft_of_the_panel_wins_over_the_kept_one(tmp_path):
    got = run_remember(tmp_path, {"server": "Текст, что не ушёл", "tires.chat-draft:/chats/7/send": "Старый"})
    assert got["typed"] == "Текст, что не ушёл"


def test_the_three_blocks_carry_their_keys(tmp_path):
    templates = STATIC.parent / "templates"
    assert 'data-remember="point-stages"' in (templates / "mypoint.html").read_text(encoding="utf-8")
    assert 'data-remember="order-extra"' in (templates / "_order_window.html").read_text(encoding="utf-8")
    assert 'data-remember="order-chain"' in (templates / "_order_chain.html").read_text(encoding="utf-8")
    keep = block("13f) Бесшовность", "13c) Карточка заказа")
    for line in keep.splitlines():
        assert "sessionStorage." not in line or ("try {" in line and "catch (e" in line), line
