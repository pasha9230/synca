"""«Критичное» (the owner 2026-11-06): the one place of what goes to Telegram and how the critical news reach people while Telegram is down.

Critical: a change of the purchase (purchase_alert), a failed send to a client, a stop of the publishing of a feed or a feed check with an
error, a failure of a deploy or of a service, a red failure of a rule. It goes to Telegram (as before) and, always in addition (VK_ALWAYS),
in VK to each of its people who has a VK page bound (the panel: «Подключения → Telegram», the same row as the chat) and to their bell.
Everything else goes to Telegram no more (TELEGRAM_ONLY_CRITICAL): it stays in the bell and in the journal. The messages to the points in VK
and to the clients are not news of this module and do not change.
Nothing here raises: a failed VK or bell is printed, never a new alert."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional

from . import connections, store
from .transport import Transport, urllib_transport

# ---- the settings: only here -------------------------------------------------------------------------------------------------------------
TELEGRAM_ONLY_CRITICAL = True      # True: only the critical news go to Telegram; False: everything, as before 2026-11-06
VK_ALWAYS = True                   # True: the critical news go to VK always, with Telegram; False: only when Telegram did not take them
VK_REPEAT = timedelta(hours=6)     # the same text to the same VK page within this time is not sent again (as cli.REPEAT_PAUSE_SECONDS)
# -------------------------------------------------------------------------------------------------------------------------------------------

KIND = "telegram_users"            # order_notify.KIND: the chats and the VK pages of the panel users live in one setting
VK_KEY = "vk_peers"                # {login: the number of the person's VK page}
VK_MARK = "critical-vk:"           # app_state: "<VK_MARK><hash of page and text>" -> when it went
BELL = "critical"                  # audit action: a critical news for the bell, {"text", "to": [logins]}
SEEN = "critical_seen:"            # app_state: "<SEEN><login>" -> the id of the last critical news this person pressed «Разобрался» on
BELL_DAYS = 7                      # older critical news are no longer rows of the bell (the journal keeps them)
RED_UPTO = "critical_red_upto"     # app_state: the last order_event already passed on by forward_red
RED_SHOWN = 10
VK_ID = re.compile(r"^\d{1,15}$")


def telegram_allowed(critical: bool) -> bool:
    """May this news go to Telegram: a critical one always; another only while TELEGRAM_ONLY_CRITICAL is off."""
    return bool(critical) or not TELEGRAM_ONLY_CRITICAL


def vk_peers(conn) -> Dict[str, str]:
    found = connections.get_config(conn, KIND, 0).get(VK_KEY)
    return {str(k): str(v) for k, v in found.items() if str(v or "").strip()} if isinstance(found, dict) else {}


def set_user_vk(conn, login, peer, now: str, author: str) -> None:
    """Save (or, when empty, remove) the VK page of a panel user: the number of the page (the community writes only to who wrote to it)."""
    from . import orders
    login, text = str(login or "").strip(), str(peer or "").strip()
    if not login or len(login) > 60:
        raise orders.OrderError("Логин пользователя: от 1 до 60 знаков.")
    if text and (not VK_ID.match(text) or text == "0"):
        raise orders.OrderError("ВК: номер страницы, только цифры.")
    config = connections.get_config(conn, KIND, 0)
    peers = dict(config.get(VK_KEY)) if isinstance(config.get(VK_KEY), dict) else {}
    if text:
        peers[login] = text
    else:
        peers.pop(login, None)
    connections.set_config(conn, KIND, 0, {**config, VK_KEY: peers}, now, author)


def without_vk(conn, logins: Iterable[str]) -> List[str]:
    """Who of these people has no VK page bound (the report of the panel)."""
    peers = vk_peers(conn)
    return sorted({x for x in logins if x and x not in peers})


def _mark(peer: str, text: str) -> str:
    return VK_MARK + hashlib.sha256(("%s|%s" % (peer, text)).encode("utf-8")).hexdigest()[:16]


def _border(now: str, span: timedelta) -> str:
    return (datetime.fromisoformat(now[:19]) - span).isoformat(timespec="seconds")


def tell(conn, box, logins: Iterable[str], text: str, now: str, vk_http: Transport = urllib_transport, telegram_ok: Optional[bool] = None,
         bell: bool = True) -> dict:
    """The critical `text` to these people beside Telegram: a row of their bell (`bell`) and VK to each who has a page bound (always, or only when
    Telegram did not take it: VK_ALWAYS). One page gets one text once within VK_REPEAT, even when two logins share it or the caller repeats.
    Never raises. Returns {"bell": bool, "vk": [logins sent], "vk_failed": [...], "no_vk": [...]}."""
    from . import vk_send
    people = sorted({str(x) for x in logins if x})
    out = {"bell": False, "vk": [], "vk_failed": [], "no_vk": []}
    if not people or not str(text or "").strip():
        return out
    if bell:
        try:
            store.write_audit(conn, now[:19], "system", BELL, {"text": text[:4000], "to": people})
            out["bell"] = True
        except Exception as err:                                  # noqa: BLE001 - a busy base: printed, never a new alert
            _rollback(conn)
            print("критичное: колокольчик не записан (%s)" % type(err).__name__, file=sys.stderr)
    if not (VK_ALWAYS or telegram_ok is False):
        return out
    try:
        peers = vk_peers(conn)
        sender = vk_send.sender_for(conn, box, http=vk_http)
        window = _border(now, VK_REPEAT)
        conn.execute("DELETE FROM app_state WHERE key LIKE ? AND value <= ?", (VK_MARK + "%", window))   # never pile up
        conn.commit()
        done: Dict[str, bool] = {}
        for login in people:
            peer = peers.get(login)
            if not peer:
                out["no_vk"].append(login)
                continue
            if peer in done:                                           # two logins, one page: one message
                out["vk" if done[peer] else "vk_failed"].append(login)
                continue
            key = _mark(peer, text)
            if store.get_state(conn, key) and store.get_state(conn, key) > window:
                done[peer] = True                                      # sent within VK_REPEAT: not twice
                out["vk"].append(login)
                continue
            if sender is None:
                done[peer] = False
                out["vk_failed"].append(login)
                continue
            try:
                sender(peer, text, random_id=vk_send.stable_id("critical|%s|%s|%s" % (peer, text, now[:13])))
                store.set_state(conn, key, now[:19])
                done[peer] = True
                out["vk"].append(login)
            except vk_send.VkError as err:
                done[peer] = False
                out["vk_failed"].append(login)
                print("критичное: ВК «%s» не принял сообщение: %s" % (login, err), file=sys.stderr)
    except Exception as err:                                      # noqa: BLE001 - whatever it is: printed, never a new alert
        _rollback(conn)
        print("критичное: ВК не отправлен (%s)" % type(err).__name__, file=sys.stderr)
    return out


def _rollback(conn) -> None:
    try:
        conn.rollback()
    except Exception:                                             # noqa: BLE001
        pass


def bell_rows(conn, login: str, now: Optional[str] = None) -> List[dict]:
    """The red row of the bell: the newest critical news of this person not yet «Разобрался» (younger than BELL_DAYS with `now`); the older
    ones are counted in it («more») and go with it."""
    seen = int(store.get_state(conn, SEEN + login) or 0)
    border = _border(now, timedelta(days=BELL_DAYS)) if now else ""
    found = []
    for row_id, at, detail in conn.execute("SELECT id, at, detail FROM audit WHERE action = ? AND id > ? ORDER BY id DESC", (BELL, seen)):
        try:
            data = json.loads(detail or "{}")
        except ValueError:
            continue
        if login in (data.get("to") or []) and (at or "") >= border:
            found.append((row_id, at, str(data.get("text") or "")))
    if not found:
        return []
    row_id, at, text = found[0]
    return [{"id": "critical-%d" % row_id, "order_id": None, "chat_row": None, "client": "", "at": at, "author": "", "kind": "critical",
             "level": "red", "more": len(found) - 1, "title": "Критичное", "text": text[:300], "href": "/alerts",
             "ack_href": "/alerts/critical/%d/ack" % row_id}]


def ack(conn, row_id: int, login: str) -> None:
    """«Разобрался»: this critical news and every older one leave this person's bell. No commit of its own."""
    if int(row_id) > int(store.get_state(conn, SEEN + login) or 0):
        conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (SEEN + login, str(int(row_id))))


def forward_red(conn, box, now: str, tg_http: Transport = urllib_transport, vk_http: Transport = urllib_transport) -> int:
    """The red failures of the orders that are critical (a send to a client that failed: a red «chat» record; a red failure of a rule: a red
    «rule» record), passed on once: one message per set of people to Telegram and VK (they are in the bell already as red records). Who: the
    one who answers for the order, both owners when nobody does (order_alerts.told_people); never of an order of a point's Avito account nor
    of one a point user answers for (the point has its bell). The first run only remembers where the journal is. The mark moves before the
    sending, from the value read (of two ticks, one sends): a failed sending is printed, not repeated. Returns how many records were passed."""
    from . import order_alerts, order_notify, order_points
    top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM order_event").fetchone()[0]
    upto = store.get_state(conn, RED_UPTO)
    if upto is None or upto == "":
        conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (RED_UPTO, str(top)))
        conn.commit()
        return 0
    if top <= int(upto):
        return 0
    if conn.execute("UPDATE app_state SET value = ? WHERE key = ? AND value = ?", (str(top), RED_UPTO, upto)).rowcount != 1:
        conn.rollback()
        return 0
    rows = conn.execute("SELECT id, order_id, kind, text FROM order_event WHERE id > ? AND id <= ? AND level = 'red' AND kind IN ('chat', 'rule') "
                        "ORDER BY id", (int(upto), top)).fetchall()
    conn.commit()
    groups: Dict[tuple, List[str]] = {}
    for _id, order_id, _kind, text in rows:
        if order_points.of_account(conn, order_id) is not None:
            continue
        people = tuple(order_alerts.told_people(conn, order_id, []))
        if people:
            groups.setdefault(people, []).append("№%s: %s" % (order_id, " ".join(str(text or "").split())[:200]))
    passed = 0
    for people, lines in sorted(groups.items()):
        body = "Tires Sync: сбой по заказам (%d):\n%s" % (len(lines), "\n".join(lines[:RED_SHOWN]))
        if len(lines) > RED_SHOWN:
            body += "\nи ещё %d — в «Уведомлениях»" % (len(lines) - RED_SHOWN)
        how = order_notify.tell_people(conn, box, list(people), body, tg_http=tg_http, now=now, critical=True)
        if "не отправлен" in how:
            print("критичное: сбой по заказам не отправлен в Telegram: %s" % how, file=sys.stderr)
        tell(conn, box, people, body, now, vk_http=vk_http, telegram_ok="отправлено" in how, bell=False)
        passed += len(lines)
    return passed
