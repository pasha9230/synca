"""Reminders for changes the owner agreed to in advance: the day before and on the day itself, once each, to Telegram.

Now: an Id that two accounts share on purpose until a date (`adopted_<account>.shared`); on that date the position gets its own Id again."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import List, Tuple

from . import connections, mirror_feed, store


def sent_key(account_id: int, until: str, kind: str) -> str:
    return "reminder:%d:%s:%s" % (account_id, until, kind)


def due(conn, today: date) -> List[Tuple[str, str]]:
    """[(key, text)] of the reminders that are due on `today` and were not sent yet. The key is written to the state once the text is out."""
    out: List[Tuple[str, str]] = []
    for account in connections.list_accounts(conn):
        by_date = defaultdict(int)
        for until in mirror_feed._shared_raw(conn, account["id"]).values():
            by_date[until] += 1
        name = connections.display_name(conn, account["id"])
        for until, count in sorted(by_date.items()):
            try:
                day = date.fromisoformat(until)
            except ValueError:
                continue
            if today == day - timedelta(days=1):
                kind, text = "before", (
                    "Tires Sync, напоминание: завтра (%s) у аккаунта «%s» %d позиций перейдут с общего Id на свои (_a%d). Перед сменой проверьте отчёт "
                    "Авито: первые объявления этих позиций должны быть закончены. Подтверждать ничего не нужно, смена согласована заранее." % (
                        day.strftime("%d.%m.%Y"), name, count, account["id"]))
            elif today >= day:
                kind, text = "on", (
                    "Tires Sync, напоминание: срок общего Id (%s) у аккаунта «%s» вышел, %d позиций публикуются под своими Id (_a%d). Проверьте в отчёте "
                    "Авито, нет ли блокировок «Повторное размещение», и вычеркните запись в журнале проекта (STATE.md)." % (
                        day.strftime("%d.%m.%Y"), name, count, account["id"]))
            else:
                continue
            key = sent_key(account["id"], until, kind)
            if not store.get_state(conn, key):
                out.append((key, text))
    return out
