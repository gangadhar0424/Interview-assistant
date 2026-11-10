# Phase 10A — Electron Validation Report

Date: 2026-11-09. Baseline: commit `94f1a0e` (verified merge). **Phase 10 is NOT complete.** No Electron source code was
changed in 10A: this was validation only. Nothing was committed or pushed after `94f1a0e`.

## 1. What was tested

| Area | Method |
|---|---|
| Static + unit | `npm run typecheck`, `npm test` (vitest), `npm run build` |
| Packaging | `npm run package:win` with signing disabled (`CSC_IDENTITY_AUTO_DISCOVERY=false`) |
| Launch | The packaged `release/win-unpacked/Interview Assistant.exe` (same `app.asar` the installer contains), isolated `--user-data-dir` |
| End-to-end | Packaged app against the **real local backend** (FastAPI + Postgres/pgvector + Redis + Dramatiq worker, fake LLM provider, migrations 0001–0008, scratch database), synthetic user/resume/sessions |
| Driving | Chrome DevTools Protocol over a loopback port (Node 22 built-ins only, no dependency added) + Win32 queries for real window state |
| Security | Live probes of the running renderer, on-disk scan of the app data directory, code review |

## 2. Results

### Desktop unit/static/build
- typecheck pass; **27/27** vitest tests pass; production build pass.
- `npm audit --omit=dev`: **0** vulnerabilities. Full `npm audit`: **6 moderate**, all in the electron-builder/minimatch dev chain (matches the existing docs; no high/critical).

### Windows package / launch
- **Built:** `Interview Assistant-0.1.0-Windows-x64.exe` (NSIS, ~124 MB) + `win-unpacked/`. **Unsigned** (`NotSigned`, confirmed with Authenticode). Default Electron icon. A "duplicate dependency references react-dom" builder warning (harmless to output).
- **Launched:** the packaged exe starts, loads the renderer from inside `app.asar`, the trusted-renderer check passes against the asar path, and the UI renders.
- **Not done:** the NSIS installer itself was **not executed** (a silent install writes Start Menu/Desktop shortcuts and a registry uninstall entry on your machine; I did not want to change your system state without asking). Installer install/uninstall remains untested.

### End-to-end against the real backend (all passed)
- Launch → header shows "Backend connected".
- Wrong password through the real form → "Invalid email or password."; correct login → sessions list; session shows **Ready**, resume `cv.pdf`.
- Typed question through the UI → "Complete". Raw event order via the bridge: `question.created → retrieval.done → answer.started → 158 × answer.delta → answer.done`, first token ~42 ms after the request. Retrieval pulled the uploaded resume chunk (visible in the echoed prompt as prompt-v2 tag `[E1 | resume | source … | chunk 0]`).
- Cancel: `cancelled` event; history shows that answer `cancelled`, no duplicates; every question has exactly one answer.
- History isolation: a question asked in session B never appears in session A's history.
- Cross-user/unknown session: safe `404 session_not_found`, for both history and ask.
- Signup: weak password → `validation_error`; duplicate email → generic `signup_failed`; new user returns only `id,email,created_at`; new user with no sessions → UI "No sessions yet".
- Restart persistence: after fully quitting and relaunching, the app resumed the signed-in user with no password; settings persisted.
- Backend URL change clears the sign-in; restoring it and logging in again works. Logout clears state and deletes `refresh-token.bin`.
- **Backend unavailable:** with the API stopped, every IPC call returns in a few ms with the generic message "Could not reach the backend…" (no stack, host or internals); the stream emits the same safe `network_error`; header shows "Backend unavailable"; the user stays signed in. After the API returns, the indicator recovered by itself (15 s poll) and calls succeeded without restart.
- Copy: real clipboard verified (`desktop-copy-check-123`).
- Window (verified against real Win32 state): always-on-top on/off, opacity (0.96→alpha 244, 0.7→alpha 178), compact 390×520 ↔ expanded 760×740, minimum size enforced (requested 120×120 → 360×460), close-to-tray hides the window while all processes stay alive.

### Manual QA checklist (`docs/DESKTOP_MANUAL_CHECKLIST.md`)

| Item | Result |
|---|---|
| Visible window opens; packaged build no blank screen | Pass (packaged) |
| Tray icon visible | **Not verified** (no way to see the notification area from here) |
| Connection indicator; API down/up recovery; URL change clears sign-in | Pass |
| Sign up / sign in / invalid credentials safe error | Pass |
| Restart resumes saved session; sign-out returns to login | Pass |
| Select session shows company/title/resume/readiness; no-session guidance | Pass |
| Resize + minimum size; expand/collapse (persist across restart); opacity ≥65%; always-on-top both states | Pass (OS-level); expand persistence: settings file round-trips, restart showed `expanded:false` as last set |
| Minimize / restore / close-to-tray / quit | Close-to-tray pass; restore-from-tray click and tray Quit **not verified**; UI minimize button **not exercised** |
| Typed question streams in order; second question once in history | Pass |
| Stop generation mid-stream | Partial: cancel issued immediately after start (the fake provider finishes in ms); history reflects backend state, no duplicate |
| Backend/provider error shows safe message | Pass for network and 404; a *provider* failure was **not** exercised |
| Copy answer | Pass |
| Switch sessions, only that history | Pass |
| Typecheck + tests | Pass |
| Renderer exposes no tokens or Node API | Pass (see §3) |
| External navigation/popups/permission prompts denied | `window.open` → null verified; navigation/permission handlers confirmed in code, prompts not triggered |
| No audio/screenshot/hidden-window/evasion | Pass (code search + running behaviour) |

## 3. Security findings

Verified live on the packaged app:
- **No Node in the renderer:** `require/process/module/Buffer` undefined; no `ipcRenderer`/`electron` global.
- **Bridge allow-list:** exactly 15 named functions (`app.*` 7, `auth.*` 4, `sessions.*` 4) + 2 subscriptions.
- **Renderer network:** `fetch` to the backend and to `https://example.com` both blocked (CSP `connect-src 'none'`).
- **No credentials in the renderer:** no JWT/bearer/token strings in DOM, localStorage, sessionStorage, cookies, stream messages, or IPC payloads; `DesktopUser` has only `id,email,created_at`.
- **At rest:** `refresh-token.bin` is a 95-byte `v10` safeStorage-encrypted blob; no file in the app-data directory contains a JWT; `settings.json` has only `alwaysOnTop, backendBaseUrl, expanded, opacity`.
- **Input validation at the IPC boundary:** non-loopback `http://` URL, unknown setting key, wrong types and non-UUID ids are all rejected with safe messages.
- **Logging:** `apps/desktop/src/main` contains no `console.*` calls, so tokens cannot be logged there.
- Context isolation, sandbox, `nodeIntegration: false`, `webviewTag: false`, navigation/`window.open`/permission handlers, and per-handler sender-URL validation were confirmed in code (`index.ts`, `ipc.ts`, `ipc-security.ts`).

**CSP `'unsafe-inline'` analysis.** The app has no `style=` markup and the build emits one CSS file; the only inline style attribute is framer-motion's `opacity/transform`, which it writes through the CSSOM, and CSSOM writes are not governed by `style-src`. I built a throwaway variant with `style-src 'self'` (no `unsafe-inline`) and drove every screen (login, sessions, assistant, history, settings): **0 CSP violations, 0 exceptions, page fully styled (168 CSS rules)**. A positive control confirmed the detector: an injected `<style>` was blocked under the strict policy and allowed under the current one. **Recommendation: change to `style-src 'self'`** (one-line change in `src/renderer/index.html`). I did not apply it because you asked for a recommendation; it needs a re-run of the UI smoke after the change. If a future dependency needs inline `<style>`, use a nonce, not `unsafe-inline`.

Lower-severity findings (none exploitable from the normal UI):
1. `updateSettings({opacity: 0.5})` (out of range) returns the misleading "Desktop settings file is invalid."; `copyText` with a non-string returns "The text is too large to copy." The normal UI cannot produce either.
2. `GET /metrics` on the API is unauthenticated (already documented); not a desktop issue.

## 4. Defects found and fixed

- **No Electron source defects were found, and no desktop code was changed.**
- Doc fix made: `DESKTOP_WINDOWS.md` now warns about the `ELECTRON_RUN_AS_NODE` pitfall below.

Environment/tooling findings:
- **`ELECTRON_RUN_AS_NODE=1` is set in this VS Code–hosted shell.** With it, any Electron binary behaves like plain Node ("bad option: --remote-debugging-port") and the app exits immediately. It is not an app bug, but anyone running `npm run dev` from a VS Code-spawned terminal may hit it; unset it first.
- In this environment `npm ci` did not download the Electron runtime (`node_modules/electron/path.txt` absent); I ran `node node_modules/electron/install.js` once. I did not determine why the install script was skipped.

## 5. Unexplained observation

The app window was found **minimized (off-screen)** several times during testing, always in runs where always-on-top was **off**. I could not reproduce it from isolated settings calls (all four patch types left it un-minimized), the app has no blur/minimize handler (only the explicit `app:minimize` IPC, which my probes never called), and with always-on-top on it was never seen. I cannot attribute it to the app or to my harness. **Please look for this during hands-on testing with always-on-top off.**

## 6. Could not be tested, and why

- NSIS installer install/uninstall (changes system state; not done without your approval).
- Tray icon appearance, tray menu, restore-from-tray click, tray Quit (no UI automation of the notification area).
- Mid-stream cancel against a slow model (fake provider answers in ms); provider-error display (no failing provider configured).
- A real OpenAI/Ollama provider, Hindi/Telugu rendering, high-DPI/multi-monitor, other OSes, signed builds, auto-update.
- The DevTools-window experience (the CDP port was used instead; equivalent for exposure checks).

## 7. Remaining work for Phase 10B

1. **Code-signing decision** (certificate source/cost, EV vs OV, SmartScreen reputation). Current builds are unsigned and flagged as dev-only.
2. **Auto-update strategy** (electron-updater vs manual; update channel host; signature verification; rollback). Nothing exists yet.
3. **CSP:** adopt `style-src 'self'` (verified above); consider `Trusted Types`, `form-action 'none'`, and an explicit `frame-ancestors`.
4. **Hardening:** consider Electron fuses (disable `ELECTRON_RUN_AS_NODE`, node CLI inspect arguments, cookie encryption) so release builds cannot be driven the way this validation drove them; fix the two misleading error messages; set an app icon; resolve or accept the 6 moderate dev-dependency advisories.
5. **Finish manual QA:** installer install/uninstall, tray flows, window behaviour with always-on-top off, provider-error and slow-stream cancel, scaling/multi-monitor.
6. **Decide** whether the desktop app should eventually share the WebSocket path (Phase 9) instead of SSE; audio/screenshots stay out of scope until separately approved.
7. Add an automated Windows launch smoke to CI (the CDP approach used here needs no new dependency) if you want regression protection.

## 8. Repository state

- HEAD `94f1a0e` (checkpoint merge commit); safety tag `safety/pre-dheeraj-merge-20261109` kept. Not pushed.
- Uncommitted: this report, the `DESKTOP_WINDOWS.md` note and the `PHASES.md` status update.
- Cleanup done: scratch databases dropped, API/worker/Electron stopped, temp app-data removed, Docker containers stopped. `apps/desktop/release/` (git-ignored, ~120 MB build output) was left in place for your own testing.
