import os
import re
import hmac
import hashlib
import logging
from datetime import datetime
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".ogg", ".aac", ".flac", ".wma", ".opus", ".amr"}


def verify_sendgrid_signature(request, webhook_key: str) -> bool:
    """
    Verify the SendGrid Event Webhook signature (signed event webhook).
    SendGrid sends the HMAC-SHA256 of the raw body using the webhook key.
    If SENDGRID_WEBHOOK_KEY is not set, verification is skipped (dev mode).
    """
    if not webhook_key:
        return True

    signature = request.headers.get("X-Twilio-Email-Event-Webhook-Signature", "")
    timestamp = request.headers.get("X-Twilio-Email-Event-Webhook-Timestamp", "")

    if not signature or not timestamp:
        logger.warning("SendGrid signature headers missing — accepting anyway")
        return True

    try:
        payload = timestamp.encode() + request.get_data()
        expected = hmac.new(webhook_key.encode(), payload, hashlib.sha256).digest()
        import base64
        expected_b64 = base64.b64encode(expected).decode()
        return hmac.compare_digest(expected_b64, signature)
    except Exception as e:
        logger.error(f"Signature verification error: {e}")
        return False


def parse_sendgrid_inbound(request, storage_dir: str) -> list[dict]:
    """
    Parse a SendGrid Inbound Parse webhook POST.

    SendGrid sends multipart/form-data with:
      - from, to, subject, text, html (string fields)
      - envelope (JSON string: {"from": "...", "to": ["..."]})
      - attachment-info (JSON string with attachment metadata)
      - attachments (count, as string)
      - attachment1, attachment2 … (FileStorage objects)

    Returns a list of dicts (one per audio attachment) ready for the pipeline.
    """
    import json

    voicemail_dir = os.path.join(storage_dir, "voicemails")
    os.makedirs(voicemail_dir, exist_ok=True)

    sender = request.form.get("from", "")
    subject = request.form.get("subject", "")
    message_id = request.form.get("headers", "")

    # Extract Message-ID from raw headers string
    mid_match = re.search(r"Message-ID:\s*(<[^>]+>)", message_id, re.IGNORECASE)
    message_id = mid_match.group(1) if mid_match else _generate_message_id(sender, subject)

    # Parse received_at from the Date header inside the headers blob
    received_at = datetime.utcnow()
    date_match = re.search(r"^Date:\s*(.+)$", request.form.get("headers", ""), re.IGNORECASE | re.MULTILINE)
    if date_match:
        try:
            received_at = parsedate_to_datetime(date_match.group(1).strip())
        except Exception:
            pass

    # Parse attachment-info metadata
    attachment_info = {}
    try:
        attachment_info = json.loads(request.form.get("attachment-info", "{}"))
    except Exception:
        pass

    num_attachments = int(request.form.get("attachments", "0") or 0)
    logger.info(f"SendGrid webhook: from={sender!r}, subject={subject!r}, attachments={num_attachments}")

    results = []
    for i in range(1, num_attachments + 1):
        key = f"attachment{i}"
        file_obj = request.files.get(key)
        if file_obj is None:
            continue

        # Determine filename
        filename = file_obj.filename or ""
        if not filename:
            info = attachment_info.get(key, {})
            filename = info.get("filename", f"attachment{i}.wav")

        filename = _decode_filename(filename)
        ext = os.path.splitext(filename)[1].lower()
        if ext not in AUDIO_EXTENSIONS:
            logger.info(f"Skipping non-audio attachment: {filename} ({ext})")
            continue

        safe_name = _safe_filename(message_id, filename)
        save_path = os.path.join(voicemail_dir, safe_name)
        file_obj.save(save_path)
        logger.info(f"Saved attachment #{i}: {save_path} ({os.path.getsize(save_path)} bytes)")

        results.append({
            "message_id": f"{message_id}_{i}" if num_attachments > 1 else message_id,
            "filename": filename,
            "saved_path": save_path,
            "sender": sender,
            "subject": subject,
            "received_at": received_at,
            "uid": None,
            "source": "sendgrid_webhook",
        })

    if num_attachments > 0 and not results:
        logger.warning(f"Webhook received {num_attachments} attachment(s) but none were audio files")

    return results


def _decode_filename(name: str) -> str:
    """Best-effort decode of RFC 2047 or percent-encoded filenames."""
    from email.header import decode_header, make_header
    try:
        return str(make_header(decode_header(name)))
    except Exception:
        return name


def _safe_filename(message_id: str, filename: str) -> str:
    safe_id = re.sub(r"[^a-zA-Z0-9]", "_", message_id)[:50]
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    return f"{safe_id}_{safe_name}"


def _generate_message_id(sender: str, subject: str) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    slug = re.sub(r"[^a-zA-Z0-9]", "", sender + subject)[:20]
    return f"<sg-{ts}-{slug}@voiceintel>"
