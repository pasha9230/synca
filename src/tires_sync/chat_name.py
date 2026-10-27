"""The client's own name from the words they wrote in an Avito chat (the owner 2026-10-27): the profile name is often a nick («DM»).

Only the client's incoming messages are read; our messages are looked at only to see that we asked for the name («как к вам обращаться?»).
Taken: «меня зовут …», «моё имя …», «имя: …», «ФИО: …», an answer right after our question about the name, and a message that is just a
name beside a phone («Иван 8 900 …», «Иван Петров, +7…»). Anything else, and everything on the stop-list (greetings, shop words, brands,
cities, streets), is not a name."""
from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

MAX_NAME = 60
WORD = r"[A-Za-zА-Яа-яЁё]+(?:-[A-Za-zА-Яа-яЁё]+)?"
ONLY_LETTERS = re.compile(r"^%s$" % WORD)
NAME_WORDS = re.compile(r"\s*(%s)" % WORD)
PHONE_LIKE = re.compile(r"(?<!\d)(?:[+]?[78][\s\-()]*)?\(?\d{3}\)?[\s\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
EXPLICIT = re.compile(r"(?:меня\s+зовут|мо[её]\s+имя(?:\s+будет)?|зовут\s+меня|(?:^|[\s,.;!?])(?:имя\s*[:\-–—]|(?:ф\.?\s?и\.?\s?о\.?|фио)(?![а-яёa-z])\s*[:\-–—]?))\s*(?:[:\-–—]\s*)?", re.I)
THIS_IS = re.compile(r"^\s*(?:это|я|зовут|меня)\s+", re.I)
ASKED = re.compile(r"как\s+(?:к\s+вам|вас|вам|к\s+Вам|Вас|Вам)\s+(?:обращаться|зовут|называть)|ваше\s+имя|как\s+вас\s+зовут|представьтесь|назовите\s+(?:ваше\s+)?имя|на\s+какое\s+имя|имя\s+(?:и\s+)?(?:телефон|номер)", re.I)
LABELS = {"тел", "телефон", "номер", "мобильный", "моб", "мой", "моя", "звоните", "звонить", "контакт", "контакты", "whatsapp", "ватсап", "вотсап", "тг", "wa"}

STOP = frozenset("""
здравствуйте здравствуй здраствуйте здрасте привет приветствую добрый добрая доброе добрых день вечер утро ночи доброго утра дня вечера
спасибо благодарю пожалуйста пожалуста извините простите ок окей хорошо ладно договорились отлично понятно ясно ага угу
да нет не есть нету нужно нужны нужен надо хочу хотим купить куплю возьму беру заберу заберём заберем подскажите скажите подскажи
можно можете могу будет буду есть наличии наличие цена цену цены стоимость сколько почем резина резину шины шину шин колеса колесо диски диск
комплект комплекта штуки штуки шт руб рублей рубли адрес адреса точка точку магазин магазина склад доставка доставку самовывоз
актуально актуальна актуален еще ещё объявление объявления товар товара заказ заказа заказать оплата оплату оплатить наличными картой
зима зимняя зимние зимней лето летняя летние летней всесезонка всесезонные шипы шипованная шипованные липучка
москва питер петербург санкт-петербург спб мск область ленинградская ленобласть всеволожск гатчина колпино кудрово мурино
улица ул проспект пр шоссе переулок дом корпус квартира кв метро юнона руставели янино казакова маршала голландская
ikon nokian michelin bridgestone continental pirelli goodyear dunlop yokohama hankook kumho cordiant viatti toyo nexen gislaved
maxxis kama кама нокиан мишлен бриджстоун континенталь пирелли гудйир данлоп йокогама ханкук кумхо кордиант виатти тойо нексен
hello hi thanks thank yes no ok okay
муж жена сын дочь брат сестра отец мама папа мастер водитель менеджер сосед бог богу
указано указан указана так же любое любой неважно профиле профиль
""".split())


FIRST_NAMES = frozenset("""
александр алексей александра алина алиса алла алёна алена анатолий андрей анастасия анна
антон антонина аркадий арсений артём артем артур богдан борис вадим валентин валентина
валерий валерия варвара василий вера вероника виктор виктория виталий владимир владислав вячеслав
галина геннадий георгий глеб григорий давид даниил данил данила дарья денис дмитрий
евгений евгения екатерина елена елизавета егор ефим жанна зинаида злата зоя иван
игорь илья инна ирина карина кирилл клавдия константин кристина ксения лариса лев
леонид лидия лилия любовь людмила максим маргарита марина мария марк матвей михаил
надежда наталья наталия никита николай нина олег олеся ольга оксана павел пётр
петр полина раиса роман ростислав руслан светлана святослав сергей софия софья станислав
степан таисия тамара татьяна тимофей тимур тихон ульяна фёдор федор филипп эдуард
элина эльвира юлия юрий яков ярослав ян янина саша алекс леша лёша
андрюха андрюша аня анечка антоша артемий вася витя вова володя гоша гриша
даша дима женя жора зина игорёк катя кирюша коля костя лена леночка
лёня лера лиза лика люда макс маша миша надя наташа ника оля
олечка паша петя рома серёжа сережа соня стас стёпа таня тома толя
тоня ира юра юля юлечка слава света сеня сёма семён семен тоша
фома эльдар рустам ренат ильдар ильнур айрат азамат шамиль мурат марат ахмед
ислам руфина альбина диана дина римма регина рената амина аида азиза гульнара
ляйсан камилла камила милана мила майя марта ангелина анжела алевтина агата аделина
арина вика виолетта влада эмилия ева кира лана лия мирослава василиса зарина
нелли нонна инесса уля шура ваня вадик владик гена эльза эрик эмиль
альберт арсен арман ашот гарик гайк армен демид захар зиновий игнат иннокентий
ираклий кузьма лука макар мирон назар нестор никифор осип прохор радик савелий
самуил тарас трофим устин фаддей харитон эльмир
 влад настя ксюша саня серёга серега тёма тема даня валя галя люба эдик магомед али тигран карен самвел рашид алишер фарид рамиль роберт рафаэль""".split())
_FOLDED = frozenset(n.replace("ё", "е") for n in FIRST_NAMES)


def _is_first_name(word: str) -> bool:
    return word.lower().replace("ё", "е") in _FOLDED


def _clean(words: Sequence[str]) -> Optional[str]:
    """The words as a name, or None: letters only, one to three words, none of them on the stop-list, at most 60 characters."""
    if not 1 <= len(words) <= 3:
        return None
    parts = []
    for word in words:
        if not ONLY_LETTERS.match(word) or len(word.replace("-", "")) < 2 or word.lower() in STOP or word.lower() in LABELS:
            return None
        if any(piece.lower() in STOP for piece in word.split("-")):
            return None
        parts.append("-".join(piece[:1].upper() + piece[1:].lower() for piece in word.split("-")))
    name = " ".join(parts)
    return name if len(name) <= MAX_NAME else None


def _words_after(text: str, start: int) -> List[str]:
    """Up to three words that follow `start`, cut at the first sign that is not a name: a digit, a stop word, a lower-case second word."""
    out, pos = [], start
    while len(out) < 3:
        found = NAME_WORDS.match(text, pos)
        if not found:
            break
        word = found.group(1)
        if out and (not word[:1].isupper() or word.lower() in STOP or len(word) < 2):
            break
        out.append(word)
        pos = found.end()
        if text[pos:pos + 1] in ".,;:!?\n(":                       # a name ends at the punctuation; «Иван Петров» are two words, «Иван, шины» one
            break
    while out and _clean(out) is None:                                # a stop word inside: keep the clean beginning only
        out.pop()
    return out


def _explicit(text: str) -> Optional[str]:
    for found in EXPLICIT.finditer(text):
        words = _words_after(text, found.end())
        name = _clean(words)
        if name and (words[0][:1].isupper() or _is_first_name(words[0].split("-")[0])):      # «имя - любое»: a lower-case word is a name only if known
            return name
    return None


def _bare(text: str, after_question: bool) -> Optional[str]:
    """A message that is only a name (after our question, with «это»/«я» allowed), or only a name beside a phone."""
    rest = text
    phone = PHONE_LIKE.search(rest)
    if phone is not None:
        rest = rest[:phone.start()] + " " + rest[phone.end():]
    elif not after_question:
        return None
    rest = THIS_IS.sub("", rest.lstrip(), count=1)                     # «это Иван», «я Ольга»
    words = re.findall(r"[^\s,;:.!?()+\-–—/|]+(?:-[^\s,;:.!?()+\-–—/|]+)*", rest)
    words = [w for w in words if w.lower() not in LABELS]
    if not words or len(words) > 2 or re.search(r"\d", rest):
        return None
    if len(words) == 2 and not all(w[:1].isupper() for w in words):
        return None
    if len(words) == 1 and not (words[0][:1].isupper() or after_question):
        return None
    if not any(_is_first_name(w.split('-')[0]) for w in words):             # no marker, so only a known first name is a name
        return None
    return _clean(words)


def name_from_messages(messages: Sequence[Tuple[str, str]]) -> str:
    """The name the client wrote: `messages` are (direction «in»/«out», text), the oldest first. A statement with a marker («меня зовут»,
    «имя:») beats a guess from the shape of a message (a name beside a phone, an answer to our question); the latest wins inside each; "" when none."""
    for index in range(len(messages) - 1, -1, -1):
        direction, text = messages[index]
        if direction == "in" and str(text or "").strip():
            name = _explicit(str(text))
            if name:
                return name
    for index in range(len(messages) - 1, -1, -1):
        direction, text = messages[index]
        text = str(text or "")
        if direction != "in" or not text.strip():
            continue
        previous = next(((d, t) for d, t in reversed(messages[:index]) if t), None)
        asked = previous is not None and previous[0] == "out" and bool(ASKED.search(str(previous[1])))
        name = _bare(text, asked)
        if name:
            return name
    return ""
