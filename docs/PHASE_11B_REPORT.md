# Phase 11B — Screenshot Input, Desktop: Report

Date: 2026-11-10. Scope: the Electron desktop client only (D-51, `docs/PHASE_11_PLAN.md`), on top of the accepted Phase 11A backend (commit `2708d60`). **Phase 11 is NOT marked complete. 11C is not started. No real vision provider was added or called. Nothing is committed or pushed.** The backend, web app, migrations and dependencies are unchanged (no new npm dependency).

## 1. What was built

| Area | Files | Behaviour |
|---|---|---|
| Capture orchestration (pure, testable) | `src/main/capture-service.ts` | State machine `idle → countdown → capturing → held → extracting`. Constants: 3 s countdown, 5 s minimum between captures, 10 captures per session, 60 s hold limit, 10 min extraction-token life, 5 MiB PNG cap |
| Screen adapter (the only place that touches the screen) | `src/main/capture-electron.ts`, `src/main/capture-image.ts` | `screen`/`desktopCapturer`/`nativeImage`: display list by name and size (no thumbnails), single-display capture scaled to a 2560 px long edge, PNG only (shrunk in steps if over 5 MiB, never another format), blank-image detection, ≤640 px preview |
| IPC | `src/main/ipc.ts`, `src/main/ipc-validation.ts` | Four named channels: `capture:list-displays`, `capture:start`, `capture:cancel`, `capture:extract`; each runs the existing sender validation; strict argument validation; `capture:event` is main → renderer only; `sessions:stream-start` gets an optional, validated `extractionId` |
| API client | `src/main/api-client.ts` | `extractScreenshot`: raw `image/png` POST with the bearer token (added in main only), one refresh-and-retry on 401, safe error mapping; `streamQuestion` adds `input_source: "screenshot"` only when asked (typed body is still exactly `{text}`) |
| Settings | `src/main/settings-store.ts`, `src/shared/contracts.ts` | `screenshotInputEnabled` (default **false**); old settings files load as off; invalid types are rejected |
| Native consent and indicators | `src/main/index.ts` | Turning the setting on shows a **native** dialog from the main process (renderer cannot fake or skip it); tray tooltip changes during countdown/capture/held and the taskbar button flashes at capture |
| Preload | `src/preload/index.ts` | Four named functions plus one event subscription; no generic channel |
| Renderer | `src/renderer/src/store.ts`, `ui/ScreenshotPanel.tsx`, `ui/App.tsx`, `styles.css` | Capture screen… → display picker → countdown banner with Cancel → preview with "Held in memory only — not sent" → Send for text extraction / Discard → extracted text in an editable box → Ask. Settings toggle with the disclosure text. Panel appears only when the setting is on |

## 2. The approved requirements, and how each is met

1. **Explicit user initiation:** nothing runs until `capture:start`; there is no timer, hotkey, startup, or background path (source guards below).
2. **Opt-in, off by default:** setting defaults to false; every capture channel refuses while off (verified in the packaged app); enabling needs the native confirmation.
3. **Display capture only:** no window, region, audio, disk, hotkey or persistence code exists (source guards).
4. **Main-process capture:** `desktopCapturer` is referenced only in `capture-electron.ts`, imported only by `main/index.ts`, reached only through `CaptureService`. The renderer and preload never import Electron capture APIs.
5. **Conditions:** at start the window must be visible, focused and not minimised, the feature on, a user signed in, and the session present and **ready** (checked against the backend). At capture time the window must still be visible and not minimised (focus is deliberately not required then, so the user can switch to the window they want captured; see §6).
6. **Indicators and countdown:** visible banner "Capturing … in 3/2/1…" with Cancel, tray tooltip and taskbar flash. The countdown is cancellable at any point.
7. **Preview, memory only:** after capture the renderer receives only a ≤640 px thumbnail and the banner "Held in memory only — not sent"; the full PNG stays in a main-process `Buffer`.
8. **Explicit send:** nothing is uploaded until the user presses Send (`capture:extract`); a held image is never sent automatically.
9. **Editable text, explicit Ask:** the extracted text appears in an editable box; the existing SSE question path starts only when the user presses Ask.
10. **`input_source`:** set to `screenshot` only through a **single-use extraction token** issued by main after a completed extraction, bound to that session, expiring in 10 minutes. A forged, replayed, expired or wrong-session id is refused; if the question fails to start, the token is restored so the user can retry; typed questions never carry it.
11. **Discard:** the buffer is zero-filled and released after extraction (success **and** failure), user cancel, countdown cancel, a capture still in flight when cancelled, hold timeout, sign-out/expiry, session change, feature disabled, backend URL change, window hide (including close-to-tray), minimise, navigation/reload, renderer crash, window close and quit.
12. **No persistence:** the image and extracted text are never written to disk, settings, the token store or logs (capture modules contain no `fs`, logging or clipboard code; the settings file is asserted to contain only the five settings).
13. **Hardening preserved:** context isolation, sandbox, narrow IPC, sender validation, strict CSP (`style-src 'self'`, `connect-src 'none'`, `img-src 'self' data:`: the preview is a `data:` thumbnail, no CSP change), denied permissions, fuses, token handling: all unchanged and asserted.
14. **No credentials in the renderer:** verified in tests and in the packaged app (bridge, events and IPC payloads carry no token or image).

## 3. Tests and checks (all actually executed)

**Desktop (`apps/desktop`)**

| Check | Result |
|---|---|
| `npm run typecheck` | pass (tests included) |
| `npm test` | **174 passed** in 10 files (was 33 before 11B; +141) |
| `npm run build` | pass; renderer CSP in the build still `style-src 'self'` |
| `npm run package:win` (unsigned) | pass; NSIS installer + `win-unpacked`; fuses read back unchanged |
| Packaged app launch smoke (real backend, fake LLM + fake vision enabled) | pass: 0 CSP violations/errors; no Node globals; bridge exposes `listDisplays/start/cancel/extract` + `onCaptureEvent`; Settings toggle present and **off**; Assistant page shows **no** Capture button; while off, `listDisplays`/`start` return `screenshot_disabled`; unknown capture id, bad arguments and a forged extraction id are refused; a **typed question still completes**; settings file holds `screenshotInputEnabled:false` |

New desktop test files: `capture-service.test.ts` (50), `capture-ipc.test.ts` (40, drives the real `registerDesktopIpc` with a fake window/screen/backend), `capture-store.test.ts` (19), `capture-api-client.test.ts` (16), `capture-guards.test.ts` (14), plus additions to `ipc-validation.test.ts`.

Coverage requested: explicit capture only (nothing happens on construction/time; listing displays never captures; capture only after the full countdown); rejection when disabled, signed out, window hidden/unfocused/minimised, no or unready session, unknown display, one-at-a-time, minimum interval and per-session cap; cancellation at every stage (choosing, countdown, capture in flight, held, uploading, review) in the service, the IPC layer and the renderer; preview before send and explicit confirmation (no upload or question until `extract`/Ask); discard after success, failure (422/413/503/429, network error, 401), cancellation, timeout, sign-out, backend change, window hide/minimise/close, reload, renderer crash and quit (asserted by checking the actual buffer is all zeros); IPC sender validation on every capture channel, `stream-start` and `update-settings` (no network, capture or consent for an untrusted sender); argument validation; the renderer cannot capture or obtain the full image; typed Q&A unchanged (exact request body and bridge arguments).

**Source guards** (`capture-guards.test.ts`): `desktopCapturer` only in the adapter; adapter imported only by `index.ts`; `captureDisplay` called from one place, behind the countdown and capture-time checks; no `globalShortcut`, `getDisplayMedia`, `MediaRecorder` or `setInterval` in main; no disk/clipboard/log/web-storage use in the capture modules; `node:fs` only in the settings and token stores; renderer has no Electron import; the preload channel list equals the approved list; CSP/sandbox/permission settings unchanged.

**Live compatibility with the real Phase 11A backend** (real compiled `DesktopApiClient`, synthetic PNG, fake providers): login, extract (`Fake extraction: 200x100 image …`), a screenshot-sourced question streamed to `answer.done`, a typed question streamed to `answer.done`; the database recorded `source = 'screenshot'` for the first and `typed` for the second; a non-PNG body and a 10×10 PNG returned `422 invalid_image`, an unknown session `404 session_not_found`; no screenshot table exists.

**Electron adapter pieces under the real runtime** (a synthetic 3000×1700 image, no screen read): the display list reports the machine's display; PNG encoding works; the preview resizes to 640×363 and is a valid `data:image/png` URL; blank-image detection works.

**Backend (unchanged since 11A, re-run):** `test_screenshot_api/validation`, `test_vision_provider`, `test_body_limit_patterns`, `test_qa_api`, `test_qa_sse_contract`, `test_realtime_qa`: **170 passed**. The full 1649-test backend run was done for 11A; no backend code changed since.

## 4. Not tested (do not treat as tested)

- **A real screen capture.** `captureElectronDisplay` (the `desktopCapturer.getSources` call, per-display thumbnail selection, DPI scaling, the shrink loop and the blank check on real frames) was **not run**: running it would capture the owner's actual screen, and the plan says no real capture during development. It is covered only by the structure around it (fake host in tests) and the pieces listed above.
- The native consent dialog, the tray tooltip text, the taskbar flash and the on-screen look of the countdown/preview (no UI automation of native windows). The packaged smoke did **not** turn the setting on, so the enabled flow (Capture button → countdown → preview → send → review → Ask) was verified with fakes and with the real API client against the real backend, but **not end to end in the packaged UI**.
- Multi-monitor and high-DPI behaviour; behaviour with always-on-top on/off while capturing; the installer.
- Any real vision provider (none exists) and real extraction quality.
- Mid-stream cancel against a slow model (as in Phase 10).

## 5. Manual checklist for the owner

Added to `docs/DESKTOP_MANUAL_CHECKLIST.md` ("Screenshot input (Phase 11B)"): default off, native confirmation, display list without thumbnails, countdown/cancel, tray/taskbar indicators, no capture while hidden or minimised, preview and discard triggers, send → review → Ask, two displays at 125–150%, disabling while a preview is held, backend/vision errors, typed questions unchanged. Backend for the test: `APP_ENV=local`, `LLM_ENABLED_PROVIDERS=fake`, `VISION_ENABLED_PROVIDERS=fake`.

## 6. Deviations and design notes

1. **Focus is required at start but not at capture time.** The plan listed "visible and focused"; a strict focus check at capture would cancel the capture whenever the user clicks the window they want captured during the countdown, which defeats the countdown. The window must still be visible (and not minimised) at capture time.
2. **Consent is a native dialog when enabling the setting**, not a separate first-use dialog (equivalent protection, simpler, and it cannot be bypassed from the renderer).
3. **A wrong-session use of an extraction token burns it** (stricter than necessary; a legitimate flow never hits it).
4. **The session is checked against the backend at start** (`GET /sessions`), adding one request per capture, so "valid session selected" is enforced in main rather than trusted from the renderer.
5. The assistant window is **not** hidden or minimised during capture (owner decision), so it appears in the screenshot.
6. **Limitation:** `desktopCapturer.getSources` renders every display; the frames of the displays that were not chosen are produced and dropped immediately (never kept, previewed or sent). Documented in the adapter.
7. The renderer holds a ≤640 px thumbnail (a data URL) while the preview is shown; it is dropped when the image is sent or discarded.
8. Per-session capture counts and the minimum interval live in memory (reset on app restart).

## 7. Known limitations and risks

- Real-capture behaviour is unverified until the owner's manual test.
- The countdown is a visible banner in the assistant window; if the user moves that window off-screen the indicator is the tray tooltip and taskbar flash only.
- DRM/protected or GPU-composited content may come back blank; the app then reports a safe error and does not try to bypass it.
- A screenshot can contain other people's or confidential data; the disclosure text, preview, no-storage rule and the opt-in are the mitigations. A real vision provider needs its own privacy review (OQ-4 for images) before use.
- Per-API-process limits on the backend (R-30) are unchanged.

## 8. Repository state

Uncommitted: the desktop changes and new files above, and the documentation updates (`PHASES.md`, `DECISIONS.md` D-51 status, `PRIVACY.md`, `TESTING_STRATEGY.md`, `DESKTOP_ARCHITECTURE.md`, `DESKTOP_WINDOWS.md`, `DESKTOP_MANUAL_CHECKLIST.md`, `README.md`) and this report. Scratch database, API, worker and Electron processes were stopped and removed; compose containers and Docker Desktop are left running. `apps/desktop/release/` (git-ignored) holds the packaged build for the owner's manual test.
