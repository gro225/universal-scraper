"""Уровень прогона: FSM planning -> running -> completed.

Фаза 0 держит состояние в Postgres, а параллелизм — в asyncio-воркерах одного процесса.
Точка роста: воркеры выносятся в отдельные контейнеры (пулы fetch-light / fetch-render /
extract / media), а FSM переезжает в Temporal. Контракты при этом не меняются —
фронтир и репозитории уже общие.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from .. import connectors, db, search, storage
from ..config import settings
from ..connectors import render
from ..frontier import Frontier, heartbeat
from ..hashing import canonical_url, registrable_domain
from ..media import MediaSkipped, process_media
from ..models import ExtractResult, RunState, SourceRef, Task

log = logging.getLogger("harvester.run")


class Budget:
    """Жёсткие бюджеты: превышение останавливает прогон, а не жжёт ресурсы молча."""

    def __init__(self, task: Task):
        self.max_pages = task.crawl.budget.max_pages
        self.max_bytes = task.crawl.budget.max_bytes
        self.deadline = time.monotonic() + task.crawl.budget.max_wall_clock_s
        self.pages = 0
        self.bytes = 0
        self._lock = asyncio.Lock()

    async def consume(self, size: int) -> None:
        async with self._lock:
            self.pages += 1
            self.bytes += size

    def exhausted(self) -> str | None:
        if self.pages >= self.max_pages:
            return "max_pages"
        if self.bytes >= self.max_bytes:
            return "max_bytes"
        if time.monotonic() >= self.deadline:
            return "max_wall_clock"
        return None


class RunContext:
    def __init__(self, task: Task, task_id: uuid.UUID, run_id: uuid.UUID):
        self.task = task
        self.task_id = task_id
        self.run_id = run_id
        self.frontier = Frontier(
            run_id,
            task.crawl.politeness.delay_per_domain_ms,
            task.crawl.politeness.respect_robots,
        )
        self.budget = Budget(task)
        self.seed_domains: set[str] = set()
        self.stop_reason: str | None = None
        self.index_buffer: list[dict[str, Any]] = []
        self.counters = {
            "seeds": 0, "fetched": 0, "documents": 0, "duplicates": 0,
            "media": 0, "media_new": 0, "errors": 0, "skipped_robots": 0,
            "media_skipped": 0, "infra_errors": 0, "rendered": 0,
        }

    async def emit(self, event: str, **fields: Any) -> None:
        """Событие прогресса в Redis Stream — его читает SSE в API."""
        from ..frontier import redis

        r = await redis()
        await r.xadd(
            f"run:{self.run_id}:events",
            {"event": event, "data": json.dumps(fields, default=str)},
            maxlen=1000,
            approximate=True,
        )


# ----------------------------------------------------------------- точка входа


async def execute(task: Task, *, task_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
    ctx = RunContext(task, task_id, run_id)
    try:
        await storage.ensure_bucket()
        await search.ensure_index()

        await db.update_run(run_id, state=RunState.PLANNING)
        # пульс начинается сразу: discovery тоже может идти минутами
        watcher = asyncio.create_task(_stop_watcher(ctx))
        try:
            await _plan(ctx)
            await db.update_run(run_id, state=RunState.RUNNING)
            await _crawl(ctx)
        finally:
            watcher.cancel()
        await _flush_index(ctx)

        stats = await db.run_stats(run_id)
        stats.update(ctx.counters)
        stats["stop_reason"] = ctx.stop_reason
        await db.update_run(run_id, state=RunState.COMPLETED, stats=stats, finished=True)
        await ctx.emit("completed", **stats)
        return stats
    except Exception as exc:  # прогон падает целиком только на неожиданной ошибке
        log.exception("run failed")
        await db.update_run(
            run_id, state=RunState.FAILED, error=f"{type(exc).__name__}: {exc}", finished=True
        )
        await ctx.emit("failed", error=str(exc))
        raise
    finally:
        # Браузер T1 живёт ровно один прогон: держать Chromium между прогонами
        # незачем, а забытый процесс переживает падение и течёт памятью.
        await render.close()
        await ctx.frontier.cleanup()


# ------------------------------------------------------------------ фаза plan


async def _plan(ctx: RunContext) -> None:
    """Тема -> список seed-URL. Фаза 2 добавит сюда дизамбигуацию."""
    refs: list[SourceRef] = []

    for name in ctx.task.discovery.connectors:
        try:
            conn = connectors.get(name)
        except KeyError:
            log.warning("пропускаю неизвестный коннектор %s", name)
            continue
        try:
            async for ref in conn.discover(ctx.task):
                refs.append(ref)
        except NotImplementedError:
            continue
        except Exception:
            log.exception("discovery %s упал", name)

    # явные seeds из DSL всегда идут с максимальным приоритетом
    for url in ctx.task.discovery.seeds:
        refs.insert(0, SourceRef(url=url, priority=1.0, discovered_by="task.seeds"))

    refs = refs[: ctx.task.discovery.max_seeds]

    # Режим рендера едет в подсказке самого ref: фетчер видит только SourceRef,
    # а не задачу. Дальше подсказка наследуется найденными ссылками (_enqueue_links).
    if (mode := ctx.task.crawl.render) != "auto":
        for ref in refs:
            ref.hint.setdefault("render", mode == "always")

    ctx.seed_domains = {registrable_domain(r.url) for r in refs}
    ctx.counters["seeds"] = await ctx.frontier.push_many(refs)

    await ctx.emit(
        "planned",
        seeds=ctx.counters["seeds"],
        domains=len(ctx.seed_domains),
        queries=len(ctx.task.discovery.extra_queries) or None,
    )
    log.info("plan: %s seed'ов на %s доменах", ctx.counters["seeds"], len(ctx.seed_domains))


# ----------------------------------------------------------------- фаза crawl


async def _stop_watcher(ctx: RunContext, interval: float = 2.0) -> None:
    """Пульс прогона + слежение за флагом остановки.

    Пульс нужен, чтобы убитый процесс не оставлял вечно висящую запись `running`:
    ключ в Redis протухает сам, и API помечает такой прогон прерванным.
    """
    while True:
        try:
            await heartbeat(ctx.run_id)
            if await ctx.frontier.stop_requested():
                ctx.stop_reason = ctx.stop_reason or "stopped_by_user"
                log.info("получен запрос на остановку прогона")
                return
        except Exception:
            log.debug("stop watcher: не смог обратиться к Redis", exc_info=True)
        await asyncio.sleep(interval)


async def _crawl(ctx: RunContext) -> None:
    s = settings()
    crawl_workers = [
        asyncio.create_task(_crawl_worker(ctx, i)) for i in range(s.crawl_workers)
    ]
    media_workers = [
        asyncio.create_task(_media_worker(ctx, i)) for i in range(s.media_workers)
    ]
    await asyncio.gather(*crawl_workers)
    # добираем хвост медиа-очереди после остановки обхода
    await asyncio.gather(*media_workers)


BATCH = 12  # сколько кандидатов воркер рассматривает за раз


async def _crawl_worker(ctx: RunContext, index: int) -> None:
    idle = 0
    starved = 0
    glitches = 0
    while True:
        if ctx.stop_reason:  # остановлено извне или другим воркером
            return
        if reason := ctx.budget.exhausted():
            ctx.stop_reason = ctx.stop_reason or reason
            return

        # Весь цикл под защитой: сбой инфраструктуры (Redis, БД) не должен
        # ронять многочасовой прогон — воркер отступает и пробует снова.
        try:
            batch = await ctx.frontier.pop_batch(BATCH)
            if not batch:
                idle += 1
                if idle > 20:  # очередь пуста достаточно долго — расходимся
                    return
                await asyncio.sleep(0.5)
                continue
            idle = 0

            ref = await _claim(ctx, batch)
            if ref is None:
                # вся пачка — из занятых доменов; ждём, но не жёстко, очередь глубокая
                starved += 1
                await asyncio.sleep(min(0.1 * starved, 1.5))
                continue
            starved = 0
            glitches = 0

            try:
                await _process_url(ctx, ref)
            except Exception:
                ctx.counters["errors"] += 1
                log.debug("worker %s: ошибка на %s", index, ref.url, exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            glitches += 1
            ctx.counters["infra_errors"] += 1
            log.warning("worker %s: сбой инфраструктуры (%s подряд)", index, glitches,
                        exc_info=True)
            if glitches >= 10:
                ctx.stop_reason = ctx.stop_reason or "infrastructure_failure"
                return
            await asyncio.sleep(min(1.0 * glitches, 15.0))


async def _claim(ctx: RunContext, batch: list[SourceRef]) -> SourceRef | None:
    """Берёт из пачки первый URL со свободным доменом, остальные возвращает в очередь.

    Это и есть вежливость без простоя: задержка соблюдается по каждому домену,
    но воркер тем временем занят другим доменом.
    """
    claimed: SourceRef | None = None
    leftovers: list[SourceRef] = []

    for ref in batch:
        if claimed is not None:
            leftovers.append(ref)
            continue
        if not await ctx.frontier.domain_is_open(ref.url):
            continue  # домен в холодильнике — URL выбрасываем
        if await ctx.frontier.try_slot(ref.url) > 0:
            leftovers.append(ref)
            continue
        claimed = ref

    for ref in leftovers:
        await ctx.frontier.requeue(ref)
    return claimed


async def _process_url(ctx: RunContext, ref: SourceRef) -> None:
    """Слот домена уже занят вызывающим (_claim)."""
    if not await ctx.frontier.allowed_by_robots(ref.url):
        ctx.counters["skipped_robots"] += 1
        await db.log_fetch(ctx.run_id, ref.url, error="robots_disallow")
        return

    conn = connectors.resolve(ref)
    started = time.monotonic()
    try:
        raw = await conn.fetch(ref)
    except Exception as exc:
        ctx.counters["errors"] += 1
        await ctx.frontier.record_failure(ref.url)
        await db.log_fetch(
            ctx.run_id, ref.url, error=f"{type(exc).__name__}: {exc}"[:500],
            ms=int((time.monotonic() - started) * 1000),
        )
        return

    if raw.status >= 400:
        await ctx.frontier.record_failure(ref.url)
    else:
        await ctx.frontier.record_success(ref.url)

    ctx.counters["fetched"] += 1
    if raw.tier != "t0":
        ctx.counters["rendered"] += 1
    await ctx.budget.consume(len(raw.body))

    # 1. Сырьё в Bronze — ДО парсинга. Из него всё пересобирается без нового обхода.
    sha_hex = hashlib.sha256(raw.body).hexdigest()
    raw_ref = storage.raw_key(ctx.task_id, ctx.run_id, sha_hex)
    await storage.put(raw_ref, raw.body, raw.content_type)
    await db.log_fetch(
        ctx.run_id, ref.url, status=raw.status, tier=raw.tier,
        size=len(raw.body), ms=raw.elapsed_ms, raw_ref=raw_ref,
    )

    # 2. Извлечение
    result: ExtractResult = conn.extract(raw, ctx.task)
    if result.document is not None:
        result.document.raw_ref = raw_ref
        await _store_document(ctx, result, ref)

    # 3. Новые ссылки во фронтир
    await _enqueue_links(ctx, ref, result.links)


async def _store_document(ctx: RunContext, result: ExtractResult, ref: SourceRef) -> None:
    doc = result.document
    assert doc is not None

    doc_id = await db.insert_document(ctx.task_id, ctx.run_id, doc)
    if doc_id is None:
        ctx.counters["duplicates"] += 1
        return

    ctx.counters["documents"] += 1
    await db.add_links(ctx.run_id, doc_id, result.links[:100])

    if ctx.task.extract.media.download:
        for media_ref in result.media:
            await ctx.frontier.push_media(
                {
                    "url": media_ref.url,
                    "document_id": str(doc_id),
                    "role": media_ref.role,
                    "position": media_ref.position,
                }
            )

    ctx.index_buffer.append(
        {
            "id": str(doc_id),
            "task_id": str(ctx.task_id),
            "run_id": str(ctx.run_id),
            "kind": doc.kind,
            "title": doc.title or "",
            "text": doc.text[:20_000],
            "excerpt": doc.text[:400],
            "author": doc.author,
            "domain": doc.domain,
            "lang": doc.lang,
            "source_url": doc.source_url,
            "published_at": doc.published_at.isoformat() if doc.published_at else None,
            "published_at_ts": int(doc.published_at.timestamp()) if doc.published_at else 0,
            "fetched_at_ts": int(datetime.now(UTC).timestamp()),
        }
    )
    if len(ctx.index_buffer) >= 50:
        await _flush_index(ctx)

    if ctx.counters["documents"] % 25 == 0:
        await ctx.emit("progress", **ctx.counters, queue=await ctx.frontier.size())


async def _flush_index(ctx: RunContext) -> None:
    if ctx.index_buffer:
        await search.index_documents(ctx.index_buffer)
        ctx.index_buffer.clear()


async def _enqueue_links(ctx: RunContext, ref: SourceRef, links: list[str]) -> None:
    scope = ctx.task.crawl.scope
    if scope.follow == "none" or ref.depth >= scope.max_depth:
        return

    same_domain_only = scope.follow == "seed_domain"
    out: list[SourceRef] = []
    for url in links:
        if not _passes_filters(url, scope.allow, scope.deny):
            continue
        dom = registrable_domain(url)
        is_foreign = dom not in ctx.seed_domains
        if same_domain_only and is_foreign:
            continue
        # в режиме outlinks чужие домены берём только на один шаг вглубь
        if is_foreign and ref.depth + 1 > 1:
            continue
        out.append(
            SourceRef(
                url=canonical_url(url),
                depth=ref.depth + 1,
                priority=max(0.05, ref.priority * 0.7),
                discovered_by=ref.url,
                hint=dict(ref.hint),  # режим рендера наследуется вглубь
            )
        )
    if out:
        await ctx.frontier.push_many(out[:200])


def _passes_filters(url: str, allow: list[str], deny: list[str]) -> bool:
    if any(fnmatch.fnmatch(url, pattern) for pattern in deny):
        return False
    return any(fnmatch.fnmatch(url, pattern) for pattern in allow) if allow else True


# ----------------------------------------------------------------- фаза media


async def _media_worker(ctx: RunContext, index: int) -> None:
    s = settings()
    idle = 0
    async with httpx.AsyncClient(
        timeout=s.fetch_timeout_s,
        follow_redirects=True,
        headers={"User-Agent": s.user_agent},
    ) as client:
        glitches = 0
        while True:
            # пользовательская остановка должна гасить и докачку медиа
            if ctx.stop_reason == "stopped_by_user":
                return
            try:
                job = await ctx.frontier.pop_media()
                if job is None:
                    # выходим, только когда обход уже закончился и очередь пуста
                    idle += 1
                    if idle > 10 and (ctx.stop_reason or await ctx.frontier.size() == 0):
                        return
                    await asyncio.sleep(1.0)
                    continue
                idle = 0
                glitches = 0

                # та же логика, что у краулера: занятый CDN не должен держать воркер
                if await ctx.frontier.try_slot(job["url"]) > 0:
                    await ctx.frontier.push_media(job)
                    await asyncio.sleep(0.05)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                glitches += 1
                ctx.counters["infra_errors"] += 1
                if glitches >= 10:
                    return
                await asyncio.sleep(min(1.0 * glitches, 15.0))
                continue

            try:
                _, is_new = await process_media(
                    url=job["url"],
                    document_id=uuid.UUID(job["document_id"]),
                    role=job["role"],
                    position=job["position"],
                    spec=ctx.task.extract.media,
                    client=client,
                )
                ctx.counters["media"] += 1
                ctx.counters["media_new"] += int(is_new)
            except MediaSkipped:
                ctx.counters["media_skipped"] += 1
            except Exception:
                ctx.counters["errors"] += 1
                log.debug("media worker %s: %s", index, job["url"], exc_info=True)
