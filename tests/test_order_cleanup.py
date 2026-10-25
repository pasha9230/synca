"""«Убрать пустые карточки из «Нового заказа»»: only deals made from chats that nobody worked on go to «Удалённые», a copy of the base comes first,
the chats stay, and a card touched meanwhile is left alone."""
from __future__ import annotations

import sqlite3

import pytest

from panel_helpers import login, make_app, post
from test_chat_deals import Fake
from tires_sync import chat_deals as deals, chats, connections, order_cleanup as cleanup, order_wait, orders, store, trash

MADE = "2026-10-13T12:00:00"
NOW = "2026-10-19T12:00:00"
NAMES = ("empty", "manual", "button", "webmim", "moved", "note", "want", "task", "fresh", "sale")


def base(path):
    conn = store.connect(path)
    orders.ensure_pipeline(conn, MADE)
    account = connections.create_account(conn, "Основной", MADE, "o")
    connections.set_modes(conn, account, True, True, MADE, "o")
    fake = Fake()
    for n, name in enumerate(n for n in NAMES if n != "manual"):
        fake.add("c%d" % n, name, [("in", "Здравствуйте, есть 185/65 R15?")], item=100 + n)
    chats.sync_account(conn, fake, account, MADE)
    cards = {}
    for chat in chats.list_chats(conn):
        made = NOW if chat["peer_name"] == "fresh" else MADE
        by = "manager" if chat["peer_name"] == "button" else deals.SYSTEM_AUTHOR
        cards[chat["peer_name"]] = deals.create_deal(conn, chat["id"], made, by)
        orders.add_item(conn, cards[chat["peer_name"]], goods_id="1", name="Ikon 185/65 R15", brand_key="ikon", season="winter", diameter=15,
                        territory="1", qty=4, sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=made, author="manager")
    cards["manual"] = orders.create_card(conn, {"client_name": "Иван"}, MADE, "manager")
    orders.update_card(conn, cards["webmim"], {"webmim_number": "12345"}, MADE, "manager")
    stages = orders.list_stages(conn)
    orders.move_card(conn, cards["moved"], stages[1]["id"], "2026-10-14T10:00:00", "manager")
    orders.move_card(conn, cards["moved"], stages[0]["id"], "2026-10-14T10:05:00", "manager")
    orders.add_note(conn, cards["note"], "Перезвонить", "2026-10-14T10:00:00", "manager")
    order_wait.add_want(conn, cards["want"], {"brand": "Ikon", "width": "185", "profile": "65", "radius": "15", "qty": "4"}, MADE, "manager")
    conn.execute("INSERT INTO order_task (order_id, rule_id, entered_at, kind, status, title, created_at) VALUES (?, 1, ?, 'notify', 'done', 'Сообщить', ?)",
                 (cards["task"], MADE, MADE))
    conn.execute("INSERT INTO order_task (order_id, rule_id, entered_at, kind, status, title, created_at) VALUES (?, 2, ?, 'notify', 'pending', 'Сообщить', ?)",
                 (cards["empty"], MADE, MADE))
    conn.execute("UPDATE order_card SET sale_ids = '[7]' WHERE id = ?", (cards["sale"],))
    conn.commit()
    return conn, cards


def test_the_preview_takes_only_untouched_chat_deals_and_says_why_the_others_stay(tmp_path):
    conn, cards = base(tmp_path / "t.db")
    plan = cleanup.preview(conn, NOW)
    assert [c["id"] for c in plan["remove"]] == [cards["empty"]]
    assert plan["total"] == len(NAMES)
    kept = dict(plan["kept"])
    for key in ("not_chat", "by_person", "webmim", "moved", "edited", "wants", "tasks", "fresh", "sale"):
        assert kept[cleanup.REASONS[key]] == 1, key


def test_the_run_takes_a_copy_first_moves_the_cards_to_deleted_and_keeps_their_chats(tmp_path):
    db = tmp_path / "t.db"
    conn, cards = base(db)
    chat_id = int(orders.CHAT_REF.match(orders.get_card(conn, cards["empty"])["avito_chat"]).group(1))
    done = cleanup.run(conn, db, NOW, "owner", expected=cleanup.preview(conn, NOW)["sign"])
    assert done == {"removed": 1, "skipped": 0, "left": 0, "backup": "pre-cleanup-new-20261019-120000.db"}
    copy = sqlite3.connect(str(tmp_path / done["backup"]))
    assert copy.execute("SELECT COUNT(*) FROM order_card WHERE id = ?", (cards["empty"],)).fetchone()[0] == 1     # the copy is from before
    copy.close()
    with pytest.raises(orders.OrderError):
        orders.get_card(conn, cards["empty"])
    for name in NAMES[1:]:
        orders.get_card(conn, cards[name])                                                                          # the others are all there
    chat = chats.get_chat(conn, chat_id)
    assert chat is not None and chat["order_id"] is None
    [row] = trash.list_deleted(conn)
    assert row["record_id"] == cards["empty"]
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = ?", (cleanup.ACTION,)).fetchone()[0] == 1
    assert trash.restore_order(conn, row["id"], NOW, "owner") == cards["empty"]                                    # and it comes back


def test_nothing_is_done_when_the_list_changed_since_the_page_was_opened(tmp_path):
    db = tmp_path / "t.db"
    conn, cards = base(db)
    with pytest.raises(orders.OrderError):
        cleanup.run(conn, db, NOW, "owner", expected=cleanup.sign([{"id": cards["note"]}]))
    orders.get_card(conn, cards["empty"])
    assert not list(tmp_path.glob("pre-cleanup-new-*.db"))


def test_a_card_touched_between_the_check_and_its_delete_stays(tmp_path):
    conn, cards = base(tmp_path / "t.db")
    orders.add_note(conn, cards["empty"], "Клиент перезвонил", "2026-10-18T09:00:00", "manager")
    cutoff = cleanup.cutoff_for(NOW)
    with pytest.raises(orders.OrderError):
        trash.delete_order(conn, cards["empty"], NOW, "owner",
                           only_if=lambda c, card: cleanup.REASONS[r] if (r := cleanup.keep_reason(c, card, cutoff)) else None)
    orders.get_card(conn, cards["empty"])
    assert not trash.list_deleted(conn)


def test_the_panel_shows_the_rule_and_the_count_and_the_button_removes(tmp_path):
    app = make_app(tmp_path, NOW=lambda: __import__("datetime").datetime(2026, 10, 19, 9, 0, 0))
    conn, cards = base(app.config["DB_PATH"])
    client = app.test_client()
    tok = login(client)
    page = client.get("/orders/cleanup").get_data(as_text=True)
    assert "Будет убрано: <b>1</b>" in page and cleanup.REASONS["edited"] in page and 'name="expected" value="%s"' % cleanup.preview(conn, NOW)["sign"] in page
    assert "/orders/cleanup" in client.get("/deleted").get_data(as_text=True)
    assert post(client, "/orders/cleanup", tok, expected="0000").status_code == 302
    orders.get_card(conn, cards["empty"])                                                                           # wrong number: nothing
    response = post(client, "/orders/cleanup", tok, expected=cleanup.preview(conn, NOW)["sign"])
    assert response.status_code == 302 and "/deleted" in response.headers["Location"]
    with pytest.raises(orders.OrderError):
        orders.get_card(conn, cards["empty"])
    assert list(tmp_path.glob("pre-cleanup-new-*.db"))
