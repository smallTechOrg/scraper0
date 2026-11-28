"""Typed settings (prefix SCRAPER0_), from the environment and `.env` at the repo root.

Secrets are SecretStr and never logged. Everything a tenant or the platform admin can change at
runtime (AI providers, keys, budgets, sources) lives in the database, not here.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

JobMode = Literal["thread", "inline", "cloud_tasks"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCRAPER0_", env_file=REPO_ROOT / ".env", env_file_encoding="utf-8",
                                      env_ignore_empty=True, extra="ignore")

    env: Literal["local", "staging", "production", "test"] = "local"
    host: str = "127.0.0.1"
    port: int = 5001
    log_format: Literal["json", "console"] = "json"
    log_level: str = "INFO"

    # --- database: the hashtaglocal Postgres instance, own `scraper0` database. Hard cap on connections.
    database_url: str = f"sqlite:///{REPO_ROOT}/data/scraper0.db"
    db_pool_size: int = Field(2, ge=1, le=10)
    db_max_overflow: int = Field(1, ge=0, le=10)
    auto_migrate: bool = True

    # --- crypto: Fernet key encrypting AI keys / portal credentials at rest. Required outside local/test.
    secret_key: SecretStr | None = None

    # --- dashboard sessions
    session_days: int = 14
    cookie_secure: bool = False
    frontend_url: str = "http://localhost:3000"
    cors_origins: list[str] = ["http://localhost:3000"]

    # --- bootstrap (first boot only; then managed in the dashboard)
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: SecretStr | None = None
    bootstrap_groq_api_key: SecretStr | None = None
    bootstrap_groq_model: str = "meta-llama/llama-4-scout-17b-16e-instruct"

    # --- jobs: thread (local worker), inline (run inside the submitting request), cloud_tasks (Cloud Run)
    job_mode: JobMode = "thread"
    job_workers: int = Field(2, ge=1, le=8)
    internal_token: SecretStr | None = None          # shared secret for /internal/* (Cloud Tasks / Scheduler)
    recipe_export_dir: str | None = None             # when set, locking a version writes its recipe file here
    public_base_url: str = "http://127.0.0.1:5001"   # where Cloud Tasks calls back
    cloud_tasks_queue: str | None = None             # projects/<p>/locations/<l>/queues/<q>
    cloud_tasks_service_account: str | None = None

    # --- crawling
    min_host_interval_s: float = 1.0
    respect_robots: bool = True
    # auto = render pages that are JavaScript shells in headless Chromium (and read the JSON APIs they call)
    render_js: Literal["auto", "never"] = "auto"

    # --- connectors
    captcha_solver: Literal["llm", "easyocr", "auto"] = "llm"   # auto = easyocr when installed, else llm
    browser_headless: bool = True

    # --- BBMP complaint attachments (downloaded from media_url)
    max_upload_mb: int = 40

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()
