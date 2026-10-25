"""Ads that stand in the cabinet under an Id our feed does not have.

Avito knows a tire by its photos and product, not by our Id. When the same tire comes back under another supplier code (or an Id was renewed),
our feed has a new Id while the first ad is still alive in the cabinet: Avito blocks the new one as a repeat («we published your first offer
again») and the next upload takes the first one down. The plan below finds such pairs by the tire (brand, model, size, indices) so that the
position can take the Id of the ad that is alive."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

_TITLE = re.compile(r"^(.*?)\s+(\d{3})/(\d{2,3})\s*R(\d{2})\s*(\d{2,3})([A-Z]{1,2})\b")


def _norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper())


def key_title(title: str) -> Optional[Tuple]:
    """The tire behind an Avito title «Brand Model 235/55 R19 105T»: (brand+model, width, height, diameter, load, speed)."""
    found = _TITLE.match(title or "")
    if not found:
        return None
    return (_norm(found.group(1)), found.group(2), found.group(3), found.group(4), found.group(5), found.group(6))


def key_ad(ad: ET.Element) -> Tuple:
    text = lambda tag: (ad.findtext(tag) or "").strip()
    return (_norm(text("Brand") + " " + text("Model")), text("TireSectionWidth"), text("TireAspectRatio"), text("RimDiameter"),
            text("LoadIndex").split("/")[0], text("SpeedIndex").upper())


PACK = re.compile(r"-x\d+(_|$)")


def plan(feed: Dict[str, ET.Element], cabinet: List[dict], foreign_ids=()) -> List[dict]:
    """[{orphan, feed_id, title}]: a live ad of the cabinet whose Id our feed does not have, and the one ad of the feed that is the same tire under
    another Id which is not itself live in the cabinet. `cabinet` is [{ad_id, status, title}]. A tire with two candidates is left alone."""
    live = {c["ad_id"] for c in cabinet if c.get("status") == "active" and c.get("ad_id")}
    foreign = set(foreign_ids)
    by_key: Dict[Tuple, List[str]] = {}
    for ad_id, ad in feed.items():
        if not PACK.search(ad_id):                                     # a set of four is not a tire of its own here
            by_key.setdefault(key_ad(ad), []).append(ad_id)
    orphans: Dict[Tuple, List[dict]] = {}
    for item in cabinet:
        orphan = item.get("ad_id")
        if item.get("status") != "active" or not orphan or orphan in feed or PACK.search(orphan) or orphan in foreign:
            continue
        key = key_title(item.get("title") or "")
        if key:
            orphans.setdefault(key, []).append(item)
    out: List[dict] = []
    for key, items in orphans.items():
        candidates = by_key.get(key, [])
        # one live ad and one ad of the feed for the tire, and that one is not alive itself; anything else (two lots, RunFlat and plain, studded
        # and not) is for a human: the title of Avito does not tell the variants apart
        if len(items) == 1 and len(candidates) == 1 and candidates[0] not in live:
            out.append({"orphan": items[0]["ad_id"], "feed_id": candidates[0], "title": items[0].get("title")})
    return out
