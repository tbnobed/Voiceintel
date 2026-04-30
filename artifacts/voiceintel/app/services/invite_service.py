"""
User-invitation service.

Responsible for:
  - generating cryptographically-random invite tokens
  - building the absolute accept URL (respecting reverse-proxy headers)
  - sending the invite email via SendGrid (no-op if SendGrid is not configured)
  - accepting an invite — creates the User row and marks the invite consumed
  - resending / revoking invites

Routes are kept thin and delegate all business logic here so the same flows
can be exercised from tests or admin actions without duplication.
"""

import os
import logging
import secrets
from datetime import datetime
from typing import Optional, Tuple

from flask import request

from app import db
from app.models.invite import UserInvite, DEFAULT_INVITE_TTL_DAYS
from app.models.user import User, ROLES
from app.services.email_service import send_notification_email

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _base_url() -> str:
    """
    Resolve the public base URL for invite links. Precedence:
      1. APP_BASE_URL env var (preferred for self-hosted; e.g. https://voice-ai.obtv.io)
      2. REPLIT_DOMAINS (Replit-managed deployments)
      3. Current request scheme + Host (last-resort fallback; works behind a
         reverse proxy that sets X-Forwarded-Proto)
    """
    explicit = os.environ.get("APP_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")

    domains = os.environ.get("REPLIT_DOMAINS", "").strip()
    if domains:
        host = domains.split(",")[0].strip()
        return f"https://{host}"

    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    return f"{proto}://{request.host}"


def build_invite_url(token: str) -> str:
    return f"{_base_url()}/invite/{token}"


# ---------------------------------------------------------------------------
# Token generation
# ---------------------------------------------------------------------------

def _generate_token() -> str:
    """43-char URL-safe token (256 bits of entropy from token_urlsafe(32))."""
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# Create / resend / revoke
# ---------------------------------------------------------------------------

def create_invite(
    email: str,
    name: str,
    role: str,
    team_ids: Optional[list],
    invited_by: User,
    ttl_days: int = DEFAULT_INVITE_TTL_DAYS,
) -> UserInvite:
    """
    Create and persist an invite, auto-revoking any existing pending invite for
    the same email. Caller is responsible for sending the email afterwards
    (typically via send_invite_email).
    """
    email = email.strip().lower()
    name = name.strip()
    role = role if role in ROLES else "viewer"

    # Auto-revoke prior pending/expired invites for this email so the listing
    # never shows multiple "live" invites for the same person.
    prior = (
        UserInvite.query
        .filter_by(email=email, accepted_at=None, revoked_at=None)
        .all()
    )
    for inv in prior:
        inv.revoked_at = datetime.utcnow()

    invite = UserInvite(
        email=email,
        name=name,
        role=role,
        token=_generate_token(),
        invited_by_id=invited_by.id,
        expires_at=UserInvite.default_expiry(ttl_days),
        last_sent_at=datetime.utcnow(),
        send_count=1,
    )
    invite.team_ids = team_ids or []
    db.session.add(invite)
    db.session.commit()
    return invite


def resend_invite(invite: UserInvite, ttl_days: int = DEFAULT_INVITE_TTL_DAYS) -> bool:
    """
    Resend the invite email. If the invite has been revoked or expired,
    generate a fresh token, clear the revoked flag, and extend the expiry —
    "Resend" always produces a working link. Otherwise reuse the existing
    token so any link the user already has in their inbox keeps working.
    """
    if invite.accepted_at:
        return False
    now = datetime.utcnow()
    needs_reset = invite.revoked_at is not None or invite.expires_at < now
    if needs_reset:
        invite.token = _generate_token()
        invite.expires_at = UserInvite.default_expiry(ttl_days)
        invite.revoked_at = None
    invite.last_sent_at = now
    invite.send_count = (invite.send_count or 0) + 1
    db.session.commit()
    return send_invite_email(invite)


def revoke_invite(invite: UserInvite) -> None:
    if invite.accepted_at or invite.revoked_at:
        return
    invite.revoked_at = datetime.utcnow()
    db.session.commit()


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_invite_email(invite: UserInvite) -> bool:
    """
    Send the invitation email via SendGrid. Returns False if SendGrid isn't
    configured (so the route can still surface a useful flash message).
    """
    url = build_invite_url(invite.token)
    inviter_name = invite.invited_by.name if invite.invited_by else "An administrator"
    expires_str = invite.expires_at.strftime("%B %d, %Y")

    subject = f"{inviter_name} invited you to VoiceIntel"

    text_body = (
        f"Hi {invite.name},\n\n"
        f"{inviter_name} has invited you to VoiceIntel — the voicemail "
        f"intelligence dashboard.\n\n"
        f"To set up your account, click the link below:\n\n"
        f"{url}\n\n"
        f"This invitation expires on {expires_str}.\n\n"
        f"If you weren't expecting this email you can safely ignore it."
    )

    html_body = f"""\
<!doctype html>
<html><body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0d0b00; color: #DFD6A7; padding: 40px 20px; margin: 0;">
  <table align="center" width="100%" style="max-width: 520px; background: #1a1400; border: 1px solid rgba(175,155,70,0.16); border-radius: 10px; padding: 32px;">
    <tr><td>
      <h2 style="font-family: Georgia, serif; font-weight: 400; color: #F7CE5B; margin: 0 0 16px 0;">You're invited to VoiceIntel</h2>
      <p style="line-height: 1.55; margin: 0 0 14px 0;">Hi {invite.name},</p>
      <p style="line-height: 1.55; margin: 0 0 14px 0;"><strong style="color: #F7CE5B;">{inviter_name}</strong> has invited you to join VoiceIntel — the voicemail intelligence dashboard.</p>
      <p style="line-height: 1.55; margin: 0 0 24px 0;">Click the button below to set up your account.</p>
      <p style="text-align: center; margin: 0 0 24px 0;">
        <a href="{url}" style="display: inline-block; background: #F7CE5B; color: #000; text-decoration: none; font-weight: 600; padding: 12px 28px; border-radius: 6px;">Set Up Account</a>
      </p>
      <p style="line-height: 1.55; font-size: 12px; color: rgba(175,155,70,0.7); margin: 0 0 6px 0;">Or copy this link into your browser:</p>
      <p style="line-height: 1.55; font-size: 12px; word-break: break-all; margin: 0 0 24px 0;"><a href="{url}" style="color: #AF9B46;">{url}</a></p>
      <p style="line-height: 1.55; font-size: 12px; color: rgba(175,155,70,0.7); margin: 0;">This invitation expires on <strong>{expires_str}</strong>. If you weren't expecting this email, you can safely ignore it.</p>
    </td></tr>
  </table>
</body></html>"""

    # Warn if the public base URL was derived from request headers — a
    # mis-configured proxy could let a Host-header attack poison the link.
    # In production, APP_BASE_URL should always be set explicitly.
    if not os.environ.get("APP_BASE_URL", "").strip() and not os.environ.get("REPLIT_DOMAINS", "").strip():
        logger.warning(
            "Invite link for %s built from request Host header — set "
            "APP_BASE_URL env var to pin the public URL.", invite.email,
        )

    ok = send_notification_email(invite.email, subject, text_body, html_body)
    if not ok:
        # Don't log the URL itself — it's a bearer token equivalent to a
        # password-reset link. The route layer is responsible for surfacing
        # the link to the admin who created the invite if the email failed.
        logger.warning(
            "Invite email to %s could not be sent (SendGrid not configured "
            "or send failed).", invite.email,
        )
    return ok


# ---------------------------------------------------------------------------
# Lookup + acceptance
# ---------------------------------------------------------------------------

def find_invite_by_token(token: str) -> Optional[UserInvite]:
    if not token or len(token) > 64:
        return None
    return UserInvite.query.filter_by(token=token).first()


def accept_invite(
    invite: UserInvite, name: str, password: str
) -> Tuple[bool, str, Optional[User]]:
    """
    Apply the invite: create the User row, attach team memberships, and stamp
    the invite as accepted. Returns (success, message, user_or_none).

    Race-safety: the invite row is locked with SELECT ... FOR UPDATE so two
    concurrent submissions can't both pass the status check. The User unique
    constraint on email is the second line of defense — IntegrityError is
    caught and surfaced as a clean message rather than a 500.
    """
    from sqlalchemy.exc import IntegrityError

    if not name.strip():
        return False, "Name is required.", None
    if not password or len(password) < 8:
        return False, "Password must be at least 8 characters.", None

    # Re-fetch the invite with a row-level lock so a parallel acceptance
    # blocks until this transaction commits or rolls back. SQLite ignores
    # FOR UPDATE silently, which is fine for dev.
    locked = (
        UserInvite.query
        .filter_by(id=invite.id)
        .with_for_update()
        .first()
    )
    if locked is None:
        return False, "Invitation not found.", None
    if locked.status != "pending":
        return False, f"This invitation has been {locked.status}.", None

    # Pre-check (avoids IntegrityError in the common case)
    if User.query.filter_by(email=locked.email).first():
        return False, "An account with this email already exists. Please sign in.", None

    user = User(
        email=locked.email,
        name=name.strip(),
        role=locked.role,
        is_active=True,
    )
    user.set_password(password)
    db.session.add(user)

    try:
        db.session.flush()  # surfaces IntegrityError before we touch teams
    except IntegrityError:
        db.session.rollback()
        return False, "An account with this email already exists. Please sign in.", None

    # Apply team memberships if any were attached to the invite
    team_ids = locked.team_ids
    if team_ids:
        from app.models.team import Team
        teams = Team.query.filter(Team.id.in_(team_ids)).all()
        if teams:
            user.teams.extend(teams)

    locked.accepted_at = datetime.utcnow()
    locked.accepted_user_id = user.id
    db.session.commit()
    return True, "Account created.", user


# ---------------------------------------------------------------------------
# Aggregate helpers (for badges + dashboards)
# ---------------------------------------------------------------------------

def pending_invite_count() -> int:
    """Count of invites that are still actionable (not accepted/revoked/expired)."""
    return (
        UserInvite.query
        .filter(
            UserInvite.accepted_at.is_(None),
            UserInvite.revoked_at.is_(None),
            UserInvite.expires_at > datetime.utcnow(),
        )
        .count()
    )
