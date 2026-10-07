"""Канонизация URL и хеши для трёх уровней дедупликации.

- URL:      canonical_url() + url_key()  -> дедуп в фронтире
- контент:  content_sha256()             -> точный дубль
- near-dup: simhash()                    -> перепечатки/незначительные правки
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = re.compile(
    r"^(utm_.*|fbclid|gclid|yclid|ysclid|_ga|_gl|mc_[ce]id|igshid|si|ref|ref_src|"
    r"spm|from|source|campaign_id)$",
    re.I,
)

DEFAULT_PORTS = {"http": "80", "https": "443"}


def canonical_url(url: str, *, keep_query: bool = True) -> str:
    """Приводит URL к канонической форме, чтобы одинаковые страницы не обходились дважды."""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "http").lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    netloc = host
    if parts.port and str(parts.port) != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"

    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    if not path:
        path = "/"

    query = ""
    if keep_query and parts.query:
        pairs = [
            (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not TRACKING_PARAMS.match(k)
        ]
        query = urlencode(sorted(pairs))

    return urlunsplit((scheme, netloc, path, query, ""))


def url_key(url: str) -> str:
    """Короткий ключ URL для set'а посещённого во фронтире."""
    return hashlib.sha1(canonical_url(url).encode()).hexdigest()


def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def registrable_domain(url: str) -> str:
    """Грубое приближение eTLD+1 без внешнего списка суффиксов — достаточно для Фазы 0."""
    host = domain_of(url)
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    two_level = {"co", "com", "org", "net", "gov", "edu", "ac"}
    if parts[-2] in two_level and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def content_sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def simhash(text: str, *, shingle: int = 3) -> int:
    """64-битный simhash по шинглам слов. Расстояние Хэмминга <= 3 ≈ near-duplicate."""
    tokens = _TOKEN_RE.findall(text.lower())
    if len(tokens) < shingle:
        tokens = tokens or ["\x00"]
        shingles = [" ".join(tokens)]
    else:
        shingles = [
            " ".join(tokens[i : i + shingle]) for i in range(len(tokens) - shingle + 1)
        ]

    vector = [0] * 64
    for sh in shingles:
        h = int.from_bytes(hashlib.blake2b(sh.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            vector[bit] += 1 if (h >> bit) & 1 else -1

    out = 0
    for bit in range(64):
        if vector[bit] > 0:
            out |= 1 << bit
    # приводим к знаковому int64 — так оно ложится в postgres bigint
    return out - (1 << 64) if out >= (1 << 63) else out


def hamming(a: int, b: int) -> int:
    return bin((a ^ b) & ((1 << 64) - 1)).count("1")
