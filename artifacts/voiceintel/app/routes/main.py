import os
from flask import Blueprint, render_template, request, redirect, url_for, abort, send_file, current_app, jsonify, flash
from flask_login import login_required, current_user
from sqlalchemy import func, desc, or_
from sqlalchemy.orm import selectinload
from datetime import datetime, timedelta
from collections import Counter

from app import db
from app.models.voicemail import Voicemail, Transcript, Insight, Category, VoicemailNote, Callback
from app.models.user import User
from app.models.team import Team
from app.services.nlp_service import STOPWORDS
from app.utils.team_scope import scope_voicemails, can_view_voicemail, is_unrestricted, user_team_ids


def _filter_keywords(keywords):
    """Drop common filler/stopwords and very short tokens from a keyword list."""
    return [
        kw for kw in keywords
        if kw and len(kw) >= 3 and kw.lower() not in STOPWORDS
    ]

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
@login_required
def dashboard():
    today = datetime.utcnow().date()
    week_ago = datetime.utcnow() - timedelta(days=7)

    # Scope all dashboard queries to the voicemails this user is allowed to
    # see — agents/viewers only see their teams (+ unrouted), supervisors and
    # admins see everything.
    base = scope_voicemails(Voicemail.query, current_user)

    total = base.count()
    today_count = scope_voicemails(
        Voicemail.query.filter(func.date(Voicemail.received_at) == today),
        current_user,
    ).count()
    urgent_count = scope_voicemails(
        Voicemail.query.filter_by(is_urgent=True), current_user,
    ).count()

    cat_dist_q = (
        db.session.query(Category.name, func.count(Voicemail.id))
        .join(Voicemail, Voicemail.category_id == Category.id, isouter=True)
    )
    cat_dist_q = scope_voicemails(cat_dist_q, current_user)
    category_dist = cat_dist_q.group_by(Category.name).all()

    trend_q = db.session.query(
        func.date(Voicemail.received_at).label("day"),
        func.count(Voicemail.id).label("count"),
    ).filter(Voicemail.received_at >= week_ago)
    trend_q = scope_voicemails(trend_q, current_user)
    daily_trend = [
        {"day": str(d), "count": c}
        for d, c in trend_q.group_by(func.date(Voicemail.received_at))
                           .order_by("day").all()
    ]

    # Keywords from insights — scope by joining to voicemails.
    if is_unrestricted(current_user):
        insights_with_kw = Insight.query.filter(Insight.keywords.isnot(None)).limit(100).all()
    else:
        ins_q = (
            db.session.query(Insight)
            .join(Voicemail, Voicemail.id == Insight.voicemail_id)
            .filter(Insight.keywords.isnot(None))
        )
        ins_q = scope_voicemails(ins_q, current_user)
        insights_with_kw = ins_q.limit(100).all()
    all_keywords = []
    for ins in insights_with_kw:
        if ins.keywords:
            all_keywords.extend(_filter_keywords(ins.keywords))
    top_keywords = [kw for kw, _ in Counter(all_keywords).most_common(15)]

    recent = scope_voicemails(
        Voicemail.query.order_by(desc(Voicemail.created_at)),
        current_user,
    ).limit(5).all()

    return render_template(
        "dashboard.html",
        total=total,
        today_count=today_count,
        urgent_count=urgent_count,
        category_dist=category_dist,
        daily_trend=daily_trend,
        top_keywords=top_keywords,
        recent=recent,
    )


@main_bp.route("/voicemails")
@login_required
def voicemail_list():
    page = request.args.get("page", 1, type=int)
    per_page = 20

    q = request.args.get("q", "").strip()
    category_id = request.args.get("category", type=int)
    urgency = request.args.get("urgency")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    # Team filter: numeric team_id, or the literal string "unrouted"
    team_filter = request.args.get("team", "").strip()

    # Sorting — whitelist of allowed sort keys mapped to ORDER BY expressions.
    # Each tuple is (asc_expr, desc_expr) so we can apply NULLS-LAST consistently
    # by always tie-breaking on received_at desc.
    sort = request.args.get("sort", "received_at")
    direction = request.args.get("dir", "desc").lower()
    if direction not in ("asc", "desc"):
        direction = "desc"

    sort_columns = {
        "received_at":       Voicemail.received_at,
        "subject":           Voicemail.subject,            # caller-name column
        "category":          Category.name,                # via outer join below
        "is_urgent":         Voicemail.is_urgent,
        "processing_status": Voicemail.processing_status,
    }
    if sort not in sort_columns:
        sort = "received_at"

    query = Voicemail.query.join(
        Transcript, Voicemail.id == Transcript.voicemail_id, isouter=True
    )
    # Outer join Category so we can sort by category name and still see
    # voicemails that have no category assigned.
    query = query.join(Category, Voicemail.category_id == Category.id, isouter=True)

    if q:
        query = query.filter(Transcript.text.ilike(f"%{q}%"))

    if category_id:
        query = query.filter(Voicemail.category_id == category_id)

    if urgency == "urgent":
        query = query.filter(Voicemail.is_urgent == True)
    elif urgency == "normal":
        query = query.filter(Voicemail.is_urgent == False)

    if date_from:
        try:
            dt = datetime.strptime(date_from, "%Y-%m-%d")
            query = query.filter(Voicemail.received_at >= dt)
        except ValueError:
            pass

    if date_to:
        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            query = query.filter(Voicemail.received_at < dt)
        except ValueError:
            pass

    # Team filter (manual selection from the UI)
    if team_filter == "unrouted":
        query = query.filter(Voicemail.team_id.is_(None))
    elif team_filter.isdigit():
        query = query.filter(Voicemail.team_id == int(team_filter))

    # Visibility scoping — agents only see their team(s) + unrouted.
    query = scope_voicemails(query, current_user)

    primary = sort_columns[sort]
    primary = primary.asc() if direction == "asc" else primary.desc()
    # Always tie-break on most recent first so the order is deterministic.
    query = query.order_by(primary, desc(Voicemail.received_at), desc(Voicemail.id))

    # Eager-load callbacks (and their assignee users) and team so the new
    # columns don't trigger N+1 queries.
    query = query.options(
        selectinload(Voicemail.callbacks).selectinload(Callback.assignee),
        selectinload(Voicemail.team),
    )

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    categories = Category.query.order_by(Category.name).all()

    # Teams the user can pick from in the filter dropdown.
    if is_unrestricted(current_user):
        available_teams = Team.query.order_by(Team.name).all()
    else:
        ids = user_team_ids(current_user)
        available_teams = (
            Team.query.filter(Team.id.in_(ids)).order_by(Team.name).all()
            if ids else []
        )

    # Admins/supervisors get the full team list for the bulk-assign action bar.
    bulk_assign_teams = []
    if current_user.is_admin or current_user.is_supervisor:
        bulk_assign_teams = Team.query.order_by(Team.name).all()
    # Pass can_bulk as a top-level template var so every Jinja block (content
    # AND scripts) can see it. A `{% set %}` declared inside a block is scoped
    # to that block and won't be visible in {% block scripts %}.
    can_bulk = bool(bulk_assign_teams) and (current_user.is_admin or current_user.is_supervisor)

    return render_template(
        "voicemails.html",
        voicemails=pagination.items,
        pagination=pagination,
        categories=categories,
        available_teams=available_teams,
        bulk_assign_teams=bulk_assign_teams,
        can_bulk=can_bulk,
        team_filter=team_filter,
        q=q,
        category_id=category_id,
        urgency=urgency,
        date_from=date_from,
        date_to=date_to,
        sort=sort,
        sort_dir=direction,
    )


@main_bp.route("/voicemails/<int:vm_id>")
@login_required
def voicemail_detail(vm_id):
    vm = Voicemail.query.get_or_404(vm_id)
    if not can_view_voicemail(vm, current_user):
        abort(403)
    q = request.args.get("q", "").strip()
    # Active users who can be assigned a callback (admin/supervisor/agent),
    # for the assignment dropdown shown to admins/supervisors.
    assignable_users = []
    if current_user.can_assign_callbacks:
        assignable_users = (
            User.query
            .filter(User.is_active.is_(True), User.role.in_(("admin", "supervisor", "agent")))
            .order_by(User.name)
            .all()
        )
    # All teams (admins/supervisors only) for the manual-override dropdown.
    all_teams = []
    if current_user.is_admin or current_user.is_supervisor:
        all_teams = Team.query.order_by(Team.name).all()
    return render_template(
        "voicemail_detail.html",
        vm=vm, q=q,
        assignable_users=assignable_users,
        all_teams=all_teams,
    )


# ---------------------------------------------------------------------------
# Manual team override (admin/supervisor only)
# ---------------------------------------------------------------------------

@main_bp.route("/voicemails/<int:vm_id>/team", methods=["POST"])
@login_required
def voicemail_set_team(vm_id):
    if not (current_user.is_admin or current_user.is_supervisor):
        abort(403)
    vm = Voicemail.query.get_or_404(vm_id)
    raw = request.form.get("team_id", "").strip()
    if raw == "" or raw == "none":
        vm.team_id = None
        vm.team_locked = False  # release the lock so auto-routing can run
        flash("Team cleared. Auto-routing rules will apply on the next change.", "success")
    else:
        try:
            tid = int(raw)
        except ValueError:
            flash("Invalid team selection.", "error")
            return redirect(url_for("main.voicemail_detail", vm_id=vm.id))
        team = Team.query.get(tid)
        if not team:
            flash("Team not found.", "error")
            return redirect(url_for("main.voicemail_detail", vm_id=vm.id))
        vm.team_id = team.id
        vm.team_locked = True
        flash(f"Voicemail manually assigned to {team.name}.", "success")
    db.session.commit()
    return redirect(url_for("main.voicemail_detail", vm_id=vm.id))


# ---------------------------------------------------------------------------
# Bulk team assignment (admin/supervisor only)
# ---------------------------------------------------------------------------

@main_bp.route("/voicemails/bulk/team", methods=["POST"])
@login_required
def voicemails_bulk_set_team():
    if not (current_user.is_admin or current_user.is_supervisor):
        abort(403)

    raw_ids = request.form.getlist("vm_ids")
    vm_ids = []
    for r in raw_ids:
        try:
            vm_ids.append(int(r))
        except (TypeError, ValueError):
            continue
    if not vm_ids:
        flash("No voicemails selected.", "error")
        return redirect(request.referrer or url_for("main.voicemail_list"))

    raw = request.form.get("team_id", "").strip()
    target_team = None
    clearing = raw == "" or raw == "none"
    if not clearing:
        try:
            tid = int(raw)
        except ValueError:
            flash("Invalid team selection.", "error")
            return redirect(request.referrer or url_for("main.voicemail_list"))
        target_team = Team.query.get(tid)
        if not target_team:
            flash("Team not found.", "error")
            return redirect(request.referrer or url_for("main.voicemail_list"))

    # Only operate on voicemails the user can actually see (defence in depth —
    # admin/supervisor see all anyway, but we apply scope_voicemails so this
    # endpoint behaves consistently if the role check is ever relaxed).
    base_q = Voicemail.query.filter(Voicemail.id.in_(vm_ids))
    base_q = scope_voicemails(base_q, current_user)
    vms = base_q.all()

    updated = 0
    for vm in vms:
        if clearing:
            vm.team_id = None
            vm.team_locked = False
        else:
            vm.team_id = target_team.id
            vm.team_locked = True
        updated += 1
    if updated:
        db.session.commit()

    if clearing:
        flash(f"Cleared team on {updated} voicemail{'s' if updated != 1 else ''}. Auto-routing will re-evaluate.", "success")
    else:
        flash(f"Assigned {updated} voicemail{'s' if updated != 1 else ''} to {target_team.name}.", "success")
    return redirect(request.referrer or url_for("main.voicemail_list"))


# ---------------------------------------------------------------------------
# Voicemail notes (any signed-in user can post)
# ---------------------------------------------------------------------------

@main_bp.route("/voicemails/<int:vm_id>/notes", methods=["POST"])
@login_required
def add_voicemail_note(vm_id):
    vm = Voicemail.query.get_or_404(vm_id)
    body = request.form.get("body", "").strip()
    if not body:
        flash("Note cannot be empty.", "error")
    elif len(body) > 5000:
        flash("Note is too long (max 5000 characters).", "error")
    else:
        note = VoicemailNote(voicemail_id=vm.id, author_id=current_user.id, body=body)
        db.session.add(note)
        db.session.commit()
    return redirect(url_for("main.voicemail_detail", vm_id=vm.id) + "#notes")


@main_bp.route("/voicemails/<int:vm_id>/notes/<int:note_id>/delete", methods=["POST"])
@login_required
def delete_voicemail_note(vm_id, note_id):
    note = VoicemailNote.query.get_or_404(note_id)
    if note.voicemail_id != vm_id:
        abort(404)
    # Author, admins, and supervisors can delete a note.
    if note.author_id != current_user.id and not current_user.is_admin and not current_user.is_supervisor:
        abort(403)
    db.session.delete(note)
    db.session.commit()
    return redirect(url_for("main.voicemail_detail", vm_id=vm_id) + "#notes")


@main_bp.route("/analytics")
@login_required
def analytics():
    now = datetime.utcnow()
    week_ago   = now - timedelta(days=7)
    month_ago  = now - timedelta(days=30)

    total        = Voicemail.query.count()
    week_count   = Voicemail.query.filter(Voicemail.received_at >= week_ago).count()
    month_count  = Voicemail.query.filter(Voicemail.received_at >= month_ago).count()
    urgent_count = Voicemail.query.filter_by(is_urgent=True).count()

    # Average duration (seconds)
    avg_dur_row = db.session.query(func.avg(Voicemail.duration)).filter(
        Voicemail.duration.isnot(None)
    ).scalar()
    avg_duration = round(avg_dur_row or 0)

    # 30-day daily trend
    daily_rows = (
        db.session.query(
            func.date(Voicemail.received_at).label("day"),
            func.count(Voicemail.id).label("cnt"),
        )
        .filter(Voicemail.received_at >= month_ago)
        .group_by(func.date(Voicemail.received_at))
        .order_by("day")
        .all()
    )
    daily_trend = [{"day": str(r.day), "count": r.cnt} for r in daily_rows]

    # Sentiment distribution
    sentiment_rows = (
        db.session.query(Insight.sentiment, func.count(Insight.id))
        .group_by(Insight.sentiment)
        .all()
    )
    sentiment_dist = {s or "neutral": c for s, c in sentiment_rows}

    # Category distribution
    cat_rows = (
        db.session.query(Category.name, func.count(Voicemail.id))
        .join(Voicemail, Voicemail.category_id == Category.id, isouter=True)
        .group_by(Category.name)
        .order_by(func.count(Voicemail.id).desc())
        .all()
    )
    category_dist = [{"name": n, "count": c} for n, c in cat_rows if c > 0]

    # Top 20 keywords with frequency
    all_kw: list = []
    for ins in Insight.query.filter(Insight.keywords.isnot(None)).all():
        if ins.keywords:
            all_kw.extend(_filter_keywords(ins.keywords))
    kw_counter = Counter(all_kw)
    top_keywords = [{"word": w, "count": c} for w, c in kw_counter.most_common(20)]

    # Hourly call distribution (0-23)
    hour_rows = (
        db.session.query(
            func.extract("hour", Voicemail.received_at).label("hr"),
            func.count(Voicemail.id).label("cnt"),
        )
        .filter(Voicemail.received_at.isnot(None))
        .group_by("hr")
        .order_by("hr")
        .all()
    )
    hourly = {int(r.hr): r.cnt for r in hour_rows}
    hourly_dist = [{"hour": h, "count": hourly.get(h, 0)} for h in range(24)]

    # Urgency keywords across all insights
    urg_kw: list = []
    for ins in Insight.query.filter(Insight.urgency_keywords.isnot(None)).all():
        if ins.urgency_keywords:
            urg_kw.extend(ins.urgency_keywords)
    top_urgency_kw = [{"word": w, "count": c} for w, c in Counter(urg_kw).most_common(10)]

    # Processing status breakdown
    status_rows = (
        db.session.query(Voicemail.processing_status, func.count(Voicemail.id))
        .group_by(Voicemail.processing_status)
        .all()
    )
    status_dist = {s: c for s, c in status_rows}

    # Latest cached AI insight (generated hourly by the background scheduler).
    from app.services.insights_service import get_latest_insight
    latest_insight = get_latest_insight()

    return render_template(
        "analytics.html",
        total=total,
        week_count=week_count,
        month_count=month_count,
        urgent_count=urgent_count,
        avg_duration=avg_duration,
        daily_trend=daily_trend,
        sentiment_dist=sentiment_dist,
        category_dist=category_dist,
        top_keywords=top_keywords,
        hourly_dist=hourly_dist,
        top_urgency_kw=top_urgency_kw,
        status_dist=status_dist,
        latest_insight=latest_insight,
    )


@main_bp.route("/analytics/insights")
@login_required
def analytics_insights():
    """
    Return the most recent cached AI insight as JSON. The actual generation
    runs hourly in a background scheduler (see app.services.insights_service),
    so this endpoint is instant and never blocks on Ollama.
    """
    from app.services.insights_service import get_latest_insight
    row = get_latest_insight()
    if row is None:
        return jsonify({
            "status":       "pending",
            "text":         None,
            "generated_at": None,
            "error":        None,
        })
    return jsonify({
        "status":       row.status,
        "text":         row.text,
        "generated_at": row.generated_at.isoformat() + "Z" if row.generated_at else None,
        "error":        row.error_message,
    })


@main_bp.route("/voicemails/poll")
@login_required
def voicemail_poll():
    """
    Lightweight endpoint for live-update polling.
    Returns the total count plus the most-recently-created voicemail's id and
    status. An optional ?id= param also returns the status of a specific
    voicemail (used by the detail page while transcription is in progress).
    """
    latest = Voicemail.query.order_by(desc(Voicemail.created_at)).first()
    payload = {
        "total": Voicemail.query.count(),
        "latest_id": latest.id if latest else None,
        "latest_status": latest.processing_status if latest else None,
    }
    # Detail-page targeted check.
    vm_id = request.args.get("id", type=int)
    if vm_id:
        vm = Voicemail.query.get(vm_id)
        payload["vm_status"] = vm.processing_status if vm else None
    return jsonify(payload)


@main_bp.route("/voicemails/<int:vm_id>/delete", methods=["POST"])
@login_required
def voicemail_delete(vm_id):
    # Only admins and supervisors can delete voicemails — this also cascades
    # to callbacks and notes, so it must be tightly restricted.
    if not (current_user.is_admin or current_user.is_supervisor):
        flash("You don't have permission to delete voicemails.", "error")
        return redirect(url_for("main.voicemail_detail", vm_id=vm_id))
    vm = Voicemail.query.get_or_404(vm_id)
    # Delete audio files from disk
    for path in (vm.original_path, vm.converted_path):
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except Exception as e:
                current_app.logger.warning(f"Could not delete file {path}: {e}")
    db.session.delete(vm)
    db.session.commit()
    return redirect(url_for("main.voicemail_list"))


@main_bp.route("/voicemails/<int:vm_id>/status", methods=["POST"])
@login_required
def voicemail_set_status(vm_id):
    vm = Voicemail.query.get_or_404(vm_id)
    new_status = request.form.get("status", "").strip()
    allowed = {"pending", "processing", "completed", "error"}
    if new_status in allowed:
        vm.processing_status = new_status
        db.session.commit()
    return redirect(url_for("main.voicemail_detail", vm_id=vm_id))


@main_bp.route("/voicemails/<int:vm_id>/audio")
@login_required
def serve_audio(vm_id):
    import mimetypes
    vm = Voicemail.query.get_or_404(vm_id)
    if not can_view_voicemail(vm, current_user):
        abort(403)
    # Prefer converted WAV; fall back to original
    audio_path = vm.converted_path or vm.original_path
    if not audio_path:
        abort(404)

    # Flask 2.x requires an absolute path for send_file
    if not os.path.isabs(audio_path):
        # Resolve relative to the app root (parent of the 'app' package)
        base = os.path.dirname(current_app.root_path)
        audio_path = os.path.join(base, audio_path)

    if not os.path.isfile(audio_path):
        current_app.logger.warning(f"Audio file not found: {audio_path}")
        abort(404)

    mime = mimetypes.guess_type(audio_path)[0] or "audio/mpeg"
    try:
        return send_file(
            audio_path,
            mimetype=mime,
            conditional=True,   # supports Range requests for seeking
        )
    except Exception as e:
        current_app.logger.error(f"Audio serve error: {e}")
        abort(500)
