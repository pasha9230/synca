from __future__ import annotations

import pytest

from tires_sync import store, terms

NOW = "2026-10-09T10:00:00"


@pytest.fixture
def conn(tmp_path):
    return store.connect(tmp_path / "t.db")


def row(**kw):
    base = {"supplier_id": 1, "brand_key": "GISLAVED", "season": "summer", "size_class": "small", "markdown_pct": -8.5,
            "discount_pct": 0, "bonus_pct": 13, "monthly_pct": 10, "valid_from": "2026-09-01"}
    base.update(kw)
    return base


def test_a_term_is_saved_listed_replaced_for_the_same_start_and_deleted(conn):
    terms.save_term(conn, row(), NOW, "owner")
    terms.save_term(conn, row(bonus_pct=12), NOW, "owner")                     # same brand, season, size, start: replaced
    terms.save_term(conn, row(valid_from="2026-10-01", bonus_pct=11), NOW, "owner")
    listed = terms.list_terms(conn)
    assert [(t["valid_from"], t["bonus_pct"]) for t in listed] == [("2026-09-01", 12.0), ("2026-10-01", 11.0)]
    terms.delete_term(conn, listed[0]["id"], NOW, "owner")
    assert len(terms.list_terms(conn)) == 1
    assert [e["action"] for e in store.list_audit(conn, 10)] == ["term_deleted", "term_saved", "term_saved", "term_saved"]


@pytest.mark.parametrize("bad", [
    {"brand_key": " "}, {"season": "spring"}, {"size_class": "huge"}, {"valid_from": "01.09.2026"}, {"valid_from": "2026-13-40"},
    {"bonus_pct": 80}, {"discount_pct": -1}, {"markdown_pct": float("nan")}, {"monthly_pct": "abc"}, {"markdown_pct": -60},
])
def test_bad_terms_are_refused(conn, bad):
    with pytest.raises(ValueError):
        terms.save_term(conn, row(**bad), NOW, "owner")
    assert terms.list_terms(conn) == []


CSV = """Бренд;Сезон;Диаметр;Уценка (09.2026);Скидка, %;Бонус, %;ЕЖ, %
Gislaved;лето;до R17 вкл.;-8,5;;13;10
Gislaved;лето;выше R17;-9,2;;13;10
IKON;зима;до R17 вкл.;-5;;7;10
Formula;зима;выше R17;-7;;9;10
Cordiant;зима;от R15 до R17 вкл.;2,5;9;;10
NEXEN;зима;все радиусы;3;9;;10
Nope;лето;все радиусы;1;;;10
Gislaved;осень;выше R17;1;;;10
"""
KEYS = ["GISLAVED", "IKON TYRES", "PIRELLI FORMULA", "CORDIANT", "NEXEN", "PIRELLI"]


def test_the_owners_table_is_imported_with_brands_matched_to_the_panels_keys(conn):
    done, errors = terms.import_csv(conn, CSV, "2026-09-01", KEYS, NOW, "owner")
    assert done == 6 and len(errors) == 2
    assert "Nope" in errors[0] and "осень" in errors[1]
    got = {(t["brand_key"], t["season"], t["size_class"]): t for t in terms.list_terms(conn)}
    g = got[("GISLAVED", "summer", "small")]
    assert (g["markdown_pct"], g["discount_pct"], g["bonus_pct"], g["monthly_pct"], g["valid_from"]) == (-8.5, 0.0, 13.0, 10.0, "2026-09-01")
    assert ("GISLAVED", "summer", "large") in got and got[("GISLAVED", "summer", "large")]["markdown_pct"] == -9.2
    assert ("IKON TYRES", "winter", "small") in got and ("PIRELLI FORMULA", "winter", "large") in got
    assert got[("CORDIANT", "winter", "small")]["discount_pct"] == 9.0 and got[("CORDIANT", "winter", "small")]["bonus_pct"] == 0.0
    assert got[("NEXEN", "winter", "all")]["markdown_pct"] == 3.0


def test_import_without_the_needed_columns_or_with_no_rows_is_explained(conn):
    with pytest.raises(ValueError):
        terms.import_csv(conn, "a;b\n1;2\n", "2026-09-01", KEYS, NOW, "owner")
    with pytest.raises(ValueError):
        terms.import_csv(conn, "Бренд;Сезон;Диаметр;Уценка\n", "2026-09-01", KEYS, NOW, "owner")
    with pytest.raises(ValueError):
        terms.import_csv(conn, CSV, "not a date", KEYS, NOW, "owner")


def test_an_ambiguous_brand_name_is_not_guessed(conn):
    keys = ["PIRELLI", "PIRELLI FORMULA"]
    done, errors = terms.import_csv(conn, "Бренд;Сезон;Диаметр;Уценка\nPirelli;зима;все радиусы;-7\n", "2026-09-01", keys, NOW, "owner")
    assert done == 1 and terms.list_terms(conn)[0]["brand_key"] == "PIRELLI"          # the exact name wins
    done, errors = terms.import_csv(conn, "Бренд;Сезон;Диаметр;Уценка\nFormul;зима;все радиусы;-7\n", "2026-09-01", ["PIRELLI FORMULA", "FORMULA PLUS"], NOW, "owner")
    assert done == 0 and errors


def test_pricing_percents_come_from_the_markdown_column(conn):
    terms.import_csv(conn, CSV, "2026-09-01", KEYS, NOW, "owner")
    pct, missing = terms.pricing_from_terms(terms.list_terms(conn), "2026-09-15")
    assert pct["GISLAVED|summer"] == {"pct_small": -8.5, "pct_large": -9.2}
    assert pct["NEXEN|winter"] == {"pct_small": 3.0, "pct_large": 3.0}                 # «все радиусы» fills both
    assert "CORDIANT|winter" not in pct and "CORDIANT|winter" in missing               # only the small size is given: no half rule
    assert "IKON TYRES|winter" in missing and "PIRELLI FORMULA|winter" in missing


def test_a_saved_term_can_be_changed_in_place_and_the_change_is_journaled(conn):
    terms.save_term(conn, row(), NOW, "owner")
    tid = terms.list_terms(conn)[0]["id"]
    terms.update_term(conn, tid, row(bonus_pct=12, markdown_pct=-7, brand_key=" gislaved ", valid_from="2026-10-01"), NOW, "owner")
    listed = terms.list_terms(conn)
    assert len(listed) == 1 and listed[0]["id"] == tid
    assert (listed[0]["bonus_pct"], listed[0]["markdown_pct"], listed[0]["brand_key"], listed[0]["valid_from"]) == (12.0, -7.0, "GISLAVED", "2026-10-01")
    event = store.list_audit(conn, 1)[0]
    assert event["action"] == "term_updated"


def test_a_change_keeps_the_supplier_of_the_row(conn):
    terms.save_term(conn, row(supplier_id=1), NOW, "owner")
    tid = terms.list_terms(conn)[0]["id"]
    terms.update_term(conn, tid, row(supplier_id=7, bonus_pct=5), NOW, "owner")
    assert terms.list_terms(conn)[0]["supplier_id"] == 1


def test_a_change_into_another_existing_term_is_refused_and_nothing_is_lost(conn):
    terms.save_term(conn, row(), NOW, "owner")
    terms.save_term(conn, row(season="winter"), NOW, "owner")
    first = [t for t in terms.list_terms(conn) if t["season"] == "summer"][0]
    with pytest.raises(ValueError, match="уже есть"):
        terms.update_term(conn, first["id"], row(season="winter"), NOW, "owner")
    assert sorted(t["season"] for t in terms.list_terms(conn)) == ["summer", "winter"]


def test_a_change_of_a_missing_term_is_explained(conn):
    with pytest.raises(ValueError, match="уже нет"):
        terms.update_term(conn, 999, row(), NOW, "owner")


@pytest.mark.parametrize("bad", [{"brand_key": " "}, {"bonus_pct": 80}, {"valid_from": "01.09.2026"}, {"season": "spring"}])
def test_a_bad_change_is_refused_and_the_term_stays_as_it_was(conn, bad):
    terms.save_term(conn, row(), NOW, "owner")
    tid = terms.list_terms(conn)[0]["id"]
    with pytest.raises(ValueError):
        terms.update_term(conn, tid, row(**bad), NOW, "owner")
    assert terms.list_terms(conn)[0]["bonus_pct"] == 13.0
