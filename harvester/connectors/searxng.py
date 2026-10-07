"""Discovery через self-hosted SearXNG — основная рабочая лошадка Фазы 0.

Без API-ключей и без внешних расходов. Требует, чтобы в settings.yml SearXNG был
включён формат json (см. deploy/searxng/settings.yml).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

from ..config import settings
from ..hashing import canonical_url, registrable_domain
from ..models import SourceRef, Task
from .base import register


class SearxngConnector:
    name = "searxng"

    def matches(self, ref: SourceRef) -> bool:
        return False  # только discovery, страницы не тянет

    async def discover(self, task: Task) -> AsyncIterator[SourceRef]:
        s = settings()
        queries = build_queries(task)
        seen_domains: dict[str, int] = {}
        emitted = 0

        async with httpx.AsyncClient(
            base_url=s.searxng_url,
            timeout=s.fetch_timeout_s,
            headers={"User-Agent": s.user_agent},
        ) as client:
            for query in queries:
                for page in range(1, task.discovery.pages_per_query + 1):
                    if emitted >= task.discovery.max_seeds:
                        return
                    try:
                        resp = await client.get(
                            "/search",
                            params={
                                "q": query,
                                "format": "json",
                                "pageno": page,
                                "language": (task.query.languages or ["en"])[0],
                                "safesearch": 0,
                            },
                        )
                        resp.raise_for_status()
                        results = resp.json().get("results", [])
                    except Exception:
                        break  # поисковик недоступен — не роняем весь discovery

                    if not results:
                        break

                    for rank, item in enumerate(results):
                        url = item.get("url")
                        if not url or not url.startswith("http"):
                            continue
                        dom = registrable_domain(url)
                        # не даём одному домену съесть весь бюджет seed'ов
                        seen_domains[dom] = seen_domains.get(dom, 0) + 1
                        if seen_domains[dom] > 8:
                            continue
                        emitted += 1
                        yield SourceRef(
                            url=canonical_url(url),
                            depth=0,
                            priority=_priority(rank, page),
                            discovered_by=f"searxng:{query}",
                            hint={
                                "title": item.get("title"),
                                "snippet": item.get("content"),
                                "engine": item.get("engine"),
                            },
                        )
                        if emitted >= task.discovery.max_seeds:
                            return

    async def fetch(self, ref: SourceRef):  # pragma: no cover - не используется
        raise NotImplementedError

    def extract(self, raw, task):  # pragma: no cover - не используется
        raise NotImplementedError


def _priority(rank: int, page: int) -> float:
    """Чем выше в выдаче, тем раньше обойдём."""
    return max(0.05, 1.0 - 0.03 * rank - 0.15 * (page - 1))


def build_queries(task: Task) -> list[str]:
    """Расширение запроса.

    Фаза 0 — детерминированные шаблоны. Фаза 2 заменит это на LLM-генерацию
    вариантов с учётом дизамбигуации (см. ARCHITECTURE.md §5).
    """
    subject = task.query.subject.strip()
    quoted = f'"{subject}"' if " " in subject else subject

    queries: list[str] = [quoted]
    for hint in task.query.include_hints:
        queries.append(f"{quoted} {hint}")
    queries.extend(task.discovery.extra_queries)

    if task.query.intent:
        queries.append(f"{quoted} {task.query.intent}")

    # немного «вертикалей», чтобы выдача не схлопнулась в один тип источника
    queries.extend([f"{quoted} интервью", f"{quoted} interview", f"{quoted} photos"])

    # дедуп с сохранением порядка
    out, seen = [], set()
    for q in queries:
        q = q.strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


register(SearxngConnector())
