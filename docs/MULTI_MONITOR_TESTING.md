# Multi-monitor and window placement

Status: **implemented in M-6 (D-71). Automated tests use mocked displays. On real Windows only a single-display laptop (125% scaling) has been exercised; everything needing a second monitor, mixed scaling, docking, sleep or remote desktop is NOT yet verified.**

Written for people who use the Interview Assistant desktop app, plus the checklist for whoever tests it on Windows.

## How the assistant window behaves

- **It opens where you last left it**, separately for the compact and the expanded layout, on a display that exists now. Move or resize it by dragging, and the app remembers (a moment after you stop).
- **First run:** the top right of the primary display. After that the primary display is never assumed: the display you used is.
- **It never moves by itself during normal use.** It is repositioned only at startup, after a display change (a short pause lets a dock finish), when you switch compact or expanded, or when you choose a display.
- **If it is at least 60% visible it is left exactly where it is**, even straddling two screens. If less, it is moved fully inside a screen: the one it was on, otherwise the **nearest** one.
- **Unplugging the monitor it is on** moves it to a connected display. Your remembered place is kept, so **when that monitor comes back the window returns to where you put it**, unless you moved it in the meantime. The same applies when a resolution or scaling change squeezed it and is changed back.
- **Compact and expanded** each remember their own size and place. Expanding near a screen edge keeps the window fully on screen; on a very small screen it shrinks to fit.
- **Choosing a display:** Settings, Displays lists the connected displays (number, primary, size, scaling, position) and shows which one the assistant is on. "Move here" puts the assistant in the middle of that display and remembers it. This only moves the assistant window. The screenshot display chooser is separate and unchanged.
- Always-on-top, opacity, the tray, the keyboard shortcuts and crash recovery work as before. Placement never starts capture or listening.

### What is saved

Only inside the app's `settings.json`, as `windowPlacement`: for each mode its position, size, and the geometry of the display it was on. Nothing is sent anywhere. Bad or absurd values (for example Windows' "minimised window" position of -32000) are ignored and the default place is used. The section is written only after you have moved or resized the window.

## A Windows scaling quirk (measured)

On a 125% scaled display, bare Electron (without this app) returns a window a few pixels wider than requested (asking for 390 gave 391 to 397) and re-applying what it reports makes it wider again. The app therefore saves the size it **intended**, treats differences up to 12 pixels as rounding, and keeps 8 pixels clear of the right and bottom screen edge. A window can still look 5 to 7 pixels wider than 390 at 125% and 150% scaling; it does not grow on each restart.

## Automated tests (mocked: they do not prove real Windows behaviour)

`tests/window-placement.test.ts` (geometry, the manager, scenarios for startup, saved positions, invalid coordinates, disconnect and reconnect, resolution and scaling changes, display selection, compact and expanded, restart, shutdown, and the scaling-rounding regression), `tests/placement-ipc.test.ts` (settings persistence, the two display channels, mode routing, always-on-top unchanged), `tests/placement-guards.test.ts` (placement cannot touch capture, audio, always-on-top, a repeating timer; shortcuts and recovery do not use it).

## Real Windows checks already done (one display only)

On the built app, throw-away profile, laptop display 1536x864 at 125%, driven with a real mouse drag, Win32 window moves and the browser debugging protocol (test aids, not part of the app): first run fully inside the work area and marked primary and current in the display list; a real title-bar drag saved the new position; restart restored the position exactly and the saved size stayed 390x520 (the window measured up to 7 pixels larger, see the quirk above); an external native window move was saved; expanding from the bottom right edge stayed inside the screen and returning to compact restored the compact place; both modes saved separately; "move to display" centred the window and a non-existent display was refused; a real renderer crash and recovery left the window where it was; a saved -32000 position was discarded; a saved position far off screen was brought fully inside; garbage coordinates were ignored.

## Windows manual checklist (record results in `docs/DESKTOP_MANUAL_CHECKLIST.md`)

Use the packaged app (`npm run package:win`). For each item note the monitors, resolutions and scaling.

**A. Laptop display only**
1. Fresh profile: the window opens fully visible at the top right. Drag it, quit from the tray, start again: same place.
2. Expand and collapse near each screen edge: always fully visible, returns to the compact place.
3. Change the display scaling (100%, 125%, 150%) in Windows settings while the app runs: the window stays usable and visible, and does not jump if it was already usable.

**B. Two external displays (plus the laptop if available)**
4. Place the assistant on each display in turn by dragging; restart each time: it returns to the same display and spot.
5. Settings, Displays: the list matches Windows' Display settings (count, primary, resolution, scaling, order). "Move here" centres the window on the chosen display, also for a display to the left of or above the primary (negative positions).
6. Drag it so it straddles two displays and restart: it is kept as it was (at least 60% visible).

**C. Different resolutions and scaling**
7. With one monitor at 100% and another at 150% or 175%, drag between them: size stays the same apart from a few pixels, no large jump in size, no blur or clipping, nothing stuck half off screen.
8. Restart with the window on the high-scaling monitor, then on the low-scaling one: placement is right both times. Restart 5 times in a row: the window does **not** get wider each time.
9. Use "Move here" between the monitors with different scaling: the window ends up at the expected size.

**D. Disconnecting and reconnecting**
10. With the assistant on monitor 2, unplug it (or disable it in Windows, or turn it off with the cable out): within about a second the window moves fully onto a connected display and is usable.
11. Plug it back in: the window returns to where you had put it on monitor 2. Repeat, but this time move the window yourself after unplugging: it must NOT jump back when the monitor returns.
12. Unplug the monitor while the window is hidden in the tray, then show it from the tray: it is on a connected display. Unplug while it is minimized and then restore it: same.
13. Quit with the monitor unplugged, start the app, plug the monitor in: window appears on the connected display (and moves back to monitor 2 if your last placement was there).

**E. Restart behaviour**
14. Restart with each monitor arrangement above, and after changing which monitor is primary: never off screen, never behind the taskbar.
15. Delete the `windowPlacement` part of `settings.json` while the app is closed: it opens at the top right of the primary display.

**F. Compact and expanded**
16. Set different places for compact and expanded (on different monitors), restart in each mode: each is restored.
17. Expanded on a small monitor: shrinks to fit and stays fully visible.

**G. Moving between displays**
18. Drag across monitors repeatedly and fast; use Win+Shift+Arrow to move it. It lands where released and the position is remembered after a moment.

**H. Sleep, wake, docking, remote desktop**
19. Put the displays to sleep (or sleep the PC) and wake it: the window is where it was; no jump.
20. Dock and undock a laptop with external monitors, with the assistant on an external one: after undocking it is fully visible on the laptop screen; after docking again it returns to the external monitor. Note how long it takes and whether it flickers.
21. Connect over Remote Desktop (which changes the display setup) and disconnect: the window is visible afterwards and returns sensibly.

**I. Safety and compatibility**
22. Keep-on-top is still honoured after every move above (open another window over it; toggle the setting).
23. The keyboard shortcuts show, hide and toggle it without changing its position; the tray menu "Show Assistant" does too.
24. Crash the renderer (Task Manager, end the renderer process): the window reloads in place.
25. At no point does moving, restoring or choosing a display start listening or a screenshot.
