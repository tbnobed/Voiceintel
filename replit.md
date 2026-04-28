# Workspace

## Overview

pnpm workspace monorepo using TypeScript for backend services, plus a Python Flask web application (VoiceIntel).

## Applications

### VoiceIntel (Python Flask) — Primary App
- **Location**: `artifacts/voiceintel/`
- **URL**: `/` (root)
- **Port**: 5000
- **Workflow**: "VoiceIntel"
- **Stack**: Python 3.11, Flask, SQLAlchemy, PostgreSQL, faster-whisper, APScheduler

### API Server (Node.js)
- **Location**: `artifacts/api-server/`
- **URL**: `/api`
- **Port**: 8080

### Canvas/Mockup Sandbox
- **Location**: `artifacts/mockup-sandbox/`
- **URL**: `/__mockup`
- **Port**: 8081

## VoiceIntel Features
- **Local Whisper transcription** via faster-whisper (no cloud APIs)
- **IMAP email ingestion** — polls for voicemail attachments (configurable interval)
- **NLP pipeline** — keywords, sentiment, urgency detection, category classification
- **Analytics dashboard** — daily trend chart, category distribution, keyword cloud
- **Full-text search** across all transcripts
- **Voicemail detail** — audio player, full transcript, segments timeline, insights
- **Background scheduler** via APScheduler

## Environment Variables (for VoiceIntel)
| Variable | Description | Default |
|---|---|---|
| `DATABASE_URL` | PostgreSQL connection URL | (set as secret) |
| `IMAP_HOST` | IMAP server hostname | — |
| `IMAP_PORT` | IMAP port | 993 |
| `IMAP_USERNAME` | Email username | — |
| `IMAP_PASSWORD` | Email password | — |
| `IMAP_FOLDER` | Folder to monitor | INBOX |
| `WHISPER_MODEL` | tiny/base/small/medium/large-v2 | base |
| `POLL_INTERVAL` | Email poll interval (seconds) | 60 |

## File Structure (VoiceIntel)
```
artifacts/voiceintel/
  app/
    routes/      # Flask blueprints (main.py, api.py)
    models/      # SQLAlchemy models (voicemail, transcript, insight, category)
    services/    # Pipeline services (email, audio, transcription, NLP, pipeline)
    templates/   # Jinja2 templates (base, dashboard, voicemails, detail)
    static/      # CSS + JS
  storage/       # Audio file storage (voicemails/, processed/)
  main.py        # App entry point + scheduler
  requirements.txt
```

## Stack (Node.js/TypeScript monorepo)

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
