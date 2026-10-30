"""«Приехал» while the order still stands on «Заказ оформлен» (the owner 2026-10-21, order №423): the point writes when the tyres are there,
the stage in the panel is often not moved to «Перемещение на точку» yet. The command and the button of «Рабочее место» take it from both stages:
«Товар на точке» and «можно забирать» once, as from «Перемещение». A new order is still doubtful, with a plain answer."""
from __future__ import annotations

from test_mypoint import manager, press
from test_vk_commands import GROUP, NOW, SENT, _stage, hook, ready, to_client, to_points
from tires_sync import orders


def placed(tmp_path, *avito):
    app, client, tok, conn, pid, card = ready(tmp_path, *avito)
    orders.move_card(conn, card, _stage(conn, "placed"), NOW, "o")                       # the transfer is made in WebMIM, the panel not moved yet
    assert orders.get_card(conn, card)["stage"]["code"] == "placed"
    return app, client, tok, conn, pid, card


def outcome(conn):
    return [r[0] for r in conn.execute("SELECT outcome FROM order_command_log ORDER BY rowid")]


def test_arrived_on_placed_moves_to_the_point_and_tells_the_client_once(tmp_path):
    app, client, tok, conn, pid, card = placed(tmp_path, SENT)
    line = {"from_id": -int(GROUP), "text": "Заказ №5000100 оформлен, перемещение 26R-1380486"}
    hook(app, conn, "Приехали шины", reply=line)                                            # as order 423: an answer to the line of the order
    got = orders.get_card(conn, card)
    assert got["stage"]["code"] == "at_point" and got["reserve_until"]
    assert len(to_client(app)) == 1 and outcome(conn)[-1].startswith("заказ перенесён на «Товар на точке»; клиенту отправлено")
    assert any("отмечено: приехал" in text for _peer, text in to_points(app))
    hook(app, conn, "Приехали шины", event="e2", reply=line)                                     # the point repeats it
    assert len(to_client(app)) == 1


def test_arrived_on_moving_is_as_before(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    hook(app, conn, "приехал 5000100")
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point" and len(to_client(app)) == 1


def test_arrived_on_a_new_order_is_doubtful_and_says_it_is_not_placed(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    orders.move_card(conn, card, _stage(conn, "new"), NOW, "o")
    hook(app, conn, "приехали 5000100")
    assert orders.get_card(conn, card)["stage"]["code"] == "new" and to_client(app) == []
    assert outcome(conn)[-1].startswith("спорно: ")
    assert any("Заказ №5000100 ещё не оформлен в WebMIM" in text for _peer, text in to_points(app))


def test_the_button_of_my_point_shows_and_marks_a_placed_order(tmp_path):
    app, client, tok, conn, pid, card = placed(tmp_path, SENT)
    mine = manager(app, conn, pid)
    page = mine.get("/my-point?tab=moving").get_data(as_text=True)                     # «Едут» (the owner 2026-10-30: no «Все»)
    assert "/my-point/%d/arrived" % card in page
    _tok, page = press(mine, card, "arrived")
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point" and len(to_client(app)) == 1
    assert "отмечено: приехал" in page


def test_the_owner_confirms_a_doubtful_arrival_of_a_placed_order(tmp_path):
    from panel_helpers import post
    from tires_sync import order_manager as mgr
    app, client, tok, conn, pid, card = placed(tmp_path, SENT)
    hook(app, conn, "приехали")                                                              # no number: doubtful, waits for the owner
    [item] = [m for m in mgr.list_inbox(conn) if m["dispute"]]
    post(client, "/orders/inbox/%d/confirm" % item["id"], tok, order_no="5000100")
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point" and len(to_client(app)) == 1
