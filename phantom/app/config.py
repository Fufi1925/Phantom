from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    phantom_base_url: str = "http://127.0.0.1:8787/phantom"
    phantom_secret_key: str = "dev-only-change-me"
    phantom_cookie_name: str = "phantom_session"
    phantom_cookie_path: str = "/phantom"
    phantom_session_max_age: int = 60 * 60 * 24 * 7

    phantom_discord_client_id: str = ""
    phantom_discord_client_secret: str = ""

    phantom_bot_token: str = ""
    phantom_bot_owner_ids: str = ""

    phantom_brand_name: str = "Phantom"
    phantom_footer: str = "Powered by University"

    phantom_host: str = "0.0.0.0"
    phantom_port: int = 8787
    phantom_db_path: str = "data/phantom.db"

    @property
    def base_url(self) -> str:
        return self.phantom_base_url.rstrip("/")

    @property
    def root_path(self) -> str:
        # "/phantom" from https://x/phantom
        from urllib.parse import urlparse

        path = urlparse(self.base_url).path.rstrip("/")
        return path or ""

    @property
    def oauth_redirect_uri(self) -> str:
        return f"{self.base_url}/auth/callback"

    @property
    def owner_ids(self) -> set[int]:
        out: set[int] = set()
        for part in (self.phantom_bot_owner_ids or "").split(","):
            part = part.strip()
            if part.isdigit():
                out.add(int(part))
        return out

    @property
    def db_path(self) -> Path:
        import os
        # On Railway the shared volume is /data — keep Phantom isolated there.
        data_dir = os.getenv("DATA_DIR", "").strip()
        p = Path(self.phantom_db_path)
        if data_dir:
            base = Path(data_dir) / "phantom"
            base.mkdir(parents=True, exist_ok=True)
            return base / (p.name if not p.is_absolute() else p.name)
        if not p.is_absolute():
            p = ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache
def get_settings() -> Settings:
    return Settings()
