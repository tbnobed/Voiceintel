from datetime import datetime
from app import db


class Category(db.Model):
    __tablename__ = "categories"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    voicemails = db.relationship("Voicemail", back_populates="category_obj")

    def to_dict(self):
        return {"id": self.id, "name": self.name, "description": self.description}


class Voicemail(db.Model):
    __tablename__ = "voicemails"

    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.String(512), nullable=False)
    filename = db.Column(db.String(512), nullable=False)
    sender = db.Column(db.String(512))
    subject = db.Column(db.String(1024))
    received_at = db.Column(db.DateTime)
    original_path = db.Column(db.String(1024))
    converted_path = db.Column(db.String(1024))
    duration = db.Column(db.Float)
    file_size = db.Column(db.Integer)
    processing_status = db.Column(db.String(50), default="pending")
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"))
    is_urgent = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    category_obj = db.relationship("Category", back_populates="voicemails")
    transcript = db.relationship("Transcript", back_populates="voicemail", uselist=False, cascade="all, delete-orphan")
    insights = db.relationship("Insight", back_populates="voicemail", uselist=False, cascade="all, delete-orphan")

    __table_args__ = (db.UniqueConstraint("message_id", "filename", name="uq_message_filename"),)

    def to_dict(self):
        return {
            "id": self.id,
            "message_id": self.message_id,
            "filename": self.filename,
            "sender": self.sender,
            "subject": self.subject,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "duration": self.duration,
            "file_size": self.file_size,
            "processing_status": self.processing_status,
            "category": self.category_obj.name if self.category_obj else None,
            "is_urgent": self.is_urgent,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "transcript_preview": (
                self.transcript.text[:200] + "..." if self.transcript and self.transcript.text and len(self.transcript.text) > 200
                else (self.transcript.text if self.transcript else None)
            ),
        }


class Transcript(db.Model):
    __tablename__ = "transcripts"

    id = db.Column(db.Integer, primary_key=True)
    voicemail_id = db.Column(db.Integer, db.ForeignKey("voicemails.id"), nullable=False, unique=True)
    text = db.Column(db.Text)
    language = db.Column(db.String(10))
    segments = db.Column(db.JSON)
    processing_time = db.Column(db.Float)
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    voicemail = db.relationship("Voicemail", back_populates="transcript")

    def to_dict(self):
        return {
            "id": self.id,
            "voicemail_id": self.voicemail_id,
            "text": self.text,
            "language": self.language,
            "segments": self.segments,
            "processing_time": self.processing_time,
            "error": self.error,
        }


class Insight(db.Model):
    __tablename__ = "insights"

    id = db.Column(db.Integer, primary_key=True)
    voicemail_id = db.Column(db.Integer, db.ForeignKey("voicemails.id"), nullable=False, unique=True)
    keywords = db.Column(db.JSON)
    sentiment = db.Column(db.String(20))
    sentiment_score = db.Column(db.Float)
    urgency_keywords = db.Column(db.JSON)
    category = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    voicemail = db.relationship("Voicemail", back_populates="insights")

    def to_dict(self):
        return {
            "id": self.id,
            "voicemail_id": self.voicemail_id,
            "keywords": self.keywords,
            "sentiment": self.sentiment,
            "sentiment_score": self.sentiment_score,
            "urgency_keywords": self.urgency_keywords,
            "category": self.category,
        }


class Setting(db.Model):
    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(200), unique=True, nullable=False)
    value = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @classmethod
    def get(cls, key, default=None):
        s = cls.query.filter_by(key=key).first()
        return s.value if s else default

    @classmethod
    def set(cls, key, value):
        s = cls.query.filter_by(key=key).first()
        if s:
            s.value = str(value)
        else:
            s = cls(key=key, value=str(value))
            db.session.add(s)
        db.session.commit()
