
import asyncio
import base64
import os
import wave
from pathlib import Path
from typing import Awaitable, Callable

from dotenv import load_dotenv
from google import genai
from google.genai import types


load_dotenv()

API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = os.getenv(
    "GEMINI_LIVE_MODEL",
    "gemini-3.5-live-translate-preview",
)

if not API_KEY:
    raise RuntimeError("GEMINI_API_KEY is not configured.")

client = genai.Client(api_key=API_KEY)


async def stream_microphone_translation(
    audio_queue: asyncio.Queue[bytes | None],
    target_language_code: str,
    emit_event: Callable[[dict], Awaitable[None]],
) -> None:
    """Forward 16 kHz PCM microphone chunks to Gemini Live and emit results."""
    if target_language_code not in ("en", "vi"):
        raise ValueError("target_language_code must be 'en' or 'vi'.")

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        translation_config=types.TranslationConfig(
            target_language_code=target_language_code,
            echo_target_language=True,
        ),
    )

    stop_requested = asyncio.Event()

    async with client.aio.live.connect(model=MODEL_NAME, config=config) as session:
        async def send_audio() -> None:
            while True:
                chunk = await audio_queue.get()
                if chunk is None:
                    await session.send_realtime_input(audio_stream_end=True)
                    stop_requested.set()
                    return

                await session.send_realtime_input(
                    audio=types.Blob(
                        data=chunk,
                        mime_type="audio/pcm;rate=16000",
                    )
                )

        sender_task = asyncio.create_task(send_audio())
        try:
            async with asyncio.timeout(15 * 60):
                async for response in session.receive():
                    content = response.server_content
                    if content is None:
                        continue

                    if content.input_transcription and content.input_transcription.text:
                        await emit_event({
                            "type": "input_transcription",
                            "text": content.input_transcription.text,
                        })

                    if content.output_transcription and content.output_transcription.text:
                        await emit_event({
                            "type": "output_transcription",
                            "text": content.output_transcription.text,
                        })

                    if content.model_turn and content.model_turn.parts:
                        for part in content.model_turn.parts:
                            if part.inline_data and part.inline_data.data:
                                await emit_event({
                                    "type": "translated_audio", 
                                    "mime_type": part.inline_data.mime_type
                                    or "audio/pcm;rate=24000",
                                    "data": base64.b64encode(
                                        part.inline_data.data
                                    ).decode("ascii"),
                                })

                    if stop_requested.is_set() and content.turn_complete:
                        break

            if sender_task.done():
                await sender_task
        finally:
            if not sender_task.done():
                sender_task.cancel()
                try:
                    await sender_task
                except asyncio.CancelledError:
                    pass


async def translate_wav(
    wav_path: str,
    target_language_code: str = "vi",
) -> dict:
    """
    Test Live Translate using a WAV file.
    WAV must contain mono, 16-bit PCM audio at 16 kHz.
    """
    path = Path(wav_path)

    if not path.is_file():
        raise FileNotFoundError(f"Audio file not found: {path}")

    if target_language_code not in ("en", "vi"):
        raise ValueError("target_language_code must be 'en' or 'vi'.")

    with wave.open(str(path), "rb") as wav:
        if (
            wav.getnchannels() != 1
            or wav.getsampwidth() != 2
            or wav.getframerate() != 16000
            or wav.getcomptype() != "NONE"
        ):
            raise ValueError(
                "WAV must be uncompressed PCM, mono, 16-bit, 16000 Hz."
            )

        audio_data = wav.readframes(wav.getnframes())

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        translation_config=types.TranslationConfig(
            target_language_code=target_language_code,
            echo_target_language=True,
        ),
    )

    result = {
        "input_transcript": "",
        "translated_transcript": "",
        "audio_chunks_received": 0,
    }

    async with client.aio.live.connect(
        model=MODEL_NAME,
        config=config,
    ) as session:
        # Send audio in approximately 100 ms chunks:
        # 16000 samples/sec * 2 bytes/sample * 0.1 sec = 3200 bytes.
        chunk_size = 3200

        for start in range(0, len(audio_data), chunk_size):
            chunk = audio_data[start : start + chunk_size]

            await session.send_realtime_input(
                audio=types.Blob(
                    data=chunk,
                    mime_type="audio/pcm;rate=16000",
                )
            )

            # Keep real-time pacing instead of sending the whole file at once.
            await asyncio.sleep(len(chunk) / (16000 * 2))

        # Signal end of audio input, then collect server events.
        await session.send_realtime_input(audio_stream_end=True)

        try:
            async with asyncio.timeout(30):
                async for response in session.receive():
                    content = response.server_content
                    if content is None:
                        continue

                    if content.input_transcription:
                        result["input_transcript"] += (
                            content.input_transcription.text or ""
                        )

                    if content.output_transcription:
                        result["translated_transcript"] += (
                            content.output_transcription.text or ""
                        )

                    if content.model_turn and content.model_turn.parts:
                        for part in content.model_turn.parts:
                            if part.inline_data:
                                result["audio_chunks_received"] += 1

                    if content.turn_complete:
                        break

        except TimeoutError:
            print("Timed out while waiting for translation output.")

    return result
