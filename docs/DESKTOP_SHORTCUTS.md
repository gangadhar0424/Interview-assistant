# Desktop keyboard shortcuts

Status: **implemented in M-4 (D-69); automated tests use mocks, the Windows manual checks below are NOT yet done.**

Written for people who use the Interview Assistant desktop app, plus the checklist for whoever tests it on Windows.

## What they do

Shortcuts work from any application, even when the assistant is hidden in the tray. They only **show, hide, or move to** part of the assistant. **No shortcut starts listening or takes a screenshot.** To start either, you still open the panel and press its button yourself, and everything that protects you stays in force: the acceptable-use notice, the audio or screenshot consent, the visible recording indicator, and the screenshot countdown and preview.

| Action | Default | What happens |
|---|---|---|
| Show or hide the assistant | `Alt+Shift+A` | Shows and focuses the window. If it is already in front, hides it to the tray. |
| Hide the assistant | `Alt+Shift+H` | Hides the window to the tray. Listening and any held screenshot stop when it is hidden, exactly as with the close button. |
| Go to audio input | `Alt+Shift+L` | Shows the window, opens the Assistant page and moves focus to the audio panel. It does **not** start listening. |
| Go to screenshot input | `Alt+Shift+S` | Shows the window, opens the Assistant page and moves focus to the screenshot panel. It does **not** capture. If screenshot input is off, a note tells you to turn it on in Settings; it is never turned on for you. |

Showing the window with a shortcut brings it forward where it already is: it never changes the window's position or screen (M-6). Use Settings, Displays to move it to another screen.

## Changing or turning off shortcuts

Open **Settings, Keyboard shortcuts**.

- **Use keyboard shortcuts** turns every shortcut on or off.
- To change one, select its box and press the new keys. The box shows what you pressed. **Tab** and **Escape** keep their normal meaning, so you can always leave the box.
- **Reset** returns one shortcut to its default; **Reset all** returns all four and turns shortcuts on.
- Press **Save shortcuts**. Each shortcut then shows its status.

Statuses:

| Status | Meaning |
|---|---|
| Active | Registered and working. |
| Shortcuts are turned off | The master switch is off. |
| Another application is already using this shortcut | Windows would not give the combination to this app. Choose another. The other shortcuts still work. |
| Another action already uses this shortcut | Two actions have the same keys. Saving refuses this; it can only appear for an older settings file. |
| Not a valid shortcut | The combination is not allowed (see below). |

### Rules for a shortcut

Every shortcut must be **Shift + Ctrl or Alt + one key**, where the key is a letter, a number, F1 to F12, Space, an arrow key, Home, End, Page Up or Page Down.

Why so strict: a global shortcut takes the keys away from every other program.
- A shortcut without Shift, or with only one modifier, would take ordinary typing and editing keys such as `Ctrl+C`.
- **Ctrl+Alt together is refused**, because on many keyboard layouts Windows treats it as the AltGr key used to type characters, so it would stop you typing them.
- The Windows key is reserved by Windows and is refused.

Settings are saved on your computer (the app's `settings.json`) and survive restarts. The file only gains a `shortcuts` section once you change something from the defaults.

## For developers

- Code: `apps/desktop/src/shared/shortcuts.ts` (definitions, validation), `src/main/shortcuts.ts` (the manager), wiring in `src/main/index.ts`, channels `app:get-shortcuts` and `app:set-shortcuts` plus the main-to-renderer event `shortcut:event`, UI in `ui/ShortcutSettings.tsx`.
- Registration happens once the app is ready; everything is removed at `will-quit`. Re-saving replaces only what this app registered.
- Tests: `tests/shortcuts.test.ts`, `tests/shortcuts-ipc.test.ts`, `tests/shortcut-guards.test.ts`. They use fake registrars and windows, so **they show our logic is right, not how Windows behaves.**

## Windows manual checks still required

Run on a real Windows 10/11 machine with the packaged app (`npm run package:win`) and record the result in `docs/DESKTOP_MANUAL_CHECKLIST.md`.

1. First launch: the four default shortcuts show as Active in Settings.
2. `Alt+Shift+A` from another application brings the window forward and focuses it (Windows can refuse to move focus; note any case where only the taskbar flashes). Press it again: it hides to the tray.
3. With the window hidden in the tray, `Alt+Shift+L` shows it on the Assistant page with the audio panel focused. Confirm **no** microphone indicator, no recording banner and no tray "listening" state appear.
4. Same for `Alt+Shift+S`: nothing is captured and no countdown starts. With screenshot input off, the note appears and the setting stays off.
5. With the acceptable-use notice not yet accepted, `Alt+Shift+L` shows the notice and the Listen buttons stay hidden or disabled.
6. While listening (started by pressing the button), `Alt+Shift+H` hides the window and listening stops, as with the close button.
7. Change a shortcut, save, and confirm the old combination stops working and the new one works. Restart the app and confirm it persisted.
8. Conflict: start another program that registers the same combination (for example a clipboard manager), then start this app. The shortcut shows "another application is using it", the others still work, and nothing crashes. Close the other program, press Save again, and confirm it becomes Active.
9. Try a combination that includes Ctrl and Alt, one without Shift, and one using the Windows key: each is refused with a message.
10. Press and hold a shortcut: it fires once, not repeatedly.
11. Quit from the tray menu: the shortcuts stop working (no leftover registration). Launch a second copy while the first runs: the second exits (single instance) and does not leave shortcuts behind.
12. Use a non-English keyboard layout that has an AltGr key and confirm typing is not affected.
13. Turn shortcuts off in Settings: none of them respond; turn them on again: they do.
14. Keyboard-only use: reach the shortcut boxes with Tab, change one, leave with Tab, and check a screen reader announces the label, the current keys and the status.
