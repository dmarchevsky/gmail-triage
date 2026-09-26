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


def split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_secret_key: str = ""
    # Cloudflare Access (see app/cf_access.py). The UI is reachable only through a
    # cloudflared tunnel behind an Access application that signs visitors in with
    # Google; the app verifies the Access JWT and allowlists its email claim.
    cf_access_team_domain: str = ""  # e.g. yourteam.cloudflareaccess.com
    cf_access_aud: str = ""  # comma-separated AUD tags (one per Access application)
    cf_access_issuer: str = ""  # comma-separated; defaults to https://<team domain>
    cf_access_allowed_emails: str = ""  # comma-separated Google addresses
    # Local development only: skip Access verification entirely. Mutually
    # exclusive with CF_ACCESS_* so it can never be switched on in production.
    dev_auth: bool = False
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
        cf_values = (self.cf_access_team_domain, self.cf_access_aud,
                     self.cf_access_issuer, self.cf_access_allowed_emails)
        if self.dev_auth:
            if any(v.strip() for v in cf_values):
                raise RuntimeError(
                    "DEV_AUTH is on together with CF_ACCESS_* settings. DEV_AUTH disables "
                    "authentication and is for local development only; refusing to start."
                )
            return
        missing = [name for name, value in (
            ("CF_ACCESS_TEAM_DOMAIN", self.cf_access_team_domain),
            ("CF_ACCESS_AUD", self.cf_access_aud),
            ("CF_ACCESS_ALLOWED_EMAILS", self.cf_access_allowed_emails),
        ) if not split_csv(value)]
        if missing:
            raise RuntimeError(
                f"{', '.join(missing)} not set. The UI is protected only by Cloudflare "
                "Access; set them (or DEV_AUTH=true for local development). "
                "Refusing to start."
            )

    @property
    def cf_access_audiences(self) -> list[str]:
        return split_csv(self.cf_access_aud)

    @property
    def cf_access_issuers(self) -> list[str]:
        # Normally the team domain. A renamed Zero Trust team keeps its ORIGINAL name
        # in `iss`, so the issuer is settable on its own rather than derived.
        return split_csv(self.cf_access_issuer) or [f"https://{self.cf_access_team_domain}"]

    @property
    def allowed_emails(self) -> set[str]:
        return {e.lower() for e in split_csv(self.cf_access_allowed_emails)}

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
