"""What Avito says about our last upload, boiled down to reasons: «pay the advance», «a parameter is wrong», and so on.
Read only. The summary is kept in the database so that the home page can show it without a call to Avito."""
from __future__ import annotations

import html
import json
import re
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from . import store

KEY = "avito_report:%d"
EXAMPLES = 5
TAGS = re.compile(r"<[^>]+>")
PAYMENT = "пополнить аванс"


def clean(text: str) -> str:
    return " ".join(html.unescape(TAGS.sub("", text or "")).split())[:300]


def kind_of(text: str) -> str:
    return "payment" if PAYMENT in text.lower() else "error"


def summarize(items: List[dict]) -> Dict[str, object]:
    """Counts by status and the non-active ads grouped by the reason Avito gave. An ad without a reason (a closed one, for example) is only counted."""
    counts: Dict[str, int] = {}
    groups: "OrderedDict[str, dict]" = OrderedDict()
    for item in items:
        status = item.get("avito_status") or "unknown"
        counts[status] = counts.get(status, 0) + 1
        if status == "active":
            continue
        text = clean("; ".join(
            "%s: %s" % (m.get("title", ""), m.get("description", "")) if m.get("description") else str(m.get("title", ""))
            for m in item.get("messages") or []))
        if not text:
            continue
        group = groups.setdefault(text, {"text": text, "kind": kind_of(text), "count": 0, "examples": []})
        group["count"] += 1
        if len(group["examples"]) < EXAMPLES and item.get("ad_id"):
            group["examples"].append(str(item["ad_id"]))
    ordered = sorted(groups.values(), key=lambda g: (g["kind"] != "payment", -g["count"]))
    return {"counts": counts, "total": sum(counts.values()), "groups": ordered}


def collect(conn, client, account_id: int, at: str) -> Optional[dict]:
    """Ask Avito about the last successful upload (or the running one) and keep the summary. None when there is no upload yet."""
    last = client.last_successful_upload()
    current = client.current_upload()
    upload = last if last is not None else current
    if upload is None:
        return None
    kind = "last_successful" if last is not None else "current"
    summary = summarize(list(client.upload_items(kind)))
    summary.update(at=at, upload_id=upload.get("upload_id"), upload_status=upload.get("status"),
                   running=current.get("upload_id") if current and current.get("status") == "processing" else None,
                   last_started_at=last.get("started_at") if last is not None else None)      # when Avito last took our file (the manual ads of 2026-10-22)
    store.set_state(conn, KEY % account_id, json.dumps(summary, ensure_ascii=False))
    return summary


def started_moscow(text) -> Optional[datetime]:
    """The start of an upload as Moscow time without a zone (the clock of our cycle). A time without a zone is read as Moscow time: earlier than
    reading it as UTC (Avito gives «Z»), so a manual ad is rather recognised late than too soon. Unreadable: None."""
    try:
        when = datetime.fromisoformat(str(text or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        return when
    return when.astimezone(timezone.utc).replace(tzinfo=None) + timedelta(hours=3)


def last_started(conn, account_id: int) -> Optional[datetime]:
    """When the last successful upload of the account started (Moscow time), as the last cycle read it; None when it is not known."""
    return started_moscow((load(conn, account_id) or {}).get("last_started_at"))


def load(conn, account_id: int) -> Optional[dict]:
    raw = store.get_state(conn, KEY % account_id)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None
