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
NOTE = "staff_note"                # audit action: a news of an order that went to Telegram before 2026-11-06, now the person's bell only
BELLS = {BELL: ("critical", "Критичное", "critical_seen:"),         # action -> (the prefix of the row id, its title, its app_state mark of
         NOTE: ("staffnote", "Оповещение", "staff_note_seen:")}    # «Разобрался»: "<mark><login>" -> the last id pressed on)
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
            key = _mark("bell:" + ",".join(people), text)              # the same news to the same people within VK_REPEAT: one row
            if not (store.get_state(conn, key) and store.get_state(conn, key) > _border(now, VK_REPEAT)):
                store.write_audit(conn, now[:19], "system", BELL, {"text": text[:4000], "to": people})
                store.set_state(conn, key, now[:19])
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


def note(conn, logins: Iterable[str], text: str, now: str) -> bool:
    """A news that is not critical but went to a person's Telegram before (an order's alert, «точка не открыла»): a red row of their own bell
    until «Разобрался», never in the journal of the order (a point may read that). Never raises; False when it could not be written."""
    people = sorted({str(x) for x in logins if x})
    if not people or not str(text or "").strip():
        return False
    try:
        store.write_audit(conn, now[:19], "system", NOTE, {"text": text[:4000], "to": people})
        return True
    except Exception as err:                                      # noqa: BLE001 - a busy base: printed, never a new alert
        _rollback(conn)
        print("оповещение: колокольчик не записан (%s)" % type(err).__name__, file=sys.stderr)
        return False


NOTE_SHOWN = 20                    # the bell shows this many own notes one by one (the rest: «more» on the last one)
NOTE_ACKED = "staff_note_acked:"   # app_state: "<NOTE_ACKED><login>" -> JSON list of the notes this person pressed «Разобрался» on


def _acked_notes(conn, login: str) -> List[int]:
    try:
        return [int(x) for x in json.loads(store.get_state(conn, NOTE_ACKED + login) or "[]")]
    except (ValueError, TypeError):
        return []


def bell_rows(conn, login: str, now: Optional[str] = None, action: str = BELL) -> List[dict]:
    """The red rows of the bell of this kind (BELLS), younger than BELL_DAYS with `now`. «Критичное»: one row, the newest not yet «Разобрался»,
    the older ones counted in it («more») and going with it. «Оповещение» (an order's alert that a person once read in Telegram one by one):
    a row each, the NOTE_SHOWN newest, each until its own «Разобрался»."""
    prefix, title, mark = BELLS[action]
    seen = int(store.get_state(conn, mark + login) or 0)
    acked = set(_acked_notes(conn, login)) if action == NOTE else set()
    border = _border(now, timedelta(days=BELL_DAYS)) if now else ""
    found = []
    for row_id, at, detail in conn.execute("SELECT id, at, detail FROM audit WHERE action = ? AND id > ? ORDER BY id DESC", (action, seen)):
        if row_id in acked:
            continue
        try:
            data = json.loads(detail or "{}")
        except ValueError:
            continue
        if login in (data.get("to") or []) and (at or "") >= border:
            found.append((row_id, at, str(data.get("text") or "")))
    shown = found[:NOTE_SHOWN] if action == NOTE else found[:1]
    rows = [{"id": "%s-%d" % (prefix, row_id), "order_id": None, "chat_row": None, "client": "", "at": at, "author": "", "kind": prefix,
             "level": "red", "more": 0, "title": title, "text": text[:300], "href": "/alerts", "ack_href": "/alerts/critical/%d/ack" % row_id}
            for row_id, at, text in shown]
    if rows:
        rows[-1]["more"] = len(found) - len(shown)
    return rows


def ack(conn, row_id: int, login: str) -> None:
    """«Разобрался»: «Критичное» — this news and every older one leave this person's bell; «Оповещение» — this one only. No commit of its own."""
    action = action_of(conn, row_id)
    if action is None:
        return
    if action == NOTE:
        kept = [x for x in _acked_notes(conn, login) if x != int(row_id)][-499:] + [int(row_id)]     # the newest 500: never pile up
        conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (NOTE_ACKED + login, json.dumps(kept)))
        return
    mark = BELLS[action][2] + login
    if int(row_id) > int(store.get_state(conn, mark) or 0):
        conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (mark, str(int(row_id))))


def names_login(conn, login: str) -> bool:
    """Is this login among the people of a bell row (users.login_taken: a new person of the name must not get them)."""
    for (detail,) in conn.execute("SELECT detail FROM audit WHERE action IN (%s) AND detail LIKE ?" % ",".join("?" * len(BELLS)),
                                  list(BELLS) + ['%' + json.dumps(login, ensure_ascii=False) + '%']):
        try:
            if login in (json.loads(detail or "{}").get("to") or []):
                return True
        except ValueError:
            continue
    return False


def rename_login(conn, old: str, new: str) -> None:
    """Inside the caller's transaction (users.rename_login): the person's rows of the bell and their «Разобрался» marks follow the new login."""
    for mark in [m for _p, _t, m in BELLS.values()] + [NOTE_ACKED]:
        conn.execute("DELETE FROM app_state WHERE key = ?", (mark + new,))
        conn.execute("UPDATE app_state SET key = ? WHERE key = ?", (mark + new, mark + old))
    for row_id, detail in conn.execute("SELECT id, detail FROM audit WHERE action IN (%s) AND detail LIKE ?" % ",".join("?" * len(BELLS)),
                                       list(BELLS) + ['%' + json.dumps(old, ensure_ascii=False) + '%']).fetchall():
        try:
            data = json.loads(detail or "{}")
        except ValueError:
            continue
        if old in (data.get("to") or []):
            data["to"] = sorted({new if x == old else x for x in data["to"]})
            conn.execute("UPDATE audit SET detail = ? WHERE id = ?", (json.dumps(data, ensure_ascii=False), row_id))


def action_of(conn, row_id: int) -> Optional[str]:
    """The kind of the bell (BELLS) of this record of the journal, or None when it is no row of a bell."""
    row = conn.execute("SELECT action FROM audit WHERE id = ?", (int(row_id),)).fetchone()
    return row[0] if row and row[0] in BELLS else None


def _critical_red(kind: str, text: str) -> bool:
    """A red record that is critical: every red failure of a rule; of the chat records only a send to a client that failed (not «Позиция не
    определена», not a message to a point)."""
    text = str(text or "")
    low = text.lower()
    return kind == "rule" or ("клиенту" in low and not low.startswith(("позиция не", "сообщение точке", "точке")))


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
    rows = [r for r in conn.execute("SELECT id, order_id, kind, text FROM order_event WHERE id > ? AND id <= ? AND level = 'red' "
                                    "AND kind IN ('chat', 'rule') ORDER BY id", (int(upto), top)) if _critical_red(r[2], r[3])]
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
