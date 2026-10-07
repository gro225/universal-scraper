"""Реестр коннекторов. Импорт модуля регистрирует плагин."""

from . import generic_web, searxng  # noqa: F401
from .base import available, get, register, resolve

__all__ = ["available", "get", "register", "resolve"]
