"""The news of an order are its responsible's only (the owner 2026-11-03): the bell, «Уведомления», the counter and Telegram; both owners hear
of an order nobody answers for; «Срочно ответить» never of a closed order; the chats without an order go to «Ответственный за заказы этого
аккаунта» (as before when the account has none). The points' own rows and the work «за точку» are as they were. And the one-time fill of the
orders without a stored responsible."""
from __future__ import annotations

import json

from test_inquiry import message
from test_notify_by_point import OWN_ACCOUNT, POINT_ACCOUNT, card, chat, env, point
from tires_sync import chat_deals, cli, connections, order_alerts, order_arrived, order_notify, orders, shop_cart, store, users
from http_fakes import ScriptedHttp

NOW = "2026-10-15T12:00:00"
STAFF = ("owner", "zamantyres", "partner1", "olga")
OTHER_ACCOUNT = 2
TG_OK = (200, {"ok": True})


def setup(tmp_path):
    conn, box = env(tmp_path)
    users.add_user(conn, "zamantyres", "пароль-для-замана-1", NOW, role="owner")
    users.add_user(conn, "olga", "пароль-для-ольги-1", NOW, role="chats")
    conn.execute("INSERT INTO account (id, name, feed_token, created, chats_enabled) VALUES (2, 'Бета', 't2', '2026-10-01', 1)")
    store.set_state(conn, order_alerts.URGENT_START_KEY, "2026-10-01T00:00:00")
    conn.commit()
    return conn, box


def staff_list(conn):
    return [u["login"] for u in users.list_users(conn) if u["role"] not in ("client", "point")] + ["owner"]


def of_orders(conn, login, **kw):
    return {r["order_id"] for r in order_alerts.page_rows(conn, login, NOW, **kw) if r["order_id"] is not None}


def urgent(conn, login):
    return {r["chat_row"] for r in order_alerts.page_rows(conn, login, NOW) if r.get("urgent")}


def chats_seen(conn, login):
    return {r["chat_row"] for r in order_alerts.page_rows(conn, login, NOW) if r.get("chat_row")}


def test_the_owner_and_zaman_each_hear_only_of_their_own_orders(tmp_path):
    conn, box = setup(tmp_path)
    pavel, zaman = card(conn, "owner"), card(conn, "zamantyres")
    for cid in (pavel["id"], zaman["id"]):
        orders.add_event(conn, cid, NOW, "система", "reserve", "Резерв заканчивается", level="yellow")
        orders.add_event(conn, cid, NOW, "система", "notify", "Оповещение не отправлено: Telegram недоступен", level="red")
    conn.commit()
    assert of_orders(conn, "owner") == {pavel["id"]} and of_orders(conn, "zamantyres") == {zaman["id"]}
    assert order_alerts.counts(conn, "owner") == {"red": 1, "yellow": 1, "green": 1}
    assert order_alerts.counts(conn, "zamantyres") == {"red": 1, "yellow": 1, "green": 1}
    assert of_orders(conn, "partner1") == set() and order_alerts.counts(conn, "olga")["red"] == 0
    assert set(order_alerts.order_levels(conn, "zamantyres", NOW)) == {zaman["id"]}            # the dots by the numbers too
    orders.update_card(conn, pavel["id"], {"responsible": "zamantyres"}, NOW, "owner")          # given to Заман: his from now on
    assert of_orders(conn, "owner") == set() and of_orders(conn, "zamantyres") == {pavel["id"], zaman["id"]}


def test_an_order_nobody_answers_for_is_both_owners(tmp_path):
    conn, box = setup(tmp_path)
    robot = card(conn, "")                                                    # made by «чаты», no responsible
    gone = card(conn, "ghost")                                                # a person who is not in the panel
    users.add_user(conn, "off1", "пароль-выключенного-1", NOW, role="partner")
    users.set_active(conn, "off1", False, NOW, "owner")
    off = card(conn, "off1")                                                  # a switched-off person answers for nothing
    for cid in (robot["id"], gone["id"], off["id"]):
        orders.add_event(conn, cid, NOW, "система", "notify", "Оповещение не отправлено: некому", level="red")
    conn.commit()
    ids = {robot["id"], gone["id"], off["id"]}
    for login in ("owner", "zamantyres"):
        assert of_orders(conn, login) == ids and order_alerts.counts(conn, login)["red"] == 3
    for login in ("partner1", "olga"):
        assert of_orders(conn, login) == set() and order_alerts.counts(conn, login)["red"] == 0
    assert order_alerts.owner_people(conn) == ["owner", "zamantyres"]


def test_srochno_otvetit_only_to_the_responsible_and_never_of_a_closed_order(tmp_path):
    conn, box = setup(tmp_path)
    rows = {}
    for code in ("placed", "done", "cancel", "return", "shipped"):
        cid = card(conn, "zamantyres")["id"]
        conn.execute("UPDATE order_card SET stage_id = (SELECT id FROM order_stage WHERE code = ?) WHERE id = ?", (code, cid))
        rows[code] = chat(conn, OWN_ACCOUNT, cid, peer=code)
        message(conn, rows[code], "2026-10-15T10:30:00")
    nobody = card(conn, "")["id"]
    rows["nobody"] = chat(conn, OWN_ACCOUNT, nobody, peer="n")
    message(conn, rows["nobody"], "2026-10-15T10:30:00")
    assert urgent(conn, "zamantyres") == {rows["placed"], rows["nobody"]}     # done, cancel, return, shipped: closed
    assert urgent(conn, "owner") == {rows["nobody"]}
    assert urgent(conn, "olga") == set() and urgent(conn, "partner1") == set()
    assert {r["chat_row"] for r in order_alerts.urgent_rows(conn, NOW)} == {rows["placed"], rows["nobody"]}   # for everybody, the points too


def test_the_chats_without_an_order_go_to_the_account_responsible(tmp_path):
    conn, box = setup(tmp_path)
    connections.set_responsible(conn, OWN_ACCOUNT, "zamantyres", staff_list(conn), NOW, "owner")
    theirs = chat(conn, OWN_ACCOUNT, None, peer="a", unread=1)               # Шина-Колесо → Заман
    message(conn, theirs, "2026-10-15T10:30:00", text="Есть шины?")
    common = chat(conn, OTHER_ACCOUNT, None, peer="b", unread=1)             # no setting: as before, everybody
    message(conn, common, "2026-10-15T10:30:00", text="Есть диски?")
    assert chats_seen(conn, "zamantyres") == {theirs, common}
    assert urgent(conn, "zamantyres") == {theirs, common}
    for login in ("owner", "partner1", "olga"):
        assert chats_seen(conn, login) == {common} and urgent(conn, login) == {common}
    connections.set_responsible(conn, OTHER_ACCOUNT, "owner", staff_list(conn), NOW, "owner")
    assert chats_seen(conn, "owner") == {common} and chats_seen(conn, "olga") == set()
    users.set_active(conn, "zamantyres", False, NOW, "owner")                 # a switched-off responsible: as when there is none
    assert theirs in chats_seen(conn, "olga")


def test_the_points_rows_and_the_work_as_a_point_are_as_before(tmp_path):
    conn, box = setup(tmp_path)
    pid = point(conn, channel="panel")
    users.add_user(conn, "gamma1", "пароль-для-точки-1", NOW, role="point", point_id=pid)
    theirs = card(conn, "zamantyres", pid)                                    # the point's order Заман answers for
    orders.add_event(conn, theirs["id"], NOW, "система", "reserve", "Резерв заканчивается", level="yellow")
    conn.commit()
    assert theirs["id"] in of_orders(conn, "gamma1")
    assert theirs["id"] in of_orders(conn, "owner", staff=False)              # the owner working as the point: the point's rows
    assert theirs["id"] not in of_orders(conn, "owner") and theirs["id"] in of_orders(conn, "zamantyres")


def test_telegram_of_an_order_goes_to_its_responsible_only(tmp_path):
    conn, box = setup(tmp_path)
    pid = point(conn, channel="panel", account=None)
    users.add_user(conn, "gamma1", "пароль-для-точки-1", NOW, role="point", point_id=pid)
    office = ["owner", "partner1"]
    assert order_alerts.told_people(conn, card(conn, "zamantyres")["id"], office) == ["zamantyres"]
    assert order_alerts.told_people(conn, card(conn, "")["id"], office) == ["owner", "zamantyres"]
    assert order_alerts.told_people(conn, card(conn, "gamma1", pid)["id"], office) == office      # a point's: as before
    told = []
    mine = card(conn, "zamantyres", pid)
    robot = card(conn, "", pid)
    conn.execute("UPDATE order_card SET stage_id = (SELECT id FROM order_stage WHERE code = 'at_point') WHERE id IN (?, ?)",
                 (mine["id"], robot["id"]))
    conn.commit()
    found = [{"card": orders.get_card(conn, mine["id"]), "reason": "нет чата"}, {"card": orders.get_card(conn, robot["id"]), "reason": "нет чата"}]
    order_arrived.untold, keep = (lambda c, now: found), order_arrived.untold
    try:
        assert order_arrived.check(conn, NOW, lambda logins, text: told.append(logins)) == 2
    finally:
        order_arrived.untold = keep
    assert told == [["zamantyres"], ["owner", "zamantyres"]]


def test_a_failed_telegram_of_a_responsible_stays_on_their_bell(tmp_path):
    conn, box = setup(tmp_path)
    order_notify.set_user_chat(conn, "zamantyres", "555", NOW, "owner")
    theirs = card(conn, "zamantyres")
    tg = ScriptedHttp((403, {}))
    outcome = order_notify.deliver(conn, box, theirs, "Позиция появилась", NOW, tg_http=tg)
    assert len(tg.calls) == 1 and "ответственному" in outcome                # nothing to the owner's chat
    assert order_alerts.counts(conn, "zamantyres")["red"] == 1 and order_alerts.counts(conn, "owner")["red"] == 0
    tg = ScriptedHttp(TG_OK, TG_OK)
    outcome = order_notify.deliver(conn, box, card(conn, "ghost"), "Позиция появилась", NOW, tg_http=tg)
    assert {json.loads(c[3])["chat_id"] for c in tg.calls} == {"1000", "555"}  # nobody answers for it: both owners
    assert order_alerts.counts(conn, "owner")["red"] == 1 and order_alerts.counts(conn, "zamantyres")["red"] == 2


def _made(conn, author, source="", responsible=""):
    return orders.create_card(conn, {"client_name": "Иван", "source": source, "responsible": responsible}, NOW, author)


def test_the_fill_of_the_old_orders_follows_the_rules_of_a_new_one(tmp_path, capsys):
    conn, box = setup(tmp_path)
    connections.set_responsible(conn, OWN_ACCOUNT, "owner", staff_list(conn), NOW, "owner")
    connections.set_responsible(conn, OTHER_ACCOUNT, "zamantyres", staff_list(conn), NOW, "owner")
    pid = point(conn, channel="panel")                                        # Гамма: the point of POINT_ACCOUNT
    users.add_user(conn, "gamma1", "пароль-для-точки-1", NOW, role="point", point_id=pid)
    of_own = _made(conn, chat_deals.SYSTEM_AUTHOR, "avito")
    chat(conn, OWN_ACCOUNT, of_own, peer="a")
    of_other = _made(conn, "olga", "avito")                                   # made from the chat by a person: the account's rule
    chat(conn, OTHER_ACCOUNT, of_other, peer="b")
    of_point = _made(conn, chat_deals.SYSTEM_AUTHOR, "avito")
    chat(conn, POINT_ACCOUNT, of_point, peer="c")
    by_call = _made(conn, "звонки Авито", "avito")
    conn.execute("INSERT INTO avito_call (account_id, call_id, call_at, order_id) VALUES (2, 1, ?, ?)", (NOW, by_call))
    shop_cart.set_responsible(conn, "partner1", NOW, "owner")
    shop = _made(conn, "client1", "shop")
    manual = _made(conn, "zamantyres")
    acting = _made(conn, "Павел за точку «Гамма»")
    robot = _made(conn, chat_deals.SYSTEM_AUTHOR, "avito")                    # no chat left: nobody
    named = _made(conn, "owner", responsible="olga")                           # has one: untouched
    conn.commit()
    want = {of_own: "owner", of_other: "zamantyres", of_point: "gamma1", by_call: "zamantyres", shop: "partner1", manual: "zamantyres"}
    dry = chat_deals.fill_responsible(conn, NOW, "cli")
    assert dry["set"] == want and sorted(dry["left"]) == sorted([acting, robot])
    assert dry["by"] == {"аккаунт Авито": 4, "каталог клиента": 1, "кто создал": 1}
    assert conn.execute("SELECT COUNT(*) FROM order_card WHERE responsible = ''").fetchone()[0] == 8      # dry: nothing written
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    assert cli.main(["orders-fill-responsible", "--db", db, "--apply"]) == 0
    assert "записано: 6; без ответственного осталось: 2" in capsys.readouterr().out
    stored = dict(conn.execute("SELECT id, responsible FROM order_card"))
    assert {k: stored[k] for k in want} == want and stored[named] == "olga" and stored[acting] == stored[robot] == ""
    audit = json.loads(conn.execute("SELECT detail FROM audit WHERE action = ?", (chat_deals.FILL_AUDIT,)).fetchone()[0])
    assert audit["set"] == {str(k): v for k, v in want.items()} and sorted(audit["left"]) == sorted([acting, robot])
    assert not conn.execute("SELECT 1 FROM order_event WHERE kind = 'field' AND text LIKE 'Ответственный:%'").fetchone()   # no «taken»
    assert chat_deals.fill_responsible(conn, NOW, "cli", apply=True)["set"] == {}                          # a second run: nothing new
