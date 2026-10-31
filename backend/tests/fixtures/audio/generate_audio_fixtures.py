"""Generate deterministic, non-speech PCM fixtures for audio-contract tests."""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path


SAMPLE_RATE = 16_000
SAMPLE_WIDTH_BYTES = 2
OUTPUT_DIR = Path(__file__).resolve().parent


def write_pcm16_mono(path: Path, samples: list[int]) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))


def make_speech_pause_speech_samples() -> list[int]:
    """Make two voiced-like harmonic blocks with an exact 600 ms zero gap."""
    first_block = (0.0, 0.8, 145.0)
    second_block = (1.4, 2.2, 185.0)
    total_samples = int(2.2 * SAMPLE_RATE)
    samples: list[int] = []

    for sample_index in range(total_samples):
        time_seconds = sample_index / SAMPLE_RATE
        active_block = next(
            (
                block
                for block in (first_block, second_block)
                if block[0] <= time_seconds < block[1]
            ),
            None,
        )

        if active_block is None:
            samples.append(0)
            continue

        _, _, fundamental_hz = active_block
        phase = 2.0 * math.pi * fundamental_hz * time_seconds
        value = (
            0.22 * math.sin(phase)
            + 0.08 * math.sin(2.0 * phase)
            + 0.04 * math.sin(3.0 * phase)
        )
        samples.append(round(value * 32767))

    return samples


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    write_pcm16_mono(
        OUTPUT_DIR / "speech_pause_speech.wav",
        make_speech_pause_speech_samples(),
    )
    write_pcm16_mono(
        OUTPUT_DIR / "silence_100ms.wav",
        [0] * int(SAMPLE_RATE / 10),
    )
    print(f"Generated PCM fixtures in {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
