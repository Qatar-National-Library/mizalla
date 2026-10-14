import getpass
import logging
import os
import secrets
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for
from flask_login import LoginManager
from flask_wtf.csrf import CSRFError, CSRFProtect

from .extensions import db
from .models import Member
from .schema import upgrade_schema

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Sign in to continue."
csrf = CSRFProtect()


def _wants_json() -> bool:
    """Whether this request came from a JS ``fetch`` that will parse the body as JSON.

    The composers post with ``fetch`` and call ``response.json()`` unconditionally,
    so any HTML answer - a styled error page, a redirect to the login form, or
    Flask-WTF's CSRF rejection - surfaces as a JSON parse error that quotes the
    markup and says nothing about why the request failed. Marking the request lets
    those same failures answer in JSON, so the page can show the real reason.
    """
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    accept = request.accept_mimetypes
    return bool(accept["application/json"]) and not accept["text/html"]


def _error_page(status: int, title: str, message: str):
    """The styled error page for a browser, or a JSON error for a fetch.

    Both carry the same message, so a user sees the same words either way and the
    JS has something to display instead of a parser error.
    """
    if _wants_json():
        return jsonify(error=message), status
    return render_template("error.html", title=title, message=message), status


def _load_or_create_secret_key(instance_path: str) -> str:
    configured_key = os.environ.get("SECRET_KEY")
    if configured_key:
        return configured_key

    secret_path = Path(instance_path) / ".secret_key"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing_key = secret_path.read_text(encoding="utf-8").strip()
        if existing_key:
            return existing_key
    except FileNotFoundError:
        pass

    generated_key = secrets.token_hex(32)
    try:
        with secret_path.open("x", encoding="utf-8") as secret_file:
            secret_file.write(generated_key)
        return generated_key
    except FileExistsError:
        return secret_path.read_text(encoding="utf-8").strip()


@login_manager.user_loader
def load_user(member_id: str) -> Member | None:
    return db.session.get(Member, int(member_id))


DEFAULT_PROJECT_TEAM_ROLES = [
    "Project Manager",
    "Analyst",
    "Developer/Implementer",
    "Qa/Tester",
    "Infrastructure Management",
    "Integration Manager",
    "Configuration Manager",
    "Deployment Manager",
]


def _load_env_file(env_file: Path) -> None:
    """Populate ``os.environ`` from the ``.env`` file beside the package.

    The ``flask run`` CLI loads ``.env`` on its own, but a WSGI server, a cron
    job, and a test that calls :func:`create_app` directly all do not - and the
    difference is invisible until a credential silently reads as unset and a
    service reports itself unconfigured. Loading it here makes every entry point
    agree. Real environment variables are never overridden by this call, so
    under a WSGI server a properly configured host beats the file.

    Note that ``flask run`` and ``app.run()`` are the exception: both call
    Flask's own ``load_dotenv(revert=False)`` first, which re-applies ``.env``
    with ``override=True``. In those two entry points ``.env`` wins over the
    process environment, whatever :func:`create_app` then does.

    A missing python-dotenv is a warning, not a crash: the app still starts, and
    the settings screen reports the credentials as unset, which is a truer
    answer than refusing to boot.
    """
    if not env_file.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        current_app_logger = logging.getLogger(__name__)
        current_app_logger.warning(
            "python-dotenv is not installed, so %s was not read. Install it or set the "
            "credentials in the process environment.", env_file,
        )
        return
    load_dotenv(env_file, override=True)


def _load_project_team_roles() -> list[str]:
    raw = os.environ.get("PROJECT_TEAM_ROLES")
    if raw:
        roles = [r.strip() for r in raw.split(",") if r.strip()]
        if roles:
            return roles
    return list(DEFAULT_PROJECT_TEAM_ROLES)


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    # Ahead of from_mapping: SECRET_KEY and the Azure credential checks both read
    # os.environ, and .env is where those values live.
    _load_env_file(Path(app.root_path).parent / ".env")
    app.config.from_mapping(
        SECRET_KEY=_load_or_create_secret_key(app.instance_path),
        SQLALCHEMY_DATABASE_URI="sqlite:///mizalla.db",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        JIRA_BASE_URL=os.environ.get("JIRA_BASE_URL", "https://jira.qnl.qa/").strip(),
        PROJECT_TEAM_ROLES=_load_project_team_roles(),
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
        MAX_RECIPIENTS=int(os.environ.get("MAX_RECIPIENTS", "2000")),
        SENDGRID_API_KEY=os.environ.get("SENDGRID_API_KEY", "").strip(),
        UPLOAD_FOLDER=str(Path(app.instance_path) / "uploads"),
        AUTO_CREATE_DB=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true",
    )
    if test_config:
        app.config.update(test_config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    from .admin import bp as admin_bp
    from .ai import bp as ai_bp
    from .api import bp as api_bp
    from .auth import bp as auth_bp
    from .bulk_email import bp as bulk_email_bp
    from .bulk_sms import bp as bulk_sms_bp
    from .routes import bp as main_bp
    from .upload_file import bp as upload_file_bp
    from .video_upload import bp as video_upload_bp
    from .whatsapp import bp as whatsapp_bp
    from .feedback import bp as feedback_bp
    from .remote_exec import bp as remote_exec_bp

    csrf.exempt(api_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(bulk_email_bp)
    app.register_blueprint(bulk_sms_bp)
    app.register_blueprint(whatsapp_bp)
    app.register_blueprint(ai_bp)
    app.register_blueprint(upload_file_bp)
    app.register_blueprint(video_upload_bp)
    app.register_blueprint(feedback_bp)
    app.register_blueprint(remote_exec_bp)
    
    from .teams import is_future_teams_meeting
    app.jinja_env.globals["is_future_teams_meeting"] = is_future_teams_meeting

    from .access import is_pm_membership
    from .feature_access import has_feature_access
    from .models import FEATURE_LABELS
    from .service_nav import nav_services

    app.jinja_env.globals["feature_labels"] = FEATURE_LABELS
    # Used by the project people lists to badge the rows that carry Project
    # Manager authority, so the markup does not restate the rule.
    app.jinja_env.globals["is_pm_membership"] = is_pm_membership
    # Templates use this to decide whether to show a service link at all, so
    # it has to be a callable rather than a value computed at import time.
    app.jinja_env.globals["has_feature_access"] = has_feature_access
    # Builds the grant-gated header links. Also a callable: the grants are read
    # from the database per request.
    app.jinja_env.globals["nav_services"] = nav_services
    from .jira import jira_ticket_url

    # No base_url argument: the helper resolves the hot jira_base_url setting
    # so an administrator override applies without a restart.
    app.jinja_env.globals["jira_ticket_url"] = jira_ticket_url

    @app.errorhandler(413)
    def upload_too_large(_error):
        return _error_page(413, "Upload too large", "The maximum upload size is 16 MB.")

    # access.py raises 403 and 404 constantly (hidden project, wrong role, missing
    # record), so without these Flask serves its bare unbranded page and the styled
    # error template below is unreachable for the two most common failures.
    @app.errorhandler(403)
    def forbidden(_error):
        return _error_page(
            403,
            "Not permitted",
            "You do not have access to this record. Project visibility comes from a role on that project.",
        )

    @app.errorhandler(404)
    def not_found(_error):
        return _error_page(
            404,
            "Not found",
            "That record does not exist, or it belongs to a project you cannot see.",
        )

    @app.errorhandler(500)
    def server_error(_error):
        db.session.rollback()
        return _error_page(
            500,
            "Something went wrong",
            "The request could not be completed. Nothing was saved.",
        )

    # Flask-Login's default answer to an unauthenticated request is a redirect to
    # the login page. fetch() follows that redirect and reads the login form as the
    # response body, so an expired session looked like a JSON parse failure.
    @login_manager.unauthorized_handler
    def unauthorized():
        if _wants_json():
            return jsonify(error="Your session expired. Refresh the page and sign in again."), 401
        return redirect(url_for("auth.login", next=request.full_path))

    @app.errorhandler(CSRFError)
    def csrf_rejected(error):
        return _error_page(
            400,
            "Session expired",
            "Your session expired or this page is stale. Refresh the page and try again.",
        )

    # A malformed body or a bad URL reaches here without a more specific handler.
    @app.errorhandler(400)
    def bad_request(error):
        return _error_page(
            400,
            "Bad request",
            "The request could not be understood. Refresh the page and try again.",
        )

    @app.after_request
    def set_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.cli.command("create-admin")
    def create_admin():
        """Create the first administrator without storing a default password."""
        email = input("Admin email: ").strip().lower()
        if email.count("@") != 1 or any(char.isspace() for char in email) or db.session.scalar(db.select(Member).filter_by(email=email)):
            raise SystemExit("A valid, unused email address is required.")
        name = input("Admin name: ").strip()
        password = getpass.getpass("Password (minimum 12 characters): ")
        confirmation = getpass.getpass("Confirm password: ")
        if not name or len(password) < 12 or password != confirmation:
            raise SystemExit("Name and matching password of at least 12 characters are required.")
        admin = Member(name=name, email=email, is_admin=True)
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()
        print(f"Administrator {email} created.")

    @app.cli.command("run-scheduled-jobs")
    def run_scheduled_jobs():
        """Run every scheduled email job once. Intended for a cron entry or Task Scheduler."""
        from .jobs import run_all_scheduled_jobs

        results = run_all_scheduled_jobs()
        for job_name, count in results.items():
            print(f"{job_name}: {count} email(s) sent")
        print(f"Total: {sum(results.values())} email(s) sent")

    if app.config["AUTO_CREATE_DB"]:
        with app.app_context():
            db.create_all()
            if upgrade_schema():
                app.logger.info(
                    "Rebuilt project_membership so its role check constraint accepts the "
                    "Delegated PM and Delegated Backup roles."
                )

            # The global "Project Manager" flag no longer grants authority on
            # every project, so record the authority each flagged member already
            # had as an explicit per-project team role. Idempotent.
            from .feature_access import backfill_global_pm_roles

            if backfill_global_pm_roles():
                app.logger.info(
                    "Recorded per-project Project Manager roles for globally flagged PMs."
                )

            # Credentials that used to be editable in the settings table are
            # environment-only now. Drop any row still holding one, so a secret
            # typed into an older admin form does not sit in the database.
            from .settings import purge_retired_settings

            if purge_retired_settings():
                app.logger.info("Removed settings rows for credentials that are now read from .env.")

            # Members who already had Bulk Emailer access hold the legacy
            # boolean flag; seed equivalent grant rows so the feature-access
            # table is the single source of truth from here on. Idempotent.
            from .feature_access import backfill_legacy_access

            if backfill_legacy_access():
                app.logger.info("Seeded feature-access grants from the legacy can_bulk_email flag.")

    return app