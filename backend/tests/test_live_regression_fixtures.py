from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import ModuleType
from unittest import mock
from uuid import UUID


BACKEND_DIR = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(BACKEND_DIR))

import database  # noqa: E402
from audio_vad import PauseDetector  # noqa: E402
from latency_metrics import (  # noqa: E402
    LiveLatencyTracker,
    nearest_rank,
    read_latency_report,
    record_samples,
    summarize_samples,
)
from stop_drain import StopDrainWindow  # noqa: E402
from transcript_assembler import (  # noqa: E402
    TranscriptAssembler,
    VADBoundaryTracker,
    count_sentence_endings,
    ends_with_sentence_punctuation,
    merge_transcript_snapshot,
)


def load_live_router_without_provider_credentials():
    """Import persistence helpers without constructing a Gemini client."""
    fake_service = ModuleType("gemini_live_service")
    fake_service.MODEL_NAME = "offline-test-model"
    fake_service.client = object()
    fake_text_service = ModuleType("gemini_service")
    fake_text_service.translate_text = mock.Mock(
        side_effect=AssertionError("Text translation must be mocked in this test.")
    )

    with mock.patch.dict(
        sys.modules,
        {
            "gemini_live_service": fake_service,
            "gemini_service": fake_text_service,
        },
    ):
        sys.modules.pop("live_session_router", None)
        return importlib.import_module("live_session_router")


def load_main_without_provider_credentials():
    """Import FastAPI routes with mocked Gemini services for local tests."""
    fake_live_service = ModuleType("gemini_live_service")
    fake_live_service.MODEL_NAME = "offline-test-model"
    fake_live_service.client = object()
    fake_text_service = ModuleType("gemini_service")
    fake_text_service.translate_text = mock.Mock()

    with mock.patch.dict(
        sys.modules,
        {
            "gemini_live_service": fake_live_service,
            "gemini_service": fake_text_service,
        },
    ):
        sys.modules.pop("live_session_router", None)
        sys.modules.pop("main", None)
        return importlib.import_module("main")


class AudioFixtureContractTests(unittest.TestCase):
    def test_wav_fixtures_match_pcm16_mono_16khz_contract(self):
        pause_wav = FIXTURE_DIR / "audio" / "speech_pause_speech.wav"
        silence_wav = FIXTURE_DIR / "audio" / "silence_100ms.wav"

        for path in (pause_wav, silence_wav):
            with self.subTest(path=path.name), wave.open(str(path), "rb") as wav_file:
                self.assertEqual(wav_file.getnchannels(), 1)
                self.assertEqual(wav_file.getsampwidth(), 2)
                self.assertEqual(wav_file.getframerate(), 16_000)
                self.assertEqual(wav_file.getcomptype(), "NONE")
                self.assertEqual(wav_file.getnframes() % 1_600, 0)

        with wave.open(str(silence_wav), "rb") as wav_file:
            self.assertEqual(wav_file.getnframes(), 1_600)
            self.assertEqual(set(wav_file.readframes(1_600)), {0})

        with wave.open(str(pause_wav), "rb") as wav_file:
            self.assertEqual(wav_file.getnframes(), 35_200)
            self.assertEqual(wav_file.getnframes() * 2, 22 * 3_200)

    def test_pause_detector_reports_the_600ms_gap_once(self):
        detector = PauseDetector(rms_threshold=450, silence_duration_ms=600)
        pause_wav = FIXTURE_DIR / "audio" / "speech_pause_speech.wav"
        boundaries = []

        with wave.open(str(pause_wav), "rb") as wav_file:
            while True:
                frame = wav_file.readframes(1_600)
                if not frame:
                    break
                if detector.process_frame(frame):
                    boundaries.append(wav_file.tell() // 1_600 - 1)

        self.assertEqual(boundaries, [13])
        self.assertTrue(detector.speech_active)

    def test_short_silence_does_not_end_speech(self):
        detector = PauseDetector(rms_threshold=450, silence_duration_ms=600)
        pause_wav = FIXTURE_DIR / "audio" / "silence_100ms.wav"

        with wave.open(str(pause_wav), "rb") as wav_file:
            silence_frame = wav_file.readframes(1_600)

        self.assertFalse(detector.process_frame(silence_frame))
        self.assertFalse(detector.speech_active)

    def test_single_loud_frame_is_not_enough_to_start_speech(self):
        detector = PauseDetector(
            rms_threshold=450,
            min_speech_duration_ms=200,
            silence_duration_ms=600,
        )
        loud_frame = b"\xff\x3f" * 1_600
        silence_frame = b"\x00\x00" * 1_600

        self.assertFalse(detector.process_frame(loud_frame))
        self.assertFalse(detector.speech_active)
        self.assertFalse(detector.process_frame(silence_frame))
        self.assertFalse(detector.speech_active)

    def test_event_fixtures_are_explicitly_synthetic_and_cover_required_cases(self):
        path = FIXTURE_DIR / "live_event_sequences.synthetic.json"
        fixture = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(fixture["fixture_version"], 1)
        self.assertEqual(fixture["source_kind"], "hand_authored_synthetic_scenarios")
        self.assertIn("not captured Gemini", fixture["notice"])

        scenarios = {item["name"]: item for item in fixture["scenarios"]}
        self.assertEqual(
            set(scenarios),
            {
                "two_turns_with_chunked_transcripts",
                "provisional_transcript_corrected_by_final",
                "stop_before_translation_arrives",
                "reconnect_keeps_database_session_sequence",
            },
        )
        for scenario in scenarios.values():
            self.assertTrue(scenario["events"])
            self.assertTrue(scenario["expected_segments"])


class TranscriptAssemblerTests(unittest.TestCase):
    def test_sentence_ending_detector_accepts_terminal_marks_after_quotes(self):
        self.assertTrue(ends_with_sentence_punctuation("It is ready."))
        self.assertTrue(ends_with_sentence_punctuation("Are you ready?\""))
        self.assertTrue(ends_with_sentence_punctuation("Sẵn sàng rồi!"))
        self.assertFalse(ends_with_sentence_punctuation("Oh,"))
        self.assertFalse(ends_with_sentence_punctuation("Are you"))
        self.assertFalse(ends_with_sentence_punctuation("below the"))

    def test_sentence_ending_count_ignores_times_decimals_and_ellipsis_groups(self):
        self.assertEqual(
            count_sentence_endings(
                "The meeting starts at 9:00, sorry. at 10:00. Version 3.5 works..."
            ),
            3,
        )

    def test_translation_repair_detects_punctuated_but_missing_last_sentence(self):
        live_router = load_live_router_without_provider_credentials()
        source = "Good morning everyone. The meeting starts at 9:00, sorry. at 10:00."
        partial_translation = "Chào buổi sáng mọi người. Cuộc họp bắt đầu lúc 9 giờ, xin lỗi."

        self.assertTrue(ends_with_sentence_punctuation(partial_translation))
        self.assertEqual(count_sentence_endings(source), 3)
        self.assertEqual(count_sentence_endings(partial_translation), 2)
        self.assertTrue(
            live_router._translation_needs_repair(source, partial_translation)
        )
        self.assertFalse(
            live_router._stream_translation_ready_to_commit(
                source,
                partial_translation,
            )
        )
        self.assertTrue(
            live_router._stream_translation_ready_to_commit(
                "The meeting starts at ten.",
                "Cuộc họp bắt đầu lúc mười giờ.",
            )
        )

    def test_synthetic_event_sequences_assemble_into_expected_segments(self):
        fixture = json.loads(
            (FIXTURE_DIR / "live_event_sequences.synthetic.json").read_text(
                encoding="utf-8"
            )
        )

        for scenario in fixture["scenarios"]:
            with self.subTest(scenario=scenario["name"]):
                transcript = TranscriptAssembler()
                actual_segments = []

                def save_current(force: bool = False) -> None:
                    source_text = transcript.source_for_save(force=force)
                    translated_text = transcript.translated_text
                    if source_text:
                        actual_segments.append({
                            "sequence_number": len(actual_segments) + 1,
                            "source_text": source_text,
                            "translated_text": translated_text or None,
                            "status": "completed" if translated_text else "failed",
                        })
                    transcript.reset()

                for event in scenario["events"]:
                    event_type = event["type"]
                    if event_type == "input_partial":
                        transcript.set_source_preview(event["text"])
                    elif event_type == "input_final":
                        transcript.set_source_final(event["text"])
                    elif event_type == "input_transcription":
                        transcript.add_source_fragment(event["text"])
                    elif event_type == "output_partial":
                        transcript.set_translation_final(event["text"])
                    elif event_type == "output_final":
                        transcript.set_translation_final(event["text"])
                    elif event_type == "output_transcription":
                        transcript.add_translation_fragment(event["text"])
                    elif event_type == "turn_complete":
                        save_current()
                    elif event_type == "stop":
                        save_current(force=True)

                if transcript.source_text or transcript.source_preview:
                    save_current(force=True)

                expected_segments = scenario["expected_segments"]
                self.assertEqual(len(actual_segments), len(expected_segments))
                for actual, expected in zip(actual_segments, expected_segments):
                    for key, value in expected.items():
                        self.assertEqual(actual.get(key), value)

    def test_revised_partial_is_replaced_by_final_text_not_concatenated(self):
        transcript = TranscriptAssembler()
        transcript.set_source_preview("The meeting starts at nine")
        transcript.set_source_preview("The meeting started at nine.")
        transcript.set_source_final("The meeting started at nine.")
        transcript.set_translation_final("Cuộc họp bắt đầu lúc chín giờ.")
        transcript.set_translation_final("Cuộc họp bắt đầu lúc chín giờ.")

        self.assertEqual(transcript.source_text, "The meeting started at nine.")
        self.assertEqual(transcript.source_preview, "")
        self.assertEqual(transcript.translated_text, "Cuộc họp bắt đầu lúc chín giờ.")

    def test_adjacent_fragments_preserve_repeated_words(self):
        transcript = TranscriptAssembler()
        transcript.add_source_fragment("I said very ")
        transcript.add_source_fragment("very clearly.")
        transcript.add_translation_fragment("Tôi nói rất ")
        transcript.add_translation_fragment("rất rõ ràng.")

        self.assertEqual(transcript.source_text, "I said very very clearly.")
        self.assertEqual(transcript.translated_text, "Tôi nói rất rất rõ ràng.")

    def test_cumulative_source_update_replaces_its_shorter_prefix(self):
        transcript = TranscriptAssembler()
        transcript.add_source_fragment(
            "Good morning, everyone. The meeting starts at 9:00 sorry,"
        )
        transcript.add_source_fragment(
            "Good morning, everyone. The meeting starts at 9:00, sorry, at 10:00."
        )

        self.assertEqual(
            transcript.source_text,
            "Good morning, everyone. The meeting starts at 9:00, sorry, at 10:00.",
        )

    def test_short_repeated_source_fragment_is_still_appended(self):
        transcript = TranscriptAssembler()
        transcript.add_source_fragment("We need ")
        transcript.add_source_fragment("we need four volunteers.")

        self.assertEqual(transcript.source_text, "We need we need four volunteers.")

    def test_stale_cumulative_source_update_does_not_undo_newer_text(self):
        transcript = TranscriptAssembler()
        transcript.add_source_fragment(
            "Good morning, everyone. The meeting starts at 9:00, sorry, at 10:00."
        )
        transcript.add_source_fragment(
            "Good morning, everyone. The meeting starts at 9:00 sorry,"
        )

        self.assertEqual(
            transcript.source_text,
            "Good morning, everyone. The meeting starts at 9:00, sorry, at 10:00.",
        )


class VADBoundaryTrackerTests(unittest.TestCase):
    def test_pause_needs_both_transcripts_and_a_quiet_settle_window(self):
        tracker = VADBoundaryTracker(settle_ms=300)
        tracker.note_pause(1.0)

        self.assertFalse(
            tracker.is_ready(
                2.0,
                has_source=True,
                has_translation=False,
                source_sentence_complete=True,
                translation_sentence_complete=False,
                speech_active=False,
            )
        )

        tracker.note_transcript(2.0)
        self.assertFalse(
            tracker.is_ready(
                2.29,
                has_source=True,
                has_translation=True,
                source_sentence_complete=True,
                translation_sentence_complete=True,
                speech_active=False,
            )
        )
        self.assertTrue(
            tracker.is_ready(
                2.30,
                has_source=True,
                has_translation=True,
                source_sentence_complete=True,
                translation_sentence_complete=True,
                speech_active=False,
            )
        )

    def test_active_speech_holds_the_candidate_boundary(self):
        tracker = VADBoundaryTracker(settle_ms=100)
        tracker.note_pause(1.0)
        tracker.note_transcript(1.0)

        self.assertFalse(
            tracker.is_ready(
                5.0,
                has_source=True,
                has_translation=True,
                source_sentence_complete=True,
                translation_sentence_complete=True,
                speech_active=True,
            )
        )

    def test_clause_pause_does_not_become_sentence_boundary(self):
        tracker = VADBoundaryTracker(settle_ms=100)
        tracker.note_pause(1.0)
        tracker.note_transcript(1.0)

        self.assertFalse(
            tracker.is_ready(
                2.0,
                has_source=True,
                has_translation=True,
                source_sentence_complete=False,
                translation_sentence_complete=False,
                speech_active=False,
            )
        )

    def test_multiple_uncommitted_pauses_are_counted_then_reset(self):
        tracker = VADBoundaryTracker(settle_ms=100)
        tracker.note_pause(1.0)
        tracker.note_pause(2.0)

        self.assertEqual(tracker.pending_pauses, 2)
        tracker.reset()
        self.assertEqual(tracker.pending_pauses, 0)
        self.assertIsNone(tracker.ready_at())


class TranscriptAssemblerRegressionTests(unittest.TestCase):
    def test_multiple_turns_do_not_bleed_into_the_next_buffer(self):
        transcript = TranscriptAssembler()
        transcript.add_source_fragment("First sentence.")
        transcript.add_translation_fragment("Câu thứ nhất.")
        first = (transcript.source_text, transcript.translated_text)
        transcript.reset()

        transcript.add_source_fragment("Second sentence.")
        transcript.add_translation_fragment("Câu thứ hai.")
        second = (transcript.source_text, transcript.translated_text)

        self.assertEqual(first, ("First sentence.", "Câu thứ nhất."))
        self.assertEqual(second, ("Second sentence.", "Câu thứ hai."))

    def test_unfinalized_preview_is_only_used_when_stop_forces_a_save(self):
        transcript = TranscriptAssembler()
        transcript.set_source_preview("One unfinished sentence")

        self.assertEqual(transcript.source_for_save(), "")
        self.assertEqual(
            transcript.source_for_save(force=True),
            "One unfinished sentence",
        )

    def test_force_save_prefers_a_newer_cumulative_preview_without_duplication(self):
        transcript = TranscriptAssembler()
        transcript.add_source_fragment(
            "Good morning, everyone. The meeting starts at 9:00 sorry,"
        )
        transcript.set_source_preview(
            "Good morning, everyone. The meeting starts at 9:00, sorry, at 10:00."
        )

        expected = (
            "Good morning, everyone. The meeting starts at 9:00, sorry, at 10:00."
        )
        self.assertEqual(transcript.source_for_display(), expected)
        self.assertEqual(transcript.source_for_save(force=True), expected)

    def test_stale_preview_does_not_undo_a_longer_final_transcript(self):
        transcript = TranscriptAssembler()
        transcript.add_source_fragment(
            "Good morning, everyone. The meeting starts at 9:00, sorry, at 10:00."
        )
        transcript.set_source_preview(
            "Good morning, everyone. The meeting starts at 9:00, sorry,"
        )

        self.assertEqual(
            transcript.source_for_display(),
            transcript.source_text,
        )
        self.assertEqual(
            transcript.source_for_save(force=True),
            transcript.source_text,
        )

    def test_cumulative_preview_reconciliation_keeps_spoken_repetition(self):
        self.assertEqual(
            merge_transcript_snapshot(
                "The volunteers are important",
                "The volunteers are important, we need, we need four.",
            ),
            "The volunteers are important, we need, we need four.",
        )

    def test_unrelated_preview_text_is_appended_without_fuzzy_deduplication(self):
        self.assertEqual(
            merge_transcript_snapshot("We need", "we need four volunteers."),
            "We need we need four volunteers.",
        )


class ProviderActivityProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live_router = load_live_router_without_provider_credentials()

    def test_audio_items_are_bracketed_by_manual_activity_markers(self):
        first_audio = b"\x01\x00" * 1_600
        second_audio = b"\x02\x00" * 1_600
        utterance_start = self.live_router._START_UTTERANCE
        utterance_end = self.live_router._END_UTTERANCE
        queued_items = (
            utterance_start,
            first_audio,
            utterance_end,
            utterance_start,
            second_audio,
            utterance_end,
        )
        expected_actions = [
            "activity_start",
            "audio",
            "activity_end",
            "activity_start",
            "audio",
            "activity_end",
        ]

        utterance_active = False
        actual_actions = []
        for item in queued_items:
            action, utterance_active = self.live_router._provider_action_for_audio_item(
                item,
                utterance_active,
            )
            if action:
                actual_actions.append(action)

        self.assertEqual(actual_actions, expected_actions)
        self.assertFalse(utterance_active)

    def test_stop_tail_keeps_every_candidate_frame_before_end_and_sentinel(self):
        first = b"\x01\x00" * 1_600
        second = b"\x02\x00" * 1_600
        sentinel = object()
        tail = self.live_router._final_audio_queue_items(
            speech_active=False,
            speech_candidate=True,
            pending_frames=[first, second],
            start_marker=self.live_router._START_UTTERANCE,
            end_marker=self.live_router._END_UTTERANCE,
            stop_marker=sentinel,
        )

        self.assertEqual(
            tail,
            [
                self.live_router._START_UTTERANCE,
                first,
                second,
                self.live_router._END_UTTERANCE,
                sentinel,
            ],
        )

        utterance_active = False
        actions = []
        for item in tail[:-1]:
            action, utterance_active = self.live_router._provider_action_for_audio_item(
                item,
                utterance_active,
            )
            if action:
                actions.append(action)
        self.assertEqual(actions, ["activity_start", "audio", "audio", "activity_end"])
        self.assertFalse(utterance_active)

    def test_stop_tail_does_not_replay_silence_when_no_speech_is_pending(self):
        sentinel = object()
        tail = self.live_router._final_audio_queue_items(
            speech_active=False,
            speech_candidate=False,
            pending_frames=[b"silence"],
            start_marker=self.live_router._START_UTTERANCE,
            end_marker=self.live_router._END_UTTERANCE,
            stop_marker=sentinel,
        )

        self.assertEqual(tail, [sentinel])

    def test_live_config_uses_manual_activity_detection(self):
        config = self.live_router._build_live_config("vi")

        self.assertTrue(
            config.realtime_input_config.automatic_activity_detection.disabled
        )
        self.assertEqual(
            config.translation_config.target_language_code,
            "vi",
        )


class LiveStopDrainTests(unittest.TestCase):
    def test_late_transcript_fragment_restarts_the_quiet_window(self):
        window = StopDrainWindow(
            started_at=0.0,
            quiet_seconds=1.5,
            timeout_seconds=20.0,
        )
        window.note_transcript(0.2)
        self.assertIsNone(window.decision(1.6))

        window.note_transcript(1.4)
        self.assertIsNone(window.decision(2.8))
        self.assertEqual(window.decision(2.9), "quiet")

    def test_terminal_provider_marker_still_waits_for_quiet_window(self):
        window = StopDrainWindow(
            started_at=5.0,
            quiet_seconds=1.0,
            timeout_seconds=20.0,
        )
        window.note_terminal(5.5)

        self.assertIsNone(window.decision(6.4))
        self.assertEqual(window.decision(6.5), "provider_complete")

    def test_drain_has_a_hard_timeout_if_provider_sends_nothing(self):
        window = StopDrainWindow(
            started_at=10.0,
            quiet_seconds=2.0,
            timeout_seconds=0.5,
        )

        self.assertIsNone(window.decision(10.49))
        self.assertEqual(window.decision(10.5), "timeout")

    def test_provider_marker_before_stop_does_not_skip_post_stop_settle(self):
        window = StopDrainWindow(
            started_at=20.0,
            quiet_seconds=1.0,
            timeout_seconds=20.0,
            last_activity_at=18.0,
            terminal_at=18.2,
        )

        self.assertIsNone(window.decision(20.99))
        self.assertEqual(window.decision(21.0), "provider_complete")


class LiveLatencyTrackerTests(unittest.TestCase):
    def test_latency_tracker_measures_partial_and_stop_finalization(self):
        tracker = LiveLatencyTracker()
        tracker.note_audio(10.0)
        tracker.note_source(10.2)
        tracker.note_translation(10.6)
        tracker.note_stop(12.0)
        tracker.note_final_persisted(12.3)

        self.assertEqual(
            tracker.samples(),
            {
                "audio_to_source_ms": 200.0,
                "audio_to_translation_ms": 600.0,
                "stop_to_final_persisted_ms": 300.0,
            },
        )

    def test_p50_p95_use_documented_nearest_rank_percentiles(self):
        summary = summarize_samples([4, 1, 2, 3, 5])

        self.assertEqual(summary["sample_count"], 5)
        self.assertEqual(summary["p50_ms"], 3.0)
        self.assertEqual(summary["p95_ms"], 5.0)
        self.assertIsNone(nearest_rank([], 0.95))

    def test_latency_samples_are_persisted_and_aggregated_from_sqlite(self):
        temp_dir = tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parent
        )
        original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(temp_dir.name) / "latency-tests.sqlite3"
        try:
            database.init_db()
            with database.get_connection() as connection:
                connection.execute(
                    "INSERT INTO presentations (presentation_id, status, started_at) "
                    "VALUES (?, ?, ?)",
                    ("presentation-test", "active", "2026-01-01T00:00:00+00:00"),
                )
                connection.execute(
                    "INSERT INTO sessions "
                    "(session_id, presentation_id, session_number, status, started_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        "session-test",
                        "presentation-test",
                        1,
                        "active",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )

            inserted = record_samples(
                "session-test",
                {
                    "audio_to_source_ms": 100,
                    "audio_to_translation_ms": 250,
                    "stop_to_final_persisted_ms": 400,
                },
            )
            report = read_latency_report()
        finally:
            database.DATABASE_PATH = original_database_path
            temp_dir.cleanup()

        self.assertEqual(inserted, 3)
        self.assertEqual(
            report["metrics"]["audio_to_translation_ms"]["p50_ms"], 250.0
        )
        self.assertEqual(
            report["metrics"]["stop_to_final_persisted_ms"]["sample_count"], 1
        )


class LiveTranslationRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live_router = load_live_router_without_provider_credentials()

    def test_truncated_live_translation_is_replaced_from_complete_source(self):
        translate = self.live_router.gemini_service.translate_text
        translate.reset_mock(side_effect=True, return_value=True)
        translate.return_value = {
            "en": "The meeting starts at 9:00, sorry, at 10:00.",
            "vn": "Cuộc họp bắt đầu lúc 9 giờ, xin lỗi, lúc 10 giờ.",
        }

        recovered, did_recover = self.live_router._repair_translation_from_source(
            "The meeting starts at 9:00, sorry, at 10:00.",
            "en",
            "vi",
            "Cuộc họp bắt đầu lúc 9 giờ, xin lỗi, lúc",
        )

        self.assertEqual(recovered, "Cuộc họp bắt đầu lúc 9 giờ, xin lỗi, lúc 10 giờ.")
        self.assertTrue(did_recover)
        translate.assert_called_once_with(
            "The meeting starts at 9:00, sorry, at 10:00.", "en"
        )

    def test_punctuated_translation_with_missing_sentence_is_repaired(self):
        translate = self.live_router.gemini_service.translate_text
        translate.reset_mock(side_effect=True, return_value=True)
        translate.return_value = {
            "en": "Good morning everyone. The meeting starts at 9:00, sorry. at 10:00.",
            "vn": "Chào buổi sáng mọi người. Cuộc họp bắt đầu lúc 9 giờ, xin lỗi, lúc 10 giờ.",
        }
        source = "Good morning everyone. The meeting starts at 9:00, sorry. at 10:00."
        partial_translation = "Chào buổi sáng mọi người. Cuộc họp bắt đầu lúc 9 giờ, xin lỗi."

        recovered, did_recover = self.live_router._repair_translation_from_source(
            source,
            "en",
            "vi",
            partial_translation,
        )

        self.assertEqual(
            recovered,
            "Chào buổi sáng mọi người. Cuộc họp bắt đầu lúc 9 giờ, xin lỗi, lúc 10 giờ.",
        )
        self.assertTrue(did_recover)
        translate.assert_called_once_with(source, "en")

    def test_complete_live_translation_does_not_trigger_extra_gemini_call(self):
        translate = self.live_router.gemini_service.translate_text
        translate.reset_mock()

        recovered, did_recover = self.live_router._repair_translation_from_source(
            "The meeting starts at 10.",
            "en",
            "vi",
            "Cuộc họp bắt đầu lúc 10 giờ.",
        )

        self.assertEqual(recovered, "Cuộc họp bắt đầu lúc 10 giờ.")
        self.assertFalse(did_recover)
        translate.assert_not_called()

    def test_incomplete_source_is_not_repaired_as_if_it_were_complete(self):
        translate = self.live_router.gemini_service.translate_text
        translate.reset_mock()

        recovered, did_recover = self.live_router._repair_translation_from_source(
            "The meeting starts at 10",
            "en",
            "vi",
            "Cuộc họp bắt đầu lúc",
        )

        self.assertEqual(recovered, "Cuộc họp bắt đầu lúc")
        self.assertFalse(did_recover)
        translate.assert_not_called()


class RetryEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = load_main_without_provider_credentials()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parent
        )
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "retry-tests.sqlite3"
        database.init_db()
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO presentations (presentation_id, status, started_at) "
                "VALUES (?, ?, ?)",
                ("presentation-test", "active", "2026-01-01T00:00:00+00:00"),
            )
            connection.execute(
                "INSERT INTO sessions "
                "(session_id, presentation_id, session_number, status, started_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "session-test",
                    "presentation-test",
                    1,
                    "active",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            connection.execute(
                """
                INSERT INTO segments (
                    segment_id, session_id, sequence_number, source_text,
                    source_language, en, vn, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "00000000-0000-4000-8000-000000000001",
                    "session-test",
                    1,
                    "The meeting starts at ten.",
                    "en",
                    "The meeting starts at ten.",
                    "Cuộc họp bắt đầu lúc",
                    "failed",
                    "2026-01-01T00:00:00+00:00",
                ),
            )

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def test_retry_translates_and_completes_a_real_failed_database_row(self):
        with mock.patch.object(
            self.main.gemini_service,
            "translate_text",
            return_value={
                "en": "The meeting starts at ten.",
                "vn": "Cuộc họp bắt đầu lúc mười giờ.",
            },
        ) as translate:
            response = self.main.retry_failed_segment(
                "session-test",
                UUID("00000000-0000-4000-8000-000000000001"),
            )

        self.assertEqual(response.status, "completed")
        self.assertEqual(response.vn, "Cuộc họp bắt đầu lúc mười giờ.")
        translate.assert_called_once_with(
            text="The meeting starts at ten.", source_language="en"
        )
        with database.get_connection() as connection:
            row = connection.execute(
                "SELECT status, vn FROM segments WHERE segment_id = ?",
                ("00000000-0000-4000-8000-000000000001",),
            ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["vn"], "Cuộc họp bắt đầu lúc mười giờ.")

    def test_failed_retry_keeps_the_segment_retryable_after_provider_error(self):
        with mock.patch.object(
            self.main.gemini_service,
            "translate_text",
            side_effect=RuntimeError("simulated provider outage"),
        ):
            with self.assertRaises(self.main.HTTPException) as raised:
                self.main.retry_failed_segment(
                    "session-test",
                    UUID("00000000-0000-4000-8000-000000000001"),
                )

        self.assertEqual(raised.exception.status_code, 502)
        with database.get_connection() as connection:
            row = connection.execute(
                "SELECT status FROM segments WHERE segment_id = ?",
                ("00000000-0000-4000-8000-000000000001",),
            ).fetchone()
        self.assertEqual(row["status"], "failed")


class SummaryInputEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = load_main_without_provider_credentials()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parent
        )
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "summary-tests.sqlite3"
        database.init_db()
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO presentations (presentation_id, status, started_at) "
                "VALUES (?, ?, ?)",
                ("presentation-test", "active", "2026-01-01T00:00:00+00:00"),
            )
            connection.execute(
                "INSERT INTO sessions "
                "(session_id, presentation_id, session_number, status, started_at, ended_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "session-test",
                    "presentation-test",
                    1,
                    "finished",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:10:00+00:00",
                ),
            )
            connection.executemany(
                """
                INSERT INTO segments (
                    segment_id, session_id, sequence_number, source_text,
                    source_language, en, vn, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        f"00000000-0000-4000-8000-{number:012d}",
                        "session-test",
                        number,
                        source,
                        "en",
                        source,
                        translated,
                        "completed",
                        "2026-01-01T00:00:00+00:00",
                    )
                    for number, source, translated in [
                        (1, "First point.", "Ý thứ nhất."),
                        (2, "Second point.", "Ý thứ hai."),
                    ]
                ],
            )

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def test_summary_input_preserves_order_and_bilingual_text(self):
        result = self.main.get_session_summary_input("session-test")

        self.assertEqual(result.segment_count, 2)
        self.assertEqual(result.transcript_en, "First point. Second point.")
        self.assertEqual(result.transcript_vn, "Ý thứ nhất. Ý thứ hai.")
        self.assertEqual(
            [segment.sequence_number for segment in result.segments],
            [1, 2],
        )

    def test_presentation_summary_aggregates_finished_sessions_in_order(self):
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO sessions "
                "(session_id, presentation_id, session_number, status, started_at, ended_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "session-two",
                    "presentation-test",
                    2,
                    "finished",
                    "2026-01-01T00:10:00+00:00",
                    "2026-01-01T00:20:00+00:00",
                ),
            )
            connection.execute(
                """
                INSERT INTO segments (
                    segment_id, session_id, sequence_number, source_text,
                    source_language, en, vn, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "00000000-0000-4000-8000-000000000003",
                    "session-two",
                    1,
                    "Third point.",
                    "en",
                    "Third point.",
                    "Ý thứ ba.",
                    "completed",
                    "2026-01-01T00:10:00+00:00",
                ),
            )
            connection.execute(
                "UPDATE presentations SET status = ?, ended_at = ? "
                "WHERE presentation_id = ?",
                ("finished", "2026-01-01T00:20:00+00:00", "presentation-test"),
            )

        result = self.main.get_presentation_summary_input("presentation-test")

        self.assertEqual(result.session_count, 2)
        self.assertEqual(result.segment_count, 3)
        self.assertEqual(
            result.transcript_en,
            "First point. Second point.\nThird point.",
        )
        self.assertEqual(
            result.transcript_vn,
            "Ý thứ nhất. Ý thứ hai.\nÝ thứ ba.",
        )
        self.assertEqual(
            [session.session_number for session in result.sessions],
            [1, 2],
        )
        self.assertEqual(
            [segment.sequence_number for segment in result.sessions[0].segments],
            [1, 2],
        )

    def test_presentation_cannot_finish_while_a_session_is_active(self):
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO sessions "
                "(session_id, presentation_id, session_number, status, started_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "session-two",
                    "presentation-test",
                    2,
                    "active",
                    "2026-01-01T00:10:00+00:00",
                ),
            )

        with self.assertRaises(self.main.HTTPException) as raised:
            self.main.finish_presentation("presentation-test")

        self.assertEqual(raised.exception.status_code, 409)

        with database.get_connection() as connection:
            connection.execute(
                "UPDATE sessions SET status = ?, ended_at = ? WHERE session_id = ?",
                ("finished", "2026-01-01T00:20:00+00:00", "session-two"),
            )

        result = self.main.finish_presentation("presentation-test")
        self.assertEqual(result.status, "finished")
        self.assertEqual(result.total_sessions, 2)
        self.assertEqual(result.finished_sessions, 2)

class LiveSegmentPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live_router = load_live_router_without_provider_credentials()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parent
        )
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "offline-tests.sqlite3"
        database.init_db()

        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO presentations (presentation_id, status, started_at) "
                "VALUES (?, ?, ?)",
                ("presentation-test", "active", "2026-01-01T00:00:00+00:00"),
            )
            connection.execute(
                "INSERT INTO sessions "
                "(session_id, presentation_id, session_number, status, started_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "session-test",
                    "presentation-test",
                    1,
                    "active",
                    "2026-01-01T00:00:00+00:00",
                ),
            )

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def test_live_segments_are_persisted_in_sequence_for_english_to_vietnamese(self):
        first = self.live_router._save_live_segment(
            "session-test", "vi", "The meeting starts at nine.", "Cuộc họp bắt đầu lúc chín giờ."
        )
        second = self.live_router._save_live_segment(
            "session-test", "vi", "Please take your seats.", "Vui lòng vào chỗ ngồi."
        )

        self.assertEqual(first["sequence_number"], 1)
        self.assertEqual(first["source_language"], "en")
        self.assertEqual(first["en"], "The meeting starts at nine.")
        self.assertEqual(first["vn"], "Cuộc họp bắt đầu lúc chín giờ.")
        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["sequence_number"], 2)

    def test_replaying_live_persistence_with_same_idempotency_key_does_not_duplicate(self):
        segment_id = "00000000-0000-4000-8000-000000000099"
        args = (
            "session-test",
            "vi",
            "The meeting starts at nine.",
            "Cuộc họp bắt đầu lúc chín giờ.",
        )

        first = self.live_router._save_live_segment(
            *args,
            segment_id=segment_id,
        )
        replay = self.live_router._save_live_segment(
            *args,
            segment_id=segment_id,
        )

        self.assertEqual(replay["segment_id"], first["segment_id"])
        self.assertEqual(replay["sequence_number"], first["sequence_number"])
        with database.get_connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM segments WHERE session_id = ?",
                ("session-test",),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_live_persistence_rejects_reusing_key_for_different_content(self):
        segment_id = "00000000-0000-4000-8000-000000000098"
        self.live_router._save_live_segment(
            "session-test",
            "vi",
            "The meeting starts at nine.",
            "Cuộc họp bắt đầu lúc chín giờ.",
            segment_id=segment_id,
        )

        with self.assertRaisesRegex(ValueError, "already used"):
            self.live_router._save_live_segment(
                "session-test",
                "vi",
                "A different sentence.",
                "Một câu khác.",
                segment_id=segment_id,
            )
        with database.get_connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM segments WHERE session_id = ?",
                ("session-test",),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_source_is_preserved_as_failed_when_translation_is_missing(self):
        saved = self.live_router._save_live_segment(
            "session-test", "vi", "I have one more point.", ""
        )

        self.assertEqual(saved["source_text"], "I have one more point.")
        self.assertEqual(saved["en"], "I have one more point.")
        self.assertIsNone(saved["vn"])
        self.assertEqual(saved["status"], "failed")

    def test_nonempty_but_truncated_translation_is_not_marked_completed(self):
        saved = self.live_router._save_live_segment(
            "session-test",
            "vi",
            "The meeting starts at ten.",
            "Cuộc họp bắt đầu lúc",
        )

        self.assertEqual(saved["source_text"], "The meeting starts at ten.")
        self.assertEqual(saved["vn"], "Cuộc họp bắt đầu lúc")
        self.assertEqual(saved["status"], "failed")

    def test_punctuated_translation_known_to_be_incomplete_is_saved_as_failed(self):
        source = "Good morning everyone. The meeting starts at 9:00, sorry. at 10:00."
        partial_translation = "Chào buổi sáng mọi người. Cuộc họp bắt đầu lúc 9 giờ, xin lỗi."
        saved = self.live_router._save_live_segment(
            "session-test",
            "vi",
            source,
            partial_translation,
        )

        self.assertEqual(saved["source_text"], source)
        self.assertEqual(saved["vn"], partial_translation)
        self.assertEqual(saved["status"], "failed")

    def test_repaired_stop_translation_is_saved_as_completed(self):
        source = "Good morning everyone. The meeting starts at 9:00, sorry. at 10:00."
        with mock.patch.object(
            self.live_router.gemini_service,
            "translate_text",
            return_value={
                "en": source,
                "vn": "Chào buổi sáng mọi người. Cuộc họp bắt đầu lúc 9 giờ, xin lỗi, lúc 10 giờ.",
            },
        ):
            translated, recovered = self.live_router._repair_translation_from_source(
                source,
                "en",
                "vi",
                "Chào buổi sáng mọi người. Cuộc họp bắt đầu lúc 9 giờ, xin lỗi.",
            )

        saved = self.live_router._save_live_segment(
            "session-test",
            "vi",
            source,
            translated,
            translation_repaired=recovered,
        )

        self.assertTrue(recovered)
        self.assertEqual(
            saved["vn"],
            "Chào buổi sáng mọi người. Cuộc họp bắt đầu lúc 9 giờ, xin lỗi, lúc 10 giờ.",
        )
        self.assertEqual(saved["status"], "completed")


if __name__ == "__main__":
    unittest.main()
