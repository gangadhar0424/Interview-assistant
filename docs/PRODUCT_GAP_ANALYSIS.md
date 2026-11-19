# Product Gap Analysis

Status: **PROPOSED. Audit only; no application code, dependency, migration or configuration was changed.** Date: 2026-11-18.

## Method and limits

- Read: CLAUDE.md, README, PHASES, DECISIONS, DESKTOP_RELEASE, PHASE_12_REPORT, PHASE_13_14_PLAN, RISKS_AND_OPEN_QUESTIONS, PRIVACY, existing SECURITY_REVIEW; the desktop main/preload/shared/renderer sources named below; the backend `ai/`, `audio/`, `screenshots/`, `qa/` modules (entry points and adapters); CI workflows and `apps/desktop/package.json`.
- Not read line by line: the web app internals, ingestion/retrieval internals, migrations, and every test body. Claims about those rely on docs and file names only and are marked "(docs)".
- Evidence for "missing" is a repository search with no match (stated per row), not a proof of absence in code I did not open.
- **Competitor products (ParakeetAI, ClearMyRound, Final Round AI) were not researched in this pass** (no web access was used). The rubric below is the 18 capabilities in the directive; any competitor-specific feature claims should be verified separately before they drive scope.
- Nothing was executed. No test result or measurement is asserted here.

## Summary

| # | Capability | Status |
|---|---|---|
| 1 | Desktop architecture | Fully implemented (Windows only) |
| 2 | Floating assistant window | Implemented, needs improvement |
| 3 | Live audio processing | Implemented, needs improvement |
| 4 | Speech-to-text | Partially implemented |
| 5 | Real-time question detection | Implemented, needs improvement |
| 6 | Context-aware answers | Fully implemented (pending real-provider validation) |
| 7 | Resume / JD context | Fully implemented |
| 8 | Coding interview assistance | Partially implemented |
| 9 | Screenshot question input | Fully implemented (backend and desktop) |
| 10 | Provider selection and fallback | Fully implemented (not live-tested) |
| 11 | Keyboard shortcuts | Missing |
| 12 | Multi-monitor support | Partially implemented |
| 13 | Privacy and consent controls | Implemented, needs improvement |
| 14 | Session management | Implemented, needs improvement |
| 15 | Performance and latency | Missing (no measurement) |
| 16 | Windows packaging and signing | Partially implemented |
| 17 | Automatic updates | Missing |
| 18 | Error handling and crash recovery | Partially implemented |

## Detail

**1. Desktop architecture: Fully implemented.** Electron + React + TS in `apps/desktop`. `apps/desktop/src/main/index.ts:200-202` sets `contextIsolation: true` and `sandbox: true`; `setWindowOpenHandler` denies, `will-navigate` is restricted, permission handlers are installed (`index.ts:209-233`). Fuses are configured in `apps/desktop/package.json` (`electronFuses`). Single-instance lock `index.ts:319`. Typed IPC with validation: `ipc-validation.ts`, `ipc-security.ts`, `preload/index.ts`, `shared/contracts.ts`. Gap: Windows only (OQ-12 open).

**2. Floating window: Implemented, needs improvement.** `window-controller.ts` provides compact/expanded sizes, opacity (0.65-1.0), always-on-top, close-to-tray. Missing: persisted window position, click-through or "dock" modes, behaviour when the display layout changes.

**3. Live audio: Implemented, needs improvement.** `audio-service.ts` (explicit start, window-visible gate, 2 h cap, in-memory buffers zero-filled, rate/queue limits), `audio-vad.ts` (fixed energy threshold RMS 0.02, 700 ms end-of-silence, 28 s forced cut), microphone and Windows loopback via `media-permissions.ts`. Gap: segmentation is utterance-based, so the transcript cannot appear before roughly 0.7 s of silence plus upload plus provider time (derived from the constants, not measured). A fixed energy threshold will mis-segment in noisy rooms.

**4. Speech-to-text: Partially implemented.** Backend adapters for OpenAI, Groq, Sarvam, Gemini (`services/api/app/ai/adapters/stt.py`), router with breaker and fallback (`ai/stt_router.py`), per-session voice provider (D-57). Gap: **never run against a real provider** (OQ-4, OQ-5 in RISKS_AND_OPEN_QUESTIONS; PHASE_12_REPORT "not verified"). Batch upload only; no streaming STT; no WebSocket audio (deferred in CLAUDE.md).

**5. Question detection: Implemented, needs improvement.** `services/api/app/qa/detection.py` + `audio/service.py:168` return an advisory `is_question`; schema comment says "simple English heuristic" (`audio/schemas.py:13`). Hindi/Telugu detection quality is unknown. Desktop requires user confirmation of detected questions; auto-answer is off by default (PHASE_12_REPORT). No evaluation set for detection exists that I found.

**6. Context-aware answers: Fully implemented.** D-66 answer pipeline (router, hybrid retrieval, exact-text answers, validation; `qa/answer_router.py`, `retrieval/hybrid.py`, `qa/validation.py`). Pending owner testing per CLAUDE.md; dev embeds with the fake model until `EMBEDDING_PROVIDER=fastembed`; the 31 DB-backed pipeline tests could not run in my last session (no Postgres).

**7. Resume/JD context: Fully implemented.** Resumes, documents, job import (SSRF-safe, D-36), session wizard and desktop create-session (Phase 7, 11.5).

**8. Coding interview assistance: Partially implemented.** A `coding` question type exists (`preferences/enums.py`), and screenshots go through a vision extraction prompt (`screenshots/service.py`, `build_vision_extraction_prompt`). Not verified: code-block rendering in the desktop answer view, language selection, complexity/test-case structure in the coding template, and long-answer scrolling in the compact window. No coding-specific evaluation.

**9. Screenshot input: Fully implemented.** `capture-service.ts` (explicit start, visible countdown, cancel, held-image state), `capture-electron.ts` (only module touching `desktopCapturer`, memory only), `capture-image.ts` (size caps, blank-frame detection), backend `screenshots/router.py` (PNG validation, size cap, nothing persisted). Known limitation documented in `capture-electron.ts`: `getSources` renders all displays and drops the unchosen ones.

**10. Provider selection and fallback: Fully implemented.** Separate reasoning and voice registries, exclusive routing, circuit breaker (`ai/router.py`, `ai/stt_router.py`, `ai/breaker.py`), wizard and desktop dropdowns (D-57/D-58). Not live-tested. Persisted usage/cost records are not implemented (R-24).

**11. Keyboard shortcuts: Missing.** Search of `apps/desktop/src` for `globalShortcut`, `accelerator`, `shortcut` finds no shortcut registration (only a `keydown` handler search returned nothing relevant). Everything requires mouse focus on the window.

**12. Multi-monitor: Partially implemented.** `capture-electron.ts` lists displays and captures a chosen one with a picker. **Manual multi-monitor testing was deferred and never done** (PHASE_10B_MANUAL_TEST_PLAN). Window placement across monitors and DPI changes are untested.

**13. Privacy and consent: Implemented, needs improvement.** Strong: native audio consent, explicit start, visible tray/in-app indicators, memory-only audio and screenshots, video/display capture denied (`media-permissions.ts`), PRIVACY.md. Gaps: no acceptable-use / interview-rules acknowledgement (OQ-15 open); no account deletion/export or retention jobs (PRIVACY.md, 13A plan R6); no `audit_events` writers (PHASE_13_14_PLAN); third-party provider data terms unverified (OQ-4). `setContentProtection` is not used anywhere (search of `apps/desktop/src`), see SECURITY_REVIEW addendum.

**14. Session management: Implemented, needs improvement.** Web wizard and list, desktop create-session, history GETs, detached generation with resume (Phase 9C). Transcript lines are not persisted by design (500-line in-memory cap, `audio-service.ts`). Gap: no in-app session list/switch on desktop beyond what Phase 11.5 added (not re-verified), no end-of-session summary or optional transcript export.

**15. Performance and latency: Missing.** Backend has Prometheus histograms (`core/metrics.py`: `http_request_duration`, `llm_request_duration`, `answer_stage_seconds`). No time-to-first-token or transcribe-latency metric was found by name, and the desktop has no timing instrumentation (search for `performance.now|perf_hooks` in `apps/desktop/src` returned nothing). R-1 in RISKS_AND_OPEN_QUESTIONS is still open. See PERFORMANCE_BASELINE.md.

**16. Packaging and signing: Partially implemented.** NSIS x64 target, fuses, icon, protocol handler (`package.json` build block). Unsigned. **The NSIS installer itself was never executed by the engineering phases** (DESKTOP_RELEASE §5); the owner did later install/uninstall manually (PHASE_10B_MANUAL_TEST_PLAN). No signing configuration. CI builds the app but does not run `package:win` on Windows (CI is `ubuntu-latest` only, `.github/workflows/ci.yml`).

**17. Auto-update: Missing.** No `electron-updater` or `autoUpdater` in the repo (search of `apps/desktop`). D-50 is Proposed. Safe updates require signing first (DESKTOP_RELEASE §4).

**18. Error handling and crash recovery: Partially implemented.** Typed, user-safe error messages, SSE/WS cancel and resume, audio stops on `render-process-gone` (`ipc.ts:227`), closed-window cleanup. Missing: renderer reload/recovery after a crash, main-process `uncaughtException` handling, local diagnostic log file (IDs only), an in-app "report a problem" bundle. `crashReporter` is not used.

## Documentation drift noticed

- CLAUDE.md "Current Status" is one very long paragraph that mixes accepted and pending phases; it is hard to tell the single current gate. Suggest a short status table.
- `docs/PHASES.md` mentions Phase 10 deferrals that Phase 12 report partially closed; some "deferred" items (multi-monitor, signing) remain genuinely open and are carried into the roadmap.
- `docs/SECURITY_REVIEW.md` is titled "Phase 3 Security Review"; later phases have no review (addendum added).
