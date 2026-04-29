import os
import re
import hmac
import hashlib
import logging
from datetime import datetime
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".ogg", ".aac", ".flac", ".wma", ".opus", ".amr"}


def _get_webhook_key() -> str:
    """
    Resolve webhook key: env var takes precedence, then Setting table.
    """
    key = os.environ.get("SENDGRID_WEBHOOK_KEY", "")
    if not key:
        try:
            from app.models.voicemail import Setting
            key = Setting.get("sendgrid_webhook_key", "")
        except Exception:
            pass
    return key


def verify_sendgrid_signature(request, webhook_key: str = "") -> bool:
    """
    Verify SendGrid Inbound Parse webhook authenticity.

    Supports two modes (checked in order):

    1. **URL token** (recommended for Inbound Parse):
       Add ?token=<SENDGRID_WEBHOOK_KEY> to the webhook URL you register
       in SendGrid. The key is compared with constant-time comparison.

    2. **Signed Event Webhook HMAC** (for Event Webhooks):
       SendGrid sends X-Twilio-Email-Event-Webhook-Signature +
       X-Twilio-Email-Event-Webhook-Timestamp headers.

    If no key is configured, verification is skipped (dev/open mode).
    """
    if not webhook_key:
        webhook_key = _get_webhook_key()
    if not webhook_key:
        logger.debug("No webhook key configured — accepting request without verification")
        return True

    # --- Mode 1: URL token ---
    url_token = request.args.get("token", "")
    if url_token:
        ok = hmac.compare_digest(url_token, webhook_key)
        if not ok:
            logger.warning("Webhook URL token mismatch — rejecting request")
        return ok

    # --- Mode 2: Signed Event Webhook (HMAC-SHA256) ---
    signature = request.headers.get("X-Twilio-Email-Event-Webhook-Signature", "")
    timestamp = request.headers.get("X-Twilio-Email-Event-Webhook-Timestamp", "")

    if signature and timestamp:
        try:
            import base64
            payload = timestamp.encode() + request.get_data()
            expected = hmac.new(webhook_key.encode(), payload, hashlib.sha256).digest()
            expected_b64 = base64.b64encode(expected).decode()
            ok = hmac.compare_digest(expected_b64, signature)
            if not ok:
                logger.warning("Webhook HMAC signature mismatch — rejecting request")
            return ok
        except Exception as e:
            logger.error(f"Signature verification error: {e}")
            return False

    # Webhook key is set but no verification material was provided.
    # Accept with a warning so a misconfigured sender doesn't silently drop mail.
    logger.warning(
        "Webhook key configured but no token param or signature headers found — "
        "accepting request. Add ?token=<key> to your SendGrid webhook URL to enforce auth."
    )
    return True


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

    # ── Path 1: top-level SendGrid attachments ────────────────────────────────
    for i in range(1, num_attachments + 1):
        key = f"attachment{i}"
        file_obj = request.files.get(key)
        if file_obj is None:
            continue

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

    # ── Path 2: raw email fallback for forwarded messages ─────────────────────
    # When the voicemail email is forwarded, the MP3 is a nested MIME part
    # that SendGrid does not expose as a top-level attachment (attachments=0).
    # SendGrid includes the full RFC 2822 message in the "email" field when
    # "Send Raw" is enabled in Inbound Parse settings, or in the "charsets"
    # payload. We always try to extract from it as a safety net.
    if not results:
        results = _extract_from_raw_email(
            request.form.get("email", ""),
            message_id, sender, subject, received_at, voicemail_dir,
        )

    return results


def _extract_from_raw_email(
    raw: str,
    message_id: str,
    sender: str,
    subject: str,
    received_at,
    voicemail_dir: str,
) -> list[dict]:
    """
    Walk every MIME part of the raw RFC 2822 email string (including nested
    parts inside forwarded messages) and save any audio attachments found.
    """
    if not raw:
        return []

    import email as _email

    results = []
    try:
        msg = _email.message_from_string(raw)
    except Exception as exc:
        logger.warning(f"Could not parse raw email field: {exc}")
        return []

    audio_parts = []
    for part in msg.walk():
        ct = part.get_content_type()
        disposition = part.get("Content-Disposition", "")
        filename = part.get_filename()

        # Accept parts that are either explicitly attached or whose content
        # type looks like audio (catches inline audio in forwarded messages).
        is_audio_type = ct.startswith("audio/")
        is_attachment = "attachment" in disposition.lower()

        if not filename and not is_audio_type:
            continue

        filename = _decode_filename(filename or "")
        if not filename:
            # Derive a filename from the content-type subtype
            subtype = ct.split("/")[-1] if "/" in ct else "wav"
            filename = f"voicemail.{subtype}"

        ext = os.path.splitext(filename)[1].lower()
        if ext not in AUDIO_EXTENSIONS and not is_audio_type:
            continue

        payload = part.get_payload(decode=True)
        if not payload:
            continue

        audio_parts.append((filename, payload))

    logger.info(f"Raw email parse: found {len(audio_parts)} nested audio part(s)")

    for idx, (filename, payload) in enumerate(audio_parts, start=1):
        mid = f"{message_id}_raw{idx}" if len(audio_parts) > 1 else message_id
        safe_name = _safe_filename(mid, filename)
        save_path = os.path.join(voicemail_dir, safe_name)
        with open(save_path, "wb") as fh:
            fh.write(payload)
        logger.info(f"Saved nested audio part: {save_path} ({len(payload)} bytes)")
        results.append({
            "message_id": mid,
            "filename": filename,
            "saved_path": save_path,
            "sender": sender,
            "subject": subject,
            "received_at": received_at,
            "uid": None,
            "source": "sendgrid_webhook_raw",
        })

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
