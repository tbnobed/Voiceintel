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
    from app.routes.api import api_bp
    from app.routes.auth import auth_bp
    from app.routes.admin import admin_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")

    with app.app_context():
        db.create_all()
        _seed_categories()
        _seed_admin_user()

    return app


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
