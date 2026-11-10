# Phase 12 — Live Audio to Answer (12A + 12B combined): Pre-flight Plan

Status: **PLAN FOR OWNER APPROVAL; nothing implemented.** No code, dependency, migration, WebSocket message or Electron setting was changed to produce it. Proposed decision: D-53.

Written for: the project owner. It merges roadmap items 12A (real-time transcription) and 12B (question detection and auto-answer) into one batch and replaces the separate proof-of-concept phases with a short spike at the start of the batch.

## 1. What was inspected (facts, not assumptions)

| Area | Finding |
|---|---|
| Electron | 45.2.3. Typings (`electron.d.ts`): `session.setDisplayMediaRequestHandler` can grant `Streams.audio: 'loopback' \| 'loopbackWithMute'`, i.e. **system audio, supported on Windows only**; `setPermissionRequestHandler`/`setPermissionCheckHandler` know the `media` permission and its request details carry the requested media types |
| Current desktop posture | `main/index.ts` denies **every** permission request (`callback(false)`); there is no permission-check handler and no display-media handler; sandbox, context isolation, CSP (`default-src 'self'`, `script-src 'self'`, `connect-src 'none'`), fuses as in D-49. The renderer has no media access today |
| Existing patterns to reuse | screenshot flow (opt-in setting with native consent, `CaptureService` state machine with main-side gating, memory-only buffers wiped on every exit path, raw-body upload, single-use extraction token to mark `input_source`), `VisionProvider` + `FakeVisionProvider` + `VisionRouter`, body-limit pattern limits, per-user limiter, sentinel privacy tests |
| Backend Q&A | `QuestionAnswerer.ask(user, session, text, override, input_source)` is the single answering path (SSE and WebSocket both use it); the desktop uses SSE |
| WebSocket gateway | JSON text frames only, 16 KiB frame cap, a binary frame closes the socket with `1003`; `audio.*` and `transcript.*` messages are listed as "not built" |
| Database | `detected_questions.source` CHECK already allows `'audio'` (migration 0007); there is no `transcript_segments` table; `QuestionCreate.input_source` accepts `typed \| screenshot` |
| Providers | no speech-to-text port exists; OQ-5 (which STT provider) is **open**; no STT provider is approved |
| Policy | `PRIVACY.md`: audio needs explicit user initiation, a persistent visible indicator, consent of everyone whose speech is captured under local law and platform rules, ephemeral processing, no raw audio by default; OQ-11 (retention/legal) is unresolved |

## 2. Audio capture on Windows with the current Electron architecture

- **Microphone:** capturable with `navigator.mediaDevices.getUserMedia({audio})` in the renderer (Chromium's WASAPI path). Needs the `media` permission, currently denied.
- **System / loopback audio** (what the interviewer says in a call): capturable with `getDisplayMedia` plus a main-process `setDisplayMediaRequestHandler` that grants `audio: 'loopback'`. Windows only. Per-application capture is not available; everything the PC plays is included. `loopbackWithMute` would silence the user's speakers, so plain `loopback` is the right mode. Protected (DRM) playback may be silent.
- **Separately?** Yes. They are two independent streams with two different grants, which also allows labelling the source ("interviewer" vs "me") — useful because questions come from the interviewer's audio.
- **Unverified (the batch's first step, a spike, not a separate phase):** (a) whether the handler may return audio only or Chromium insists on a video track (fallback: grant the app's *own* frame as the video source and stop that track immediately, so no screen pixels are ever captured); (b) that an `AudioWorklet` module loads from the packaged `file://` renderer under the existing `script-src 'self'`; (c) clean 16 kHz mono PCM16 output and CPU cost. If loopback fails, the MVP falls back to microphone only.
- **Can main retain control of the sensitive parts?** Mostly yes. Chromium's media APIs only run in the renderer, so the renderer necessarily touches raw audio frames; everything else stays in main: the permission and loopback grants, the on/off state, voice-activity segmentation, utterance buffers, the WAV build, the upload with the bearer token, wiping, and the "is a question" handling. A native audio addon in main would avoid renderer exposure but adds a native dependency, signing and build complexity; **rejected for the MVP**.

### Exact security changes required (the only ones)

1. **Permission request handler:** from "deny everything" to "deny everything except `media` when (a) the user has pressed Start and main is waiting for the capture (state `arming`), (b) the request is audio only (no video, no display capture), (c) it comes from the main window's trusted renderer URL." All other permissions stay denied. The matching `setPermissionCheckHandler` follows the same rule (default deny).
2. **Display-media handler (new, loopback only):** registered but returns `null` (deny) unless state is `arming` for the system source; then it returns `{ audio: 'loopback' }` (or audio plus the app's own frame per the spike).
3. **New narrow IPC (named, validated, sender-checked):** `audio:start(source, sessionId)`, `audio:stop`, `audio:chunk(ArrayBuffer)` (accepted only while `listening`, size-capped, rate-capped), one event channel `audio:event` main → renderer.
4. **A settings flag** `audioInputEnabled` (default **off**; turning it on needs a native confirmation dialog, exactly like screenshots).
5. **No change:** sandbox, context isolation, fuses, token storage, the rest of the CSP (if the worklet cannot load from `'self'`, the only addition would be `worker-src 'self'`, never `unsafe-inline`/`blob:`), no new npm dependency.

## 3. Architecture (smallest that works, reusing what exists)

```text
Renderer: getUserMedia | getDisplayMedia (loopback) -> AudioWorklet -> PCM16 16 kHz mono frames (100 ms)
   -- audio:chunk (IPC, only while listening) -->
Main AudioService: state machine + visible indicators + energy VAD -> utterance (<= 30 s) -> WAV (memory)
   -- POST /api/v1/sessions/{id}/transcribe  (raw audio/wav, bearer added in main) -->
FastAPI: auth, ownership, limiter, size cap, WAV validation -> STTProvider (fake) -> text
         -> detector (pure function) -> {text, is_question, confidence, provider, model, latency_ms}
Main -> renderer event: utterance text + "possible question" flag (audio already wiped)
User presses Answer (or an opt-in auto-answer toggle, default off)
   -> existing SSE question path with input_source = "audio" (single-use utterance token, as for screenshots)
```

**There is no second answering system**: detected questions go through the existing `sessions:stream-start` / `POST /sessions/{id}/questions`. The desktop keeps using SSE.

### Speech-to-text provider abstraction (mirrors `VisionProvider`)
- Port `STTProvider.transcribe(audio: AudioInput, language: str | None, model: str) -> SttResult(text, usage)`; `AudioInput` (PCM bytes, sample rate, duration) and `SttResult.text` hide their content from `repr`; adapters raise only `LLMError` subclasses.
- `FakeSTTProvider`: deterministic (scripted reply, or reply derived from the audio digest, scripted failures, optional delay, cancel counter, records digests only), plus an `SttRouter` with allow-list, fallback, timeout, breaker and usage metering. Allow-list `STT_ENABLED_PROVIDERS`: empty by default; `fake` only, local environments only.
- **No real provider is selected or integrated** (OQ-5 stays open; a real adapter, e.g. an OpenAI-compatible transcription endpoint, would be a later separate approval after privacy, retention, cost and terms review).
- Utterance-at-a-time (batch) transcription, **not** streaming partials, for the MVP: simpler, deterministic to test, no WebSocket audio frames. Latency is utterance end plus one provider call.

### Question detection (simple, testable)
- Pure function `classify(text) -> (is_question, confidence, reason)` in the backend (`app/qa/detection.py`), single source of truth for all clients. Rules: a question mark; interrogative or request openers ("what / why / how / when / where / who / which", "can / could / would / do / does / did / is / are / have you", "tell me", "describe", "explain", "walk me through", "give me an example", "talk about"); a minimum word count; filters for acknowledgements and fillers ("okay", "right", "thanks", "sounds good"). Hindi and Telugu: a small interrogative-word list, **informational only** (no gate).
- Evaluated against a small labelled English fixture set with a precision/recall gate; hi/te fixtures are reported, not gated.
- **Possible-question UX:** a flagged utterance shows an "Answer" button; an opt-in "Auto-answer detected questions" toggle (default off) triggers the same path. The roadmap's LLM-classifier second stage and follow-up resolution are **deferred** (extra latency, cost and privacy exposure; the user confirmation makes heuristics sufficient for a first version).

## 4. Do we need a transcript table or WebSocket changes? **No (for the MVP)**

- **Transcript table: not necessary.** The live transcript is shown from renderer memory while listening and cleared on stop, sign-out, session change and quit. A question that the user answers is persisted by the existing tables (`detected_questions.raw_text`, `source = 'audio'`; `generated_answers`), exactly as screenshot questions. Persisting every utterance is what would create a transcript-retention problem (OQ-11: retention and legal obligations unresolved, and `PRIVACY.md` prefers minimal retention). If transcript review/export is wanted later, it is an optional **12C** with the `transcript_segments` design in `DATABASE.md`, a retention policy and its own approval. This amends 12A's roadmap acceptance line "transcript stored and viewable" to "live transcript viewable".
- **WebSocket:** not required. Audio frames over the gateway would need binary support (it currently closes with `1003`) or base64 inside 16 KiB frames, a protocol change plus generated-contract updates; REST per utterance reuses the screenshot pattern and the desktop already uses SSE. WebSocket audio/partial-transcript messages are only worth building if streaming partials are later required.
- **Database migration: none.** `'audio'` is already an allowed `detected_questions.source`.
- **Contract additions (additive, no existing contract changes):** `POST /api/v1/sessions/{id}/transcribe` (new; raw `audio/wav`, PCM16 mono 16 kHz, `AUDIO_MAX_BYTES` about 1.5 MiB / 30 s, `200 {text, truncated, is_question, confidence, provider, model, latency_ms}`, same error style as screenshots) and `input_source: "audio"` on the question endpoint.

## 5. Privacy design (MVP)

- **Explicit, visible:** off by default; native consent to enable; every listening session starts with an explicit **Start** and needs a visible and focused window, a ready session and a selected source; a persistent in-app banner "● Listening to interviewer audio / microphone" with a one-click **Stop**, plus a tray tooltip and taskbar flash on start.
- **No hidden or background recording:** listening stops automatically when the window is hidden or minimised, on sign-out, session change, disabling the setting, a reload or crash, quit, and after a maximum continuous time (proposed 2 hours). No timers start it; no hotkey.
- **Memory only:** a short ring of frames plus the current utterance in main; the buffer is zero-filled after each upload and on every exit path; the backend holds the audio only for the request. No audio is written anywhere (no temp file, no spool, no queue).
- **No content in telemetry:** logs, metrics, usage records and error messages carry IDs, byte counts, durations, status and latency only; sentinel tests assert audio bytes, their digests and the transcript text never appear.
- **Consent of others:** the disclosure text says audio of other people may be captured and processed; compliance with local law and the interview platform's rules is the user's responsibility (wording is an owner decision).

## 6. Smallest recommended MVP

1. Opt-in setting, native consent, explicit Start/Stop, visible indicator.
2. **One source at a time, selectable: "Interviewer audio (system)" and "My microphone"** (shared pipeline; the second source costs one extra capture call). If the loopback spike fails, microphone only.
3. Energy-VAD utterances (≤ 30 s) → `/transcribe` with the fake provider → live transcript list in the window (English MVP) with a "Possible question" flag.
4. Answer button (and optional auto-answer toggle, default off) → existing SSE Q&A with `input_source = audio`.
5. No transcript persistence, no WebSocket change, no migration, no real STT provider.

## 7. Test-first execution plan (tests are written before or with each piece)

**Backend (pytest, deterministic):** WAV validation table (corrupt, truncated, wrong rate/channels/bit depth, lying lengths, oversize, over duration); fake STT determinism, scripted failures, fallback, breaker, timeout and cancellation; the endpoint (auth, foreign/unknown session indistinguishable, readiness 409, 415/413/422/429/503, slot release on every path); `input_source: audio` stored and invalid values refused; detector unit tests plus the labelled-set precision/recall gate; **privacy sentinels** (PCM sentinel, its base64/digest, transcript sentinel absent from logs, metrics, usage records and every error body); nothing persisted (no new table, no file); SSE and WebSocket contract suites unchanged; `mypy` over app+tests (CI's command), `ruff`.
**Desktop (vitest):** VAD/segmentation on synthetic PCM (silence, speech, short pause, long utterance cut, minimum length); WAV builder; `AudioService` state machine (gating: disabled, no session, hidden/unfocused/minimised window, no consent; stop on hide/minimise/sign-out/session change/quit; buffers wiped; chunks refused unless listening, size/rate caps); a pure `decidePermission` function (allows only audio-only `media` while arming; denies video, display capture and everything else; default deny) and the display-media handler (denies unless armed); IPC sender validation and strict arguments through the real `registerDesktopIpc` harness; source guards (permission handler change confined to one module, no disk writes, no console, no `openExternal`); renderer store (transcript only in memory, cleared on stop; possible-question chip; Answer is explicit; auto-answer default off); regressions for typed Q&A, screenshots and session creation.
**Replay integration:** synthetic PCM fixtures of scripted "interviewer" utterances → VAD → real compiled desktop client → real local backend with fake STT and fake LLM → question detected → answer streamed → `source = audio` in the database. Packaged-app smoke: permissions still default-deny while the setting is off.
**Manual (real hardware):** short checklist in §9.

## 8. Acceptance criteria

1. Nothing listens until the user enables the setting, confirms natively and presses Start; the indicator is visible and Stop always works.
2. Listening stops on every condition in §5; no audio is ever written to disk, a database or a log.
3. Permission and display-media grants exist only in the armed state, audio only; all other permissions remain denied (tested).
4. Replayed fake audio produces the scripted transcript, the expected possible-question flags, and a streamed answer stored with `source = audio`.
5. The detector meets the English precision/recall gate on the labelled set; Hindi/Telugu results are reported.
6. Sentinel tests prove no audio, digest or transcript text in logs, metrics, usage or errors.
7. No migration, no WebSocket change, no new dependency, no real STT provider; existing typed Q&A, screenshot and session-creation behaviour unchanged.
8. Backend (including CI's `mypy` over tests), desktop typecheck/tests/build/package smoke pass; the owner's manual hardware checklist passes.

## 9. Manual checklist for real microphone / system audio (owner)

Enable the setting (native dialog appears; Cancel leaves it off); Start with the microphone: speak a question, see the transcript line and the possible-question flag, press Answer; Stop and confirm the banner disappears and Windows' microphone-in-use indicator goes away; Start with interviewer audio while a video/call plays a spoken question; confirm it is transcribed and your own speakers keep playing; hide the window or minimise: listening stops; sign out / switch session: stops; leave it running for the maximum time or check the auto-stop; confirm no audio files appear in the temp and app-data folders; try Bluetooth headphones and a different default output device; silence and protected-content playback behave safely (no crash, clear message).

## 10. Owner decisions required before implementation

1. **Approve the permission change** described in §2 (audio-only `media` and loopback display-media grants, armed state only, plus the new IPC and the default-off setting).
2. **Sources in the MVP:** both selectable (recommended) or interviewer audio only; and agree the fallback to microphone-only if the loopback spike fails.
3. **No transcript persistence and no WebSocket change** for the MVP (amends 12A's acceptance wording); optional later 12C.
4. **Detection:** heuristic stage plus user confirmation, auto-answer toggle default off; the LLM second stage and follow-up resolution deferred (amends 12B's acceptance wording).
5. **Consent/disclosure wording** for audio that may include other people (OQ-11; legal review is outside this plan).
6. **STT provider:** fake only for this batch; a real provider choice (OQ-5) stays a separate approval.
7. **English only** for the gated detector; Hindi/Telugu informational.
8. The additive contracts (`/transcribe`, `input_source: audio`).

Not decisions (already settled by the repository): reuse of the existing Q&A pipeline, the memory-only/no-raw-audio rule, the opt-in-with-native-consent pattern, no migration.
