import asyncio
import logging
import os
import math
from collections import deque
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from google.genai import types

import database
import gemini_live_service
import gemini_service
import latency_metrics
from audio_vad import PauseDetector
from transcript_assembler import (
    TranscriptAssembler,
    VADBoundaryTracker,
    count_sentence_endings,
    ends_with_sentence_punctuation,
)
from stop_drain import StopDrainWindow


logger = logging.getLogger(__name__)
router = APIRouter()


def _translation_needs_repair(source_text: str, translated_text: str) -> bool:
    """Detect missing terminal punctuation or fewer translated sentences."""
    source_complete = ends_with_sentence_punctuation(source_text)
    translation_complete = ends_with_sentence_punctuation(translated_text)
    return bool(
        not source_complete
        or not translation_complete
        or count_sentence_endings(source_text)
        > count_sentence_endings(translated_text)
    )


def _stream_translation_ready_to_commit(
    source_text: str,
    translated_text: str,
) -> bool:
    """A streamed translation is safe to persist only when sentence shape agrees."""
    return bool(
        source_text.strip()
        and translated_text.strip()
        and not _translation_needs_repair(source_text, translated_text)
    )

LIVE_MODEL = gemini_live_service.MODEL_NAME
LIVE_CLIENT = gemini_live_service.client

MAX_AUDIO_FRAME_BYTES = 3200
AUDIO_QUEUE_SIZE = 20
LIVE_VAD_RMS_THRESHOLD = float(os.getenv("LIVE_VAD_RMS_THRESHOLD", "450"))
LIVE_VAD_MIN_SPEECH_MS = int(os.getenv("LIVE_VAD_MIN_SPEECH_MS", "200"))
LIVE_VAD_SILENCE_MS = int(os.getenv("LIVE_VAD_SILENCE_MS", "600"))
LIVE_SEGMENT_SETTLE_MS = int(os.getenv("LIVE_SEGMENT_SETTLE_MS", "300"))
LIVE_STOP_DRAIN_TIMEOUT_MS = int(os.getenv("LIVE_STOP_DRAIN_TIMEOUT_MS", "20000"))
LIVE_STOP_QUIET_MS = int(os.getenv("LIVE_STOP_QUIET_MS", "1500"))
LIVE_STOP_TRANSLATION_REPAIR_TIMEOUT_MS = int(
    os.getenv("LIVE_STOP_TRANSLATION_REPAIR_TIMEOUT_MS", "15000")
)
LIVE_AUDIO_QUEUE_DRAIN_TIMEOUT_MS = int(
    os.getenv("LIVE_AUDIO_QUEUE_DRAIN_TIMEOUT_MS", "20000")
)
LIVE_VAD_PREROLL_FRAMES = max(
    2,
    math.ceil(LIVE_VAD_MIN_SPEECH_MS / 100) + 2,
)
_START_UTTERANCE = object()
_END_UTTERANCE = object()


def _repair_translation_from_source(
    source_text: str,
    source_language: str,
    target_language: str,
    current_translation: str,
) -> tuple[str, bool]:
    """Replace an unfinished Live translation with a full text translation.

    Live Translate can close without a final-output marker. At Stop, a stable,
    sentence-complete source is safer to translate again as one complete unit
    than to present the last streamed fragment as a successful translation.
    """
    if not ends_with_sentence_punctuation(source_text):
        return current_translation, False
    if not _translation_needs_repair(source_text, current_translation):
        return current_translation, False

    translated = gemini_service.translate_text(source_text, source_language)
    key = "en" if target_language == "en" else "vn"
    candidate = translated.get(key, "").strip()
    if not candidate or not ends_with_sentence_punctuation(candidate):
        return current_translation, False
    # This is a non-streaming translation of the full source. It may correctly
    # combine source sentences, so punctuation-count equality is not required.
    # If it returns the same suspicious partial text, keep the segment failed.
    return candidate, candidate != current_translation


async def _wait_for_live_drain(
    provider_task: asyncio.Task,
    activity_changed: asyncio.Event,
    window: StopDrainWindow,
) -> str:
    """Drain final Gemini events until a terminal marker, quiet window, or cap."""
    loop = asyncio.get_running_loop()

    while True:
        if provider_task.done():
            return "provider_closed"

        now = loop.time()
        decision = window.decision(now)
        if decision is not None:
            return decision

        wake_at = window.next_wakeup_at()
        activity_changed.clear()
        wake_task = asyncio.create_task(activity_changed.wait())
        try:
            done, _ = await asyncio.wait(
                {provider_task, wake_task},
                timeout=max(0.01, wake_at - now),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if provider_task in done:
                return "provider_closed"
            if wake_task not in done:
                decision = window.decision(loop.time())
                if decision is not None:
                    return decision
        finally:
            if not wake_task.done():
                wake_task.cancel()
                try:
                    await wake_task
                except asyncio.CancelledError:
                    pass


def _is_session_active(session_id: str) -> bool:
    with database.get_connection() as connection:
        row = connection.execute(
            "SELECT status FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()

    return row is not None and row["status"] == "active"


def _save_live_segment(
    session_id: str,
    target_language: str,
    source_text: str,
    translated_text: str,
    *,
    translation_repaired: bool = False,
    segment_id: str | None = None,
) -> dict:
    """
    Persist one completed provider turn.

    If the source or translation is unfinished, or an unrepaired live
    translation contains fewer sentence endings than the source, preserve the
    text and mark the segment failed so the retry endpoint can be used.
    """
    source_text = source_text.strip()
    translated_text = translated_text.strip()

    if not source_text:
        raise ValueError("Cannot save a live segment without source text.")

    source_language = "en" if target_language == "vi" else "vi"

    if source_language == "en":
        en_text = source_text
        vn_text = translated_text or None
    else:
        vn_text = source_text
        en_text = translated_text or None

    source_is_complete = ends_with_sentence_punctuation(source_text)
    translation_is_complete = ends_with_sentence_punctuation(translated_text)
    sentence_counts_are_acceptable = (
        translation_repaired
        or count_sentence_endings(source_text)
        <= count_sentence_endings(translated_text)
    )
    segment_status = (
        "completed"
        if (
            translated_text
            and source_is_complete
            and translation_is_complete
            and sentence_counts_are_acceptable
        )
        else "failed"
    )
    # The caller keeps this key with the active transcript buffer and reuses
    # it if finalization has to be retried after a persistence interruption.
    segment_id = str(segment_id or uuid4())
    created_at = datetime.now(timezone.utc).isoformat()

    with database.get_connection() as connection:
        # Reserve the SQLite write lock while assigning the sequence number.
        connection.execute("BEGIN IMMEDIATE")

        session = connection.execute(
            "SELECT status FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()

        if session is None:
            raise ValueError("Session not found.")

        if session["status"] != "active":
            raise ValueError("Session is not active.")

        existing = connection.execute(
            "SELECT * FROM segments WHERE segment_id = ?",
            (segment_id,),
        ).fetchone()
        if existing is not None:
            same_segment = (
                existing["session_id"] == session_id
                and existing["source_text"] == source_text
                and existing["source_language"] == source_language
                and existing["en"] == en_text
                and existing["vn"] == vn_text
                and existing["status"] == segment_status
            )
            if not same_segment:
                raise ValueError(
                    "The live segment idempotency key is already used "
                    "for different segment data."
                )
            return dict(existing)

        row = connection.execute(
            """
            SELECT COALESCE(MAX(sequence_number), 0) AS max_number
            FROM segments
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()

        sequence_number = (row["max_number"] or 0) + 1

        connection.execute(
            """
            INSERT INTO segments (
                segment_id,
                session_id,
                sequence_number,
                source_text,
                source_language,
                en,
                vn,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                segment_id,
                session_id,
                sequence_number,
                source_text,
                source_language,
                en_text,
                vn_text,
                segment_status,
                created_at,
            ),
        )

        saved = connection.execute(
            "SELECT * FROM segments WHERE segment_id = ?",
            (segment_id,),
        ).fetchone()

    return dict(saved)


async def _forward_audio_queue(audio_queue, provider_session) -> None:
    """Send manually delimited utterances over one persistent Live session."""
    utterance_active = False

    while True:
        audio_chunk = await audio_queue.get()

        if audio_chunk is None:
            if utterance_active:
                await provider_session.send_realtime_input(
                    activity_end=types.ActivityEnd()
                )
            return

        action, utterance_active = _provider_action_for_audio_item(
            audio_chunk,
            utterance_active,
        )
        if action is None:
            continue
        if action == "activity_start":
            await provider_session.send_realtime_input(
                activity_start=types.ActivityStart()
            )
            continue
        if action == "activity_end":
            await provider_session.send_realtime_input(
                activity_end=types.ActivityEnd()
            )
            continue

        await provider_session.send_realtime_input(
            audio=types.Blob(
                data=audio_chunk,
                mime_type="audio/pcm;rate=16000",
            )
        )


def _build_live_config(target_language: str):
    """Build Live Translate config with client-controlled speech boundaries."""
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=True
            )
        ),
        translation_config=types.TranslationConfig(
            target_language_code=target_language,
            echo_target_language=True,
        ),
    )


def _provider_action_for_audio_item(
    item: bytes | object,
    utterance_active: bool,
) -> tuple[str | None, bool]:
    """Pure state transition used by the audio sender and offline tests."""
    if item is _START_UTTERANCE:
        return (None, True) if utterance_active else ("activity_start", True)
    if item is _END_UTTERANCE:
        return ("activity_end", False) if utterance_active else (None, False)
    if not utterance_active:
        raise RuntimeError("Audio frame received outside an active utterance.")
    return "audio", True


def _final_audio_queue_items(
    *,
    speech_active: bool,
    speech_candidate: bool,
    pending_frames: list[bytes],
    start_marker: object,
    end_marker: object,
    stop_marker: object,
) -> list[bytes | object]:
    """Build the lossless FIFO tail that closes the final utterance."""
    items: list[bytes | object] = []
    if speech_active or speech_candidate:
        if not speech_active:
            items.append(start_marker)
        items.extend(pending_frames)
        items.append(end_marker)
    items.append(stop_marker)
    return items


@router.websocket("/api/v1/live-sessions/{session_id}")
async def live_session_websocket(websocket: WebSocket, session_id: str):
    await websocket.accept()

    send_lock = asyncio.Lock()
    stop_requested = asyncio.Event()
    audio_queue: asyncio.Queue[bytes | object | None] = asyncio.Queue(
        maxsize=AUDIO_QUEUE_SIZE
    )
    pause_detector = PauseDetector(
        rms_threshold=LIVE_VAD_RMS_THRESHOLD,
        min_speech_duration_ms=LIVE_VAD_MIN_SPEECH_MS,
        silence_duration_ms=LIVE_VAD_SILENCE_MS,
    )
    last_vad_diagnostic_at = 0.0

    async def send_event(event: dict) -> None:
        try:
            async with send_lock:
                await websocket.send_json(event)
        except (WebSocketDisconnect, RuntimeError):
            stop_requested.set()

    try:
        start_message = await asyncio.wait_for(
            websocket.receive_json(),
            timeout=10,
        )

        if not isinstance(start_message, dict) or start_message.get("type") != "start":
            await send_event({
                "type": "error",
                "message": "The first WebSocket message must have type 'start'.",
            })
            await websocket.close(code=1008)
            return

        target_language = start_message.get("target_language_code")

        if target_language not in ("en", "vi"):
            await send_event({
                "type": "error",
                "message": "target_language_code must be 'en' or 'vi'.",
            })
            await websocket.close(code=1008)
            return

        active = await asyncio.to_thread(_is_session_active, session_id)

        if not active:
            await send_event({
                "type": "error",
                "message": "Session not found or is not active.",
            })
            await websocket.close(code=1008)
            return

        source_language = "en" if target_language == "vi" else "vi"

        transcript = TranscriptAssembler()
        boundary_tracker = VADBoundaryTracker(settle_ms=LIVE_SEGMENT_SETTLE_MS)
        pending_segment_id = str(uuid4())
        transcript_lock = asyncio.Lock()
        boundary_wakeup = asyncio.Event()
        turn_boundary_seen = False
        segments_saved = 0
        translation_repairs = 0
        vad_finalized_segments = 0
        vad_merged_pauses = 0
        pause_count = 0
        input_transcription_events = 0
        output_transcription_events = 0
        input_finished_events = 0
        output_finished_events = 0
        input_timing_events = 0
        output_timing_events = 0
        provider_generation_completions = 0
        provider_turn_completions = 0
        transcript_activity_changed = asyncio.Event()
        last_transcript_event_at: float | None = None
        last_provider_terminal_at: float | None = None
        stop_requested_at: float | None = None
        segments_saved_at_stop: int | None = None
        stop_drain_window: StopDrainWindow | None = None
        stop_drain_reason = "not_stopped"
        stop_drain_elapsed_ms = 0.0
        audio_sender_failed = False
        latency_tracker = latency_metrics.LiveLatencyTracker()

        def note_transcript_activity() -> None:
            nonlocal last_transcript_event_at
            last_transcript_event_at = asyncio.get_running_loop().time()
            if stop_drain_window is not None:
                stop_drain_window.note_transcript(last_transcript_event_at)
            transcript_activity_changed.set()

        def last_live_activity_at() -> float | None:
            values = [
                value
                for value in (
                    last_transcript_event_at,
                    last_provider_terminal_at,
                )
                if value is not None
            ]
            return max(values) if values else None

        def note_provider_terminal() -> None:
            nonlocal last_provider_terminal_at
            last_provider_terminal_at = asyncio.get_running_loop().time()
            if stop_drain_window is not None:
                stop_drain_window.note_terminal(last_provider_terminal_at)
            transcript_activity_changed.set()

        async def finalize_segment(force: bool = False) -> bool:
            nonlocal turn_boundary_seen, segments_saved
            nonlocal translation_repairs
            nonlocal pending_segment_id
            translation_recovered = False

            async with transcript_lock:
                source_text = transcript.source_for_save(force=force).strip()
                translated_text = transcript.translated_text.strip()

                if not source_text:
                    return False

                # Live Translate streams continuously, so a local VAD pause
                # or a provider boundary must be observed before committing.
                if not force:
                    if not turn_boundary_seen or not translated_text:
                        return False
                    # A period at the end is not enough: the live model can
                    # finish one part of a correction while omitting the rest.
                    # Keep the utterance buffered until more output arrives or
                    # Stop triggers a whole-source repair.
                    if not _stream_translation_ready_to_commit(
                        source_text,
                        translated_text,
                    ):
                        return False

                # Live Translate may stop without output-finished/turn-complete
                # markers. After Stop, repair an unfinished streamed translation
                # from the complete source before persisting it. This is a
                # finalization fallback only; normal live partials remain fast.
                repair_needed = _translation_needs_repair(
                    source_text,
                    translated_text,
                )
                if force and repair_needed:
                    try:
                        translated_text, translation_repaired = await asyncio.wait_for(
                            asyncio.to_thread(
                                _repair_translation_from_source,
                                source_text,
                                source_language,
                                target_language,
                                translated_text,
                            ),
                            timeout=LIVE_STOP_TRANSLATION_REPAIR_TIMEOUT_MS / 1000,
                        )
                        translation_recovered = translation_repaired
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Timed out repairing the final live translation for session %s.",
                            session_id,
                        )
                    except Exception:
                        logger.exception(
                            "Could not repair the final live translation for session %s.",
                            session_id,
                        )

                if translation_recovered:
                    translation_repairs += 1

                saved = await asyncio.to_thread(
                    _save_live_segment,
                    session_id,
                    target_language,
                    source_text,
                    translated_text,
                    translation_repaired=translation_recovered,
                    segment_id=pending_segment_id,
                )

                transcript.reset()
                boundary_tracker.reset()
                turn_boundary_seen = False
                segments_saved += 1
                pending_segment_id = str(uuid4())

            await send_event({
                "type": "segment_final",
                "segment_id": saved["segment_id"],
                "session_id": saved["session_id"],
                "sequence_number": saved["sequence_number"],
                "source_language": source_language,
                "target_language": target_language,
                "source_text": saved["source_text"],
                "en": saved["en"],
                "vn": saved["vn"],
                "status": saved["status"],
                "translation_recovered": translation_recovered,
                "created_at": saved["created_at"],
            })
            return True

        async def finalize_after_vad_pause() -> None:
            """Commit text after a pause once both transcripts go quiet."""
            nonlocal turn_boundary_seen
            nonlocal vad_finalized_segments
            nonlocal vad_merged_pauses

            loop = asyncio.get_running_loop()
            while True:
                await boundary_wakeup.wait()
                boundary_wakeup.clear()

                while boundary_tracker.pending_pauses:
                    has_source = bool(
                        transcript.source_text.strip()
                    )
                    has_translation = bool(transcript.translated_text.strip())
                    source_sentence_complete = ends_with_sentence_punctuation(
                        transcript.source_text
                    )
                    translation_sentence_complete = ends_with_sentence_punctuation(
                        transcript.translated_text
                    )

                    if (
                        not has_source
                        or not has_translation
                        or not source_sentence_complete
                        or not translation_sentence_complete
                        or pause_detector.speech_active
                    ):
                        await boundary_wakeup.wait()
                        boundary_wakeup.clear()
                        continue

                    if boundary_tracker.is_ready(
                        loop.time(),
                        has_source=has_source,
                        has_translation=has_translation,
                        source_sentence_complete=source_sentence_complete,
                        translation_sentence_complete=translation_sentence_complete,
                        speech_active=False,
                    ):
                        pauses_in_batch = boundary_tracker.pending_pauses
                        turn_boundary_seen = True
                        committed = await finalize_segment()
                        if committed:
                            vad_finalized_segments += 1
                            vad_merged_pauses += max(0, pauses_in_batch - 1)
                            break
                        turn_boundary_seen = False
                        await boundary_wakeup.wait()
                        boundary_wakeup.clear()
                        continue

                    next_wake_at = boundary_tracker.ready_at()
                    if next_wake_at is None:
                        break

                    timeout = max(0.01, next_wake_at - loop.time())
                    try:
                        await asyncio.wait_for(boundary_wakeup.wait(), timeout=timeout)
                    except asyncio.TimeoutError:
                        pass
                    boundary_wakeup.clear()

        async def receive_client_audio() -> None:
            nonlocal last_vad_diagnostic_at, pause_count
            nonlocal stop_requested_at, segments_saved_at_stop
            nonlocal stop_drain_reason
            pending_audio_frames: deque[bytes] = deque(
                maxlen=LIVE_VAD_PREROLL_FRAMES
            )

            async def enqueue_audio_item(item: bytes | object) -> bool:
                try:
                    await asyncio.wait_for(audio_queue.put(item), timeout=2)
                    return True
                except asyncio.TimeoutError:
                    stop_requested.set()
                    await send_event({
                        "type": "error",
                        "message": (
                            "Audio queue is full. The stream is being stopped "
                            "to prevent unbounded buffering."
                        ),
                    })
                    return False

            try:
                while True:
                    message = await websocket.receive()

                    if message.get("type") == "websocket.disconnect":
                        stop_requested.set()
                        return

                    audio_chunk = message.get("bytes")

                    if audio_chunk is not None:
                        if len(audio_chunk) > MAX_AUDIO_FRAME_BYTES:
                            await send_event({
                                "type": "error",
                                "message": (
                                    "Audio frame is larger than 100 ms. "
                                    "Expected at most 3200 bytes of PCM16 audio."
                                ),
                            })
                            continue

                        was_speech_active = pause_detector.speech_active
                        try:
                            pause_detected = pause_detector.process_frame(audio_chunk)
                        except ValueError as exc:
                            await send_event({
                                "type": "error",
                                "message": f"Invalid PCM16 audio frame: {exc}",
                            })
                            continue

                        if pause_detector.last_rms >= LIVE_VAD_RMS_THRESHOLD:
                            latency_tracker.note_audio(
                                asyncio.get_running_loop().time()
                            )

                        if was_speech_active:
                            if not await enqueue_audio_item(audio_chunk):
                                return
                        else:
                            pending_audio_frames.append(audio_chunk)
                            if pause_detector.speech_started_now:
                                boundary_wakeup.set()
                                if not await enqueue_audio_item(_START_UTTERANCE):
                                    return
                                for buffered_frame in pending_audio_frames:
                                    if not await enqueue_audio_item(buffered_frame):
                                        return
                                pending_audio_frames.clear()

                        if pause_detected:
                            pause_count += 1
                            boundary_tracker.note_pause(
                                asyncio.get_running_loop().time()
                            )
                            boundary_wakeup.set()
                            if not await enqueue_audio_item(_END_UTTERANCE):
                                return
                            await send_event({
                                "type": "speech_pause_detected",
                                "pause_number": pause_count,
                                "silence_ms": LIVE_VAD_SILENCE_MS,
                            })

                        now = asyncio.get_running_loop().time()
                        if now - last_vad_diagnostic_at >= 1.0:
                            last_vad_diagnostic_at = now
                            await send_event({
                                "type": "vad_diagnostic",
                                "rms": round(pause_detector.last_rms, 1),
                                "threshold": LIVE_VAD_RMS_THRESHOLD,
                                "speech_active": pause_detector.speech_active,
                                "speech_candidate_ms": round(
                                    pause_detector.speech_candidate_ms
                                ),
                                "silence_progress_ms": round(
                                    pause_detector.silence_progress_ms
                                ),
                                "min_speech_ms": LIVE_VAD_MIN_SPEECH_MS,
                                "required_silence_ms": LIVE_VAD_SILENCE_MS,
                            })

                        continue

                    text_message = message.get("text")

                    if text_message:
                        import json

                        try:
                            command = json.loads(text_message)
                        except json.JSONDecodeError:
                            await send_event({
                                "type": "error",
                                "message": "Invalid WebSocket control message.",
                            })
                            continue

                        if (
                            isinstance(command, dict)
                            and command.get("type") == "stop"
                        ):
                            stop_requested_at = asyncio.get_running_loop().time()
                            segments_saved_at_stop = segments_saved
                            latency_tracker.note_stop(stop_requested_at)
                            stop_requested.set()
                            return

            except WebSocketDisconnect:
                stop_requested_at = asyncio.get_running_loop().time()
                segments_saved_at_stop = segments_saved
                latency_tracker.note_stop(stop_requested_at)
                stop_requested.set()
            finally:
                async def flush_audio_queue() -> None:
                    # The FIFO sentinel makes the sender deliver every pending
                    # frame and activity_end before it exits. Never evict audio.
                    for item in _final_audio_queue_items(
                        speech_active=pause_detector.speech_active,
                        speech_candidate=pause_detector.speech_candidate_ms > 0,
                        pending_frames=list(pending_audio_frames),
                        start_marker=_START_UTTERANCE,
                        end_marker=_END_UTTERANCE,
                        stop_marker=None,
                    ):
                        await audio_queue.put(item)

                try:
                    await asyncio.wait_for(
                        flush_audio_queue(),
                        timeout=LIVE_AUDIO_QUEUE_DRAIN_TIMEOUT_MS / 1000,
                    )
                except asyncio.TimeoutError:
                    stop_drain_reason = "audio_queue_drain_timeout"
                    logger.error(
                        "Timed out flushing the final live audio queue for session %s; queued audio was retained, not discarded.",
                        session_id,
                    )
                    if sender_task is not None and not sender_task.done():
                        sender_task.cancel()

        async def send_audio_to_gemini(provider_session) -> None:
            await _forward_audio_queue(audio_queue, provider_session)

        async def receive_gemini_events(provider_session) -> None:
            nonlocal turn_boundary_seen
            nonlocal last_provider_terminal_at
            nonlocal input_transcription_events
            nonlocal output_transcription_events
            nonlocal input_finished_events
            nonlocal output_finished_events
            nonlocal input_timing_events
            nonlocal output_timing_events
            nonlocal provider_generation_completions
            nonlocal provider_turn_completions

            async with asyncio.timeout(15 * 60):
                async for response in provider_session.receive():
                    content = response.server_content

                    if content is None:
                        continue

                    if (
                        content.input_transcription
                        and content.input_transcription.finished is True
                    ):
                        input_finished_events += 1
                    if content.input_transcription and (
                        content.input_transcription.start_offset is not None
                        or content.input_transcription.end_offset is not None
                    ):
                        input_timing_events += 1

                    output_transcription_finished = bool(
                        content.output_transcription
                        and content.output_transcription.finished is True
                    )
                    if output_transcription_finished:
                        output_finished_events += 1
                    if content.output_transcription and (
                        content.output_transcription.start_offset is not None
                        or content.output_transcription.end_offset is not None
                    ):
                        output_timing_events += 1

                    if (
                        content.interim_input_transcription
                        and content.interim_input_transcription.text
                    ):
                        note_transcript_activity()
                        latency_tracker.note_source(
                            asyncio.get_running_loop().time()
                        )
                        async with transcript_lock:
                            transcript.set_source_preview(
                                content.interim_input_transcription.text
                            )
                            preview = transcript.source_for_display()
                            boundary_tracker.note_transcript(
                                asyncio.get_running_loop().time()
                            )
                        boundary_wakeup.set()
                        await send_event({
                            "type": "input_partial",
                            "text": preview,
                        })

                    if (
                        content.input_transcription
                        and content.input_transcription.text
                    ):
                        note_transcript_activity()
                        latency_tracker.note_source(
                            asyncio.get_running_loop().time()
                        )
                        input_transcription_events += 1
                        text = content.input_transcription.text
                        async with transcript_lock:
                            assembled = transcript.add_source_fragment(text)
                            boundary_tracker.note_transcript(
                                asyncio.get_running_loop().time()
                            )
                        boundary_wakeup.set()
                        await send_event({
                            "type": "input_partial",
                            "text": assembled,
                        })

                        await finalize_segment()

                    if (
                        content.output_transcription
                        and content.output_transcription.text
                    ):
                        note_transcript_activity()
                        latency_tracker.note_translation(
                            asyncio.get_running_loop().time()
                        )
                        output_transcription_events += 1
                        text = content.output_transcription.text
                        async with transcript_lock:
                            assembled = transcript.add_translation_fragment(text)
                            boundary_tracker.note_transcript(
                                asyncio.get_running_loop().time()
                            )
                        boundary_wakeup.set()
                        await send_event({
                            "type": "output_partial",
                            "text": assembled,
                        })

                        await finalize_segment()

                    if content.model_turn and content.model_turn.parts:
                        import base64

                        for part in content.model_turn.parts:
                            if part.inline_data and part.inline_data.data:
                                await send_event({
                                    "type": "translated_audio",
                                    "mime_type": (
                                        part.inline_data.mime_type
                                        or "audio/pcm;rate=24000"
                                    ),
                                    "data": base64.b64encode(
                                        part.inline_data.data
                                    ).decode("ascii"),
                                })

                    provider_boundary_seen = False
                    if content.generation_complete:
                        provider_generation_completions += 1
                        provider_boundary_seen = True
                    if output_transcription_finished:
                        provider_boundary_seen = True
                    if content.turn_complete:
                        provider_turn_completions += 1
                        provider_boundary_seen = True

                    if provider_boundary_seen:
                        note_provider_terminal()
                        turn_boundary_seen = True
                        await finalize_segment()

                    if content.turn_complete and stop_requested.is_set():
                        return

        config = _build_live_config(target_language)

        receiver_task = None
        sender_task = None
        provider_task = None
        boundary_task = None

        # Begin draining client messages before connecting to Gemini. This
        # lets the bounded queue absorb the first few microphone frames.
        receiver_task = asyncio.create_task(receive_client_audio())

        try:
            async with LIVE_CLIENT.aio.live.connect(
                model=LIVE_MODEL,
                config=config,
            ) as provider_session:
                await send_event({
                    "type": "session_ready",
                    "session_id": session_id,
                    "source_language": source_language,
                    "target_language": target_language,
                    "audio_format": "pcm_s16le/16000/mono",
                    "vad": {
                        "rms_threshold": LIVE_VAD_RMS_THRESHOLD,
                        "min_speech_ms": LIVE_VAD_MIN_SPEECH_MS,
                        "silence_ms": LIVE_VAD_SILENCE_MS,
                        "preroll_ms": LIVE_VAD_PREROLL_FRAMES * 100,
                        "provider_activity_mode": "manual",
                    },
                })

                sender_task = asyncio.create_task(
                    send_audio_to_gemini(provider_session)
                )
                provider_task = asyncio.create_task(
                    receive_gemini_events(provider_session)
                )
                boundary_task = asyncio.create_task(
                    finalize_after_vad_pause()
                )

                done, _ = await asyncio.wait(
                    {receiver_task, provider_task, sender_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if sender_task in done:
                    try:
                        await sender_task
                    except asyncio.CancelledError:
                        audio_sender_failed = True
                        if stop_drain_reason != "audio_queue_drain_timeout":
                            stop_drain_reason = "audio_sender_cancelled"
                        logger.error(
                            "Gemini audio sender was cancelled for session %s; preserving received transcript.",
                            session_id,
                        )
                        if not receiver_task.done():
                            receiver_task.cancel()
                        if not provider_task.done():
                            provider_task.cancel()
                        for task in (receiver_task, provider_task):
                            try:
                                await task
                            except (
                                asyncio.CancelledError,
                                WebSocketDisconnect,
                            ):
                                pass
                    except Exception:
                        audio_sender_failed = True
                        stop_drain_reason = "audio_sender_error"
                        logger.exception(
                            "Gemini audio sender failed for session %s; preserving received transcript.",
                            session_id,
                        )
                        receiver_task.cancel()
                        try:
                            await receiver_task
                        except asyncio.CancelledError:
                            pass
                        if not provider_task.done():
                            provider_task.cancel()
                            try:
                                await provider_task
                            except asyncio.CancelledError:
                                pass
                    else:
                        # A normal sender exit means it consumed the FIFO
                        # sentinel that receive_client_audio enqueued at Stop.
                        if not receiver_task.done():
                            await receiver_task
                            done.add(receiver_task)

                if audio_sender_failed:
                    pass
                elif receiver_task in done:
                    await receiver_task
                    # First drain every PCM frame and the final activity_end
                    # marker from the bounded queue. Starting the provider
                    # timeout before this task finishes can truncate the last
                    # syllable when Gemini is still receiving audio.
                    if audio_sender_failed:
                        stop_drain_reason = "audio_sender_error"
                    elif sender_task is not None and not sender_task.done():
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(sender_task), timeout=20
                            )
                        except asyncio.TimeoutError:
                            stop_drain_reason = "audio_sender_timeout"
                            logger.error(
                                "Timed out draining the live audio queue for session %s.",
                                session_id,
                            )
                            sender_task.cancel()
                            try:
                                await sender_task
                            except asyncio.CancelledError:
                                pass
                        except asyncio.CancelledError:
                            audio_sender_failed = True
                            if stop_drain_reason != "audio_queue_drain_timeout":
                                stop_drain_reason = "audio_sender_cancelled"
                            logger.error(
                                "Gemini audio sender was cancelled for session %s; preserving received transcript.",
                                session_id,
                            )
                        except Exception:
                            audio_sender_failed = True
                            stop_drain_reason = "audio_sender_error"
                            logger.exception(
                                "Gemini audio sender failed for session %s; preserving received transcript.",
                                session_id,
                            )

                    await send_event({
                        "type": "stop_draining",
                        "session_id": session_id,
                        "message": "Đã xả audio cuối; đang chờ transcript/bản dịch cuối.",
                    })
                    drain_started_at = asyncio.get_running_loop().time()
                    if not audio_sender_failed and stop_drain_reason not in {
                        "audio_queue_drain_timeout",
                        "audio_sender_timeout",
                        "audio_sender_error",
                        "audio_sender_cancelled",
                    }:
                        stop_drain_window = StopDrainWindow(
                            started_at=drain_started_at,
                            quiet_seconds=LIVE_STOP_QUIET_MS / 1000,
                            timeout_seconds=LIVE_STOP_DRAIN_TIMEOUT_MS / 1000,
                            last_activity_at=last_live_activity_at(),
                            terminal_at=last_provider_terminal_at,
                        )
                        stop_drain_reason = await _wait_for_live_drain(
                            provider_task,
                            transcript_activity_changed,
                            stop_drain_window,
                        )
                    stop_drain_elapsed_ms = round(
                        (asyncio.get_running_loop().time() - drain_started_at) * 1000
                    )

                    if not provider_task.done():
                        provider_task.cancel()
                    try:
                        await provider_task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        stop_drain_reason = "provider_error"
                        logger.exception(
                            "Gemini Live closed with an error for session %s; preserving buffered transcript.",
                            session_id,
                        )
                else:
                    try:
                        await provider_task
                    except Exception:
                        stop_drain_reason = "provider_error"
                        logger.exception(
                            "Gemini Live closed with an error for session %s; preserving buffered transcript.",
                            session_id,
                        )
                    receiver_task.cancel()
                    try:
                        await receiver_task
                    except asyncio.CancelledError:
                        pass
                    if sender_task is not None and not sender_task.done():
                        try:
                            await asyncio.wait_for(sender_task, timeout=20)
                        except asyncio.TimeoutError:
                            sender_task.cancel()
                    if stop_drain_reason == "not_stopped":
                        stop_drain_reason = "provider_closed"

        finally:
            for task in (receiver_task, sender_task, provider_task, boundary_task):
                if task is not None and not task.done():
                    task.cancel()

            for task in (receiver_task, sender_task, provider_task, boundary_task):
                if task is not None:
                    try:
                        await task
                    except (
                        asyncio.CancelledError,
                        WebSocketDisconnect,
                        asyncio.TimeoutError,
                    ):
                        pass
                    except Exception:
                        logger.exception("Live translation task failed.")

        # Save any remaining text when the user stops or the provider closes.
        # If translation text is missing, save the source as a failed segment.
        await finalize_segment(force=True)

        if (
            stop_requested_at is not None
            and segments_saved_at_stop is not None
            and segments_saved > segments_saved_at_stop
        ):
            latency_tracker.note_final_persisted(
                asyncio.get_running_loop().time()
            )

        latency_samples = latency_tracker.samples()
        if latency_samples:
            try:
                await asyncio.to_thread(
                    latency_metrics.record_samples,
                    session_id,
                    latency_samples,
                )
            except Exception:
                logger.exception(
                    "Could not save live latency metrics for session %s.",
                    session_id,
                )

        await send_event({
            "type": "stream_stopped",
            "session_id": session_id,
            "segments_saved": segments_saved,
            "translation_repairs": translation_repairs,
            "stop_drain_reason": stop_drain_reason,
            "stop_drain_elapsed_ms": stop_drain_elapsed_ms,
            "latency_samples_ms": latency_samples,
            "source_snapshot_replacements": transcript.source_snapshot_replacements,
            "stale_source_snapshots_ignored": transcript.stale_source_snapshots_ignored,
            "vad_finalized_segments": vad_finalized_segments,
            "vad_merged_pauses": vad_merged_pauses,
            "segment_settle_ms": LIVE_SEGMENT_SETTLE_MS,
            "pauses_detected": pause_count,
            "input_transcription_events": input_transcription_events,
            "output_transcription_events": output_transcription_events,
            "input_finished_events": input_finished_events,
            "output_finished_events": output_finished_events,
            "input_timing_events": input_timing_events,
            "output_timing_events": output_timing_events,
            "provider_generation_completions": provider_generation_completions,
            "provider_turn_completions": provider_turn_completions,
            "session_remains_active": True,
        })

        try:
            await websocket.close(code=1000)
        except RuntimeError:
            pass

    except asyncio.TimeoutError:
        await send_event({
            "type": "error",
            "message": "Timed out while waiting for the start message.",
        })
        try:
            await websocket.close(code=1008)
        except RuntimeError:
            pass

    except WebSocketDisconnect:
        pass

    except Exception:
        logger.exception("Session-aware live translation failed.")
        await send_event({
            "type": "error",
            "message": (
                "Live translation failed. Check the backend terminal "
                "for details."
            ),
        })
        try:
            await websocket.close(code=1011)
        except RuntimeError:
            pass
