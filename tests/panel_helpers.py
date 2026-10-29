from __future__ import annotations

from datetime import datetime

from werkzeug.security import generate_password_hash

from tires_sync import store
from tires_sync.panel import create_app
from tires_sync.settings_store import init_settings
from helpers import catalog_xml, commodity_xml, settings_dict

REF_TEXT = (
    '<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n<model name="Other Model"/>\n</make>\n'
    '<make name="Pirelli">\n<model name="Test Model"/>\n</make>\n'
    '<make name="Laufenn">\n<model name="G Fit"/>\n</make>\n</Shiny>'
)
UNLOAD = catalog_xml(
    commodity_xml(NNOMMODIF="100"),
    commodity_xml(NNOMMODIF="101", SMODEL="Test Model 2"),
    commodity_xml(NNOMMODIF="102", SMARKA="Лауфенн", SMODIFNAME="LAUFENN 205/55/16 G Fit", SMODEL="G Fit"),
    commodity_xml(NNOMMODIF="103", SMARKA="Неизвестный", SMODIFNAME="UNKNOWN 205/55/16 X", SMODEL="X"),
)


def make_app(tmp_path, with_data=True, **cfg):
    db = tmp_path / "t.db"
    conn = store.connect(db)
    init_settings(conn, settings_dict(), "2026-10-07T10:00:00")
    conn.close()
    unload = tmp_path / "unload.xml"
    reference = tmp_path / "reference.txt"
    if with_data:
        unload.write_bytes(UNLOAD)
        reference.write_text(REF_TEXT, encoding="utf-8")
    config = {
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "PASSWORD_HASH": generate_password_hash("pw", method="pbkdf2:sha256:1000"),
        "DB_PATH": str(db),
        "UNLOAD_PATH": str(unload),
        "REFERENCE_PATH": str(reference),
        "NOW": lambda: datetime(2026, 10, 7, 12, 0, 0),
    }
    config.update(cfg)
    return create_app(config)


def shared_mark_of(app) -> str:
    """The mark a session of the shared owner password carries (panel/common.shared_mark), for a session planted by a test."""
    from tires_sync.panel.common import shared_mark
    with app.app_context():
        return shared_mark()


def login(client) -> str:
    mark = shared_mark_of(client.application)
    with client.session_transaction() as session:
        session["user"] = "owner"
        session["epoch"] = 0
        session["csrf"] = "tok"
        session["pw"] = mark
    return "tok"


def review(client) -> str:
    """The address of the page of the working draft (/draft/N), as «Подробнее» of the bar opens it: the changes of the settings pages pile up
    there and the pages stay where they are (the owner 2026-10-29)."""
    return client.get("/catalog/review").headers["Location"]


def post(client, url, tok, **data):
    if url.startswith("/draft/") and url.endswith("/confirm") and "rev" not in data:
        import re
        page = client.get(url[: -len("/confirm")]).get_data(as_text=True)
        found = re.search(r'name="rev" value="([0-9a-f]+)"', page)
        if found:
            data["rev"] = found.group(1)
    if url == "/economics/sales/add" and "sale_token" not in data:               # a manual sale is one per opened form: open it as a person would
        import re
        page = client.get("/economics/sales/new?goods_id=%s" % data.get("goods_id", "")).get_data(as_text=True)
        found = re.search(r'name="sale_token" value="([0-9a-f]+)"', page)
        if found:
            data["sale_token"] = found.group(1)
    return client.post(url, data=dict({"csrf": tok}, **data))
