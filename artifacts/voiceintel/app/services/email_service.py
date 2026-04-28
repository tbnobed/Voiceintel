import os
import re
import email
import imaplib
import logging
from datetime import datetime
from email.header import decode_header, make_header

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".ogg", ".aac", ".flac", ".wma"}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _decode_header_value(value):
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value or ""


def _get_imap_config():
    return {
        "host": os.environ.get("IMAP_HOST", ""),
        "port": int(os.environ.get("IMAP_PORT", "993")),
        "username": os.environ.get("IMAP_USERNAME", ""),
        "password": os.environ.get("IMAP_PASSWORD", ""),
        "folder": os.environ.get("IMAP_FOLDER", "INBOX"),
    }


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
    Validate the SendGrid API key by calling /v3/user/profile.
    Returns (success, message).
    """
    if not api_key:
        cfg = _get_sendgrid_config()
        api_key = cfg["api_key"]
    if not api_key:
        return False, "No API key configured."
    try:
        from sendgrid import SendGridAPIClient
        sg = SendGridAPIClient(api_key=api_key)
        resp = sg.client.user.profile.get()
        if resp.status_code == 200:
            import json
            profile = json.loads(resp.body)
            return True, f"Connected as {profile.get('username', 'unknown')}."
        return False, f"API returned status {resp.status_code}."
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Inbound: IMAP fetch
# ---------------------------------------------------------------------------

def _connect():
    cfg = _get_imap_config()
    if not cfg["host"] or not cfg["username"]:
        logger.warning("IMAP not configured. Skipping email ingestion.")
        return None, None

    try:
        mail = imaplib.IMAP4_SSL(cfg["host"], cfg["port"])
        mail.login(cfg["username"], cfg["password"])
        mail.select(cfg["folder"])
        return mail, cfg
    except Exception as e:
        logger.error(f"IMAP connection failed: {e}")
        return None, None


def fetch_voicemail_emails(storage_dir):
    """
    Fetch unread emails with audio attachments via IMAP.
    Returns list of dicts with metadata + saved file paths.
    """
    mail, cfg = _connect()
    if mail is None:
        return []

    results = []
    voicemail_dir = os.path.join(storage_dir, "voicemails")
    os.makedirs(voicemail_dir, exist_ok=True)

    try:
        _, message_ids = mail.search(None, "UNSEEN")
        uid_list = message_ids[0].split()
        logger.info(f"Found {len(uid_list)} unread emails")

        for uid in uid_list:
            try:
                _, msg_data = mail.fetch(uid, "(RFC822)")
                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                message_id = msg.get("Message-ID", f"uid-{uid.decode()}")
                sender = _decode_header_value(msg.get("From", ""))
                subject = _decode_header_value(msg.get("Subject", ""))
                date_str = msg.get("Date", "")

                try:
                    from email.utils import parsedate_to_datetime
                    received_at = parsedate_to_datetime(date_str)
                except Exception:
                    received_at = datetime.utcnow()

                audio_attachments = []
                for part in msg.walk():
                    filename = part.get_filename()
                    if not filename:
                        continue

                    filename = _decode_header_value(filename)
                    ext = os.path.splitext(filename)[1].lower()
                    if ext not in AUDIO_EXTENSIONS:
                        continue

                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue

                    safe_filename = _safe_filename(message_id, filename)
                    save_path = os.path.join(voicemail_dir, safe_filename)

                    with open(save_path, "wb") as f:
                        f.write(payload)

                    audio_attachments.append({
                        "message_id": message_id,
                        "filename": filename,
                        "saved_path": save_path,
                        "sender": sender,
                        "subject": subject,
                        "received_at": received_at,
                        "uid": uid,
                    })
                    logger.info(f"Saved attachment: {save_path}")

                if audio_attachments:
                    results.extend(audio_attachments)
                else:
                    mail.store(uid, "+FLAGS", "\\Seen")

            except Exception as e:
                logger.error(f"Error processing email uid={uid}: {e}")

    except Exception as e:
        logger.error(f"Email fetch error: {e}")
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    return results


def mark_email_read(uid_bytes):
    """Mark an email as read after successful processing."""
    mail, _ = _connect()
    if mail is None:
        return
    try:
        mail.store(uid_bytes, "+FLAGS", "\\Seen")
        mail.logout()
    except Exception as e:
        logger.error(f"Failed to mark email as read: {e}")


def _safe_filename(message_id, filename):
    safe_id = re.sub(r"[^a-zA-Z0-9]", "_", message_id)[:50]
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    return f"{safe_id}_{safe_name}"
