---
name: VoiceIntel Five9 SFTP pipeline
description: How Five9 call recordings flow into VoiceIntel via SFTP, and how they are distinguished from email voicemails.
---

## Architecture
- asyncssh SFTP server runs as a daemon thread inside the Flask/gunicorn container (app/services/sftp_server.py)
- Files land in `sftp_incoming/`, watcher (APScheduler, 30s) moves them to `voicemails/` and submits to `task_runner`
- Same pipeline as email voicemails: FFmpeg → Whisper → NLP → Ollama AI summary

## Key fields added to Voicemail model
- `source` VARCHAR(50) DEFAULT 'email' — 'sftp' for Five9 recordings
- `agent` VARCHAR(255) — parsed from filename: `<phone> by <agent> @ <time>.wav`

## Routing
- SFTP items set sender = `<phone> <five9-sftp@voiceintel.internal>`, recipient = ''
- Five9 campaign teams are seeded on boot.
- The watcher extracts the campaign from the filename and the pipeline matches it to a team case-insensitively before regular routing rules run.
- The date directory must never be used as a campaign fallback.

**Why:** Five9's current export layout places a date at `recordings/<created_date>/`; treating that component as an owner silently creates unrouted recordings.

**How to apply:** Preserve the filename-based campaign parser whenever updating Five9 ingestion. If parsing fails, leave the recording unrouted rather than guessing from the directory.

## Historical recording repair
- On startup, incomplete SFTP rows are backfilled from their stored filename: missing agent values and unlocked, missing team assignments are repaired.
- Flattened legacy names begin with `recordings_<date>_`; strip that storage prefix before applying the normal filename parser.

**Why:** Records created before campaign parsing otherwise remain visible in the Recordings view with blank Agent and Team fields even after new uploads are routed correctly.

**How to apply:** Keep the backfill idempotent and never overwrite a populated agent, an assigned team, or a manually locked team.

## Source recognition
- Treat `message_id` values beginning with `sftp-` as Five9 recordings even when a historical row has an incorrect or missing `source` value.
- The startup backfill normalizes that source value to `sftp`.

**Why:** The Recordings page must not hide completed Five9 uploads because an older database default saved the source as email.

**How to apply:** Use the source-or-message-ID rule consistently in Recordings, Voicemails, and analytics filters so a record cannot appear in both views or neither.

## UI separation
- `/voicemails` filters WHERE source='email' OR source IS NULL
- `/recordings` filters WHERE source='sftp'
- `begin_auth` in sftp_server.py must return True (not False) for unknown users — False means "allow without auth" in asyncssh

## Five9 filename pattern confirmed
`recordings/<created_date>/<phone><Campaign Name> by <agent_email> @ <HH_MM_SS AM/PM>.wav`

Campaign text is appended directly after the leading phone digits and may contain spaces or hyphens, such as `3182907743Outbound - Donor Care by smiller@tbn.tv @ 12_46_20 PM.wav`.

## Schema guard
Both `source` and `agent` columns are added by `_ensure_voicemail_columns()` in app/__init__.py — safe to re-run.

**Why:** Source/agent live on the model so the recordings page can filter by source and display agent without parsing filenames at query time.
