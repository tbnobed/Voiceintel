import os
import time
import logging

logger = logging.getLogger(__name__)

_model = None
_model_size = None


def _get_model(model_size="base"):
    global _model, _model_size
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
        logger.info(f"Loading Whisper model '{model_size}' on {device} ({compute_type})")
        _model = WhisperModel(model_size, device=device, compute_type=compute_type)
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
            logger.error(f"Transcription error for {file_path}: {e}")
            result["error"] = str(e)
            result["processing_time"] = round(time.time() - start, 2)

        return result
