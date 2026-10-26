"""CRM, the client's card (the owner 2026-10-23, п. 12, 13, 25): the car, the tags («Прагматик» by hand, «передан точкой» by itself), the
inquiries (the first and the repeated ones), the purchases, the proposals to join two clients (never silently), the join and its undoing,
the list with a search and a tag filter, the rights."""
from __future__ import annotations

import pytest

from panel_helpers import login, make_app, post
from tires_sync import clients, order_manager, orders, store, users

NOW = "2026-10-23T12:00:00"


@pytest.fixture
def conn(tmp_path):
    c = store.connect(str(tmp_path / "t.db"))
    orders.ensure_pipeline(c, NOW)
    return c


def _order(conn, phone, name="Иван", at=NOW):
    card = orders.create_card(conn, {"client_name": name, "client_phone": phone}, at, "o")
    conn.execute("UPDATE order_card SET created_at = ? WHERE id = ?", (at, card))
    conn.commit()
    return card


def _chat(conn, peer, name="Татьяна", account=1, order_id=None, created_ts=0):
    n = conn.execute("SELECT COUNT(*) FROM avito_chat").fetchone()[0]
    return conn.execute("INSERT INTO avito_chat (account_id, chat_id, peer_id, peer_name, order_id, created_ts) VALUES (?,?,?,?,?,?)",
                        (account, "c-%s-%d" % (peer, n), peer, name, order_id, created_ts)).lastrowid


def _message(conn, chat_row, at, direction="in", text="есть 205/55 R16?"):
    n = conn.execute("SELECT COUNT(*) FROM avito_message").fetchone()[0]
    conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, text) VALUES (?,?,?,?,?,?)",
                 (chat_row, "m%d" % n, at, n, direction, text))


def _call(conn, phone, at="2026-10-23T10:00:00", answered=1, account=1):
    n = conn.execute("SELECT COUNT(*) FROM avito_call").fetchone()[0]
    row = conn.execute("INSERT INTO avito_call (account_id, call_id, call_at, buyer_phone, answered, talk_seconds) VALUES (?,?,?,?,?,?)",
                       (account, 1000 + n, at, phone, answered, 65 if answered else 0)).lastrowid
    clients.attach_call(conn, row, NOW, "calls", own=set())
    conn.commit()
    return row


def _client(conn, order_id):
    return clients.client_of_order(conn, order_id)["id"]


# ---- the car, the tags -------------------------------------------------------------------------------------------------------------

def test_the_car_is_typed_by_hand_and_a_wrong_year_is_refused(conn):
    cid = _client(conn, _order(conn, "+79001112233"))
    clients.save(conn, cid, "Иван", "любит Nokian", NOW, "owner", car={"brand": " Kia ", "model": "Rio", "year": "2017"})
    conn.commit()
    c = clients.get_client(conn, cid)
    assert (c["car_brand"], c["car_model"], c["car_year"]) == ("Kia", "Rio", 2017)
    assert clients.car_text(c) == "Kia Rio, 2017"
    for year in ("1800", "абв", "3000"):
        with pytest.raises(clients.ClientError):
            clients.save(conn, cid, "Иван", "", NOW, "owner", car={"brand": "Kia", "model": "Rio", "year": year})
    clients.save(conn, cid, "Иван", "", NOW, "owner", car={"brand": "", "model": "", "year": ""})        # emptied
    assert clients.get_client(conn, cid)["car_year"] is None
    clients.save(conn, cid, "Иван П.", "", NOW, "owner")                                                 # no car given: the car is kept
    assert clients.get_client(conn, cid)["name"] == "Иван П."


def test_the_pragmatic_tag_and_free_tags_are_set_by_hand_with_the_journal(conn):
    cid = _client(conn, _order(conn, "+79001112233"))
    clients.set_tag(conn, cid, clients.PRAGMATIC, True, NOW, "owner")
    clients.set_tag(conn, cid, "  опт  ", True, NOW, "owner")
    clients.set_tag(conn, cid, "опт", True, NOW, "owner")                                                # twice: one tag
    conn.commit()
    assert clients.tags_of(conn, cid) == [clients.PRAGMATIC, "опт"]
    clients.set_tag(conn, cid, "опт", False, NOW, "owner")
    assert clients.tags_of(conn, cid) == [clients.PRAGMATIC]
    for bad in ("", "x" * (clients.TAG_MAX + 1), clients.HANDED):                                        # «передан точкой» is never set by hand
        with pytest.raises(clients.ClientError):
            clients.set_tag(conn, cid, bad, True, NOW, "owner")
    for n in range(clients.TAGS_MAX - 1):
        clients.set_tag(conn, cid, "тег%d" % n, True, NOW, "owner")
    with pytest.raises(clients.ClientError):
        clients.set_tag(conn, cid, "лишний", True, NOW, "owner")
    assert [x["action"] for x in store.list_audit(conn)].count("client_tagged") >= 3
    assert clients.HANDED == order_manager.HANDOFF_TAG


@pytest.mark.parametrize("how", ["order", "call", "chat"])
def test_handed_by_a_point_shows_by_itself_and_survives_a_join(conn, how):
    cid = _client(conn, _order(conn, "+79001112233"))
    other_order = _order(conn, "+79005556677")
    other = _client(conn, other_order)
    assert not clients.handed(conn, cid) and not clients.handed(conn, other)
    if how == "order":
        conn.execute("INSERT INTO order_handoff_source (order_id, kind) VALUES (?, 'vk')", (other_order,))
    elif how == "call":
        call = _call(conn, "+79005556677")
        conn.execute("INSERT INTO order_handoff_source (order_id, kind, call_id) VALUES (?, 'call', ?)", (other_order + 100, str(call)))
    else:
        chat = _chat(conn, "p-1")
        conn.execute("INSERT INTO client_chat (chat_row, client_id, how, at) VALUES (?, ?, 'chat', ?)", (chat, other, NOW))
        conn.execute("INSERT INTO order_handoff_source (order_id, kind, chat_row) VALUES (?, 'chat', ?)", (other_order + 100, chat))
    conn.commit()
    assert clients.handed(conn, other) and not clients.handed(conn, cid)
    assert [r["id"] for r in clients.list_clients(conn, tag=clients.HANDED)] == [other]
    clients.merge(conn, cid, other, NOW, "owner")
    conn.commit()
    assert clients.handed(conn, cid)


# ---- the inquiries, the purchases --------------------------------------------------------------------------------------------------

def test_inquiries_are_the_first_touch_and_every_touch_after_a_day_of_silence(conn):
    order = _order(conn, "+79001112233", at="2026-10-20T11:00:00")
    cid = _client(conn, order)
    chat = _chat(conn, "p-1")
    conn.execute("INSERT INTO client_chat (chat_row, client_id, how, at) VALUES (?, ?, 'chat', ?)", (chat, cid, NOW))
    _message(conn, chat, "2026-10-20T10:00:00")                       # the first inquiry: a message
    _message(conn, chat, "2026-10-20T10:05:00", "out")
    _message(conn, chat, "2026-10-20T12:00:00")                       # the same conversation (the order at 11:00 too)
    _message(conn, chat, "2026-10-21T13:00:00", "out")                # our message keeps the conversation going
    _message(conn, chat, "2026-10-22T12:59:00")                       # under a day after our message: the same inquiry
    _call(conn, "+79001112233", at="2026-10-23T14:00:00", answered=0)  # a day of silence: a new inquiry by a call
    _call(conn, "+79001112233", at="2026-10-23T14:02:00")
    found = clients.inquiries(conn, cid)
    assert [(i["at"], i["via"]) for i in found] == [("2026-10-23T14:00:00", "call"), ("2026-10-20T10:00:00", "chat")]       # newest first
    assert clients.NEW_INQUIRY_GAP_HOURS == 24
    entity = clients.card_view(conn, clients.get_client(conn, cid), {}, None, True)
    values = {f["label"]: f["value"] for f in entity["fields"]}
    assert values["Первое обращение"] == "2026-10-20T10:00:00" and values["Последнее обращение"] == "2026-10-23T14:00:00"
    assert values["Обращений"] == 2


def test_a_chat_without_messages_counts_from_its_start_and_an_order_alone_is_an_inquiry(conn):
    cid = _client(conn, _order(conn, "+79001112233", at="2026-10-01T09:00:00"))
    chat = _chat(conn, "p-1", created_ts=1760000000)                  # 2025-10-09 12:53 MSK
    conn.execute("INSERT INTO client_chat (chat_row, client_id, how, at) VALUES (?, ?, 'chat', ?)", (chat, cid, NOW))
    found = clients.inquiries(conn, cid)
    assert [i["via"] for i in found] == ["order", "chat"]
    assert found[1]["at"].startswith("2025-10-09T")


def test_the_purchases_show_what_the_size_the_sum_the_point_and_the_outcome(conn):
    pid = conn.execute("INSERT INTO point (name) VALUES ('Бета')").lastrowid
    order = _order(conn, "+79001112233")
    conn.execute("UPDATE order_card SET point_id = ? WHERE id = ?", (pid, order))
    conn.execute("INSERT INTO order_item (order_id, goods_id, name, qty, supplier_price, sale_unit) VALUES (?, 'g1', 'Nokian Hakkapeliitta 10 205/55 R16 94T', 4, 5000, 6250)", (order,))
    conn.execute("INSERT INTO order_item (order_id, goods_id, name, qty, supplier_price, sale_unit) VALUES (?, 'g2', 'Шиномонтаж', 1, 0, 1000)", (order,))
    done = conn.execute("SELECT id FROM order_stage WHERE kind = 'won' LIMIT 1").fetchone()[0]
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (done, order))
    conn.commit()
    bought = clients.purchases_view(conn, clients.get_client(conn, _client(conn, order)))
    assert len(bought) == 1
    p = bought[0]
    assert p["id"] == order and p["point"] == "Бета" and p["won"] and p["total"] == 26000
    assert [(i["name"], i["size"], i["qty"], i["unit"], i["sum"]) for i in p["items"]] == [
        ("Nokian Hakkapeliitta 10 205/55 R16 94T", "205/55 R16", 4, 6250, 25000), ("Шиномонтаж", "", 1, 1000, 1000)]
    assert "supplier_price" not in p["items"][0]                       # the purchase price is never on the card


# ---- the proposals to join, the join and its undoing ------------------------------------------------------------------------------

def test_a_phone_of_another_client_proposes_a_join_and_not_the_same_hides_it(conn):
    a = _client(conn, _order(conn, "+79001112233"))
    order_b = _order(conn, "+79005556677", name="Ваня")
    b = _client(conn, order_b)
    assert clients.duplicates(conn) == []
    conn.execute("UPDATE order_card SET client_phone = '+79001112233' WHERE id = ?", (order_b,))   # the order of b has a's phone
    conn.commit()
    found = clients.duplicates(conn, a)
    assert [(d["other"], d["why"]) for d in found] == [(b, ["телефон в заказе №%d" % order_b])]
    assert [(d["a"], d["b"]) for d in clients.duplicates(conn)] == [(min(a, b), max(a, b))]
    clients.skip_pair(conn, b, a, NOW, "owner")
    conn.commit()
    assert clients.duplicates(conn, a) == [] and clients.duplicates(conn) == []


def test_a_chat_of_anothers_order_and_a_phone_in_a_chat_propose_a_join_one_peer_id_on_two_accounts_does_not(conn):
    """Хвост З11: the same peer id on two accounts is no sign any more (Avito gives a person another id in every account: 0 of 2606)."""
    a = _client(conn, _order(conn, "+79001112233"))
    order_b = _order(conn, "+79005556677", name="Ваня")
    b = _client(conn, order_b)
    c = _client(conn, _order(conn, "+79004443322", name="Петя"))
    d = _client(conn, _order(conn, "+79007778899", name="Коля"))
    conn.execute("INSERT INTO client_peer (account_id, peer_id, client_id) VALUES (1, 'p-7', ?)", (a,))
    conn.execute("INSERT INTO client_peer (account_id, peer_id, client_id) VALUES (2, 'p-7', ?)", (b,))
    chat = _chat(conn, "p-8", order_id=order_b)                       # c's chat is bound to b's order
    conn.execute("INSERT INTO client_chat (chat_row, client_id, how, at) VALUES (?, ?, 'manual', ?)", (chat, c, NOW))
    chat_d = _chat(conn, "p-9")
    conn.execute("INSERT INTO client_chat (chat_row, client_id, how, at) VALUES (?, ?, 'chat', ?)", (chat_d, d, NOW))
    _message(conn, chat_d, NOW, text="мой номер 8 900 111-22-33, перезвоните")
    conn.commit()
    why = {(x["a"], x["b"]): x["why"] for x in clients.duplicates(conn)}
    assert (min(a, b), max(a, b)) not in why
    assert why[(min(b, c), max(b, c))] == ["чат Ч-%d привязан к заказу №%d" % (chat, order_b)]
    assert why[(min(a, d), max(a, d))] == ["телефон в чате Ч-%d" % chat_d]
    assert len(why) == 2


def test_own_numbers_and_merged_clients_are_never_proposed(conn):
    a = _client(conn, _order(conn, "+79001112233"))
    order_b = _order(conn, "+79005556677", name="Ваня")
    b = _client(conn, order_b)
    conn.execute("UPDATE order_card SET client_phone = '+79001112233' WHERE id = ?", (order_b,))
    clients.merge(conn, a, b, NOW, "owner")
    conn.commit()
    assert clients.duplicates(conn) == []


def test_a_join_takes_the_tags_and_the_car_and_the_undoing_gives_everything_back(conn):
    order_a = _order(conn, "+79001112233")
    a = _client(conn, order_a)
    order_b = _order(conn, "+79005556677", name="Ваня")
    b = _client(conn, order_b)
    chat_b = _chat(conn, "p-2", name="Ваня")
    conn.execute("INSERT INTO client_chat (chat_row, client_id, how, at) VALUES (?, ?, 'chat', ?)", (chat_b, b, NOW))
    conn.execute("INSERT INTO client_peer (account_id, peer_id, client_id) VALUES (1, 'p-2', ?)", (b,))
    call_b = _call(conn, "+79005556677")
    clients.save(conn, a, "Иван", "заметка а", NOW, "owner")
    clients.save(conn, b, "Ваня", "заметка б", NOW, "owner", car={"brand": "Kia", "model": "Rio", "year": "2017"})
    clients.set_tag(conn, b, clients.PRAGMATIC, True, NOW, "owner")
    clients.set_tag(conn, a, "опт", True, NOW, "owner")
    conn.commit()
    before_a, before_b = clients.get_client(conn, a), clients.get_client(conn, b)

    clients.merge(conn, a, b, NOW, "owner")
    conn.commit()
    joined = clients.get_client(conn, a)
    assert clients.tags_of(conn, a) == [clients.PRAGMATIC, "опт"] and clients.car_text(joined) == "Kia Rio, 2017"
    assert {o["id"] for o in joined["orders"]} == {order_a, order_b} and [k["id"] for k in joined["calls"]] == [call_b]
    assert [m["id"] for m in clients.merged_from(conn, a)] == [b]
    late_call = _call(conn, "+79005556677", at="2026-10-23T15:00:00")          # after the join: the number is a's now
    assert clients.client_of_call(conn, late_call) == a
    manual = _order(conn, "+79009990000", name="руками")
    conn.execute("UPDATE client_order SET client_id = ?, how = 'manual' WHERE order_id = ?", (a, manual))
    conn.commit()

    clients.unmerge(conn, a, b, NOW, "owner")
    conn.commit()
    back_a, back_b = clients.get_client(conn, a), clients.get_client(conn, b)
    assert back_b["merged_into"] is None and clients.merged_from(conn, a) == []
    for key in ("phones", "peers", "name", "note", "car_brand", "car_model", "car_year"):
        assert back_a[key] == before_a[key], key
        assert back_b[key] == before_b[key], key
    assert [o["id"] for o in back_b["orders"]] == [order_b] and [h["id"] for h in back_b["chats"]] == [chat_b]
    assert sorted(k["id"] for k in back_b["calls"]) == sorted([call_b, late_call])       # the call linked by b's number after the join too
    assert {o["id"] for o in back_a["orders"]} == {order_a, manual}                      # a link made by a person stays
    assert clients.tags_of(conn, a) == ["опт"] and clients.tags_of(conn, b) == [clients.PRAGMATIC]
    texts = [e["text"] for e in clients.client_events(conn, back_a)]
    assert "Отсоединён клиент №%d" % b in texts
    clients.merge(conn, a, b, NOW, "owner")                                              # may be joined again
    conn.commit()
    assert clients.get_client(conn, b)["merged_into"] == a


def test_the_undoing_keeps_what_was_changed_after_the_join_and_refuses_a_chain(conn):
    a = _client(conn, _order(conn, "+79001112233"))
    b = _client(conn, _order(conn, "+79005556677", name="Ваня"))
    c = _client(conn, _order(conn, "+79004443322", name="Петя"))
    clients.save(conn, b, "Ваня", "заметка б", NOW, "owner")
    clients.merge(conn, a, b, NOW, "owner")
    clients.save(conn, a, "Иван Иванович", "новая заметка", NOW, "owner")                # edited after the join: kept
    clients.merge(conn, c, a, NOW, "owner")
    conn.commit()
    with pytest.raises(clients.ClientError):
        clients.unmerge(conn, a, b, NOW, "owner")                                        # a itself is joined into c: undo that first
    with pytest.raises(clients.ClientError):
        clients.unmerge(conn, c, b, NOW, "owner")                                        # b was not joined into c
    clients.unmerge(conn, c, a, NOW, "owner")
    clients.unmerge(conn, a, b, NOW, "owner")
    conn.commit()
    back = clients.get_client(conn, a)
    assert (back["name"], back["note"]) == ("Иван Иванович", "новая заметка")


def test_a_join_made_before_this_code_is_undone_by_its_journal(conn):
    a = _client(conn, _order(conn, "+79001112233"))
    order_b = _order(conn, "+79005556677", name="Ваня")
    b = _client(conn, order_b)
    conn.execute("UPDATE client_order SET client_id = ? WHERE client_id = ?", (a, b))
    conn.execute("UPDATE client_phone SET client_id = ? WHERE client_id = ?", (a, b))
    conn.execute("UPDATE client SET merged_into = ? WHERE id = ?", (a, b))
    store.write_audit(conn, NOW, "owner", "client_merged", {"into": a, "from": b, "orders": [order_b], "chats": [], "phones": ["+79005556677"],
                                                          "peers": [], "calls": []})
    clients.unmerge(conn, a, b, NOW, "owner")
    conn.commit()
    assert [o["id"] for o in clients.get_client(conn, b)["orders"]] == [order_b]
    assert clients.get_client(conn, b)["phones"] == ["+79005556677"]


# ---- the list, the pages, the rights -----------------------------------------------------------------------------------------------

def test_the_list_finds_by_the_car_and_filters_by_a_tag(conn):
    a = _client(conn, _order(conn, "+79001112233"))
    b = _client(conn, _order(conn, "+79005556677", name="Ваня"))
    clients.save(conn, a, "Иван", "", NOW, "owner", car={"brand": "Kia", "model": "Rio", "year": "2017"})
    clients.set_tag(conn, b, clients.PRAGMATIC, True, NOW, "owner")
    conn.commit()
    assert [r["id"] for r in clients.list_clients(conn, "rio", fold=True)] == [a]
    rows = clients.list_clients(conn, tag=clients.PRAGMATIC)
    assert [r["id"] for r in rows] == [b] and rows[0]["tags"] == [clients.PRAGMATIC]
    assert [r["id"] for r in clients.list_clients(conn, tag="нет такого")] == []
    assert clients.PRAGMATIC in clients.all_tags(conn) and clients.HANDED in clients.all_tags(conn)


def _as(app, conn, login_, role, point_id=None):
    users.add_user(conn, login_, "длинный-пароль-1", NOW, role=role, point_id=point_id)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user"], s["epoch"], s["csrf"] = login_, store.get_session_epoch(conn, login_), "tok"
    return client


def test_the_card_page_the_list_the_duplicates_and_the_rights(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    a = _client(conn, _order(conn, "+79001112233"))
    order_b = _order(conn, "+79005556677", name="Ваня")
    b = _client(conn, order_b)
    conn.execute("UPDATE order_card SET client_phone = '+79001112233' WHERE id = ?", (order_b,))
    conn.commit()
    owner = app.test_client()
    tok = login(owner)
    post(owner, "/clients/%d/tag" % a, tok, tag=clients.PRAGMATIC, on="1")
    post(owner, "/clients/%d/save" % a, tok, name="Иван", note="", car_brand="Kia", car_model="Rio", car_year="2017")
    page = owner.get("/clients/%d" % a).get_data(as_text=True)
    assert 'class="badge badge--err crm-pragmatic"' in page and "Kia Rio, 2017" in page
    assert "Возможно, тот же клиент" in page and "/clients/%d/merge" % a in page and "/clients/%d/skip/%d" % (a, b) in page
    assert "Ваня" in owner.get("/clients/duplicates").get_data(as_text=True)
    lst = owner.get("/clients?tag=%s" % clients.PRAGMATIC).get_data(as_text=True)
    assert "/clients/%d" % a in lst and "/clients/%d\"" % b not in lst
    assert "CRM" in owner.get("/clients").get_data(as_text=True)
    post(owner, "/clients/%d/merge" % a, tok, other=str(b))
    assert clients.get_client(conn, b)["merged_into"] == a
    assert "/clients/%d/unmerge/%d" % (a, b) in owner.get("/clients/%d" % a).get_data(as_text=True)
    post(owner, "/clients/%d/unmerge/%d" % (a, b), tok)
    assert clients.get_client(conn, b)["merged_into"] is None
    post(owner, "/clients/%d/skip/%d" % (a, b), tok)
    assert clients.duplicates(conn, a) == []

    viewer = _as(app, conn, "viewer1", "viewer")
    assert viewer.get("/clients/%d" % a).status_code == 200 and viewer.get("/clients/duplicates").status_code == 200
    for url in ("/clients/%d/tag" % a, "/clients/%d/unmerge/%d" % (a, b), "/clients/%d/skip/%d" % (a, b)):
        assert viewer.post(url, data={"csrf": "tok", "tag": "x", "on": "1"}).status_code == 403
    operator = _as(app, conn, "olya", "chats")
    assert operator.post("/clients/%d/tag" % a, data={"csrf": "tok", "tag": "опт", "on": "1"}).status_code == 302
    assert "опт" in clients.tags_of(conn, a)
    pid = conn.execute("INSERT INTO point (name) VALUES ('Бета')").lastrowid
    conn.commit()
    point = _as(app, conn, "pointman", "point", point_id=pid)
    for url in ("/clients/%d" % a, "/clients/duplicates"):                                    # not the point's client; «CRM» of its own part:
        assert point.get(url).status_code == 403                                               # tests/test_point_isolation.py (2026-10-26)
    assert point.post("/clients/%d/tag" % a, data={"csrf": "tok", "tag": "x", "on": "1"}).status_code == 403


def test_the_menu_has_crm_and_the_lane_of_the_inquiries_has_its_place(tmp_path, monkeypatch):
    """«CRM» in the menu for those who see the clients, a point too (its own part, 2026-10-26); _client_inquiry.html (the lane crm-inquiry) is drawn in the
    head of the card and in every row of the list with the client's number."""
    from jinja2 import ChoiceLoader, DictLoader
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    a = _client(conn, _order(conn, "+79001112233"))
    owner = app.test_client()
    login(owner)
    assert 'href="/clients">' in owner.get("/orders").get_data(as_text=True)
    pid = conn.execute("INSERT INTO point (name) VALUES ('Бета')").lastrowid
    conn.commit()
    point = _as(app, conn, "pointman", "point", point_id=pid)
    assert 'href="/clients"' in point.get("/orders").get_data(as_text=True)                   # the menu of the panel («Моя точка» has its own)
    app.jinja_env.loader = ChoiceLoader([DictLoader({"_client_inquiry.html": "[статус {{ inquiry_client_id }} {{ inquiry_place }}]"}), app.jinja_env.loader])
    assert "[статус %d card]" % a in owner.get("/clients/%d" % a).get_data(as_text=True)
    assert "[статус %d list]" % a in owner.get("/clients").get_data(as_text=True)


# ---- review round 1 ----------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("keep_is_k", [True, False])
def test_the_undoing_leaves_with_the_kept_client_its_own_order_that_has_the_others_phone(conn, keep_is_k):
    """K's order joined K by its Avito person but has G's phone (that is why the join was proposed): undoing the join keeps it with K."""
    g = _client(conn, _order(conn, "+79005556677", name="Ваня"))
    k = _client(conn, _order(conn, "+79001112233"))
    chat = _chat(conn, "p-5", name="Иван")
    conn.execute("INSERT INTO client_chat (chat_row, client_id, how, at) VALUES (?, ?, 'chat', ?)", (chat, k, NOW))
    conn.execute("INSERT INTO client_peer (account_id, peer_id, client_id) VALUES (1, 'p-5', ?)", (k,))
    x = orders.create_card(conn, {"client_name": "Иван", "client_phone": "+79005556677", "avito_chat": "Ч-%d" % chat}, NOW, "o")
    assert clients.client_of_order(conn, x)["id"] == k and conn.execute("SELECT how FROM client_order WHERE order_id = ?", (x,)).fetchone()[0] == "phone"
    keep, gone = (k, g) if keep_is_k else (g, k)
    clients.merge(conn, keep, gone, NOW, "owner")
    clients.unmerge(conn, keep, gone, NOW, "owner")
    conn.commit()
    assert clients.client_of_order(conn, x)["id"] == k and clients.client_of_chat(conn, chat)["id"] == k


def test_a_deal_made_from_a_chat_after_the_join_goes_back_with_its_chat(conn):
    from tires_sync import chat_deals
    k = _client(conn, _order(conn, "+79001112233"))
    first = _chat(conn, "p-6", name="Ваня")
    chat_deals.create_deal(conn, first, NOW)
    conn.commit()
    g = clients.client_of_chat(conn, first)["id"]
    clients.merge(conn, k, g, NOW, "owner")
    conn.commit()
    later = _chat(conn, "p-6", name="Ваня")                                   # the same person writes again after the join
    order = orders.create_card(conn, {"client_name": "Ваня"}, NOW, "o")       # an order without a phone, bound to the chat (a deal of it)
    chat_deals._attach(conn, {"id": later, "item_title": "", "peer_name": "Ваня", "account_id": 1}, order, NOW, "o")   # «привязать к заказу»
    conn.commit()
    assert conn.execute("SELECT how FROM client_order WHERE order_id = ?", (order,)).fetchone()[0] == "chat"
    assert clients.client_of_chat(conn, later)["id"] == k and clients.client_of_order(conn, order)["id"] == k
    clients.unmerge(conn, k, g, NOW, "owner")
    conn.commit()
    assert clients.client_of_chat(conn, later)["id"] == g and clients.client_of_order(conn, order)["id"] == g


def test_a_split_and_an_undone_join_are_not_proposed_again(conn):
    a = _client(conn, _order(conn, "+79001112233"))
    second = _order(conn, "+79001112233", name="Ваня")
    assert _client(conn, second) == a
    new = clients.split_order(conn, second, NOW, "owner")                     # a person said: another one, though the phone is the same
    conn.commit()
    assert clients.duplicates(conn, a) == [] and clients.duplicates(conn) == []
    b = _client(conn, _order(conn, "+79005556677"))
    conn.execute("UPDATE order_card SET client_phone = '+79001112233' WHERE id IN (SELECT order_id FROM client_order WHERE client_id = ?)", (b,))
    conn.commit()
    assert [d["other"] for d in clients.duplicates(conn, b)] == [a]
    clients.merge(conn, a, b, NOW, "owner")
    clients.unmerge(conn, a, b, NOW, "owner")
    conn.commit()
    assert clients.duplicates(conn) == [] and new


def test_a_system_message_is_no_inquiry(conn):
    cid = _client(conn, _order(conn, "+79001112233", at="2026-10-01T09:00:00"))
    chat = _chat(conn, "p-1")
    conn.execute("INSERT INTO client_chat (chat_row, client_id, how, at) VALUES (?, ?, 'chat', ?)", (chat, cid, NOW))
    conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, kind, text) VALUES (?, 's1', '2026-10-05T10:00:00', 1, 'in', 'system', 'Объявление снято')", (chat,))
    _message(conn, chat, "2026-10-06T10:00:00", text="")
    conn.execute("UPDATE avito_message SET kind = 'image' WHERE chat_row = ? AND message_id != 's1'", (chat,))
    assert [i["via"] for i in clients.inquiries(conn, cid)] == ["chat", "order"]          # a photo of the client is, the system's is not


def test_the_tag_filter_shows_the_tags_of_living_clients_and_a_tag_in_another_case_is_the_same(conn):
    a = _client(conn, _order(conn, "+79001112233"))
    b = _client(conn, _order(conn, "+79005556677", name="Ваня"))
    clients.set_tag(conn, b, "старый", True, NOW, "owner")
    clients.set_tag(conn, a, "опт", True, NOW, "owner")
    clients.set_tag(conn, a, "Опт", True, NOW, "owner")
    clients.merge(conn, a, b, NOW, "owner")
    clients.set_tag(conn, a, "старый", False, NOW, "owner")
    conn.commit()
    assert clients.tags_of(conn, a) == ["опт"] and "старый" not in clients.all_tags(conn)


def test_the_list_finds_by_the_year_of_the_car(conn):
    a = _client(conn, _order(conn, "+79001112233"))
    clients.save(conn, a, "Иван", "", NOW, "owner", car={"brand": "Kia", "model": "Rio", "year": "2017"})
    conn.commit()
    assert [r["id"] for r in clients.list_clients(conn, "2017")] == [a]


# ---- review round 2 ----------------------------------------------------------------------------------------------------------------

def test_joins_are_undone_from_the_latest(conn):
    a = _client(conn, _order(conn, "+79001112233"))
    b = _client(conn, _order(conn, "+79005556677", name="Ваня"))
    c = _client(conn, _order(conn, "+79004443322", name="Петя"))
    clients.merge(conn, a, b, NOW, "owner")
    clients.merge(conn, a, c, NOW, "owner")
    with pytest.raises(clients.ClientError):
        clients.unmerge(conn, a, b, NOW, "owner")                     # c was joined later: first c
    clients.unmerge(conn, a, c, NOW, "owner")
    clients.unmerge(conn, a, b, NOW, "owner")
    conn.commit()
    assert clients.get_client(conn, b)["merged_into"] is None and clients.get_client(conn, c)["merged_into"] is None


def test_an_order_of_the_kept_clients_own_chat_with_the_others_phone_stays(conn):
    k = _client(conn, _order(conn, "+79001112233"))
    g = _client(conn, _order(conn, "+79005556677", name="Ваня"))
    own_chat = _chat(conn, "p-k", name="Иван")
    conn.execute("INSERT INTO client_chat (chat_row, client_id, how, at) VALUES (?, ?, 'chat', ?)", (own_chat, k, NOW))
    conn.execute("INSERT INTO client_peer (account_id, peer_id, client_id) VALUES (1, 'p-k', ?)", (k,))
    clients.merge(conn, k, g, NOW, "owner")
    conn.commit()
    x = orders.create_card(conn, {"client_name": "Иван", "client_phone": "+79005556677", "avito_chat": "Ч-%d" % own_chat}, NOW, "o")
    assert clients.client_of_order(conn, x)["id"] == k
    clients.unmerge(conn, k, g, NOW, "owner")
    conn.commit()
    assert clients.client_of_order(conn, x)["id"] == k                 # the Avito person decides, as when the order was linked


def test_a_tag_takes_the_spelling_already_in_use_and_a_join_does_not_double_it(conn):
    a = _client(conn, _order(conn, "+79001112233"))
    b = _client(conn, _order(conn, "+79005556677", name="Ваня"))
    clients.set_tag(conn, a, "опт", True, NOW, "owner")
    clients.set_tag(conn, b, "ОПТ", True, NOW, "owner")
    assert clients.tags_of(conn, b) == ["опт"]
    conn.execute("UPDATE client_tag SET tag = 'Опт' WHERE client_id = ?", (b,))     # an old spelling
    clients.merge(conn, a, b, NOW, "owner")
    conn.commit()
    assert clients.tags_of(conn, a) == ["опт"]


def test_not_the_same_holds_after_one_of_them_is_joined_into_another(conn):
    a = _client(conn, _order(conn, "+79001112233"))
    second = _order(conn, "+79001112233", name="Ваня")
    new = clients.split_order(conn, second, NOW, "owner")
    x = _client(conn, _order(conn, "+79007778899", name="Коля"))
    clients.merge(conn, x, a, NOW, "owner")
    conn.commit()
    assert clients.duplicates(conn, new) == [] and clients.duplicates(conn) == []


def test_a_chat_with_only_avitos_own_messages_counts_from_its_start(conn):
    cid = _client(conn, _order(conn, "+79001112233", at="2026-10-01T09:00:00"))
    chat = _chat(conn, "p-1", created_ts=1760000000)
    conn.execute("INSERT INTO client_chat (chat_row, client_id, how, at) VALUES (?, ?, 'chat', ?)", (chat, cid, NOW))
    conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, kind, text) VALUES (?, 's1', '2026-10-05T10:00:00', 1, 'in', 'system', 'интересуется')", (chat,))
    assert [i["via"] for i in clients.inquiries(conn, cid)] == ["order", "chat"]
