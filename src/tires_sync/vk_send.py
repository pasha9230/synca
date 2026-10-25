"""A message to a person through the VK community (the managers of the points): messages.send, written in the name of the community.
VK lets the community write only to people who wrote to it first or allowed its messages; a manager bound to a point has done that."""
from __future__ import annotations

import hashlib
import json
import random
import urllib.parse
from typing import Callable, Optional

from . import connections
from .secretbox import SecretBox, SecretKeyError
from .transport import Transport, urllib_transport

URL = "https://api.vk.com/method/messages.send"
API_VERSION = "5.131"
VK_LIMIT = 4000


REFUSED = "ВК не принял сообщение"    # VK answered with an error (912: the keyboard needs «Возможности ботов» on in the community)


class VkError(Exception):
    pass


def stable_id(seed: str) -> int:
    """A random_id of VK made from what the message is (the order, the person, the text…): the same seed is the same message to VK, so a
    repeat of one sending is dropped, while another message gets another id (1…2³¹−1, as order_engine.vk_random_id)."""
    return int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) % (2 ** 31 - 2) + 1


def send_message(token: str, peer_id, text: str, http: Transport = urllib_transport, random_id: Optional[int] = None,
                 keyboard: Optional[str] = None) -> int:
    """Send `text` to the person `peer_id`; returns the id of the message. The key travels in the body, never in the address or in an error.
    `keyboard`: the bot's buttons (JSON); VK refuses it with code 912 while the community has «Возможности ботов» off."""
    token, peer, text = (token or "").strip(), str(peer_id or "").strip(), (text or "").strip()
    if not token:
        raise VkError("ВК не настроен: нет ключа доступа сообщества.")
    if not (peer.isascii() and peer.isdigit() and peer != "0"):
        raise VkError("ВК: у менеджера не привязан идентификатор (нужен номер из цифр).")
    if not text:
        raise VkError("ВК: пустое сообщение.")
    fields = {"access_token": token, "v": API_VERSION, "peer_id": peer, "message": text[:VK_LIMIT],
              "random_id": random_id if random_id is not None else random.randint(1, 2 ** 31 - 1)}
    if keyboard:
        fields["keyboard"] = keyboard
    body = urllib.parse.urlencode(fields).encode("utf-8")
    try:
        status, _, raw = http("POST", URL, {"Content-Type": "application/x-www-form-urlencoded"}, body)
    except OSError:
        raise VkError("ВК недоступен: нет связи с сервером.") from None
    if status != 200:
        raise VkError("ВК отклонил запрос (код %s)." % status)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise VkError("ВК вернул непонятный ответ.") from None
    if not isinstance(data, dict):
        raise VkError("ВК вернул непонятный ответ.")
    if "error" in data:
        error = data["error"] if isinstance(data["error"], dict) else {}
        raise VkError(REFUSED + " (код %s): %s" % (str(error.get("error_code", "?"))[:6], str(error.get("error_msg", ""))[:120]))
    if not isinstance(data.get("response"), int):
        raise VkError("ВК вернул непонятный ответ.")
    return data["response"]


def sender_for(conn, box: Optional[SecretBox], http: Transport = urllib_transport) -> Optional[Callable[[str, str], int]]:
    """A function (peer_id, text, random_id=None, keyboard=None) that writes through the community, or None when the key of the community is not saved or
    cannot be read. The same `random_id` again is the same message to VK: a repeat after a cut-off answer is not delivered twice."""
    if box is None:
        return None
    try:
        token = connections.get_secret(conn, box, "vk", 0, "token")
    except SecretKeyError:
        return None
    if not token:
        return None
    return lambda peer_id, text, random_id=None, keyboard=None: send_message(token, peer_id, text, http=http, random_id=random_id, keyboard=keyboard)
