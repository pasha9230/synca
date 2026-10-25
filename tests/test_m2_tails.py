"""Night lane M2 (2026-10-21): the tails of the alerts and of the chats."""
from __future__ import annotations

from xml.etree import ElementTree as ET

from tires_sync import connections, photo_library, store
from test_feed_update import goods, post, public_feed, rev_of, setup, unload


# --- 5: the preview of «Обновить фид» builds the photo links as the cycle does -----------------------------------------------

LIBRARY = ["https://feeds.example.test/photos/aa.jpg", "https://feeds.example.test/photos/bb.jpg"]


class _Library(dict):
    """Own library links for every set: what `photo_library.own_photos_for` gives an account with the switch on and ready frames."""
    def get(self, key, default=None):
        return list(LIBRARY)


def _spy_preview(client):
    from tires_sync.panel import feed_update
    seen, real = [], feed_update.build_ads

    def spy(*args, **kwargs):
        result = real(*args, **kwargs)
        seen.extend(result[0])
        return result
    feed_update.build_ads = spy
    try:
        client.get("/feed/update")
    finally:
        feed_update.build_ads = real
    return seen


def _urls(nodes):
    return sorted(i.get("url") for n in nodes for i in n.findall("Images/Image"))


def test_the_preview_takes_the_accounts_own_photos_as_the_cycle_does(tmp_path, monkeypatch):
    """The cycle gives the ads the account's own library photos; the preview built without them, so its ads had other links."""
    app, client, tok, conn, account = setup(tmp_path)
    store.set_state(conn, "public_base", "https://feeds.example.test")
    conn.commit()
    monkeypatch.setattr(photo_library, "own_photos_for", lambda conn, account_id, base: photo_library.OwnPhotos(_Library(), {}, frozenset(LIBRARY)))
    unload(app, goods(1, 2))
    preview = _urls(_spy_preview(client))
    post(client, "/feed/update/confirm", tok, rev=rev_of(client))
    published = _urls(ET.fromstring(public_feed(app, conn, account).encode("utf-8")).findall("Ad"))
    assert any("/photos/" in u for u in published)                                   # the cycle used the library
    assert preview == published


def test_the_preview_does_not_change_the_feed(tmp_path):
    """Opening the preview writes nothing: the feed built after it is the same file as the one built without it."""
    app, client, tok, conn, account = setup(tmp_path)
    connections.set_photo_style(conn, account, {"border": "#112233"}, "2026-10-07T10:00:00", "owner")
    store.set_state(conn, "public_base", "https://feeds.example.test")
    conn.commit()
    unload(app, goods(1, 2))
    post(client, "/feed/update/confirm", tok, rev=rev_of(client))
    first = public_feed(app, conn, account)
    _spy_preview(client)
    post(client, "/feed/update/confirm", tok, rev=rev_of(client))
    assert public_feed(app, conn, account) == first


# --- 4: the chat's «Отправить» never stays dead -----------------------------------------------------------------------------

CHAT_SEND_HARNESS = r"""
const fs = require("fs");
const handlers = {doc: {}, win: {}}, timers = [];
const anyEl = () => new Proxy(function () {}, {get: (t, k) => k === "length" ? 0 : (k === Symbol.iterator ? [][Symbol.iterator] : (k === "forEach" ? () => {} : anyEl())), apply: () => anyEl()});
const on = (box) => (t, f) => (box[t] = box[t] || []).push(f);
global.document = {addEventListener: on(handlers.doc), querySelectorAll: (s) => s === "form[data-photo-form]" && global.FORM ? [global.FORM] : [], querySelector: () => null, getElementById: () => null,
  body: anyEl(), documentElement: anyEl(), createElement: () => anyEl(), hidden: false, cookie: ""};
global.window = {addEventListener: on(handlers.win), location: {}, matchMedia: () => ({matches: false, addEventListener() {}}), fetch: () => new Promise(() => {}), FormData: function () {}};
global.location = {}; global.localStorage = {getItem: () => null, setItem() {}}; global.navigator = {};
global.MutationObserver = function () { return {observe() {}}; }; global.Event = function (t) { this.type = t; };
global.setTimeout = (f, ms) => { timers.push({f, ms}); return timers.length; }; global.clearTimeout = (id) => { if (timers[id - 1]) timers[id - 1].f = null; };
try { eval(fs.readFileSync(process.argv[2], "utf8")); } catch (e) { /* later parts need more of a browser; the handlers are in place */ }
const attrs = () => { const a = {}; return {setAttribute: (k, v) => (a[k] = v), removeAttribute: (k) => delete a[k], has: (k) => k in a, disabled: false}; };
const buttons = [attrs(), attrs()], note = {textContent: "Фото уменьшится", classList: {add() {}, remove() {}}};
const form = {dataset: {}, matches: (s) => s === "form[data-once]" || s === "form[data-photo-form]",
  querySelectorAll: (s) => s === "button" ? buttons : [], querySelector: (s) => s === "[data-photo-note]" ? note : null};
global.FORM = form;
const submit = () => { const e = {target: form, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }};
  (handlers.doc.submit || []).forEach((f) => f(e)); return e; };
const out = {};
submit();
out.sent = form.dataset.sent === "1" && buttons.every((b) => b.has("aria-busy"));
out.second_blocked = submit().defaultPrevented;                                  // part 6: a second press sends nothing while the first goes
const watch = timers.filter((t) => t.f && t.ms >= 30000);
out.watch = watch.length === 1;                                                  // the blocked second press did not start a second watch
watch.forEach((t) => t.f());
out.after_wait = !form.dataset.sent && buttons.every((b) => !b.has("aria-busy") && !b.disabled) && /не отвечает/.test(note.textContent);
submit();
(handlers.win.pageshow || []).forEach((f) => f({persisted: true}));
out.after_back = !form.dataset.sent && buttons.every((b) => !b.has("aria-busy")) && /из истории/.test(note.textContent);
submit();                                                                         // a photo the panel refused: part 7 ends the send with its own note
note.textContent = "Файл слишком большой"; delete form.dataset.sent;
timers.filter((t) => t.f && t.ms >= 30000).forEach((t) => t.f());
out.photo_note_kept = note.textContent === "Файл слишком большой";
console.log(JSON.stringify(out));
"""


def test_the_chat_send_button_comes_back_after_back_or_a_silent_panel(tmp_path):
    """After «Назад» the browser restored the chat with its form marked as sent: «Отправить» did nothing and said nothing. A panel that
    does not answer kept it dead too. Now the button comes back with a note to check the chat (the same token never sends twice)."""
    import json
    import shutil
    import subprocess
    from pathlib import Path
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("no node")
    harness = tmp_path / "harness.js"
    harness.write_text(CHAT_SEND_HARNESS, encoding="utf-8")
    app_js = Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static" / "app.js"
    result = json.loads(subprocess.run([node, str(harness), str(app_js)], capture_output=True, text=True, timeout=30, check=True).stdout.strip().splitlines()[-1])
    assert result == {"sent": True, "second_blocked": True, "watch": True, "after_wait": True, "after_back": True, "photo_note_kept": True}


# --- 1: a client's new message in a chat of an order is an alert «Клиент написал» ------------------------------------------------

def _chat_order(tmp_path):
    from test_chats_sync import FakeClient, NOW as SYNC_NOW, chat, db, msg
    from tires_sync import chat_deals, chats, orders
    conn, account = db(tmp_path)
    orders.ensure_pipeline(conn, SYNC_NOW)
    client = FakeClient([[chat("c1", read=0, text="Здравствуйте")]], {"c1": [msg("m1", "Здравствуйте")]})
    chats.sync_account(conn, client, account, SYNC_NOW)                      # the first read: what is there is the past
    [row] = chats.list_chats(conn)
    card = orders.create_card(conn, dict(client_name="Татьяна"), SYNC_NOW, "o")
    chat_deals.link_chat(conn, row["id"], card, SYNC_NOW, "owner")
    return conn, account, client, row["id"], card, chat, msg


def _client_wrote(conn, card):
    return [e for e in conn.execute("SELECT text, level, author FROM order_event WHERE order_id = ? AND kind = 'chat' AND text LIKE 'Клиент написал:%'", (card,))]


def test_a_new_message_of_the_client_in_a_chat_of_an_order_is_one_alert_until_it_is_read_or_answered(tmp_path):
    from tires_sync import chats, order_alerts
    from test_chats_sync import TS
    conn, account, client, chat_row, card, chat, msg = _chat_order(tmp_path)
    conn.execute("UPDATE avito_chat SET unread = 0 WHERE id = ?", (chat_row,))       # the manager has read the first message
    conn.execute("UPDATE avito_message SET is_read = 1")
    conn.commit()
    assert _client_wrote(conn, card) == []
    client.chat_pages = [[chat("c1", updated=TS + 10, text="Есть в наличии?")]]
    client.messages["c1"] = [msg("m1", "Здравствуйте", read=1), msg("m2", "Есть в наличии?", created=TS + 10)]
    chats.sync_account(conn, client, account, "2026-10-13T12:05:00")
    [(text, level, author)] = _client_wrote(conn, card)
    assert text == "Клиент написал: Есть в наличии?" and level == "yellow" and author == "чаты Авито"
    assert text in [a["text"] for a in order_alerts.open_alerts(conn, "owner")]
    assert [r["text"] for r in order_alerts.page_rows(conn, "owner") if r["kind"] == "chat"] == [text]   # one row in «Уведомления» and the bell
    client.chat_pages = [[chat("c1", updated=TS + 20, text="Алло?")]]                    # more messages while the first waits: no new alert
    client.messages["c1"] = client.messages["c1"] + [msg("m3", "Алло?", created=TS + 20), msg("m4", "Ответьте", created=TS + 21)]
    chats.sync_account(conn, client, account, "2026-10-13T12:10:00")
    assert len(_client_wrote(conn, card)) == 1
    client.chat_pages = [[chat("c1", updated=TS + 30, text="Да, есть", direction="out")]]  # we answered: the chat waits for nobody
    client.messages["c1"] = client.messages["c1"] + [msg("m5", "Да, есть", direction="out", created=TS + 30)]
    chats.sync_account(conn, client, account, "2026-10-13T12:15:00")
    client.chat_pages = [[chat("c1", updated=TS + 40, text="Беру")]]                     # the client writes again: a new alert
    client.messages["c1"] = client.messages["c1"] + [msg("m6", "Беру", created=TS + 40)]
    chats.sync_account(conn, client, account, "2026-10-13T12:20:00")
    assert [t for t, _l, _a in _client_wrote(conn, card)] == ["Клиент написал: Есть в наличии?", "Клиент написал: Беру"]


def test_no_alert_for_our_own_message_nor_for_a_closed_order(tmp_path):
    from tires_sync import chats, orders
    from test_chats_sync import TS
    conn, account, client, chat_row, card, chat, msg = _chat_order(tmp_path)
    conn.execute("UPDATE avito_chat SET unread = 0 WHERE id = ?", (chat_row,))
    conn.execute("UPDATE avito_message SET is_read = 1")
    conn.commit()
    client.chat_pages = [[chat("c1", updated=TS + 10, text="Добрый день", direction="out")]]
    client.messages["c1"] = [msg("m1", "Здравствуйте", read=1), msg("m2", "Добрый день", direction="out", created=TS + 10)]
    chats.sync_account(conn, client, account, "2026-10-13T12:05:00")
    assert _client_wrote(conn, card) == []
    won = conn.execute("SELECT id FROM order_stage WHERE kind = 'won' LIMIT 1").fetchone()[0]
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (won, card))      # closed (the move itself needs its fields: not the point here)
    conn.commit()
    assert orders.get_card(conn, card)["stage"]["kind"] == "won"
    client.chat_pages = [[chat("c1", updated=TS + 20, text="Спасибо")]]                 # a closed deal has its own note «после закрытия»
    client.messages["c1"] = client.messages["c1"] + [msg("m3", "Спасибо", created=TS + 20)]
    chats.sync_account(conn, client, account, "2026-10-13T12:10:00")
    assert _client_wrote(conn, card) == []


# --- 2 and 3: one source of rows for the page, the bell, its counter and the dots; green ones expire after 7 days -----------------

def _ids_on_page(client):
    import re
    return re.findall(r'<a href="/(orders|chats)/(\d+)">', client.get("/alerts").get_data(as_text=True).split("<tbody>")[1])


def test_the_bell_counts_the_rows_of_the_page_and_not_those_of_a_deleted_order(tmp_path):
    """A guard: a deleted order takes its journal with it (trash), so the bell, the counter and the page agree."""
    from test_alerts_page import card, counter, event, setup
    from tires_sync import trash
    app, client, tok, conn = setup(tmp_path)
    keep, gone = card(conn, "Ольга"), card(conn, "Пётр")
    event(conn, keep, "notify", "Оповещение не отправлено: нет ВК")
    event(conn, gone, "notify", "Оповещение не отправлено: нет ВК")
    trash.delete_order(conn, gone, "2026-10-20T12:01:00", "owner")
    conn.commit()
    data = client.get("/alerts/feed").get_json()
    rows = _ids_on_page(client)
    assert data["count"] == len(rows) == len(data["items"]) == counter(client.get("/").get_data(as_text=True))[0] == 2      # keep: created + red
    assert {int(n) for _k, n in rows} == {keep}


def test_the_dots_are_the_most_important_colour_of_the_page_rows(tmp_path):
    from test_alerts_page import card, event, setup
    from test_order_dots import dots
    from tires_sync import order_alerts, orders
    app, client, tok, conn = setup(tmp_path)
    mine, other = card(conn, "Ольга", by="owner"), card(conn, "Пётр")             # «Заказ создан» by the owner is not an alert for the owner
    orders.add_event(conn, mine, "2026-10-20T12:00:00", "owner", "note", "x", level="yellow")
    event(conn, other, "reserve", "Оповещение о конце резерва")
    conn.commit()
    shown = {oid: colour for oid, (colour, _t) in dots(client.get("/orders?v=list").get_data(as_text=True)).items()}
    assert shown == {other: "yellow"} == order_alerts.order_levels(conn, "owner", "2026-10-20T12:00:00")
    assert {int(n) for _k, n in _ids_on_page(client)} == {other}


def test_a_green_alert_older_than_seven_days_is_not_shown_but_stays_in_the_journal(tmp_path):
    from test_alerts_page import card, setup
    from tires_sync import orders
    app, client, tok, conn = setup(tmp_path)                                     # the panel clock: 2026-10-20 12:00 Moscow
    cid = card(conn, "Ольга")
    orders.add_event(conn, cid, "2026-10-13T11:59:00", "система", "field", "Резерв: товар на точке (старое)", level="green")
    orders.add_event(conn, cid, "2026-10-13T12:01:00", "система", "field", "Резерв: товар на точке (неделя без минуты)", level="green")
    orders.add_event(conn, cid, "2026-10-01T10:00:00", "система", "reserve", "Оповещение о конце резерва (старое)", level="yellow")
    conn.commit()
    texts = [i["text"] for i in client.get("/alerts/feed").get_json()["items"]]
    assert "Резерв: товар на точке (старое)" not in texts and "Резерв: товар на точке (неделя без минуты)" in texts
    assert "Оповещение о конце резерва (старое)" in texts                                    # only green ones expire
    assert "Резерв: товар на точке (старое)" not in client.get("/alerts").get_data(as_text=True)
    assert "Резерв: товар на точке (старое)" in [e["text"] for e in orders.list_events(conn, cid)]  # the journal keeps it


# --- 1: a chat without an order whose client waits is a row «Клиент написал» -------------------------------------------------------

def _unlinked_chat(app, conn):
    from test_chats_sync import FakeClient, TS, chat, msg
    from tires_sync import chats
    account = connections.create_account(conn, "Основной", "2026-10-20T10:00:00", "o")
    conn.execute("UPDATE account SET chats_enabled = 1 WHERE id = ?", (account,))
    conn.commit()
    client = FakeClient([[chat("c1", read=0, text="Старый вопрос")]], {"c1": [msg("m1", "Старый вопрос")]})
    chats.sync_account(conn, client, account, "2026-10-20T10:00:00")                   # the first read: the past, no row
    client.chat_pages = [[chat("c1", updated=TS + 10, text="Есть 205/55 R16?")]]
    client.messages["c1"] = [msg("m1", "Старый вопрос"), msg("m2", "Есть 205/55 R16?", created=TS + 10)]
    conn.execute("UPDATE avito_message SET at = '2026-10-20T09:00:00'")                    # before the first read
    conn.commit()
    return account, client, chats.list_chats(conn)[0]["id"]


def test_a_chat_without_an_order_is_one_row_in_the_page_the_bell_and_the_counter_until_it_is_answered(tmp_path):
    from test_alerts_page import counter, setup
    from test_chats_sync import TS, msg
    from tires_sync import chats
    app, client, tok, conn = setup(tmp_path)
    account, avito, chat_row = _unlinked_chat(app, conn)
    assert client.get("/alerts/feed").get_json()["count"] == 0                             # what was there before the first read is the past
    chats.sync_account(conn, avito, account, "2026-10-20T11:00:00")
    conn.execute("UPDATE avito_message SET at = '2026-10-20T11:00:00' WHERE message_id = 'm2'")
    conn.commit()
    data = client.get("/alerts/feed").get_json()
    [item] = data["items"]
    assert data["count"] == 1 and counter(client.get("/").get_data(as_text=True))[0] == 1
    assert item["href"] == "/chats/%d" % chat_row and item["title"] == "Чат без заказа" and item["text"] == "Клиент написал: Есть 205/55 R16?"
    assert item["level"] == "yellow" and item["more"] == 1 and item["client"] == "Татьяна"           # two messages wait: one row
    page = client.get("/alerts").get_data(as_text=True)
    assert '<a href="/chats/%d">Чат без заказа</a>' % chat_row in page and "/ack" not in page
    avito.chat_pages[0][0]["updated"] = TS + 30
    avito.messages["c1"] = avito.messages["c1"] + [msg("m3", "Да, есть", direction="out", created=TS + 30)]
    chats.sync_account(conn, avito, account, "2026-10-20T11:05:00")                        # answered: nobody waits
    assert client.get("/alerts/feed").get_json()["count"] == 0


def test_a_chat_marked_no_reply_or_with_an_order_has_no_chat_row(tmp_path):
    from test_alerts_page import card, setup
    from tires_sync import chat_deals, chats, order_alerts
    app, client, tok, conn = setup(tmp_path)
    account, avito, chat_row = _unlinked_chat(app, conn)
    chats.sync_account(conn, avito, account, "2026-10-20T11:00:00")
    conn.execute("UPDATE avito_message SET at = '2026-10-20T11:00:00' WHERE message_id = 'm2'")
    conn.commit()
    assert [r["chat_row"] for r in order_alerts.chat_rows(conn)] == [chat_row]
    chats.set_status(conn, chat_row, "no_reply", "2026-10-20T11:01:00")
    conn.commit()
    assert order_alerts.chat_rows(conn) == []
    chats.set_status(conn, chat_row, "open", "2026-10-20T11:02:00")
    chat_deals.link_chat(conn, chat_row, card(conn, "Татьяна"), "2026-10-20T11:02:00", "owner")
    assert order_alerts.chat_rows(conn) == []                                              # its order's journal holds the alert (part 1)


def test_a_chat_of_an_order_marked_no_reply_alerts_again_when_the_client_writes(tmp_path):
    """«Не требует ответа» leaves the unread count as it is: the next message of the client opens the chat again and is an alert."""
    from tires_sync import chats
    from test_chats_sync import TS
    conn, account, client, chat_row, card, chat, msg = _chat_order(tmp_path)                 # the first message waits (unread 1)
    chats.set_status(conn, chat_row, "no_reply", "2026-10-13T12:01:00")
    conn.commit()
    client.chat_pages = [[chat("c1", updated=TS + 10, text="А всё-таки?")]]
    client.messages["c1"] = [msg("m1", "Здравствуйте"), msg("m2", "А всё-таки?", created=TS + 10)]
    chats.sync_account(conn, client, account, "2026-10-13T12:05:00")
    assert [t for t, _l, _a in _client_wrote(conn, card)] == ["Клиент написал: А всё-таки?"]
