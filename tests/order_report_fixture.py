"""One test base for the report of the orders: sales over three months, several points, kopecks, a loss, manual sales and reversals.
The golden numbers in tests/test_order_report_rates.py were taken from this base with the report as it was before the rates got dates."""
from __future__ import annotations

from tires_sync import sales

NOW = "2026-10-20T12:00:00"

# (order, point, day, [(sale_total, supplier_price, pieces), ...]) — every position of an order is one sale
ORDERS = [
    (101, "Бета", "2026-08-03", [(20000, 4000, 4)]),
    (102, "Бета", "2026-08-15", [(10000.55, 4000, 2), (6000.40, 4100.10, 1)]),
    (103, "Альфа", "2026-08-31", [(16101.37, 4000, 4)]),
    (104, "", "2026-08-20", [(9000, 4000, 2)]),
    (201, "Бета", "2026-09-01", [(16050, 4000, 4)]),
    (202, "Альфа", "2026-09-10", [(16001, 4000, 4)]),                # net below zero after the fee
    (203, "Альфа", "2026-09-30", [(30000.99, 7000.33, 4), (5000, 4000, 1)]),
    (204, "Гамма", "2026-09-17", [(12345.67, 2900, 4)]),
    (301, "Бета", "2026-10-01", [(24000, 5000, 4)]),
    (302, "Бета", "2026-10-09", [(8000.01, 3500, 2)]),
    (303, "Гамма", "2026-10-12", [(19999.99, 4444.44, 4)]),
    (304, "Альфа", "2026-10-19", [(7000, 3000, 2), (3000.5, 1000.25, 1), (100, 50, 1)]),
    (305, "", "2026-10-20", [(4100, 4000, 1)]),
]
REVERSED_ORDERS = [
    (150, "Бета", "2026-08-10", [(50000, 4000, 4)]),
    (350, "Альфа", "2026-10-05", [(40000, 5000, 4)]),
]
MANUAL = [
    ("Бета", "2026-08-12", 15000, 3000, 4),
    ("", "2026-09-05", 7777.77, 1500, 4),
    ("Альфа", "2026-10-03", 21000.5, 4000, 4),
]
REVERSED_MANUAL = [("Бета", "2026-10-11", 99999, 1000, 4)]


def _sale(conn, day, point, note, source, sale_total, supplier, pieces, n):
    return sales.add_sale(conn, day=day, goods_id="g%d" % n, brand_key="nokian", season="winter", diameter=16, name="Nokian 205/55 R16 №%d" % n,
                          ad_kind="single", pieces=pieces, sale_total=sale_total, supplier_price=supplier, terms=None, fee_pct=0, promo_pct=0,
                          account_id=None, point=point, note=note, now=NOW, author="o", today="2026-10-20", source=source)


def fill(conn) -> None:
    n = 0
    for orders_list, reverse in ((ORDERS, False), (REVERSED_ORDERS, True)):
        for order, point, day, items in orders_list:
            for sale_total, supplier, pieces in items:
                n += 1
                sale_id = _sale(conn, day, point, "Заказ №%d" % order, "order", sale_total, supplier, pieces, n)
                if reverse:
                    sales.reverse_sale(conn, sale_id, NOW, "o", from_order=True)
    for manual_list, reverse in ((MANUAL, False), (REVERSED_MANUAL, True)):
        for point, day, sale_total, supplier, pieces in manual_list:
            n += 1
            sale_id = _sale(conn, day, point, "вручную", "manual", sale_total, supplier, pieces, n)
            if reverse:
                sales.reverse_sale(conn, sale_id, NOW, "o")


PERIODS = [("2026-08-01", "2026-10-31", ""), ("2026-08-01", "2026-08-31", ""), ("2026-09-01", "2026-09-30", ""),
           ("2026-10-01", "2026-10-31", ""), ("2026-08-01", "2026-10-31", "Альфа"), ("2026-09-15", "2026-10-10", ""),
           ("2026-07-01", "2026-07-31", "")]
