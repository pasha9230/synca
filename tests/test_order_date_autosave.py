"""«Дата доставки» of the order window saves by itself, as the client and the phone (the owner 2026-11-11): «Оформить в MIM» takes the
stored date (webmim_create.fields), so a date changed and placed at once went with the old one. The record is that of «Сохранить»; a
wrong date is refused and nothing is saved; nothing is sent to the client."""
from __future__ import annotations

from test_order_autosave import autosave, journal
from test_order_window import created, setup
from panel_helpers import post
from tires_sync import orders, webmim_create
from tires_sync import order_points as points


def test_the_date_saves_by_itself_with_the_record_and_goes_to_mim(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    a = points.save_point(conn, {"name": "Альфа"})
    cid = created(post(client, "/orders/new", tok, client_name="Иван", point_id=str(a), delivery_date="2026-11-20"))
    before = journal(conn, cid)
    sent_before = conn.execute("SELECT COUNT(*) FROM order_event WHERE order_id = ?", (cid,)).fetchone()[0]
    r = autosave(client, cid, tok, "delivery_date", "2026-11-25")
    assert r.status_code == 200 and r.get_json() == {"ok": True, "value": "2026-11-25"}
    assert orders.get_card(conn, cid)["delivery_date"] == "2026-11-25"
    assert journal(conn, cid)[len(before):] == ["%s: 2026-11-25" % orders.LABELS["delivery_date"]]
    # one record, the field's: no message, no other event of the order
    assert conn.execute("SELECT COUNT(*) FROM order_event WHERE order_id = ?", (cid,)).fetchone()[0] == sent_before + 1
    assert webmim_create.fields(conn, orders.get_card(conn, cid))["date"] == "2026-11-25"


def test_the_record_is_that_of_save(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    one, two = (created(post(client, "/orders/new", tok, client_name="Иван")) for _ in range(2))
    assert autosave(client, one, tok, "delivery_date", "2026-12-01").status_code == 200
    post(client, "/orders/%d/save" % two, tok, delivery_date="2026-12-01")
    assert journal(conn, one)[-1] == journal(conn, two)[-1]


def test_a_wrong_date_is_refused_and_nothing_is_saved(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван", delivery_date="2026-11-20"))
    before = journal(conn, cid)
    for value in ("20.11.2026", "2026-13-01", "0002-11-20", "завтра"):
        r = autosave(client, cid, tok, "delivery_date", value)
        assert r.status_code == 400 and r.get_json()["ok"] is False and r.get_json()["error"], value
    assert orders.get_card(conn, cid)["delivery_date"] == "2026-11-20"
    assert journal(conn, cid) == before


def test_the_field_autosaves_only_when_left_or_changed(tmp_path, monkeypatch):
    # a date typed by keys passes through half dates: no save on the pause of «input», only on change and leaving (data-autosave-whole)
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван"))
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    assert 'type="date" name="delivery_date" value="" data-autosave data-autosave-whole' in page
