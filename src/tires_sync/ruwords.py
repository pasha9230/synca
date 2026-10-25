"""Russian spellings of the English words that tire model names are made of.

The supplier sometimes sends a model in Russian letters («Бизнес», «Офф Роуд»); Avito has the English one. Sound-based
comparison alone does not get there («БИЗНЕС» is not «BUSINESS»), so the common words are listed here. Several Russian
spellings may lead to one English word. Uppercase, no spaces: the same form normalize_model gives."""
from __future__ import annotations

import re
from typing import Dict, List

ENGLISH_TO_RUSSIAN: Dict[str, List[str]] = {
    "BUSINESS": ["БИЗНЕС"], "OFF": ["ОФФ"], "ROAD": ["РОУД", "РОАД"], "OFFROAD": ["ОФФРОУД", "ОФРОУД", "ОФФРОАД"],
    "ICE": ["АЙС"], "SNOW": ["СНОУ", "СНОВ"], "WINTER": ["ВИНТЕР", "УИНТЕР"], "SUMMER": ["САММЕР", "САМЕР"],
    "SPORT": ["СПОРТ"], "COMFORT": ["КОМФОРТ"], "ECO": ["ЭКО", "ЕКО"], "ENERGY": ["ЭНЕРДЖИ", "ЭНЕРГИ"],
    "TOURING": ["ТУРИНГ"], "TOUR": ["ТУР"], "CROSS": ["КРОСС", "КРОС"], "CLIMATE": ["КЛАЙМАТ", "КЛАЙМЕТ", "КЛИМАТ"],
    "CROSSCLIMATE": ["КРОССКЛАЙМАТ", "КРОССКЛАЙМЕТ"], "CONTACT": ["КОНТАКТ"], "PREMIUM": ["ПРЕМИУМ"],
    "ULTRA": ["УЛЬТРА"], "MAX": ["МАКС", "МАХ"], "PLUS": ["ПЛЮС", "ПЛАС"], "PRO": ["ПРО"], "GRIP": ["ГРИП", "ГРИПП"],
    "ULTRAGRIP": ["УЛЬТРАГРИП"], "SEASON": ["СИЗОН", "СЕЗОН"], "ALL": ["ОЛЛ"], "TERRAIN": ["ТЕРРЕЙН", "ТЕРРЕЙНЕ"],
    "MUD": ["МАД", "МАДД"], "TRAIL": ["ТРЕЙЛ", "ТРЕИЛ"], "CARGO": ["КАРГО"], "VAN": ["ВАН"], "TRANSPORTER": ["ТРАНСПОРТЕР"],
    "VANCO": ["ВАНКО"], "DRIVE": ["ДРАЙВ", "ДРАЙВЕ"], "DRIVER": ["ДРАЙВЕР"], "EXTREME": ["ЭКСТРИМ", "ЭКСТРЕМ", "ЭКСТРИМУМ"],
    "EXTRA": ["ЭКСТРА"], "ARCTIC": ["АРКТИК"], "NORDIC": ["НОРДИК"], "NORD": ["НОРД"], "FROST": ["ФРОСТ"],
    "GLACIER": ["ГЛЕЙСЕР", "ГЛАЦИЕР"], "BLIZZARD": ["БЛИЗЗАРД", "БЛИЗАРД"], "SUV": ["СУВ", "ЭСЮВИ"], "HP": ["ХП", "ЭЙЧПИ"],
    "UHP": ["УХП"], "XL": ["ХЛ", "ИКСЭЛ", "ИКСЛ"], "RUNFLAT": ["РАНФЛЕТ", "РАНФЛЭТ"], "RUN": ["РАН"], "FLAT": ["ФЛЕТ", "ФЛЭТ"],
    "PERFORMANCE": ["ПЕРФОРМАНС", "ПЕРФОРМАНСЕ"], "SPEED": ["СПИД"], "SAFETY": ["СЕЙФТИ"], "SMART": ["СМАРТ"],
    "SILENT": ["САЙЛЕНТ"], "RACING": ["РЕЙСИНГ"], "TURBO": ["ТУРБО"], "HIGH": ["ХАЙ"], "WAY": ["ВЕЙ", "УЭЙ"],
    "HIGHWAY": ["ХАЙВЕЙ"], "STAR": ["СТАР"], "SUPER": ["СУПЕР"], "TRAC": ["ТРАК", "ТРЭК"], "TRACTION": ["ТРАКШН", "ТРАКШЕН"],
    "CHAMPION": ["ЧЕМПИОН"], "CLASSIC": ["КЛАССИК", "КЛАСИК"], "CITY": ["СИТИ"], "AQUA": ["АКВА"], "RAIN": ["РЕЙН"],
    "WET": ["УЭТ", "ВЕТ"], "DRY": ["ДРАЙ"], "STUD": ["СТАД"], "STUDDED": ["СТАДДЕД"], "CONTROL": ["КОНТРОЛ", "КОНТРОЛЬ"],
    "BLUE": ["БЛЮ", "БЛУ"], "GREEN": ["ГРИН"], "RED": ["РЕД"], "BLACK": ["БЛЭК", "БЛЕК"], "WHITE": ["УАЙТ", "ВАЙТ"],
    "GOLD": ["ГОЛД"], "FORCE": ["ФОРС"], "POWER": ["ПАУЭР", "ПАВЕР"], "MOTION": ["МОУШН", "МОУШЕН"], "RANGE": ["РЭНДЖ", "РЕНДЖ"],
    "TIGER": ["ТАЙГЕР"], "SHARK": ["ШАРК"], "EAGLE": ["ИГЛ", "ИГЛЕ"], "LION": ["ЛАЙОН", "ЛИОН"], "ROCK": ["РОК"],
    "STONE": ["СТОУН"], "STREET": ["СТРИТ"], "AVENUE": ["АВЕНЮ", "АВЕНЬЮ"], "TOP": ["ТОП"], "TOPSPEED": ["ТОПСПИД"],
    "ECONOMY": ["ЭКОНОМИ"], "EFFICIENT": ["ЭФФИШЕНТ"], "GRAND": ["ГРАНД"], "TOURER": ["ТУРЕР", "ТУРЕРЕ"],
    "MASTER": ["МАСТЕР"], "CHALLENGER": ["ЧЕЛЛЕНДЖЕР", "ЧЕЛЕНДЖЕР"], "ADVANCE": ["ЭДВАНС", "АДВАНС"], "ADVANTAGE": ["ЭДВАНТАЖ"],
    "SPORTS": ["СПОРТС"], "SUPERSPORT": ["СУПЕРСПОРТ"], "ULTRAHIGH": ["УЛЬТРАХАЙ"], "COOL": ["КУЛ", "КУЛЛ"], "FAST": ["ФАСТ"],
    "FIRST": ["ФЕРСТ", "ФЁРСТ"], "ONE": ["УАН"], "NEW": ["НЬЮ"], "NEXT": ["НЕКСТ"], "NANO": ["НАНО"],
    "NATURE": ["НЕЙЧЕР"], "ECOWINGER": ["ЭКОВИНГЕР"], "WINGER": ["ВИНГЕР"], "TRIANGLE": ["ТРИАНГЛ"], "RIDER": ["РАЙДЕР"],
    "RIDE": ["РАЙД"], "WILD": ["ВАЙЛД"], "FOX": ["ФОКС"], "DEEP": ["ДИП"], "SNOWTRAC": ["СНОУТРАК"], "TRACK": ["ТРЕК"],
    "WINTRAC": ["ВИНТРАК"], "POLAR": ["ПОЛЯР", "ПОЛАР"], "NORTH": ["НОРТ"], "TAIGA": ["ТАЙГА"], "KAMA": ["КАМА"],
    "FLAME": ["ФЛЕЙМ"], "EURO": ["ЕВРО"], "EUROPE": ["ЕВРОПА"], "ASIA": ["АЗИЯ"], "ALPIN": ["АЛЬПИН"], "ALPINE": ["АЛЬПАЙН"],
    "SAVER": ["СЕЙВЕР"], "PILOT": ["ПИЛОТ"], "PRIMACY": ["ПРАЙМАСИ"], "LATITUDE": ["ЛАТИТЬЮД", "ЛАТИТЬЮДЕ"], "AGILIS": ["АЖИЛИС"],
    "ARCTICA": ["АРКТИКА"], "CARRIER": ["КЕРРИЕР"], "CAMPING": ["КЕМПИНГ"], "ADVENTURE": ["АДВЕНЧЕР", "ЭДВЕНЧЕР"],
}

_REVERSE: Dict[str, str] = {}
for _english, _russian_list in ENGLISH_TO_RUSSIAN.items():
    for _russian in _russian_list:
        _REVERSE.setdefault(_russian, _english)

_SPLIT = re.compile(r"[\s\-_/]+")


def translate_words(value: str) -> str:
    """Known Russian words of a model name turned into the English ones; everything else is left as it is."""
    words = [word for word in _SPLIT.split(value.strip()) if word]
    out = []
    for word in words:
        key = word.upper().replace("Ё", "Е")
        out.append(_REVERSE.get(key) or _REVERSE.get(word.upper()) or word)
    return " ".join(out)


# Well-known tire brands in Russian letters. A hit is safe to apply: the list is explicit, nothing is guessed.
BRANDS_RU_TO_EN: Dict[str, str] = {}
for _english, _russian_list in {
    "MICHELIN": ["МИШЛЕН", "МИШЕЛИН", "МИШЕЛЕН"], "NOKIAN": ["НОКИАН"], "CONTINENTAL": ["КОНТИНЕНТАЛЬ", "КОНТИНЕНТАЛ"],
    "BRIDGESTONE": ["БРИДЖСТОУН", "БРИДЖСТОН", "БРИДЖСТОУНЕ"], "GOODYEAR": ["ГУДИЕР", "ГУДЬЕР", "ГУДИР"], "PIRELLI": ["ПИРЕЛЛИ", "ПИРЕЛИ"],
    "YOKOHAMA": ["ЙОКОГАМА", "ЙОКОХАМА", "ЮКОХАМА"], "DUNLOP": ["ДАНЛОП", "ДУНЛОП"], "HANKOOK": ["ХАНКУК", "ХАНКООК"],
    "KUMHO": ["КУМХО"], "TOYO": ["ТОЙО"], "NEXEN": ["НЕКСЕН"], "MATADOR": ["МАТАДОР"], "CORDIANT": ["КОРДИАНТ"],
    "BARUM": ["БАРУМ"], "GISLAVED": ["ГИСЛАВЕД"], "FALKEN": ["ФАЛКЕН"], "MAXXIS": ["МАКСИС"], "KLEBER": ["КЛЕБЕР"],
    "SAVA": ["САВА"], "TIGAR": ["ТИГАР"], "LAUFENN": ["ЛАУФЕНН", "ЛАУФЕН"], "IKON": ["ИКОН"], "VIATTI": ["ВИАТТИ"],
    "ARMSTRONG": ["АРМСТРОНГ"], "ROADSTONE": ["РОУДСТОУН", "РОАДСТОН"], "LANDSAIL": ["ЛЭНДСЕЙЛ", "ЛАНДСЕЙЛ"],
    "TRIANGLE": ["ТРИАНГЛ", "ТРАЯНГЛ"], "KAPSEN": ["КАПСЕН"], "SAILUN": ["САЙЛУН"], "WESTLAKE": ["ВЕСТЛЕЙК"], "GOODRIDE": ["ГУДРАЙД"],
    "ARIVO": ["АРИВО"], "NITTO": ["НИТТО"], "FEDERAL": ["ФЕДЕРАЛ"], "GENERAL": ["ДЖЕНЕРАЛ"], "SEMPERIT": ["ЗЕМПЕРИТ", "СЕМПЕРИТ"],
    "FULDA": ["ФУЛДА"], "UNIROYAL": ["ЮНИРОЯЛ"], "DEBICA": ["ДЕБИЦА"], "KORMORAN": ["КОРМОРАН"], "BFGOODRICH": ["БФГУДРИЧ"],
}.items():
    for _russian in _russian_list:
        BRANDS_RU_TO_EN.setdefault(_russian, _english)
