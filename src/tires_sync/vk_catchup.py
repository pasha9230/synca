"""The backstop of VK's Callback (the owner 2026-10-28, order 450: «Приехали» of Янино never reached /hook/vk/…, VK did not call). Every 10–15
minutes (`vk-catchup`, a timer of its own) the history of each point's conversation is read with the community's key, and a message of the
point that never came by Callback is acted on exactly as if it had (vk_callback._act: the same commands, answers and journal). A message is
acted on once whichever way it came (vk_callback.claim); nothing older than a day is ever answered; the owner hears of it once a day."""
from __future__ import annotations

import json
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from . import chats, connections, order_manager, order_notify, store, vk_callback, vk_send
from .avito import AvitoClient
from .secretbox import SecretKeyError
from .transport import Transport, urllib_transport

HISTORY_URL = "https://api.vk.com/method/messages.getHistory"
LOOK_BACK = timedelta(hours=24)                   # the first run, a mark lost: never further (old history is never answered)
SETTLE = timedelta(minutes=3)                     # a younger message is left to Callback: it may still be on its way
PAGE = 200                                        # the most VK gives at once
MAX_PAGES = 5
CURSOR_KEY = "vk_catchup_last_%s"                 # app_state per peer: the id of the last message looked at
DAY_KEY = "vk_catchup_told"                       # app_state: the day (UTC) the owner was told
DOWN_KEY = "vk_catchup_down_told"                 # app_state: the day (UTC) the owner was told the backstop itself does not work
SOURCE = "catchup"
JOURNAL_LINE = "ВК: сообщение подобрано без уведомления"
MISSED = ("Tires Sync: ВК не прислал боту уведомление о сообщениях точек (%d), бот подобрал их сам из переписки. "
          "Какие — в журнале действий: «%s».")
DOWN = "Tires Sync: подборщик ВК не работает: %s. Сообщения точек, о которых ВК не уведомил, не подбираются."
BOT = "бот ВК"


def channel(conn, local: str, box, vk_http: Transport = urllib_transport, avito_http: Transport = urllib_transport,
            tg_http: Transport = urllib_transport):
    """What the bot needs to act on a manager's message: (send to a manager in VK, deliver to a client in Avito, alert about an order).
    Each is built from the saved keys; a missing key is a refusal at the moment of use. `local`: the panel time of the message (Moscow).
    The panel (routes_vk.channel) and the command `vk-catchup` use the same one."""
    sender = vk_send.sender_for(conn, box, http=vk_http)

    def deliver(chat_row, text, token):
        chat = chats.get_chat(conn, chat_row)
        if box is None or chat is None:
            raise chats.ChatError("Мастер-ключ недоступен: ключи Авито не расшифровать.")
        client_id = connections.get_secret(conn, box, "avito", chat["account_id"], "client_id")
        client_secret = connections.get_secret(conn, box, "avito", chat["account_id"], "client_secret")
        if not client_id or not client_secret:
            raise chats.ChatError("У аккаунта не заданы ключи Авито.")
        client = AvitoClient(client_id, client_secret, http=avito_http, allow_write=True)
        chats.send_reply(conn, client, chat_row, text, token, local, BOT)

    def notify(card, text):
        return order_notify.deliver(conn, box, card, text, local, tg_http=tg_http, vk_http=vk_http)
    return sender, deliver, notify


def history_for(token: str, group: str, http: Transport = urllib_transport) -> Callable[[str, int, int], list]:
    """messages.getHistory of the community: (peer, offset, count) → the messages, the newest first. The key travels in the body, never in the
    address or in an error; a refusal of VK is vk_send.VkError."""
    def history(peer, offset, count):
        fields = {"access_token": token, "v": vk_send.API_VERSION, "peer_id": str(peer), "offset": int(offset), "count": int(count),
                  "group_id": str(group)}
        try:
            status, _, raw = http("POST", HISTORY_URL, {"Content-Type": "application/x-www-form-urlencoded"},
                                  urllib.parse.urlencode(fields).encode("utf-8"))
        except OSError:
            raise vk_send.VkError("ВК недоступен: нет связи с сервером.") from None
        if status != 200:
            raise vk_send.VkError("ВК отклонил запрос (код %s)." % status)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise vk_send.VkError("ВК вернул непонятный ответ.") from None
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            raise vk_send.VkError("ВК не дал переписку (код %s): %s" % (str(data["error"].get("error_code", "?"))[:6],
                                                                       str(data["error"].get("error_msg", ""))[:120]))
        items = data.get("response", {}).get("items") if isinstance(data, dict) and isinstance(data.get("response"), dict) else None
        if not isinstance(items, list):
            raise vk_send.VkError("ВК вернул непонятный ответ.")
        return [m for m in items if isinstance(m, dict)]
    return history


def _when(msg: dict) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(int(msg.get("date")), timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _id(msg: dict) -> int:
    try:
        return int(msg.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _came_by_callback(conn, message_id: str, raw_id: int, group: str, since: datetime) -> bool:
    """The bot already has this message from Callback: its mark (vk_callback.claim), a trace of the bot (a command, a question), or, for
    what came before the marks, an event of Callback with this message. Such a message is never redone here: a cut work is «Входящие»'s
    (vk_callback.sweep)."""
    if order_manager.is_handled(conn, message_id) or conn.execute("SELECT 1 FROM vk_message WHERE message_id = ?", (message_id,)).fetchone():
        return True
    rows = conn.execute("SELECT payload FROM vk_event WHERE type = 'message_new' AND at >= ? AND event_id NOT LIKE ?",
                        (since.isoformat(timespec="seconds"), SOURCE + "-%")).fetchall()
    for (payload,) in rows:
        try:
            msg = vk_callback._message(json.loads(payload), group)
        except ValueError:                                          # cut at MAX_PAYLOAD: the id is looked for in the text
            if ('"id": %d,' % raw_id) in payload:
                return True
            continue
        if msg is not None and msg["id"] == str(raw_id):
            return True
    return False


def _peers(conn) -> list:
    rows = conn.execute("SELECT DISTINCT vk_peer_id FROM point WHERE vk_peer_id != ''").fetchall()
    return [str(r[0]).strip() for r in rows if str(r[0]).strip().isascii() and str(r[0]).strip().isdigit() and str(r[0]).strip() != "0"]


def _new_messages(history: Callable, peer: str, after: int, floor: datetime) -> list:
    """The messages of the conversation after the id `after` and not older than `floor`, the oldest first."""
    found = []
    for page in range(MAX_PAGES):
        items = history(peer, page * PAGE, PAGE)
        found += [m for m in items if _id(m) > after and (_when(m) or floor - timedelta(seconds=1)) >= floor]
        oldest = items[-1] if items else None
        if len(items) < PAGE or oldest is None or _id(oldest) <= after or (_when(oldest) or floor) <= floor:
            break
    else:
        logging.getLogger("tires_sync.vk").warning("ВК: у %s больше %d новых сообщений, старшие не прочитаны", peer, MAX_PAGES * PAGE)
    return sorted({_id(m): m for m in found}.values(), key=_id)


def _is_points(msg: dict, peer: str) -> bool:
    """A message the point itself wrote: incoming, from the peer of the point in its own conversation. Ours (the bot's, an admin's in the
    name of the community) are outgoing with the community as the author."""
    return (not msg.get("out") and str(msg.get("from_id", "")) == peer and str(msg.get("peer_id", peer)) == peer
            and not msg.get("admin_author_id"))


def run(conn, box, now: datetime, channel: Callable, history: Optional[Callable] = None, tg_http: Transport = urllib_transport,
        vk_http: Transport = urllib_transport, mode: str = "act") -> dict:
    """One look at the conversations of the points. `now`: UTC, as the panel's; `channel(conn, local)` as vk_callback.handle's;
    `history(peer, offset, count)` (tests) — else messages.getHistory with the community's key. `mode`: "act"; "dry" lists in "would" what
    would be picked and changes nothing; "seed" marks the newest message of each conversation as looked at and acts on none (the first start,
    when the day before must not be redone). Returns {"set_up", "points", "picked", "errors", "would", "no_key"}. A failure of one point is a warning
    and its mark stays (the next run reads it again); a backstop that cannot work at all is told to the owner once a day."""
    log = logging.getLogger("tires_sync.vk")
    done = {"set_up": False, "points": 0, "picked": 0, "errors": 0, "would": [], "no_key": False}
    group = str(connections.get_config(conn, "vk", 0).get("group_id") or "")
    if not group:
        return done
    if history is None:
        try:
            token = connections.get_secret(conn, box, "vk", 0, "token") if box is not None else ""
        except SecretKeyError:
            token = ""
        if not token:
            done["no_key"] = True                                                     # set up, yet the key is not there or not readable
            if mode == "act":
                _tell_down(conn, box, now, "ключ сообщества не сохранён или не читается")
            return done
        history = history_for(token, group, vk_http)
    done["set_up"] = True
    floor, settle = now - LOOK_BACK, now - SETTLE
    for peer in _peers(conn):
        done["points"] += 1
        key = CURSOR_KEY % peer
        try:
            after = int(store.get_state(conn, key) or 0)
        except ValueError:
            after = 0
        try:
            messages = history(peer, 0, 1) if mode == "seed" else _new_messages(history, peer, after, floor)
        except Exception as err:                                                     # noqa: BLE001 — one point, never the others
            conn.rollback()
            done["errors"] += 1
            log.warning("ВК: переписка с %s не прочитана: %s", peer, err)
            continue
        if mode == "seed":
            newest = max([_id(m) for m in messages] + [after])
            if newest > after:
                store.set_state(conn, key, str(newest))
            continue
        for msg in messages:
            when, raw_id = _when(msg), _id(msg)
            if when is None or when > settle:                                         # Callback may still bring it: the next run
                break
            try:
                if mode == "dry":
                    parsed = _would(conn, msg, raw_id, peer, group, floor)
                    if parsed is not None:
                        done["would"].append({"peer": peer, "message_id": str(raw_id), "text": parsed["text"]})
                    continue
                if _take(conn, box, msg, raw_id, peer, group, now, floor, channel, tg_http):
                    done["picked"] += 1
            except Exception:                                                         # noqa: BLE001 — the mark stays before it
                conn.rollback()
                done["errors"] += 1
                log.exception("ВК: сообщение %s от %s не обработано подборщиком", raw_id, peer)
                break
            store.set_state(conn, key, str(raw_id))
    if mode == "act" and done["points"] and done["errors"] >= done["points"]:
        _tell_down(conn, box, now, "переписка ни одной точки не прочитана")
    if done["picked"]:
        _tell_owner(conn, box, now, done["picked"])
    return done


def _would(conn, msg: dict, raw_id: int, peer: str, group: str, floor: datetime) -> Optional[dict]:
    """The message as the bot would get it (vk_callback._message) when it is the point's own and the bot does not have it yet, else None."""
    if not raw_id or not _is_points(msg, peer):
        return None
    parsed = vk_callback._message({"type": "message_new", "group_id": group, "object": {"message": msg}}, group)
    if parsed is None or not (parsed["text"].strip() or parsed["voice"]):                # a photo, a sticker: nothing for the bot
        return None
    if _came_by_callback(conn, "vk-%d" % raw_id, raw_id, group, floor - timedelta(days=1)):
        return None
    return parsed


def _take(conn, box, msg: dict, raw_id: int, peer: str, group: str, now: datetime, floor: datetime, channel: Callable, tg_http: Transport) -> bool:
    """One message of the conversation: acted on when it is the point's own and the bot does not have it yet. True: picked up here.
    Raises when the message could not even be taken (a busy base): its mark stays, the next run tries again."""
    parsed = _would(conn, msg, raw_id, peer, group, floor)
    if parsed is None:
        return False
    message_id = "vk-%d" % raw_id
    data = {"type": "message_new", "group_id": int(group) if group.isdigit() else group, "source": SOURCE, "object": {"message": msg}}
    event_id = "%s-%d" % (SOURCE, raw_id)
    conn.execute("INSERT OR IGNORE INTO vk_event (event_id, at, type, group_id, payload) VALUES (?,?,?,?,?)",     # «Входящие» sees a cut work
                 (event_id, now.isoformat(timespec="seconds"), "message_new", group, json.dumps(dict(data, event_id=event_id), ensure_ascii=False)[:vk_callback.MAX_PAYLOAD]))
    conn.commit()
    if not vk_callback._act(conn, data, group, event_id, now, channel, box, tg_http, source=SOURCE):
        if not conn.execute("SELECT 1 FROM vk_message WHERE message_id = ?", (message_id,)).fetchone():
            raise RuntimeError("the message %s could not be taken" % message_id)                 # nobody has it: never step over it
        return False
    point = order_manager._point_of(conn, peer)
    store.write_audit(conn, now.isoformat(timespec="seconds"), "system", "vk_catchup",
                      {"text": JOURNAL_LINE, "point": point["name"] if point else "", "peer": peer, "message_id": str(raw_id),
                       "message": parsed["text"][:200]})
    logging.getLogger("tires_sync.vk").warning("%s: точка %s, сообщение %s", JOURNAL_LINE, peer, raw_id)
    return True


def _tell_down(conn, box, now: datetime, why: str) -> None:
    """The backstop cannot work (no key, VK refuses every conversation): the owner hears it once a day. Never raises."""
    day = now.date().isoformat()
    try:
        if store.get_state(conn, DOWN_KEY) == day:
            return
        from . import cli                                                             # the alerts of the owner live with the commands
        if cli._alert(conn, box, DOWN % why):
            store.set_state(conn, DOWN_KEY, day)
    except Exception:                                                                 # noqa: BLE001
        conn.rollback()
        logging.getLogger("tires_sync.vk").exception("the owner could not be told that the backstop of VK does not work")


def _tell_owner(conn, box, now: datetime, count: int) -> None:
    """Callback missed messages: the owner hears it once a day (the first find of the day, with how many this run picked up). Never raises."""
    day = now.date().isoformat()
    try:
        if store.get_state(conn, DAY_KEY) == day:
            return
        from . import cli                                                             # the alerts of the owner live with the commands
        if cli._alert(conn, box, MISSED % (count, JOURNAL_LINE), notice=True):
            store.set_state(conn, DAY_KEY, day)
    except Exception:                                                                 # noqa: BLE001
        conn.rollback()
        logging.getLogger("tires_sync.vk").exception("the owner could not be told that VK missed messages")
