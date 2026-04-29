"""
Callback task management.

A "callback" is a follow-up phone call that needs to be made in response to a
voicemail. Supervisors and admins assign them to agents (or to themselves);
the assignee marks them in_progress / completed / cancelled.

Routes:
  GET  /tasks                              — task inbox (mine + supervisor view)
  POST /voicemails/<vm_id>/callbacks       — create a callback (assign)
  POST /tasks/<cb_id>/update               — update status / notes / assignee
  POST /tasks/<cb_id>/delete               — delete (admin/supervisor only)
"""
import logging
from datetime import datetime
from flask import Blueprint, render_template, redirect, url_for, request, flash, abort
from flask_login import login_required, current_user
from app import db
from app.models.user import User
from app.models.voicemail import (
    Voicemail, Callback, CALLBACK_STATUSES, CALLBACK_PRIORITIES,
)

logger = logging.getLogger(__name__)
tasks_bp = Blueprint("tasks", __name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_due(value: str):
    """Parse <input type='datetime-local'> value (e.g. '2026-04-30T15:00')."""
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _can_modify_callback(cb: Callback) -> bool:
    """Assignee can update their own task; supervisors/admins can update any."""
    if current_user.is_admin or current_user.is_supervisor:
        return True
    return cb.assignee_id == current_user.id


def _safe_next(value: str, fallback: str) -> str:
    """Only allow same-origin path redirects (prevents open-redirect)."""
    if not value:
        return fallback
    # Reject any URL with a scheme/host/protocol-relative — only allow plain paths.
    if value.startswith("/") and not value.startswith("//"):
        return value
    return fallback


# ---------------------------------------------------------------------------
# Task inbox
# ---------------------------------------------------------------------------

@tasks_bp.route("/tasks")
@login_required
def task_list():
    """Show callbacks. Defaults to 'mine'; supervisors/admins can view all."""
    view = request.args.get("view", "mine")
    status_filter = request.args.get("status", "open")  # open | all | <status>

    q = Callback.query

    if view == "all" and (current_user.is_admin or current_user.is_supervisor):
        pass  # no assignee filter
    else:
        view = "mine"
        q = q.filter(Callback.assignee_id == current_user.id)

    if status_filter == "open":
        q = q.filter(Callback.status.in_(("pending", "in_progress")))
    elif status_filter in CALLBACK_STATUSES:
        q = q.filter(Callback.status == status_filter)
    # else "all" — no filter

    callbacks = q.order_by(
        db.case(
            (Callback.status == "in_progress", 0),
            (Callback.status == "pending",     1),
            (Callback.status == "completed",   2),
            (Callback.status == "cancelled",   3),
            else_=4,
        ),
        Callback.priority.desc(),  # 'urgent' > 'normal' alphabetically
        Callback.created_at.desc(),
    ).all()

    # Counts for the header tabs
    mine_open = Callback.query.filter(
        Callback.assignee_id == current_user.id,
        Callback.status.in_(("pending", "in_progress")),
    ).count()
    all_open = None
    if current_user.is_admin or current_user.is_supervisor:
        all_open = Callback.query.filter(
            Callback.status.in_(("pending", "in_progress"))
        ).count()

    return render_template(
        "tasks.html",
        callbacks=callbacks,
        view=view,
        status_filter=status_filter,
        mine_open=mine_open,
        all_open=all_open,
    )


# ---------------------------------------------------------------------------
# Create callback
# ---------------------------------------------------------------------------

@tasks_bp.route("/voicemails/<int:vm_id>/callbacks", methods=["POST"])
@login_required
def create_callback(vm_id):
    if not current_user.can_assign_callbacks:
        abort(403)
    vm = Voicemail.query.get_or_404(vm_id)

    raw_assignee = request.form.get("assignee_id", "").strip()
    try:
        assignee_id = int(raw_assignee)
    except (TypeError, ValueError):
        flash("Please choose someone to assign the callback to.", "error")
        return redirect(url_for("main.voicemail_detail", vm_id=vm.id))

    assignee = User.query.get(assignee_id)
    if not assignee or not assignee.is_active or not assignee.can_be_assigned_callback:
        flash("That user cannot be assigned a callback.", "error")
        return redirect(url_for("main.voicemail_detail", vm_id=vm.id))

    priority = request.form.get("priority", "normal")
    if priority not in CALLBACK_PRIORITIES:
        priority = "normal"

    cb = Callback(
        voicemail_id=vm.id,
        assignee_id=assignee.id,
        assigner_id=current_user.id,
        priority=priority,
        notes=request.form.get("notes", "").strip() or None,
        due_at=_parse_due(request.form.get("due_at", "")),
    )
    db.session.add(cb)
    db.session.commit()
    flash(f"Callback assigned to {assignee.name}.")
    return redirect(url_for("main.voicemail_detail", vm_id=vm.id))


# ---------------------------------------------------------------------------
# Update callback (status / notes)
# ---------------------------------------------------------------------------

@tasks_bp.route("/tasks/<int:cb_id>/update", methods=["POST"])
@login_required
def update_callback(cb_id):
    cb = Callback.query.get_or_404(cb_id)
    if not _can_modify_callback(cb):
        abort(403)

    new_status = request.form.get("status", cb.status)
    if new_status in CALLBACK_STATUSES:
        was_open = cb.is_open
        cb.status = new_status
        if new_status == "completed" and was_open:
            cb.completed_at = datetime.utcnow()
        elif new_status in ("pending", "in_progress"):
            cb.completed_at = None

    # Supervisors/admins can also reassign and change priority/notes from the list view.
    if current_user.is_admin or current_user.is_supervisor:
        new_assignee = request.form.get("assignee_id", "").strip()
        if new_assignee:
            try:
                u = User.query.get(int(new_assignee))
                if u and u.is_active and u.can_be_assigned_callback:
                    cb.assignee_id = u.id
            except (TypeError, ValueError):
                pass
        new_priority = request.form.get("priority")
        if new_priority in CALLBACK_PRIORITIES:
            cb.priority = new_priority
        if "notes" in request.form:
            cb.notes = request.form.get("notes", "").strip() or None

    db.session.commit()
    flash("Callback updated.")
    next_url = _safe_next(request.form.get("next"), url_for("tasks.task_list"))
    return redirect(next_url)


# ---------------------------------------------------------------------------
# Delete callback
# ---------------------------------------------------------------------------

@tasks_bp.route("/tasks/<int:cb_id>/delete", methods=["POST"])
@login_required
def delete_callback(cb_id):
    if not current_user.can_assign_callbacks:
        abort(403)
    cb = Callback.query.get_or_404(cb_id)
    vm_id = cb.voicemail_id
    db.session.delete(cb)
    db.session.commit()
    flash("Callback removed.")
    next_url = _safe_next(
        request.form.get("next"),
        url_for("main.voicemail_detail", vm_id=vm_id),
    )
    return redirect(next_url)
