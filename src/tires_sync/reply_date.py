"""The date a client names in an answer to the reminder of the reservation (the owner 2026-11-06): «12.11», «12/11», «12.11.2026», «12 ноября»,
«сегодня», «завтра», «послезавтра», «в пятницу». Only today and the next 30 days are a date (`ok`); one before or later is `far`; two
different dates are `many`; nothing, `none`; a «не», «нет», «отказ…», «перезвон…», «передума…», «отмен…», «откаж…», a promise to tell later («завтра сообщу / напишу / позвоню / скажу / уточню /
подумаю») is `refused` whatever date is in it
(«сегодня не успею», «завтра не смогу»: the review 2026-11-06, the responsible's, never a date). A weekday is the next one after the day of the message (on a Friday «в пятницу» is a week later).
«в 17.11», «к 12.30» is a time, not a date (`none`: the responsible's).
A day without a year is this year's, or the next year's when it has passed. «12.30», a phone, a price are no date (no such month)."""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional, Set, Tuple

AHEAD = 30
MONTHS = ("январ", "феврал", "март", "апрел", "ма[яй]", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр")
WEEKDAYS = ("понедельник", "вторник", "сред[ау]", "четверг", "пятниц[ау]", "суббот[ау]", "воскресень[ея]")
NUMERIC = re.compile(r"(?<![\d.,/:])(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?(?![\d/:]|[.,]\d)")
WORDS = re.compile(r"(?<!\d)(\d{1,2})(?:-?го)?\s+(%s)[а-я]*" % "|".join("(%s)" % m for m in MONTHS))
RELATIVE = re.compile(r"(?<![а-я])(послезавтра|завтра|сегодня)(?![а-я])")
REFUSAL = re.compile(r"(?<![а-я])(не|нет|отказ[а-я]*|перезвон[а-я]*|передума[а-я]*|отмен[а-я]*|никак|нельзя|вряд|откаж[а-я]*|позвон[а-я]*|напиш[а-я]*|скаж[а-я]*|сообщ[а-я]*|уточн[а-я]*|подума[а-я]*|решу|реш[иа]м)(?![а-я])")
WEEKDAY = re.compile(r"(?<![а-я])(%s)(?![а-я])" % "|".join("(%s)" % w for w in WEEKDAYS))


def _day(day: date, dd: int, mm: int, year: Optional[int]) -> Optional[date]:
    try:
        found = date(year if year is not None else day.year, mm, dd)
    except ValueError:
        return None
    if year is None and found < day:
        try:
            found = date(day.year + 1, mm, dd)
        except ValueError:
            return None
    return found


def found_dates(text: str, day: date) -> Set[date]:
    """Every date the text names, counted from `day` (the day the client wrote it)."""
    text = str(text or "").lower().replace("ё", "е")
    dates: Set[date] = set()
    for match in NUMERIC.finditer(text):
        dd, mm, yy = match.groups()
        if not yy and re.search(r"(?<![а-я])[вк]\s*$", text[:match.start()]):
            continue                                            # «в 17.11», «к 12.30»: a time
        year = None if not yy else int(yy) + (2000 if len(yy) == 2 else 0)
        found = _day(day, int(dd), int(mm), year)
        if found is not None:
            dates.add(found)
    for match in WORDS.finditer(text):
        month = next(i for i in range(len(MONTHS)) if match.group(3 + i)) + 1
        found = _day(day, int(match.group(1)), month, None)
        if found is not None:
            dates.add(found)
    for word in RELATIVE.findall(text):
        dates.add(day + timedelta(days={"сегодня": 0, "завтра": 1, "послезавтра": 2}[word]))
    for match in WEEKDAY.finditer(text):
        weekday = next(i for i in range(len(WEEKDAYS)) if match.group(2 + i))
        dates.add(day + timedelta(days=(weekday - day.weekday() - 1) % 7 + 1))
    return dates


def parse(text: str, day: date) -> Tuple[str, Optional[date]]:
    """("ok", the date) for one date from `day` to `day` + 30 days; ("far", it) for one outside; ("many", None); ("refused", None);
    ("none", None)."""
    if REFUSAL.search(str(text or "").lower().replace("ё", "е")):
        return "refused", None
    dates = found_dates(text, day)
    if not dates:
        return "none", None
    if len(dates) > 1:
        return "many", None
    [found] = dates
    return ("ok" if day <= found <= day + timedelta(days=AHEAD) else "far"), found
