"""Разбор темы на подтемы: кластеризация и названия."""

import numpy as np
import pytest

from harvester.analysis import DEFAULT_THRESHOLD, MAX_DOCUMENTS, analyze, cluster, label_topics


def _blobs(n_groups=3, per_group=10, dim=8, spread=0.12, seed=0) -> np.ndarray:
    """Заведомо разделимые облака на нормированной сфере."""
    rng = np.random.default_rng(seed)
    centers = np.eye(n_groups, dim) * 5
    X = np.vstack([c + rng.normal(0, spread, (per_group, dim)) for c in centers])
    return X / np.linalg.norm(X, axis=1, keepdims=True)


# ------------------------------------------------------------- кластеризация


def test_finds_the_number_of_groups_itself():
    """Число подтем заранее неизвестно — его и хотим узнать."""
    groups = cluster(_blobs(n_groups=3), threshold=DEFAULT_THRESHOLD)
    assert len(groups) == 3
    assert sorted(len(g) for g in groups) == [10, 10, 10]


def test_every_document_lands_somewhere():
    """Шума нет: потерять документ хуже, чем показать группу из одного."""
    X = _blobs(n_groups=4, per_group=7)
    assigned = sorted(i for g in cluster(X) for i in g)
    assert assigned == list(range(len(X)))


def test_groups_are_disjoint():
    groups = cluster(_blobs())
    flat = [i for g in groups for i in g]
    assert len(flat) == len(set(flat))


def test_higher_threshold_merges_more():
    X = _blobs(n_groups=3)
    assert len(cluster(X, threshold=0.05)) >= len(cluster(X, threshold=1.5))


def test_result_is_sorted_by_size():
    X = np.vstack([_blobs(1, 12, seed=1), _blobs(1, 3, seed=2) + 0.001])
    X = X / np.linalg.norm(X, axis=1, keepdims=True)
    sizes = [len(g) for g in cluster(X)]
    assert sizes == sorted(sizes, reverse=True)


def test_degenerate_inputs():
    assert cluster(np.empty((0, 4))) == []
    assert cluster(np.ones((1, 4))) == [[0]]


# -------------------------------------------------------------- названия


def test_label_picks_words_specific_to_the_group():
    X = _blobs(n_groups=2, per_group=5)
    texts = ["концерт альбом группа музыка"] * 5 + ["чистящее средство уборка бытовая"] * 5
    topics = analyze(X, texts)

    labels = {t.label for t in topics}
    assert any("альбом" in label or "концерт" in label for label in labels)
    assert any("чистящее" in label or "уборка" in label for label in labels)


def test_words_common_to_everything_are_not_a_label():
    """Слово, которое есть у всех, ничего не различает."""
    X = _blobs(n_groups=2, per_group=5)
    texts = ["общее слово альбом"] * 5 + ["общее слово уборка"] * 5
    topics = analyze(X, texts)
    for t in topics:
        assert t.keywords and t.keywords[0] != "общее"


def test_stopwords_and_ui_noise_are_dropped():
    topics = [type("T", (), {"members": [0, 1], "size": 2, "keywords": [], "label": ""})()]
    label_topics(topics, ["cookie policy subscribe альбом", "cookie policy subscribe альбом"])
    assert "cookie" not in topics[0].keywords
    assert "альбом" in topics[0].keywords


def test_representative_is_the_closest_to_the_centre():
    X = _blobs(n_groups=1, per_group=9, spread=0.2, seed=3)
    topic = analyze(X, ["текст"] * 9)[0]
    centroid = X[topic.members].mean(axis=0)
    centroid /= np.linalg.norm(centroid)
    best = topic.members[int(np.argmax(X[topic.members] @ centroid))]
    assert topic.representative == best


def test_refuses_corpora_too_big_for_the_distance_matrix():
    with pytest.raises(ValueError, match="слишком много"):
        analyze(np.zeros((MAX_DOCUMENTS + 1, 4)), [""] * (MAX_DOCUMENTS + 1))
