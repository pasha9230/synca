"""The phone in the ads belongs to the Avito account the feed is for: not the owner's, not the one of the file's author."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import date

import pytest

from helpers import catalog_xml, commodity_xml, settings_dict
from tires_sync import connections, cycle, mirror_feed, store
from tires_sync.config import settings_from_dict
from tires_sync.settings_store import active_settings, init_settings

NOW = "2026-10-14T12:00:00"
OWNER = "+70000000000"                                     # tests/helpers: settings_dict()["contact_phone"]


@pytest.mark.parametrize("raw, want", [
    ("79000000016", "+79000000016"), ("+7 (900) 000-00-16", "+79000000016"), ("89000000016", "+79000000016"),
    ("9000000016", "+79000000016"), ("", ""), (None, ""), ("12345", ""), ("not a phone", "")])
def test_a_phone_is_written_the_way_the_feed_wants_it(raw, want):
    assert connections.normalize_phone(raw) == want


def setup(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    init_settings(conn, settings_dict(), NOW)
    return conn, connections.create_account(conn, "Партнёр-2", NOW, "o")


def test_the_identity_keeps_the_phone_of_the_account(tmp_path):
    conn, account = setup(tmp_path)
    assert connections.get_avito_identity(conn, account)["phone"] == ""
    connections.set_avito_identity(conn, account, 30000002, "Партнёр-2", "79000000016")
    assert connections.get_avito_identity(conn, account) == {"id": 30000002, "name": "Партнёр-2", "phone": "+79000000016", "phone_source": "cabinet", "phones": [], "pinned": False}
    connections.set_avito_identity(conn, account, 30000002, "Партнёр-2")                       # a check that did not bring a phone
    assert connections.get_avito_identity(conn, account)["phone"] == "+79000000016"


def test_the_effective_settings_carry_the_phone_of_the_account_only_when_it_is_known(tmp_path):
    conn, account = setup(tmp_path)
    settings = active_settings(conn)
    assert settings.contact_phone == OWNER and connections.effective_settings(conn, account, settings).contact_phone == OWNER
    connections.set_avito_identity(conn, account, 1, "n", "+79000000016")
    assert connections.effective_settings(conn, account, settings).contact_phone == "+79000000016"
    assert settings.contact_phone == OWNER                                                       # the general settings are not changed


def ad(ad_id, phone="+79990000001"):
    return '<Ad><Id>%s</Id><Title>Шина</Title><Price>5000</Price><ContactPhone>%s</ContactPhone></Ad>' % (ad_id, phone)


def test_the_mirror_sends_the_phone_of_its_account_instead_of_the_one_of_the_file(tmp_path):
    conn, account = setup(tmp_path)
    mirror_feed.import_feed(conn, account, ('<Ads>%s%s</Ads>' % (ad("1_1"), ad("2_1", "+79990000002"))).encode(), NOW)
    cfg = settings_from_dict(settings_dict(), enforce_pct_limit=False)
    def phones(**kw):
        return [a.findtext("ContactPhone") for a in ET.fromstring(mirror_feed.render(conn, account, date(2026, 10, 14), cfg, **kw))]
    assert phones() == ["+79990000001", "+79990000002"]                                          # nothing known: as the file has it
    assert phones(contact_phone="+79000000016") == ["+79000000016", "+79000000016"]


def test_the_cycle_gives_the_mirror_its_own_phone_and_the_ids_stay(tmp_path):
    conn, account = setup(tmp_path)
    mirror_feed.import_feed(conn, account, ('<Ads>%s</Ads>' % ad("100_1")).encode(), NOW)
    connections.set_avito_identity(conn, account, 30000002, "Партнёр-2", "79000000016")
    cycle.run_for_accounts(conn, catalog_xml(commodity_xml(NNOMMODIF="100")), {}, date(2026, 10, 14), tmp_path / "feed")
    path = tmp_path / "feed" / connections.get_account(conn, account)["feed_token"] / "next" / "avito_feed.xml"
    root = ET.parse(path).getroot()
    assert [(a.findtext("Id"), a.findtext("ContactPhone")) for a in root] == [("100_1", "+79000000016")]


def test_a_new_position_of_the_mirror_gets_the_phone_of_the_account_not_the_one_of_the_file(tmp_path):
    from tires_sync.catalog import parse_reference
    conn, account = setup(tmp_path)
    mirror_feed.import_feed(conn, account, ('<Ads>%s</Ads>' % ad("100_1", "+79990000001")).encode(), NOW)
    connections.set_avito_identity(conn, account, 30000002, "Партнёр-2", "79000000016")
    ref = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
    unload = catalog_xml(commodity_xml(NNOMMODIF="100"), commodity_xml(NNOMMODIF="200", SMNFCODE="NEW2"))
    cycle.run_for_accounts(conn, unload, ref, date(2026, 10, 14), tmp_path / "feed")
    path = tmp_path / "feed" / connections.get_account(conn, account)["feed_token"] / "next" / "avito_feed.xml"
    phones = {a.findtext("Id"): a.findtext("ContactPhone") for a in ET.parse(path).getroot()}
    assert phones == {"100_1": "+79000000016", "200": "+79000000016"}


def test_a_phone_is_shown_the_way_a_person_reads_it():
    assert connections.format_phone("+79000000012") == "+7 900 000-00-12"
    assert connections.format_phone("89000000012") == "+7 900 000-00-12"
    assert connections.format_phone("") == "" and connections.format_phone("позвоните") == "позвоните"


def test_the_file_phone_choice_means_nothing_for_an_account_that_builds_its_own_feed(tmp_path):
    conn, account = setup(tmp_path)
    connections.set_avito_identity(conn, account, 1, "n", "79000000012")
    connections.set_phone_source(conn, account, "file", NOW, "o")
    assert connections.get_avito_identity(conn, account)["phone_source"] == "cabinet"                    # our own feed: the cabinet's phone, never the general number
    assert connections.effective_settings(conn, account, active_settings(conn)).contact_phone == "+79000000012"
    mirror_feed.import_feed(conn, account, ('<Ads>%s</Ads>' % ad("100_1")).encode(), NOW)               # the account becomes a mirror: the choice is honoured again
    assert connections.get_avito_identity(conn, account)["phone_source"] == "file"


def test_a_pinned_phone_survives_the_connection_check(tmp_path):
    conn, account = setup(tmp_path)
    connections.set_avito_identity(conn, account, 1, "n", "79000000013", ["79000000013", "79000000012"])
    assert connections.get_avito_identity(conn, account)["phone"] == "+79000000013"
    connections.pin_phone(conn, account, "79000000012", NOW, "o")
    connections.set_avito_identity(conn, account, 1, "n", "79000000013", ["79000000013", "79000000012"])          # a later check
    ident = connections.get_avito_identity(conn, account)
    assert ident["phone"] == "+79000000012" and ident["pinned"] is True and ident["phones"] == ["+79000000013", "+79000000012"]
    assert connections.effective_settings(conn, account, active_settings(conn)).contact_phone == "+79000000012"
    import pytest
    with pytest.raises(ValueError):
        connections.pin_phone(conn, account, "79990000000", NOW, "o")                                           # not one of the cabinet's numbers
    connections.pin_phone(conn, account, "", NOW, "o")
    connections.set_avito_identity(conn, account, 1, "n", "79000000013", ["79000000013", "79000000012"])
    assert connections.get_avito_identity(conn, account)["phone"] == "+79000000013"


CABINET = ["79000000013", "79000000012"]


def pinned_to_the_second(conn, account):
    connections.set_avito_identity(conn, account, 1, "n", "79000000013", CABINET)
    connections.pin_phone(conn, account, "79000000012", NOW, "o")


def test_new_keys_then_the_check_bring_the_cabinet_number_back(tmp_path):
    """The fault of 2026-10-23 («Партнёр-2»): new keys wiped the number but kept «pinned», and the check put no number: the feed stood."""
    conn, account = setup(tmp_path)
    connections.set_avito_identity(conn, account, 1, "n", "79000000013", CABINET)
    connections.pin_phone(conn, account, "79000000013", NOW, "o")
    connections.forget_identity(conn, account)
    assert connections.get_avito_identity(conn, account)["phone"] == ""          # until the check, no number of a cabinet that may be another
    connections.set_avito_identity(conn, account, 1, "n", "79000000013", ["79000000013"])
    ident = connections.get_avito_identity(conn, account)
    assert ident["phone"] == "+79000000013"
    assert connections.effective_settings(conn, account, active_settings(conn)).contact_phone == "+79000000013"


def test_a_pin_left_without_a_number_gives_way_to_the_cabinet_number(tmp_path):
    """The data the fault of 2026-10-23 left: «pinned» without a number. The check puts the cabinet's main number and unpins."""
    conn, account = setup(tmp_path)
    connections.set_config(conn, "avito", account, {"phone_pinned": True, "avito_phones": ["+79000000013"]}, NOW, "o")
    connections.set_avito_identity(conn, account, 1, "n", "79000000013", ["79000000013"])
    ident = connections.get_avito_identity(conn, account)
    assert ident["phone"] == "+79000000013" and ident["pinned"] is False


def test_the_same_keys_again_keep_the_pinned_number(tmp_path):
    conn, account = setup(tmp_path)
    pinned_to_the_second(conn, account)
    connections.forget_identity(conn, account)
    connections.set_avito_identity(conn, account, 1, "n", "79000000013", CABINET)
    ident = connections.get_avito_identity(conn, account)
    assert ident["phone"] == "+79000000012" and ident["pinned"] is True
    assert connections.effective_settings(conn, account, active_settings(conn)).contact_phone == "+79000000012"


def test_keys_of_another_cabinet_take_its_main_number_not_the_pinned_one(tmp_path):
    conn, account = setup(tmp_path)
    pinned_to_the_second(conn, account)
    connections.forget_identity(conn, account)
    connections.set_avito_identity(conn, account, 2, "другой", "79990001122", ["79990001122", "79990003344"])
    ident = connections.get_avito_identity(conn, account)
    assert ident["phone"] == "+79990001122" and ident["pinned"] is False and ident["phones"] == ["+79990001122", "+79990003344"]


def test_the_only_number_of_a_cabinet_is_taken_when_the_main_one_is_not_given(tmp_path):
    conn, account = setup(tmp_path)
    pinned_to_the_second(conn, account)
    connections.forget_identity(conn, account)
    connections.set_avito_identity(conn, account, 2, "другой", "", ["79990001122"])
    assert connections.get_avito_identity(conn, account)["phone"] == "+79990001122"


def test_a_check_without_the_list_of_numbers_leaves_a_pin_as_it_is(tmp_path):
    """The minute tick of the chats (cabinet_user_id) gives no list: it does not unpin, and after new keys it gives no number to another
    cabinet (the same one gets its pinned number back: test_the_chats_tick_gives_the_same_cabinet_its_pinned_number_back)."""
    conn, account = setup(tmp_path)
    pinned_to_the_second(conn, account)
    connections.set_avito_identity(conn, account, 1, "n", "79000000013")
    assert connections.get_avito_identity(conn, account)["phone"] == "+79000000012"
    connections.forget_identity(conn, account)
    connections.set_avito_identity(conn, account, 3, "n", "79000000013")
    ident = connections.get_avito_identity(conn, account)
    assert ident["phone"] == "" and ident["pinned"] is True                    # the check with the list decides


def test_accounts_whose_keys_did_not_change_keep_their_number(tmp_path):
    conn, account = setup(tmp_path)
    other = connections.create_account(conn, "Основной", NOW, "o")
    pinned_to_the_second(conn, account)
    connections.set_avito_identity(conn, other, 3, "m", "79110002233", ["79110002233"])
    before = [connections.effective_settings(conn, a, active_settings(conn)).contact_phone for a in (account, other)]
    for _ in range(2):                                                          # later checks of the same cabinets
        connections.set_avito_identity(conn, account, 1, "n", "79000000013", CABINET)
        connections.set_avito_identity(conn, other, 3, "m", "79110002233", ["79110002233"])
    assert [connections.effective_settings(conn, a, active_settings(conn)).contact_phone for a in (account, other)] == before
    assert before == ["+79000000012", "+79110002233"]


def test_a_check_whose_list_avito_did_not_give_keeps_a_pin_of_the_same_cabinet_only(tmp_path):
    """get_self gives [] when Avito sends no list: that proves nothing — the pin of the same cabinet stays, another cabinet gets its main number."""
    conn, account = setup(tmp_path)
    pinned_to_the_second(conn, account)
    connections.set_avito_identity(conn, account, 1, "n", "79000000013", [])              # a later check without the list: as before
    assert connections.get_avito_identity(conn, account)["phone"] == "+79000000012"
    connections.forget_identity(conn, account)                                           # the same keys again
    connections.set_avito_identity(conn, account, 1, "n", "79000000013", [])
    ident = connections.get_avito_identity(conn, account)
    assert ident["phone"] == "+79000000012" and ident["pinned"] is True
    connections.forget_identity(conn, account)                                           # keys of another cabinet
    connections.set_avito_identity(conn, account, 2, "другой", "79990001122", [])
    ident = connections.get_avito_identity(conn, account)
    assert ident["phone"] == "+79990001122" and ident["pinned"] is False


def test_after_new_keys_no_number_of_the_old_cabinet_can_be_pinned(tmp_path):
    """Review 2026-10-23 (1): the old cabinet's list stayed after new keys, so its number could be pinned for the new cabinet."""
    conn, account = setup(tmp_path)
    pinned_to_the_second(conn, account)
    connections.forget_identity(conn, account)
    assert connections.get_avito_identity(conn, account)["phones"] == []                 # the page shows no choice until the check
    with pytest.raises(ValueError):
        connections.pin_phone(conn, account, "79000000013", NOW, "o")
    assert connections.get_avito_identity(conn, account)["phone"] == ""


def test_the_chats_tick_gives_the_same_cabinet_its_pinned_number_back(tmp_path):
    """Review 2026-10-23 (2): the same keys saved again and only the chats' tick (no list) — the feed must not stand until a check."""
    conn, account = setup(tmp_path)
    pinned_to_the_second(conn, account)
    connections.forget_identity(conn, account)
    connections.set_avito_identity(conn, account, 1, "n", "79000000013")                 # cabinet_user_id: the same cabinet
    ident = connections.get_avito_identity(conn, account)
    assert ident["phone"] == "+79000000012" and ident["pinned"] is True
    connections.forget_identity(conn, account)
    connections.set_avito_identity(conn, account, 2, "другой", "79990001122")             # another cabinet: the check decides
    ident = connections.get_avito_identity(conn, account)
    assert ident["phone"] == "" and ident["pinned"] is True
    connections.set_avito_identity(conn, account, 2, "другой", "79990001122", ["79990001122"])
    assert connections.get_avito_identity(conn, account)["phone"] == "+79990001122"
