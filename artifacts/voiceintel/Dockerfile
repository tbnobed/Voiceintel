FROM python:3.11-slim

# ── System dependencies ───────────────────────────────────────────────────────
# ffmpeg   — audio conversion (MP3 → WAV)
# libgomp1 — required by faster-whisper / CTranslate2
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgomp1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies ───────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download TextBlob corpora at build time so first run is instant
RUN python -m textblob.download_corpora

# ── Application code ──────────────────────────────────────────────────────────
COPY . .

# Storage directories (overridden by volume mount in production)
RUN mkdir -p storage/voicemails storage/processed instance

# Whisper model cache will be stored in a volume
ENV TRANSFORMERS_CACHE=/data/model_cache
ENV HF_HOME=/data/model_cache

EXPOSE 5000

# Single-worker gunicorn + thread pool keeps the APScheduler thread stable
# while handling concurrent HTTP requests.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "1", \
     "--threads", "4", \
     "--worker-class", "gthread", \
     "--timeout", "300", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "wsgi:app"]
