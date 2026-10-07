"""Эмбеддинги и оценка релевантности.

Зачем это нужно. Прогон по теме приносит омонимы: на `Sol Lemon` половина выдачи —
чистящее средство Pine-Sol Lemon Fresh. Без оценки такой мусор ложится в хранилище
наравне с находками.

Как считается оценка. Эксперимент на 60 реальных документах того прогона показал
решающую вещь: **косинуса к одной лишь теме не хватает**. Pine-Sol лексически близок
к «Sol Lemon» и обгоняет настоящие находки — и по заголовку, и по полному тексту.
Работает только разность двух якорей:

    relevance = cos(документ, что ищем) - cos(документ, что НЕ ищем)

С вычитанием антипримера верх выдачи занимают группы и музжурналы, низ — Clorox,
Liquid-Plumr и Walmart. Антипример берётся из `query.exclude_hints`, который до
этого в DSL лежал без дела.

Оценка **не удаляет** документ. Она пишется в `documents.relevance`, а порог
применяется при чтении — значит его можно менять задним числом, ничего не потеряв.

Модель — ONNX через fastembed, а не sentence-transformers: torch на 8 ГБ памяти
не ставится, а 384-мерная multilingual-MiniLM весит 0.22 ГБ и знает русский.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING

import numpy as np

from .config import settings

if TYPE_CHECKING:
    from .models import Task

log = logging.getLogger("harvester.embeddings")

_model = None
_model_lock = threading.Lock()

# сколько символов документа отдаём модели: у MiniLM окно 128 токенов,
# дальше всё равно обрежется, а лишний текст только замедляет
MAX_CHARS = 1200


class EmbeddingsUnavailable(RuntimeError):
    pass


def model():
    """Ленивая загрузка: без неё импорт пакета тянул бы 0.22 ГБ модели."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                try:
                    from fastembed import TextEmbedding
                except ImportError as exc:  # pragma: no cover
                    raise EmbeddingsUnavailable(
                        "нужен fastembed:  pip install -e '.[embed]'"
                    ) from exc
                s = settings()
                log.info("загружаю модель эмбеддингов %s", s.embed_model)
                _model = TextEmbedding(s.embed_model, cache_dir=s.embed_cache_dir)
    return _model


def embed(texts: list[str]) -> list[np.ndarray]:
    """Нормированные векторы. Синхронно и CPU-bound — звать через embed_async."""
    if not texts:
        return []
    out = []
    for vec in model().embed([t[:MAX_CHARS] for t in texts]):
        norm = np.linalg.norm(vec)
        out.append(vec / norm if norm else vec)
    return out


async def embed_async(texts: list[str]) -> list[np.ndarray]:
    """ONNX считает на CPU и держал бы event loop — уводим в поток."""
    return await asyncio.to_thread(embed, texts)


# ------------------------------------------------------------------- якоря задачи


class Anchors:
    """Что ищем и что точно не ищем — в векторном виде."""

    def __init__(self, positive: np.ndarray, negative: np.ndarray | None):
        self.positive = positive
        self.negative = negative

    @property
    def has_negative(self) -> bool:
        return self.negative is not None

    def score(self, vec: np.ndarray) -> float:
        """Разность косинусов. Без антипримера — обычный косинус (слабее на омонимах)."""
        value = float(self.positive @ vec)
        if self.negative is not None:
            value -= float(self.negative @ vec)
        return value


def anchor_texts(task: Task) -> tuple[str, str | None]:
    """Собирает тексты якорей из DSL. Вынесено отдельно — так их видно в тестах."""
    q = task.query
    if not (q.subject or "").strip():
        # Тему не задавали (точечный захват) — оценивать не от чего.
        # Проверяем именно subject: `intent` у grab заполнен служебной строкой,
        # и якорь из него был бы бессмысленным.
        return "", None

    positive_parts = [q.subject, q.intent or "", *q.include_hints]
    positive = ", ".join(p.strip() for p in positive_parts if p and p.strip())

    negative = ", ".join(h.strip() for h in q.exclude_hints if h.strip()) or None
    return positive, negative


async def anchors_for(task: Task) -> Anchors | None:
    """None — если у задачи нет темы (точечный захват): оценивать нечего."""
    positive, negative = anchor_texts(task)
    if not positive:
        return None
    texts = [positive] + ([negative] if negative else [])
    vectors = await embed_async(texts)
    return Anchors(vectors[0], vectors[1] if negative else None)


def document_text(title: str | None, text: str) -> str:
    """Что именно отдаём модели от документа."""
    return f"{(title or '').strip()}. {text.strip()}"[:MAX_CHARS]
