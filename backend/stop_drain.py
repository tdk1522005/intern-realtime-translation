"""Provider-independent stop/drain timing policy for realtime transcripts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StopDrainWindow:
    """Wait for a quiet transcript window, with a hard upper time limit."""

    started_at: float
    quiet_seconds: float
    timeout_seconds: float
    last_activity_at: float | None = None
    terminal_at: float | None = None

    def __post_init__(self) -> None:
        if self.quiet_seconds < 0:
            raise ValueError("quiet_seconds cannot be negative")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    @property
    def deadline(self) -> float:
        return self.started_at + self.timeout_seconds

    def note_transcript(self, now: float) -> None:
        self.last_activity_at = now

    def note_terminal(self, now: float) -> None:
        self.terminal_at = now

    def quiet_deadline(self) -> float:
        anchors = [self.started_at]
        if self.last_activity_at is not None:
            anchors.append(self.last_activity_at)
        if self.terminal_at is not None:
            anchors.append(self.terminal_at)
        return max(anchors) + self.quiet_seconds

    def decision(self, now: float) -> str | None:
        """Return the drain result once safe to stop waiting."""
        if now >= self.deadline:
            return "timeout"
        if now >= self.quiet_deadline():
            return "provider_complete" if self.terminal_at is not None else "quiet"
        return None

    def next_wakeup_at(self) -> float:
        return min(self.deadline, self.quiet_deadline())
