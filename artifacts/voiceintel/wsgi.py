# Production WSGI entrypoint (used by gunicorn inside Docker).
# Starts the APScheduler background thread once, then exports the Flask app.
from main import app, _start_scheduler

_start_scheduler()
