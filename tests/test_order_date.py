"""The template variable {order_date}: the day the order last entered «Заказ оформлен», else today."""
from __future__ import annotations

import re

from tires_sync import order_points as points, order_templates as tpl, orders, store

NOW = "2026-10-12T12:00:00"


def setup(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    pid = points.save_point(conn, dict(name="Бета", manager_name="Алексей", channel="vk"))
    cid = orders.create_card(conn, dict(client_name="Иван", client_phone="+7 900 111 22 33", point_id=pid, delivery_date="2026-10-15"), NOW, "o")
    orders.add_item(conn, cid, goods_id="1", name="Nokian 205/55 R16", brand_key="nokian", season="winter", diameter=16, territory="1",
                    qty=4, sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")
    return conn, cid


def stage(conn, code):
    return next(s["id"] for s in orders.list_stages(conn) if s["code"] == code)


def render(conn, cid, now=None):
    return tpl.render(conn, orders.get_card(conn, cid), "Оформлен {order_date}", now)


def test_the_variable_is_listed_and_a_template_with_it_is_saved(tmp_path):
    conn, _ = setup(tmp_path)
    assert tpl.VARIABLES["order_date"] == "Дата оформления"
    tid = tpl.add_template(conn, "Дата", "client", "Заказ оформлен {order_date}")
    assert tpl.get_template(conn, tid)["body"] == "Заказ оформлен {order_date}"


def test_the_date_is_the_day_the_order_entered_placed(tmp_path):
    conn, cid = setup(tmp_path)
    orders.move_card(conn, cid, stage(conn, "placed"), "2026-10-13T09:30:00", "o")
    assert render(conn, cid, "2026-10-20T10:00:00") == ("Оформлен 13.10.2026", [])


def test_a_later_entry_into_placed_wins(tmp_path):
    conn, cid = setup(tmp_path)
    orders.move_card(conn, cid, stage(conn, "placed"), "2026-10-13T09:30:00", "o")
    orders.move_card(conn, cid, stage(conn, "moving"), "2026-10-14T09:30:00", "o")
    orders.move_card(conn, cid, stage(conn, "placed"), "2026-10-16T09:30:00", "o")
    orders.move_card(conn, cid, stage(conn, "moving"), "2026-10-17T09:30:00", "o")
    assert render(conn, cid, "2026-10-20T10:00:00")[0] == "Оформлен 16.10.2026"


def test_without_an_entry_the_date_is_today(tmp_path):
    conn, cid = setup(tmp_path)
    assert render(conn, cid, "2026-10-20T10:00:00") == ("Оформлен 20.10.2026", [])
    assert re.fullmatch(r"Оформлен \d{2}\.\d{2}\.\d{4}", render(conn, cid)[0])      # no `now` given: the panel clock is used, never empty


def test_leaving_placed_is_not_an_entry_and_other_cards_do_not_count(tmp_path):
    conn, cid = setup(tmp_path)
    pid = conn.execute("SELECT point_id FROM order_card WHERE id = ?", (cid,)).fetchone()[0]
    other = orders.create_card(conn, dict(client_name="Пётр", client_phone="+7 900 111 22 34", point_id=pid, delivery_date="2026-10-15"), NOW, "o")
    orders.add_item(conn, other, goods_id="1", name="Nokian 205/55 R16", brand_key="nokian", season="winter", diameter=16, territory="1",
                    qty=4, sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")
    orders.move_card(conn, other, stage(conn, "placed"), "2026-10-13T09:30:00", "o")
    orders.move_card(conn, other, stage(conn, "moving"), "2026-10-14T09:30:00", "o")
    assert render(conn, cid, "2026-10-20T10:00:00")[0] == "Оформлен 20.10.2026"
    assert render(conn, other, "2026-10-20T10:00:00")[0] == "Оформлен 13.10.2026"


def test_a_renamed_placed_stage_keeps_the_date_while_the_order_stays_in_it(tmp_path):
    conn, cid = setup(tmp_path)
    orders.move_card(conn, cid, stage(conn, "placed"), "2026-10-13T09:30:00", "o")
    conn.execute("UPDATE order_stage SET name = 'Оформлен' WHERE code = 'placed'")
    conn.commit()
    assert render(conn, cid, "2026-10-20T10:00:00")[0] == "Оформлен 13.10.2026"
