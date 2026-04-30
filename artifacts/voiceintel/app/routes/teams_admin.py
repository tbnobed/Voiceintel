"""
Admin UI for Teams + Routing Rules.

Mounted at /admin/teams (registered in app/__init__.py).
Permission: admin OR supervisor (any user-management role).
"""
import logging
import re
from flask import Blueprint, render_template, redirect, url_for, request, flash, abort
from flask_login import login_required, current_user
from app import db
from app.models.user import User
from app.models.team import Team, RoutingRule, RULE_KINDS, RULE_KIND_LABELS

logger = logging.getLogger(__name__)
teams_admin_bp = Blueprint("teams_admin", __name__)


def _required():
    if not current_user.is_authenticated or not current_user.can_manage_users:
        abort(403)


def _team_access_required(team):
    """Admins can manage any team; supervisors only their own."""
    if current_user.is_admin:
        return
    if not current_user.is_authenticated or not current_user.is_supervisor:
        abort(403)
    if team not in current_user.teams:
        abort(403)


def _visible_teams_query():
    """Teams the current user is allowed to see in admin lists."""
    if current_user.is_admin:
        return Team.query.order_by(Team.name)
    my_ids = [t.id for t in current_user.teams]
    if not my_ids:
        # Empty result, but still a valid query for templates that iterate it.
        return Team.query.filter(Team.id == -1)
    return Team.query.filter(Team.id.in_(my_ids)).order_by(Team.name)


def _slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (s or "").strip().lower())
    return re.sub(r"-+", "-", s).strip("-") or "team"


_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{3,8}$")


def _safe_color(value: str, fallback: str = "#F7CE5B") -> str:
    """Whitelist hex colors only — prevents CSS-injection via inline style attr."""
    value = (value or "").strip()
    return value if _COLOR_RE.match(value) else fallback


# ---------------------------------------------------------------------------
# Teams CRUD
# ---------------------------------------------------------------------------

@teams_admin_bp.route("/", strict_slashes=False)
@login_required
def list_teams():
    _required()
    teams = _visible_teams_query().all()
    return render_template("admin/teams.html", teams=teams)


@teams_admin_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_team():
    # Only admins can create new teams.
    if not current_user.is_authenticated or not current_user.is_admin:
        abort(403)
    error = None
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        color = _safe_color(request.form.get("color", ""))

        if not name:
            error = "Name is required."
        else:
            slug = _slugify(name)
            if Team.query.filter((Team.name == name) | (Team.slug == slug)).first():
                error = "A team with that name already exists."
            else:
                t = Team(name=name, slug=slug, description=description, color=color)
                db.session.add(t)
                db.session.commit()
                flash(f"Team '{name}' created.", "success")
                return redirect(url_for("teams_admin.team_detail", team_id=t.id))
    return render_template("admin/team_form.html", team=None, error=error,
                           action="Create Team")


@teams_admin_bp.route("/<int:team_id>/edit", methods=["GET", "POST"])
@login_required
def edit_team(team_id):
    _required()
    team = Team.query.get_or_404(team_id)
    _team_access_required(team)
    error = None
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        color = _safe_color(request.form.get("color", ""), fallback=team.color)

        if not name:
            error = "Name is required."
        else:
            existing = Team.query.filter(Team.name == name, Team.id != team.id).first()
            if existing:
                error = "Another team already uses that name."
            else:
                team.name = name
                team.description = description
                team.color = color
                # Keep slug stable unless name changed dramatically
                if not team.slug:
                    team.slug = _slugify(name)
                db.session.commit()
                flash("Team updated.", "success")
                return redirect(url_for("teams_admin.team_detail", team_id=team.id))
    return render_template("admin/team_form.html", team=team, error=error,
                           action="Save Changes")


@teams_admin_bp.route("/<int:team_id>/delete", methods=["POST"])
@login_required
def delete_team(team_id):
    # Only admins can delete teams.
    if not current_user.is_authenticated or not current_user.is_admin:
        abort(403)
    team = Team.query.get_or_404(team_id)
    name = team.name
    db.session.delete(team)
    db.session.commit()
    flash(f"Team '{name}' deleted.", "success")
    return redirect(url_for("teams_admin.list_teams"))


# ---------------------------------------------------------------------------
# Team detail (members + rules)
# ---------------------------------------------------------------------------

@teams_admin_bp.route("/<int:team_id>")
@login_required
def team_detail(team_id):
    _required()
    team = Team.query.get_or_404(team_id)
    _team_access_required(team)
    member_ids = {u.id for u in team.members}
    q = User.query.filter(User.is_active.is_(True))
    if member_ids:
        q = q.filter(~User.id.in_(member_ids))
    if not current_user.is_admin:
        # Supervisors should only see/add users that are already in one of
        # their other teams (and never admin accounts).
        my_team_ids = [t.id for t in current_user.teams]
        if my_team_ids:
            q = q.filter(User.teams.any(Team.id.in_(my_team_ids)))
        else:
            q = q.filter(User.id == -1)
        q = q.filter(User.role != "admin")
    available_users = q.order_by(User.name).all()
    return render_template(
        "admin/team_detail.html",
        team=team,
        available_users=available_users,
        rule_kinds=RULE_KINDS,
        rule_kind_labels=RULE_KIND_LABELS,
    )


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------

@teams_admin_bp.route("/<int:team_id>/members/add", methods=["POST"])
@login_required
def add_member(team_id):
    _required()
    team = Team.query.get_or_404(team_id)
    _team_access_required(team)
    user_id = request.form.get("user_id", type=int)
    if not user_id:
        flash("Please select a user.", "error")
        return redirect(url_for("teams_admin.team_detail", team_id=team_id))
    user = User.query.get_or_404(user_id)
    # Prevent supervisors from sneaking arbitrary users (or elevated accounts)
    # onto teams they manage.
    if not current_user.is_admin:
        if user.role in ("admin", "supervisor"):
            abort(403)
        my_team_ids = {t.id for t in current_user.teams}
        if not (my_team_ids & {t.id for t in user.teams}):
            abort(403)
    if user not in team.members:
        team.members.append(user)
        db.session.commit()
        flash(f"Added {user.name} to {team.name}.", "success")
    return redirect(url_for("teams_admin.team_detail", team_id=team_id))


@teams_admin_bp.route("/<int:team_id>/members/<int:user_id>/remove", methods=["POST"])
@login_required
def remove_member(team_id, user_id):
    _required()
    team = Team.query.get_or_404(team_id)
    _team_access_required(team)
    user = User.query.get_or_404(user_id)
    # Supervisors must not be able to remove admins or peer supervisors from
    # teams they happen to share with them.
    if not current_user.is_admin and user.role in ("admin", "supervisor"):
        abort(403)
    if user in team.members:
        team.members.remove(user)
        db.session.commit()
        flash(f"Removed {user.name} from {team.name}.", "success")
    return redirect(url_for("teams_admin.team_detail", team_id=team_id))


# ---------------------------------------------------------------------------
# Routing rules
# ---------------------------------------------------------------------------

@teams_admin_bp.route("/<int:team_id>/rules/add", methods=["POST"])
@login_required
def add_rule(team_id):
    _required()
    team = Team.query.get_or_404(team_id)
    _team_access_required(team)
    kind = request.form.get("kind", "")
    pattern = request.form.get("pattern", "").strip()
    priority = request.form.get("priority", type=int) or 100
    is_active = request.form.get("is_active") == "on"

    if kind not in RULE_KINDS:
        flash("Invalid rule type.", "error")
    elif not pattern:
        flash("Pattern is required.", "error")
    else:
        r = RoutingRule(
            team_id=team.id,
            kind=kind,
            pattern=pattern,
            priority=priority,
            is_active=is_active,
        )
        db.session.add(r)
        db.session.commit()
        flash("Rule added.", "success")
    return redirect(url_for("teams_admin.team_detail", team_id=team_id) + "#rules")


@teams_admin_bp.route("/rules/<int:rule_id>/toggle", methods=["POST"])
@login_required
def toggle_rule(rule_id):
    _required()
    rule = RoutingRule.query.get_or_404(rule_id)
    _team_access_required(rule.team)
    rule.is_active = not rule.is_active
    db.session.commit()
    return redirect(url_for("teams_admin.team_detail", team_id=rule.team_id) + "#rules")


@teams_admin_bp.route("/rules/<int:rule_id>/delete", methods=["POST"])
@login_required
def delete_rule(rule_id):
    _required()
    rule = RoutingRule.query.get_or_404(rule_id)
    _team_access_required(rule.team)
    team_id = rule.team_id
    db.session.delete(rule)
    db.session.commit()
    flash("Rule removed.", "success")
    return redirect(url_for("teams_admin.team_detail", team_id=team_id) + "#rules")
