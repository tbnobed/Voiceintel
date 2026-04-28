# VoiceIntel

Voicemail intelligence dashboard with local Whisper transcription, IMAP email ingestion, and NLP analysis.

## Features

- **Local transcription** via faster-whisper (no cloud APIs)
- **IMAP email ingestion** — polls for voicemail attachments every 60 seconds
- **NLP insights** — keywords, sentiment, urgency detection, category classification
- **Analytics dashboard** — trends, categories, keyword cloud
- **Full-text search** across all transcripts
- **GPU acceleration** — CUDA if available, CPU fallback

## Setup

1. Copy `.env.example` to `.env` and fill in your values
2. Install dependencies: `pip install -r requirements.txt`
3. Set `DATABASE_URL` to your PostgreSQL connection string
4. Configure IMAP credentials for email ingestion
5. Run: `python main.py`

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `DATABASE_URL` | PostgreSQL connection URL | sqlite:///voiceintel.db |
| `IMAP_HOST` | IMAP server hostname | — |
| `IMAP_PORT` | IMAP port (993 for SSL) | 993 |
| `IMAP_USERNAME` | Email username | — |
| `IMAP_PASSWORD` | Email password / app password | — |
| `IMAP_FOLDER` | Folder to monitor | INBOX |
| `WHISPER_MODEL` | Model size: tiny/base/small/medium/large-v2 | base |
| `POLL_INTERVAL` | Email poll interval in seconds | 60 |

## Whisper Models

| Model | Speed | Accuracy |
|---|---|---|
| tiny | Fastest | Basic |
| base | Fast | Good |
| small | Moderate | Better |
| medium | Slow | Great |
| large-v2 | Slowest | Best |

GPU is detected automatically — CUDA is used if available, otherwise CPU.
