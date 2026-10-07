-- Векторный слой: семантическая оценка релевантности и поиск по смыслу.
-- Размерность 384 — модель paraphrase-multilingual-MiniLM-L12-v2 (см. config.embed_dim).

create extension if not exists vector;

alter table documents add column if not exists embedding vector(384);

-- HNSW: быстрее ivfflat на выборке и не требует обучения на данных.
-- Косинус, потому что векторы нормированы и сравниваем по направлению.
create index if not exists documents_embedding_idx
    on documents using hnsw (embedding vector_cosine_ops);

-- Отбор по порогу релевантности — частый запрос из UI и API
create index if not exists documents_relevance_idx
    on documents (task_id, relevance desc nulls last);
