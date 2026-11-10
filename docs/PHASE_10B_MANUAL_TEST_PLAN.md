# Phase 10B — Owner Manual Test Plan (packaged Windows app)

**Executed by the owner on 2026-11-10.** Results: install, first launch, tray icon/menu, minimize/restore from tray, tray Quit, display scaling, uninstall/reinstall **PASS**; no unexpected minimization with always-on-top off; **multi-monitor NOT TESTED** (deferred); **mid-stream cancel NOT TESTED** (no slow provider; deferred). Summary in [PHASE_10_CLOSURE_REPORT.md](./PHASE_10_CLOSURE_REPORT.md).

Covers **only** what automation could not verify (see [PHASE_10B_REPORT.md](./PHASE_10B_REPORT.md) §8). The rest of the app was verified against a real local backend. Use a **non-production account and synthetic data**. The fake LLM provider is sufficient; **no paid provider is required for Phase 10 acceptance**.

Record for each step: **Pass / Fail / Not tested**, plus notes and screenshots for failures. Do not mark anything Pass that you did not observe.

## 0. Preparation

1. Build the installer (unsigned): `cd apps/desktop`, `$env:CSC_IDENTITY_AUTO_DISCOVERY="false"`, `npm run package:win`. Installer: `apps/desktop/release/Interview Assistant-0.1.0-Windows-x64.exe`.
2. Prefer a clean Windows VM or a spare Windows user account. If using your own account, the app data folder (about `%APPDATA%\interview-assistant-desktop`) may already hold dev settings; rename it first to test a true first run.
3. Start the backend with the fake provider: `docker compose up -d --wait`; in `services/api` set `LLM_ENABLED_PROVIDERS=fake` and `LLM_FALLBACK_CHAIN=fake` (APP_ENV=local), run migrations, start `uvicorn app.main:get_app --factory --port 8000` and the Dramatiq worker (see README). Create a user, upload a resume, create a session (web app) and wait until it shows Ready.
4. Make sure `ELECTRON_RUN_AS_NODE` is **not** set in any terminal you use to launch anything.
5. Expect Windows SmartScreen / "unknown publisher" warnings: the build is unsigned (expected, not a failure).

## 1. NSIS installation

- [ ] Run the installer. It is not one-click; it offers per-user install and lets you choose the folder.
- [ ] Complete the install.
- [ ] Start Menu entry **Interview Assistant** exists with the new icon (cyan rounded square with three bars).
- [ ] Desktop shortcut exists with the same icon.
- [ ] **Settings → Apps → Installed apps** lists Interview Assistant 0.1.0.
- [ ] The install folder contains `Interview Assistant.exe` and `resources\app.asar`.

## 2. Launch after installation

- [ ] Launch from the Start Menu: a visible window appears (no blank/black screen, no "could not start" dialog).
- [ ] Header shows **Backend connected** (API running). Taskbar shows the correct icon.
- [ ] Sign in, select the Ready session, ask a question: the answer streams to **Complete** and appears under History.
- [ ] Quit completely (step 5) and launch again from the **Desktop shortcut**: you are still signed in without re-entering the password.
- [ ] Stop the API: the header changes to **Backend unavailable** with no technical details; start it again: it recovers by itself (up to ~15 s).

## 3. Tray icon and menu

- [ ] A tray icon is visible (check the hidden-icons chevron if not); tooltip reads "Interview Assistant — visible desktop assistant".
- [ ] Right-click: the menu shows **Show Assistant** and **Quit Interview Assistant**.
- [ ] Double-clicking the tray icon brings the window to the front.

## 4. Minimize and tray restore

Run this once with always-on-top **on** and once **off** (Settings).

- [ ] Title-bar **−** (tooltip "Minimize"): the window goes to the **taskbar** (not the tray). Clicking the taskbar button restores it.
- [ ] Minimize with **−**, then tray **Show Assistant**: the window is restored and visible (not left minimized).
- [ ] Title-bar **×** (tooltip "Close to tray"): the window disappears, the tray icon stays, and `Interview Assistant` is still in Task Manager.
- [ ] Tray **Show Assistant** and tray double-click both bring it back with your sign-in and screen intact.
- [ ] **Open observation from 10A:** with always-on-top off, use the app normally for a while (switch to other apps, click away). Is the window ever found minimized? If so, note exactly what you did just before. It was not reproduced in the controlled test.
- [ ] The opacity slider cannot go below 65% and the window stays clearly visible; expand/collapse works and survives a restart.

## 5. Tray Quit

- [ ] Tray right-click → **Quit Interview Assistant**: the window and tray icon disappear and **no** `Interview Assistant` processes remain in Task Manager (allow a few seconds).
- [ ] Launch again: it starts normally.
- [ ] Closing with **×** never quits the app by itself (only tray Quit does).

## 6. High-DPI / display scaling

For each of 100%, 125%, 150% (and 200% if possible) in **Settings → System → Display → Scale** (sign out/in if Windows asks):

- [ ] Text and icons are sharp, not blurry; no clipped buttons or overlapping text on Login, Sessions, Assistant, History and Settings.
- [ ] Compact (about 390×520) and expanded (about 760×740) sizes both fit on screen; the window can still be resized and keeps its minimum size.
- [ ] The tray icon and taskbar icon look acceptable.

## 7. Multi-monitor (only if a second display is available)

- [ ] Drag the window (by the title bar) to the second monitor and back; it stays always-on-top if that is enabled.
- [ ] Expand/collapse on the second monitor: the window stays on that monitor and visible.
- [ ] Monitors with different scaling: the window re-renders crisply after moving.
- [ ] Unplug or disable the second monitor while the window is on it: the window is still reachable (on the remaining display, or via tray Show Assistant).
- [ ] Restart after leaving the window on a monitor that is no longer connected: it still opens somewhere visible. (Window position is not saved by the app, so it should open on the default monitor.)

## 8. Mid-stream cancellation (conditional)

The built-in fake provider answers in milliseconds, and its only delay (`first_token_delay`) is a code-level constructor setting, **not** a configuration option, so there is no safe slow test provider today without changing code. Choose one:

- **Skip and record "Not tested — needs slow provider"** (acceptable for Phase 10 acceptance), or
- if you already run a local model (for example Ollama through the OpenAI-compatible adapter) and are comfortable using **synthetic data only**, ask a long question and press **Stop** while tokens are arriving. Check that streaming stops promptly, the partial text is kept, History shows one entry marked cancelled (no duplicate), and you can immediately ask another question.

Real-provider integration is a separate future validation item (including provider data-handling terms, OQ-4) and is not needed for Phase 10 acceptance.

## 9. Uninstall

- [ ] **Settings → Apps → Installed apps → Interview Assistant → Uninstall** (quit the app first, or let the uninstaller close it).
- [ ] Start Menu and Desktop shortcuts are removed, the install folder is gone, and the app disappears from Installed apps.
- [ ] Note whether `%APPDATA%\interview-assistant-desktop` still exists. Expected: **it remains** (settings and the DPAPI-encrypted refresh token). This is a documented known item, not a failure; to remove it, sign out before uninstalling or delete the folder.
- [ ] Reinstall: the app starts normally (and if the data folder remained, you are still signed in).

## 10. Report back

Send the Pass / Fail / Not-tested list with notes. For anything that fails or surprises you (especially a minimized window, a blank window, or a tray problem), include the steps you took. Then we decide whether to accept and close Phase 10.

## Explicitly deferred (not part of this acceptance)

- **Code signing** and **auto-update**: release/deployment decisions for when a production release is prepared (D-50, [DESKTOP_RELEASE.md](./DESKTOP_RELEASE.md)). Unsigned warnings in this test are expected.
- Real paid or hosted LLM provider validation; WebSocket client; audio and screenshots.
