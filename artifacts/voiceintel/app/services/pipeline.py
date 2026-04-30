import os
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def _load_urgency_keywords() -> list:
    """
    Load the unified urgency keyword list from the settings table.

    Priority:
      1. 'urgency_keywords' setting (the unified admin-managed list).
      2. 'custom_urgency_keywords' (legacy key, migrated automatically).
      3. Seed from nlp_service.DEFAULT_URGENCY_KEYWORDS on first run.
    """
    try:
        from app.models.voicemail import Setting
        from app.services.nlp_service import DEFAULT_URGENCY_KEYWORDS

        raw = Setting.get("urgency_keywords", "")
        if raw:
            return json.loads(raw)

        # Migrate from old custom-only key
        legacy_raw = Setting.get("custom_urgency_keywords", "")
        if legacy_raw:
            legacy = json.loads(legacy_raw)
            if legacy:
                # Merge legacy custom list with defaults and save under new key
                merged = sorted(set(DEFAULT_URGENCY_KEYWORDS) | {k.lower() for k in legacy})
                Setting.set("urgency_keywords", json.dumps(merged))
                return merged

        # First run — seed from defaults
        Setting.set("urgency_keywords", json.dumps(sorted(DEFAULT_URGENCY_KEYWORDS)))
        return list(DEFAULT_URGENCY_KEYWORDS)
    except Exception:
        from app.services.nlp_service import DEFAULT_URGENCY_KEYWORDS
        return list(DEFAULT_URGENCY_KEYWORDS)


def process_email_items(app, items: list):
    """
    Core pipeline: convert audio → transcribe → NLP → persist → run triggers.
    Accepts a list of item dicts (from the SendGrid webhook).
    Each item must have: message_id, filename, saved_path, sender, subject, received_at.
    """
    from app import db
    from app.models.voicemail import Voicemail, Transcript, Insight, Category
    from app.services import audio_service
    from app.services.transcription_service import TranscriptionService
    from app.services import nlp_service
    from app.services.trigger_service import run_triggers

    with app.app_context():
        storage_dir = app.config["STORAGE_DIR"]
        model_size = app.config["WHISPER_MODEL"]
        transcriber = TranscriptionService(model_size)
        processed_dir = os.path.join(storage_dir, "processed")
        custom_kw = _load_urgency_keywords()

        for item in items:
            voicemail = None
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

                # ── Commit the record immediately so it appears in the UI ──────
                # The voicemail shows as "processing" while transcription runs.
                # If transcription crashes, it stays visible with status "error".
                voicemail = Voicemail(
                    message_id=item["message_id"],
                    filename=item["filename"],
                    sender=item.get("sender"),
                    recipient=item.get("recipient"),
                    subject=item.get("subject"),
                    received_at=item.get("received_at"),
                    original_path=item["saved_path"],
                    converted_path=converted_path,
                    duration=duration,
                    file_size=file_size,
                    processing_status="processing",
                )
                db.session.add(voicemail)
                db.session.commit()
                logger.info(f"Voicemail record created id={voicemail.id}: {item['filename']}")

                # Defensive: if an admin soft-deleted between record creation
                # and now (rare but possible during reprocessing), skip every
                # remaining downstream stage. Treat refresh failure as 'skip'
                # so we never proceed against an unknown row state.
                try:
                    db.session.refresh(voicemail)
                except Exception as refresh_err:
                    logger.warning(
                        f"Could not refresh vm {voicemail.id} after initial commit "
                        f"({refresh_err}); skipping pipeline."
                    )
                    continue
                if getattr(voicemail, "deleted_at", None) is not None:
                    logger.info(
                        f"Skipping pipeline for vm {voicemail.id}: "
                        f"soft-deleted before transcription"
                    )
                    continue

                # ── First-pass routing (recipient/sender/phone rules) ─────────
                try:
                    from app.services import routing_service
                    routing_service.route_voicemail(voicemail, commit=True)
                except Exception as routing_err:
                    logger.warning(f"First-pass routing failed for vm {voicemail.id}: {routing_err}")

                # ── Transcription (slow — runs after first commit) ────────────
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
                    nlp = nlp_service.analyze(transcription["text"], extra_urgency_keywords=custom_kw)
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

                if transcription.get("error"):
                    voicemail.processing_status = "error"
                elif not (transcription.get("text") or "").strip():
                    voicemail.processing_status = "no_speech"
                else:
                    voicemail.processing_status = "completed"
                db.session.commit()
                logger.info(f"Processed voicemail id={voicemail.id}: {item['filename']} status={voicemail.processing_status}")

                # Re-check soft-delete state before AI summary, second-pass
                # routing, and trigger notifications. Soft-deleted voicemails
                # must not generate outbound emails or further mutations.
                # Treat refresh failure as 'skip' — better to drop side
                # effects than to act on a row whose state we can't confirm.
                try:
                    db.session.refresh(voicemail)
                except Exception as refresh_err:
                    logger.warning(
                        f"Could not refresh vm {voicemail.id} before post-processing "
                        f"({refresh_err}); skipping AI summary + triggers."
                    )
                    continue
                if getattr(voicemail, "deleted_at", None) is not None:
                    logger.info(f"Skipping post-processing for vm {voicemail.id}: soft-deleted mid-pipeline")
                    continue

                # ── Per-voicemail AI summary (Phi-3 via Ollama) ──────────────
                # Best-effort — a model timeout/outage must NOT mark the
                # voicemail itself as failed. The summary card on the detail
                # page will show the error and offer a Regenerate button.
                if transcription.get("text"):
                    try:
                        from app.services import ai_summary_service
                        ai_result = ai_summary_service.generate_and_store(voicemail)
                        db.session.commit()
                        logger.info(
                            f"AI summary for vm {voicemail.id}: status={ai_result['status']} "
                            f"in {ai_result.get('duration_ms', 0)} ms"
                        )
                    except Exception as ai_err:
                        logger.warning(f"AI summary failed for vm {voicemail.id}: {ai_err}")
                        try:
                            db.session.rollback()
                        except Exception:
                            pass

                # AI summary is the slowest stage (Phi-3 can take 10-30s),
                # so re-check soft-delete state once more before second-pass
                # routing and trigger notifications. This closes the TOCTOU
                # window where an admin could soft-delete during AI summary.
                try:
                    db.session.refresh(voicemail)
                except Exception as refresh_err:
                    logger.warning(
                        f"Could not refresh vm {voicemail.id} before triggers "
                        f"({refresh_err}); skipping routing + triggers."
                    )
                    continue
                if getattr(voicemail, "deleted_at", None) is not None:
                    logger.info(
                        f"Skipping routing + triggers for vm {voicemail.id}: "
                        f"soft-deleted during AI summary"
                    )
                    continue

                # ── Second-pass routing (gives keyword rules a chance) ───────
                try:
                    from app.services import routing_service
                    routing_service.route_voicemail(voicemail, commit=True)
                except Exception as routing_err:
                    logger.warning(f"Second-pass routing failed for vm {voicemail.id}: {routing_err}")

                # Run automation triggers
                try:
                    run_triggers(app, voicemail)
                except Exception as te:
                    logger.error(f"Trigger engine error for voicemail {voicemail.id}: {te}")

            except Exception as e:
                logger.error(f"Pipeline error for {item.get('filename')}: {e}", exc_info=True)
                try:
                    db.session.rollback()
                    if voicemail and voicemail.id:
                        voicemail.processing_status = "error"
                        db.session.commit()
                except Exception:
                    pass



def reprocess_voicemail(app, voicemail_id):
    """Re-run transcription + NLP for an existing voicemail."""
    from app import db
    from app.models.voicemail import Voicemail, Transcript, Insight, Category
    from app.services.transcription_service import TranscriptionService
    from app.services import nlp_service
    from app.services.trigger_service import run_triggers

    with app.app_context():
        vm = Voicemail.query.get(voicemail_id)
        if not vm:
            return False, "Voicemail not found"

        model_size = app.config["WHISPER_MODEL"]
        transcriber = TranscriptionService(model_size)
        custom_kw = _load_urgency_keywords()

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
            nlp = nlp_service.analyze(transcription["text"], extra_urgency_keywords=custom_kw)
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

        if transcription.get("error"):
            vm.processing_status = "error"
        elif not (transcription.get("text") or "").strip():
            vm.processing_status = "no_speech"
        else:
            vm.processing_status = "completed"
        db.session.commit()

        # ── Regenerate the AI summary too (best-effort) ─────────────────
        if transcription.get("text"):
            try:
                from app.services import ai_summary_service
                ai_result = ai_summary_service.generate_and_store(vm)
                db.session.commit()
                logger.info(
                    f"AI summary for vm {vm.id}: status={ai_result['status']} "
                    f"in {ai_result.get('duration_ms', 0)} ms"
                )
            except Exception as ai_err:
                logger.warning(f"AI summary failed for vm {vm.id}: {ai_err}")
                try:
                    db.session.rollback()
                except Exception:
                    pass

        # Run automation triggers on reprocessed voicemail too
        try:
            run_triggers(app, vm)
        except Exception as te:
            logger.error(f"Trigger engine error for voicemail {vm.id}: {te}")

        return True, "Reprocessed successfully"
