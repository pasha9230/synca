"""The permanent base of catalog positions: a position the supplier took away stays known (T5)."""
from __future__ import annotations

from helpers import make_offer
from tires_sync import catalog_archive as archive, store
from tires_sync.webmim import ParseResult

T1, T2, T3 = "2026-10-19T10:00:00", "2026-10-19T18:00:00", "2026-10-20T10:00:00"


def parsed(*offers, unavailable=()):
    return ParseResult(offers={o.goods_id: o for o in offers}, unavailable=set(unavailable))


def many(count, prefix=""):
    return [make_offer(goods_id="%s%d" % (prefix, 1000 + i), code="C%s%d" % (prefix, i), name="Шина %d" % i) for i in range(count)]


def test_a_position_that_left_the_unload_stays_known_with_its_data_and_the_date(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    keep, go = make_offer(goods_id="1", code="K1"), make_offer(goods_id="2", code="G2", name="Ушедшая шина", stock=7, base_price=4100.0)
    archive.record(conn, parsed(keep, go), T1)
    assert archive.gone_offers(conn) == []
    stats = archive.record(conn, parsed(keep), T2)
    [(offer, info)] = archive.gone_offers(conn)
    assert stats["gone"] == 1 and offer.goods_id == "2" and offer.name == "Ушедшая шина" and offer.code == "G2" and offer.stock == 0
    assert info == {"gone_since": T2, "last_stock": 7, "last_price": 4100.0, "last_seen": T1, "first_seen": T1}
    archive.record(conn, parsed(keep), T3)
    assert archive.gone_offers(conn)[0][1]["gone_since"] == T2                  # the date of the first miss stays


def test_a_position_that_came_back_is_not_gone_and_is_not_doubled(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    one, two = make_offer(goods_id="1"), make_offer(goods_id="2")
    archive.record(conn, parsed(one, two), T1)
    archive.record(conn, parsed(one), T2)
    stats = archive.record(conn, parsed(one, make_offer(goods_id="2", stock=3)), T3)
    assert stats["returned"] == 1 and archive.gone_offers(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM catalog_item").fetchone()[0] == 2
    assert conn.execute("SELECT first_seen, last_seen, last_stock FROM catalog_item WHERE goods_id = '2'").fetchone() == (T1, T3, 3)


def test_a_supplier_whose_unload_was_not_usable_marks_nothing(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    archive.record(conn, parsed(make_offer(goods_id="1"), make_offer(goods_id="s2-5")), T1)
    stats = archive.record(conn, parsed(make_offer(goods_id="1"), unavailable=[2]), T2)
    assert archive.gone_offers(conn) == [] and stats["gone"] == 0


def test_an_empty_unload_or_a_collapsed_one_marks_nothing(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    archive.record(conn, parsed(*many(40)), T1)
    assert archive.record(conn, parsed(), T2)["gone"] == 0                      # nothing came: a broken file, not a empty shop
    stats = archive.record(conn, parsed(*many(10)), T2)                         # 30 of 40 vanished at once: not believed
    assert stats["gone"] == 0 and stats["suspicious"] == [1] and archive.gone_offers(conn) == []
    stats = archive.record(conn, parsed(*many(35)), T3)                         # a normal drop is believed
    assert stats["gone"] == 5 and len(archive.gone_offers(conn)) == 5


def test_the_second_supplier_is_judged_apart_from_the_first(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    archive.record(conn, parsed(*many(30), *many(30, "s2-")), T1)
    stats = archive.record(conn, parsed(*many(28), *many(30, "s2-")), T2)       # two of the first supplier left, the second is whole
    assert stats["gone"] == 2 and {o.goods_id for o, _ in archive.gone_offers(conn)} == {"1028", "1029"}


def test_the_table_is_made_on_a_database_that_did_not_have_it(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    store.connect(path).close()
    raw = sqlite3.connect(path)
    raw.execute("DROP TABLE catalog_item")
    raw.commit()
    raw.close()
    conn = store.connect(path)
    archive.record(conn, parsed(make_offer(goods_id="1")), T1)
    assert conn.execute("SELECT COUNT(*) FROM catalog_item").fetchone()[0] == 1


def test_a_supplier_flagged_unavailable_is_skipped_even_when_part_of_his_positions_came(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    archive.record(conn, parsed(make_offer(goods_id="s2-5"), make_offer(goods_id="s2-6")), T1)
    archive.record(conn, parsed(make_offer(goods_id="s2-5"), unavailable=[2]), T2)
    assert archive.gone_offers(conn) == []


def test_a_supplier_with_no_position_at_all_in_this_unload_marks_nothing_while_the_other_is_judged(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    archive.record(conn, parsed(make_offer(goods_id="1"), make_offer(goods_id="2"), make_offer(goods_id="s2-5")), T1)
    archive.record(conn, parsed(make_offer(goods_id="1")), T2)                  # the second supplier sent nothing, the first lost «2»
    assert [o.goods_id for o, _ in archive.gone_offers(conn)] == ["2"]


def test_a_collapse_is_believed_when_it_repeats_in_enough_cycles_in_a_row_and_forgotten_when_it_does_not(tmp_path):
    assert archive.BELIEVE_AFTER == 18                                                                # about 18 hours with the cycle every hour (2026-10-20)
    conn = store.connect(str(tmp_path / "x.db"))
    archive.record(conn, parsed(*many(40)), T1)
    for _ in range(archive.BELIEVE_AFTER - 1):
        assert archive.record(conn, parsed(*many(10)), T2)["gone"] == 0                               # not believed yet
    archive.record(conn, parsed(*many(40)), T2)                                                       # the supplier is whole again: the count starts over
    for _ in range(archive.BELIEVE_AFTER - 1):
        assert archive.record(conn, parsed(*many(10)), T3)["gone"] == 0
    stats = archive.record(conn, parsed(*many(10)), T3)                                               # the last one in a row
    assert stats["gone"] == 30 and stats["believed"] == [1] and len(archive.gone_offers(conn)) == 30
    assert archive.record(conn, parsed(*many(10)), T3)["gone"] == 0 and len(archive.gone_offers(conn)) == 30


def test_a_position_the_supplier_still_sends_with_a_broken_field_is_not_gone(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    archive.record(conn, parsed(make_offer(goods_id="1"), make_offer(goods_id="2")), T1)
    result = parsed(make_offer(goods_id="1"))
    result.issues["2"] = ["bad_price"]
    assert archive.record(conn, result, T2)["gone"] == 0 and archive.gone_offers(conn) == []


def test_a_row_that_cannot_be_read_any_more_does_not_take_the_list_down(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    archive.record(conn, parsed(make_offer(goods_id="1"), make_offer(goods_id="2")), T1)
    archive.record(conn, parsed(make_offer(goods_id="1")), T2)
    conn.execute("UPDATE catalog_item SET data = '{broken' WHERE goods_id = '2'")
    conn.commit()
    assert archive.gone_offers(conn) == []


def test_the_cycle_alerts_about_a_collapse_and_never_stops_on_the_trouble_of_the_base(tmp_path, monkeypatch, capsys):
    from tires_sync import cli
    conn = store.connect(str(tmp_path / "x.db"))
    sent = []
    monkeypatch.setattr(cli, "_alert", lambda c, box, text, **kw: sent.append(text) or True)
    cli._record_archive(conn, None, parsed(*many(40)))
    cli._record_archive(conn, None, parsed(*many(10)))
    assert len(sent) == 1 and "поставщика №1" in sent[0] and "пропала большая часть" in sent[0] and "ещё в 17 циклах" in sent[0]
    for _ in range(archive.BELIEVE_AFTER - 2):
        cli._record_archive(conn, None, parsed(*many(10)))
    assert len(sent) == 1                                                       # told once, not every hour
    assert "ещё в 1 циклах" in capsys.readouterr().err                         # the journal has the real number of cycles left
    cli._record_archive(conn, None, parsed(*many(10)))
    assert len(sent) == 2 and "помечены в каталоге" in sent[1]
    conn.execute("DROP TABLE catalog_item")                                    # the base is broken: only a warning, the cycle goes on
    cli._record_archive(conn, None, parsed(*many(10)))
    assert "база позиций каталога не обновлена" in capsys.readouterr().err
