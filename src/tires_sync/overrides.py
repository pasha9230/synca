from __future__ import annotations

SUPPLIER = "1"
VALID = ("on", "off")
# reasons that only hide a position; a manual "on" lets it through
FORCEABLE = frozenset({"commercial", "old_production", "diameter", "not_enabled"})


def override_key(goods_id: str, supplier: str = SUPPLIER) -> str:
    return "%s:%s" % (supplier, goods_id)
