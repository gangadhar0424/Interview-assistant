# Electron Desktop MVP Architecture

Status: Windows-focused MVP implemented; pending owner review. Decision D-47 selects Electron for this MVP and supersedes the earlier Tauri choice in D-09.

## Structure

```text
apps/desktop/
  src/main/       Electron app lifecycle, API client, settings, secure storage, IPC
  src/preload/    Narrow typed contextBridge
  src/shared/     IPC and API event contracts
  src/renderer/   React UI, Zustand state, Tailwind styles
  tests/          API, SSE, storage, IPC trust, window and renderer-state tests
```

The app is independent of `apps/web/`. It does not duplicate backend session or answer-generation logic.

## Process and trust boundaries

- **Main process:** owns all backend `fetch` calls, access-token memory, refresh-token persistence, settings, window controls, tray, clipboard, and stream cancellation.
- **Preload:** exposes only the typed `window.desktop` bridge. It does not expose `ipcRenderer`, Node APIs, tokens, or arbitrary request methods.
- **Renderer:** React/TypeScript UI only. Electron runs it with context isolation, sandboxing, web security, disabled Node integration, a restrictive CSP, denied browser permissions/popups, and trusted-renderer URL checks on IPC.
- IPC payloads are validated in the main process. API/network errors are returned as typed, safe error values; raw exception details and credentials are not sent to the renderer.

## Authentication and credentials

The app uses the existing `/api/v1/auth/login`, `/signup`, `/refresh`, and `/logout` routes:

1. Main process receives the access token and HttpOnly refresh cookie from the API.
2. The access token stays in main-process memory only.
3. Refresh token is encrypted and decrypted with Electron `safeStorage` and stored in the user's app-data directory. On Windows, Electron delegates encryption to OS-backed protection.
4. If `safeStorage` is unavailable or stored credentials cannot be read, the operation fails explicitly; no plaintext fallback exists.
5. Backend URL changes clear the stored authentication and abort active streams.

Provider API keys are not used by or included in the desktop app. Interview content is sent only to the configured existing backend.

## Questions, streaming and cancellation

The main-process API client uses the existing typed Q&A contract:

- `GET /api/v1/sessions` and `GET /api/v1/sessions/{id}/questions` populate session selection and history.
- `POST /api/v1/sessions/{id}/questions` requests `text/event-stream`.
- The incremental SSE parser handles CRLF/LF/CR boundaries across chunks, validates known event payloads, and requires a terminal `answer.done` or `answer.error` event.
- Request IDs correlate each stream to its renderer state. Delta indexes are applied in order; duplicate events are ignored, and out-of-order deltas produce an explicit failure.
- Cancel aborts the main-process fetch. The backend's existing disconnect behavior owns answer cancellation and persistence; the desktop does not persist answer copies.

## Window and privacy boundaries

The app has a visible frameless window with compact/expanded layouts, resizing, opacity, always-on-top, minimize, and close-to-tray controls. The visible system tray has Show and Quit actions. There is no hidden window, screen-capture exclusion, audio capture, monitoring evasion, or stealth mode. **Session creation (Phase 11.5, D-52):** a New session form (company, optional title, pasted job description, one ready resume, ready documents, answer style from `GET /ai/options`) runs entirely through the main process (`sessions:create-options`, `sessions:create`; the form is validated in `shared/session-form.ts` on both sides, the backend stays authoritative); the renderer never sees a token and receives only file names/status and the new session's summary; drafts live in renderer memory only. With no ready resume the form explains what to do and shows the web app link as text (no browser-launch capability exists). **Screenshot input (Phase 11B, D-51)** is opt-in (off by default; turning it on needs a native confirmation from the main process) and capture-on-demand only: the main process alone owns `desktopCapturer` (`capture-electron.ts`, reachable only through `CaptureService`), requires an explicit start, a visible and focused assistant window and a ready session, shows a cancellable 3-second countdown with a tray tooltip and taskbar flash, holds the PNG in memory only, shows a thumbnail preview, sends the image to `POST /sessions/{id}/screenshots` only after the user confirms, wipes it afterwards, and marks the later question `input_source = screenshot` only through a single-use token. The renderer gets four named capture channels and a small preview; never the full image, tokens or a generic capture API. Details: `docs/PHASE_11B_REPORT.md`.

## Web → desktop handoff (D-55)
The desktop registers `interviewassistant://` (installer `build.protocols`; development-only fallback in `main/index.ts`) and holds a single-instance lock. Links arrive via `argv` (cold start) or `second-instance`, pass through `DeepLinkInbox` and the strict parser (`main/deep-link.ts`), and drive `HandoffController` (`main/handoff.ts`), which redeems the code through `DesktopApiClient.redeemHandoff` only as the signed-in desktop user. The renderer gets `HandoffState` through `handoff:event` / `handoff:get-state` (no code, ever) and shows the READY banner. READY selects the prepared session and starts nothing; a running capture needs a native confirmation before the switch.

## Build and verification

From `apps/desktop/`:

```powershell
npm ci
npm run typecheck
npm test
npm run build
npm run package:win
```

`npm run package:win` produces an unsigned local NSIS build. Signing, auto-update, cross-platform QA, and WebSocket-client integration are not part of this MVP. Manual interaction checks are in [DESKTOP_MANUAL_CHECKLIST.md](./DESKTOP_MANUAL_CHECKLIST.md).

