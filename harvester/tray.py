"""Иконка в строке меню: статус прогона, запуск задач, быстрые ссылки.

Трей общается с системой **только по HTTP**, не лезет в БД и Redis напрямую. Поэтому
он одинаково работает и когда API поднят в Docker, и когда локально, и когда на datadom
(HARVESTER_API=http://<server-lan-ip>:8080).

macOS: pystray требует главного потока, поэтому опрос состояния живёт в фоновом потоке,
а иконка — в главном. Уведомления идут через osascript: он не зависит от бэкенда pystray
и показывает нормальный системный баннер.

Запуск:  harvester-tray
"""

from __future__ import annotations

import logging
import os
import pathlib
import subprocess
import sys
import threading
import webbrowser
from dataclasses import dataclass, field
from typing import Any

import httpx
from PIL import Image, ImageDraw

try:
    import pystray
except ImportError:  # pragma: no cover - подсказка вместо трейсбека
    sys.exit("Нужен pystray:  pip install pystray")

log = logging.getLogger("harvester.tray")

API = os.environ.get("HARVESTER_API", "http://localhost:8080")
TASKS_DIR = pathlib.Path(os.environ.get("HARVESTER_TASKS", "tasks")).resolve()
POLL_SECONDS = float(os.environ.get("HARVESTER_TRAY_POLL", "4"))

ICON_SIZE = 44


# --------------------------------------------------------------------------- состояние


@dataclass
class State:
    online: bool = False
    run: dict[str, Any] | None = None      # активный прогон, если есть
    tasks: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @property
    def running(self) -> bool:
        return self.run is not None

    @property
    def headline(self) -> str:
        if not self.online:
            return "API недоступен"
        if not self.run:
            docs = sum(int(t.get("documents") or 0) for t in self.tasks)
            return f"Простой · {docs} документов"
        s = self.run.get("stats") or {}
        return (
            f"Идёт «{self.run.get('task_name')}» · "
            f"{s.get('documents', 0)} док · {s.get('media', 0)} изобр"
        )


# ------------------------------------------------------------------------------ иконка


def make_icon(state: State) -> Image.Image:
    """Рисуем иконку сами: цвет кольца = состояние, без внешних файлов."""
    if not state.online:
        color = (200, 90, 95, 255)      # красный — связи нет
    elif state.running:
        color = (90, 180, 130, 255)     # зелёный — идёт обход
    else:
        color = (140, 140, 140, 255)    # серый — простой

    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad, width = 5, 5
    d.ellipse([pad, pad, ICON_SIZE - pad, ICON_SIZE - pad], outline=color, width=width)
    if state.running:
        # заполненная сердцевина = активность
        c, r = ICON_SIZE // 2, 7
        d.ellipse([c - r, c - r, c + r, c + r], fill=color)
    return img


def notify(icon: pystray.Icon, title: str, message: str) -> None:
    """Системный баннер. osascript надёжнее бэкенда pystray на macOS."""
    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'display notification {message!r} with title {title!r}'],
                check=False, capture_output=True, timeout=5,
            )
            return
        except Exception:
            pass
    try:
        icon.notify(message, title)
    except Exception:
        log.debug("уведомление не показано", exc_info=True)


def clipboard_urls() -> list[str]:
    """Ссылки из буфера обмена — по одной в строке."""
    if sys.platform != "darwin":
        return []
    try:
        text = subprocess.run(
            ["pbpaste"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return []
    return [
        line.strip() for line in text.splitlines()
        if line.strip().startswith(("http://", "https://"))
    ]


# --------------------------------------------------------------------------- API-клиент


class Client:
    def __init__(self, base: str = API):
        self.base = base.rstrip("/")
        self._http = httpx.Client(timeout=6.0)

    def _get(self, path: str) -> Any:
        r = self._http.get(self.base + path)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, **kw) -> Any:
        r = self._http.post(self.base + path, **kw)
        r.raise_for_status()
        return r.json()

    def snapshot(self) -> State:
        try:
            runs = self._get("/runs?active=true&limit=1")["runs"]
            tasks = self._get("/tasks?limit=50")["tasks"]
            return State(online=True, run=runs[0] if runs else None, tasks=tasks)
        except Exception as exc:
            return State(online=False, error=f"{type(exc).__name__}: {exc}")

    def start_task_file(self, path: pathlib.Path) -> str:
        created = self._post(
            "/tasks",
            content=path.read_bytes(),
            headers={"Content-Type": "text/yaml"},
        )
        run = self._post(f"/tasks/{created['task_id']}/runs")
        return run["run_id"]

    def start_task_id(self, task_id: str) -> str:
        return self._post(f"/tasks/{task_id}/runs")["run_id"]

    def grab(self, urls: list[str], depth: int = 0) -> dict[str, Any]:
        return self._post("/grab", json={"urls": urls, "depth": depth})

    def stop(self, run_id: str) -> None:
        self._post(f"/runs/{run_id}/stop")

    def close(self) -> None:
        self._http.close()


# -------------------------------------------------------------------------- приложение


class TrayApp:
    def __init__(self) -> None:
        self.client = Client()
        self.state = State()
        self._stop_polling = threading.Event()
        self.icon = pystray.Icon(
            "harvester", make_icon(self.state), "Universal Harvester",
            menu=self._menu(),
        )

    # ------------------------------------------------------------------ меню
    def _menu(self) -> pystray.Menu:
        Item, Menu = pystray.MenuItem, pystray.Menu
        return Menu(
            Item(lambda _: self.state.headline, None, enabled=False),
            Menu.SEPARATOR,
            Item("Собрать ссылку из буфера", self._on_grab_clipboard),
            Item("Запустить задачу", Menu(self._task_items)),
            Item("Остановить прогон", self._on_stop, visible=lambda _: self.state.running),
            Menu.SEPARATOR,
            Item("Открыть результаты", lambda: webbrowser.open(f"{API}/ui")),
            Item("Ещё", Menu(
                Item("Swagger (API)", lambda: webbrowser.open(f"{API}/docs")),
                Item("MinIO — сырьё и картинки", lambda: webbrowser.open("http://localhost:9001")),
                Item("Meilisearch", lambda: webbrowser.open("http://localhost:7700")),
                Item("SearXNG", lambda: webbrowser.open("http://localhost:8888")),
            )),
            Menu.SEPARATOR,
            Item("Обновить сейчас", self._refresh_now),
            Item("Выход", self._on_quit),
        )

    def _task_items(self):
        """Задачи из БД плюс YAML-файлы, которых там ещё нет."""
        seen: set[str] = set()
        for task in self.state.tasks:
            seen.add(task["name"])
            yield pystray.MenuItem(
                f"{task['name']} ({task['documents']})",
                self._make_starter(task_id=task["id"], label=task["name"]),
            )
        if TASKS_DIR.is_dir():
            files = [p for p in sorted(TASKS_DIR.glob("*.yaml")) if p.stem not in seen]
            if files and seen:
                yield pystray.Menu.SEPARATOR
            for path in files:
                yield pystray.MenuItem(
                    f"{path.stem} (из файла)", self._make_starter(path=path, label=path.stem)
                )
        if not self.state.tasks and not TASKS_DIR.is_dir():
            yield pystray.MenuItem("нет задач", None, enabled=False)

    # --------------------------------------------------------------- действия
    def _make_starter(self, *, label: str, task_id: str | None = None,
                      path: pathlib.Path | None = None):
        def start(icon=None, item=None):
            threading.Thread(
                target=self._start, args=(label, task_id, path), daemon=True
            ).start()
        return start

    def _start(self, label: str, task_id: str | None, path: pathlib.Path | None) -> None:
        try:
            if self.state.running:
                notify(self.icon, "Harvester", "Прогон уже идёт — сначала остановите его")
                return
            run_id = (
                self.client.start_task_id(task_id) if task_id
                else self.client.start_task_file(path)
            )
            notify(self.icon, "Harvester", f"Запущено: {label}")
            log.info("запущен прогон %s (%s)", run_id, label)
        except Exception as exc:
            notify(self.icon, "Harvester — ошибка", f"Не удалось запустить {label}: {exc}")
        finally:
            self._refresh_now()

    def _on_grab_clipboard(self, icon=None, item=None) -> None:
        """Скопировал ссылку — забрал страницу. Самый короткий путь из всех."""

        def go() -> None:
            urls = [u for u in clipboard_urls() if u]
            if not urls:
                notify(self.icon, "Harvester", "В буфере нет ссылки")
                return
            try:
                res = self.client.grab(urls)
                notify(self.icon, "Harvester",
                       f"Собираю {len(urls)} адр.: {res.get('name', '')}")
            except Exception as exc:
                notify(self.icon, "Harvester — ошибка", str(exc))
            finally:
                self._refresh_now()

        threading.Thread(target=go, daemon=True).start()

    def _on_stop(self, icon=None, item=None) -> None:
        run = self.state.run
        if not run:
            return

        def stop() -> None:
            try:
                self.client.stop(run["id"])
                notify(self.icon, "Harvester", "Останавливаю прогон, собранное сохранится")
            except Exception as exc:
                notify(self.icon, "Harvester — ошибка", str(exc))
            finally:
                self._refresh_now()

        threading.Thread(target=stop, daemon=True).start()

    def _on_quit(self, icon=None, item=None) -> None:
        self._stop_polling.set()
        self.client.close()
        self.icon.stop()

    def _refresh_now(self, icon=None, item=None) -> None:
        threading.Thread(target=self._poll_once, daemon=True).start()

    # ------------------------------------------------------------------ опрос
    def _poll_once(self) -> None:
        previous = self.state
        self.state = self.client.snapshot()
        self.icon.icon = make_icon(self.state)
        self.icon.title = f"Harvester — {self.state.headline}"
        self.icon.update_menu()

        # прогон только что завершился — сообщаем итог
        if previous.running and not self.state.running and self.state.online:
            stats = (previous.run or {}).get("stats") or {}
            notify(
                self.icon, "Harvester",
                f"Прогон завершён: {stats.get('documents', 0)} документов, "
                f"{stats.get('media', 0)} изображений",
            )

    def _poll_loop(self) -> None:
        while not self._stop_polling.is_set():
            try:
                self._poll_once()
            except Exception:
                log.debug("опрос не удался", exc_info=True)
            self._stop_polling.wait(POLL_SECONDS)

    def run(self) -> None:
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self.icon.run()   # блокирует главный поток — требование macOS


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # трей опрашивает API каждые несколько секунд — без этого лог забивается
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    TrayApp().run()


if __name__ == "__main__":
    main()
