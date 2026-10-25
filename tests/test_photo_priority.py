from tires_sync import photo_library as pl
from tires_sync import store

NOW = "2026-10-21T10:00:00"


def _set(conn, key, rank, brand, model, studded=1, cls="r15_18", state="needed"):
    conn.execute("INSERT INTO photo_set (key, state, rank, brand_name, model_name, model_key, season, studded, size_class)"
                 " VALUES (?,?,?,?,?,?,'winter',?,?)", (key, state, rank, brand, model, model.replace(" ", "").upper(), studded, cls))


def _db(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    _set(conn, "wi32", 0, "Kumho", "WinterCraft Ice Wi32")
    _set(conn, "ice7suv", 1, "Ikon Tyres", "Character Ice 7 SUV")
    _set(conn, "ice7", 2, "Ikon Tyres", "Character Ice 7")
    _set(conn, "ice8", 3, "Ikon Tyres", "CHARACTER ICE 8")
    _set(conn, "snow3suv", 4, "Ikon Tyres", "Autograph Snow 3 SUV", studded=0)
    _set(conn, "wiz", 5, "Pirelli", "Winter Ice Zero")
    _set(conn, "izfr", 6, "Pirelli", "Ice Zero FR", studded=0)
    _set(conn, "ice8big", 7, "Ikon Tyres", "Character Ice 8", cls="r19p")
    conn.commit()
    return conn


def _order(conn):
    return [r[0] for r in conn.execute("SELECT key FROM photo_set WHERE state='needed' ORDER BY rank")]


SOLD = """Ikon Tyres 205/55/16 T 94 Ikon Character Ice 8 XL Ш.
Ikon Tyres 185/65/15 T 92 Ikon Character Ice 8 XL Ш.
Товар Ikon Tyres 225/50/17 T 98 Ikon Character Ice 7 XL Ш.
Kumho 225/65/17 T 106 WI32 Ш. CN
Pirelli 195/65/15 T 95 WINTER ICE ZERO FR XL
Ikon Tyres  195/55/16  R 91 Ikon Autograph Snow 3  XL
Ikon Tyres 175/70/14 T 84 Ikon Character Ice 8 Ш.
Ikon Tyres 195/55/16 H 91 Ikon
Magnetto Lada Vesta 15009 AM 6,0\\R15 4*100 ET50 d60,1 black
"""


def test_matches_by_brand_class_and_name(tmp_path):
    conn = _db(tmp_path)
    matched, unmatched = pl.priority_matches(conn, SOLD)
    assert matched == {"ice8": 2, "ice7": 1, "wi32": 1, "izfr": 1}
    # R14 is out of scope; SUV and non-SUV never meet; a line without a model and non-tire lines are reported
    assert "Ikon Tyres Autograph Snow 3 (R15–R18)" in unmatched
    assert any("Ikon Tyres 195/55/16 H 91 Ikon" in u for u in unmatched)
    assert any("Magnetto" in u for u in unmatched)
    assert not any("175/70/14" in u for u in unmatched)


def test_apply_priority_puts_sold_first_by_count(tmp_path):
    conn = _db(tmp_path)
    store.set_state(conn, pl.PRIORITY_KEY, SOLD)
    pl.apply_priority(conn)
    assert _order(conn) == ["ice8", "wi32", "ice7", "izfr", "ice7suv", "snow3suv", "wiz", "ice8big"]


def test_no_priority_text_keeps_order(tmp_path):
    conn = _db(tmp_path)
    before = _order(conn)
    pl.apply_priority(conn)
    assert _order(conn) == before


def test_sync_sets_keeps_priority(tmp_path):
    from tests.test_photo_queue import cands, offer
    conn = store.connect(tmp_path / "t.db")
    store.set_state(conn, pl.PRIORITY_KEY, "Ikon Tyres 205/55/16 T 94 Ikon Zeta XL Ш.\n")
    ok = cands(offer(1, model_raw="Alpha", brand_raw="Ikon Tyres", studded=True), offer(2, model_raw="Zeta", brand_raw="Ikon Tyres"))
    pl.sync_sets(conn, pl.needed_sets(ok, 4), NOW)
    first = conn.execute("SELECT model_key FROM photo_set WHERE state='needed' ORDER BY rank LIMIT 1").fetchone()[0]
    assert first == "ZETA"
