import pytest

from tires_sync import store
from tires_sync.candidates import Evaluation
from tires_sync.catalog import parse_reference, resolve_model
from tires_sync.mappings import MappingError, confirm_mapping, pending_mappings

REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Autograph Ice 9"/>\n<model name="Character Ice 7"/>\n</make>\n</Shiny>')


def test_pending_groups_goods_by_pair_and_sorts_by_count():
    ev = Evaluation(unmatched={
        "1": ("Ikon Tyres", "Ikon Autograph Ice 9"),
        "2": ("Ikon Tyres", "Ikon Autograph Ice 9"),
        "3": ("Ikon Tyres", "Ikon Character Ice 7"),
    })
    pending = pending_mappings(ev, REF)
    assert [(p.model_raw, p.goods_count) for p in pending] == [
        ("Ikon Autograph Ice 9", 2), ("Ikon Character Ice 7", 1)]
    assert pending[0].goods_ids == ["1", "2"]
    assert pending[0].suggestions == ["Autograph Ice 9"]


def test_confirm_mapping_stores_it_audits_and_makes_resolution_work(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    confirm_mapping(conn, "Ikon Tyres", "Ikon Autograph Ice 9", "Autograph Ice 9", REF, "2026-10-07T10:00:00", "owner")
    confirmed = store.load_confirmed_mappings(conn)
    assert resolve_model("Ikon Tyres", "ikon autograph ice-9", confirmed, REF) == "Autograph Ice 9"
    assert store.list_audit(conn)[0]["action"] == "mapping_confirm"


def test_confirm_mapping_rejects_model_missing_from_reference(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    with pytest.raises(MappingError):
        confirm_mapping(conn, "Ikon Tyres", "Whatever", "No Such Model", REF, "2026-10-07T10:00:00", "owner")
    assert store.load_confirmed_mappings(conn) == {}


def test_reconfirming_the_same_pair_replaces_instead_of_duplicating(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    confirm_mapping(conn, "Ikon Tyres", "X Model", "Autograph Ice 9", REF, "2026-10-07T10:00:00", "owner")
    confirm_mapping(conn, "Ikon Tyres", "x-model", "Character Ice 7", REF, "2026-10-07T11:00:00", "owner")
    assert list(store.load_confirmed_mappings(conn).values()) == ["Character Ice 7"]


from tires_sync.mappings import confirm_brand_mapping


def test_confirm_brand_mapping_validates_against_reference_and_audits(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    confirm_brand_mapping(conn, "LAUFEN", "Ikon Tyres", REF, "2026-10-08T10:00:00", "owner")
    assert store.load_brand_map(conn) == {"LAUFEN": "Ikon Tyres"}
    assert store.list_audit(conn)[0]["action"] == "brand_mapping_confirm"
    with pytest.raises(MappingError):
        confirm_brand_mapping(conn, "X", "No Such Brand", REF, "2026-10-08T10:00:00", "owner")
    assert store.load_brand_map(conn) == {"LAUFEN": "Ikon Tyres"}
