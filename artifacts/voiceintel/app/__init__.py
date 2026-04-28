import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from apscheduler.schedulers.background import BackgroundScheduler

db = SQLAlchemy()
migrate = Migrate()
scheduler = BackgroundScheduler()


def create_app():
    app = Flask(__name__)

    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///voiceintel.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["WHISPER_MODEL"] = os.environ.get("WHISPER_MODEL", "base")
    app.config["STORAGE_DIR"] = os.environ.get("STORAGE_DIR", "storage")
    app.config["POLL_INTERVAL"] = int(os.environ.get("POLL_INTERVAL", "60"))

    storage_dir = app.config["STORAGE_DIR"]
    os.makedirs(os.path.join(storage_dir, "voicemails"), exist_ok=True)
    os.makedirs(os.path.join(storage_dir, "processed"), exist_ok=True)

    db.init_app(app)
    migrate.init_app(app, db)

    from app.routes.main import main_bp
    from app.routes.api import api_bp
    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp, url_prefix="/api")

    with app.app_context():
        db.create_all()
        _seed_categories()

    return app


def _seed_categories():
    from app.models.voicemail import Category
    default_categories = [
        ("Prayer Request", "Calls related to prayer or spiritual support"),
        ("Donation Issue", "Issues or questions about donations"),
        ("Technical Issue", "Technical problems or support requests"),
        ("Complaint", "Complaints or dissatisfaction"),
        ("General Inquiry", "General questions or information requests"),
        ("Urgent", "Time-sensitive or emergency matters"),
    ]
    for name, desc in default_categories:
        if not Category.query.filter_by(name=name).first():
            cat = Category(name=name, description=desc)
            db.session.add(cat)
    db.session.commit()
