"""
SFTP incoming-file watcher.

Scans  <STORAGE_DIR>/sftp_incoming/  every 30 seconds (APScheduler job
registered in app/__init__.py).  For each mature audio file found:

  1. Parse caller phone, agent name, and timestamp from the Five9 filename.
  2. Move the file atomically into storage/voicemails/ (same volume, same
     filesystem → rename(), not copy-then-delete).
  3. Submit a pipeline item to the existing task_runner queue so the file
     goes through FFmpeg conversion → Whisper transcription → NLP → AI summary.

Five9 default Recording File Name Pattern produces paths like:
  recordings/<owner>/<created_date>/<phone> by <agent_name> @ <time>_<module>.wav
  e.g.  recordings/Owner/4_11_2012/3330001235 by Agent Name @ 12_52_19 PM_Ivr Module.wav

We flatten the directory tree into a single filename when moving to voicemails/
so the pipeline's audio-serving logic doesn't need to handle nested paths.

Duplicate-processing safety
────────────────────────────
The atomic move is the lock.  A file that has been moved is no longer in
sftp_incoming/ so a concurrent or restarted scan cannot pick it up again.
If the pipeline queue is full (task_runner.submit returns False) we move the
file back to its original location so the next scheduler tick retries it.
"""

import logging
import os
import re
import shutil
from datetime import datetime

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".ogg", ".aac", ".flac", ".wma", ".opus", ".amr"}

# Minimum file age in seconds before we consider it fully uploaded.
# Five9 writes sequentially; 10 s gives enough margin even on a slow link.
MIN_AGE_SECONDS = 10


# ──────────────────────────────────────────────────────────────────────────────
# Filename parsing
# ──────────────────────────────────────────────────────────────────────────────

def _parse_five9_filename(path: str) -> dict:
    """
    Extract caller phone, agent name, and recording timestamp from a Five9
    recording path.  Returns a dict with keys:
      number      – digits-only caller phone (e.g. "3330001235"), or ""
      agent       – agent display name, or ""
      recorded_at – datetime (best effort), or None

    Five9 stem format (from the VCC Recording File Name Pattern screen):
      "<number> by <agent> @ <HH_MM_SS AM/PM>_<module>"
    Example stem:
      "3330001235 by Agent Name @ 12_52_19 PM_Ivr Module"
    """
    basename = os.path.basename(path)
    stem, _ext = os.path.splitext(basename)

    number = ""
    agent = ""
    recorded_at = None

    # Primary pattern: "<number> by <agent> @ <time_with_module>"
    m = re.match(r"^(\S+)\s+by\s+(.+?)\s+@\s+(.+)$", stem, re.IGNORECASE)
    if m:
        number_raw = m.group(1).strip()
        agent_raw = m.group(2).strip()
        time_raw = m.group(3).strip()

        # Number: keep only the digits part (strip any prefix like "+1")
        number = re.sub(r"\D", "", number_raw)

        # Agent: strip trailing "_Ivr Module" or similar suffix that sometimes
        # bleeds into the agent token when the pattern has no space before @.
        agent = re.sub(r"_[A-Z][a-z].*$", "", agent_raw).strip()

        # Time: "12_52_19 PM" → try strptime
        # Strip any trailing module suffix after the AM/PM token.
        time_clean = re.sub(r"_(Ivr|Preview|Agent|Module).*$", "", time_raw, flags=re.IGNORECASE)
        for fmt in ("%I_%M_%S %p", "%H_%M_%S"):
            try:
                t = datetime.strptime(time_clean.strip(), fmt)
                today = datetime.utcnow().date()
                recorded_at = datetime(today.year, today.month, today.day,
                                       t.hour, t.minute, t.second)
                break
            except ValueError:
                continue
    else:
        # Fallback: treat the whole stem as a phone number candidate
        number = re.sub(r"\D", "", stem)[:20]

    return {"number": number, "agent": agent, "recorded_at": recorded_at}


# ──────────────────────────────────────────────────────────────────────────────
# Directory scan
# ──────────────────────────────────────────────────────────────────────────────

def _collect_ready_files(incoming_dir: str) -> list[str]:
    """
    Walk sftp_incoming/ recursively.  Return paths of audio files whose last
    modification time is at least MIN_AGE_SECONDS ago (i.e. upload is done).
    """
    now = datetime.utcnow().timestamp()
    ready = []
    for root, _dirs, files in os.walk(incoming_dir):
        for fname in sorted(files):           # sorted for deterministic order
            ext = os.path.splitext(fname)[1].lower()
            if ext not in AUDIO_EXTENSIONS:
                continue
            full = os.path.join(root, fname)
            try:
                age = now - os.path.getmtime(full)
                if age >= MIN_AGE_SECONDS:
                    ready.append(full)
            except OSError:
                pass
    return ready


# ──────────────────────────────────────────────────────────────────────────────
# Pipeline ingestion
# ──────────────────────────────────────────────────────────────────────────────

def _ingest_one(app, src_path: str, voicemails_dir: str, incoming_dir: str) -> None:
    """Move one file to voicemails/ and submit it to the pipeline."""
    from app.services import task_runner
    from app.services.pipeline import process_email_items

    filename = os.path.basename(src_path)
    meta = _parse_five9_filename(src_path)

    # Build a flat destination name that preserves the relative Five9 path
    # (owner/date/filename) as underscores so serving audio works without
    # nested dirs.
    try:
        rel = os.path.relpath(src_path, incoming_dir)
    except ValueError:
        rel = filename
    flat_name = rel.replace(os.sep, "_").replace("/", "_")
    dest_path = os.path.join(voicemails_dir, flat_name)

    # Atomic rename — same filesystem (same Docker volume).
    try:
        os.makedirs(voicemails_dir, exist_ok=True)
        shutil.move(src_path, dest_path)
        logger.info("SFTP watcher: moved %r → %s", filename, dest_path)
    except Exception as exc:
        logger.error("SFTP watcher: could not move %r: %s", src_path, exc)
        return

    # Synthesise a pipeline item that the existing process_email_items()
    # understands.  We set 'sender' to the phone number so caller_phone
    # routing rules in Teams still fire.
    number = meta["number"]
    agent = meta["agent"]

    # subject mirrors the Five9 stem: "<phone> by <agent>"
    if number and agent:
        subject = f"{number} by {agent}"
    elif number:
        subject = number
    else:
        subject = filename

    sender = (
        f"{number} <five9-sftp@voiceintel.internal>" if number
        else "five9-sftp@voiceintel.internal"
    )

    received_at = meta["recorded_at"] or datetime.utcnow()

    item = {
        "message_id": f"sftp-{flat_name}",
        "filename": filename,
        "saved_path": dest_path,
        "sender": sender,
        "recipient": "",
        "subject": subject,
        "received_at": received_at,
        "uid": None,
        "source": "sftp",
        "agent": agent or None,
    }

    def _run():
        process_email_items(app, [item])

    accepted = task_runner.submit(_run)
    if accepted:
        logger.info(
            "SFTP watcher: submitted %r to pipeline (caller=%r, agent=%r)",
            filename, number, agent,
        )
    else:
        # Queue full — move file back so the next tick retries it.
        logger.warning(
            "SFTP watcher: pipeline queue full, returning %r to incoming", filename
        )
        try:
            shutil.move(dest_path, src_path)
        except Exception as exc2:
            logger.error(
                "SFTP watcher: could not return %r to incoming: %s", dest_path, exc2
            )


# ──────────────────────────────────────────────────────────────────────────────
# APScheduler entry point
# ──────────────────────────────────────────────────────────────────────────────

def process_sftp_incoming(app) -> None:
    """
    APScheduler job — runs every 30 seconds.
    Scans sftp_incoming/ and submits any ready audio files to the pipeline.
    """
    with app.app_context():
        storage_dir = app.config["STORAGE_DIR"]
        incoming_dir = os.path.join(storage_dir, "sftp_incoming")
        voicemails_dir = os.path.join(storage_dir, "voicemails")

        if not os.path.isdir(incoming_dir):
            return  # SFTP never ran, nothing to do

        files = _collect_ready_files(incoming_dir)
        if not files:
            return

        logger.info("SFTP watcher: %d file(s) ready to process", len(files))
        for path in files:
            _ingest_one(app, path, voicemails_dir, incoming_dir)
