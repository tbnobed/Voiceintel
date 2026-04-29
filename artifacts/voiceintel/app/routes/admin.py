import json
import logging
from datetime import datetime
from flask import Blueprint, render_template, redirect, url_for, request, flash, abort, jsonify
from flask_login import login_required, current_user
from app import db
from app.models.user import User, ROLES, ROLE_LABELS
from app.models.trigger import AutomationTrigger
from app.models.voicemail import Setting

logger = logging.getLogger(__name__)
admin_bp = Blueprint("admin", __name__)


def _admin_required():
    if not current_user.is_authenticated or not current_user.is_admin:
        abort(403)


def _user_management_required():
    """Admins and supervisors can manage users."""
    if not current_user.is_authenticated or not current_user.can_manage_users:
        abort(403)


# ---------------------------------------------------------------------------
# Admin overview
# ---------------------------------------------------------------------------

@admin_bp.route("/", strict_slashes=False)
@login_required
def index():
    _admin_required()
    from app.models.voicemail import Category
    from app.models.team import Team
    user_count = User.query.count()
    trigger_count = AutomationTrigger.query.filter_by(is_active=True).count()
    cat_count = Category.query.count()
    team_count = Team.query.count()
    custom_kw_raw = Setting.get("custom_urgency_keywords", "[]")
    try:
        custom_kw = json.loads(custom_kw_raw)
    except Exception:
        custom_kw = []
    # Check if SendGrid is configured (env var or DB)
    import os as _os
    sg_configured = bool(
        _os.environ.get("SENDGRID_API_KEY") or Setting.get("sendgrid_api_key", "")
    )
    return render_template(
        "admin/index.html",
        user_count=user_count,
        trigger_count=trigger_count,
        custom_kw_count=len(custom_kw),
        cat_count=cat_count,
        team_count=team_count,
        sg_configured=sg_configured,
    )


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

@admin_bp.route("/users")
@login_required
def users():
    _user_management_required()
    all_users = User.query.order_by(User.created_at).all()
    return render_template("admin/users.html", users=all_users, role_labels=ROLE_LABELS)


@admin_bp.route("/users/new", methods=["GET", "POST"])
@login_required
def new_user():
    _user_management_required()
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        name = request.form.get("name", "").strip()
        role = request.form.get("role", "viewer")
        password = request.form.get("password", "")

        # Only admins may create other admins.
        if role == "admin" and not current_user.is_admin:
            error = "Only administrators can create admin accounts."
        elif not email or not name or not password:
            error = "All fields are required."
        elif User.query.filter_by(email=email).first():
            error = "A user with that email already exists."
        elif role not in ROLES:
            error = "Invalid role."
        else:
            user = User(email=email, name=name, role=role)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            return redirect(url_for("admin.users"))
    return render_template(
        "admin/user_form.html",
        user=None, error=error, action="Create",
        roles=ROLES, role_labels=ROLE_LABELS,
    )


@admin_bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
def edit_user(user_id):
    _user_management_required()
    user = User.query.get_or_404(user_id)

    # Supervisors cannot edit admin accounts (only other admins can).
    if user.is_admin and not current_user.is_admin:
        flash("Only administrators can edit admin accounts.", "error")
        return redirect(url_for("admin.users"))

    error = None
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        role = request.form.get("role", "viewer")
        is_active = request.form.get("is_active") == "1"
        new_password = request.form.get("password", "").strip()

        # Only admins may grant or revoke the admin role.
        if (role == "admin" or user.is_admin) and not current_user.is_admin:
            error = "Only administrators can change the admin role."
        elif not name:
            error = "Name is required."
        elif role not in ROLES:
            error = "Invalid role."
        else:
            user.name = name
            user.role = role
            user.is_active = is_active
            if new_password:
                user.set_password(new_password)
            db.session.commit()
            return redirect(url_for("admin.users"))
    return render_template(
        "admin/user_form.html",
        user=user, error=error, action="Save Changes",
        roles=ROLES, role_labels=ROLE_LABELS,
    )


@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@login_required
def delete_user(user_id):
    _user_management_required()
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        return jsonify({"error": "Cannot delete your own account."}), 400
    # Supervisors cannot delete admin accounts.
    if user.is_admin and not current_user.is_admin:
        flash("Only administrators can delete admin accounts.", "error")
        return redirect(url_for("admin.users"))
    db.session.delete(user)
    db.session.commit()
    return redirect(url_for("admin.users"))


# ---------------------------------------------------------------------------
# Category management
# ---------------------------------------------------------------------------

@admin_bp.route("/categories")
@login_required
def categories():
    _admin_required()
    from app.models.voicemail import Category, Voicemail
    from sqlalchemy import func
    cats = (
        db.session.query(Category, func.count(Voicemail.id).label("vm_count"))
        .outerjoin(Voicemail, Voicemail.category_id == Category.id)
        .group_by(Category.id)
        .order_by(Category.name)
        .all()
    )
    return render_template("admin/categories.html", cats=cats)


@admin_bp.route("/categories/new", methods=["GET", "POST"])
@login_required
def new_category():
    _admin_required()
    from app.models.voicemail import Category
    error = None
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        if not name:
            error = "Category name is required."
        elif Category.query.filter(db.func.lower(Category.name) == name.lower()).first():
            error = "A category with that name already exists."
        else:
            db.session.add(Category(name=name, description=description or None))
            db.session.commit()
            flash("Category created.")
            return redirect(url_for("admin.categories"))
    return render_template("admin/category_form.html", category=None, error=error, action="Create Category")


@admin_bp.route("/categories/<int:cat_id>/edit", methods=["GET", "POST"])
@login_required
def edit_category(cat_id):
    _admin_required()
    from app.models.voicemail import Category
    cat = Category.query.get_or_404(cat_id)
    error = None
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        if not name:
            error = "Category name is required."
        elif (
            Category.query
            .filter(db.func.lower(Category.name) == name.lower(), Category.id != cat_id)
            .first()
        ):
            error = "Another category with that name already exists."
        else:
            cat.name = name
            cat.description = description or None
            db.session.commit()
            flash("Category updated.")
            return redirect(url_for("admin.categories"))
    return render_template("admin/category_form.html", category=cat, error=error, action="Save Changes")


@admin_bp.route("/categories/<int:cat_id>/delete", methods=["POST"])
@login_required
def delete_category(cat_id):
    _admin_required()
    from app.models.voicemail import Category, Voicemail
    cat = Category.query.get_or_404(cat_id)
    count = Voicemail.query.filter_by(category_id=cat_id).count()
    if count:
        flash(f"Cannot delete — {count} voicemail(s) are assigned to this category. Reassign them first.", "error")
        return redirect(url_for("admin.categories"))
    db.session.delete(cat)
    db.session.commit()
    flash("Category deleted.")
    return redirect(url_for("admin.categories"))


# ---------------------------------------------------------------------------
# Urgency keywords
# ---------------------------------------------------------------------------

@admin_bp.route("/keywords", methods=["GET", "POST"])
@login_required
def keywords():
    _admin_required()
    from app.services.nlp_service import DEFAULT_URGENCY_KEYWORDS

    if request.method == "POST":
        action = request.form.get("action", "save")
        if action == "reset":
            kw_list = sorted(DEFAULT_URGENCY_KEYWORDS)
            Setting.set("urgency_keywords", json.dumps(kw_list))
            flash(f"Reset to {len(kw_list)} default urgency keywords.")
        else:
            raw = request.form.get("keywords", "")
            kw_list = sorted({
                kw.strip().lower()
                for kw in raw.replace("\n", ",").split(",")
                if kw.strip()
            })
            Setting.set("urgency_keywords", json.dumps(kw_list))
            flash(f"Saved {len(kw_list)} urgency keyword(s).")
        return redirect(url_for("admin.keywords"))

    # Load from DB (seeded from defaults on first run)
    raw = Setting.get("urgency_keywords", "")
    if raw:
        try:
            kw_list = json.loads(raw)
        except Exception:
            kw_list = sorted(DEFAULT_URGENCY_KEYWORDS)
    else:
        # First visit — seed and save
        kw_list = sorted(DEFAULT_URGENCY_KEYWORDS)
        Setting.set("urgency_keywords", json.dumps(kw_list))

    return render_template(
        "admin/keywords.html",
        kw_list=kw_list,
        default_count=len(DEFAULT_URGENCY_KEYWORDS),
    )


# ---------------------------------------------------------------------------
# Automation triggers
# ---------------------------------------------------------------------------

@admin_bp.route("/triggers")
@login_required
def triggers():
    _admin_required()
    all_triggers = AutomationTrigger.query.order_by(AutomationTrigger.created_at.desc()).all()
    return render_template("admin/triggers.html", triggers=all_triggers)


@admin_bp.route("/triggers/new", methods=["GET", "POST"])
@login_required
def new_trigger():
    _admin_required()
    from app.models.voicemail import Category
    categories = Category.query.order_by(Category.name).all()
    error = None

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        condition_type = request.form.get("condition_type", "")
        condition_value = request.form.get("condition_value", "").strip()
        action_type = request.form.get("action_type", "")
        action_value = request.form.get("action_value", "").strip()

        if not name:
            error = "Trigger name is required."
        elif condition_type not in AutomationTrigger.CONDITION_LABELS:
            error = "Invalid condition type."
        elif action_type not in AutomationTrigger.ACTION_LABELS:
            error = "Invalid action type."
        else:
            t = AutomationTrigger(
                name=name,
                condition_type=condition_type,
                condition_value=condition_value,
                action_type=action_type,
                action_value=action_value,
                created_by_id=current_user.id,
            )
            db.session.add(t)
            db.session.commit()
            return redirect(url_for("admin.triggers"))

    return render_template(
        "admin/trigger_form.html",
        trigger=None,
        categories=categories,
        error=error,
        action="Create Trigger",
    )


@admin_bp.route("/triggers/<int:trigger_id>/edit", methods=["GET", "POST"])
@login_required
def edit_trigger(trigger_id):
    _admin_required()
    from app.models.voicemail import Category
    trigger = AutomationTrigger.query.get_or_404(trigger_id)
    categories = Category.query.order_by(Category.name).all()
    error = None

    if request.method == "POST":
        trigger.name = request.form.get("name", "").strip()
        trigger.condition_type = request.form.get("condition_type", "")
        trigger.condition_value = request.form.get("condition_value", "").strip()
        trigger.action_type = request.form.get("action_type", "")
        trigger.action_value = request.form.get("action_value", "").strip()
        trigger.is_active = request.form.get("is_active") == "1"
        db.session.commit()
        return redirect(url_for("admin.triggers"))

    return render_template(
        "admin/trigger_form.html",
        trigger=trigger,
        categories=categories,
        error=error,
        action="Save Changes",
    )


@admin_bp.route("/triggers/<int:trigger_id>/toggle", methods=["POST"])
@login_required
def toggle_trigger(trigger_id):
    _admin_required()
    trigger = AutomationTrigger.query.get_or_404(trigger_id)
    trigger.is_active = not trigger.is_active
    db.session.commit()
    return redirect(url_for("admin.triggers"))


@admin_bp.route("/triggers/<int:trigger_id>/delete", methods=["POST"])
@login_required
def delete_trigger(trigger_id):
    _admin_required()
    trigger = AutomationTrigger.query.get_or_404(trigger_id)
    db.session.delete(trigger)
    db.session.commit()
    return redirect(url_for("admin.triggers"))


# ---------------------------------------------------------------------------
# Integrations (SendGrid)
# ---------------------------------------------------------------------------

_SENDGRID_SETTINGS = [
    ("sendgrid_api_key",      "SendGrid API Key",              "password", "SG.xxxxxxxxxxxxxxxx…"),
    ("sendgrid_from_email",   "From Email Address",            "email",    "alerts@yourdomain.com"),
    ("sendgrid_from_name",    "From Display Name",             "text",     "VoiceIntel"),
    ("sendgrid_admin_email",  "Admin Notification Email(s)",   "text",     "ops@yourdomain.com"),
    ("sendgrid_webhook_key",  "Inbound Parse Webhook Secret",  "text",     "random-secret-token"),
]


@admin_bp.route("/integrations/test", methods=["POST"])
@login_required
def integrations_test():
    """AJAX endpoint — returns JSON result of SendGrid API key validation."""
    _admin_required()
    from app.services.email_service import test_sendgrid_connection
    from flask import jsonify
    key = request.form.get("sendgrid_api_key", "").strip()
    ok, msg = test_sendgrid_connection(key or None)
    return jsonify({"ok": ok, "msg": msg})


@admin_bp.route("/integrations", methods=["GET", "POST"])
@login_required
def integrations():
    _admin_required()

    if request.method == "POST":
        action = request.form.get("action", "save")
        if action == "save":
            # Save all settings
            for key, *_ in _SENDGRID_SETTINGS:
                val = request.form.get(key, "").strip()
                # Don't overwrite API key with blank if field was left empty
                if key == "sendgrid_api_key" and not val:
                    continue
                Setting.set(key, val)
            flash("Integration settings saved.")
            return redirect(url_for("admin.integrations"))

    current = {key: Setting.get(key, "") for key, *_ in _SENDGRID_SETTINGS}
    # Mask stored API key for display
    if current.get("sendgrid_api_key"):
        raw = current["sendgrid_api_key"]
        current["sendgrid_api_key_masked"] = raw[:6] + "…" + raw[-4:] if len(raw) > 12 else "••••••••"
    else:
        current["sendgrid_api_key_masked"] = ""

    # Build example webhook URL
    # Respect X-Forwarded-Proto so the URL shows https:// when behind a reverse proxy.
    import os as _os
    domains = _os.environ.get("REPLIT_DOMAINS", "")
    if domains:
        host = domains.split(",")[0].strip()
        base_url = f"https://{host}"
    else:
        proto = request.headers.get("X-Forwarded-Proto", request.scheme)
        base_url = f"{proto}://{request.host}"
    webhook_token = current.get("sendgrid_webhook_key", "")
    webhook_url = f"{base_url}/api/webhook/inbound"
    if webhook_token:
        webhook_url += f"?token={webhook_token}"

    return render_template(
        "admin/integrations.html",
        settings=_SENDGRID_SETTINGS,
        current=current,
        webhook_url=webhook_url,
    )
