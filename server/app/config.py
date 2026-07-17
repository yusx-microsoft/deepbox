"""Environment-backed deepbox server configuration.

Development defaults keep local setup simple. Production mode deliberately
fails closed when the signing secret or browser origin allowlist is missing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SECRET = "dev-secret-change-me"


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _origins(raw: str) -> frozenset[str]:
    return frozenset(
        value.strip().rstrip("/")
        for value in raw.split(",")
        if value.strip()
    )


@dataclass(frozen=True)
class Settings:
    environment: str
    secret: str
    database_url: str
    data_dir: Path
    public_url: str | None
    allowed_origins: frozenset[str]
    cookie_secure: bool
    cookie_samesite: str
    host: str
    port: int

    @property
    def production(self) -> bool:
        return self.environment == "production"

    def origin_allowed(self, origin: str | None) -> bool:
        # Development remains convenient for localhost. Production always has
        # a non-empty allowlist because validate() rejects an empty one.
        if not self.allowed_origins:
            return not self.production
        if not origin:
            return False
        return origin.rstrip("/") in self.allowed_origins

    def validate(self) -> None:
        if self.environment not in {"development", "test", "production"}:
            raise RuntimeError("DEEPBOX_ENV must be development, test, or production")
        if self.production and self.secret == DEFAULT_SECRET:
            raise RuntimeError("DEEPBOX_SECRET must be set in production")
        if self.production and not self.allowed_origins:
            raise RuntimeError("DEEPBOX_ALLOWED_ORIGINS must be set in production")
        if self.production and not self.cookie_secure:
            raise RuntimeError("DEEPBOX_COOKIE_SECURE must be true in production")
        if self.cookie_samesite not in {"lax", "strict", "none"}:
            raise RuntimeError("DEEPBOX_COOKIE_SAMESITE must be lax, strict, or none")
        if self.production and self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("DEEPBOX_HOST must be loopback in production")
        if self.production and any(not origin.startswith("https://") for origin in self.allowed_origins):
            raise RuntimeError("production origins must use HTTPS")
        if not (1 <= self.port <= 65535):
            raise RuntimeError("DEEPBOX_PORT must be between 1 and 65535")


def load_settings() -> Settings:
    public_url = os.getenv("DEEPBOX_PUBLIC_URL", "").strip().rstrip("/") or None
    allowed = _origins(os.getenv("DEEPBOX_ALLOWED_ORIGINS", ""))
    # A configured public URL is also an allowed browser origin unless the
    # operator explicitly supplies additional origins.
    if public_url:
        allowed = frozenset({*allowed, public_url})
    result = Settings(
        environment=os.getenv("DEEPBOX_ENV", "development").strip().lower(),
        secret=os.getenv("DEEPBOX_SECRET", DEFAULT_SECRET),
        database_url=os.getenv("DEEPBOX_DATABASE_URL", "sqlite:///deepbox.db"),
        data_dir=Path(os.getenv("DEEPBOX_DATA_DIR", str(PROJECT_DIR / "data"))).resolve(),
        public_url=public_url,
        allowed_origins=allowed,
        cookie_secure=_bool("DEEPBOX_COOKIE_SECURE", False),
        cookie_samesite=os.getenv("DEEPBOX_COOKIE_SAMESITE", "lax").strip().lower(),
        host=os.getenv("DEEPBOX_HOST", "127.0.0.1").strip(),
        port=int(os.getenv("DEEPBOX_PORT", "8077")),
    )
    result.validate()
    return result


settings = load_settings()
