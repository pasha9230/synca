"""Lane 3 «Уведомления» (the owner 2026-10-26: a point hears of its news in the panel, the bell with a sound, no Telegram): the question
«Клиент забрал?» is a row of the bell of that point only, never of another point or of the staff; its key in /my-point/feed is of the stay at
the point, so that a new stay rings again; notify.js rings once per key, the feed's question and its bell row being one."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_mypoint import manager, press
from test_mypoint_board import DAY, MORNING, asked, at_point
from test_vk_commands import _card, _stage, ready
from tires_sync import order_points as points, orders
from tires_sync.panel import routes_alerts, routes_mypoint

QUESTION = "Клиент забрал заказ № 5000100?"
NOTIFY_JS = Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static" / "notify.js"


@pytest.fixture
def clock(monkeypatch):
    """The panel's time is the test's: the last day of the reservation, in the working hours."""
    moment = {"now": MORNING}
    monkeypatch.setattr(routes_mypoint, "local_iso", lambda: moment["now"])
    monkeypatch.setattr(routes_alerts, "local_iso", lambda: moment["now"])
    return moment


def bell(client):
    return client.get("/alerts/feed").get_json()


def questions(client):
    return [it for it in bell(client)["items"] if it["title"].startswith("Клиент забрал")]


def stay(conn, card):
    return re.sub(r"\D", "", orders.get_card(conn, card)["entered_at"] or "")


# --- the bell -----------------------------------------------------------------------------------------------------------------------

def test_the_question_is_a_row_of_the_bell_of_its_point_only(tmp_path, clock):
    app, owner, tok, conn, pid, card = ready(tmp_path)
    at_point(conn, card)
    mine = manager(app, conn, pid)
    other = points.save_point(conn, dict(name="Гамма", channel="vk", vk_peer_id="777"))
    _card(conn, other, webmim="700700", code="at_point")
    theirs = manager(app, conn, other, login_="gamma2")
    assert questions(mine) == []                                                            # not asked yet: no row
    asked(conn, card)
    rows = questions(mine)
    assert [(r["title"], r["level"], r["href"], r["order_id"]) for r in rows] == [(QUESTION, "yellow", "/my-point", card)]
    assert bell(mine)["count"] >= 1
    assert QUESTION in mine.get("/alerts").get_data(as_text=True)
    assert questions(theirs) == [] and QUESTION not in theirs.get("/alerts").get_data(as_text=True)     # another point: never
    assert questions(owner) == [] and QUESTION not in owner.get("/alerts").get_data(as_text=True)       # the staff: no point alert


def test_the_question_row_goes_when_the_point_answers(tmp_path, clock):
    app, owner, tok, conn, pid, card = ready(tmp_path)
    at_point(conn, card)
    asked(conn, card)
    mine = manager(app, conn, pid)
    assert len(questions(mine)) == 1
    press(mine, card, "reserve_issued")
    assert questions(mine) == []


def test_the_question_row_goes_at_the_end_of_the_working_day(tmp_path, clock):
    app, owner, tok, conn, pid, card = ready(tmp_path)
    at_point(conn, card)
    asked(conn, card)
    mine = manager(app, conn, pid)
    clock["now"] = DAY + "T20:30:00"
    assert questions(mine) == []


RETURN = "Заказ № 5000100: возврат отмечен, ждёт суммы от менеджера"


def returns(client):
    return [it for it in bell(client)["items"] if it["title"] == RETURN]


def test_a_return_the_point_pressed_is_a_green_row_of_its_bell_until_the_order_moves_to_return(tmp_path, clock):
    app, owner, tok, conn, pid, card = ready(tmp_path)
    at_point(conn, card)
    mine = manager(app, conn, pid)
    mate = manager(app, conn, pid, login_="beta2")                                           # another person of the same point
    other = points.save_point(conn, dict(name="Гамма", channel="vk", vk_peer_id="777"))
    theirs = manager(app, conn, other, login_="gamma2")
    assert returns(mine) == []
    press(mine, card, "returned")                                                          # no amount: the order stays «На точке»
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point"
    for person in (mine, mate):
        assert [(r["level"], r["href"], r["order_id"]) for r in returns(person)] == [("green", "/my-point", card)]
    assert returns(theirs) == [] and returns(owner) == []                                  # the staff keep their yellow «заполните сумму»
    assert any("заполните сумму возврата" in it["text"] for it in bell(owner)["items"])
    orders.update_card(conn, card, dict(return_amount="16000"), DAY + "T12:00:00", "o")
    conn.commit()
    assert returns(mine) == []                                                             # the amount is in: it waits for it no more
    orders.move_card(conn, card, _stage(conn, "return"), DAY + "T12:00:00", "o")
    conn.commit()
    assert returns(mine) == []


def test_a_return_of_an_earlier_stay_is_no_row(tmp_path, clock):
    app, owner, tok, conn, pid, card = ready(tmp_path)
    at_point(conn, card)
    mine = manager(app, conn, pid)
    press(mine, card, "returned")
    orders.move_card(conn, card, _stage(conn, "moving"), DAY + "T12:00:00", "o")
    orders.move_card(conn, card, _stage(conn, "at_point"), DAY + "T12:30:00", "o")
    conn.commit()
    assert returns(mine) == []


# --- the key of the question in /my-point/feed -----------------------------------------------------------------------------------------

def feed_questions(client):
    return [it["key"] for it in client.get("/my-point/feed").get_json()["items"] if it["key"].startswith("question-")]


def test_the_question_key_is_of_the_stay_at_the_point_and_a_new_stay_gives_a_new_key(tmp_path, clock):
    app, owner, tok, conn, pid, card = ready(tmp_path)
    at_point(conn, card)
    asked(conn, card)
    mine = manager(app, conn, pid)
    first = feed_questions(mine)
    assert first == ["question-%d-%s" % (card, stay(conn, card))] and stay(conn, card)
    orders.move_card(conn, card, _stage(conn, "moving"), DAY + "T12:00:00", "o")            # the order left the point and came back
    orders.move_card(conn, card, _stage(conn, "at_point"), DAY + "T12:30:00", "o")
    conn.commit()
    asked(conn, card, when=DAY + "T13:00:00")
    clock["now"] = DAY + "T13:00:00"
    again = feed_questions(mine)
    assert again == ["question-%d-%s" % (card, stay(conn, card))] and again != first


# --- notify.js: one sound and one notification per key --------------------------------------------------------------------------------

HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const feeds = JSON.parse(process.argv[3]);
const shown = [], rings = [];
let step = 0, tick = null, store = {};
const ctx = {
  console,
  document: {querySelector: () => null, addEventListener: () => {}},
  localStorage: {getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }},
  fetch: () => Promise.resolve({ok: true, redirected: false, json: () => Promise.resolve({who: "gamma", items: feeds[step++] || []})}),
  setInterval: fn => { tick = fn; },
  Notification: Object.assign(function (title, opts) { shown.push([title, opts.tag]); }, {permission: "granted"}),
  AudioContext: function () {
    return {state: "running", currentTime: 0, destination: {}, resume: () => Promise.resolve(),
            createOscillator: () => ({frequency: {}, connect: () => {}, start: () => rings.push(step), stop: () => {}}),
            createGain: () => ({gain: {setValueAtTime: () => {}, exponentialRampToValueAtTime: () => {}}, connect: () => {}})};
  },
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const settle = () => new Promise(r => setTimeout(r, 0));
(async () => {
  await settle(); await settle();
  for (let i = 1; i < feeds.length; i++) { tick(); await settle(); await settle(); }
  process.stdout.write(JSON.stringify({shown, rings: [...new Set(rings)]}));
})();
"""


def run_notify(tmp_path, feeds):
    node = shutil.which("node")
    if not node:
        pytest.skip("no node to run notify.js")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    out = subprocess.run([node, str(harness), str(NOTIFY_JS), json.dumps(feeds)], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def item(key, title="x"):
    return {"key": key, "title": title, "text": "", "href": "/my-point"}


def test_notify_rings_once_per_question_key_and_its_bell_row_is_the_same_question(tmp_path):
    question, row = item("question-7-20261016123000", QUESTION), item("alert-question-7-20261016123000-0", QUESTION)
    later = item("question-7-20261017100000", QUESTION)
    got = run_notify(tmp_path, [[], [question, row], [question, row], [row, question], [later, item("alert-question-7-20261017100000-0")]])
    assert got["shown"] == [[QUESTION, "question-7-20261016123000"], [QUESTION, "question-7-20261017100000"]]
    assert got["rings"] == [2, 5]                                                            # the second and the fifth look only


def test_notify_rings_once_for_a_question_of_a_stay_with_no_time(tmp_path):
    got = run_notify(tmp_path, [[], [item("question-7-", QUESTION), item("alert-question-7--0", QUESTION)]])
    assert got["shown"] == [[QUESTION, "question-7-"]] and got["rings"] == [2]


def test_notify_says_nothing_of_what_was_there_at_the_first_look(tmp_path):
    question = item("question-7-20261016123000", QUESTION)
    assert run_notify(tmp_path, [[question], [question]]) == {"shown": [], "rings": []}
