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

    elif at == "notify_admin":
        logger.info(
            f"Trigger '{trigger.name}': sending admin notification "
            f"for voicemail {voicemail.id} ('{voicemail.subject}')"
        )
        _send_notification(trigger, voicemail, recipient="admin")

    elif at == "send_email":
        recipient = av or "admin"
        logger.info(
            f"Trigger '{trigger.name}': sending email to {recipient!r} "
            f"for voicemail {voicemail.id}"
        )
        _send_notification(trigger, voicemail, recipient=recipient)


def _send_notification(trigger, voicemail, recipient: str):
    """Build and send a formatted notification email via SendGrid."""
    try:
        from app.services.email_service import send_notification_email

        cat_name = voicemail.category_obj.name if voicemail.category_obj else "N/A"
        sentiment = ""
        if voicemail.insights:
            sentiment = voicemail.insights.sentiment or ""

        duration_str = ""
        if voicemail.duration:
            mins = int(voicemail.duration) // 60
            secs = int(voicemail.duration) % 60
            duration_str = f"{mins}:{secs:02d}"

        transcript_preview = ""
        if voicemail.transcript and voicemail.transcript.text:
            transcript_preview = voicemail.transcript.text[:400]
            if len(voicemail.transcript.text) > 400:
                transcript_preview += "…"

        subject = f"[VoiceIntel] {trigger.name} — Voicemail #{voicemail.id}"

        body = (
            f"Automation trigger fired: {trigger.name}\n\n"
            f"Voicemail #{voicemail.id}\n"
            f"From:      {voicemail.sender or 'Unknown'}\n"
            f"Subject:   {voicemail.subject or '(none)'}\n"
            f"Received:  {voicemail.received_at or 'N/A'}\n"
            f"Category:  {cat_name}\n"
            f"Sentiment: {sentiment}\n"
            f"Urgent:    {'Yes' if voicemail.is_urgent else 'No'}\n"
            + (f"Duration:  {duration_str}\n" if duration_str else "")
            + (f"\nTranscript preview:\n{transcript_preview}\n" if transcript_preview else "")
        )

        html_body = _build_html_email(trigger, voicemail, cat_name, sentiment, duration_str, transcript_preview)

        send_notification_email(
            to=recipient,
            subject=subject,
            body=body,
            html_body=html_body,
        )
    except Exception as e:
        logger.error(f"Notification send error for trigger '{trigger.name}': {e}")


def _build_html_email(trigger, voicemail, cat_name, sentiment, duration_str, transcript_preview) -> str:
    urgent_badge = (
        '<span style="background:#c0392b;color:#fff;padding:2px 8px;border-radius:3px;font-size:11px;font-weight:600;">URGENT</span>'
        if voicemail.is_urgent else ""
    )
    transcript_section = ""
    if transcript_preview:
        transcript_section = f"""
        <tr>
          <td style="padding:16px 24px 24px;">
            <div style="font-size:11px;font-weight:600;color:#999;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px;">Transcript Preview</div>
            <div style="background:#f7f7f7;border-left:3px solid #F7CE5B;padding:12px 16px;font-size:13px;line-height:1.7;color:#333;font-style:italic;">
              {transcript_preview}
            </div>
          </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>VoiceIntel Alert</title></head>
<body style="margin:0;padding:0;background:#111;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#111;padding:32px 0;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="background:#1a1a0f;border-radius:8px;overflow:hidden;border:1px solid #2a2a15;">
        <!-- Header -->
        <tr>
          <td style="background:#1F1300;padding:20px 24px;border-bottom:1px solid #2a2a15;">
            <span style="font-size:18px;font-weight:700;color:#F7CE5B;letter-spacing:-.3px;">VoiceIntel</span>
            <span style="color:#666;font-size:13px;margin-left:12px;">Automation Alert</span>
          </td>
        </tr>
        <!-- Trigger name -->
        <tr>
          <td style="padding:20px 24px 12px;border-bottom:1px solid #2a2a15;">
            <div style="font-size:11px;font-weight:600;color:#AF9B46;text-transform:uppercase;letter-spacing:.1em;margin-bottom:6px;">Trigger Fired</div>
            <div style="font-size:20px;font-weight:600;color:#DFD6A7;">{trigger.name} {urgent_badge}</div>
          </td>
        </tr>
        <!-- Details grid -->
        <tr>
          <td style="padding:16px 24px;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="width:50%;vertical-align:top;padding-bottom:12px;">
                  <div style="font-size:11px;color:#666;text-transform:uppercase;letter-spacing:.08em;margin-bottom:3px;">From</div>
                  <div style="font-size:13px;color:#DFD6A7;">{voicemail.sender or "Unknown"}</div>
                </td>
                <td style="width:50%;vertical-align:top;padding-bottom:12px;">
                  <div style="font-size:11px;color:#666;text-transform:uppercase;letter-spacing:.08em;margin-bottom:3px;">Received</div>
                  <div style="font-size:13px;color:#DFD6A7;">{voicemail.received_at or "N/A"}</div>
                </td>
              </tr>
              <tr>
                <td style="vertical-align:top;padding-bottom:12px;">
                  <div style="font-size:11px;color:#666;text-transform:uppercase;letter-spacing:.08em;margin-bottom:3px;">Category</div>
                  <div style="font-size:13px;color:#DFD6A7;">{cat_name}</div>
                </td>
                <td style="vertical-align:top;padding-bottom:12px;">
                  <div style="font-size:11px;color:#666;text-transform:uppercase;letter-spacing:.08em;margin-bottom:3px;">Sentiment</div>
                  <div style="font-size:13px;color:#DFD6A7;">{sentiment.capitalize() if sentiment else "N/A"}</div>
                </td>
              </tr>
              {f'<tr><td colspan="2" style="padding-bottom:12px;"><div style="font-size:11px;color:#666;text-transform:uppercase;letter-spacing:.08em;margin-bottom:3px;">Duration</div><div style="font-size:13px;color:#DFD6A7;">{duration_str}</div></td></tr>' if duration_str else ""}
            </table>
          </td>
        </tr>
        {transcript_section}
        <!-- Footer -->
        <tr>
          <td style="background:#1F1300;padding:14px 24px;border-top:1px solid #2a2a15;">
            <span style="font-size:12px;color:#555;">Voicemail #{voicemail.id} &middot; VoiceIntel automated alert</span>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
