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
    # Connection-pool hardening. Without these, idle Postgres connections
    # silently die after a few hours (Docker/firewall TCP idle timeouts) and
    # SQLAlchemy hands the dead socket to the next caller, who then blocks on
    # the OS read until kernel TCP keepalive fires (~2 hours). pool_pre_ping
    # validates the connection with a cheap SELECT 1 before reuse, and
    # pool_recycle proactively reopens connections older than 30 minutes.
    # Skip for SQLite — these options aren't applicable.
    if not app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "pool_pre_ping": True,
            "pool_recycle": 1800,
        }
    app.config["WHISPER_MODEL"] = os.environ.get("WHISPER_MODEL", "base")
    app.config["STORAGE_DIR"] = os.environ.get("STORAGE_DIR", "storage")
    storage_dir = app.config["STORAGE_DIR"]
    os.makedirs(os.path.join(storage_dir, "voicemails"), exist_ok=True)
    os.makedirs(os.path.join(storage_dir, "processed"), exist_ok=True)

    db.init_app(app)
    migrate.init_app(app, db)

    _register_jinja_filters(app)

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
            from app.models.voicemail import Callback, Voicemail
            from app.utils.team_scope import scope_voicemails
            # Join Voicemail so the sidebar badge always agrees with the
            # /tasks page: soft-deleted voicemails' callbacks must NOT
            # count, and non-admins are still team-scoped.
            badge_q = (
                db.session.query(Callback)
                .join(Voicemail, Voicemail.id == Callback.voicemail_id)
                .filter(
                    Callback.assignee_id == current_user.id,
                    Callback.status.in_(("pending", "in_progress")),
                )
            )
            badge_q = scope_voicemails(badge_q, current_user)
            ctx["open_task_count"] = badge_q.count()
        except Exception:
            ctx["open_task_count"] = 0
        # Pending-invite badge is only relevant to user managers.
        if getattr(current_user, "can_manage_users", False):
            try:
                from app.models.invite import UserInvite
                from datetime import datetime as _dt
                q = UserInvite.query.filter(
                    UserInvite.accepted_at.is_(None),
                    UserInvite.revoked_at.is_(None),
                    UserInvite.expires_at > _dt.utcnow(),
                )
                if not getattr(current_user, "is_admin", False):
                    # Supervisors only see badges for invites they sent.
                    q = q.filter(UserInvite.invited_by_id == current_user.id)
                ctx["pending_invite_count"] = q.count()
            except Exception:
                ctx["pending_invite_count"] = 0
        return ctx

    with app.app_context():
        db.create_all()
        _ensure_voicemails_columns()
        _ensure_insights_columns()
        _seed_categories()
        _seed_admin_user()

    _start_insights_scheduler(app)

    return app


def _register_jinja_filters(app):
    """
    Display-only timezone conversion. The database always stores UTC
    (`datetime.utcnow()` everywhere); these filters convert to the operator's
    local zone at render time. Set `DISPLAY_TZ` env var (default
    `America/Chicago`) to change the displayed zone — no schema change needed.

    Filters:
      {{ dt | localtime }}                       → "Apr 29, 2026 11:20 PM"
      {{ dt | localtime('%b %d, %Y %H:%M %Z') }} → "Apr 29, 2026 23:20 CDT"
      {{ dt | tz_abbr }}                         → "CDT" / "CST" for that moment
    """
    import logging as _logging
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    log = _logging.getLogger(__name__)

    tz_name = os.environ.get("DISPLAY_TZ", "America/Chicago")
    try:
        zone = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        log.warning(f"DISPLAY_TZ={tz_name!r} not found in tz database; falling back to UTC")
        zone = ZoneInfo("UTC")
        tz_name = "UTC"
    log.info(f"Display timezone: {tz_name}")

    _UTC = ZoneInfo("UTC")

    def _to_local(dt):
        """Coerce a datetime (assumed UTC if naive) into the display zone."""
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_UTC)
        return dt.astimezone(zone)

    def localtime_filter(dt, fmt="%b %d, %Y %I:%M %p"):
        local = _to_local(dt)
        if local is None:
            return ""
        return local.strftime(fmt)

    def tz_abbr_filter(dt=None):
        if dt is None:
            local = datetime.now(zone)
        else:
            local = _to_local(dt)
            if local is None:
                return ""
        return local.strftime("%Z")

    app.jinja_env.filters["localtime"] = localtime_filter
    app.jinja_env.filters["tz_abbr"]   = tz_abbr_filter
    # Expose the configured zone name as a global so templates / JS data attrs
    # can reference it (e.g. for the analytics "X mins ago" widget).
    app.jinja_env.globals["DISPLAY_TZ_NAME"] = tz_name


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
    # Marked daemon so a worker shutdown during the 60s window doesn't block.
    boot_kick = threading.Timer(60.0, _job)
    boot_kick.daemon = True
    boot_kick.start()


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
    # Soft-delete columns for the admin Deleted folder.
    if "deleted_at" not in cols:
        statements.append("ALTER TABLE voicemails ADD COLUMN deleted_at TIMESTAMP")
        statements.append(
            "CREATE INDEX IF NOT EXISTS ix_voicemails_deleted_at ON voicemails (deleted_at)"
        )
    if "deleted_by_id" not in cols:
        statements.append("ALTER TABLE voicemails ADD COLUMN deleted_by_id INTEGER")

    if not statements:
        return

    with db.engine.begin() as conn:
        for stmt in statements:
            try:
                conn.execute(text(stmt))
                log.info(f"Schema guard: applied '{stmt}'")
            except Exception as e:
                log.warning(f"Schema guard: '{stmt}' failed: {e}")


def _ensure_insights_columns():
    """
    Idempotent boot guard for the per-voicemail AI summary feature.
    Adds the `ai_*` columns to the existing `insights` table if they are
    missing. No-op once the columns exist. Safe on Postgres and SQLite.
    """
    import logging as _logging
    from sqlalchemy import inspect, text
    log = _logging.getLogger(__name__)

    try:
        insp = inspect(db.engine)
        if "insights" not in insp.get_table_names():
            return  # First boot — db.create_all() already made the columns.
        cols = {c["name"] for c in insp.get_columns("insights")}
    except Exception as e:
        log.warning(f"Schema guard: could not inspect insights table: {e}")
        return

    statements = []
    if "ai_summary" not in cols:
        statements.append("ALTER TABLE insights ADD COLUMN ai_summary TEXT")
    if "ai_intent" not in cols:
        statements.append("ALTER TABLE insights ADD COLUMN ai_intent TEXT")
    else:
        # Existing deployments created ai_intent as VARCHAR(300); widen it to
        # TEXT so longer Phi-3 outputs don't crash the pipeline. Postgres
        # accepts ALTER COLUMN TYPE; SQLite ignores VARCHAR length anyway so
        # we just skip the failure on that backend.
        statements.append("ALTER TABLE insights ALTER COLUMN ai_intent TYPE TEXT")
    if "ai_action_items" not in cols:
        # JSON on Postgres, TEXT-with-JSON on SQLite — both accept this DDL.
        statements.append("ALTER TABLE insights ADD COLUMN ai_action_items JSON")
    if "ai_suggested_response" not in cols:
        statements.append("ALTER TABLE insights ADD COLUMN ai_suggested_response TEXT")
    if "ai_caller_name" not in cols:
        # Caller's spoken name extracted by Phi-3 from the transcript when
        # the carrier-supplied caller-ID is generic ("Wireless Caller", a
        # city/state, "Anonymous", etc.). 120 chars comfortably covers
        # full names plus business names.
        statements.append("ALTER TABLE insights ADD COLUMN ai_caller_name VARCHAR(120)")
    if "ai_status" not in cols:
        statements.append("ALTER TABLE insights ADD COLUMN ai_status VARCHAR(20)")
    if "ai_error" not in cols:
        statements.append("ALTER TABLE insights ADD COLUMN ai_error TEXT")
    if "ai_duration_ms" not in cols:
        statements.append("ALTER TABLE insights ADD COLUMN ai_duration_ms INTEGER")
    if "ai_generated_at" not in cols:
        statements.append("ALTER TABLE insights ADD COLUMN ai_generated_at TIMESTAMP")

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
