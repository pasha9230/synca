"""Former model names (the description variable {old_model}) from a CSV file: brand,model,former_name (the owner 2026-10-26).
Without apply nothing is written. With it every changed row goes through the panel's save (same check, same journal line) and the
whole file is one transaction: a refused row stops the file unless skip_bad. An empty name is skipped, removing stays in the panel."""
from __future__ import annotations

import csv
from pathlib import Path

from . import store
from .mappings import MappingError, check_old_model_name, save_old_model_name

COLUMNS = ("brand", "model", "former_name")


class _OneTransaction:
    """The connection the panel's save writes through: the store commits after each journal line, here the file commits once."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, *args):
        return self._conn.execute(*args)

    def commit(self) -> None:
        pass


def read_rows(path):
    """[(line number, brand, model, former name)]; ValueError when the header lacks a column."""
    with open(Path(path), encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = [(name or "").strip().casefold() for name in (reader.fieldnames or [])]
        missing = [c for c in COLUMNS if c not in header]
        if missing:
            raise ValueError("в первой строке файла нет колонок: %s (нужны %s)" % (", ".join(missing), ",".join(COLUMNS)))
        reader.fieldnames = header
        return [(reader.line_num, row.get("brand") or "", row.get("model") or "", row.get("former_name") or "") for row in reader]


def run(conn, ref, path, apply: bool, skip_bad: bool, now: str, out=print) -> int:
    """Print the plan row by row and write it with apply; 0 when all is fine, 1 when a row was refused."""
    saved = store.load_old_model_names(conn)
    changes, unchanged, empty, refused, seen = [], 0, 0, 0, {}
    for line, brand, model, typed in read_rows(path):
        where = "строка %d: %s %s" % (line, brand.strip(), model.strip())
        if not " ".join(typed.split()):
            empty += 1
            out("%s: пропущено, прежнее название пустое (убрать можно только в панели)" % where)
            continue
        try:
            avito_brand, avito_model, text = check_old_model_name(ref, brand, model, typed)
            if (avito_brand, avito_model) in seen:
                raise MappingError("модель уже есть в файле, строка %d" % seen[(avito_brand, avito_model)])
        except MappingError as err:
            refused += 1
            out("%s: отказ: %s" % (where, err))
            continue
        seen[(avito_brand, avito_model)] = line
        before = saved.get((avito_brand, avito_model), "")
        if before == text:
            unchanged += 1
            out("строка %d: %s %s: без изменений" % (line, avito_brand, avito_model))
            continue
        changes.append((brand, model, typed))
        out("строка %d: %s %s: «%s» → «%s»" % (line, avito_brand, avito_model, before, text))
    out("Итого: изменится %d, без изменений %d, пропущено пустых %d, отказов %d." % (len(changes), unchanged, empty, refused))
    if not apply:
        out("Пробный прогон: ничего не записано. Записать: добавьте --apply.")
        return 1 if refused else 0
    if refused and not skip_bad:
        out("Есть отказы: ничего не записано. Исправьте файл или добавьте --skip-bad, чтобы записать остальные строки.")
        return 1
    one = _OneTransaction(conn)
    try:
        for brand, model, typed in changes:
            save_old_model_name(one, ref, brand, model, typed, now, "cli")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    out("Записано: %d%s. Фид обновится со следующим циклом." % (len(changes), ", строки с отказом пропущены" if refused else ""))
    return 0
