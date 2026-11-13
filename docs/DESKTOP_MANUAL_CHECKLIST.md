# Desktop MVP Manual QA Checklist

Use a non-production account and synthetic interview data. Start the API and prepare one ready session in the web app first.

## Startup and connection

- [ ] `npm run dev` opens the visible desktop window and tray icon.
- [ ] Packaged build opens the renderer without a blank screen or startup error.
- [ ] Connection indicator changes from checking to connected when the API is available.
- [ ] Stop the API or enter an invalid URL; the app shows unavailable state and recovers after the API returns.
- [ ] Update backend URL in Settings; the app clears the old sign-in and checks the new URL.

## Authentication and session selection

- [ ] Sign up or sign in with a valid test account; invalid credentials show a safe error.
- [ ] Restart the app and confirm a saved session resumes without re-entering the password.
- [ ] Sign out and confirm the account is returned to the login screen.
- [ ] Select a session and confirm company/title, resume, and readiness state are displayed.
- [ ] A user with no sessions sees the setup guidance and no fabricated session data.

## Window lifecycle and settings

- [ ] Resize the window and verify its minimum size.
- [ ] Expand/collapse the window and verify the preference survives restart.
- [ ] Adjust opacity; the window remains clearly visible (minimum 65%).
- [ ] Toggle always-on-top and verify both states.
- [ ] Minimize (title-bar "−" goes to the taskbar, not the tray), restore, close-to-tray ("×", hides to the tray), show from tray, and explicitly quit. With always-on-top **off**, note whether the window is ever found minimized and what preceded it (open observation from Phase 10A, not reproduced in 10B).
- [ ] Confirm closing the window leaves a visible tray affordance and does not silently terminate.

## Typed question, streaming, cancellation and history

- [ ] Submit a question to a ready session; confirm answer tokens appear incrementally and in order.
- [ ] Submit a second question after completion; confirm each answer appears once in history.
- [ ] Stop generation mid-stream; confirm the partial result is not duplicated and history reflects backend state.
- [ ] Exercise a backend/provider error and confirm the UI shows a safe message, not raw provider details.
- [ ] Copy an answer and paste it into a local text field.
- [ ] Switch sessions and confirm only that selected session's history is displayed.

## Security checks

- [ ] Run `npm run typecheck` and `npm test`; confirm IPC-origin, token-storage, SSE-ordering, cancellation, and window-control tests pass.
- [ ] Confirm renderer DevTools do not expose access/refresh tokens or a Node API.
- [ ] Confirm external navigation/popups and browser permission prompts are denied.
- [ ] Confirm there is no audio capture, hidden window, or capture-evasion behavior, and that a screenshot is never taken unless you turned the setting on and pressed Capture.

## Session creation and refresh (Phase 11.5)

- [ ] Sessions page: **New session** opens the form (also from the empty state). Company, optional job title, job description, resumes (one), documents, and an "Answer style" line showing the defaults (English · Auto · Full script · Balanced · Simple) with **Customize**.
- [ ] Press **Create session** with everything empty: the messages appear (company, description, resume, and the two AI model choices) and nothing is sent.
- [ ] The form shows **Reasoning model** and **Voice listening model** dropdowns listing only the providers this server has enabled (D-57); with several offered, neither is preselected; with exactly one it is preselected; with none it says "None available on this server". Neither shows a key or endpoint.
- [ ] Fill company, description, choose a ready resume (not-ready ones are greyed with their status), **both AI models**, optionally documents and a customised style; **Create session**: you land on the Assistant page with the new session selected, and it is in the Sessions list.
- [ ] Ask a typed question in the new session: it streams to Complete.
- [ ] Cancel (or sign out) with text typed in the form: reopening the form shows it empty.
- [ ] An account with **no ready resume**: the form shows "You need a ready resume first", the web link with **Copy link** (paste it somewhere to check), **Refresh** and **Back**.
- [ ] Stop the API, press the ↻ refresh arrow: the red "Could not reach the backend" banner and "Backend unavailable" appear. Start the API, press ↻ once: the banner disappears and the header says "Backend connected" immediately (no waiting, no clicking the header).
- [ ] With the API stopped, **Create session** shows a clear error and keeps what you typed.
- [ ] Existing session selection, typed Q&A and (if enabled) the screenshot flow still work.

## Screenshot input (Phase 11B; needs a backend with `VISION_ENABLED_PROVIDERS=fake`, `APP_ENV=local`)

- [ ] Settings: "Allow capturing a screenshot" is **off** on a fresh install; the Assistant page shows no Capture button.
- [ ] Turning it on shows a **native** confirmation dialog; Cancel leaves it off; Turn on enables it and the Capture button appears.
- [ ] Capture screen… lists your displays by name and size only (no thumbnails), then Start capture shows the 3-second countdown; the tray tooltip changes and the taskbar button flashes at capture.
- [ ] Cancel during the countdown: nothing is captured.
- [ ] With the window hidden in the tray or minimised, a capture cannot be started; hiding or minimising during the countdown cancels it.
- [ ] After capture: a preview with "Held in memory only — not sent"; waiting a minute, Discard, switching session, signing out or hiding the window discards it.
- [ ] Send for text extraction → "Screenshot discarded. Review the extracted text" with editable text (fake provider: `Fake extraction: …`); nothing is answered until you press **Ask**; the answer then streams normally and the history entry works as usual.
- [ ] With two displays: pick the second one; the preview shows that display. At 125%/150% scaling the preview looks right.
- [ ] Disable the setting while a preview is held: it disappears. Stop the backend / disable vision: a clear error and no leftover image.
- [ ] Typed questions behave exactly as before.


## Phase 12 — Live audio (manual, real Windows PC)
1. Fresh profile: audio is OFF; no mic prompt/indicator at launch (`audioInputEnabled` false).
2. Turn on audio: native consent dialog appears; Cancel keeps it off; Turn on enables the panel.
3. Start (microphone): tray tooltip says RECORDING, in-app indicator visible; speak a question — transcript line appears, "Answer this question?" confirm shows; confirming streams an answer (history shows input source audio).
4. Stop: indicator clears, Windows mic-in-use icon disappears within seconds.
5. Hide/minimize window, sign out, switch session, disable audio during listening: capture stops each time.
6. Denied mic permission in Windows privacy settings: clear error, nothing stuck "listening".
7. System audio source: play a video with speech; transcript matches; no screen picker/video capture; Stop works.
8. Confirm nothing is written to disk (no wav/transcript files in userData) and logs contain no transcript text.
9. Auto-answer toggle is OFF by default; when ON, only detected questions answer.

## Web → desktop handoff (manual, real Windows PC; packaged installer only; D-55 + D-56)

**What counts as a pass.** A scenario is passed only when a person performed it with the **packaged NSIS installer** (`npm run package:win`), a real browser and the real desktop app, and wrote the result in the table at the end of this section. Automated tests, environment smoke checks and `npm run dev` runs do **not** count. Until the table is filled in, Milestone 2 is "verified automatically, manual verification pending".

**D-56 in one paragraph.** The user signs in **only on the web**. Pressing **Start Interview** opens the desktop app, which redeems the 60-second single-use code **by itself** and adopts a desktop login for the same user: there is **no desktop sign-in step** and no password prompt. A desktop already signed in as the same user keeps its login; one signed in as a different account asks first (native "Switch account?"); a running capture asks first (native "Switch to a new prepared session?"). READY never starts audio or screenshots.

**Prerequisites**
1. Docker services up (`docker compose up -d --wait`), migrations at head (`uv run alembic upgrade head`), the **API restarted after the D-56 code was pulled** (the old API still requires a desktop login), the Dramatiq worker running, and the web app running (`npm run dev` in `apps/web`, http://localhost:3000).
2. An API with `APP_ENV=local` and `STT_ENABLED_PROVIDERS=fake` so the Listen button works in scenario 4 (the fake transcriber returns canned text; real speech-to-text is not part of Milestone 2).
3. The installer built **from the current tree** (`apps/desktop`: `npm run package:win`; output `release/Interview Assistant-0.1.0-Windows-x64.exe`, unsigned: SmartScreen shows "More info" then "Run anyway"). Uninstall any older build first, because older builds still contain the pre-D-56 sign-in rule.
4. The desktop backend URL (desktop **Settings**) is `http://127.0.0.1:8000` (the default).
5. Two web accounts (A and B), each with a Ready resume and at least one `ready` session (A needs two: A1 and A2). Browser: Chrome or Edge; allow the "Open Interview Assistant?" prompt when it appears, and optionally tick "always allow".

**READY means:** a READY banner naming the session; the tray tooltip does **not** say RECORDING; no Windows microphone icon; **Settings** still shows audio and screenshot input off. (The desktop window is see-through by design, so other windows may show behind it.)

**Where things are in the desktop app:** *Settings → Audio input* (the switch that asks for the native consent), *Settings → Sign out*, and *Assistant → Audio input → Listen: microphone*.

| # | Scenario | Steps | You must observe |
|---|---|---|---|
| 1 | Cold start, never signed in on the desktop | Fresh install, or Settings → Sign out then quit from the tray menu. On the web (signed in as A) open session A1 and press **Start Interview**; allow the browser prompt. | The app starts and shows **READY for A1** without any login form (the login screen may appear for a moment with "Opening your prepared session…" but never asks for input). Quit and restart the app from the Start menu: it is still signed in as A. |
| 2 | Cold start, already signed in as the same user | Quit from the tray. Press Start Interview for A1. | READY for A1, no password, no account dialog. |
| 3 | App already running | With the app running, press Start Interview in each state: window visible; hidden to tray (×); minimized. | The same window is restored and focused. One window, one tray icon. READY shows. |
| 4 | Capture-active switching | Settings → Audio input: turn on and accept the native consent. On Assistant press **Listen: microphone** (tray tooltip says RECORDING). On the web press Start Interview for **A2**. | A native **"Switch to a new prepared session?"** dialog. **Keep current capture**: recording continues and the declined notice shows. Repeat and choose **Switch session**: recording stops (tooltip back to normal, no microphone icon), READY shows A2, nothing starts recording. |
| 5 | Not-installed fallback | Uninstall the app. Press Start Interview. | After about 3 seconds the page shows "The desktop app did not open" with a download link (or "ask your administrator" if no installer URL is configured). No code and no `interviewassistant://` text anywhere in the page. |
| 6 | Retry with a fresh code | Install the app. Press Start Interview again (DevTools → Network open, preserve log). | READY. Two separate `POST …/handoff` calls show **different** codes in their responses. Opening the first link again shows the "invalid, expired or already used" notice. |
| 7 | Expired link (optional; also covered by automated tests) | Press Start Interview and keep the browser's "Open app?" prompt unanswered for over 60 seconds, then allow it. | The app shows the generic "invalid, expired or already used" notice; nobody is signed in because of it. |
| 8 | Different account on the desktop | Desktop signed in as **B** (Settings → Sign out, then sign in as B). On the web, signed in as A, press Start Interview. | A native **"Switch account?"** dialog (no account details). **Keep current account**: still B, declined notice. Repeat and choose **Switch account**: the desktop is now A and shows READY for A's session; B's sessions are no longer listed. |
| 9 | Sign-out clears the handoff | With READY showing, Settings → Sign out. | The READY banner disappears and the login screen shows. A later Start Interview signs in again with no password. |
| 10 | Web and desktop sessions are independent | After a handoff, sign out on the **web**. Then (after another handoff) sign out on the **desktop**. | Signing out of the web leaves the desktop signed in; signing out of the desktop leaves the web signed in. |

Development mode only (`npm run dev` in `apps/desktop`, not a supported path): registration uses the electron binary plus the app path; typing `interviewassistant://start?h=<43 characters>` into Win+R should open the dev app, and anything malformed (short code, extra parameter) is ignored with the "invalid" notice.

### Results (fill in; only a person using the packaged installer may mark a row)
| # | Result (Pass / Fail / Not performed) | Date | Browser | Installer build | Notes |
|---|---|---|---|---|---|
| 1 | Not performed | | | | |
| 2 | Not performed | | | | |
| 3 | Not performed | | | | |
| 4 | Not performed | | | | |
| 5 | Not performed | | | | |
| 6 | Not performed | | | | |
| 7 | Not performed | | | | |
| 8 | Not performed | | | | |
| 9 | Not performed | | | | |
| 10 | Not performed | | | | |

Milestone 2 may be marked complete only when rows 1 to 6 and 8 to 10 are **Pass** (row 7 is optional).
