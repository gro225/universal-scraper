"""Подготовка DOM к отправке в LLM.

Индукция рецепта (ARCHITECTURE.md §8.4) требует от модели одного: увидеть **каркас**
страницы и вернуть селекторы. Содержимое при этом — балласт, за который платят токенами.
Поэтому здесь мы:

  1. выкидываем теги, не несущие структуры (script/style/svg/...);
  2. оставляем только атрибуты, по которым строят селекторы (class/id/name/href/src);
  3. заменяем длинные текстовые узлы плейсхолдерами `{{t0}}`, `{{t1}}`, ...

Возвращается пара (минифицированный html, карта подстановок). Карта нужна, чтобы
проверить индуцированный селектор: LLM говорит «заголовок в `h1`», мы достаём оттуда
плейсхолдер и по карте видим исходный текст.

Идея подсмотрена в scraperai (GPL-3.0); здесь она переписана с нуля на selectolax,
кода оттуда не взято.
"""

from __future__ import annotations

import re

# lexbor, а не parser: бэкенд Modest удалён в selectolax 1.0
from selectolax.lexbor import LexborHTMLParser as HTMLParser

# Теги без структурной ценности для селекторов
DROP_TAGS = (
    "script", "style", "noscript", "svg", "iframe", "link", "template",
    "canvas", "audio", "video", "source", "track", "object", "embed",
)

# Атрибуты, по которым реально строят CSS/XPath
KEEP_ATTRS = frozenset({"class", "id", "name", "href", "src", "alt", "itemprop", "role"})

# Значения class/id вида "css-1a2b3c" или "sc-fzXfMB" меняются при каждой сборке —
# селектор на них жить не будет, а токены они едят.
_HASHED = re.compile(r"^(css-[0-9a-z]{5,}|sc-[A-Za-z0-9]{6,}|[a-f0-9]{8,}|jsx-\d+)$")

PLACEHOLDER = "{{t%d}}"


def minify_for_llm(
    html: str,
    *,
    keep_attrs: frozenset[str] = KEEP_ATTRS,
    drop_tags: tuple[str, ...] = DROP_TAGS,
    max_text_chars: int = 60,
    max_class_tokens: int = 4,
    max_output_chars: int | None = 60_000,
) -> tuple[str, dict[str, str]]:
    """Сжимает HTML до структурного каркаса.

    max_text_chars — длиннее этого текстовый узел заменяется плейсхолдером.
    max_output_chars — жёсткий предел на выдачу; при превышении см. split_for_llm().
    """
    tree = HTMLParser(html)

    for tag in drop_tags:
        for node in tree.css(tag):
            node.decompose()
    for node in tree.css("head, comment"):
        node.decompose()

    for node in tree.css("*"):
        _strip_attrs(node, keep_attrs, max_class_tokens)

    substitutions: dict[str, str] = {}
    body = tree.body or tree.root
    if body is not None:
        _substitute_text(body, substitutions, max_text_chars)

    out = (body.html if body is not None else tree.html) or ""
    out = re.sub(r">\s+<", "><", out)
    out = re.sub(r"\s{2,}", " ", out).strip()

    if max_output_chars is not None and len(out) > max_output_chars:
        out = out[:max_output_chars]
    return out, substitutions


def _strip_attrs(node, keep_attrs: frozenset[str], max_class_tokens: int) -> None:
    attrs = dict(node.attributes)
    for name, value in attrs.items():
        if name not in keep_attrs:
            del node.attrs[name]
            continue
        if name == "class" and value:
            # оставляем первые осмысленные классы: остальные — шум сборщика
            tokens = [t for t in value.split() if not _HASHED.match(t)]
            if not tokens:
                del node.attrs[name]
            else:
                node.attrs[name] = " ".join(tokens[:max_class_tokens])
        elif name in ("href", "src") and value and len(value) > 80:
            # длинные URL не помогают выбрать селектор, но стоят токенов
            node.attrs[name] = value[:80]


def _substitute_text(node, substitutions: dict[str, str], max_text_chars: int) -> None:
    for child in list(node.iter(include_text=True)):
        if child.tag == "-text":
            text = (child.text() or "").strip()
            if len(text) > max_text_chars:
                key = PLACEHOLDER % len(substitutions)
                substitutions[key] = text
                child.replace_with(key)
        else:
            _substitute_text(child, substitutions, max_text_chars)


def split_for_llm(
    html: str, *, max_chars: int = 30_000, min_chars: int = 2_000
) -> list[tuple[str, str]]:
    """Режет страницу на части под лимит контекста, сохраняя CSS-путь каждой.

    Возвращает [(css_path, html_fragment), ...]. Нужно, когда даже минифицированная
    страница не влезает в окно модели: индуцируем рецепт по частям, а `css_path`
    позволяет собрать полный селектор.
    """
    tree = HTMLParser(html)
    root = tree.body or tree.root
    if root is None:
        return []

    parts: list[tuple[str, str]] = []
    _split_node(root, "body", parts, max_chars, min_chars)
    return parts or [("body", (root.html or "")[:max_chars])]


def _split_node(node, path: str, out: list, max_chars: int, min_chars: int) -> None:
    html = node.html or ""
    if len(html) <= max_chars:
        if len(html) >= min_chars or not out:
            out.append((path, html))
        return

    children = list(node.iter(include_text=False))
    if not children:
        out.append((path, html[:max_chars]))
        return

    counters: dict[str, int] = {}
    for child in children:
        counters[child.tag] = counters.get(child.tag, 0) + 1
        child_path = f"{path} > {child.tag}:nth-of-type({counters[child.tag]})"
        _split_node(child, child_path, out, max_chars, min_chars)
