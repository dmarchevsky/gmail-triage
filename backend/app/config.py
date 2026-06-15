"""Application configuration from environment variables.

Runtime-tunable settings (polling interval, LLM URL overrides, etc.) live in
the `settings` DB table (see services/settings_service.py); this module only
covers what must be known before the DB is available.
"""

import base64
import hashlib
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic_settings import BaseSettings, SettingsConfigDict

FORBIDDEN_SECRET_VALUES = {"", "changeme", "change-me", "default", "secret"}


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_secret_key: str = ""
    ui_password: str = ""
    data_dir: Path = Path("./data")
    database_url: str = ""  # derived from data_dir when empty
    llm_base_url: str = "http://host.docker.internal:8081/v1"
    llm_model: str = "local"
    host: str = "0.0.0.0"
    port: int = 8080
    tz: str = "UTC"
    log_level: str = "INFO"
    # Path to built frontend assets; served if present.
    static_dir: Path = Path(__file__).resolve().parent.parent / "static"

    def validate_secrets(self) -> None:
        """Refuse to run with missing/default secrets (spec §6.2, §6.4)."""
        if self.app_secret_key.strip().lower() in FORBIDDEN_SECRET_VALUES:
            raise RuntimeError(
                "APP_SECRET_KEY is not set (or is a known default). "
                "Set a strong random value in the environment; refusing to start."
            )
        if self.ui_password.strip().lower() in FORBIDDEN_SECRET_VALUES:
            raise RuntimeError(
                "UI_PASSWORD is not set (or is a known default). It is the bootstrap "
                "password (overridable, and disable-able, at runtime in Settings); "
                "refusing to start without it."
            )

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.data_dir / 'mailtriage.db'}"

    def fernet(self) -> Fernet:
        """Fernet keyed from APP_SECRET_KEY (sha256 -> urlsafe b64)."""
        digest = hashlib.sha256(self.app_secret_key.encode()).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


@lru_cache
def get_config() -> AppConfig:
    return AppConfig()
