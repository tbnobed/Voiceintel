import os
import struct
import tempfile
import unittest
import wave
from unittest.mock import patch

from flask import render_template

from app.services import audio_service
from app.services.transcription_service import TranscriptionService


class StereoAudioServiceTests(unittest.TestCase):
    def _make_stereo_wav(self, path):
        with wave.open(path, "wb") as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(16000)
            frames = [
                struct.pack("<hh", 1200, -1200)
                for _ in range(160)
            ]
            output.writeframes(b"".join(frames))

    def test_split_stereo_audio_creates_independent_mono_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "call.wav")
            self._make_stereo_wav(source)

            paths, error = audio_service.split_stereo_channels(source, directory)

            self.assertIsNone(error)
            self.assertEqual(audio_service.get_audio_channel_count(source), 2)
            self.assertEqual(audio_service.get_audio_channel_count(paths["left"]), 1)
            self.assertEqual(audio_service.get_audio_channel_count(paths["right"]), 1)

    def test_split_stereo_audio_rejects_non_stereo_input(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "mono.wav")
            with wave.open(source, "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(struct.pack("<h", 1000) * 160)

            paths, error = audio_service.split_stereo_channels(source, directory)

            self.assertIsNone(paths)
            self.assertIn("Expected stereo audio", error)


class StereoSpeakerLabelTests(unittest.TestCase):
    def test_right_agent_channel_assigns_roles_and_orders_turns(self):
        with tempfile.TemporaryDirectory() as directory:
            left = os.path.join(directory, "left.wav")
            right = os.path.join(directory, "right.wav")
            open(left, "wb").close()
            open(right, "wb").close()

            service = TranscriptionService()

            def fake_transcribe(path):
                if path == left:
                    return {
                        "segments": [{"start": 4.0, "end": 5.0, "text": "Caller question"}],
                        "error": None,
                    }
                return {
                    "segments": [{"start": 1.0, "end": 3.0, "text": "Agent greeting"}],
                    "error": None,
                }

            service.transcribe = fake_transcribe
            with patch(
                "app.services.audio_service.split_stereo_channels",
                return_value=({"left": left, "right": right}, None),
            ):
                result = service.transcribe_stereo_channels(
                    "unused.wav",
                    directory,
                    agent_channel="right",
                )

            self.assertIsNone(result["error"])
            self.assertEqual(
                result["speaker_segments"],
                [
                    {
                        "start": 1.0,
                        "end": 3.0,
                        "text": "Agent greeting",
                        "speaker": "agent",
                    },
                    {
                        "start": 4.0,
                        "end": 5.0,
                        "text": "Caller question",
                        "speaker": "caller",
                    },
                ],
            )

    def test_stereo_failure_returns_safe_unlabeled_fallback(self):
        service = TranscriptionService()
        with patch(
            "app.services.audio_service.split_stereo_channels",
            return_value=(None, "Expected stereo audio; found 1 channel(s)."),
        ):
            result = service.transcribe_stereo_channels("mono.wav", "/tmp")

        self.assertEqual(result["speaker_segments"], [])
        self.assertIn("Expected stereo audio", result["error"])

    def test_channel_error_discards_partial_speaker_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            left = os.path.join(directory, "left.wav")
            right = os.path.join(directory, "right.wav")
            open(left, "wb").close()
            open(right, "wb").close()

            service = TranscriptionService()

            def fake_transcribe(path):
                if path == left:
                    return {
                        "segments": [{"start": 0.0, "end": 1.0, "text": "Agent only"}],
                        "error": None,
                    }
                return {"segments": [], "error": "Channel transcription failed"}

            service.transcribe = fake_transcribe
            with patch(
                "app.services.audio_service.split_stereo_channels",
                return_value=({"left": left, "right": right}, None),
            ):
                result = service.transcribe_stereo_channels("unused.wav", directory)

            self.assertEqual(result["speaker_segments"], [])
            self.assertIn("Channel transcription failed", result["error"])


class SpeakerLabelTemplateTests(unittest.TestCase):
    def test_detail_template_contains_agent_and_caller_labels(self):
        template_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "app",
            "templates",
            "voicemail_detail.html",
        )
        with open(template_path, encoding="utf-8") as template:
            markup = template.read()

        self.assertIn("Agent & Caller Transcript", markup)
        self.assertIn("'Agent' if is_agent else 'Caller'", markup)


class StereoPipelineAndTemplateTests(unittest.TestCase):
    def test_new_sftp_item_persists_and_renders_speaker_turns(self):
        from app import create_app, db
        from app.models.voicemail import Transcript, Voicemail
        from app.services.pipeline import process_email_items

        class FakeTranscriber:
            def __init__(self, _model_size):
                pass

            def transcribe(self, _path):
                return {
                    "text": "Agent greeting Caller question",
                    "language": "en",
                    "segments": [
                        {"start": 0.0, "end": 2.0, "text": "Agent greeting"},
                        {"start": 2.0, "end": 4.0, "text": "Caller question"},
                    ],
                    "processing_time": 0.1,
                    "error": None,
                }

            def transcribe_stereo_channels(self, _path, _output_dir, agent_channel):
                assert agent_channel == "left"
                return {
                    "speaker_segments": [
                        {"start": 0.0, "end": 2.0, "text": "Agent greeting", "speaker": "agent"},
                        {"start": 2.0, "end": 4.0, "text": "Caller question", "speaker": "caller"},
                    ],
                    "error": None,
                }

        saved_environment = {
            key: os.environ.get(key)
            for key in ("DATABASE_URL", "STORAGE_DIR", "SFTP_ENABLED", "SEED_FIVE9_TEAMS")
        }
        try:
            with tempfile.TemporaryDirectory() as directory:
                source = os.path.join(directory, "new-stereo-call.wav")
                open(source, "wb").close()
                os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(directory, 'voiceintel.db')}"
                os.environ["STORAGE_DIR"] = os.path.join(directory, "storage")
                os.environ["SFTP_ENABLED"] = "false"
                os.environ["SEED_FIVE9_TEAMS"] = "false"
                app = create_app()

                with app.app_context(), \
                     patch("app.services.transcription_service.TranscriptionService", FakeTranscriber), \
                     patch("app.services.audio_service.convert_audio", return_value=source), \
                     patch("app.services.audio_service.get_audio_duration", return_value=4.0), \
                     patch("app.services.audio_service.get_file_size", return_value=1024), \
                     patch("app.services.trigger_service.run_triggers"), \
                     patch(
                         "app.services.ai_summary_service.generate_and_store",
                         return_value={"status": "success", "duration_ms": 1},
                     ):
                    process_email_items(app, [{
                        "message_id": "sftp-new-stereo-test",
                        "filename": "8162164041Outbound by agent@example.com @ 3_00_00 PM.wav",
                        "saved_path": source,
                        "sender": "8162164041 <five9-sftp@voiceintel.internal>",
                        "recipient": "",
                        "subject": "8162164041 by agent@example.com",
                        "source": "sftp",
                        "agent": "agent@example.com",
                    }])

                    vm = Voicemail.query.filter_by(message_id="sftp-new-stereo-test").one()
                    transcript = Transcript.query.filter_by(voicemail_id=vm.id).one()
                    self.assertEqual(transcript.speaker_segments[0]["speaker"], "agent")
                    self.assertEqual(transcript.speaker_segments[1]["speaker"], "caller")

                    with app.test_request_context("/voicemails/test"):
                        html = render_template(
                            "voicemail_detail.html",
                            vm=vm,
                            all_teams=[],
                            assignable_users=[],
                            q="",
                        )
                    self.assertIn("Agent &amp; Caller Transcript", html)
                    self.assertIn("Agent · agent@example.com", html)
                    self.assertIn("Caller question", html)
        finally:
            for key, value in saved_environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()