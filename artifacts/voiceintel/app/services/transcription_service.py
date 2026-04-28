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
        import torch

        device = "cuda" if torch.cuda.is_available() else "cpu"
        compute_type = "float16" if device == "cuda" else "int8"

        logger.info(f"Loading Whisper model '{model_size}' on {device} ({compute_type})")
        _model = WhisperModel(model_size, device=device, compute_type=compute_type)
        _model_size = model_size
        logger.info("Whisper model loaded successfully")
        return _model
    except ImportError:
        logger.warning("faster-whisper not installed. Transcription will return placeholder.")
        return None
    except Exception as e:
        logger.error(f"Failed to load Whisper model: {e}")
        return None


class TranscriptionService:
    def __init__(self, model_size="base"):
        self.model_size = model_size

    def transcribe(self, file_path):
        start = time.time()
        result = {
            "text": None,
            "language": None,
            "segments": [],
            "processing_time": None,
            "error": None,
        }

        if not os.path.exists(file_path):
            result["error"] = f"File not found: {file_path}"
            return result

        model = _get_model(self.model_size)

        if model is None:
            result["error"] = "Whisper model unavailable. Install faster-whisper to enable transcription."
            result["text"] = "[Transcription unavailable - faster-whisper not installed]"
            result["processing_time"] = time.time() - start
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
                segments_list.append({
                    "start": round(seg.start, 2),
                    "end": round(seg.end, 2),
                    "text": seg.text.strip(),
                })
                full_text_parts.append(seg.text.strip())

            result["text"] = " ".join(full_text_parts)
            result["language"] = info.language
            result["segments"] = segments_list
            result["processing_time"] = round(time.time() - start, 2)

            logger.info(
                f"Transcribed {file_path} in {result['processing_time']}s, "
                f"language={result['language']}, words={len(full_text_parts)}"
            )
        except Exception as e:
            logger.error(f"Transcription error for {file_path}: {e}")
            result["error"] = str(e)
            result["processing_time"] = round(time.time() - start, 2)

        return result
