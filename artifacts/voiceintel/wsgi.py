# Production WSGI entrypoint used by gunicorn inside Docker.
from main import app

# Pre-warm the Whisper model at worker startup so the first voicemail
# doesn't pay the one-time model-loading cost (~10-15s on CPU).
import threading

def _preload_model():
    with app.app_context():
        try:
            from app.services.transcription_service import _get_model
            _get_model(app.config["WHISPER_MODEL"])
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Whisper preload failed: {e}")

threading.Thread(target=_preload_model, daemon=True, name="whisper-preload").start()
