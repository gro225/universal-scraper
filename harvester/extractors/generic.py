"""Generic-извлечение. Каскад от дешёвого к дорогому (ARCHITECTURE.md §8).

Фаза 0 реализует ступени 1-2:
  1. структурные разметки: JSON-LD / OpenGraph  — бесплатно и точно
  2. trafilatura: основной текст, автор, дата
Ступени 3-4 (site adapters, LLM-экстрактор с индукцией CSS-рецептов) — Фаза 2;
точка расширения — extractors/registry.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import trafilatura
from dateutil import parser as dateparser
from selectolax.parser import HTMLParser

from ..hashing import canonical_url, content_sha256, domain_of, simhash
from ..models import DocumentDraft, ExtractResult, MediaRef, MediaSpec, RawCapture, Task

SKIP_EXT = re.compile(
    r"\.(css|js|json|xml|rss|ico|svg|woff2?|ttf|eot|zip|gz|exe|dmg|apk|mp3|wav)(\?|$)", re.I
)
IMAGE_EXT = re.compile(r"\.(jpe?g|png|webp|gif|avif|bmp|tiff?)(\?|$)", re.I)


def extract_html(raw: RawCapture, task: Task) -> ExtractResult:
    if raw.status >= 400 or not _is_html(raw):
        return ExtractResult()

    html = raw.body.decode(_charset(raw), errors="replace")
    tree = HTMLParser(html)
    base = raw.final_url

    structured = _structured_data(tree)
    og = _open_graph(tree)
    core = _trafilatura(html, base)

    title = core.get("title") or og.get("title") or _tag_text(tree, "title")
    text = (core.get("text") or "").strip()
    if not text:
        text = _fallback_text(tree)

    want = set(task.extract.want)
    media = _media_refs(tree, base, og, task.extract.media) if "images" in want else []
    links = _links(tree, base) if "links" in want else []

    if len(text) < task.extract.min_text_chars and not media:
        # ни текста, ни картинок — страница бесполезна, но ссылки с неё берём
        return ExtractResult(links=links)

    doc = DocumentDraft(
        kind=_kind(structured, og),
        source_url=raw.url,
        canonical_url=_canonical(tree, base),
        domain=domain_of(base),
        connector="generic_web",
        lang=core.get("lang") or _lang(tree),
        title=_clean(title),
        text=text,
        author=core.get("author") or structured.get("author"),
        published_at=_published(core, structured, og),
        payload={
            "og": og,
            "structured": structured if "structured_ld" in want else {},
            "http_status": raw.status,
            "final_url": raw.final_url,
            "tier": raw.tier,
            "description": og.get("description") or core.get("description"),
        },
        content_hash=content_sha256((title or "").encode() + b"\x00" + text.encode()),
        simhash=simhash(text),
    )
    return ExtractResult(document=doc, media=media[: task.extract.media.max_per_document],
                         links=links)


# ------------------------------------------------------------------ ступень 1


def _structured_data(tree: HTMLParser) -> dict[str, Any]:
    """JSON-LD: самый дешёвый и самый точный источник метаданных."""
    out: dict[str, Any] = {}
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text())
        except Exception:
            continue
        for obj in _flatten_ld(data):
            if not isinstance(obj, dict):
                continue
            out.setdefault("@type", obj.get("@type"))
            for key in ("headline", "name", "datePublished", "dateModified", "description"):
                if obj.get(key) and key not in out:
                    out[key] = obj[key]
            author = obj.get("author")
            if author and "author" not in out:
                if isinstance(author, dict):
                    out["author"] = author.get("name")
                elif isinstance(author, list) and author:
                    first = author[0]
                    out["author"] = first.get("name") if isinstance(first, dict) else str(first)
                else:
                    out["author"] = str(author)
    return out


def _flatten_ld(data: Any) -> list[Any]:
    if isinstance(data, list):
        return [x for item in data for x in _flatten_ld(item)]
    if isinstance(data, dict):
        items = [data]
        if "@graph" in data:
            items.extend(_flatten_ld(data["@graph"]))
        return items
    return []


def _open_graph(tree: HTMLParser) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in tree.css("meta"):
        prop = node.attributes.get("property") or node.attributes.get("name") or ""
        content = node.attributes.get("content")
        if not content:
            continue
        if prop.startswith(("og:", "twitter:", "article:")):
            out[prop.split(":", 1)[1] if prop.startswith("og:") else prop] = content
        elif prop in ("description", "author"):
            out[prop] = content
    return out


# ------------------------------------------------------------------ ступень 2


def _trafilatura(html: str, url: str) -> dict[str, Any]:
    try:
        doc = trafilatura.bare_extraction(
            html, url=url, include_comments=False, include_tables=True, with_metadata=True
        )
    except Exception:
        return {}
    if not doc:
        return {}
    if not isinstance(doc, dict):  # trafilatura >= 2.0 отдаёт объект
        doc = doc.as_dict()
    return {
        "title": doc.get("title"),
        "text": doc.get("text") or doc.get("raw_text") or "",
        "author": doc.get("author"),
        "date": doc.get("date"),
        "lang": doc.get("language"),
        "description": doc.get("description"),
    }


def _fallback_text(tree: HTMLParser) -> str:
    for node in tree.css("script, style, nav, header, footer, noscript, svg"):
        node.decompose()
    body = tree.body
    text = body.text(separator=" ", strip=True) if body else ""
    return re.sub(r"\s{2,}", " ", text)[:200_000]


# ------------------------------------------------------------- медиа и ссылки


def _media_refs(
    tree: HTMLParser, base: str, og: dict[str, str], spec: MediaSpec
) -> list[MediaRef]:
    refs: list[MediaRef] = []
    seen: set[str] = set()

    def add(url: str | None, role: str, alt: str | None = None) -> None:
        if not url:
            return
        absolute = urljoin(base, url.strip())
        if not absolute.startswith("http") or absolute in seen:
            return
        if absolute.startswith("data:"):
            return
        seen.add(absolute)
        refs.append(MediaRef(url=absolute, media_type="image", role=role,
                             position=len(refs), alt=alt))

    add(og.get("image"), "hero")
    for node in tree.css("img"):
        attrs = node.attributes
        if _too_small(attrs, spec):
            continue  # иконки и распорки — незачем их вообще скачивать
        src = attrs.get("src") or attrs.get("data-src") or attrs.get("data-lazy-src")
        if not src and attrs.get("srcset"):
            src = attrs["srcset"].split(",")[-1].strip().split(" ")[0]
        add(src, "inline", attrs.get("alt"))
    for node in tree.css("picture source"):
        srcset = node.attributes.get("srcset")
        if srcset:
            add(srcset.split(",")[-1].strip().split(" ")[0], "inline")
    return refs


def _too_small(attrs: dict[str, str | None], spec: MediaSpec) -> bool:
    """Отсев по атрибутам width/height — до скачивания, а не после.

    Страницы полны иконок 40–60 px, которые всё равно не пройдут порог. Качать их,
    чтобы потом выбросить, — это лишние запросы и лишние отказы от CDN.
    """
    for attr, limit in (("width", spec.min_width), ("height", spec.min_height)):
        raw = (attrs.get(attr) or "").strip().removesuffix("px")
        if raw.isdigit() and int(raw) < limit:
            return True
    return False


def _links(tree: HTMLParser, base: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(base, href)
        if not absolute.startswith("http") or SKIP_EXT.search(absolute):
            continue
        if IMAGE_EXT.search(absolute):
            continue
        key = canonical_url(absolute)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= 300:
            break
    return out


# ------------------------------------------------------------------ мелочи


def _is_html(raw: RawCapture) -> bool:
    ct = (raw.content_type or "").lower()
    return "html" in ct or (not ct and raw.body[:512].lstrip().startswith(b"<"))


def _charset(raw: RawCapture) -> str:
    ct = (raw.content_type or "").lower()
    match = re.search(r"charset=([\w\-]+)", ct)
    return match.group(1) if match else "utf-8"


def _tag_text(tree: HTMLParser, tag: str) -> str | None:
    node = tree.css_first(tag)
    return node.text(strip=True) if node else None


def _lang(tree: HTMLParser) -> str | None:
    html = tree.css_first("html")
    lang = html.attributes.get("lang") if html else None
    return lang.split("-")[0].lower() if lang else None


def _canonical(tree: HTMLParser, base: str) -> str | None:
    node = tree.css_first('link[rel="canonical"]')
    href = node.attributes.get("href") if node else None
    return canonical_url(urljoin(base, href)) if href else canonical_url(base)


def _kind(structured: dict[str, Any], og: dict[str, str]) -> str:
    ld_type = str(structured.get("@type") or og.get("type") or "").lower()
    if any(t in ld_type for t in ("newsarticle", "blogposting", "article", "report")):
        return "article"
    if "product" in ld_type:
        return "product"
    if any(t in ld_type for t in ("person", "profile", "organization")):
        return "profile"
    if any(t in ld_type for t in ("video", "musicrecording", "musicalbum")):
        return "media_page"
    return "page"


def _published(core: dict, structured: dict, og: dict) -> datetime | None:
    for candidate in (
        core.get("date"),
        structured.get("datePublished"),
        og.get("published_time"),
        og.get("article:published_time"),
    ):
        if not candidate:
            continue
        try:
            dt = dateparser.parse(str(candidate))
            if dt and 1990 < dt.year < 2100:
                return dt
        except (ValueError, OverflowError, TypeError):
            continue
    return None


def _clean(value: str | None) -> str | None:
    return re.sub(r"\s{2,}", " ", value).strip()[:500] if value else None
