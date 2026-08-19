import os
import logging
import subprocess
import math
import wave
import uuid

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


def get_audio_channel_count(file_path):
    """Return the channel count for the first audio stream, or None on failure."""
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=channels",
            "-of", "default=noprint_wrappers=1:nokey=1",
            file_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return int(result.stdout.strip())
    except (FileNotFoundError, ValueError, subprocess.TimeoutExpired):
        pass
    return None


def split_stereo_channels(input_path, output_dir):
    """
    Extract left and right audio channels as independent mono 16kHz WAV files.

    Returns (paths, error), where paths is {"left": ..., "right": ...}.
    This deliberately does not guess on mono, multi-channel, or unreadable
    audio: callers can retain their existing unlabeled transcript instead.
    """
    channel_count = get_audio_channel_count(input_path)
    if channel_count != 2:
        if channel_count is None:
            return None, "Could not determine audio channel layout."
        return None, f"Expected stereo audio; found {channel_count} channel(s)."

    os.makedirs(output_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(input_path))[0]
    extraction_id = uuid.uuid4().hex
    paths = {
        "left": os.path.join(output_dir, f"{base}_{extraction_id}_left_channel.wav"),
        "right": os.path.join(output_dir, f"{base}_{extraction_id}_right_channel.wav"),
    }

    try:
        for label, channel_index in (("left", 0), ("right", 1)):
            cmd = [
                "ffmpeg",
                "-i", input_path,
                "-map", "0:a:0",
                "-filter:a", f"pan=mono|c0=c{channel_index}",
                "-ac", "1",
                "-ar", "16000",
                "-acodec", "pcm_s16le",
                "-y",
                paths[label],
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "FFmpeg channel extraction failed.")
    except FileNotFoundError:
        return None, "FFmpeg is unavailable for stereo channel extraction."
    except subprocess.TimeoutExpired:
        return None, "Stereo channel extraction timed out."
    except Exception as exc:
        for path in paths.values():
            try:
                os.remove(path)
            except OSError:
                pass
        return None, f"Stereo channel extraction failed: {exc}"

    logger.info("Extracted stereo channels from %s", input_path)
    return paths, None


def label_segments_by_channel(channel_paths, segments, agent_channel="left"):
    """
    Assign already-transcribed segments to the consistently louder channel.

    Five9 files can contain some cross-channel bleed. Transcribing each
    channel independently can therefore put two speakers into one channel's
    transcript. Measuring short windows within each mixed-transcript segment
    preserves the original turn boundaries while rejecting an exchange,
    interruption, or overlapped speech inside a purported turn.

    Returns (labeled_segments, error). If any speech window is ambiguous, the
    caller should retain the mixed transcript rather than publish guesses.
    """
    if agent_channel not in {"left", "right"}:
        return [], f"Invalid Five9 agent channel {agent_channel!r}."

    try:
        channel_data = {}
        for channel in ("left", "right"):
            with wave.open(channel_paths[channel], "rb") as audio:
                if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
                    return [], f"{channel.title()} extracted channel has an unsupported format."
                channel_data[channel] = (
                    audio.getframerate(),
                    audio.readframes(audio.getnframes()),
                )
    except (OSError, wave.Error) as exc:
        return [], f"Could not read extracted stereo channels: {exc}"

    def rms_for_window(channel, start, end):
        rate, raw = channel_data[channel]
        first = max(0, int(float(start) * rate))
        last = min(len(raw) // 2, max(first + 1, int(float(end) * rate)))
        samples = raw[first * 2:last * 2]
        if not samples:
            return 0.0
        total = 0
        count = len(samples) // 2
        for offset in range(0, len(samples), 2):
            sample = int.from_bytes(samples[offset:offset + 2], "little", signed=True)
            total += sample * sample
        return math.sqrt(total / count)

    labeled = []
    for segment in segments or []:
        text = (segment.get("text") or "").strip()
        if not text:
            continue
        start = segment.get("start", 0)
        end = segment.get("end", start)
        # A Whisper "segment" is not guaranteed to be a single speaker turn.
        # Check dominance over short windows and reject the labeled view if the
        # active channel switches, overlaps materially, or has no clear voice.
        duration = max(0.01, float(end) - float(start))
        window_size = 0.12
        windows = []
        cursor = float(start)
        while cursor < float(end):
            window_end = min(float(end), cursor + window_size)
            windows.append((
                rms_for_window("left", cursor, window_end),
                rms_for_window("right", cursor, window_end),
            ))
            cursor = window_end
        if not windows:
            return [], (
                "Stereo channels were not distinct for every transcript turn; "
                "mixed transcript retained."
            )

        peak_rms = max(max(left_rms, right_rms) for left_rms, right_rms in windows)
        speech_floor = max(40.0, peak_rms * 0.12)
        active_channels = set()
        for left_rms, right_rms in windows:
            louder = max(left_rms, right_rms)
            quieter = min(left_rms, right_rms)
            if louder < speech_floor:
                continue  # silence between spoken words
            if louder / max(quieter, 1) < 1.25:
                return [], (
                    "Stereo channels overlapped for a transcript turn; "
                    "mixed transcript retained."
                )
            active_channels.add("left" if left_rms > right_rms else "right")

        if len(active_channels) != 1:
            return [], (
                "Stereo channels were not distinct for every transcript turn; "
                "mixed transcript retained."
            )

        active_channel = active_channels.pop()
        labeled.append({
            "start": start,
            "end": end,
            "text": segment["text"],
            "speaker": "agent" if active_channel == agent_channel else "caller",
        })

    return labeled, None


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
