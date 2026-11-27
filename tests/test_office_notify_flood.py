"""The owner 2026-11-27: the office got endless notifications with a sound about the stages of fresh orders. The cause: notify.js replaced its
list of what was shown with the keys of its own feed, and two tabs of one login with other feeds (an office tab on /tasks/feed and one
«Работать как точка» on /my-point/feed, a tab in front and one behind) wrote over each other — each saw the other's items as new on every look.
Now the list is added to; the office gets no ring for the stages of orders («едет к вам», «на точке»); its tasks «Позвонить…» ring once by
default, the repeat every 2 hours and the sound itself are «Мои уведомления» of «Настройки → Уведомления». Made-up data only."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from panel_helpers import login, make_app, post
from test_as_point import as_point, cheap_hashes, owner, w  # noqa: F401 - the fixtures
from test_call_tasks import ARRIVE, at_point, staffed
from test_order_reserve import db
from tires_sync import office_notify, orders, store
from tires_sync.panel import routes_mypoint

NOTIFY_JS = Path(routes_mypoint.__file__).parent / "static" / "notify.js"


# ---- notify.js: two tabs of one login ring once ----------------------------------------------------------------------------------------

HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const [file, feedsJson] = process.argv.slice(2);
const feeds = JSON.parse(feedsJson);                      // feeds[tab] = the items that tab's feed gives on every look
const storage = {data: {}, getItem(k) { return k in this.data ? this.data[k] : null; }, setItem(k, v) { this.data[k] = String(v); }};
let shown = [];
function Note(title) { shown.push(title); }
Note.permission = "granted";
Note.requestPermission = () => Promise.resolve("granted");
const tabs = feeds.map((items, n) => {
  const tab = {tick: null};
  const ctx = {console, JSON, Math, Number, String, Object, Array, Date, Notification: Note, FormData: function () { this.append = () => {}; },
    document: {querySelector: () => null, addEventListener() {}, readyState: "complete", visibilityState: "visible", get activeElement() { return null; },
               currentScript: {getAttribute: a => a === "data-feed" ? "/feed" + n : null}},
    localStorage: storage, sessionStorage: {getItem: () => null, setItem() {}, removeItem() {}},
    location: {pathname: "/orders", href: "https://example.com/orders"},
    fetch: () => Promise.resolve({ok: true, redirected: false, json: () => Promise.resolve({who: "owner", items: JSON.parse(JSON.stringify(items))})}),
    setInterval: fn => { tab.tick = fn; }, setTimeout: () => 0, addEventListener() {}, focus() {}};
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(file, "utf8"), ctx);
  return tab;
});
const settle = () => new Promise(r => setImmediate(r));
(async () => {
  await settle(); await settle();
  for (let round = 0; round < 6; round++) for (const tab of tabs) { tab.tick(); await settle(); await settle(); }
  process.stdout.write(JSON.stringify({shown}));
})();
"""


def run_tabs(tmp_path, feeds):
    node = shutil.which("node")
    if not node:
        pytest.skip("no node to run notify.js")
    harness = tmp_path / "tabs.js"
    harness.write_text(HARNESS, encoding="utf-8")
    out = subprocess.run([node, str(harness), str(NOTIFY_JS), json.dumps(feeds)], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)["shown"]


CALL = {"key": "call-task-1-0", "title": "Позвонить", "text": "", "href": "/orders/1"}


def test_two_tabs_with_other_feeds_ring_once_not_on_every_look(tmp_path):
    # an office tab (its tasks only) and a tab «Работать как точка» (the point's orders too): the order is new once, then it is known
    assert run_tabs(tmp_path, [[CALL], [CALL]]) == []                         # the first look: what is there is not new
    shown = run_tabs(tmp_path, [[CALL], [CALL, {"key": "order-1", "title": "Заказ № 1 едет к вам", "text": "", "href": "/my-point"}]])
    assert shown == ["Заказ № 1 едет к вам"]                                  # once — it was 6, one per round of the two tabs


def test_an_item_that_comes_and_goes_between_tabs_rings_once(tmp_path):
    # tab 0 never has the item, tab 1 has it from its second look: one notification, not one per round (it was 6)
    script = HARNESS.replace("json: () => Promise.resolve({who: \"owner\", items: JSON.parse(JSON.stringify(items))})",
                             "json: () => Promise.resolve({who: \"owner\", items: JSON.parse(JSON.stringify(tab.looks++ ? items : items.filter(i => i.key !== \"order-7\")))})")
    script = script.replace("const tab = {tick: null};", "const tab = {tick: null, looks: 0};")
    assert script != HARNESS
    node = shutil.which("node")
    if not node:
        pytest.skip("no node to run notify.js")
    harness = tmp_path / "tabs2.js"
    harness.write_text(script, encoding="utf-8")
    order = {"key": "order-7", "title": "Заказ № 7 на точке", "text": "", "href": "/my-point"}
    out = subprocess.run([node, str(harness), str(NOTIFY_JS), json.dumps([[CALL], [CALL, order]])], capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(out.stdout)["shown"] == ["Заказ № 7 на точке"]


def test_a_quiet_item_gives_no_notification(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("no node to run notify.js")
    script = HARNESS.replace("const tab = {tick: null};", "const tab = {tick: null, looks: 0};").replace(
        "items: JSON.parse(JSON.stringify(items))", "items: JSON.parse(JSON.stringify(tab.looks++ ? items : []))")
    harness = tmp_path / "tabs3.js"
    harness.write_text(script, encoding="utf-8")
    quiet = dict(CALL, key="order-9", title="Тихо", quiet=True)
    loud = dict(CALL, key="call-task-2-0", title="Громко")
    out = subprocess.run([node, str(harness), str(NOTIFY_JS), json.dumps([[quiet, loud]])], capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(out.stdout)["shown"] == ["Громко"]


def test_the_list_of_what_was_shown_is_added_to_and_kept_short():
    js = NOTIFY_JS.read_text(encoding="utf-8")
    assert "memory = remember(seen, keys);" in js and "writeSeen(who, memory);" in js and "writeSeen(who, keys);" not in js
    assert "SEEN_MAX = 500" in js and "out.slice(out.length - SEEN_MAX)" in js


# ---- the office's own choice -------------------------------------------------------------------------------------------------------

def test_the_call_tasks_ring_once_by_default_and_every_2_hours_when_chosen(tmp_path):
    conn = db(tmp_path)
    at_point(conn, staffed(conn))
    later = "2026-10-15T14:30:00"                                             # 4.5 hours after the task was made
    keys = lambda: [i["key"] for i in office_notify.calls(conn, "manager1", later)]
    assert office_notify.get(conn, "manager1") == {"calls": True, "repeat": False}
    assert keys()[0].endswith("-0")                                           # no new key every 2 hours: one ring
    office_notify.put(conn, "manager1", True, True)
    assert keys()[0].endswith("-2")                                           # the repeat chosen: as before (2026-11-12)
    office_notify.put(conn, "manager1", False, True)
    assert office_notify.get(conn, "manager1") == {"calls": False, "repeat": False}
    assert all(i.get("quiet") for i in office_notify.calls(conn, "manager1", later))
    store.set_state(conn, office_notify.KEY % "manager1", "garbage")
    assert office_notify.get(conn, "manager1") == office_notify.DEFAULT


def test_the_settings_show_the_switch_and_save_it(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, ARRIVE)
    client = app.test_client()
    tok = login(client)
    page = client.get("/settings/notify").get_data(as_text=True)
    assert "Мои уведомления" in page and 'action="/settings/notify/mine"' in page and "О смене этапов заказов офис не оповещается" in page
    assert post(client, "/settings/notify/mine", tok, calls="1").status_code == 302
    assert office_notify.get(conn, "owner") == {"calls": True, "repeat": False}
    assert post(client, "/settings/notify/mine", tok).status_code == 302
    assert office_notify.get(conn, "owner")["calls"] is False
    assert client.post("/settings/notify/mine", data={"calls": "1"}).status_code == 400        # no csrf
    cid = at_point(conn)
    items = client.get("/tasks/feed").get_json()["items"]
    assert [i["href"] for i in items] == ["/orders/%d" % cid] and items[0]["quiet"] is True


def test_the_office_working_as_a_point_gets_no_ring_for_the_stages(w, monkeypatch):
    client = owner(w)
    assert as_point(client, w["pa"]) in (200, 302)
    card = orders.get_card(w["conn"], w["mine"])
    monkeypatch.setattr(routes_mypoint, "_cards", lambda conn, pid: [card])
    monkeypatch.setattr(routes_mypoint, "_oth_cards", lambda conn, pid: [])
    items = client.get("/my-point/feed").get_json()["items"]
    stages = [i for i in items if i["key"].startswith("order-")]
    assert stages and all(i.get("quiet") for i in stages)
