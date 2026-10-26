"""The check of a feed before publication: every rule of feed_check_rules.json, and the gate in the cycle."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

import pytest

from helpers import catalog_xml, commodity_xml, make_offer, make_settings, settings_dict
from tires_sync import connections, cycle, feed_check, store
from tires_sync.candidates import Candidate
from tires_sync.catalog import parse_reference
from tires_sync.feed import render_ad
from tires_sync.settings_store import init_settings

NOW = "2026-10-14T12:00:00"
REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
TEMPLATE = feed_check.template_tags_of(cycle.TEMPLATE_PATH)


def ad(**changes):
    cand = Candidate("100", make_offer(goods_id="100", stock=12), 4810, "Ikon Tyres", "Test Model")
    node = render_ad(cand, 4810, "P1", make_settings(), date(2026, 10, 14))
    for tag, value in changes.items():
        found = node.find(tag)
        if value is None:
            if found is not None:
                node.remove(found)
        else:
            if found is None:
                found = ET.SubElement(node, tag)
            found.text = value
    return node


def ctx(**kw):
    base = dict(mode="generated", template_tags=TEMPLATE, reference=REF, stocks={"100": 12}, min_stock=4, phone="+70000000000")
    base.update(kw)
    return feed_check.Context(**base)


def rules_of(findings):
    return sorted({f.rule for f in findings})


def test_a_good_ad_passes_every_rule():
    assert feed_check.check_feed([ad()], ctx()) == []


@pytest.mark.parametrize("changes, extra, rule", [
    ({"Quantity": "за 1 шт."}, {}, "template_tags"),
    ({"TargetAudience": "Частные лица"}, {}, "template_tags"),
    ({"Price": "0"}, {}, "price"),
    ({"Title": "Шина  с двойным пробелом"}, {}, "title"),
    ({"Title": "x" * 71}, {}, "title"),
    ({"Title": "Ikon 205/65 109/107 C Character"}, {}, "commercial"),
    ({"Images": None}, {}, "photo"),
    ({"Model": "Нет Такой"}, {}, "model_in_reference"),
    ({"ContactPhone": "+79990000000"}, {}, "phone"),
    ({"Id": "100-x4"}, {}, "pack4"),
    ({}, {"stocks": {"100": 2}}, "stock"),
    ({}, {"stocks": {}}, "stock"),
    ({}, {"other_ids": frozenset({"100"})}, "cross_account_id"),
])
def test_each_rule_catches_its_violation(changes, extra, rule):
    findings = feed_check.check_feed([ad(**changes)], ctx(**extra))
    assert rule in rules_of(findings)
    assert all(f.level == "critical" for f in findings if f.rule == rule)


def test_duplicates_and_an_empty_feed_are_critical():
    assert rules_of(feed_check.check_feed([ad(), ad()], ctx())) == ["duplicate_id", "duplicate_position"] or "duplicate_id" in rules_of(feed_check.check_feed([ad(), ad()], ctx()))
    assert [f.rule for f in feed_check.check_feed([], ctx())] == ["empty_feed"]
    twice = [ad(Id="100_a1"), ad(Id="100_a2")]
    assert "duplicate_position" in rules_of(feed_check.check_feed(twice, ctx()))


def test_a_mirror_is_judged_more_gently_where_the_file_is_not_ours():
    findings = feed_check.check_feed([ad(Title="Ikon 205/65 109/107 C Character", Model="Нет Такой")], ctx(mode="mirror"))
    assert {f.rule: f.level for f in findings} == {"commercial": "warning", "model_in_reference": "warning"}
    bad = feed_check.check_feed([ad(Quantity="за 1 шт.", Price="0")], ctx(mode="mirror"))
    assert {f.rule for f in feed_check.critical(bad)} == {"template_tags", "price"}


def test_a_held_position_is_not_judged_by_stock():
    assert feed_check.check_feed([ad()], ctx(stocks={}, held=frozenset({"100"}))) == []


def test_a_held_position_whose_fresh_stock_fell_below_the_threshold_is_reported_not_stopped():
    for mode in ("generated", "mirror"):
        for kw in (dict(stocks={"100": 2}), dict(stocks={}, known_stocks={"100": 3})):        # an offer, or a row broken elsewhere
            found = feed_check.check_feed([ad()], ctx(mode=mode, held=frozenset({"100"}), **kw))
            assert [(f.rule, f.level, f.ad_id) for f in found] == [("held_low_stock", "warning", "100")] and "порог 4" in found[0].text
    for kw in (dict(stocks={"100": 4}), dict(stocks={}), dict(stocks={}, known_stocks={"100": 9})):     # enough, or not known: as before
        assert feed_check.check_feed([ad()], ctx(held=frozenset({"100"}), **kw)) == []
    assert feed_check.check_feed([ad()], ctx(stocks={"100": 2}, held=frozenset({"100"}), min_stock=2)) == []
    not_held = feed_check.check_feed([ad()], ctx(stocks={}, known_stocks={"100": 2}))          # not held: the rule «stock» as before
    assert [(f.rule, f.level) for f in not_held] == [("stock", "critical")] and "нет у поставщика" in not_held[0].text


OWN = "https://panel.example/photos/%s.jpg" % ("a" * 64)
OTHER = "https://panel.example/photos/%s.jpg" % ("b" * 64)
PREFIX = "https://panel.example/img/tok/"


def with_images(*urls):
    node = ad(Images=None)
    images = ET.SubElement(node, "Images")
    for url in urls:
        ET.SubElement(images, "Image", url=url)
    return node


def test_photo_rule_accepts_own_library_links():
    assert feed_check.check_feed([with_images(OWN, PREFIX + "100.jpg")], ctx(photo_prefix=PREFIX, allowed_photos=frozenset({OWN}))) == []
    assert rules_of(feed_check.check_feed([with_images("https://x.example/a.jpg")], ctx(photo_prefix=PREFIX))) == ["photo"]


def test_photo_rule_rejects_other_accounts_copy():
    for mode in ("generated", "mirror"):
        found = feed_check.check_feed([with_images(OWN, OTHER, PREFIX + "100.jpg")],
                                      ctx(mode=mode, photo_prefix=PREFIX, allowed_photos=frozenset({OWN})))
        assert [(f.rule, f.level) for f in found] == [("own_photo_foreign", "critical")] and OTHER in found[0].text
    off = feed_check.check_feed([with_images(OWN, PREFIX + "100.jpg")], ctx(photo_prefix=PREFIX))     # the switch is off: no library link at all
    assert {f.rule for f in off} == {"photo", "own_photo_foreign"}


def test_the_rules_file_covers_every_rule_the_code_uses():
    rules = feed_check.load_rules()["rules"]
    for rule in ("template_tags", "required_fields", "empty_feed", "duplicate_id", "duplicate_position", "cross_account_id", "stock", "price",
                 "phone", "pack4", "commercial", "model_in_reference", "title", "photo", "shrink",
                 "own_photo_foreign", "held_low_stock", "studs_conflict", "template_missing", "title_age_word", "double_reissue_mark"):
        assert set(rules[rule]) >= {"generated", "mirror", "text"} and rules[rule]["generated"] in ("critical", "warning", "off")


def test_the_cycle_does_not_pass_a_feed_that_breaks_a_critical_rule(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    account = connections.create_account(conn, "Основной", NOW, "o")
    connections.set_avito_identity(conn, account, None, "A", "79000000011")        # the cabinet's phone differs from the general number of the settings
    connections.set_phone_source(conn, account, "file", NOW, "o")                    # ...but the owner keeps the file's phone: not judged
    ok = cycle.run_for_accounts(conn, catalog_xml(commodity_xml(NNOMMODIF="100")), REF, date(2026, 10, 14), tmp_path / "feed")
    assert [r.ok for r in ok] == [True]
    connections.set_phone_source(conn, account, "cabinet", NOW, "o")                 # now the cabinet's phone is the rule: the draft has it (effective settings)
    again = cycle.run_for_accounts(conn, catalog_xml(commodity_xml(NNOMMODIF="100")), REF, date(2026, 10, 14), tmp_path / "feed")
    assert [r.ok for r in again] == [True]


def test_a_mirror_with_a_broken_ad_is_stopped_by_the_gate_and_the_cli_check_reports_it(tmp_path, capsys):
    from tires_sync import mirror_feed
    from tires_sync.cli import main
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    account = connections.create_account(conn, "Чужой", NOW, "o")
    file = '<Ads><Ad><Id>999_1</Id><Title>T</Title><Price>5000</Price><SomeNewTag>1</SomeNewTag><ContactPhone>+79990000001</ContactPhone></Ad></Ads>'.encode()
    mirror_feed.import_feed(conn, account, file, NOW)
    results = cycle.run_for_accounts(conn, catalog_xml(commodity_xml(NNOMMODIF="999")), REF, date(2026, 10, 14), tmp_path / "feed")
    assert [r.ok for r in results] == [False] and "проверка фида не пройдена" in results[0].error and "template_tags" in results[0].error
    conn.close()
    (tmp_path / "unload.xml").write_bytes(catalog_xml(commodity_xml(NNOMMODIF="999")))
    (tmp_path / "reference.txt").write_text('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>', encoding="utf-8")
    code = main(["feed-check", "--db", str(tmp_path / "t.db"), "--unload", str(tmp_path / "unload.xml"), "--reference", str(tmp_path / "reference.txt"),
                 "--feed-root", str(tmp_path / "feed")])
    out = capsys.readouterr().out
    assert code == 3 and "ОШИБКИ" in out and "[critical] template_tags" in out and "SomeNewTag" in out


# ---- «address»: the address of another account in the feed of this one (owner's goal: the accounts must not look like duplicates of each other)

def test_an_address_id_that_belongs_to_another_account_is_a_critical_finding():
    findings = feed_check.check_feed([ad(SellerAddressID="P1")], ctx(foreign_addresses=frozenset({"P1"})))
    assert [(f.rule, f.level) for f in findings] == [("address", "critical")]
    assert feed_check.check_feed([ad(SellerAddressID="P2")], ctx(foreign_addresses=frozenset({"P1"}))) == []   # an address nobody else has: not judged
    assert feed_check.check_feed([ad(SellerAddressID="P1")], ctx()) == []                                       # no foreign set (the primary account, a mirror): not judged
    assert feed_check.check_feed([ad(SellerAddressID="P1")], ctx(mode="mirror", foreign_addresses=frozenset({"P1"}))) == []   # a file that is not ours


def test_the_rules_file_has_the_address_rule():
    rule = feed_check.load_rules()["rules"]["address"]
    assert rule["generated"] == "critical" and rule["mirror"] == "off" and rule["text"]


@pytest.mark.usefixtures("no_own_address")
def test_the_cycle_stops_the_feed_of_a_new_account_that_carries_the_address_of_another(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    first = connections.create_account(conn, "Основной", NOW, "o")
    second = connections.create_account(conn, "Второй", NOW, "o")
    for n in (first, second):
        connections.set_live(conn, n, True, NOW, "o")
    unload = catalog_xml(commodity_xml(NNOMMODIF="100"))
    results = cycle.run_for_accounts(conn, unload, REF, date(2026, 10, 14), tmp_path / "feed")
    assert [r.ok for r in results] == [True, False] and "address" in results[1].error        # no addresses of its own: the general one (P1) is the primary's
    connections.save_addresses(conn, second, "percent", None, [{"name": "Т", "address_id": "9001", "enabled": True, "share": 100, "address_text": ""}], NOW, "o")
    again = cycle.run_for_accounts(conn, unload, REF, date(2026, 10, 14), tmp_path / "feed")
    assert [r.ok for r in again] == [True, True]                                               # its own address: passes


def test_an_address_that_is_the_account_s_own_is_not_foreign_even_when_another_account_or_the_settings_have_it_too(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(point_values={"9001": 100.0}), NOW)                   # the general settings carry 9001...
    first = connections.create_account(conn, "Основной", NOW, "o")
    second = connections.create_account(conn, "Второй", NOW, "o")
    for n in (first, second):
        connections.set_live(conn, n, True, NOW, "o")
    connections.save_addresses(conn, second, "percent", None, [{"name": "Т", "address_id": "9001", "enabled": True, "share": 100, "address_text": ""}], NOW, "o")
    results = cycle.run_for_accounts(conn, catalog_xml(commodity_xml(NNOMMODIF="100")), REF, date(2026, 10, 14), tmp_path / "feed")
    assert [r.ok for r in results] == [True, True]                              # 9001 is in the general settings, but it is also the second account's own


def test_the_primary_account_is_never_judged_by_the_address_rule(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    first = connections.create_account(conn, "Основной", NOW, "o")
    connections.set_live(conn, first, True, NOW, "o")
    assert cycle._foreign_addresses(conn, connections.get_account(conn, first)) == frozenset()


def test_foreign_addresses_are_those_of_the_other_accounts_and_the_settings_and_a_disabled_address_is_still_the_owner_s(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(point_values={"9001": 50.0, "9003": 50.0}), NOW)             # the general settings carry 9003 too
    first = connections.create_account(conn, "Основной", NOW, "o")
    second = connections.create_account(conn, "Второй", NOW, "o")
    third = connections.create_account(conn, "Третий", NOW, "o")
    row = lambda address_id, enabled: {"name": "Т", "address_id": address_id, "enabled": enabled, "share": 100 if enabled else 0, "address_text": ""}
    connections.save_addresses(conn, second, "percent", None, [row("9002", True), row("9003", False)], NOW, "o")
    connections.save_addresses(conn, third, "percent", None, [row("9100", True)], NOW, "o")
    get = lambda n: cycle._foreign_addresses(conn, connections.get_account(conn, n))
    assert get(second) == frozenset({"9001", "9100"})                     # not its own 9002 and 9003 (a switched-off address is still its own)
    assert get(third) == frozenset({"9001", "9002", "9003"})
    assert get(first) == frozenset()


# ---- `feed-check` from the command line (and after a deploy) judges a feed as the cycle did: the held positions, the Ids without their code

REF_TEXT = '<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>'
BROKEN = catalog_xml(commodity_xml(NNOMMODIF="100", NREST="abc") + commodity_xml(NNOMMODIF="101"))     # the row of 100 is broken: the cycle holds it
GONE = catalog_xml(commodity_xml(NNOMMODIF="101"))                                                      # 100 left the supplier, no cycle since


def held_by_the_cycle(tmp_path):
    """100 and 101 published; then the row of 100 breaks: the cycle keeps it on its snapshot (HOLD) and publishes. The unload of that cycle is
    in unload.xml."""
    from tires_sync import publishing
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    account = connections.create_account(conn, "Основной", NOW, "o")
    connections.set_live(conn, account, True, NOW, "o")
    cycle.run_for_accounts(conn, catalog_xml(commodity_xml(NNOMMODIF="100") + commodity_xml(NNOMMODIF="101")), REF, date(2026, 10, 14), tmp_path / "feed")
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    results = cycle.run_for_accounts(conn, BROKEN, REF, date(2026, 10, 15), tmp_path / "feed")
    assert [(r.ok, r.counts) for r in results] == [(True, {"hold": 1, "keep": 1})]          # the cycle's own check passes it
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    (tmp_path / "unload.xml").write_bytes(BROKEN)
    (tmp_path / "reference.txt").write_text(REF_TEXT, encoding="utf-8")
    return conn, account


def cli_check(tmp_path, capsys, *extra):
    from tires_sync.cli import main
    code = main(["feed-check", "--db", str(tmp_path / "t.db"), "--unload", str(tmp_path / "unload.xml"), "--reference", str(tmp_path / "reference.txt"),
                 "--feed-root", str(tmp_path / "feed"), *extra])
    return code, capsys.readouterr().out


def test_the_cli_check_does_not_judge_by_stock_a_position_the_cycle_holds(tmp_path, capsys):
    conn, _ = held_by_the_cycle(tmp_path)
    conn.close()
    for extra in ((), ("--published",)):
        code, out = cli_check(tmp_path, capsys, *extra)
        assert code == 0 and "чисто" in out and "stock" not in out, out


def test_the_cli_check_still_stops_a_position_that_is_gone_and_not_held(tmp_path, capsys):
    conn, _ = held_by_the_cycle(tmp_path)
    conn.close()
    (tmp_path / "unload.xml").write_bytes(GONE)                 # 100 left the supplier after the cycle that holds it
    for extra in ((), ("--published",)):
        code, out = cli_check(tmp_path, capsys, *extra)
        assert code == 0 and "stock" not in out, out              # held: judged as the cycle judged it
    (tmp_path / "unload.xml").write_bytes(catalog_xml(commodity_xml(NNOMMODIF="100", NREST="abc")))   # now 101 is gone, and nobody holds it
    for extra in ((), ("--published",)):
        code, out = cli_check(tmp_path, capsys, *extra)
        assert code == 3 and "[critical] stock 101" in out and "stock 100" not in out, out


def test_the_cli_check_judges_a_published_feed_older_than_the_last_draft_with_the_holds_of_both(tmp_path, capsys):
    from tires_sync import publishing
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    account = connections.create_account(conn, "Основной", NOW, "o")
    connections.set_live(conn, account, True, NOW, "o")
    cycle.run_for_accounts(conn, catalog_xml(commodity_xml(NNOMMODIF="100") + commodity_xml(NNOMMODIF="101")), REF, date(2026, 10, 14), tmp_path / "feed")
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    connections.set_live(conn, account, False, NOW, "o")
    cycle.run_for_accounts(conn, BROKEN, REF, date(2026, 10, 15), tmp_path / "feed")      # the cycle holds 100; this draft is not published
    assert publishing.publish_live_accounts(conn, tmp_path / "feed")[0]["skipped"]
    conn.close()
    (tmp_path / "unload.xml").write_bytes(BROKEN)
    (tmp_path / "reference.txt").write_text(REF_TEXT, encoding="utf-8")
    for extra in ((), ("--published",)):                      # the published file is the older one: 100 is in it as a usual ad
        code, out = cli_check(tmp_path, capsys, *extra)
        assert code == 0 and "stock" not in out, out
    (tmp_path / "unload.xml").write_bytes(catalog_xml(commodity_xml(NNOMMODIF="100", NREST="abc")))   # 101 gone, held by nobody
    code, out = cli_check(tmp_path, capsys, "--published")
    assert code == 3 and "[critical] stock 101" in out and "stock 100" not in out, out


def test_the_cli_check_reads_an_id_that_does_not_carry_its_code_from_the_cycle_s_record(tmp_path, capsys):
    conn, account = held_by_the_cycle(tmp_path)
    acct = connections.get_account(conn, account)
    draft = tmp_path / "feed" / acct["feed_token"] / "next" / "avito_feed.xml"
    draft.write_text(draft.read_text(encoding="utf-8").replace("<Id>101</Id>", "<Id>kabcdefghijk_r20261015</Id>"), encoding="utf-8")
    assert "kabcdefghijk_r20261015" in draft.read_text(encoding="utf-8")
    cycle.save_check_inputs(conn, account, draft, ["100"], {"kabcdefghijk_r20261015": "101", "100": "100"})
    conn.close()
    code, out = cli_check(tmp_path, capsys)
    assert code == 0 and "stock" not in out, out                # the alias Id is 101, which the supplier has
    conn = store.connect(tmp_path / "t.db")
    cycle.save_check_inputs(conn, account, draft, ["100"], {})
    conn.close()
    code, out = cli_check(tmp_path, capsys)
    assert code == 3 and "[critical] stock kabcdefghijk_r20261015" in out, out


def test_the_cli_check_reads_an_id_bound_to_two_codes_as_the_build_kept_it(tmp_path, capsys):
    # 2026-10-21/22: Id 113105674045 was bound to its own code (stock 12) and to a gone inherited one; the build kept its own code, the
    # stand-alone check read the gone one («нет у поставщика»). The record of the cycle reads it as the build did.
    import json
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    account = connections.create_account(conn, "Основной", NOW, "o")
    store.set_state(conn, "adopted_%d" % account, json.dumps({"ids": {"100": "100", "999": "100"}, "images": {}, "shared": {}}))
    unload = catalog_xml(commodity_xml(NNOMMODIF="100"))
    assert [r.ok for r in cycle.run_for_accounts(conn, unload, REF, date(2026, 10, 14), tmp_path / "feed")] == [True]
    conn.close()
    (tmp_path / "unload.xml").write_bytes(unload)
    (tmp_path / "reference.txt").write_text(REF_TEXT, encoding="utf-8")
    code, out = cli_check(tmp_path, capsys)
    assert code == 0 and "stock" not in out, out


def test_the_cycle_records_what_it_gave_its_check(tmp_path):
    conn, account = held_by_the_cycle(tmp_path)
    acct = connections.get_account(conn, account)
    for source in ("next", "published"):
        held, id_codes = cycle.check_inputs(conn, acct, tmp_path / "feed", source)
        assert held == frozenset({"100"}) and id_codes == {"100": "100", "101": "101"}   # every Id: one read as its own code overrides a binding
    store.set_state(conn, "feed_check_inputs_%d" % account, "not json")
    assert cycle.check_inputs(conn, acct, tmp_path / "feed")[0] == frozenset({"100"})   # a broken record: what the publication recorded


def test_the_cli_check_uses_the_positions_of_every_supplier_as_the_cycle_does(tmp_path, capsys):
    from tires_sync.cli import main
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(territory_rn="1001", enabled=[["IKON TYRES", "winter"], ["S2:IKON TYRES", "winter"]],
                                      pricing={"IKON TYRES|winter": {"pct_small": -3.0, "pct_large": -3.0},
                                               "S2:IKON TYRES|winter": {"pct_small": 5.0, "pct_large": 5.0}}), NOW)
    connections.create_account(conn, "A", NOW, "o")
    two = connections.create_supplier(conn, "Второй", NOW, "o")
    connections.set_config(conn, "webmim", connections.supplier_owner(two), {"base_url": "https://b.example", "mode": "apikey", "territory_rn": "2002"}, NOW, "o")
    conn.close()
    (tmp_path / "unload.xml").write_bytes(catalog_xml(commodity_xml(NNOMMODIF="5", TERRITORY_RN="1001", NPRICE_DELAYED="5000")))
    (tmp_path / "unload-2.xml").write_bytes(catalog_xml(commodity_xml(NNOMMODIF="5", TERRITORY_RN="2002", NPRICE_DELAYED="5000", SLOAD="100")))
    (tmp_path / "reference.txt").write_text(REF_TEXT, encoding="utf-8")
    args = ["--db", str(tmp_path / "t.db"), "--unload", str(tmp_path / "unload.xml"), "--reference", str(tmp_path / "reference.txt"),
            "--feed-root", str(tmp_path / "feed")]
    assert main(["run-cycle", *args]) == 0
    capsys.readouterr()
    code, out = cli_check(tmp_path, capsys)
    assert code == 0 and "чисто" in out, out                     # s2-5 is the second supplier's: not «нет у поставщика»


def studs_ad(name, template=None):
    """The ad of a winter tire named `name`, its studs read from the name as the unload is read (webmim.studded_in_text)."""
    from tires_sync.webmim import studded_in_text
    offer = make_offer(goods_id="100", stock=12, name=name, name_orig=name, studded=studded_in_text(name))
    return render_ad(Candidate("100", offer, 4810, "Ikon Tyres", "Test Model"), 4810, "P1", make_settings(), date(2026, 10, 14), template=template)


@pytest.mark.parametrize("tail, conflict", [
    ("", False), ("Ш.", False), ("шип", False), ("шипованная", False), ("Внешипованная", False), ("Шипованная нет", False),
    ("нешип.", False), ("без шипов", False), ("под шип", False), ("шипуемая", False), ("Шипы: нет", False), ("липучка", False),
    ("нешип. Ш.", True), ("шипуемая Ш.", True), ("без шипов Ш.", True),      # the mark «Ш.» wins the reading: studs, while the text says none
])
def test_the_studs_of_our_own_ad_contradict_its_text_only_where_the_mark_won(tail, conflict):
    node = studs_ad(("Ikon 205/55/16 T 94 Test Model " + tail).strip())
    found = [f for f in feed_check.check_feed([node], ctx()) if f.rule == "studs_conflict"]
    assert bool(found) == conflict, (tail, node.findtext("TireType"), node.findtext("Title"))
    if conflict:
        assert node.findtext("TireType") == "Зимние шипованные"
        assert [f.level for f in found] == ["critical"] and "Зимние шипованные" in found[0].text and "названии" in found[0].text


def test_a_negation_only_in_the_description_stops_a_studded_ad():
    node = ad(TireType="Зимние шипованные", Title="Ikon Tyres 205 55 R16 T 94 Test Model в наличии",
              Description="Ikon 205/55/16 T 94 Test Model без шипов Ш.\nСезон: зимняя")      # a title of the account's own template has no name
    found = [f for f in feed_check.check_feed([node], ctx()) if f.rule == "studs_conflict"]
    assert [f.level for f in found] == ["critical"] and "описании" in found[0].text and "без шипов" in found[0].text


def test_a_non_studded_ad_whose_title_says_studs_is_stopped_and_the_description_is_not_read_for_it():
    found = feed_check.check_feed([ad(TireType="Зимние нешипованные", Title="Ikon Tyres 205 55 R16 Test Model шипованная")], ctx())
    assert [(f.rule, f.level) for f in found] == [("studs_conflict", "critical")] and "шипованная" in found[0].text
    for title in ("Ikon Tyres 205 55 R16 Test Model нешипованная", "Ikon Tyres 205 55 R16 Test Model под шип", "Ikon Tyres Test Model Шипы: нет"):
        assert feed_check.check_feed([ad(TireType="Зимние нешипованные", Title=title)], ctx()) == [], title
    generic = ad(TireType="Зимние нешипованные", Description="В наличии шипованные и нешипованные шины, шипуемые под заказ")
    assert feed_check.check_feed([generic], ctx()) == []                     # a template's own words on studs: not this tire


def test_summer_and_all_season_ads_are_not_judged_by_studs():
    for kind in ("Летние", "Всесезонные"):
        assert feed_check.check_feed([ad(TireType=kind, Title="Ikon Tyres 205 55 R16 Test Model без шипов шип")], ctx()) == [], kind


def test_a_mirror_gets_a_warning_for_both_contradictions():
    both = [ad(TireType="Зимние шипованные", Title="Ikon Tyres Test Model нешип."),
            ad(Id="101", TireType="Зимние нешипованные", Title="Ikon Tyres Test Model шип")]
    found = feed_check.check_feed(both, ctx(mode="mirror", stocks={"100": 12, "101": 12}))
    assert [(f.rule, f.level, f.ad_id) for f in found if f.rule == "studs_conflict"] == [
        ("studs_conflict", "warning", "100"), ("studs_conflict", "warning", "101")]


def test_the_default_description_template_says_nothing_of_studs():
    from tires_sync.description import DEFAULT_TEMPLATE, SEASON_WORD
    from tires_sync.webmim import NOT_STUDDED
    assert "шип" not in DEFAULT_TEMPLATE.lower() and not NOT_STUDDED.search(DEFAULT_TEMPLATE)
    assert not any("шип" in word for word in SEASON_WORD.values())
    for name in ("Ikon 205/55/16 T 94 Test Model Ш.", "Ikon 205/55/16 T 94 Test Model нешип.", "Ikon 205/55/16 T 94 Test Model"):
        node = studs_ad(name)                                                  # the default template, its season word, the search line
        assert "Сезон: зимняя" in node.findtext("Description")
        assert [f for f in feed_check.check_feed([node], ctx()) if f.rule == "studs_conflict"] == [], name


def test_a_negation_is_read_within_one_line_of_the_description():
    """Review 1: «\\s» of NOT_STUDDED takes a line break too — an account's template line ending in «под» before a line «шипованные …»."""
    split = ad(TireType="Зимние шипованные", Description="Подберём шины под\nшипованные модели в наличии\nПишите без\nшипов не оставим")
    assert feed_check.check_feed([split], ctx()) == []
    one_line = ad(TireType="Зимние шипованные", Description="Первая строка\nIkon Test Model без шипов")
    assert rules_of(feed_check.check_feed([one_line], ctx())) == ["studs_conflict"]


# ---- «template_missing»: without the Avito template the feed is not published (owner's decision 2026-10-25)

TEMPLATE_TEXT = "Шаблон Авито не найден или испорчен"


@pytest.mark.parametrize("content, problem", [
    (None, "файл не найден или не читается"),
    (b"", "файл не разбирается как XML"),
    (b"<Ads><Ad><Id>1</Id>", "файл не разбирается как XML"),
    (b"<Ads></Ads>", "в файле нет объявления-образца"),
    (b"<Ads><Ad/></Ads>", "в файле нет объявления-образца"),
    (b'<?xml version="1.0" encoding="bogus-enc"?><Ads><Ad><Id/></Ad></Ads>', "файл не разбирается как XML"),
])
def test_a_missing_or_broken_template_is_read_as_a_problem(tmp_path, content, problem):
    path = tmp_path / "template.xml"
    if content is not None:
        path.write_bytes(content)
    assert feed_check.read_template(path) == (frozenset(), problem)


def test_the_template_of_today_is_read_whole():
    assert feed_check.read_template(cycle.TEMPLATE_PATH) == (TEMPLATE, "")
    assert {"Id", "Category", "Brand", "Price", "Title"} <= TEMPLATE      # the real template, not two empty sets that agree


@pytest.mark.parametrize("mode", ["generated", "mirror"])
def test_a_template_problem_is_a_critical_finding_in_both_modes(mode):
    findings = feed_check.check_feed([ad()], ctx(mode=mode, template_problem="файл не найден или не читается"))
    missing = [f for f in findings if f.rule == "template_missing"]
    assert [(f.level, f.ad_id) for f in missing] == [("critical", "")] and missing[0].text.startswith(TEMPLATE_TEXT)
    assert "файл не найден или не читается" in missing[0].text
    assert feed_check.check_feed([], ctx(mode=mode, template_problem="x"))[0].rule == "template_missing"      # an empty feed is told too
    assert TEMPLATE_TEXT in feed_check.summarize(feed_check.critical(findings))


def test_with_the_template_read_there_is_no_template_finding():
    assert "template_missing" not in rules_of(feed_check.check_feed([ad()], ctx()))
    assert "template_missing" in feed_check.load_rules()["rules"]


def test_without_the_template_the_gate_stops_the_feed_the_published_one_stays_and_the_cli_check_reports_it(tmp_path, monkeypatch, capsys):
    from tires_sync import publishing
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    account = connections.create_account(conn, "Основной", NOW, "o")
    connections.set_live(conn, account, True, NOW, "o")
    unload = catalog_xml(commodity_xml(NNOMMODIF="100") + commodity_xml(NNOMMODIF="101"))
    first = cycle.run_for_accounts(conn, unload, REF, date(2026, 10, 14), tmp_path / "feed")
    assert [r.ok for r in first] == [True]
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    token = connections.list_feed_accounts(conn)[0]["feed_token"]
    published = (tmp_path / "feed" / token / "avito_feed.xml").read_bytes()

    monkeypatch.setattr(cycle, "TEMPLATE_PATH", tmp_path / "нет-шаблона.xml")
    results = cycle.run_for_accounts(conn, unload, REF, date(2026, 10, 15), tmp_path / "feed")
    assert [r.ok for r in results] == [False]
    assert "проверка фида не пройдена" in results[0].error and "template_missing: " + TEMPLATE_TEXT in results[0].error
    assert "template_tags" not in results[0].error
    publishing.publish_live_accounts(conn, tmp_path / "feed", skip_ids={r.account_id for r in results if not r.ok})
    assert (tmp_path / "feed" / token / "avito_feed.xml").read_bytes() == published        # the published feed stays as it was
    conn.close()

    (tmp_path / "unload.xml").write_bytes(unload)
    (tmp_path / "reference.txt").write_text(REF_TEXT, encoding="utf-8")
    code, out = cli_check(tmp_path, capsys)
    assert code == 3 and "ОШИБКИ" in out and "[critical] template_missing" in out and TEMPLATE_TEXT in out
    monkeypatch.setattr(cycle, "TEMPLATE_PATH", tmp_path / "broken.xml")
    (tmp_path / "broken.xml").write_bytes(b"<Ads><Ad>")
    code, out = cli_check(tmp_path, capsys, "--published")
    assert code == 3 and "[critical] template_missing" in out and "файл не разбирается как XML" in out


def test_without_the_template_a_mirror_is_stopped_by_the_gate_too(tmp_path, monkeypatch):
    from tires_sync import mirror_feed
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    account = connections.create_account(conn, "Чужой", NOW, "o")
    file = '<Ads><Ad><Id>999_1</Id><Title>T</Title><Price>5000</Price><ContactPhone>+79990000001</ContactPhone></Ad></Ads>'.encode()
    mirror_feed.import_feed(conn, account, file, NOW)
    unload = catalog_xml(commodity_xml(NNOMMODIF="999"))
    monkeypatch.setattr(cycle, "TEMPLATE_PATH", tmp_path / "нет-шаблона.xml")
    results = cycle.run_for_accounts(conn, unload, REF, date(2026, 10, 14), tmp_path / "feed")
    assert [r.ok for r in results] == [False] and "template_missing: " + TEMPLATE_TEXT in results[0].error
    assert "template_tags" not in results[0].error
    conn.close()


# ---- «title_age_word»: «старше» left in a title is reported, never stops the feed (owner's decision 2026-10-25)

@pytest.mark.parametrize("title", [
    "Ikon Tyres 205 55 R16 Test Model (BMW) старше в наличии",
    "Ikon Tyres 205 55 R16 Test Model СТАРШЕ 3-х лет в наличии",
    "Ikon Tyres 205 55 R16 Test Model Старше",
    "Ikon Tyres 205 55 R16 Test Model (старше) в наличии",
    "Ikon Tyres 205 55 R16 Test Model старше, в наличии",
])
def test_a_title_with_the_word_older_gets_a_warning_with_its_id(title):
    for mode in ("generated", "mirror"):
        found = feed_check.check_feed([ad(Title=title)], ctx(mode=mode))
        assert [(f.rule, f.level, f.ad_id) for f in found] == [("title_age_word", "warning", "100")], (mode, title)
        assert "В заголовке осталось «старше»" in found[0].text and title in found[0].text
        assert feed_check.critical(found) == []                               # publication goes on


@pytest.mark.parametrize("title", [
    "Ikon Tyres 205 55 R16 Test Model в наличии",
    "Ikon Tyres 205 55 R16 Test Model старшая в наличии",
    "Ikon Tyres 205 55 R16 Test Model старшие в наличии",
    "Ikon Tyres 205 55 R16 Test Model старшее в наличии",
    "Ikon Tyres 205 55 R16 Test Model Старшеклассник",
    "Ikon Tyres 205 55 R16 Test Model постарше",
])
def test_a_title_without_the_word_older_is_not_reported(title):
    assert [f for f in feed_check.check_feed([ad(Title=title)], ctx()) if f.rule == "title_age_word"] == [], title


def test_the_word_older_only_in_the_description_is_not_reported():
    node = ad(Description="Ikon 205/55/16 Test Model старше 3-х лет")       # the age note stays in the description (owner, 2026-10-25)
    assert feed_check.check_feed([node], ctx()) == []


def test_a_feed_with_the_word_older_in_a_title_passes_the_gate_with_a_note():
    found = feed_check.check_feed([ad(Title="Ikon Tyres 205 55 R16 Test Model (BMW) старше в наличии")], ctx())
    assert feed_check.critical(found) == [] and feed_check.summarize(found) == "title_age_word: 1 (например 100)"


# ---- «double_reissue_mark»: an Id with two renewal marks (tails2 report, 2026-10-25). A warning for now: an Id of a mirror file renewed
# twice on purpose (mirror-reissue) is legal and stays after a move to our assembly; critical only after the live feeds are measured

@pytest.mark.parametrize("ad_id", ["106_r20261020_r20261021", "106_a2_r20261020_r20261021", "106-x4_r20261020_r20261021"])
def test_an_id_with_two_renewal_marks_is_reported(ad_id):
    for mode in ("generated", "mirror"):
        found = feed_check.check_feed([ad(Id=ad_id)], ctx(mode=mode))
        assert [(f.rule, f.level, f.ad_id) for f in found if f.rule == "double_reissue_mark"] == [("double_reissue_mark", "warning", ad_id)]


@pytest.mark.parametrize("ad_id", ["106", "106_r20261020", "106_a2_r20261020", "106-x4_a2_r20261020", "kabc123def45_r20261020", "106_r2026102"])
def test_an_id_with_one_mark_or_none_is_not_reported(ad_id):
    assert [f for f in feed_check.check_feed([ad(Id=ad_id)], ctx()) if f.rule == "double_reissue_mark"] == []
