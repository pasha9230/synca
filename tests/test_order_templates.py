"""Message templates of the Orders section: storage, variable check, rendering."""
from __future__ import annotations

import pytest

from tires_sync import order_points as points, order_templates as tpl, orders, store

NOW = "2026-10-12T12:00:00"


def db(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    return conn


def full_card(conn):
    pid = points.save_point(conn, dict(name="Бета", manager_name="Алексей", channel="vk"))
    card_id = orders.create_card(conn, dict(client_name="Иван", client_phone="+7 900 111 22 33", point_id=pid, delivery_date="2026-10-15",
                                            delivery_time="14:00"), NOW, "o")
    orders.add_item(conn, card_id, goods_id="1", name="Nokian 205/55 R16", brand_key="nokian", season="winter", diameter=16, territory="1",
                    qty=4, sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")
    return card_id


def test_a_template_is_saved_updated_and_deleted_and_the_variables_are_checked(tmp_path):
    conn = db(tmp_path)
    tid = tpl.add_template(conn, "Клиенту: оформлен", "client", "Здравствуйте, {client_name}! Заказ №{order_no}.")
    assert tpl.get_template(conn, tid)["audience"] == "client"
    tpl.update_template(conn, tid, "Клиенту", "manager", "Заказ {order_no}")
    assert tpl.get_template(conn, tid)["audience"] == "manager" and len(tpl.list_templates(conn)) == 1
    for bad in (("", "client", "x"), ("n", "other", "x"), ("n", "client", ""), ("n", "client", "{nope}"), ("n" * 81, "client", "x"), ("n", "client", "x" * 2001)):
        with pytest.raises(orders.OrderError):
            tpl.add_template(conn, *bad)
    tpl.delete_template(conn, tid)
    assert tpl.list_templates(conn) == []
    with pytest.raises(orders.OrderError):
        tpl.get_template(conn, tid)


def test_render_fills_every_variable_from_the_card(tmp_path):
    conn = db(tmp_path)
    card = orders.get_card(conn, full_card(conn))
    text, missing = tpl.render(conn, card, "{client_name}|{client_phone}|{order_no}|{items}|{total}|{point}|{manager}|{delivery_date}|{delivery_time}")
    assert missing == []
    assert text.split("|")[:3] == ["Иван", "+7 900 111 22 33", str(card["id"])]
    assert "Nokian 205/55 R16 × 4" in text and "20 000" in text and "Бета" in text and "Бета" == text.split("|")[6]
    assert "15.10.2026" in text and "14:00" in text


def test_a_missing_value_is_reported_and_never_left_empty(tmp_path):
    conn = db(tmp_path)
    card = orders.get_card(conn, orders.create_card(conn, dict(client_name="Иван"), NOW, "o"))
    text, missing = tpl.render(conn, card, "{client_name}, {client_phone}, {point}")
    assert missing == ["Телефон", "Точка"] and "{" not in text


def test_the_owners_two_order_texts_render_for_the_client_and_the_manager(tmp_path):
    conn = db(tmp_path)
    assert tpl.add_samples(conn) == 4 and tpl.add_samples(conn) == 0                      # the third: «можно забирать», the fourth: «срок резерва истекает»; a second press adds nothing
    card_id = full_card(conn)
    conn.execute("INSERT INTO avito_address (account_id, position, name, address_id, enabled, share, address_text) VALUES (1, 0, 'Бета', '77', 1, 1, "
                 "'Санкт-Петербург, ул. Генерала Примерова, 35А')")
    conn.execute("UPDATE point SET avito_address_id = '77'")
    conn.commit()
    orders.update_card(conn, card_id, {"webmim_number": "5000100"}, "2026-10-12T12:00:00", "o")
    card = orders.get_card(conn, card_id)
    client, manager, _arrived, _reserve = tpl.list_templates(conn)
    text, missing = tpl.render(conn, card, client["body"])
    assert missing == [] and text.startswith("Заказ 5000100 оформлен на перемещение.")
    assert "Товар Nokian 205/55 R16\nКод 1\nКол-во 4 шт\nЦена ед. 5 000 ₽\nСумма 20 000 ₽" in text
    assert "Территория Санкт-Петербург, ул. Генерала Примерова, 35А" in text and "Хранение резерва до 18.10.2026" in text and "К оплате принимаем наличные" in text and "+7 900 111 22 33" in text
    short, _ = tpl.render(conn, card, manager["body"])
    assert "наличные" not in short and "резерва" not in short and "Яндекс" not in short and "Код 1" in short
    bare = orders.get_card(conn, orders.create_card(conn, dict(client_name="Иван"), NOW, "o"))
    assert "Номер заказа WebMIM" in tpl.render(conn, bare, client["body"])[1]            # not before the WebMIM number is entered


def test_money_in_the_text_is_written_for_people(tmp_path):
    conn = db(tmp_path)
    cid = full_card(conn)
    conn.execute("UPDATE order_item SET sale_unit = 313641.75, qty = 4 WHERE order_id = ?", (cid,))
    conn.commit()
    text, _ = tpl.render(conn, orders.get_card(conn, cid), "{items_full}|{total}")
    assert "Цена ед. 313 641.75 ₽" in text and "Сумма 1 254 567 ₽" in text and text.endswith("|1 254 567") and "e+" not in text


def test_the_reserve_date_is_the_planned_delivery_plus_three_days_not_the_order_day_and_cents_are_kept(tmp_path):
    conn = db(tmp_path)
    cid = full_card(conn)
    orders.move_card(conn, cid, next(s["id"] for s in orders.list_stages(conn) if s["code"] == "placed"), "2026-10-12T12:00:00", "o")
    orders.move_card(conn, cid, next(s["id"] for s in orders.list_stages(conn) if s["code"] == "moving"), "2026-10-14T09:00:00", "o")
    text, _ = tpl.render(conn, orders.get_card(conn, cid), "{hold_until}")
    assert text == "18.10.2026"                                                          # delivery 15th + 3 days; the day of the order (12th) does not count since 2026-10-19
    assert tpl.money(100.5) == "100.50" and tpl.money(100) == "100" and tpl.money(1254567) == "1 254 567"
