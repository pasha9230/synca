from __future__ import annotations

import time

from helpers import catalog_xml, commodity_xml
from panel_helpers import login, make_app


def test_catalog_with_1850_positions_and_mostly_unmatched_models_opens_quickly(tmp_path):
    app = make_app(tmp_path)
    ref = "<Shiny>\n<make name=\"Ikon Tyres\">\n" + "".join('<model name="Model %03d X"/>\n' % i for i in range(400)) + "</make>\n</Shiny>"
    (tmp_path / "reference.txt").write_text(ref, encoding="utf-8")
    rows = [commodity_xml(NNOMMODIF=str(i), SMODEL="Weird %d model" % (i % 300), SMNFCODE="C%d" % i,
                          SWIDTH=str(175 + (i % 8) * 10), SDIAMETR=str(14 + i % 6)) for i in range(1850)]
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*rows))
    client = app.test_client()
    login(client)
    started = time.time()
    html = client.get("/catalog").get_data(as_text=True)
    took = time.time() - started
    assert took < 4.0, "catalog page took %.1f s" % took
    assert html.count('class="gname"') == 100 and "страница 1 из 19" in html and len(html) < 400000
    started = time.time()
    last = client.get("/catalog/table?p=19").get_data(as_text=True)
    assert last.count('class="gname"') == 50 and time.time() - started < 4.0
