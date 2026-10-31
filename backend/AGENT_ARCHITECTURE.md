# Live translation agent architecture

This document defines the backend core's parent coordinator and specialist
workers. It does not include the web/app UI or the T5 summarizer implementation.

## Design rule

The live path is latency-sensitive. The parent coordinator routes work to
specialist agents, but only the Gemini translation agent calls an AI model for
each spoken stream. Audio detection, transcript assembly, persistence, recovery,
and measurement are deterministic workers. Translation repair remains a
bounded fallback after Stop when the streamed result is incomplete.

## Agent responsibilities

| Agent | Input | Output / responsibility |
|---|---|---|
| `LiveSessionOrchestrator` (parent) | `session_id`, target language, configured live model | Validates and prepares session context, constructs the specialist team, and delegates session work. |
| `AudioVADAgent` | PCM16 mono 16 kHz frames and VAD thresholds | Creates the pause detector; emits speech/pause state to the live coordinator. It does not edit recognized words. |
| `GeminiTranslationAgent` | Target language, optional provider resumption handle, audio stream | Builds Live API configuration and opens a resumed or fresh provider connection. A rejected handle falls back to a new provider stream. |
| `TranscriptAgent` | Provider partial/final transcript events and VAD settle time | Creates replaceable transcript buffers and candidate-boundary tracking. The live coordinator decides when an utterance is safe to save. |
| `MemoryAgent` | Session context, segment text, provider resumption updates | Loads prior state; retains a stable pending segment ID; atomically saves bilingual segments and advances the next ID; stores provider resume handles. |
| `MetricsAgent` | Monotonic timings and connection mode | Persists audio-to-transcript, audio-to-translation, Stop-to-final, and provider-connection timing samples. |
| `SessionConnectionAgent` | `session_id` | Allows one live WebSocket per session in this backend process so concurrent streams cannot race the same pending segment key. |

The agent contract is the typed `LiveSessionContext` plus specialist method
inputs/outputs in `live_agents.py`. Provider-specific setup is isolated in
`GeminiTranslationAgent`; browser/app event names remain in the existing
WebSocket contract.

## Start and reconnect flow

1. The client opens `WS /api/v1/live-sessions/{session_id}` and sends the
   existing `start` message with `target_language_code`.
2. The parent coordinator obtains the active session's persisted segment count,
   last sequence number, pending segment ID, and the latest unexpired Gemini
   resumption handle for the same model and target language.
3. The translation agent attempts to resume with the provider handle. If there
   is no handle, it opens a fresh provider stream. If Gemini rejects a saved
   handle, the agent clears it and retries once with a fresh stream.
4. `session_ready` reports `context_restore_status`,
   `context_restored`, `history_segments_available`, `last_sequence_number`,
   and `provider_connect_ms`. Transcript rows remain in SQLite regardless of
   provider resume success.
5. New audio continues through the existing VAD, transcript, persistence, and
   metrics workers. New provider resumption updates replace the stored handle;
   a non-resumable update clears the stale handle.
6. On `provider_go_away`, the client can finish the current connection and
   reconnect with the same active session ID and target language. The backend
   then tries the latest handle again.

Handles are private provider tokens. They stay in the local SQLite database,
are scoped to `(session_id, model_name, target_language)`, and are discarded
after two hours without an update. They are never sent to the browser/app.

## Duplicate protection and ordering

- The pending `segment_id` is written before streaming begins and survives a
  backend reconnect.
- Saving a segment and advancing the next pending ID happen in one SQLite
  transaction. A persistence replay with the same ID and same content returns
  the existing row; reusing an ID for different content is rejected.
- The client should upsert `segment_final` by `segment_id`, sort by
  `sequence_number`, and send only new microphone audio after reconnect. The
  current raw binary audio contract has no client frame IDs, so replaying old
  audio bytes cannot be safely deduplicated without changing that contract.
- A provider resume token restores provider-side conversation state when the
  chosen model accepts it. If resumption is unavailable or rejected,
  `context_restored` is `false`; persisted transcript remains accessible via
  REST, but it is not injected as text into the audio-only Live Translate model.

The connection guard is process-local. The current development server uses one
Uvicorn worker. A multi-worker deployment needs a shared lease/lock store before
running several backend workers against the same sessions.

## Metrics

`GET /api/v1/metrics/live-latency` now includes `provider_connect_ms` p50/p95
and `provider_connection_modes` counts/percentiles for `fresh`, `resumed`, and
`resume_fallback`, alongside the existing live metrics. The offline tests
validate aggregation logic; real Gemini resume latency still requires a manual
microphone test with the configured model.

## Ownership boundary

- Web and app call this backend contract; they do not receive Gemini keys.
- This backend exports a finished presentation through
  `GET /api/v1/presentations/{presentation_id}/summary-input`.
- The T5 teammate owns summarization and calls that endpoint after all sessions
  and the presentation have been finished.
