"""The client's own name from the Avito chat (the owner 2026-10-27): read from the client's words only, beats the profile nick,
never overwrites a name a manager typed, journal line «Имя из чата: …»."""
from __future__ import annotations

import pytest

from tires_sync import chat_deals as deals, chat_name, chats, orders

from test_chat_deals import NOW, Fake, setup, sync

ASK = ("out", "Здравствуйте! Как к вам обращаться?")


def name(*messages):
    return chat_name.name_from_messages(list(messages))


@pytest.mark.parametrize("text,expected", [
    ("Меня зовут Иван", "Иван"), ("меня зовут иван, нужны шины 185/65 R15", "Иван"), ("Здравствуйте! Меня зовут Мария Петрова.", "Мария Петрова"),
    ("Добрый день, меня зовут Анна-Мария, актуально?", "Анна-Мария"), ("Моё имя Сергей", "Сергей"), ("мое имя - Олег", "Олег"),
    ("Имя: Дмитрий", "Дмитрий"), ("имя - Ольга, телефон ниже", "Ольга"), ("ФИО: Иванов Иван Иванович", "Иванов Иван Иванович"),
    ("фио Петров Пётр", "Петров Пётр"), ("Ф.И.О.: Сидорова Анна", "Сидорова Анна"), ("Меня зовут Алексей и мне нужны шины", "Алексей"),
    ("МЕНЯ ЗОВУТ ИГОРЬ", "Игорь"), ("меня зовут John Smith", "John Smith"), ("Меня зовут Янина", "Янина"),
    ("Иван 8 900 123 45 67", "Иван"), ("Иван Петров, +7 921 000 00 00", "Иван Петров"), ("+7 (921) 000-00-00 Наталья", "Наталья"),
    ("Андрей, 89001234567", "Андрей"), ("Телефон 8 900 123 45 67 Виктор", "Виктор"), ("Елена Смирнова 89001234567", "Елена Смирнова"),
])
def test_a_name_the_client_states_is_found(text, expected):
    assert name(("in", text)) == expected


@pytest.mark.parametrize("answer,expected", [
    ("Это Павел", "Павел"), ("это павел", "Павел"), ("Я Ольга", "Ольга"), ("Игорь", "Игорь"), ("Игорь Сидоров", "Игорь Сидоров"), ("Зовут меня Артём", "Артём"),
])
def test_an_answer_right_after_our_question_about_the_name_is_a_name(answer, expected):
    assert name(ASK, ("in", answer)) == expected


@pytest.mark.parametrize("question", [
    "Как вас зовут?", "Подскажите, пожалуйста, ваше имя", "Представьтесь, пожалуйста", "Как к Вам обращаться?", "Назовите имя и телефон для заказа"])
def test_our_question_may_be_worded_in_several_ways(question):
    assert name(("out", question), ("in", "Это Павел")) == "Павел"


@pytest.mark.parametrize("text", [
    "Здравствуйте", "Здравствуйте, актуально?", "Добрый день", "Спасибо", "Спасибо большое", "Да", "Нет", "Добрый вечер, есть в наличии?", "Привет",
    "Это актуально?", "Это новая резина?", "Это ваша Michelin?", "Сколько стоит", "Заберу сегодня", "Москва", "Санкт-Петербург", "Юнона", "Руставели",
    "Ikon 185/65 R15", "Nokian Hakkapeliitta", "89001234567", "Звоните 8 900 123 45 67", "Тел. 8 900 123 45 67", "Мой номер 8 900 123 45 67",
    "Шины 8 900 123 45 67", "Зимняя резина, 8 900 123 45 67", "Иван", "иван 89001234567", "иван петров 89001234567", "Привет Иван 89001234567",
    "Меня зовут", "меня зовут 5 раз", "имя: ", "Имя: 12345", "ФИО: -", "Меня зовут Здравствуйте", "я на Юноне", "Я с Янино", "Это Иван ждёт", "",
])
def test_greetings_shop_words_brands_places_numbers_and_chatter_are_not_names(text):
    assert name(("in", text)) == ""


@pytest.mark.parametrize("answer", ["Да", "Нет", "Не знаю", "Спасибо", "Здравствуйте", "Москва", "Зимние шины нужны", "89001234567", "Иван 5", "А зачем вам?", "Michelin"])
def test_the_answer_to_our_question_must_still_be_a_name(answer):
    assert name(ASK, ("in", answer)) == ""


def test_a_bare_word_without_our_question_or_a_phone_is_not_a_name():
    assert name(("in", "Игорь")) == "" and name(("out", "Игорь"), ("in", "Спасибо")) == ""


def test_only_the_clients_messages_are_read_never_ours():
    assert name(("out", "Меня зовут Менеджер Иван"), ("in", "Здравствуйте")) == ""
    assert name(("out", "Это Павел, магазин Tires"), ("in", "Добрый день")) == ""
    assert name(("out", "Имя: Пётр, 8 900 123 45 67")) == ""
    assert name(("in", "Это Павел")) == ""                                   # nothing was asked


def test_the_latest_statement_wins():
    assert name(("in", "Меня зовут Иван"), ("in", "Нет, меня зовут Пётр")) == "Пётр"
    assert name(("in", "Меня зовут Иван"), ("in", "Спасибо")) == "Иван"


def test_a_name_is_capitalised_and_at_most_sixty_characters():
    assert name(("in", "меня зовут ИВАН ПЕТРОВ")) == "Иван Петров"
    assert name(("in", "Меня зовут " + "Ы" * 61)) == ""
    assert len(name(("in", "ФИО: " + "Аб" * 10 + " " + "Вг" * 10 + " " + "Де" * 10))) <= 60


# ---- the deal ------------------------------------------------------------------------------------------------------------------------

def deal_of(conn):
    [chat] = chats.list_chats(conn)
    return chat, orders.get_card(conn, chat["order_id"])


def event_texts(conn, card_id):
    return [e["text"] for e in orders.list_events(conn, card_id)]


def make(conn, account, fake, chat_id="c1", nick="DM", texts=None):
    fake.add(chat_id, nick, texts or [("in", "Здравствуйте, меня зовут Иван Петров, +7 900 000 00 17")])
    chats.sync_account(conn, fake, account, NOW)
    [chat] = [c for c in chats.list_chats(conn) if c["chat_id"] == chat_id]
    return deals.create_deal(conn, chat["id"], NOW, "менеджер")


def test_the_deal_takes_the_name_the_client_wrote_instead_of_the_nick(tmp_path):
    conn, account, fake = setup(tmp_path)
    card_id = make(conn, account, fake)
    card = orders.get_card(conn, card_id)
    assert card["client_name"] == "Иван Петров" and card["client_phone"] == "+79000000017"
    assert "Имя из чата: Иван Петров" in event_texts(conn, card_id)


def test_without_a_stated_name_the_profile_name_stays_and_no_line_is_written(tmp_path):
    conn, account, fake = setup(tmp_path)
    card_id = make(conn, account, fake, nick="Татьяна", texts=[("in", "Здравствуйте, это актуально? 8 900 000 00 17")])
    assert orders.get_card(conn, card_id)["client_name"] == "Татьяна"
    assert not any(t.startswith("Имя из чата") for t in event_texts(conn, card_id))


def test_a_name_that_is_the_profile_name_writes_no_line(tmp_path):
    conn, account, fake = setup(tmp_path)
    card_id = make(conn, account, fake, nick="Иван", texts=[("in", "Меня зовут Иван")])
    assert orders.get_card(conn, card_id)["client_name"] == "Иван"
    assert not any(t.startswith("Имя из чата") for t in event_texts(conn, card_id))


def test_a_name_from_our_question_and_the_clients_answer(tmp_path):
    conn, account, fake = setup(tmp_path)
    card_id = make(conn, account, fake, texts=[("in", "Добрый день"), ("out", "Как к вам обращаться?"), ("in", "Это Павел")])
    assert orders.get_card(conn, card_id)["client_name"] == "Павел"


def test_a_name_in_our_own_message_is_never_taken(tmp_path):
    conn, account, fake = setup(tmp_path)
    card_id = make(conn, account, fake, nick="DM", texts=[("in", "Здравствуйте"), ("out", "Меня зовут Менеджер Сергей, чем помочь?")])
    assert orders.get_card(conn, card_id)["client_name"] == "DM"


def test_the_name_told_later_fills_a_deal_that_has_the_profile_name_once(tmp_path):
    conn, account, fake = setup(tmp_path)
    card_id = make(conn, account, fake, texts=[("in", "Здравствуйте, актуально?")])
    assert orders.get_card(conn, card_id)["client_name"] == "DM"
    fake.add("c1", "DM", [("in", "Здравствуйте, актуально?"), ("out", "Да. Как вас зовут?"), ("in", "Сергей")])
    chats.sync_account(conn, fake, account, NOW)
    assert deals.process_all(conn, NOW)["names"] == 1
    assert orders.get_card(conn, card_id)["client_name"] == "Сергей"
    assert event_texts(conn, card_id).count("Имя из чата: Сергей") == 1
    assert deals.process_all(conn, NOW)["names"] == 0
    assert event_texts(conn, card_id).count("Имя из чата: Сергей") == 1


def test_a_name_typed_by_a_manager_is_never_overwritten(tmp_path):
    conn, account, fake = setup(tmp_path)
    card_id = make(conn, account, fake, texts=[("in", "Здравствуйте, актуально?")])
    orders.update_card(conn, card_id, {"client_name": "Иванов Сергей Петрович"}, NOW, "менеджер")
    fake.add("c1", "DM", [("in", "Здравствуйте, актуально?"), ("out", "Как вас зовут?"), ("in", "Сергей")])
    chats.sync_account(conn, fake, account, NOW)
    assert deals.process_all(conn, NOW)["names"] == 0
    assert orders.get_card(conn, card_id)["client_name"] == "Иванов Сергей Петрович"
    assert not any(t.startswith("Имя из чата") for t in event_texts(conn, card_id))


def test_a_manager_name_set_over_the_chat_name_stays_too(tmp_path):
    conn, account, fake = setup(tmp_path)
    card_id = make(conn, account, fake)
    orders.update_card(conn, card_id, {"client_name": "Иван П. (Юнона)"}, NOW, "менеджер")
    assert deals.process_all(conn, NOW)["names"] == 0
    assert orders.get_card(conn, card_id)["client_name"] == "Иван П. (Юнона)"


def test_an_empty_name_field_is_filled(tmp_path):
    conn, account, fake = setup(tmp_path)
    card_id = make(conn, account, fake, nick="", texts=[("in", "Здравствуйте, актуально?")])
    fake.add("c1", "", [("in", "Здравствуйте, актуально?"), ("in", "Меня зовут Олег")])
    chats.sync_account(conn, fake, account, NOW)
    assert deals.process_all(conn, NOW)["names"] == 1
    assert orders.get_card(conn, card_id)["client_name"] == "Олег"


def test_a_closed_deal_is_left_alone(tmp_path):
    conn, account, fake = setup(tmp_path)
    card_id = make(conn, account, fake, texts=[("in", "Здравствуйте, актуально?")])
    closed = conn.execute("SELECT id FROM order_stage WHERE kind != 'open' LIMIT 1").fetchone()[0]
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (closed, card_id))
    conn.commit()
    fake.add("c1", "DM", [("in", "Здравствуйте, актуально?"), ("in", "Меня зовут Олег")])
    chats.sync_account(conn, fake, account, NOW)
    assert deals.process_all(conn, NOW)["names"] == 0 and orders.get_card(conn, card_id)["client_name"] == "DM"


def test_a_chat_joining_the_open_order_of_the_same_phone_fills_a_profile_name_only(tmp_path):
    conn, account, fake = setup(tmp_path)
    first = make(conn, account, fake, chat_id="c1", nick="DM", texts=[("in", "Здравствуйте, 8 900 000 00 17")])
    assert orders.get_card(conn, first)["client_name"] == "DM"
    second = make(conn, account, fake, chat_id="c2", nick="DM", texts=[("in", "Это опять я, меня зовут Иван, 8 900 000 00 17")])
    assert second == first and orders.get_card(conn, first)["client_name"] == "Иван"
    orders.update_card(conn, first, {"client_name": "Иван Иванович"}, NOW, "менеджер")
    third = make(conn, account, fake, chat_id="c3", nick="DM", texts=[("in", "Меня зовут Пётр, 8 900 000 00 17")])
    assert third == first and orders.get_card(conn, first)["client_name"] == "Иван Иванович"


def test_the_handover_to_a_point_takes_the_name_the_client_wrote(tmp_path):
    import secrets
    from tires_sync import order_manager as mgr, order_points as points
    conn, account, fake = setup(tmp_path)
    fake.add("c1", "DM", [("in", "Здравствуйте, меня зовут Иван Петров")])
    chats.sync_account(conn, fake, account, NOW)
    [chat] = chats.list_chats(conn)
    pid = points.save_point(conn, {"name": "Бета", "manager_name": "А", "channel": "vk"})
    conn.execute("UPDATE point SET avito_account_id = ? WHERE id = ?", (chat["account_id"], pid))
    conn.commit()
    order, _ = mgr.hand_over(conn, pid, {"kind": "chat", "chat_row": chat["id"]}, "89001112233", mgr.handoff_fields({}), secrets.token_hex(12), "point1", NOW)
    assert orders.get_card(conn, order)["client_name"] == "Иван Петров"
