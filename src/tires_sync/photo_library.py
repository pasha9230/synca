"""Photo library: the set key groups tire positions that share one set of generated photos."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from collections import Counter
from typing import TYPE_CHECKING, Dict, FrozenSet, List, Optional

from .catalog import normalize_model
from .models import Offer, Season

if TYPE_CHECKING:
    from .candidates import Candidate

MIN_DIAMETER = 15
DAILY_CAP_USD = 5.0
MAX_ATTEMPTS = 3
MAX_OPEN_JOBS = 10          # queue depth: a regenerate or a set4 waits behind at most this many jobs
QUEUE_STOP_KEY = "image_queue_stopped"


@dataclass(frozen=True)
class SetKey:
    brand_key: str
    model_key: str
    season: Season
    studded: bool
    size_class: str

    def text(self) -> str:
        return "%s|%s|%s|%s|%s" % (self.brand_key, self.model_key, self.season.value, "s" if self.studded else "n", self.size_class)


def size_class(diameter: int) -> Optional[str]:
    if diameter < MIN_DIAMETER:
        return None
    return "r15_18" if diameter <= 18 else "r19p"


def set_key_for(offer: Offer) -> Optional[SetKey]:
    cls = size_class(offer.diameter)
    if cls is None:
        return None
    return SetKey(offer.brand_key.lower(), normalize_model(offer.model_raw), offer.season, bool(offer.studded), cls)


FRAME_KINDS = ("three_quarter", "set4")
STATE_NEEDED = "needed"
STATE_DECISION = "needs_decision"


@dataclass
class SetPlan:
    key: SetKey
    positions: int
    stock: int
    ref_url: str
    size_hint: str
    state: str
    reason: str
    brand_name: str = ""
    model_name: str = ""
    aliases: List[SetKey] = field(default_factory=list)      # other supplier spellings of the same catalog model


def _most_common(values: List[str]) -> str:
    counts = Counter(v for v in values if v)
    if not counts:
        return ""
    return min(counts, key=lambda v: (-counts[v], v))


def _merge_spellings(plans: List[SetPlan]) -> List[SetPlan]:
    """Sets of one catalog model (brand + model name, season, studs, size class) written differently by the supplier
    become one: the spelling with the most positions leads, the others are its aliases."""
    groups: Dict[tuple, List[SetPlan]] = {}
    for p in plans:
        k = p.key
        name = (k.brand_key, normalize_model(p.brand_name), normalize_model(p.model_name)) if p.model_name else (k.text(),)
        groups.setdefault(name + (k.season, k.studded, k.size_class), []).append(p)
    out = []
    for same in groups.values():
        same.sort(key=lambda p: (-p.positions, -p.stock, p.key.text()))
        lead = same[0]
        for p in same[1:]:
            lead.positions += p.positions
            lead.stock += p.stock
            lead.aliases.extend([p.key] + p.aliases)
        out.append(lead)
    return out


def needed_sets(ok: Dict[str, "Candidate"], min_stock: int, seasons=frozenset({Season.WINTER})) -> List[SetPlan]:
    """Sets of published-able positions that need generated photos, in generation order."""
    groups: Dict[SetKey, list] = {}
    for cand in ok.values():
        offer = cand.offer
        if offer.season not in seasons or offer.stock < min_stock:
            continue
        key = set_key_for(offer)
        if key is not None:
            groups.setdefault(key, []).append(cand)
    plans: List[SetPlan] = []
    for key, cands in groups.items():
        offers = [c.offer for c in cands]
        ref_url = _most_common([o.image for o in offers])
        if not ref_url:
            continue
        hint = _most_common(["%d/%d R%d" % (o.width, o.height, o.diameter) for o in offers])
        if key.season in (Season.ALL, Season.UNKNOWN):
            state, reason = STATE_DECISION, "сезон не определён: нужно решение владельца"
        else:
            state, reason = STATE_NEEDED, ""
        brand = _most_common([c.avito_brand for c in cands]) or _most_common([o.brand_raw for o in offers])
        model = _most_common([c.avito_model for c in cands]) or _most_common([o.model_raw for o in offers])
        plans.append(SetPlan(key, len(offers), sum(o.stock for o in offers), ref_url, hint, state, reason, brand, model))
    plans = _merge_spellings(plans)
    plans.sort(key=lambda p: (not p.key.studded, -p.positions, -p.stock, p.key.text()))
    return plans


PRIORITY_KEY = "photo_priority_sold"        # the owner's list of sold tires, one per line, as pasted in the panel
_SOLD_SIZE = re.compile(r"(\d{3})/(\d{2})(?:/|\s*R)\s*(\d{2})")
_SOLD_HEAD = re.compile(r"^\s*[A-Z]{1,2}\s+\d{2,3}(?:/\d{2,3})?\s+(?:C\s+)?")      # speed and load index
_SOLD_TAIL = re.compile(r"(?<!\S)(XL|KOREA|VIETNAM|CHINA|CN|Ш\.?|Ш)(?!\S)", re.I)


def _sold_lines(conn, text: str):
    """(raw line, brand, model, size class, studded) per tire line; model is '' when the line has none, brand None when unknown."""
    brands = sorted({b for row in conn.execute("SELECT brand_name, brand_key FROM photo_set") for b in row if b}, key=len, reverse=True)
    for raw in text.splitlines():
        line = " ".join(raw.replace('"', " ").split())
        line = re.sub(r"^Товар\s+", "", line)
        if not line:
            continue
        brand = next((b for b in brands if line.lower().startswith(b.lower() + " ")), None)
        size = _SOLD_SIZE.search(line)
        if brand is None or size is None:
            yield line, None, "", None, False
            continue
        cls = size_class(int(size.group(3)))
        rest = _SOLD_SIZE.sub(" ", line[len(brand):], count=1)
        studded = bool(re.search(r"(?<!\S)Ш\.?(?!\S)", rest)) or "STUD" in rest.upper()
        model = _SOLD_TAIL.sub(" ", _SOLD_HEAD.sub(" ", rest))
        first = brand.split()[0]
        model = re.sub(r"^\s*%s(?=\s|$)" % re.escape(first), " ", model, flags=re.I)
        yield line, brand, " ".join(model.split()), cls, studded


def priority_matches(conn, text: str):
    """Sold lines → {needed set key: sold count}, and the lines that found no set (R14 and smaller are skipped).
    A name matches exactly, or one name holds the other (WI32 ↔ WinterCraft Ice Wi32), but SUV never meets non-SUV;
    among several, the same studs win, then the closest name."""
    cols = ("key", "brand_name", "brand_key", "model_name", "model_key", "studded", "size_class")
    sets = [dict(zip(cols, r)) for r in conn.execute("SELECT %s FROM photo_set WHERE state = ?" % ", ".join(cols), (STATE_NEEDED,))]
    for s in sets:
        s["names"] = {n for n in (normalize_model(s["model_name"] or ""), s["model_key"] or "") if n}
    matched, unmatched = Counter(), Counter()
    for line, brand, model, cls, studded in _sold_lines(conn, text):
        if brand is not None and cls is None:
            continue
        name = normalize_model(model)
        if not name:
            unmatched[line] += 1
            continue
        best = []
        for s in sets:
            if s["size_class"] != cls or brand.lower() not in ((s["brand_name"] or "").lower(), (s["brand_key"] or "").lower()):
                continue
            for n in s["names"]:
                if n == name:
                    best.append((0, 0, s["key"]))
                elif (name in n or n in name) and ("SUV" in n) == ("SUV" in name):
                    best.append((1 + (bool(s["studded"]) != studded), abs(len(n) - len(name)), s["key"]))
        if not best:
            unmatched["%s %s (%s)" % (line[:len(brand)], model, "R19+" if cls == "r19p" else "R15–R18")] += 1
            continue
        top = min(best)[:2]
        for key in {k for *rank, k in best if tuple(rank) == top}:
            matched[key] += 1
    return dict(matched), unmatched


def apply_priority(conn) -> None:
    """Re-rank needed sets: sold models first (more sold first), then the plan order. Does not commit."""
    from . import store
    text = store.get_state(conn, PRIORITY_KEY) or ""
    if not text.strip():
        return
    sold, _ = priority_matches(conn, text)
    rows = conn.execute("SELECT key, rank FROM photo_set WHERE state = ? ORDER BY rank, key", (STATE_NEEDED,)).fetchall()
    ordered = sorted(rows, key=lambda r: (-sold.get(r[0], 0), r[1], r[0]))
    for rank, (key, _) in enumerate(ordered):
        conn.execute("UPDATE photo_set SET rank = ? WHERE key = ?", (rank, key))


def _settled_key(conn, p: SetPlan, previous: Dict[str, str], present: FrozenSet[str]) -> SetKey:
    """The key a merged set keeps, chosen the same way every cycle so ads do not swap photos:
    the spelling with the most approved frames (the earlier approval wins a tie), then any frames, then an already
    needed set, then the key text. A spelling that was the set of these keys before stays a candidate even when it
    is sold out today, so its frames keep serving the other spellings."""
    keys = {k.text(): k for k in [p.key] + p.aliases}
    for text in list(keys):
        target = previous.get(text)
        if target and target not in keys and target not in present:
            row = conn.execute("SELECT brand_key, model_key, season, studded, size_class FROM photo_set WHERE key = ?", (target,)).fetchone()
            if row:
                keys[target] = SetKey(row[0], row[1], Season(row[2]), bool(row[3]), row[4])

    def score(text):
        approved, first = conn.execute("SELECT COUNT(*), MIN(COALESCE(decided, created)) FROM set_frame WHERE set_key = ? AND status = 'approved'", (text,)).fetchone()
        frames = conn.execute("SELECT COUNT(*) FROM set_frame WHERE set_key = ?", (text,)).fetchone()[0]
        needed = conn.execute("SELECT 1 FROM photo_set WHERE key = ? AND state = 'needed'", (text,)).fetchone() is not None
        return (-approved, first or "~", -frames, not needed, text)

    return keys[min(keys, key=score)]


def _key_parts(text: str):
    brand, model, season, studs, cls = text.split("|")
    return brand, model, Season(season), studs == "s", cls


def set_in_feed(conn, set_ids, on: bool) -> int:
    """Send the sets (photo_set rowids) to the feed or take them out; returns the sets changed. Does not commit."""
    ids = [int(i) for i in set_ids]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    return conn.execute("UPDATE photo_set SET in_feed = ? WHERE rowid IN (%s) AND in_feed != ?" % marks, [int(on)] + ids + [int(on)]).rowcount


def aliases(conn) -> Dict[str, str]:
    """Other supplier spellings -> the key of their set."""
    return dict(conn.execute("SELECT alias_key, set_key FROM set_alias").fetchall())


def sync_sets(conn, plans: List[SetPlan], now: str) -> None:
    """Upsert planned sets (rank = plan order); needed sets absent from plans are retired and their queued jobs dropped.
    Does not commit: the caller commits (enqueue does)."""
    keep = set()
    previous = aliases(conn)
    present = frozenset(k.text() for p in plans for k in [p.key] + p.aliases)
    conn.execute("DELETE FROM set_alias")
    for rank, p in enumerate(plans):
        k = _settled_key(conn, p, previous, present)
        keep.add(k.text())
        sold_out = [SetKey(*_key_parts(a)) for a, t in previous.items() if t == k.text() and a not in present]
        for alias in [p.key] + p.aliases + sold_out:
            if alias.text() != k.text():
                conn.execute("INSERT OR REPLACE INTO set_alias (alias_key, set_key) VALUES (?,?)", (alias.text(), k.text()))
        conn.execute(
            "INSERT INTO photo_set (key, brand_key, model_key, season, studded, size_class, ref_url, size_hint, state, reason, rank, brand_name, model_name, positions, stock)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET brand_name=excluded.brand_name, model_name=excluded.model_name, ref_url=excluded.ref_url, size_hint=excluded.size_hint, rank=excluded.rank,"
            " positions=excluded.positions, stock=excluded.stock,"
            " state=CASE WHEN photo_set.state IN ('needed','needs_decision','retired') THEN excluded.state ELSE photo_set.state END,"
            " reason=CASE WHEN photo_set.state IN ('needed','needs_decision','retired') THEN excluded.reason ELSE photo_set.reason END",
            (k.text(), k.brand_key, k.model_key, k.season.value, int(k.studded), k.size_class, p.ref_url, p.size_hint, p.state, p.reason, rank, p.brand_name, p.model_name,
             p.positions, p.stock))
    for (key,) in conn.execute("SELECT key FROM photo_set WHERE state = ?", (STATE_NEEDED,)).fetchall():
        if key not in keep:
            conn.execute("UPDATE photo_set SET state = 'retired' WHERE key = ?", (key,))
            conn.execute("DELETE FROM image_job WHERE set_key = ? AND status = 'queued'", (key,))
    apply_priority(conn)


def spent_today(conn, now: str) -> float:
    """Cost of the jobs updated on the (UTC) day of `now`: the clock and rule of the daily cap."""
    return conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM image_job WHERE substr(updated, 1, 10) = ?", (now[:10],)).fetchone()[0]


RUSH_KEY = "image_rush"                     # set keys the owner asked for with «Сгенерировать» on a model (photo_agent)


def rush_keys(conn) -> List[str]:
    """Models asked for by name, in the order asked; first in the queue and in the handout."""
    from . import store
    try:
        keys = json.loads(store.get_state(conn, RUSH_KEY) or "[]")
    except ValueError:
        return []
    return [k for k in keys if isinstance(k, str)] if isinstance(keys, list) else []


def queue_stopped(conn) -> bool:
    """The owner's stop switch: nothing is queued or handed out while it is on."""
    from . import store
    return store.get_state(conn, QUEUE_STOP_KEY) == "1"


def enqueue(conn, now: str, max_attempts: int = MAX_ATTEMPTS, daily_cap_usd: float = DAILY_CAP_USD, est_job_usd: float = 0.10,
            max_open: int = MAX_OPEN_JOBS) -> int:
    """Queue generation jobs for needed sets (three_quarter first, set4 after it is approved); returns new jobs.
    The queue holds the `max_open` most important open jobs: running ones always count, queued ones of lower-ranked
    sets give way (they are deleted unstarted and queued again later). Budget = spent today + kept jobs at the
    estimated cost; stops before it would pass the daily cap. Stopped queue: nothing."""
    if queue_stopped(conn):
        conn.commit()
        return 0
    running = conn.execute("SELECT COUNT(*) FROM image_job WHERE status = 'running'").fetchone()[0]
    budget = spent_today(conn, now) + running * est_job_usd
    slots = max_open - running
    keep, added = [], 0
    rush = rush_keys(conn)
    sets = conn.execute("SELECT key FROM photo_set WHERE state = ? ORDER BY rank, key", (STATE_NEEDED,)).fetchall()
    sets.sort(key=lambda r: rush.index(r[0]) if r[0] in rush else len(rush))     # stable: the models asked for by name first
    for row in sets:
        if slots <= 0 or budget + est_job_usd > daily_cap_usd + 1e-9:
            break
        key = row[0]
        for kind in FRAME_KINDS:
            if _has_frame(conn, key, kind, "approved"):
                continue
            if kind == "set4" and not _has_frame(conn, key, "three_quarter", "approved"):
                continue
            if _has_frame(conn, key, kind, "pending"):
                continue
            if conn.execute("SELECT 1 FROM image_job WHERE set_key=? AND kind=? AND status='running'", (key, kind)).fetchone():
                continue
            if attempts_used(conn, key, kind) >= max_attempts:
                continue
            queued = conn.execute("SELECT id FROM image_job WHERE set_key=? AND kind=? AND status='queued' ORDER BY id LIMIT 1", (key, kind)).fetchone()
            if queued:
                keep.append(queued[0])
            else:
                keep.append(conn.execute("INSERT INTO image_job (set_key, kind, status, created, updated) VALUES (?,?,?,?,?)",
                                         (key, kind, "queued", now, now)).lastrowid)
                added += 1
            slots -= 1
            budget += est_job_usd
            break                                                  # one frame of a set at a time
    marks = ",".join("?" * len(keep)) or "NULL"
    conn.execute("DELETE FROM image_job WHERE status = 'queued' AND id NOT IN (%s)" % marks, keep)
    conn.commit()
    return added


def attempts_used(conn, key: str, kind: str) -> int:
    """Failed jobs plus rejected frames of one frame kind of a set."""
    tries = conn.execute("SELECT COUNT(*) FROM image_job WHERE set_key=? AND kind=? AND status='failed'", (key, kind)).fetchone()[0]
    return tries + conn.execute("SELECT COUNT(*) FROM set_frame WHERE set_key=? AND kind=? AND status='rejected'", (key, kind)).fetchone()[0]


def _has_frame(conn, key: str, kind: str, status: str) -> bool:
    return conn.execute("SELECT 1 FROM set_frame WHERE set_key=? AND kind=? AND status=?", (key, kind, status)).fetchone() is not None


MAX_PHOTO_BYTES = 25 * 1024 * 1024
_SIGNATURES = (("jpg", b"\xff\xd8\xff"), ("png", b"\x89PNG"))


def save_photo(conn, root: Path, data: bytes, source: str, now: str) -> int:
    """Store the picture as `root/<sha256>.<ext>` (bytes as given) and return its `photo.id`; the same bytes give the same id."""
    if len(data) > MAX_PHOTO_BYTES:
        raise ValueError("Файл больше 25 МБ")
    ext = next((e for e, sig in _SIGNATURES if data.startswith(sig)), None)
    if ext is None:
        raise ValueError("Это не JPEG и не PNG")
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
    except Exception:
        raise ValueError("Файл не открывается как картинка")
    sha = hashlib.sha256(data).hexdigest()
    row = conn.execute("SELECT id FROM photo WHERE sha256=?", (sha,)).fetchone()
    target = Path(root) / ("%s.%s" % (sha, ext))
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=sha + ".", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.chmod(tmp, 0o644)
            os.replace(tmp, str(target))
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    if row is not None:
        return row[0]
    with conn:
        cur = conn.execute("INSERT INTO photo(sha256, ext, width, height, bytes, source, created) VALUES (?,?,?,?,?,?,?)",
                           (sha, ext, width, height, len(data), source, now))
    return cur.lastrowid


def photo_url(base: str, sha256: str, ext: str) -> str:
    return "%s/photos/%s.%s" % (base, sha256, ext)


_LIBRARY_LINK = re.compile(r"/photos/[0-9a-f]{64}\.(?:jpg|png)(?:\?|$)")


def is_library_link(url: str) -> bool:
    """A link to our photo library (`photo_url`)."""
    return bool(_LIBRARY_LINK.search(url or ""))


def card_size_text(offer: Offer) -> str:
    """The size line of a card: `205/55 R16 94T`, plus ` XL` when the name has the XL token."""
    text = "%d/%d R%d %s%s" % (offer.width, offer.height, offer.diameter, offer.load, offer.speed)
    return text + " XL" if "XL" in re.split(r"[^0-9A-Za-zА-Яа-я]+", offer.name.upper()) else text


def card_variant(offer: Offer, is_pack: bool) -> str:
    """`account_photo.variant` of a card: the size line and `|x4` for a set of four (Id `-x4`), else `|x1`."""
    return "%s|%s" % (card_size_text(offer), "x4" if is_pack else "x1")


MAX_IMAGES = 10


@dataclass
class OwnPhotos:
    """The library links of one account: by_set = [three_quarter, set4] (+ crop), cards = {"<set key>#<variant>": link}."""
    by_set: Dict[str, List[str]]
    cards: Dict[str, str]
    allowed: FrozenSet[str]
    aliases: Dict[str, str] = field(default_factory=dict)      # another supplier spelling -> the set key


def _link(conn, base: str, photo_id: int) -> Optional[str]:
    row = conn.execute("SELECT sha256, ext FROM photo WHERE id=?", (photo_id,)).fetchone()
    return photo_url(base, row[0], row[1]) if row else None


def _account_link(conn, base: str, account_id: int, source_id: int, kind: str, variant: str = "") -> Optional[str]:
    row = conn.execute("SELECT photo_id FROM account_photo WHERE account_id=? AND source_photo_id=? AND kind=? AND variant=?",
                       (account_id, source_id, kind, variant)).fetchone()
    return _link(conn, base, row[0]) if row else None


def own_photos_for(conn, account_id: int, base: str) -> Optional[OwnPhotos]:
    """The account's own library links, or None when the switch `own_photos` is off or the account has no photo address.
    A set counts only when the owner sent it to the feed, with both frames approved and this account's files ready:
    originals for the primary account, `copy` for the rest."""
    from . import connections, store
    if store.get_state(conn, "own_photos") != "1" or not base:
        return None
    by_set: Dict[str, List[str]] = {}
    cards: Dict[str, str] = {}
    rows = conn.execute("SELECT set_key, kind, photo_id FROM set_frame WHERE status='approved'").fetchall()
    frames: Dict[str, Dict[str, int]] = {}
    for key, kind, photo_id in rows:
        frames.setdefault(key, {})[kind] = photo_id
    chosen = {r[0] for r in conn.execute("SELECT key FROM photo_set WHERE in_feed = 1")}
    for key, got in frames.items():
        if key not in chosen or any(kind not in got for kind in FRAME_KINDS):
            continue
        if account_id == connections.PRIMARY_ACCOUNT:
            links = [_link(conn, base, got[kind]) for kind in FRAME_KINDS]
        else:
            links = [_account_link(conn, base, account_id, got[kind], "copy") for kind in FRAME_KINDS]
        if not all(links):
            continue
        crop = _account_link(conn, base, account_id, got["three_quarter"], "crop")
        by_set[key] = links + ([crop] if crop else [])
        for variant, photo_id in conn.execute("SELECT variant, photo_id FROM account_photo WHERE account_id=? AND source_photo_id=? AND kind='card'",
                                              (account_id, got["three_quarter"])).fetchall():
            link = _link(conn, base, photo_id)
            if link:
                cards["%s#%s" % (key, variant)] = link
    allowed = frozenset(u for links in by_set.values() for u in links) | frozenset(cards.values())
    return OwnPhotos(by_set, cards, allowed, aliases(conn))


def image_urls(own: Optional[OwnPhotos], offer: Optional[Offer], is_pack: bool, fallback: Optional[str]) -> List[str]:
    """The links of an ad's `Images`: three_quarter, set4, card, crop, then `fallback` (the link the ad has without own photos).
    Not more than 10, no repeats; only `fallback` when the set has no ready own photos."""
    urls: List[str] = []
    key = set_key_for(offer) if own is not None and offer is not None else None
    text = own.aliases.get(key.text(), key.text()) if key is not None else None
    links = own.by_set.get(text) if text is not None else None
    if links:
        card = own.cards.get("%s#%s" % (text, card_variant(offer, is_pack)))
        urls = links[:2] + ([card] if card else []) + links[2:]
    if fallback:
        urls.append(fallback)
    out: List[str] = []
    for url in urls:
        if url not in out:
            out.append(url)
    return out[:MAX_IMAGES]
