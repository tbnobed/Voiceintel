from datetime import datetime, timedelta
from app import db


# Invitation status is derived from timestamps rather than stored as a column —
# this keeps the source of truth in (accepted_at, revoked_at, expires_at) and
# avoids drift between status and reality.
INVITE_STATUSES = ("pending", "accepted", "revoked", "expired")
INVITE_STATUS_LABELS = {
    "pending":  "Pending",
    "accepted": "Accepted",
    "revoked":  "Revoked",
    "expired":  "Expired",
}

DEFAULT_INVITE_TTL_DAYS = 7


class UserInvite(db.Model):
    """
    Email invitation to create a VoiceIntel account.

    The invite carries the target email, name, role, and optional team
    assignments. When the recipient clicks the link they choose a password and
    (optionally) confirm/edit the suggested name — at that point a User row is
    created and `accepted_at` + `accepted_user_id` are populated.

    Old pending invites for the same email are auto-revoked when a new one is
    issued (see invite_service.create_invite).
    """
    __tablename__ = "user_invites"

    id = db.Column(db.Integer, primary_key=True)

    email = db.Column(db.String(255), nullable=False, index=True)
    name  = db.Column(db.String(200), nullable=False)
    role  = db.Column(db.String(20),  nullable=False, default="viewer")

    # URL-safe token (secrets.token_urlsafe(32) = 43 chars). Looked up by exact
    # match — uniqueness enforced at the column level so two invites can never
    # share a token.
    token = db.Column(db.String(64), nullable=False, unique=True, index=True)

    # Optional team assignments applied to the user when the invite is
    # accepted. Stored as a comma-separated list of integer team IDs to avoid
    # adding a junction table for what's effectively transient setup data.
    team_ids_csv = db.Column(db.String(500), nullable=False, default="")

    # Nullable + SET NULL so deleting the inviter (e.g. a former supervisor)
    # doesn't fail because of historical invites. The "Sent By" column
    # falls back to "—" in the UI when this is NULL.
    invited_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    invited_by    = db.relationship("User", foreign_keys=[invited_by_id])

    created_at  = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at  = db.Column(db.DateTime, nullable=False)

    # Set when accepted; the new User row is linked via accepted_user_id.
    accepted_at      = db.Column(db.DateTime, nullable=True)
    accepted_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    accepted_user    = db.relationship("User", foreign_keys=[accepted_user_id])

    # Set when an admin/supervisor manually invalidates the invite.
    revoked_at = db.Column(db.DateTime, nullable=True)

    # Tracks resends — useful for UI ("sent 3 times") and rate-limiting.
    last_sent_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    send_count   = db.Column(db.Integer,  default=1, nullable=False)

    # ── Derived state ────────────────────────────────────────────────────────
    @property
    def status(self) -> str:
        if self.accepted_at:
            return "accepted"
        if self.revoked_at:
            return "revoked"
        if self.expires_at and self.expires_at < datetime.utcnow():
            return "expired"
        return "pending"

    @property
    def status_label(self) -> str:
        return INVITE_STATUS_LABELS.get(self.status, self.status.title())

    @property
    def is_pending(self) -> bool:
        return self.status == "pending"

    @property
    def is_actionable(self) -> bool:
        """True if the invite can still be revoked or resent (not accepted)."""
        return self.accepted_at is None

    # ── Team helpers ────────────────────────────────────────────────────────
    @property
    def team_ids(self) -> list:
        if not self.team_ids_csv:
            return []
        return [int(x) for x in self.team_ids_csv.split(",") if x.strip().isdigit()]

    @team_ids.setter
    def team_ids(self, ids):
        ids = ids or []
        self.team_ids_csv = ",".join(str(int(i)) for i in ids if i)

    @classmethod
    def default_expiry(cls, days: int = DEFAULT_INVITE_TTL_DAYS) -> datetime:
        return datetime.utcnow() + timedelta(days=days)
