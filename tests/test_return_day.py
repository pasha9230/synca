"""The owner 2026-11-12: «Сегодня возврат!». On the day of the return (oth_return_plan.return_date), from 09:00 Moscow, a point that works in
the panel is told once per point and date (not per order; the numbers of the orders are in the text), marked «Важное», in its bell and on
«Рабочее место»; an open page rings at 09:00, 12:00 and 15:00 (a new key of /my-point/feed for each), until «Подписал акт и отдал» is pressed
for every order of the date. Only the panel of the point: nothing to VK, nothing to a client, nothing to a point that works in VK. The
reminder of an overdue date (remind_handover) is as it was."""
from __future__ import annotations

from datetime import date

import pytest

from test_mypoint import manager
from test_point_oth_sell import setup
from test_point_return_tabs import to_return_stage
from test_vk_commands import _card, _stage, to_client, to_points
from tires_sync import order_oth, order_reserve as reserve, orders
from tires_sync.panel import routes_alerts, routes_mypoint

DAY = "2026-11-13"                                   # a Friday
TEXT = "Сегодня возврат! Распечатайте акт и передайте с шинами доставке поставщика."


@pytest.fixture
def clock(monkeypatch):
    moment = {"now": DAY + "T09:05:00"}
    monkeypatch.setattr(routes_mypoint, "local_iso", lambda: moment["now"])
    monkeypatch.setattr(routes_alerts, "local_iso", lambda: moment["now"])
    return moment


def in_panel(conn, pid):
    conn.execute("UPDATE point SET channel = 'panel' WHERE id = ?", (pid,))
    conn.commit()


def agreed(conn, card, day=DAY):
    """The order on «На возврат» with the return agreed for `day`."""
    to_return_stage(conn, card)
    order_oth.agree(conn, orders.get_card(conn, card), date.fromisoformat("2026-11-10"), "2026-11-10T10:00:00", "owner", day)


def second(conn, pid, day=DAY, webmim="5000200"):
    card = _card(conn, pid, code="at_point", webmim=webmim)
    orders.move_card(conn, card, _stage(conn, "oth"), "2026-10-14T09:00:00", "точка", changes={"cancel_reason": "Клиент не пришёл"})
    conn.commit()
    agreed(conn, card, day)
    return card


def marks(conn):
    return [r[0] for r in conn.execute("SELECT key FROM app_state WHERE key LIKE 'oth_return_day:%' ORDER BY key")]


def sign(conn, card):
    assert order_oth.sign_act(conn, orders.get_card(conn, card), "Бета", DAY + "T10:00:00", "gamma") in ("return", "signed")
    conn.commit()


def feed_keys(client):
    return [it["key"] for it in client.get("/my-point/feed").get_json()["items"] if "return-day" in it["key"]]


def test_one_notice_per_point_and_date_with_the_numbers_of_the_orders(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    in_panel(conn, pid)
    agreed(conn, card)
    other = second(conn, pid)
    assert order_oth.tell_return_day(conn, DAY + "T09:00:00") == 1
    assert marks(conn) == ["oth_return_day:%d:%s" % (pid, DAY)]
    assert order_oth.tell_return_day(conn, DAY + "T09:20:00") == 0                   # the same hour again: no second one
    assert order_oth.tell_return_day(conn, DAY + "T12:00:00") == 0
    assert len(marks(conn)) == 1
    told = order_oth.return_day(conn, pid, DAY + "T09:20:00")
    assert told["text"] == TEXT and told["date"] == DAY and told["ids"] == [card, other]
    assert told["orders"] == ["5000100", "5000200"]


def test_not_before_nine_not_without_a_date_and_not_after_the_act_is_signed(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    in_panel(conn, pid)
    to_return_stage(conn, card)                                                       # «На возврат», the date not agreed yet
    assert order_oth.tell_return_day(conn, DAY + "T10:00:00") == 0 and marks(conn) == []
    order_oth.agree(conn, orders.get_card(conn, card), date.fromisoformat("2026-11-10"), "2026-11-10T10:00:00", "owner", DAY)
    assert order_oth.tell_return_day(conn, "2026-11-12T10:00:00") == 0               # the day before
    assert order_oth.tell_return_day(conn, DAY + "T08:59:00") == 0 and marks(conn) == []
    assert order_oth.return_day(conn, pid, DAY + "T08:59:00") is None
    sign(conn, card)
    assert order_oth.tell_return_day(conn, DAY + "T10:00:00") == 0 and marks(conn) == []


def test_no_run_at_nine_the_first_run_of_the_day_after_it_tells(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    in_panel(conn, pid)
    agreed(conn, card)
    assert order_oth.tell_return_day(conn, DAY + "T16:40:00") == 1
    assert order_oth.return_day(conn, pid, DAY + "T16:40:00")["sound"] == "15"
    assert order_oth.return_day(conn, pid, "2026-11-14T10:00:00") is None             # the next day: the overdue reminder's, not this one


def test_the_regular_run_tells_and_nothing_goes_to_vk_or_to_a_client(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    in_panel(conn, pid)
    agreed(conn, card)
    before = (len(to_client(app)), len(to_points(app)))

    def channel(*args, **kwargs):
        raise AssertionError("«Сегодня возврат!» is the panel's only: %r" % (args,))

    reserve.check(conn, DAY + "T09:01:00", channel, channel, channel)
    reserve.check(conn, DAY + "T09:31:00", channel, channel, channel)
    assert marks(conn) == ["oth_return_day:%d:%s" % (pid, DAY)]
    assert (len(to_client(app)), len(to_points(app))) == before
    assert not [e for e in orders.list_events(conn, card) if "Сегодня возврат" in e["text"]]      # no record of the order for the office's bell


def test_a_point_that_works_in_vk_is_told_nothing(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)                                    # the point Бета: VK
    agreed(conn, card)
    assert order_oth.tell_return_day(conn, DAY + "T09:30:00") == 0 and marks(conn) == []
    assert order_oth.return_day(conn, pid, DAY + "T09:30:00") is None
    assert not to_points(app)


def test_the_overdue_reminder_is_as_it_was(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    in_panel(conn, pid)
    agreed(conn, card, "2026-11-11")
    told = []
    assert order_oth.remind_handover(conn, DAY + "T10:00:00", lambda c, text: told.append(text)) == 1
    assert len(told) == 1 and "11.11" in told[0]
    assert order_oth.tell_return_day(conn, DAY + "T10:00:00") == 0                   # not today's return


def test_three_keys_of_the_sound_and_none_once_every_act_is_signed(tmp_path, clock):
    app, owner, conn, pid, card = setup(tmp_path)
    in_panel(conn, pid)
    agreed(conn, card)
    other = second(conn, pid)
    mine = manager(app, conn, pid)
    assert feed_keys(mine) == []                                                      # the run has not told yet
    order_oth.tell_return_day(conn, clock["now"])
    heard = []
    for at in ("09:05", "11:59", "12:00", "14:59", "15:00", "19:00"):
        clock["now"] = "%sT%s:00" % (DAY, at)
        keys = feed_keys(mine)
        assert len(keys) == 2 and keys[1] == "alert-%s-0" % keys[0]                  # itself and its row of the bell: one key for notify.js
        heard.append(keys[0])
    assert heard == ["return-day-20261113-09"] * 2 + ["return-day-20261113-12"] * 2 + ["return-day-20261113-15"] * 2
    item = next(it for it in mine.get("/my-point/feed").get_json()["items"] if it["key"] == "return-day-20261113-15")
    assert item["title"] == "Сегодня возврат!" and "Распечатайте акт" in item["text"] and "5000100" in item["text"]
    sign(conn, card)
    assert feed_keys(mine) and order_oth.return_day(conn, pid, clock["now"])["ids"] == [other]
    sign(conn, other)
    assert feed_keys(mine) == [] and order_oth.return_day(conn, pid, clock["now"]) is None


def test_notify_js_rings_once_for_the_notice_and_its_row_of_the_bell():
    from pathlib import Path
    source = (Path(routes_mypoint.__file__).parent / "static" / "notify.js").read_text(encoding="utf-8")
    assert "(?:question|return-day)" in source


@pytest.mark.parametrize("view", ["old", "new"])
def test_the_bell_and_the_workplace_show_it_marked_important(tmp_path, clock, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    in_panel(conn, pid)
    agreed(conn, card)
    mine = manager(app, conn, pid)
    assert "data-return-day" not in mine.get("/my-point?tab=here").get_data(as_text=True)
    order_oth.tell_return_day(conn, clock["now"])
    for tab in ("here", "return"):                                                    # on every tab of «Рабочее место»
        page = mine.get("/my-point?tab=%s" % tab).get_data(as_text=True)
        assert "data-return-day" in page and TEXT in page and ">Важное<" in page and "№ 5000100" in page
    rows = [it for it in mine.get("/alerts/feed").get_json()["items"] if str(it["id"]).startswith("return-day-")]
    assert len(rows) == 1 and rows[0]["title"] == "Важное · Сегодня возврат!" and "5000100" in rows[0]["text"]
    assert rows[0]["href"] == "/my-point?tab=return"
    assert "Важное · Сегодня возврат!" in mine.get("/alerts").get_data(as_text=True)
    assert not [it for it in owner.get("/alerts/feed").get_json()["items"] if str(it["id"]).startswith("return-day-")]     # the point's only
    sign(conn, card)
    assert "data-return-day" not in mine.get("/my-point?tab=return").get_data(as_text=True)
    assert not [it for it in mine.get("/alerts/feed").get_json()["items"] if str(it["id"]).startswith("return-day-")]
