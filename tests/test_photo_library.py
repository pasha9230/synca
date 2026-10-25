from tires_sync import store
from tires_sync.models import Season
from tires_sync.photo_library import set_key_for, size_class
from helpers import make_offer


def test_size_class():
    assert size_class(14) is None
    assert size_class(15) == "r15_18"
    assert size_class(18) == "r15_18"
    assert size_class(19) == "r19p"
    assert size_class(22) == "r19p"


def test_set_key_text():
    offer = make_offer(brand_key="IKON", model_raw="Autograph Ice 9", season=Season.WINTER, studded=True, diameter=16)
    assert set_key_for(offer).text() == "ikon|AUTOGRAPHICE9|winter|s|r15_18"
    assert set_key_for(make_offer(diameter=14)) is None


def test_tables_exist(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    for table in ("photo", "photo_set", "set_frame", "image_job", "account_photo"):
        conn.execute("SELECT * FROM %s" % table).fetchall()
    conn.execute("INSERT INTO set_frame (set_key, kind, photo_id, status, created) VALUES ('k','a',1,'approved','t')")
    try:
        conn.execute("INSERT INTO set_frame (set_key, kind, photo_id, status, created) VALUES ('k','a',2,'approved','t')")
    except Exception as exc:
        assert "UNIQUE" in str(exc)
    else:
        raise AssertionError("second approved frame accepted")
    conn.execute("INSERT INTO set_frame (set_key, kind, photo_id, status, created) VALUES ('k','a',3,'rejected','t')")
