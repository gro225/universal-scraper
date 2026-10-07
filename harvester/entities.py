"""Действующие лица: кто и что упоминается в собранном.

Подтемы отвечают «из чего состоит корпус», сущности — «кто здесь действует».

Два вывода из экспериментов на корпусе sol-lemon, определивших устройство модуля.

**Первый: извлекать надо внутри подтем, а не по всему корпусу.** По всему корпусу верх
списка занимают «News», «Halloween», «Facebook» — сущности тонут в мусоре, которого
в корпусе большинство. Внутри релевантных подтем сразу всплывают настоящие:
Lemon Sol, North Jersey Blues Society, Fontaines D.C.

**Второй: без модели не обойтись.** Извлечение заглавных словосочетаний с той же
TF-IDF, что работает для названий подтем, даёт «All», «Now», «The», «Every» — артикли
и начала предложений. spaCy хотя бы отличает имя от определителя. Поэтому 71 МБ
за `xx_ent_wiki_sm` оправданы, но вынесены в extra `[entities]`.

Остаточный шум — интерфейсные фразы («Log In», «Buy Tickets», «Авторские права»).
Их снимает словарь UI_WORDS и требование встретиться минимум в двух документах.
"""

from __future__ import annotations

import collections
import logging
import math
import re
import threading
from dataclasses import dataclass

log = logging.getLogger("harvester.entities")

_nlp = None
_lock = threading.Lock()

MODEL = "xx_ent_wiki_sm"
MAX_CHARS = 1500            # хвост страницы — это футер, сущностей там нет
KEEP_TYPES = ("PER", "ORG", "LOC", "MISC")

# Слова, выдающие интерфейс, а не действующее лицо
UI_WORDS = frozenset("""
log sign buy subscribe follow share read contact privacy terms cookie cookies
menu search home about more view click download accept settings newsletter
login register cart checkout password email address copyright reserved
прессе авторские авторам рекламодателям разработчикам условия политика
подписаться войти регистрация корзина оформить контакты реклама
latest features interviews trending popular recommended playlist archive
связаться тестирование подробнее показать ещё далее сообщить
правила конфиденциальность безопасность помощь справка настройки
copyright llc inc ltd gmbh рекламa соглашение лицензия
""".split())  # noqa: SIM905 — многострочный список правится легче литерала

_CONTRACTION = re.compile(r"[’']\w{1,2}$")
# «playingLemon Sol» — склейка из-за потерянного пробела в разметке
_GLUED = re.compile(r"[a-zа-яё][A-ZА-ЯЁ]")


class EntitiesUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Mention:
    name: str
    type: str


def nlp():
    """Ленивая загрузка модели — она весит 39 МБ."""
    global _nlp
    if _nlp is None:
        with _lock:
            if _nlp is None:
                try:
                    import spacy
                except ImportError as exc:  # pragma: no cover
                    raise EntitiesUnavailable(
                        "нужен spacy:  pip install -e '.[entities]' "
                        "&& python -m spacy download xx_ent_wiki_sm"
                    ) from exc
                try:
                    log.info("загружаю модель сущностей %s", MODEL)
                    _nlp = spacy.load(MODEL, disable=["lemmatizer", "tagger"])
                except OSError as exc:
                    raise EntitiesUnavailable(
                        f"модель {MODEL} не установлена: "
                        f"python -m spacy download {MODEL}"
                    ) from exc
    return _nlp


def is_noise(name: str) -> bool:
    """Отсев интерфейсных фраз и огрызков."""
    name = name.strip()
    if not (3 < len(name) < 50):
        return True
    tokens = name.split()
    if len(tokens) > 5:
        return True
    if any(t.lower().strip(".,:") in UI_WORDS for t in tokens):
        return True
    if len(tokens) == 1 and _CONTRACTION.search(name):   # We've, It's
        return True
    if _GLUED.search(name):                              # playingLemon Sol
        return True
    return not any(ch.isalpha() for ch in name)


def extract(texts: list[str]) -> list[set[Mention]]:
    """Сущности по документам. Повторы внутри документа схлопнуты."""
    if not texts:
        return []
    out: list[set[Mention]] = []
    for doc in nlp().pipe([t[:MAX_CHARS] for t in texts], batch_size=32):
        found = set()
        for ent in doc.ents:
            if ent.label_ not in KEEP_TYPES:
                continue
            name = " ".join(ent.text.split())      # переносы строк внутри сущности
            if is_noise(name):
                continue
            found.add(Mention(name, ent.label_))
        out.append(found)
    return out


def rank(
    groups: list[list[int]], mentions: list[set[Mention]], *, min_documents: int = 2
) -> dict[int, list[tuple[Mention, int, float]]]:
    """Для каждой группы документов — её сущности с весом.

    Вес по той же логике, что названия подтем: часто внутри группы, редко в корпусе.
    Сущность, встреченная в одном документе, это обычно опечатка разметки — отсекаем.
    """
    corpus = collections.Counter()
    for found in mentions:
        corpus.update(found)
    total = max(len(mentions), 1)

    ranked: dict[int, list[tuple[Mention, int, float]]] = {}
    for gi, members in enumerate(groups):
        inside = collections.Counter()
        for i in members:
            inside.update(mentions[i])
        n = max(len(members), 1)
        scored = [
            (m, count, count / n * math.log(total / corpus[m] + 1))
            for m, count in inside.items()
            if count >= min_documents
        ]
        ranked[gi] = sorted(scored, key=lambda x: x[2], reverse=True)
    return ranked
