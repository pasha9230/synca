"""gunicorn access log without the tokens of client links («/s/<token>/…» is written as «/s/***/…»), of the webhooks, of the photos and
the feeds of an account («/img/<token>/…», «/feed/<token>/…»: the token opens the feed) and without the search query
(«?q=+7900…» is written as «?q=***»: phones and names)."""
from __future__ import annotations

import re

try:
    from gunicorn.glogging import Logger as _Logger
except ImportError:          # gunicorn is installed on the server only
    _Logger = object

_TOKEN = re.compile(r"(/(?:s|hook/(?:vk|avito))/)[^/\s?\"]+")
_ACCOUNT = re.compile(r"(/(?:img|feed)/)[0-9a-fA-F]{32}(?=[/\s?\"]|$)")        # the feed token of an account (never «/feed/update»)
# «q» after «?» or «&» (an address, the referer) or at the start (the atom of the query string); «%71» is «q» to Werkzeug
_QUERY = re.compile(r"((?:^|[?&])(?:q|(?i:%71))=)[^&\s\"#]*")


def mask(text: str) -> str:
    return _QUERY.sub(r"\1***", _ACCOUNT.sub(r"\1***", _TOKEN.sub(r"\1***", text)))


class Logger(_Logger):
    def atoms(self, resp, req, environ, request_time):
        atoms = super().atoms(resp, req, environ, request_time)
        return {key: mask(value) if isinstance(value, str) else value for key, value in atoms.items()}
