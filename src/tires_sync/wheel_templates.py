"""Safe, separate Avito title and description templates for wheel rims."""
from __future__ import annotations

import re
from string import Formatter

DEFAULT_TITLE = "{name}"
DEFAULT_DESCRIPTION = "{name}"
PLACEHOLDERS = {
    "name": "название поставщика", "brand": "бренд", "model": "модель",
    "code": "артикул", "diameter": "диаметр диска", "width": "ширина диска",
    "holes": "число крепёжных отверстий", "pcd": "диаметр расположения отверстий",
    "offset": "вылет ET", "center_bore": "диаметр ступицы DIA", "wheel_type": "тип диска",
    "color": "цвет", "stock": "остаток", "price": "цена за штуку",
}
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_FORMATTER = Formatter()


def validate_settings(raw) -> None:
    if not isinstance(raw, dict) or set(raw) - {"title", "description"}:
        raise ValueError("Некорректные настройки шаблонов дисков.")
    for key, title in (("title", True), ("description", False)):
        if key in raw:
            if not isinstance(raw[key], str):
                raise ValueError("Шаблон должен быть текстом.")
            errors = validate(raw[key], title=title)
            if errors:
                raise ValueError(" ".join(errors))


def validate(template: str, *, title: bool = False) -> list[str]:
    """Reject format expressions, conversions and malformed or unknown fields."""
    errors = []
    if not template.strip():
        errors.append("Шаблон не может быть пустым.")
    if _CONTROL.search(template):
        errors.append("Шаблон содержит управляющие символы.")
    limit = 200 if title else 6000
    if len(template) > limit:
        errors.append("Шаблон длиннее %d символов." % limit)
    if title and ("\n" in template or "\r" in template):
        errors.append("Название должно состоять из одной строки.")
    try:
        for _, field, spec, conversion in _FORMATTER.parse(template):
            if field is not None and (field not in PLACEHOLDERS or spec or conversion):
                errors.append("Неизвестная или недопустимая подстановка {%s}." % field)
    except ValueError:
        errors.append("Проверьте фигурные скобки в шаблоне.")
    return errors


def render(template: str, wheel, price, *, title: bool = False) -> str:
    errors = validate(template, title=title)
    if errors:
        raise ValueError(" ".join(errors))
    values = {key: str(getattr(wheel, key, "") or "") for key in PLACEHOLDERS if key != "price"}
    values["price"] = str(price)
    lines = []
    for line in template.splitlines():
        names = [field for _, field, _, _ in _FORMATTER.parse(line) if field is not None]
        if any(not values[name] for name in names):
            continue
        lines.append(line.format_map(values))
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if not result:
        result = str(wheel.name or '').strip()
    if not result:
        raise ValueError("Для диска %s отсутствует название." % wheel.goods_id)
    return result[:200 if title else 6000].rstrip()
