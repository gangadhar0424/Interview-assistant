# Desktop troubleshooting: window crashes and diagnostics

Status: **implemented in M-5 (D-70).** Automated tests use mocked Electron. One real Windows crash-and-recover run was done (see "What has been verified"); the other manual checks below are NOT yet done.

Written for people who use the Interview Assistant desktop app, plus the checklist for whoever tests it on Windows.

## If the window stops working

The assistant window is a small web page running in its own process. If that process crashes, the app does this by itself:

1. **Stops everything that window was doing.** Listening stops, any screenshot waiting to be sent is wiped, and any answer still streaming to the window is cancelled. This happens first, before anything else.
2. **Reloads the window once, automatically.** This usually takes under a second.
3. **Tells you.** After a successful reload a notice at the top says the window was reloaded, that you are still signed in, and that **nothing was restarted for you**. You must start listening or take a screenshot again yourself, with the usual buttons, consent, indicators and countdown.

It will not reload again by itself if the window crashes again within a minute, and never more than three automatic reloads in ten minutes. Then you see a message box:

- **Try again** reloads the window (once per click).
- **Copy diagnostics** puts a diagnostics report on your clipboard, then asks again.
- **Close** leaves things as they are. Use **Reload window** from the tray icon's menu any time to try again.

Quitting the app, closing the window to the tray, signing out of Windows or shutting down is never treated as a crash.

## Copying diagnostics for support

Open **Settings, Support, Copy diagnostics** (or use the button in the recovery notice or message box), then paste into your message. The app does not send the report anywhere.

### What the report contains

| Section | Contents |
|---|---|
| Application | app version; Electron, Chrome and Node versions; operating system, release and CPU architecture; seconds since the app started |
| Services | signed in (yes/no); audio state (idle, starting, listening); screenshot capture in progress (yes/no); window visible, focused, minimized; audio input, screenshot input, always-on-top and expanded settings (on/off); backend "local" or "remote"; whether the acceptable-use notice is accepted; the status of each keyboard shortcut |
| Recovery | how many times the window stopped, automatic and manual reloads, failed recoveries |
| Performance | the startup and latency summaries from M-2 (counts and milliseconds only) |
| Recent events | up to the newest 40: startup, window created and ready, window stopped (with Electron's reason and exit code), reload started or succeeded or failed, which button was chosen in the message box, diagnostics copied |

### What it never contains

Questions, transcripts, prompts, generated answers, audio, screenshots, resume or document text, API keys, passwords, tokens or authorization headers, your email address, account or session ids, the backend's address, or file paths. Events can only be picked from a fixed list with fixed fields, so free text cannot be recorded; the finished report is also scrubbed for anything shaped like a key, token, email address or Windows user folder.

### Limits

- **The log is in memory only.** It is lost when the app closes, so copy diagnostics while the app is still running after a problem. (The app may write only its settings and sign-in files to disk, and that rule is enforced by tests.)
- At most 200 events are kept and 16,000 characters are copied; if more would be needed the oldest events are dropped and the report says `[truncated]`.
- Only a window (renderer) crash is recovered. A crash of the whole app, a GPU or helper process crash (recorded as an event only), or a hang that the window never reports are not handled.
- There is no remote crash reporting and no automatic upload.

## For developers

- Code: `main/recovery.ts` (state machine over injected ports), `main/diagnostics.ts` (bounded typed log and report), wiring in `main/index.ts` and `main/ipc.ts`, channels `app:get-recovery` and `app:copy-diagnostics`, UI in `ui/SupportPanel.tsx`.
- The existing cleanup in `ipc.ts` (stop audio, wipe screenshots) also now aborts answer streams; the recovery module calls the same idempotent cleanup before it reloads.
- Tests: `tests/recovery.test.ts`, `diagnostics.test.ts`, `recovery-ipc.test.ts`, `recovery-guards.test.ts`, and the "renderer crash" block in `audio-ipc.test.ts`. They use fake ports, a fake clock and a mocked clipboard, so **they show our logic is right, not how Windows behaves.**

## What has been verified on real Windows

One scripted run on the built app (not the installer), with a throw-away profile: a real renderer crash was triggered through the browser debugging protocol (a test aid, not part of the app). The app recorded `renderer_gone reason=crashed`, started one automatic reload within milliseconds, reported the page healthy about 0.4 s later, showed the recovery notice on screen, and copied a 1,473-character diagnostics report. The notice was consumed once by the app's own startup. The run replaced the clipboard contents with the diagnostics report.

## Windows manual checks still required

Run with the packaged app (`npm run package:win`) and record the results in `docs/DESKTOP_MANUAL_CHECKLIST.md`.

1. **Real crash while listening.** Start listening (microphone), then end the app's renderer process from Task Manager (Details, the `Interview Assistant.exe` child with type renderer). Confirm the recording indicator and tray state clear, the window reloads once, the notice appears, and listening does **not** restart.
2. **Real crash with a screenshot waiting.** Take a screenshot but do not extract it, crash the renderer, and confirm the preview is gone and nothing is sent.
3. **Real crash during an answer.** Crash while an answer streams and confirm no answer text appears afterwards and the server request ends.
4. **Repeated crash.** Crash the renderer, wait for the reload, crash again within a minute. Confirm the message box appears, with no second automatic reload and no reload loop.
5. **Dialog buttons.** In the message box choose Copy diagnostics (paste somewhere and read the report for anything private), Try again, then Close.
6. **Failed reload.** Make the reload fail (for example rename `out/renderer/index.html` in an unpacked build) and confirm the message box appears after the 15-second timeout and the app does not hang or loop.
7. **Tray "Reload window"** reloads the page, and does not show the "stopped working" notice when nothing had failed.
8. **Quit is not a crash.** Quit from the tray menu while listening: no recovery, no message box, no leftover processes. Repeat with Windows sign-out or restart while the app is open.
9. **Hidden window.** Crash the renderer while the app sits in the tray: it reloads quietly and stays hidden.
10. **Still signed in** after recovery, without signing in again.
11. **Keyboard use and screen readers** on the recovery notice and the Support button.
12. **Report check.** After real use, copy diagnostics and confirm it holds no names, email addresses, folders or addresses.
