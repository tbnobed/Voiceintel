"""
Insights service — runs on an hourly schedule and stores the latest AI-generated
analytics summary in the `analytics_insights` table.

The route layer just reads the most recent row; no model calls happen on a
user request, so the page loads instantly regardless of Ollama state.
"""
import os
import re
import time
import logging
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import func, desc

from app import db
from app.models.voicemail import (
    Voicemail, Transcript, Insight, Category, AnalyticsInsight,
)

logger = logging.getLogger(__name__)

# Words filtered from "top keywords" — kept in sync with routes/main.py.
# Imported lazily inside _build_prompt() to avoid a circular import.


def _build_prompt() -> tuple[str | None, str | None]:
    """
    Aggregate analytics data and build the Phi-3 prompt.
    Returns (prompt, error_message). If there is no data, returns (None, msg).
    """
    from app.routes.main import _filter_keywords  # local import to avoid cycle

    now       = datetime.utcnow()
    week_ago  = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    # Exclude soft-deleted voicemails from every aggregate the AI summarises.
    not_deleted = Voicemail.deleted_at.is_(None)

    total = Voicemail.query.filter(not_deleted).count()
    if total == 0:
        return None, "No voicemail data to analyse yet."

    urgent_count = Voicemail.query.filter(not_deleted, Voicemail.is_urgent == True).count()
    week_count   = Voicemail.query.filter(not_deleted, Voicemail.received_at >= week_ago).count()
    month_count  = Voicemail.query.filter(not_deleted, Voicemail.received_at >= month_ago).count()

    sentiment_rows = (
        db.session.query(Insight.sentiment, func.count(Insight.id))
        .join(Voicemail, Voicemail.id == Insight.voicemail_id)
        .filter(not_deleted)
        .group_by(Insight.sentiment).all()
    )
    sentiment_dist = {s or "neutral": c for s, c in sentiment_rows}

    cat_rows = (
        db.session.query(Category.name, func.count(Voicemail.id))
        .join(Voicemail, Voicemail.category_id == Category.id, isouter=True)
        .filter((Voicemail.id.is_(None)) | not_deleted)
        .group_by(Category.name).order_by(func.count(Voicemail.id).desc()).limit(5).all()
    )

    all_kw: list = []
    kw_q = (
        db.session.query(Insight)
        .join(Voicemail, Voicemail.id == Insight.voicemail_id)
        .filter(Insight.keywords.isnot(None), not_deleted)
        .limit(200)
    )
    for ins in kw_q.all():
        if ins.keywords:
            all_kw.extend(_filter_keywords(ins.keywords))
    top_kw = [w for w, _ in Counter(all_kw).most_common(15)]

    recent_vms = (
        Voicemail.query
        .join(Transcript, Voicemail.id == Transcript.voicemail_id)
        .filter(not_deleted, Transcript.text.isnot(None))
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
    return prompt, None


# Headings the model is instructed to use, in order. Used by _trim_repeats()
# below to detect when the model has restarted the analysis and looped.
_SECTION_HEADINGS = (
    "Volume & Trends",
    "Caller Sentiment & Urgency",
    "Key Themes",
    "Recommendations",
)


def _trim_repeats(text: str) -> str:
    """
    Phi-3 mini sometimes ignores its 4-paragraph instruction and just keeps
    writing — repeating the entire analysis 2-3 times until it hits max_tokens.
    Detect that by finding the SECOND occurrence of the first heading
    ("Volume & Trends") and chopping everything from there onward.

    Matches the heading whether the model renders it as **Volume & Trends**,
    Volume & Trends:, or plain Volume & Trends.
    """
    if not text:
        return text

    # Find every occurrence of the first heading. Use a tolerant regex so we
    # catch optional bold markers and trailing punctuation/colons.
    first_heading = re.escape(_SECTION_HEADINGS[0])
    pattern = re.compile(
        r"(?:\*\*\s*)?" + first_heading + r"\s*(?:\*\*)?\s*[:\-—]?",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(text))
    if len(matches) < 2:
        return text.strip()

    # Cut just before the second occurrence and strip trailing whitespace.
    cutoff = matches[1].start()
    return text[:cutoff].rstrip()


def _trim_history(keep: int = 24) -> None:
    """Delete all but the most recent `keep` rows so the table doesn't grow forever."""
    ids_to_keep = [
        r.id for r in
        AnalyticsInsight.query.order_by(AnalyticsInsight.generated_at.desc()).limit(keep).all()
    ]
    if ids_to_keep:
        AnalyticsInsight.query.filter(~AnalyticsInsight.id.in_(ids_to_keep)).delete(
            synchronize_session=False
        )
        db.session.commit()


def generate_and_store_insight() -> AnalyticsInsight:
    """
    Build the prompt, call Ollama, and persist the result.
    Always inserts a row (success or error) so the UI can show what happened.
    """
    started_ms = int(time.time() * 1000)

    prompt, no_data_msg = _build_prompt()
    if no_data_msg:
        row = AnalyticsInsight(
            text=None,
            status="error",
            error_message=no_data_msg,
            duration_ms=int(time.time() * 1000) - started_ms,
            generated_at=datetime.utcnow(),
        )
        db.session.add(row)
        db.session.commit()
        _trim_history()
        return row

    ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")

    try:
        from openai import OpenAI
        client = OpenAI(base_url=ollama_url, api_key="ollama", timeout=600.0)

        # Non-streaming — we're a background job, latency doesn't matter.
        resp = client.chat.completions.create(
            model="phi3:mini",
            max_tokens=600,
            temperature=0.4,
            messages=[{"role": "user", "content": prompt}],
            extra_body={"keep_alive": -1},
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            raise RuntimeError("Empty response from model")

        # Defensive: drop any duplicated re-runs the model appended.
        text = _trim_repeats(text)

        row = AnalyticsInsight(
            text=text,
            status="success",
            duration_ms=int(time.time() * 1000) - started_ms,
            generated_at=datetime.utcnow(),
        )
        db.session.add(row)
        db.session.commit()
        _trim_history()
        logger.info(f"AI insights generated successfully in {row.duration_ms} ms")
        return row

    except Exception as e:
        err_type = type(e).__name__
        err_msg  = str(e).splitlines()[0][:500] if str(e) else "unknown"
        logger.error(f"AI insights generation failed: {err_type}: {err_msg}", exc_info=True)
        row = AnalyticsInsight(
            text=None,
            status="error",
            error_message=f"{err_type}: {err_msg}",
            duration_ms=int(time.time() * 1000) - started_ms,
            generated_at=datetime.utcnow(),
        )
        db.session.add(row)
        db.session.commit()
        _trim_history()
        return row


def get_latest_insight() -> AnalyticsInsight | None:
    """Return the most recent insight row (any status), or None if the table is empty."""
    return (
        AnalyticsInsight.query
        .order_by(AnalyticsInsight.generated_at.desc())
        .first()
    )
