"""«На ОТХ» (the owner 2026-10-23): the client of a refused order is unlinked, the goods are our stock (the page «На ОТХ», a mark in the
catalog, «Вернуть поставщику» as a mark of ours); in the feed only «Шины в наличии» (tests/test_oth_feed.py)."""
from __future__ import annotations

import re

from helpers import catalog_xml, commodity_xml, make_settings
from panel_helpers import login, make_app, post
from tests.test_order_reserve import card_at, db, stage
from tires_sync import clients, inquiry, order_oth, orders, store, users
from tires_sync.catalog import parse_reference
from tires_sync.sync import run_cycle

T = "2026-10-17T10:00:00"


def linked(conn, cid):
    row = conn.execute("SELECT client_id FROM client_order WHERE order_id = ?", (cid,)).fetchone()
    return row[0] if row else None


def to_oth(conn, cid, when=T):
    orders.move_card(conn, cid, stage(conn, "oth"), when, "o")


def test_entering_oth_unlinks_the_client_and_keeps_the_link_aside(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    client_id = linked(conn, cid)
    assert client_id and clients.get_client(conn, client_id)["orders"]
    to_oth(conn, cid)
    assert linked(conn, cid) is None and orders.get_card(conn, cid)["client"] is None
    assert conn.execute("SELECT client_id, detached_by FROM order_oth_client WHERE order_id = ?", (cid,)).fetchone() == (client_id, "o")
    client = clients.get_client(conn, client_id)
    assert client["orders"] == [] and clients.purchases_view(conn, client) == []
    assert any("Клиент отвязан от заказа" in e["text"] for e in orders.list_events(conn, cid))
    card = orders.get_card(conn, cid)
    assert card["client_name"] == "Иван" and card["client_phone"]                 # the order keeps who refused: a record, not a link
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'oth_detach'").fetchone()[0] == 1


def test_saving_the_order_or_joining_the_old_data_never_links_it_again(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "oth", T)
    orders.update_card(conn, cid, {"client_phone": "+7 900 111 22 44", "comment": "ждёт"}, "2026-10-18T10:00:00", "o")
    assert clients.attach_order(conn, cid, "2026-10-18T10:00:00", "o") is None
    plan = clients.plan_links(conn)
    assert all(cid not in e["orders"] for e in plan["new_clients"] + plan["to_existing"] + plan["conflicts"])
    clients.apply_links(conn, plan, "2026-10-18T11:00:00")
    assert linked(conn, cid) is None


def test_back_to_an_open_stage_links_the_same_client_again(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "oth", T)
    client_id = conn.execute("SELECT client_id FROM order_oth_client WHERE order_id = ?", (cid,)).fetchone()[0]
    orders.move_card(conn, cid, stage(conn, "at_point"), "2026-10-18T10:00:00", "o")
    assert linked(conn, cid) == client_id
    assert conn.execute("SELECT COUNT(*) FROM order_oth_client").fetchone()[0] == 0
    assert any("Клиент снова привязан" in e["text"] for e in orders.list_events(conn, cid))
    to_oth(conn, cid, "2026-10-19T10:00:00")                                        # refused once more: unlinked once more
    assert linked(conn, cid) is None


def test_back_from_oth_goes_to_the_client_it_was_merged_into(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "oth", T)
    gone = conn.execute("SELECT client_id FROM order_oth_client WHERE order_id = ?", (cid,)).fetchone()[0]
    keep = clients._new_client(conn, "Иван Петров", T, "o")
    conn.commit()
    clients.merge(conn, keep, gone, "2026-10-18T09:00:00", "o")
    orders.move_card(conn, cid, stage(conn, "moving"), "2026-10-18T10:00:00", "o")
    assert linked(conn, cid) == keep


def test_a_closed_stage_after_oth_keeps_the_order_unlinked_and_the_client_not_ordered(tmp_path):
    conn = db(tmp_path)
    done = card_at(conn, "oth", T)
    client_id = conn.execute("SELECT client_id FROM order_oth_client WHERE order_id = ?", (done,)).fetchone()[0]
    orders.move_card(conn, done, stage(conn, "done"), "2026-10-18T10:00:00", "o")              # sold to somebody else: not the client's
    cancelled = card_at(conn, "oth", T)
    orders.move_card(conn, cancelled, stage(conn, "cancel"), "2026-10-18T10:00:00", "o", changes={"cancel_reason": "вернули"})
    assert linked(conn, done) is None and linked(conn, cancelled) is None
    assert conn.execute("SELECT COUNT(*) FROM order_oth_client").fetchone()[0] == 2
    assert clients.get_client(conn, client_id)["orders"] == []


def _status_after_done(tmp_path, before):
    """The client writes, then the order goes from `before` to «Выполнено»: the status of the inquiry."""
    conn = db(tmp_path)
    conn.execute("INSERT INTO account (id, name, feed_token, created, chats_enabled) VALUES (1, 'Альфа', 't1', '2026-10-01', 1)")
    cid = card_at(conn, before, T)
    client_id = conn.execute("SELECT client_id FROM client_order WHERE order_id = ? UNION SELECT client_id FROM order_oth_client WHERE order_id = ?",
                             (cid, cid)).fetchone()[0]
    chat = conn.execute("INSERT INTO avito_chat (account_id, chat_id, peer_id, peer_name) VALUES (1, 'c1', 'p1', 'Иван')").lastrowid
    clients._link_chat(conn, client_id, chat, "chat", T)
    conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, kind, text) VALUES (?, 'm1', '2026-10-18T09:00:00', 0, 'in', 'text', 'Ну что?')",
                 (chat,))
    conn.commit()
    inquiry.tick(conn, "2026-10-18T09:05:00")
    orders.move_card(conn, cid, stage(conn, "done"), "2026-10-18T10:00:00", "o")
    inquiry.tick(conn, "2026-10-18T10:05:00")
    return inquiry.get(conn, client_id)["status"]


def test_a_client_who_ordered_is_no_longer_ordered_when_the_order_goes_to_oth(tmp_path):
    conn = db(tmp_path)
    conn.execute("INSERT INTO account (id, name, feed_token, created, chats_enabled) VALUES (1, 'Альфа', 't1', '2026-10-01', 1)")
    cid = card_at(conn, "moving", T)
    client_id = linked(conn, cid)
    chat = conn.execute("INSERT INTO avito_chat (account_id, chat_id, peer_id, peer_name) VALUES (1, 'c1', 'p1', 'Иван')").lastrowid
    clients._link_chat(conn, client_id, chat, "chat", T)
    conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, kind, text) VALUES (?, 'm1', '2026-10-17T09:00:00', 0, 'in', 'text', 'Когда?')",
                 (chat,))
    conn.commit()
    inquiry.tick(conn, "2026-10-17T11:00:00")
    assert inquiry.get(conn, client_id)["status"] == "ordered"
    inquiry.tick(conn, "2026-10-17T11:01:00")                                     # looked at: «заказал» waits for a change of an order
    to_oth(conn, cid, "2026-10-18T10:00:00")
    inquiry.tick(conn, "2026-10-18T10:01:00")
    assert inquiry.get(conn, client_id)["status"] != "ordered"


def test_an_order_done_after_oth_does_not_make_the_client_ordered(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert _status_after_done(tmp_path / "a", "at_point") == "ordered"             # the control: the client's own order
    assert _status_after_done(tmp_path / "b", "oth") != "ordered"


def test_an_order_without_a_client_goes_to_oth_and_back_quietly(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    conn.execute("DELETE FROM client_order WHERE order_id = ?", (cid,))
    conn.commit()
    to_oth(conn, cid)
    assert conn.execute("SELECT COUNT(*) FROM order_oth_client").fetchone()[0] == 0
    orders.move_card(conn, cid, stage(conn, "at_point"), "2026-10-18T10:00:00", "o")
    assert not any("Клиент" in e["text"] for e in orders.list_events(conn, cid))


def test_the_stock_lists_what_where_from_which_order_since_when_and_the_return_mark(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "oth", T)
    card_at(conn, "at_point", T)                                                   # an order of a client: not ours
    rows = order_oth.stock(conn)
    assert [(r["order_id"], r["goods_id"], r["qty"], r["point"], r["since"][:10], r["until"]) for r in rows["stock"]] == [
        (cid, "1", 4, "Бета", "2026-10-17", "2026-10-24")]
    assert order_oth.goods_on_oth(conn) == {"1": 4}
    item = rows["stock"][0]["item_id"]
    order_oth.mark_returned(conn, item, "2026-10-20T10:00:00", "o")
    assert order_oth.stock(conn)["stock"] == [] and order_oth.goods_on_oth(conn) == {}
    assert order_oth.stock(conn)["returned"][0]["returned_by"] == "o"
    assert any("возвращено поставщику" in e["text"] for e in orders.list_events(conn, cid))
    assert orders.get_card(conn, cid)["stage"]["code"] == "oth"                   # a mark only: the order stays where it is
    try:
        order_oth.mark_returned(conn, item, "2026-10-20T10:01:00", "o")
        raise AssertionError("marked twice")
    except order_oth.OthError:
        pass
    order_oth.unmark_returned(conn, item, "2026-10-20T11:00:00", "o")
    assert order_oth.goods_on_oth(conn) == {"1": 4}


def test_an_order_that_left_oth_is_not_stock_and_a_new_stay_forgets_the_old_marks(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "oth", T)
    item = order_oth.stock(conn)["stock"][0]["item_id"]
    order_oth.mark_returned(conn, item, "2026-10-18T10:00:00", "o")
    orders.move_card(conn, cid, stage(conn, "at_point"), "2026-10-19T10:00:00", "o")
    assert order_oth.stock(conn) == {"stock": [], "returned": []}
    try:
        order_oth.mark_returned(conn, item, "2026-10-19T11:00:00", "o")
        raise AssertionError("marked an order not on ОТХ")
    except order_oth.OthError:
        pass
    to_oth(conn, cid, "2026-10-20T10:00:00")
    assert [r["item_id"] for r in order_oth.stock(conn)["stock"]] == [item]


ROWS = (commodity_xml(NNOMMODIF="1", SMODIFNAME="IKON 205/55/16 T 94 Test Model Ш.", SMODIFNAME_ORIG="IKON 205/55/16 T 94 Test Model Ш.", NREST="12",
                      NPRICE_DELAYED="5000", SMNFCODE="C1"),
        commodity_xml(NNOMMODIF="2", SMODIFNAME="IKON 215/60/17 T 94 Test Model", SMODIFNAME_ORIG="IKON 215/60/17 T 94 Test Model", SWIDTH="215",
                      SHEIGHT="60", SDIAMETR="17", NREST="12", NPRICE_DELAYED="7000", SMNFCODE="C2"))


def panel(tmp_path):
    app = make_app(tmp_path)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*ROWS))
    owner = app.test_client()
    tok = login(owner)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, T)
    return app, owner, tok, conn


def test_the_page_na_oth_the_return_button_and_the_menu(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    cid = card_at(conn, "oth", T)
    html = owner.get("/oth").get_data(as_text=True)
    assert 'href="/oth"' in html and ">На ОТХ<" in html
    assert "Nokian 205/55 R16" in html and "Бета" in html and 'href="/orders/%d"' % cid in html and "Вернуть поставщику" in html
    item = re.search(r'name="item" value="(\d+)"', html).group(1)
    r = post(owner, "/oth/return", tok, item=item)
    assert r.status_code == 302 and r.headers["Location"].endswith("/oth")
    html = owner.get("/oth").get_data(as_text=True)
    assert "Возвращено поставщику" in html and "На ОТХ ничего нет" in html and "Отменить" in html
    post(owner, "/oth/return", tok, item=item, undo="1")
    assert order_oth.goods_on_oth(conn) == {"1": 4}
    assert post(owner, "/oth/return", tok, item="999").status_code == 302
    assert "Этой позиции нет на ОТХ" in owner.get("/oth").get_data(as_text=True)


def test_a_manager_of_a_point_does_not_see_the_page(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    pid = card_at(conn, "oth", T) and conn.execute("SELECT id FROM point").fetchone()[0]
    users.add_user(conn, "point1", "длинный-пароль-1", T, role="point", point_id=pid)
    c = app.test_client()
    with c.session_transaction() as session:
        session.update(user="point1", epoch=store.get_session_epoch(conn, "point1"), csrf="tok")
    assert c.get("/oth").status_code in (302, 403)
    item = order_oth.stock(conn)["stock"][0]["item_id"]
    assert c.post("/oth/return", data={"csrf": "tok", "item": item}).status_code in (302, 403)
    assert order_oth.goods_on_oth(conn) == {"1": 4}


def test_the_catalog_marks_the_positions_on_oth(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    assert "на ОТХ ·" not in owner.get("/catalog").get_data(as_text=True)
    card_at(conn, "oth", T)
    html = owner.get("/catalog").get_data(as_text=True)
    row = next(r for r in html.split("<tr") if ">C1<" in r)
    assert "на ОТХ · 4 шт." in row and 'href="/oth"' in row
    assert "на ОТХ ·" not in next(r for r in html.split("<tr") if ">C2<" in r)


REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')


def test_goods_on_oth_change_only_the_first_line_of_their_ad_in_the_feed(tmp_path):
    # the owner 2026-10-29: the ad of a position on «На ОТХ» starts with «Шины в наличии» (tests/test_oth_feed.py); nothing else changes
    from datetime import date
    feeds = []
    for name, with_oth in (("plain", False), ("oth", True)):
        folder = tmp_path / name
        folder.mkdir()
        conn = db(folder)
        if with_oth:
            card_at(conn, "oth", T)
            assert order_oth.goods_on_oth(conn) == {"1": 4}
        run_cycle(catalog_xml(*ROWS), make_settings(), REF, conn, date(2026, 10, 17), out_dir=folder)
        feeds.append((folder / "avito_feed.xml").read_text(encoding="utf-8"))
    assert "<Id>1</Id>" in feeds[0] and feeds[0].count("<Description>") == 2
    assert feeds[1] == feeds[0].replace("<Description>🔥 IKON 205/55/16", "<Description>Шины в наличии\n🔥 IKON 205/55/16", 1) != feeds[0]


def test_a_closed_stage_set_by_mistake_then_an_open_one_links_the_client_again(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "oth", T)
    client_id = conn.execute("SELECT client_id FROM order_oth_client WHERE order_id = ?", (cid,)).fetchone()[0]
    orders.move_card(conn, cid, stage(conn, "cancel"), "2026-10-18T10:00:00", "o", changes={"cancel_reason": "ошиблись"})
    assert linked(conn, cid) is None
    orders.move_card(conn, cid, stage(conn, "at_point"), "2026-10-18T10:05:00", "o")          # no other way to give the client back
    assert linked(conn, cid) == client_id


def test_a_mark_made_a_moment_ago_in_another_tab_is_a_message_not_an_error(tmp_path, monkeypatch):
    conn = db(tmp_path)
    card_at(conn, "oth", T)
    item = order_oth.stock(conn)["stock"][0]
    order_oth.mark_returned(conn, item["item_id"], "2026-10-20T10:00:00", "o")
    monkeypatch.setattr(order_oth, "_stock_item", lambda conn, item_id: dict(item))          # the other tab read it before the mark
    try:
        order_oth.mark_returned(conn, item["item_id"], "2026-10-20T10:00:01", "o")
        raise AssertionError("marked twice")
    except order_oth.OthError:
        pass
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'oth_return'").fetchone()[0] == 1


def test_the_journal_of_actions_says_every_oth_record_in_words(tmp_path):
    import json
    from tires_sync.panel import texts
    conn = db(tmp_path)
    cid = card_at(conn, "oth", T)
    item = order_oth.stock(conn)["stock"][0]["item_id"]
    order_oth.mark_returned(conn, item, "2026-10-18T10:00:00", "o")
    order_oth.unmark_returned(conn, item, "2026-10-18T10:01:00", "o")
    orders.move_card(conn, cid, stage(conn, "at_point"), "2026-10-18T10:02:00", "o")
    entries = [{"action": a, "detail": json.loads(d)} for a, d in conn.execute("SELECT action, detail FROM audit WHERE action LIKE 'oth%' ORDER BY id")]
    assert [e["action"] for e in entries] == ["oth_detach", "oth_return", "oth_return_undo", "oth_reattach"]
    shown = [texts.audit_text(e) for e in entries]
    assert all("oth" not in s and "№%d" % cid in s for s in shown), shown


def _clients_with(conn, phone):
    return [r[0] for r in conn.execute("SELECT client_id FROM client_phone WHERE phone = ?", (phone,))]


def test_a_new_phone_with_the_move_to_oth_makes_no_client_and_adds_no_phone(tmp_path):
    """Хвост З15: the phone changed in the same save as the move to «На ОТХ»: no empty client, the client put aside keeps its phones."""
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    client_id = linked(conn, cid)
    phones = clients.get_client(conn, client_id)["phones"]
    count = conn.execute("SELECT COUNT(*) FROM client").fetchone()[0]
    orders.move_card(conn, cid, stage(conn, "oth"), T, "o", changes={"client_phone": "+7 900 555 66 77"})
    assert orders.get_card(conn, cid)["client_phone"] == "+7 900 555 66 77" and linked(conn, cid) is None
    assert conn.execute("SELECT COUNT(*) FROM client").fetchone()[0] == count and _clients_with(conn, "+79005556677") == []
    assert clients.get_client(conn, client_id)["phones"] == phones
    orders.update_card(conn, cid, {"client_phone": "+7 900 555 66 88"}, T, "o")          # a phone typed on «На ОТХ»: no client either
    assert linked(conn, cid) is None and _clients_with(conn, "+79005556688") == []


def test_a_new_phone_with_any_other_move_links_its_client_as_before(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "moving", "2026-10-15T10:00:00")
    orders.move_card(conn, cid, stage(conn, "at_point"), T, "o", changes={"client_phone": "+7 900 555 66 77"})
    assert linked(conn, cid) in _clients_with(conn, "+79005556677")
    same = card_at(conn, "moving", "2026-10-15T10:00:00")
    orders.move_card(conn, same, stage(conn, "moving"), T, "o", changes={"client_phone": "+7 900 555 66 99"})   # no move: saved and linked
    assert linked(conn, same) in _clients_with(conn, "+79005556699")


def test_a_deleted_order_on_oth_takes_its_client_aside_and_its_marks_and_brings_them_back(tmp_path):
    """Хвост З16: «Удалённые» keep the client put aside and the marks «возвращено» of an order on «На ОТХ», and give them back."""
    from tires_sync import trash
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    client_id = linked(conn, cid)
    to_oth(conn, cid)
    item = orders.get_card(conn, cid)["items"][0]["id"]
    order_oth.mark_returned(conn, item, "2026-10-18T10:00:00", "o")
    aside = lambda: conn.execute("SELECT client_id FROM order_oth_client WHERE order_id = ?", (cid,)).fetchall()       # noqa: E731
    marks = lambda: conn.execute("SELECT item_id FROM oth_return WHERE order_id = ?", (cid,)).fetchall()               # noqa: E731
    record = trash.delete_order(conn, cid, "2026-10-18T11:00:00", "o")
    assert aside() == [] and marks() == []
    trash.restore_order(conn, record, "2026-10-18T12:00:00", "o")
    assert aside() == [(client_id,)] and marks() == [(item,)] and linked(conn, cid) is None      # still on «На ОТХ»: still unlinked


def test_an_order_restored_after_its_oth_stage_was_deleted_gets_its_client_back(tmp_path):
    from tires_sync import trash
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    client_id = linked(conn, cid)
    to_oth(conn, cid)
    record = trash.delete_order(conn, cid, "2026-10-18T11:00:00", "o")
    conn.execute("DELETE FROM order_stage WHERE code = 'oth'")
    conn.commit()
    trash.restore_order(conn, record, "2026-10-18T12:00:00", "o")
    assert orders.get_card(conn, cid)["stage"]["kind"] == "open" and linked(conn, cid) == client_id
    assert conn.execute("SELECT COUNT(*) FROM order_oth_client WHERE order_id = ?", (cid,)).fetchone()[0] == 0


def test_a_phone_typed_on_oth_links_its_client_when_the_order_goes_to_an_open_stage(tmp_path):
    """Review 1 of хвост З15: an order on «На ОТХ» without a client gets its phone there (no client then); back on an open stage it joins
    the client of that phone; into a closed stage it stays unlinked."""
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    conn.execute("DELETE FROM client_order WHERE order_id = ?", (cid,))                     # came without a client (no phone then)
    conn.commit()
    to_oth(conn, cid)
    orders.update_card(conn, cid, {"client_phone": "+7 900 555 66 77"}, T, "o")
    assert linked(conn, cid) is None and _clients_with(conn, "+79005556677") == []
    closed = stage(conn, "cancel")                                                           # a finished stage (lost)
    orders.move_card(conn, cid, closed, "2026-10-18T10:00:00", "o", changes={"cancel_reason": "передумал"})
    assert linked(conn, cid) is None                                                         # a closed stage: the client refused, as before
    orders.move_card(conn, cid, stage(conn, "at_point"), "2026-10-19T10:00:00", "o")
    assert linked(conn, cid) in _clients_with(conn, "+79005556677")


def test_a_phone_typed_with_the_move_from_oth_to_a_finished_stage_links_nothing(tmp_path):
    """Review 2 of хвост З15: from «На ОТХ» into a finished stage the client refused: a phone typed in that very move makes no client."""
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    conn.execute("DELETE FROM client_order WHERE order_id = ?", (cid,))
    conn.commit()
    to_oth(conn, cid)
    orders.move_card(conn, cid, stage(conn, "cancel"), "2026-10-18T10:00:00", "o",
                     changes={"cancel_reason": "передумал", "client_phone": "+7 900 555 66 77"})
    assert linked(conn, cid) is None and _clients_with(conn, "+79005556677") == []


def test_an_order_left_unlinked_on_purpose_stays_so_on_a_move_between_open_stages(tmp_path):
    """Review 2 of хвост З15: only a move from «На ОТХ» links an unlinked order by itself; an order the joining left to a person (a phone
    of another client, a disputed group of clients-link) is not linked by an ordinary move."""
    conn = db(tmp_path)
    cid = card_at(conn, "moving", "2026-10-15T10:00:00")
    conn.execute("DELETE FROM client_order WHERE order_id = ?", (cid,))
    conn.commit()
    clients_before = conn.execute("SELECT COUNT(*) FROM client").fetchone()[0]
    orders.move_card(conn, cid, stage(conn, "at_point"), T, "o")
    assert linked(conn, cid) is None and conn.execute("SELECT COUNT(*) FROM client").fetchone()[0] == clients_before


# ---- «Вернуть поставщику» по количеству (the owner 2026-10-24: «можно вернуть любое число шин, но не больше, чем было в заказе») ------------

def _refused(call, *args, **kwargs) -> str:
    try:
        call(*args, **kwargs)
    except order_oth.OthError as err:
        return str(err)
    raise AssertionError("not refused")


def test_a_part_is_returned_the_rest_stays_on_oth_and_the_catalog_counts_the_rest(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "oth", T)
    item = order_oth.stock(conn)["stock"][0]["item_id"]
    order_oth.mark_returned(conn, item, "2026-10-20T10:00:00", "o", qty="2")
    rows = order_oth.stock(conn)
    assert [(r["item_id"], r["qty"], r["ordered"], r["returned_qty"]) for r in rows["stock"]] == [(item, 2, 4, 2)]
    assert [(r["item_id"], r["qty"]) for r in rows["returned"]] == [(item, 2)]
    assert order_oth.goods_on_oth(conn) == {"1": 2}
    assert orders.get_card(conn, cid)["stage"]["code"] == "oth"
    assert any(e["text"] == "ОТХ: возвращено поставщику — Nokian 205/55 R16, 2 шт. (всего возвращено 2 из 4)" for e in orders.list_events(conn, cid))
    assert "не больше 2 шт." in _refused(order_oth.mark_returned, conn, item, "2026-10-20T10:01:00", "o", qty="3")
    order_oth.mark_returned(conn, item, "2026-10-20T10:02:00", "o", qty=2)
    assert order_oth.stock(conn)["stock"] == [] and order_oth.goods_on_oth(conn) == {}
    assert order_oth.stock(conn)["returned"][0]["qty"] == 4
    assert "вся возвращена" in _refused(order_oth.mark_returned, conn, item, "2026-10-20T10:03:00", "o", qty="1")
    assert conn.execute("SELECT qty FROM oth_return WHERE item_id = ?", (item,)).fetchone()[0] == 4


def test_a_wrong_quantity_is_refused_and_nothing_is_written(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "oth", T)
    item = order_oth.stock(conn)["stock"][0]["item_id"]
    events = len(orders.list_events(conn, cid))
    for bad in ("0", "-1", "abc", "1.5", "", "5"):
        _refused(order_oth.mark_returned, conn, item, "2026-10-20T10:00:00", "o", qty=bad)
    assert conn.execute("SELECT COUNT(*) FROM oth_return").fetchone()[0] == 0 and len(orders.list_events(conn, cid)) == events
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'oth_return'").fetchone()[0] == 0


def test_the_undo_goes_by_quantity_and_never_below_what_is_returned(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "oth", T)
    item = order_oth.stock(conn)["stock"][0]["item_id"]
    assert "не отмечена" in _refused(order_oth.unmark_returned, conn, item, "2026-10-20T09:00:00", "o", qty="1")
    order_oth.mark_returned(conn, item, "2026-10-20T10:00:00", "o", qty="3")
    assert "не больше 3 шт." in _refused(order_oth.unmark_returned, conn, item, "2026-10-20T10:01:00", "o", qty="4")
    order_oth.unmark_returned(conn, item, "2026-10-20T10:02:00", "o", qty="1")
    rows = order_oth.stock(conn)
    assert rows["stock"][0]["qty"] == 2 and rows["returned"][0]["qty"] == 2 and order_oth.goods_on_oth(conn) == {"1": 2}
    assert any(e["text"] == "ОТХ: отметка «возвращено поставщику» снята — Nokian 205/55 R16, 1 шт. (возвращено 2 из 4)" for e in orders.list_events(conn, cid))
    order_oth.unmark_returned(conn, item, "2026-10-20T10:03:00", "o")                     # no number: every returned piece
    assert conn.execute("SELECT COUNT(*) FROM oth_return").fetchone()[0] == 0 and order_oth.goods_on_oth(conn) == {"1": 4}


def test_no_number_returns_every_piece_left_and_an_old_mark_of_the_whole_position_reads_as_all(tmp_path):
    conn = db(tmp_path)
    card_at(conn, "oth", T)
    item = order_oth.stock(conn)["stock"][0]["item_id"]
    order_oth.mark_returned(conn, item, "2026-10-20T10:00:00", "o", qty="1")
    order_oth.mark_returned(conn, item, "2026-10-20T10:01:00", "o")                       # the older form sends no number: the rest (3)
    assert conn.execute("SELECT qty FROM oth_return WHERE item_id = ?", (item,)).fetchone()[0] == 4
    other = card_at(conn, "oth", T)                                                        # a mark made before this change: the whole quantity
    old = orders.get_card(conn, other)["items"][0]["id"]
    conn.execute("INSERT INTO oth_return (item_id, order_id, qty, at, author) VALUES (?, ?, 4, ?, 'o')", (old, other, T))
    conn.commit()
    assert order_oth.stock(conn)["stock"] == [] and len(order_oth.stock(conn)["returned"]) == 2
    orders.update_item(conn, old, 6, 5000, "2026-10-20T10:02:00", "o")                    # the order got two more after the old mark of four:
    rest = [r for r in order_oth.stock(conn)["stock"] if r["item_id"] == old]               # the two are ours, as a part returned
    assert [(r["qty"], r["returned_qty"]) for r in rest] == [(2, 4)] and order_oth.goods_on_oth(conn) == {"1": 2}


def test_an_empty_number_from_the_page_is_a_wrong_number_never_all(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    card_at(conn, "oth", T)
    item = order_oth.stock(conn)["stock"][0]["item_id"]
    post(owner, "/oth/return", tok, item=str(item), qty="")
    assert "Количество: целое число от 1." in owner.get("/oth").get_data(as_text=True)
    assert conn.execute("SELECT COUNT(*) FROM oth_return").fetchone()[0] == 0
    post(owner, "/oth/return", tok, item=str(item), qty="2")
    post(owner, "/oth/return", tok, item=str(item), qty=" ", undo="1")
    assert conn.execute("SELECT qty FROM oth_return").fetchone()[0] == 2


def test_a_quantity_lowered_after_a_return_leaves_no_negative_rest(tmp_path):
    conn = db(tmp_path)
    card_at(conn, "oth", T)
    item = order_oth.stock(conn)["stock"][0]["item_id"]
    order_oth.mark_returned(conn, item, "2026-10-20T10:00:00", "o", qty="3")
    orders.update_item(conn, item, 2, 5000, "2026-10-20T10:01:00", "o")                    # the manager corrects the order: two tyres, not four
    rows = order_oth.stock(conn)
    assert rows["stock"] == [] and rows["returned"][0]["qty"] == 3 and order_oth.goods_on_oth(conn) == {}
    assert "вся возвращена" in _refused(order_oth.mark_returned, conn, item, "2026-10-20T10:02:00", "o", qty="1")
    order_oth.unmark_returned(conn, item, "2026-10-20T10:03:00", "o", qty="2")
    assert order_oth.stock(conn)["stock"][0]["qty"] == 1


def test_another_tab_cannot_return_more_than_the_order_had(tmp_path, monkeypatch):
    conn = db(tmp_path)
    card_at(conn, "oth", T)
    item_id = order_oth.stock(conn)["stock"][0]["item_id"]
    stale = order_oth._stock_item(conn, item_id)                                           # what the other tab saw: nothing returned yet
    order_oth.mark_returned(conn, item_id, "2026-10-20T10:00:00", "o", qty="3")
    monkeypatch.setattr(order_oth, "_stock_item", lambda conn, item_id: dict(stale))
    assert "другой вкладке" in _refused(order_oth.mark_returned, conn, item_id, "2026-10-20T10:00:01", "o", qty="2")
    assert conn.execute("SELECT qty FROM oth_return").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'oth_return'").fetchone()[0] == 1


def test_the_page_returns_and_undoes_a_number_of_tyres(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    card_at(conn, "oth", T)
    html = owner.get("/oth").get_data(as_text=True)
    item = re.search(r'name="item" value="(\d+)"', html).group(1)
    assert re.search(r'name="qty" value="4" min="1" max="4"', html)
    post(owner, "/oth/return", tok, item=item, qty="1")
    html = owner.get("/oth").get_data(as_text=True)
    assert "Отмечено как возвращённое поставщику" in html
    assert "из 4, возвращено 1" in html and "Возвращено поставщику" in html                   # the part is in both tables
    assert re.search(r'name="qty" value="3" min="1" max="3"', html) and re.search(r'name="qty" value="1" min="1" max="1"', html)
    post(owner, "/oth/return", tok, item=item, qty="9")
    assert "Можно вернуть не больше 3 шт." in owner.get("/oth").get_data(as_text=True)
    post(owner, "/oth/return", tok, item=item, qty="1", undo="1")
    assert order_oth.goods_on_oth(conn) == {"1": 4}


def test_the_journal_of_actions_names_the_number_of_an_undo(tmp_path):
    import json
    from tires_sync.panel import texts
    conn = db(tmp_path)
    card_at(conn, "oth", T)
    item = order_oth.stock(conn)["stock"][0]["item_id"]
    order_oth.mark_returned(conn, item, "2026-10-18T10:00:00", "o", qty="2")
    order_oth.unmark_returned(conn, item, "2026-10-18T10:01:00", "o", qty="1")
    shown = [texts.audit_text({"action": a, "detail": json.loads(d)}) for a, d in
             conn.execute("SELECT action, detail FROM audit WHERE action IN ('oth_return', 'oth_return_undo') ORDER BY id")]
    assert "(2 шт.)" in shown[0] and "снята (1 шт.)" in shown[1], shown
