import os
import json
from flask import Blueprint, render_template, request, redirect, url_for, abort, send_file, current_app, jsonify, Response, stream_with_context
from flask_login import login_required
from sqlalchemy import func, desc, or_
from datetime import datetime, timedelta
from collections import Counter

from app import db
from app.models.voicemail import Voicemail, Transcript, Insight, Category
from app.services.nlp_service import STOPWORDS


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
            all_keywords.extend(_filter_keywords(ins.keywords))
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
    )


def _sse(payload: dict) -> str:
    """Format a Server-Sent Events data frame."""
    return f"data: {json.dumps(payload)}\n\n"


@main_bp.route("/analytics/insights")
@login_required
def analytics_insights():
    """
    Stream a narrative summary of voicemail analytics from Phi-3 mini (local, via
    Ollama) as Server-Sent Events. Streaming keeps the connection alive past
    cold-start delays and lets the UI render tokens as they arrive.
    """
    ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")

    # Gather all analytics data inside the request context (before the generator
    # starts), so the stream itself doesn't need DB access.
    now       = datetime.utcnow()
    week_ago  = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    total = Voicemail.query.count()
    if total == 0:
        return Response(
            _sse({"error": "No voicemail data to analyse yet."}) + _sse({"done": True}),
            mimetype="text/event-stream",
        )

    urgent_count = Voicemail.query.filter_by(is_urgent=True).count()
    week_count   = Voicemail.query.filter(Voicemail.received_at >= week_ago).count()
    month_count  = Voicemail.query.filter(Voicemail.received_at >= month_ago).count()

    sentiment_rows = db.session.query(Insight.sentiment, func.count(Insight.id)).group_by(Insight.sentiment).all()
    sentiment_dist = {s or "neutral": c for s, c in sentiment_rows}

    cat_rows = (
        db.session.query(Category.name, func.count(Voicemail.id))
        .join(Voicemail, Voicemail.category_id == Category.id, isouter=True)
        .group_by(Category.name).order_by(func.count(Voicemail.id).desc()).limit(5).all()
    )

    all_kw: list = []
    for ins in Insight.query.filter(Insight.keywords.isnot(None)).limit(200).all():
        if ins.keywords:
            all_kw.extend(_filter_keywords(ins.keywords))
    top_kw = [w for w, _ in Counter(all_kw).most_common(15)]

    recent_vms = (
        Voicemail.query
        .join(Transcript, Voicemail.id == Transcript.voicemail_id)
        .filter(Transcript.text.isnot(None))
        .order_by(desc(Voicemail.received_at))
        .limit(12)
        .all()
    )
    snippets = []
    for vm in recent_vms:
        if vm.transcript and vm.transcript.text:
            snippets.append(f'- "{vm.transcript.text[:250]}"')

    data_block = (
        f"Total voicemails: {total}\n"
        f"Last 7 days: {week_count} | Last 30 days: {month_count}\n"
        f"Urgent: {urgent_count} ({round(urgent_count / total * 100)}%)\n"
        f"Sentiment breakdown: {dict(sentiment_dist)}\n"
        f"Top categories: {[f'{n} ({c})' for n, c in cat_rows]}\n"
        f"Top keywords: {top_kw}\n\n"
        f"Recent transcript excerpts (themes only — do not quote):\n"
        + "\n".join(snippets)
    )

    prompt = (
        "<|user|>\n"
        "You are a voicemail analyst. Based on the data below, write a concise analysis "
        "in exactly 4 short paragraphs with these headings:\n\n"
        "**Volume & Trends** — summarise call volume and any notable patterns.\n"
        "**Caller Sentiment & Urgency** — describe the emotional tone and urgency level.\n"
        "**Key Themes** — identify the most common topics callers raise.\n"
        "**Recommendations** — give 2-3 concrete, actionable suggestions.\n\n"
        "Be specific and data-driven. Keep each paragraph to 2-3 sentences.\n\n"
        f"{data_block}\n<|end|>\n<|assistant|>"
    )

    def generate():
        # Send an initial "connected" frame straight away so the browser knows
        # we're alive even while Phi-3 is still loading into memory. Padding
        # with a 2 KB SSE comment forces any in-the-middle reverse proxy
        # (nginx, Cloudflare, etc.) to flush its first buffer, so the browser
        # actually starts reading the stream instead of waiting.
        yield ":" + (" " * 2048) + "\n\n"
        yield _sse({"status": "connecting"})

        try:
            from openai import OpenAI
            # Generous timeout to accommodate cold-start model loading.
            client = OpenAI(base_url=ollama_url, api_key="ollama", timeout=600.0)

            stream = client.chat.completions.create(
                model="phi3:mini",
                max_tokens=600,
                temperature=0.4,
                messages=[{"role": "user", "content": prompt}],
                stream=True,
                # Ollama-specific: keep model resident in RAM forever so the
                # next request is instant. Sent via extra_body because the
                # standard OpenAI schema doesn't include this field.
                extra_body={"keep_alive": -1},
            )

            yield _sse({"status": "generating"})

            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if delta:
                    yield _sse({"delta": delta})

            yield _sse({"done": True})
        except Exception as e:
            current_app.logger.error(f"Ollama insights stream error: {e}", exc_info=True)
            # Surface the actual error class + first line so we can debug
            # production failures instead of guessing.
            err_type = type(e).__name__
            err_msg  = str(e).splitlines()[0][:200] if str(e) else "unknown"
            yield _sse({
                "error": f"Local model failed ({err_type}): {err_msg}"
            })
            yield _sse({"done": True})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


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
