"""Доступ к Silver-слою (PostgreSQL). Тонкие функции-репозитории, без ORM."""

from __future__ import annotations

import json
import pathlib
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncpg

from .config import settings
from .models import DocumentDraft, RunState, Task

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "migrations"

_pool: asyncpg.Pool | None = None


async def pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            settings().database_url,
            min_size=2,
            max_size=16,
            init=_init_conn,
        )
    return _pool


async def _init_conn(conn: asyncpg.Connection) -> None:
    # jsonb <-> dict без ручного json.dumps на каждом вызове
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# --------------------------------------------------------------------- миграции


async def migrate() -> list[str]:
    p = await pool()
    applied: list[str] = []
    async with p.acquire() as conn:
        await conn.execute(
            "create table if not exists schema_migrations ("
            " name text primary key, applied_at timestamptz not null default now())"
        )
        done = {r["name"] for r in await conn.fetch("select name from schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text())
                await conn.execute(
                    "insert into schema_migrations (name) values ($1)", path.name
                )
            applied.append(path.name)
    return applied


# ------------------------------------------------------------------ задачи/прогоны


async def upsert_task(task: Task) -> uuid.UUID:
    p = await pool()
    return await p.fetchval(
        """
        insert into tasks (name, version, spec) values ($1, $2, $3)
        on conflict (name) do update
            set version = excluded.version,
                spec = excluded.spec,
                updated_at = now()
        returning id
        """,
        task.name,
        task.version,
        task.model_dump(mode="json"),
    )


async def get_task(task_id: uuid.UUID) -> dict[str, Any] | None:
    p = await pool()
    row = await p.fetchrow("select * from tasks where id = $1", task_id)
    return dict(row) if row else None


async def create_run(task_id: uuid.UUID, task: Task) -> uuid.UUID:
    p = await pool()
    return await p.fetchval(
        "insert into runs (task_id, state, spec_snapshot) values ($1, $2, $3) returning id",
        task_id,
        RunState.PLANNING.value,
        task.model_dump(mode="json"),
    )


async def update_run(
    run_id: uuid.UUID,
    *,
    state: RunState | None = None,
    stats: dict[str, Any] | None = None,
    error: str | None = None,
    finished: bool = False,
) -> None:
    p = await pool()
    await p.execute(
        """
        update runs set
            state       = coalesce($2, state),
            stats       = coalesce($3, stats),
            error       = coalesce($4, error),
            finished_at = case when $5 then now() else finished_at end
        where id = $1
        """,
        run_id,
        state.value if state else None,
        stats,
        error,
        finished,
    )


async def get_run(run_id: uuid.UUID) -> dict[str, Any] | None:
    p = await pool()
    row = await p.fetchrow("select * from runs where id = $1", run_id)
    return dict(row) if row else None


async def list_tasks(limit: int = 100) -> list[dict[str, Any]]:
    p = await pool()
    rows = await p.fetch(
        """
        select t.id, t.name, t.version, t.updated_at,
               t.spec #>> '{query,subject}' as subject,
               (select count(*) from documents d where d.task_id = t.id) as documents
          from tasks t order by t.updated_at desc limit $1
        """,
        limit,
    )
    return [dict(r) for r in rows]


async def list_runs(
    limit: int = 20, *, states: list[str] | None = None
) -> list[dict[str, Any]]:
    """Последние прогоны. states=['planning','running'] — только живые."""
    p = await pool()
    rows = await p.fetch(
        """
        select r.id, r.task_id, t.name as task_name, r.state, r.error,
               r.started_at, r.finished_at, r.stats
          from runs r join tasks t on t.id = r.task_id
         where ($2::text[] is null or r.state = any($2))
         order by r.started_at desc limit $1
        """,
        limit,
        states,
    )
    return [dict(r) for r in rows]


# --------------------------------------------------------------------- документы


async def insert_document(
    task_id: uuid.UUID, run_id: uuid.UUID, doc: DocumentDraft
) -> uuid.UUID | None:
    """Возвращает id нового документа либо None, если это точный дубль внутри задачи.

    Дубль не выбрасывается: его URL доклеивается в source_urls существующей записи.
    """
    p = await pool()
    async with p.acquire() as conn:
        new_id = await conn.fetchval(
            """
            insert into documents (
                task_id, run_id, kind, source_url, canonical_url, domain, connector,
                lang, title, text, author, published_at, fetched_at, payload,
                raw_ref, content_hash, simhash, relevance, source_urls
            ) values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,
                      array[$4::text])
            on conflict (task_id, content_hash) do nothing
            returning id
            """,
            task_id,
            run_id,
            doc.kind,
            doc.source_url,
            doc.canonical_url,
            doc.domain,
            doc.connector,
            doc.lang,
            doc.title,
            doc.text,
            doc.author,
            doc.published_at,
            datetime.now(UTC),
            doc.payload,
            doc.raw_ref,
            doc.content_hash,
            doc.simhash,
            doc.relevance,
        )
        if new_id is None:
            await conn.execute(
                """
                update documents
                   set source_urls = (
                        select array_agg(distinct u)
                          from unnest(source_urls || array[$3::text]) as u)
                 where task_id = $1 and content_hash = $2
                """,
                task_id,
                doc.content_hash,
                doc.source_url,
            )
        return new_id


async def upsert_media(
    *,
    sha256: bytes,
    media_type: str,
    mime: str | None,
    size: int,
    width: int | None,
    height: int | None,
    blob_ref: str,
    thumb_ref: str | None,
    source_url: str,
    exif: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, bool]:
    """Возвращает (media_id, is_new). Дедуп глобальный — по sha256 контента."""
    p = await pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into media_assets
                (sha256, media_type, mime, bytes, width, height, blob_ref, thumb_ref,
                 source_url, exif)
            values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            on conflict (sha256) do nothing
            returning id
            """,
            sha256, media_type, mime, size, width, height, blob_ref, thumb_ref,
            source_url, exif or {},
        )
        if row:
            return row["id"], True
        existing = await conn.fetchval("select id from media_assets where sha256 = $1", sha256)
        return existing, False


async def link_media(
    document_id: uuid.UUID, media_id: uuid.UUID, role: str, position: int
) -> None:
    p = await pool()
    await p.execute(
        "insert into document_media (document_id, media_id, role, position)"
        " values ($1,$2,$3,$4) on conflict do nothing",
        document_id, media_id, role, position,
    )


async def log_fetch(
    run_id: uuid.UUID,
    url: str,
    *,
    status: int | None = None,
    tier: str = "t0",
    size: int | None = None,
    ms: int | None = None,
    error: str | None = None,
    raw_ref: str | None = None,
) -> None:
    p = await pool()
    await p.execute(
        "insert into fetch_log (run_id, url, status, tier, bytes, ms, error, raw_ref)"
        " values ($1,$2,$3,$4,$5,$6,$7,$8)",
        run_id, url, status, tier, size, ms, error, raw_ref,
    )


async def add_links(run_id: uuid.UUID, src: uuid.UUID | None, urls: list[str]) -> None:
    if not urls:
        return
    p = await pool()
    await p.executemany(
        "insert into links (run_id, src_document_id, dst_url) values ($1,$2,$3)",
        [(run_id, src, u) for u in urls],
    )


# ------------------------------------------------------------------------ выборки


async def run_stats(run_id: uuid.UUID) -> dict[str, Any]:
    p = await pool()
    row = await p.fetchrow(
        """
        select
          (select count(*) from documents where run_id = $1)                as documents,
          (select count(*) from fetch_log where run_id = $1)                as fetches,
          (select count(*) from fetch_log where run_id = $1 and error is not null) as errors,
          (select count(distinct domain) from documents where run_id = $1)  as domains,
          (select coalesce(sum(bytes), 0) from fetch_log where run_id = $1) as bytes,
          (select count(distinct dm.media_id)
             from document_media dm join documents d on d.id = dm.document_id
            where d.run_id = $1)                                            as media
        """,
        run_id,
    )
    return dict(row)


async def list_items(
    task_id: uuid.UUID | None = None,
    *,
    domain: str | None = None,
    min_relevance: float | None = None,
    topic_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    p = await pool()
    rows = await p.fetch(
        """
        select d.id, d.kind, d.title, d.source_url, d.domain, d.lang, d.relevance,
               d.published_at, d.fetched_at, left(d.text, 400) as excerpt,
               (select count(*) from document_media dm where dm.document_id = d.id) as media_count
          from documents d
         where ($1::uuid is null or d.task_id = $1)
           and ($2::text is null or d.domain = $2)
           and ($5::real is null or d.relevance >= $5)
           and ($6::uuid is null or d.topic_id = $6)
         order by d.created_at desc
         limit $3 offset $4
        """,
        task_id, domain, limit, offset, min_relevance, topic_id,
    )
    return [dict(r) for r in rows]


# ------------------------------------------------------------------- эмбеддинги


def _vec(values) -> str:
    """pgvector принимает литерал вида '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{float(x):.6f}" for x in values) + "]"


async def save_embeddings(rows: list[tuple[uuid.UUID, Any, float | None]]) -> None:
    """(document_id, вектор, релевантность) пачкой."""
    if not rows:
        return
    p = await pool()
    await p.executemany(
        "update documents set embedding = $2::vector, relevance = $3 where id = $1",
        [(doc_id, _vec(vec), rel) for doc_id, vec, rel in rows],
    )


async def rescore_task(task_id: uuid.UUID, positive, negative=None) -> int:
    """Пересчитать релевантность по новым якорям, не трогая векторы документов.

    Векторы документов от подсказок не зависят — меняется только, с чем их сравнивают.
    Поэтому правка `exclude_hints` стоит одного UPDATE, а не повторного прогона модели.

    `<=>` это косинусное расстояние (1 - cos), поэтому
    cos_pos - cos_neg  ==  dist_neg - dist_pos.
    """
    p = await pool()
    if negative is None:
        sql = """
            update documents set relevance = 1 - (embedding <=> $2::vector)
             where task_id = $1 and embedding is not null
        """
        args = (task_id, _vec(positive))
    else:
        sql = """
            update documents
               set relevance = (embedding <=> $3::vector) - (embedding <=> $2::vector)
             where task_id = $1 and embedding is not null
        """
        args = (task_id, _vec(positive), _vec(negative))
    result = await p.execute(sql, *args)
    return int(result.split()[-1]) if result.startswith("UPDATE") else 0


async def documents_without_embedding(
    task_id: uuid.UUID | None = None, *, limit: int = 200
) -> list[dict[str, Any]]:
    """Для дозаполнения уже собранного."""
    p = await pool()
    rows = await p.fetch(
        """
        select d.id, d.title, d.text, d.task_id
          from documents d
         where d.embedding is null
           and ($1::uuid is null or d.task_id = $1)
         order by d.created_at
         limit $2
        """,
        task_id, limit,
    )
    return [dict(r) for r in rows]


async def semantic_search(
    vector, *, task_id: uuid.UUID | None = None, limit: int = 20,
    min_relevance: float | None = None,
) -> list[dict[str, Any]]:
    """Поиск по смыслу: ближайшие по косинусу документы."""
    p = await pool()
    rows = await p.fetch(
        """
        select d.id, d.kind, d.title, d.source_url, d.domain, d.lang, d.relevance,
               d.published_at, left(d.text, 400) as excerpt,
               1 - (d.embedding <=> $1::vector) as similarity
          from documents d
         where d.embedding is not null
           and ($2::uuid is null or d.task_id = $2)
           and ($4::real is null or d.relevance >= $4)
         order by d.embedding <=> $1::vector
         limit $3
        """,
        _vec(vector), task_id, limit, min_relevance,
    )
    return [dict(r) for r in rows]


async def relevance_histogram(task_id: uuid.UUID) -> list[dict[str, Any]]:
    """Распределение оценок — чтобы подобрать порог глазами, а не наугад."""
    p = await pool()
    rows = await p.fetch(
        """
        select width_bucket(relevance, -1, 1, 20) as bucket,
               round(min(relevance)::numeric, 3) as lo,
               round(max(relevance)::numeric, 3) as hi,
               count(*) as n
          from documents
         where task_id = $1 and relevance is not null
         group by 1 order by 1
        """,
        task_id,
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------- подтемы


async def documents_for_clustering(
    task_id: uuid.UUID, *, limit: int = 5000
) -> list[dict[str, Any]]:
    """Документы с векторами. Берём самые релевантные — если корпус не влезает целиком."""
    p = await pool()
    rows = await p.fetch(
        """
        select id, coalesce(title, '') as title, left(text, 400) as body,
               embedding::text as vec, relevance
          from documents
         where task_id = $1 and embedding is not null
         order by relevance desc nulls last
         limit $2
        """,
        task_id, limit,
    )
    return [dict(r) for r in rows]


async def replace_topics(task_id: uuid.UUID, topics: list[dict[str, Any]]) -> int:
    """Записывает разбиение целиком: старое удаляется, новое вставляется.

    Кластеризация не инкрементальна — новые документы меняют всё разбиение,
    поэтому частичное обновление только путало бы."""
    p = await pool()
    async with p.acquire() as conn, conn.transaction():
        await conn.execute('update documents set topic_id = null where task_id = $1', task_id)
        await conn.execute('delete from topics where task_id = $1', task_id)
        for t in topics:
            topic_id = await conn.fetchval(
                """
                insert into topics
                    (task_id, label, keywords, size, centroid, avg_relevance,
                     representative_document_id)
                values ($1,$2,$3,$4,$5::vector,$6,$7) returning id
                """,
                task_id, t['label'], t['keywords'], t['size'],
                _vec(t['centroid']), t.get('avg_relevance'), t.get('representative'),
            )
            await conn.execute(
                'update documents set topic_id = $1 where id = any($2::uuid[])',
                topic_id, t['members'],
            )
    return len(topics)


async def list_topics(task_id: uuid.UUID) -> list[dict[str, Any]]:
    p = await pool()
    rows = await p.fetch(
        """
        select t.id, t.label, t.keywords, t.size, t.avg_relevance,
               d.title as representative_title, d.source_url as representative_url,
               d.domain as representative_domain,
               (select array_agg(distinct x.domain)
                  from (select domain from documents where topic_id = t.id
                         order by relevance desc nulls last limit 5) x) as domains
          from topics t
          left join documents d on d.id = t.representative_document_id
         where t.task_id = $1
         order by t.avg_relevance desc nulls last, t.size desc
        """,
        task_id,
    )
    return [dict(r) for r in rows]


async def list_media(
    task_id: uuid.UUID | None = None, *, limit: int = 60, offset: int = 0
) -> list[dict[str, Any]]:
    p = await pool()
    rows = await p.fetch(
        """
        select m.id, m.media_type, m.mime, m.bytes, m.width, m.height, m.source_url,
               m.first_seen,
               (select d.title from document_media dm
                  join documents d on d.id = dm.document_id
                 where dm.media_id = m.id limit 1) as document_title
          from media_assets m
         where $1::uuid is null or exists (
                select 1 from document_media dm
                  join documents d on d.id = dm.document_id
                 where dm.media_id = m.id and d.task_id = $1)
         order by m.first_seen desc
         limit $2 offset $3
        """,
        task_id, limit, offset,
    )
    return [dict(r) for r in rows]


async def get_media(media_id: uuid.UUID) -> dict[str, Any] | None:
    p = await pool()
    row = await p.fetchrow("select * from media_assets where id = $1", media_id)
    return dict(row) if row else None
