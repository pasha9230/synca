"""What to do with ads Avito does not take: the owner's rule (2026-10-15) is that an ad gets a new Id only when Avito says «удалено из Личного
кабинета», and that an ad already alive in the cabinet is never doubled.

So, for an account: (1) a position whose Id is blocked or deleted while the same tire is alive in the cabinet under another Id takes that Id
(`reconcile`); (2) a position whose Id Avito reports as deleted and which has no live twin gets a new Id (`reissue_own`), at most a few per
cycle and never twice within a week (an ad that is deleted again is reported, not renewed again).

Owner, 2026-10-19: (2) is only a proposal. A position that was published before keeps its Id when it comes back, so the cycle applies
(1) and reports (2); a new Id is given only by his word (`ids-heal --confirm --renew`), and `ids-unrenew` takes one back.

A position that was taken off the feed and has been published again within RECENT_DAYS keeps its Id (owner, 2026-10-15): Avito must take the first ad
back from the archive, and the report that still calls it deleted was written while the ad was off the feed. The window is counted from the
date of the cutoff (so it is three to four days) and by the clock of the server that wrote the event.

Known gaps (reviewed 2026-10-15): the return of a set of four alone and the return of a tire under a new supplier code (it takes the Id of an older
ad) leave no `removed` event of their own code; the first is covered by the rule «a code with a live ad is not renewed», the second is not."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from typing import Dict, List

from . import mirror_feed, reconcile

DELETED = "удалено из личного кабинета"
LIMIT = 20
PAUSE_DAYS = 7
RECENT_DAYS = 3


def deleted_ids(items: List[dict]) -> List[str]:
    """The Ids Avito reports as deleted from the cabinet (from the items of an upload report). Only Avito's own words «удалено из Личного кабинета»
    count (owner, 2026-10-19: while the Id is in the cabinet in any status, archived or taken off too, it gets no new Id); a bare status `removed`
    is also what an ad taken off our feed gets."""
    out: List[str] = []
    for item in items:
        text = " ".join("%s %s" % (m.get("title", ""), m.get("description", "")) for m in item.get("messages") or []).lower()
        ad_id = item.get("ad_id")
        if ad_id and DELETED in text:
            out.append(str(ad_id))
    return out


def plan(feed: Dict[str, ET.Element], report_items: List[dict], cabinet: List[dict], inverse: Dict[str, str], renewed: Dict[str, str],
         today: date, limit: int = LIMIT, foreign_ids=(), recent=()) -> Dict[str, list]:
    """{"bind": [{orphan, feed_id, code, title}], "renew": [code], "repeat": [code], "over_limit": [code], "recent": [code], "alive": [code]}.
    `recent`: the codes taken off the feed and published again lately; they are never renewed here. `alive`: the codes of which another ad (the single
    ad next to a set of four, for example) is alive in the cabinet: a renewal is per code and would give that live ad a new Id too."""
    code_of = lambda ad_id: inverse.get(ad_id) or mirror_feed.supplier_code(ad_id)
    live_codes = {code_of(str(i["ad_id"])) for i in cabinet if i.get("status") == "active" and i.get("ad_id")}
    binds = reconcile.plan(feed, cabinet, foreign_ids)
    for entry in binds:
        entry["code"] = code_of(entry["feed_id"])
    taken = {entry["code"] for entry in binds}
    renew: List[str] = []
    repeat: List[str] = []
    waiting: List[str] = []
    alive: List[str] = []
    recent = set(recent)
    for ad_id in dict.fromkeys(deleted_ids(report_items)):
        if ad_id not in feed:
            continue                                                   # not in our feed any more: nothing to renew
        code = code_of(ad_id)
        if not code or code in taken or code in renew or code in repeat or code in waiting or code in alive:
            continue
        if code in live_codes:
            alive.append(code)
            continue
        if code in recent:
            waiting.append(code)
            continue
        stamp = renewed.get(code)                                      # «r20261015»
        if stamp:
            try:
                when = datetime.strptime(stamp[1:], "%Y%m%d").date()
            except ValueError:
                when = None
            if when is not None and today - when < timedelta(days=PAUSE_DAYS):
                repeat.append(code)
                continue
        renew.append(code)
    return {"bind": binds, "renew": renew[:limit], "repeat": repeat, "over_limit": renew[limit:], "recent": waiting, "alive": alive}
