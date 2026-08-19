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
- 15 Five9 campaign teams are seeded on boot (_seed_five9_teams in app/__init__.py)
- No routing rules yet — all recordings land with team_id=NULL (Task #6)

## UI separation
- `/voicemails` filters WHERE source='email' OR source IS NULL
- `/recordings` filters WHERE source='sftp'
- `begin_auth` in sftp_server.py must return True (not False) for unknown users — False means "allow without auth" in asyncssh

## Five9 filename pattern confirmed
`<phone> by <agent_email> @ <HH_MM_SS AM/PM>.wav` (no owner/campaign directory prefix confirmed from VCC screenshot)

## Schema guard
Both `source` and `agent` columns are added by `_ensure_voicemail_columns()` in app/__init__.py — safe to re-run.

**Why:** Source/agent live on the model so the recordings page can filter by source and display agent without parsing filenames at query time.
