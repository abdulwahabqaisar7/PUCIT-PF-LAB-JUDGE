"""Configuration classes for CPJudge."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def normalize_db_url(url: str | None) -> str | None:
    """Normalizes database connection URL for SQLAlchemy (e.g. postgres:// -> postgresql://)."""
    if not url:
        return None
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


def get_default_db_uri() -> str:
    """Returns PostgreSQL URI if DATABASE_URL is set, else SQLite instance file URI."""
    raw = os.environ.get("DATABASE_URL")
    if raw:
        return normalize_db_url(raw)
    instance_dir = BASE_DIR / "instance"
    instance_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{instance_dir / 'cpjudge.db'}"


class Config:
    """Base configuration."""

    @property
    def SECRET_KEY(self) -> str:
        return os.environ.get("SECRET_KEY", "cpjudge-insecure-dev-secret-key-change-in-production")

    @property
    def SQLALCHEMY_DATABASE_URI(self) -> str:
        return get_default_db_uri()

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Security limits
    # Max HTTP payload limit (16 MB for teacher ZIP test case packages and CSV rosters)
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024
    # Max student code submission size (64 KB)
    MAX_STUDENT_CODE_BYTES = 64 * 1024

    # Rate limiting configuration (Flask-Limiter)
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_DEFAULT = "200 per day; 50 per hour"

    # CSRF protection
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = 3600

    # Sandboxing settings
    USE_BWRAP = os.environ.get("USE_BWRAP", "false").lower() in ("true", "1", "yes")
    SANDBOX_USER = os.environ.get("SANDBOX_USER", "judge_sandbox")

    # Timezone settings (Pakistan Standard Time, PKT / Asia/Karachi)
    APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "Asia/Karachi")
    TIMEZONE_LABEL = "PKT"

    # Reverse proxy, SSL, and session cookie security
    @property
    def USE_PROXY_FIX(self) -> bool:
        return os.environ.get("USE_PROXY_FIX", "false").lower() in ("true", "1", "yes")

    @property
    def SESSION_COOKIE_SECURE(self) -> bool:
        return os.environ.get("SESSION_COOKIE_SECURE", "false").lower() in ("true", "1", "yes")

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    @property
    def REMEMBER_COOKIE_SECURE(self) -> bool:
        return self.SESSION_COOKIE_SECURE

    REMEMBER_COOKIE_HTTPONLY = True

    @property
    def PREFERRED_URL_SCHEME(self) -> str:
        return os.environ.get("PREFERRED_URL_SCHEME", "http")


class DevelopmentConfig(Config):
    DEBUG = True


class TestingConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False

    @property
    def SQLALCHEMY_DATABASE_URI(self) -> str:
        return "sqlite:///:memory:"


class ProductionConfig(Config):
    DEBUG = False

    @property
    def SECRET_KEY(self) -> str:
        return os.environ.get("SECRET_KEY", "cpjudge-production-secret-change-immediately")

    # HTTPS and Reverse Proxy defaults for production internet deployment
    @property
    def PREFERRED_URL_SCHEME(self) -> str:
        return os.environ.get("PREFERRED_URL_SCHEME", "https")

    @property
    def SESSION_COOKIE_SECURE(self) -> bool:
        return os.environ.get("SESSION_COOKIE_SECURE", "false").lower() in ("true", "1", "yes")

    @property
    def REMEMBER_COOKIE_SECURE(self) -> bool:
        return self.SESSION_COOKIE_SECURE

    @property
    def USE_PROXY_FIX(self) -> bool:
        return os.environ.get("USE_PROXY_FIX", "false").lower() in ("true", "1", "yes")


config_by_name = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig,
}
