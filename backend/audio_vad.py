"""Lightweight PCM energy endpointing for the live audio stream."""

from __future__ import annotations

import math
import struct


class PauseDetector:
    """Detect a speech-to-silence boundary using frame RMS and hangover.

    This is an energy-based endpoint detector, not a neural speech classifier.
    Audio frames are always forwarded; the detector only reports when enough
    consecutive low-energy audio has followed detected speech.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16_000,
        rms_threshold: float = 450.0,
        min_speech_duration_ms: int = 200,
        silence_duration_ms: int = 600,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if rms_threshold < 0:
            raise ValueError("rms_threshold cannot be negative")
        if min_speech_duration_ms <= 0:
            raise ValueError("min_speech_duration_ms must be positive")
        if silence_duration_ms <= 0:
            raise ValueError("silence_duration_ms must be positive")

        self.sample_rate = sample_rate
        self.rms_threshold = rms_threshold
        self.min_speech_duration_ms = min_speech_duration_ms
        self.silence_duration_ms = silence_duration_ms
        self.speech_active = False
        self.speech_started_now = False
        self._speech_seen_ms = 0.0
        self._silence_seen_ms = 0.0
        self.last_rms = 0.0

    @property
    def speech_candidate_ms(self) -> float:
        return self._speech_seen_ms

    @property
    def silence_progress_ms(self) -> float:
        return self._silence_seen_ms

    def process_frame(self, pcm_frame: bytes) -> bool:
        """Return True once when a sustained pause ends an active utterance."""
        self.speech_started_now = False
        if len(pcm_frame) % 2:
            raise ValueError("PCM16 frame must contain an even number of bytes")
        sample_count = len(pcm_frame) // 2
        if sample_count == 0:
            return False

        squared_sum = sum(
            sample * sample
            for (sample,) in struct.iter_unpack("<h", pcm_frame)
        )
        rms = math.sqrt(squared_sum / sample_count)
        self.last_rms = rms
        frame_duration_ms = sample_count * 1000.0 / self.sample_rate

        if rms >= self.rms_threshold:
            if not self.speech_active:
                self._speech_seen_ms += frame_duration_ms
                if self._speech_seen_ms < self.min_speech_duration_ms:
                    return False
                self.speech_active = True
                self.speech_started_now = True
                self._speech_seen_ms = 0.0
            self._silence_seen_ms = 0.0
            return False

        if not self.speech_active:
            self._speech_seen_ms = 0.0
            return False

        self._silence_seen_ms += frame_duration_ms
        if self._silence_seen_ms < self.silence_duration_ms:
            return False

        self.speech_active = False
        self._speech_seen_ms = 0.0
        self._silence_seen_ms = 0.0
        return True

    def reset(self) -> None:
        self.speech_active = False
        self.speech_started_now = False
        self._speech_seen_ms = 0.0
        self._silence_seen_ms = 0.0
