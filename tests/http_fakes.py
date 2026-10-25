from __future__ import annotations

import json


class ScriptedHttp:
    """Transport that replays scripted replies in order and records every call."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        reply = self.replies.pop(0)
        status, payload = reply[0], reply[1]
        extra = reply[2] if len(reply) > 2 else {}
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload).encode()
        elif isinstance(payload, str):
            data = payload.encode()
        else:
            data = payload
        return status, {k.lower(): v for k, v in extra.items()}, data
