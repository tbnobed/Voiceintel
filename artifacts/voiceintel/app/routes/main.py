import os
import re
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
    # All times in DB are stored as naive UTC (datetime.utcnow). For the
    # "Today" tile and the daily-trend chart we want a calendar day in the
    # display timezone (DISPLAY_TZ, default America/Chicago) so a 9pm
    # Central voicemail lands on the right local day, not the next UTC day.
    tz_name = os.environ.get("DISPLAY_TZ", "America/Chicago")
    try:
        from zoneinfo import ZoneInfo
        local_now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        # Bad DISPLAY_TZ: fall back to UTC for both the Python "now" and the
        # SQL conversion below, so the tile and the trend chart still agree.
        current_app.logger.warning("Invalid DISPLAY_TZ %r — falling back to UTC", tz_name)
        tz_name = "UTC"
        local_now = datetime.utcnow()
    today = local_now.date()
    week_ago = datetime.utcnow() - timedelta(days=7)

    # SQL expression that converts the stored UTC timestamp into the
    # configured display timezone; used for date-bucketing "today" and the
    # 7-day trend so both agree.
    received_local = func.timezone(tz_name, func.timezone("UTC", Voicemail.received_at))

    # Scope all dashboard queries to the voicemails this user is allowed to
    # see — agents/viewers only see their teams (+ unrouted), supervisors and
    # admins see everything.
    base = scope_voicemails(Voicemail.query, current_user)

    total = base.count()
    today_count = scope_voicemails(
        Voicemail.query.filter(func.date(received_local) == today),
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

    # `received_local` (defined above) buckets by display timezone so a
    # 9pm Central call lands on the right day, not the next UTC day.
    trend_q = db.session.query(
        func.date(received_local).label("day"),
        func.count(Voicemail.id).label("count"),
    ).filter(Voicemail.received_at >= week_ago)
    trend_q = scope_voicemails(trend_q, current_user)
    daily_trend = [
        {"day": str(d), "count": c}
        for d, c in trend_q.group_by(func.date(received_local))
                           .order_by("day").all()
    ]

    # Keywords from insights — scope by joining to voicemails so soft-deleted
    # rows (and team scoping) are honoured uniformly for every role.
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

    # Sentiment distribution — joined to Voicemail so we can apply scoping.
    sent_q = (
        db.session.query(Insight.sentiment, func.count(Insight.id))
        .join(Voicemail, Voicemail.id == Insight.voicemail_id)
        .filter(Insight.sentiment.isnot(None))
    )
    sent_q = scope_voicemails(sent_q, current_user)
    sentiment_dist = {
        (s or "neutral"): c for s, c in sent_q.group_by(Insight.sentiment).all()
    }

    return render_template(
        "dashboard.html",
        total=total,
        today_count=today_count,
        urgent_count=urgent_count,
        category_dist=category_dist,
        daily_trend=daily_trend,
        top_keywords=top_keywords,
        sentiment_dist=sentiment_dist,
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
    # Sentiment filter — drives drill-down from the Analytics doughnut chart.
    # Whitelist to the values the NLP service emits.
    sentiment = request.args.get("sentiment", "").strip().lower()
    if sentiment not in ("positive", "negative", "neutral"):
        sentiment = ""

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
        # Search across transcript text, subject (carrier puts the caller's
        # phone + name here), and sender. Subject + sender matter for the
        # Frequent Callers analytics card, which links to ?q=<digits>.
        like = f"%{q}%"
        clauses = [
            Transcript.text.ilike(like),
            Voicemail.subject.ilike(like),
            Voicemail.sender.ilike(like),
        ]
        # If the query looks like a phone number (mostly digits, length ≥7),
        # also match against the digit-stripped subject/sender so a query
        # like "5305728897" finds subjects containing "(530) 572-8897" or
        # "+1 530-572-8897". Postgres regexp_replace is used here.
        q_digits = re.sub(r"\D+", "", q)
        if len(q_digits) >= 7 and len(q_digits) >= len(q) - 4:
            digit_pat = f"%{q_digits}%"
            clauses.append(
                func.regexp_replace(Voicemail.subject, r"\D", "", "g").ilike(digit_pat)
            )
            clauses.append(
                func.regexp_replace(Voicemail.sender, r"\D", "", "g").ilike(digit_pat)
            )
        query = query.filter(or_(*clauses))

    if category_id:
        query = query.filter(Voicemail.category_id == category_id)

    if sentiment:
        # Insight rows are 1:1 with voicemails. Use an inner join so we only
        # match VMs that actually have the requested sentiment recorded.
        # 'neutral' also matches rows where sentiment is NULL, since the
        # analytics chart bucket-counts NULL → "neutral".
        query = query.join(Insight, Insight.voicemail_id == Voicemail.id, isouter=True)
        if sentiment == "neutral":
            query = query.filter(
                (Insight.sentiment == "neutral") | (Insight.sentiment.is_(None))
            )
        else:
            query = query.filter(Insight.sentiment == sentiment)

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

    # Admins see every team in the bulk-assign action bar; supervisors are
    # restricted to teams they belong to.
    bulk_assign_teams = []
    if current_user.is_admin:
        bulk_assign_teams = Team.query.order_by(Team.name).all()
    elif current_user.is_supervisor:
        ids = user_team_ids(current_user)
        bulk_assign_teams = (
            Team.query.filter(Team.id.in_(ids)).order_by(Team.name).all()
            if ids else []
        )
    # Pass can_bulk as a top-level template var so every Jinja block (content
    # AND scripts) can see it. A `{% set %}` declared inside a block is scoped
    # to that block and won't be visible in {% block scripts %}.
    # `can_bulk` gates the row checkboxes and action bar — anyone who can do
    # *any* bulk op (assign team OR delete) needs them.
    can_bulk_assign = bool(bulk_assign_teams) and (current_user.is_admin or current_user.is_supervisor)
    can_bulk_delete = current_user.is_admin or current_user.is_supervisor
    can_bulk = can_bulk_assign or can_bulk_delete

    return render_template(
        "voicemails.html",
        voicemails=pagination.items,
        pagination=pagination,
        categories=categories,
        available_teams=available_teams,
        bulk_assign_teams=bulk_assign_teams,
        can_bulk=can_bulk,
        can_bulk_assign=can_bulk_assign,
        can_bulk_delete=can_bulk_delete,
        team_filter=team_filter,
        q=q,
        category_id=category_id,
        urgency=urgency,
        sentiment=sentiment,
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
    # for the assignment dropdown shown to admins/supervisors. Supervisors
    # only see themselves + active agents on one of their teams.
    assignable_users = []
    if current_user.can_assign_callbacks:
        if current_user.is_admin:
            assignable_users = (
                User.query
                .filter(User.is_active.is_(True), User.role.in_(("admin", "supervisor", "agent")))
                .order_by(User.name)
                .all()
            )
        else:
            sup_team_ids = user_team_ids(current_user)
            base = User.query.filter(User.is_active.is_(True))
            if sup_team_ids:
                shared_agents = (
                    base.filter(User.role == "agent")
                        .filter(User.teams.any(Team.id.in_(sup_team_ids)))
                )
                # Always include the supervisor themselves.
                assignable_users = (
                    shared_agents.union(base.filter(User.id == current_user.id))
                                 .order_by(User.name)
                                 .all()
                )
            else:
                assignable_users = base.filter(User.id == current_user.id).all()
    # Teams shown in the manual-override dropdown. Admins see every team;
    # supervisors are restricted to teams they belong to.
    all_teams = []
    if current_user.is_admin:
        all_teams = Team.query.order_by(Team.name).all()
    elif current_user.is_supervisor:
        ids = user_team_ids(current_user)
        all_teams = (
            Team.query.filter(Team.id.in_(ids)).order_by(Team.name).all()
            if ids else []
        )
    return render_template(
        "voicemail_detail.html",
        vm=vm, q=q,
        assignable_users=assignable_users,
        all_teams=all_teams,
    )


# ---------------------------------------------------------------------------
# Manual team override (admin/supervisor only)
# ---------------------------------------------------------------------------

@main_bp.route("/voicemails/<int:vm_id>/ai-summary", methods=["POST"])
@login_required
def voicemail_regenerate_ai_summary(vm_id):
    """
    Re-run the per-voicemail AI summary on demand. Available to anyone who
    can view the voicemail; the cost is small (~3-5s of GPU time) and the
    feature is most useful to the agent reading the voicemail.

    Returns JSON so the page can update inline without a full reload.
    """
    vm = Voicemail.query.get_or_404(vm_id)
    if not can_view_voicemail(vm, current_user):
        abort(403)
    # Read-only viewers can see summaries that the pipeline produced
    # automatically, but should not be able to trigger expensive GPU work.
    if not (current_user.is_admin or current_user.is_supervisor or current_user.is_agent):
        abort(403)
    if not vm.transcript or not vm.transcript.text:
        return jsonify({"ok": False, "error": "No transcript available to summarise."}), 400

    # Cheap in-flight debounce: if another regeneration was kicked off less than
    # 60 seconds ago for this voicemail, refuse rather than burn GPU twice and
    # risk last-write-wins stomping. The pipeline path doesn't go through here,
    # so it's not affected.
    ins = vm.insights
    if ins and ins.ai_status == "pending" and ins.ai_generated_at:
        age = (datetime.utcnow() - ins.ai_generated_at).total_seconds()
        if 0 <= age < 60:
            return jsonify({
                "ok": False,
                "error": "A summary is already being generated. Please wait a few seconds.",
            }), 409

    # Mark pending so a concurrent caller sees the debounce window. Use a
    # separate transaction so the marker is durable before the slow model call.
    from sqlalchemy.exc import IntegrityError
    from app.models.voicemail import Insight as _Insight
    if ins is None:
        try:
            ins = _Insight(voicemail_id=vm.id, ai_status="pending", ai_generated_at=datetime.utcnow())
            db.session.add(ins)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            ins = _Insight.query.filter_by(voicemail_id=vm.id).one()
            ins.ai_status = "pending"
            ins.ai_generated_at = datetime.utcnow()
            db.session.commit()
    else:
        ins.ai_status = "pending"
        ins.ai_generated_at = datetime.utcnow()
        db.session.commit()

    try:
        from app.services import ai_summary_service
        result = ai_summary_service.generate_and_store(vm)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"AI summary regen failed for vm {vm.id}: {e}", exc_info=True)
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500

    if result.get("status") != "success":
        return jsonify({
            "ok": False,
            "error": result.get("error_message") or "Model returned no usable output.",
            "status": result.get("status"),
        }), 502

    return jsonify({
        "ok": True,
        "status": "success",
        "summary":            result.get("summary"),
        "intent":             result.get("intent"),
        "action_items":       result.get("action_items") or [],
        "suggested_response": result.get("suggested_response"),
        "duration_ms":        result.get("duration_ms"),
    })


@main_bp.route("/voicemails/<int:vm_id>/team", methods=["POST"])
@login_required
def voicemail_set_team(vm_id):
    if not (current_user.is_admin or current_user.is_supervisor):
        abort(403)
    vm = Voicemail.query.get_or_404(vm_id)
    # Supervisors can only re-team voicemails they're allowed to see.
    if not can_view_voicemail(vm, current_user):
        abort(403)
    # Supervisors can only assign to teams they belong to (and can only clear
    # a voicemail that's currently on one of their teams or unrouted, which
    # the can_view check above already enforces).
    sup_team_ids = None
    if current_user.is_supervisor and not current_user.is_admin:
        sup_team_ids = set(user_team_ids(current_user))
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
        if sup_team_ids is not None and team.id not in sup_team_ids:
            flash("You can only assign voicemails to teams you belong to.", "error")
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
        # Supervisors can only assign to teams they belong to.
        if (
            current_user.is_supervisor and not current_user.is_admin
            and target_team.id not in set(user_team_ids(current_user))
        ):
            flash("You can only assign voicemails to teams you belong to.", "error")
            return redirect(request.referrer or url_for("main.voicemail_list"))

    # Only operate on voicemails the user can actually see — admins see all,
    # supervisors are now restricted to their own teams (+ unrouted). We lock
    # the rows for the duration of the transaction (SELECT ... FOR UPDATE)
    # and re-evaluate the scope predicate inside the lock so a voicemail that
    # races out of the supervisor's scope between selection and commit cannot
    # be overwritten.
    base_q = Voicemail.query.filter(Voicemail.id.in_(vm_ids))
    base_q = scope_voicemails(base_q, current_user).with_for_update()
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
    else:
        db.session.rollback()

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
    if not can_view_voicemail(vm, current_user):
        abort(403)
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
    # Must be allowed to see the parent voicemail in the first place.
    if not can_view_voicemail(note.voicemail, current_user):
        abort(403)
    # Author, admins, and supervisors can delete a note (supervisors only on
    # voicemails they can see — already enforced above).
    if note.author_id != current_user.id and not current_user.is_admin and not current_user.is_supervisor:
        abort(403)
    db.session.delete(note)
    db.session.commit()
    return redirect(url_for("main.voicemail_detail", vm_id=vm_id) + "#notes")


@main_bp.route("/analytics")
@login_required
def analytics():
    # Analytics is restricted to admins and supervisors. Agents and viewers
    # don't have visibility across the full inbox so the cross-team metrics
    # would be misleading for them.
    if not (current_user.is_admin or current_user.is_supervisor):
        flash("You don't have permission to view analytics.", "error")
        return redirect(url_for("main.dashboard"))

    now = datetime.utcnow()
    week_ago   = now - timedelta(days=7)
    month_ago  = now - timedelta(days=30)

    # Bucket analytics by the user-facing display timezone, not UTC. Voicemails
    # are stored as naive UTC, so we re-label as UTC and convert to DISPLAY_TZ
    # before extracting day/hour. Without this, a 9pm Central call shows up
    # under "2 AM" on the hour chart.
    tz_name = os.environ.get("DISPLAY_TZ", "America/Chicago")
    received_local = func.timezone(tz_name, func.timezone("UTC", Voicemail.received_at))

    # Every aggregation below is scoped to the voicemails this user is allowed
    # to see — admins see everything, supervisors see only their teams (+
    # unrouted). For Insight-based aggregations we join through Voicemail so
    # scope_voicemails can apply its team filter.
    total        = scope_voicemails(Voicemail.query, current_user).count()
    week_count   = scope_voicemails(
        Voicemail.query.filter(Voicemail.received_at >= week_ago), current_user,
    ).count()
    month_count  = scope_voicemails(
        Voicemail.query.filter(Voicemail.received_at >= month_ago), current_user,
    ).count()
    urgent_count = scope_voicemails(
        Voicemail.query.filter_by(is_urgent=True), current_user,
    ).count()

    # Average duration (seconds)
    avg_dur_q = db.session.query(func.avg(Voicemail.duration)).filter(
        Voicemail.duration.isnot(None)
    )
    avg_dur_q = scope_voicemails(avg_dur_q, current_user)
    avg_duration = round(avg_dur_q.scalar() or 0)

    # 30-day daily trend (bucketed in DISPLAY_TZ)
    daily_q = (
        db.session.query(
            func.date(received_local).label("day"),
            func.count(Voicemail.id).label("cnt"),
        )
        .filter(Voicemail.received_at >= month_ago)
    )
    daily_q = scope_voicemails(daily_q, current_user)
    daily_rows = daily_q.group_by(func.date(received_local)).order_by("day").all()
    daily_trend = [{"day": str(r.day), "count": r.cnt} for r in daily_rows]

    # Sentiment distribution — joined to Voicemail for scoping.
    sent_q = (
        db.session.query(Insight.sentiment, func.count(Insight.id))
        .join(Voicemail, Voicemail.id == Insight.voicemail_id)
    )
    sent_q = scope_voicemails(sent_q, current_user)
    sentiment_rows = sent_q.group_by(Insight.sentiment).all()
    sentiment_dist = {s or "neutral": c for s, c in sentiment_rows}

    # Category distribution — include the id so the analytics page can link
    # each row to /voicemails?category=<id> for drill-down.
    cat_q = (
        db.session.query(Category.id, Category.name, func.count(Voicemail.id))
        .join(Voicemail, Voicemail.category_id == Category.id, isouter=True)
    )
    cat_q = scope_voicemails(cat_q, current_user)
    cat_rows = (
        cat_q.group_by(Category.id, Category.name)
             .order_by(func.count(Voicemail.id).desc())
             .all()
    )
    category_dist = [
        {"id": cid, "name": n, "count": c} for cid, n, c in cat_rows if c > 0
    ]

    # Top 20 keywords with frequency — scope insights via voicemail join.
    kw_q = (
        db.session.query(Insight)
        .join(Voicemail, Voicemail.id == Insight.voicemail_id)
        .filter(Insight.keywords.isnot(None))
    )
    kw_q = scope_voicemails(kw_q, current_user)
    all_kw: list = []
    for ins in kw_q.all():
        if ins.keywords:
            all_kw.extend(_filter_keywords(ins.keywords))
    kw_counter = Counter(all_kw)
    top_keywords = [{"word": w, "count": c} for w, c in kw_counter.most_common(20)]

    # Hourly call distribution (0-23, bucketed in DISPLAY_TZ)
    hour_q = (
        db.session.query(
            func.extract("hour", received_local).label("hr"),
            func.count(Voicemail.id).label("cnt"),
        )
        .filter(Voicemail.received_at.isnot(None))
    )
    hour_q = scope_voicemails(hour_q, current_user)
    hour_rows = hour_q.group_by("hr").order_by("hr").all()
    hourly = {int(r.hr): r.cnt for r in hour_rows}
    hourly_dist = [{"hour": h, "count": hourly.get(h, 0)} for h in range(24)]

    # Urgency keywords across all insights — scope via voicemail join.
    urg_q = (
        db.session.query(Insight)
        .join(Voicemail, Voicemail.id == Insight.voicemail_id)
        .filter(Insight.urgency_keywords.isnot(None))
    )
    urg_q = scope_voicemails(urg_q, current_user)
    urg_kw: list = []
    for ins in urg_q.all():
        if ins.urgency_keywords:
            urg_kw.extend(ins.urgency_keywords)
    top_urgency_kw = [{"word": w, "count": c} for w, c in Counter(urg_kw).most_common(10)]

    # Frequent callers — group voicemails by parsed caller phone number
    # (digits only, so "(555) 123-4567" and "555-123-4567" land in the
    # same bucket). Eager-load `insights` so `display_caller_name` (which
    # reads `vm.insights.ai_caller_name`) doesn't N+1 across hundreds of
    # rows. Voicemails with no parseable phone are skipped — without a
    # stable identifier we can't safely cluster them.
    fc_q = (
        db.session.query(Voicemail)
        .options(selectinload(Voicemail.insights))
        .filter(Voicemail.subject.isnot(None))
    )
    fc_q = scope_voicemails(fc_q, current_user)
    fc_groups: dict[str, dict] = {}
    for vm in fc_q.all():
        ci = vm.caller_info or {}
        phone_raw = (ci.get("phone") or "").strip()
        digits = re.sub(r"\D+", "", phone_raw)
        if not digits:
            continue
        g = fc_groups.setdefault(digits, {
            "phone": phone_raw,
            "name": None,
            "count": 0,
            "last_received": None,
            "last_vm_id": None,
        })
        g["count"] += 1
        if vm.received_at and (g["last_received"] is None or vm.received_at > g["last_received"]):
            g["last_received"] = vm.received_at
            g["last_vm_id"] = vm.id
        # First non-empty display name wins; display_caller_name already
        # prefers the AI-extracted name over generic carrier placeholders.
        if not g["name"]:
            nm = vm.display_caller_name
            if nm:
                g["name"] = nm
    # Only surface callers who have left at least this many voicemails. Keeps
    # the card focused on actually-frequent callers and avoids cluttering it
    # with one-offs.
    FREQUENT_CALLER_MIN_COUNT = 5
    # Sort by count desc, then most-recent desc as a deterministic tiebreaker.
    # Keep `last_received` as a raw datetime so the `localtime` Jinja filter
    # can render it in DISPLAY_TZ.
    frequent_callers = [
        {
            "phone": g["phone"],
            "phone_digits": d,
            "name": g["name"],
            "count": g["count"],
            "last_received": g["last_received"],
            "last_vm_id": g["last_vm_id"],
        }
        for d, g in sorted(
            (item for item in fc_groups.items() if item[1]["count"] >= FREQUENT_CALLER_MIN_COUNT),
            key=lambda item: (-item[1]["count"], -(item[1]["last_received"].timestamp() if item[1]["last_received"] else 0)),
        )[:10]
    ]

    # Processing status breakdown
    status_q = (
        db.session.query(Voicemail.processing_status, func.count(Voicemail.id))
    )
    status_q = scope_voicemails(status_q, current_user)
    status_rows = status_q.group_by(Voicemail.processing_status).all()
    status_dist = {s: c for s, c in status_rows}

    # Latest cached AI insight is generated from ALL voicemails globally by
    # the background scheduler, so we only show it to admins. Supervisors
    # would otherwise see themes derived from other teams' transcripts.
    from app.services.insights_service import get_latest_insight
    latest_insight = get_latest_insight() if current_user.is_admin else None

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
        frequent_callers=frequent_callers,
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

    The cached insight is generated from every transcript globally, so we
    only expose it to admins to avoid leaking other teams' themes to
    supervisors.
    """
    if not current_user.is_admin:
        abort(403)
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
    Lightweight endpoint for live-update polling. All values are scoped to
    voicemails the caller can see, so a supervisor's "new voicemail" toast
    only fires for activity on their teams.
    """
    scoped = scope_voicemails(Voicemail.query, current_user)
    latest = scoped.order_by(desc(Voicemail.created_at)).first()
    payload = {
        "total": scoped.count(),
        "latest_id": latest.id if latest else None,
        "latest_status": latest.processing_status if latest else None,
    }
    # Detail-page targeted check — only return status for voicemails the
    # caller is allowed to see; otherwise pretend it doesn't exist.
    vm_id = request.args.get("id", type=int)
    if vm_id:
        vm = Voicemail.query.get(vm_id)
        payload["vm_status"] = (
            vm.processing_status
            if vm and can_view_voicemail(vm, current_user)
            else None
        )
    return jsonify(payload)


@main_bp.route("/voicemails/<int:vm_id>/delete", methods=["POST"])
@login_required
def voicemail_delete(vm_id):
    # Soft-delete: mark the voicemail as moved to the admin Deleted folder.
    # Audio files and DB rows are preserved so an admin can restore it. Only
    # admins and supervisors can move voicemails to the trash; anyone else
    # gets bounced. Supervisors are scoped to voicemails they can see.
    if not (current_user.is_admin or current_user.is_supervisor):
        flash("You don't have permission to delete voicemails.", "error")
        return redirect(url_for("main.voicemail_detail", vm_id=vm_id))
    vm = Voicemail.query.get_or_404(vm_id)
    if not can_view_voicemail(vm, current_user):
        abort(403)
    if vm.deleted_at is None:
        vm.deleted_at = datetime.utcnow()
        vm.deleted_by_id = current_user.id
        db.session.commit()
    flash("Voicemail moved to the Deleted folder.", "success")
    return redirect(url_for("main.voicemail_list"))


@main_bp.route("/voicemails/bulk/delete", methods=["POST"])
@login_required
def voicemails_bulk_delete():
    """
    Soft-delete every selected voicemail in one shot. Mirrors the permission
    rules of the per-row delete: admins and supervisors only, scoped to rows
    they can actually see.
    """
    if not (current_user.is_admin or current_user.is_supervisor):
        abort(403)
    raw_ids = request.form.getlist("vm_ids")
    vm_ids = []
    for raw in raw_ids:
        try:
            vm_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    if not vm_ids:
        flash("No voicemails selected.", "error")
        return redirect(request.referrer or url_for("main.voicemail_list"))

    # SELECT ... FOR UPDATE inside the user's scope so a supervisor cannot
    # trash a row that races out of their team mid-request.
    base_q = Voicemail.query.filter(Voicemail.id.in_(vm_ids))
    base_q = scope_voicemails(base_q, current_user).with_for_update()
    vms = base_q.all()

    now = datetime.utcnow()
    deleted = 0
    for vm in vms:
        if vm.deleted_at is None:
            vm.deleted_at = now
            vm.deleted_by_id = current_user.id
            deleted += 1
    if deleted:
        db.session.commit()
    else:
        db.session.rollback()
    flash(
        f"Moved {deleted} voicemail{'s' if deleted != 1 else ''} to the Deleted folder.",
        "success",
    )
    return redirect(request.referrer or url_for("main.voicemail_list"))


# ---------------------------------------------------------------------------
# Admin-only Deleted folder (list / restore / permanently purge)
# ---------------------------------------------------------------------------

@main_bp.route("/voicemails/deleted")
@login_required
def voicemails_deleted():
    if not current_user.is_admin:
        abort(403)
    page = request.args.get("page", 1, type=int)
    per_page = 25
    pagination = (
        scope_voicemails(Voicemail.query, current_user, include_deleted=True)
        .filter(Voicemail.deleted_at.isnot(None))
        .order_by(desc(Voicemail.deleted_at))
        .paginate(page=page, per_page=per_page, error_out=False)
    )
    return render_template(
        "voicemails_deleted.html",
        voicemails=pagination.items,
        pagination=pagination,
    )


@main_bp.route("/voicemails/<int:vm_id>/restore", methods=["POST"])
@login_required
def voicemail_restore(vm_id):
    if not current_user.is_admin:
        abort(403)
    vm = Voicemail.query.get_or_404(vm_id)
    if vm.deleted_at is not None:
        vm.deleted_at = None
        vm.deleted_by_id = None
        db.session.commit()
        flash("Voicemail restored.", "success")
    return redirect(request.referrer or url_for("main.voicemails_deleted"))


@main_bp.route("/voicemails/<int:vm_id>/purge", methods=["POST"])
@login_required
def voicemail_purge(vm_id):
    """Permanently delete a soft-deleted voicemail — DB rows + audio files."""
    if not current_user.is_admin:
        abort(403)
    vm = Voicemail.query.get_or_404(vm_id)
    if vm.deleted_at is None:
        # Refuse to permanently delete something that hasn't been trashed
        # first — forces the explicit two-step flow.
        flash("Voicemail must be in the Deleted folder before it can be purged.", "error")
        return redirect(url_for("main.voicemail_detail", vm_id=vm.id))
    # Resolve relative audio paths the same way serve_audio() does so we
    # actually find the files no matter what cwd the worker is using.
    base = os.path.dirname(current_app.root_path)
    for path in (vm.original_path, vm.converted_path):
        if not path:
            continue
        full_path = path if os.path.isabs(path) else os.path.join(base, path)
        if os.path.isfile(full_path):
            try:
                os.remove(full_path)
            except Exception as e:
                current_app.logger.warning(f"Could not delete file {full_path}: {e}")
        else:
            current_app.logger.info(f"Purge: audio file already missing: {full_path}")
    db.session.delete(vm)
    db.session.commit()
    flash("Voicemail permanently deleted.", "success")
    return redirect(url_for("main.voicemails_deleted"))


@main_bp.route("/voicemails/<int:vm_id>/status", methods=["POST"])
@login_required
def voicemail_set_status(vm_id):
    # Status changes affect downstream pipeline behaviour, so restrict to
    # admins/supervisors and require they can see the voicemail.
    if not (current_user.is_admin or current_user.is_supervisor):
        abort(403)
    vm = Voicemail.query.get_or_404(vm_id)
    if not can_view_voicemail(vm, current_user):
        abort(403)
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
