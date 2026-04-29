import re
from datetime import datetime
from app import db


def parse_voicemail_subject(subject: str) -> dict:
    """
    Extract caller name, phone, call date, and call time from a voicemail
    notification subject line.

    Handles formats such as:
      "New Voice Message from FAULKNER R. (262) 968-2401 on 04/24/2026 11:25 AM"
      "Fw: New Voice Message from FOURROUX EILEEN (225) 907-3484 on 04/26/2026 15:39"

    Returns a dict with keys: caller_name, phone, call_date, call_time.
    Any unrecognised field is None.
    """
    result = {"caller_name": None, "phone": None, "call_date": None, "call_time": None}
    if not subject:
        return result

    # Strip common prefixes (Fw:, Re:, Fwd:)
    cleaned = re.sub(r"^(Fw:|Re:|Fwd:)\s*", "", subject.strip(), flags=re.IGNORECASE)

    # Match "New Voice Message from <name> <phone> on <date> <time>"
    pattern = re.compile(
        r"new\s+voice\s+message\s+from\s+"
        r"(?P<name>.+?)\s+"
        r"(?P<phone>\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4})"
        r"(?:\s+on\s+"
        r"(?P<date>\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})"
        r"(?:\s+(?P<time>\d{1,2}:\d{2}(?:\s*[APap][Mm])?))?"
        r")?",
        re.IGNORECASE,
    )
    m = pattern.search(cleaned)
    if not m:
        return result

    raw_name = m.group("name").strip()
    # Collapse multiple spaces, then title-case
    result["caller_name"] = re.sub(r"\s+", " ", raw_name).title()

    # Normalise phone to (XXX) XXX-XXXX
    digits = re.sub(r"\D", "", m.group("phone"))
    if len(digits) == 10:
        result["phone"] = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    elif len(digits) == 11 and digits[0] == "1":
        result["phone"] = f"({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    else:
        result["phone"] = m.group("phone")

    if m.group("date"):
        result["call_date"] = m.group("date")
    if m.group("time"):
        result["call_time"] = m.group("time").strip().upper()

    return result


class AnalyticsInsight(db.Model):
    """
    Cached output of the hourly background AI-insights job. We always read the
    most recent row (`order_by(generated_at.desc()).first()`); old rows are
    kept for history/debugging but trimmed periodically.
    """
    __tablename__ = "analytics_insights"

    id            = db.Column(db.Integer, primary_key=True)
    text          = db.Column(db.Text)              # markdown body, may be NULL on error
    status        = db.Column(db.String(20), default="pending", nullable=False)  # pending|success|error
    error_message = db.Column(db.Text)
    duration_ms   = db.Column(db.Integer)           # how long generation took
    generated_at  = db.Column(db.DateTime, default=datetime.utcnow, index=True, nullable=False)


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
    callbacks = db.relationship(
        "Callback",
        back_populates="voicemail",
        cascade="all, delete-orphan",
        order_by="Callback.created_at.desc()",
    )
    notes = db.relationship(
        "VoicemailNote",
        back_populates="voicemail",
        cascade="all, delete-orphan",
        order_by="VoicemailNote.created_at.desc()",
    )

    __table_args__ = (db.UniqueConstraint("message_id", "filename", name="uq_message_filename"),)

    @property
    def caller_info(self) -> dict:
        """Parsed caller name, phone, call date/time from the subject line."""
        return parse_voicemail_subject(self.subject)

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


# ---------------------------------------------------------------------------
# Follow-up callback tasks (assignable to agents)
# ---------------------------------------------------------------------------

CALLBACK_STATUSES = ("pending", "in_progress", "completed", "cancelled")
CALLBACK_PRIORITIES = ("normal", "urgent")


class Callback(db.Model):
    """A follow-up phone call task assigned to a user (agent/supervisor/admin)
    in response to a voicemail."""
    __tablename__ = "callbacks"

    id           = db.Column(db.Integer, primary_key=True)
    voicemail_id = db.Column(db.Integer, db.ForeignKey("voicemails.id", ondelete="CASCADE"), nullable=False, index=True)
    assignee_id  = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True)
    assigner_id  = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    status       = db.Column(db.String(20), nullable=False, default="pending", index=True)
    priority     = db.Column(db.String(20), nullable=False, default="normal")
    notes        = db.Column(db.Text)               # supervisor's instructions
    due_at       = db.Column(db.DateTime)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    completed_at = db.Column(db.DateTime)

    voicemail = db.relationship("Voicemail", back_populates="callbacks")
    assignee  = db.relationship("User", foreign_keys=[assignee_id])
    assigner  = db.relationship("User", foreign_keys=[assigner_id])

    @property
    def is_open(self) -> bool:
        return self.status in ("pending", "in_progress")

    @property
    def status_label(self) -> str:
        return {
            "pending":     "Pending",
            "in_progress": "In Progress",
            "completed":   "Completed",
            "cancelled":   "Cancelled",
        }.get(self.status, self.status.title())


class VoicemailNote(db.Model):
    """Free-form note attached to a voicemail. Anyone signed-in can post one;
    used for follow-up notes, call dispositions, internal comments."""
    __tablename__ = "voicemail_notes"

    id           = db.Column(db.Integer, primary_key=True)
    voicemail_id = db.Column(db.Integer, db.ForeignKey("voicemails.id", ondelete="CASCADE"), nullable=False, index=True)
    author_id    = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True)
    body         = db.Column(db.Text, nullable=False)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    voicemail = db.relationship("Voicemail", back_populates="notes")
    author    = db.relationship("User", foreign_keys=[author_id])

    @property
    def author_name(self) -> str:
        return self.author.name if self.author else "(deleted user)"
