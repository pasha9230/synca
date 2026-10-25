"""tire_match: one tire at two suppliers, whatever each of them writes in its name."""
from __future__ import annotations

import pytest

from helpers import catalog_xml, commodity_xml, make_offer
from tires_sync import store, tire_match, tochki_feed, webmim
from tires_sync.models import Season

MIM_NAME = "Ikon Tyres 155/80/13 T 79 Ikon Character Ice 7 Ш."
TOCHKI_NAME = "IKON 155/80R13 79T Character Ice 7 (Nordman 7) TL (шип.)"


def mim(number="100", model="Ikon Character Ice 7", name=MIM_NAME, **kw):
    values = dict(NNOMMODIF=number, TERRITORY_RN="T1", SWIDTH="155", SHEIGHT="80", SDIAMETR="13", SLOAD="79", SSPEED="T", SMARKA="Ikon Tyres",
                  SMODEL=model, SMODIFNAME=name, SMODIFNAME_ORIG=name, SSEASON="Зимняя")
    values.update(kw)
    return values


def mim_offers(*rows, supplier=1):
    return webmim.parse_unload(catalog_xml(*(commodity_xml(**r) for r in rows)), "T1", supplier).offers


def tochki_row(code="TCH1", name=TOCHKI_NAME, model="Character Ice 7 (Nordman 7)", thorn=True, **kw):
    row = {"code": code, "name": name, "marka": "IKON", "model": model, "season": "w", "thorn": thorn, "rest": 41, "price": 3500.0,
           "width": "155", "height": "80", "diameter": "13", "load_index": "79", "speed_index": "T", "puncture": "", "strengthening": "false",
           "card": True}
    row.update(kw)
    return row


def tochki_offers(*rows, supplier=2):
    return tochki_feed.parse_snapshot({"items": list(rows)}, supplier).offers


def test_the_owners_example_is_one_tire_at_both_suppliers():
    a, b = mim_offers(mim())["100"], tochki_offers(tochki_row())["s2-TCH1"]
    ia, ib = tire_match.identity(a), tire_match.identity(b)
    assert ia.bucket == ib.bucket and ia.model == ib.model == "CHARACTERICE7"
    assert ia.brand == ib.brand == "IKON" and ia.tire_type == "Зимние шипованные" and "NORDMAN7" in ib.synonyms
    result = tire_match.pairs({**mim_offers(mim()), **tochki_offers(tochki_row())}, tochki_supplier=2)
    assert result.groups == [("100", "s2-TCH1")] and not result.ambiguous and not result.doubtful
    assert result.rank_of("100") == 0 and result.rank_of("s2-TCH1") == 2


@pytest.mark.parametrize("model", ["Character Ice 7", "IKON Character Ice 7 TL"])
def test_the_model_may_come_without_brackets_or_with_the_brand_and_service_words(model):
    result = tire_match.pairs({**mim_offers(mim(model="Character Ice 7")), **tochki_offers(tochki_row(model=model))}, tochki_supplier=2)
    assert result.groups == [("100", "s2-TCH1")]


def test_the_name_in_brackets_pairs_with_the_old_name_at_the_other_supplier():
    offers = {**mim_offers(mim(model="Ikon Nordman 7", name="Ikon Tyres 155/80/13 T 79 Ikon Nordman 7 Ш.")), **tochki_offers(tochki_row())}
    assert tire_match.pairs(offers).groups == [("100", "s2-TCH1")]


def test_two_names_in_brackets_never_pair_by_themselves():
    offers = {**mim_offers(mim(model="Alpha (Китай) (Nordman 7)", name="Ikon Tyres 155/80/13 T 79 Alpha Ш.")),
              **tochki_offers(tochki_row(model="Beta (Nordman 7)", name="IKON 155/80R13 79T Beta (Nordman 7) (шип.)"))}
    assert tire_match.pairs(offers).groups == []


@pytest.mark.parametrize("change", [
    dict(width="165"), dict(load_index="82"), dict(speed_index="Q"), dict(thorn=False, name="IKON 155/80R13 79T Character Ice 7"),
    dict(strengthening="true"), dict(puncture="RunFlat"), dict(model="Character Ice 7 SUV"), dict(season="s", thorn=False, name="IKON x"),
])
def test_another_tire_is_not_a_pair(change):
    row = tochki_row(**change)
    if "width" in change:
        row["name"] = "IKON 165/80R13 79T Character Ice 7 (шип.)"
    result = tire_match.pairs({**mim_offers(mim()), **tochki_offers(row)})
    assert result.groups == []


def test_two_codes_of_one_supplier_are_not_a_cross_pair():
    assert tire_match.pairs(mim_offers(mim("100"), mim("101"))).groups == []


def test_a_name_that_contradicts_the_fields_is_doubtful_and_never_paired():
    offers = {**mim_offers(mim(name="Ikon Tyres 175/70/13 T 79 Ikon Character Ice 7 Ш.")), **tochki_offers(tochki_row())}
    result = tire_match.pairs(offers)
    assert result.groups == [] and "размер" in result.doubtful["100"]
    unstudded = mim_offers(mim(name="Ikon Tyres 155/80/13 T 79 Ikon Character Ice 7 нешип."))
    assert unstudded["100"].studded is False                     # WebMIM reads «нешип.» as no studs (run catalog-chats (8); was studs)
    assert tire_match.pairs({**unstudded, **tochki_offers(tochki_row())}).groups == []                # no studs ↔ studs: never one tire
    from dataclasses import replace
    contradicting = {"100": replace(unstudded["100"], studded=True)}                                 # the fields say studs, the name «нешип.»
    assert "без шипов" in tire_match.pairs({**contradicting, **tochki_offers(tochki_row())}).doubtful["100"]


def test_without_studs_in_the_name_and_the_fields_is_not_doubtful_and_pairs():
    """«… без шипов»: WebMIM reads no studs (run catalog-chats (8)) and the name says the same, so no contradiction (was: doubtful)."""
    name = "Ikon Tyres 155/80/13 T 79 Ikon Character Ice 7 без шипов"
    offers = {**mim_offers(mim(name=name)), **mim_offers(mim("200", name=name), supplier=3)}
    assert offers["100"].studded is False and offers["s3-200"].studded is False
    result = tire_match.pairs(offers, tochki_supplier=2)
    assert result.doubtful == {} and result.groups == [("100", "s3-200")]


def test_one_supplier_with_two_models_in_one_group_is_ambiguous():
    offers = {**mim_offers(mim("100"), mim("101", model="Ikon Nordman 7", name="Ikon Tyres 155/80/13 T 79 Ikon Nordman 7 Ш.")),
              **tochki_offers(tochki_row())}
    result = tire_match.pairs(offers)
    assert result.groups == [] and result.ambiguous == [("100", "101", "s2-TCH1")]


def test_a_second_webmim_row_pairs_too_and_ranks_between():
    offers = {**mim_offers(mim()), **mim_offers(mim("200"), supplier=3)}
    result = tire_match.pairs(offers, tochki_supplier=2)
    assert result.groups == [("100", "s3-200")] and result.rank_of("s3-200") == 1


def test_brand_forms():
    assert tire_match.brand_form("Ikon Tyres") == tire_match.brand_form("IKON") == tire_match.brand_form("S2:IKON TYRES") == "IKON"
    assert tire_match.brand_form("Nokian Tyres") == "NOKIAN" and tire_match.brand_form("Pirelli Formula") == "FORMULA"


def test_xl_from_the_webmim_name_and_from_the_4tochki_card():
    offers = mim_offers(mim("100", name="Ikon 205/55/16 T 94 Test XL"), mim("101", name="Ikon 205/55/16 T 94 Test XXL"),
                        mim("102", name="Ikon 205/55/16 T 94 Test Extra Load"))
    assert [offers[g].xl for g in ("100", "101", "102")] == [True, False, True]
    cards = tochki_offers(tochki_row("A", strengthening="true"), tochki_row("B", strengthening=True), tochki_row("C"),
                          tochki_row("D", name="IKON 155/80R13 79T Character Ice 7 XL"))
    assert [cards["s2-" + c].xl for c in "ABCD"] == [True, True, False, True]
    assert make_offer().xl is False                                                   # every old call keeps working


def test_the_owners_synonyms_are_only_added_and_pair_the_models(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    offers = {**mim_offers(mim(model="Ikon Autograph Ice 9", name="Ikon Tyres 155/80/13 T 79 Ikon Autograph Ice 9 Ш.")),
              **tochki_offers(tochki_row(model="Hakkapeliitta 9", name="IKON 155/80R13 79T Hakkapeliitta 9 (шип.)"))}
    assert tire_match.pairs(offers, store.load_model_synonyms(conn)).groups == []
    assert tire_match.add_synonym(conn, "Ikon Tyres", "Hakkapeliitta 9", "Ikon Autograph Ice 9", "владелец", "2026-10-25T00:00:00") == (
        True, "IKON", "HAKKAPELIITTA9", "AUTOGRAPHICE9")
    assert tire_match.add_synonym(conn, "IKON", "Hakkapeliitta 9", "Autograph Ice 9", "владелец", "2026-10-25T00:00:01")[0] is False
    with pytest.raises(store.SynonymConflict):
        tire_match.add_synonym(conn, "IKON", "Hakkapeliitta 9", "Nordman 9", "владелец", "2026-10-25T00:00:02")
    assert store.load_model_synonyms(conn) == {("IKON", "HAKKAPELIITTA9"): "AUTOGRAPHICE9"}
    assert tire_match.pairs(offers, store.load_model_synonyms(conn)).groups == [("100", "s2-TCH1")]


def test_the_cli_adds_a_synonym(tmp_path, capsys):
    from tires_sync.cli import main
    db = str(tmp_path / "t.db")
    assert main(["match-synonym-add", "--db", db, "Ikon Tyres", "Nordman 7", "Character Ice 7"]) == 0
    assert main(["match-synonym-add", "--db", db, "Ikon Tyres", "Nordman 7", "Character Ice 8"]) == 2
    assert "записано" in capsys.readouterr().out
    assert store.load_model_synonyms(store.connect(db)) == {("IKON", "NORDMAN7"): "CHARACTERICE7"}


def test_the_memo_gives_the_same_pairs():
    offers = {**mim_offers(mim()), **tochki_offers(tochki_row())}
    memo: dict = {}
    first = tire_match.pairs(offers, {}, 2, memo)
    assert len(memo) == 2 and tire_match.pairs(offers, {}, 2, memo).groups == first.groups == [("100", "s2-TCH1")]


def test_words_that_tell_models_apart_are_kept_and_russian_brackets_are_not_names():
    assert tire_match.model_form("Ice Zero FR", "Pirelli") != tire_match.model_form("Ice Zero", "Pirelli")
    assert tire_match.model_form("Hakka RF", "Nokian") != tire_match.model_form("Hakka", "Nokian")
    assert tire_match._bracket_forms("X (Китай) (Уценка) (DOT2022) (Made in Russia) (Nordman 7)", "Ikon") == {"NORDMAN7"}


def test_xl_glued_to_the_index():
    assert webmim.xl_in_text("Ikon 205/55/16 T 94XL Test") and not webmim.xl_in_text("Ikon XXL")


def test_synonym_chains_are_refused(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.add_model_synonym(conn, "IKON", "B", "A", "o", "t")
    with pytest.raises(store.SynonymConflict):
        store.add_model_synonym(conn, "IKON", "C", "B", "o", "t")           # B is itself another name of A
    with pytest.raises(store.SynonymConflict):
        store.add_model_synonym(conn, "IKON", "A", "D", "o", "t")           # A is the main name of B
    assert store.load_model_synonyms(conn) == {("IKON", "B"): "A"}


def test_the_panel_cache_keeps_only_the_current_view():
    class View:
        def __init__(self, offers):
            self.offers = offers
    first = View({**mim_offers(mim()), **tochki_offers(tochki_row())})
    assert tire_match.cached_pairs(first, {}, 2).groups == [("100", "s2-TCH1")]
    second = View(mim_offers(mim("300")))                                  # a new unload: the old positions are let go
    assert tire_match.cached_pairs(second, {}, 2).groups == []
    assert {v[0].goods_id for v in tire_match._cache["entry"][4].values()} == {"300"}   # the memo, kept in one entry (run tails4)
    assert tire_match.cached_pairs(second, {("IKON", "CHARACTERICE7"): "X"}, 2) is not tire_match.cached_pairs(second, {}, 2)
