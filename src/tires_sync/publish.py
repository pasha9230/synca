from __future__ import annotations

import os
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

from .avito import AvitoClient, UploadTooSoon

FEED_NAME = "tires"
SHRINK_LIMIT = 0.5
MAX_PROBLEMS = 20


class PublishError(Exception):
    pass


def count_ads(data: bytes) -> int:
    return len(ET.fromstring(data).findall("Ad"))


def promote_feed(folder, allow_shrink: bool = False, expected_removals: int = 0) -> Dict[str, int]:
    """Make the candidate feed (next/) the one Avito reads, unless it shrank suspiciously."""
    folder = Path(folder)
    candidate = folder / "next" / "avito_feed.xml"
    public = folder / "avito_feed.xml"
    if not candidate.exists():
        raise PublishError("Черновик фида ещё не создан: сначала нужен успешный цикл.")
    data = candidate.read_bytes()
    try:
        new = count_ads(data)
    except ET.ParseError:
        raise PublishError("Черновик фида повреждён (некорректный XML).") from None
    if new == 0:
        raise PublishError("В черновике фида нет объявлений.")
    old = 0
    if public.exists():
        try:
            old = count_ads(public.read_bytes())
        except (ET.ParseError, OSError):
            old = 0
    if old and new < old * SHRINK_LIMIT and new < old - expected_removals and not allow_shrink:
        raise PublishError(
            "Фид сократился с %d до %d объявлений (более чем вдвое): проверьте выгрузку поставщика "
            "или добавьте --allow-shrink, если так и задумано." % (old, new))
    fd, tmp = tempfile.mkstemp(dir=str(folder), prefix="avito_feed.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, 0o640)
        os.replace(tmp, str(public))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return {"new": new, "old": old}


def _merged_feeds(profile: dict, feed_url: str, feed_name: str) -> List[dict]:
    feeds = [dict(f) for f in profile.get("feeds_data") or []]
    ours = {"feed_name": feed_name, "feed_url": feed_url}
    for index, feed in enumerate(feeds):
        if feed.get("feed_name") == feed_name:
            feeds[index] = ours
            return feeds
    feeds.append(ours)
    return feeds


def plan_feed(client: AvitoClient, feed_url: str, feed_name: str = FEED_NAME) -> Dict[str, object]:
    """Read-only: what would change in the Avito autoload profile."""
    profile = client.get_profile()
    feeds = profile.get("feeds_data") or []
    current = next((f for f in feeds if f.get("feed_name") == feed_name), None)
    if current is None and any(f.get("feed_url") == feed_url for f in feeds):
        return {"action": "none", "feed_url": feed_url, "feed_name": feed_name, "profile": profile}      # the link is there already, under another name
    if current is None:
        action = "add"
    elif current.get("feed_url") == feed_url:
        action = "none"
    else:
        action = "replace"
    return {"action": action, "feed_url": feed_url, "feed_name": feed_name, "profile": profile}


def ensure_feed(
    client: AvitoClient, feed_url: str, feed_name: str = FEED_NAME, plan: Optional[Dict[str, object]] = None
) -> Dict[str, object]:
    """Point the autoload profile at our feed, keeping everything else the owner configured."""
    plan = plan or plan_feed(client, feed_url, feed_name)
    if plan["action"] == "none":
        return plan
    profile = plan["profile"]
    client.set_profile(
        feed_url=feed_url, feed_name=feed_name, autoload_enabled=bool(profile.get("autoload_enabled", False)),
        report_email=profile.get("report_email") or "", schedule=profile.get("schedule") or [],
        other_feeds=[f for f in _merged_feeds(profile, feed_url, feed_name) if f.get("feed_name") != feed_name],
    )
    return plan


def upload_now(client: AvitoClient, last_upload_at: Optional[float]) -> bool:
    try:
        client.start_upload(last_upload_at=last_upload_at)
    except UploadTooSoon:
        raise PublishError("Загрузку можно запускать не чаще раза в час: прошлая была совсем недавно.") from None
    return True


def _upload_view(upload: Optional[dict]) -> Optional[dict]:
    if upload is None:
        return None
    return {
        "upload_id": upload.get("upload_id"), "status": upload.get("status"), "started_at": upload.get("started_at"),
        "count": (upload.get("stats") or {}).get("count"), "events": upload.get("events") or [],
    }


def status_report(client: AvitoClient) -> dict:
    current = client.current_upload()
    last = client.last_successful_upload()
    report = {"current": _upload_view(current), "last": _upload_view(last), "counts": {}, "problems": []}
    kind = "last_successful" if last is not None else ("current" if current is not None else None)
    if kind is None:
        return report
    counts: Counter = Counter()
    problems = []
    for item in client.upload_items(kind):
        status = item.get("avito_status") or "unknown"
        counts[status] += 1
        if status != "active" and len(problems) < MAX_PROBLEMS:
            text = "; ".join(
                "%s: %s" % (m.get("title", ""), m.get("description", "")) if m.get("description") else str(m.get("title", ""))
                for m in item.get("messages") or [])
            problems.append({"ad_id": item.get("ad_id"), "status": status, "text": text})
    report["counts"] = dict(counts)
    report["problems"] = problems
    return report
