"""Task API. Всё, что делает UI, доступно здесь — UI это лишь генератор Task DSL."""

from __future__ import annotations

import asyncio
import json
import pathlib
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import ValidationError

from .. import db, embeddings, search, storage
from ..frontier import is_alive, redis, request_stop
from ..models import RunState, Task
from ..orchestrator import execute

UI_HTML = pathlib.Path(__file__).with_name("ui.html")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.migrate()
    await storage.ensure_bucket()
    await search.ensure_index()
    await reap_stale_runs()   # подчистить хвосты после падения/ребута
    yield
    await db.close()
    await storage.close()


app = FastAPI(title="Universal Harvester", version="0.1.0", lifespan=lifespan)

_running: dict[str, asyncio.Task] = {}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
async def ui() -> str:
    """Страница просмотра собранного. Меню в трее ведёт сюда."""
    return UI_HTML.read_text(encoding="utf-8")


# ------------------------------------------------------------------- задачи


@app.post("/tasks", status_code=201)
async def create_task(request: Request) -> dict[str, Any]:
    """Принимает Task DSL как JSON (application/json) или YAML (text/yaml)."""
    body = await request.body()
    ctype = request.headers.get("content-type", "")
    try:
        task = (
            Task.model_validate_json(body)
            if "json" in ctype
            else Task.from_yaml(body.decode())
        )
    except Exception as exc:
        raise HTTPException(422, f"невалидный Task DSL: {exc}") from exc

    task_id = await db.upsert_task(task)
    return {"task_id": str(task_id), "name": task.name, "spec": task.model_dump(mode="json")}


@app.post("/grab", status_code=202)
async def grab(
    urls: list[str] = Body(..., embed=True),
    depth: int = Body(0, embed=True),
    same_domain: bool = Body(True, embed=True),
    media: bool = Body(True, embed=True),
    max_pages: int | None = Body(None, embed=True),
    name: str | None = Body(None, embed=True),
) -> dict[str, Any]:
    """Собрать конкретные адреса. Создаёт задачу и сразу запускает прогон.

    Короткий путь для «спарси вот эту страницу» — без темы, поиска и YAML.
    """
    try:
        task = Task.for_urls(
            urls, name=name, depth=depth, same_domain=same_domain,
            media=media, max_pages=max_pages,
        )
    except (ValueError, ValidationError) as exc:
        raise HTTPException(422, str(exc)) from exc

    task_id = await db.upsert_task(task)
    started = await start_run(task_id)
    return {**started, "task_id": str(task_id), "name": task.name,
            "urls": task.discovery.seeds}


@app.get("/tasks")
async def list_tasks(limit: int = Query(100, le=500)) -> dict[str, Any]:
    tasks = await db.list_tasks(limit)
    return {"tasks": [{**t, "id": str(t["id"])} for t in tasks]}


@app.get("/tasks/{task_id}")
async def read_task(task_id: uuid.UUID) -> dict[str, Any]:
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "задача не найдена")
    return {**task, "id": str(task["id"])}


# ------------------------------------------------------------------ прогоны


@app.post("/tasks/{task_id}/runs", status_code=202)
async def start_run(task_id: uuid.UUID) -> dict[str, str]:
    record = await db.get_task(task_id)
    if not record:
        raise HTTPException(404, "задача не найдена")

    task = Task.model_validate(record["spec"])
    run_id = await db.create_run(task_id, task)

    async def _runner() -> None:
        try:
            await execute(task, task_id=task_id, run_id=run_id)
        finally:
            _running.pop(str(run_id), None)

    _running[str(run_id)] = asyncio.create_task(_runner())
    return {"run_id": str(run_id), "state": "planning"}


async def reap_stale_runs() -> int:
    """Помечает прогоны, чей процесс исчез (kill, падение, ребут).

    Живой прогон раз в пару секунд обновляет ключ-пульс в Redis. Нет пульса —
    процесса нет, и запись `running` в БД врёт. Без этой уборки трей вечно
    показывает призрачный прогон и отказывается запускать новые задачи.
    """
    reaped = 0
    for run in await db.list_runs(200, states=["planning", "running"]):
        if not await is_alive(run["id"]):
            await db.update_run(
                run["id"],
                state=RunState.FAILED,
                error="процесс прогона исчез (нет пульса)",
                finished=True,
            )
            reaped += 1
    return reaped


@app.get("/runs")
async def list_runs(
    limit: int = Query(20, le=200), active: bool = False
) -> dict[str, Any]:
    """active=true — только те, что сейчас идут (нужно трею и UI)."""
    if active:
        await reap_stale_runs()
    states = ["planning", "running"] if active else None
    runs = await db.list_runs(limit, states=states)
    return {
        "runs": [
            {**r, "id": str(r["id"]), "task_id": str(r["task_id"])} for r in runs
        ]
    }


@app.post("/runs/{run_id}/stop", status_code=202)
async def stop_run(run_id: uuid.UUID) -> dict[str, str]:
    """Просит прогон остановиться.

    Через флаг в Redis, поэтому работает и для прогонов, запущенных из CLI
    в другом процессе. Уже собранное остаётся в хранилище.
    """
    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(404, "прогон не найден")
    if run["state"] not in ("planning", "running"):
        return {"run_id": str(run_id), "state": run["state"], "detail": "уже не выполняется"}
    await request_stop(run_id)
    return {"run_id": str(run_id), "state": "stopping"}


@app.get("/runs/{run_id}")
async def read_run(run_id: uuid.UUID) -> dict[str, Any]:
    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(404, "прогон не найден")
    live = await db.run_stats(run_id)
    return {
        "id": str(run["id"]),
        "task_id": str(run["task_id"]),
        "state": run["state"],
        "error": run["error"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "stats": {**(run["stats"] or {}), **live},
    }


@app.get("/runs/{run_id}/events")
async def run_events(run_id: uuid.UUID) -> StreamingResponse:
    """SSE-поток прогресса: читаем Redis Stream, который пишет оркестратор."""

    async def stream():
        r = await redis()
        last_id = "0"
        idle = 0
        while idle < 600:
            entries = await r.xread({f"run:{run_id}:events": last_id}, count=20, block=2000)
            if not entries:
                idle += 1
                yield ": keep-alive\n\n"
                continue
            idle = 0
            for _, messages in entries:
                for msg_id, fields in messages:
                    last_id = msg_id
                    payload = json.dumps(
                        {"event": fields.get("event"), **json.loads(fields.get("data", "{}"))}
                    )
                    yield f"event: {fields.get('event')}\ndata: {payload}\n\n"
                    if fields.get("event") in ("completed", "failed"):
                        return

    return StreamingResponse(stream(), media_type="text/event-stream")


# ------------------------------------------------------- данные и интеграции


@app.get("/items")
async def list_items(
    task_id: uuid.UUID | None = None,
    domain: str | None = None,
    min_relevance: float | None = Query(None, description="Порог: -1..1, обычно 0.2-0.4"),
    topic_id: uuid.UUID | None = None,
    limit: int = Query(50, le=500),
    offset: int = 0,
) -> dict[str, Any]:
    items = await db.list_items(task_id, domain=domain, min_relevance=min_relevance,
                                topic_id=topic_id, limit=limit, offset=offset)
    return {"items": [{**i, "id": str(i["id"])} for i in items], "limit": limit,
            "offset": offset}


@app.post("/search")
async def search_items(
    q: str = Body(..., embed=True),
    task_id: str | None = Body(None, embed=True),
    limit: int = Body(20, embed=True),
    semantic: bool = Body(False, embed=True),
    min_relevance: float | None = Body(None, embed=True),
) -> dict[str, Any]:
    """semantic=false — по словам (Meilisearch); true — по смыслу (эмбеддинги)."""
    if not semantic:
        return await search.search(q, task_id=task_id, limit=limit)

    try:
        vector = (await embeddings.embed_async([q]))[0]
    except embeddings.EmbeddingsUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    hits = await db.semantic_search(
        vector, task_id=uuid.UUID(task_id) if task_id else None,
        limit=limit, min_relevance=min_relevance,
    )
    return {"hits": [{**h, "id": str(h["id"])} for h in hits], "mode": "semantic"}


@app.get("/tasks/{task_id}/topics")
async def task_topics(task_id: uuid.UUID, min_size: int = 1) -> dict[str, Any]:
    """Подтемы задачи: из чего состоит корпус. Считаются командой `harvester topics`."""
    topics = await db.list_topics(task_id)
    return {"topics": [{**t, "id": str(t["id"]),
                        "representative_document_id": None}
                       for t in topics if t["size"] >= min_size]}


@app.get("/tasks/{task_id}/relevance")
async def task_relevance(task_id: uuid.UUID) -> dict[str, Any]:
    """Распределение оценок — чтобы подобрать порог по данным, а не наугад."""
    return {"buckets": await db.relevance_histogram(task_id)}


@app.get("/media")
async def list_media(
    task_id: uuid.UUID | None = None,
    limit: int = Query(60, le=300),
    offset: int = 0,
) -> dict[str, Any]:
    items = await db.list_media(task_id, limit=limit, offset=offset)
    return {"media": [{**m, "id": str(m["id"])} for m in items],
            "limit": limit, "offset": offset}


@app.get("/media/{media_id}")
async def get_media(media_id: uuid.UUID, thumb: bool = False) -> RedirectResponse:
    asset = await db.get_media(media_id)
    if not asset:
        raise HTTPException(404, "медиа не найдено")
    key = asset["thumb_ref"] if thumb and asset["thumb_ref"] else asset["blob_ref"]
    return RedirectResponse(await storage.presign(key))
