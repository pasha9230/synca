"""The client: phones kept in E.164, the joining of the old orders and chats (a report first, --confirm writes, a repeat changes nothing),
a new order or a deal from a chat joins its client, merging and splitting, the card hides the money without the right."""
from __future__ import annotations

import pytest

from panel_helpers import login, make_app, post
from order_fixtures import card_of_phone
from tires_sync import chat_deals, clients, connections, orders, sales, store, users
from tires_sync.cli import main

NOW = "2026-10-21T12:00:00"


@pytest.fixture
def conn(tmp_path):
    c = store.connect(str(tmp_path / "t.db"))
    orders.ensure_pipeline(c, NOW)
    return c


def _chat(conn, peer, name="Татьяна", account=1, order_id=None, chat_id=None):
    return conn.execute("INSERT INTO avito_chat (account_id, chat_id, peer_id, peer_name, order_id) VALUES (?,?,?,?,?)",
                        (account, chat_id or "c-%s-%s" % (peer, conn.execute("SELECT COUNT(*) FROM avito_chat").fetchone()[0]), peer, name, order_id)).lastrowid


def _forget_clients(conn):
    """The data as the old code left it: orders and chats, no clients."""
    for table in ("client", "client_phone", "client_peer", "client_order", "client_chat"):
        conn.execute("DELETE FROM %s" % table)
    conn.commit()


@pytest.mark.parametrize("raw, phone", [("8 (900) 111-22-33", "+79001112233"), ("+7 900 1112233", "+79001112233"), ("9001112233", "+79001112233"),
                                        ("79001112233", "+79001112233"), ("+44 20 7946 0958", "+442079460958"), ("12345", ""), ("", ""),
                                        ("4420794609", "+74420794609")])
def test_the_phone_is_kept_in_e164_and_shown_the_russian_way(raw, phone):
    assert clients.normalize(raw) == phone
    assert clients.display("+79001112233") == "+7 900 111-22-33"


def test_two_orders_of_one_phone_and_chats_of_one_person_join_one_client(conn):
    a = card_of_phone(conn, {"client_name": "Иван", "client_phone": "8 900 111-22-33"}, NOW, "o")
    b = card_of_phone(conn, {"client_name": "Иван П.", "client_phone": "+7 (900) 111 22 33"}, NOW, "o")
    assert orders.get_card(conn, a)["client"]["id"] == orders.get_card(conn, b)["client"]["id"]
    other = card_of_phone(conn, {"client_name": "Пётр", "client_phone": "+7 900 999-88-77"}, NOW, "o")
    assert orders.get_card(conn, other)["client"]["id"] != orders.get_card(conn, a)["client"]["id"]
    quiet = card_of_phone(conn, {"client_name": "Без телефона"}, NOW, "o")
    assert orders.get_card(conn, quiet)["client"] is None
    orders.update_card(conn, quiet, {"client_phone": "89001112233"}, NOW, "o")                    # the phone came later
    assert orders.get_card(conn, quiet)["client"]["id"] == orders.get_card(conn, a)["client"]["id"]
    assert orders.get_card(conn, a)["client_phone"] == "8 900 111-22-33"                          # the order itself keeps what was typed


def test_a_deal_from_a_chat_joins_the_client_of_the_peer(conn):
    first = _chat(conn, "p-9")
    card = chat_deals.create_deal(conn, first, NOW)
    client_id = orders.get_card(conn, card)["client"]["id"]
    assert clients.client_of_chat(conn, first)["id"] == client_id
    second = _chat(conn, "p-9", chat_id="c-other-ad")                                               # the same person about another ad
    card2 = chat_deals.create_deal(conn, second, NOW)
    assert orders.get_card(conn, card2)["client"]["id"] == client_id == clients.client_of_chat(conn, second)["id"]


def test_the_command_reports_first_writes_with_confirm_and_a_repeat_changes_nothing(conn, tmp_path, capsys):
    a = card_of_phone(conn, {"client_name": "Иван", "client_phone": "8 900 111-22-33"}, NOW, "o")
    b = card_of_phone(conn, {"client_name": "Иван", "client_phone": "+79001112233"}, NOW, "o")
    c = card_of_phone(conn, {"client_name": "Пётр", "client_phone": "+79009998877"}, NOW, "o")
    lone = card_of_phone(conn, {"client_name": "Никто"}, NOW, "o")
    chat_a = _chat(conn, "p-1", order_id=a)                                                         # bound to Иван's order
    chat_x = _chat(conn, "p-1")                                                                     # the same peer, another chat
    chat_y = _chat(conn, "p-2", name="Ольга")                                                       # a person with no order: no client yet
    _forget_clients(conn)
    db = str(tmp_path / "t.db")
    assert main(["clients-link", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "новых клиентов: 2" in out and "заказов свяжется: 3" in out and "чатов: 2" in out and "ничего не записано" in out
    assert conn.execute("SELECT COUNT(*) FROM client").fetchone()[0] == 0                        # the report wrote nothing
    before = conn.execute("SELECT id, client_phone, client_name FROM order_card ORDER BY id").fetchall()
    assert main(["clients-link", "--db", db, "--confirm"]) == 0
    assert conn.execute("SELECT COUNT(*) FROM client").fetchone()[0] == 2
    ivan = clients.client_of_order(conn, a)["id"]
    assert clients.client_of_order(conn, b)["id"] == ivan == clients.client_of_chat(conn, chat_a)["id"] == clients.client_of_chat(conn, chat_x)["id"]
    assert clients.client_of_order(conn, c)["id"] != ivan and clients.client_of_order(conn, lone) is None
    assert clients.client_of_chat(conn, chat_y) is None
    assert conn.execute("SELECT id, client_phone, client_name FROM order_card ORDER BY id").fetchall() == before    # orders untouched
    capsys.readouterr()
    assert main(["clients-link", "--db", db, "--confirm"]) == 0
    assert "новых клиентов: 0" in capsys.readouterr().out and conn.execute("SELECT COUNT(*) FROM client").fetchone()[0] == 2


def test_different_peers_with_one_phone_are_one_client(conn):
    a = card_of_phone(conn, {"client_name": "Иван", "client_phone": "+79001112233"}, NOW, "o")
    b = card_of_phone(conn, {"client_name": "Иван", "client_phone": "+79001112233"}, NOW, "o")
    _chat(conn, "p-1", order_id=a, account=1)
    _chat(conn, "p-7", order_id=b, account=2)                                                       # another Avito account, another peer id
    _forget_clients(conn)
    clients.apply_links(conn, clients.plan_links(conn), NOW)
    client = clients.get_client(conn, clients.client_of_order(conn, a)["id"])
    assert len(client["chats"]) == 2 and {p["peer_id"] for p in client["peers"]} == {"p-1", "p-7"} and client["phones"] == ["+79001112233"]


def test_a_group_with_two_clients_is_left_for_a_person(conn):
    a = card_of_phone(conn, {"client_name": "Иван", "client_phone": "+79001112233"}, NOW, "o")
    b = card_of_phone(conn, {"client_name": "Пётр", "client_phone": "+79009998877"}, NOW, "o")
    _chat(conn, "p-1", order_id=a)
    _chat(conn, "p-1", order_id=b)                                                                  # one peer wrote about both: who is it?
    plan = clients.plan_links(conn)
    assert len(plan["conflicts"]) == 1 and plan["chats"] == 0


def test_merge_and_split_keep_the_journal_and_survive_the_joining(conn):
    a = card_of_phone(conn, {"client_name": "Иван", "client_phone": "+79001112233"}, NOW, "o")
    b = card_of_phone(conn, {"client_name": "Ваня", "client_phone": "+79005556677"}, NOW, "o")
    ia, ib = clients.client_of_order(conn, a)["id"], clients.client_of_order(conn, b)["id"]
    clients.merge(conn, ia, ib, NOW, "owner")
    conn.commit()
    merged = clients.get_client(conn, ia)
    assert {o["id"] for o in merged["orders"]} == {a, b} and merged["phones"] == ["+79001112233", "+79005556677"]
    assert clients.get_client(conn, ib)["merged_into"] == ia
    with pytest.raises(clients.ClientError):
        clients.merge(conn, ia, ia, NOW, "owner")
    new = clients.split_order(conn, b, NOW, "owner")
    conn.commit()
    assert clients.client_of_order(conn, b)["id"] == new and clients.client_of_order(conn, a)["id"] == ia
    assert clients.plan_links(conn)["orders"] == 0 and clients.plan_links(conn)["conflicts"] == []    # the split is never undone by the joining
    actions = [x["action"] for x in store.list_audit(conn)]
    assert "client_merged" in actions and "client_split" in actions


def test_the_card_the_list_and_the_money_right(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    card = card_of_phone(conn, {"client_name": "Иван", "client_phone": "89001112233"}, NOW, "o")
    sales.add_sale(conn, day="2026-10-10", goods_id="g1", brand_key="nokian", season="winter", diameter=16, name="Nokian", ad_kind="single", pieces=4,
                   sale_total=23500, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, account_id=None, point="", note="Заказ №%d" % card,
                   now=NOW, author="o", today="2026-10-21", source="order")
    client_id = clients.client_of_order(conn, card)["id"]
    owner = app.test_client()
    tok = login(owner)
    page = owner.get("/clients/%d" % client_id).get_data(as_text=True)
    assert "+7 900 111-22-33" in page and "23 500 ₽" in page and "Заказ" in page
    assert "Иван" in owner.get("/clients?q=900111").get_data(as_text=True)
    assert 'href="/clients/%d"' % client_id in owner.get("/orders/%d" % card).get_data(as_text=True)          # from the order window
    post(owner, "/clients/%d/save" % client_id, tok, name="Иван Петров", note="любит Nokian")
    assert clients.get_client(conn, client_id)["note"] == "любит Nokian"
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    users.add_user(conn, "olya", "длинный-пароль-1", NOW, role="chats")
    users.add_user(conn, "viewer1", "длинный-пароль-1", NOW, role="viewer")
    for login_, can_edit in (("olya", True), ("viewer1", False)):
        client = app.test_client()
        with client.session_transaction() as s:
            s["user"], s["epoch"], s["csrf"] = login_, store.get_session_epoch(conn, login_), "tok"
        page = client.get("/clients/%d" % client_id).get_data(as_text=True)
        assert "Иван Петров" in page and "23 500" not in page                                  # no money right: no sum
        assert client.post("/clients/%d/save" % client_id, data={"csrf": "tok", "name": "Иван Петров", "note": "любит Nokian"}).status_code == (302 if can_edit else 403)
    pid = conn.execute("INSERT INTO point (name) VALUES ('Бета')").lastrowid
    conn.commit()
    users.add_user(conn, "pointman", "длинный-пароль-1", NOW, role="point", point_id=pid)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user"], s["epoch"] = "pointman", store.get_session_epoch(conn, "pointman")
    assert client.get("/clients").status_code == 403                                           # no «CRM» for a point (the owner 2026-10-29)
    assert client.get("/clients/%d" % client_id).status_code == 403


def test_the_old_code_reads_a_base_with_clients(conn, tmp_path):
    """Rollback: the client tables mean nothing to the code before them (they are only added)."""
    a = card_of_phone(conn, {"client_name": "Иван", "client_phone": "+79001112233"}, NOW, "o")
    assert conn.execute("SELECT client_name FROM order_card WHERE id = ?", (a,)).fetchone()[0] == "Иван"
    assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'client%'")} == {
        "client", "client_phone", "client_peer", "client_order", "client_chat", "client_call",      # client_call: the calls (2026-10-23)
        "client_tag", "client_pair_skip",                                                          # CRM (2026-10-23): the tags, «не тот же»
        "client_inquiry",                                                                          # the status of the inquiry (2026-10-23)
        "client_terms", "client_brand",                                    # the conditions of the role «Клиент» (a buyer, 2026-10-29), not of CRM
        "client_profile"}                                                  # the buyer's contacts of the cart (shop_cart, 2026-10-30): only added


def test_a_new_phone_of_a_linked_order_goes_to_its_client_unless_another_has_it(conn):
    a = card_of_phone(conn, {"client_name": "Иван", "client_phone": "+79001112233"}, NOW, "o")
    card_of_phone(conn, {"client_name": "Пётр", "client_phone": "+79009998877"}, NOW, "o")
    orders.update_card(conn, a, {"client_phone": "8 900 555-66-77"}, NOW, "o")
    client = clients.get_client(conn, clients.client_of_order(conn, a)["id"])
    assert client["phones"] == ["+79001112233", "+79005556677"]
    orders.update_card(conn, a, {"client_phone": "+79009998877"}, NOW, "o")                           # Пётр's number: never taken over
    assert clients.get_client(conn, client["id"])["phones"] == ["+79001112233", "+79005556677"]


def test_an_old_order_of_the_phone_joins_the_client_not_the_order_split_off_with_it(conn):
    a = card_of_phone(conn, {"client_name": "Иван", "client_phone": "+79001112233"}, NOW, "o")
    b = card_of_phone(conn, {"client_name": "Иван", "client_phone": "+79001112233"}, NOW, "o")
    old = card_of_phone(conn, {"client_name": "Иван", "client_phone": "+79001112233"}, NOW, "o")
    ivan = clients.client_of_order(conn, a)["id"]
    clients.split_order(conn, b, NOW, "owner")                                                        # a person said: b is somebody else
    conn.execute("DELETE FROM client_order WHERE order_id = ?", (old,))                              # an order the old code made
    conn.commit()
    plan = clients.plan_links(conn)
    assert plan["conflicts"] == [] and plan["to_existing"] == [dict(orders=[old], chats=[], phones=["+79001112233"], peers=[], client=ivan)]


# --- review round 1 ------------------------------------------------------------------------------------------------------------------

def test_our_own_and_dummy_numbers_never_join_people(conn):
    conn.execute("INSERT INTO point (name, manager_phone) VALUES ('Бета', '+7 911 222-33-44')")
    conn.commit()
    a = card_of_phone(conn, {"client_name": "Иван", "client_phone": "+79112223344"}, NOW, "o")       # the point's own number
    b = card_of_phone(conn, {"client_name": "Пётр", "client_phone": "+79112223344"}, NOW, "o")
    c = card_of_phone(conn, {"client_name": "Оля", "client_phone": "+7 000 000-00-00"}, NOW, "o")
    d = card_of_phone(conn, {"client_name": "Аня", "client_phone": "79999999999"}, NOW, "o")
    assert all(clients.client_of_order(conn, x) is None for x in (a, b, c, d))
    _forget_clients(conn)
    assert clients.plan_links(conn)["orders"] == 0


def test_the_report_shows_the_big_groups(conn, tmp_path, capsys):
    for i in range(6):
        card_of_phone(conn, {"client_name": "Имя %d" % i, "client_phone": "+79001112233"}, NOW, "o")
    _forget_clients(conn)
    assert main(["clients-link", "--db", str(tmp_path / "t.db")]) == 0
    out = capsys.readouterr().out
    assert "крупные группы" in out and "6 заказов" in out and "+7 900 111-22-33" in out


def test_a_chat_without_a_deal_alone_makes_no_client(conn):
    _chat(conn, "p-5")
    _chat(conn, "p-5")                                                                                # the same person, two questions, no order
    a = card_of_phone(conn, {"client_name": "Иван", "client_phone": "+79001112233"}, NOW, "o")
    with_deal = _chat(conn, "p-6", order_id=a)
    _forget_clients(conn)
    plan = clients.plan_links(conn)
    assert plan["chats"] == 1 and [e["chats"] for e in plan["new_clients"]] == [[with_deal]]


def test_the_journal_of_a_merge_lists_what_moved(conn):
    a = card_of_phone(conn, {"client_name": "Иван", "client_phone": "+79001112233"}, NOW, "o")
    b = card_of_phone(conn, {"client_name": "Ваня", "client_phone": "+79005556677"}, NOW, "o")
    ia, ib = clients.client_of_order(conn, a)["id"], clients.client_of_order(conn, b)["id"]
    clients.merge(conn, ia, ib, NOW, "owner")
    entry = next(x for x in store.list_audit(conn) if x["action"] == "client_merged")
    moved = {k: v for k, v in entry["detail"].items() if k not in ("keep_before", "keep_after", "tags_added", "keep_had")}      # those: for «Разъединить» (CRM)
    assert moved == {"into": ia, "from": ib, "orders": [b], "chats": [], "phones": ["+79005556677"], "peers": [],
                     "calls": []}                                                                # the calls too (2026-10-23)



def test_the_cabinet_numbers_and_more_dummies_are_not_a_client(conn, monkeypatch):
    connections.create_account(conn, "Партнёр-2", NOW, "o")
    monkeypatch.setattr(connections, "get_avito_identity", lambda c, i: {"phone": "+79110000001", "phones": ["+79110000002"]})
    monkeypatch.setattr(connections, "_file_phone", lambda c, i: "+7 911 000-00-03")
    for phone in ("+79110000001", "+79110000002", "+79110000003", "8 900 000-00-00", "+7 999 777-77-77"):
        card = card_of_phone(conn, {"client_name": "x", "client_phone": phone}, NOW, "o")
        assert clients.client_of_order(conn, card) is None, phone
    assert clients.usable("+79001112233", set()) == "+79001112233"


def test_a_failing_client_link_leaves_no_orphan_and_keeps_the_order(conn, monkeypatch):
    import sqlite3
    real = clients._link_order

    def broken(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(clients, "_link_order", broken)
    card = card_of_phone(conn, {"client_name": "Иван", "client_phone": "+79001112233"}, NOW, "o")
    assert orders.get_card(conn, card)["client_name"] == "Иван" and conn.execute("SELECT COUNT(*) FROM client").fetchone()[0] == 0
    monkeypatch.setattr(clients, "_link_order", real)


def test_the_events_read_the_journal_by_the_action_index(conn):
    plan = conn.execute("EXPLAIN QUERY PLAN SELECT id FROM audit WHERE action IN (%s) AND json_extract(detail, '$.client') = ? ORDER BY id DESC LIMIT 5"
                        % ",".join("?" * len(clients._EVENT_ACTIONS)), clients._EVENT_ACTIONS + (1,)).fetchall()      # more actions with CRM (tags, unmerge)
    assert any("audit_action" in str(row[-1]) for row in plan), plan
    store.connect(conn.execute("PRAGMA database_list").fetchone()[2]).close()           # a second start: the index is made once, quietly


def test_a_client_made_by_the_command_shows_the_same_moscow_time_as_one_made_in_the_panel(conn, tmp_path, monkeypatch):
    from datetime import datetime
    from tires_sync import cli
    monkeypatch.setattr(cli, "moscow_now", lambda: datetime(2026, 10, 22, 15, 0, 0))                # the Moscow clock; UTC would be 12:00
    panel_order = card_of_phone(conn, {"client_name": "Пётр", "client_phone": "+79009998877"}, "2026-10-22T15:00:00", "owner")     # panel: stamped in Moscow
    old_order = card_of_phone(conn, {"client_name": "Иван", "client_phone": "+79001112233"}, NOW, "o")
    conn.execute("DELETE FROM client_phone WHERE client_id NOT IN (SELECT client_id FROM client_order WHERE order_id = ?)", (panel_order,))
    conn.execute("DELETE FROM client_order WHERE order_id = ?", (old_order,))
    conn.execute("DELETE FROM client WHERE id NOT IN (SELECT client_id FROM client_order)")
    conn.commit()
    assert main(["clients-link", "--db", str(tmp_path / "t.db"), "--confirm"]) == 0
    from_panel = clients.client_of_order(conn, panel_order)["id"]
    from_command = clients.client_of_order(conn, old_order)["id"]
    assert from_panel != from_command
    assert clients.get_client(conn, from_command)["created_at"] == "2026-10-22T15:00:00"                # the command: the Moscow clock, as the panel
    assert clients.get_client(conn, from_panel)["created_at"] == "2026-10-22T15:00:00"
    events = clients.client_events(conn, clients.get_client(conn, from_command))
    assert [e["at"] for e in events if e.get("made")] == ["2026-10-22T15:00:00"]
