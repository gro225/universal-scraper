# Universal Harvester — Фаза 0

Универсальная система парсинга: **тема → пауки → единое хранилище → аналитика**.
Пользователь описывает задачу (`Sol Lemon`), система сама находит источники, обходит их,
извлекает текст и изображения и складывает всё в общее хранилище с единой моделью данных.

Руководство пользователя: **[USAGE.md](USAGE.md)** — запуск, Task DSL, CLI, API, эксплуатация.
Полная архитектура и дорожная карта: **[ARCHITECTURE.md](ARCHITECTURE.md)**.
Это Фаза 0 — работающий скелет, на котором проверяются архитектурные решения.

---

## Быстрый старт

```bash
git clone <repo> && cd universal-scraper
make up                                   # postgres, redis, minio, meilisearch, searxng, api

harvester grab https://example.com/page   # собрать конкретный адрес, без YAML
make run TASK=tasks/sol-lemon.yaml        # найти по теме: discovery -> crawl -> extract
```

| Сервис | URL | Что смотреть |
|---|---|---|
| API | http://localhost:8080/docs | постановка задач, прогресс, поиск |
| MinIO | http://localhost:9001 | Bronze-слой глазами (`minioadmin`/`minioadmin`) |
| Meilisearch | http://localhost:7700 | полнотекстовый индекс |
| SearXNG | http://localhost:8888 | discovery-поисковик |

Локально без Docker (инфраструктура всё равно нужна из compose):

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env
.venv/bin/harvester migrate
.venv/bin/harvester run tasks/sol-lemon.yaml -v
```

---

## Критерий готовности Фазы 0

Один прогон эталонной задачи даёт **≥ 200 документов и ≥ 100 изображений** в едином
хранилище, и всё это ищется одним запросом. CLI печатает вердикт сам:

```bash
harvester run tasks/sol-lemon.yaml -v
harvester search "Sol Lemon"
harvester stats <run_id>
```

---

## Что уже работает

| Слой | Фаза 0 | Модуль |
|---|---|---|
| Интерфейс задач | Task DSL (YAML), точечный захват `grab`, REST API, CLI, трей, веб-страница | `models.py`, `api/main.py`, `cli.py`, `tray.py` |
| Планировщик | шаблонное расширение запроса, discovery через SearXNG | `connectors/searxng.py` |
| Оркестрация | FSM прогона в Postgres, фронтир в Redis, бюджеты | `orchestrator/run.py`, `frontier.py` |
| Fetch | T0 (HTTP), robots.txt, вежливость, circuit breaker, SSRF-guard | `connectors/generic_web.py` |
| Извлечение | JSON-LD/OpenGraph → trafilatura → медиа и ссылки | `extractors/generic.py` |
| Релевантность | эмбеддинги (fastembed/ONNX) + pgvector, оценка «тема минус антипример», поиск по смыслу | `embeddings.py`, `db.py` |
| Разбор темы | кластеризация корпуса на подтемы, названия по TF-IDF | `analysis.py` |
| Задел под Фазу 2 | минификация DOM для LLM-промпта (53–87% экономии) | `extractors/minify.py` |
| Медиа | скачивание, дедуп по sha256, размеры, EXIF, webp-превью | `media/pipeline.py` |
| Хранилище | Bronze (MinIO) + Silver (Postgres) + Meilisearch | `storage.py`, `db.py`, `search.py` |
| Аналитика | `/items`, `/search`, `/media/{id}`, SSE-прогресс | `api/main.py` |

## Чего в Фазе 0 намеренно нет

Это не пробелы, а следующие фазы (см. ARCHITECTURE.md §14):

- **Фаза 1** — осталось: pHash/OCR/CLIP у медиа, near-dup дедуп по simhash (сам simhash
  уже считается и пишется). Рендер SPA (crawl4ai) и фильтр релевантности — сделаны.
- **Фаза 2** — коннекторы (RSS, Reddit, YouTube, Telegram), LLM-экстрактор с индукцией
  CSS-рецептов, дизамбигуация темы, расписания, event bus.
- **Фаза 3** — начата: подтемы корпуса есть. Осталось: граф сущностей, таймлайн,
  Gold-слой (Parquet/Iceberg), дашборды, алерты на новые находки.

---

## Что показал реальный прогон

Скелет прогонялся на живом интернете по `tasks/sol-lemon.yaml`. Результат одного прогона:

| Метрика | Значение |
|---|---|
| документов | **477** (283 из них с прикреплёнными изображениями) |
| изображений | **727** уникальных по контенту, 174 МБ |
| доменов | 112 |
| фетчей | 715, из них 14% с ошибкой (в основном 403) |
| темп | ~81 фетч/мин при задержке 1.5 с на домен |
| типы документов | page 283, article 86, profile 66, product 31, media_page 11 |

Критерий Фазы 0 (≥200 документов, ≥100 изображений) выполнен с запасом.
Три наблюдения, которые стоит знать до запуска на своей теме:

**1. Дизамбигуация нужна раньше, чем кажется.** Запрос `"Sol Lemon"` даёт выдачу, где
рядом стоят настоящая находка (`Lemon Sol — Environmental Architecture`, readdork.com),
арт-объект, браслет из яшмы и чистящее средство Pine-Sol Lemon Fresh с target.com.
Хранилище засоряется, бюджет уходит в никуда. Это ровно тот сценарий, ради которого
в архитектуре есть пробный обход с кластеризацией перед основным (ARCHITECTURE.md §5) —
в Фазе 0 его заменяют ручные `include_hints`/`exclude_hints`.

**2. Голый HTTP упирается в антибот-защиту.** 14% фетчей — 403 и таймауты от сайтов,
требующих JS-рендеринга или блокирующих датацентровые адреса. Это не баг краулера,
а граница тира T0; её снимает Playwright-тир Фазы 1.

**3. Вежливость дороже сети.** Узкое место — не пропускная способность, а задержка
в 1.5 с на домен. Поэтому воркер не ждёт занятый домен: он берёт пачку кандидатов из
фронтира и выбирает первый URL со свободным доменом, а остальные возвращает в очередь
(`orchestrator/run.py:_claim`). Без этого восемь воркеров простаивали на двух-трёх
доменах, и темп падал втрое — с ~80 до ~25 фетчей/мин.

---

## Ключевые архитектурные решения в коде

**1. Сырьё сохраняется до парсинга.** `orchestrator/run.py:_process_url` кладёт ответ в Bronze
раньше, чем вызывает экстрактор. Поменяли логику извлечения — перепарсили архив, не трогая
чужие сайты. Это то, что делает эксперименты дешёвыми.

**2. Текст и картинка — граждане одного класса.** `documents` и `media_assets` связаны M:N,
у обоих есть текстовое представление. В Фазе 1 сюда добавятся `ocr_text` и `caption`, и один
поисковый запрос начнёт находить и статьи, и фото.

**3. Дедуп на трёх уровнях.** URL (`hashing.canonical_url`), контент (`sha256`, уникальный
индекс по `(task_id, content_hash)`), картинки (`sha256` глобально, `phash` — Фаза 1).
Дубль документа не выбрасывается: его URL доклеивается в `source_urls` — важно для аналитики
«сколько источников это перепечатали».

**4. Плагин вместо форка.** Новый источник = класс с `matches/discover/fetch/extract`
(`connectors/base.py`) + `register(...)`. Ядро о конкретных сайтах не знает.

**5. Бюджеты жёсткие.** `max_pages`, `max_bytes`, `max_wall_clock_s` из DSL останавливают
прогон и пишут `stop_reason`, а не жгут ресурсы молча.

---

## Формат задачи

```yaml
task: sol-lemon
query:
  subject: "Sol Lemon"
  include_hints: ["music", "artist"]      # сужают выдачу до одной интерпретации
discovery:
  connectors: [searxng, generic_web]
  max_seeds: 200
crawl:
  scope: {follow: seed_domain_plus_outlinks, max_depth: 2}
  politeness: {respect_robots: true, delay_per_domain_ms: 1500}
  budget: {max_pages: 2000, max_wall_clock_s: 3600}
extract:
  want: [article_text, images, structured_ld, links]
  media: {download: true, min_width: 200, min_height: 200}
```

Полный пример с комментариями — `tasks/sol-lemon.yaml`.

---

## API

```bash
curl -X POST localhost:8080/tasks --data-binary @tasks/sol-lemon.yaml \
     -H 'Content-Type: text/yaml'
curl -X POST localhost:8080/tasks/$TASK_ID/runs
curl -N localhost:8080/runs/$RUN_ID/events           # SSE-прогресс
curl "localhost:8080/items?task_id=$TASK_ID&limit=20"
curl -X POST localhost:8080/search -H 'Content-Type: application/json' \
     -d '{"q": "Sol Lemon", "task_id": "'$TASK_ID'"}'
```

---

## Данные напрямую

```sql
-- документы с числом картинок
select d.domain, count(*) docs, count(dm.media_id) media
  from documents d
  left join document_media dm on dm.document_id = d.id
 group by 1 order by 2 desc;

-- перепечатки: один контент, много источников
select title, array_length(source_urls, 1) as sources
  from documents where array_length(source_urls, 1) > 1
 order by 2 desc;
```

Bronze лежит в MinIO как `raw/{task_id}/{run_id}/{sha[:2]}/{sha}` и
`media/{sha[:2]}/{sha}` — content-addressed, поэтому одна и та же картинка
с 50 сайтов хранится один раз.

---

## Разработка

```bash
make test      # pytest
make lint      # ruff
make shell     # bash внутри контейнера api
make reset     # снести все данные (docker compose down -v)
```

---

## Правовое

Краулер по умолчанию соблюдает `robots.txt`, держит паузы между запросами к одному домену
и не обходит авторизацию или paywall. Перед запуском укажите в `USER_AGENT` рабочий контакт —
этого ждут администраторы сайтов. Ответственность за соответствие ToS конкретных источников
и законодательству о персональных данных лежит на операторе системы.
