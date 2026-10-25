from tires_sync.catalog import normalize_model, parse_reference, resolve_brand, resolve_model, suggest

REF_TEXT = (
    '<Shiny>\r\n<make name="Ikon Tyres">\r\n<model name="Character Ice 7"/>\r\n<model name="Nordman 8 SUV"/>\r\n</make>\r\n'
    '<make name="Pirelli">\r\n<model name="Turanza ER-50 S&S AQ"/>\r\n<model name="Ice Zero"/>\r\n</make>\r\n'
    '<make name="Formula">\r\n<model name="Ice"/>\r\n</make>\r\n'
    '<make name="NEXEN">\r\n<model name="A"/>\r\n</make>\r\n<make name="Nexen">\r\n<model name="A"/>\r\n<model name="B"/>\r\n</make>\r\n'
    '</Shiny>'
)
REF = parse_reference(REF_TEXT)


def test_parse_reference_survives_unescaped_ampersand():
    assert "Turanza ER-50 S&S AQ" in REF["Pirelli"]
    assert REF["Ikon Tyres"] == frozenset({"Character Ice 7", "Nordman 8 SUV"})


def test_parse_reference_handles_escaped_entities():
    ref = parse_reference('<Shiny><make name="A"><model name="X &amp; Y"/></make></Shiny>'.replace("><", ">\n<"))
    assert "X & Y" in ref["A"]


def test_resolve_brand_uses_aliases_and_case_insensitive_match():
    assert resolve_brand("IKON TYRES", REF) == "Ikon Tyres"
    assert resolve_brand("PIRELLI", REF) == "Pirelli"
    assert resolve_brand("PIRELLI FORMULA", REF) == "Formula"
    assert resolve_brand("UNKNOWN", REF) is None


def test_resolve_brand_is_deterministic_when_reference_has_two_cases():
    assert resolve_brand("NEXEN", REF) == "Nexen"  # у второго больше моделей


def test_resolve_model_exact_after_normalization():
    assert resolve_model("Ikon Tyres", "character-ice 7", {}, REF) == "Character Ice 7"
    assert normalize_model("A/T 1-2_3") == "AT123"


def test_confirmed_mapping_wins_over_exact():
    confirmed = {("Ikon Tyres", normalize_model("Ikon Character Ice 7")): "Character Ice 7"}
    assert resolve_model("Ikon Tyres", "Ikon Character Ice 7", confirmed, REF) == "Character Ice 7"


def test_unmatched_model_returns_none_and_never_first_model():
    assert resolve_model("Ikon Tyres", "Totally New", {}, REF) is None


def test_suggest_returns_close_models():
    assert suggest("Ikon Tyres", "Nordman 8", REF) == ["Nordman 8 SUV"]


def test_kama_alias_resolves_to_long_avito_name():
    ref = parse_reference('<Shiny>\n<make name="КАМА (Нижнекамский шинный завод)">\n<model name="Alga"/>\n</make>\n</Shiny>')
    assert resolve_brand("КАМА", ref) == "КАМА (Нижнекамский шинный завод)"


def test_suggest_prefers_longest_contained_model_first():
    ref = parse_reference(
        '<Shiny>\n<make name="Ikon Tyres">\n<model name="Autograph Ice 9"/>\n<model name="Autograph Ice 9 SUV"/>\n'
        '<model name="Autograph Ice 10"/>\n</make>\n</Shiny>')
    assert suggest("Ikon Tyres", "Ikon Autograph Ice 9 SUV", ref) == ["Autograph Ice 9 SUV", "Autograph Ice 9"]


def test_suggest_finds_short_supplier_code_inside_long_avito_name():
    ref = parse_reference('<Shiny>\n<make name="Kumho">\n<model name="WinterCraft Ice Wi32"/>\n<model name="Ecsta PS71"/>\n</make>\n</Shiny>')
    assert suggest("Kumho", "WI-32", ref) == ["WinterCraft Ice Wi32"]


def test_suggest_returns_nothing_for_unrelated_names_and_ignores_tiny_fragments():
    ref = parse_reference('<Shiny>\n<make name="Kumho">\n<model name="WinterCraft Ice Wi32"/>\n</make>\n</Shiny>')
    assert suggest("Kumho", "Totally Unrelated Thing", ref) == []
    assert suggest("Kumho", "I", ref) == []


from tires_sync.catalog import suggest_brand


def test_brand_map_overrides_default_resolution_only_when_target_exists():
    ref = parse_reference('<Shiny>\n<make name="Laufenn">\n<model name="G Fit"/>\n</make>\n</Shiny>')
    assert resolve_brand("LAUFEN", ref) is None
    assert resolve_brand("LAUFEN", ref, {"LAUFEN": "Laufenn"}) == "Laufenn"
    assert resolve_brand("LAUFEN", ref, {"LAUFEN": "No Such Brand"}) is None


def test_suggest_brand_finds_close_names_and_nothing_for_unrelated():
    ref = parse_reference('<Shiny>\n<make name="Laufenn">\n</make>\n<make name="Pirelli">\n</make>\n</Shiny>')
    assert suggest_brand("LAUFEN", ref) == ["Laufenn"]
    assert suggest_brand("ВОЛЖ.ШЗ", ref) == []
