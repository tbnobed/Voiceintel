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

# Make the CUDA libraries shipped by the nvidia-* pip wheels visible to
# CTranslate2 at runtime. If the container has no GPU access, faster-whisper
# silently falls back to CPU and these paths are simply ignored.
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.11/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.11/site-packages/nvidia/cudnn/lib:${LD_LIBRARY_PATH}

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
