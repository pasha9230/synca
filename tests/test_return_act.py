"""The owner 2026-11-12: the act of a return as a file. The office (the owner and the partner, as «На возврат» of the page «На ОТХ») attaches
a PDF to a pair «point + date of the return» on the page «На ОТХ»: a PDF only, up to 1.9 MB, a second file of the pair replaces the first,
«Удалить» removes it; every change is in the journal. The point downloads the act of its own point only («Скачать акт»: on the plaque of the
return, by its orders of that date, in «Сегодня возврат!»); another point's file is a 404."""
from __future__ import annotations

import io
from datetime import date

import pytest

from test_mypoint import manager
from test_point_oth_sell import setup
from test_point_return_tabs import to_return_stage
from tires_sync import order_oth, order_points as points, orders, oth_return_act as acts, store, users
from tires_sync.panel import MOSCOW_AUDIT, routes_alerts, routes_mypoint

DAY = "2026-11-13"
NOW = "2026-11-12T15:00:00"
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
PDF2 = b"%PDF-1.7\nanother act\n%%EOF\n"


@pytest.fixture
def clock(monkeypatch):
    moment = {"now": DAY + "T09:05:00"}
    monkeypatch.setattr(routes_mypoint, "local_iso", lambda: moment["now"])
    monkeypatch.setattr(routes_alerts, "local_iso", lambda: moment["now"])
    return moment


def person(app, conn, login, role, point_id=None):
    users.add_user(conn, login, "long-password-1", "2026-10-13T12:00:00", role=role, point_id=point_id)
    conn.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = login
        session["epoch"] = store.get_session_epoch(conn, login)
        session["csrf"] = "tok"
    return client


def upload(client, point, day=DAY, data=PDF, name="act.pdf"):
    return client.post("/oth/return-act", data={"csrf": "tok", "point": str(point), "return_date": day, "file": (io.BytesIO(data), name)},
                       content_type="multipart/form-data")


def rows(conn):
    return conn.execute("SELECT id, point_id, return_date, name, size, data FROM oth_return_act ORDER BY id").fetchall()


def journal(conn):
    return [r[0] for r in conn.execute("SELECT action FROM audit WHERE action LIKE 'oth_act_file_%' ORDER BY id")]


def agreed(conn, card, day=DAY):
    to_return_stage(conn, card)
    order_oth.agree(conn, orders.get_card(conn, card), date.fromisoformat("2026-11-10"), "2026-11-10T10:00:00", "owner", day)


def test_the_office_attaches_replaces_and_deletes_the_act_and_the_journal_keeps_it(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    assert 'action="/oth/return-act"' in owner.get("/oth").get_data(as_text=True)
    assert upload(owner, pid).status_code == 302
    [(act, point, day, name, size, data)] = rows(conn)
    assert (point, day, name, size, bytes(data)) == (pid, DAY, "act.pdf", len(PDF), PDF)
    assert upload(owner, pid, data=PDF2, name="new.pdf").status_code == 302          # the same pair: the file is replaced
    [(again, _, _, name, size, data)] = rows(conn)
    assert again == act and (name, bytes(data)) == ("new.pdf", PDF2)
    assert upload(owner, pid, day="2026-11-16").status_code == 302                   # another date: its own file
    assert len(rows(conn)) == 2
    page = owner.get("/oth").get_data(as_text=True)
    assert "new.pdf" in page and 'action="/oth/return-act/%d/delete"' % act in page and 'href="/oth/return-act/%d"' % act in page
    got = owner.get("/oth/return-act/%d" % act)
    assert got.status_code == 200 and got.mimetype == "application/pdf" and got.data == PDF2
    assert got.headers["X-Content-Type-Options"] == "nosniff" and "attachment" in got.headers["Content-Disposition"]
    assert owner.post("/oth/return-act/%d/delete" % act, data={"csrf": "tok"}).status_code == 302
    assert [r[2] for r in rows(conn)] == ["2026-11-16"] and owner.get("/oth/return-act/%d" % act).status_code == 404
    assert journal(conn) == ["oth_act_file_added", "oth_act_file_replaced", "oth_act_file_added", "oth_act_file_deleted"]
    assert {"oth_act_file_added", "oth_act_file_replaced", "oth_act_file_deleted"} <= MOSCOW_AUDIT
    detail = conn.execute("SELECT detail FROM audit WHERE action = 'oth_act_file_replaced'").fetchone()[0]
    assert '"date": "%s"' % DAY in detail and "new.pdf" in detail


@pytest.mark.parametrize("data, name, day, point", [
    (b"GIF89a not a pdf", "act.pdf", DAY, None),                                      # not a PDF inside
    (PDF, "act.txt", DAY, None),                                                      # not a PDF by its name
    (b"", "act.pdf", DAY, None),                                                      # no file
    (PDF + b"0" * 1_900_000, "act.pdf", DAY, None),                                   # over 1.9 MB
    (PDF, "act.pdf", "13.11.2026", None),                                             # not a date
    (PDF, "act.pdf", DAY, 99999),                                                     # no such point
])
def test_what_is_not_a_small_pdf_of_a_point_and_a_date_is_refused(tmp_path, data, name, day, point):
    app, owner, conn, pid, card = setup(tmp_path)
    assert upload(owner, point or pid, day=day, data=data, name=name).status_code == 302
    assert rows(conn) == [] and journal(conn) == []
    with pytest.raises(acts.ActError):
        acts.save(conn, point or pid, day, name, data, NOW, "owner")


def test_the_largest_file_allowed_is_taken(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    data = PDF + b"0" * (acts.ACT_MAX - len(PDF))
    assert acts.ACT_MAX == 1_900_000 and acts.save(conn, pid, DAY, "act.pdf", data, NOW, "owner")[1] is False
    assert rows(conn)[0][4] == acts.ACT_MAX


def test_only_the_owner_and_the_partner_attach_and_delete(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    assert upload(person(app, conn, "partner1", "partner"), pid).status_code == 302
    [(act, *_)] = rows(conn)
    for login, role in (("chats1", "chats"), ("viewer1", "viewer")):
        other = person(app, conn, login, role)
        assert upload(other, pid, data=PDF2).status_code == 403
        assert other.post("/oth/return-act/%d/delete" % act, data={"csrf": "tok"}).status_code == 403
        assert 'action="/oth/return-act"' not in other.get("/oth").get_data(as_text=True)
    mine = manager(app, conn, pid)
    assert upload(mine, pid, data=PDF2).status_code == 403
    assert mine.post("/oth/return-act/%d/delete" % act, data={"csrf": "tok"}).status_code == 403
    assert mine.get("/oth/return-act/%d" % act).status_code == 403                    # the office's address; the point has its own
    assert bytes(rows(conn)[0][5]) == PDF and journal(conn) == ["oth_act_file_added"]
    assert owner.post("/oth/return-act/%d/delete" % act, data={}).status_code in (400, 403)      # no CSRF: nothing
    assert len(rows(conn)) == 1


def test_a_point_downloads_the_act_of_its_own_point_only(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    other = points.save_point(conn, dict(name="Гамма", channel="panel"))
    act, _ = acts.save(conn, pid, DAY, "act.pdf", PDF, NOW, "owner")
    foreign, _ = acts.save(conn, other, DAY, "gamma.pdf", PDF2, NOW, "owner")
    mine, theirs = manager(app, conn, pid), manager(app, conn, other, "delta")
    got = mine.get("/my-point/return-act/%d" % act)
    assert got.status_code == 200 and got.mimetype == "application/pdf" and got.data == PDF
    assert got.headers["X-Content-Type-Options"] == "nosniff" and got.headers["Cache-Control"] == "no-store"
    assert mine.get("/my-point/return-act/%d" % foreign).status_code == 404           # another point's id
    assert theirs.get("/my-point/return-act/%d" % act).status_code == 404
    assert theirs.get("/my-point/return-act/%d" % foreign).data == PDF2
    assert mine.get("/my-point/return-act/999999").status_code == 404
    assert app.test_client().get("/my-point/return-act/%d" % act).status_code == 302  # signed out: the login page
    assert owner.get("/my-point/return-act/%d?p=%d" % (foreign, other)).status_code == 200      # the office looks at the point it picked
    assert owner.get("/my-point/return-act/%d?p=%d" % (foreign, pid)).status_code == 404


@pytest.mark.parametrize("view", ["old", "new"])
def test_the_point_sees_the_button_on_the_plaque_and_in_todays_notice(tmp_path, clock, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    conn.execute("UPDATE point SET channel = 'panel' WHERE id = ?", (pid,))
    agreed(conn, card)
    order_oth.tell_return_day(conn, clock["now"])
    mine = manager(app, conn, pid)
    page = mine.get("/my-point?tab=return").get_data(as_text=True)
    assert "data-return-day" in page and "Скачать акт" not in page                    # no file yet: no button
    acts.save(conn, pid, "2026-11-16", "later.pdf", PDF, NOW, "owner")                # of another date: not this order's
    assert "Скачать акт" not in mine.get("/my-point?tab=return").get_data(as_text=True)
    act, _ = acts.save(conn, pid, DAY, "act.pdf", PDF, NOW, "owner")
    page = mine.get("/my-point?tab=return").get_data(as_text=True)
    link = 'href="/my-point/return-act/%d"' % act
    assert page.count(link) == 2 and page.count("Скачать акт") == 2                   # the notice of today and the plaque of the order
    assert page.count(link) == mine.get("/my-point?tab=here").get_data(as_text=True).count(link)     # both are above the tabs
    assert order_oth.sign_act(conn, orders.get_card(conn, card), "Бета", DAY + "T10:00:00", "gamma") in ("return", "signed")
    conn.commit()
    assert "data-return-day" not in mine.get("/my-point?tab=return").get_data(as_text=True)


def test_an_order_given_and_waiting_for_the_amount_keeps_the_button_by_its_card(tmp_path, clock):
    app, owner, conn, pid, card = setup(tmp_path)
    agreed(conn, card)
    conn.execute("UPDATE order_item SET supplier_price = 0 WHERE order_id = ?", (card,))     # the refund cannot be counted: it stays on «На возврат»
    conn.commit()
    act, _ = acts.save(conn, pid, DAY, "act.pdf", PDF, NOW, "owner")
    assert order_oth.sign_act(conn, orders.get_card(conn, card), "Бета", DAY + "T10:00:00", "gamma") == "signed"
    page = manager(app, conn, pid).get("/my-point?tab=return").get_data(as_text=True)
    assert 'id="order-%d"' % card in page and page.count('href="/my-point/return-act/%d"' % act) == 1


def test_the_table_is_only_added(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(oth_return_act)")]
    assert cols == ["id", "point_id", "return_date", "name", "size", "data", "uploaded_at", "uploaded_by"]
    assert "oth_return_act" in store.SCHEMA and "DROP" not in store.SCHEMA.split("oth_return_act")[1][:600]
