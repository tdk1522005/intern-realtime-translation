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


_OPEN_ENGLISH_TAILS = frozenset({
    "a", "an", "the", "and", "or", "but", "because", "if", "when",
    "at", "by", "for", "from", "in", "into", "of", "on", "to",
    "with", "without", "please", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would",
    "can", "could", "should", "may", "might", "my", "your", "our",
    "their", "this", "that", "these", "those",
})
_OPEN_VIETNAMESE_TAILS = frozenset({
    "và", "hoặc", "nhưng", "vì", "nếu", "khi", "là", "ở", "tại",
    "với", "của", "cho", "vào", "từ", "đến", "để", "một", "những",
    "các", "đã", "sẽ", "đang", "lúc", "bởi", "theo",
    "xin",
})


def plausible_unpunctuated_stop_source(text: str, source_language: str) -> bool:
    """Conservative check for a complete spoken phrase lacking final punctuation.

    This is only a candidate for confirmation after a clean Stop and a
    whole-source translation. It cannot prove semantic completeness.
    """
    candidate = text.strip()
    if not candidate or candidate[-1] in ",;:-–—…":
        return False
    if ends_with_sentence_punctuation(candidate):
        return False
    words = _transcript_tokens(candidate)
    if not words:
        return False
    tails = _OPEN_ENGLISH_TAILS if source_language == "en" else _OPEN_VIETNAMESE_TAILS
    return words[-1] not in tails


def is_unfinished_standalone_phrase(text: str, language: str) -> bool:
    """A lone request prefix is incomplete even if ASR adds a period."""
    words = _transcript_tokens(text)
    return words == (["xin"] if language == "vi" else ["please"])


def is_source_fragment_at_stop(text: str, language: str) -> bool:
    """Identify source speech to preserve without declaring a full sentence.

    Only call this for a clean explicit Stop. A provider/transport failure is
    a different state even if its last transcript happens to look unfinished.
    """
    candidate = text.strip()
    # Without a reliable final ASR marker, missing punctuation cannot prove
    # that the last words have arrived. Keep the phrase continuable even when
    # it looks plausible as a standalone sentence.
    return bool(candidate and (
        is_unfinished_standalone_phrase(candidate, language)
        or not ends_with_sentence_punctuation(candidate)
    ))


def join_spoken_continuation(previous: str, following: str) -> str:
    """Join real speech across Stop without deduplicating repeated words."""
    prefix = re.sub(r"[.!?…。！？]+$", "", previous.strip()).rstrip()
    suffix = following.strip()
    return " ".join(part for part in (prefix, suffix) if part)


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
        self.source_preview_tail_preservations = 0

    def set_source_preview(self, text: str) -> str:
        """Replace the provisional input snapshot; never append revisions."""
        self.source_preview = text
        return self.source_preview

    def add_source_fragment(self, text: str, *, finished: bool = False) -> str:
        """Append canonical text while retaining a newer interim tail if needed."""
        if _is_cumulative_prefix(self.source_text, text):
            self.source_text = text
            self.source_snapshot_replacements += 1
        elif _is_cumulative_prefix(text, self.source_text):
            # An older cumulative update arrived after a longer snapshot.
            self.stale_source_snapshots_ignored += 1
            if finished:
                self.source_preview = ""
            return self.source_for_display()
        else:
            self.source_text += text

        # A shorter canonical event can precede the last one or two words of
        # a cumulative interim hypothesis. Keep that visible until a newer
        # canonical event catches up. An explicit finished marker supersedes it.
        if not finished and self.has_unconfirmed_source_tail():
            self.source_preview_tail_preservations += 1
        else:
            self.source_preview = ""
        return self.source_for_display()

    def has_unconfirmed_source_tail(self) -> bool:
        """Whether the interim source extends the canonical source by tokens."""
        return bool(
            self.source_preview
            and _is_cumulative_prefix(self.source_text, self.source_preview)
            and len(_transcript_tokens(self.source_preview))
            > len(_transcript_tokens(self.source_text))
        )

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
