import os
import logging

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".ogg", ".aac", ".flac", ".wma"}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _decode_header_value(value):
    try:
        from email.header import decode_header, make_header
        return str(make_header(decode_header(value)))
    except Exception:
        return value or ""


def _get_sendgrid_config():
    """
    Resolve SendGrid config: environment variables take precedence,
    then fall back to values stored in the Setting table.
    """
    api_key = os.environ.get("SENDGRID_API_KEY", "")
    from_email = os.environ.get("SENDGRID_FROM_EMAIL", "")
    from_name = os.environ.get("SENDGRID_FROM_NAME", "VoiceIntel")
    admin_email = os.environ.get("SENDGRID_ADMIN_EMAIL", "")

    # Fall back to DB settings if env vars are absent
    try:
        from app.models.voicemail import Setting
        if not api_key:
            api_key = Setting.get("sendgrid_api_key", "")
        if not from_email:
            from_email = Setting.get("sendgrid_from_email", "")
        if from_name == "VoiceIntel":
            db_name = Setting.get("sendgrid_from_name", "")
            if db_name:
                from_name = db_name
        if not admin_email:
            admin_email = Setting.get("sendgrid_admin_email", "")
    except Exception:
        pass

    return {
        "api_key": api_key,
        "from_email": from_email,
        "from_name": from_name,
        "admin_email": admin_email,
    }


# ---------------------------------------------------------------------------
# Outbound: SendGrid
# ---------------------------------------------------------------------------

def send_notification_email(to: str, subject: str, body: str, html_body: str = "") -> bool:
    """
    Send an email via the SendGrid HTTP API.

    `to` may be:
      - a plain email address ("ops@example.com")
      - the special value "admin" → uses configured admin_email
      - comma-separated list of addresses

    Returns True on success, False on failure (never raises).
    """
    cfg = _get_sendgrid_config()

    if not cfg["api_key"]:
        logger.warning("SendGrid not configured (no API key) — skipping notification")
        return False
    if not cfg["from_email"]:
        logger.warning("SendGrid FROM address not configured — skipping notification")
        return False

    # Resolve recipient
    if to.strip().lower() == "admin":
        to = cfg["admin_email"]
    if not to:
        logger.warning("No recipient address resolved for notification — skipping")
        return False

    recipients = [addr.strip() for addr in to.split(",") if addr.strip()]

    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail, To, From, Content

        from_obj = From(email=cfg["from_email"], name=cfg["from_name"])

        message = Mail()
        message.from_email = from_obj
        message.subject = subject
        for r in recipients:
            message.add_to(To(r))
        message.add_content(Content("text/plain", body))
        if html_body:
            message.add_content(Content("text/html", html_body))

        sg = SendGridAPIClient(api_key=cfg["api_key"])
        response = sg.send(message)
        logger.info(
            f"SendGrid email sent to {recipients}: "
            f"status={response.status_code}"
        )
        return response.status_code in (200, 202)

    except Exception as e:
        logger.error(f"SendGrid send error: {e}")
        return False


def test_sendgrid_connection(api_key: str = "") -> tuple[bool, str]:
    """
    Validate the SendGrid API key by calling /v3/scopes.
    This endpoint is accessible by any valid key regardless of permissions.
    Returns (success, message).
    """
    if not api_key:
        cfg = _get_sendgrid_config()
        api_key = cfg["api_key"]
    if not api_key:
        return False, "No API key configured."
    try:
        import json, requests as _requests
        resp = _requests.get(
            "https://api.sendgrid.com/v3/scopes",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if resp.status_code == 200:
            scopes = resp.json().get("scopes", [])
            has_send = any("mail" in s for s in scopes)
            has_parse = any("inbound_parse" in s for s in scopes)
            parts = []
            if has_send:
                parts.append("Mail Send ✓")
            else:
                parts.append("Mail Send ✗")
            if has_parse:
                parts.append("Inbound Parse ✓")
            else:
                parts.append("Inbound Parse ✗")
            return True, "API key valid — " + ", ".join(parts) + "."
        if resp.status_code == 401:
            return False, "Invalid API key — authentication failed."
        return False, f"API returned status {resp.status_code}."
    except Exception as e:
        return False, str(e)


