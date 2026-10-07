"""T0-фетчер: обычный HTTP. Дефолт для всего, что не имеет своего коннектора.

Ничего не парсит — только достаёт байты и отдаёт их дальше. Парсинг живёт в extractors/.
Когда T0 отдал пустой каркас SPA, ответ эскалируется в T1 (браузер, connectors/render.py);
пока render_enabled=False, эскалация — no-op и поведение ровно прежнее.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from ..config import settings
from ..models import ExtractResult, RawCapture, SourceRef, Task
from .base import register
from .render import escalate, render_first, render_requested


class FetchError(Exception):
    pass


class GenericWebConnector:
    name = "generic_web"

    def matches(self, ref: SourceRef) -> bool:
        return ref.url.startswith(("http://", "https://"))

    async def discover(self, task: Task) -> AsyncIterator[SourceRef]:
        for url in task.discovery.seeds:
            yield SourceRef(url=url, depth=0, priority=1.0, discovered_by="task.seeds")

    async def fetch(self, ref: SourceRef) -> RawCapture:
        s = settings()
        await _guard_ssrf(ref.url)

        # Задача просит рендерить всегда — идём в браузер сразу, без бесполезного
        # запроса T0 к тому же домену.
        forced = render_requested(ref)
        if forced and (rendered := await render_first(ref)) is not None:
            return rendered

        started = datetime.now(UTC)

        async with httpx.AsyncClient(
            timeout=s.fetch_timeout_s,
            follow_redirects=True,
            max_redirects=5,
            headers={
                "User-Agent": s.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru,en;q=0.8",
            },
        ) as client, client.stream("GET", ref.url) as resp:
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body.extend(chunk)
                if len(body) > s.max_response_bytes:
                    break  # обрезаем гигантские ответы, а не падаем
            elapsed = (datetime.now(UTC) - started).total_seconds()
            capture = RawCapture(
                url=ref.url,
                final_url=str(resp.url),
                status=resp.status_code,
                headers={k.lower(): v for k, v in resp.headers.items()},
                body=bytes(body),
                content_type=resp.headers.get("content-type"),
                elapsed_ms=int(elapsed * 1000),
                fetched_at=started,
                tier="t0",
            )

        # Соединение уже отпущено: браузер не должен занимать слот пула httpx.
        # Если T1 выше уже пробовали и он не смог — второй раз не лезем.
        return capture if forced else await escalate(ref, capture)

    def extract(self, raw: RawCapture, task: Task) -> ExtractResult:
        from ..extractors.generic import extract_html

        return extract_html(raw, task)


_PRIVATE_HOSTS = {"localhost", "metadata.google.internal"}


async def _guard_ssrf(url: str) -> None:
    """Не даём краулеру ходить во внутреннюю сеть (SSRF).

    Резолв обязан быть неблокирующим и с таймаутом: синхронный getaddrinfo на зависшем
    DNS встаёт колом и вешает весь event loop, то есть все воркеры разом.
    """
    host = urlsplit(url).hostname or ""
    if host in _PRIVATE_HOSTS:
        raise FetchError(f"запрещённый хост: {host}")
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(host, None), timeout=5)
    except (socket.gaierror, TimeoutError) as exc:
        raise FetchError(f"DNS не резолвится: {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise FetchError(f"приватный IP запрещён: {host} -> {ip}")


register(GenericWebConnector())
