import os
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

from app import create_app, scheduler

app = create_app()


def _start_scheduler():
    from app.services.pipeline import run_ingestion_pipeline

    interval = app.config.get("POLL_INTERVAL", 60)

    scheduler.add_job(
        func=run_ingestion_pipeline,
        args=[app],
        trigger="interval",
        seconds=interval,
        id="email_poll",
        replace_existing=True,
    )

    if not scheduler.running:
        scheduler.start()
        logger.info(f"Scheduler started — polling every {interval}s")


if __name__ == "__main__":
    _start_scheduler()
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting VoiceIntel on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
