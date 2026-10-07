"""Полнотекстовый индекс (Meilisearch) поверх Silver.

Фаза 0: индексируются документы. Фаза 1: сюда же приедут media_assets
(ocr_text + caption) — тогда один запрос будет находить и статьи, и фото.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import settings

INDEX = "documents"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings().meili_key}"}


async def ensure_index() -> None:
    base = settings().meili_url
    async with httpx.AsyncClient(base_url=base, headers=_headers(), timeout=15) as c:
        r = await c.get(f"/indexes/{INDEX}")
        if r.status_code == 404:
            await c.post("/indexes", json={"uid": INDEX, "primaryKey": "id"})
        await c.patch(
            f"/indexes/{INDEX}/settings",
            json={
                "searchableAttributes": ["title", "text", "author", "domain"],
                "filterableAttributes": ["task_id", "run_id", "domain", "kind", "lang"],
                "sortableAttributes": ["published_at_ts", "fetched_at_ts"],
                "displayedAttributes": [
                    "id", "title", "excerpt", "source_url", "domain", "kind",
                    "lang", "published_at", "media_count",
                ],
            },
        )


async def index_documents(docs: list[dict[str, Any]]) -> None:
    if not docs:
        return
    base = settings().meili_url
    async with httpx.AsyncClient(base_url=base, headers=_headers(), timeout=30) as c:
        await c.post(f"/indexes/{INDEX}/documents", json=docs)


async def search(
    q: str, *, task_id: str | None = None, limit: int = 20, offset: int = 0
) -> dict[str, Any]:
    base = settings().meili_url
    body: dict[str, Any] = {"q": q, "limit": limit, "offset": offset}
    if task_id:
        body["filter"] = f'task_id = "{task_id}"'
    async with httpx.AsyncClient(base_url=base, headers=_headers(), timeout=15) as c:
        r = await c.post(f"/indexes/{INDEX}/search", json=body)
        r.raise_for_status()
        return r.json()
