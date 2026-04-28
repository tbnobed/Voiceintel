from datetime import datetime
from app import db


class AutomationTrigger(db.Model):
    __tablename__ = "automation_triggers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    # Condition
    # condition_type: "category" | "keyword" | "sentiment" | "is_urgent" | "always"
    condition_type = db.Column(db.String(50), nullable=False)
    # condition_value: category name, comma-separated keywords, sentiment value, or empty
    condition_value = db.Column(db.String(500), default="")

    # Action
    # action_type: "mark_urgent" | "send_email" | "add_label" | "notify_admin"
    action_type = db.Column(db.String(50), nullable=False)
    # action_value: email address for send_email, label text for add_label, else empty
    action_value = db.Column(db.Text, default="")

    trigger_count = db.Column(db.Integer, default=0)
    last_triggered = db.Column(db.DateTime)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    created_by = db.relationship("User", foreign_keys=[created_by_id])

    CONDITION_LABELS = {
        "always":    "Always (every voicemail)",
        "category":  "Category equals",
        "keyword":   "Transcript contains keyword",
        "sentiment": "Sentiment is",
        "is_urgent": "Flagged as urgent",
    }

    ACTION_LABELS = {
        "mark_urgent":    "Mark as Urgent",
        "add_label":      "Prepend Label to Subject",
        "notify_admin":   "Email Admin Notification",
        "send_email":     "Send Email to Address",
    }

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "is_active": self.is_active,
            "condition_type": self.condition_type,
            "condition_value": self.condition_value,
            "condition_label": self.CONDITION_LABELS.get(self.condition_type, self.condition_type),
            "action_type": self.action_type,
            "action_value": self.action_value,
            "action_label": self.ACTION_LABELS.get(self.action_type, self.action_type),
            "trigger_count": self.trigger_count,
            "last_triggered": self.last_triggered.isoformat() if self.last_triggered else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
