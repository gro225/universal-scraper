"""Тир T1: эвристика эскалации и поведение без установленного crawl4ai.

Браузер здесь не поднимается — проверяется решение «рендерить или нет» и то,
что отсутствие зависимости никогда не ломает прогон.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from harvester.config import settings
from harvester.connectors import render
from harvester.models import RawCapture, SourceRef, Task

SPA = (
    '<!doctype html><html><body><div id="root"></div>'
    '<script src="/app.js"></script></body></html>'
)
NOSCRIPT = (
    "<html><body><noscript>Please enable JavaScript to view this site.</noscript>"
    "<div></div></body></html>"
)
FULL = "<html><body><article><p>%s</p></article></body></html>" % ("Живой текст страницы. " * 60)


@pytest.fixture(autouse=True)
def _render_on(monkeypatch):
    """Тир включён, но браузера нет — самый интересный для нас режим."""
    s = settings()
    monkeypatch.setattr(s, "render_enabled", True, raising=False)
    monkeypatch.setattr(s, "render_on_block", False, raising=False)
    render.reset_availability()
    yield
    render.reset_availability()


def _capture(
    body: str, *, status: int = 200, tier: str = "t0", ct: str | None = None
) -> RawCapture:
    return RawCapture(
        url="https://example.com/p",
        final_url="https://example.com/p",
        status=status,
        headers={},
        body=body.encode(),
        content_type=ct if ct is not None else "text/html; charset=utf-8",
        fetched_at=datetime.now(UTC),
        tier=tier,
    )


def _ref(**hint) -> SourceRef:
    return SourceRef(url="https://example.com/p", hint=hint)


# ------------------------------------------------------------------ эвристика


def test_spa_shell_triggers_render():
    assert render.needs_render(_capture(SPA), _ref()) == "spa_shell"


def test_noscript_notice_triggers_render():
    assert render.needs_render(_capture(NOSCRIPT), _ref()) == "noscript"


def test_full_page_is_left_on_t0():
    assert render.needs_render(_capture(FULL), _ref()) is None


def test_hint_forces_render_even_on_full_page():
    assert render.needs_render(_capture(FULL), _ref(render=True)) == "hint"


def test_hint_can_forbid_render_on_empty_shell():
    assert render.needs_render(_capture(SPA), _ref(render=False)) is None


def test_already_rendered_is_not_escalated_twice():
    assert render.needs_render(_capture(SPA, tier="t1"), _ref()) is None


def test_non_html_is_never_rendered():
    assert render.needs_render(_capture("{}", ct="application/json"), _ref()) is None


def test_error_page_is_not_rendered():
    assert render.needs_render(_capture(SPA, status=500), _ref()) is None


def test_block_is_skipped_unless_explicitly_enabled(monkeypatch):
    blocked = _capture(SPA, status=403)
    assert render.needs_render(blocked, _ref()) is None

    monkeypatch.setattr(settings(), "render_on_block", True, raising=False)
    assert render.needs_render(blocked, _ref()) == "blocked"


def test_visible_text_ignores_scripts_and_chrome():
    noisy = (
        "<html><body><script>var a = 'x'.repeat(9999);</script>"
        "<nav>меню меню меню</nav><p>суть</p></body></html>"
    )
    assert render.visible_text_len(noisy) < 20


# ------------------------------------------------------------------ эскалация


async def test_missing_crawl4ai_falls_back_to_t0(monkeypatch):
    """Нет зависимости — отдаём T0, а не роняем страницу."""
    monkeypatch.setattr(
        render, "_get_crawler", _raiser(render.RendererUnavailable("нет crawl4ai"))
    )
    raw = _capture(SPA)
    assert await render.escalate(_ref(), raw) is raw


async def test_render_failure_falls_back_to_t0(monkeypatch):
    monkeypatch.setattr(render, "render", _raiser(render.RenderError("timeout")))
    raw = _capture(SPA)
    assert await render.escalate(_ref(), raw) is raw


async def test_successful_render_replaces_capture(monkeypatch):
    monkeypatch.setattr(render, "render", _returner(_capture(FULL, tier="t1")))
    result = await render.escalate(_ref(), _capture(SPA))

    assert result.tier == "t1"
    assert "Живой текст" in result.body.decode()


async def test_render_without_new_text_is_discarded(monkeypatch):
    """Если браузер вернул тот же пустой каркас — незачем подменять захват."""
    monkeypatch.setattr(render, "render", _returner(_capture(SPA, tier="t1")))
    raw = _capture(SPA)
    assert await render.escalate(_ref(), raw) is raw


async def test_disabled_tier_never_touches_browser(monkeypatch):
    monkeypatch.setattr(settings(), "render_enabled", False, raising=False)
    monkeypatch.setattr(render, "render", _raiser(AssertionError("браузер не должен вызываться")))
    raw = _capture(SPA)
    assert await render.escalate(_ref(), raw) is raw


# ------------------------------------------------------- режим crawl.render


def test_task_render_mode_defaults_to_auto():
    task = Task.model_validate({"name": "t", "discovery": {"seeds": ["https://a.example"]}})
    assert task.crawl.render == "auto"


def test_render_mode_rejects_garbage():
    with pytest.raises(ValidationError):
        Task.model_validate(
            {"name": "t", "discovery": {"seeds": ["https://a.example"]},
             "crawl": {"render": "sometimes"}}
        )


def test_forced_ref_skips_t0_probe():
    assert render.render_requested(_ref(render=True)) is True
    assert render.render_requested(_ref(render=False)) is False
    assert render.render_requested(_ref()) is False


def test_forced_probe_is_off_when_tier_disabled(monkeypatch):
    monkeypatch.setattr(settings(), "render_enabled", False, raising=False)
    assert render.render_requested(_ref(render=True)) is False


async def test_render_first_swallows_failures(monkeypatch):
    monkeypatch.setattr(render, "render", _raiser(render.RenderError("timeout")))
    assert await render.render_first(_ref(render=True)) is None


async def test_always_mode_does_not_touch_http(monkeypatch):
    """render=always экономит запрос T0, а не добавляет второй к тому же домену."""
    from harvester.connectors import generic_web

    monkeypatch.setattr(generic_web, "render_first", _returner(_capture(FULL, tier="t1")))
    monkeypatch.setattr(
        generic_web.httpx, "AsyncClient", _raiser(AssertionError("T0 не должен вызываться"))
    )

    raw = await generic_web.GenericWebConnector().fetch(
        SourceRef(url="https://example.com/p", hint={"render": True})
    )
    assert raw.tier == "t1"


async def test_always_mode_still_guards_against_ssrf():
    """Рендер не должен становиться дырой во внутреннюю сеть."""
    from harvester.connectors import generic_web

    with pytest.raises(generic_web.FetchError):
        await generic_web.GenericWebConnector().fetch(
            SourceRef(url="http://localhost:8080/admin", hint={"render": True})
        )


def _raiser(exc: Exception):
    async def _fn(*args, **kwargs):
        raise exc

    return _fn


def _returner(value):
    async def _fn(*args, **kwargs):
        return value

    return _fn
