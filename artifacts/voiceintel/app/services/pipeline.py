import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def process_email_items(app, items: list):
    """
    Core pipeline: convert audio → transcribe → NLP → persist.
    Accepts a list of item dicts (from IMAP fetch or SendGrid webhook).
    Each item must have: message_id, filename, saved_path, sender, subject, received_at.
    """
    from app import db
    from app.models.voicemail import Voicemail, Transcript, Insight, Category
    from app.services import audio_service
    from app.services.transcription_service import TranscriptionService
    from app.services import nlp_service

    with app.app_context():
        storage_dir = app.config["STORAGE_DIR"]
        model_size = app.config["WHISPER_MODEL"]
        transcriber = TranscriptionService(model_size)
        processed_dir = os.path.join(storage_dir, "processed")

        for item in items:
            try:
                existing = Voicemail.query.filter_by(
                    message_id=item["message_id"],
                    filename=item["filename"],
                ).first()
                if existing:
                    logger.info(f"Duplicate skipped: {item['message_id']} / {item['filename']}")
                    continue

                try:
                    converted_path = audio_service.convert_audio(item["saved_path"], processed_dir)
                except Exception as e:
                    logger.error(f"Audio conversion failed: {e}")
                    converted_path = item["saved_path"]

                duration = audio_service.get_audio_duration(converted_path or item["saved_path"])
                file_size = audio_service.get_file_size(item["saved_path"])

                voicemail = Voicemail(
                    message_id=item["message_id"],
                    filename=item["filename"],
                    sender=item.get("sender"),
                    subject=item.get("subject"),
                    received_at=item.get("received_at"),
                    original_path=item["saved_path"],
                    converted_path=converted_path,
                    duration=duration,
                    file_size=file_size,
                    processing_status="processing",
                )
                db.session.add(voicemail)
                db.session.flush()

                transcription = transcriber.transcribe(converted_path or item["saved_path"])

                transcript = Transcript(
                    voicemail_id=voicemail.id,
                    text=transcription.get("text"),
                    language=transcription.get("language"),
                    segments=transcription.get("segments"),
                    processing_time=transcription.get("processing_time"),
                    error=transcription.get("error"),
                )
                db.session.add(transcript)

                if transcription.get("text"):
                    nlp = nlp_service.analyze(transcription["text"])
                    cat_name = nlp.get("category", "General Inquiry")
                    cat = Category.query.filter_by(name=cat_name).first()
                    voicemail.category_id = cat.id if cat else None
                    voicemail.is_urgent = nlp.get("is_urgent", False)

                    insight = Insight(
                        voicemail_id=voicemail.id,
                        keywords=nlp.get("keywords"),
                        sentiment=nlp.get("sentiment"),
                        sentiment_score=nlp.get("sentiment_score"),
                        urgency_keywords=nlp.get("urgency_keywords"),
                        category=nlp.get("category", "General Inquiry"),
                    )
                    db.session.add(insight)

                voicemail.processing_status = "error" if transcription.get("error") else "completed"
                db.session.commit()
                logger.info(f"Processed voicemail id={voicemail.id}: {item['filename']} (source={item.get('source','imap')})")

            except Exception as e:
                logger.error(f"Pipeline error for {item.get('filename')}: {e}")
                try:
                    db.session.rollback()
                except Exception:
                    pass


def run_ingestion_pipeline(app):
    """IMAP polling pipeline: fetch emails → process_email_items."""
    from app.services import email_service

    with app.app_context():
        storage_dir = app.config["STORAGE_DIR"]
        logger.info("Starting IMAP ingestion pipeline...")
        try:
            emails = email_service.fetch_voicemail_emails(storage_dir)
        except Exception as e:
            logger.error(f"IMAP ingestion failed: {e}")
            emails = []

        if emails:
            process_email_items(app, emails)

            # Mark emails as read after successful pipeline
            from app.services import email_service as es
            for item in emails:
                if item.get("uid"):
                    try:
                        es.mark_email_read(item["uid"])
                    except Exception:
                        pass


def reprocess_voicemail(app, voicemail_id):
    """Re-run transcription + NLP for an existing voicemail."""
    from app import db
    from app.models.voicemail import Voicemail, Transcript, Insight, Category
    from app.services.transcription_service import TranscriptionService
    from app.services import nlp_service

    with app.app_context():
        vm = Voicemail.query.get(voicemail_id)
        if not vm:
            return False, "Voicemail not found"

        model_size = app.config["WHISPER_MODEL"]
        transcriber = TranscriptionService(model_size)

        audio_path = vm.converted_path or vm.original_path
        if not audio_path or not os.path.exists(audio_path):
            return False, "Audio file not found"

        vm.processing_status = "processing"
        db.session.commit()

        transcription = transcriber.transcribe(audio_path)

        if vm.transcript:
            vm.transcript.text = transcription.get("text")
            vm.transcript.language = transcription.get("language")
            vm.transcript.segments = transcription.get("segments")
            vm.transcript.processing_time = transcription.get("processing_time")
            vm.transcript.error = transcription.get("error")
        else:
            transcript = Transcript(
                voicemail_id=vm.id,
                text=transcription.get("text"),
                language=transcription.get("language"),
                segments=transcription.get("segments"),
                processing_time=transcription.get("processing_time"),
                error=transcription.get("error"),
            )
            db.session.add(transcript)

        if transcription.get("text"):
            nlp = nlp_service.analyze(transcription["text"])
            cat_name = nlp.get("category", "General Inquiry")
            cat = Category.query.filter_by(name=cat_name).first()
            vm.category_id = cat.id if cat else None
            vm.is_urgent = nlp.get("is_urgent", False)

            if vm.insights:
                vm.insights.keywords = nlp.get("keywords")
                vm.insights.sentiment = nlp.get("sentiment")
                vm.insights.sentiment_score = nlp.get("sentiment_score")
                vm.insights.urgency_keywords = nlp.get("urgency_keywords")
                vm.insights.category = cat_name
            else:
                insight = Insight(
                    voicemail_id=vm.id,
                    keywords=nlp.get("keywords"),
                    sentiment=nlp.get("sentiment"),
                    sentiment_score=nlp.get("sentiment_score"),
                    urgency_keywords=nlp.get("urgency_keywords"),
                    category=cat_name,
                )
                db.session.add(insight)

        vm.processing_status = "error" if transcription.get("error") else "completed"
        db.session.commit()
        return True, "Reprocessed successfully"
