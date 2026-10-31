"""Small, testable latency tracker and percentile reporting helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from uuid import uuid4

import database


METRIC_DESCRIPTIONS = {
    "audio_to_source_ms": "first audio frame received to first source transcript",
    "audio_to_translation_ms": "first audio frame received to first translated transcript",
    "stop_to_final_persisted_ms": "Stop command received to final segment persisted",
}

PROVIDER_CONNECTION_MODES = ("fresh", "resumed", "resume_fallback")


@dataclass
class LiveLatencyTracker:
    first_audio_at: float | None = None
    first_source_at: float | None = None
    first_translation_at: float | None = None
    stop_requested_at: float | None = None
    final_persisted_at: float | None = None

    def note_audio(self, now: float) -> None:
        if self.first_audio_at is None:
            self.first_audio_at = now

    def note_source(self, now: float) -> None:
        if self.first_source_at is None:
            self.first_source_at = now

    def note_translation(self, now: float) -> None:
        if self.first_translation_at is None:
            self.first_translation_at = now

    def note_stop(self, now: float) -> None:
        if self.stop_requested_at is None:
            self.stop_requested_at = now

    def note_final_persisted(self, now: float) -> None:
        self.final_persisted_at = now

    def samples(self) -> dict[str, float]:
        result: dict[str, float] = {}
        if self.first_audio_at is not None and self.first_source_at is not None:
            result["audio_to_source_ms"] = max(
                0.0,
                round((self.first_source_at - self.first_audio_at) * 1000, 3),
            )
        if self.first_audio_at is not None and self.first_translation_at is not None:
            result["audio_to_translation_ms"] = max(
                0.0,
                round((self.first_translation_at - self.first_audio_at) * 1000, 3),
            )
        if self.stop_requested_at is not None and self.final_persisted_at is not None:
            result["stop_to_final_persisted_ms"] = max(
                0.0,
                round((self.final_persisted_at - self.stop_requested_at) * 1000, 3),
            )
        return result


def nearest_rank(values: Iterable[float], percentile: float) -> float | None:
    """Return a deterministic nearest-rank percentile, or None for no samples."""
    if not 0 < percentile <= 1:
        raise ValueError("percentile must be greater than 0 and at most 1")
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def summarize_samples(values: Iterable[float]) -> dict[str, float | int | None]:
    ordered = sorted(float(value) for value in values)
    return {
        "sample_count": len(ordered),
        "p50_ms": nearest_rank(ordered, 0.50),
        "p95_ms": nearest_rank(ordered, 0.95),
        "min_ms": ordered[0] if ordered else None,
        "max_ms": ordered[-1] if ordered else None,
    }


def record_samples(session_id: str, samples: dict[str, float]) -> int:
    """Persist one live connection's measured latency values in SQLite."""
    accepted = {
        name: float(value)
        for name, value in samples.items()
        if name in METRIC_DESCRIPTIONS
        and math.isfinite(float(value))
        and float(value) >= 0
    }
    if not accepted:
        return 0

    created_at = datetime.now(timezone.utc).isoformat()
    with database.get_connection() as connection:
        connection.executemany(
            """
            INSERT INTO live_latency_samples (
                sample_id, session_id, metric_name, latency_ms, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (str(uuid4()), session_id, name, value, created_at)
                for name, value in accepted.items()
            ],
        )
    return len(accepted)


def record_provider_connection_sample(
    session_id: str,
    latency_ms: float,
    connection_mode: str,
) -> bool:
    """Persist provider setup latency and whether session context resumed."""
    value = float(latency_ms)
    if (
        connection_mode not in PROVIDER_CONNECTION_MODES
        or not math.isfinite(value)
        or value < 0
    ):
        return False

    with database.get_connection() as connection:
        connection.execute(
            """
            INSERT INTO live_provider_connection_samples (
                sample_id, session_id, connection_mode, latency_ms, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                session_id,
                connection_mode,
                value,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    return True


def read_latency_report(*, max_samples: int = 10_000) -> dict:
    """Aggregate the most recent persisted samples for each latency metric."""
    with database.get_connection() as connection:
        rows = connection.execute(
            """
            SELECT metric_name, latency_ms
            FROM live_latency_samples
            ORDER BY created_at DESC, sample_id DESC
            LIMIT ?
            """,
            (max_samples,),
        ).fetchall()
        provider_rows = connection.execute(
            """
            SELECT connection_mode, latency_ms
            FROM live_provider_connection_samples
            ORDER BY created_at DESC, sample_id DESC
            LIMIT ?
            """,
            (max_samples,),
        ).fetchall()

    grouped: dict[str, list[float]] = {
        name: [] for name in METRIC_DESCRIPTIONS
    }
    for row in rows:
        grouped[row["metric_name"]].append(float(row["latency_ms"]))

    provider_by_mode = {mode: [] for mode in PROVIDER_CONNECTION_MODES}
    for row in provider_rows:
        provider_by_mode[row["connection_mode"]].append(
            float(row["latency_ms"])
        )
    provider_all = [value for values in provider_by_mode.values() for value in values]

    metrics = {
        name: {
            "description": METRIC_DESCRIPTIONS[name],
            **summarize_samples(values),
        }
        for name, values in grouped.items()
    }
    metrics["provider_connect_ms"] = {
        "description": "provider connection setup time, including resumption fallback",
        **summarize_samples(provider_all),
    }

    return {
        "method": "nearest_rank",
        "window_limit": max_samples,
        "metrics": metrics,
        "provider_connection_modes": {
            mode: summarize_samples(values)
            for mode, values in provider_by_mode.items()
        },
    }
