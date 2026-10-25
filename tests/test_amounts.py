import pytest

from tires_sync.amounts import parse_amount


@pytest.mark.parametrize("text, want", [
    ("19240", 19240.0), ("19 240", 19240.0), ("19 240,50", 19240.5), ("19240.5 ₽", 19240.5), ("19 240,5", 19240.5),
    ("1.500", 1500.0), ("1,500", 1500.0), ("19,240.50", 19240.5), ("19.240,50", 19240.5), ("1.234.567", 1234567.0),
    ("0.125", 0.125), ("0,5", 0.5), ("9150", 9150.0), ("12.50", 12.5), ("-100", -100.0), ("  7 руб.", 7.0),
])
def test_amounts_are_read_the_way_a_person_means_them(text, want):
    assert parse_amount(text) == want


@pytest.mark.parametrize("bad", ["", "abc", "12a", "1.2.3.4x", "--5", None, "1..5"])
def test_what_is_not_a_number_is_refused(bad):
    with pytest.raises(ValueError):
        parse_amount(bad)


def test_safe_path_refuses_control_characters_that_browsers_strip():
    from tires_sync.panel.common import safe_path
    for bad in ("/\t/evil.example", "/\x0b/evil.example", "/ok\x00", "/ok\r\nSet-Cookie: x=1", "//evil.example", "/\\evil"):
        assert safe_path(bad) == ""
    assert safe_path("/economics/sales?p=7") == "/economics/sales?p=7"


def test_a_sale_written_from_an_order_is_reversed_only_through_the_order(tmp_path):
    from tires_sync import sales, store
    conn = store.connect(tmp_path / "t.db")
    sale_id = sales.add_sale(conn, day="2026-10-14", goods_id="1", brand_key="IKON TYRES", season="winter", diameter=16, name="n", ad_kind="single",
                             pieces=1, sale_total=100.0, supplier_price=50.0, terms=None, fee_pct=0.0, promo_pct=0.0, account_id=None, point="",
                             note="Заказ №5", now="2026-10-14T10:00:00", author="o", today="2026-10-14", source="order")
    with pytest.raises(ValueError, match="через заказ"):
        sales.reverse_sale(conn, sale_id, "2026-10-14T11:00:00", "o")
    sales.reverse_sale(conn, sale_id, "2026-10-14T11:00:00", "o", from_order=True)                    # the order itself may
    assert conn.execute("SELECT reversed_at FROM sale").fetchone()[0] is not None
