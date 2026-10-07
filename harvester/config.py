from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Конфиг из окружения. См. .env.example."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- инфраструктура ---
    database_url: str = "postgresql://harvester:harvester@localhost:5432/harvester"
    redis_url: str = "redis://localhost:6379/0"

    s3_endpoint: str = "http://localhost:9000"
    # Адрес, по которому хранилище видно снаружи (браузеру). В compose api ходит в
    # minio:9000, но presigned-ссылка с таким хостом у пользователя не откроется.
    s3_public_endpoint: str | None = None
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "harvester"
    s3_region: str = "us-east-1"

    meili_url: str = "http://localhost:7700"
    meili_key: str = "harvester-dev-key"

    searxng_url: str = "http://localhost:8888"

    # --- поведение краулера ---
    user_agent: str = (
        "UniversalHarvester/0.1 (+research crawler; contact: admin@example.com)"
    )
    fetch_timeout_s: float = 20.0
    max_response_bytes: int = 8 * 1024 * 1024
    max_media_bytes: int = 25 * 1024 * 1024
    crawl_workers: int = 8
    media_workers: int = 4

    # --- тир T1: рендер в браузере (crawl4ai) ---
    # Выключен по умолчанию: требует `pip install -e '.[render]'` и `crawl4ai-setup`.
    render_enabled: bool = False
    # Вкладок одновременно. Не равно crawl_workers: вкладка стоит ~150–300 МБ,
    # а T1 нужен меньшинству страниц — очередь всё равно движется на T0.
    render_concurrency: int = 3
    render_timeout_s: float = 45.0
    # Ниже этого объёма видимого текста страница считается недорендеренной.
    render_min_text_chars: int = 500
    render_wait_until: str = "domcontentloaded"
    render_wait_ms: int = 0
    render_scroll: bool = False
    # Пробовать браузер на 403/429. Обычно бесполезно: настоящая антибот-защита
    # требует stealth-фетчера (Scrapling), а не просто headless-браузера.
    render_on_block: bool = False

    # --- API ---
    api_host: str = "0.0.0.0"
    api_port: int = 8080


@lru_cache
def settings() -> Settings:
    return Settings()
