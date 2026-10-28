"""Receiver of the VK community's Callback API (the managers' answers): confirms the address, records the events, and gives a new private
message of a point's manager to the bot (order_manager.dispatch); the bot's answer goes back to them through the community.
Public by necessity: guarded by a secret path, the community number and VK's secret key."""
from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
from datetime import datetime, timedelta
from typing import Callable, Optional, Tuple

from . import connections, order_manager, order_notify, order_points as points, store, vk_send
from .transport import Transport, urllib_transport

PATH_KEY = "vk_hook_token"
KEEP_DAYS = 30
MAX_PAYLOAD = 4000
CONFIRMATION = re.compile(r"^[A-Za-z0-9]{1,32}$")
SECRET = re.compile(r"^[A-Za-z0-9_\-]{1,50}$")


def path_token(conn) -> str:
    """The secret part of the address VK is given; created once."""
    token = store.get_state(conn, PATH_KEY)
    if not token:
        token = secrets.token_urlsafe(18)
        store.set_state(conn, PATH_KEY, token)
    return token


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def recent(conn, limit: int = 10) -> list:
    return [{"at": r[0], "type": r[1]} for r in conn.execute("SELECT at, type FROM vk_event ORDER BY id DESC LIMIT ?", (limit,))]


def _message(data: dict, group: str) -> Optional[dict]:
    """The private message of a person to the community in a `message_new` event, or None (a chat, the community itself, garbage)."""
    obj = data.get("object") if isinstance(data.get("object"), dict) else {}
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj           # API 5.103+ wraps it; older versions do not
    sender, peer = str(msg.get("from_id", "")), str(msg.get("peer_id", msg.get("from_id", "")))
    if not (sender.isascii() and sender.isdigit() and sender != "0" and peer == sender):       # negative: a community; another peer: a chat
        return None
    reply = msg.get("reply_message") if isinstance(msg.get("reply_message"), dict) else {}
    reply_no = order_manager.line_number(reply.get("text")) if str(reply.get("from_id", "")) == "-" + group else None    # a line of the community «Заказ …»
    text, voice, transcript, link = str(msg.get("text") or ""), "", "", ""
    for attachment in msg.get("attachments") if isinstance(msg.get("attachments"), list) else []:
        audio = attachment.get("audio_message") if isinstance(attachment, dict) and attachment.get("type") == "audio_message" else None
        if isinstance(audio, dict):                                   # a voice message: VK's own transcript when it has one
            transcript = str(audio.get("transcript") or "").strip()
            done = transcript and audio.get("transcript_state", "done") == "done" and not audio.get("transcript_error")
            voice = "done" if done else "pending"
            transcript = transcript if done else ""
            link = str(audio.get("link_mp3") or audio.get("link_ogg") or "")    # the record, for «Входящие» when there is no transcript
    return {"id": str(msg.get("id") or msg.get("conversation_message_id") or ""), "from": sender, "text": text, "reply_no": reply_no, "voice": voice,
            "transcript": transcript if voice == "done" else "", "voice_link": link}


def claim(conn, message_id: str, peer: str, source: str, now: datetime) -> bool:
    """The bot takes the message `message_id` («vk-<id of VK>»): True only the first time, whichever way it came (Callback, the backstop of
    vk_catchup), so a message is acted on once. Taken in one statement: of two processes, one acts."""
    taken = conn.execute("INSERT OR IGNORE INTO vk_message (message_id, peer, at, source) VALUES (?,?,?,?)",
                         (str(message_id), str(peer), now.isoformat(timespec="seconds"), source)).rowcount == 1
    conn.execute("DELETE FROM vk_message WHERE at < ?", ((now - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds"),))
    conn.commit()
    return taken


def _act(conn, data: dict, group: str, event_id: str, now: datetime, channel: Callable, box=None, tg_http: Transport = urllib_transport,
         source: str = "callback") -> bool:
    """A message of a manager goes to the bot; its answer goes back (signed «(бот) »). A number the panel does not know is told to the
    partner in Telegram (`box`: the keys). Never raises: the event is recorded whatever happens here. True: the message was the bot's to act
    on (taken now, not before by another way); `source`: which way it came."""
    msg = _message(data, group)
    if msg is None:
        return False
    local = (now + timedelta(hours=3)).isoformat(timespec="seconds")                   # the journal of the orders keeps panel time (Moscow)
    log = logging.getLogger("tires_sync.vk")
    taken = False
    try:
        taken = claim(conn, "vk-" + (msg["id"] or event_id), msg["from"], source, now)
        if not taken:                                                                 # came the other way already (vk_catchup)
            return False
        send, deliver, notify = channel(conn, local)
        binding = bool(order_manager.BIND_CODE.match(str(msg["text"] or "").strip().upper())) and order_manager._point_of(conn, msg["from"]) is None
        answer = order_manager.dispatch(conn, "vk-" + (msg["id"] or event_id), msg["from"], msg["text"], msg["reply_no"], local, deliver, notify,
                                        voice=msg["voice"], transcript=msg["transcript"], voice_link=msg["voice_link"])
    except Exception:
        conn.rollback()
        log.exception("the message of VK event %s could not be handled", event_id)
        return taken
    try:
        panel = points.panel_point_of(conn, msg["from"]) if answer else None
        if panel is not None:                                                         # the owner 2026-10-23: the command is done, the answer
            if answer != order_manager.HINT:                                          # is on «Моя точка», never in VK (the hint of the VK
                points.add_note(conn, panel["id"], "answer", answer, local, asked=msg["text"] or msg["transcript"])     # keyboard: not at all)
            if not binding:                                                           # the answer to the binding code: in VK too (the lead
                answer = ""                                                           # 2026-10-23), the manager waits for it there
    except Exception:
        conn.rollback()
        log.exception("the answer to VK event %s could not be kept for the point in the panel", event_id)
        answer = answer if binding else ""                                            # a point in the panel hears only its binding in VK
    message_id = "vk-" + (msg["id"] or event_id)
    if answer and send is None:
        log.warning("the answer to VK event %s did not go: the key of the community is not saved or cannot be read", event_id)
        _not_answered(conn, box, now, msg["from"], answer, NO_KEY)          # no key: the hint keeps its pause (no try per message)
    if answer and send is not None:
        try:
            _answer(conn, send, msg["from"], answer, message_id)
        except Exception as err:                                                      # the bot's answer did not reach the point
            log.warning("the answer to VK event %s did not go: %s", event_id, err)
            _not_answered(conn, box, now, msg["from"], answer, _reason(err), str(err))
            _hint_back(conn, msg["from"], answer, local)
    _tell_missing(conn, box, message_id, tg_http, event_id, local)                   # after the answer: Telegram must not keep the point waiting
    try:                                                                              # the handed-over clients: 15 minutes (nothing to the points)
        order_manager.handoff_tick(conn, local, notify=notify)
    except Exception:
        conn.rollback()
        log.exception("the look at the handed-over clients failed after VK event %s", event_id)
    return True


NO_KEY = "ключ сообщества не сохранён или не читается (Подключения → ВК)"
NOT_ANSWERED = "Tires Sync: бот ВК не может ответить точкам: %s. Кому не ушло — в журнале действий."


def _reason(err: Exception) -> str:
    """Why the answer did not go, without the answer and VK's own words: the same failure is the same alert (cli._alert keeps it 6 hours)."""
    if not isinstance(err, vk_send.VkError):
        return "сбой панели (%s)" % type(err).__name__
    text = str(err)
    return text.split("):", 1)[0] + ")" if text.startswith(vk_send.REFUSED) and "):" in text else text.rstrip(".")


def _hint_back(conn, peer: str, answer: str, at: str) -> None:
    """VK did not take the hint: its pause of 30 minutes is given back (order_manager.hint_failed). Never raises."""
    if answer != order_manager.HINT:
        return
    try:
        order_manager.hint_failed(conn, peer, at)
    except Exception:
        conn.rollback()
        logging.getLogger("tires_sync.vk").exception("the pause of the hint to %s could not be given back", peer)


def _alarm(conn, box, text: str) -> None:
    from . import cli                                                                 # the alerts of the owner live with the commands
    cli._alert(conn, box, text)


def _not_answered(conn, box, now: datetime, peer: str, answer: str, reason: str, error: str = "") -> None:
    """The bot's answer did not reach the point: whom and what goes to the journal of the actions, the owner gets one alert of the same
    words for the same failure. Never raises: the event is handled whatever happens here."""
    try:
        point = order_manager._point_of(conn, peer)
        store.write_audit(conn, now.isoformat(timespec="seconds"), "system", "vk_answer_failed",
                          {"point": point["name"] if point else "", "peer": str(peer), "reason": reason, "error": error[:300], "answer": answer[:1000]})
        if point is not None:                                                         # a stranger who blocked the community is no alarm
            _alarm(conn, box, NOT_ANSWERED % reason)
    except Exception:
        conn.rollback()
        logging.getLogger("tires_sync.vk").exception("the owner could not be told that the bot's answer to %s did not go", peer)


def _tell_missing(conn, box, message_id: str, tg_http: Transport, event_id: str, now: str = "") -> None:
    """The point wrote a number the panel does not know: the partner (order_notify.missing_order_people) hears it in Telegram once; how it went
    is added to the outcome of the command («Входящие»). The bell shows it by itself (order_alerts.page_rows). Never raises."""
    try:
        found = order_manager.missing_orders(conn, message_id)
        told = conn.execute("SELECT 1 FROM order_command_log WHERE message_id = ? AND outcome LIKE '%; Telegram%'", (message_id,)).fetchone()
        if not found or told:                                                     # the same message once more (another event of VK): told already
            return
        how = order_notify.tell_people(conn, box, order_notify.missing_order_people(conn), found[0]["text"] + ".", tg_http, now=now)
        conn.execute("UPDATE order_command_log SET outcome = outcome || ? WHERE message_id = ?", ("; " + how, message_id))
        conn.commit()
    except Exception:
        conn.rollback()
        logging.getLogger("tires_sync.vk").exception("the partner could not be told of the unknown order of VK event %s", event_id)


def _answer(conn, send: Callable, peer: str, text: str, message_id: str = "") -> None:
    """The bot's answer, signed «(бот) » (the owner 2026-10-24); to a point with the button «Передать под заказ». While VK refuses the keyboard
    (the community has «Возможности ботов» off, 912) or refuses the message for any other reason, the same answer goes once more without it:
    a point is never left without its answer. The random_id comes from the point's message and the answer: a repeat of the same answer to
    the same message is dropped by VK; the try without the keyboard has an id of its own (VK might keep the id of the refused one)."""
    text = order_manager.signed(text)
    random_id = vk_send.stable_id("answer|%s|%s|%s" % (message_id, peer, text)) if message_id else None
    plain_id = vk_send.stable_id("answer|%s|%s|%s|plain" % (message_id, peer, text)) if message_id else None
    if order_manager._point_of(conn, peer) is None:
        send(peer, text, random_id=random_id)
        return
    try:
        send(peer, text, random_id=random_id, keyboard=order_manager.KEYBOARD)
    except vk_send.VkError as err:
        if not str(err).startswith(vk_send.REFUSED):              # no answer at all from VK: the same would fail again
            raise
        send(peer, text, random_id=plain_id)                      # 912 (no «Возможности ботов»), a bad keyboard: the answer goes without it


LOST_AFTER = timedelta(minutes=2)                 # the work on a message takes well under a minute and a half (two calls of 30 s at worst)
STUCK_AFTER = timedelta(minutes=5)
SINCE_KEY = "vk_dispatch_since"                   # the moment the bot began to act on the messages (set by the first sweep)


def started(conn, now: datetime) -> str:
    """The moment the bot began to act on the messages: the first message it took (or the first look at «Входящие»). What came before was
    handled by people and is never shown as lost."""
    since = store.get_state(conn, SINCE_KEY)
    if not since:
        since = now.isoformat(timespec="seconds")
        store.set_state(conn, SINCE_KEY, since)
    return since


def safe_sweep(conn, now: datetime) -> int:
    """sweep that never raises (a busy base, a broken event): it is a look at the past, never a reason to fail a request."""
    try:
        return sweep(conn, now)
    except Exception:
        conn.rollback()
        logging.getLogger("tires_sync.vk").exception("the check of the VK messages left unhandled failed")
        return 0


def sweep(conn, now: datetime) -> int:
    """The work after «ok» runs in the panel's process: a restart (every deploy) can cut it. What was cut is shown in «Входящие» once:
    a message of a point that left no trace, a command that never ended. Nothing is redone by itself: a late «можно забирать» is the
    owner's call. Returns how many lines were made."""
    local = now + timedelta(hours=3)
    since = started(conn, now)
    made = order_manager.note_stuck(conn, local.isoformat(timespec="seconds"), (local - STUCK_AFTER).isoformat(timespec="seconds"))
    rows = conn.execute("SELECT event_id, at, group_id, payload FROM vk_event WHERE type = 'message_new' AND at < ? AND at > ? AND at >= ? ORDER BY id",
                        ((now - LOST_AFTER).isoformat(timespec="seconds"), (now - timedelta(days=1)).isoformat(timespec="seconds"), since)).fetchall()
    for event_id, at, group, payload in rows:
        try:
            msg = _message(json.loads(payload), group)
        except ValueError:
            continue
        if msg is None or not (msg["text"].strip() or msg["voice"]) or order_manager._point_of(conn, msg["from"]) is None:    # a photo; a stranger
            continue
        message_id = "vk-" + (msg["id"] or event_id)
        if not order_manager.is_handled(conn, message_id):
            made += order_manager.note_lost(conn, message_id, msg["from"], (msg["text"] if msg["voice"] != "pending" else order_manager.voice_line(msg["text"], msg["voice_link"])) or ("(голосовое) %s" % msg["transcript"] if msg["transcript"] else "(голосовое сообщение)"),
                                            (datetime.fromisoformat(at) + timedelta(hours=3)).isoformat(timespec="seconds"), local.isoformat(timespec="seconds"))
    return made


def handle(conn, box, token: str, body: bytes, now: datetime, channel: Optional[Callable] = None,
           run: Optional[Callable[[Callable], None]] = None, tg_http: Transport = urllib_transport) -> Tuple[int, str]:
    """(HTTP status, plain text). 404 for a wrong address, 403 for a wrong community or secret, 400 for garbage, otherwise 200.
    `channel(conn, local time)` gives (send to a manager, deliver to a client, alert about an order). The work on a message is given to
    `run(job)`, job(conn): the panel runs it after «ok» has gone (sending to Avito and VK takes longer than VK waits); without `run` it is
    done here on `conn`. The event is recorded first, so a repeat of VK never acts twice."""
    if not _same(token or "", path_token(conn)):
        return 404, ""
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeError):
        return 400, ""
    if not isinstance(data, dict):
        return 400, ""
    config = connections.get_config(conn, "vk", 0)
    group = str(config.get("group_id") or "")
    if not group or not _same(str(data.get("group_id", "")), group):
        return 403, ""
    kind = str(data.get("type") or "")
    if kind == "confirmation":
        return 200, str(config.get("confirmation") or "")
    expected = connections.get_secret(conn, box, "vk", 0, "callback_secret") or ""
    if not expected or not _same(str(data.get("secret", "")), expected):
        return 403, ""
    data.pop("secret", None)                                              # the key is never written to the log
    event_id = str(data.get("event_id") or "") or secrets.token_hex(8)
    fresh = conn.execute("INSERT OR IGNORE INTO vk_event (event_id, at, type, group_id, payload) VALUES (?,?,?,?,?)",
                         (event_id, now.isoformat(timespec="seconds"), kind[:60], group, json.dumps(data, ensure_ascii=False)[:MAX_PAYLOAD])).rowcount == 1
    conn.execute("DELETE FROM vk_event WHERE at < ?", ((now - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds"),))
    conn.commit()
    if fresh and kind == "message_new" and channel is not None:                       # VK repeats an event it thinks lost: acted on once
        try:
            started(conn, now)                                                        # the event itself is recorded: it is acted on anyway
        except Exception:
            conn.rollback()
            logging.getLogger("tires_sync.vk").exception("the start of the VK bot could not be marked")

        def job(db):
            _act(db, data, group, event_id, now, channel, box, tg_http)
            safe_sweep(db, now)                                                       # after «ok»: a busy base must not delay VK
        (run or (lambda work: work(conn)))(job)
    return 200, "ok"
