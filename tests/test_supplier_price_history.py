"""История цены поставщика и своя цена позиции, которой нет у поставщика (слово владельца 2026-11-05): «Если ранее была у поставщика, мы цену свою
все равно можем выставить, когда она пропала из наличия, так же можно вести историю как менялась цена у поставщика. Если у поставщика в наличии
не было, то мы менять не можем цену». История: запись только при изменении закупки, первый цикл только запоминает, видит её тот, кому можно
видеть закупку (право «money»). Своя цена сама объявление не создаёт."""
from __future__ import annotations

import sqlite3

from helpers import make_offer
from panel_helpers import login, post
from test_manual_prices import GONE, _flash, _gone_app, _owner
from test_oth_feed import ads_of, public, run
from test_panel_stale_ux import app_of, stale_setup
from test_roles import person
from test_stock_pass import AFTER, FIVE, prepared
from tires_sync import catalog_archive, store
from tires_sync.webmim import ParseResult


def _cycle(conn, at, **prices):
    catalog_archive.record(conn, ParseResult(offers={g: make_offer(goods_id=g, base_price=p) for g, p in prices.items()}), at)


def _history(conn, goods_id):
    return [(r["at"], r["was"], r["price"]) for r in catalog_archive.price_history(conn, goods_id)[0]]


# --- запись -------------------------------------------------------------------------------------------------------------------------

def test_the_first_cycle_only_remembers_and_a_row_is_written_only_when_the_purchase_changed(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    _cycle(conn, "2026-11-05T09:00:00")                                  # ни одной позиции (поставщики недоступны) — не начало истории
    assert catalog_archive.price_history(conn, "a")[1] == ""
    conn.execute("INSERT INTO catalog_item (goods_id, data, first_seen, last_seen, last_price) VALUES ('a', '{}', 'x', 'x', 4000)")
    conn.commit()                                       # архив был и до истории: его закупка — не «было» первого цикла
    _cycle(conn, "2026-11-05T10:00:00", a=5000.0, b=3000.0)
    assert _history(conn, "a") == [] and catalog_archive.price_history(conn, "a")[1] == "2026-11-05T10:00:00"
    _cycle(conn, "2026-11-05T11:00:00", a=5000.0, b=3000.0)                     # не изменилась — записи нет
    _cycle(conn, "2026-11-05T12:00:00", a=5200.0, b=3000.0, c=1000.0)           # новая позиция c — без «было»
    _cycle(conn, "2026-11-05T13:00:00", b=0.0, c=1000.0)                        # a пропала; у b закупка неизвестна — не запись
    _cycle(conn, "2026-11-05T14:00:00", a=4900.0, b=3100.0, c=1000.0)           # a вернулась с другой ценой; b: от неизвестной — не запись
    assert _history(conn, "a") == [("2026-11-05T14:00:00", 5200.0, 4900.0), ("2026-11-05T12:00:00", 5000.0, 5200.0)]
    assert _history(conn, "b") == [] and _history(conn, "c") == []
    assert catalog_archive.price_history(conn, "a")[1] == "2026-11-05T10:00:00"   # с какого цикла ведётся — не меняется


def test_the_list_is_limited_newest_first(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    for n in range(catalog_archive.HISTORY_SHOWN + 5):
        _cycle(conn, "2026-11-05T%02d:00:00" % n, a=1000.0 + n)
    rows = _history(conn, "a")
    assert len(rows) == catalog_archive.HISTORY_SHOWN and rows[0][2] == 1000.0 + catalog_archive.HISTORY_SHOWN + 4


def test_the_table_is_made_on_a_database_that_did_not_have_it_and_nothing_else_is_touched(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    _cycle(conn, "2026-11-05T10:00:00", a=5000.0)
    conn.execute("DROP TABLE supplier_price_change")
    conn.commit()
    conn.close()
    conn = store.connect(tmp_path / "t.db")
    assert conn.execute("SELECT goods_id, last_price FROM catalog_item").fetchall() == [("a", 5000.0)]
    assert conn.execute("SELECT COUNT(*) FROM supplier_price_change").fetchone()[0] == 0


def test_the_cycle_writes_the_history_and_an_own_price_alone_does_not_put_a_gone_position_on_avito(tmp_path):
    prepared(tmp_path, price_overrides={"1:102": {"price": 7000}, "2:102": {"pct": 10}})
    assert run(tmp_path, FIVE) == 0
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.row_factory = sqlite3.Row
    before = conn.execute("SELECT last_price FROM catalog_item WHERE goods_id = '103'").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM supplier_price_change").fetchone()[0] == 0       # первый цикл только запоминает
    conn.close()
    assert run(tmp_path, AFTER) == 0                                     # 102 ушла, своего остатка нет; у 103 другая закупка
    assert "102" not in ads_of(public(tmp_path, 1)) and "102_a2" not in ads_of(public(tmp_path, 2))
    conn = store.connect(tmp_path / "t.db")
    rows = conn.execute("SELECT goods_id, was, price FROM supplier_price_change").fetchall()
    assert rows == [("103", before, 6000.0)]


# --- карточка: показ и права ----------------------------------------------------------------------------------------------------------

def test_the_card_shows_the_history_to_who_may_see_the_purchase_only(tmp_path):
    app, conn, _ids = _gone_app(tmp_path)
    _cycle(conn, "2026-11-05T12:00:00", **{GONE: 4000.0})                     # пропавшая вернулась ненадолго…
    _cycle(conn, "2026-11-05T13:00:00", **{GONE: 4400.0})
    _cycle(conn, "2026-11-05T14:00:00", **{"100": 5000.0})                    # …и ушла снова
    for role in ("owner", "partner"):
        client = _owner(app)[0] if role == "owner" else person(app, role)[0]
        page = client.get("/catalog/item/%s" % GONE).get_data(as_text=True).replace("\xa0", " ")
        assert "data-price-history" in page and "4 000 ₽" in page and "4 400 ₽" in page, role
    for role in ("chats", "viewer"):                                         # каталог видят, закупку — нет
        client = person(app, role)[0]
        page = client.get("/catalog/item/%s" % GONE).get_data(as_text=True)
        assert client.get("/catalog/item/%s" % GONE).status_code == 200
        assert "data-price-history" not in page and "4 400" not in page, role


def test_a_card_without_changes_says_since_when_it_is_kept(tmp_path):
    app, conn, _ids = _gone_app(tmp_path)
    page = _owner(app)[0].get("/catalog/item/%s" % GONE).get_data(as_text=True)
    assert "data-price-history" in page and "Закупка не менялась" in page


# --- позиция, которой никогда не было у поставщика ------------------------------------------------------------------------------------

def test_a_position_never_at_the_supplier_has_the_own_price_closed_with_a_clear_reason(tmp_path):
    stale_setup(tmp_path)                                                    # s2-5: старый файл второго поставщика, только для просмотра
    conn = store.connect(tmp_path / "t.db")
    assert catalog_archive.item_info(conn, "s2-5") is None
    first = conn.execute("SELECT id FROM account ORDER BY id").fetchone()[0]
    conn.close()
    client = app_of(tmp_path).test_client()
    tok = login(client)
    page = client.get("/catalog/item/s2-5").get_data(as_text=True)
    assert "data-own-closed" in page and "только для просмотра" in page and "её нет и не было" in page
    assert "никогда не было у поставщика" not in page                     # она у поставщика есть, только не в нашей выгрузке
    assert "data-price-history" not in page                               # её закупку цикл не пишет: истории нет
    assert 'action="/catalog/item/s2-5/own-price"' not in page
    r = post(client, "/catalog/item/s2-5/own-price", tok, kind="price", value="4500", a=[str(first)], action="save")
    said = _flash(client, r)
    assert "по которой считаются цены: она показана только для просмотра" in said and "никогда не было у поставщика" not in said
    r = post(client, "/catalog/item/s2-77/own-price", tok, kind="price", value="4500", a=[str(first)], action="save")
    assert "нет в выгрузке и не было в ней раньше" in _flash(client, r)       # Id, которого не было нигде
    conn = store.connect(tmp_path / "t.db")
    from tires_sync import staging
    assert not (staging.summary(conn)["changes"] or {}).get("price_overrides")


def test_the_closed_note_is_not_shown_to_a_role_without_the_purchase(tmp_path):
    stale_setup(tmp_path)
    client = person(app_of(tmp_path), "chats")[0]
    page = client.get("/catalog/item/s2-5").get_data(as_text=True)
    assert "data-own-closed" not in page and "data-price-history" not in page


def test_a_position_known_but_not_marked_gone_yet_is_told_so_not_never(tmp_path):
    app, conn, (first, _) = _gone_app(tmp_path)
    conn.execute("UPDATE catalog_item SET gone_since = '' WHERE goods_id = ?", (GONE,))    # пропажа ещё не принята (обвал, битый файл)
    conn.commit()
    client, tok = _owner(app)
    r = post(client, "/catalog/item/%s/own-price" % GONE, tok, kind="price", value="4500", a=[str(first)], action="save")
    said = _flash(client, r)
    assert "Позиции сейчас нет в выгрузке" in said and "не было" not in said
