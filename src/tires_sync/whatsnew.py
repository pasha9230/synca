"""«Что нового» (the owner 2026-11-02): the updates of the panel by number, newest first, each item for the roles it names. The texts
are files of the code, data/whatsnew/NNNN.md, and go out with the deploy:

    number: 1
    date: 02.11.2026
    title: Обновление панели №1

    ## ✅ Заголовок
    roles: owner, partner, point
    Текст: абзацы через пустую строку, строки «- » — список, **жирный**.

A file that does not read (a header missing, a number not the file's, a role nobody has, an item without roles or text) is skipped and
logged; the panel never falls over a text. The owner and the partner see every item; anyone else the items naming their role (working
as a point: the point's). New for a person = an update with items for their role newer than the number they last saw for that role
(user_pref whatsnew.seen, {role: number}); opening the page marks it seen."""
from __future__ import annotations

import logging
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from markupsafe import Markup, escape

from . import user_prefs, users

DIR = Path(__file__).resolve().parent / "data" / "whatsnew"
NAME = re.compile(r"^(\d{4})\.md$")
SEEN_KEY = "whatsnew.seen"
SEES_ALL = frozenset({"owner", "partner"})
log = logging.getLogger(__name__)


class BadFile(ValueError):
    """The file is not an update the page can show."""


def _body(lines: list) -> Markup:
    """The text of an item as safe HTML: everything escaped, then paragraphs, «- » lists and **bold** only."""
    out, para, items = [], [], []

    def flush():
        if para:
            out.append("<p>%s</p>" % "<br>".join(para))
            para.clear()
        if items:
            out.append("<ul>%s</ul>" % "".join("<li>%s</li>" % i for i in items))
            items.clear()

    for raw in lines:
        line = raw.strip()
        if not line:
            flush()
            continue
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", str(escape(line[2:] if line.startswith("- ") else line)))
        if line.startswith("- "):
            if para:
                flush()
            items.append(text)
        else:
            if items:
                flush()
            para.append(text)
    flush()
    return Markup("".join(out))


def parse(text: str, number: int) -> dict:
    """{"number", "date", "title", "items": [{"title", "roles", "body"}]} of a file; BadFile with the reason."""
    head, _, rest = text.replace("\r\n", "\n").partition("\n## ")
    fields = {}
    for line in head.splitlines():
        if line.strip():
            key, sep, value = line.partition(":")
            if not sep:
                raise BadFile("header line without «:»: %r" % line)
            fields[key.strip()] = value.strip()
    if set(fields) != {"number", "date", "title"}:
        raise BadFile("the header must be number, date, title: %s" % sorted(fields))
    if fields["number"] != str(number):
        raise BadFile("number %r is not the file's %d" % (fields["number"], number))
    try:
        datetime.strptime(fields["date"], "%d.%m.%Y")
    except ValueError:
        raise BadFile("date %r is not ДД.ММ.ГГГГ" % fields["date"]) from None
    if not fields["title"]:
        raise BadFile("no title")
    items = []
    for chunk in rest.split("\n## ") if rest else []:
        lines = chunk.split("\n")
        title, roles_line, body = lines[0].strip(), (lines[1] if len(lines) > 1 else "").strip(), lines[2:]
        if not title:
            raise BadFile("an item without a title")
        if not roles_line.startswith("roles:"):
            raise BadFile("item %r: the line after the title must be «roles: …»" % title)
        roles = [r.strip() for r in roles_line[len("roles:"):].split(",") if r.strip()]
        unknown = [r for r in roles if r not in users.ROLES]
        if not roles or unknown:
            raise BadFile("item %r: roles %s" % (title, unknown or "missing"))
        if not any(line.strip() for line in body):
            raise BadFile("item %r: no text" % title)
        items.append({"title": title, "roles": frozenset(roles), "body": _body(body)})
    if not items:
        raise BadFile("no items")
    return {"number": number, "date": fields["date"], "title": fields["title"], "items": items}


def load(folder: Path = DIR) -> list:
    """Every update that reads, newest first; a file that does not is logged and left out."""
    found = []
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        match = NAME.match(path.name)
        try:
            if not match or int(match.group(1)) < 1:
                raise BadFile("the name must be NNNN.md, from 0001")
            found.append(parse(path.read_text(encoding="utf-8"), int(match.group(1))))
        except (BadFile, OSError, UnicodeDecodeError) as err:
            log.warning("whatsnew: %s skipped: %s", path.name, err)
    return sorted(found, key=lambda u: u["number"], reverse=True)


@lru_cache(maxsize=1)
def shipped() -> tuple:
    """The updates of this deploy (read once: the files change with the code only)."""
    return tuple(load())


def for_role(updates, role: str) -> list:
    """The updates as `role` sees them: only its items, an update without any left out."""
    out = []
    for update in updates:
        items = [i for i in update["items"] if role in SEES_ALL or role in i["roles"]]
        if items:
            out.append(dict(update, items=items))
    return out


def latest(updates, role: str) -> int:
    """The number of the newest update `role` sees; 0: none."""
    shown = for_role(updates, role)
    return shown[0]["number"] if shown else 0


def _seen(conn, login: str) -> dict:
    value = user_prefs.get(conn, login, SEEN_KEY, {})
    return value if isinstance(value, dict) else {}


def has_new(conn, login: str, role: str, updates) -> bool:
    """The dot of the menu: an update for `role` newer than the one `login` saw last as that role."""
    if not login or not role:
        return False
    seen = _seen(conn, login).get(role, 0)
    return latest(updates, role) > (seen if isinstance(seen, int) else 0)


def mark_seen(conn, login: str, role: str, updates, now: str) -> None:
    """The page was opened: everything `role` sees is read for `login`. Writes only when something was new."""
    if login and has_new(conn, login, role, updates):
        seen = _seen(conn, login)
        seen[role] = latest(updates, role)
        user_prefs.put(conn, login, SEEN_KEY, seen, now)
