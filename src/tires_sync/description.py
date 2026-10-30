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
    "code", "country", "made_in", "price", "search", "stock", "old_model", "new_only",
)
NEW_ONLY = "{new_only}"     # a line marker, not a value: the line is left out for a position with the supplier's age note (owner, 2026-10-26)

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
    "country": "страна происхождения бренда (из выгрузки поставщика, обычными словами: «Корея», «КНР», «США»)",
    "made_in": "строка целиком: «Страна производства: КНР», если в названии у поставщика в конце «CN»; иначе пусто — строка не выводится",
    "price": "цена в рублях, целое число",
    "search": "варианты написания размера для поиска",
    "stock": "остаток у поставщика, штук (в объявление количество не передаётся, только в текст)",
    "old_model": "прежнее название модели вместе со словами «Прежнее название шин», например «Прежнее название шин Nokian Hakkapeliitta 9» "
                 "у Ikon Autograph Ice 9 (название задаётся в «Сопоставлениях»); если не задано, строка с этой подстановкой не выводится; "
                 "если в строке перед подстановкой уже написано «Прежнее название шин», слова не повторяются",
    "new_only": "метка строки, сама не печатается: строка выводится, только если в названии у поставщика нет пометки о возрасте "
                "(«старше 3-х лет», «старше 5-ти лет»); например, в конце строки «Все позиции новые: 2025-2026 года производства»",
}

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
COUNTRY_WORDS = {           # the supplier's uppercase country names in a normal form; any other one is title-cased (owner, 2026-10-27)
    "КИТАЙ": "КНР", "КОРЕЯ": "Корея", "КОРЕЯ, РЕСПУБЛИКА": "Корея", "СОЕДИНЕННЫЕ ШТАТЫ АМЕРИКИ": "США", "РОССИЯ": "Россия",
    "ИТАЛИЯ": "Италия", "ГЕРМАНИЯ": "Германия", "ЯПОНИЯ": "Япония", "ТАИЛАНД": "Таиланд", "БЕЛАРУСЬ": "Беларусь",
    "ФРАНЦИЯ": "Франция", "ВЕНГРИЯ": "Венгрия",
}
BRAND_COUNTRY = {           # brand -> its country over the supplier's value: Gislaved by the owner's word, the rest have none in the unload
    "GISLAVED": "Россия", "BARS": "Россия", "HAIDA": "КНР",
    "ATTAR": "Казахстан",   # KamaTyresKZ (Tatneft and Allur), the plant in Saran
    "PACE": "КНР",          # SD-International, Shanghai
}                           # Satoya: no reliable source, the line drops
MADE_IN_CHINA = re.compile(r"\bCN\b\W*$")     # the supplier marks a tire made in China with «CN» at the end of the name (before its age note)
MADE_IN_TEXT = "Страна производства: КНР"       # {made_in}: the whole line, so a template carries it as «{made_in}» alone (the owner 2026-10-30)
OLD_MODEL_WORDS = "Прежнее название шин"      # {old_model} prints these words before the former name (owner, 2026-10-27)
_OLD_MODEL_WORDS_BEFORE = re.compile(r"прежнее\s+название\s+шин[\W_]*$", re.IGNORECASE)     # the template line already says them itself (a colon, a dash, a quote after them is fine)
_LATIN_TWINS = str.maketrans("aceopxyABCEHKMOPTXY", "асеорхуАВСЕНКМОРТХУ")     # the owner types some Cyrillic letters as Latin ones
_NEW_ONLY_MARK = re.compile(r"[ \t]*\{new_only\}")
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


def _starts_with_words(name: str) -> bool:
    return bool(re.match(r"\s*прежнее\s+название\s+шин\b", name, re.IGNORECASE))      # a name saved with the words in it: not doubled


def brand_country(c: Candidate) -> str:
    key = re.sub(r"^S\d+:", "", c.offer.brand_key or "").upper()
    override = BRAND_COUNTRY.get((c.avito_brand or "").upper()) or BRAND_COUNTRY.get(key)
    if override:
        return override
    raw = " ".join((c.offer.country or "").split()).upper()
    return COUNTRY_WORDS.get(raw) or raw.title()


def values(c: Candidate, price: int, old_model: str = "") -> Dict[str, str]:
    from .feed import AGE_NOTE                          # feed imports this module
    o = c.offer
    brand_ru = BRAND_RU.get(c.avito_brand, "")
    speed_load = ("%s %s" % (o.speed, o.load.split("/")[0].strip())).strip()
    return {
        "title": o.name_orig, "title_ru": o.name, "brand": c.avito_brand, "brand_ru": brand_ru,
        "brand_full": BRAND_FULL.get(c.avito_brand) or ("%s / %s" % (c.avito_brand, brand_ru) if brand_ru else c.avito_brand),
        "model": c.avito_model, "size": "%d/%d R%d" % (o.width, o.height, o.diameter),
        "size_slash": "%d/%d/%d" % (o.width, o.height, o.diameter), "indexes": speed_load,
        "season": SEASON_WORD.get(o.season, ""), "code": o.code, "country": brand_country(c),
        "made_in": MADE_IN_TEXT if MADE_IN_CHINA.search(AGE_NOTE.sub(" ", o.name_orig or "")) else "",     # the whole line (the owner 2026-10-30)
        "price": str(price), "search": search_variants(o.width, o.height, o.diameter), "stock": str(o.stock),
        "old_model": ("%s %s" % (OLD_MODEL_WORDS, old_model)) if old_model and not _starts_with_words(old_model) else old_model,
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


def _value(m, line: str, data: Dict[str, str], bare_old_model: str) -> str:
    name = m.group(1)
    if name == "old_model" and _OLD_MODEL_WORDS_BEFORE.search(line[:m.start()].translate(_LATIN_TWINS)):
        return re.sub(r"^\s*прежнее\s+название\s+шин\s*[:\-–—]?\s*", "", bare_old_model, flags=re.IGNORECASE)   # «Прежнее название шин {old_model}» in the template: the words are not doubled
    return data.get(name, m.group(0))


def render_description(template: str, c: Candidate, price: int, old_models=None) -> str:
    """`old_models`: (Avito brand, Avito model) -> the former name (store.load_old_model_names); without it {old_model} is empty."""
    from .feed import AGE_NOTE                          # feed imports this module: the supplier's age note, read here when rendering
    bare_old_model = (old_models or {}).get((c.avito_brand, c.avito_model), "")
    data = values(c, price, bare_old_model)
    aged = bool(AGE_NOTE.search(c.offer.name_orig or "") or AGE_NOTE.search(c.offer.name or ""))
    lines: List[str] = []
    for line in template.splitlines():
        if NEW_ONLY in line:
            if aged:
                continue                                # the tires are older: no line that claims this year's production
            line = _NEW_ONLY_MARK.sub("", line)         # with the spaces before it: «…производства {new_only}» renders as before
        names = [n for n in _PLACEHOLDER.findall(line) if n in data]
        if names and any(not data[n] for n in names):
            continue
        lines.append(_PLACEHOLDER.sub(lambda m: _value(m, line, data, bare_old_model), line))
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return CONTROL_CHARS.sub("", text).strip()
