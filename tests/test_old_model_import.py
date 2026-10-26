"""Former model names from a file (the owner 2026-10-26: 31 names by a command, not row by row in the panel). The command
`old-model-import` only reads without --apply; with it every row goes through the panel's check and journal in one transaction, and a
refused row stops the whole file unless --skip-bad. An empty name in the file is skipped: removing stays in the panel."""
from __future__ import annotations

import json

import pytest

from tires_sync import store
from tires_sync.cli import main

REFERENCE = ('<Shiny>\n<make name="Ikon Tyres">\n<model name="Autograph Ice 9"/>\n<model name="Autograph Snow 3"/>\n</make>\n'
             '<make name="Pirelli">\n<model name="Ice Zero"/>\n</make>\n</Shiny>')


@pytest.fixture
def paths(tmp_path):
    db = tmp_path / "state.db"
    store.connect(db).close()
    ref = tmp_path / "reference.txt"
    ref.write_text(REFERENCE, encoding="utf-8")
    return tmp_path, db, ref


def run(paths, rows, *flags, header="brand,model,former_name"):
    tmp, db, ref = paths
    csv_file = tmp / "names.csv"
    csv_file.write_text(header + "\n" + "".join(r + "\n" for r in rows), encoding="utf-8")
    return main(["old-model-import", str(csv_file), "--db", str(db), "--reference", str(ref), *flags])


def names(db):
    conn = store.connect(db)
    try:
        return store.load_old_model_names(conn)
    finally:
        conn.close()


def journal(db):
    conn = store.connect(db)
    try:
        return [(r[0], json.loads(r[1])) for r in conn.execute("SELECT author, detail FROM audit WHERE action = 'old_model_name' ORDER BY id")]
    finally:
        conn.close()


def seed(db, brand, model, name):
    conn = store.connect(db)
    store.set_old_model_name(conn, brand, model, name, "2026-10-01T00:00:00", "owner")
    conn.close()


GOOD = ["Ikon Tyres,Autograph Ice 9,Nokian Hakkapeliitta 9", "Pirelli,Ice Zero,Pirelli Winter Ice Zero"]


def test_dry_run_writes_nothing_and_shows_was_and_becomes(paths, capsys):
    _, db, _ = paths
    seed(db, "Pirelli", "Ice Zero", "Old Pirelli")
    assert run(paths, GOOD) == 0
    out = capsys.readouterr().out
    assert "Ikon Tyres Autograph Ice 9: «» → «Nokian Hakkapeliitta 9»" in out
    assert "Pirelli Ice Zero: «Old Pirelli» → «Pirelli Winter Ice Zero»" in out
    assert "ничего не записано" in out and "--apply" in out
    assert names(db) == {("Pirelli", "Ice Zero"): "Old Pirelli"}
    assert journal(db) == []


def test_apply_writes_the_names_and_a_journal_line_per_row(paths, capsys):
    _, db, _ = paths
    assert run(paths, GOOD, "--apply") == 0
    assert names(db) == {("Ikon Tyres", "Autograph Ice 9"): "Nokian Hakkapeliitta 9", ("Pirelli", "Ice Zero"): "Pirelli Winter Ice Zero"}
    assert journal(db) == [
        ("cli", {"avito_brand": "Ikon Tyres", "avito_model": "Autograph Ice 9", "before": "", "after": "Nokian Hakkapeliitta 9"}),
        ("cli", {"avito_brand": "Pirelli", "avito_model": "Ice Zero", "before": "", "after": "Pirelli Winter Ice Zero"}),
    ]
    assert "Записано: 2" in capsys.readouterr().out


def test_a_refused_row_stops_the_whole_apply(paths, capsys):
    _, db, _ = paths
    assert run(paths, GOOD + ["Ikon Tyres,No Such Model,Something"], "--apply") == 1
    out = capsys.readouterr().out
    assert "Модели «No Such Model» нет у бренда «Ikon Tyres»" in out
    assert "ничего не записано" in out and "--skip-bad" in out
    assert names(db) == {}
    assert journal(db) == []


@pytest.mark.parametrize("bad", ["Nobody,Autograph Ice 9,X", "Ikon Tyres,Autograph Snow 3," + "x" * 101,
                                 "Ikon Tyres,Autograph Snow 3,bad​name"])
def test_every_kind_of_refusal_blocks(paths, bad):
    _, db, _ = paths
    assert run(paths, GOOD + [bad], "--apply") == 1
    assert names(db) == {} and journal(db) == []


def test_skip_bad_writes_the_good_rows_only(paths, capsys):
    _, db, _ = paths
    assert run(paths, ["Ikon Tyres,No Such Model,Something"] + GOOD, "--apply", "--skip-bad") == 0
    assert set(names(db)) == {("Ikon Tyres", "Autograph Ice 9"), ("Pirelli", "Ice Zero")}
    assert len(journal(db)) == 2
    out = capsys.readouterr().out
    assert "Записано: 2" in out and "отказ" in out


def test_an_unchanged_row_writes_no_journal_line(paths, capsys):
    _, db, _ = paths
    seed(db, "Ikon Tyres", "Autograph Ice 9", "Nokian Hakkapeliitta 9")
    assert run(paths, [GOOD[0]], "--apply") == 0
    assert "без изменений" in capsys.readouterr().out
    assert journal(db) == []
    assert names(db) == {("Ikon Tyres", "Autograph Ice 9"): "Nokian Hakkapeliitta 9"}


def test_brand_case_does_not_matter_and_the_model_takes_the_avito_spelling(paths):
    _, db, _ = paths
    assert run(paths, ["ikon tyres,autograph-ice  9,  Nokian   Hakkapeliitta 9 "], "--apply") == 0
    assert names(db) == {("Ikon Tyres", "Autograph Ice 9"): "Nokian Hakkapeliitta 9"}


def test_extra_columns_and_their_order_do_not_matter(paths):
    _, db, _ = paths
    assert run(paths, ["note,Nokian Hakkapeliitta 9,Autograph Ice 9,Ikon Tyres,x"], "--apply",
               header="comment,former_name,model,brand,more") == 0
    assert names(db) == {("Ikon Tyres", "Autograph Ice 9"): "Nokian Hakkapeliitta 9"}


def test_an_empty_former_name_is_skipped_not_deleted(paths, capsys):
    _, db, _ = paths
    seed(db, "Pirelli", "Ice Zero", "Old Pirelli")
    assert run(paths, ["Pirelli,Ice Zero,   ", GOOD[0]], "--apply") == 0
    assert "пропущено" in capsys.readouterr().out
    assert names(db) == {("Pirelli", "Ice Zero"): "Old Pirelli", ("Ikon Tyres", "Autograph Ice 9"): "Nokian Hakkapeliitta 9"}
    assert [d["avito_model"] for _, d in journal(db)] == ["Autograph Ice 9"]


def test_the_same_model_twice_in_the_file_is_refused(paths):
    _, db, _ = paths
    assert run(paths, [GOOD[0], "ikon tyres,AUTOGRAPH ICE 9,Another"], "--apply") == 1
    assert names(db) == {}


@pytest.mark.parametrize("header", ["brand,model", "марка,модель,старое", ""])
def test_a_file_without_the_columns_is_an_error(paths, header, capsys):
    _, db, _ = paths
    assert run(paths, GOOD, "--apply", header=header) == 2
    assert "error" in capsys.readouterr().err
    assert names(db) == {}


def test_a_byte_order_mark_does_not_hide_the_first_column(paths):
    tmp, db, ref = paths
    csv_file = tmp / "names.csv"
    csv_file.write_text("brand,model,former_name\n" + GOOD[0] + "\n", encoding="utf-8-sig")
    assert main(["old-model-import", str(csv_file), "--db", str(db), "--reference", str(ref), "--apply"]) == 0
    assert names(db) == {("Ikon Tyres", "Autograph Ice 9"): "Nokian Hakkapeliitta 9"}


def test_a_failure_in_the_middle_of_the_apply_leaves_nothing(paths, monkeypatch):
    _, db, _ = paths
    real = store.write_audit
    calls = []

    def failing(conn, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("disk full")
        return real(conn, *args, **kwargs)

    monkeypatch.setattr(store, "write_audit", failing)
    assert run(paths, GOOD, "--apply") == 2
    monkeypatch.setattr(store, "write_audit", real)
    assert names(db) == {} and journal(db) == []


def test_skip_bad_without_apply_still_writes_nothing(paths):
    _, db, _ = paths
    assert run(paths, GOOD + ["Ikon Tyres,No Such Model,X"], "--skip-bad") == 1
    assert names(db) == {} and journal(db) == []
