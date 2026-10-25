from __future__ import annotations

from tires_sync.catalog import classify_model, resolve_brand, resolve_model, suggest_brand
from tires_sync.mappings import is_safe_proposal

REF = {
    "Michelin": frozenset({"Primacy 4", "Pilot Sport 4"}),
    "Nokian Tyres": frozenset({"Hakkapeliitta 10", "Nordman 8"}),
    "Matador": frozenset({"MP 47"}),
}


def test_lookalike_cyrillic_letters_match_the_latin_model_silently():
    raw = "Рrimacy 4"          # Cyrillic Р instead of Latin P
    assert classify_model("Michelin", raw, REF) == ("exact", ["Primacy 4"])
    assert resolve_model("Michelin", raw, {}, REF) == "Primacy 4"


def test_russian_spelling_is_a_confident_proposal_not_applied_silently():
    raw = "Хакапелиита 10"
    assert classify_model("Nokian Tyres", raw, REF) == ("code", ["Hakkapeliitta 10"])
    assert resolve_model("Nokian Tyres", raw, {}, REF) is None
    assert is_safe_proposal("Nokian Tyres", raw, "Hakkapeliitta 10")


def test_russian_spelling_that_fits_two_models_is_not_guessed():
    ref = {"Nokian Tyres": frozenset({"Nordman 8", "Nordman 8 SUV"})}
    assert classify_model("Nokian Tyres", "Нордман 8", ref) == ("code", ["Nordman 8"])    # exact sound match wins
    ref = {"Nokian Tyres": frozenset({"Nordmann 8", "Nordman 8"})}                    # both sound the same
    assert classify_model("Nokian Tyres", "Нордман 8", ref)[0] != "code"


def test_brand_written_in_cyrillic_resolves_to_the_latin_brand():
    assert resolve_brand("МICHELIN", REF) == "Michelin"      # Cyrillic М
    assert resolve_brand("S2:ММICHELIN", REF) is None
    assert resolve_brand("S2:МICHELIN", REF) == "Michelin"
    assert resolve_brand("МАТАДОР", REF) == "Matador"       # a listed brand in Russian letters is applied
    assert resolve_brand("ДЖАНКИН", {"Junkin": frozenset()}) is None   # an unlisted one is only suggested, never applied silently
    assert resolve_brand("НЕТАКОЙ", REF) is None


def test_brand_suggestion_uses_the_russian_spelling():
    assert suggest_brand("МАТАДОР", REF)[0] == "Matador"


def test_plain_latin_input_is_unchanged():
    assert classify_model("Michelin", "Primacy 4", REF)[0] == "exact"
    assert resolve_model("Michelin", "Unknown X", {}, REF) is None


def test_digits_are_never_squashed_when_comparing_by_sound():
    ref = {"Nokian Tyres": frozenset({"Nordman 7", "Nordman 8"})}
    assert classify_model("Nokian Tyres", "Нордман 77", ref)[0] != "code"
    assert classify_model("Nokian Tyres", "Нордман 7", ref) == ("code", ["Nordman 7"])
    assert not is_safe_proposal("Nokian Tyres", "Нордман 77", "Nordman 7")


WORDS_REF = {"Nokian Tyres": frozenset({"Business 4", "Off Road 2", "Cross Climate SUV", "Hakkapeliitta 10"}),
             "Michelin": frozenset({"Energy Saver", "Ice Grip"})}


def test_common_russian_words_of_a_model_find_the_english_model():
    assert classify_model("Nokian Tyres", "Бизнес 4", WORDS_REF) == ("code", ["Business 4"])
    assert classify_model("Nokian Tyres", "Офф Роуд 2", WORDS_REF) == ("code", ["Off Road 2"])
    assert classify_model("Nokian Tyres", "ОффРоуд 2", WORDS_REF) == ("code", ["Off Road 2"])
    assert classify_model("Nokian Tyres", "Кросс Клаймат СУВ", WORDS_REF) == ("code", ["Cross Climate SUV"])
    assert classify_model("Michelin", "Энерджи Сейвер", WORDS_REF) == ("code", ["Energy Saver"])
    assert classify_model("Michelin", "Айс Грип", WORDS_REF) == ("code", ["Ice Grip"])


def test_such_matches_are_taken_by_the_automatic_run_but_never_applied_silently():
    for raw, wanted in (("Бизнес 4", "Business 4"), ("Офф Роуд 2", "Off Road 2"), ("Айс Грип", "Ice Grip")):
        ref_brand = "Michelin" if "Грип" in raw else "Nokian Tyres"
        assert is_safe_proposal(ref_brand, raw, wanted)
        assert resolve_model(ref_brand, raw, {}, WORDS_REF) is None


def test_the_number_keeps_models_apart_and_unknown_words_do_not_match():
    assert classify_model("Nokian Tyres", "Бизнес 5", WORDS_REF)[0] != "code"
    assert classify_model("Nokian Tyres", "Бизнес", WORDS_REF)[0] == "none"


def test_one_model_in_another_spelling_is_a_suggestion_for_latin_names_too():
    ref = {"Ikon Tyres": frozenset({"Character Eco", "Autograph Ice 9"})}
    assert classify_model("Ikon Tyres", "Charakter Eco", ref) == ("code", ["Character Eco"])
    assert classify_model("Ikon Tyres", "Autograph Ice 9", ref) == ("exact", ["Autograph Ice 9"])


def test_well_known_brands_in_russian_letters_are_resolved_from_the_explicit_list():
    ref = {"Nokian Tyres": frozenset({"Hakkapeliitta 10"}), "Michelin": frozenset({"Primacy 4"}), "Pirelli": frozenset({"P Zero"})}
    assert resolve_brand("НОКИАН", ref) == "Nokian Tyres"
    assert resolve_brand("S2:МИШЛЕН", ref) == "Michelin"
    assert resolve_brand("Пирелли".upper(), ref) == "Pirelli"
    assert resolve_brand("НЕЗНАЙКА", ref) is None
    assert resolve_brand("НОКИАН", {"Michelin": frozenset()}) is None


def test_apostrophes_dots_and_brackets_do_not_make_another_model_but_a_plus_does():
    assert is_safe_proposal("Nexen", "NFera AU7", "N'Fera AU7")
    assert is_safe_proposal("Viatti", "Strada 2 V-134", "Strada 2 (V-134)")
    assert not is_safe_proposal("DoubleStar", "DW16+", "DW16")
    assert not is_safe_proposal("Roadstone", "ROADIAN HP", "Roadian HP SUV")


# --- review: a plus or an ampersand makes another model ------------------------------------------------------------

PLUS_REF = {"Michelin": frozenset({"Sport", "Sport+", "Ice Zero", "Winter & Ice 2", "Grip 5"})}


def test_a_plus_or_an_ampersand_is_never_dropped_when_comparing_by_sound():
    assert classify_model("Michelin", "Спорт", PLUS_REF)[1] == ["Sport"]
    assert classify_model("Michelin", "Sport+", PLUS_REF) == ("exact", ["Sport+"])
    assert classify_model("Michelin", "Ice Zero+", PLUS_REF)[0] == "none"
    assert classify_model("Michelin", "Winter Ice 2", PLUS_REF)[0] == "none"
    assert not is_safe_proposal("Michelin", "Спорт", "Sport+")
    assert not is_safe_proposal("Michelin", "Spоrt+", "Sport")                 # Cyrillic «о» inside a Latin word
    assert is_safe_proposal("Michelin", "Спорт", "Sport")


def test_a_brand_in_russian_letters_goes_to_the_plain_brand_not_to_a_longer_name():
    ref = {"Michelin": frozenset({"Primacy 4"}), "Michelin Pilot": frozenset({"A", "B", "C"}), "Goodyear Wrangler": frozenset({"X", "Y"})}
    assert resolve_brand("МИШЛЕН", ref) == "Michelin"
    assert resolve_brand("ГУДИЕР", ref) is None                      # only a longer name exists: not a guess
    tyres = {"Nokian Tyres": frozenset({"H"}), "Nokian Pro": frozenset({"A", "B"})}
    assert resolve_brand("НОКИАН", tyres) == "Nokian Tyres"
