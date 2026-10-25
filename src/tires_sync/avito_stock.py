"""The number in stock of our ads on Avito, by the supplier's stock (the owner 2026-10-24, «количество в фиде, вариант А»).

The tire category takes no Quantity in the feed, so the number goes through Avito's stock API (stock-management/1): `info` reads it,
`stocks` sets it. Off unless switched on per account (connection kind `avito_stock`: mode off / list / all, the codes of the list, an
optional ceiling); with the switch off not a single request is made. What goes: the supplier's stock as it is (a set of four: whole sets),
0 for an ad that left the feed; an ad whose stock is unknown (no row in the unload, a row with an error, its own point) gets nothing.
Only a number that changed is sent; the journal `avito_stock` keeps the last one and how it went, and what Avito showed before our first
send (`--restore` puts that back). A write is made once: 429 leaves the rest «not sent», a failure without an answer marks the batch
«unknown»; either goes again in the next pass (the number is absolute, so sending it again changes nothing)."""
from __future__ import annotations

import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from . import connections, feed_check, store
from .avito import STOCK_INFO_CHUNK, STOCK_MAX, STOCK_SET_CHUNK, AvitoAuthError, AvitoClient, AvitoError, RateLimited

KIND = "avito_stock"
MODES = ("off", "list", "all")
MODE_LABELS = {"off": "выключено", "list": "только позиции из списка", "all": "все объявления"}
MAX_CODES = 500
PAUSE = 1.0                  # seconds between two requests: at most 60 a minute, Avito allows 100 for each method
FAILED_PAUSE = timedelta(days=1)   # an ad Avito refused is offered the same number again after a day, not every cycle
MAPPING_CHUNK = 50           # our Ids per request that finds their Avito ids
REFUSAL_STOP = 5             # refusals in a row of one-ad requests: Avito refuses the method itself, the pass stops (not one request per ad)
DOWN_STOP = 2                 # one-ad readings in a row Avito answers 5xx: the service is down, the pass stops (as a failed batch did before)

SKIP_UNKNOWN = "остаток неизвестен (нет в выгрузке или строка с ошибкой)"
SKIP_OWN = "своя точка: остаток не от поставщика"
SKIP_NOT_LISTED = "не в списке"
SKIP_NO_ITEM = "нет номера объявления на Авито"
SKIP_MANY_ITEMS = "у Id несколько живых объявлений на Авито"
SKIP_SAME = "без изменений"
SKIP_NO_CODE = "код позиции по Id не установлен"
SKIP_REFUSED = "Авито отказал, повтор через сутки"
GONE = "снято из фида"


def moscow_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=3)


def config(conn, account_id: int) -> dict:
    raw = connections.get_config(conn, KIND, account_id)
    mode = raw.get("mode") if raw.get("mode") in MODES else "off"
    cap = raw.get("cap")
    return {"mode": mode, "codes": [str(c) for c in raw.get("codes") or []],
            "cap": int(cap) if isinstance(cap, int) and not isinstance(cap, bool) and 1 <= cap <= STOCK_MAX else None}


def parse_codes(text: str) -> List[str]:
    codes = []
    for code in re.split(r"[\s,;]+", str(text or "").strip()):
        if not code:
            continue
        if not re.fullmatch(r"[0-9A-Za-z._-]{1,40}", code):
            raise ValueError("Код «%s» не похож на код позиции." % code[:40])
        if code not in codes:
            codes.append(code)
    if len(codes) > MAX_CODES:
        raise ValueError("В списке больше %d кодов." % MAX_CODES)
    return codes


def parse_cap(text: str) -> Optional[int]:
    text = str(text or "").strip()
    if not text:
        return None
    if not re.fullmatch(r"\d{1,6}", text) or not 1 <= int(text) <= STOCK_MAX:
        raise ValueError("Потолок: целое число от 1 до %d или пусто." % STOCK_MAX)
    return int(text)


def save_config(conn, account_id: int, mode: str, codes_text: str, cap_text: str, now: str, author: str) -> dict:
    """Check and keep the switch of an account; ValueError with a message for the owner when something is wrong."""
    if mode not in MODES:
        raise ValueError("Неизвестный режим остатков.")
    codes, cap = parse_codes(codes_text), parse_cap(cap_text)
    if mode == "list" and not codes:
        raise ValueError("Режим «только из списка» без кодов: впишите хотя бы один код.")
    cfg = {"mode": mode, "codes": codes, "cap": cap}
    connections.set_config(conn, KIND, account_id, cfg, now, author)
    store.write_audit(conn, now, author, "avito_stock_config", {"account_id": account_id, "mode": mode, "codes": len(codes), "cap": cap})
    return cfg


@dataclass
class Line:
    ad_id: str
    code: str
    quantity: Optional[int] = None      # what goes to Avito; None: nothing goes
    stock: Optional[int] = None         # the supplier's stock behind it
    pack: int = 1                       # pieces in one unit of the ad: a set of four is 4
    avito_id: Optional[int] = None
    last: Optional[int] = None          # the last number Avito took
    skip: str = ""                      # why nothing goes
    gone: bool = False                  # the ad left the feed: 0


@dataclass
class Plan:
    lines: List[Line]
    note: str = ""                      # why the account gives nothing at all
    stale: bool = False                 # the cycle's record of the codes is not about the published file: the codes are read without it


class _Pacer:
    def __init__(self, sleep: Callable[[float], None]):
        self._sleep, self._first = sleep, True

    def __call__(self) -> None:
        if not self._first:
            self._sleep(PAUSE)
        self._first = False


def _chunks(values: List, size: int) -> Iterable[List]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _feed_ids(path: Path) -> Optional[List[str]]:
    try:
        root = ET.parse(str(path)).getroot()
    except (OSError, ET.ParseError):
        return None
    return [ad.findtext("Id") or "" for ad in root.findall("Ad") if ad.findtext("Id")]


def _pack(ad_id: str) -> int:
    found = feed_check.PACK.search(str(ad_id).split("_")[0])
    return int(found.group(1)) if found else 1


def _journal(conn, account_id: int) -> Dict[int, tuple]:
    return {row[0]: row[1:] for row in conn.execute(
        "SELECT avito_id, ad_id, code, quantity, status, at, before_at FROM avito_stock WHERE account_id = ?", (account_id,))}


def _items_of(conn, account_id: int) -> Dict[str, List[int]]:
    out: Dict[str, List[int]] = {}
    for avito_id, ad_id in conn.execute(                    # seen live by the last reading of the cabinet: the status of an ad gone since is never lowered
            "SELECT avito_id, ad_id FROM avito_ad WHERE account_id = ? AND status = 'active' AND ad_id IS NOT NULL AND ad_id != '' "
            "AND seen_at = (SELECT MAX(seen_at) FROM avito_ad WHERE account_id = ?)", (account_id, account_id)):
        out.setdefault(str(ad_id), []).append(int(avito_id))
    return out


def plan(conn, account: dict, parsed, feed_root, cfg: dict, client: Optional[AvitoClient] = None,
         now: Optional[datetime] = None) -> Plan:
    """What each ad of the account's published feed should show (reads only: the base, the feed file, and with `client` Avito's own map of
    our Ids for the ads the daily statistics have not mapped yet)."""
    from . import cycle
    if account.get("feed_mode") == "mirror":
        return Plan([], "перенос чужого файла: коды позиций неизвестны")
    if parsed is None:
        return Plan([], "нет выгрузки поставщика")
    ids = _feed_ids(Path(feed_root) / account["feed_token"] / "avito_feed.xml")
    if not ids:
        return Plan([], "опубликованного фида нет или он пуст")
    codes, recorded = _codes_of(conn, account, Path(feed_root) / account["feed_token"] / "avito_feed.xml", ids, parsed)
    own = {gid for gid, item in store.load_published(conn, account["id"]).items() if item.location == "own"}
    listed = set(cfg["codes"]) if cfg["mode"] == "list" else None
    journal = _journal(conn, account["id"])
    items = _items_of(conn, account["id"])
    lines: List[Line] = []
    for ad_id in ids:
        code = codes.get(ad_id, "")
        line = Line(ad_id, code, pack=_pack(ad_id))
        lines.append(line)
        if not code:
            line.skip = SKIP_NO_CODE
            continue
        if listed is not None and code not in listed:
            line.skip = SKIP_NOT_LISTED
            continue
        offer = parsed.offers.get(code)
        if code in own:
            line.skip = SKIP_OWN
        elif offer is None or code in parsed.issues:
            line.skip = SKIP_UNKNOWN
        else:
            line.stock = max(0, int(offer.stock))
            line.quantity = min(line.stock // line.pack, cfg["cap"] or STOCK_MAX, STOCK_MAX)
    _bind(lines, items, client)
    in_feed = set(ids)
    codes_in_feed = {line.code for line in lines if line.code}
    bound_now = {line.avito_id for line in lines if line.avito_id is not None}
    for avito_id, (ad_id, code, quantity, status, _at, _before) in sorted(journal.items()):
        if ad_id in in_feed or code in codes_in_feed or avito_id in bound_now:     # the position is still in the feed (maybe under another Id)
            continue
        if status == "restored" or (status in ("sent", "failed") and quantity == 0):   # already 0, or Avito refused 0 (a deleted ad): never again
            continue
        if listed is not None and code not in listed:
            continue
        lines.append(Line(ad_id, code, quantity=0, avito_id=avito_id, gone=True))
    now = now or moscow_now()
    for line in lines:
        if line.skip or line.avito_id is None:
            line.quantity = None
        if line.quantity is None:
            continue
        row = journal.get(line.avito_id)
        if row is None:
            continue
        _ad, _code, quantity, status, at, _before = row
        if status == "sent":
            line.last = quantity
            if quantity == line.quantity:
                line.skip, line.quantity = SKIP_SAME, None
        elif status == "failed" and quantity == line.quantity and _recent(at, now):
            line.skip, line.quantity = SKIP_REFUSED, None
    return Plan(lines, stale=not recorded)


def _codes_of(conn, account: dict, path: Path, ids: List[str], parsed) -> Tuple[Dict[str, str], bool]:
    """Id -> the code of its position, the way the cycle's check reads it (cycle.gate): the Ids the cycle recorded for exactly this file,
    the account's bindings (inherited and alias Ids), and only then the Id that is the code itself in this account's own form
    (`<code>[-xN]<suffix>[_rYYYYMMDD]`). An Id none of them explains gets no code: a guess could put one position's number on another."""
    from . import cycle, mirror_feed
    from .settings_store import active_settings
    try:
        saved = json.loads(store.get_state(conn, cycle._inputs_key(account["id"])) or "{}")
    except (ValueError, TypeError):
        saved = {}
    recorded = dict(saved.get("id_codes") or {}) if isinstance(saved, dict) and saved.get("sha") and saved.get("sha") == cycle._sha_of(path) else {}
    bound_ids = mirror_feed.adopted_maps(conn, account["id"])[0]
    eff = connections.effective_settings(conn, account["id"], active_settings(conn))
    bound = cycle._bound_codes(bound_ids, getattr(parsed, "offers", {}) or {}, eff.min_stock, store.load_published(conn, account["id"]))
    by_id: Dict[str, List[str]] = {}
    for code, bound_id in bound_ids.items():
        by_id.setdefault(str(bound_id), []).append(code)
    suffix = connections.feed_style(conn, account["id"])[0]
    out: Dict[str, str] = {}
    for ad_id in ids:
        plain = re.sub(r"-x\d+$", "", ad_id)
        code = recorded.get(ad_id) or bound.get(ad_id) or bound.get(plain)
        if not code:
            mine = by_id.get(ad_id) or by_id.get(plain) or []
            own = mirror_feed.supplier_code(plain)
            code = own if own in mine else mine[0] if len(mine) == 1 else ""        # several bound and none picked over its own: its own (as _bound_codes)
        if not code:
            guess = feed_check._code(ad_id)
            if re.fullmatch(re.escape(guess) + r"(-x\d+)?" + re.escape(suffix) + r"(_r\d{8})?", ad_id) and str(ad_id) not in by_id:
                code = guess
        out[ad_id] = code
    return out, bool(recorded) or bool(saved.get("sha")) and saved.get("sha") == cycle._sha_of(path)


def _recent(at: str, now: datetime) -> bool:
    try:
        return now - datetime.fromisoformat(at) < FAILED_PAUSE
    except (TypeError, ValueError):
        return False


def _bind(lines: List[Line], items: Dict[str, List[int]], client: Optional[AvitoClient]) -> None:
    """The Avito id of every ad that has something to show: from the base, else (with a client) from Avito's map of our Ids."""
    wanted = [line for line in lines if line.quantity is not None and not line.skip and line.avito_id is None]
    missing = sorted({line.ad_id for line in wanted if line.ad_id not in items})
    found: Dict[str, int] = {}
    if client is not None:
        for chunk in _chunks(missing, MAPPING_CHUNK):
            for ad_id, avito_id in client.avito_ids_for(chunk).items():
                if avito_id:
                    found[str(ad_id)] = int(avito_id)
    for line in wanted:
        known = items.get(line.ad_id) or ([found[line.ad_id]] if line.ad_id in found else [])
        if len(known) > 1:
            line.skip = SKIP_MANY_ITEMS
        elif known:
            line.avito_id = known[0]
        else:
            line.skip = SKIP_NO_ITEM


def _mark(conn, account_id: int, line: Line, quantity: Optional[int], status: str, error: str, now: str) -> None:
    conn.execute(
        "INSERT INTO avito_stock (account_id, avito_id, ad_id, code, quantity, status, error, at) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(account_id, avito_id) DO UPDATE SET ad_id = excluded.ad_id, code = excluded.code, quantity = excluded.quantity, "
        "status = excluded.status, error = excluded.error, at = excluded.at",
        (account_id, line.avito_id, line.ad_id, line.code, quantity, status, error[:300], now))


def _keep_before(conn, account_id: int, line: Line, answer: dict, now: str) -> None:
    unlimited = bool(answer.get("is_unlimited"))
    quantity = answer.get("quantity")
    before = int(quantity) if isinstance(quantity, int) and not isinstance(quantity, bool) and not unlimited else None
    conn.execute(
        "INSERT INTO avito_stock (account_id, avito_id, ad_id, code, before_quantity, before_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(account_id, avito_id) DO UPDATE SET before_quantity = excluded.before_quantity, before_at = excluded.before_at "
        "WHERE avito_stock.before_at IS NULL",
        (account_id, line.avito_id, line.ad_id, line.code, before, now))


def _client_error(err: AvitoError) -> bool:
    """Avito refused the request itself (a bad id, a bad body): the ads, not the service; the keys (a refused token: any status) and 429
    are not that."""
    return not isinstance(err, AvitoAuthError) and 400 <= err.status < 500 and err.status not in (401, 403, 429)


def _outcome(err: BaseException) -> str:
    """What a failed write left: «not_sent» (429, the keys refused: Avito did not take it), «failed» (Avito refused the request
    itself), «unknown» (5xx, the network, an answer that is not one: it may have gone)."""
    if isinstance(err, (RateLimited, AvitoAuthError)) or (isinstance(err, AvitoError) and err.status in (401, 403)):
        return "not_sent"
    if isinstance(err, AvitoError) and _client_error(err):
        return "failed"
    return "unknown"


def _keys_refused(client: AvitoClient) -> str:
    """The token first, before any request of a pass: refused keys (whatever the status) or a token service down stop the pass at once,
    and are never taken for Avito refusing our ads. Empty when the token is there (it is then kept for the pass)."""
    try:
        client._bearer()
    except (AvitoError, OSError) as err:
        return "токен Авито: %s" % _describe(err)
    return ""


def _server_error(err: AvitoError) -> bool:
    """A 5xx of the reading itself (not 429, not the keys): with `unread`, one ad may be the cause, not the service."""
    return not isinstance(err, (RateLimited, AvitoAuthError)) and err.status >= 500


def read_info(client: AvitoClient, avito_ids: List[int], pace: Callable[[], None],
              unread: Optional[Dict[int, str]] = None) -> Tuple[Dict[int, dict], Dict[int, str]]:
    """Avito's stock of these ads: (answers by id, refusals by id). A refused batch is asked again one ad at a time (one dead id fails a
    whole batch). A failure of the service (429, 5xx, the network, the keys) is raised. With `unread`, a 5xx is asked again one ad at a time
    too: an ad Avito answers 5xx alone goes there (it is not refused: asked again next pass); DOWN_STOP of them in a row: the service, raised."""
    found: Dict[int, dict] = {}
    refused: Dict[int, str] = {}
    in_row, down = [0], [0]

    def refuse(one: int, err: AvitoError) -> None:
        refused[one] = "%s %s" % (err.status, err.message)
        in_row[0] += 1
        if in_row[0] >= REFUSAL_STOP:
            raise AvitoError(err.status, err.code, "Авито отказывает подряд (%d раз): %s" % (in_row[0], err.message))

    def alone(err: AvitoError) -> bool:
        return _client_error(err) or (unread is not None and _server_error(err))

    def miss(one: int, err: AvitoError) -> None:
        if _client_error(err):
            refuse(one, err)
            return
        unread[one] = "%s %s" % (err.status, err.message)
        down[0] += 1
        if down[0] >= DOWN_STOP:
            raise err
    for chunk in _chunks(list(avito_ids), STOCK_INFO_CHUNK):
        pace()
        try:
            for entry in client.stocks_info(chunk):
                if _item_id(entry) is not None:
                    found[_item_id(entry)] = entry
            in_row[0] = down[0] = 0
            continue
        except AvitoError as err:
            if not alone(err):
                raise
            if len(chunk) == 1:
                miss(chunk[0], err)
                continue
        for one in chunk:
            pace()
            try:
                for entry in client.stocks_info([one]):
                    if _item_id(entry) is not None:
                        found[_item_id(entry)] = entry
                in_row[0] = down[0] = 0
            except AvitoError as err:
                if not alone(err):
                    raise
                miss(one, err)
    for one in avito_ids:
        if one not in found and one not in refused and one not in (unread or {}):
            refused[one] = "Авито не вернул остаток"
    return found, refused


def push(conn, client: AvitoClient, account: dict, lines: List[Line], now: datetime, sleep: Callable[[float], None] = time.sleep,
         author: str = "система") -> Dict[str, int]:
    """Send the numbers of `lines` that have one (a write: the client must allow it). Before the first send of an ad, what Avito shows is
    kept for --restore; an ad Avito does not know there is not sent."""
    now_iso = now.isoformat(timespec="seconds")
    counts = {"sent": 0, "failed": 0, "unknown": 0, "not_sent": 0}
    todo = [line for line in lines if line.quantity is not None and not line.skip and line.avito_id is not None]
    if not todo:
        return counts
    refused_keys = _keys_refused(client)
    if refused_keys:
        for line in todo:
            _mark(conn, account["id"], line, line.quantity, "not_sent", refused_keys, now_iso)
        counts["not_sent"] = len(todo)
        conn.commit()
        _audit(conn, account, counts, now_iso, author)
        return counts
    pace = _Pacer(sleep)
    journal = _journal(conn, account["id"])
    fresh = [line for line in todo if journal.get(line.avito_id) is None or not journal[line.avito_id][5]]
    unread: Dict[int, str] = {}
    try:
        found, refused = read_info(client, [line.avito_id for line in fresh], pace, unread) if fresh else ({}, {})
    except (AvitoError, OSError) as err:
        for line in todo:
            _mark(conn, account["id"], line, line.quantity, "not_sent", "чтение остатков: %s" % _describe(err), now_iso)
        counts["not_sent"] = len(todo)
        conn.commit()
        _audit(conn, account, counts, now_iso, author)
        return counts
    ready = []
    for line in todo:
        if line.avito_id in unread:                             # Avito failed on this ad's reading alone: asked again next pass
            _mark(conn, account["id"], line, line.quantity, "not_sent", "чтение остатков: %s" % unread[line.avito_id], now_iso)
            counts["not_sent"] += 1
            continue
        if line.avito_id in refused:
            _mark(conn, account["id"], line, line.quantity, "failed", "чтение остатков: %s" % refused[line.avito_id], now_iso)
            counts["failed"] += 1
            continue
        if line.avito_id in found:
            _keep_before(conn, account["id"], line, found[line.avito_id], now_iso)
        ready.append(line)
    conn.commit()
    stopped = ""
    queue = list(_chunks(ready, STOCK_SET_CHUNK))
    in_row = 0
    while queue:
        chunk = queue.pop(0)
        if stopped:
            for line in chunk:
                _mark(conn, account["id"], line, line.quantity, "not_sent", stopped, now_iso)
            counts["not_sent"] += len(chunk)
            continue
        pace()
        try:
            answers = client.set_stocks([(line.avito_id, line.quantity) for line in chunk])
        except (AvitoError, OSError) as err:
            outcome = _outcome(err)
            if outcome == "failed" and len(chunk) > 1:          # a refused batch changed nothing: one ad at a time finds the one Avito refuses
                queue[0:0] = [[line] for line in chunk]
                continue
            for line in chunk:
                _mark(conn, account["id"], line, line.quantity, outcome, _describe(err), now_iso)
            counts[outcome] += len(chunk)
            in_row = in_row + 1 if outcome == "failed" else 0
            if outcome != "failed":
                stopped = "проход остановлен: %s" % _describe(err)
            elif in_row >= REFUSAL_STOP:
                stopped = "проход остановлен: Авито отказывает подряд (%d раз): %s" % (in_row, _describe(err))
            conn.commit()
            continue
        in_row = 0
        by_id = {_item_id(answer): answer for answer in answers}
        for line in chunk:
            answer = by_id.get(line.avito_id)
            if answer is None:
                _mark(conn, account["id"], line, line.quantity, "unknown", "нет в ответе Авито", now_iso)
                counts["unknown"] += 1
            elif answer.get("success") is True:
                _mark(conn, account["id"], line, line.quantity, "sent", "", now_iso)
                counts["sent"] += 1
            else:
                errors = answer.get("errors") or []
                _mark(conn, account["id"], line, line.quantity, "failed", "; ".join(str(e) for e in errors) or "Авито не принял", now_iso)
                counts["failed"] += 1
        conn.commit()
    _audit(conn, account, counts, now_iso, author)
    return counts


def _item_id(entry) -> Optional[int]:
    try:
        return int(entry["item_id"]) if isinstance(entry, dict) and not isinstance(entry.get("item_id"), bool) else None
    except (KeyError, TypeError, ValueError):
        return None


def _describe(err: BaseException) -> str:
    if isinstance(err, AvitoError):
        return ("%s %s" % (err.status, err.message))[:200]
    return ("%s: %s" % (type(err).__name__, err))[:200]


def _audit(conn, account: dict, counts: Dict[str, int], now_iso: str, author: str, action: str = "avito_stock_push") -> None:
    store.write_audit(conn, now_iso, author, action, dict(counts, account_id=account["id"]))
    conn.commit()


def unrestorable(conn, account_id: int) -> int:
    """Ads whose number we changed and --restore cannot give back: before us Avito showed «no limit», which the API cannot set."""
    return conn.execute("SELECT COUNT(*) FROM avito_stock WHERE account_id = ? AND before_at IS NOT NULL AND before_quantity IS NULL "
                        "AND status != 'restored'", (account_id,)).fetchone()[0]


def summary(conn, account_id: int) -> dict:
    """For the page of the account: how many ads stand at each status, when the last pass was, a few of Avito's refusals."""
    counts = dict(conn.execute("SELECT status, COUNT(*) FROM avito_stock WHERE account_id = ? GROUP BY status", (account_id,)).fetchall())
    last = conn.execute("SELECT MAX(at) FROM avito_stock WHERE account_id = ?", (account_id,)).fetchone()[0]
    refused = [{"code": code, "ad_id": ad_id, "error": error} for code, ad_id, error in conn.execute(
        "SELECT code, ad_id, error FROM avito_stock WHERE account_id = ? AND status = 'failed' ORDER BY at DESC LIMIT 5", (account_id,))]
    return {"counts": counts, "last_at": last or "", "refused": refused}


def restore_lines(conn, account_id: int) -> List[Line]:
    """The ads whose number we changed, with what Avito showed before our first send."""
    return [Line(ad_id, code, quantity=before, avito_id=avito_id, last=quantity) for avito_id, ad_id, code, quantity, before in conn.execute(
        "SELECT avito_id, ad_id, code, quantity, before_quantity FROM avito_stock WHERE account_id = ? AND before_at IS NOT NULL "
        "AND before_quantity IS NOT NULL AND status != 'restored' ORDER BY avito_id", (account_id,))]


def restore(conn, client: AvitoClient, account: dict, now: datetime, sleep: Callable[[float], None] = time.sleep,
            author: str = "система") -> Dict[str, int]:
    """Put back the numbers Avito showed before our first send. Only with the account switched off: else the next cycle sends ours again."""
    if config(conn, account["id"])["mode"] != "off":
        raise ValueError("сначала выключите остатки у аккаунта «%s» (режим «выкл»), потом возвращайте прежние числа" % account["name"])
    now_iso = now.isoformat(timespec="seconds")
    counts = {"restored": 0, "failed": 0, "unknown": 0, "not_sent": 0}
    lines = restore_lines(conn, account["id"])
    stopped = _keys_refused(client) if lines else ""
    pace = _Pacer(sleep)
    queue = list(_chunks(lines, STOCK_SET_CHUNK))
    in_row = 0
    while queue:
        chunk = queue.pop(0)
        if stopped:
            counts["not_sent"] += len(chunk)
            continue
        pace()
        try:
            answers = client.set_stocks([(line.avito_id, line.quantity) for line in chunk])
        except (AvitoError, OSError) as err:
            outcome = _outcome(err)
            if outcome == "failed" and len(chunk) > 1:          # one deleted ad fails the batch: one ad at a time gives the others back
                queue[0:0] = [[line] for line in chunk]
                continue
            if outcome in ("unknown", "failed"):                # unknown: it may have gone; either way the next --restore sends it again
                for line in chunk:
                    _mark(conn, account["id"], line, line.last, outcome, "возврат: %s" % _describe(err), now_iso)
            counts[outcome] += len(chunk)
            in_row = in_row + 1 if outcome == "failed" else 0
            if outcome != "failed" or in_row >= REFUSAL_STOP:
                stopped = _describe(err)
            conn.commit()
            continue
        in_row = 0
        done = {_item_id(a) for a in answers if isinstance(a, dict) and a.get("success") is True}
        for line in chunk:
            if line.avito_id in done:
                _mark(conn, account["id"], line, line.quantity, "restored", "", now_iso)
                counts["restored"] += 1
            else:
                counts["failed"] += 1
        conn.commit()
    _audit(conn, account, counts, now_iso, author, "avito_stock_restore")
    return counts


def info(conn, client: AvitoClient, account: dict, parsed, feed_root, limit: int = 5,
         sleep: Callable[[float], None] = time.sleep) -> Tuple[List[dict], str]:
    """`stock-push --info`: what Avito shows as the stock of a few of our live ads (reads only; the codes of the account's list first).
    Rows: code, ad_id, item_id, ours (the number we would send), answer (Avito's entry as it came) or error."""
    cfg = config(conn, account["id"])
    p = plan(conn, account, parsed, feed_root, {"mode": "all", "codes": [], "cap": cfg["cap"]}, client=client)
    if p.note:
        return [], p.note
    listed = set(cfg["codes"])
    bound = [line for line in p.lines if line.avito_id is not None and not line.gone]
    bound.sort(key=lambda line: line.code not in listed)
    chosen = bound[:max(1, limit)]
    if not chosen:
        return [], "ни одно объявление фида не связано с номером на Авито"
    found, refused = read_info(client, [line.avito_id for line in chosen], _Pacer(sleep))
    rows = []
    for line in chosen:
        ours = None if line.stock is None else min(line.stock // line.pack, cfg["cap"] or STOCK_MAX)
        rows.append({"code": line.code, "ad_id": line.ad_id, "item_id": line.avito_id, "ours": ours,
                     "answer": found.get(line.avito_id), "error": refused.get(line.avito_id, "")})
    return rows, ""


def _client_for(conn, box, account: dict, http, write: bool) -> Optional[AvitoClient]:
    client_id = connections.get_secret(conn, box, "avito", account["id"], "client_id")
    client_secret = connections.get_secret(conn, box, "avito", account["id"], "client_secret")
    if not client_id or not client_secret:
        return None
    return AvitoClient(client_id, client_secret, http=http, now=time.time, allow_write=write)


def after_cycle(conn, box, feed_root, parsed, results, http, now: Optional[datetime] = None,
                sleep: Callable[[float], None] = time.sleep, out=None, err=None) -> None:
    """After a cycle published its feeds: the numbers of every switched-on account whose feed this cycle published (not one stopped by
    the mass removal guard). An account switched off costs nothing, not a single request; any failure is a warning, never the cycle's."""
    if box is None or parsed is None:
        return
    now = now or moscow_now()
    out, err = out or sys.stdout, err or sys.stderr
    if conn.in_transaction:            # the cycle's own work is done: the rollback below takes back only this step's (push commits in batches,
        conn.commit()                  # so a savepoint would not hold)
    for result in results or []:
        try:
            if not result.ok or result.guard_tripped:
                continue
            account = connections.get_account(conn, result.account_id)
            if account is None or not account["live"]:
                continue
            cfg = config(conn, account["id"])
            if cfg["mode"] == "off":
                continue
            client = _client_for(conn, box, account, http, write=True)
            if client is None:
                continue
            p = plan(conn, account, parsed, feed_root, cfg, client=client, now=now)
            if p.note:
                print("%s: остатки на Авито не отправлены: %s" % (account["name"], p.note), file=out)
                continue
            counts = push(conn, client, account, p.lines, now, sleep=sleep)
            print("%s: остатки на Авито: %s" % (account["name"], ", ".join("%s %d" % (k, v) for k, v in counts.items())), file=out)
        except Exception as error:      # noqa: BLE001 - the numbers on Avito must never fail a cycle
            conn.rollback()
            print("предупреждение: остатки на Авито не отправлены (%s: %s)" % (type(error).__name__, str(error)[:120]), file=err)
