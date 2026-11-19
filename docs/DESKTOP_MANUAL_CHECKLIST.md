# Desktop MVP Manual QA Checklist

Use a non-production account and synthetic interview data. Start the API and prepare one ready session in the web app first.

## Startup and connection

- [ ] `npm run dev` opens the visible desktop window and tray icon.
- [ ] Packaged build opens the renderer without a blank screen or startup error.
- [ ] Connection indicator changes from checking to connected when the API is available.
- [ ] Stop the API or enter an invalid URL; the app shows unavailable state and recovers after the API returns.
- [ ] Update backend URL in Settings; the app clears the old sign-in and checks the new URL.

## Authentication and session selection

- [ ] Open the app with nobody signed in: it shows "Start from the web app" and has **no** email/password fields and no Sign in / Create account buttons (D-65). Sign in on the web, open a Ready session and press **Start Interview**: the desktop opens signed in.
- [ ] Restart the app and confirm a saved session resumes without re-entering the password.
- [ ] Sign out and confirm the app returns to the "Start from the web app" screen.
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


## Phase 12 — Live audio (manual, real Windows PC; flow amended by D-59)
1. Fresh profile: audio is OFF; no mic prompt/indicator at launch (`audioInputEnabled` false).
2. The Listen buttons are visible on the Assistant page. The first press of Listen shows the native consent dialog (once); Cancel starts nothing and keeps audio off; Turn on starts listening. Later presses, and later app launches, do not ask again (Settings → Audio input can turn it off).
3. Start (microphone): tray tooltip says RECORDING, in-app indicator visible; speak a question — the transcript line appears and the answer starts streaming at once without pressing Ask (history shows input source audio). A line that is not a question, or one heard while an answer is streaming, stays with an Ask button.
4. Stop: indicator clears, Windows mic-in-use icon disappears within seconds.
5. Hide/minimize window, sign out, switch session, disable audio during listening: capture stops each time.
6. Denied mic permission in Windows privacy settings: clear error, nothing stuck "listening".
7. System audio source: play a video with speech; transcript matches; no screen picker/video capture; Stop works.
8. Confirm nothing is written to disk (no wav/transcript files in userData) and logs contain no transcript text.
9. Only detected questions are answered automatically; other lines wait for Ask.

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
| 1 | Cold start, never signed in on the desktop | Fresh install, or Settings → Sign out then quit from the tray menu. On the web (signed in as A) open session A1 and press **Start Interview**; allow the browser prompt. | The app starts and shows **READY for A1** without any login form (the "Start from the web app" screen may appear for a moment with "Opening your prepared session…" but never asks for input). Quit and restart the app from the Start menu: it is still signed in as A. |
| 2 | Cold start, already signed in as the same user | Quit from the tray. Press Start Interview for A1. | READY for A1, no password, no account dialog. |
| 3 | App already running | With the app running, press Start Interview in each state: window visible; hidden to tray (×); minimized. | The same window is restored and focused. One window, one tray icon. READY shows. |
| 4 | Capture-active switching | Settings → Audio input: turn on and accept the native consent. On Assistant press **Listen: microphone** (tray tooltip says RECORDING). On the web press Start Interview for **A2**. | A native **"Switch to a new prepared session?"** dialog. **Keep current capture**: recording continues and the declined notice shows. Repeat and choose **Switch session**: recording stops (tooltip back to normal, no microphone icon), READY shows A2, nothing starts recording. |
| 5 | Not-installed fallback | Uninstall the app. Press Start Interview. | After about 3 seconds the page shows "The desktop app did not open" with a download link (or "ask your administrator" if no installer URL is configured). No code and no `interviewassistant://` text anywhere in the page. |
| 6 | Retry with a fresh code | Install the app. Press Start Interview again (DevTools → Network open, preserve log). | READY. Two separate `POST …/handoff` calls show **different** codes in their responses. Opening the first link again shows the "invalid, expired or already used" notice. |
| 7 | Expired link (optional; also covered by automated tests) | Press Start Interview and keep the browser's "Open app?" prompt unanswered for over 60 seconds, then allow it. | The app shows the generic "invalid, expired or already used" notice; nobody is signed in because of it. |
| 8 | Different account on the desktop | Desktop signed in as **B** (Settings → Sign out, then on the web sign in as B and press Start Interview). On the web, signed in as A, press Start Interview. | A native **"Switch account?"** dialog (no account details). **Keep current account**: still B, declined notice. Repeat and choose **Switch account**: the desktop is now A and shows READY for A's session; B's sessions are no longer listed. |
| 9 | Sign-out clears the handoff | With READY showing, Settings → Sign out. | The READY banner disappears and the "Start from the web app" screen shows. A later Start Interview signs in again with no password. |
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

## Account and reinstall (D-59, packaged installer)

- [ ] Sidebar **Account** (and the account chip) opens a page with the **full** email, session count and Sign out.
- [ ] Signed in as account A: uninstall the app from Windows Settings, reinstall, open it directly: it shows the "Start from the web app" screen, **not** account A's sessions.
- [ ] Signed in on the web as B, press **Start Interview**: the desktop shows B's session (with the native "Switch account?" prompt if it was signed in as A).

## Desktop presence check (D-60)

- [ ] Desktop app **not installed/running**: web **Start Interview** shows "desktop app was not detected" with the installer link and **no** Windows "Get an app" dialog.
- [ ] Same page, **open it anyway** with the app missing: Windows may show its dialog (expected).
- [ ] Desktop app **running**: Start Interview opens/focuses it on the session with no "not detected" message.
- [ ] Quit the dev build, then check `HKCU:\Software\Classes\interviewassistant` is gone (dev runs only).

## Stale installer copies (local web download link)

`NEXT_PUBLIC_DESKTOP_DOWNLOAD_URL=http://localhost:3000/downloads/InterviewAssistant-Setup.exe` serves the git-ignored file `apps/web/public/downloads/InterviewAssistant-Setup.exe`. It is a **copy**, not a build output: after every `npm run package:win` copy the new `apps/desktop/release/Interview Assistant-0.1.0-Windows-x64.exe` over it, or the web "Download the Windows installer" link (and any copy in Downloads) keeps installing the old build. Compare `sha256sum` of both files to confirm.

## Voice provider on an older session (D-62)

- [ ] On a session created before D-57 (no voice provider), press **Listen: microphone**: there is **no** "RECORDING" banner, the app says a voice provider is needed and shows a provider picker.
- [ ] Pick a provider and **Save**: the picker disappears and it says to press Listen; pressing Listen then records and transcribes.

## Provider failure and spoken questions (D-63)

- [ ] Ask a spoken question on a session whose answer provider has no credit: the page scrolls to the answer card, which says the provider has no credit or quota, and an **Answer provider** picker appears.
- [ ] Pick a working provider and **Save**, then ask again: the answer streams and completes.


## Multi-monitor and window placement (M-6)

The full checklist (laptop only, two external displays, mixed scaling, disconnect and reconnect, restart, compact and expanded, moving between displays, sleep, docking and remote desktop) is in [MULTI_MONITOR_TESTING.md](./MULTI_MONITOR_TESTING.md). Results: **not yet recorded** (only a single-display laptop at 125% was exercised, by automation).

## Coding answers (M-8, packaged installer)

Use a session with a ready status. Ask each of these typed, then repeat at the compact and the expanded window size. A person must mark each row; none is marked yet.

| # | Check | Pass |
|---|---|---|
| 1 | Ask "Write a Python function two_sum(nums, target) that returns the indices of two numbers that add up to target." The answer shows headings, then a code block labelled **Python** with a Copy button | |
| 2 | **Copy** on the code block, paste into a text editor: only the code, indented correctly, no backticks and no label | |
| 3 | A long solution (for example "implement an LRU cache in Python with tests"): the block scrolls inside itself, vertically and horizontally, and the page does not grow past the window | |
| 4 | Tab to a code block: it can be scrolled with the keyboard | |
| 5 | Ask a SQL question and a Java question: labels read **SQL** and **Java** | |
| 6 | While the answer is streaming, an open code block shows "writing..." and the text so far; nothing flickers into raw backticks | |
| 7 | **Copy answer** under the card copies the whole answer; History shows the same answer with the same code blocks | |
| 8 | Ask "Write a function that finds the duplicates in a list." The answer states its assumptions instead of inventing requirements | |
| 9 | Capture a screenshot of a coding problem with visible constraints and examples and answer it: the question text sent keeps the constraints and examples | |
| 10 | Nothing in any answer says the code was run or tested | |

## Session management (M-9, packaged installer)

Use a signed-in account with at least two sessions, one with a few answered questions. A person must mark each row; none is marked yet.

| # | Check | Pass |
|---|---|---|
| 1 | The Sessions page shows each session's name, company, state (Active, Completed or Interrupted), question count and last activity; the open session says **Current** | |
| 2 | **Rename** a session, press Enter: the new name shows in the list, the Ask page selector and History; Escape cancels | |
| 3 | Open session A, ask a question, switch to B: B shows only B's history and none of A's answer; switching back shows A's | |
| 4 | Switch sessions while an answer is streaming: the old answer stops and does not appear under the new session | |
| 5 | Press Listen, then switch session: listening stops and Listen has to be pressed again | |
| 6 | **Complete** the open session: listening stops, the state becomes Completed, Ask is disabled; **Reopen** returns it to Active and starts nothing | |
| 7 | **Delete** shows a native confirmation; Cancel changes nothing; Delete removes the session, and the next session opens without recording | |
| 8 | **Export** shows the native warning, then a Save dialog; cancelling at either step writes nothing; saving writes a readable `.md` (and **JSON** a `.json`) containing the questions and answers and no resume text, tokens or e-mail | |
| 9 | Disconnect the network and try Rename, Complete, Delete and Export: each says the session was not changed or the export failed, and nothing is written; reconnect and retry works | |
| 10 | Delete a session in the web app, then Complete it here without refreshing: "This session is no longer available" and the list refreshes | |
| 11 | Close and reopen the app: the most recently used ready session is selected and nothing is recording | |
| 12 | Create a session, drop the network just as you press Create, retry: only one session exists | |
