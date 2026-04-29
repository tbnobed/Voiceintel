"""
Teams + automatic voicemail routing.

A Team groups agents who handle a particular line of incoming voicemails
(e.g. "Sales", "Support", "Spanish"). Agents can belong to one or many teams.

A RoutingRule auto-assigns an incoming voicemail to a team based on:
  - recipient_email   : exact match on the SendGrid Inbound Parse "to" field
                        (e.g. team1@mail3.opscal.io)
  - recipient_domain  : domain part of the recipient address
  - sender_email      : exact match on the From: address
  - sender_domain     : domain part of the From: address
  - keyword           : case-insensitive substring search of the transcript
  - caller_phone      : digits-only substring of the caller's phone number

Rules are evaluated lowest-priority-number first. The first match wins —
each voicemail belongs to at most one team.
"""
from datetime import datetime
from app import db


# Cap on rule kinds and a friendly label map for the admin UI.
RULE_KINDS = (
    "recipient_email",
    "recipient_domain",
    "sender_email",
    "sender_domain",
    "keyword",
    "caller_phone",
)

RULE_KIND_LABELS = {
    "recipient_email":  "Recipient address (exact)",
    "recipient_domain": "Recipient domain",
    "sender_email":     "Sender address (exact)",
    "sender_domain":    "Sender domain",
    "keyword":          "Transcript keyword",
    "caller_phone":     "Caller phone (digits)",
}


# Junction table — agents <-> teams (many-to-many).
team_members = db.Table(
    "team_members",
    db.Column("team_id", db.Integer,
              db.ForeignKey("teams.id", ondelete="CASCADE"),
              primary_key=True),
    db.Column("user_id", db.Integer,
              db.ForeignKey("users.id", ondelete="CASCADE"),
              primary_key=True),
    db.Column("created_at", db.DateTime, default=datetime.utcnow),
)


class Team(db.Model):
    __tablename__ = "teams"

    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(120), unique=True, nullable=False)
    slug        = db.Column(db.String(120), unique=True, nullable=False)
    description = db.Column(db.Text)
    # Hex color (with leading #) used for the pill in lists. Defaults to gold.
    color       = db.Column(db.String(9), default="#F7CE5B", nullable=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    members = db.relationship(
        "User",
        secondary=team_members,
        back_populates="teams",
        lazy="selectin",
    )
    rules = db.relationship(
        "RoutingRule",
        back_populates="team",
        cascade="all, delete-orphan",
        order_by="RoutingRule.priority, RoutingRule.id",
    )
    voicemails = db.relationship(
        "Voicemail",
        back_populates="team",
        passive_deletes=True,
    )

    def to_dict(self):
        return {
            "id":          self.id,
            "name":        self.name,
            "slug":        self.slug,
            "description": self.description,
            "color":       self.color,
            "members":     len(self.members),
        }


class RoutingRule(db.Model):
    __tablename__ = "routing_rules"

    id         = db.Column(db.Integer, primary_key=True)
    team_id    = db.Column(db.Integer,
                           db.ForeignKey("teams.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    kind       = db.Column(db.String(40), nullable=False)   # see RULE_KINDS
    pattern    = db.Column(db.String(500), nullable=False)  # value to match
    priority   = db.Column(db.Integer, nullable=False, default=100, index=True)
    is_active  = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    team = db.relationship("Team", back_populates="rules")

    @property
    def kind_label(self) -> str:
        return RULE_KIND_LABELS.get(self.kind, self.kind)
