from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Set

from .candidates import Candidate, Evaluation
from .overrides import override_key
from .models import Action, Decision, DecisionSet, PublishedItem, Settings
from .points import assign_points


SETTINGS_DRIVEN = frozenset({"not_enabled", "diameter", "manual_off", "brand_excluded", "size_excluded", "same_tire"})


OTH_LIMIT = 20             # ads that come back for «На ОТХ» in one run of an account (the owner 2026-10-29); the rest wait for the next one

# Other sellers of the supplier put goods into their baskets during the day and the goods leave the unload; the baskets are emptied at
# midnight and the catalog fills up again by the morning cycle. A position that left during the day is therefore kept until the first
# cycle at or after this hour (Moscow time) that follows the night, and only then, if it is still gone, taken off.
CART_REFILL_HOUR = 6
CART_REFILL_MINUTE = 30                      # Avito reads the feed at 07:00: the last check and update run at 06:45, after this moment
CART_REASONS = ("absent_from_supplier", "low_stock")
# The owner's decision of 2026-10-14: a position that left the unload or fell below the stock threshold is taken off at once, not kept
# for the morning refill (this reverses the decision of 2026-10-12). The wait itself stays here, off, in case the owner wants it back.
WAIT_FOR_MORNING_REFILL = False


def cart_release_at(since: datetime) -> datetime:
    """The first CART_REFILL_HOUR:CART_REFILL_MINUTE strictly after `since`: the earliest moment at which the morning catalog is complete."""
    release = since.replace(hour=CART_REFILL_HOUR, minute=CART_REFILL_MINUTE, second=0, microsecond=0)
    return release if release > since else release + timedelta(days=1)


def _hold_since(pub: PublishedItem, now: datetime) -> datetime:
    first = pub.first_hold
    if isinstance(first, datetime):
        return first
    if first is not None:                                     # a hold written before the time was kept: assume it began at noon that day
        return datetime.combine(first, time(12, 0))
    return now


def cart_decision(gid: str, pub: PublishedItem, reason: str, now: Optional[datetime]) -> Decision:
    """REMOVE for a position that left the unload (or fell below the stock threshold); when WAIT_FOR_MORNING_REFILL is on, only after the morning refill."""
    removal = Decision(gid, Action.REMOVE, reason, suppressed=(reason == "low_stock"))
    if not WAIT_FOR_MORNING_REFILL or now is None or pub.location == "own":
        return removal
    since = _hold_since(pub, now)
    if now >= cart_release_at(since):
        return removal
    return Decision(gid, Action.HOLD, "cart_wait:" + reason, pub.price, pub.point, first_hold=since)


def hold_decision(gid: str, pub: PublishedItem, reason: str, settings: Settings, today: date) -> Decision:
    first = pub.first_hold or today
    if isinstance(first, datetime):
        first = first.date()
    wait = max(1, settings.stale_days) if "supplier_unavailable" in reason else settings.stale_days     # a missing file is not a bad row
    if (today - first).days >= wait:
        return Decision(gid, Action.REMOVE, "stale:" + reason)
    return Decision(gid, Action.HOLD, reason, pub.price, pub.point, first_hold=first)


def decide(
    ev: Evaluation,
    published: Dict[str, PublishedItem],
    suppressed: Set[str],
    known_points: Dict[str, str],
    settings: Settings,
    today: date,
    now: Optional[datetime] = None,
    already_live=frozenset(),
    removed_before: int = 0,
    oth=None,
    oth_retired=None,
) -> DecisionSet:
    """`now` (Moscow time) switches on the wait for the morning refill; without it positions that left are taken off at once.
    `removed_before`: ads of the account the light stock passes took off within the last hour; the guard counts them with this run's.
    `oth`: goods_id -> pieces on «На ОТХ» (sync.plan_decisions, the owner 2026-10-29): in the unload, built afresh whatever its stock (and
    published if it is not); out of it, held on its saved ad (HOLD «oth:<reason>»), or published again from its last ad (`oth_retired`:
    code -> the price of that ad). At most OTH_LIMIT come back in a run; the owner's own switches still take them off."""
    oth = oth or {}
    oth_retired = oth_retired or {}
    decisions: List[Decision] = []
    queue: List[Candidate] = []
    oth_queue: List[str] = []

    def floor(goods_id: str) -> int:
        # Owner's rule (2026-10-14): fewer than the threshold is never published, whatever the manual switch says. A manual «on» lifts exclusions
        # (brand, season, size, commercial), not the stock rule.
        return settings.min_stock

    for gid in sorted(set(ev.ok) | set(ev.problems) | set(published) | (set(oth_retired) & set(oth))):
        pub: Optional[PublishedItem] = published.get(gid)
        if pub is not None and pub.location == "own":
            decisions.append(Decision(gid, Action.KEEP, "own_point", pub.price, pub.point))
            continue
        cand = ev.ok.get(gid)
        probs = ev.problems.get(gid)
        if cand is None and probs is None:
            if pub is None and gid in oth and gid in oth_retired and gid not in ev.excluded:
                oth_queue.append(gid)                                     # gone from the unload, taken off before: its last ad comes back
                continue
            if pub is not None:
                reason = ev.excluded.get(gid, "absent_from_supplier")
                decisions.append(cart_decision(gid, pub, reason, now) if reason in CART_REASONS else Decision(gid, Action.REMOVE, reason))
            continue
        if probs is not None:
            if pub is not None:
                known_stock = ev.stocks.get(gid)
                if known_stock is not None and known_stock < floor(gid):
                    decisions.append(cart_decision(gid, pub, "low_stock", now))
                else:
                    decisions.append(hold_decision(gid, pub, ";".join(probs), settings, today))
            else:
                decisions.append(Decision(gid, Action.SKIP, "problem:" + ";".join(probs)))
            continue
        stock = cand.offer.stock
        if gid in oth and pub is not None and stock < floor(gid):          # our own tires at a point: the ad stays, built as any other
            decisions.append(Decision(gid, Action.UPDATE if cand.price != pub.price else Action.KEEP, "oth:low_stock",
                                      cand.price if cand.price != pub.price else pub.price, pub.point))
        elif gid in oth and pub is None and stock < floor(gid) + (settings.hysteresis if gid in suppressed else 0):
            oth_queue.append(gid)                                         # below the threshold: published for OTH all the same
        elif pub is not None:
            if stock < floor(gid):
                decisions.append(cart_decision(gid, pub, "low_stock", now))
            elif cand.price != pub.price:
                decisions.append(Decision(gid, Action.UPDATE, "price_changed", cand.price, pub.point))
            else:
                decisions.append(Decision(gid, Action.KEEP, "unchanged", pub.price, pub.point))
        else:
            need = floor(gid) + (settings.hysteresis if gid in suppressed else 0)
            if stock >= need:
                queue.append(cand)
            else:
                decisions.append(Decision(gid, Action.SKIP, "low_stock"))

    if oth:
        # The owner 2026-10-29: the tires of a refused order lie at our point; while the order stays in «На ОТХ» its ad stays, on the saved ad
        # and its last price, whatever the supplier has. The owner's own switches (SETTINGS_DRIVEN) still take it off.
        decisions = [Decision(d.goods_id, Action.HOLD, "oth:" + d.reason, published[d.goods_id].price, published[d.goods_id].point,
                              first_hold=published[d.goods_id].first_hold or today)
                     if d.action is Action.REMOVE and d.goods_id in oth and d.reason not in SETTINGS_DRIVEN and d.goods_id in published else d
                     for d in decisions]

    tripped = False
    removals = [d for d in decisions if d.action is Action.REMOVE and d.reason not in SETTINGS_DRIVEN]
    limit = max(settings.guard_min, int(settings.guard_fraction * len(published)))
    if published and removals and len(removals) + removed_before > limit and not settings.allow_mass_removal:
        tripped = True
        guarded: List[Decision] = []
        for d in decisions:
            if d.action is Action.REMOVE and d.reason not in SETTINGS_DRIVEN:
                p = published[d.goods_id]
                guarded.append(
                    Decision(d.goods_id, Action.HOLD, "guard:" + d.reason, p.price, p.point,
                             first_hold=p.first_hold or today)
                )
            else:
                guarded.append(d)
        decisions = guarded

    moved: List[str] = []
    if settings.point_values:
        stranded = [i for i, d in enumerate(decisions)
                    if d.action in (Action.KEEP, Action.UPDATE) and d.point not in settings.point_values
                    and published.get(d.goods_id) is not None and published[d.goods_id].location == "supplier"
                    and d.goods_id in ev.ok]
        if stranded:
            on_valid = Counter(p.point for p in published.values()
                               if p.location == "supplier" and p.point in settings.point_values)
            for i, point in zip(stranded, assign_points(len(stranded), on_valid, settings)):
                d = decisions[i]
                decisions[i] = Decision(d.goods_id, Action.UPDATE, "point_changed", ev.ok[d.goods_id].price, point)
                moved.append(point)

    queue.sort(key=lambda c: c.goods_id)
    live = [c for c in queue if c.goods_id in already_live]            # ads that are already on Avito (an account moved from a mirror): never deferred
    rest = [c for c in queue if c.goods_id not in already_live]
    allowed = live + rest[: settings.portion_limit]
    deferred = [c for c in rest[settings.portion_limit:] if c.goods_id not in oth]
    oth_queue = sorted(oth_queue + [c.goods_id for c in rest[settings.portion_limit:] if c.goods_id in oth])   # OTH waits for no portion
    oth_now = oth_queue[:OTH_LIMIT]
    for gid in oth_queue[OTH_LIMIT:]:
        decisions.append(Decision(gid, Action.DEFER, "oth:limit"))
    counts: Counter = Counter(p.point for p in published.values() if p.location == "supplier")
    counts.update(moved)
    points: Dict[str, str] = {}
    fresh: List[Candidate] = []
    for c in allowed:
        known = known_points.get(c.goods_id)
        if known:
            points[c.goods_id] = known
            counts[known] += 1
        else:
            fresh.append(c)
    for c, point in zip(fresh, assign_points(len(fresh), counts, settings)):
        points[c.goods_id] = point
    for c in allowed:
        decisions.append(Decision(c.goods_id, Action.PUBLISH, "new", c.price, points[c.goods_id]))
    unknown = [g for g in oth_now if not known_points.get(g)]
    assigned = dict(zip(unknown, assign_points(len(unknown), counts, settings)))
    for gid in oth_now:
        price = ev.ok[gid].price if gid in ev.ok else oth_retired[gid]
        decisions.append(Decision(gid, Action.PUBLISH, "oth:new" if gid in ev.ok else "oth:retired", price,
                                  known_points.get(gid) or assigned[gid]))
    for c in deferred:
        decisions.append(Decision(c.goods_id, Action.DEFER, "portion_limit"))
    decisions.sort(key=lambda d: d.goods_id)
    return DecisionSet(decisions, tripped)
