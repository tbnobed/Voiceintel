"""
Automation trigger engine.
Called after each voicemail is analyzed to check and fire matching rules.
"""
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)


def run_triggers(app, voicemail):
    """
    Evaluate all active AutomationTriggers against `voicemail`.
    Runs inside an existing app context (called from pipeline).
    """
    from app.models.trigger import AutomationTrigger
    from app import db

    triggers = AutomationTrigger.query.filter_by(is_active=True).all()
    if not triggers:
        return

    transcript_text = ""
    if voicemail.transcript and voicemail.transcript.text:
        transcript_text = voicemail.transcript.text.lower()

    insights = voicemail.insights

    for trigger in triggers:
        try:
            if _matches(trigger, voicemail, transcript_text, insights):
                _execute(trigger, voicemail, app)
                trigger.trigger_count = (trigger.trigger_count or 0) + 1
                trigger.last_triggered = datetime.utcnow()
        except Exception as e:
            logger.error(f"Trigger {trigger.id} '{trigger.name}' error: {e}")

    db.session.commit()


def _matches(trigger, voicemail, transcript_text: str, insights) -> bool:
    ct = trigger.condition_type
    cv = (trigger.condition_value or "").strip()

    if ct == "always":
        return True

    if ct == "is_urgent":
        return bool(voicemail.is_urgent)

    if ct == "category":
        cat_name = voicemail.category_obj.name if voicemail.category_obj else ""
        return cat_name.lower() == cv.lower()

    if ct == "sentiment":
        sentiment = insights.sentiment if insights else ""
        return sentiment == cv

    if ct == "keyword":
        keywords = [k.strip().lower() for k in cv.replace(",", " ").split() if k.strip()]
        for kw in keywords:
            pattern = r"\b" + re.escape(kw) + r"\b"
            if re.search(pattern, transcript_text):
                return True
        return False

    return False


def _execute(trigger, voicemail, app):
    from app import db
    at = trigger.action_type
    av = (trigger.action_value or "").strip()

    if at == "mark_urgent":
        if not voicemail.is_urgent:
            voicemail.is_urgent = True
            logger.info(f"Trigger '{trigger.name}': marked voicemail {voicemail.id} urgent")

    elif at == "add_label":
        if av and voicemail.subject and av not in voicemail.subject:
            voicemail.subject = f"{av} {voicemail.subject}"
            logger.info(f"Trigger '{trigger.name}': added label '{av}' to voicemail {voicemail.id}")

    elif at in ("notify_admin", "send_email"):
        # Log the notification — hook up to email_service if SMTP is configured
        recipient = av or "admin"
        logger.info(
            f"Trigger '{trigger.name}': would send notification to {recipient} "
            f"for voicemail {voicemail.id} ('{voicemail.subject}')"
        )
        _try_send_notification(trigger, voicemail, recipient, app)


def _try_send_notification(trigger, voicemail, recipient: str, app):
    """Best-effort email notification. Silently skips if email is not configured."""
    try:
        from app.services.email_service import send_notification_email
        send_notification_email(
            to=recipient,
            subject=f"[VoiceIntel] Trigger fired: {trigger.name}",
            body=(
                f"Automation trigger '{trigger.name}' fired on voicemail #{voicemail.id}.\n\n"
                f"From: {voicemail.sender}\n"
                f"Subject: {voicemail.subject}\n"
                f"Received: {voicemail.received_at}\n"
                f"Category: {voicemail.category_obj.name if voicemail.category_obj else 'N/A'}\n"
                f"Urgent: {voicemail.is_urgent}\n\n"
                f"View: /voicemails/{voicemail.id}"
            ),
        )
    except Exception:
        pass
