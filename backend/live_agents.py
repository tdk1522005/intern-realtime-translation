"""Small agent team for one live translation session.

The coordinator owns session setup and delegates narrow jobs to specialist
agents. The audio, transcript, memory, and metrics agents are deterministic
workers; only GeminiTranslationAgent calls an AI model. This keeps extra model
calls out of the real-time audio path.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator
from uuid import NAMESPACE_URL, uuid4, uuid5

from google.genai import types

import database
import latency_metrics
from audio_vad import PauseDetector
from transcript_assembler import (
    TranscriptAssembler,
    VADBoundaryTracker,
    count_sentence_endings,
    ends_with_sentence_punctuation,
)


logger = logging.getLogger(__name__)
RESUMPTION_HANDLE_TTL = timedelta(hours=2)


@dataclass(frozen=True)
class LiveSessionContext:
    """Typed hand-off shared by the coordinator and its specialist agents."""

    session_id: str
    source_language: str
    target_language: str
    model_name: str
    resumption_handle: str | None
    pending_segment_id: str
    persisted_segment_count: int
    last_sequence_number: int


def next_segment_id(session_id: str, segment_id: str) -> str:
    """Return a stable successor key so retries can advance state idempotently."""
    return str(
        uuid5(
            NAMESPACE_URL,
            f"realtime-translation:{session_id}:{segment_id}:next",
        )
    )


class AudioVADAgent:
    """Own audio format boundary detection configuration for a live session."""

    def create_detector(
        self,
        *,
        rms_threshold: float,
        min_speech_duration_ms: int,
        silence_duration_ms: int,
    ) -> PauseDetector:
        return PauseDetector(
            rms_threshold=rms_threshold,
            min_speech_duration_ms=min_speech_duration_ms,
            silence_duration_ms=silence_duration_ms,
        )


class TranscriptAgent:
    """Own replaceable partial and final transcript assembly state."""

    def create_buffers(
        self,
        *,
        settle_ms: int,
    ) -> tuple[TranscriptAssembler, VADBoundaryTracker]:
        return TranscriptAssembler(), VADBoundaryTracker(settle_ms=settle_ms)


class MemoryAgent:
    """Load durable session state and persist completed bilingual segments."""

    def __init__(self, database_module=database) -> None:
        self.database = database_module

    def prepare_session(
        self,
        session_id: str,
        target_language: str,
        model_name: str,
        *,
        preferred_pending_segment_id: str | None = None,
    ) -> LiveSessionContext | None:
        if target_language not in ("en", "vi"):
            raise ValueError("target_language must be 'en' or 'vi'.")

        source_language = "en" if target_language == "vi" else "vi"
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()

        with self.database.get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                "SELECT status FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if session is None or session["status"] != "active":
                return None

            state = connection.execute(
                """
                SELECT resumption_handle, resumption_updated_at,
                       pending_segment_id
                FROM live_provider_context
                WHERE session_id = ? AND model_name = ? AND target_language = ?
                """,
                (session_id, model_name, target_language),
            ).fetchone()

            handle = state["resumption_handle"] if state else None
            pending_id = state["pending_segment_id"] if state else None
            if handle and state["resumption_updated_at"]:
                try:
                    handle_updated_at = datetime.fromisoformat(
                        state["resumption_updated_at"]
                    )
                    if now - handle_updated_at > RESUMPTION_HANDLE_TTL:
                        handle = None
                except ValueError:
                    handle = None

            pending_id = str(
                pending_id or preferred_pending_segment_id or uuid4()
            )
            connection.execute(
                """
                INSERT INTO live_provider_context (
                    session_id, model_name, target_language,
                    resumption_handle, resumption_updated_at,
                    pending_segment_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, model_name, target_language)
                DO UPDATE SET
                    resumption_handle = excluded.resumption_handle,
                    resumption_updated_at = CASE
                        WHEN excluded.resumption_handle IS NULL THEN NULL
                        ELSE live_provider_context.resumption_updated_at
                    END,
                    pending_segment_id = excluded.pending_segment_id,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    model_name,
                    target_language,
                    handle,
                    state["resumption_updated_at"] if handle and state else None,
                    pending_id,
                    now_text,
                ),
            )
            segment_state = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(MAX(sequence_number), 0) AS max_sequence
                FROM segments WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()

        return LiveSessionContext(
            session_id=session_id,
            source_language=source_language,
            target_language=target_language,
            model_name=model_name,
            resumption_handle=handle,
            pending_segment_id=pending_id,
            persisted_segment_count=int(segment_state["total"]),
            last_sequence_number=int(segment_state["max_sequence"]),
        )

    def save_resumption_update(
        self,
        context: LiveSessionContext,
        update: Any,
    ) -> bool:
        """Save the newest resumable provider token; clear stale tokens."""
        handle = (getattr(update, "new_handle", None) or "").strip()
        resumable = getattr(update, "resumable", None)
        if not handle or resumable is False:
            self.clear_resumption_handle(context)
            return False

        updated_at = datetime.now(timezone.utc).isoformat()
        with self.database.get_connection() as connection:
            connection.execute(
                """
                UPDATE live_provider_context
                SET resumption_handle = ?, resumption_updated_at = ?,
                    updated_at = ?
                WHERE session_id = ? AND model_name = ? AND target_language = ?
                """,
                (
                    handle,
                    updated_at,
                    updated_at,
                    context.session_id,
                    context.model_name,
                    context.target_language,
                ),
            )
        return True

    def clear_resumption_handle(self, context: LiveSessionContext) -> None:
        with self.database.get_connection() as connection:
            connection.execute(
                """
                UPDATE live_provider_context
                SET resumption_handle = NULL, resumption_updated_at = NULL,
                    updated_at = ?
                WHERE session_id = ? AND model_name = ? AND target_language = ?
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    context.session_id,
                    context.model_name,
                    context.target_language,
                ),
            )

    def save_live_segment(
        self,
        context: LiveSessionContext,
        source_text: str,
        translated_text: str,
        *,
        translation_repaired: bool = False,
        segment_id: str | None = None,
    ) -> dict:
        source_text = source_text.strip()
        translated_text = translated_text.strip()
        if not source_text:
            raise ValueError("Cannot save a live segment without source text.")

        source_language = context.source_language
        target_language = context.target_language
        en_text = source_text if source_language == "en" else translated_text or None
        vn_text = source_text if source_language == "vi" else translated_text or None
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
        segment_id = str(segment_id or context.pending_segment_id)
        next_pending_id = next_segment_id(context.session_id, segment_id)
        created_at = datetime.now(timezone.utc).isoformat()

        with self.database.get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                "SELECT status FROM sessions WHERE session_id = ?",
                (context.session_id,),
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
                    existing["session_id"] == context.session_id
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
                saved = dict(existing)
                self._advance_pending_id(
                    connection, context, segment_id, next_pending_id, created_at
                )
                return saved

            state = connection.execute(
                """
                SELECT pending_segment_id FROM live_provider_context
                WHERE session_id = ? AND model_name = ? AND target_language = ?
                """,
                (context.session_id, context.model_name, target_language),
            ).fetchone()
            if state and state["pending_segment_id"] != segment_id:
                raise ValueError(
                    "This live segment belongs to an older connection state. "
                    "Reload session state before persisting another segment."
                )

            row = connection.execute(
                """
                SELECT COALESCE(MAX(sequence_number), 0) AS max_number
                FROM segments WHERE session_id = ?
                """,
                (context.session_id,),
            ).fetchone()
            sequence_number = int(row["max_number"] or 0) + 1
            connection.execute(
                """
                INSERT INTO segments (
                    segment_id, session_id, sequence_number, source_text,
                    source_language, en, vn, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    segment_id,
                    context.session_id,
                    sequence_number,
                    source_text,
                    source_language,
                    en_text,
                    vn_text,
                    segment_status,
                    created_at,
                ),
            )
            self._advance_pending_id(
                connection, context, segment_id, next_pending_id, created_at
            )
            saved = connection.execute(
                "SELECT * FROM segments WHERE segment_id = ?",
                (segment_id,),
            ).fetchone()
            return dict(saved)

    def _advance_pending_id(
        self,
        connection,
        context: LiveSessionContext,
        segment_id: str,
        next_pending_id: str,
        updated_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO live_provider_context (
                session_id, model_name, target_language,
                pending_segment_id, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_id, model_name, target_language)
            DO UPDATE SET
                pending_segment_id = CASE
                    WHEN live_provider_context.pending_segment_id = ?
                    THEN excluded.pending_segment_id
                    ELSE live_provider_context.pending_segment_id
                END,
                updated_at = excluded.updated_at
            """,
            (
                context.session_id,
                context.model_name,
                context.target_language,
                next_pending_id,
                updated_at,
                segment_id,
            ),
        )


class GeminiTranslationAgent:
    """Adapt a session context to Gemini Live and recover from stale handles."""

    @staticmethod
    def build_config(
        target_language: str,
        resumption_handle: str | None = None,
    ):
        config = types.LiveConnectConfig(
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
        if resumption_handle:
            config.session_resumption = types.SessionResumptionConfig(
                handle=resumption_handle,
                transparent=False,
            )
        return config

    @asynccontextmanager
    async def connect(
        self,
        client,
        context: LiveSessionContext,
        memory_agent: MemoryAgent,
    ) -> AsyncIterator[tuple[Any, str]]:
        async with AsyncExitStack() as stack:
            resume_attempted = bool(context.resumption_handle)
            try:
                provider_session = await stack.enter_async_context(
                    client.aio.live.connect(
                        model=context.model_name,
                        config=self.build_config(
                            context.target_language,
                            context.resumption_handle,
                        ),
                    )
                )
                connection_mode = "resumed" if resume_attempted else "fresh"
            except Exception:
                if not resume_attempted:
                    raise
                logger.warning(
                    "Gemini rejected the saved resumption handle for session %s; "
                    "opening a fresh provider stream.",
                    context.session_id,
                    exc_info=True,
                )
                await asyncio.to_thread(
                    memory_agent.clear_resumption_handle, context
                )
                provider_session = await stack.enter_async_context(
                    client.aio.live.connect(
                        model=context.model_name,
                        config=self.build_config(context.target_language),
                    )
                )
                connection_mode = "resume_fallback"

            yield provider_session, connection_mode


class MetricsAgent:
    """Create per-connection trackers and persist their measured samples."""

    @staticmethod
    def create_tracker() -> latency_metrics.LiveLatencyTracker:
        return latency_metrics.LiveLatencyTracker()

    @staticmethod
    def persist(session_id: str, samples: dict[str, float]) -> int:
        return latency_metrics.record_samples(session_id, samples)

    @staticmethod
    def persist_provider_connection(
        session_id: str,
        latency_ms: float,
        connection_mode: str,
    ) -> bool:
        return latency_metrics.record_provider_connection_sample(
            session_id,
            latency_ms,
            connection_mode,
        )


class SessionConnectionAgent:
    """Prevent two local WebSockets from mutating one session concurrently."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    async def try_acquire(self, session_id: str) -> bool:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = self._locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            return False
        await lock.acquire()
        return True

    def release(self, session_id: str) -> None:
        lock = self._locks.get(session_id)
        if lock is None or not lock.locked():
            return
        lock.release()
        if not lock.locked():
            self._locks.pop(session_id, None)


_SESSION_CONNECTION_AGENT = SessionConnectionAgent()


class LiveSessionOrchestrator:
    """Parent coordinator that prepares and delegates one live session."""

    def __init__(self, model_name: str, database_module=database) -> None:
        self.model_name = model_name
        self.memory = MemoryAgent(database_module)
        self.audio = AudioVADAgent()
        self.transcript = TranscriptAgent()
        self.translation = GeminiTranslationAgent()
        self.metrics = MetricsAgent()
        self.connections = _SESSION_CONNECTION_AGENT

    def prepare(
        self,
        session_id: str,
        target_language: str,
        *,
        preferred_pending_segment_id: str | None = None,
    ) -> LiveSessionContext | None:
        return self.memory.prepare_session(
            session_id,
            target_language,
            self.model_name,
            preferred_pending_segment_id=preferred_pending_segment_id,
        )
