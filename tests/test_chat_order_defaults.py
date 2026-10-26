"""«Создать заказ» from a chat or a call (the owner 2026-10-26): the point (the chat's account, or the point the client named in their own
messages), the responsible (the point's manager for a point's account) and the delivery date (the next day) come filled in."""
from __future__ import annotations

import pytest

from tires_sync import calls, chat_deals as deals, chats, connections, order_points as points, orders, store, users

from test_calls import FakeClient, call
from test_chat_deals import Fake

NOW = "2026-10-26T15:00:00"

YUNONA = {"id": 1, "name": "Юнона", "address": "Санкт-Петербург, ул. Маршала Казакова, д. 35"}
RUSTAVELI = {"id": 2, "name": "Руставели", "address": "Санкт-Петербург, ул. Руставели, 66"}
YANINO = {"id": 3, "name": "Янино", "address": "Ленинградская обл., Всеволожский р-н, д. Янино-1, Голландская ул., 8"}
POINTS = [YUNONA, RUSTAVELI, YANINO]


def named(*texts_old_to_new):
    """The point named in the client's messages; the texts as they were written (the oldest first)."""
    found = deals.named_point(list(reversed(texts_old_to_new)), POINTS)
    return found[0]["name"] if found else None


# ---- the matcher ---------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Заберу на Юноне", "Юнона"), ("а можно забрать с юноны?", "Юнона"), ("Юнона", "Юнона"), ("ЮНОНУ знаю, удобно", "Юнона"),
    ("Удобнее на Руставели", "Руставели"), ("ул руставели 66 подойдёт", "Руставели"), ("Руставели", "Руставели"),
    ("Я в Янино живу, заберу там", "Янино"), ("Можно в Янино-1?", "Янино"), ("Заберу на Голландской", "Янино"),
    ("Ближе всего Маршала Казакова", "Юнона"), ("на Казакова заберу", "Юнона"), ("На казаковой ок", "Юнона"),
    ("Заберу на Маршала Жукова", None), ("Казаков Иван меня зовут", None), ("Меня зовут Янина", None),
    ("Руставелли", None), ("Юн", None), ("Петербург, когда можно забрать?", None), ("улица, дом 35", None),
    ("Здравствуйте, актуально?", None), ("", None), ("Дом 66, корпус 8", None),
])
def test_a_point_is_found_by_its_name_or_its_street_in_russian_word_forms(text, expected):
    assert named(text) == expected


@pytest.mark.parametrize("text", [
    "Руставели не подходит, далеко", "Юнона далеко", "Юнона нет", "Я живу на Руставели", "Мы живем в Янино", "я из Янино",
    "Казакова Мария", "Мария Казакова, телефон ниже", "Есть европейской резины?", "Голландская резина есть?",
])
def test_a_point_named_but_not_chosen_and_a_street_word_out_of_place_name_no_point(text):
    assert named(text) is None


def test_a_refusal_after_the_offer_is_never_a_choice():
    assert named("Юнона или Руставели?", "Руставели не подходит") is None
    assert named("Руставели не подходит, заберу на Юноне") == "Юнона"
    assert named("На Руставели, а не на Юноне") == "Руставели"


def test_a_street_of_another_shape_counts_after_a_preposition_or_a_street_word():
    euro = [{"id": 7, "name": "Север", "address": "Кудрово, Европейский пр., 14"}]
    assert deals.named_point(["Заберу на Европейском"], euro)[0]["id"] == 7
    assert deals.named_point(["Европейский или азиатский протектор?"], euro) is None


def test_the_last_mention_wins_and_a_message_without_a_point_never_clears_it():
    assert named("Хотел на Юноне", "Нет, лучше на Руставели") == "Руставели"
    assert named("Заберу на Руставели", "Спасибо, во сколько можно?") == "Руставели"
    assert named("Спасибо", "Здравствуйте") is None


@pytest.mark.parametrize("text,expected", [
    ("Юнона или Руставели, где ближе?", None), ("Есть на Юноне и на Руставели?", None),
    ("Не на Юноне, а на Руставели", "Руставели"), ("На Руставели, а не на Юноне", "Руставели"),
    ("Хотел на Юноне, но лучше на Руставели", "Руставели"), ("Был на Юноне, передумал, заберу в Янино", "Янино"),
    ("Мне не на Юноне", None), ("Юнона, точнее Руставели", "Руставели"),
])
def test_two_points_in_one_message_are_taken_only_when_the_choice_is_clear(text, expected):
    assert named(text) == expected


def test_an_unclear_last_message_never_falls_back_to_an_older_one():
    assert named("Заберу на Юноне", "Хм, Юнона или Руставели?") is None
    assert named("Заберу на Юноне", "Ой, нет, не на Юноне") is None


def test_a_word_two_points_share_names_no_point():
    both = [{"id": 1, "name": "Альфа", "address": "Кудрово, ул. Европейская, 1"}, {"id": 2, "name": "Бета", "address": "Кудрово, ул. Ленинградская, 5"}]
    assert deals.named_point(["Заберу в Кудрово"], both) is None
    assert deals.named_point(["Заберу на Европейской"], both)[0]["id"] == 1


def test_the_found_text_is_returned_for_the_journal():
    point, text = deals.named_point(["Спасибо", "Заберу на Юноне"], POINTS)
    assert point["id"] == 1 and text == "Заберу на Юноне"


def test_next_day_crosses_the_month_and_the_year():
    assert deals.next_day("2026-10-26T15:00:00") == "2026-10-27"
    assert deals.next_day("2026-10-31T23:59:59") == "2026-11-01"
    assert deals.next_day("2026-12-31T00:00:01") == "2027-01-01"
    assert deals.next_day("2028-02-28T10:00:00") == "2028-02-29"


# ---- create_deal ---------------------------------------------------------------------------------------------------------------------

def setup(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    main = connections.create_account(conn, "Павел", NOW, "o")
    of_point = connections.create_account(conn, "Юнона-аккаунт", NOW, "o")
    other = connections.create_account(conn, "Руставели-аккаунт", NOW, "o")
    yunona = points.save_point(conn, {"name": "Юнона", "manager_name": "А", "channel": "panel", "avito_account_id": of_point,
                                      "address": "Санкт-Петербург, ул. Маршала Казакова, д. 35", "default_time": "с 12 до 20"})
    rustaveli = points.save_point(conn, {"name": "Руставели", "manager_name": "Б", "channel": "panel", "avito_account_id": other,
                                         "address": "Санкт-Петербург, ул. Руставели, 66"})
    test_point = points.save_point(conn, {"name": "Тестовая", "manager_name": "В", "channel": "panel"})     # no account
    return conn, {"main": main, "yunona": of_point, "rustaveli": other}, {"yunona": yunona, "rustaveli": rustaveli, "test": test_point}


def chat_of(conn, account, texts, chat_id="c1"):
    fake = Fake()
    fake.add(chat_id, "Татьяна", texts)
    chats.sync_account(conn, fake, account, NOW)
    return [c for c in chats.list_chats(conn) if c["chat_id"] == chat_id][0]["id"]


def journal(conn, card_id):
    return [e["text"] for e in orders.list_events(conn, card_id)]


def test_a_chat_of_a_points_account_gives_the_point_its_manager_the_next_day_and_the_points_time(tmp_path):
    conn, acc, pt = setup(tmp_path)
    users.add_user(conn, "yun", "pass-word-1", NOW, role="point", point_id=pt["yunona"])
    row = chat_of(conn, acc["yunona"], [("in", "Здравствуйте, актуально?")])
    card = orders.get_card(conn, deals.create_deal(conn, row, NOW, "pavel"))
    assert card["point_id"] == pt["yunona"] and card["responsible"] == "yun"
    assert card["delivery_date"] == "2026-10-27" and card["delivery_time"] == "с 12 до 20"
    assert any("по аккаунту Авито" in t and "Юнона" in t for t in journal(conn, card["id"]))


def test_the_point_the_client_named_wins_over_the_points_account(tmp_path):
    conn, acc, pt = setup(tmp_path)
    users.add_user(conn, "yun", "pass-word-1", NOW, role="point", point_id=pt["yunona"])
    row = chat_of(conn, acc["yunona"], [("in", "Здравствуйте"), ("out", "Забрать можно на Юноне или на Руставели"), ("in", "Мне на Руставели")])
    card = orders.get_card(conn, deals.create_deal(conn, row, NOW, "pavel"))
    assert card["point_id"] == pt["rustaveli"] and card["responsible"] == "pavel"         # Руставели has no manager: the default (none = who made it)
    assert "Точка из сообщения клиента: «Руставели» («Мне на Руставели»)" in journal(conn, card["id"])
    assert not any("по аккаунту Авито" in t for t in journal(conn, card["id"]))


def test_the_client_naming_the_accounts_own_point_is_the_binding_by_the_account(tmp_path):
    conn, acc, pt = setup(tmp_path)
    row = chat_of(conn, acc["yunona"], [("in", "Заберу у вас на Юноне")])
    card = orders.get_card(conn, deals.create_deal(conn, row, NOW, "pavel"))
    assert card["point_id"] == pt["yunona"] and card["delivery_time"] == "с 12 до 20"
    assert any("по аккаунту Авито" in t for t in journal(conn, card["id"]))
    assert not any(t.startswith("Точка из сообщения клиента") for t in journal(conn, card["id"]))


def test_the_manager_of_the_point_the_client_named_answers_on_a_points_account(tmp_path):
    conn, acc, pt = setup(tmp_path)
    users.add_user(conn, "yun", "pass-word-1", NOW, role="point", point_id=pt["yunona"])
    users.add_user(conn, "rus", "pass-word-1", NOW, role="point", point_id=pt["rustaveli"])
    row = chat_of(conn, acc["yunona"], [("in", "Мне на Руставели")])
    card = orders.get_card(conn, deals.create_deal(conn, row, NOW, "pavel"))
    assert card["point_id"] == pt["rustaveli"] and card["responsible"] == "rus"


def test_an_order_waiting_for_the_goods_gets_no_delivery_date(tmp_path):
    conn, acc, pt = setup(tmp_path)
    row = chat_of(conn, acc["main"], [("in", "Здравствуйте")])
    assert orders.get_card(conn, deals.create_deal(conn, row, NOW, "pavel", delivery=False))["delivery_date"] == ""
    other = chat_of(conn, acc["main"], [("in", "Жду Ikon 205/55 R16")], chat_id="c2")
    form = {"brand": "Ikon Tyres", "width": "205", "profile": "55", "radius": "16", "season": "winter", "qty": "4", "any_brand": "1"}
    card_id, _added, _moved = deals.wait_from_chat(conn, other, form, [], NOW, "pavel")       # «Клиент ждёт эту шину»
    assert orders.get_card(conn, card_id)["delivery_date"] == ""


def test_a_main_account_takes_the_point_from_the_clients_words_only(tmp_path):
    conn, acc, pt = setup(tmp_path)
    orders.set_default_responsible(conn, "worker2")
    row = chat_of(conn, acc["main"], [("in", "Заберу на Юноне")])
    card = orders.get_card(conn, deals.create_deal(conn, row, NOW, "pavel"))
    assert card["point_id"] == pt["yunona"] and card["responsible"] == "worker2" and card["delivery_date"] == "2026-10-27"
    assert card["delivery_time"] == "с 12 до 20"                                          # the point's usual time, as when a person chooses it
    assert any(t.startswith("Точка из сообщения клиента: «Юнона»") for t in journal(conn, card["id"]))


def test_our_own_messages_never_choose_the_point(tmp_path):
    conn, acc, pt = setup(tmp_path)
    row = chat_of(conn, acc["main"], [("in", "Здравствуйте, актуально?"), ("out", "Да. Забрать: Юнона, Руставели, Янино"),
                                      ("out", "Ближе всего вам будет на Юноне")])
    card = orders.get_card(conn, deals.create_deal(conn, row, NOW, "pavel"))
    assert card["point_id"] is None and card["delivery_date"] == "2026-10-27" and card["delivery_time"] == ""
    assert not any("Точка" in t for t in journal(conn, card["id"]))


def test_a_points_account_without_a_point_user_keeps_the_default_responsible(tmp_path):
    conn, acc, pt = setup(tmp_path)
    orders.set_default_responsible(conn, "worker2")
    users.add_user(conn, "other", "pass-word-1", NOW, role="point", point_id=pt["rustaveli"])     # another point's manager
    users.add_user(conn, "boss", "pass-word-1", NOW, role="owner")
    row = chat_of(conn, acc["yunona"], [("in", "Здравствуйте")])
    assert orders.get_card(conn, deals.create_deal(conn, row, NOW, "pavel"))["responsible"] == "worker2"


def test_the_first_active_manager_of_the_point_answers(tmp_path):
    conn, acc, pt = setup(tmp_path)
    users.add_user(conn, "zed", "pass-word-1", NOW, role="point", point_id=pt["yunona"])
    users.add_user(conn, "amy", "pass-word-1", NOW, role="point", point_id=pt["yunona"])
    users.set_active(conn, "zed", False, NOW, "boss")
    assert deals.point_responsible(conn, pt["yunona"]) == "amy"
    users.set_active(conn, "zed", True, NOW, "boss")
    assert deals.point_responsible(conn, pt["yunona"]) == "zed"                          # made first
    assert deals.point_responsible(conn, pt["test"]) == "" and deals.point_responsible(conn, None) == ""


def test_the_existing_card_of_a_chat_is_kept_as_before(tmp_path):
    conn, acc, pt = setup(tmp_path)
    row = chat_of(conn, acc["main"], [("in", "Мой телефон +7 900 000-00-17, заберу на Руставели")])
    card_id = deals.create_deal(conn, row, NOW, "pavel")
    card = orders.get_card(conn, card_id)
    assert card["client_name"] == "Татьяна" and card["client_phone"] == "+79000000017" and card["avito_chat"] == "Ч-%d" % row
    assert card["source"] == "avito" and "Объявление: Ikon 185/65 R15" in card["comment"] and "Павел" in card["tags"]
    assert deals.create_deal(conn, row, NOW, "pavel") == card_id                          # a second press gives the same order


# ---- «Создать заказ» from a call -----------------------------------------------------------------------------------------------------

def test_a_call_to_a_points_account_gives_its_manager_and_the_next_day(tmp_path):
    conn, acc, pt = setup(tmp_path)
    users.add_user(conn, "yun", "pass-word-1", NOW, role="point", point_id=pt["yunona"])
    calls.sync_account(conn, FakeClient([[call(1)]]), acc["yunona"], NOW, lambda s: None)
    card = orders.get_card(conn, calls.create_order(conn, calls.list_calls(conn)[0]["id"], "2026-12-31T22:00:00", "pavel"))
    assert card["point_id"] == pt["yunona"] and card["responsible"] == "yun" and card["delivery_date"] == "2027-01-01"


def test_a_call_to_a_main_account_keeps_the_default_responsible_and_gets_the_next_day(tmp_path):
    conn, acc, pt = setup(tmp_path)
    orders.set_default_responsible(conn, "worker2")
    calls.sync_account(conn, FakeClient([[call(1)]]), acc["main"], NOW, lambda s: None)
    card = orders.get_card(conn, calls.create_order(conn, calls.list_calls(conn)[0]["id"], NOW, "pavel"))
    assert card["point_id"] is None and card["responsible"] == "worker2" and card["delivery_date"] == "2026-10-27"
