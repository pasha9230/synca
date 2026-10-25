"""An order without a point is bound by itself to the point the client wrote to (the owner 2026-10-22): the Avito account of exactly one
point, the VK of the point. A point already set is never changed (the first contact wins); an account of two points binds nothing."""
from __future__ import annotations

from panel_helpers import post
from test_chat_deals import Fake
from test_point_rights import bind, world
from test_roles import person
from test_vk_commands import PEER
from tires_sync import chat_deals as deals, chats, connections, order_manager as mgr, order_points as points, orders, store

NOW = "2026-10-22T12:00:00"


def base(tmp_path):
    """The account «Основной» (Татьяна) of no point, the account «Альфа» (Пётр) of the point Альфа, the point Гамма without one."""
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    main = connections.create_account(conn, "Основной", NOW, "o")
    rust_acc = connections.create_account(conn, "Альфа", NOW, "o")
    for account, chat_id, name in ((main, "c1", "Татьяна"), (rust_acc, "c9", "Пётр")):
        fake = Fake()
        fake.add(chat_id, name, [("in", "Есть зимние 205/55?")])
        chats.sync_account(conn, fake, account, NOW)
    rust = points.save_point(conn, {"name": "Альфа", "manager_name": "А", "channel": "vk", "avito_account_id": str(rust_acc)})
    gamma = points.save_point(conn, {"name": "Гамма", "manager_name": "Б", "channel": "vk"})
    by_name = {c["peer_name"]: c["id"] for c in chats.list_chats(conn)}
    return dict(conn=conn, main=main, rust_acc=rust_acc, rust=rust, gamma=gamma, petr=by_name["Пётр"], tanya=by_name["Татьяна"])


def _bound_events(conn, card_id):
    return [e for e in orders.list_events(conn, card_id) if e["text"].startswith("Привязан к точке")]


def test_the_point_of_the_account_is_found_and_an_account_of_two_points_or_of_none_finds_nothing(tmp_path):
    w = base(tmp_path)
    assert points.point_by_account(w["conn"], w["rust_acc"])["id"] == w["rust"]
    assert points.point_by_account(w["conn"], w["main"]) is None
    assert points.point_by_account(w["conn"], None) is None
    points.save_point(w["conn"], {"name": "Гамма", "manager_name": "Б", "channel": "vk", "avito_account_id": str(w["rust_acc"])}, w["gamma"])
    assert points.point_by_account(w["conn"], w["rust_acc"]) is None


def test_an_order_made_from_a_chat_of_the_point_account_is_of_the_point_with_a_record_by_the_system(tmp_path):
    w = base(tmp_path)
    card_id = deals.create_deal(w["conn"], w["petr"], NOW, "owner")
    assert orders.get_card(w["conn"], card_id)["point_id"] == w["rust"]
    bound = _bound_events(w["conn"], card_id)
    assert len(bound) == 1 and "«Альфа»" in bound[0]["text"] and "по аккаунту Авито" in bound[0]["text"] and deals.ref(w["petr"]) in bound[0]["text"]
    assert bound[0]["author"] == deals.SYSTEM_AUTHOR and bound[0]["kind"] == "field"                   # one record, as «Взят точкой» of «Взять»


def test_an_order_from_a_chat_of_the_main_account_stays_without_a_point(tmp_path):
    w = base(tmp_path)
    card_id = deals.create_deal(w["conn"], w["tanya"], NOW, "owner")
    assert orders.get_card(w["conn"], card_id)["point_id"] is None and _bound_events(w["conn"], card_id) == []


def test_an_account_of_two_points_binds_no_point(tmp_path):
    w = base(tmp_path)
    points.save_point(w["conn"], {"name": "Гамма", "manager_name": "Б", "channel": "vk", "avito_account_id": str(w["rust_acc"])}, w["gamma"])
    card_id = deals.create_deal(w["conn"], w["petr"], NOW, "owner")
    assert orders.get_card(w["conn"], card_id)["point_id"] is None and _bound_events(w["conn"], card_id) == []


def test_a_point_already_set_is_never_changed_by_a_chat_of_another_point(tmp_path):
    w = base(tmp_path)
    card_id = orders.create_card(w["conn"], {"client_name": "Пётр", "point_id": str(w["gamma"])}, NOW, "o")
    assert deals.bind_chat(w["conn"], card_id, w["petr"], "field", NOW, "owner")["state"] == "linked"
    assert orders.get_card(w["conn"], card_id)["point_id"] == w["gamma"] and _bound_events(w["conn"], card_id) == []
    other = orders.create_card(w["conn"], {"client_name": "Пётр", "point_id": str(w["gamma"])}, NOW, "o")
    deals.link_chat(w["conn"], w["petr"], other, NOW, "owner")                                     # «Привязать» in «Чаты» moves it
    assert orders.get_card(w["conn"], other)["point_id"] == w["gamma"] and _bound_events(w["conn"], other) == []


def test_a_chat_of_the_point_bound_to_an_order_without_a_point_binds_the_point(tmp_path):
    w = base(tmp_path)
    card_id = orders.create_card(w["conn"], {"client_name": "Пётр"}, NOW, "o")
    assert deals.bind_chat(w["conn"], card_id, w["petr"], "phone", NOW, "owner")["state"] == "linked"
    assert orders.get_card(w["conn"], card_id)["point_id"] == w["rust"] and len(_bound_events(w["conn"], card_id)) == 1
    later = orders.create_card(w["conn"], {"client_name": "Пётр"}, NOW, "o")
    deals.link_chat(w["conn"], w["petr"], later, NOW, "owner")                                     # the chat moves: the new order is bound too
    assert orders.get_card(w["conn"], later)["point_id"] == w["rust"]
    assert orders.get_card(w["conn"], card_id)["point_id"] == w["rust"]                           # the first keeps its point
    chat_of_main = orders.create_card(w["conn"], {"client_name": "Татьяна"}, NOW, "o")
    deals.bind_chat(w["conn"], chat_of_main, w["tanya"], "field", NOW, "owner")
    assert orders.get_card(w["conn"], chat_of_main)["point_id"] is None


def test_the_owner_makes_the_order_from_the_chat_of_the_point_in_the_panel_and_it_is_of_the_point(tmp_path):
    w = world(tmp_path)
    bind(w)
    post(w["owner"], "/chats/%d/create-deal" % w["petr"]["id"], w["tok"])
    card_id = chats.get_chat(w["conn"], w["petr"]["id"])["order_id"]
    assert card_id > 0 and orders.get_card(w["conn"], card_id)["point_id"] == w["rust"]
    assert len(_bound_events(w["conn"], card_id)) == 1


def test_the_manager_of_the_point_makes_the_order_from_the_own_chat_once_bound(tmp_path):
    w = world(tmp_path)
    bind(w)
    manager, conn = person(w["app"], "point", w["rust"])
    manager.post("/chats/%d/create-deal" % w["petr"]["id"], data={"csrf": "tok"})
    card_id = chats.get_chat(conn, w["petr"]["id"])["order_id"]
    assert card_id > 0 and orders.get_card(conn, card_id)["point_id"] == w["rust"]
    assert len(_bound_events(conn, card_id)) == 1                                                   # bound by the account; the fallback adds nothing
    assert [e for e in orders.list_events(conn, card_id) if e["text"].startswith("Точка:")] == []


def test_the_manager_of_a_point_with_a_shared_account_still_takes_the_order_to_the_own_point(tmp_path):
    w = world(tmp_path)
    bind(w)
    points.save_point(w["conn"], {"name": "Гамма", "manager_name": "Б", "channel": "vk", "avito_account_id": str(w["rust_acc"])}, w["gamma"])
    manager, conn = person(w["app"], "point", w["rust"])
    manager.post("/chats/%d/create-deal" % w["petr"]["id"], data={"csrf": "tok"})
    card_id = chats.get_chat(conn, w["petr"]["id"])["order_id"]
    assert card_id > 0 and orders.get_card(conn, card_id)["point_id"] == w["rust"] and _bound_events(conn, card_id) == []
    taken = [e for e in orders.list_events(conn, card_id) if e["text"].startswith("Точка:")]
    assert len(taken) == 1 and taken[0]["author"] == "point1"                                       # the manager's own record, as before


def test_the_new_order_window_with_the_chat_of_the_point_binds_the_point(tmp_path):
    w = world(tmp_path)
    bind(w)
    post(w["owner"], "/orders/new", w["tok"], client_name="Пётр", avito_chat=deals.ref(w["petr"]["id"]))
    card_id = chats.get_chat(w["conn"], w["petr"]["id"])["order_id"]
    assert card_id > 0 and orders.get_card(w["conn"], card_id)["point_id"] == w["rust"]


def test_a_client_handed_over_in_vk_is_of_the_point_that_wrote(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    points.save_point(conn, {"name": "Альфа", "manager_name": "А", "channel": "vk"})
    beta = points.save_point(conn, {"name": "Бета", "manager_name": "Алексей", "channel": "vk", "vk_peer_id": PEER})
    mgr.dispatch(conn, "vk-1", PEER, "Клиент 8 900 111-22-33 зимние 205/55 R16", None, NOW)
    card_id = conn.execute("SELECT order_id FROM order_handoff").fetchone()[0]
    assert orders.get_card(conn, card_id)["point_id"] == beta
    assert any(e["text"].startswith(mgr.ASK) and "Точка «Бета»" in e["text"] for e in orders.list_events(conn, card_id))    # the owner's words 2026-10-23


# ---- round 2 (the owner 2026-10-22: «Ок по всем шести предложениям»): a point taken off by hand, a closed order, duplicates, races ----

def _close(conn, card_id, code="done"):
    conn.execute("UPDATE order_card SET stage_id = (SELECT id FROM order_stage WHERE code = ?) WHERE id = ?", (code, card_id))
    conn.commit()


def test_a_point_taken_off_by_hand_is_never_bound_again_by_a_chat(tmp_path):
    w = base(tmp_path)
    card_id = orders.create_card(w["conn"], {"client_name": "Пётр", "point_id": str(w["gamma"])}, NOW, "o")
    orders.update_card(w["conn"], card_id, {"point_id": ""}, NOW, "owner")
    deals.bind_chat(w["conn"], card_id, w["petr"], "field", NOW, "owner")
    assert orders.get_card(w["conn"], card_id)["point_id"] is None and _bound_events(w["conn"], card_id) == []
    orders.update_card(w["conn"], card_id, {"point_id": str(w["gamma"])}, NOW, "owner")           # a person sets one again: as always
    assert orders.get_card(w["conn"], card_id)["point_id"] == w["gamma"]


def test_the_window_that_takes_the_point_off_and_binds_the_chat_in_one_save_keeps_the_order_without_a_point(tmp_path):
    w = world(tmp_path)
    bind(w)
    card_id = orders.create_card(w["conn"], {"client_name": "Пётр", "point_id": str(w["gamma"])}, NOW, "o")
    post(w["owner"], "/orders/%d/save" % card_id, w["tok"], point_id="", chat_bind=str(w["petr"]["id"]))
    assert chats.get_chat(w["conn"], w["petr"]["id"])["order_id"] == card_id
    assert orders.get_card(w["conn"], card_id)["point_id"] is None and _bound_events(w["conn"], card_id) == []


def test_a_closed_order_gets_no_point_by_a_chat(tmp_path):
    w = base(tmp_path)
    for code in ("done", "cancel", "return"):
        card_id = orders.create_card(w["conn"], {"client_name": "Пётр"}, NOW, "o")
        _close(w["conn"], card_id, code)
        deals.link_chat(w["conn"], w["petr"], card_id, NOW, "owner")
        assert orders.get_card(w["conn"], card_id)["point_id"] is None and _bound_events(w["conn"], card_id) == [], code


def test_the_order_that_gets_the_chats_of_a_duplicate_gets_the_point_by_the_same_rules(tmp_path):
    w = base(tmp_path)
    source = orders.create_card(w["conn"], {"client_name": "Пётр"}, NOW, "o")
    assert w["tanya"] < w["petr"]                                                                   # the chat of no point comes first
    deals.bind_chat(w["conn"], source, w["tanya"], "field", NOW, "owner")
    deals.bind_chat(w["conn"], source, w["petr"], "field", NOW, "owner")
    target = orders.create_card(w["conn"], {"client_name": "Пётр"}, NOW, "o")
    assert deals.merge_chats(w["conn"], source, target, NOW, "owner") == 2
    assert orders.get_card(w["conn"], target)["point_id"] == w["rust"]
    bound = _bound_events(w["conn"], target)
    assert len(bound) == 1 and bound[0]["author"] == deals.SYSTEM_AUTHOR and deals.ref(w["petr"]) in bound[0]["text"]
    for prepare in (lambda c: orders.update_card(w["conn"], c, {"point_id": str(w["gamma"])}, NOW, "o"),                  # a point set
                    lambda c: (orders.update_card(w["conn"], c, {"point_id": str(w["gamma"])}, NOW, "o"),
                               orders.update_card(w["conn"], c, {"point_id": ""}, NOW, "owner")),                           # taken off by hand
                    lambda c: _close(w["conn"], c)):                                                                       # closed
        other = orders.create_card(w["conn"], {"client_name": "Пётр"}, NOW, "o")
        prepare(other)
        before = orders.get_card(w["conn"], other)["point_id"]
        deals.merge_chats(w["conn"], target, other, NOW, "owner")
        assert orders.get_card(w["conn"], other)["point_id"] == before and _bound_events(w["conn"], other) == []
        target = other


def test_an_order_taken_by_another_point_between_the_read_and_the_write_keeps_that_point(tmp_path, monkeypatch):
    w = base(tmp_path)
    card_id = orders.create_card(w["conn"], {"client_name": "Пётр"}, NOW, "o")
    found = points.point_by_account

    def taken_meanwhile(conn, account_id):                                                         # «Взять» of Гамма lands after the card was read
        conn.execute("UPDATE order_card SET point_id = ? WHERE id = ? AND point_id IS NULL", (w["gamma"], card_id))
        return found(conn, account_id)
    monkeypatch.setattr(points, "point_by_account", taken_meanwhile)
    assert deals.bind_chat(w["conn"], card_id, w["petr"], "field", NOW, "owner")["state"] == "linked"
    assert orders.get_card(w["conn"], card_id)["point_id"] == w["gamma"] and _bound_events(w["conn"], card_id) == []


def test_the_manager_of_a_point_never_takes_an_order_another_point_took_meanwhile(tmp_path, monkeypatch):
    w = world(tmp_path)
    bind(w)
    points.save_point(w["conn"], {"name": "Гамма", "manager_name": "Б", "channel": "vk", "avito_account_id": str(w["rust_acc"])}, w["gamma"])
    manager, conn = person(w["app"], "point", w["rust"])
    made, read = deals.create_deal, orders.get_card
    stale = {}

    def create_then_taken(conn_, chat_row, *args, **kwargs):                                       # the deal is made, then Гамма presses «Взять»
        card_id = made(conn_, chat_row, *args, **kwargs)
        stale.update(read(conn_, card_id))
        other = store.connect(w["app"].config["DB_PATH"])
        other.execute("UPDATE order_card SET point_id = ? WHERE id = ? AND point_id IS NULL", (w["gamma"], card_id))
        other.commit()
        return card_id

    def read_before_the_take(conn_, card_id):                                                      # what was read before «Взять» landed
        return dict(stale) if stale.get("id") == card_id else read(conn_, card_id)
    monkeypatch.setattr(deals, "create_deal", create_then_taken)
    monkeypatch.setattr(orders, "get_card", read_before_the_take)
    manager.post("/chats/%d/create-deal" % w["petr"]["id"], data={"csrf": "tok"})
    monkeypatch.undo()
    card_id = chats.get_chat(conn, w["petr"]["id"])["order_id"]
    assert card_id > 0 and orders.get_card(conn, card_id)["point_id"] == w["gamma"]
    assert [e for e in orders.list_events(conn, card_id) if e["text"].startswith("Точка:")] == []


def _usual_time(conn, point_id, account_id=None):
    data = dict(points.get_point(conn, point_id), default_time="с 12 до 18")
    data["avito_account_id"] = str(account_id or data["avito_account_id"] or "")
    points.save_point(conn, data, point_id)


def test_the_point_bound_by_the_chat_brings_its_usual_delivery_time_and_a_time_typed_stays(tmp_path):
    w = base(tmp_path)
    _usual_time(w["conn"], w["rust"])
    card_id = deals.create_deal(w["conn"], w["petr"], NOW, "owner")
    card = orders.get_card(w["conn"], card_id)
    assert card["point_id"] == w["rust"] and card["delivery_time"] == "с 12 до 18"
    told = [e for e in orders.list_events(w["conn"], card_id) if e["text"].startswith("Время доставки:")]
    assert len(told) == 1 and told[0]["author"] == deals.SYSTEM_AUTHOR and "с 12 до 18" in told[0]["text"]
    typed = orders.create_card(w["conn"], {"client_name": "Пётр", "delivery_time": "после 19"}, NOW, "o")
    deals.link_chat(w["conn"], w["petr"], typed, NOW, "owner")
    assert orders.get_card(w["conn"], typed)["point_id"] == w["rust"] and orders.get_card(w["conn"], typed)["delivery_time"] == "после 19"


def test_the_manager_of_the_point_brings_the_usual_time_too_and_never_a_point_that_is_gone(tmp_path):
    w = world(tmp_path)
    bind(w)
    _usual_time(w["conn"], w["rust"])
    points.save_point(w["conn"], {"name": "Гамма", "manager_name": "Б", "channel": "vk", "avito_account_id": str(w["rust_acc"])}, w["gamma"])
    manager, conn = person(w["app"], "point", w["rust"])
    manager.post("/chats/%d/create-deal" % w["petr"]["id"], data={"csrf": "tok"})
    card_id = chats.get_chat(conn, w["petr"]["id"])["order_id"]
    assert orders.get_card(conn, card_id)["point_id"] == w["rust"] and orders.get_card(conn, card_id)["delivery_time"] == "с 12 до 18"
    free = orders.create_card(conn, {"client_name": "Никто"}, NOW, "o")
    assert deals.claim_point(conn, free, 999, NOW, "point1", "Точка: 999") is False                # no such point: nothing is written
    assert orders.get_card(conn, free)["point_id"] is None


def test_the_manager_of_the_point_takes_no_closed_order_and_no_order_whose_point_was_taken_off_by_hand(tmp_path):
    """The owner 2026-10-22: _to_own_point follows the rules of the binding by a chat (the existing order of the chat comes back from «Создать заказ»)."""
    w = world(tmp_path)
    bind(w)
    manager, conn = person(w["app"], "point", w["rust"])
    closed = orders.create_card(conn, {"client_name": "Пётр"}, NOW, "o")
    _close(conn, closed)
    deals.link_chat(conn, w["petr"]["id"], closed, NOW, "owner")
    manager.post("/chats/%d/create-deal" % w["petr"]["id"], data={"csrf": "tok"})
    assert chats.get_chat(conn, w["petr"]["id"])["order_id"] == closed and orders.get_card(conn, closed)["point_id"] is None
    cleared = orders.create_card(conn, {"client_name": "Пётр", "point_id": str(w["gamma"])}, NOW, "o")
    orders.update_card(conn, cleared, {"point_id": ""}, NOW, "owner")
    deals.link_chat(conn, w["petr"]["id"], cleared, NOW, "owner")
    manager.post("/chats/%d/create-deal" % w["petr"]["id"], data={"csrf": "tok"})
    assert chats.get_chat(conn, w["petr"]["id"])["order_id"] == cleared and orders.get_card(conn, cleared)["point_id"] is None
    assert [e for e in orders.list_events(conn, cleared) if e["text"] == "Точка: %d" % w["rust"]] == []
