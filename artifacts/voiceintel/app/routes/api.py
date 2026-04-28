import os
import threading
from flask import Blueprint, request, jsonify, current_app
from sqlalchemy import func, desc
from datetime import datetime, timedelta
from collections import Counter

from app import db
from app.models.voicemail import Voicemail, Transcript, Insight, Category, Setting

api_bp = Blueprint("api", __name__)


@api_bp.route("/stats")
def stats():
    today = datetime.utcnow().date()
    week_ago = datetime.utcnow() - timedelta(days=7)

    total = Voicemail.query.count()
    today_count = Voicemail.query.filter(func.date(Voicemail.received_at) == today).count()
    urgent_count = Voicemail.query.filter_by(is_urgent=True).count()
    pending_count = Voicemail.query.filter_by(processing_status="pending").count()

    category_dist = (
        db.session.query(Category.name, func.count(Voicemail.id))
        .join(Voicemail, Voicemail.category_id == Category.id, isouter=True)
        .group_by(Category.name)
        .all()
    )

    daily_trend = (
        db.session.query(
            func.date(Voicemail.received_at).label("day"),
            func.count(Voicemail.id).label("count"),
        )
        .filter(Voicemail.received_at >= week_ago)
        .group_by(func.date(Voicemail.received_at))
        .order_by("day")
        .all()
    )

    all_kw = []
    for ins in Insight.query.filter(Insight.keywords.isnot(None)).limit(200).all():
        if ins.keywords:
            all_kw.extend(ins.keywords)
    top_keywords = [{"word": w, "count": c} for w, c in Counter(all_kw).most_common(15)]

    return jsonify({
        "total": total,
        "today": today_count,
        "urgent": urgent_count,
        "pending": pending_count,
        "categories": [{"name": n, "count": c} for n, c in category_dist],
        "daily_trend": [{"day": str(d), "count": c} for d, c in daily_trend],
        "top_keywords": top_keywords,
    })


@api_bp.route("/voicemails")
def list_voicemails():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    q = request.args.get("q", "").strip()
    category_id = request.args.get("category", type=int)
    urgency = request.args.get("urgency")

    query = Voicemail.query.join(Transcript, Voicemail.id == Transcript.voicemail_id, isouter=True)

    if q:
        query = query.filter(Transcript.text.ilike(f"%{q}%"))
    if category_id:
        query = query.filter(Voicemail.category_id == category_id)
    if urgency == "urgent":
        query = query.filter(Voicemail.is_urgent == True)

    pagination = query.order_by(desc(Voicemail.received_at)).paginate(
        page=page, per_page=per_page, error_out=False
    )

    return jsonify({
        "voicemails": [vm.to_dict() for vm in pagination.items],
        "total": pagination.total,
        "pages": pagination.pages,
        "page": page,
    })


@api_bp.route("/voicemails/<int:vm_id>")
def get_voicemail(vm_id):
    vm = Voicemail.query.get_or_404(vm_id)
    data = vm.to_dict()
    if vm.transcript:
        data["transcript"] = vm.transcript.to_dict()
    if vm.insights:
        data["insights"] = vm.insights.to_dict()
    return jsonify(data)


@api_bp.route("/voicemails/<int:vm_id>/reprocess", methods=["POST"])
def reprocess(vm_id):
    app = current_app._get_current_object()

    def _do():
        from app.services.pipeline import reprocess_voicemail
        reprocess_voicemail(app, vm_id)

    thread = threading.Thread(target=_do, daemon=True)
    thread.start()
    return jsonify({"status": "started", "voicemail_id": vm_id})


@api_bp.route("/search")
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": [], "query": q})

    matches = (
        Voicemail.query
        .join(Transcript, Voicemail.id == Transcript.voicemail_id)
        .filter(Transcript.text.ilike(f"%{q}%"))
        .order_by(desc(Voicemail.received_at))
        .limit(50)
        .all()
    )

    results = []
    for vm in matches:
        text = vm.transcript.text or ""
        idx = text.lower().find(q.lower())
        snippet_start = max(0, idx - 80)
        snippet_end = min(len(text), idx + len(q) + 80)
        snippet = ("..." if snippet_start > 0 else "") + text[snippet_start:snippet_end] + ("..." if snippet_end < len(text) else "")

        results.append({
            **vm.to_dict(),
            "snippet": snippet,
            "highlight_start": idx - snippet_start if idx >= 0 else -1,
            "highlight_length": len(q),
        })

    return jsonify({"results": results, "query": q, "count": len(results)})


@api_bp.route("/poll", methods=["POST"])
def trigger_poll():
    """Manually trigger email ingestion."""
    app = current_app._get_current_object()

    def _do():
        from app.services.pipeline import run_ingestion_pipeline
        run_ingestion_pipeline(app)

    thread = threading.Thread(target=_do, daemon=True)
    thread.start()
    return jsonify({"status": "started"})


@api_bp.route("/categories")
def categories():
    cats = Category.query.order_by(Category.name).all()
    return jsonify([c.to_dict() for c in cats])


@api_bp.route("/webhook/inbound", methods=["GET", "POST"])
def sendgrid_inbound():
    """
    SendGrid Inbound Parse webhook.

    Configure SendGrid → Settings → Inbound Parse → Add Host & URL:
        URL: https://<your-domain>/api/webhook/inbound
        Check POST the raw, full MIME message: NO  (default multipart form)
        Check Send Grid spam check: optional
    """
    if request.method == "GET":
        # Liveness check for SendGrid configuration wizard
        return jsonify({"status": "ok", "service": "VoiceIntel inbound webhook"}), 200

    app = current_app._get_current_object()
    webhook_key = os.environ.get("SENDGRID_WEBHOOK_KEY", "")

    from app.services.webhook_service import verify_sendgrid_signature, parse_sendgrid_inbound

    if not verify_sendgrid_signature(request, webhook_key):
        return jsonify({"error": "Invalid signature"}), 403

    storage_dir = app.config["STORAGE_DIR"]
    try:
        items = parse_sendgrid_inbound(request, storage_dir)
    except Exception as e:
        app.logger.error(f"Webhook parse error: {e}")
        return jsonify({"error": str(e)}), 400

    if not items:
        # Acknowledge to SendGrid even if there were no audio attachments
        return jsonify({"status": "ignored", "reason": "no audio attachments found"}), 200

    # Process in background so SendGrid gets a fast 200
    def _process():
        from app.services.pipeline import process_email_items
        process_email_items(app, items)

    threading.Thread(target=_process, daemon=True).start()

    return jsonify({
        "status": "accepted",
        "queued": len(items),
        "filenames": [i["filename"] for i in items],
    }), 200


@api_bp.route("/health")
def health():
    return jsonify({"status": "ok"})
