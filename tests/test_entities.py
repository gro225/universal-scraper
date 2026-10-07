"""Действующие лица: отсев интерфейсного шума и вес сущности в подтеме.

Модель здесь не грузится — проверяется логика отбора, а не качество NER.
Качество проверено на корпусе sol-lemon (см. USAGE.md).
"""

import pytest

from harvester.entities import Mention, is_noise, rank

# ----------------------------------------------------------------- отсев шума


@pytest.mark.parametrize("name", [
    "Log In", "Buy Tickets", "Subscribe to", "Privacy Policy",
    "Авторские права", "Конфиденциальность", "Google LLC",
])
def test_interface_phrases_are_not_actors(name):
    """Навигация и подвал сайта — не действующие лица."""
    assert is_noise(name) is True


@pytest.mark.parametrize("name", [
    "Lemon Sol", "North Jersey Blues Society", "Fontaines D.C.",
    "Audrey Hobert", "Omni 'Verse",
])
def test_real_names_survive(name):
    assert is_noise(name) is False


def test_glued_words_are_rejected():
    """Потерянный пробел в разметке даёт «playingLemon Sol» — это не имя."""
    assert is_noise("playingLemon Sol") is True
    assert is_noise("Lemon Sol") is False


def test_contractions_are_rejected():
    assert is_noise("We've") is True
    assert is_noise("It's") is True


@pytest.mark.parametrize("name", ["ab", "", "   ", "123", "...", "x" * 60])
def test_degenerate_names(name):
    assert is_noise(name) is True


def test_overly_long_phrases_are_rejected():
    assert is_noise("One Two Three Four Five Six") is True


# --------------------------------------------------------------------- вес


def _m(name: str) -> Mention:
    return Mention(name, "PER")


def test_entity_specific_to_a_group_outranks_a_ubiquitous_one():
    """Сущность, которая есть везде, ничего не характеризует."""
    everywhere, only_here = _m("Везде"), _m("Только тут")
    mentions = [
        {everywhere, only_here}, {everywhere, only_here},
        {everywhere}, {everywhere},
    ]
    ranked = rank([[0, 1], [2, 3]], mentions)
    first = [m.name for m, _, _ in ranked[0]]
    assert first[0] == "Только тут"


def test_single_document_mentions_are_dropped():
    """Одно упоминание — обычно огрех разметки, а не действующее лицо."""
    mentions = [{_m("Разовый")}, {_m("Частый")}, {_m("Частый")}]
    ranked = rank([[0, 1, 2]], mentions)
    names = [m.name for m, _, _ in ranked[0]]
    assert names == ["Частый"]


def test_min_documents_is_adjustable():
    mentions = [{_m("Разовый")}, {_m("Частый")}, {_m("Частый")}]
    ranked = rank([[0, 1, 2]], mentions, min_documents=1)
    assert {m.name for m, _, _ in ranked[0]} == {"Разовый", "Частый"}


def test_counts_are_per_group():
    shared = _m("Общий")
    ranked = rank([[0, 1], [2]], [{shared}, {shared}, {shared}])
    assert ranked[0][0][1] == 2          # в первой группе два документа
    assert ranked[1] == []               # во второй один — ниже порога


def test_empty_input_is_safe():
    assert rank([], []) == {}
    assert rank([[]], [set()]) == {0: []}
