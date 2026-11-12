"""A temporary panel base with invented data for the screen audit (tests/e2e). Everything here is fake: example.com, numbers +7 900 000-xx-xx,
names nobody has. Nothing is read from the production base or the server; nothing is sent anywhere.

    python tests/e2e/seed.py <dir>          # makes <dir>/panel.db and the files the panel reads; prints the logins

The orders are made through the panel's own code (orders.create_card / move_card / order_oth.agree) with the stage rules asleep
(fire_rules=False: no message, no task of a rule), then their dates are shifted by SQL so that the reservation reads «день 3» / «день 7»."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent.parent / "src"), str(HERE.parent)]

from helpers import catalog_xml, commodity_xml, settings_dict  # noqa: E402  (tests/helpers.py: the same fake unload as the unit tests)

from tires_sync import connections, order_oth, order_points, order_webmim, orders, oth_storage, store, tasks, users  # noqa: E402
from tires_sync.settings_store import init_settings  # noqa: E402

PASSWORD = "e2e-password-1"          # every seeded user (a fake base: the password is not a secret)
OWNER_PASSWORD = "e2e-owner-1"       # the shared owner password of the panel (PASSWORD_HASH)
UI_GROUPS = ("client", "point", "office")

REF_TEXT = ('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n<model name="Other Model"/>\n</make>\n'
            '<make name="Pirelli">\n<model name="Test Model"/>\n</make>\n</Shiny>')
UNLOAD = catalog_xml(
    commodity_xml(NNOMMODIF="100"),
    commodity_xml(NNOMMODIF="101", SMODEL="Test Model 2", SMODIFNAME="IKON 215/60/17 T 96 Test Model 2", SDIAMETR="17", SWIDTH="215", SHEIGHT="60"),
    commodity_xml(NNOMMODIF="102", SMARKA="Pirelli", SMODIFNAME="PIRELLI 205/55/16 T 94 Test Model", NPRICE_DELAYED="7000"),
    commodity_xml(NNOMMODIF="103", SMARKA="Неизвестный", SMODIFNAME="UNKNOWN 205/55/16 X", SMODEL="X"),
)

# who can sign in (login → role); the owner signs in with the shared password, login «owner»
PEOPLE = {
    "owner": ("owner", None, "Владелец Тест"),
    "partner1": ("partner", None, "Партнёр Тест"),
    "office1": ("chats", None, "Офис Тест"),            # «Менеджер чатов»: the office manager
    "viewer1": ("viewer", None, "Наблюдатель Тест"),
    "point1": ("point", 1, "Точка Один"),
    "point2": ("point", 2, "Точка Два"),
    "client1": ("client", None, "Магазин Пример"),
}


def iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def build(root: Path, now: datetime = None) -> dict:
    now = now or datetime.now().replace(second=0, microsecond=0)
    root.mkdir(parents=True, exist_ok=True)
    db = root / "panel.db"
    if db.exists():
        db.unlink()
    (root / "unload.xml").write_bytes(UNLOAD)
    (root / "reference.txt").write_text(REF_TEXT, encoding="utf-8")
    at = iso(now - timedelta(days=12))
    conn = store.connect(db)
    init_settings(conn, settings_dict(contact_phone="+79000000000"), at)
    for group in UI_GROUPS:                                              # «Новый вид»: on for everybody, as in production
        store.set_state(conn, "ui_new.%s" % group, "all")

    # --- two Avito accounts, three points ------------------------------------------------------------------------------------------
    north = connections.create_account(conn, "Север Пример", at, "seed")
    south = connections.create_account(conn, "Юг Пример", at, "seed")
    connections.set_avito_identity(conn, south, None, "Юг Пример", "79000000102")
    connections.save_addresses(conn, south, "percent", None,
                               [{"name": "Юг", "address_id": "7000002", "enabled": True, "share": 100, "address_text": ""}], at, "seed")
    conn.execute("UPDATE account SET chats_enabled = 1 WHERE id IN (?, ?)", (north, south))
    conn.commit()
    pts = []
    for n, (name, account) in enumerate((("Точка Один", north), ("Точка Два", south), ("Точка Три", north)), 1):
        pts.append(order_points.save_point(conn, dict(
            name=name, manager_name="Менеджер %d" % n, manager_phone="+7 900 000-10-%02d" % n, channel=("vk", "panel", "telegram")[n - 1],
            webmim_address_id="ADDR%d" % n, default_time="10:00–20:00", address="г. Пример, ул. Тестовая, %d" % n,
            hours="ежедневно 10:00–20:00", avito_account_id=account,
            vk_peer_id=str(9000 + n) if n == 1 else "")))

    # --- users of every role -------------------------------------------------------------------------------------------------------
    for login, (role, point, name) in PEOPLE.items():
        if login == "owner":
            continue
        users.add_user(conn, login, PASSWORD, at, role=role, point_id=pts[point - 1] if point else None, display_name=name)
    conn.commit()

    orders.ensure_pipeline(conn, at)
    stage = {s["code"]: s for s in orders.list_stages(conn)}

    # --- orders in every stage -----------------------------------------------------------------------------------------------------
    made, where = {}, {}
    phone_no = [0]

    def card(label, path, point=0, days_ago=1, webmim="", transfer="", qty=4, extra=None, item="IKON 205/55/16 T 94 Test Model",
             goods="100", price=5000):
        phone_no[0] += 1
        when = iso(now - timedelta(days=days_ago, hours=phone_no[0]))
        fields = dict(client_name="Клиент %s" % label, client_phone="+7 900 000-20-%02d" % phone_no[0], point_id=pts[point],
                      delivery_date=(now + timedelta(days=2)).date().isoformat(), comment="Тестовый заказ: %s" % label)
        fields.update(extra or {})
        cid = orders.create_card(conn, fields, when, "seed")
        orders.add_item(conn, cid, goods_id=goods, name=item, brand_key="IKON TYRES", season="winter", diameter=16, territory="T1", qty=qty,
                        sale_unit=price, supplier_price=price * 0.8, terms=None, fee_pct=0, promo_pct=0, now=when, author="seed")
        if webmim:
            orders.update_card(conn, cid, dict(webmim_number=webmim, transfer_number=transfer), when, "seed")
        for code in path:
            orders.move_card(conn, cid, stage[code]["id"], when, "seed", fire_rules=False)
            stored = oth_storage.record_of(conn, cid) if code == "oth" else None
            if stored:                                  # the order went to «На ОТХ»: it is cancelled, its tyres are the record Х (oth_storage)
                cid = stored                            # the rest of the path is the record's, as on the board
                if extra:
                    orders.update_card(conn, cid, {k: v for k, v in extra.items() if k.startswith("return_")}, when, "seed")
        conn.execute("UPDATE order_card SET entered_at = ?, created_at = ? WHERE id = ?", (when, when, cid))
        conn.commit()
        made[label] = cid
        where[label] = point
        return cid

    card("новый", [], days_ago=0)
    card("в работе", ["talk"], days_ago=0)
    card("оформлен в WebMIM", ["placed"], webmim="500101", days_ago=1)
    card("номер перемещения", ["placed"], webmim="500102", transfer="ПР-000102", days_ago=1, point=1)
    card("едет на точку", ["placed", "moving"], webmim="500103", transfer="ПР-000103", days_ago=2)
    card("на точке", ["placed", "moving", "at_point"], webmim="500104", transfer="ПР-000104", days_ago=0, point=1)
    r3 = card("резерв день 3", ["placed", "moving", "at_point"], webmim="500105", transfer="ПР-000105", days_ago=3)
    r7 = card("резерв день 7", ["placed", "moving", "at_point"], webmim="500106", transfer="ПР-000106", days_ago=7, point=1)
    for cid, days in ((r3, 3), (r7, 7)):
        conn.execute("UPDATE order_card SET reserve_until = ? WHERE id = ?", ((now - timedelta(days=days) + timedelta(days=3)).date().isoformat(), cid))
    oth = card("ОТХ", ["placed", "moving", "at_point", "oth"], webmim="500107", transfer="ПР-000107", days_ago=9)
    card("на возврат", ["placed", "moving", "at_point", "oth", "return_given"], webmim="500108", transfer="ПР-000108", days_ago=10, point=1)
    agreed = card("согласовано на возврат", ["placed", "moving", "at_point", "oth", "return_given"], webmim="500109", transfer="ПР-000109",
                  days_ago=11)
    try:
        order_oth.agree(conn, orders.get_card(conn, agreed), now.date(), iso(now), "seed")
    except Exception as err:                                            # the audit shows the card anyway; the seed says why
        print("seed: «согласовано на возврат» not agreed: %s" % err, file=sys.stderr)
    card("вернули поставщику", ["placed", "moving", "at_point", "oth", "return_given"], webmim="500110", transfer="ПР-000110", days_ago=14,
         extra=dict(return_kind="supplier", return_reason="Тест: вернули", return_amount="20000"))
    ret = made["вернули поставщику"]
    orders.move_card(conn, ret, stage["return"]["id"], iso(now - timedelta(days=13)), "seed", fire_rules=False)
    card("выдан", ["placed", "moving", "at_point", "done"], webmim="500111", transfer="ПР-000111", days_ago=5)
    card("отменён до точки", ["placed", "cancel"], webmim="500112", days_ago=4, extra=dict(cancel_reason="отменил клиент"))
    split = card("два заказа WebMIM", ["placed"], webmim="500113", transfer="ПР-000113", qty=2, days_ago=2, point=2)
    try:                                                                # a second WebMIM order: pieces added after the own number
        order_webmim.snapshot(conn, split, iso(now), fresh=True)
        item = orders.list_items(conn, split)[0]
        orders.update_item(conn, item["id"], 4, item["sale_unit"], iso(now), "seed")
        order_webmim.set_extra_numbers(conn, split, item["id"], "500114", "ПР-000114", iso(now), "seed")
    except Exception as err:
        print("seed: the split order is not split: %s" % err, file=sys.stderr)
    conn.commit()

    # --- chats of both accounts, some linked to the orders -------------------------------------------------------------------------
    chats = []
    for n in range(1, 9):
        account = north if n % 2 else south
        last = now - timedelta(hours=n * 3)
        linked = list(made.values())[n] if n <= 4 else None
        row = conn.execute(
            "INSERT INTO avito_chat (account_id, chat_id, item_id, item_title, item_url, price_string, peer_id, peer_name, last_text, last_at,"
            " last_dir, unread, status, order_id, updated_ts, created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (account, "u2i-e2e-%d" % n, str(4000000 + n), "Шины IKON 205/55 R16 зимние, %d шт" % (n % 4 + 1), "https://example.com/item/%d" % n,
             "%d ₽" % (5000 * (n % 4 + 1)), str(800 + n), "Покупатель %d" % n, "Здравствуйте, есть в наличии?" if n % 3 else "Спасибо, заберу завтра",
             iso(last), "in" if n % 2 else "out", 1 if n % 3 else 0, "open" if n < 7 else "closed", linked, int(last.timestamp()),
             int(last.timestamp()) - 3600)).lastrowid
        chats.append(row)
        for m in range(4):
            moment = last - timedelta(minutes=(4 - m) * 7)
            conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, kind, text, is_read) VALUES (?,?,?,?,?,?,?,?)",
                         (row, "m-%d-%d" % (n, m), iso(moment), int(moment.timestamp()), "in" if m % 2 == 0 else "out", "text",
                          ("Здравствуйте, есть в наличии?", "Да, есть 4 шт.", "А доставка на точку когда?", "Послезавтра, после 12:00.")[m], 1))
    conn.commit()

    # --- alerts: open red ones, one already resolved; tasks ------------------------------------------------------------------------
    for label in ("резерв день 7", "ОТХ", "оформлен в WebMIM"):
        orders.add_event(conn, made[label], iso(now - timedelta(hours=2)), "system", "refused", "Тест: требует действия (%s)" % label, "red")
    orders.add_event(conn, made["едет на точку"], iso(now - timedelta(hours=5)), "system", "refused", "Тест: закрытое уведомление", "red")
    closed = conn.execute("SELECT max(id) FROM order_event").fetchone()[0]
    from tires_sync import order_alerts
    order_alerts.resolve(conn, closed, iso(now - timedelta(hours=4)), "owner")
    conn.commit()
    for n, label in enumerate(("новый", "в работе", "на точке")):
        try:
            tasks.add(conn, kind=sorted(tasks.KINDS)[0], title="Тест: перезвонить (%s)" % label, due_at=iso(now + timedelta(hours=n + 2))[:16],
                      note="Тестовая задача", now=iso(now), author="owner", order_id=made[label])
        except Exception as err:
            print("seed: task not made: %s" % err, file=sys.stderr)
    conn.commit()
    conn.close()
    return {"db": str(db), "orders": made, "own_orders": sorted(v for k, v in made.items() if where[k] == 0),
            "foreign_order": next(v for k, v in made.items() if where[k] == 1), "points": pts, "accounts": [north, south], "chats": chats}


if __name__ == "__main__":
    out = build(Path(sys.argv[1] if len(sys.argv) > 1 else "e2e-tmp"))
    print(json.dumps(out, ensure_ascii=False, indent=1))
