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
- [ ] Confirm there is no audio capture, screenshot capture, hidden window, or capture-evasion behavior.

