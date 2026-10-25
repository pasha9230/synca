from __future__ import annotations

import copy
from xml.sax.saxutils import escape

from tires_sync.config import settings_from_dict
from tires_sync.models import Offer, Season, Settings

BASE_SETTINGS = {
    "version": 1,
    "territory_rn": "T1",
    "min_stock": 4,
    "hysteresis": 1,
    "min_diameter": 15,
    "portion_limit": 100,
    "stale_days": 3,
    "price_jump_limit": 30.0,
    "guard_min": 10,
    "guard_fraction": 0.3,
    "allow_mass_removal": False,
    "pct_limit": 15.0,
    "date_end_days": 29,
    "date_end_renew_days": 7,
    "contact_phone": "+70000000000",
    "point_mode": "percent",
    "point_values": {"P1": 40, "P2": 40, "P3": 20},
    "point_rest": None,
    "enabled": [["IKON TYRES", "winter"], ["PIRELLI", "winter"]],
    "pricing": {
        "IKON TYRES|winter": {"pct_small": -3.82, "pct_large": -4.28},
        "PIRELLI|winter": {"pct_small": -5.85, "pct_large": -6.2},
    },
}


def settings_dict(**overrides):
    raw = copy.deepcopy(BASE_SETTINGS)
    raw.update(overrides)
    return raw


def make_settings(**overrides) -> Settings:
    return settings_from_dict(settings_dict(**overrides))


def make_offer(**kw) -> Offer:
    values = dict(
        goods_id="100", name="IKON 205/55/16 T 94 Test Model", name_orig="Ikon  205/55/16  T 94 Test Model",
        brand_raw="Ikon Tyres", brand_key="IKON TYRES", model_raw="Test Model", season=Season.WINTER,
        width=205, height=55, diameter=16, speed="T", load="94", code="TS1", stock=10, base_price=5000.0,
        image="https://example.test/1.jpg", runflat=False, studded=False, car_type="", mnf_year="",
        territory_rn="T1",
    )
    values.update(kw)
    return Offer(**values)


COMMODITY_DEFAULTS = dict(
    NNOMMODIF="100", SMODIFNAME="IKON 205/55/16 T 94 Test Model",
    SMODIFNAME_ORIG="Ikon  205/55/16  T 94 Test Model", SSEASON="Зимняя", SMARKA="Ikon Tyres",
    SMODEL="Test Model", SDIAMETR="16", SWIDTH="205", SHEIGHT="55", SSPEED="T", SLOAD="94",
    SMNFCODE="TS1", NREST="10", NPRICE_DELAYED="5000", SPICTURE="https://example.test/1.jpg",
    SRUNFLAT="", SCARTYPE="", SMNFYEAR="", TERRITORY_RN="T1",
)


def commodity_xml(**kw) -> str:
    values = dict(COMMODITY_DEFAULTS)
    if "SLOAD" not in kw and "NNOMMODIF" in kw:
        # Positions of a test are different tires (one tire is published once, see same_tire.py): the load index follows the number
        import re as _re
        digits = _re.sub(r"\D", "", str(kw["NNOMMODIF"]))
        number = int(digits) if digits else sum(map(ord, str(kw["NNOMMODIF"])))
        values["SLOAD"] = str(94 + (number - 100) % 30)
    values.update(kw)
    body = "".join("<%s>%s</%s>" % (k, escape(str(v)), k) for k, v in values.items())
    return "<COMMODITY>%s</COMMODITY>" % body


def catalog_xml(*commodities: str) -> bytes:
    return ("<CATALOG><COMMODITIES>%s</COMMODITIES></CATALOG>" % "".join(commodities)).encode("utf-8")
