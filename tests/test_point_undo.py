"""The owner 2026-11-02 (the answer «всегда»): «Отменить» of «Приехал», «Выдал» and «На ОТХ» on «Рабочее место» — with no time limit, while
nothing happened on the order after the press (another press, a command of the point, a move) and no sale is recorded; it puts the order
back where it was, tells the owner and the partner, is an action of «История точки», and says that a message sent to the client stays.
«Передал клиента» is undone by «Отменить передачу» (order_manager.cancel_handoff, its own tests). Also the card of 2026-11-02 and the menu."""
from __future__ import annotations

import re

import pytest

from panel_helpers import post
from test_mypoint import manager, press, token, workplace
from test_vk_commands import SENT, _card, _stage, ready, to_client
from tires_sync import order_notify, order_points as points, orders, point_log, users
from tires_sync.panel import routes_mypoint

T1, T2, T3 = "2026-10-16T11:00:00", "2026-10-16T12:00:00", "2026-10-16T13:00:00"


@pytest.fixture
def clock(monkeypatch):
    moment = {"now": T1}
    monkeypatch.setattr(routes_mypoint, "local_iso", lambda: moment["now"])
    return moment


@pytest.fixture
def told(monkeypatch):
    sent = []
    monkeypatch.setattr(order_notify, "tell_people", lambda conn, box, logins, text, **k: sent.append((logins, text)) or "ok")
    return sent


def refuse_to_oth(mine, card, clock):
    """«ОТКАЗ» with its reason: the order goes to «На ОТХ» (the owner 2026-11-05; it was «Клиент не пришёл», then «Да, на «На ОТХ»»)."""
    clock["now"] = T2
    press(mine, card, "refuse")


def refused_move(conn, mine, card):
    """A press of the point that moves nothing: «ОТКАЗ» of an order without the client's phone (its red record is the point's action)."""
    conn.execute("UPDATE order_card SET client_phone = '' WHERE id = ?", (card,))
    conn.commit()
    press(mine, card, "refuse")


def undo(mine, card):
    r = post(mine, "/my-point/%d/undo" % card, "tok")
    assert r.status_code == 302
    return mine.get(r.headers["Location"]).get_data(as_text=True)


def state(conn, card):
    c = orders.get_card(conn, card)
    return c["stage"]["code"], c["entered_at"], c["reserve_until"], c["cancel_reason"]


def test_arrived_is_undone_back_to_the_way_and_the_office_is_told(tmp_path, clock, told):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    users.add_user(conn, "zaman", "password-123", T1, role="partner")
    conn.commit()
    before = state(conn, card)
    mine = manager(app, conn, pid)
    press(mine, card, "arrived")
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point" and len(to_client(app)) == 1
    page = workplace(mine)
    assert 'action="/my-point/%d/undo"' % card in page and "не отзывается" in page and "«Приехал»" in page
    clock["now"] = T2
    page = undo(mine, card)
    assert "Отметка «Приехал» отменена" in page and "не отзывается" in page
    assert state(conn, card) == before                                                  # the stage, the moment, the date, the reason
    assert len(to_client(app)) == 1                                                     # nothing new to the client, nothing recalled
    assert told and told[0][0] == ["owner"] and "отменила отметку «Приехал»" in told[0][1]
    [record] = [e for e in orders.list_events(conn, card) if "отменила отметку" in e["text"]]
    assert record["level"] == "yellow" and point_log.UNDO_HINT in record["text"]
    assert [r["kind"] for r in point_log.history(conn, pid)][:2] == ["undo", "arrived"]
    assert "отметку уже не отменить" in undo(mine, card).lower() or "уже не отменить" in undo(mine, card)     # a second press


def test_issued_is_undone_into_the_same_stay_at_the_point(tmp_path, clock, told):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    orders.move_card(conn, card, _stage(conn, "at_point"), "2026-10-13T12:00:00", "o")
    before = state(conn, card)
    mine = manager(app, conn, pid)
    press(mine, card, "issued")
    assert orders.get_card(conn, card)["stage"]["code"] == "done"
    reserve_lines = lambda: [e["text"] for e in orders.list_events(conn, card) if e["text"].startswith("Резерв: товар на точке")]
    lines_before = reserve_lines()
    clock["now"] = T2
    undo(mine, card)
    assert state(conn, card) == before and to_client(app) == []                          # the same stay: no new «можно забирать»
    texts = [e["text"] for e in orders.list_events(conn, card)]
    assert reserve_lines() == lines_before                                             # the undo's move leaves no «Резерв…» line of its own
    assert any("снова на этапе «На точке, ждём клиента», резерв до 16.10.2026" in t for t in texts)
    assert not any("[резерв:на-отх:" in t for t in texts)                               # no reminder went: a later one still counts


def test_to_oth_is_undone_with_the_reason_and_the_reservation(tmp_path, clock, told):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    orders.move_card(conn, card, _stage(conn, "at_point"), "2026-10-13T12:00:00", "o")
    before = state(conn, card)
    mine = manager(app, conn, pid)
    refuse_to_oth(mine, card, clock)
    assert orders.get_card(conn, card)["stage"]["code"] == "oth"
    clock["now"] = T3
    assert "«ОТКАЗ» отменена" in undo(mine, card)
    assert state(conn, card) == before


def test_a_later_action_or_move_or_sale_makes_it_final(tmp_path, clock, told):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    mine = manager(app, conn, pid)
    press(mine, card, "arrived")
    clock["now"] = T2
    refused_move(conn, mine, card)                                                      # a later press of the point
    assert 'action="/my-point/%d/undo"' % card not in workplace(mine)
    assert "уже не отменить" in undo(mine, card) and orders.get_card(conn, card)["stage"]["code"] == "at_point"

    second = _card(conn, pid, webmim="700300")
    clock["now"] = T1
    press(mine, second, "arrived")
    orders.move_card(conn, second, _stage(conn, "done"), T2, "owner")                   # the owner moved it
    clock["now"] = T3
    assert "уже не отменить" in undo(mine, second) and orders.get_card(conn, second)["stage"]["code"] == "done"

    third = _card(conn, pid, webmim="700400", code="at_point")
    clock["now"] = T1
    press(mine, third, "issued")
    conn.execute("UPDATE order_card SET sale_ids = '[1]' WHERE id = ?", (third,))         # a sale is recorded: closed
    conn.commit()
    clock["now"] = T2
    assert "уже не отменить" in undo(mine, third) and orders.get_card(conn, third)["stage"]["code"] == "done"
    assert told == []


def test_a_vk_command_after_the_press_makes_it_final(tmp_path, clock, told):
    from test_vk_commands import hook
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    orders.move_card(conn, card, _stage(conn, "at_point"), "2026-10-13T12:00:00", "o")
    mine = manager(app, conn, pid)
    refuse_to_oth(mine, card, clock)
    from tires_sync import order_oth
    order_oth.agree(conn, orders.get_card(conn, card), orders.date.fromisoformat("2026-10-14"), "2026-10-14T10:00:00", "owner")     # the lead 2026-11-05: agreed first
    hook(app, conn, "№5000100 возврат отдал подтверждаю")                                            # the point's own VK, after the press
    assert "уже не отменить" in undo(mine, card)


def test_another_points_order_and_a_press_that_moved_nothing_have_no_undo(tmp_path, clock, told):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    other = points.save_point(conn, dict(name="Гамма", channel="vk", vk_peer_id="777"))
    theirs = _card(conn, other, webmim="700100", code="at_point")
    mine = manager(app, conn, pid)
    assert post(mine, "/my-point/%d/undo" % theirs, "tok").status_code in (403, 404)     # never another point's order
    orders.move_card(conn, card, _stage(conn, "at_point"), "2026-10-13T12:00:00", "o")
    refused_move(conn, mine, card)                                                      # the stage stays: nothing to undo
    assert point_log.undo_list(conn, pid) == []


def test_the_undo_is_kept_without_a_time_limit(tmp_path, clock, told):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    mine = manager(app, conn, pid)
    press(mine, card, "arrived")
    clock["now"] = "2027-03-01T10:00:00"                                                # months later, nothing happened since
    assert "отменена" in undo(mine, card) and orders.get_card(conn, card)["stage"]["code"] == "moving"


# ---- the card and the menu of 2026-11-02 ---------------------------------------------------------------------------------------------

def test_the_card_has_the_date_under_the_number_a_big_issue_and_refuse_with_its_window(tmp_path, clock):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    orders.move_card(conn, card, _stage(conn, "at_point"), "2026-10-13T12:00:00", "o")
    page = manager(app, conn, pid).get("/my-point?tab=here").get_data(as_text=True)
    head = re.search(r'<div class="mp-card-head">(.*?)</div>', page[page.index('id="order-%d"' % card):], re.S).group(1)
    assert head.index("№ 5000100") < head.index("от 13.10.2026") < head.index("на точке")    # one under another, centred by .mp-card-head
    issued = re.search(r'action="/my-point/%d/issued".*?<button class="([^"]+)"' % card, page, re.S).group(1)
    refuse = re.search(r'action="/my-point/%d/refuse"([^>]*)>.*?<button class="([^"]+)"' % card, page, re.S)
    assert "btn--primary" in issued and "mp-btn--big" in issued
    assert "btn--danger" in refuse.group(2) and "mp-btn--big" not in refuse.group(2)      # «ОТКАЗ» (the owner 2026-11-05)
    assert 'data-confirm-title="Отказ по заказу № 5000100?"' in refuse.group(1) and "data-confirm-danger" in refuse.group(1)   # never alert()


def test_the_sidebar_of_the_point_has_calls_and_no_orders(tmp_path, clock):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    page = manager(app, conn, pid).get("/my-point").get_data(as_text=True)
    sidebar = page[page.index('class="sidebar"'):page.index("</aside>")] if "</aside>" in page else page
    assert 'href="/calls"' in sidebar and 'href="/orders"' not in sidebar
    assert 'href="/orders"' in client.get("/my-point").get_data(as_text=True)          # the owner keeps «Заказы»


def test_an_undone_issue_is_not_moved_to_oth_by_the_reminder_of_before(tmp_path, clock, told):
    from tires_sync import order_reserve as reserve
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    orders.move_card(conn, card, _stage(conn, "at_point"), "2026-10-13T12:00:00", "o")
    got = orders.get_card(conn, card)
    chat = conn.execute("SELECT id FROM avito_chat WHERE order_id = ?", (card,)).fetchone()[0]
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status) VALUES (?, ?, '2026-10-16T10:00:00', 'система', 'sent')",
                 ("reserve-%d-%s" % (card, re.sub(r"\D", "", got["entered_at"])), chat))   # the reminder of the last day went
    conn.execute("UPDATE avito_chat SET synced_at = '2099-01-01T00:00:00'")
    conn.commit()
    mine = manager(app, conn, pid)
    press(mine, card, "issued")
    clock["now"] = T2
    undo(mine, card)
    assert not reserve._no_answer_step(conn, orders.get_card(conn, card), "2026-10-16T23:00:00")
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point"


def test_an_unexpected_error_inside_the_undo_rolls_it_all_back(tmp_path, clock, told, monkeypatch):
    """The review 2026-11-12: an error that is not an OrderError inside «Отменить» (here: the journal's record) rolls back the claim and
    the move, never leaves them hanging in the open transaction for whoever commits next; the undo can be pressed again."""
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    mine = manager(app, conn, pid)
    press(mine, card, "arrived")
    pressed = state(conn, card)
    real = orders.add_event

    def broken(conn_, order_id, *a, **k):
        if "отменила отметку" in str(a):
            raise RuntimeError("журнал недоступен")
        return real(conn_, order_id, *a, **k)
    monkeypatch.setattr(orders, "add_event", broken)
    with pytest.raises(RuntimeError):
        point_log.undo(conn, orders.get_card(conn, card), points.get_point(conn, pid), "Анна", T2)
    assert not conn.in_transaction and state(conn, card) == pressed                     # nothing half-done waits for a commit
    assert point_log.undoable(conn, orders.get_card(conn, card), pid) is not None       # the claim is back: «Отменить» works again
