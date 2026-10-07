"""T1-фетчер: рендер страницы настоящим браузером через crawl4ai.

Зачем отдельный тир. T0 (httpx) отдаёт то, что прислал сервер, а половина
современного веба присылает пустой каркас и дорисовывает содержимое в браузере.
Такая страница проходит весь конвейер и оседает документом из 200 символов меню.

Чего тут НЕ происходит — извлечения. crawl4ai умеет отдавать markdown, но
бойлерплейт он чистит хуже trafilatura, а trafilatura у нас уже стоит ступенью 2
в extractors/generic.py. Поэтому наружу уходит обычный RawCapture с отрендеренным
HTML: дальше работает ровно тот же экстрактор, что и для T0. Один путь извлечения,
два способа добыть HTML.

Зависимость необязательная: `pip install -e '.[render]'` и один раз `crawl4ai-setup`
(ставит браузеры Playwright). Пока render_enabled=False, crawl4ai не импортируется.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import UTC, datetime
from typing import Any

from selectolax.parser import HTMLParser

from ..config import settings
from ..models import RawCapture, SourceRef

log = logging.getLogger("harvester.render")


class RendererUnavailable(RuntimeError):
    """crawl4ai не установлен или браузер не поднялся."""


class RenderError(RuntimeError):
    """Отрендерить конкретную страницу не вышло."""


# --------------------------------------------------------------- эвристика T1

# Каркасы SPA: пустой контейнер, который заполняется только скриптом.
_SPA_MARKERS = (
    'id="root"',
    'id="app"',
    'id="__next"',
    'id="__nuxt"',
    "data-reactroot",
    "ng-version",
)
_NOSCRIPT_MARKERS = (
    "enable javascript",
    "включите javascript",
    "javascript is required",
    "requires javascript",
)
_BLOCKED_STATUSES = frozenset({403, 429})


def visible_text_len(html: str) -> int:
    """Сколько текста реально увидит человек. Дёшево: тот же selectolax, что в экстракторе."""
    try:
        tree = HTMLParser(html)
    except Exception:
        return len(html)
    for node in tree.css("script, style, nav, header, footer, noscript, svg, template"):
        node.decompose()
    body = tree.body
    return len(body.text(separator=" ", strip=True)) if body else 0


def needs_render(raw: RawCapture, ref: SourceRef | None = None) -> str | None:
    """Причина эскалации в T1, либо None. Решение принимается по сырью T0.

    Порядок важен: явная подсказка в задаче бьёт любую эвристику — и когда просит
    рендерить, и когда запрещает (иначе на дорогом домене легко сжечь бюджет).
    """
    s = settings()
    hint = (ref.hint or {}).get("render") if ref else None
    if hint is False:
        return None
    if hint is True or hint == "always":
        return "hint"

    if raw.tier != "t0":
        return None  # уже отрендерено — второй раз незачем

    if raw.status in _BLOCKED_STATUSES:
        # Рендер сам по себе не обходит защиту — помогает только когда блок
        # выставлен скриптом проверки. Против Cloudflare нужен stealth-фетчер.
        return "blocked" if s.render_on_block else None

    if raw.status >= 400 or not _looks_html(raw):
        return None

    html = raw.body.decode(errors="replace")
    text_len = visible_text_len(html)
    if text_len >= s.render_min_text_chars:
        return None

    lowered = html[:200_000].lower()
    if any(marker in lowered for marker in _NOSCRIPT_MARKERS):
        return "noscript"
    if any(marker in lowered for marker in _SPA_MARKERS):
        return "spa_shell"
    return "thin" if text_len < s.render_min_text_chars else None


def _looks_html(raw: RawCapture) -> bool:
    ct = (raw.content_type or "").lower()
    return "html" in ct or (not ct and raw.body[:512].lstrip().startswith(b"<"))


# ------------------------------------------------------------- жизненный цикл

# Браузер поднимается один на процесс: старт Chromium стоит секунды, держать его
# по одному на воркер — верный способ съесть память и словить таймауты.
_crawler: Any = None
_crawler_loop: asyncio.AbstractEventLoop | None = None
_start_lock = asyncio.Lock()
_slots: asyncio.Semaphore | None = None
_unavailable: str | None = None


async def _get_crawler() -> Any:
    global _crawler, _crawler_loop, _slots, _unavailable

    loop = asyncio.get_running_loop()
    if _crawler is not None and _crawler_loop is loop:
        return _crawler

    async with _start_lock:
        if _crawler is not None and _crawler_loop is loop:
            return _crawler
        if _crawler is not None:  # другой event loop (тесты, перезапуск) — начинаем заново
            await close()
        if _unavailable:
            raise RendererUnavailable(_unavailable)

        s = settings()
        try:
            from crawl4ai import AsyncWebCrawler, BrowserConfig
        except ImportError as exc:
            _unavailable = "crawl4ai не установлен: pip install -e '.[render]' && crawl4ai-setup"
            raise RendererUnavailable(_unavailable) from exc

        browser_cfg = _build(
            BrowserConfig,
            headless=True,
            user_agent=s.user_agent,
            verbose=False,
            text_mode=False,
            light_mode=True,
        )
        crawler = AsyncWebCrawler(config=browser_cfg)
        try:
            await crawler.start()
        except Exception as exc:
            # Самая частая причина — не выполнен crawl4ai-setup. Запоминаем отказ,
            # чтобы не пытаться поднять браузер на каждой странице прогона.
            _unavailable = f"браузер не стартовал ({type(exc).__name__}: {exc})"
            raise RendererUnavailable(_unavailable) from exc

        _crawler = crawler
        _crawler_loop = loop
        _slots = asyncio.Semaphore(max(1, s.render_concurrency))
        log.info("T1: браузер поднят, параллельных вкладок %s", s.render_concurrency)
        return _crawler


async def close() -> None:
    """Гасит браузер. Вызывается в конце прогона; повторный вызов безопасен."""
    global _crawler, _crawler_loop, _slots

    crawler, _crawler = _crawler, None
    _crawler_loop = None
    _slots = None
    if crawler is None:
        return
    try:
        await crawler.close()
    except Exception:
        log.debug("T1: браузер закрылся с ошибкой", exc_info=True)


def reset_availability() -> None:
    """Забыть прошлый отказ — например, после установки браузеров."""
    global _unavailable
    _unavailable = None


def _build(cls: type, /, **kwargs: Any) -> Any:
    """Конструктор, устойчивый к дрейфу API crawl4ai между минорными версиями.

    Неизвестный параметр молча отбрасывается: потерять необязательную опцию
    дешевле, чем уронить многочасовой прогон на TypeError.
    """
    try:
        params = inspect.signature(cls).parameters
    except (TypeError, ValueError):
        return cls(**kwargs)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return cls(**kwargs)
    dropped = [k for k in kwargs if k not in params]
    if dropped:
        log.debug("T1: %s не знает параметров %s — пропускаю", cls.__name__, dropped)
    return cls(**{k: v for k, v in kwargs.items() if k in params})


# --------------------------------------------------------------------- рендер


async def render(url: str, *, timeout_s: float | None = None) -> RawCapture:
    """Один URL через браузер. Бросает RendererUnavailable / RenderError."""
    s = settings()
    crawler = await _get_crawler()

    from crawl4ai import CacheMode, CrawlerRunConfig

    run_cfg = _build(
        CrawlerRunConfig,
        cache_mode=CacheMode.BYPASS,  # кэш держим свой, в Bronze
        page_timeout=int((timeout_s or s.render_timeout_s) * 1000),
        wait_until=s.render_wait_until,
        delay_before_return_html=max(0.0, s.render_wait_ms / 1000),
        scan_full_page=s.render_scroll,
        remove_overlay_elements=True,
        verbose=False,
    )

    started = datetime.now(UTC)
    slots = _slots or asyncio.Semaphore(max(1, s.render_concurrency))
    async with slots:
        try:
            result = await crawler.arun(url=url, config=run_cfg)
        except Exception as exc:
            raise RenderError(f"{type(exc).__name__}: {exc}") from exc

    if not getattr(result, "success", True):
        raise RenderError(str(getattr(result, "error_message", "") or "render failed"))

    html = getattr(result, "html", None) or getattr(result, "cleaned_html", None) or ""
    if not html:
        raise RenderError("браузер вернул пустой HTML")

    headers = {
        str(k).lower(): str(v)
        for k, v in (getattr(result, "response_headers", None) or {}).items()
    }
    headers.setdefault("content-type", "text/html; charset=utf-8")

    return RawCapture(
        url=url,
        final_url=str(getattr(result, "redirected_url", None) or getattr(result, "url", url)),
        status=int(getattr(result, "status_code", None) or 200),
        headers=headers,
        body=html.encode()[: s.max_response_bytes],
        content_type=headers["content-type"],
        elapsed_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
        fetched_at=started,
        tier="t1",
    )


def render_requested(ref: SourceRef) -> bool:
    """Задача велит рендерить эту ссылку всегда, не спрашивая эвристику."""
    if not settings().render_enabled:
        return False
    return (ref.hint or {}).get("render") in (True, "always")


async def render_first(ref: SourceRef) -> RawCapture | None:
    """Сразу T1, минуя T0. None — если не вышло, тогда зовущий идёт обычным путём.

    Смысл в вежливости: при render=always заход через T0 означал бы два запроса
    к домену на каждую страницу, причём первый — заведомо бесполезный.
    """
    try:
        return await render(ref.url)
    except RendererUnavailable as exc:
        log.warning("T1 недоступен, остаюсь на T0: %s", exc)
    except RenderError as exc:
        log.debug("T1 не справился с %s: %s", ref.url, exc)
    return None


async def escalate(ref: SourceRef, raw: RawCapture) -> RawCapture:
    """T0 -> T1, если страница выглядит недорендеренной.

    Никогда не бросает: если браузера нет или он не справился, отдаём исходный
    захват T0. Пустой каркас лучше, чем потерянная страница.
    """
    if not settings().render_enabled:
        return raw
    reason = needs_render(raw, ref)
    if reason is None:
        return raw

    try:
        rendered = await render(ref.url)
    except RendererUnavailable as exc:
        log.warning("T1 недоступен, остаюсь на T0: %s", exc)
        return raw
    except RenderError as exc:
        log.debug("T1 не справился с %s: %s", ref.url, exc)
        return raw

    # Рендер имеет смысл только если он реально добавил содержимого.
    before = visible_text_len(raw.body.decode(errors="replace"))
    after = visible_text_len(rendered.body.decode(errors="replace"))
    if after <= before:
        log.debug("T1 не добавил текста на %s (%s -> %s), беру T0", ref.url, before, after)
        return raw

    log.debug("T1 %s: %s, текст %s -> %s", ref.url, reason, before, after)
    return rendered
