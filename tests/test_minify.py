from harvester.extractors.minify import minify_for_llm, split_for_llm

_LONG_TEXT = "Очень длинный абзац с содержимым страницы. " * 6
_LONG_QUERY = "q=" + "z" * 120

PAGE = f"""
<!doctype html><html><head><title>t</title><style>.a{{color:red}}</style></head>
<body>
  <script>var x = 1;</script>
  <div class="card css-1a2b3c product-card sc-fzXfMB extra another yetmore"
       id="p1" data-testid="x" data-reactid="7" onclick="go()">
    <h1 class="title">Короткий</h1>
    <p class="body">{_LONG_TEXT}</p>
    <a href="https://example.com/very/long/path?{_LONG_QUERY}">link</a>
    <img src="/img/a.png" alt="pic">
  </div>
</body></html>
"""


def test_drops_noise_tags_and_attributes():
    out, _ = minify_for_llm(PAGE)

    assert "<script" not in out and "<style" not in out
    assert "data-testid" not in out and "onclick" not in out
    assert 'id="p1"' in out          # id — основа селектора, остаётся
    assert "product-card" in out     # осмысленный класс остаётся


def test_drops_hashed_class_names():
    """Классы вида css-1a2b3c меняются при каждой сборке — селектор на них не живёт."""
    out, _ = minify_for_llm(PAGE)

    assert "css-1a2b3c" not in out
    assert "sc-fzXfMB" not in out


def test_limits_class_token_count():
    out, _ = minify_for_llm(PAGE, max_class_tokens=2)
    classes = out.split('class="')[1].split('"')[0]

    assert len(classes.split()) <= 2


def test_long_text_becomes_placeholder_and_is_recoverable():
    out, subs = minify_for_llm(PAGE)

    assert "{{t0}}" in out
    assert len(subs) == 1
    assert subs["{{t0}}"].startswith("Очень длинный абзац")
    # длинный текст ушёл из разметки, но восстановим по карте
    assert "Очень длинный абзац с содержимым" not in out


def test_short_text_survives():
    """Короткие подписи — это признаки для выбора селектора, их резать нельзя."""
    out, _ = minify_for_llm(PAGE)
    assert "Короткий" in out


def test_truncates_long_urls():
    out, _ = minify_for_llm(PAGE)
    href = out.split('href="')[1].split('"')[0]
    assert len(href) <= 80


def test_minification_actually_saves_a_lot():
    out, _ = minify_for_llm(PAGE)
    assert len(out) < len(PAGE) / 2


def test_split_keeps_paths_and_respects_budget():
    big = "<body>" + "".join(f"<div class='c{i}'>{'x' * 900}</div>" for i in range(20)) + "</body>"
    parts = split_for_llm(big, max_chars=3000, min_chars=100)

    assert len(parts) > 1
    assert all(len(html) <= 3000 for _, html in parts)
    assert all(path.startswith("body") for path, _ in parts)


def test_split_returns_whole_page_when_it_fits():
    parts = split_for_llm("<body><div>маленькая страница</div></body>", max_chars=50_000)

    assert len(parts) == 1
    assert parts[0][0] == "body"


def test_empty_input_is_safe():
    out, subs = minify_for_llm("")
    assert subs == {}
    assert split_for_llm("") in ([], [("body", "<body></body>")])
