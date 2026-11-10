# Phase 11 — Screenshot Input: Architecture and Implementation Plan

Status: **PLANNED, NOT STARTED.** This document is a proposal for owner review. It changes no code, API contract, schema or dependency. Nothing here is binding until the owner accepts it (it would be recorded as a new decision; see §12).

Written for: the project owner, as the review basis for approving Phase 11.

## 1. Current architecture findings (what Phase 11 can reuse)

Verified by reading the repository at commit `ae53045`.

| Area | Finding | Reuse / implication |
|---|---|---|
| **Desktop main process** | `apps/desktop/src/main`: all network and credentials live in main (`api-client.ts`, `secure-refresh-store.ts`); API paths are allow-listed to `/api/v1/*` and `/health`; every IPC channel goes through `handle()` which runs `assertSender` (`isTrustedRenderer`) and returns safe `DesktopResult` errors | New capture/upload channels follow the same pattern; the renderer never sees tokens |
| **Preload bridge** | `contextBridge` exposes 15 named functions + 2 subscriptions, no generic `invoke` | Add a few named functions only; no generic channel |
| **Renderer / CSP** | `connect-src 'none'`, `img-src 'self' data:`, `style-src 'self'`; no Node; no permissions (handler denies all) | A small preview thumbnail as a `data:` URL is already allowed; no CSP change needed. `getDisplayMedia` stays denied and is not used |
| **Window/Electron settings** | sandbox, context isolation, no webview, nav and `window.open` blocked, fuses applied (D-49) | Capture must use main-process `desktopCapturer` (Electron 45, no renderer capture API). No new fuse or security setting is required |
| **Backend Q&A** | `POST /sessions/{id}/questions` (SSE) and WS `question.submit` run the same `QuestionAnswerer.ask`; ownership from `CurrentUser`; per-user rate limit and concurrency cap (`QuestionLimits`); question text capped at `MAX_QUERY_CHARS = 2000`; the question is wrapped as untrusted data by `PromptBuilder` | The whole answer path can be reused unchanged if the screenshot is converted to question **text** first |
| **DB** | `detected_questions.source` already allows `'screenshot'` (CHECK from migration 0007; the repository hard-codes `"typed"` today); `DATABASE.md` also sketches a `screenshots` table, not implemented | No migration is needed if screenshots are not persisted (§5) |
| **AI layer** | `LLMProvider` port takes `Message(role, content: str)` only; `Capability = "streaming" \| "completion"`; `ProviderRegistry` allow-list; `LLMRouter` fallback + breaker + usage; `FakeLLMProvider` is scripted; OpenAI adapter maps text messages only; no `VisionProvider` exists | Image input cannot ride on `Message` without changing the text port. Add a **separate** `VisionProvider` port beside it (§6) |
| **Prompting** | single `PromptBuilder` with versioned template files; a test forbids inline prompt strings | The vision-extraction prompt must be a template file built by `PromptBuilder` |
| **File storage / upload** | `FileStorage` (local disk), upload validation by magic bytes, `BodySizeLimitMiddleware` (**exact-path** limits; multipart is spooled to disk by Starlette) | Not reusable as-is for screenshots: they should not touch disk, and the session-scoped path is not an exact path (§7) |
| **WS gateway** | frames capped at 16 KiB; text/JSON only; `screenshot.submitted` is listed as "not Phase 9" | Images do not belong on the WS channel; the desktop app uses SSE anyway |
| **Logging/privacy** | request log carries path/status/ids only; redaction filters exist for WS tickets; `PRIVACY.md` requires purpose, storage, retention, third-party processing and consent be specified **before** implementing screenshots | This plan must be accepted before Phase 11 starts |
| **Existing design docs** | `ARCHITECTURE.md` §10 and `API.md` §5 already sketch `POST /sessions/{id}/screenshots`, validation, retention, `VisionProvider`, OCR-vs-multimodal; D-19 is still *Proposed*; PoC-7 (OCR vs multimodal) is assigned to Phase 11 | This plan refines and, in places, narrows those sketches (retention, response shape) |

## 2. Recommended MVP scope

**A two-step, user-confirmed "screenshot → question text" flow:**

1. The user presses **Capture** in the desktop app and picks **one display**.
2. The app shows the captured image as a preview **before anything leaves the machine**.
3. The user presses **Send for text extraction**; the backend's vision provider reads the screenshot and returns the question/problem **as text**; the image is discarded.
4. The extracted text appears in the existing question box, **editable**; the user presses **Ask** and the existing text pipeline (retrieval, prompt, SSE streaming) answers it. The stored question is marked `source = 'screenshot'`.

### Capture-mode evaluation

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| Full screen (primary display only) | simplest | wrong display on multi-monitor; most incidental data | no (subsumed by display selection) |
| **Selected display** | `desktopCapturer` supports it natively; no extra window; predictable; works with the preview | still captures everything on that display | **Recommended MVP** |
| Selected application/window | less incidental data; same API (`types: ['window']`) | window titles are sensitive in a picker; minimised/occluded/protected windows come back black | best **first follow-up** (11C) |
| User-selected region | least data, best privacy | needs a transparent overlay window, DPI/multi-monitor math, and its own indicator; the most code and risk | later, if requested |

The preview-and-confirm step gives most of the privacy benefit of window/region capture at a fraction of the complexity. **Owner decision (OQ-11-1):** accept display capture for the MVP, or prefer window capture first.

### Explicitly out of MVP
Background/periodic/automatic capture (never), hotkey-triggered capture, window or region capture, multimodal answering with the image in the answer prompt, storing screenshots, regenerate-from-image, OCR engine, JPEG/WebP input, clipboard/file-drop images, audio, screen *recording*, capture exclusion of other apps, and any change to existing endpoints' behaviour.

### What the user sees (visual indication)
- **Before:** Capture is in the main window only and is **off until the user enables "Screenshot input" in Settings** (default off), with the disclosure text in §4. The first use also shows a **native** confirmation dialog from the main process (a compromised renderer cannot fake or skip it). Choosing a display shows labels only (name, resolution, primary); **no thumbnails are taken to build the picker**.
- **During:** a visible 3-second countdown banner ("Capturing *Display 1* in 3… **Cancel**") controlled by the main process; at capture time the taskbar button flashes and the tray tooltip reads "Capturing screen…". The capture is one frame, not a stream, so "during" is the countdown plus the brief flash.
- **After:** a preview with the banner "Held in memory only — not sent. **Send for text extraction** · **Discard**". While sending: "Sending… (cancel)". After: "Screenshot discarded." The image can be re-viewed only until discarded.
- **Own window:** to keep the assistant window out of the shot, the main process hides it for the capture instant (≲300 ms) and restores it with the preview. This is a short, user-initiated, visible interruption, **not** capture exclusion from other apps. **Owner decision (OQ-11-2):** keep this, or capture with the assistant window visible (simpler, but the window covers part of the screen). It interacts with the unconfirmed minimize observation from Phase 10 and must be tested with always-on-top on and off.

## 3. Privacy and security design

### Explicit initiation and preventing hidden capture
- The only capture code is in the Electron **main** process. The renderer cannot call `desktopCapturer`, `getDisplayMedia`, or read the screen; the permission handler continues to deny everything.
- Main-enforced preconditions for `capture:start`: sender is the trusted renderer; the feature is enabled in Settings; the first-use native consent was given; the main window is **visible and focused** (so capture can never occur while hidden in the tray or minimised); a user is signed in and a ready session is selected; at most one capture in flight; minimum 5 s between captures and a per-session cap (e.g. 10); and the countdown (cancellable) must complete. IPC cannot prove a user gesture, so these main-side gates are the defence if the renderer were compromised.
- No timers, hotkeys, schedulers or capture on app start; no API to list or fetch past captures; no file or clipboard output.
- The code-search test "no `desktopCapturer` call outside the capture module" and "capture module has exactly one entry point" are part of the acceptance tests.

### Sensitive screen content
- Preview before send; **extracted text is shown and editable before the answer pipeline sees it** (this also blocks silent prompt injection from on-screen text).
- Display selection shows names only, never window titles or thumbnails.
- Settings and first-use text state plainly that the *whole selected display* is captured and that the image goes to the configured backend and then to its vision provider (§4). Recommend the user close private windows first. No OCR/redaction is attempted in the MVP.

### Format and limits
- **Desktop output: PNG only**, produced by `nativeImage.toPNG()` (no EXIF/metadata), downscaled so the long edge ≤ 2560 px (still legible for text); if the PNG exceeds the cap, downscale further, never switch format.
- **Backend accepts `image/png` only** in the MVP. Limits (configurable, defaults proposed): body ≤ **5 MB** (matches `ARCHITECTURE.md`), long edge ≤ **4096 px**, total ≤ **16 megapixels**, short edge ≥ 64 px. JPEG/WebP can be added later with an explicit metadata-strip step.
- Validation without a new dependency: PNG signature, bounded chunk walk (CRC and chunk sizes), IHDR dimensions checked **before** any decoding or provider call, reject trailing data or too many chunks, then re-emit only the critical chunks (IHDR, PLTE, IDAT, IEND) so ancillary text/metadata chunks never reach a provider. Never decode pixels in the API process.

### Memory handling and persistence
- **MVP does not persist screenshots.** Desktop: the PNG exists only as a `Buffer` in main, referenced by an opaque, single-use **capture id** (valid ≤ 60 s, tied to the sender and session). The renderer gets only a ≤ 640 px preview data URL and the id, never the full image and never a way to supply its own bytes or a file path. On send, discard, expiry, sign-out, session change or quit the buffer is zero-filled and released. Nothing is written to disk.
- Backend: the raw request body is read into a bounded in-memory buffer (no multipart, so nothing spools to disk), validated, passed to the vision provider, and dropped. No file, no table, no cache, no queue, no retry store. This **narrows** the `ARCHITECTURE.md`/`DATABASE.md` retention design and would supersede D-19's "short retention" for the MVP (§12). If a later phase wants stored screenshots, it needs the `screenshots` table, a `FileStorage` namespace, a retention sweeper (default 24 h–7 d, deletable) and its own approval.
- The extracted text is **not stored** until the user presses Ask; then it is the question's `raw_text`.

### Logging and telemetry
- Never log, trace, or put in metrics/usage records: image bytes, base64, extracted text, window/display titles, file names. Log IDs, byte count, dimensions, status, outcome and latency only. The vision provider's request and response bodies are never logged. Errors use fixed safe messages (the existing pattern).
- Tests inject a sentinel into the image/extracted text and assert it is absent from all captured logs, metrics and error bodies.

### Authorization and isolation
- `CurrentUser` only; the session must belong to the caller and be `ready` (foreign or unknown session → the existing indistinguishable `404 session_not_found`). Identity and session come from the token and path, never from the body.
- A per-user limiter for extraction (proposed: 6 per minute, 1 concurrent, in-process like `QuestionLimits`) and a size cap enforced before buffering. The extracted text can only be sent to the session it was extracted for, through the normal question endpoint (which re-checks ownership).

### Secure Electron IPC
- New channels (named, validated, `assertSender`, safe errors): `capture:list-displays`, `capture:start`, `capture:cancel`, `capture:discard`, `capture:extract` (capture id + session id; main uploads using its own token). Strict schema: ids are UUIDs, no free-form fields, unknown keys rejected.
- The API client gets one new method for the raw `image/png` upload and keeps the `/api/v1/` path allow-list. Tokens, refresh token and API keys never reach the renderer; no provider key exists on the desktop.
- No CSP change and no weakening of any Electron setting; no new Electron dependency; Windows only for now (macOS would need the Screen Recording permission flow later).

### Third-party processing (must be disclosed)
**Sending a screenshot to a vision provider transmits everything visible in it (other people's messages, credentials on screen, confidential material) to a third party.** The fake provider has no such effect, but a real one does. Before any real provider is enabled for screenshots: review that provider's data-use, retention and training terms for **image** inputs (OQ-4 currently covers text only), prefer zero-retention terms, show the in-app disclosure, and do not enable it by default. Image processing must be a separate, explicit server-side allow-list (`VISION_ENABLED_PROVIDERS`), off by default, so enabling a text provider never enables screenshot upload.

## 4. In-app disclosure (draft text for owner review)
"Screenshot input captures everything visible on the display you choose and sends it to your Interview Assistant server, which may forward it to an AI provider to read the text. Close anything private first. Screenshots are shown to you before sending, are not saved, and are discarded after text extraction."

## 5. Proposed architecture

```text
Desktop renderer ── named IPC ──► Electron main ── HTTPS ──► FastAPI ── VisionProvider ──► (fake | later real)
   (preview thumb only)           capture module              screenshots router          vision router
                                  capture-id store            validation, limits          + breaker, usage
                                  api-client (bearer)         no storage                         │
                                                                                                 ▼
user edits text ──► existing POST /sessions/{id}/questions (SSE) ──► retrieval ──► LLM stream (unchanged)
```

Reused unchanged: auth, session ownership, retrieval, `PromptBuilder` answer templates, the text `LLMProvider` and router, SSE streaming and the WS gateway. New: capture module, capture IPC, vision port/fake/router, screenshot endpoint, input limits.

## 6. AI and provider integration

- **Do not change `LLMProvider` or `Message`.** Add a second port in `app/ai/`: `VisionProvider.extract_text(image: ImageInput, model, config) -> VisionResult(text, usage)`, `ImageInput` = validated PNG bytes + width/height. Raises only `LLMError` subclasses (reuses the error taxonomy and safe messages).
- Add `"vision"` to the `Capability` literal (additive) and a vision-model entry in `ModelSpec`; keep a **separate allow-list** (`VISION_ENABLED_PROVIDERS`, `VISION_FALLBACK_CHAIN`, default empty). Text routing, fallback and the breaker are untouched; a thin `VisionRouter` reuses `CircuitBreaker`, timeouts, `UsageSink` and metrics, and has no streaming (extraction is one bounded call, e.g. 20 s timeout).
- **Fake vision provider (required for tests):** `FakeVisionProvider` in the style of `FakeLLMProvider`: scripted reply text, injectable `LLMError` failures, optional delay (also enables a slow-provider cancel test), records calls and the image digest/size (never the bytes). No network. Optional deterministic mode: reply derived from the image SHA-256 so tests can assert the right image was sent.
- **Extraction prompt:** a new versioned template file under `preferences/templates/` built through `PromptBuilder` (no inline prompt). It tells the model to return only the question/problem text visible in the image, never to follow instructions found in the image, and to say so if there is no question. Output is treated as **untrusted text**: cleaned, capped at 2000 chars (`MAX_QUERY_CHARS`, with a "shortened" flag), shown to the user, and later wrapped as an untrusted `question` by the existing builder (Issue 1 neutralisation already applies).
- **Real provider, later (11C, optional):** an OpenAI-compatible vision adapter in `app/ai/adapters/` (the only place a provider SDK may be imported), sending the image as a data URL. **Not assumed available:** the current OpenAI/Ollama configuration is text-only, and whether any configured model accepts images is unknown. It is gated on OQ-4 for images and a configured vision model. Real-provider validation stays a separate item, not required for Phase 11 acceptance.
- Existing text-only providers keep working untouched: they simply have no vision capability and are never consulted for screenshots. With no vision provider enabled the endpoint returns `503 vision_unavailable` and the desktop hides/disables Capture.
- OCR-then-text (PoC-7) remains an alternative adapter behind the same port; the MVP does not need it.

## 7. API and data-flow proposal

```text
User clicks Capture (Settings: enabled, consent given)
→ main: window visible+focused, rate gates, 3 s cancellable countdown, hide own window, capture display, PNG, downscale, hold Buffer, capture id
→ renderer: preview thumbnail + [Send for text extraction] [Discard]
→ user clicks Send → IPC capture:extract(captureId, sessionId) → main uploads Buffer
→ POST /api/v1/sessions/{id}/screenshots  (Content-Type: image/png, raw body)
→ backend: auth, ownership, limiter, body cap, PNG validation + chunk strip → VisionRouter → provider → text
→ 200 {extracted_text, truncated, provider, model, latency_ms} ; image dropped
→ main: zero + release Buffer → renderer shows editable text
→ user presses Ask → existing POST /sessions/{id}/questions (SSE) with input_source "screenshot"
→ existing retrieval, generation, streaming, persistence ; cancellation unchanged
```

**Best fit: one new REST endpoint + one additive optional field on the existing question endpoint.** Neither a WebSocket message (16 KiB frame cap, text-only, desktop uses SSE) nor folding the image into the Q&A request fits.

1. **New** `POST /api/v1/sessions/{id}/screenshots`: raw `image/png` body (not multipart, so Starlette never spools it to disk); response `200 {extracted_text, truncated, provider, model, latency_ms}` (synchronous, bounded timeout). Errors (fixed codes, no internals): `401`, `404 session_not_found`, `409 session_not_ready`, `413 screenshot_too_large`, `415 unsupported_image_type`, `422 invalid_image` (corrupt, dimensions out of range), `429 rate_limited`/`too_many_in_flight`, `503 vision_unavailable`, `502 vision_failed` (provider error, safe message). This differs from the `API.md` sketch (`202 {screenshot_id, question_id}`, multipart, stored screenshot) because nothing is stored; the doc must be updated first when implementation starts (contract-first rule).
2. **Additive** optional `input_source: "typed" | "screenshot"` (default `typed`) on `POST /sessions/{id}/questions` so `detected_questions.source` is recorded correctly. No change for existing clients; `extra="forbid"` remains; SSE events, WS messages and history shape are unchanged (history may later expose `source`, not needed for the MVP).
3. `BodySizeLimitMiddleware` currently matches **exact paths**; the screenshot path contains the session id, so it needs a small extension (pattern/prefix) — a required, tested change, not an optional one.
4. No new table and **no migration** in the MVP.

## 8. Test strategy

### Automated — backend
| Area | Tests |
|---|---|
| Validation | PNG signature, truncated/corrupt, oversize (header and streamed), bad dimensions, decompression-bomb dimensions (checked before provider call), non-PNG types, trailing data, ancillary chunks stripped, empty body |
| Isolation | foreign/unknown session → indistinguishable 404 (with and without an image); another user's session never reaches the provider; identity only from the token |
| Fake provider | scripted text returned; digest of the image received matches what was uploaded; extraction text is cleaned, capped, flagged `truncated`; injection text in the "image" does not change behaviour |
| Provider failure | each `LLMError` maps to a safe status/message; fallback within the vision chain; breaker opens; no provider → `503`; text pipeline unaffected |
| Cancellation | client disconnect mid-extraction releases the limiter slot and stops the provider call (slow fake) |
| Cleanup | no file under the storage root is created; no temp files; handle dropped (the module holds no reference after return) |
| Logs/metrics | sentinel in image bytes and extracted text absent from all logs, metrics, usage records, error bodies |
| Limits | per-user rate and concurrency; body cap enforced before buffering (declared and chunked) |
| Contracts/regression | `QuestionCreate` accepts the new optional field, still rejects unknown fields; SSE/WS golden transcripts unchanged; `source='screenshot'` stored; prompt-inline-strings test passes; `mypy --strict`, `ruff`; **full backend suite** at the milestone |
| Docs/API | `API.md` updated before the code; import-boundary test (no SDK import outside adapters) |

### Automated — desktop (unit/static; no real capture)
- Capture module with a fake `desktopCapturer`: preconditions (disabled, not consented, window hidden or unfocused, no session, rate limit, in-flight, expired/reused/foreign capture id) refuse without capturing; countdown cancel produces no capture; Buffer is zero-filled and the id invalid after send/discard/expiry/sign-out.
- IPC: untrusted sender rejected on every new channel; strict argument validation; the renderer payloads contain only a preview and an id (never full-image bytes, token or path); error messages safe.
- Source guards: `desktopCapturer` referenced only in the capture module; no `getDisplayMedia`, no `fs` write, no timers that call capture; preload allow-list lists exactly the new named functions.
- CSP unchanged (assert `img-src 'self' data:`, `connect-src 'none'`); typecheck, unit tests, build, package build, packaged-app launch smoke; token-boundary regression (the Phase 10 smoke).

### Manual (owner, packaged Windows build, fake vision provider)
Default-off Settings flow and consent dialog; the countdown, flash and tray tooltip are visible; Cancel truly prevents capture; no capture happens while the window is hidden in the tray or minimised; display picker on 1 and 2 monitors and at 100–150% scaling; preview matches the screen; Discard; Send → extracted text → edit → Ask → streamed answer; the screenshot cannot be recovered afterwards; sign-out/quit discards a pending capture; behaviour with always-on-top on and off (the own-window hide must not leave the window minimised); provider error and backend-down messages; slow-provider cancel (the fake provider's delay option).

### Not part of Phase 11 acceptance
Any real vision provider, OCR accuracy (PoC-7), window/region capture, macOS/Linux.

## 9. Acceptance criteria

1. A screenshot is captured **only** after an explicit user action in the visible app, with Settings opt-in, one-time native consent, a cancellable countdown and the indicators in §2; no background, periodic, hotkey-less or hidden capture exists (guards and tests in §8).
2. The user sees the image before it leaves the machine and can discard it.
3. A PNG within limits is converted to editable text by the **fake** vision provider and answered through the existing streaming pipeline with `source = 'screenshot'`; invalid/oversize/foreign-session requests are refused with safe, tested errors.
4. Images are **never persisted** (desktop or backend), are zero-filled/dropped after use, and never appear in logs, metrics, usage records or error bodies.
5. The renderer never receives tokens, the full image, or a way to supply image bytes/paths; every new IPC channel validates the sender and arguments.
6. Provider failures, timeouts, cancellation and "no vision provider" degrade safely without affecting typed Q&A, text routing or existing SSE/WS contracts.
7. No dependency, migration, CSP weakening or Electron-setting change; `API.md`, `PRIVACY.md`, `ARCHITECTURE.md` §10, `DATABASE.md` note, `DESKTOP_*` docs and the decision record updated.
8. Backend full regression, desktop typecheck/tests/build/package/smoke pass; owner manual checklist passes (multi-monitor is allowed to be "not tested" as in Phase 10).

## 10. Proposed phasing (matches the repo's A/B/C convention)

- **11A — Backend (no desktop):** `VisionProvider` port, `FakeVisionProvider`, vision router/config (default off), extraction template, PNG validation, `POST …/screenshots`, limiter, `input_source`, body-limit extension, docs and tests. Contract-first: update `API.md` before code.
- **11B — Desktop:** capture module, IPC, API client method, Settings opt-in and consent, countdown/preview UI, tests and the manual checklist.
- **11C (optional, separate approval):** real OpenAI-compatible vision adapter and its privacy review; window capture; stored screenshots with retention; region capture.

## 11. Risks and open questions

| # | Risk / question | Mitigation or decision needed |
|---|---|---|
| R-11-1 | Screenshots expose unrelated sensitive data to a third party | Opt-in, preview, editable text, no persistence, separate vision allow-list off by default, disclosure; real-provider terms review before enabling |
| R-11-2 | Prompt injection through on-screen text | Extracted text is untrusted, shown to the user, capped, and passes through the existing untrusted-question wrapping and tag neutralisation |
| R-11-3 | A compromised renderer calling capture | Capture only in main; main-side gates (visible+focused window, rate limit, countdown, consent, single-use ids); no image bytes ever sent to the renderer |
| R-11-4 | Integrity/consent concerns of using such a tool in live interviews | Unchanged project position (`PRIVACY.md`); the feature is visibly indicated and user-initiated; no capture exclusion or concealment |
| R-11-5 | Hiding the own window during capture interacts with the unconfirmed minimize observation | Test with always-on-top on/off; owner may choose to not hide it |
| R-11-6 | Vision calls are slow (seconds) and can fail | Bounded timeout, safe errors, retry by the user, fake-provider tests |
| R-11-7 | Long problems exceed the 2000-character question limit | MVP caps with a "shortened" flag and the user can edit; a larger screenshot-specific limit would change retrieval inputs and needs a decision |
| R-11-8 | Per-process in-memory limiter (as for questions) | Accepted, same as R-30; Redis-backed limiter is a later hardening |
| R-11-9 | Black/empty captures from protected content or GPU issues | Detect an all-black image and tell the user; no workaround that bypasses protection |
| OQ-11-1 | Display capture (recommended) vs window capture first? | owner |
| OQ-11-2 | Hide the assistant window during capture? | owner |
| OQ-11-3 | Is "PNG only, no storage, extraction-then-confirm" acceptable in place of the stored-screenshot design in `ARCHITECTURE.md` §10 / D-19? | owner (would supersede D-19 for the MVP) |
| OQ-11-4 | Is a vision provider available/approved at all, and who owns its terms for image data (OQ-4 for images)? | owner; not needed for Phase 11 acceptance |
| OQ-11-5 | Countdown length (3 s proposed) and per-session capture cap (10 proposed) | owner/default accepted |

## 12. Documentation and decisions to update when implementation starts
`API.md` §5 (screenshot endpoint shape, `input_source`), `ARCHITECTURE.md` §10, `DATABASE.md` (note: no `screenshots` table in the MVP), `PRIVACY.md` (screenshots section), `DESKTOP_ARCHITECTURE.md`/`DESKTOP_WINDOWS.md`/manual checklist, `TESTING_STRATEGY.md`, `PHASES.md`, and a new decision (D-51, *Proposed* until accepted) that records "screenshot MVP = display capture, PNG only, user-confirmed extraction, no persistence" and supersedes the retention part of D-19. Nothing is changed by this plan.
