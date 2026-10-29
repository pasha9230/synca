"""«Моя точка»: the phone page of a point's manager. Its buttons do what the VK commands do (the same result, the same records), the point
is taken in the query, and the client hears «можно забирать» once per arrival."""
from __future__ import annotations

import re

from panel_helpers import post
from test_vk_commands import SENT, _card, _stage, hook, ready, to_client, to_points
from tires_sync import order_points as points, orders, store, users

PASSWORD = "password-123"


def manager(app, conn, point_id, login_="gamma"):
    users.add_user(conn, login_, PASSWORD, "2026-10-13T12:00:00", role="point", point_id=point_id)
    conn.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = login_
        session["epoch"] = store.get_session_epoch(conn, login_)
        session["csrf"] = "tok"
    return client


def token(page, card, action):
    found = re.search(r'action="/my-point/%d/%s"[^>]*>.*?name="token" value="([0-9a-f]+)"' % (card, action), page, re.S)
    assert found, (card, action)
    return found.group(1)


def press(client, card, action, tok="tok", p=None, again=None):
    page = client.get("/my-point" + ("?p=%d" % p if p else "")).get_data(as_text=True)
    data = dict(token=again or token(page, card, action))
    if p:
        data["p"] = str(p)
    r = post(client, "/my-point/%d/%s" % (card, action), tok, **data)
    assert r.status_code == 302, r.status_code
    return data["token"], client.get(r.headers["Location"]).get_data(as_text=True)


def two(tmp_path, *avito):
    """Two panels side by side: one gets the VK command, the other the button."""
    (tmp_path / "vk").mkdir()
    (tmp_path / "page").mkdir()
    return ready(tmp_path / "vk", *avito), ready(tmp_path / "page", *avito)


def journal(conn, card):
    return [(e["kind"], e["text"], e["level"]) for e in reversed(orders.list_events(conn, card))]


# --- what the manager sees ---------------------------------------------------------------------------------------------------------

def test_the_manager_sees_the_orders_of_the_own_point_only_with_phone_items_and_term(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    other = points.save_point(conn, dict(name="Гамма", channel="vk", vk_peer_id="777"))
    theirs = _card(conn, other, webmim="700100")
    orders.update_card(conn, theirs, dict(client_name="Чужой Клиент", client_phone="+7 911 000 00 00"), "2026-10-13T12:00:00", "o")
    here = _card(conn, pid, webmim="700200", code="at_point")
    mine = manager(app, conn, pid)
    page = mine.get("/my-point?p=%d" % other).get_data(as_text=True)                      # asking for another point gives the own one
    assert "Бета" in page and "5000100" in page and "700200" in page
    assert "700100" not in page and "Чужой Клиент" not in page and "+7 911" not in page
    assert 'href="tel:+79001112233"' in page and "Nokian Hakkapeliitta 10" in page and "Кол-во 4 шт" in page    # the VK text (owner 2026-10-26)
    assert "Хранение до" in page                                                          # the order at the point has its term
    assert 'class="sidebar"' in page and "mypoint.css" in page                             # in the panel's frame with the menu (the owner 2026-10-29)
    assert page.index("700200") < page.index("5000100")                                   # newest first (same last event: the later order)
    assert '/my-point/%d/arrived' % card in page and '/my-point/%d/issued' % here in page and '/my-point/%d/no_show' % here in page
    assert '/my-point/%d/no_show' % card not in page                                      # only the marks that fit the stage («выдал» fits on the way too: owner 2026-10-21)
    assert 'data-confirm-title="Выдать заказ' in page and 'data-confirm-title="Клиент не пришёл?"' in page       # the panel's window (UX 1.0g)


def test_an_order_of_another_point_cannot_be_marked_even_by_its_address(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    other = points.save_point(conn, dict(name="Гамма", channel="vk", vk_peer_id="777"))
    theirs = _card(conn, other, webmim="700100")
    mine = manager(app, conn, pid)
    r = post(mine, "/my-point/%d/arrived" % theirs, "tok", token="a" * 24, p=str(other))
    assert r.status_code in (403, 404)
    assert orders.get_card(conn, theirs)["stage"]["code"] == "moving" and to_client(app) == []


def test_the_owner_picks_a_point_and_other_roles_have_no_page(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    other = points.save_point(conn, dict(name="Гамма", channel="vk", vk_peer_id="777"))
    _card(conn, other, webmim="700100")
    page = client.get("/my-point?p=%d" % other).get_data(as_text=True)
    assert "700100" in page and "5000100" not in page and '<option value="%d" selected>Гамма' % other in page
    for role in ("chats", "viewer"):
        users.add_user(conn, role, PASSWORD, "2026-10-13T12:00:00", role=role)
        conn.commit()
        someone = app.test_client()
        with someone.session_transaction() as session:
            session["user"], session["epoch"], session["csrf"] = role, store.get_session_epoch(conn, role), "tok"
        assert someone.get("/my-point").status_code == 403, role
        assert post(someone, "/my-point/%d/arrived" % card, "tok", token="a" * 24).status_code == 403, role


def test_the_point_starts_on_its_page_and_keeps_only_its_own_doors(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    assert mine.get("/").headers["Location"].endswith("/my-point")
    assert mine.get("/my-point").status_code == 200 and mine.get("/account/password").status_code == 200
    for url in ("/catalog", "/economics", "/connections", "/users", "/clients/duplicates", "/shares"):     # «Чаты», «Уведомления», «CRM»: its own
        assert mine.get(url).status_code == 403, url


# --- the buttons do what the VK commands do -----------------------------------------------------------------------------------------

def test_arrived_moves_the_order_and_tells_the_client_once_as_the_vk_command(tmp_path):
    (vk_app, _c, _t, vk_conn, _p, vk_card), (app, client, tok, conn, pid, card) = two(tmp_path, SENT)
    hook(vk_app, vk_conn, "Приехал 5000100")
    mine = manager(app, conn, pid)
    _tok, page = press(mine, card, "arrived")
    got, vk_got = orders.get_card(conn, card), orders.get_card(vk_conn, vk_card)
    assert got["stage"]["code"] == vk_got["stage"]["code"] == "at_point" and got["reserve_until"] == vk_got["reserve_until"]
    assert to_client(app) == to_client(vk_app) and len(to_client(app)) == 1                 # the same «можно забирать», word for word
    assert [(k, t) for k, t, _l in journal(conn, card)] == [(k, t) for k, t, _l in journal(vk_conn, vk_card)]
    assert "Заказ №5000100 — отмечено: приехал" in page and "Клиенту отправлено" in page
    log = conn.execute("SELECT word, order_id, outcome FROM order_command_log WHERE message_id LIKE 'page-%'").fetchall()
    vk_log = vk_conn.execute("SELECT word, order_id, outcome FROM order_command_log").fetchall()
    assert [(w, o) for w, _i, o in log] == [(w, o) for w, _i, o in vk_log] and log[0][1] == card


def test_a_second_press_sends_nothing_twice(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    mine = manager(app, conn, pid)
    first, _page = press(mine, card, "arrived")
    _t, page = press(mine, card, "arrived", again=first)                                   # the same form sent again (a double tap)
    assert "уже выполнена" in page and len(to_client(app)) == 1
    r = post(mine, "/my-point/%d/arrived" % card, "tok", token="e" * 24)                    # a new press from an old page (another tab)
    assert "уже отмечен" in mine.get(r.headers["Location"]).get_data(as_text=True) and len(to_client(app)) == 1
    hook(app, conn, "приехал 5000100", event="vk-after")                                   # and the VK command after the button
    assert len(to_client(app)) == 1
    assert [o for (o,) in conn.execute("SELECT outcome FROM order_command_log WHERE message_id = 'page-' || ?", ("e" * 24,))] == [
        "уже отмечено; клиенту отправлено"]


def test_issued_closes_the_order_and_tells_the_client_nothing(tmp_path):
    (vk_app, _c, _t, vk_conn, _p, vk_card), (app, client, tok, conn, pid, card) = two(tmp_path)
    orders.move_card(vk_conn, vk_card, _stage(vk_conn, "at_point"), "2026-10-13T12:00:00", "o")
    hook(vk_app, vk_conn, "выдал 5000100")
    orders.move_card(conn, card, _stage(conn, "at_point"), "2026-10-13T12:00:00", "o")
    conn.commit()
    mine = manager(app, conn, pid)
    _t, page = press(mine, card, "issued")
    assert orders.get_card(conn, card)["stage"]["code"] == "done" and to_client(app) == [] == to_client(vk_app)
    assert [(k, t) for k, t, _l in journal(conn, card)] == [(k, t) for k, t, _l in journal(vk_conn, vk_card)]
    assert "отмечено: выдан" in page
    assert "5000100" not in mine.get("/my-point").get_data(as_text=True)                  # a closed order leaves the page


def test_no_show_writes_the_red_record_as_the_vk_command_and_the_client_hears_nothing(tmp_path):
    (vk_app, _c, _t, vk_conn, _p, vk_card), (app, client, tok, conn, pid, card) = two(tmp_path)
    orders.move_card(vk_conn, vk_card, _stage(vk_conn, "at_point"), "2026-10-13T12:00:00", "o")
    hook(vk_app, vk_conn, "клиент не пришёл 5000100")
    orders.move_card(conn, card, _stage(conn, "at_point"), "2026-10-13T12:00:00", "o")
    conn.commit()
    mine = manager(app, conn, pid)
    _t, page = press(mine, card, "no_show")
    red = [(k, t) for k, t, l in journal(conn, card) if l == "red"]
    assert red and red == [(k, t) for k, t, l in journal(vk_conn, vk_card) if l == "red"]
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point" and to_client(app) == []
    assert "отмечено: клиент не пришёл" in page


def test_no_show_of_the_point_account_order_tells_the_staff_and_not_the_point_by_vk_and_by_button(tmp_path):
    """The owner 2026-10-26: the point says «клиент не пришёл» about an order of its own Avito account; the staff call the client."""
    (vk_app, _c, _t, vk_conn, vk_pid, vk_card), (app, client, tok, conn, pid, card) = two(tmp_path)
    for c, point_id, order in ((vk_conn, vk_pid, vk_card), (conn, pid, card)):
        account = c.execute("SELECT account_id FROM avito_chat WHERE order_id = ?", (order,)).fetchone()[0]
        c.execute("UPDATE point SET avito_account_id = ? WHERE id = ?", (account, point_id))
        orders.move_card(c, order, _stage(c, "at_point"), "2026-10-13T12:00:00", "o")
        c.commit()
        assert points.of_account(c, order) is not None
    hook(vk_app, vk_conn, "клиент не пришёл 5000100")
    said = [text for _peer, text in to_points(vk_app)]
    assert said and all("Клиент не пришёл за заказом" not in t for t in said) and any("С клиентом свяжутся" in t for t in said)
    _t, page = press(manager(app, conn, pid), card, "no_show")
    assert "отмечено: клиент не пришёл" in page and points.unseen_alerts(conn, pid, "") == []
    for c, order in ((vk_conn, vk_card), (conn, card)):
        told = [t for k, t, _l in journal(c, order) if k == "notify"]
        assert told and not any("точк" in t for t in told), told


def test_a_mark_that_does_not_fit_the_stage_changes_nothing(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    mine = manager(app, conn, pid)
    for action in ("no_show",):                                                              # the order is still on its way («выдал» fits it: test_issued_stage.py)
        r = post(mine, "/my-point/%d/%s" % (card, action), "tok", token=("c%d" % len(action)).ljust(24, "0"))
        page = mine.get(r.headers["Location"]).get_data(as_text=True)
        assert "не подходит" in page or "отмечают, когда заказ на точке" in page
    assert orders.get_card(conn, card)["stage"]["code"] == "moving" and to_client(app) == []
    assert not [e for e in orders.list_events(conn, card) if e["level"] == "red"]
    assert post(mine, "/my-point/%d/explode" % card, "tok", token="d" * 24).status_code == 404
    page = mine.get("/my-point").get_data(as_text=True)
    assert post(mine, "/my-point/%d/arrived" % card, "tok", token="not-a-token").status_code == 302
    assert orders.get_card(conn, card)["stage"]["code"] == "moving" and "5000100" in page


def test_the_vk_command_then_the_button_tell_the_client_once(tmp_path):
    """Team lead's check: a press on the page and the same command from VK on one order never give the client two «можно забирать»."""
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    mine = manager(app, conn, pid)
    page = mine.get("/my-point").get_data(as_text=True)
    stale = token(page, card, "arrived")                                                   # the page was open before the VK command
    hook(app, conn, "приехал 5000100", event="vk-first")
    assert len(to_client(app)) == 1 and orders.get_card(conn, card)["stage"]["code"] == "at_point"
    r = post(mine, "/my-point/%d/arrived" % card, "tok", token=stale)
    assert "уже отмечен" in mine.get(r.headers["Location"]).get_data(as_text=True)
    assert len(to_client(app)) == 1


def test_each_button_of_a_card_is_its_own_press(tmp_path):
    """«Клиент не пришёл», then the client came: «Выдал» from the same page is a new press, not «уже выполнена» (it would leave the order)."""
    app, client, tok, conn, pid, card = ready(tmp_path)
    orders.move_card(conn, card, _stage(conn, "at_point"), "2026-10-13T12:00:00", "o")
    conn.commit()
    mine = manager(app, conn, pid)
    page = mine.get("/my-point").get_data(as_text=True)
    assert token(page, card, "no_show") != token(page, card, "issued")
    post(mine, "/my-point/%d/no_show" % card, "tok", token=token(page, card, "no_show"))
    r = post(mine, "/my-point/%d/issued" % card, "tok", token=token(page, card, "issued"))
    assert "отмечено: выдан" in mine.get(r.headers["Location"]).get_data(as_text=True)
    assert orders.get_card(conn, card)["stage"]["code"] == "done"


def test_a_failure_after_the_press_is_logged_and_changes_nothing(tmp_path, monkeypatch):
    from tires_sync.panel import routes_vk
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    mine = manager(app, conn, pid)
    monkeypatch.setattr(routes_vk, "channel", lambda *a: (_ for _ in ()).throw(RuntimeError("database is locked")))
    tok_, page = press(mine, card, "arrived")
    assert "Не получилось выполнить отметку" in page and to_client(app) == []
    assert orders.get_card(conn, card)["stage"]["code"] == "moving"
    assert conn.execute("SELECT outcome FROM order_command_log WHERE message_id = ?", ("page-" + tok_,)).fetchone() == ("ошибка при выполнении",)


def test_a_manager_of_a_deleted_point_gets_no_page_and_no_error(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    gone = points.save_point(conn, dict(name="Закрытая", channel="vk", vk_peer_id="888"))
    mine = manager(app, conn, gone, login_="closed")
    points.delete_point(conn, gone)
    conn.commit()
    assert mine.get("/my-point").status_code == 403


# --- «Передать клиента» (lane 4B2): the VK command «клиент <телефон> <что нужно>» as a form -------------------------------------------

def handoff_token(page):
    found = re.search(r'<form method="post" action="/my-point" class="mp-handoff-form"[^>]*>.*?name="token" value="([0-9a-f]+)"', page, re.S)
    assert found, "no handoff form"
    return found.group(1)


def hand_over(client, phone, need, tok_=None, p=None):
    page = client.get("/my-point" + ("?p=%d" % p if p else "")).get_data(as_text=True)
    data = dict(token=tok_ or handoff_token(page), phone=phone, need=need)
    if p:
        data["p"] = str(p)
    return data["token"], post(client, "/my-point", "tok", **data)


def handed(conn):
    return conn.execute("SELECT order_id, point_id, phone, need, responsible FROM order_handoff ORDER BY rowid").fetchall()


def test_the_form_hands_the_client_over_as_the_vk_command(tmp_path):
    from tires_sync import order_manager as mgr
    (vk_app, _c, _t, vk_conn, _p, _card1), (app, client, tok, conn, pid, card) = two(tmp_path)
    for c in (vk_conn, conn):
        mgr.set_duty(c, {d: "worker2" for d in range(7)}, (10, 20), "", "2026-10-13")
        users.add_user(c, "worker2", PASSWORD, "2026-10-13T12:00:00", role="partner", display_name="Партнёр")
        c.commit()
    hook(vk_app, vk_conn, "клиент 8 (900) 222-33-44 зимние 205/55 R16, 4 шт", event="h1")
    mine = manager(app, conn, pid)
    _tok, r = hand_over(mine, "8 (900) 222-33-44", "зимние 205/55   R16, 4 шт")
    assert r.status_code == 302
    page = mine.get(r.headers["Location"]).get_data(as_text=True)
    [(order_id, point_id, phone, need, who)] = handed(conn)
    [(_vk_order, vk_point, vk_phone, vk_need, vk_who)] = handed(vk_conn)
    assert (point_id, phone, need, who) == (vk_point, vk_phone, vk_need, vk_who) == (pid, "+79002223344", "зимние 205/55 R16, 4 шт", "worker2")
    card = orders.get_card(conn, order_id)
    assert card["stage"]["code"] == "from_point" and card["source"] == "pvz" and card["responsible"] == "worker2"
    assert "Передано, ответственный — Партнёр. Заказ №%d." % order_id in page
    assert [e["text"] for e in orders.list_events(conn, order_id) if e["kind"] == "handoff"] == [          # the same record, its source named
        e["text"].replace("(команда ВК)", "(«Моя точка»)") for e in orders.list_events(vk_conn, _vk_order) if e["kind"] == "handoff"]
    assert to_client(app) == []                                                              # nothing goes to the client


def test_a_double_press_hands_one_client_over(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    first, _r = hand_over(mine, "+7 900 222-33-44", "летние")
    _t, r = hand_over(mine, "+7 900 222-33-44", "летние", tok_=first)
    assert "уже передан" in mine.get(r.headers["Location"]).get_data(as_text=True) and len(handed(conn)) == 1


def test_the_manager_hands_over_to_the_own_point_only_and_the_owner_to_the_chosen_one(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    other = points.save_point(conn, dict(name="Гамма", channel="vk", vk_peer_id="777"))
    conn.execute("UPDATE point SET vk_peer_id = '777' WHERE id = ?", (other,))
    conn.commit()
    mine = manager(app, conn, pid)
    hand_over(mine, "+7 900 222-33-44", "", p=other)                                      # the manager's «p» is not listened to
    _t, _r = hand_over(client, "+7 900 333-44-55", "", p=other)                              # the owner hands over for the point picked
    assert [row[1] for row in handed(conn)] == [pid, other]


def test_a_wrong_phone_makes_nothing_and_says_so_under_the_field(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    for phone in ("12345", "", "8 900 111 22 3", "+1 202 555 0100"):
        _t, r = hand_over(mine, phone, "зимние шины")
        page = r.get_data(as_text=True)
        assert r.status_code == 400 and 'id="mp-handoff-err"' in page and "10 цифр" in page, phone
        assert ">зимние шины</textarea>" in page                                             # what was written stays in the field
    assert handed(conn) == [] and conn.execute("SELECT COUNT(*) FROM order_command_log").fetchone()[0] == 0


def test_a_point_without_vk_hands_over_from_the_page_too(tmp_path):
    """The owner 2026-10-23 (the points work in the panel): the page needs no VK of the point; before, it needed one as the sender."""
    app, client, tok, conn, pid, card = ready(tmp_path)
    loose = points.save_point(conn, dict(name="Без ВК", channel="telegram"))
    mine = manager(app, conn, loose, login_="loose")
    page = mine.get("/my-point").get_data(as_text=True)
    assert "Передать клиента" in page and "mp-handoff-form" in page and "привязана к ВК" not in page
    r = post(mine, "/my-point", "tok", token="f" * 24, phone="+7 900 222-33-44", need="")
    assert r.status_code == 302 and len(handed(conn)) == 1


def test_the_phone_field_and_the_dictation_button(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    page = manager(app, conn, pid).get("/my-point").get_data(as_text=True)
    form = re.search(r'<form method="post" action="/my-point" class="mp-handoff-form".*?</form>', page, re.S).group(0)
    assert 'type="tel" name="phone" required' in form and 'maxlength="500"' in form
    assert re.search(r"<button[^>]*data-dictate[^>]*hidden", form) and "mypoint.js" in page and "<script>" not in page


DICTATE_HARNESS = r"""
const fs = require("fs");
const make = (tag) => ({tag, hidden: true, value: "", textContent: "Надиктовать", listeners: {}, classList: {add() {}, remove() {}},
  addEventListener(t, f) { this.listeners[t] = f; }, dispatchEvent() {}, closest() { return form; }, querySelector() { return field; }});
const field = make("textarea"), button = make("button"), form = make("form");
global.Event = function (t) { this.type = t; };
global.document = {querySelector: (s) => s === "[data-dictate]" ? button : null};
const run = (withSpeech) => {
  button.hidden = true; field.value = "летние";
  global.window = withSpeech ? {webkitSpeechRecognition: function () { global.REC = this; this.start = () => {}; this.stop = () => {}; }} : {};
  eval(fs.readFileSync(process.argv[2], "utf8"));
};
const out = {};
run(false); out.no_speech_hidden = button.hidden === true;
run(true); out.unknown_policy_hidden = button.hidden === true;                    // Safari: no way to know the site allows it
document.featurePolicy = {allowsFeature: () => true};                               // a site that allows the microphone
run(true); out.speech_shown = button.hidden === false;
button.listeners.click();
global.REC.onresult({results: [[{transcript: "205/55 R16 4 шт"}]]});
out.appended = field.value === "летние 205/55 R16 4 шт" && global.REC.lang === "ru-RU";
global.REC.onresult({results: [[{transcript: "ш".repeat(600)}]]});
out.clamped = field.value.length === 500;
document.featurePolicy = {allowsFeature: (f) => f !== "microphone"};                 // the site forbids the microphone (Permissions-Policy)
run(true); out.policy_hidden = button.hidden === true;
delete document.featurePolicy;
console.log(JSON.stringify(out));
"""


def test_the_dictation_appears_only_with_the_browsers_microphone_and_adds_to_the_text(tmp_path):
    import json
    import shutil
    import subprocess
    from pathlib import Path
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("no node")
    harness = tmp_path / "dictate.js"
    harness.write_text(DICTATE_HARNESS, encoding="utf-8")
    js = Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static" / "mypoint.js"
    out = subprocess.run([node, str(harness), str(js)], capture_output=True, text=True, timeout=30, check=True).stdout.strip().splitlines()[-1]
    assert json.loads(out) == {"no_speech_hidden": True, "unknown_policy_hidden": True, "speech_shown": True, "appended": True, "clamped": True,
                               "policy_hidden": True}


def test_the_panel_forbids_the_microphone_so_the_page_has_no_working_dictation_yet(tmp_path):
    """Team lead's decision 2026-10-21 (variant B): the panel's Permissions-Policy stays; the button exists in the page but mypoint.js shows
    it only where the browser confirms the microphone is allowed, i.e. nowhere until the owner decides."""
    app, client, tok, conn, pid, card = ready(tmp_path)
    r = manager(app, conn, pid).get("/my-point")
    assert "microphone=()" in r.headers.get("Permissions-Policy", "")


# --- review round 1 of the handoff form --------------------------------------------------------------------------------------------

def test_the_order_says_who_handed_the_client_over_from_the_page(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    _t, _r = hand_over(client, "+7 900 222-33-44", "", p=pid)                                # the owner, for the point
    [(order_id, *_rest)] = handed(conn)
    notes = [(e["author"], e["text"]) for e in orders.list_events(conn, order_id) if e["kind"] == "note"]
    assert notes and notes[0][1].startswith("Клиент передан через «Моя точка»") and notes[0][0] != "менеджер Бета"


def test_the_owner_hands_over_only_to_the_point_on_the_form(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    page = client.get("/my-point?p=%d" % pid).get_data(as_text=True)
    for p in ("999", ""):                                                                     # the point is gone, or no point in the form
        r = post(client, "/my-point", "tok", token=handoff_token(page), phone="+7 900 222-33-44", need="", **({"p": p} if p else {}))
        assert r.status_code == 400 and "Точка не выбрана" in r.get_data(as_text=True), p
    assert handed(conn) == []


def test_a_failure_after_the_order_was_made_still_says_passed_and_makes_one_order(tmp_path, monkeypatch):
    from tires_sync.panel import routes_mypoint
    app, client, tok, conn, pid, card = ready(tmp_path)
    real = routes_mypoint.mgr.handoff

    def breaks_after(*a, **k):
        real(*a, **k)
        raise RuntimeError("the alert failed")
    monkeypatch.setattr(routes_mypoint.mgr, "handoff", breaks_after)
    mine = manager(app, conn, pid)
    first, r = hand_over(mine, "+7 900 222-33-44", "летние")
    assert r.status_code == 302 and "Передано, ответственный" in mine.get(r.headers["Location"]).get_data(as_text=True)
    assert len(handed(conn)) == 1


def test_a_failed_handoff_keeps_the_text_and_a_resend_is_not_called_passed(tmp_path, monkeypatch):
    app, client, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    real = orders.create_card
    monkeypatch.setattr(orders, "create_card", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("database is locked")))
    first, r = hand_over(mine, "+7 900 222-33-44", "зимние")
    page = r.get_data(as_text=True)
    assert r.status_code == 400 and "Не получилось принять клиента" in page and ">зимние</textarea>" in page
    monkeypatch.setattr(orders, "create_card", real)
    _t, r = hand_over(mine, "+7 900 222-33-44", "зимние", tok_=first)                      # «Назад» and the same form again
    page = r.get_data(as_text=True)
    assert "уже передан" not in page and "Прошлая попытка не удалась" in page and handed(conn) == []


def test_what_is_needed_is_cleaned_as_the_vk_command_and_too_long_is_refused(tmp_path):
    from tires_sync import order_manager as mgr
    app, client, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    _t, r = hand_over(mine, "+7 900 222-33-44", "x" * 501)
    assert r.status_code == 400 and "500" in r.get_data(as_text=True) and handed(conn) == []
    hand_over(mine, "+7 900 222-33-44", " - 4 шт.  ")
    assert handed(conn)[0][3] == mgr.parse_handoff("клиент 89002223344 - 4 шт.  ")["need"] == "4 шт"
