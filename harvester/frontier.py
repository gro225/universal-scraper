"""Frontier — быстрый уровень оркестрации (тысячи URL в минуту).

Отвечает за: приоритетную очередь, дедуп URL, robots.txt, вежливость по доменам
и circuit breaker (домен, который отдаёт 429/5xx, уходит в холодильник, а не роняет прогон).
"""

from __future__ import annotations

import json
import time
import uuid
from urllib.robotparser import RobotFileParser

import httpx
import redis.asyncio as aioredis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from .config import settings
from .hashing import canonical_url, domain_of, url_key
from .models import SourceRef

# Атомарный token-bucket на домен: возвращает 0, если можно идти сейчас,
# иначе — сколько миллисекунд подождать.
_RATE_LUA = """
local key   = KEYS[1]
local now   = tonumber(ARGV[1])
local delay = tonumber(ARGV[2])
local nxt   = tonumber(redis.call('GET', key) or '0')
if now >= nxt then
  redis.call('SET', key, now + delay, 'PX', delay * 4 + 1000)
  return 0
end
return nxt - now
"""

_redis: aioredis.Redis | None = None


async def redis() -> aioredis.Redis:
    """Клиент с ретраями.

    Фронтир — единственная точка, через которую идёт весь обход: транзиентный таймаут
    Redis не должен убивать многочасовой прогон, поэтому ретраи включены на уровне
    клиента, а не размазаны по вызывающим.
    """
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            settings().redis_url,
            decode_responses=True,
            socket_timeout=10,
            socket_connect_timeout=5,
            socket_keepalive=True,
            health_check_interval=30,
            retry=Retry(ExponentialBackoff(base=0.1, cap=2), retries=5),
            retry_on_error=[RedisConnectionError, RedisTimeoutError],
        )
    return _redis


async def close() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


def stop_key(run_id: uuid.UUID | str) -> str:
    return f"stop:{run_id}"


def alive_key(run_id: uuid.UUID | str) -> str:
    return f"alive:{run_id}"


HEARTBEAT_TTL = 45


async def heartbeat(run_id: uuid.UUID | str) -> None:
    """Отметка «прогон жив».

    Без неё убитый процесс навсегда оставляет запись в состоянии running: БД об этом
    узнать неоткуда. Ключ с TTL сам протухает, если прогон исчез.
    """
    r = await redis()
    await r.setex(alive_key(run_id), HEARTBEAT_TTL, "1")


async def is_alive(run_id: uuid.UUID | str) -> bool:
    r = await redis()
    return bool(await r.exists(alive_key(run_id)))


async def request_stop(run_id: uuid.UUID | str) -> None:
    """Просит прогон остановиться.

    Флаг в Redis, а не отмена asyncio-задачи: прогон мог быть запущен из CLI в другом
    процессе, и API всё равно должен уметь его остановить.
    """
    r = await redis()
    await r.setex(stop_key(run_id), 86400, "1")


class Frontier:
    def __init__(self, run_id: uuid.UUID, politeness_delay_ms: int, respect_robots: bool):
        self.run_id = str(run_id)
        self.delay_ms = politeness_delay_ms
        self.respect_robots = respect_robots
        self._rate_script = None

    # ---------------------------------------------------------------- ключи
    @property
    def _queue(self) -> str:
        return f"frontier:{self.run_id}"

    @property
    def _seen(self) -> str:
        return f"seen:{self.run_id}"

    @property
    def _media_queue(self) -> str:
        return f"media:{self.run_id}"

    # -------------------------------------------------------------- очередь
    async def push(self, ref: SourceRef) -> bool:
        """Кладёт URL в очередь. False — если такой URL уже видели в этом прогоне."""
        r = await redis()
        key = url_key(ref.url)
        if not await r.sadd(self._seen, key):
            return False
        ref.url = canonical_url(ref.url)
        # zset по возрастанию score, поэтому высокий приоритет = низкий score
        await r.zadd(self._queue, {ref.model_dump_json(): -ref.priority + ref.depth})
        return True

    async def push_many(self, refs: list[SourceRef]) -> int:
        return sum([await self.push(ref) for ref in refs])

    async def requeue(self, ref: SourceRef, penalty: float = 0.05) -> None:
        """Вернуть URL в очередь, минуя дедуп.

        Нужно, когда домен занят: воркер не должен простаивать на нём, пока в очереди
        ждут URL других доменов. Небольшой штраф к score не даёт одному домену
        крутиться в голове очереди.
        """
        r = await redis()
        await r.zadd(self._queue, {ref.model_dump_json(): -ref.priority + ref.depth + penalty})

    async def pop(self) -> SourceRef | None:
        refs = await self.pop_batch(1)
        return refs[0] if refs else None

    async def pop_batch(self, n: int) -> list[SourceRef]:
        """Достаёт до n кандидатов сразу.

        Воркеру нужен выбор: из пачки он берёт первый URL, чей домен свободен,
        а остальные возвращает. Иначе очередь, забитая парой доменов, съедает
        всех воркеров на ожидании.
        """
        r = await redis()
        popped = await r.zpopmin(self._queue, n)
        return [SourceRef.model_validate_json(member) for member, _ in popped]

    async def size(self) -> int:
        r = await redis()
        return await r.zcard(self._queue)

    async def push_media(self, payload: dict) -> None:
        r = await redis()
        await r.rpush(self._media_queue, json.dumps(payload))

    async def pop_media(self) -> dict | None:
        r = await redis()
        raw = await r.lpop(self._media_queue)
        return json.loads(raw) if raw else None

    async def media_size(self) -> int:
        r = await redis()
        return await r.llen(self._media_queue)

    async def cleanup(self) -> None:
        r = await redis()
        await r.delete(
            self._queue, self._seen, self._media_queue,
            stop_key(self.run_id), alive_key(self.run_id),
        )

    async def stop_requested(self) -> bool:
        r = await redis()
        return bool(await r.exists(stop_key(self.run_id)))

    # ------------------------------------------------------------ вежливость
    async def try_slot(self, url: str) -> int:
        """Неблокирующая попытка занять слот домена.

        Возвращает 0, если идти можно сейчас, иначе — сколько мс ждать. Вызывающий
        решает: подождать или взять из очереди URL другого домена.
        """
        r = await redis()
        if self._rate_script is None:
            self._rate_script = r.register_script(_RATE_LUA)
        wait_ms = await self._rate_script(
            keys=[f"rl:{domain_of(url)}"], args=[int(time.time() * 1000), self.delay_ms]
        )
        return int(wait_ms)

    # -------------------------------------------------- circuit breaker домена
    async def domain_is_open(self, url: str) -> bool:
        r = await redis()
        return not await r.exists(f"cb:{domain_of(url)}")

    async def record_failure(self, url: str) -> None:
        """Серия отказов -> домен уходит в холодильник с экспоненциальным бэкоффом."""
        r = await redis()
        host = domain_of(url)
        fails = await r.incr(f"cbf:{host}")
        await r.expire(f"cbf:{host}", 600)
        if fails >= 5:
            cooldown = min(60 * 2 ** (fails - 5), 3600)
            await r.setex(f"cb:{host}", cooldown, "1")

    async def record_success(self, url: str) -> None:
        r = await redis()
        await r.delete(f"cbf:{domain_of(url)}")

    # ---------------------------------------------------------------- robots
    async def allowed_by_robots(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        r = await redis()
        host = domain_of(url)
        cache_key = f"robots:{host}"
        body = await r.get(cache_key)
        if body is None:
            body = await self._fetch_robots(url)
            await r.setex(cache_key, 86400, body)
        parser = RobotFileParser()
        parser.parse(body.splitlines())
        return parser.can_fetch(settings().user_agent, url)

    async def _fetch_robots(self, url: str) -> str:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        try:
            async with httpx.AsyncClient(
                timeout=10, follow_redirects=True,
                headers={"User-Agent": settings().user_agent},
            ) as c:
                resp = await c.get(robots_url)
                # 4xx трактуем как «ограничений нет» — стандартное поведение
                return resp.text if resp.status_code == 200 else ""
        except Exception:
            return ""
