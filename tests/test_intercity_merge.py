"""Orders to another city in «Объединить заказы» and in the owner's words of «Команды» (the owner 2026-11-02): such orders merge only with each
other, to one city by one company, none sent yet; the new order is to another city with their city, company, the first track number and the
labels of all; «Разъединить» leaves each source its own. The owner's word into «Выполнено» of such an order is «Передал курьеру ТК».
Fakes only: nothing leaves the tests."""
from __future__ import annotations

import pytest

from test_intercity import city, code, tg, track_tasks  # noqa: F401  (fixture)
from test_order_merge import LATEST, NOW, _card, _merge, _second
from test_vk_commands import SENT, _stage, hook, ready, to_client, to_points
from tests.test_order_reserve import card_at, db
from tires_sync import order_intercity as intercity, order_manager as mgr, order_merge, order_rules as rules, orders
from tests.test_oth_sale import T

PDF = b"%PDF-1.4\n% labels\n" + b"0" * 100


def _to_city(conn, cid, town="Псков", carrier="ПЭК", track="", labels=()):
    intercity.save(conn, cid, dict(enabled="1", city=town, carrier=carrier, track=track), NOW, "owner")
    for name in labels:
        intercity.add_label(conn, cid, name, PDF + name.encode(), NOW, "owner")


def _labels(conn, cid):
    return [(r["name"], intercity.label_file(conn, cid, r["id"])[1]) for r in intercity.labels(conn, cid)]


def _texts(conn, cid):
    return [e["text"] for e in orders.list_events(conn, cid)]


# ---- «Объединить заказы» ---------------------------------------------------------------------------------------------------------------

def test_two_orders_to_one_city_merge_into_one_to_that_city_with_all_labels(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "at_point", T)
    b = _second(conn, "at_point")
    _to_city(conn, a, track="ПЭК-1", labels=("a1.pdf", "a2.pdf"))
    _to_city(conn, b, town="псков", track="ПЭК-2", labels=("b1.pdf",))           # the case of the city does not matter
    assert _card(conn, a)["reserve_until"]                                       # a reservation of before the checkbox
    mine = {src: (intercity.of(conn, src), _labels(conn, src)) for src in (a, b)}
    new = _merge(conn, [a, b], code="at_point")
    card = _card(conn, new)
    assert intercity.of(conn, new) == {"on": True, "city": "Псков", "carrier": "ПЭК", "track": "ПЭК-1"}     # the first's track number
    assert [n for n, _d in _labels(conn, new)] == ["a1.pdf", "a2.pdf", "b1.pdf"]
    assert _labels(conn, new) == mine[a][1] + mine[b][1]
    assert card["reserve_until"] == ""                                           # no reservation: the courier takes it
    texts = _texts(conn, new)
    assert any("Трек-номер заказа №%s: ПЭК-2 — не перенесён" % _card(conn, b)["webmim_number"] in t for t in texts)
    assert any(t.startswith("Доставка в другой город: да — Псков, ТК ПЭК, трек-номер ПЭК-1; этикеток из заказов: 3") for t in texts)
    for src in (a, b):                                                           # the sources keep their own
        assert (intercity.of(conn, src), _labels(conn, src)) == mine[src]
    order_merge.unmerge(conn, new, LATEST, "o")                                  # each its own mark, fields and labels
    for src in (a, b):
        assert _card(conn, src)["stage"]["code"] == "at_point"
        assert (intercity.of(conn, src), _labels(conn, src)) == mine[src]


def test_the_only_track_number_goes_and_nothing_is_journaled_about_another(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "moving", T)
    b = _second(conn)
    _to_city(conn, a)
    _to_city(conn, b, track="КИТ-9", carrier="ПЭК")
    new = _merge(conn, [a, b])
    assert intercity.of(conn, new)["track"] == "КИТ-9"
    assert not any("не перенесён" in t for t in _texts(conn, new))
    assert conn.execute("SELECT COUNT(*) FROM order_event WHERE order_id = ? AND level != 'grey'", (new,)).fetchone()[0] == 0


@pytest.mark.parametrize("town, carrier", [("Тверь", "ПЭК"), ("Псков", "КИТ"), ("", "ПЭК")])
def test_orders_to_different_cities_or_companies_are_refused(tmp_path, town, carrier):
    conn = db(tmp_path)
    a = card_at(conn, "moving", T)
    b = _second(conn)
    _to_city(conn, a)
    _to_city(conn, b, town=town, carrier=carrier)
    with pytest.raises(order_merge.MergeError, match="один город и одна ТК"):
        _merge(conn, [a, b])
    assert _card(conn, a)["stage"]["code"] == "moving" and order_merge.into(conn, a) is None


def test_an_order_to_another_city_and_an_ordinary_one_are_refused(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "moving", T)
    b = _second(conn)
    _to_city(conn, b)
    with pytest.raises(order_merge.MergeError, match="в другой город и обычный"):
        _merge(conn, [a, b])
    with pytest.raises(order_merge.MergeError, match="в другой город и обычный"):
        _merge(conn, [b, a])
    assert order_merge.into(conn, a) is None and order_merge.into(conn, b) is None


def test_an_order_shipped_already_is_refused(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "at_point", T)
    b = _second(conn, "at_point")
    _to_city(conn, a)
    _to_city(conn, b)
    intercity.ship(conn, _card(conn, a), "менеджер Бета", NOW, lambda *x: None)
    assert _card(conn, a)["stage"]["code"] == "done"                             # through «Отправлен»
    with pytest.raises(order_merge.MergeError, match="уже отправлен"):
        _merge(conn, [a, b], code="at_point")
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (_stage(conn, "shipped"), a))     # still on «Отправлен»
    conn.commit()
    with pytest.raises(order_merge.MergeError, match="уже отправлен"):
        _merge(conn, [a, b], code="at_point")


# ---- the owner's words of «Команды» ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("target", ["done", "shipped"])
def test_the_owners_word_into_done_ships_an_order_to_another_city(tmp_path, tg, target):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    rules.add_command(conn, _stage(conn, "at_point"), "отгрузил", _stage(conn, target))
    city(conn, card)
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    hook(app, conn, "отгрузил 5000100")
    assert code(conn, card) == "done" and intercity.was_shipped(conn, orders.get_card(conn, card))
    assert to_client(app) == ["Заказ 5000100 отправлен в город Псков транспортной компанией ПЭК.\n\nСпасибо за покупку!"]
    assert "передан курьеру ТК" in to_points(app)[-1][1]
    assert tg and "курьеру ТК ПЭК в Псков" in tg[0][1]
    assert [t for _a, t, _d, _c in track_tasks(conn, card)] == ["Внести трек-номер заказа №5000100"]
    hook(app, conn, "отгрузил 5000100", event="e2")                              # again: nothing more
    assert len(to_client(app)) == 1


def test_the_owners_word_of_an_ordinary_order_moves_it_as_before(tmp_path, tg):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    rules.add_command(conn, _stage(conn, "at_point"), "отгрузил", _stage(conn, "done"))
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    hook(app, conn, "отгрузил 5000100")
    assert code(conn, card) == "done" and not intercity.was_shipped(conn, orders.get_card(conn, card))
    assert not any("отправлен в город" in t for t in to_client(app)) and not tg and track_tasks(conn, card) == []


def test_the_owners_word_on_a_stage_the_courier_does_not_fit_goes_to_the_owner(tmp_path, tg):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    rules.add_command(conn, _stage(conn, "new"), "отгрузил", _stage(conn, "done"))
    city(conn, card)
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (_stage(conn, "new"), card))
    conn.commit()
    hook(app, conn, "отгрузил 5000100")
    assert code(conn, card) == "new" and not intercity.was_shipped(conn, orders.get_card(conn, card))
    assert "передано владельцу" in to_points(app)[-1][1] and not tg


def test_the_owner_confirms_a_disputed_owners_word_as_the_courier(tmp_path, tg):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    rules.add_command(conn, _stage(conn, "at_point"), "отгрузил", _stage(conn, "done"))
    city(conn, card)
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    hook(app, conn, "отгрузил")                                                  # no number: a question for the owner
    assert code(conn, card) == "at_point"
    item_id = conn.execute("SELECT id FROM manager_message ORDER BY id DESC LIMIT 1").fetchone()[0]
    reply = mgr.confirm(conn, item_id, "5000100", "2026-10-13T13:00:00", "owner", deliver=lambda *a: None)
    assert code(conn, card) == "done" and intercity.was_shipped(conn, orders.get_card(conn, card))
    assert "передан курьеру ТК" in reply and "подтвердил владелец" in reply
    assert len(track_tasks(conn, card)) == 1
