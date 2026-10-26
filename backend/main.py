
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

import database
import gemini_service
import gemini_live_service

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Live Translation Backend",
    description="Backend for English-Vietnamese live translation",
    version="0.4.0",
)


# =========================================================
# Request models
# =========================================================

class TranslationRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    source_language: Literal["en", "vi"]

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Text must not be empty.")
        return value


class SegmentRequest(BaseModel):
    segment_id: UUID
    sequence_number: int = Field(ge=1)
    source_text: str = Field(min_length=1, max_length=5000)
    source_language: Literal["en", "vi"]

    @field_validator("source_text")
    @classmethod
    def validate_source_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Source text must not be empty.")
        return value


# =========================================================
# Response models
# =========================================================

class TranslationResponse(BaseModel):
    status: Literal["success", "pending", "error"]
    source_language: Literal["en", "vi"]
    en: str
    vn: str


class PresentationResponse(BaseModel):
    presentation_id: str
    status: str
    started_at: str


class SessionResponse(BaseModel):
    session_id: str
    presentation_id: str
    session_number: int
    status: str
    started_at: str


class SegmentResponse(BaseModel):
    segment_id: str
    session_id: str
    sequence_number: int
    source_text: str
    source_language: Literal["en", "vi"]
    en: str | None
    vn: str | None
    status: str
    created_at: str


class SessionStatusResponse(BaseModel):
    session_id: str
    presentation_id: str
    session_number: int
    status: str
    started_at: str
    ended_at: str | None
    total_segments: int
    completed_segments: int
    failed_segments: int
    pending_segments: int


class TranscriptResponse(BaseModel):
    session_id: str
    session_status: str
    ready: bool
    total_segments: int
    completed_segments: int
    failed_segments: int
    pending_segments: int
    missing_sequence_numbers: list[int]
    segments: list[SegmentResponse]


class SummarySegmentInput(BaseModel):
    segment_id: str
    sequence_number: int
    source_text: str
    source_language: Literal["en", "vi"]
    en: str
    vn: str


class SummaryInputResponse(BaseModel):
    session_id: str
    presentation_id: str
    session_number: int
    session_status: str
    segment_count: int
    transcript_en: str
    transcript_vn: str
    segments: list[SummarySegmentInput]


# =========================================================
# Startup
# =========================================================

@app.on_event("startup")
def startup():
    database.init_db()


# =========================================================
# Health check
# =========================================================

@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "translation-backend",
        "version": "0.4.0",
    }


@app.get("/live", response_class=HTMLResponse, include_in_schema=False)
def live_translation_page():
    page_path = Path(__file__).with_name("live.html")
    return HTMLResponse(page_path.read_text(encoding="utf-8"))


@app.websocket("/api/v1/live-translate")
async def live_translation_websocket(websocket: WebSocket):
    await websocket.accept()
    receiver_task = None
    translator_task = None

    try:
        start_message = await websocket.receive_json()
        if not isinstance(start_message, dict) or start_message.get("type") != "start":
            await websocket.send_json({
                "type": "error",
                "message": "The first message must be a start configuration.",
            })
            await websocket.close(code=1008)
            return

        target_language_code = start_message.get("target_language_code", "vi")
        if target_language_code not in ("en", "vi"):
            await websocket.send_json({
                "type": "error",
                "message": "target_language_code must be 'en' or 'vi'.",
            })
            await websocket.close(code=1008)
            return

        audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=20)

        async def emit_event(event: dict) -> None:
            await websocket.send_json(event)

        async def receive_audio() -> None:
            try:
                while True:
                    message = await websocket.receive()
                    if message.get("type") == "websocket.disconnect":
                        break
                    audio_chunk = message.get("bytes")
                    if audio_chunk is not None:
                        if len(audio_chunk) > 16_000:
                            await websocket.send_json({
                                "type": "error",
                                "message": "Audio chunks must be 100 ms or smaller.",
                            })
                            continue
                        await asyncio.wait_for(audio_queue.put(audio_chunk), timeout=2)
                        continue

                    text_message = message.get("text")
                    if text_message:
                        command = json.loads(text_message)
                        if isinstance(command, dict) and command.get("type") == "stop":
                            break
            except WebSocketDisconnect:
                pass
            finally:
                try:
                    await asyncio.wait_for(audio_queue.put(None), timeout=2)
                except asyncio.TimeoutError:
                    logger.warning("Audio queue remained full while closing live session")

        receiver_task = asyncio.create_task(receive_audio())
        translator_task = asyncio.create_task(
            gemini_live_service.stream_microphone_translation(
                audio_queue=audio_queue,
                target_language_code=target_language_code,
                emit_event=emit_event,
            )
        )
        done, _ = await asyncio.wait(
            {receiver_task, translator_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if receiver_task in done:
            await receiver_task
            try:
                # Give Gemini a short window to return the last translated
                # phrase. A missing turn_complete must not lock the UI forever.
                await asyncio.wait_for(translator_task, timeout=5)
            except asyncio.TimeoutError:
                translator_task.cancel()
                try:
                    await translator_task
                except asyncio.CancelledError:
                    pass
        else:
            # The provider ended or failed while the client was still sending.
            await translator_task
            receiver_task.cancel()
            try:
                await receiver_task
            except asyncio.CancelledError:
                pass

        await websocket.send_json({"type": "session_complete"})
        await websocket.close(code=1000)

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.exception("Live translation WebSocket failed")
        try:
            await websocket.send_json({
                "type": "error",
                "message": "Live translation failed. Check the backend terminal for details.",
            })
        except Exception:
            pass
    finally:
        for task in (receiver_task, translator_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (receiver_task, translator_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, WebSocketDisconnect):
                    pass
                except Exception:
                    pass


# =========================================================
# Create presentation
# =========================================================

@app.post(
    "/api/v1/presentations",
    response_model=PresentationResponse,
    status_code=201,
)
def create_presentation():
    presentation_id = str(uuid4())
    started_at = datetime.now(timezone.utc).isoformat()

    try:
        with database.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO presentations (
                    presentation_id,
                    status,
                    started_at
                )
                VALUES (?, ?, ?)
                """,
                (presentation_id, "active", started_at),
            )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Could not create presentation.",
        ) from exc

    return PresentationResponse(
        presentation_id=presentation_id,
        status="active",
        started_at=started_at,
    )


# =========================================================
# Create session
# =========================================================

@app.post(
    "/api/v1/presentations/{presentation_id}/sessions",
    response_model=SessionResponse,
    status_code=201,
)
def create_session(presentation_id: str):
    session_id = str(uuid4())
    started_at = datetime.now(timezone.utc).isoformat()

    try:
        with database.get_connection() as connection:
            presentation = connection.execute(
                """
                SELECT presentation_id, status
                FROM presentations
                WHERE presentation_id = ?
                """,
                (presentation_id,),
            ).fetchone()

            if presentation is None:
                raise HTTPException(
                    status_code=404,
                    detail="Presentation not found.",
                )

            if presentation["status"] != "active":
                raise HTTPException(
                    status_code=409,
                    detail="Presentation is not active.",
                )

            latest_session = connection.execute(
                """
                SELECT MAX(session_number) AS max_number
                FROM sessions
                WHERE presentation_id = ?
                """,
                (presentation_id,),
            ).fetchone()

            session_number = (latest_session["max_number"] or 0) + 1

            connection.execute(
                """
                INSERT INTO sessions (
                    session_id,
                    presentation_id,
                    session_number,
                    status,
                    started_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    presentation_id,
                    session_number,
                    "active",
                    started_at,
                ),
            )

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Could not create session.",
        ) from exc

    return SessionResponse(
        session_id=session_id,
        presentation_id=presentation_id,
        session_number=session_number,
        status="active",
        started_at=started_at,
    )


# =========================================================
# Receive, translate, and save a segment
# =========================================================

def translate_and_update_segment(
    segment_id: str,
    source_text: str,
    source_language: str,
) -> SegmentResponse:
    """Translate a saved segment and persist its final state."""
    try:
        translation = gemini_service.translate_text(
            text=source_text,
            source_language=source_language,
        )
    except Exception as exc:
        logger.exception("Gemini translation failed for segment %s", segment_id)
        try:
            with database.get_connection() as connection:
                connection.execute(
                    "UPDATE segments SET status = ? WHERE segment_id = ?",
                    ("failed", segment_id),
                )
        except Exception:
            logger.exception("Could not mark segment %s as failed", segment_id)

        if getattr(exc, "code", None) == 503:
            raise HTTPException(
                status_code=503,
                detail="Gemini is temporarily unavailable. Please retry shortly.",
            ) from exc
        raise HTTPException(
            status_code=502,
            detail="Translation provider failed. The source segment was saved.",
        ) from exc

    try:
        with database.get_connection() as connection:
            connection.execute(
                """
                UPDATE segments
                SET en = ?, vn = ?, status = ?
                WHERE segment_id = ?
                """,
                (
                    translation["en"],
                    translation["vn"],
                    "completed",
                    segment_id,
                ),
            )
            segment = connection.execute(
                "SELECT * FROM segments WHERE segment_id = ?",
                (segment_id,),
            ).fetchone()
    except Exception as exc:
        logger.exception("Could not save translation for segment %s", segment_id)
        raise HTTPException(
            status_code=500,
            detail="Translation succeeded, but could not save its result.",
        ) from exc

    return SegmentResponse(**dict(segment))

@app.post(
    "/api/v1/sessions/{session_id}/segments",
    response_model=SegmentResponse,
    status_code=201,
)
def create_segment(
    session_id: str,
    request: SegmentRequest,
    response: Response,
):
    segment_id = str(request.segment_id)
    created_at = datetime.now(timezone.utc).isoformat()

    # Save the original text before calling Gemini. This keeps the source
    # segment even if the provider is temporarily unavailable.
    try:
        with database.get_connection() as connection:
            session = connection.execute(
                """
                SELECT session_id, status
                FROM sessions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()

            if session is None:
                raise HTTPException(
                    status_code=404,
                    detail="Session not found.",
                )

            if session["status"] != "active":
                raise HTTPException(
                    status_code=409,
                    detail="Session is not active.",
                )

            existing_segment = connection.execute(
                "SELECT * FROM segments WHERE segment_id = ?",
                (segment_id,),
            ).fetchone()

            if existing_segment is not None:
                same_request = (
                    existing_segment["session_id"] == session_id
                    and existing_segment["sequence_number"]
                    == request.sequence_number
                    and existing_segment["source_text"] == request.source_text
                    and existing_segment["source_language"]
                    == request.source_language
                )

                if not same_request:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "This segment_id already belongs to a different "
                            "segment or request."
                        ),
                    )

                response.status_code = 200
                return SegmentResponse(**dict(existing_segment))

            existing_sequence = connection.execute(
                """
                SELECT segment_id
                FROM segments
                WHERE session_id = ? AND sequence_number = ?
                """,
                (session_id, request.sequence_number),
            ).fetchone()

            if existing_sequence is not None:
                raise HTTPException(
                    status_code=409,
                    detail="This sequence number already exists in the session.",
                )

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
                    request.sequence_number,
                    request.source_text,
                    request.source_language,
                    None,
                    None,
                    "received",
                    created_at,
                ),
            )

    except HTTPException:
        raise
    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            # A simultaneous retry may have inserted the same request after
            # our initial lookup. Return that saved row if its payload matches.
            with database.get_connection() as connection:
                existing_segment = connection.execute(
                    "SELECT * FROM segments WHERE segment_id = ?",
                    (segment_id,),
                ).fetchone()

            if (
                existing_segment is not None
                and existing_segment["session_id"] == session_id
                and existing_segment["sequence_number"] == request.sequence_number
                and existing_segment["source_text"] == request.source_text
                and existing_segment["source_language"] == request.source_language
            ):
                response.status_code = 200
                return SegmentResponse(**dict(existing_segment))

            raise HTTPException(
                status_code=409,
                detail=(
                    "This segment_id or sequence number is already used "
                    "by another segment."
                ),
            ) from exc
        raise HTTPException(
            status_code=500,
            detail="Could not save segment.",
        ) from exc

    # Do not keep a SQLite transaction open while waiting for the model.
    return translate_and_update_segment(
        segment_id=segment_id,
        source_text=request.source_text,
        source_language=request.source_language,
    )


@app.post(
    "/api/v1/sessions/{session_id}/segments/{segment_id}/retry",
    response_model=SegmentResponse,
)
def retry_failed_segment(session_id: str, segment_id: UUID):
    segment_id = str(segment_id)

    try:
        with database.get_connection() as connection:
            session = connection.execute(
                "SELECT session_id, status FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()

            if session is None:
                raise HTTPException(status_code=404, detail="Session not found.")

            if session["status"] != "active":
                raise HTTPException(
                    status_code=409,
                    detail="Session is not active.",
                )

            segment = connection.execute(
                """
                SELECT * FROM segments
                WHERE session_id = ? AND segment_id = ?
                """,
                (session_id, segment_id),
            ).fetchone()

            if segment is None:
                raise HTTPException(status_code=404, detail="Segment not found.")

            if segment["status"] == "completed":
                return SegmentResponse(**dict(segment))

            if segment["status"] != "failed":
                raise HTTPException(
                    status_code=409,
                    detail="Only failed segments can be retried.",
                )

            # Claim the failed row atomically so two retry requests cannot both
            # call Gemini for the same segment at the same time.
            update = connection.execute(
                """
                UPDATE segments
                SET status = ?, en = NULL, vn = NULL
                WHERE session_id = ? AND segment_id = ? AND status = ?
                """,
                ("received", session_id, segment_id, "failed"),
            )

            if update.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail="Segment is already being retried.",
                )

            source_text = segment["source_text"]
            source_language = segment["source_language"]

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not prepare retry for segment %s", segment_id)
        raise HTTPException(
            status_code=500,
            detail="Could not prepare segment retry.",
        ) from exc

    return translate_and_update_segment(
        segment_id=segment_id,
        source_text=source_text,
        source_language=source_language,
    )


# =========================================================
# Text translation with Gemini
# =========================================================

@app.post(
    "/api/v1/translate",
    response_model=TranslationResponse,
)
def translate_text(request: TranslationRequest):
    try:
        result = gemini_service.translate_text(
            text=request.text,
            source_language=request.source_language,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        # Keep provider details and secrets out of the API response.
        logger.exception("Gemini translation request failed")
        if getattr(exc, "code", None) == 503:
            raise HTTPException(
                status_code=503,
                detail="Gemini is temporarily unavailable. Please retry shortly.",
            ) from exc
        raise HTTPException(
            status_code=502,
            detail="Translation provider failed.",
        ) from exc

    return TranslationResponse(
        status="success",
        source_language=request.source_language,
        en=result["en"],
        vn=result["vn"],
    )
@app.get(
    "/api/v1/sessions/{session_id}/segments",
    response_model=list[SegmentResponse],
)
def get_session_segments(session_id: str):
    try:
        with database.get_connection() as connection:
            session = connection.execute(
                """
                SELECT session_id
                FROM sessions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()

            if session is None:
                raise HTTPException(
                    status_code=404,
                    detail="Session not found.",
                )

            segments = connection.execute(
                """
                SELECT *
                FROM segments
                WHERE session_id = ?
                ORDER BY sequence_number ASC
                """,
                (session_id,),
            ).fetchall()

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Could not retrieve session segments.",
        ) from exc

    return [SegmentResponse(**dict(segment)) for segment in segments]


def _load_session_status(connection, session_id: str) -> SessionStatusResponse:
    row = connection.execute(
        """
        SELECT
            s.session_id,
            s.presentation_id,
            s.session_number,
            s.status,
            s.started_at,
            s.ended_at,
            COUNT(g.segment_id) AS total_segments,
            SUM(CASE WHEN g.status = 'completed' THEN 1 ELSE 0 END)
                AS completed_segments,
            SUM(CASE WHEN g.status = 'failed' THEN 1 ELSE 0 END)
                AS failed_segments,
            SUM(CASE WHEN g.segment_id IS NOT NULL
                      AND g.status NOT IN ('completed', 'failed')
                     THEN 1 ELSE 0 END) AS pending_segments
        FROM sessions AS s
        LEFT JOIN segments AS g ON g.session_id = s.session_id
        WHERE s.session_id = ?
        GROUP BY s.session_id
        """,
        (session_id,),
    ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Session not found.")

    return SessionStatusResponse(
        session_id=row["session_id"],
        presentation_id=row["presentation_id"],
        session_number=row["session_number"],
        status=row["status"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        total_segments=row["total_segments"] or 0,
        completed_segments=row["completed_segments"] or 0,
        failed_segments=row["failed_segments"] or 0,
        pending_segments=row["pending_segments"] or 0,
    )


@app.get(
    "/api/v1/sessions/{session_id}",
    response_model=SessionStatusResponse,
)
def get_session_status(session_id: str):
    try:
        with database.get_connection() as connection:
            return _load_session_status(connection, session_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not retrieve session status for %s", session_id)
        raise HTTPException(
            status_code=500,
            detail="Could not retrieve session status.",
        ) from exc


@app.get(
    "/api/v1/sessions/{session_id}/transcript",
    response_model=TranscriptResponse,
)
def get_session_transcript(session_id: str):
    try:
        with database.get_connection() as connection:
            status = _load_session_status(connection, session_id)
            rows = connection.execute(
                """
                SELECT * FROM segments
                WHERE session_id = ?
                ORDER BY sequence_number ASC
                """,
                (session_id,),
            ).fetchall()

        sequence_numbers = [row["sequence_number"] for row in rows]
        present = set(sequence_numbers)
        missing = [
            number
            for number in range(1, max(sequence_numbers, default=0) + 1)
            if number not in present
        ]
        ready = (
            status.failed_segments == 0
            and status.pending_segments == 0
            and not missing
        )

        return TranscriptResponse(
            session_id=session_id,
            session_status=status.status,
            ready=ready,
            total_segments=status.total_segments,
            completed_segments=status.completed_segments,
            failed_segments=status.failed_segments,
            pending_segments=status.pending_segments,
            missing_sequence_numbers=missing,
            segments=[SegmentResponse(**dict(row)) for row in rows],
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not retrieve transcript for session %s", session_id)
        raise HTTPException(
            status_code=500,
            detail="Could not retrieve session transcript.",
        ) from exc


@app.get(
    "/api/v1/sessions/{session_id}/summary-input",
    response_model=SummaryInputResponse,
)
def get_session_summary_input(session_id: str):
    try:
        with database.get_connection() as connection:
            status = _load_session_status(connection, session_id)
            rows = connection.execute(
                """
                SELECT segment_id, sequence_number, source_text,
                       source_language, en, vn, status
                FROM segments
                WHERE session_id = ?
                ORDER BY sequence_number ASC
                """,
                (session_id,),
            ).fetchall()

        if status.status != "finished":
            raise HTTPException(
                status_code=409,
                detail="Finish the session before exporting its summary input.",
            )

        if not rows:
            raise HTTPException(
                status_code=409,
                detail="Cannot export summary input for an empty session.",
            )

        if any(
            row["status"] != "completed" or not row["en"] or not row["vn"]
            for row in rows
        ):
            raise HTTPException(
                status_code=409,
                detail="Every segment must be completed before export.",
            )

        segments = [
            SummarySegmentInput(
                segment_id=row["segment_id"],
                sequence_number=row["sequence_number"],
                source_text=row["source_text"],
                source_language=row["source_language"],
                en=row["en"],
                vn=row["vn"],
            )
            for row in rows
        ]

        return SummaryInputResponse(
            session_id=status.session_id,
            presentation_id=status.presentation_id,
            session_number=status.session_number,
            session_status=status.status,
            segment_count=len(segments),
            transcript_en=" ".join(segment.en for segment in segments),
            transcript_vn=" ".join(segment.vn for segment in segments),
            segments=segments,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not build summary input for session %s", session_id)
        raise HTTPException(
            status_code=500,
            detail="Could not build session summary input.",
        ) from exc


@app.post(
    "/api/v1/sessions/{session_id}/finish",
    response_model=SessionStatusResponse,
)
def finish_session(session_id: str):
    try:
        with database.get_connection() as connection:
            # Reserve SQLite's write lock before checking that the session is
            # complete, so a segment cannot be inserted between validation
            # and the status update.
            connection.execute("BEGIN IMMEDIATE")
            status = _load_session_status(connection, session_id)

            if status.status == "finished":
                return status

            if status.status != "active":
                raise HTTPException(
                    status_code=409,
                    detail="Only active sessions can be finished.",
                )

            rows = connection.execute(
                "SELECT sequence_number FROM segments WHERE session_id = ?",
                (session_id,),
            ).fetchall()
            present = {row["sequence_number"] for row in rows}
            missing = [
                number
                for number in range(1, max(present, default=0) + 1)
                if number not in present
            ]

            if status.pending_segments or status.failed_segments or missing:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": (
                            "Cannot finish the session until every segment is "
                            "translated and sequence numbers are continuous."
                        ),
                        "pending_segments": status.pending_segments,
                        "failed_segments": status.failed_segments,
                        "missing_sequence_numbers": missing,
                    },
                )

            ended_at = datetime.now(timezone.utc).isoformat()
            connection.execute(
                "UPDATE sessions SET status = ?, ended_at = ? WHERE session_id = ?",
                ("finished", ended_at, session_id),
            )
            return _load_session_status(connection, session_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not finish session %s", session_id)
        raise HTTPException(
            status_code=500,
            detail="Could not finish session.",
        ) from exc

from live_session_router import router as live_session_router
app.include_router(live_session_router)

