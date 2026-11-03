"""Tests for production internet deployment configuration, cookies, and reverse proxy support."""
import os
from werkzeug.middleware.proxy_fix import ProxyFix

from cpjudge.app import create_app
from cpjudge.config import ProductionConfig


def test_proxy_fix_middleware_enabled_when_configured(monkeypatch):
    """Verifies that ProxyFix middleware wraps wsgi_app when USE_PROXY_FIX is true."""
    monkeypatch.setenv("USE_PROXY_FIX", "true")
    app = create_app("testing")
    assert isinstance(app.wsgi_app, ProxyFix)


def test_production_config_cookie_and_security_defaults(monkeypatch):
    """Verifies security defaults and cookie options for production internet deployments."""
    monkeypatch.setenv("SECRET_KEY", "test-prod-key-1234567890abcdef")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
    monkeypatch.setenv("USE_PROXY_FIX", "true")

    app = create_app("production")
    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config["REMEMBER_COOKIE_SECURE"] is True
    assert app.config["REMEMBER_COOKIE_HTTPONLY"] is True
    assert app.config["PREFERRED_URL_SCHEME"] == "https"
    assert isinstance(app.wsgi_app, ProxyFix)


def test_database_url_postgresql_dialect_normalization(monkeypatch):
    """Verifies legacy postgres:// URLs are converted to modern postgresql:// for SQLAlchemy."""
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@localhost:5432/cpjudge")
    prod_conf = ProductionConfig()
    assert prod_conf.SQLALCHEMY_DATABASE_URI.startswith("postgresql://")
