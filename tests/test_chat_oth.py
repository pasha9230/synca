from types import SimpleNamespace

from helpers import make_offer
from test_chats_pages import NOW, setup
from tires_sync import catalog_archive, chat_oth, chats, order_points, oth_manual


def test_chat_highlights_archived_oth_tire_from_customer_message(tmp_path):
    app, client, _, conn, _ = setup(tmp_path)
    chat = next(c for c in chats.list_chats(conn) if c["chat_id"] == "c1")
    point = order_points.save_point(conn, {"name": "Север", "channel": "panel"})
    tire = make_offer(goods_id="201", name="Ikon Character Ice 5 195/55 R16",
                      model_raw="Character Ice 5", width=195)
    catalog_archive.record(conn, SimpleNamespace(offers={"201": tire}, supplier_ok={1}), NOW)
    oth_manual.set_qty(conn, "201", point, 4, NOW, "office")
    conn.execute("UPDATE avito_chat SET item_title = ? WHERE id = ?", ("Объявление 225/45 R17", chat["id"]))
    conn.execute("UPDATE avito_message SET text = ? WHERE chat_row = ?",
                 ("Ищу Ikon Character Ice 5 195/55 р16", chat["id"]))
    conn.commit()

    page = client.get("/chats/%d" % chat["id"]).get_data(as_text=True)
    assert "Шина есть на ОТХ" in page
    assert "из сообщения" in page
    assert "Ikon Character Ice 5 195/55 R16 · 4 шт. · Север" in page

    oth_manual.set_qty(conn, "201", point, 0, NOW, "office")
    page = client.get("/chats/%d" % chat["id"]).get_data(as_text=True)
    assert "На ОТХ совпадений нет" in page


def test_the_ad_matches_exactly_and_point_users_only_see_their_own_stock(tmp_path):
    _, _, _, conn, _ = setup(tmp_path)
    north = order_points.save_point(conn, {"name": "Север", "channel": "panel"})
    south = order_points.save_point(conn, {"name": "Юг", "channel": "panel"})
    exact = make_offer(goods_id="201", name="Шина из объявления")
    alternative = make_offer(goods_id="202", name="Шина того же размера", model_raw="Другая модель")
    live = {o.goods_id: o for o in (exact, alternative)}
    oth_manual.set_qty(conn, "201", north, 4, NOW, "office")
    oth_manual.set_qty(conn, "202", south, 2, NOW, "office")
    messages = [{"direction": "out", "kind": "text", "text": "Есть 225/45 R17?"}]

    hint = chat_oth.suggest(conn, live, "201", "", messages)
    assert hint["source"] == "из объявления"
    assert hint["exact"] == [{"name": exact.name, "point": "Север", "qty": 4}]
    assert hint["alternatives"] == [{"name": alternative.name, "point": "Юг", "qty": 2}]
    hint = chat_oth.suggest(conn, live, "201", "", messages, south)
    assert not hint["exact"]
    assert hint["alternatives"] == [{"name": alternative.name, "point": "Юг", "qty": 2}]

    message = {"direction": "in", "kind": "text", "text": "Нужна Другая модель"}
    hint = chat_oth.suggest(conn, live, "201", "", [message])
    assert (hint["source"], hint["model"], hint["size"]) == ("из сообщения", "Другая модель", "")
    assert hint["exact"] == [{"name": alternative.name, "point": "Юг", "qty": 2}]
