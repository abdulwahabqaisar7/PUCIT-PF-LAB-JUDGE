"""Flask application factory for CPJudge."""
import os
from flask import Flask
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from cpjudge.config import config_by_name
from cpjudge.models import User, db

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message_category = "info"

csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address)


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    return db.session.get(User, int(user_id))


def create_app(config_name: str | None = None) -> Flask:
    """Creates and configures the Flask application."""
    if config_name is None:
        config_name = os.environ.get("FLASK_ENV", "development")

    app = Flask(__name__)
    config_class = config_by_name.get(config_name, config_by_name["default"])
    app.config.from_object(config_class)

    # Initialize extensions
    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    # Register blueprints (will be populated in later phases)
    # e.g., auth, teacher, student

    return app
