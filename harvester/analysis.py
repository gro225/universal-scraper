"""Разбор темы: подтемы корпуса и их названия.

Зачем. После прогона лежит тысяча документов, и первый вопрос человека — не
«покажи список», а «что тут вообще есть». Оценка релевантности отвечает
«насколько это по теме», но не отвечает «из чего тема состоит».

Кластеры отвечают. На реальном корпусе по `Sol Lemon` разбиение сразу показало
структуру: группа про музыкальное издание, группа про чистящее средство Pine-Sol,
группа про браслеты, группа про местные новости. Целую группу видно одним взглядом,
и её можно целиком принять или отбросить — это дешевле, чем перебирать документы.

Почему свой алгоритм, а не sklearn. Нужен ровно один метод — агломеративная
кластеризация со средней связью по косинусу. sklearn тянет за собой scipy, вместе
145 МБ в образ. Здесь это 60 строк на numpy, который уже есть.

Почему средняя связь и порог, а не k-means. Число подтем заранее неизвестно —
его и хотим узнать. Порог по расстоянию даёт ответ сам; k-means требует k на вход.
"""

from __future__ import annotations

import collections
import logging
import re
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger("harvester.analysis")

# Матрица расстояний квадратична по памяти: 5000 документов это ~200 МБ.
MAX_DOCUMENTS = 5000

# Косинусное расстояние, при котором документы ещё считаются одной подтемой.
# 0.75 подобрано на корпусе sol-lemon: ниже — дробится на почти-дубли,
# выше — музыка слипается с бытовой химией.
DEFAULT_THRESHOLD = 0.75

_WORD = re.compile(r"[^\W\d_]{4,}", re.UNICODE)

# Служебные слова и интерфейсный мусор, которые иначе лезут в названия подтем
STOPWORDS = frozenset("""
this that with from your have will been they their there what when which while
more most other some such only also than then them these those into over under
about after before between because during through would could should
более которые которых этого этом этой также когда чтобы может можно если
было были быть есть один одна весь вся все всех для как над под при про что
чем это этот эти тот той так там уже очень наши ваши
cookie cookies privacy policy terms accept settings browser javascript enable
subscribe newsletter sign login account search menu home page click here read
""".split())  # noqa: SIM905 — многострочный список правится легче литерала


@dataclass
class Topic:
    """Подтема: группа близких по смыслу документов."""

    members: list[int]                       # индексы в исходном массиве
    label: str = ""
    keywords: list[str] = field(default_factory=list)
    centroid: np.ndarray | None = None
    representative: int | None = None        # ближайший к центру документ

    @property
    def size(self) -> int:
        return len(self.members)


# --------------------------------------------------------------- кластеризация


def cluster(vectors: np.ndarray, *, threshold: float = DEFAULT_THRESHOLD) -> list[list[int]]:
    """Агломеративная кластеризация со средней связью по косинусу.

    Возвращает списки индексов. Шума нет: каждый документ попадает в группу,
    пусть и в одиночную — потерять документ хуже, чем показать группу из одного.
    """
    n = len(vectors)
    if n == 0:
        return []
    if n == 1:
        return [[0]]

    # векторы уже нормированы -> косинусное расстояние = 1 - скалярное произведение
    dist = 1.0 - (vectors @ vectors.T)
    np.fill_diagonal(dist, np.inf)

    clusters: dict[int, list[int]] = {i: [i] for i in range(n)}
    active = np.ones(n, dtype=bool)

    while active.sum() > 1:
        # ближайшая пара среди живых
        masked = np.where(active[:, None] & active[None, :], dist, np.inf)
        i, j = np.unravel_index(np.argmin(masked), masked.shape)
        if masked[i, j] > threshold:
            break

        # формула Ланса-Уильямса для средней связи
        ni, nj = len(clusters[i]), len(clusters[j])
        merged = (ni * dist[i, :] + nj * dist[j, :]) / (ni + nj)
        merged[i] = merged[j] = np.inf

        dist[i, :] = merged
        dist[:, i] = merged
        dist[i, i] = np.inf

        clusters[i].extend(clusters[j])
        del clusters[j]
        active[j] = False

    return sorted(clusters.values(), key=len, reverse=True)


def describe(
    members: list[int], vectors: np.ndarray, texts: list[str], *, top_k: int = 6
) -> Topic:
    """Центр, представитель и ключевые слова подтемы."""
    centroid = vectors[members].mean(axis=0)
    norm = np.linalg.norm(centroid)
    if norm:
        centroid = centroid / norm
    # представитель — ближайший к центру: он читается как «вот про это группа»
    representative = members[int(np.argmax(vectors[members] @ centroid))]
    return Topic(members=members, centroid=centroid, representative=representative)


def label_topics(topics: list[Topic], texts: list[str], *, top_k: int = 6) -> None:
    """Называет подтемы словами, частыми внутри и редкими снаружи.

    Простая TF-IDF по документам: берём слова, встречающиеся минимум в четверти
    документов группы, и взвешиваем их редкостью в остальном корпусе.
    """
    total = len(texts)
    doc_words = [
        {w for w in _WORD.findall(t.lower()) if w not in STOPWORDS} for t in texts
    ]
    corpus = collections.Counter()
    for words in doc_words:
        corpus.update(words)

    for topic in topics:
        inside = collections.Counter()
        for i in topic.members:
            inside.update(doc_words[i])

        floor = max(2, round(topic.size * 0.25))
        scored = [
            (count / topic.size * np.log(total / (corpus[word] + 1) + 1), word)
            for word, count in inside.items()
            if count >= floor
        ]
        topic.keywords = [w for _, w in sorted(scored, reverse=True)[:top_k]]
        topic.label = ", ".join(topic.keywords) or "без выраженных слов"


def analyze(
    vectors: np.ndarray, texts: list[str], *, threshold: float = DEFAULT_THRESHOLD
) -> list[Topic]:
    """Полный разбор: кластеры -> центры -> названия."""
    if len(vectors) > MAX_DOCUMENTS:
        raise ValueError(
            f"слишком много документов для кластеризации: {len(vectors)} > {MAX_DOCUMENTS}"
        )
    groups = cluster(vectors, threshold=threshold)
    topics = [describe(g, vectors, texts) for g in groups]
    label_topics(topics, texts)
    return topics
