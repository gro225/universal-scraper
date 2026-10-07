"""Логика трея без запуска GUI: строка состояния, иконка, поведение при недоступном API."""

from harvester.tray import Client, State, make_icon


def test_headline_offline():
    assert State(online=False).headline == "API недоступен"


def test_headline_idle_counts_documents():
    state = State(online=True, tasks=[{"documents": 400}, {"documents": 77}])
    assert state.headline == "Простой · 477 документов"
    assert state.running is False


def test_headline_running_shows_live_counters():
    state = State(
        online=True,
        run={"task_name": "sol-lemon", "stats": {"documents": 128, "media": 340}},
    )
    assert state.running is True
    assert "sol-lemon" in state.headline
    assert "128 док" in state.headline
    assert "340 изобр" in state.headline


def test_headline_survives_missing_stats():
    """Только что запущенный прогон ещё не имеет метрик — падать нельзя."""
    state = State(online=True, run={"task_name": "x"})
    assert "0 док" in state.headline


def test_icon_colour_differs_per_state():
    offline = make_icon(State(online=False)).tobytes()
    idle = make_icon(State(online=True)).tobytes()
    running = make_icon(State(online=True, run={"task_name": "t"})).tobytes()

    assert offline != idle != running
    assert offline != running


def test_icon_is_transparent_rgba():
    img = make_icon(State(online=True))
    assert img.mode == "RGBA"
    assert img.getpixel((0, 0))[3] == 0  # угол прозрачный


def test_snapshot_reports_offline_instead_of_raising():
    """Трей не должен падать, когда API не поднят."""
    client = Client("http://127.0.0.1:9")  # порт discard, соединения не будет
    state = client.snapshot()
    client.close()

    assert state.online is False
    assert state.error
    assert state.headline == "API недоступен"
