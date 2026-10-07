"""Задача «обойти конкретные адреса» — без темы, поиска и YAML."""

import pytest
from pydantic import ValidationError

from harvester.models import Task


def test_single_page_does_not_follow_links():
    task = Task.for_urls(["https://example.com/article"])

    assert task.discovery.seeds == ["https://example.com/article"]
    assert task.discovery.connectors == ["generic_web"]   # поисковик не нужен
    assert task.crawl.scope.follow == "none"
    assert task.crawl.scope.max_depth == 0


def test_bare_domain_gets_https():
    task = Task.for_urls(["example.com/blog", " example.org "])
    assert task.discovery.seeds == ["https://example.com/blog", "https://example.org"]


def test_depth_stays_within_seed_domains_by_default():
    task = Task.for_urls(["https://example.com"], depth=2)
    assert task.crawl.scope.follow == "seed_domain"
    assert task.crawl.scope.max_depth == 2


def test_everywhere_allows_outlinks():
    task = Task.for_urls(["https://example.com"], depth=1, same_domain=False)
    assert task.crawl.scope.follow == "seed_domain_plus_outlinks"


def test_named_page_is_saved_even_if_text_is_short():
    """Адрес назвал человек — не нам решать, что страница «слишком короткая»."""
    assert Task.for_urls(["https://example.com"]).extract.min_text_chars == 0


def test_media_can_be_disabled():
    assert Task.for_urls(["https://e.com"], media=False).extract.media.download is False


def test_autoname_is_readable_and_unique_per_url_set():
    a = Task.for_urls(["https://www.example.com/a"]).name
    b = Task.for_urls(["https://www.example.com/b"]).name
    multi = Task.for_urls(["https://example.com/a", "https://example.com/b"]).name

    assert a.startswith("grab-example-com-")
    assert a != b                  # разные адреса — разные задачи
    assert "+1" in multi           # видно, что адресов несколько


def test_explicit_name_wins():
    assert Task.for_urls(["https://e.com"], name="мой-захват").name == "мой-захват"


def test_empty_urls_rejected():
    with pytest.raises(ValueError):
        Task.for_urls([])
    with pytest.raises(ValueError):
        Task.for_urls(["", "   "])


def test_task_needs_subject_or_seeds():
    with pytest.raises(ValidationError, match="query.subject"):
        Task.model_validate({"name": "пусто", "query": {}})


def test_seeds_alone_are_enough():
    """Раньше subject был обязателен — для точечного захвата это мешало."""
    task = Task.model_validate(
        {"name": "urls-only", "discovery": {"seeds": ["https://example.com"]}}
    )
    assert task.query.subject is None


def test_subject_alone_is_still_enough():
    task = Task.model_validate({"name": "тема", "query": {"subject": "Sol Lemon"}})
    assert task.discovery.seeds == []
