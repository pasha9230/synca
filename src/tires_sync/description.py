from __future__ import annotations

import re
from typing import Dict, List

from .candidates import Candidate
from .models import Season

BRAND_RU = {"Ikon Tyres": "Айкон Тайерс", "Kumho": "Кумхо", "Yokohama": "Йокохама", "Cordiant": "Кордиант", "Pirelli": "Пирелли",
            "Formula": "Пирелли Формула", "Nexen": "Нексен", "Gislaved": "Гиславед"}
BRAND_FULL = {"Formula": "Pirelli Formula / Пирелли Формула", "Gislaved": "Гиславед"}       # how a brand is written whole in the owner's own texts
SEASON_WORD = {Season.WINTER: "зимняя", Season.SUMMER: "летняя", Season.ALL: "всесезонная"}
MAX_LENGTH = 6000

ALLOWED = (
    "title", "title_ru", "brand", "brand_ru", "brand_full", "model", "size", "size_slash", "indexes", "season",
    "code", "country", "price", "search", "stock", "old_model",
)

DEFAULT_TEMPLATE = """🔥 {title}

НОВЫЕ ШИНЫ {brand_full} для авто
▸ Модель: {model}
▸ Размер: {size}
▸ Сезон: {season}
▸ Артикул: {code}
▸ Страна происхождения бренда: {country}
▸ В наличии: {stock} шт.

💰 Цена: {price} руб. за 1 шт.

📞 Звоните по телефону.
💬 Пишите в чат Авито — ответим быстро

🏆 Огромный выбор шин
⚡ Возможен привоз под заказ
🔧 Поможем с подбором

Для поиска: {search}"""

PLACEHOLDER_HELP = {
    "title": "название товара у поставщика",
    "title_ru": "название товара у поставщика по-русски (как в его выгрузке: «Гиславед 185/55/15 …»)",
    "brand": "бренд как в Авито",
    "brand_ru": "бренд по-русски (если известен, иначе пусто)",
    "brand_full": "бренд и русское название через «/» (если русского нет, только бренд)",
    "model": "модель как в Авито",
    "size": "размер, например 205/55 R16",
    "size_slash": "размер, например 205/55/16",
    "indexes": "индексы скорости и нагрузки, например T 94",
    "season": "сезон: зимняя, летняя или всесезонная",
    "code": "артикул производителя",
    "country": "страна происхождения бренда (из выгрузки поставщика)",
    "price": "цена в рублях, целое число",
    "search": "варианты написания размера для поиска",
    "stock": "остаток у поставщика, штук (в объявление количество не передаётся, только в текст)",
    "old_model": "прежнее название модели с брендом, например Nokian Hakkapeliitta 9 у Ikon Autograph Ice 9 (задаётся в «Сопоставлениях»); "
                 "если не задано, строка с этой подстановкой не выводится",
}

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def search_variants(width: int, height: int, diameter: int) -> str:
    w, h, d = width, height, diameter
    variants = [
        "%d-%d-%d" % (w, h, d), "%d/%d/%d" % (w, h, d), "%d %d %d" % (w, h, d), "%d %d R%d" % (w, h, d),
        "%d-%dr%d" % (w, h, d), "%d/%d/R%d" % (w, h, d), "%d%dr%d" % (w, h, d), "%d/%d R%d" % (w, h, d),
        "%d %d Р%d" % (w, h, d), "%d-%d-Р%d" % (w, h, d), "%d/%d/Р%d" % (w, h, d), "%d%dр%d" % (w, h, d),
        "%d/%dR%d" % (w, h, d),
    ]
    return ", ".join(variants)


def values(c: Candidate, price: int, old_model: str = "") -> Dict[str, str]:
    o = c.offer
    brand_ru = BRAND_RU.get(c.avito_brand, "")
    speed_load = ("%s %s" % (o.speed, o.load.split("/")[0].strip())).strip()
    return {
        "title": o.name_orig, "title_ru": o.name, "brand": c.avito_brand, "brand_ru": brand_ru,
        "brand_full": BRAND_FULL.get(c.avito_brand) or ("%s / %s" % (c.avito_brand, brand_ru) if brand_ru else c.avito_brand),
        "model": c.avito_model, "size": "%d/%d R%d" % (o.width, o.height, o.diameter),
        "size_slash": "%d/%d/%d" % (o.width, o.height, o.diameter), "indexes": speed_load,
        "season": SEASON_WORD.get(o.season, ""), "code": o.code, "country": o.country,
        "price": str(price), "search": search_variants(o.width, o.height, o.diameter), "stock": str(o.stock),
        "old_model": old_model,
    }


def validate_template(template: str) -> List[str]:
    errors: List[str] = []
    if not template.strip():
        errors.append("Шаблон не может быть пустым.")
    if CONTROL_CHARS.search(template):
        errors.append("В шаблоне есть управляющие символы (например, мягкий перенос из Word): удалите их и вставьте текст заново.")
    if len(template) > MAX_LENGTH:
        errors.append("Шаблон длиннее %d символов (сейчас %d)." % (MAX_LENGTH, len(template)))
    for name in dict.fromkeys(_PLACEHOLDER.findall(template)):
        if name not in ALLOWED:
            errors.append("Неизвестная подстановка {%s}." % name)
    return errors


def render_description(template: str, c: Candidate, price: int, old_models=None) -> str:
    """`old_models`: (Avito brand, Avito model) -> the former name (store.load_old_model_names); without it {old_model} is empty."""
    data = values(c, price, (old_models or {}).get((c.avito_brand, c.avito_model), ""))
    lines: List[str] = []
    for line in template.splitlines():
        names = [n for n in _PLACEHOLDER.findall(line) if n in data]
        if names and any(not data[n] for n in names):
            continue
        lines.append(_PLACEHOLDER.sub(lambda m: data.get(m.group(1), m.group(0)), line))
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return CONTROL_CHARS.sub("", text).strip()
