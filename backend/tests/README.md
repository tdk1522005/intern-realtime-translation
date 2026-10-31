# Offline live-translation regression fixtures

Run from `backend` with the project's Python environment:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

These tests do not call Gemini and do not need `GEMINI_API_KEY`.

## Audio fixtures

- `fixtures/audio/speech_pause_speech.wav` is deterministic synthetic harmonic audio with two voiced-like blocks and a labeled 600 ms digital-silence interval. It checks PCM format, frame size, and later VAD boundary logic. It is **not human speech** and must not be used to evaluate speech recognition or translation quality.
- `fixtures/audio/silence_100ms.wav` is exactly one 100 ms PCM16 frame of silence.
- `fixtures/audio/generate_audio_fixtures.py` regenerates both WAV files.

The live websocket uses `PauseDetector`, a simple RMS energy endpoint detector. Defaults are `LIVE_VAD_RMS_THRESHOLD=450` (PCM16 RMS units), `LIVE_VAD_MIN_SPEECH_MS=200`, and `LIVE_VAD_SILENCE_MS=600`; all can be overridden in `.env`. It keeps automatic provider VAD disabled and sends explicit `activity_start` / `activity_end` markers around each locally detected utterance. A short pre-roll keeps the beginning of speech while the detector confirms it; every buffered speech frame is forwarded. Since Live Translate may stream continuously without turn-completion or transcript-finished markers, local VAD pauses are candidate segment boundaries, not sentence boundaries by themselves. Before automatic save, source and translation must both end with terminal punctuation and the translation must not have fewer sentence-ending marks than the source. The backend then waits for a quiet transcript window (`LIVE_SEGMENT_SETTLE_MS`, default 300 ms). Pauses inside a clause or sentence stay pending and may be coalesced with the next pause. This is a low-latency heuristic and needs real audio regression tests to measure late-fragment misassignment. The page reports provider markers, transcript event counts, optional timing-offset counts, VAD segment commits, coalesced pauses, and saved segments. This is a tunable baseline, not a noise-robust neural VAD.

## Event fixtures

`fixtures/live_event_sequences.synthetic.json` contains hand-authored normalized mock scenarios: chunked transcripts over multiple turns, a provisional transcript corrected by a final transcript, stopping before translation arrives, and reconnecting to a new provider stream while keeping the same database session.

These are test scenarios, **not captured Gemini SDK messages**. The fixture keeps that distinction explicit so it cannot be mistaken for evidence of actual model behavior. Later, real provider recordings can be added after the event contract and privacy-safe capture policy are defined.

The transcript tests exercise `TranscriptAssembler`: provisional input is treated as a replaceable preview, finalized fragments are appended without fuzzy overlap deletion, and buffers reset between turns. Long cumulative input updates replace their shorter prefix; stale cumulative updates are ignored. At forced stop, a longer cumulative preview can supersede a shorter canonical prefix. The browser merges cumulative display snapshots with committed text to avoid showing the same prefix twice. A conservative four-token prefix check avoids treating short repeated phrases as snapshots; the status line reports how often cumulative replacements and stale updates occur. `VADBoundaryTracker` requires both transcript sides to have sentence-ending punctuation and a quiet settle window, and holds a candidate boundary while speech is active. The live websocket emits cumulative preview snapshots and persists a segment after a provider boundary or a stable sentence-final VAD pause (or forces a flush when the user stops). Real Gemini event timing still needs a microphone test because provider event ordering, delivery delay, and optional timing fields can vary by model/API version.

Persistence also checks both sides before reporting success: a missing translation, missing terminal punctuation, or an unrepaired live translation with fewer sentence endings than the source is retained with its source but saved as `failed`, so the retry endpoint can be used. A successful non-streaming text repair of the entire source may combine source sentences, so it is allowed to use different sentence punctuation. These checks are not a semantic proof that the translation is complete; verify real provider output and inspect false failures. Each live transcript buffer keeps one segment ID through finalization; replaying persistence with that ID and identical contents returns the existing row, while reusing it for different contents is rejected. The key is held for the lifetime of that WebSocket buffer, not across a process restart.

## Stop and reconnect behavior

On Stop, the backend appends the final pending PCM frames, a manual `activity_end` marker, and one FIFO stop marker to the bounded queue. It does not discard queued frames to make room for the stop marker. Queue draining has a 20-second limit (`LIVE_AUDIO_QUEUE_DRAIN_TIMEOUT_MS`); sender draining and provider transcript draining also have bounded waits. After the sender has delivered the final audio, late transcript activity restarts a quiet window (`LIVE_STOP_QUIET_MS`, default 1500 ms), capped by `LIVE_STOP_DRAIN_TIMEOUT_MS` (default 20 seconds). The browser waits up to 90 seconds before treating Stop as unresponsive.

If the final source transcript is complete but the streamed translation is missing terminal punctuation or has fewer sentence endings than the source, the backend makes one bounded text-translation repair attempt. The successful text repair is based on the whole source, so it may combine sentences; it is accepted when the source and repaired translation both end with sentence punctuation. If repair fails or returns no terminal translation, the source and any partial translation are retained as `failed` for retry. Sentence-count comparison is a guard for streamed output, not a semantic completeness proof.

Starting the live page again reloads the persisted session transcript before requesting microphone access. This restores the app's visible history. It does not seed prior transcript text into the Live Translate model: Google's current Live Translate guide documents audio-only input and says text input is unsupported. Provider session resumption is a separate Live API capability and is not enabled for this model until support is verified against the chosen translation model.

## Latency and multi-session handoff

`GET /api/v1/metrics/live-latency` reports sample count, p50, p95, and maximum for audio-to-source, audio-to-translation, and Stop-to-final-persistence. These values are meaningful only after real microphone sessions have recorded samples; the synthetic WAV fixtures are not speech-recognition or translation benchmarks.

For a presentation composed of several sessions, finish each session with `POST /api/v1/sessions/{session_id}/finish`, then finish the presentation with `POST /api/v1/presentations/{presentation_id}/finish`. `GET /api/v1/presentations/{presentation_id}/summary-input` returns the ordered bilingual transcript grouped by session for the T5 teammate. The backend assembles input and does not run T5.

The retry regression tests use a real temporary SQLite row in status `failed`, but mock Gemini's response and outage. They verify the database state transitions and retryability; they do not prove an actual Gemini retry request. The offline suite uses no API key and does not contact Google.
