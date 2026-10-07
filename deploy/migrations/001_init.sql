-- Silver-слой: нормализованное хранилище.
-- documents и media_assets — граждане одного класса, связаны M:N.

create extension if not exists pgcrypto;

-- --------------------------------------------------------------------- задачи

create table if not exists tasks (
    id          uuid primary key default gen_random_uuid(),
    name        text not null unique,
    version     int  not null default 1,
    spec        jsonb not null,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create table if not exists runs (
    id            uuid primary key default gen_random_uuid(),
    task_id       uuid not null references tasks(id) on delete cascade,
    state         text not null default 'planning',
    spec_snapshot jsonb not null,          -- воспроизводимость: чем именно бежали
    stats         jsonb not null default '{}',
    error         text,
    started_at    timestamptz not null default now(),
    finished_at   timestamptz
);
create index if not exists runs_task_idx on runs (task_id, started_at desc);

-- ---------------------------------------------------------------- документы

create table if not exists documents (
    id            uuid primary key default gen_random_uuid(),
    task_id       uuid not null references tasks(id) on delete cascade,
    run_id        uuid not null references runs(id) on delete cascade,
    kind          text not null default 'page',   -- article|post|product|profile|page|file
    source_url    text not null,
    canonical_url text,
    domain        text not null default '',
    connector     text not null default 'generic_web',
    lang          text,
    title         text,
    text          text not null default '',
    author        text,
    published_at  timestamptz,
    fetched_at    timestamptz not null default now(),
    payload       jsonb not null default '{}',    -- специфика источника + польз. схема
    raw_ref       text not null default '',       -- ключ в Bronze
    content_hash  bytea not null,
    simhash       bigint,
    relevance     real,
    quality       real,
    labels        text[] not null default '{}',
    source_urls   text[] not null default '{}',   -- склеенные дубли
    created_at    timestamptz not null default now()
);

create unique index if not exists documents_task_hash_uidx
    on documents (task_id, content_hash);
create index if not exists documents_task_time_idx
    on documents (task_id, published_at desc nulls last);
create index if not exists documents_domain_idx  on documents (task_id, domain);
create index if not exists documents_payload_idx on documents using gin (payload jsonb_path_ops);
create index if not exists documents_fts_idx     on documents
    using gin (to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(text, '')));

-- ------------------------------------------------------------------- медиа

create table if not exists media_assets (
    id          uuid primary key default gen_random_uuid(),
    sha256      bytea not null unique,           -- глобальный дедуп по контенту
    media_type  text not null default 'image',   -- image|video|audio|document
    mime        text,
    bytes       bigint,
    width       int,
    height      int,
    duration_s  real,
    blob_ref    text not null,
    thumb_ref   text,
    phash       bigint,                          -- Фаза 1
    exif        jsonb not null default '{}',
    ocr_text    text,                            -- Фаза 1
    caption     text,                            -- Фаза 1
    source_url  text,
    first_seen  timestamptz not null default now()
);
create index if not exists media_type_idx on media_assets (media_type);

create table if not exists document_media (
    document_id uuid not null references documents(id) on delete cascade,
    media_id    uuid not null references media_assets(id) on delete cascade,
    role        text not null default 'inline',
    position    int  not null default 0,
    primary key (document_id, media_id, role, position)
);
create index if not exists document_media_media_idx on document_media (media_id);

-- ------------------------------------------------------- сущности и ссылки

create table if not exists entities (
    id          uuid primary key default gen_random_uuid(),
    name        text not null,
    type        text,
    wikidata_id text,
    aliases     text[] not null default '{}',
    unique (name, type)
);

create table if not exists document_entities (
    document_id uuid not null references documents(id) on delete cascade,
    entity_id   uuid not null references entities(id) on delete cascade,
    salience    real,
    mentions    int not null default 1,
    primary key (document_id, entity_id)
);

create table if not exists links (
    id              bigserial primary key,
    run_id          uuid not null references runs(id) on delete cascade,
    src_document_id uuid references documents(id) on delete cascade,
    dst_url         text not null,
    dst_document_id uuid references documents(id) on delete set null,
    rel             text
);
create index if not exists links_run_idx on links (run_id);

-- ------------------------------------------------------------- провенанс

create table if not exists fetch_log (
    id       bigserial primary key,
    run_id   uuid not null references runs(id) on delete cascade,
    url      text not null,
    status   int,
    tier     text,
    bytes    int,
    ms       int,
    error    text,
    raw_ref  text,
    at       timestamptz not null default now()
);
create index if not exists fetch_log_run_idx on fetch_log (run_id, at desc);
