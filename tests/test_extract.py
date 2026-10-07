from datetime import UTC, datetime

from harvester.extractors.generic import extract_html
from harvester.models import RawCapture, Task

HTML = """
<!doctype html><html lang="en"><head>
<title>Sol Lemon — новый альбом</title>
<link rel="canonical" href="https://example.com/news/sol-lemon">
<meta property="og:title" content="Sol Lemon — новый альбом">
<meta property="og:image" content="/img/cover.jpg">
<meta property="og:type" content="article">
<script type="application/ld+json">
{"@type":"NewsArticle","headline":"Sol Lemon","datePublished":"2024-03-01",
 "author":{"@type":"Person","name":"Ada Reporter"}}
</script>
</head><body>
<article><p>%s</p>
<img src="https://cdn.example.com/photo1.jpg" alt="live">
<img data-src="/photo2.png" alt="backstage">
</article>
<a href="/news/other">другая новость</a>
<a href="https://elsewhere.org/page">внешняя</a>
<a href="/style.css">css</a>
</body></html>
""" % ("Sol Lemon отыграл концерт и объявил тур. " * 20)


def _capture(body: str, url: str = "https://example.com/news/sol-lemon") -> RawCapture:
    return RawCapture(
        url=url,
        final_url=url,
        status=200,
        headers={"content-type": "text/html; charset=utf-8"},
        body=body.encode(),
        content_type="text/html; charset=utf-8",
        fetched_at=datetime.now(UTC),
    )


def _task() -> Task:
    return Task.model_validate({"name": "t", "query": {"subject": "Sol Lemon"}})


def test_extract_builds_document_with_metadata():
    result = extract_html(_capture(HTML), _task())
    doc = result.document

    assert doc is not None
    assert doc.kind == "article"
    assert "Sol Lemon" in (doc.title or "")
    assert doc.domain == "example.com"
    assert doc.lang == "en"
    assert doc.author == "Ada Reporter"
    assert doc.published_at and doc.published_at.year == 2024
    assert len(doc.text) > 250
    assert doc.content_hash and doc.simhash is not None


def test_extract_collects_media_and_resolves_relative_urls():
    result = extract_html(_capture(HTML), _task())
    urls = [m.url for m in result.media]

    assert "https://example.com/img/cover.jpg" in urls   # og:image, относительный
    assert "https://cdn.example.com/photo1.jpg" in urls  # обычный src
    assert "https://example.com/photo2.png" in urls      # lazy data-src
    assert result.media[0].role == "hero"


def test_extract_filters_links():
    result = extract_html(_capture(HTML), _task())

    assert "https://example.com/news/other" in result.links
    assert "https://elsewhere.org/page" in result.links
    assert not any(url.endswith(".css") for url in result.links)


def test_thin_page_yields_no_document_but_keeps_links():
    thin = '<html><body><p>коротко</p><a href="/next">n</a></body></html>'
    result = extract_html(_capture(thin), _task())

    assert result.document is None
    assert result.links == ["https://example.com/next"]


def test_non_html_is_ignored():
    raw = _capture("{}")
    raw.content_type = "application/json"
    raw.headers["content-type"] = "application/json"
    assert extract_html(raw, _task()).document is None
