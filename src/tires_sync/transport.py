from __future__ import annotations

import http.client
import urllib.error
import urllib.request
from typing import Callable, Dict, Optional, Tuple

Transport = Callable[[str, str, Dict[str, str], Optional[bytes]], Tuple[int, Dict[str, str], bytes]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ResponseTooLarge(OSError):
    """The server answered more than MAX_BODY: it is alive, the answer is refused (not a network failure, not repeated)."""


MAX_BODY = 200 * 1024 * 1024


def _web_only_opener() -> urllib.request.OpenerDirector:
    """build_opener without file:, ftp: and data: — a stored address never reads a file of the server; anything else is «unknown url type»."""
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler(), urllib.request.UnknownHandler(), urllib.request.HTTPHandler(),
                    urllib.request.HTTPSHandler(), urllib.request.HTTPDefaultErrorHandler(), _NoRedirect(),
                    urllib.request.HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener


_OPENER = _web_only_opener()


def urllib_transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes]) -> Tuple[int, Dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _OPENER.open(request, timeout=30) as response:
            body = response.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                raise ResponseTooLarge("ответ сервера слишком большой")
            return response.status, {k.lower(): v for k, v in response.headers.items()}, body
    except urllib.error.HTTPError as err:
        try:
            return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read(MAX_BODY)
        except http.client.HTTPException as cut:
            raise OSError("обрыв ответа сервера (%s)" % type(cut).__name__) from None
    except http.client.HTTPException as cut:                  # a cut answer (IncompleteRead, a broken status line) is a network trouble like a timeout
        raise OSError("обрыв ответа сервера (%s)" % type(cut).__name__) from None      # the kind of the failure stays in the journal, its text does not
