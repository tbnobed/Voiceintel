import os
import logging
import subprocess

logger = logging.getLogger(__name__)

SUPPORTED_FORMATS = {".wav", ".mp3", ".m4a", ".ogg", ".aac", ".flac", ".wma"}


def is_supported_audio(filename):
    ext = os.path.splitext(filename)[1].lower()
    return ext in SUPPORTED_FORMATS


def convert_audio(input_path, output_dir):
    """Convert audio to mono 16kHz WAV using FFmpeg."""
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")

    os.makedirs(output_dir, exist_ok=True)

    base = os.path.splitext(os.path.basename(input_path))[0]
    output_path = os.path.join(output_dir, f"{base}_converted.wav")

    try:
        cmd = [
            "ffmpeg",
            "-i", input_path,
            "-ac", "1",
            "-ar", "16000",
            "-acodec", "pcm_s16le",
            "-y",
            output_path,
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg error: {result.stderr}")

        logger.info(f"Converted {input_path} -> {output_path}")
        return output_path

    except FileNotFoundError:
        logger.warning("FFmpeg not found. Attempting ffmpeg-python fallback.")
        return _convert_with_ffmpeg_python(input_path, output_path)
    except subprocess.TimeoutExpired:
        raise RuntimeError("FFmpeg conversion timed out (>120s)")


def _convert_with_ffmpeg_python(input_path, output_path):
    try:
        import ffmpeg
        (
            ffmpeg
            .input(input_path)
            .output(output_path, ac=1, ar=16000, acodec="pcm_s16le")
            .overwrite_output()
            .run(capture_stdout=True, capture_stderr=True)
        )
        return output_path
    except Exception as e:
        logger.error(f"ffmpeg-python conversion failed: {e}")
        logger.info(f"Falling back to original file: {input_path}")
        return input_path


def get_audio_duration(file_path):
    """Get duration in seconds using FFprobe."""
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            file_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return float(result.stdout.strip())
    except Exception:
        pass

    try:
        import ffmpeg
        probe = ffmpeg.probe(file_path)
        return float(probe["format"]["duration"])
    except Exception:
        pass

    return None


def get_file_size(file_path):
    try:
        return os.path.getsize(file_path)
    except Exception:
        return None
