-- Подтемы: из чего состоит корпус по задаче.
-- Пересчитываются целиком, поэтому связь документа с подтемой — обнуляемая ссылка.

create table if not exists topics (
    id          uuid primary key default gen_random_uuid(),
    task_id     uuid not null references tasks(id) on delete cascade,
    label       text not null default '',
    keywords    text[] not null default '{}',
    size        int not null default 0,
    centroid    vector(384),
    avg_relevance real,
    representative_document_id uuid references documents(id) on delete set null,
    created_at  timestamptz not null default now()
);
create index if not exists topics_task_idx on topics (task_id, size desc);

alter table documents
    add column if not exists topic_id uuid references topics(id) on delete set null;
create index if not exists documents_topic_idx on documents (topic_id);
