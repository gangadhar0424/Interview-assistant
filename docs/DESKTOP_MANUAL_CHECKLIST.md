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
- [ ] Press **Create session** with everything empty: the three messages appear (company, description, resume) and nothing is sent.
- [ ] Fill company, description, choose a ready resume (not-ready ones are greyed with their status), optionally documents and a customised style; **Create session**: you land on the Assistant page with the new session selected, and it is in the Sessions list.
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

