"""
User-invitation routes.

Exposes two surfaces:
  - /admin/invites/* — admin/supervisor management UI (login-required)
  - /invite/<token>  — public acceptance page (no login)

Both are registered through a single blueprint mounted at the app root because
the public path lives outside /admin.
"""

import logging
from flask import (
    Blueprint, render_template, redirect, url_for, request, flash, abort
)
from flask_login import login_required, login_user, current_user

from app import db
from app.models.invite import UserInvite, INVITE_STATUS_LABELS
from app.models.user import ROLES, ROLE_LABELS
from app.models.team import Team
from app.services.invite_service import (
    create_invite, send_invite_email, resend_invite, revoke_invite,
    find_invite_by_token, accept_invite, build_invite_url,
)

logger = logging.getLogger(__name__)
invites_bp = Blueprint("invites", __name__)


# ---------------------------------------------------------------------------
# Authorization helpers (mirror admin.py conventions)
# ---------------------------------------------------------------------------

def _user_management_required():
    """Admins and supervisors can manage invites."""
    if not current_user.is_authenticated or not current_user.can_manage_users:
        abort(403)


def _invite_access_required(invite):
    """Admins can manage any invite; supervisors only those they sent."""
    if current_user.is_admin:
        return
    if invite.invited_by_id != current_user.id:
        abort(403)


# ---------------------------------------------------------------------------
# Admin: list
# ---------------------------------------------------------------------------

@invites_bp.route("/admin/invites")
@login_required
def list_invites():
    _user_management_required()
    status_filter = (request.args.get("status") or "all").lower()

    base_q = UserInvite.query
    if not current_user.is_admin:
        # Supervisors only see invitations they sent themselves.
        base_q = base_q.filter(UserInvite.invited_by_id == current_user.id)
    q = base_q.order_by(UserInvite.created_at.desc())
    invites = q.all()

    # Filter by derived status in Python — there are very few invites and the
    # status property already encapsulates the rules.
    if status_filter in ("pending", "accepted", "revoked", "expired"):
        invites = [i for i in invites if i.status == status_filter]

    counts = {s: 0 for s in ("pending", "accepted", "revoked", "expired")}
    for inv in q.all():
        counts[inv.status] = counts.get(inv.status, 0) + 1

    return render_template(
        "admin/invites.html",
        invites=invites,
        status_filter=status_filter,
        status_labels=INVITE_STATUS_LABELS,
        counts=counts,
    )


# ---------------------------------------------------------------------------
# Admin: create + send
# ---------------------------------------------------------------------------

@invites_bp.route("/admin/invites/new", methods=["GET", "POST"])
@login_required
def new_invite():
    _user_management_required()
    if current_user.is_admin:
        teams = Team.query.order_by(Team.name).all()
        allowed_team_ids = {t.id for t in teams}
    else:
        # Supervisors can only assign invitees to teams they belong to.
        teams = sorted(current_user.teams, key=lambda t: t.name.lower())
        allowed_team_ids = {t.id for t in teams}
    error = None

    if request.method == "POST":
        from app.models.user import User
        email = request.form.get("email", "").strip().lower()
        name  = request.form.get("name", "").strip()
        role  = request.form.get("role", "viewer")
        raw_team_ids = [int(t) for t in request.form.getlist("team_ids") if t.isdigit()]
        # Drop any team the current user isn't allowed to assign to.
        team_ids = [tid for tid in raw_team_ids if tid in allowed_team_ids]
        rejected = [tid for tid in raw_team_ids if tid not in allowed_team_ids]

        from app.routes.admin import SUPERVISOR_ASSIGNABLE_ROLES
        if not current_user.is_admin and role not in SUPERVISOR_ASSIGNABLE_ROLES:
            error = "Supervisors can only invite agent or viewer accounts."
        elif not email or not name:
            error = "Email and name are required."
        elif role not in ROLES:
            error = "Invalid role."
        elif rejected:
            error = "You can only invite users into teams you belong to."
        elif not current_user.is_admin and not team_ids:
            error = "Please select at least one team for the invitee."
        elif User.query.filter_by(email=email).first():
            error = "A user with this email already exists."
        else:
            invite = create_invite(
                email=email, name=name, role=role,
                team_ids=team_ids, invited_by=current_user,
            )
            sent = send_invite_email(invite)
            if sent:
                flash(f"Invitation sent to {invite.email}.")
            else:
                # Fall back to displaying the link if SendGrid isn't configured
                # so the invite can still be delivered out-of-band.
                url = build_invite_url(invite.token)
                flash(
                    f"Invite created but email could not be sent (check SendGrid "
                    f"settings). Share this link manually: {url}",
                    "warning",
                )
            return redirect(url_for("invites.list_invites"))

    return render_template(
        "admin/invite_form.html",
        error=error,
        roles=ROLES,
        role_labels=ROLE_LABELS,
        teams=teams,
        selected_team_ids=set(),
    )


# ---------------------------------------------------------------------------
# Admin: resend / revoke / delete
# ---------------------------------------------------------------------------

@invites_bp.route("/admin/invites/<int:invite_id>/resend", methods=["POST"])
@login_required
def resend(invite_id):
    _user_management_required()
    invite = UserInvite.query.get_or_404(invite_id)
    _invite_access_required(invite)
    if invite.role == "admin" and not current_user.is_admin:
        flash("Only administrators can manage admin invitations.", "error")
        return redirect(url_for("invites.list_invites"))
    if not invite.is_actionable:
        flash("This invitation has already been accepted.", "error")
        return redirect(url_for("invites.list_invites"))
    sent = resend_invite(invite)
    if sent:
        flash(f"Invitation re-sent to {invite.email}.")
    else:
        url = build_invite_url(invite.token)
        flash(
            f"Could not send email (check SendGrid settings). "
            f"Share this link manually: {url}",
            "warning",
        )
    return redirect(url_for("invites.list_invites"))


@invites_bp.route("/admin/invites/<int:invite_id>/revoke", methods=["POST"])
@login_required
def revoke(invite_id):
    _user_management_required()
    invite = UserInvite.query.get_or_404(invite_id)
    _invite_access_required(invite)
    if invite.role == "admin" and not current_user.is_admin:
        flash("Only administrators can manage admin invitations.", "error")
        return redirect(url_for("invites.list_invites"))
    if invite.accepted_at:
        flash("Cannot revoke an accepted invitation.", "error")
        return redirect(url_for("invites.list_invites"))
    revoke_invite(invite)
    flash(f"Invitation for {invite.email} revoked.")
    return redirect(url_for("invites.list_invites"))


@invites_bp.route("/admin/invites/<int:invite_id>/delete", methods=["POST"])
@login_required
def delete(invite_id):
    _user_management_required()
    invite = UserInvite.query.get_or_404(invite_id)
    _invite_access_required(invite)
    if invite.role == "admin" and not current_user.is_admin:
        flash("Only administrators can manage admin invitations.", "error")
        return redirect(url_for("invites.list_invites"))
    db.session.delete(invite)
    db.session.commit()
    flash("Invitation deleted.")
    return redirect(url_for("invites.list_invites"))


# ---------------------------------------------------------------------------
# Public: acceptance flow
# ---------------------------------------------------------------------------

@invites_bp.route("/invite/<token>", methods=["GET", "POST"])
def accept(token):
    """Public — no login required. Creates the user account."""
    invite = find_invite_by_token(token)

    if not invite:
        return render_template(
            "invite_error.html",
            heading="Invitation not found",
            message="This invitation link is invalid. Ask the person who invited you to send a new one.",
        ), 404

    if invite.status != "pending":
        message_map = {
            "accepted": "This invitation has already been used. Please sign in below.",
            "revoked":  "This invitation has been revoked. Ask for a new one.",
            "expired":  "This invitation has expired. Ask for a new one.",
        }
        return render_template(
            "invite_error.html",
            heading=f"Invitation {invite.status}",
            message=message_map.get(invite.status, "This invitation is no longer valid."),
            show_login=(invite.status == "accepted"),
        ), 410

    error = None
    if request.method == "POST":
        name = request.form.get("name", "").strip() or invite.name
        password = request.form.get("password", "")
        confirm  = request.form.get("password_confirm", "")

        if password != confirm:
            error = "Passwords do not match."
        else:
            ok, msg, user = accept_invite(invite, name, password)
            if not ok:
                error = msg
            else:
                # Auto-sign-in the new user — they just proved they own the
                # email by clicking the token link, so the login is
                # implicitly verified.
                login_user(user)
                flash(f"Welcome to VoiceIntel, {user.name}!")
                return redirect(url_for("main.dashboard"))

    return render_template(
        "invite_accept.html",
        invite=invite,
        error=error,
        role_label=ROLE_LABELS.get(invite.role, invite.role.title()),
    )
