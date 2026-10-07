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
from . import search as search_mod
from .models import Task
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
def search(query: str, task_id: str = typer.Option(None), limit: int = 10) -> None:
    """Полнотекстовый поиск по собранному."""

    async def _run() -> None:
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
