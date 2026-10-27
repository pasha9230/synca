"""Binding Avito chats to deals: the phone from the client's words, the ad, duplicates, manual link and unlink."""
from __future__ import annotations

import pytest

from tires_sync import chat_deals as deals, chats, connections, orders, store

NOW = "2026-10-13T12:00:00"
TS = 1_791_900_000


class Fake:
    def __init__(self):
        self.items, self.messages = [], {}

    def get_self(self):
        return {"id": 1}

    def chats(self, user_id, limit=50, offset=0, retry=True):
        return {"chats": self.items, "meta": {"has_more": False}}

    def chat_messages(self, user_id, chat_id, limit=100, offset=0, retry=True):
        return self.messages.get(chat_id, [])

    def add(self, chat_id, name, texts, item=111, updated=TS):
        self.items = [i for i in self.items if i["id"] != chat_id] + [{
            "id": chat_id, "created": TS - 50, "updated": updated,
            "context": {"type": "item", "value": {"id": item, "title": "Ikon 185/65 R15", "url": "https://www.avito.ru/x_%d" % item}},
            "users": [{"id": 1, "name": "Я"}, {"id": 9, "name": name}],
            "last_message": {"id": "last-" + chat_id, "author_id": 9, "created": updated, "content": {"text": texts[-1][1]}, "type": "text", "direction": texts[-1][0], "read": 0}}]
        self.messages[chat_id] = [{"id": "%s-%d" % (chat_id, n), "author_id": 9, "created": TS + n, "content": {"text": t}, "type": "text", "direction": d, "isRead": False, "read": 0}
                                  for n, (d, t) in enumerate(texts)]


def setup(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    account = connections.create_account(conn, "Основной", NOW, "o")
    return conn, account, Fake()


def sync(conn, account, fake, now=NOW):
    chats.sync_account(conn, fake, account, now)
    return deals.process_all(conn, now, auto_create=True)


@pytest.mark.parametrize("text,expected", [
    ("Мой телефон +7 (900) 000-00-17", "+79000000017"), ("89000000017", "+79000000017"), ("звоните 900 000 00 17 после 18", "+79000000017"),
    ("+7-921-000-00-00.", "+79210000000"), ("8 812 123 45 67", "+78121234567"),
    ("заказ 5000100 на 4 шины", None), ("карта 2200 1234 5678 9012", None), ("артикул T743172, 4 шт, 23000 руб", None), ("", None),
    ("Заказ 9123456789", None), ("Артикул 912-345-67-89", None), ("цена 9 990 999 22 33", None), ("номер 9123456789", None)])
def test_a_phone_is_found_in_free_text_and_numbers_that_are_not_phones_are_left_alone(text, expected):
    assert deals.extract_phone(text) == expected


def test_a_first_message_makes_a_deal_with_the_chat_the_ad_and_the_name(tmp_path):
    conn, account, fake = setup(tmp_path)
    fake.add("c1", "Татьяна", [("in", "Здравствуйте, это актуально?")])
    stats = sync(conn, account, fake)
    assert stats["created"] == 1
    [chat] = chats.list_chats(conn)
    card = orders.get_card(conn, chat["order_id"])
    assert card["client_name"] == "Татьяна" and card["source"] == "avito" and card["stage"]["code"] == "new"
    assert "Объявление: Ikon 185/65 R15" in card["comment"] and card["avito_chat"] == "Ч-%d" % chat["id"]
    assert any("Привязан чат" in e["text"] for e in orders.list_events(conn, card["id"]))
    assert sync(conn, account, fake)["created"] == 0 and conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 1       # no second deal


def test_our_own_messages_alone_do_not_make_a_deal(tmp_path):
    conn, account, fake = setup(tmp_path)
    fake.add("c1", "Татьяна", [("out", "Добрый день!")])
    assert sync(conn, account, fake)["created"] == 0 and conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 0



def test_manual_link_unlink_and_merge_and_an_unlinked_chat_is_left_alone(tmp_path):
    conn, account, fake = setup(tmp_path)
    fake.add("c1", "Татьяна", [("in", "Здравствуйте")])
    sync(conn, account, fake)
    chat = chats.list_chats(conn)[0]
    auto = chat["order_id"]
    deals.unlink_chat(conn, chat["id"], NOW, "owner")
    row = chats.get_chat(conn, chat["id"])
    assert row["order_id"] is None and row["unlinked"] == 1 and orders.get_card(conn, auto)["avito_chat"] == ""
    assert sync(conn, account, fake, "2026-10-13T12:20:00")["created"] == 0 and chats.get_chat(conn, chat["id"])["order_id"] is None      # the automation keeps out
    mine = orders.create_card(conn, dict(client_name="Иван"), NOW, "o")
    deals.link_chat(conn, chat["id"], mine, NOW, "owner")
    assert chats.get_chat(conn, chat["id"])["order_id"] == mine and chats.get_chat(conn, chat["id"])["unlinked"] == 0
    assert orders.get_card(conn, mine)["avito_chat"] == "Ч-%d" % chat["id"]
    with pytest.raises(orders.OrderError):
        deals.link_chat(conn, chat["id"], 9999, NOW, "owner")
    other = orders.create_card(conn, dict(client_name="Пётр"), NOW, "o")
    deals.merge_chats(conn, mine, other, NOW, "owner")                                                  # the chats of one deal move to the other
    assert chats.get_chat(conn, chat["id"])["order_id"] == other
    assert deals.create_deal(conn, chat["id"], NOW, "owner") == other                                   # a chat that has a deal gets no second one
    deals.unlink_chat(conn, chat["id"], NOW, "owner")
    made = deals.create_deal(conn, chat["id"], NOW, "owner")                                           # a new deal for an unlinked chat on demand
    assert chats.get_chat(conn, chat["id"])["order_id"] == made and made not in (mine, other)
    assert conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 4


def test_a_number_of_our_own_is_never_the_clients(tmp_path):
    conn, account, fake = setup(tmp_path)
    conn.execute("INSERT INTO point (name, manager_phone) VALUES ('Бета', '+7 (900) 000-00-15')")
    conn.commit()
    fake.add("c1", "Татьяна", [("in", "Звонил вам на 8 900 000 00 15, не дозвонился")])
    sync(conn, account, fake)
    card = orders.get_card(conn, chats.list_chats(conn)[0]["order_id"])
    assert card["client_phone"] == ""                                                                  # the quoted number is the shop's


def test_old_chats_at_the_first_sync_make_no_deals_until_the_client_writes_again(tmp_path):
    conn, account, fake = setup(tmp_path)
    fake.add("c1", "Татьяна", [("in", "Давнее сообщение")])
    assert sync(conn, account, fake, "2026-10-14T09:00:00")["created"] == 0                            # the first read of the account: this is the past
    assert conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 0
    fake.add("c1", "Татьяна", [("in", "Давнее сообщение"), ("in", "А сейчас новое")], updated=TS + 3 * 86400)
    fake.messages["c1"][1]["created"] = TS + 3 * 86400
    assert sync(conn, account, fake, "2026-10-17T10:00:00")["created"] == 1                            # a fresh message after the first read



def test_create_deal_twice_gives_one_deal_and_a_deal_in_the_making_is_not_touched(tmp_path):
    conn, account, fake = setup(tmp_path)
    fake.add("c1", "Татьяна", [("out", "Добрый день")])
    sync(conn, account, fake)
    chat = chats.list_chats(conn)[0]
    first = deals.create_deal(conn, chat["id"], NOW, "owner")
    assert deals.create_deal(conn, chat["id"], NOW, "owner") == first and conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 1
    other_fake = Fake()
    other_fake.add("c9", "Иван", [("in", "Здравствуйте")], item=9)
    chats.sync_account(conn, other_fake, account, NOW)
    busy = next(c for c in chats.list_chats(conn) if c["chat_id"] == "c9")
    conn.execute("UPDATE avito_chat SET order_id = -1 WHERE id = ?", (busy["id"],))                      # another run is making its deal right now
    conn.commit()
    with pytest.raises(orders.OrderError):
        deals.create_deal(conn, busy["id"], NOW, "owner")
    assert deals.process_all(conn, NOW, auto_create=True)["created"] == 0 and chats.get_chat(conn, busy["id"])["order_id"] == -1



def test_a_claim_left_by_a_killed_process_is_repaired_and_unlink_survives_it(tmp_path):
    conn, account, fake = setup(tmp_path)
    fake.add("c1", "Татьяна", [("in", "Здравствуйте")])
    fake.add("c2", "Иван", [("in", "Добрый день")], item=2)
    chats.sync_account(conn, fake, account, NOW)
    one, two = sorted(chats.list_chats(conn), key=lambda c: c["chat_id"])
    orphan = orders.create_card(conn, dict(client_name="Татьяна", avito_chat=deals.ref(one["id"])), NOW, "o")     # the deal was made, the process died before the link
    conn.execute("UPDATE avito_chat SET order_id = -1 WHERE id IN (?, ?)", (one["id"], two["id"]))
    conn.commit()
    deals.process_all(conn, NOW, auto_create=True)                                                                     # the first look: maybe a live claim, left alone
    assert chats.get_chat(conn, one["id"])["order_id"] == -1
    deals.process_all(conn, "2026-10-13T12:05:00", auto_create=True)                                                   # still there five minutes later: repaired
    assert chats.get_chat(conn, one["id"])["order_id"] == orphan and chats.get_chat(conn, two["id"])["order_id"] not in (-1, orphan)       # freed: it gets its own deal as any chat
    conn.execute("UPDATE avito_chat SET order_id = -1 WHERE id = ?", (two["id"],))
    conn.commit()
    deals.unlink_chat(conn, two["id"], NOW, "owner")                                                  # no crash on a leftover claim
    assert chats.get_chat(conn, two["id"])["order_id"] is None and chats.get_chat(conn, two["id"])["unlinked"] == 1


def test_enabling_the_chats_again_starts_a_new_baseline(tmp_path):
    conn, account, fake = setup(tmp_path)
    fake.add("c1", "Татьяна", [("in", "Давно")])
    sync(conn, account, fake, "2026-10-13T12:00:00")
    assert store.get_state(conn, "chats_baseline_%d" % account) == "2026-10-13T12:00:00"
    connections.set_modes(conn, account, False, True, NOW, "o")
    connections.set_modes(conn, account, True, True, NOW, "o")                                        # off for a month, on again
    assert store.get_state(conn, "chats_baseline_%d" % account) is None


def test_a_deal_made_from_a_chat_carries_the_name_of_the_account_as_a_tag(tmp_path):
    conn, account, fake = setup(tmp_path)
    other = connections.create_account(conn, "Партнёр-2", NOW, "o")
    fake.add("c1", "Татьяна", [("in", "Здравствуйте")])
    sync(conn, account, fake)
    second = Fake()
    second.add("z1", "Иван", [("in", "Добрый день")], item=2)
    second.items[0]["users"][1]["id"] = 55                                                            # a different Avito user
    chats.sync_account(conn, second, other, NOW)
    deals.process_all(conn, NOW, auto_create=True)
    tags = {c["chat_id"]: orders.get_card(conn, c["order_id"])["tags"] for c in chats.list_chats(conn)}
    assert tags == {"c1": ["Основной"], "z1": ["Партнёр-2"]}


def test_a_second_ad_of_one_client_with_the_same_phone_joins_the_open_deal(tmp_path):
    """The owner 2026-10-27 (was: every ad its own deal even with the same phone): one open order per phone, the second chat joins it."""
    conn, account, fake = setup(tmp_path)
    fake.add("c1", "Валера", [("in", "Здравствуйте, 8 900 000 00 17")], item=1)
    fake.add("c2", "Валера", [("in", "А это второе объявление, тот же 8 900 000 00 17")], item=2)
    stats = sync(conn, account, fake)
    assert stats["created"] == 1 and stats["linked"] == 1
    cards = [orders.get_card(conn, c["order_id"]) for c in sorted(chats.list_chats(conn), key=lambda c: c["chat_id"])]
    assert cards[0]["id"] == cards[1]["id"] and cards[0]["client_phone"] == "+79000000017"
    assert conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 1


def test_the_same_client_on_a_new_ad_gets_the_repeat_tag_and_a_note_with_the_earlier_deals(tmp_path):
    conn, account, fake = setup(tmp_path)
    fake.add("c1", "Валера", [("in", "Здравствуйте")], item=1)
    sync(conn, account, fake)
    first = chats.list_chats(conn)[0]["order_id"]
    assert "повторный" not in orders.get_card(conn, first)["tags"]
    fake.add("c2", "Валера", [("in", "Теперь про другое объявление")], item=2, updated=TS + 60)
    sync(conn, account, fake, "2026-10-13T12:10:00")
    second = next(c for c in chats.list_chats(conn) if c["chat_id"] == "c2")["order_id"]
    card = orders.get_card(conn, second)
    assert "повторный" in card["tags"] and "Основной" in card["tags"]
    assert any("Ранее писал" in e["text"] and "№%d" % first in e["text"] for e in orders.list_events(conn, second))
    fake.add("c3", "Другой", [("in", "Привет")], item=3)
    fake.items[-1]["users"][1]["id"] = 77                                                              # another Avito user
    sync(conn, account, fake, "2026-10-13T12:20:00")
    third = next(c for c in chats.list_chats(conn) if c["chat_id"] == "c3")["order_id"]
    assert "повторный" not in orders.get_card(conn, third)["tags"]


def test_a_client_who_writes_again_to_a_closed_deal_stays_in_it_and_the_deal_notes_it_once(tmp_path):
    conn, account, fake = setup(tmp_path)
    fake.add("c1", "Татьяна", [("in", "Хочу шины")])
    sync(conn, account, fake)
    deal = chats.list_chats(conn)[0]["order_id"]
    orders.move_card(conn, deal, next(s["id"] for s in orders.list_stages(conn) if s["code"] == "cancel"), "2026-10-13T12:30:00", "o", {"cancel_reason": "передумал"})
    fake.add("c1", "Татьяна", [("in", "Хочу шины"), ("in", "Через месяц снова нужны")], updated=TS + 30 * 86400)
    fake.messages["c1"][1]["created"] = TS + 30 * 86400
    sync(conn, account, fake, "2026-11-12T12:00:00")
    sync(conn, account, fake, "2026-11-12T12:05:00")
    assert chats.list_chats(conn)[0]["order_id"] == deal and conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 1
    notes = [e["text"] for e in orders.list_events(conn, deal) if "написал после закрытия" in e["text"]]
    assert len(notes) == 1
    assert orders.get_card(conn, deal)["stage"]["code"] == "cancel"                                    # the deal is not reopened by itself


def test_a_personal_chat_without_an_ad_makes_one_deal_named_with_the_client_only(tmp_path):
    conn, account, fake = setup(tmp_path)
    fake.add("p1", "Сергей", [("in", "Добрый день, есть вопрос")])
    fake.items[0]["context"] = {"type": "user", "value": {"id": 9}}
    sync(conn, account, fake)
    sync(conn, account, fake, "2026-10-13T12:05:00")
    card = orders.get_card(conn, chats.list_chats(conn)[0]["order_id"])
    assert card["client_name"] == "Сергей" and conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 1


def test_the_ad_of_a_chat_is_traced_to_our_position_and_its_pack(tmp_path):
    conn, account, fake = setup(tmp_path)
    for avito_id, ad_id in ((111, "118649107660"), (222, "118649107660-x4"), (333, "118649107660_20260812122712"), (444, None)):
        conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, seen_at) VALUES (?,?,?,?,?)", (account, avito_id, ad_id, "t", NOW))
    conn.commit()
    assert deals.ad_goods(conn, account, "111") == ("118649107660", 1)
    assert deals.ad_goods(conn, account, "222") == ("118649107660", 4)                                 # the «за 4 шт.» twin
    assert deals.ad_goods(conn, account, "333") == ("118649107660", 1)                                 # a mirrored ad keeps the supplier code before «_»
    assert deals.ad_goods(conn, account, "444") is None and deals.ad_goods(conn, account, "999") is None and deals.ad_goods(conn, account, "") is None
    assert deals.ad_goods(conn, account, "не число") is None


def test_a_deal_from_a_chat_gets_the_position_with_the_price_when_the_ad_is_ours(tmp_path):
    conn, account, fake = setup(tmp_path)
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, seen_at) VALUES (?,?,?,?,?)", (account, 111, "G1-x4", "t", NOW))
    conn.commit()
    asked = []

    def provider(goods_id, pack):
        asked.append((goods_id, pack))
        return dict(goods_id=goods_id, code="T1", name="Шина Ikon", brand_key="ikon", season="winter", diameter=15, territory="1",
                    sale_unit=5750, supplier_price=4800, terms=None, fee_pct=0, promo_pct=0)
    fake.add("c1", "Татьяна", [("in", "Здравствуйте")], item=111)
    chats.sync_account(conn, fake, account, NOW)
    assert deals.process_all(conn, "2026-10-13T12:00:00", position_provider=provider, auto_create=True)["created"] == 1
    [item] = orders.get_card(conn, chats.list_chats(conn)[0]["order_id"])["items"]
    assert asked == [("G1", 4)] and (item["code"], item["qty"], float(item["sale_unit"])) == ("T1", 4, 5750.0)       # 4 pieces: the minimum order


def test_a_position_that_cannot_be_priced_is_noted_and_the_deal_is_made_anyway(tmp_path):
    conn, account, fake = setup(tmp_path)
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, seen_at) VALUES (?,?,?,?,?)", (account, 111, "G1", "t", NOW))
    conn.commit()

    def broken(goods_id, pack):
        raise orders.OrderError("Для этой позиции не рассчитана цена продажи: задайте процент бренда.")
    fake.add("c1", "Татьяна", [("in", "Здравствуйте")], item=111)
    chats.sync_account(conn, fake, account, NOW)
    deals.process_all(conn, "2026-10-13T12:00:00", position_provider=broken, auto_create=True)
    deal = orders.get_card(conn, chats.list_chats(conn)[0]["order_id"])
    assert deal["items"] == [] and any("Позиция не добавлена" in e["text"] and "процент бренда" in e["text"] for e in orders.list_events(conn, deal["id"]))


def test_a_deal_notes_that_the_position_is_unknown_when_the_ad_is_not_among_ours(tmp_path):
    conn, account, fake = setup(tmp_path)
    fake.add("c1", "Татьяна", [("in", "Здравствуйте")], item=555)                                      # an ad that is not in the table of our ads

    def provider(goods_id, pack):
        raise AssertionError("must not be asked")
    chats.sync_account(conn, fake, account, NOW)
    deals.process_all(conn, "2026-10-13T12:00:00", position_provider=provider, auto_create=True)
    deal = orders.get_card(conn, chats.list_chats(conn)[0]["order_id"])
    assert deal["items"] == [] and any("Позиция не определена" in e["text"] and "555" in e["text"] for e in orders.list_events(conn, deal["id"]))


def test_a_deal_from_a_chat_goes_to_the_default_responsible_manager_when_the_owner_named_one(tmp_path):
    conn, account, fake = setup(tmp_path)
    fake.add("c1", "Татьяна", [("in", "Здравствуйте")])
    sync(conn, account, fake)
    [chat] = chats.list_chats(conn)
    assert orders.get_card(conn, chat["order_id"])["responsible"] != "worker2"                                   # nobody named: whoever made the card (the robot)
    orders.set_default_responsible(conn, "worker2")
    fake.add("c2", "Пётр", [("in", "Добрый день")], item=222)
    sync(conn, account, fake)
    second = next(c for c in chats.list_chats(conn) if c["id"] != chat["id"])
    assert orders.get_card(conn, second["order_id"])["responsible"] == "worker2"


def test_a_first_message_makes_no_deal_by_itself_but_the_button_still_does(tmp_path):
    conn, account, fake = setup(tmp_path)
    fake.add("c1", "Татьяна", [("in", "Здравствуйте, это актуально? Мой телефон +7 900 000-00-17")])
    chats.sync_account(conn, fake, account, NOW)
    assert deals.process_all(conn, NOW) == {"created": 0, "linked": 0, "phones": 0}
    [chat] = chats.list_chats(conn)
    assert chat["order_id"] is None and conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 0
    card_id = deals.create_deal(conn, chat["id"], NOW, "pavel")                      # the manager makes it with the button
    assert chats.get_chat(conn, chat["id"])["order_id"] == card_id
    assert orders.get_card(conn, card_id)["client_phone"] == "+79000000017"


def test_a_deal_made_by_the_manager_still_gets_the_phone_and_the_note_when_the_client_writes_again(tmp_path):
    conn, account, fake = setup(tmp_path)
    fake.add("c1", "Татьяна", [("in", "Здравствуйте")])
    chats.sync_account(conn, fake, account, NOW)
    [chat] = chats.list_chats(conn)
    card_id = deals.create_deal(conn, chat["id"], NOW, "pavel")
    assert orders.get_card(conn, card_id)["client_phone"] == ""
    fake.messages["c1"].append({"id": "c1-9", "author_id": 9, "created": TS + 9, "content": {"text": "Звоните +7 900 000-00-17"}, "type": "text", "direction": "in", "isRead": False, "read": 0})
    chats.sync_account(conn, fake, account, "2026-10-13T12:10:00")
    assert deals.process_all(conn, "2026-10-13T12:10:00")["phones"] == 1               # without auto_create too
    assert orders.get_card(conn, card_id)["client_phone"] == "+79000000017"
