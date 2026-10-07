import io

import pytest
from PIL import Image, UnidentifiedImageError

from harvester.media.pipeline import THUMB_MAX, _probe_and_thumb


def _png(width: int, height: int, mode: str = "RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, (width, height), "red").save(buf, format="PNG")
    return buf.getvalue()


def test_probe_returns_dimensions_and_webp_thumb():
    meta = _probe_and_thumb(_png(800, 600))

    assert (meta["width"], meta["height"]) == (800, 600)
    with Image.open(io.BytesIO(meta["thumb"])) as thumb:
        assert thumb.format == "WEBP"
        assert max(thumb.size) <= THUMB_MAX


def test_probe_handles_palette_with_transparency():
    """Такие PNG раньше валили конвертацию в превью."""
    meta = _probe_and_thumb(_png(400, 400, mode="P"))
    assert meta["thumb"]


def test_probe_rejects_vector_and_garbage():
    # SVG и мусор — это MediaSkipped на уровне пайплайна, а не ошибка обхода
    with pytest.raises((UnidentifiedImageError, OSError, ValueError)):
        _probe_and_thumb(b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>')
    with pytest.raises((UnidentifiedImageError, OSError, ValueError)):
        _probe_and_thumb(b"not an image at all")
