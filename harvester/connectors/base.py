"""Контракт плагина-источника. Ядро не знает про конкретные сайты — только про этот интерфейс.

Чтобы добавить источник (Reddit, Telegram, YouTube, маркетплейс), достаточно реализовать
Connector и зарегистрировать его. Фаза 0 реализует discover() у searxng и fetch() у generic_web.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from ..models import ExtractResult, RawCapture, SourceRef, Task


@runtime_checkable
class Connector(Protocol):
    name: str

    def matches(self, ref: SourceRef) -> bool:
        """Берётся ли этот коннектор за данный ref."""
        ...

    async def discover(self, task: Task) -> AsyncIterator[SourceRef]:
        """Превращает задачу в seed-ссылки. Не все коннекторы это умеют."""
        ...

    async def fetch(self, ref: SourceRef) -> RawCapture:
        """Достаёт сырьё. Ничего не парсит."""
        ...

    def extract(self, raw: RawCapture, task: Task) -> ExtractResult:
        """Нормализует сырьё в Document + MediaRef + ссылки."""
        ...


_REGISTRY: dict[str, Connector] = {}


def register(connector: Connector) -> Connector:
    _REGISTRY[connector.name] = connector
    return connector


def get(name: str) -> Connector:
    if name not in _REGISTRY:
        raise KeyError(f"неизвестный коннектор: {name!r} (есть: {sorted(_REGISTRY)})")
    return _REGISTRY[name]


def available() -> list[str]:
    return sorted(_REGISTRY)


def resolve(ref: SourceRef) -> Connector:
    """Первый коннектор, который берётся за ref; иначе generic_web."""
    for name, conn in _REGISTRY.items():
        if name != "generic_web" and conn.matches(ref):
            return conn
    return get("generic_web")
