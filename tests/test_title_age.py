"""The supplier's age note («старше 3-х лет», «старше 5-ти лет») is not in the title (owner, 2026-10-25, run title-age).

It stays in the description: the templates show the supplier's name there ({title} / {title_ru}). An ad that stands on Avito with a title that
still shows the note («… (BMW) старше в наличии», the live case of 25.10) gets the fresh title; its Id does not change."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import date

import pytest

from helpers import catalog_xml, commodity_xml, make_settings
from tires_sync import store
from tires_sync.catalog import parse_reference
from tires_sync.feed import compose_title
from tires_sync.overrides import override_key
from tires_sync.sync import renamed, run_cycle, title_basis

REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
TODAY = date(2026, 10, 6)
PIRELLI = "Pirelli 275/40/20 V 106 WINTER SOTTOZERO 3 XL Run Flat (BMW) старше 5-ти лет"


def test_the_live_case_of_the_day():
    assert compose_title(PIRELLI) == "Pirelli 275 40 R20 V 106 WINTER SOTTOZERO 3 Run Flat (BMW) в наличии"
    assert compose_title(PIRELLI, "", tail="комплект 4 шт.") == "Pirelli 275 40 R20 WINTER SOTTOZERO 3 Run Flat (BMW) комплект 4 шт."


@pytest.mark.parametrize("note", ["старше 3-х лет", "старше 5-ти лет", "Старше 5-ти лет", "СТАРШЕ 3-Х ЛЕТ", "старше 5 лет", "старше  3-х  лет",
                                  "старше 10-ти лет"])
def test_every_form_of_the_note_leaves_the_title(note):
    plain = "Ikon Tyres 205/55/16 T 94 Character Ice 7"
    assert compose_title(plain + " " + note) == compose_title(plain)
    assert compose_title("Ikon Tyres 205/55/16 T 94 %s Character Ice 7" % note) == compose_title(plain)      # in the middle as well


@pytest.mark.parametrize("rest", ["старше", "5-ти лет", "старшеe 3 лет", "старше 3-х летний", "(старше 3-х лет)", "старше трёх лет"])
def test_other_words_are_not_a_note(rest):
    """Only the phrase «старше N лет» standing alone (owner's choice of a narrow list): anything else is the supplier's text, as before."""
    assert compose_title("Ikon 205/55/16 Character " + rest) == "Ikon 205 55 R16 Character %s в наличии" % rest


def test_a_name_that_is_only_the_note_gives_no_title():
    assert compose_title("старше 3-х лет") == ""


def test_the_note_coming_or_going_is_not_a_rename():
    assert not renamed(PIRELLI, PIRELLI.replace(" старше 5-ти лет", ""))
    assert not renamed(PIRELLI, PIRELLI.replace("5-ти", "3-х"))
    assert renamed(PIRELLI, PIRELLI.replace("SOTTOZERO 3", "SOTTOZERO 4"))


def _cycle(tmp_path, conn, live_title, name, settings=None):
    base = tmp_path / "token"
    (base / "next").mkdir(parents=True, exist_ok=True)
    (base / "avito_feed.xml").write_text("<Ads><Ad><Id>100</Id><Title>%s</Title></Ad></Ads>" % live_title, encoding="utf-8")
    report = run_cycle(catalog_xml(commodity_xml(NNOMMODIF="100", SLOAD="94", SMODIFNAME=name, SMODIFNAME_ORIG=name)),
                       settings or make_settings(overrides={override_key("100"): "on"}), REF, conn, TODAY, out_dir=base / "next", account_id=1)
    ads = ET.parse(str(base / "next" / "avito_feed.xml")).getroot().findall("Ad")
    return report, {a.findtext("Id"): (a.findtext("Title"), a.findtext("Description")) for a in ads}


NAME = "Ikon 205/55/16 T 94 Test Model XL Run Flat (BMW) старше 5-ти лет"


def test_a_live_title_that_shows_the_note_gets_the_fresh_one_and_keeps_its_id(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    fresh = compose_title(NAME)
    assert "старше" not in fresh
    _report, ads = _cycle(tmp_path, conn, "Ikon Tyres 205 55 R16 Test Model Run Flat (BMW) старше в наличии", NAME)
    assert list(ads) == ["100"] and ads["100"][0] == fresh
    assert "старше 5-ти лет" in ads["100"][1]                                    # the note stays in the description (the supplier's name)
    assert title_basis(conn, 1) == {"100": ["100", NAME]}
    _report, ads = _cycle(tmp_path, conn, fresh, NAME)                         # published: stays as it is
    assert ads["100"][0] == fresh


def test_the_whole_note_in_a_live_title_goes_too(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    _report, ads = _cycle(tmp_path, conn, "Ikon Tyres 205 55 R16 Test Model старше 5-ти лет в наличии", NAME)
    assert ads["100"][0] == compose_title(NAME)


def test_a_live_title_without_the_note_is_kept(tmp_path):
    """The two other live ads of 25.10 with «старше 3-х лет» in the supplier's name: their kept title has no note and stays."""
    conn = store.connect(tmp_path / "t.db")
    _report, ads = _cycle(tmp_path, conn, "Ikon 205/55/16 T 94 Test Model", NAME)
    assert ads["100"][0] == "Ikon 205/55/16 T 94 Test Model"


def test_a_live_title_with_the_word_but_no_note_in_the_name_is_kept(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    plain = "Ikon 205/55/16 T 94 Test Model"
    _report, ads = _cycle(tmp_path, conn, "OLD старше", plain)
    assert ads["100"][0] == "OLD старше"


def test_the_change_counts_against_the_guard_of_mass_renames(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    live = "Ikon Tyres 205 55 R16 Test Model старше в наличии"
    report, ads = _cycle(tmp_path, conn, live, NAME, make_settings(overrides={override_key("100"): "on"}, guard_min=0, guard_fraction=0.0))
    assert report.titles_held == 1 and ads["100"][0] == live


def test_a_note_dropped_by_the_supplier_still_clears_the_kept_title(tmp_path):
    """The title was kept with the note (basis = the name with it); the supplier then drops the note: no rename, but the cut «старше» goes."""
    conn = store.connect(tmp_path / "t.db")
    live = "Ikon Tyres 205 55 R16 Test Model Run Flat (BMW) старше в наличии"
    store.set_state(conn, "title_basis_1", json.dumps({"100": ["100", NAME]}, ensure_ascii=False))   # remembered before the fix was out
    plain = NAME.replace(" старше 5-ти лет", "")
    _report, ads = _cycle(tmp_path, conn, live, plain)
    assert ads["100"][0] == compose_title(plain) and "старше" not in ads["100"][0]


def test_a_set_of_four_with_the_note_gets_the_fresh_title_too(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    (tmp_path / "next").mkdir()
    (tmp_path / "avito_feed.xml").write_text("<Ads><Ad><Id>100_a2</Id><Title>A старше в наличии</Title></Ad>"
                                             "<Ad><Id>100-x4_a2</Id><Title>A старше комплект 4 шт.</Title></Ad></Ads>", encoding="utf-8")
    run_cycle(catalog_xml(commodity_xml(NNOMMODIF="100", SLOAD="94", NREST=10, SMODIFNAME=NAME, SMODIFNAME_ORIG=NAME)),
              make_settings(pack4=True, overrides={override_key("100"): "on"}), REF, conn, TODAY, out_dir=tmp_path / "next", account_id=2,
              id_suffix="_a2")
    titles = {a.findtext("Id"): a.findtext("Title") for a in ET.parse(str(tmp_path / "next" / "avito_feed.xml")).getroot().findall("Ad")}
    assert titles == {"100_a2": compose_title(NAME), "100-x4_a2": compose_title(NAME, "", tail="комплект 4 шт.")}
