import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()


def create_app():
    app = Flask(__name__)

    app.config["SECRET_KEY"] = os.environ.get("SESSION_SECRET", "dev-secret-key-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///voiceintel.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["WHISPER_MODEL"] = os.environ.get("WHISPER_MODEL", "base")
    app.config["STORAGE_DIR"] = os.environ.get("STORAGE_DIR", "storage")
    storage_dir = app.config["STORAGE_DIR"]
    os.makedirs(os.path.join(storage_dir, "voicemails"), exist_ok=True)
    os.makedirs(os.path.join(storage_dir, "processed"), exist_ok=True)

    db.init_app(app)
    migrate.init_app(app, db)

    # Flask-Login
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please sign in to access VoiceIntel."
    login_manager.login_message_category = "info"

    @login_manager.user_loader
    def load_user(user_id):
        from app.models.user import User
        return User.query.get(int(user_id))

    from app.routes.main import main_bp
    from app.routes.tasks import tasks_bp
    from app.routes.api import api_bp
    from app.routes.auth import auth_bp
    from app.routes.admin import admin_bp
    from app.routes.teams_admin import teams_admin_bp
    from app.routes.invites import invites_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(teams_admin_bp, url_prefix="/admin/teams")
    # Invites blueprint mounts both /admin/invites/* and the public /invite/<token>
    # at the app root, so no url_prefix.
    app.register_blueprint(invites_bp)

    # Expose open-callback count + pending-invite count for sidebar badges
    # on every authenticated page.
    @app.context_processor
    def _inject_sidebar_counts():
        from flask_login import current_user
        if not current_user.is_authenticated:
            return {}
        ctx = {}
        try:
            from app.models.voicemail import Callback
            ctx["open_task_count"] = Callback.query.filter(
                Callback.assignee_id == current_user.id,
                Callback.status.in_(("pending", "in_progress")),
            ).count()
        except Exception:
            ctx["open_task_count"] = 0
        # Pending-invite badge is only relevant to user managers.
        if getattr(current_user, "can_manage_users", False):
            try:
                from app.services.invite_service import pending_invite_count
                ctx["pending_invite_count"] = pending_invite_count()
            except Exception:
                ctx["pending_invite_count"] = 0
        return ctx

    with app.app_context():
        db.create_all()
        _ensure_voicemails_columns()
        _seed_categories()
        _seed_admin_user()

    _start_insights_scheduler(app)

    return app


def _start_insights_scheduler(app):
    """
    Run the AI analytics generator once an hour in the background. Single
    gunicorn worker (see Dockerfile) means exactly one scheduler instance.
    """
    import os
    import threading
    import logging
    from apscheduler.schedulers.background import BackgroundScheduler

    log = logging.getLogger(__name__)

    # Guard against the Werkzeug auto-reloader spawning the scheduler in BOTH
    # the parent and child processes. When the reloader is active, only the
    # child process has WERKZEUG_RUN_MAIN=true; we skip the parent. Also skip
    # if another process in this app has already started the scheduler.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "false":
        return
    if app.config.get("DEBUG") and not os.environ.get("WERKZEUG_RUN_MAIN"):
        log.info("Skipping scheduler in reloader parent process")
        return
    if getattr(app, "_insights_scheduler_started", False):
        return
    app._insights_scheduler_started = True

    def _job():
        with app.app_context():
            try:
                from app.services.insights_service import generate_and_store_insight
                generate_and_store_insight()
            except Exception as e:
                log.error(f"Hourly insights job crashed: {e}", exc_info=True)

    scheduler = BackgroundScheduler(daemon=True, timezone="UTC")
    scheduler.add_job(
        _job,
        trigger="interval",
        hours=1,
        id="hourly_ai_insights",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    scheduler.start()
    log.info("AI insights scheduler started — runs every 1 hour")

    # Kick off an initial run shortly after boot so the page has fresh data.
    # Delayed 60s so the model warmer container has time to load Phi-3 first.
    threading.Timer(60.0, _job).start()


def _ensure_voicemails_columns():
    """
    Idempotent boot guard — `db.create_all()` only creates missing tables;
    it doesn't add columns to existing ones. The Teams feature added three
    new columns to `voicemails` (recipient, team_id, team_locked). This
    function adds them on first boot after the upgrade and is a no-op
    afterwards. Safe on both Postgres and SQLite.
    """
    import logging as _logging
    from sqlalchemy import inspect, text
    log = _logging.getLogger(__name__)

    try:
        insp = inspect(db.engine)
        if "voicemails" not in insp.get_table_names():
            return  # First boot — db.create_all() already made the columns.
        cols = {c["name"] for c in insp.get_columns("voicemails")}
    except Exception as e:
        log.warning(f"Schema guard: could not inspect voicemails table: {e}")
        return

    statements = []
    if "recipient" not in cols:
        statements.append("ALTER TABLE voicemails ADD COLUMN recipient VARCHAR(512)")
    if "team_id" not in cols:
        statements.append("ALTER TABLE voicemails ADD COLUMN team_id INTEGER")
    if "team_locked" not in cols:
        statements.append(
            "ALTER TABLE voicemails ADD COLUMN team_locked BOOLEAN NOT NULL DEFAULT FALSE"
        )

    if not statements:
        return

    with db.engine.begin() as conn:
        for stmt in statements:
            try:
                conn.execute(text(stmt))
                log.info(f"Schema guard: applied '{stmt}'")
            except Exception as e:
                log.warning(f"Schema guard: '{stmt}' failed: {e}")


def _seed_categories():
    from app.models.voicemail import Category
    default_categories = [
        ("Prayer Request", "Calls related to prayer or spiritual support"),
        ("Donation Issue", "Issues or questions about donations"),
        ("Technical Issue", "Technical problems or support requests"),
        ("Complaint", "Complaints or dissatisfaction"),
        ("Product Inquiry", "Calls about offers, products, promotions, or sign-ups"),
        ("General Inquiry", "General questions or information requests"),
        ("Urgent", "Time-sensitive or emergency matters"),
    ]
    for name, desc in default_categories:
        if not Category.query.filter_by(name=name).first():
            cat = Category(name=name, description=desc)
            db.session.add(cat)
    db.session.commit()


def _seed_admin_user():
    from app.models.user import User
    if not User.query.filter_by(email="admin@voiceintel.local").first():
        admin = User(
            email="admin@voiceintel.local",
            name="Admin",
            role="admin",
            is_active=True,
        )
        admin.set_password("changeme123")
        db.session.add(admin)
        db.session.commit()
