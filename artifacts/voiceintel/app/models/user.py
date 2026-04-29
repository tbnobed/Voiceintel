from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from app import db


# Roles — listed in order from highest to lowest privilege.
# - admin      : full access including user management and granting admin
# - supervisor : manage non-admin users, assign callback tasks
# - agent      : be assigned callback tasks, work them, add notes
# - viewer     : read-only access; can still add notes
ROLES = ("admin", "supervisor", "agent", "viewer")
ROLE_LABELS = {
    "admin":      "Admin",
    "supervisor": "Supervisor",
    "agent":      "Agent",
    "viewer":     "Viewer",
}


class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    name = db.Column(db.String(200), nullable=False)
    password_hash = db.Column(db.String(512), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="viewer")  # see ROLES
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)

    # Many-to-many — users can belong to one or many teams.
    teams = db.relationship(
        "Team",
        secondary="team_members",
        back_populates="members",
        lazy="selectin",
    )

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    # ── Role helpers ────────────────────────────────────────────────────────
    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_supervisor(self) -> bool:
        return self.role == "supervisor"

    @property
    def is_agent(self) -> bool:
        return self.role == "agent"

    @property
    def can_manage_users(self) -> bool:
        """Admins fully; supervisors for non-admin users (enforced at route level)."""
        return self.role in ("admin", "supervisor")

    @property
    def can_assign_callbacks(self) -> bool:
        return self.role in ("admin", "supervisor")

    @property
    def can_be_assigned_callback(self) -> bool:
        """Anyone but viewers can be assigned callback tasks."""
        return self.role in ("admin", "supervisor", "agent")

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role.title())

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "role": self.role,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login": self.last_login.isoformat() if self.last_login else None,
        }
