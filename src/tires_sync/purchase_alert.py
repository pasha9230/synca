"""«Закупка изменилась» (the owner 2026-11-05/06): after a cycle in which the supplier's purchase of any position changed (a new row of
supplier_price_change, every supplier), one message to the people who may see the purchase, and a red row of the bell for each of them until
«Разобрался». Each change once (UPTO: the last row put into a message); the first run after the deploy only remembers where the history is;
outside the alert hours (order_oth.in_alert_hours) nothing is made: the changes wait and the first cycle of the working hours tells them all
in one message. Read-only for the prices and the feeds; the sending is the caller's (cli._tell_important, the one channel of such news)."""
from __future__ import annotations

from typing import Dict, List, Optional

from . import store

UPTO = "purchase_alert_upto"           # app_state: the id of the last row of supplier_price_change already in a message
SEEN = "purchase_alert_seen:"          # app_state: "<SEEN><login>" -> the id of the last alert this person pressed «Разобрался» on
SHOWN = 20                             # the message lists this many, the biggest change in percent first
PAGE = "/prices/purchase"


def people(conn) -> List[str]:
    """Who is told: the owner's login of the alerts and every active panel user whose role may see the purchase (the right «money»: the owner
    and the partner). No login is written in the code."""
    from . import order_notify, users
    found = {order_notify.get_owner_login(conn)}
    found |= {u["login"] for u in users.list_users(conn) if u["active"] and users.has_right(u["role"], "money")}
    return sorted(found)


def may_see(conn, login: str) -> bool:
    """The bell row and the page: a signed-in person whose role may see the purchase."""
    from . import users
    role = users.get_role(conn, login)
    return bool(login) and role["active"] and users.has_right(role["role"], "money")


def _changes(conn, after: int, upto: int) -> List[dict]:
    """The changes of rows after..upto, one per position: from its first «было» to its last price; a position back where it was is left out."""
    found: Dict[str, dict] = {}
    for _id, goods_id, was, price in conn.execute(
            "SELECT id, goods_id, was, price FROM supplier_price_change WHERE id > ? AND id <= ? ORDER BY id", (after, upto)):
        row = found.setdefault(goods_id, {"goods_id": goods_id, "was": float(was)})
        row["price"] = float(price)
    rows = [r for r in found.values() if r["was"] > 0 and r["price"] != r["was"]]
    for r in rows:
        r["pct"] = (r["price"] - r["was"]) / r["was"] * 100
    rows.sort(key=lambda r: (-abs(r["pct"]), r["goods_id"]))
    return rows


def _named(conn, rows: List[dict], offers=None) -> List[dict]:
    """The name of each position (today's unload, else the archive of the catalog, else its id) and our price: the price of its ad in the
    first feed account that publishes it (published_state), None when no account does."""
    from . import catalog_archive, connections
    ids = [r["goods_id"] for r in rows]
    order = {a["id"]: n for n, a in enumerate(connections.list_feed_accounts(conn))}
    ours: Dict[str, tuple] = {}
    for n in range(0, len(ids), 500):
        chunk = ids[n:n + 500]
        for account_id, goods_id, price in conn.execute(
                "SELECT account_id, goods_id, price FROM published_state WHERE goods_id IN (%s)" % ",".join("?" * len(chunk)), chunk):
            if account_id in order and (goods_id not in ours or order[account_id] < ours[goods_id][0]):
                ours[goods_id] = (order[account_id], price)
    out = []
    for r in rows:
        offer = (offers or {}).get(r["goods_id"]) or catalog_archive.last_offer(conn, r["goods_id"])
        name = (getattr(offer, "name", "") or "").strip() or r["goods_id"]
        out.append(dict(r, name=name, ours=ours[r["goods_id"]][1] if r["goods_id"] in ours else None))
    return out


def _num(value) -> str:
    return "{:,}".format(int(round(value))).replace(",", " ")


def pct_text(value: float) -> str:
    shown = ("%.0f" % abs(value)) if abs(value) >= 1 else ("%.1f" % abs(value)).replace(".", ",")
    return ("+" if value > 0 else "−") + shown + " %"


def line(row: dict) -> str:
    """«название: 8 000 → 8 480 (+6 %), наша цена 9 500» (наша цена only when an ad has one)."""
    text = "%s: %s → %s (%s)" % (row["name"], _num(row["was"]), _num(row["price"]), pct_text(row["pct"]))
    return text + (", наша цена %s" % _num(row["ours"]) if row.get("ours") else "")


def head(rows: List[dict]) -> str:
    ups = sum(1 for r in rows if r["price"] > r["was"])
    return "Закупка у поставщика изменилась: %d %s (выросла: %d, снизилась: %d)" % (len(rows), _positions(len(rows)), ups, len(rows) - ups)


def _positions(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "позиция"
    return "позиции" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "позиций"


def text(rows: List[dict], link: str) -> str:
    """The message: the rises and the falls apart, the SHOWN biggest in percent, then «ещё N позиций» with the link to the whole list."""
    top = rows[:SHOWN]
    parts = ["Tires Sync: " + head(rows) + ". Цены за одну шину."]
    for title, part in (("Выросла:", [r for r in top if r["price"] > r["was"]]), ("Снизилась:", [r for r in top if r["price"] < r["was"]])):
        if part:
            parts.append(title)
            parts += [line(r) for r in part]
    if len(rows) > SHOWN:
        rest = len(rows) - SHOWN
        parts.append("ещё %d %s: %s" % (rest, _positions(rest), link))
    return "\n".join(parts)


def collect(conn, now: str, offers=None) -> Optional[dict]:
    """The new alert of this cycle, or None: nothing new, the first run (it only remembers where the history is) or a change that came back.
    The alert and the mark UPTO are written in one commit: a change is never in two alerts. The time `now` is the caller's (alert hours)."""
    top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM supplier_price_change").fetchone()[0]
    upto = store.get_state(conn, UPTO)
    if upto is None or upto == "":
        store.set_state(conn, UPTO, str(top))
        return None
    after = int(upto)
    if top <= after:
        return None
    rows = _changes(conn, after, top)
    alert = None
    if rows:
        cur = conn.execute("INSERT INTO purchase_alert (at, first_change, last_change, total, head) VALUES (?,?,?,?,?)",
                           (now, after, top, len(rows), head(rows)))
        alert = {"id": cur.lastrowid, "at": now, "rows": _named(conn, rows, offers)}
    conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (UPTO, str(top)))
    conn.commit()
    return alert


def get(conn, alert_id: Optional[int] = None) -> Optional[dict]:
    """One alert with its whole list (the latest when no id), or None."""
    sql = "SELECT id, at, first_change, last_change FROM purchase_alert " + ("WHERE id = ?" if alert_id is not None else "ORDER BY id DESC LIMIT 1")
    row = conn.execute(sql, (alert_id,) if alert_id is not None else ()).fetchone()
    if row is None:
        return None
    return {"id": row[0], "at": row[1], "rows": _named(conn, _changes(conn, row[2], row[3]))}


def recent(conn, limit: int = 30) -> List[dict]:
    return [{"id": r[0], "at": r[1], "total": r[2], "head": r[3]}
            for r in conn.execute("SELECT id, at, total, head FROM purchase_alert ORDER BY id DESC LIMIT ?", (int(limit),))]


def bell_rows(conn, login: str) -> List[dict]:
    """The red row of the bell: the newest alert this person has not pressed «Разобрался» on; the older ones are counted in it («more») and go
    with it. Only for who may see the purchase."""
    if not may_see(conn, login):
        return []
    seen = int(store.get_state(conn, SEEN + login) or 0)
    found = conn.execute("SELECT id, at, head FROM purchase_alert WHERE id > ? ORDER BY id DESC", (seen,)).fetchall()
    if not found:
        return []
    alert_id, at, title = found[0]
    return [{"id": "purchase-%d" % alert_id, "order_id": None, "chat_row": None, "client": "", "at": at, "author": "", "kind": "purchase",
             "level": "red", "more": len(found) - 1, "title": "Закупка изменилась", "text": title,
             "href": "%s/%d" % (PAGE, alert_id), "ack_href": "/alerts/purchase/%d/ack" % alert_id}]


def ack(conn, alert_id: int, login: str) -> None:
    """«Разобрался»: this alert and every older one leave this person's bell. No commit of its own."""
    seen = int(store.get_state(conn, SEEN + login) or 0)
    if alert_id > seen:
        conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (SEEN + login, str(int(alert_id))))
