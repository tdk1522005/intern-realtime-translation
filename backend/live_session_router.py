import asyncio
import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from google.genai import types

import database
import gemini_live_service


logger = logging.getLogger(__name__)
router = APIRouter()

LIVE_MODEL = gemini_live_service.MODEL_NAME
LIVE_CLIENT = gemini_live_service.client

MAX_AUDIO_FRAME_BYTES = 3200
AUDIO_QUEUE_SIZE = 20


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
) -> dict:
    """
    Persist one completed provider turn.

    If Gemini produced source text but no translated text before the stream
    ended, save the source as a failed segment so the existing retry endpoint
    can be used.
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

    segment_status = "completed" if translated_text else "failed"
    segment_id = str(uuid4())
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


@router.websocket("/api/v1/live-sessions/{session_id}")
async def live_session_websocket(websocket: WebSocket, session_id: str):
    await websocket.accept()

    send_lock = asyncio.Lock()
    stop_requested = asyncio.Event()
    audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue(
        maxsize=AUDIO_QUEUE_SIZE
    )

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

        source_parts: list[str] = []
        translated_parts: list[str] = []
        turn_boundary_seen = False
        segments_saved = 0

        async def finalize_segment(force: bool = False) -> None:
            nonlocal turn_boundary_seen, segments_saved

            source_text = "".join(source_parts).strip()
            translated_text = "".join(translated_parts).strip()

            if not source_text:
                return

            # During a live stream, wait for both transcripts and the model's
            # turn boundary before writing a completed segment.
            if not force:
                if not turn_boundary_seen or not translated_text:
                    return

            saved = await asyncio.to_thread(
                _save_live_segment,
                session_id,
                target_language,
                source_text,
                translated_text,
            )

            source_parts.clear()
            translated_parts.clear()
            turn_boundary_seen = False
            segments_saved += 1

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
                "created_at": saved["created_at"],
            })

        async def receive_client_audio() -> None:
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

                        try:
                            await asyncio.wait_for(
                                audio_queue.put(audio_chunk),
                                timeout=2,
                            )
                        except asyncio.TimeoutError:
                            stop_requested.set()
                            await send_event({
                                "type": "error",
                                "message": (
                                    "Audio queue is full. The stream is being "
                                    "stopped to prevent unbounded buffering."
                                ),
                            })
                            return

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
                            stop_requested.set()
                            return

            except WebSocketDisconnect:
                stop_requested.set()
            finally:
                try:
                    await asyncio.wait_for(audio_queue.put(None), timeout=2)
                except asyncio.TimeoutError:
                    # Drop queued audio explicitly if the provider is too far
                    # behind; then allow the sender to send the end marker.
                    while not audio_queue.empty():
                        try:
                            audio_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break

                    try:
                        audio_queue.put_nowait(None)
                    except asyncio.QueueFull:
                        pass

        async def send_audio_to_gemini(provider_session) -> None:
            while True:
                audio_chunk = await audio_queue.get()

                if audio_chunk is None:
                    await provider_session.send_realtime_input(
                        audio_stream_end=True
                    )
                    return

                await provider_session.send_realtime_input(
                    audio=types.Blob(
                        data=audio_chunk,
                        mime_type="audio/pcm;rate=16000",
                    )
                )

        async def receive_gemini_events(provider_session) -> None:
            nonlocal turn_boundary_seen

            async with asyncio.timeout(15 * 60):
                async for response in provider_session.receive():
                    content = response.server_content

                    if content is None:
                        continue

                    if (
                        content.input_transcription
                        and content.input_transcription.text
                    ):
                        text = content.input_transcription.text
                        source_parts.append(text)

                        await send_event({
                            "type": "input_transcription",
                            "text": text,
                        })

                        await finalize_segment()

                    if (
                        content.output_transcription
                        and content.output_transcription.text
                    ):
                        text = content.output_transcription.text
                        translated_parts.append(text)

                        await send_event({
                            "type": "output_transcription",
                            "text": text,
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

                    if content.turn_complete:
                        turn_boundary_seen = True
                        await finalize_segment()

                        if stop_requested.is_set():
                            return

        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            translation_config=types.TranslationConfig(
                target_language_code=target_language,
                echo_target_language=True,
            ),
        )

        receiver_task = None
        sender_task = None
        provider_task = None

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
                })

                sender_task = asyncio.create_task(
                    send_audio_to_gemini(provider_session)
                )
                provider_task = asyncio.create_task(
                    receive_gemini_events(provider_session)
                )

                done, _ = await asyncio.wait(
                    {receiver_task, provider_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if receiver_task in done:
                    await receiver_task

                    try:
                        await asyncio.wait_for(provider_task, timeout=12)
                    except asyncio.TimeoutError:
                        provider_task.cancel()
                        try:
                            await provider_task
                        except asyncio.CancelledError:
                            pass
                else:
                    await provider_task
                    receiver_task.cancel()
                    try:
                        await receiver_task
                    except asyncio.CancelledError:
                        pass

                if sender_task is not None and not sender_task.done():
                    try:
                        await asyncio.wait_for(sender_task, timeout=3)
                    except asyncio.TimeoutError:
                        sender_task.cancel()

        finally:
            for task in (receiver_task, sender_task, provider_task):
                if task is not None and not task.done():
                    task.cancel()

            for task in (receiver_task, sender_task, provider_task):
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

        await send_event({
            "type": "stream_stopped",
            "session_id": session_id,
            "segments_saved": segments_saved,
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