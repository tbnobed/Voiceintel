import os
import time
import logging
import threading

logger = logging.getLogger(__name__)

# Use all available CPU cores for transcription. ctranslate2's default is
# conservative (often 4). Explicitly setting this to the full core count
# gives a proportional speedup on multi-core servers.
_CPU_THREADS = os.cpu_count() or 4

_model = None
_model_size = None

# Two locks, narrowly scoped:
#   _MODEL_LOAD_LOCK serializes the first-call WhisperModel instantiation so
#   two concurrent webhooks don't both try to load the model into VRAM (which
#   can OOM the GPU and crash CUDA).
#   _TRANSCRIBE_LOCK serializes calls to model.transcribe(). faster-whisper /
#   CTranslate2 inference is NOT thread-safe — concurrent transcribe() calls
#   on the same model can corrupt internal state and hang the worker. On a
#   single CPU/GPU box, parallel transcription buys nothing anyway because
#   the model already saturates the device.
_MODEL_LOAD_LOCK = threading.Lock()
_TRANSCRIBE_LOCK = threading.Lock()


def _get_model(model_size="base"):
    global _model, _model_size
    if _model is not None and _model_size == model_size:
        return _model

    with _MODEL_LOAD_LOCK:
        # Re-check after acquiring the lock — another thread may have loaded
        # the model while we were waiting.
        if _model is not None and _model_size == model_size:
            return _model

        try:
            from faster_whisper import WhisperModel
        except ImportError:
            logger.warning("faster-whisper not installed. Run: pip install faster-whisper")
            return None

        # Detect CUDA without requiring torch — ctranslate2 handles device selection
        try:
            import ctranslate2
            providers = ctranslate2.get_supported_compute_types("cuda")
            device = "cuda"
            compute_type = "float16"
            logger.info("CUDA available — using GPU for transcription")
        except Exception:
            device = "cpu"
            compute_type = "int8"
            logger.info("No CUDA — using CPU for transcription")

        try:
            logger.info(
                f"Loading Whisper model '{model_size}' on {device} ({compute_type}) "
                f"with {_CPU_THREADS} CPU threads"
            )
            model_kwargs = dict(device=device, compute_type=compute_type)
            if device == "cpu":
                model_kwargs["cpu_threads"] = _CPU_THREADS
            _model = WhisperModel(model_size, **model_kwargs)
            _model_size = model_size
            logger.info("Whisper model loaded successfully")
            return _model
        except Exception as e:
            logger.error(f"Failed to load Whisper model: {e}")
            return None


class TranscriptionService:
    def __init__(self, model_size="base"):
        self.model_size = model_size

    def transcribe(self, file_path: str) -> dict:
        start = time.time()
        result = {
            "text": None,
            "language": None,
            "segments": [],
            "processing_time": None,
            "error": None,
        }

        if not os.path.exists(file_path):
            result["error"] = f"Audio file not found: {file_path}"
            return result

        model = _get_model(self.model_size)

        if model is None:
            result["error"] = "Whisper model could not be loaded. Check logs for details."
            return result

        try:
            # Serialize transcribe() — see _TRANSCRIBE_LOCK comment above.
            # Note: faster-whisper returns a generator, so the lock must wrap
            # the full segment-consumption loop (the actual decoding work
            # happens lazily as we iterate).
            with _TRANSCRIBE_LOCK:
                segments_gen, info = model.transcribe(
                    file_path,
                    beam_size=5,
                    vad_filter=True,
                    vad_parameters={"min_silence_duration_ms": 500},
                )

                segments_list = []
                full_text_parts = []

                for seg in segments_gen:
                    text = seg.text.strip()
                    segments_list.append({
                        "start": round(seg.start, 2),
                        "end":   round(seg.end, 2),
                        "text":  text,
                    })
                    full_text_parts.append(text)

                result["text"] = " ".join(full_text_parts)
                result["language"] = info.language
                result["segments"] = segments_list
                result["processing_time"] = round(time.time() - start, 2)

            logger.info(
                f"Transcribed '{file_path}' in {result['processing_time']}s — "
                f"lang={result['language']}, segments={len(segments_list)}"
            )
        except Exception as e:
            # faster-whisper raises "max() arg is an empty sequence" (and a few
            # similar internal errors) when its VAD filter trims the entire
            # clip — i.e. there is no detectable speech. Treat that as a clean
            # no-speech result rather than a hard transcription error so the
            # pipeline marks the voicemail as "no_speech" instead of "error".
            msg = str(e)
            no_speech_signatures = (
                "max() arg is an empty sequence",
                "min() arg is an empty sequence",
                "empty sequence",
            )
            if any(sig in msg for sig in no_speech_signatures):
                logger.info(f"No speech detected in {file_path} (VAD trimmed all audio)")
                result["text"] = ""
                result["segments"] = []
                result["processing_time"] = round(time.time() - start, 2)
            else:
                logger.error(f"Transcription error for {file_path}: {e}")
                result["error"] = msg
                result["processing_time"] = round(time.time() - start, 2)

        return result

    def transcribe_stereo_channels(
        self,
        file_path: str,
        output_dir: str,
        agent_channel: str = "left",
        mixed_transcription: dict | None = None,
    ) -> dict:
        """
        Transcribe each channel of a stereo call independently and return
        timestamped Agent/Caller turns. Speaker roles come from the channel
        mapping, not an AI guess.

        The caller should preserve the normal mixed-channel transcript when
        this method returns an error or no speaker segments.
        """
        from app.services import audio_service

        agent_channel = (agent_channel or "left").lower()
        if agent_channel not in {"left", "right"}:
            return {
                "speaker_segments": [],
                "error": (
                    f"Invalid Five9 agent channel {agent_channel!r}; "
                    "configure 'left' or 'right'."
                ),
            }

        paths, split_error = audio_service.split_stereo_channels(file_path, output_dir)
        if split_error:
            return {"speaker_segments": [], "error": split_error}

        # Use the mixed transcript's timestamps so channel bleed cannot cause
        # Whisper to merge an Agent and Caller exchange into one role.
        if mixed_transcription is not None:
            try:
                speaker_segments, label_error = audio_service.label_segments_by_channel(
                    paths,
                    mixed_transcription.get("segments") or [],
                    agent_channel,
                )
            finally:
                for path in paths.values():
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            if label_error:
                return {"speaker_segments": [], "error": label_error}
            return {"speaker_segments": speaker_segments, "error": None}

        caller_channel = "right" if agent_channel == "left" else "left"
        channel_roles = {
            agent_channel: "agent",
            caller_channel: "caller",
        }
        speaker_segments = []
        errors = []

        try:
            for channel in ("left", "right"):
                channel_result = self.transcribe(paths[channel])
                if channel_result.get("error"):
                    errors.append(f"{channel.title()} channel: {channel_result['error']}")
                    continue
                for segment in channel_result.get("segments") or []:
                    if not (segment.get("text") or "").strip():
                        continue
                    speaker_segments.append({
                        "start": segment["start"],
                        "end": segment["end"],
                        "text": segment["text"],
                        "speaker": channel_roles[channel],
                    })
        finally:
            for path in paths.values():
                try:
                    os.remove(path)
                except OSError:
                    pass

        speaker_segments.sort(
            key=lambda segment: (
                segment["start"],
                0 if segment["speaker"] == "agent" else 1,
                segment["end"],
            )
        )
        # A partial channel result must never replace the complete mixed
        # transcript in the UI. If either channel fails, callers retain the
        # existing unlabeled transcript and receive a visible fallback notice.
        if errors:
            return {
                "speaker_segments": [],
                "error": "; ".join(errors),
            }
        if not speaker_segments:
            return {
                "speaker_segments": [],
                "error": "; ".join(errors) or "No speech found in either stereo channel.",
            }

        logger.info(
            "Stereo speaker transcription completed for %s — %d labeled turns "
            "(agent channel=%s)",
            file_path,
            len(speaker_segments),
            agent_channel,
        )
        return {
            "speaker_segments": speaker_segments,
            "error": "; ".join(errors) if errors else None,
        }
