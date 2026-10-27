"""Manual feed update: what the next publication would change against what is published now. Reads only, writes nothing."""
from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path

from .. import store
from ..feed import FeedCheckError, check_feed
from ..models import Action
from ..publishing import expected_for
from ..cycle import _own_photos, _photo_version, pass_removals_lately
from ..sync import build_ads, live_images_of, plan_decisions
from .service import REASON_TEXT

EXTRA_REASONS = {"cart_wait": "ждём утреннего наполнения каталога", "absent_from_supplier": "пропала у поставщика", "stale": "проблема с данными у поставщика",
                 "invalid_ad": "объявление не собирается", "supplier_unavailable": "поставщик недоступен"}


def reason_text(code: str) -> str:
    head = (code or "").split(":", 1)[0]
    return REASON_TEXT.get(head) or EXTRA_REASONS.get(head) or head


def build_preview(conn, parsed, ref, settings, today, feed_root="", now=None) -> dict:
    """Per account: what appears, what goes, which prices change, and whether the publication guards would object."""
    from .. import connections as cx
    expected = expected_for(conn, settings)
    problems = []
    if not feed_root:
        problems.append("Каталог фидов не задан (TIRES_FEED_ROOT): фид нельзя ни собрать, ни опубликовать.")
    if 1 in parsed.unavailable or not parsed.offers:
        problems.append("Выгрузка основного поставщика недоступна или пуста: обновление остановлено.")
    accounts = []
    for account in cx.list_feed_accounts(conn):
        if account["feed_mode"] == "mirror":                                   # a mirrored feed is carried over as it is: nothing to preview
            continue
        item = {"id": account["id"], "name": cx.display_name(conn, account["id"]), "live": bool(account["live"])}
        eff = cx.effective_settings(conn, account["id"], settings)
        try:
            published, ev, ds = plan_decisions(conn, parsed, ref, eff, account["id"], today, now=now,
                                                removed_before=pass_removals_lately(conn, account["id"]))   # as the cycle's guard: the passes' hour counts
        except Exception as err:
            item["error"] = str(err)
            accounts.append(item)
            continue
        suffix, prefix = cx.feed_style(conn, account["id"])
        from .. import mirror_feed
        id_map, image_map = mirror_feed.adopted_maps(conn, account["id"])
        ads, invalid, final, _rendered, _rendered4, _owners = build_ads(
            conn, parsed, ev, published, ds, eff, account["id"], today, account.get("description_template"), suffix, prefix,
            dict(id_map, **ev.aliases, **ev.inherit), image_map, title_template=cx.get_title_template(conn, account["id"]) or None,
            photo_version=_photo_version(conn, account["id"]), own_photos=_own_photos(conn, account),
            live_images=live_images_of(Path(feed_root) / account["feed_token"] / "next" if feed_root else None))    # as the cycle: the photo links are the ones that will be published
        offers = parsed.offers
        def name(gid):
            """The supplier's name; a position that left the unload has none there, so the title of the ad we published is used."""
            if gid in offers:
                return offers[gid].name_orig or offers[gid].name
            snapshot = store.get_snapshot(conn, gid, account["id"])
            found = re.search(r"<Title>(.*?)</Title>", snapshot or "", re.S)
            return html.unescape(found.group(1)).strip() if found and found.group(1).strip() else gid
        appear = [{"id": d.goods_id, "name": name(d.goods_id), "price": d.price} for d in final if d.action is Action.PUBLISH]
        gone = [{"id": d.goods_id, "name": name(d.goods_id), "reason": reason_text(d.reason)} for d in final if d.action is Action.REMOVE]
        waiting = [{"id": d.goods_id, "name": name(d.goods_id), "reason": reason_text(d.reason.split(":", 1)[-1])}
                   for d in final if d.action is Action.HOLD and d.reason.startswith("cart_wait:")]
        changes = [{"id": d.goods_id, "name": name(d.goods_id), "old": published[d.goods_id].price, "new": d.price}
                   for d in final if d.action is Action.UPDATE and d.goods_id in published and published[d.goods_id].price != d.price]
        reasons = {}
        for row in gone:
            reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
        before = store.count_published_ads(conn, account["id"], twins=eff.pack4)
        after = len(ads)
        warning = ""
        try:
            check_feed(after, before, allow_shrink=eff.allow_mass_removal, expected_removals=expected)
        except FeedCheckError as err:
            warning = "Публикация будет отклонена: %s. Если так и должно быть, разрешите массовое снятие в настройках." % (
                "в фиде не останется объявлений" if "empty" in str(err) else "фид сократился бы более чем вдвое (%d → %d)" % (before, after))
        limit = max(eff.guard_min, int(eff.guard_fraction * len(published)))          # as the cycle's own guard: positions, not ads
        item.update(
            over=len(gone) > limit and not eff.allow_mass_removal, before=before, after=after, appear=appear, gone=gone, waiting=waiting, changes=changes, reasons=sorted(reasons.items(), key=lambda p: -p[1]),
            deferred=sum(1 for d in final if d.action is Action.DEFER),
            held=sum(1 for d in final if d.action is Action.HOLD), guard=ds.guard_tripped, unbuilt=len(invalid), warning=warning)
        accounts.append(item)
    digest = hashlib.sha256(json.dumps(
        [settings.version, problems] + [[a["id"], a["live"], a.get("before"), a.get("after"), a.get("held"), a.get("deferred"), a.get("guard"),
                                         [[x["id"], x["price"]] for x in a.get("appear", ())], [[x["id"], x["reason"]] for x in a.get("gone", ())], [x["id"] for x in a.get("waiting", ())],
                                         [[x["id"], x["old"], x["new"]] for x in a.get("changes", ())]] for a in accounts],
        ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    return {"accounts": accounts, "rev": digest, "problems": problems,
            "blocked": bool(problems) or any(a.get("warning") or a.get("error") for a in accounts),
            "over": any(a.get("over") for a in accounts),
            "anything": any(a.get("appear") or a.get("gone") or a.get("changes") for a in accounts)}


def unload_age(unload_path) -> str:
    """When the stored unload of the first supplier was downloaded (UTC iso), from the file's meta or its date."""
    from datetime import datetime
    path = Path(str(unload_path) + ".meta.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("fetched_at"):
            return str(data["fetched_at"])
    except (OSError, ValueError):
        pass
    try:
        return datetime.utcfromtimestamp(Path(unload_path).stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return ""


class FetchBusy(Exception):
    """Another download of the supplier's unload is already running."""


class fetch_lock:
    """One manual download at a time (the panel runs several workers): a second press while the first is running waits for nothing and says so."""

    def __init__(self, folder):
        self.path = Path(folder) / ".fetch.lock"
        self.handle = None

    def __enter__(self):
        import fcntl
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = open(self.path, "w")
        except OSError:
            self.handle = None                    # no lock file possible: go on, the download itself is atomic
            return self
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise FetchBusy()
        return self

    def __exit__(self, *exc):
        if self.handle is not None:
            self.handle.close()
