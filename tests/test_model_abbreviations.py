"""The supplier writes 'SC VERDE …' for Pirelli Scorpion Verde: abbreviations and spacing variants are one model."""
from __future__ import annotations

from helpers import catalog_xml, commodity_xml
from panel_helpers import login, make_app
from tires_sync import store
from tires_sync.candidates import evaluate
from tires_sync.catalog import classify_model, parse_reference, resolve_model
from tires_sync.mappings import CONFIDENT, pending_mappings
from helpers import make_offer, make_settings

REF = parse_reference(
    '<Shiny>\n<make name="Pirelli">\n<model name="Scorpion Verde"/>\n<model name="Scorpion Verde SUV"/>\n'
    '<model name="Scorpion Verde All Season"/>\n<model name="Scorpion Verde All Season SUV"/>\n'
    '<model name="Scorpion ATR SUV"/>\n<model name="Cinturato P1 Verde"/>\n<model name="Cinturato P7"/>\n</make>\n</Shiny>')


def test_the_sc_abbreviation_is_expanded_to_scorpion_and_is_a_confident_proposal():
    cases = {
        "SC VERDE SUV": "Scorpion Verde SUV",
        "SC VERDE All-Season SUV": "Scorpion Verde All Season SUV",
        "SC VERDE All-Season": "Scorpion Verde All Season",
        "SC  VERDE SUV": "Scorpion Verde SUV",            # a doubled space is the same model
        "sc verde suv": "Scorpion Verde SUV",
    }
    for raw, expected in cases.items():
        tier, found = classify_model("Pirelli", raw, REF)
        assert tier in CONFIDENT and found == [expected], (raw, tier, found)


def test_cint_is_expanded_too_and_other_brands_are_untouched():
    tier, found = classify_model("Pirelli", "CINT P7", REF)
    assert tier in CONFIDENT and found == ["Cinturato P7"]
    other = parse_reference('<Shiny>\n<make name="Kumho">\n<model name="Scorpion Verde"/>\n</make>\n</Shiny>')
    assert classify_model("Kumho", "SC VERDE", other)[0] == "none"          # the rule is Pirelli's


def test_an_abbreviation_never_guesses_between_two_models():
    ref = parse_reference('<Shiny>\n<make name="Pirelli">\n<model name="Scorpion X"/>\n<model name="Scorpion  X"/>\n</make>\n</Shiny>')
    assert classify_model("Pirelli", "SC X", ref)[0] in ("none", "ambiguous", "exact", "prefix")      # no crash on duplicates


def test_resolving_uses_the_expansion_only_after_confirmation_of_a_proposal():
    # automatic resolution must not silently map an abbreviation: it stays a proposal until accepted
    assert resolve_model("Pirelli", "SC VERDE SUV", {}, REF) is None


def pending(*models):
    offers = {str(i): make_offer(goods_id=str(i), brand_key="PIRELLI", model_raw=m) for i, m in enumerate(models, 1)}
    s = make_settings(enabled=[["PIRELLI", "winter"]], pricing={"PIRELLI|winter": {"pct_small": 0.0, "pct_large": 0.0}})
    ev = evaluate(offers, {}, s, REF, {}, None)
    return pending_mappings(ev, REF)


def test_spacing_variants_make_one_pending_line_with_all_their_goods():
    rows = pending("SC VERDE SUV", "SC  VERDE SUV", "sc verde suv")
    assert len(rows) == 1 and rows[0].goods_count == 3 and rows[0].proposal == "Scorpion Verde SUV"


def test_catalog_models_list_merges_spacing_variants(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(
        commodity_xml(NNOMMODIF="1", SMODEL="Test Model"),
        commodity_xml(NNOMMODIF="2", SMODEL="Test  Model", SMNFCODE="C2"),
        commodity_xml(NNOMMODIF="3", SMODEL="test model", SMNFCODE="C3")))
    from tires_sync.panel import service
    from tires_sync.panel.data import load_merged
    from tires_sync.settings_store import active_settings, get_active
    conn = store.connect(app.config["DB_PATH"])
    parsed = load_merged(conn, active_settings(conn), app.config["UNLOAD_PATH"])
    ref = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
    rows = service.brands_overview(conn, get_active(conn)[1], parsed, ref)
    ikon = next(r for r in rows if r["brand_key"] == "IKON TYRES")
    assert len(ikon["models"]) == 1 and ikon["models"][0]["goods"] == 3


# --- the sweep over every brand ---------------------------------------------------------------------------------------

from tires_sync.mappings import auto_mappings, is_safe_proposal


def test_safe_proposal_rule():
    assert is_safe_proposal("Kumho", "CW-51", "Winter PorTran CW51")                      # the code is the end of the model
    assert is_safe_proposal("Pirelli", "SC VERDE SUV", "Scorpion Verde SUV")              # an expanded abbreviation is exact
    assert is_safe_proposal("КАМА", "НК-242", "Кама 365 SUV (НК-242)")                    # the code in the trailing brackets
    assert not is_safe_proposal("Roadstone", "ROADIAN HP", "Roadian HP SUV")              # the supplier text is only the start
    assert not is_safe_proposal("Kumho", "PS-72 S", "Ecsta PS72 Sport S K-Silent")        # inside, followed by other words


SWEEP_REF = parse_reference(
    '<Shiny>\n<make name="Kumho">\n<model name="Winter PorTran CW51"/>\n<model name="Ecsta PS72 Sport S K-Silent"/>\n'
    '<model name="Road Venture AT52"/>\n</make>\n<make name="Pirelli">\n<model name="Scorpion Verde SUV"/>\n</make>\n'
    '<make name="Roadstone">\n<model name="Roadian HP SUV"/>\n</make>\n</Shiny>')


def sweep_offers():
    def o(i, brand, model):
        return make_offer(goods_id=str(i), brand_key=brand, model_raw=model)
    return {str(i): o(i, b, m) for i, (b, m) in enumerate([
        ("KUMHO", "CW-51"), ("KUMHO", "CW-51"), ("KUMHO", "PS-72 S"), ("KUMHO", "AT52"), ("PIRELLI", "SC VERDE SUV"),
        ("PIRELLI", "SC  VERDE SUV"), ("ROADSTONE", "ROADIAN HP"), ("KUMHO", "Winter PorTran CW51")], 1)}


def test_sweep_accepts_only_safe_proposals_and_leaves_the_rest(tmp_path):
    conn = store.connect(tmp_path / "t.db")

    class Parsed:
        offers = sweep_offers()

    report = auto_mappings(conn, Parsed, SWEEP_REF, "2026-10-14T10:00:00", "server", apply=True)
    accepted = {(a["brand"], a["model"]): a["to"] for a in report["accepted"]}
    assert accepted == {("Kumho", "CW-51"): "Winter PorTran CW51", ("Kumho", "AT52"): "Road Venture AT52",
                        ("Pirelli", "SC VERDE SUV"): "Scorpion Verde SUV"}
    assert {(h["brand"], h["model"]) for h in report["held"]} == {("Kumho", "PS-72 S"), ("Roadstone", "ROADIAN HP")}
    confirmed = store.load_confirmed_mappings(conn)
    assert confirmed[("Kumho", "CW51")] == "Winter PorTran CW51" and ("Roadstone", "ROADIANHP") not in confirmed


def test_sweep_without_apply_changes_nothing_and_second_run_finds_nothing_new(tmp_path):
    conn = store.connect(tmp_path / "t.db")

    class Parsed:
        offers = sweep_offers()

    dry = auto_mappings(conn, Parsed, SWEEP_REF, "2026-10-14T10:00:00", "server", apply=False)
    assert dry["accepted"] and store.load_confirmed_mappings(conn) == {}
    auto_mappings(conn, Parsed, SWEEP_REF, "2026-10-14T10:00:00", "server", apply=True)
    again = auto_mappings(conn, Parsed, SWEEP_REF, "2026-10-14T10:00:00", "server", apply=True)
    assert again["accepted"] == []
