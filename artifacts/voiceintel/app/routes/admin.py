import json
import logging
from datetime import datetime
from flask import Blueprint, render_template, redirect, url_for, request, flash, abort, jsonify
from flask_login import login_required, current_user
from app import db
from app.models.user import User
from app.models.trigger import AutomationTrigger
from app.models.voicemail import Setting

logger = logging.getLogger(__name__)
admin_bp = Blueprint("admin", __name__)


def _admin_required():
    if not current_user.is_authenticated or not current_user.is_admin:
        abort(403)


# ---------------------------------------------------------------------------
# Admin overview
# ---------------------------------------------------------------------------

@admin_bp.route("/")
@login_required
def index():
    _admin_required()
    user_count = User.query.count()
    trigger_count = AutomationTrigger.query.filter_by(is_active=True).count()
    custom_kw_raw = Setting.get("custom_urgency_keywords", "[]")
    try:
        custom_kw = json.loads(custom_kw_raw)
    except Exception:
        custom_kw = []
    return render_template(
        "admin/index.html",
        user_count=user_count,
        trigger_count=trigger_count,
        custom_kw_count=len(custom_kw),
    )


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

@admin_bp.route("/users")
@login_required
def users():
    _admin_required()
    all_users = User.query.order_by(User.created_at).all()
    return render_template("admin/users.html", users=all_users)


@admin_bp.route("/users/new", methods=["GET", "POST"])
@login_required
def new_user():
    _admin_required()
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        name = request.form.get("name", "").strip()
        role = request.form.get("role", "viewer")
        password = request.form.get("password", "")
        if not email or not name or not password:
            error = "All fields are required."
        elif User.query.filter_by(email=email).first():
            error = "A user with that email already exists."
        elif role not in ("admin", "viewer"):
            error = "Invalid role."
        else:
            user = User(email=email, name=name, role=role)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            return redirect(url_for("admin.users"))
    return render_template("admin/user_form.html", user=None, error=error, action="Create")


@admin_bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
def edit_user(user_id):
    _admin_required()
    user = User.query.get_or_404(user_id)
    error = None
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        role = request.form.get("role", "viewer")
        is_active = request.form.get("is_active") == "1"
        new_password = request.form.get("password", "").strip()

        if not name:
            error = "Name is required."
        elif role not in ("admin", "viewer"):
            error = "Invalid role."
        else:
            user.name = name
            user.role = role
            user.is_active = is_active
            if new_password:
                user.set_password(new_password)
            db.session.commit()
            return redirect(url_for("admin.users"))
    return render_template("admin/user_form.html", user=user, error=error, action="Save Changes")


@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@login_required
def delete_user(user_id):
    _admin_required()
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        return jsonify({"error": "Cannot delete your own account."}), 400
    db.session.delete(user)
    db.session.commit()
    return redirect(url_for("admin.users"))


# ---------------------------------------------------------------------------
# Urgency keywords
# ---------------------------------------------------------------------------

@admin_bp.route("/keywords", methods=["GET", "POST"])
@login_required
def keywords():
    _admin_required()
    error = None
    success = None

    if request.method == "POST":
        raw = request.form.get("keywords", "")
        parsed = [kw.strip().lower() for kw in raw.replace("\n", ",").split(",") if kw.strip()]
        Setting.set("custom_urgency_keywords", json.dumps(parsed))
        success = f"Saved {len(parsed)} custom urgency keyword(s)."

    raw_setting = Setting.get("custom_urgency_keywords", "[]")
    try:
        kw_list = json.loads(raw_setting)
    except Exception:
        kw_list = []

    return render_template("admin/keywords.html", kw_list=kw_list, error=error, success=success)


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
