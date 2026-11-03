"""Flask application factory for CPJudge."""
import os
from flask import Flask, redirect, render_template, request, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager, current_user
from flask_wtf.csrf import CSRFProtect

from cpjudge.config import config_by_name
from cpjudge.models import User, db

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Please log in to access this page."
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

    # Register blueprints
    from cpjudge.auth import auth_bp
    from cpjudge.auth.decorators import register_auth_hooks
    from cpjudge.student import student_bp
    from cpjudge.teacher import teacher_bp
    from cpjudge.utils.markdown import render_markdown
    from cpjudge.utils.timezone import format_pkt, to_pkt

    app.register_blueprint(auth_bp)
    app.register_blueprint(teacher_bp)
    app.register_blueprint(student_bp)
    register_auth_hooks(app)

    # Register template filters
    app.jinja_env.filters["render_markdown"] = render_markdown
    app.jinja_env.filters["format_pkt"] = format_pkt
    app.jinja_env.filters["to_pkt"] = to_pkt

    # Global template context
    @app.context_processor
    def inject_timezone():
        return {
            "TIMEZONE_LABEL": app.config.get("TIMEZONE_LABEL", "PKT"),
            "APP_TIMEZONE": app.config.get("APP_TIMEZONE", "Asia/Karachi"),
        }

    # Root route
    @app.route("/")
    def index():
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login"))
        if current_user.force_password_change:
            return redirect(url_for("auth.change_password"))
        if current_user.is_teacher:
            try:
                return redirect(url_for("teacher.dashboard"))
            except Exception:
                return redirect(url_for("auth.profile"))
        else:
            try:
                return redirect(url_for("student.dashboard"))
            except Exception:
                return redirect(url_for("auth.profile"))

    # Error handlers
    @app.errorhandler(403)
    def forbidden_error(e):
        return render_template("errors/403.html", error=e), 403

    @app.errorhandler(404)
    def not_found_error(e):
        return render_template("errors/404.html", error=e), 404

    @app.errorhandler(429)
    def ratelimit_error(e):
        return render_template("errors/429.html", error=e), 429

    return app
