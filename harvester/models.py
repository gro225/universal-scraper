"""Task DSL и runtime-модели.

Task DSL — то, что пишет пользователь (YAML) или собирает UI. Остальное — внутренние
структуры, которые ходят между слоями:
SourceRef -> RawCapture -> ExtractResult -> DocumentDraft / MediaRef.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field, model_validator

# --------------------------------------------------------------------------- Task DSL


class QuerySpec(BaseModel):
    # необязателен: когда URL заданы явно, искать тему по поисковикам незачем
    subject: str | None = None
    intent: str | None = None
    languages: list[str] = Field(default_factory=lambda: ["ru", "en"])
    include_hints: list[str] = Field(default_factory=list)
    exclude_hints: list[str] = Field(default_factory=list)


class DiscoverySpec(BaseModel):
    connectors: list[str] = Field(default_factory=lambda: ["searxng"])
    seeds: list[str] = Field(default_factory=list)
    extra_queries: list[str] = Field(default_factory=list)
    max_seeds: int = 200
    pages_per_query: int = 2


class CrawlScope(BaseModel):
    follow: Literal["none", "seed_domain", "seed_domain_plus_outlinks"] = "seed_domain"
    max_depth: int = 2
    allow: list[str] = Field(default_factory=lambda: ["*"])
    deny: list[str] = Field(
        default_factory=lambda: ["*/login*", "*/cart*", "*/signup*", "*.exe", "*.zip"]
    )


class Politeness(BaseModel):
    respect_robots: bool = True
    delay_per_domain_ms: int = 1500
    max_concurrency_per_domain: int = 2


class Budget(BaseModel):
    max_pages: int = 2000
    max_wall_clock_s: int = 3600
    max_bytes: int = 5 * 1024**3


class CrawlSpec(BaseModel):
    scope: CrawlScope = Field(default_factory=CrawlScope)
    politeness: Politeness = Field(default_factory=Politeness)
    budget: Budget = Field(default_factory=Budget)
    # Тир T1: auto — решает эвристика по объёму видимого текста, always — рендерить
    # всё подряд (дорого, для заведомо скриптовых сайтов), never — никогда.
    render: Literal["auto", "always", "never"] = "auto"


class MediaSpec(BaseModel):
    download: bool = True
    types: list[str] = Field(default_factory=lambda: ["image"])
    min_width: int = 200
    min_height: int = 200
    max_per_document: int = 20


class ExtractSpec(BaseModel):
    want: list[str] = Field(
        default_factory=lambda: ["article_text", "images", "structured_ld", "links"]
    )
    min_text_chars: int = 250
    media: MediaSpec = Field(default_factory=MediaSpec)


class QualitySpec(BaseModel):
    """Оценка качества найденного.

    Порог здесь — **не** фильтр записи: оценка всегда сохраняется, а порог применяется
    при чтении. Значит его можно поменять задним числом, ничего не перекрауливая.
    """

    embed: bool = True
    # с чем сравнивать: None -> показывать всё, оценку только проставить
    relevance_threshold: float | None = None


class Task(BaseModel):
    """Полное описание задачи. Версионируется, хранится, воспроизводится."""

    name: str
    version: int = 1
    query: QuerySpec = Field(default_factory=QuerySpec)
    discovery: DiscoverySpec = Field(default_factory=DiscoverySpec)
    crawl: CrawlSpec = Field(default_factory=CrawlSpec)
    extract: ExtractSpec = Field(default_factory=ExtractSpec)
    quality: QualitySpec = Field(default_factory=QualitySpec)

    @model_validator(mode="after")
    def _needs_subject_or_seeds(self) -> Task:
        """Задача осмысленна, если есть что искать ИЛИ откуда начинать."""
        if not self.query.subject and not self.discovery.seeds:
            raise ValueError(
                "нужно указать либо query.subject (искать по теме), "
                "либо discovery.seeds (обойти конкретные адреса)"
            )
        return self

    @classmethod
    def from_yaml(cls, text: str) -> Task:
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise ValueError("ожидался YAML-объект с описанием задачи")
        # допускаем как `name:`, так и `task:` в качестве идентификатора
        if "name" not in data and "task" in data:
            data["name"] = data.pop("task")
        return cls.model_validate(data)

    @classmethod
    def for_urls(
        cls,
        urls: Sequence[str],
        *,
        name: str | None = None,
        depth: int = 0,
        same_domain: bool = True,
        media: bool = True,
        max_pages: int | None = None,
        subject: str | None = None,
    ) -> Task:
        """Задача «обойти вот эти адреса» — без поиска по теме.

        depth=0 — только сами страницы; глубже — ходим по ссылкам, по умолчанию
        не выходя за домены исходных адресов.
        """
        urls = [u.strip() for u in urls if u and u.strip()]
        if not urls:
            raise ValueError("не задано ни одного URL")
        urls = [u if "://" in u else f"https://{u}" for u in urls]

        if depth <= 0:
            follow = "none"
        elif same_domain:
            follow = "seed_domain"
        else:
            follow = "seed_domain_plus_outlinks"

        return cls(
            name=name or _autoname(urls),
            query=QuerySpec(subject=subject, intent="обход заданных адресов"),
            discovery=DiscoverySpec(connectors=["generic_web"], seeds=urls,
                                    max_seeds=max(len(urls), 200)),
            crawl=CrawlSpec(
                scope=CrawlScope(follow=follow, max_depth=max(depth, 0)),
                budget=Budget(max_pages=max_pages or max(len(urls), 50 * max(depth, 1))),
            ),
            # адрес назван человеком — сохраняем страницу, даже если текста мало
            extract=ExtractSpec(min_text_chars=0, media=MediaSpec(download=media)),
            # адреса названы явно, темы нет — оценивать релевантность не от чего
            quality=QualitySpec(embed=bool(subject)),
        )


def _autoname(urls: Sequence[str]) -> str:
    """Читаемое и при этом уникальное имя: домен + отпечаток набора + время."""
    host = (urlsplit(urls[0]).hostname or "urls").replace("www.", "").replace(".", "-")
    digest = hashlib.sha1("\n".join(sorted(urls)).encode()).hexdigest()[:6]
    stamp = datetime.now(UTC).strftime("%m%d-%H%M")
    suffix = f"+{len(urls) - 1}" if len(urls) > 1 else ""
    return f"grab-{host}{suffix}-{stamp}-{digest}"


# --------------------------------------------------------------------------- runtime


class RunState(StrEnum):
    PLANNING = "planning"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class SourceRef(BaseModel):
    """Точка входа для обхода: URL + откуда он взялся."""

    url: str
    depth: int = 0
    priority: float = 0.5
    connector: str = "generic_web"
    discovered_by: str | None = None
    hint: dict[str, Any] = Field(default_factory=dict)


class RawCapture(BaseModel):
    """Сырой ответ. Уходит в Bronze до всякого парсинга."""

    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    body: bytes
    content_type: str | None = None
    elapsed_ms: int = 0
    fetched_at: datetime
    tier: str = "t0"

    model_config = {"arbitrary_types_allowed": True}


class MediaRef(BaseModel):
    """Ссылка на бинарь, найденная в документе. Скачивается отдельной очередью."""

    url: str
    media_type: str = "image"
    role: str = "inline"
    position: int = 0
    alt: str | None = None


class DocumentDraft(BaseModel):
    """Нормализованный документ до записи в БД."""

    kind: str = "page"
    source_url: str
    canonical_url: str | None = None
    domain: str = ""
    connector: str = "generic_web"
    lang: str | None = None
    title: str | None = None
    text: str = ""
    author: str | None = None
    published_at: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    raw_ref: str = ""
    content_hash: bytes = b""
    simhash: int | None = None
    relevance: float | None = None


class ExtractResult(BaseModel):
    document: DocumentDraft | None = None
    media: list[MediaRef] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
