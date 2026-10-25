from helpers import catalog_xml, commodity_xml
from test_same_tire import setup, run, ids, NOW, two_lots
from tires_sync import mirror_feed, store


def test_an_inherited_id_does_not_flip_back_to_the_alias_in_an_aliased_account(tmp_path):
    conn, account = setup(tmp_path)
    mirror_feed.set_alias_scheme(conn, account, True, NOW, "t")
    other = commodity_xml(NNOMMODIF="200", NREST="9", SMNFCODE="OTHER", SLOAD="91", SWIDTH="195")
    newlot = catalog_xml(commodity_xml(NNOMMODIF="125", NREST="8", SMNFCODE="LOT-B", SLOAD="94", SWIDTH="205"), other)
    run(conn, tmp_path, catalog_xml(commodity_xml(NNOMMODIF="106", NREST="8", SMNFCODE="LOT-A", SLOAD="94", SWIDTH="205"), other))
    a = mirror_feed.alias_id(account, "106")
    print("day14", ids(tmp_path, conn, account), "alias106", a)
    run(conn, tmp_path, catalog_xml(other), day=15)
    print("day15", ids(tmp_path, conn, account), list(conn.execute("select goods_id, ad_id from retired_ad")))
    run(conn, tmp_path, newlot, day=16)
    d16 = ids(tmp_path, conn, account)
    print("day16", d16)
    run(conn, tmp_path, newlot, day=17)
    d17 = ids(tmp_path, conn, account)
    print("day17", d17, "alias125", mirror_feed.alias_id(account, "125"))
    assert d16 == d17


def test_a_same_tire_group_does_not_flip_ids_in_an_aliased_account(tmp_path):
    conn, account = setup(tmp_path)
    mirror_feed.set_alias_scheme(conn, account, True, NOW, "t")
    run(conn, tmp_path, two_lots(8, 0))
    d1 = ids(tmp_path, conn, account); print("d1", d1)
    run(conn, tmp_path, two_lots(0, 50), day=15)
    d2 = ids(tmp_path, conn, account); print("d2", d2)
    run(conn, tmp_path, two_lots(0, 50), day=16)
    d3 = ids(tmp_path, conn, account); print("d3", d3)
    run(conn, tmp_path, two_lots(0, 50), day=17)
    d4 = ids(tmp_path, conn, account); print("d4", d4)
    assert d2 == d3 == d4


def test_a_saved_ad_keeps_its_alias_id(tmp_path):
    from datetime import date
    from test_id_alias import UNLOAD, REF as R2, two_accounts, ids as ids2
    from tires_sync import cycle, publishing
    conn, first, second = two_accounts(tmp_path)
    from tires_sync import connections
    for n in (first, second):
        connections.set_live(conn, n, True, NOW, "o")
    conn.commit()
    mirror_feed.set_alias_scheme(conn, second, True, NOW, "t")
    cycle.run_for_accounts(conn, UNLOAD, R2, date(2026, 10, 14), tmp_path / "feed")
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    a = ids2(tmp_path, conn, second); print("a", a)
    print(list(conn.execute("select goods_id from published_state where account_id=?", (second,))))
    conn.execute("UPDATE published_state SET location='own' WHERE account_id=?", (second,)); conn.commit()
    cycle.run_for_accounts(conn, UNLOAD, R2, date(2026, 10, 15), tmp_path / "feed")
    b = ids2(tmp_path, conn, second); print("b", b)
    assert a == b


def test_a_saved_renewed_ad_does_not_get_the_suffix_twice(tmp_path):
    from datetime import date
    from test_id_alias import UNLOAD, REF as R2, two_accounts, ids as ids2
    from tires_sync import cycle, publishing, connections
    conn, first, second = two_accounts(tmp_path)
    for n in (first, second):
        connections.set_live(conn, n, True, NOW, "o")
    conn.commit()
    cycle.run_for_accounts(conn, UNLOAD, R2, date(2026, 10, 14), tmp_path / "feed")
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    mirror_feed.reissue_own(conn, second, ["100"], "20261015", NOW, "t")
    conn.execute("UPDATE published_state SET price = price + 1 WHERE account_id=?", (second,)); conn.commit()
    cycle.run_for_accounts(conn, UNLOAD, R2, date(2026, 10, 15), tmp_path / "feed")
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    a = ids2(tmp_path, conn, second); print("a", a)
    print([r[0][:200] for r in conn.execute("select snapshot from published_state where account_id=? and goods_id='100'", (second,))])
    conn.execute("UPDATE published_state SET location='own' WHERE account_id=?", (second,)); conn.commit()
    cycle.run_for_accounts(conn, UNLOAD, R2, date(2026, 10, 16), tmp_path / "feed")
    b = ids2(tmp_path, conn, second); print("b", b)
    assert a == b


def test_a_saved_renewed_aliased_ad_keeps_its_id(tmp_path):
    from datetime import date
    from test_id_alias import UNLOAD, REF as R2, two_accounts, ids as ids2
    from tires_sync import connections, cycle, publishing
    conn, first, second = two_accounts(tmp_path)
    for n in (first, second):
        connections.set_live(conn, n, True, NOW, "o")
    conn.commit()
    mirror_feed.set_alias_scheme(conn, second, True, NOW, "t")
    cycle.run_for_accounts(conn, UNLOAD, R2, date(2026, 10, 14), tmp_path / "feed")
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    code = sorted(mirror_feed.adopted_maps(conn, second)[0])[0]
    mirror_feed.reissue_own(conn, second, [code], "20261015", NOW, "t")
    cycle.run_for_accounts(conn, UNLOAD, R2, date(2026, 10, 15), tmp_path / "feed")
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    a = ids2(tmp_path, conn, second)
    assert any(i.endswith("_r20261015") for i in a)
    conn.execute("UPDATE published_state SET location='own' WHERE account_id=?", (second,))
    conn.commit()
    cycle.run_for_accounts(conn, UNLOAD, R2, date(2026, 10, 16), tmp_path / "feed")
    assert ids2(tmp_path, conn, second) == a
