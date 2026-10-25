"""The rules of a stage set up for a point fire for an order that gets the point by itself (the Avito account of the point, the duplicate's
chats, the manager of the point making the order from a chat): once, after the point is saved. The general rules of the stage ran when the
order entered it and are not run again, not even one added since; a second binding makes nothing more. Nothing goes to the client by itself."""
from __future__ import annotations

from panel_helpers import post
from test_point_autobind import NOW, base
from test_point_rights import bind, world
from test_roles import person
from tires_sync import chat_deals as deals, chats, order_engine as engine, order_points as points, order_rules as rules, orders


def _new(conn):
    return next(s["id"] for s in orders.list_stages(conn) if s["code"] == "new")


def _rules(conn, rust, gamma):
    """On «Новый»: a reminder for everybody, a reminder and an auto tag for Альфа, a reminder for Гамма."""
    new = _new(conn)
    return dict(everybody=rules.add_rule(conn, new, "enter", 0, "remind", None, "Ответить клиенту", "ask", None),
                rust=rules.add_rule(conn, new, "enter", 0, "remind", None, "Альфа: позвонить клиенту", "ask", rust),
                tag=rules.add_rule(conn, new, "enter", 0, "tag_add", None, "альфа", "auto", rust),
                gamma=rules.add_rule(conn, new, "enter", 0, "remind", None, "Гамма: позвонить", "ask", gamma))


def _made(conn, card_id):
    """rule -> how many tasks the order has of it."""
    out = {}
    for task in engine.list_tasks(conn, card_id):
        out[task["rule_id"]] = out.get(task["rule_id"], 0) + 1
    return out


def _check_once(conn, card_id, r, point):
    card = orders.get_card(conn, card_id)
    assert card["point_id"] == point and card["stage"]["code"] == "new"
    made = _made(conn, card_id)
    assert made.get(r["rust"]) == 1 and made.get(r["tag"]) == 1 and made.get(r["everybody"]) == 1 and r["gamma"] not in made, made
    assert card["tags"].count("альфа") == 1
    assert len([e for e in orders.list_events(conn, card_id) if e["text"] == "Задача создана: Альфа: позвонить клиенту"]) == 1


def test_the_deal_made_from_a_chat_of_the_point_account_gets_the_rules_of_the_point_once(tmp_path):
    w = base(tmp_path)
    r = _rules(w["conn"], w["rust"], w["gamma"])
    card_id = deals.create_deal(w["conn"], w["petr"], NOW, "owner")
    _check_once(w["conn"], card_id, r, w["rust"])


def test_a_chat_bound_later_brings_only_the_rules_of_the_point_and_a_second_binding_nothing(tmp_path):
    w = base(tmp_path)
    r = _rules(w["conn"], w["rust"], w["gamma"])
    card_id = orders.create_card(w["conn"], {"client_name": "Пётр"}, NOW, "o")
    assert _made(w["conn"], card_id) == {r["everybody"]: 1}                                     # no point yet: the general rule only
    late = rules.add_rule(w["conn"], _new(w["conn"]), "enter", 0, "remind", None, "Новое общее правило", "ask", None)
    assert deals.bind_chat(w["conn"], card_id, w["petr"], "phone", NOW, "owner")["state"] == "linked"
    _check_once(w["conn"], card_id, r, w["rust"])
    assert late not in _made(w["conn"], card_id)                                                # a general rule is not run again, nor one added since
    deals.unbind_chat(w["conn"], card_id, w["petr"], NOW, "owner")
    deals.link_chat(w["conn"], w["petr"], card_id, NOW, "owner")                                # bound again: the point is there, nothing new
    _check_once(w["conn"], card_id, r, w["rust"])


def test_an_order_of_no_point_bound_to_a_chat_of_no_point_gets_no_rules_of_a_point(tmp_path):
    w = base(tmp_path)
    r = _rules(w["conn"], w["rust"], w["gamma"])
    card_id = orders.create_card(w["conn"], {"client_name": "Татьяна"}, NOW, "o")
    deals.bind_chat(w["conn"], card_id, w["tanya"], "field", NOW, "owner")
    assert orders.get_card(w["conn"], card_id)["point_id"] is None and _made(w["conn"], card_id) == {r["everybody"]: 1}


def test_a_point_set_before_the_binding_keeps_its_own_rules_and_gets_no_others(tmp_path):
    w = base(tmp_path)
    r = _rules(w["conn"], w["rust"], w["gamma"])
    card_id = orders.create_card(w["conn"], {"client_name": "Пётр", "point_id": str(w["gamma"])}, NOW, "o")
    deals.bind_chat(w["conn"], card_id, w["petr"], "field", NOW, "owner")
    assert _made(w["conn"], card_id) == {r["everybody"]: 1, r["gamma"]: 1}


def test_the_order_that_gets_the_chats_of_a_duplicate_gets_the_rules_of_the_point_once(tmp_path):
    w = base(tmp_path)
    source = orders.create_card(w["conn"], {"client_name": "Пётр"}, NOW, "o")
    deals.bind_chat(w["conn"], source, w["petr"], "field", NOW, "owner")
    r = _rules(w["conn"], w["rust"], w["gamma"])                                               # the rules come after the source got its point
    target = orders.create_card(w["conn"], {"client_name": "Пётр"}, NOW, "o")
    assert deals.merge_chats(w["conn"], source, target, NOW, "owner") == 1
    _check_once(w["conn"], target, r, w["rust"])


def test_the_new_order_window_with_the_chat_of_the_point_gets_the_rules_of_the_point_once(tmp_path):
    w = world(tmp_path)
    bind(w)
    r = _rules(w["conn"], w["rust"], w["gamma"])
    post(w["owner"], "/orders/new", w["tok"], client_name="Пётр", avito_chat=deals.ref(w["petr"]["id"]))
    card_id = chats.get_chat(w["conn"], w["petr"]["id"])["order_id"]
    _check_once(w["conn"], card_id, r, w["rust"])


def test_the_manager_of_a_point_with_a_shared_account_gets_the_rules_of_the_own_point_once(tmp_path):
    w = world(tmp_path)
    bind(w)
    points.save_point(w["conn"], {"name": "Гамма", "manager_name": "Б", "channel": "vk", "avito_account_id": str(w["rust_acc"])}, w["gamma"])
    r = _rules(w["conn"], w["rust"], w["gamma"])
    manager, conn = person(w["app"], "point", w["rust"])
    manager.post("/chats/%d/create-deal" % w["petr"]["id"], data={"csrf": "tok"})
    card_id = chats.get_chat(conn, w["petr"]["id"])["order_id"]
    _check_once(conn, card_id, r, w["rust"])


def test_a_failure_after_the_binding_was_saved_never_undoes_it_nor_breaks_the_binding(tmp_path, monkeypatch):
    import inspect
    w = base(tmp_path)
    r = _rules(w["conn"], w["rust"], w["gamma"])
    card_id = orders.create_card(w["conn"], {"client_name": "Пётр"}, NOW, "o")
    real = orders.get_card

    def flaky(conn, card):
        if inspect.stack()[1].function == "fire_point_rules":
            raise RuntimeError("the base went away")
        return real(conn, card)
    monkeypatch.setattr(orders, "get_card", flaky)
    assert deals.bind_chat(w["conn"], card_id, w["petr"], "phone", NOW, "owner")["state"] == "linked"
    monkeypatch.setattr(orders, "get_card", real)
    assert orders.get_card(w["conn"], card_id)["point_id"] == w["rust"] and r["rust"] not in _made(w["conn"], card_id)
