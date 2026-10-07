"""Media pipeline: скачивание бинарей, дедуп по контенту, превью, размеры.

Фаза 0: download -> sha256 -> размеры -> webp-превью -> media_assets.
Фаза 1 доклеит сюда pHash, OCR и CLIP-эмбеддинг — это те шаги, которые превращают
картинку в текст и вектор и делают её полноправным гражданином поиска.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import mimetypes
import uuid
from typing import Any

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from .. import db, storage
from ..config import settings
from ..models import MediaSpec

Image.MAX_IMAGE_PIXELS = 120_000_000  # защита от decompression bomb

THUMB_MAX = 320


class MediaSkipped(Exception):
    pass


async def process_media(
    *,
    url: str,
    document_id: uuid.UUID,
    role: str,
    position: int,
    spec: MediaSpec,
    client: httpx.AsyncClient,
) -> tuple[uuid.UUID, bool]:
    """Скачивает и регистрирует медиа. Бросает MediaSkipped, если файл не подошёл."""
    data, mime = await _download(url, client)

    if not mime or not mime.startswith("image/"):
        raise MediaSkipped(f"не изображение: {mime}")
    # Векторные и анимационные форматы Pillow не разбирает — это не ошибка обхода,
    # а сознательный пропуск (иначе они засоряют счётчик errors трейсбеками).
    if any(token in mime for token in ("svg", "x-icon", "vnd.microsoft.icon")):
        raise MediaSkipped(f"нерастровый формат: {mime}")

    try:
        meta = await asyncio.to_thread(_probe_and_thumb, data)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise MediaSkipped(f"не декодируется ({mime}): {exc}") from exc
    if meta["width"] < spec.min_width or meta["height"] < spec.min_height:
        raise MediaSkipped(f"мелкое: {meta['width']}x{meta['height']}")

    sha = hashlib.sha256(data).digest()
    sha_hex = sha.hex()
    ext = mimetypes.guess_extension(mime.split(";")[0]) or ".bin"

    blob_ref = storage.media_key(sha_hex, ext)
    thumb_ref = storage.thumb_key(sha_hex)

    media_id, is_new = await db.upsert_media(
        sha256=sha,
        media_type="image",
        mime=mime.split(";")[0],
        size=len(data),
        width=meta["width"],
        height=meta["height"],
        blob_ref=blob_ref,
        thumb_ref=thumb_ref,
        source_url=url,
        exif=meta["exif"],
    )
    # заливаем в Bronze только если контент раньше не встречался
    if is_new:
        await storage.put(blob_ref, data, mime)
        if meta["thumb"]:
            await storage.put(thumb_ref, meta["thumb"], "image/webp")

    await db.link_media(document_id, media_id, role, position)
    return media_id, is_new


async def _download(url: str, client: httpx.AsyncClient) -> tuple[bytes, str | None]:
    limit = settings().max_media_bytes
    async with client.stream("GET", url) as resp:
        if resp.status_code >= 400:
            # CDN не отдал файл — это отказ источника, а не сбой краулера
            raise MediaSkipped(f"HTTP {resp.status_code}")
        declared = resp.headers.get("content-length")
        if declared and int(declared) > limit:
            raise MediaSkipped(f"слишком большой: {declared} байт")
        buf = bytearray()
        async for chunk in resp.aiter_bytes():
            buf.extend(chunk)
            if len(buf) > limit:
                raise MediaSkipped("превышен лимит размера")
        return bytes(buf), resp.headers.get("content-type")


def _probe_and_thumb(data: bytes) -> dict[str, Any]:
    """Синхронная часть — гоняется в thread pool, чтобы не блокировать loop."""
    with Image.open(io.BytesIO(data)) as img:
        img.load()
        width, height = img.size
        exif: dict[str, Any] = {}
        try:
            raw_exif = img.getexif()
            if raw_exif:
                exif = {str(k): str(v)[:200] for k, v in raw_exif.items()}
        except Exception:
            pass

        thumb_img = ImageOps.exif_transpose(img).convert("RGB")
        thumb_img.thumbnail((THUMB_MAX, THUMB_MAX))
        out = io.BytesIO()
        thumb_img.save(out, format="WEBP", quality=80)

    return {"width": width, "height": height, "exif": exif, "thumb": out.getvalue()}
