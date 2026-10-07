import pathlib

import pytest
from pydantic import ValidationError

from harvester.connectors.searxng import build_queries
from harvester.models import Task

TASKS_DIR = pathlib.Path(__file__).resolve().parent.parent / "tasks"


def test_reference_task_parses():
    task = Task.from_yaml((TASKS_DIR / "sol-lemon.yaml").read_text())

    assert task.name == "sol-lemon"
    assert task.query.subject == "Sol Lemon"
    assert task.crawl.scope.follow == "seed_domain_plus_outlinks"
    assert task.extract.media.download is True


def test_defaults_fill_in_for_minimal_task():
    task = Task.model_validate({"name": "minimal", "query": {"subject": "X"}})

    assert task.discovery.connectors == ["searxng"]
    assert task.crawl.politeness.respect_robots is True
    assert task.crawl.budget.max_pages == 2000


def test_query_expansion_quotes_and_dedups():
    task = Task.model_validate(
        {
            "name": "q",
            "query": {"subject": "Sol Lemon", "include_hints": ["music", "music"]},
            "discovery": {"extra_queries": ['"Sol Lemon" bandcamp']},
        }
    )
    queries = build_queries(task)

    assert queries[0] == '"Sol Lemon"'
    assert '"Sol Lemon" music' in queries
    assert '"Sol Lemon" bandcamp' in queries
    assert len(queries) == len({q.lower() for q in queries})


def test_invalid_follow_mode_rejected():
    with pytest.raises(ValidationError):
        Task.model_validate(
            {"name": "bad", "query": {"subject": "X"},
             "crawl": {"scope": {"follow": "everything"}}}
        )
