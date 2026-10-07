"""Bronze-слой: неизменяемое сырьё в S3-совместимом хранилище (MinIO).

Content-addressing даёт бесплатный дедуп: одна и та же картинка с 50 сайтов лежит один раз.
Из Bronze в любой момент пересобирается Silver — без повторного обхода сайтов.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import AsyncExitStack

import aioboto3
from botocore.exceptions import ClientError

from .config import settings

_session = aioboto3.Session()
_stack: AsyncExitStack | None = None
_clients: dict[str, object] = {}
_lock = asyncio.Lock()


async def _client_for(endpoint: str):
    """Переиспользуемый клиент на endpoint.

    Создание клиента aioboto3 — это резолв кредов и новый пул соединений; делать его
    на каждую запись в Bronze дороже самой записи.
    """
    global _stack
    if endpoint not in _clients:
        async with _lock:
            if endpoint not in _clients:
                s = settings()
                if _stack is None:
                    _stack = AsyncExitStack()
                _clients[endpoint] = await _stack.enter_async_context(
                    _session.client(
                        "s3",
                        endpoint_url=endpoint,
                        aws_access_key_id=s.s3_access_key,
                        aws_secret_access_key=s.s3_secret_key,
                        region_name=s.s3_region,
                    )
                )
    return _clients[endpoint]


async def client():
    """Клиент для чтения/записи — ходит по внутреннему адресу."""
    return await _client_for(settings().s3_endpoint)


async def signing_client():
    """Клиент для presigned-ссылок — подписывает от имени внешнего адреса.

    Хост входит в подпись, поэтому подменить его в готовом URL нельзя — нужен
    отдельный клиент. Сетевых запросов он не делает: подпись считается локально.
    """
    s = settings()
    return await _client_for(s.s3_public_endpoint or s.s3_endpoint)


async def close() -> None:
    global _stack
    if _stack is not None:
        await _stack.aclose()
        _stack = None
        _clients.clear()


async def ensure_bucket() -> None:
    s = settings()
    c = await client()
    try:
        await c.head_bucket(Bucket=s.s3_bucket)
    except ClientError:
        await c.create_bucket(Bucket=s.s3_bucket)


def raw_key(task_id: uuid.UUID, run_id: uuid.UUID, sha_hex: str) -> str:
    return f"raw/{task_id}/{run_id}/{sha_hex[:2]}/{sha_hex}"


def media_key(sha_hex: str, ext: str) -> str:
    return f"media/{sha_hex[:2]}/{sha_hex}{ext}"


def thumb_key(sha_hex: str) -> str:
    return f"media/{sha_hex[:2]}/{sha_hex}.thumb.webp"


async def put(key: str, data: bytes, content_type: str | None = None) -> str:
    c = await client()
    await c.put_object(
        Bucket=settings().s3_bucket,
        Key=key,
        Body=data,
        ContentType=content_type or "application/octet-stream",
    )
    return key


async def get(key: str) -> bytes:
    c = await client()
    obj = await c.get_object(Bucket=settings().s3_bucket, Key=key)
    return await obj["Body"].read()


async def presign(key: str, expires: int = 3600) -> str:
    c = await signing_client()
    return await c.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings().s3_bucket, "Key": key},
        ExpiresIn=expires,
    )
