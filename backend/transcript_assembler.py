"""Small, provider-independent buffers for live transcript events."""

from __future__ import annotations

import re


_SENTENCE_ENDINGS = ".!?…。！？"
_TRAILING_CLOSERS = "\"'”’)]}"
_MIN_SNAPSHOT_PREFIX_TOKENS = 4


def _transcript_tokens(text: str) -> list[str]:
    return re.findall(r"[\w]+", text.casefold(), flags=re.UNICODE)


def _is_cumulative_prefix(prefix: str, value: str) -> bool:
    prefix_tokens = _transcript_tokens(prefix)
    value_tokens = _transcript_tokens(value)
    return bool(
        len(prefix_tokens) >= _MIN_SNAPSHOT_PREFIX_TOKENS
        and len(prefix_tokens) <= len(value_tokens)
        and value_tokens[: len(prefix_tokens)] == prefix_tokens
    )


def merge_transcript_snapshot(base: str, snapshot: str) -> str:
    """Merge a cumulative interim snapshot without duplicating its base text.

    Gemini interim text is a replaceable hypothesis. If it contains the full
    canonical prefix, it is a newer view of the same content; if it is only a
    prefix of canonical text, it is stale. Unrelated text is kept verbatim so
    genuine repeated words are never removed by fuzzy overlap matching.
    """
    base = base.strip()
    snapshot = snapshot.strip()
    if not base:
        return snapshot
    if not snapshot:
        return base

    base_tokens = _transcript_tokens(base)
    snapshot_tokens = _transcript_tokens(snapshot)
    if _is_cumulative_prefix(base, snapshot):
        return snapshot
    if _is_cumulative_prefix(snapshot, base):
        return base

    return f"{base} {snapshot}"


def ends_with_sentence_punctuation(text: str) -> bool:
    """Return whether text ends with terminal punctuation after closing quotes."""
    candidate = text.rstrip()
    while candidate and candidate[-1] in _TRAILING_CLOSERS:
        candidate = candidate[:-1].rstrip()
    return bool(candidate and candidate[-1] in _SENTENCE_ENDINGS)


def count_sentence_endings(text: str) -> int:
    """Count sentence-ending punctuation groups, ignoring decimal dots.

    A punctuation mark counts as a sentence boundary only when it is followed
    by optional closing quotes/brackets and then whitespace or the end of the
    text. Thus ``9:00`` and ``3.5`` do not add boundaries, while ``9:00.`` does.
    Ellipses and combinations such as ``?!`` count as one ending.
    """
    count = 0
    for match in re.finditer(r"[.!?…。！？]+", text):
        end = match.end()
        while end < len(text) and text[end] in _TRAILING_CLOSERS:
            end += 1
        if end == len(text) or text[end].isspace():
            count += 1
    return count


class TranscriptAssembler:
    """Keep provisional input separate from text used for saved segments."""

    def __init__(self) -> None:
        self.source_text = ""
        self.translated_text = ""
        self.source_preview = ""
        self.source_snapshot_replacements = 0
        self.stale_source_snapshots_ignored = 0

    def set_source_preview(self, text: str) -> str:
        """Replace the provisional input snapshot; never append revisions."""
        self.source_preview = text
        return self.source_preview

    def add_source_fragment(self, text: str) -> str:
        """Append a text fragment or replace a cumulative transcript snapshot."""
        if _is_cumulative_prefix(self.source_text, text):
            self.source_text = text
            self.source_preview = ""
            self.source_snapshot_replacements += 1
            return self.source_text
        if _is_cumulative_prefix(text, self.source_text):
            # An older cumulative update arrived after a longer snapshot.
            self.stale_source_snapshots_ignored += 1
            return self.source_for_display()

        self.source_text += text
        self.source_preview = ""
        return self.source_text

    def add_translation_fragment(self, text: str) -> str:
        """Append a streamed output fragment without dropping repeated words."""
        self.translated_text += text
        return self.translated_text

    def set_translation_final(self, text: str) -> str:
        """Replace an output preview with its authoritative final snapshot."""
        self.translated_text = text
        return self.translated_text

    def set_source_final(self, text: str) -> str:
        """Replace a provisional source snapshot with its authoritative final."""
        self.source_text = text
        self.source_preview = ""
        return self.source_text

    def source_for_save(self, *, force: bool = False) -> str:
        """Use the latest stable data, reconciling a preview when forced."""
        if not force:
            return self.source_text
        return merge_transcript_snapshot(self.source_text, self.source_preview)

    def source_for_display(self) -> str:
        """Show canonical text plus the newest replaceable hypothesis."""
        return merge_transcript_snapshot(self.source_text, self.source_preview)

    def reset(self) -> None:
        self.source_text = ""
        self.translated_text = ""
        self.source_preview = ""


class VADBoundaryTracker:
    """Wait for transcript output to settle after a local VAD pause."""

    def __init__(self, settle_ms: int = 300) -> None:
        if settle_ms < 0:
            raise ValueError("settle_ms must not be negative")
        self.settle_seconds = settle_ms / 1000
        self.pending_pauses = 0
        self.last_boundary_at: float | None = None
        self.last_transcript_at: float | None = None

    def note_pause(self, now: float) -> None:
        self.pending_pauses += 1
        self.last_boundary_at = now

    def note_transcript(self, now: float) -> None:
        self.last_transcript_at = now

    def ready_at(self) -> float | None:
        if not self.pending_pauses or self.last_boundary_at is None:
            return None
        quiet_start = max(
            self.last_boundary_at,
            self.last_transcript_at or self.last_boundary_at,
        )
        return quiet_start + self.settle_seconds

    def is_ready(
        self,
        now: float,
        *,
        has_source: bool,
        has_translation: bool,
        source_sentence_complete: bool,
        translation_sentence_complete: bool,
        speech_active: bool,
    ) -> bool:
        deadline = self.ready_at()
        return bool(
            deadline is not None
            and has_source
            and has_translation
            and source_sentence_complete
            and translation_sentence_complete
            and not speech_active
            and now >= deadline
        )

    def reset(self) -> None:
        self.pending_pauses = 0
        self.last_boundary_at = None
        self.last_transcript_at = None
