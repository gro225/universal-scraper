"""CLI: harvester migrate | run | stats | search | connectors."""

from __future__ import annotations

import asyncio
import logging
import pathlib
import uuid
import warnings

import typer
from rich.console import Console
from rich.table import Table

from . import db, storage
from . import embeddings as emb
from . import search as search_mod
from .config import settings
from .models import Task
from .models import Task as TaskModel
from .orchestrator import execute

app = typer.Typer(add_completion=False, help="Universal Harvester — Фаза 0")
console = Console()


def _guard_infra(coro_factory):
    """Понятное сообщение вместо трёхэкранного трейсбека, когда инфраструктура не поднята."""
    try:
        asyncio.run(coro_factory())
    except (OSError, ConnectionError) as exc:
        console.print(
            f"[red]Не могу подключиться к инфраструктуре:[/] {exc}\n"
            "Похоже, не подняты сервисы. Запустите [bold]make up[/] "
            "(и убедитесь, что Docker работает)."
        )
        raise typer.Exit(1) from None


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # -v должен включать логи краулера, а не сетевую требуху библиотек
    for noisy in ("httpx", "httpcore", "botocore", "aiobotocore", "boto3", "urllib3",
                  "asyncio", "redis", "trafilatura", "htmldate", "tzlocal", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # trafilatura ругается на каждую страницу без текста — это норма, не сигнал
    logging.getLogger("trafilatura").setLevel(logging.ERROR)
    warnings.filterwarnings("ignore", category=UserWarning, module="PIL")


@app.command()
def migrate() -> None:
    """Применить миграции схемы."""

    async def _run() -> None:
        applied = await db.migrate()
        await storage.ensure_bucket()
        await search_mod.ensure_index()
        console.print(
            f"[green]применено миграций:[/] {len(applied)}"
            + (f" ({', '.join(applied)})" if applied else " (схема актуальна)")
        )
        await db.close()
        await storage.close()

    _guard_infra(_run)


@app.command()
def run(
    task_file: pathlib.Path = typer.Argument(..., help="YAML с Task DSL"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Прогнать задачу целиком: discovery -> crawl -> extract -> хранилище."""
    _setup_logging(verbose)
    task = Task.from_yaml(task_file.read_text())

    async def _run() -> None:
        await db.migrate()
        task_id = await db.upsert_task(task)
        run_id = await db.create_run(task_id, task)
        console.print(f"[bold]задача[/] {task.name}  task_id={task_id}  run_id={run_id}")
        stats = await execute(task, task_id=task_id, run_id=run_id)
        _print_stats(stats)
        await db.close()
        await storage.close()

    _guard_infra(_run)


@app.command()
def grab(
    urls: list[str] = typer.Argument(..., help="Адреса страниц или сайтов"),
    depth: int = typer.Option(0, "--depth", "-d",
                              help="0 — только эти страницы; больше — идти по ссылкам"),
    everywhere: bool = typer.Option(False, "--everywhere",
                                    help="Не ограничиваться доменами исходных адресов"),
    no_media: bool = typer.Option(False, "--no-media", help="Не скачивать изображения"),
    max_pages: int = typer.Option(None, "--max-pages", help="Предел страниц"),
    name: str = typer.Option(None, "--name", help="Имя задачи (по умолчанию генерится)"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Собрать конкретные страницы или сайты — без YAML и без поиска по теме.

    \b
    harvester grab https://example.com/article
    harvester grab example.com --depth 2
    harvester grab a.ru b.ru --depth 1 --no-media
    """
    _setup_logging(verbose)
    task = Task.for_urls(
        urls, name=name, depth=depth, same_domain=not everywhere,
        media=not no_media, max_pages=max_pages,
    )

    async def _run() -> None:
        await db.migrate()
        task_id = await db.upsert_task(task)
        run_id = await db.create_run(task_id, task)
        console.print(
            f"[bold]{task.name}[/]  адресов: {len(task.discovery.seeds)}  "
            f"глубина: {task.crawl.scope.max_depth}  run_id={run_id}"
        )
        _print_stats(await execute(task, task_id=task_id, run_id=run_id), grab=True)
        await db.close()
        await storage.close()

    _guard_infra(_run)


@app.command()
def embed(
    task: str = typer.Option(None, "--task", help="Имя задачи; без него — все"),
    limit: int = typer.Option(5000, "--limit", help="Сколько документов обработать"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Досчитать эмбеддинги и релевантность для уже собранного.

    Нужно после того, как фильтр появился: документы, собранные раньше, оценок не имеют.
    """
    _setup_logging(verbose)

    async def _run() -> None:
        anchors_cache: dict = {}
        done = failed = 0
        with console.status("считаю эмбеддинги…") as status:
            while done + failed < limit:
                batch = await db.documents_without_embedding(
                    await _task_id_by_name(task) if task else None,
                    limit=min(settings().embed_batch * 4, limit - done - failed),
                )
                if not batch:
                    break
                vectors = await emb.embed_async(
                    [emb.document_text(d["title"], d["text"]) for d in batch]
                )
                rows = []
                for doc, vec in zip(batch, vectors, strict=True):
                    tid = doc["task_id"]
                    if tid not in anchors_cache:
                        record = await db.get_task(tid)
                        anchors_cache[tid] = (
                            await emb.anchors_for(TaskModel.model_validate(record["spec"]))
                            if record else None
                        )
                    anchors = anchors_cache[tid]
                    rows.append((doc["id"], vec, anchors.score(vec) if anchors else None))
                await db.save_embeddings(rows)
                done += len(rows)
                status.update(f"посчитано {done}…")

        console.print(f"[green]готово:[/] {done} документов")
        if failed:
            console.print(f"[yellow]не удалось: {failed}[/]")
        await db.close()
        await storage.close()

    _guard_infra(_run)


@app.command()
def rescore(task: str = typer.Argument(..., help="Имя задачи")) -> None:
    """Переоценить релевантность после правки include_hints / exclude_hints.

    Векторы документов не пересчитываются — только якоря, так что это мгновенно.
    Порядок: поправить YAML -> harvester rescore <задача> -> harvester relevance <задача>.
    """

    async def _run() -> None:
        task_id = await _task_id_by_name(task)
        if not task_id:
            console.print(f"[red]задача {task!r} не найдена[/]")
            raise typer.Exit(1)
        record = await db.get_task(task_id)
        spec = TaskModel.model_validate(record["spec"])
        anchors = await emb.anchors_for(spec)
        if anchors is None:
            console.print("[yellow]у задачи нет темы — оценивать не от чего[/]")
            raise typer.Exit(1)

        positive, negative = emb.anchor_texts(spec)
        console.print(f"[dim]ищем:[/] {positive}")
        console.print(f"[dim]не ищем:[/] {negative or '— (без антипримера оценка слабее)'}")
        n = await db.rescore_task(task_id, anchors.positive, anchors.negative)
        console.print(f"[green]переоценено:[/] {n} документов")
        await db.close()
        await storage.close()

    _guard_infra(_run)


@app.command()
def topics(
    task: str = typer.Argument(..., help='Имя задачи'),
    threshold: float = typer.Option(None, '--threshold', '-t',
                                    help='Косинусный порог слияния, по умолчанию 0.75'),
    show: int = typer.Option(12, '--show', help='Сколько подтем показать'),
    by: str = typer.Option('relevance', '--by',
                           help='Сортировка: relevance (по умолчанию) или size'),
    min_size: int = typer.Option(3, '--min-size',
                                 help='Не показывать группы мельче'),
    verbose: bool = typer.Option(False, '--verbose', '-v'),
) -> None:
    """Разобрать тему на подтемы: из чего состоит собранный корпус.

    Отвечает на вопрос «что тут вообще есть» — в отличие от релевантности,
    которая отвечает «насколько это по теме». Целую подтему видно одним взглядом,
    и её можно принять или отбросить целиком."""
    _setup_logging(verbose)

    async def _run() -> None:
        task_id = await _task_id_by_name(task)
        if not task_id:
            console.print(f'[red]задача {task!r} не найдена[/]')
            raise typer.Exit(1)

        rows = await db.documents_for_clustering(task_id)
        if len(rows) < 3:
            console.print('[yellow]мало документов с векторами — '
                          'сначала `harvester embed`[/]')
            raise typer.Exit(1)

        import numpy as np

        from .analysis import DEFAULT_THRESHOLD, analyze

        vectors = np.array(
            [[float(x) for x in r['vec'].strip('[]').split(',')] for r in rows]
        )
        texts = [f"{r['title']} {r['body'] or ''}" for r in rows]
        with console.status(f'разбираю {len(rows)} документов…'):
            found = analyze(vectors, texts,
                            threshold=threshold or DEFAULT_THRESHOLD)
            payload = []
            for t in found:
                rels = [rows[i]['relevance'] for i in t.members
                        if rows[i]['relevance'] is not None]
                payload.append({
                    'label': t.label, 'keywords': t.keywords, 'size': t.size,
                    'centroid': t.centroid,
                    'avg_relevance': sum(rels) / len(rels) if rels else None,
                    'representative': rows[t.representative]['id'],
                    'members': [rows[i]['id'] for i in t.members],
                })
            await db.replace_topics(task_id, payload)

        # по умолчанию сверху то, что по теме, а не то, чего больше:
        # крупнейшие подтемы обычно как раз мусорные
        ordered = sorted(
            payload,
            key=lambda t: (t['avg_relevance'] if t['avg_relevance'] is not None else -9),
            reverse=True,
        ) if by == 'relevance' else payload
        # средняя по группе из двух документов — шум, в обзор она не нужна
        ordered = [t for t in ordered if t['size'] >= min_size] or ordered

        table = Table('док.', 'релев.', 'подтема', 'представитель')
        for t in ordered[:show]:
            rel = f"{t['avg_relevance']:+.2f}" if t['avg_relevance'] is not None else '—'
            rep = next(r['title'] for r in rows if r['id'] == t['representative'])
            table.add_row(str(t['size']), rel, t['label'][:46], (rep or '')[:40])
        console.print(table)
        tail = len(payload) - show
        more = f', ещё {tail} мельче' if tail > 0 else ''
        console.print(f'[dim]подтем всего {len(payload)}{more}[/]')
        await db.close()
        await storage.close()

    _guard_infra(_run)


@app.command()
def relevance(task: str = typer.Argument(..., help="Имя задачи")) -> None:
    """Распределение оценок — чтобы выбрать порог глазами, а не наугад."""

    async def _run() -> None:
        task_id = await _task_id_by_name(task)
        if not task_id:
            console.print(f"[red]задача {task!r} не найдена[/]")
            raise typer.Exit(1)
        buckets = await db.relevance_histogram(task_id)
        if not buckets:
            console.print("[yellow]оценок нет — сначала `harvester embed`[/]")
            raise typer.Exit(1)

        peak = max(b["n"] for b in buckets)
        table = Table("диапазон", "документов", "")
        for b in buckets:
            bar = "█" * max(1, round(30 * b["n"] / peak))
            table.add_row(f"{b['lo']:+.2f} … {b['hi']:+.2f}", str(b["n"]), bar)
        console.print(table)
        total = sum(b["n"] for b in buckets)
        for thr in (0.0, 0.1, 0.2):
            kept = sum(b["n"] for b in buckets if float(b["lo"]) >= thr)
            console.print(f"  порог {thr:+.1f} -> останется {kept} из {total} "
                          f"({100 * kept / total:.0f}%)")
        await db.close()
        await storage.close()

    _guard_infra(_run)


async def _task_id_by_name(name: str | None):
    if not name:
        return None
    for t in await db.list_tasks(500):
        if t["name"] == name:
            return t["id"]
    return None


@app.command()
def stats(run_id: uuid.UUID) -> None:
    """Показать статистику прогона."""

    async def _run() -> None:
        record = await db.get_run(run_id)
        if not record:
            console.print("[red]прогон не найден[/]")
            raise typer.Exit(1)
        merged = {**(record["stats"] or {}), **await db.run_stats(run_id)}
        console.print(f"[bold]state:[/] {record['state']}")
        _print_stats(merged)
        await db.close()
        await storage.close()

    _guard_infra(_run)


@app.command()
def search(
    query: str,
    task_id: str = typer.Option(None),
    limit: int = 10,
    semantic: bool = typer.Option(False, "--semantic", "-s",
                                  help="Искать по смыслу, а не по словам"),
) -> None:
    """Поиск по собранному: по словам (Meilisearch) или по смыслу (эмбеддинги)."""

    async def _run() -> None:
        if semantic:
            vector = (await emb.embed_async([query]))[0]
            hits = await db.semantic_search(
                vector, task_id=uuid.UUID(task_id) if task_id else None, limit=limit
            )
            table = Table("близость", "релев.", "домен", "заголовок")
            for h in hits:
                rel = f"{h['relevance']:+.2f}" if h["relevance"] is not None else "—"
                table.add_row(f"{h['similarity']:.3f}", rel,
                              (h["domain"] or "")[:22], (h["title"] or "")[:48])
            console.print(table)
            await db.close()
            await storage.close()
            return

        res = await search_mod.search(query, task_id=task_id, limit=limit)
        table = Table("домен", "заголовок", "url")
        for hit in res.get("hits", []):
            table.add_row(
                hit.get("domain", ""), (hit.get("title") or "")[:60],
                (hit.get("source_url") or "")[:70],
            )
        console.print(table)
        console.print(f"[dim]найдено: {res.get('estimatedTotalHits', 0)}[/]")

    _guard_infra(_run)


@app.command()
def connectors() -> None:
    """Список зарегистрированных коннекторов."""
    from . import connectors as reg

    console.print("\n".join(f"• {name}" for name in reg.available()))


def _print_stats(stats: dict, *, grab: bool = False) -> None:
    table = Table("метрика", "значение")
    for key in (
        "seeds", "fetched", "documents", "duplicates", "domains",
        "media", "media_new", "media_skipped", "errors", "skipped_robots",
        "bytes", "stop_reason",
    ):
        if key in stats and stats[key] is not None:
            table.add_row(key, str(stats[key]))
    console.print(table)

    if grab:
        # критерий Фазы 0 про массовый сбор — к точечному захвату он неприменим
        docs = stats.get("documents", 0)
        console.print(
            f"[green]собрано документов: {docs}[/]" if docs else
            "[yellow]ничего не сохранено — страница пустая, "
            "закрыта robots.txt или отдала не HTML[/]"
        )
        return

    docs, media = stats.get("documents", 0), stats.get("media", 0)
    ok = docs >= 200 and media >= 100
    verdict = "[green]критерий Фазы 0 выполнен[/]" if ok else (
        f"[yellow]критерий Фазы 0 не достигнут (нужно ≥200 док. и ≥100 изобр.; "
        f"есть {docs}/{media})[/]"
    )
    console.print(verdict)


if __name__ == "__main__":
    app()
