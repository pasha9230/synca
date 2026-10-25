"""«Клиент ждёт эту шину» from a chat: brand, size, season, studs, quantity; the catalog gives season and studs when it knows the tire (owner, 2026-10-19)."""
from __future__ import annotations

import pytest

from helpers import make_offer
from test_chat_deals import NOW, setup
from tires_sync import chat_deals as deals, chats, order_wait, orders
from tires_sync.models import Season

IKON = make_offer(goods_id="1", brand_key="IKON TYRES", model_raw="Character Ice", width=205, height=55, diameter=16, season=Season.WINTER, studded=False, stock=0)
GONE = make_offer(goods_id="2", brand_key="KUMHO", model_raw="WinterCraft", width=185, height=65, diameter=15, season=Season.WINTER, studded=True)


def chat_row(tmp_path):
    conn, account, fake = setup(tmp_path)
    fake.add("c1", "Татьяна", [("in", "Здравствуйте, есть такие шины?")])
    chats.sync_account(conn, fake, account, NOW)
    return conn, chats.list_chats(conn)[0]["id"]


def test_the_catalog_gives_season_and_studs_to_a_tire_it_knows_and_the_manager_gives_them_to_one_it_does_not():
    line = order_wait.resolve_line([IKON, GONE], {"brand": "Ikon Tyres", "width": "205", "profile": "55", "radius": "16", "qty": "4"})
    assert line["season"] == "winter" and line["studded"] == "no"
    line = order_wait.resolve_line([IKON, GONE], {"width": "185", "profile": "65", "radius": "15"})                 # no brand: the size alone is enough
    assert line["season"] == "winter" and line["studded"] == "yes"
    with pytest.raises(orders.OrderError, match="Выберите сезон"):                                                    # a size the catalog does not have
        order_wait.resolve_line([IKON], {"brand": "Nokian", "width": "225", "profile": "45", "radius": "17"})
    line = order_wait.resolve_line([IKON], {"brand": "Nokian", "width": "225", "profile": "45", "radius": "17", "season": "summer"})
    assert line["season"] == "summer" and line["studded"] == ""


def test_the_choice_of_the_manager_stays_and_an_unclear_catalog_asks_for_the_season():
    summer = make_offer(goods_id="3", brand_key="IKON TYRES", width=205, height=55, diameter=16, season=Season.SUMMER)
    with pytest.raises(orders.OrderError, match="в нескольких сезонах"):
        order_wait.resolve_line([IKON, summer], {"brand": "Ikon Tyres", "width": "205", "profile": "55", "radius": "16"})
    line = order_wait.resolve_line([IKON, summer], {"brand": "Ikon Tyres", "width": "205", "profile": "55", "radius": "16", "season": "summer"})
    assert line["season"] == "summer"
    line = order_wait.resolve_line([IKON], {"brand": "Ikon Tyres", "width": "205", "profile": "55", "radius": "16", "season": "winter", "studded": "yes"})
    assert line["studded"] == "yes"                                                                                  # what the manager chose is not overwritten


def test_the_title_of_an_ad_gives_size_brand_season_and_studs_and_nothing_more():
    brands = ["IKON TYRES", "KUMHO"]
    assert order_wait.line_from_title("Ikon Tyres 185 55 R15 T 86 Character Ice 5 XL в наличии", brands) == {
        "qty": 4, "width": 185, "profile": 55, "radius": 15, "brand": "IKON TYRES"}
    got = order_wait.line_from_title("Зимние шины Kumho 205/65R16 шипованные", brands)
    assert (got["width"], got["profile"], got["radius"], got["brand"], got["season"], got["studded"]) == (205, 65, 16, "KUMHO", "winter", "yes")
    assert order_wait.line_from_title("Летняя резина 225/45 R17 нешипованная", brands)["studded"] == "no"
    assert order_wait.line_from_title("Шины без размера", brands) == {"qty": 4}


def test_the_button_makes_the_deal_the_line_and_the_stage_in_one_go(tmp_path):
    conn, row = chat_row(tmp_path)
    card_id, _added, _moved = deals.wait_from_chat(conn, row, {"brand": "Ikon Tyres", "width": "205", "profile": "55", "radius": "16", "qty": "4"}, [IKON], NOW, "pavel")
    card = orders.get_card(conn, card_id)
    assert card["stage"]["code"] == "wait" and chats.get_chat(conn, row)["order_id"] == card_id
    [want] = order_wait.list_wants(conn, card_id)
    assert (want["brand"], want["width"], want["season"], want["studded"], want["qty"], want["any_size"], want["any_brand"]) == ("Ikon Tyres", 205, "winter", "no", 4, False, False)
    assert card["items"] == []                                                                                         # nothing is ordered: the tire is waited for


def test_the_quantity_runs_from_one_to_a_hundred_and_the_analog_ticks_are_off_unless_set(tmp_path):
    conn, row = chat_row(tmp_path)
    base = {"brand": "Ikon Tyres", "width": "205", "profile": "55", "radius": "16", "season": "winter"}
    for bad in ("0", "101", "x", "-1", ""):
        with pytest.raises(orders.OrderError):
            deals.wait_from_chat(conn, row, dict(base, qty=bad), [], NOW, "pavel")
    assert chats.get_chat(conn, row)["order_id"] is None                                                               # a bad form made nothing
    card_id, _added, _moved = deals.wait_from_chat(conn, row, dict(base, qty="12", any_size="1"), [], NOW, "pavel")
    [want] = order_wait.list_wants(conn, card_id)
    assert want["qty"] == 12 and want["any_size"] is True and want["any_brand"] is False


def test_pressed_twice_the_line_is_added_once_and_a_second_line_joins_the_same_deal(tmp_path):
    conn, row = chat_row(tmp_path)
    form = {"brand": "Ikon Tyres", "width": "205", "profile": "55", "radius": "16", "season": "winter", "qty": "4"}
    first, added, moved = deals.wait_from_chat(conn, row, form, [], NOW, "pavel")
    assert (added, moved) == (True, True)
    assert deals.wait_from_chat(conn, row, form, [], NOW, "pavel") == (first, False, False) and len(order_wait.list_wants(conn, first)) == 1   # nothing new, nothing moved
    other = dict(form, width="215", profile="60", radius="17")
    assert deals.wait_from_chat(conn, row, other, [], NOW, "pavel") == (first, True, False) and len(order_wait.list_wants(conn, first)) == 2
    assert conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 1


def test_a_closed_deal_is_not_reopened_by_the_button(tmp_path):
    conn, row = chat_row(tmp_path)
    form = {"brand": "Ikon Tyres", "width": "205", "profile": "55", "radius": "16", "season": "winter", "qty": "4"}
    card_id, _added, _moved = deals.wait_from_chat(conn, row, form, [], NOW, "pavel")
    done = next(s for s in orders.list_stages(conn) if s["code"] == "cancel")
    orders.move_card(conn, card_id, done["id"], NOW, "pavel", changes={"cancel_reason": "передумал"})
    with pytest.raises(orders.OrderError, match="Заказ по этому чату закрыт"):          # «заказ», not «сделка» (owner's word, 2026-10-21)
        deals.wait_from_chat(conn, row, dict(form, width="215"), [], NOW, "pavel")


FORM = {"brand": "Ikon Tyres", "width": "205", "profile": "55", "radius": "16", "season": "winter", "qty": "4"}


def test_a_season_the_supplier_did_not_name_is_never_taken_from_the_catalog_and_never_breaks_the_form():
    unknown = make_offer(goods_id="5", brand_key="IKON TYRES", width=205, height=55, diameter=16, season=Season.UNKNOWN)
    assert order_wait.line_from_offer(unknown)["season"] == ""                                                         # the form asks the manager
    with pytest.raises(orders.OrderError, match="Выберите сезон"):
        order_wait.resolve_line([unknown], {"width": "205", "profile": "55", "radius": "16"})
    both = order_wait.resolve_line([unknown, IKON], {"width": "205", "profile": "55", "radius": "16"})                 # the known season wins, no KeyError
    assert both["season"] == "winter"


def test_a_deal_that_is_already_in_work_is_never_taken_back_by_the_button(tmp_path):
    conn, row = chat_row(tmp_path)
    card_id, _, _ = deals.wait_from_chat(conn, row, FORM, [], NOW, "pavel")
    for code in ("placed", "moving", "at_point", "oth"):
        stage = next(s for s in orders.list_stages(conn) if s["code"] == code)
        conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage["id"], card_id))
        conn.commit()
        with pytest.raises(orders.OrderError, match="уже в работе"):
            deals.wait_from_chat(conn, row, dict(FORM, width="215"), [], NOW, "pavel")
        assert orders.get_card(conn, card_id)["stage"]["code"] == code and len(order_wait.list_wants(conn, card_id)) == 1


def test_a_refused_move_leaves_nothing_half_done(tmp_path):
    conn, row = chat_row(tmp_path)
    conn.execute("UPDATE avito_chat SET peer_name = '', item_title = '' WHERE id = ?", (row,))
    conn.commit()
    with pytest.raises(orders.OrderError, match="нет имени клиента"):
        deals.wait_from_chat(conn, row, FORM, [], NOW, "pavel")
    assert conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 0 and chats.get_chat(conn, row)["order_id"] is None
    conn.execute("UPDATE avito_chat SET peer_name = 'Татьяна' WHERE id = ?", (row,))
    conn.commit()
    card_id, _, _ = deals.wait_from_chat(conn, row, FORM, [], NOW, "pavel")
    conn.execute("UPDATE order_card SET sale_ids = '[7]' WHERE id = ?", (card_id,))
    conn.commit()
    with pytest.raises(orders.OrderError, match="продажа"):
        deals.wait_from_chat(conn, row, dict(FORM, width="215"), [], NOW, "pavel")
    assert len(order_wait.list_wants(conn, card_id)) == 1


def test_the_brand_the_manager_types_is_matched_with_the_catalog_name_and_the_model_and_the_season_of_the_size_count():
    nokian = make_offer(goods_id="9", brand_key="NOKIAN TYRES", model_raw="Hakka", width=225, height=45, diameter=17, season=Season.WINTER, studded=True)
    other = make_offer(goods_id="10", brand_key="NOKIAN TYRES", model_raw="Wetproof", width=225, height=45, diameter=17, season=Season.SUMMER)
    got = order_wait.resolve_line([nokian, other], {"brand": "Nokian", "model": "Hakka", "width": "225", "profile": "45", "radius": "17"})
    assert got["brand"] == "NOKIAN TYRES" and got["season"] == "winter" and got["studded"] == "yes"                      # the model chose the season
    second = make_offer(goods_id="11", brand_key="S2:NOKIAN TYRES", model_raw="Hakka", width=225, height=45, diameter=17, season=Season.WINTER, studded=False)
    got = order_wait.resolve_line([second], {"brand": "Nokian Tyres", "width": "225", "profile": "45", "radius": "17"})   # the prefix of the second supplier is not part of the brand
    assert got["season"] == "winter" and got["studded"] == "no"
    summer = order_wait.resolve_line([nokian, other], {"brand": "Nokian", "width": "225", "profile": "45", "radius": "17", "season": "summer"})
    assert summer["studded"] == "no"                                                                                    # the studs of THAT season, not of the other


def test_the_title_reads_the_cyrillic_r_and_a_brand_next_to_a_sign_and_ignores_the_numbers_that_are_not_a_size():
    brands = ["PIRELLI", "IKON TYRES"]
    got = order_wait.line_from_title("Pirelli, 205/55 Р16 91V", brands)
    assert (got["brand"], got["width"], got["profile"], got["radius"]) == ("PIRELLI", 205, 55, 16)
    assert "brand" not in order_wait.line_from_title("Шины Pirellix 205/55 R16", brands)                               # a part of another word is not a brand
    assert "brand" not in order_wait.line_from_title("Шины SuperPirelli 205/55 R16", brands)                           # nor at its end
    assert "width" not in order_wait.line_from_title("Артикул 1205 55 1650", brands) and "width" not in order_wait.line_from_title("Шины 91T 2020 года", brands)


def test_one_row_of_the_catalog_base_is_read_by_its_id(tmp_path):
    from tires_sync import catalog_archive
    from tires_sync.webmim import ParseResult
    conn, row = chat_row(tmp_path)
    catalog_archive.record(conn, ParseResult(offers={"2": GONE, "3": IKON}), "2026-10-10T10:00:00")
    catalog_archive.record(conn, ParseResult(offers={"3": IKON}), "2026-10-15T10:00:00")
    assert catalog_archive.gone_offer(conn, "2").goods_id == "2" and catalog_archive.gone_offer(conn, "3") is None and catalog_archive.gone_offer(conn, "x") is None
