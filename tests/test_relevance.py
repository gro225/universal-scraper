"""Оценка релевантности: якоря из DSL и арифметика счёта.

Модель тут не грузится — проверяется логика, а не качество эмбеддингов.
Качество проверено экспериментом на 477 реальных документах (см. USAGE.md).
"""

import numpy as np

from harvester.embeddings import Anchors, anchor_texts, document_text
from harvester.models import Task


def _task(**query) -> Task:
    return Task.model_validate({"name": "t", "query": query})


# ------------------------------------------------------------------ якоря


def test_positive_anchor_collects_subject_intent_and_hints():
    pos, neg = anchor_texts(_task(subject="Sol Lemon", intent="кто это",
                                  include_hints=["music", "band"]))
    assert "Sol Lemon" in pos and "кто это" in pos and "music" in pos and "band" in pos
    assert neg is None


def test_negative_anchor_comes_from_exclude_hints():
    _, neg = anchor_texts(_task(subject="Sol Lemon",
                                exclude_hints=["чистящее средство", "украшения"]))
    assert neg is not None
    assert "чистящее средство" in neg and "украшения" in neg


def test_blank_hints_are_ignored():
    pos, neg = anchor_texts(_task(subject="тема", include_hints=["  ", ""],
                                  exclude_hints=["  "]))
    assert pos == "тема"
    assert neg is None


def test_no_subject_means_nothing_to_score_against():
    """Точечный захват: адреса названы явно, темы нет — оценивать не от чего."""
    task = Task.for_urls(["https://example.com"])
    pos, neg = anchor_texts(task)
    assert pos == ""
    assert task.quality.embed is False


def test_subject_with_urls_still_scores():
    task = Task.for_urls(["https://example.com"], subject="Sol Lemon")
    assert task.quality.embed is True
    assert anchor_texts(task)[0].startswith("Sol Lemon")


# ------------------------------------------------------------------ счёт


def _unit(*xs) -> np.ndarray:
    v = np.array(xs, dtype=float)
    return v / np.linalg.norm(v)


def test_score_without_negative_is_plain_cosine():
    anchors = Anchors(_unit(1, 0), None)
    assert anchors.has_negative is False
    assert anchors.score(_unit(1, 0)) == 1.0
    assert abs(anchors.score(_unit(0, 1))) < 1e-9


def test_negative_anchor_pushes_down_what_resembles_it():
    """Главный механизм: омоним близок к теме, но ещё ближе к антипримеру."""
    topic, junk = _unit(1, 0), _unit(0, 1)
    anchors = Anchors(topic, junk)
    homonym = _unit(1, 2)          # похож на оба, но на мусор сильнее

    assert anchors.score(topic) > 0
    assert anchors.score(homonym) < 0
    # без антипримера тот же документ выглядел бы релевантным
    assert Anchors(topic, None).score(homonym) > 0


def test_score_is_symmetric_around_zero():
    anchors = Anchors(_unit(1, 0), _unit(0, 1))
    assert abs(anchors.score(_unit(1, 1))) < 1e-9


# ------------------------------------------------------------------ текст


def test_document_text_joins_title_and_body_and_truncates():
    out = document_text("Заголовок", "тело " * 2000)
    assert out.startswith("Заголовок. тело")
    assert len(out) <= 1200


def test_document_text_survives_missing_title():
    assert document_text(None, "только тело").strip().startswith(". только тело")
