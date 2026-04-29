import os
from flask import Blueprint, render_template, request, redirect, url_for, abort, send_file, current_app
from flask_login import login_required
from sqlalchemy import func, desc, or_
from datetime import datetime, timedelta
from collections import Counter

from app import db
from app.models.voicemail import Voicemail, Transcript, Insight, Category

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
@login_required
def dashboard():
    today = datetime.utcnow().date()
    week_ago = datetime.utcnow() - timedelta(days=7)

    total = Voicemail.query.count()
    today_count = Voicemail.query.filter(
        func.date(Voicemail.received_at) == today
    ).count()
    urgent_count = Voicemail.query.filter_by(is_urgent=True).count()

    category_dist = (
        db.session.query(Category.name, func.count(Voicemail.id))
        .join(Voicemail, Voicemail.category_id == Category.id, isouter=True)
        .group_by(Category.name)
        .all()
    )

    daily_trend = [
        {"day": str(d), "count": c}
        for d, c in db.session.query(
            func.date(Voicemail.received_at).label("day"),
            func.count(Voicemail.id).label("count"),
        )
        .filter(Voicemail.received_at >= week_ago)
        .group_by(func.date(Voicemail.received_at))
        .order_by("day")
        .all()
    ]

    all_keywords = []
    insights_with_kw = Insight.query.filter(Insight.keywords.isnot(None)).limit(100).all()
    for ins in insights_with_kw:
        if ins.keywords:
            all_keywords.extend(ins.keywords)
    top_keywords = [kw for kw, _ in Counter(all_keywords).most_common(15)]

    recent = (
        Voicemail.query.order_by(desc(Voicemail.created_at)).limit(5).all()
    )

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

    query = Voicemail.query.join(Transcript, Voicemail.id == Transcript.voicemail_id, isouter=True)

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

    pagination = query.order_by(desc(Voicemail.received_at)).paginate(
        page=page, per_page=per_page, error_out=False
    )

    categories = Category.query.order_by(Category.name).all()

    return render_template(
        "voicemails.html",
        voicemails=pagination.items,
        pagination=pagination,
        categories=categories,
        q=q,
        category_id=category_id,
        urgency=urgency,
        date_from=date_from,
        date_to=date_to,
    )


@main_bp.route("/voicemails/<int:vm_id>")
@login_required
def voicemail_detail(vm_id):
    vm = Voicemail.query.get_or_404(vm_id)
    q = request.args.get("q", "").strip()
    return render_template("voicemail_detail.html", vm=vm, q=q)


@main_bp.route("/voicemails/<int:vm_id>/delete", methods=["POST"])
@login_required
def voicemail_delete(vm_id):
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
