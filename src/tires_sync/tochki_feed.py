"""4tochki as a supplier of the catalog (stage 1 of docs/integrations/4tochki-design.md).

Once an hour the cycle walks the supplier's search (passenger tires of one warehouse, page by page) and keeps what it got on disk next to
the WebMIM unload: the snapshot (`tochki-snapshot.json`), the tire cards (`tochki-cards.json`: size and indices, which the search does not
give; only new codes are asked) and the state of the last load (`tochki-state.json`: when, how many, what went wrong). A failed or empty
load keeps the previous snapshot, like a failed unload keeps the previous file.

The positions become the same `Offer` as a WebMIM position of a secondary supplier: Id `s<N>-<code>`, brand `S<N>:<brand>`, where N is the
supplier row the owner chose for 4tochki. They reach the cycle only when the switch «в фид» is on (stage 2; the panel cannot turn it on
yet); until then the catalog shows them on their own and nothing that builds a feed reads them."""
from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import connections
from .models import Offer, Season
from .tochki_client import CARDS_PER_CALL, TochkiClient, TochkiError
from .transport import Transport, urllib_transport
from .webmim import ParseResult, brand_key, merge_parsed, runflat_in_text, xl_in_text

SNAPSHOT_NAME = "tochki-snapshot.json"
CARDS_NAME = "tochki-cards.json"
STATE_NAME = "tochki-state.json"
MIN_INTERVAL = timedelta(minutes=55)     # «not more often than once an hour»: an hourly timer that starts a little early must not skip an hour
PAUSE_SECONDS = 1.0                      # between two calls (the supplier names no limits)
DEADLINE_SECONDS = 3 * 60                # the load runs at the end of the hourly cycle and must not hold it (the 06:45 run follows the
                                         # 06:40 one): the pages that do not fit stop the load, the cards that do not fit wait an hour
MAX_PAGES = 200
MIN_RATIO = 0.5                          # a list less than half of the previous one is not believed (as with the WebMIM unload)
REST_CAP = 41                            # the supplier sends 41 for «more than 40»
SEASONS = {"w": Season.WINTER, "s": Season.SUMMER, "u": Season.ALL}
NO_RUNFLAT = ("", "нет", "no", "0", "-", "false")
YES = ("true", "1", "да", "yes")           # `strengthening` (XL) of the card: «true» from the SOAP text, True in a saved test row


AUTO_NAME = re.compile(r"(?<!\d)4\s*-?\s*точ", re.I)  # a row chosen by itself must look like 4tochki («4Точки», «4 точки»; not «14 точек»)


def paths(base) -> Dict[str, Path]:
    folder = Path(base).parent
    return {"snapshot": folder / SNAPSHOT_NAME, "cards": folder / CARDS_NAME, "state": folder / STATE_NAME}


def config(conn) -> dict:
    """The 4tochki settings: login, the supplier row its positions belong to (None: not chosen, nothing is loaded) and the switch «в фид».
    No row saved and exactly one row fits (not the first, no WebMIM address): that row is used, "auto" says so (the owner 2026-10-21:
    «4 точки сделай»). Reading writes nothing: the choice is saved only when the owner saves the tab. A row chosen by itself never puts
    4tochki into the feeds: «в фид» counts only with a saved row."""
    raw = connections.get_config(conn, "tochki", 0)
    sid = raw.get("supplier_id")
    sid = sid if isinstance(sid, int) and not isinstance(sid, bool) and sid > 1 else None
    auto = False
    if sid is None:
        fits = [s for s in connections.tochki_supplier_choices(conn) if AUTO_NAME.search(s["name"] or "")]   # a new WebMIM row has no
        if len(fits) == 1:                                                                                  # address yet: not by itself
            sid, auto = fits[0]["id"], True
    return {"login": str(raw.get("login", "")), "supplier_id": sid, "feed": raw.get("feed") is True and not auto, "auto": auto}


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_state(base) -> dict:
    return _read_json(paths(base)["state"])


def _number(raw, kind=float):
    try:
        value = kind(str(raw).strip())
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _rows_of_page(items: List[dict], warehouse: int, seen: set) -> Tuple[List[dict], int]:
    rows, skipped = [], 0
    for item in items:
        code = item.get("code", "")
        if not code or code in seen:
            continue
        seen.add(code)
        if item.get("quality", "0") not in ("", "0"):            # limited, broken, defective: never offered as a new tire
            skipped += 1
            continue
        stock = next((w for w in item.get("whpr", ()) if w.get("wrh") == str(warehouse)), None)
        if stock is None:                                         # not on our warehouse
            continue
        rows.append({"code": code, "name": item.get("name", ""), "marka": item.get("marka", ""), "model": item.get("model", ""),
                     "season": item.get("season", ""), "thorn": item.get("thorn", "").lower() == "true", "type": item.get("type", ""),
                     "quality": 0, "img_big_my": item.get("img_big_my", ""), "img_big_pish": item.get("img_big_pish", ""),
                     "price": _number(stock.get("price")), "rest": _number(stock.get("rest"), int)})
    return rows, skipped


def download(client: TochkiClient, cards_path: Path, sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.monotonic) -> Tuple[int, List[dict], dict]:
    """Walk the search of the warehouse and join every position with its card. Cards the cache lacks are asked 200 at a time; when time
    runs out on them, the walk keeps what it has (those positions wait for their card till the next hour)."""
    start = clock()

    def late():
        return clock() - start > DEADLINE_SECONDS
    warehouse = client.warehouse_id()
    rows, seen, skipped, page, pages = [], set(), 0, 0, 1
    while page < pages:
        if page:
            sleep(PAUSE_SECONDS)
            if late():
                raise TochkiError("обход каталога не уложился в %d минут (страница %d из %d)." % (DEADLINE_SECONDS // 60, page + 1, pages))
        items, pages = client.find_tyre_page(warehouse, page)
        pages = min(pages, MAX_PAGES)
        got, bad = _rows_of_page(items, warehouse, seen)
        rows.extend(got)
        skipped += bad
        page += 1
    known = _read_json(cards_path)
    cards = {r["code"]: known[r["code"]] for r in rows if isinstance(known.get(r["code"]), dict)}
    missing = [r["code"] for r in rows if r["code"] not in cards]
    asked = 0
    for i in range(0, len(missing), CARDS_PER_CALL):
        if late():
            break
        sleep(PAUSE_SECONDS)
        cards.update(client.goods_info(missing[i:i + CARDS_PER_CALL]))
        asked += 1
    _write_json(cards_path, cards)
    for row in rows:
        card = cards.get(row["code"], {})
        for key, value in card.items():                           # the search's own name, brand and model win over the card's
            row.setdefault(key, value)
        row["name"] = row["name"] or card.get("name", "")
        row["card"] = row["code"] in cards
    info = {"pages": page, "skipped_quality": skipped, "card_calls": asked, "without_card": sum(1 for r in rows if not r["card"])}
    return warehouse, rows, info


def refresh(conn, box, base, now: datetime, http: Transport = urllib_transport, sleep: Callable[[float], None] = time.sleep,
            clock: Callable[[], float] = time.monotonic) -> dict:
    """One load, if 4tochki is set up and the last attempt is an hour old. Never raises for the supplier's troubles: the result says
    {"status": "off" | "wait" | "ok" | "error", "message": …} and the state file keeps it for the panel."""
    cfg = config(conn)
    if not cfg["supplier_id"] or not cfg["login"]:
        return {"status": "off", "message": "4 точки не настроены (логин или поставщик не выбран)."}
    where = paths(base)
    state = read_state(base)
    try:
        last = datetime.fromisoformat(state["attempt_at"]) if state.get("attempt_at") else None
        if last is not None and last - now <= timedelta(hours=1) and now - last < MIN_INTERVAL:   # hours ahead: the old Moscow clock, no attempt
            return {"status": "wait", "message": "прошлая загрузка меньше часа назад."}
    except (TypeError, ValueError):
        pass
    password = connections.get_secret(conn, box, "tochki", 0, "password") or ""
    if not password:
        return {"status": "off", "message": "4 точки не настроены (пароль не задан)."}
    stamp = now.isoformat(timespec="seconds")
    state["attempt_at"] = stamp
    _write_json(where["state"], state)
    started = clock()
    try:
        warehouse, rows, info = download(TochkiClient(cfg["login"], password, http=http), where["cards"], sleep, clock)
        previous = _read_json(where["snapshot"]).get("items") or []
        if not rows:
            raise TochkiError("поставщик вернул пустой список шин склада, прежние позиции оставлены.")
        if previous and len(rows) < len(previous) * MIN_RATIO:
            raise TochkiError("позиций стало %d вместо %d, прежние позиции оставлены." % (len(rows), len(previous)))
        _write_json(where["snapshot"], {"version": 1, "fetched_at": stamp, "warehouse_id": warehouse, "items": rows})
        state.update(info, ok_at=stamp, items=len(rows), seconds=round(clock() - started, 1), error="", error_at="")
        return {"status": "ok", "message": "4 точки: загружено позиций %d (страниц %d, без карточки %d)." % (
            len(rows), info["pages"], info["without_card"])}
    except Exception as err:                                      # whatever it is, the page shows that the load failed
        known = isinstance(err, (TochkiError, OSError, ValueError))
        state.update(error=(str(err)[:300] if known else "") or type(err).__name__, error_at=stamp)
        return {"status": "error", "message": "4 точки: загрузка не удалась, позиции прежние. %s" % state["error"]}
    finally:
        _write_json(where["state"], state)


def _size(raw, name: str, problems: List[str]) -> int:
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        value = None
    if value is None or value <= 0 or value != value.to_integral_value():
        problems.append("bad_%s:%s" % (name, raw))
        return 0
    return int(value)


def parse_snapshot(data: dict, supplier: int) -> ParseResult:
    """The snapshot as positions of supplier N, the same way a WebMIM unload of a secondary supplier becomes positions."""
    result = ParseResult()
    territory = str(data.get("warehouse_id", ""))
    seen, dupes = set(), set()
    for row in data.get("items") or ():
        if not isinstance(row, dict) or not row.get("code"):
            continue
        gid = "s%d-%s" % (supplier, row["code"])
        if gid in seen:
            dupes.add(gid)
            continue
        seen.add(gid)
        problems: List[str] = []
        if not row.get("card"):
            problems.append("no_card")
        width = _size(row.get("width"), "width", problems)
        height = _size(row.get("height"), "height", problems)
        diameter = _size(row.get("diameter"), "diameter", problems)
        stock, price = row.get("rest"), row.get("price")
        if not isinstance(stock, int) or isinstance(stock, bool) or stock < 0:
            problems.append("bad_stock:%s" % stock)
        if not isinstance(price, (int, float)) or isinstance(price, bool) or not math.isfinite(price):
            problems.append("bad_price:%s" % price)
        if problems:
            result.issues[gid] = problems
            continue
        name = " ".join(str(row.get("name", "")).split())
        marka, model = str(row.get("marka") or row.get("brand") or ""), str(row.get("model", ""))
        result.offers[gid] = Offer(
            goods_id=gid, name=name, name_orig=name, brand_raw=marka, brand_key="S%d:%s" % (supplier, brand_key(marka)), model_raw=model,
            season=SEASONS.get(str(row.get("season", "")).lower(), Season.UNKNOWN), width=width, height=height, diameter=diameter,
            speed=str(row.get("speed_index", "")).strip(), load=str(row.get("load_index", "")).strip(), code="", stock=stock,
            base_price=float(price), image=row.get("img_big_my") or row.get("img_big_pish") or "",
            runflat=str(row.get("puncture", "")).strip().casefold() not in NO_RUNFLAT or runflat_in_text("%s %s" % (name, model)),
            studded=row.get("thorn") is True, car_type="", mnf_year="", territory_rn=territory, country="",
            homologation=str(row.get("omolog", "")).strip(),
            xl=str(row.get("strengthening", "")).strip().casefold() in YES or xl_in_text("%s %s" % (name, model)))
    for gid in dupes:
        result.offers.pop(gid, None)
        result.issues.setdefault(gid, []).append("duplicate_in_territory")
    return result


_cache: dict = {}


def load_snapshot(base, supplier: int) -> Optional[Tuple[ParseResult, dict]]:
    """The positions of the last good load (cached until the file changes) and the snapshot's own data; None without a snapshot."""
    path = paths(base)["snapshot"]
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        return None
    key = (str(path), stamp, supplier)
    found = _cache.get(key)                       # the panel serves several requests at once: read the cache once, never twice
    if found is None:
        data = _read_json(path)
        if not isinstance(data.get("items"), list):
            return None
        found = (parse_snapshot(data, supplier), {"fetched_at": data.get("fetched_at", ""), "warehouse_id": data.get("warehouse_id")})
        _cache.clear()
        _cache[key] = found
    return found


def catalog_view(conn, parsed, base):
    """The catalog's positions with those of 4tochki added for viewing, while the switch «в фид» is off. Returns (positions, the Ids that
    are shown only). Nothing that builds a feed calls this. A supplier row that has WebMIM positions of its own is never mixed with them."""
    cfg = config(conn)
    sid = cfg["supplier_id"]
    if not sid or cfg["feed"]:
        return parsed, frozenset()
    loaded = load_snapshot(base, sid)
    if loaded is None or not loaded[0].offers:
        return parsed, frozenset()
    prefix = "s%d-" % sid
    if any(g.startswith(prefix) for g in parsed.offers):
        return parsed, frozenset()
    key = ("view", id(parsed), id(loaded[0]))
    found = _cache.get(key)
    if found is None:
        merged = merge_parsed([parsed, ParseResult(offers=loaded[0].offers)])
        merged.unavailable = set(getattr(parsed, "unavailable", ()))
        found = (merged, frozenset(loaded[0].offers), parsed, loaded[0])      # the sources are kept so that their ids are not reused
        for old in [k for k in list(_cache) if k[0] == "view"]:
            _cache.pop(old, None)
        _cache[key] = found
    return found[0], found[1]
