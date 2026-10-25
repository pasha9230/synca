from __future__ import annotations

from tires_sync.candidates import Evaluation
from tires_sync.catalog import classify_model
from tires_sync.mappings import pending_mappings

IKON = {"Ikon Tyres": frozenset({"Autograph Ice 9 SUV", "Autograph Ice 9", "Character Ice 7"})}
KUMHO = {"Kumho": frozenset({"WinterCraft Ice Wi32", "Ecowing ES31", "Road Venture AT52"})}
YOKO = {"Yokohama": frozenset({"Ice Guard G075", "Ice Guard SUV G075", "BluEarth AE51"})}


def test_exact_after_normalisation():
    assert classify_model("Ikon Tyres", "character ice-7", IKON) == ("exact", ["Character Ice 7"])


def test_brand_word_in_front_is_stripped_and_picks_the_longer_exact_model():
    assert classify_model("Ikon Tyres", "Ikon Autograph Ice 9 SUV", IKON) == ("prefix", ["Autograph Ice 9 SUV"])
    assert classify_model("Ikon Tyres", "Ikon Autograph Ice 9", IKON) == ("prefix", ["Autograph Ice 9"])


def test_code_found_in_exactly_one_model_is_confident():
    assert classify_model("Kumho", "WI-32", KUMHO) == ("code", ["WinterCraft Ice Wi32"])
    assert classify_model("Kumho", "AT52", KUMHO) == ("code", ["Road Venture AT52"])


def test_code_found_in_several_models_is_ambiguous_and_lists_them():
    tier, candidates = classify_model("Yokohama", "G075", YOKO)
    assert tier == "ambiguous" and candidates == ["Ice Guard G075", "Ice Guard SUV G075"]


def test_short_codes_are_never_guessed():
    assert classify_model("Kumho", "ES3", KUMHO)[0] == "none"


def test_unknown_model_gets_fuzzy_suggestions_only():
    tier, candidates = classify_model("Kumho", "Winter Craft Ice Wi31", KUMHO)
    assert tier == "none" and "WinterCraft Ice Wi32" in candidates


def test_pending_mappings_carry_tier_and_a_single_proposal_for_confident_ones():
    ev = Evaluation()
    ev.unmatched = {"1": ("Ikon Tyres", "Ikon Autograph Ice 9 SUV"), "2": ("Yokohama", "G075"), "3": ("Kumho", "Zzz")}
    ref = {**IKON, **YOKO, **KUMHO}
    by = {p.model_raw: p for p in pending_mappings(ev, ref)}
    assert (by["Ikon Autograph Ice 9 SUV"].tier, by["Ikon Autograph Ice 9 SUV"].proposal) == ("prefix", "Autograph Ice 9 SUV")
    assert by["Ikon Autograph Ice 9 SUV"].suggestions[0] == "Autograph Ice 9 SUV"
    assert by["G075"].tier == "ambiguous" and by["G075"].proposal is None and len(by["G075"].suggestions) == 2
    assert by["Zzz"].tier == "none" and by["Zzz"].proposal is None


def test_reference_accepts_the_avito_file_format_with_closing_model_tags():
    from tires_sync.catalog import parse_reference
    text = '<?xml version="1.0" ?>\n<Shiny>\n\t<make name="Ikon Tyres">\n\t\t<model name="Nordman 7"></model>\n\t\t<model name="Nordman 8"/>\n\t</make>\n</Shiny>\n'
    assert parse_reference(text) == {"Ikon Tyres": frozenset({"Nordman 7", "Nordman 8"})}


def test_brand_word_in_front_of_the_model_is_resolved_automatically_but_code_matches_are_not():
    from tires_sync.catalog import parse_reference, resolve_model
    ref = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Character Aqua SUV"/>\n<model name="Nordman 7 SUV"/>\n</make>\n</Shiny>')
    assert resolve_model("Ikon Tyres", "Ikon Character Aqua SUV", {}, ref) == "Character Aqua SUV"
    assert resolve_model("Ikon Tyres", "Ikon Tyres Character Aqua SUV", {}, ref) == "Character Aqua SUV"
    assert resolve_model("Ikon Tyres", "Aqua SUV", {}, ref) is None
    assert resolve_model("Ikon Tyres", "Ikon Unknown", {}, ref) is None
