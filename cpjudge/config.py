"""Configuration classes for CPJudge."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class Config:
    """Base configuration."""
    SECRET_KEY = os.environ.get("SECRET_KEY", "cpjudge-insecure-dev-secret-key-change-in-production")

    # Database configuration (SQLite default, PostgreSQL swappable via DATABASE_URL)
    raw_db_url = os.environ.get("DATABASE_URL")
    if raw_db_url:
        # SQLAlchemy 1.4+ requires 'postgresql://' instead of legacy 'postgres://'
        if raw_db_url.startswith("postgres://"):
            raw_db_url = raw_db_url.replace("postgres://", "postgresql://", 1)
        SQLALCHEMY_DATABASE_URI = raw_db_url
    else:
        # SQLite instance directory
        instance_dir = BASE_DIR / "instance"
        instance_dir.mkdir(parents=True, exist_ok=True)
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{instance_dir / 'cpjudge.db'}"

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Security limits
    # Max file upload / submission payload limit (64 KB)
    MAX_CONTENT_LENGTH = 64 * 1024

    # Rate limiting configuration (Flask-Limiter)
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_DEFAULT = "200 per day; 50 per hour"

    # CSRF protection
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = 3600

    # Sandboxing settings
    USE_BWRAP = os.environ.get("USE_BWRAP", "false").lower() in ("true", "1", "yes")
    SANDBOX_USER = os.environ.get("SANDBOX_USER", "judge_sandbox")


class DevelopmentConfig(Config):
    DEBUG = True


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False


class ProductionConfig(Config):
    DEBUG = False
    # In production, require an explicit SECRET_KEY
    SECRET_KEY = os.environ.get("SECRET_KEY")
    if not SECRET_KEY:
        # Generate persistent warning or require key
        SECRET_KEY = "cpjudge-production-secret-change-immediately"


config_by_name = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig,
}
