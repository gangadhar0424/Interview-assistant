# M-15 Owner Approval and Manual Test Plan (screen-capture privacy setting)

Prepared 2026-11-23 for the product owner. This document changes no code and no wording. It reproduces the current wording so it can be approved, lists review notes, and gives a manual validation plan to run on Windows 11.

## 0. Status

**Update 2026-11-24: owner-confirmed manual validation.** The owner reports having manually tested M-15 and that it works successfully. Overall status: **Implemented and manually validated** (implementation complete; automated testing complete; manual validation owner-confirmed). This is one overall statement: the per-row result tables below were not filled in by me and no individual result is invented. In particular, nothing here records that Google Meet, any other conferencing application or every Windows capture method was tested. The rows below keep their original NOT TESTED wording as the historical plan; if the owner has evidence for specific rows, they should be recorded by the owner. The documented limitations of Windows capture protection are unchanged, and no code, consent wording or behaviour was changed.

Feature: an optional, off-by-default Windows setting that asks Windows to leave the assistant's own window out of supported screen capture (Electron `BrowserWindow.setContentProtection`). It is a request to Windows, not a guarantee. No claim is made that the window is invisible to everything or that capture is prevented universally.

Owner decisions that are CONFIRMED and must not change:

1. Remote-session exclusion stays **disabled** (`AllowWindowCaptureExclusionInRemoteSessions` is turned off, `apps/desktop/src/main/index.ts:97`). Remote-access and remote-support tools that capture the desktop keep seeing the window. Recorded in D-80 and `docs/WINDOW_CAPTURE_PRIVACY.md` section 10c.
2. Remote-support visibility stays **enabled**: the assistant is not concealed from anyone entitled to monitor the device.
3. Both consent texts still need owner review and approval: the acceptable-use acknowledgement version 2 (`ACCEPTABLE_USE_VERSION = 2`) and the dedicated M-15 consent (`CAPTURE_PRIVACY_CONSENT_VERSION = 1`). Source: D-80 addendum in `docs/DECISIONS.md`; `docs/WINDOW_CAPTURE_PRIVACY.md` section 10a.

Current status of the feature: **IMPLEMENTED and AUTOMATED-TESTED** (`docs/PRODUCTION_ROADMAP.md` M-15; `docs/WINDOW_CAPTURE_PRIVACY.md`).

Manual testing recorded in the repository: exactly one real-Windows result, recorded in `docs/WINDOW_CAPTURE_PRIVACY.md` section 10 (2026-11-22, Windows 11 build 10.0.26300, Electron 45.2.3). A throwaway script, not part of the repository, opened a solid-colour test window and counted pixels in a memory-only Electron `desktopCapturer` screen capture. With protection on the test window was absent from that capture (0 px vs about 104,000 px off), opaque and transparent. That is a test window and one capture mechanism only. **Nothing in this document's plan has been manually tested.** No file in the repository records a manual result for Snipping Tool, Game Bar, PrintScreen, OBS, a conferencing app, a remote-support tool, two monitors, Windows below 2004, or a screen reader (`docs/WINDOW_CAPTURE_PRIVACY.md` section 7, `docs/PRODUCTION_ROADMAP.md` M-15 "Not tested"; `docs/DESKTOP_MANUAL_CHECKLIST.md` has no M-15 section).

Status words used in this document: IMPLEMENTED, AUTOMATED-TESTED, MANUALLY-TESTED, NOT TESTED, BLOCKED.

## 1. Wording for owner approval

All text below is copied exactly from the source files at the line numbers given. Typos and oddities, if any, are intentionally left as they are and are listed in section 2.

### 1a. Acknowledgement version 2 (acceptable-use notice)

File: `apps/desktop/src/shared/acknowledgement.ts`. The same constants are shown in the renderer (`apps/desktop/src/renderer/src/ui/AcceptableUseNotice.tsx`, inside the Audio and Screenshot panels, lines 16-49) and in a native dialog (`apps/desktop/src/main/index.ts:167-179`).

Version constant, line 8:

```
export const ACCEPTABLE_USE_VERSION = 2;
```

Title (`ACCEPTABLE_USE_TITLE`, line 10):

```
Acceptable use
```

Intro (`ACCEPTABLE_USE_INTRO`, lines 12-13):

```
Before you use audio or screenshot assistance, please confirm the following.
```

Statements (`ACCEPTABLE_USE_STATEMENTS`, lines 15-20; shown as a numbered list in the window and as "1. ", "2. " ... in the native dialog):

Statement 1 (line 16):

```
I am responsible for following the rules of any interview, meeting or assessment where I use this app. If those rules do not allow AI assistance, recording or screenshots, I will not use it there.
```

Statement 2 (line 17):

```
I will capture or process audio and screenshots only when I have permission to do so, including the permission of other people whose voice or screen may be captured, and as the law requires.
```

Statement 3 (line 18):

```
I will not use this app in any way that the relevant interview or assessment rules prohibit.
```

Statement 4 (line 19, the version 2 addition naming the M-15 setting):

```
If I turn on the optional setting that asks Windows to leave this window out of screen capture, I understand it is not a guarantee, does not hide the app from the taskbar, tray or monitoring software, and I will not use it to avoid monitoring or break any rule that applies to me.
```

Closing note (`ACCEPTABLE_USE_NOTE`, lines 22-23):

```
This app always shows when it is listening or capturing. It does not hide itself and must not be used to avoid monitoring.
```

Message shown when audio or screenshots are attempted before acceptance (`ACKNOWLEDGEMENT_REQUIRED_CODE` line 26, `ACKNOWLEDGEMENT_REQUIRED_MESSAGE` lines 27-28):

```
acknowledgement_required
```

```
Read and accept the acceptable-use notice before using audio or screenshots.
```

Buttons and related strings:

| String | File:line |
|---|---|
| `Review and accept…` (renderer button) | `apps/desktop/src/renderer/src/ui/AcceptableUseNotice.tsx:45` |
| `Waiting for your confirmation…` (renderer button while busy) | `apps/desktop/src/renderer/src/ui/AcceptableUseNotice.tsx:45` |
| `I understand and agree` (native dialog, button 0) | `apps/desktop/src/main/index.ts:173` |
| `Cancel` (native dialog, button 1, default and cancel button) | `apps/desktop/src/main/index.ts:173-175` |
| `Sign in before accepting the acceptable-use notice.` | `apps/desktop/src/main/ipc.ts:414` |
| `The acceptable-use notice is already open.` | `apps/desktop/src/main/ipc.ts:416` |
| `The acceptable-use notice was not accepted.` (shown after Cancel) | `apps/desktop/src/main/ipc.ts:419` |

Native dialog composition (`apps/desktop/src/main/index.ts:168-177`): type `question`; title = `ACCEPTABLE_USE_TITLE`; message = `ACCEPTABLE_USE_INTRO`; detail = the four statements numbered and separated by blank lines, then a blank line, then `ACCEPTABLE_USE_NOTE`.

### 1b. Dedicated M-15 consent

File: `apps/desktop/src/shared/capture-privacy.ts` unless stated.

Consent version constant, line 21 (with its explanation in lines 17-20):

```
export const CAPTURE_PRIVACY_CONSENT_VERSION = 1;
```

Minimum Windows build constant, line 15 (value used by the "unsupported" message below):

```
export const MIN_SUPPORTED_WINDOWS_BUILD = 19041;
```

**Settings section, shown first, always visible before the button** (`CAPTURE_PRIVACY_TITLE`, line 48; rendered as the section heading at `apps/desktop/src/renderer/src/ui/CapturePrivacyPanel.tsx:56`):

```
Hide this window from screen capture
```

Explanation paragraphs (`CAPTURE_PRIVACY_EXPLANATION`, lines 50-55). The same four paragraphs, joined by a blank line, are the detail of the native confirmation dialog.

Paragraph 1 (line 51):

```
When this is on, the app asks Windows to leave the Interview Assistant window out of screen capture that supports the request, such as many screen-sharing and recording apps. It is meant for keeping private material, like your resume or notes, from showing in a screen share you are allowed to make, and for accessibility.
```

Paragraph 2 (line 52):

```
It only affects this app's own window. The app still appears in the taskbar and the system tray, still shows its listening and capture indicators, and is still listed as a running program. In a Windows remote session the window is not excluded, so remote-access and remote-support tools still see it.
```

Paragraph 3 (line 53):

```
It is not a guarantee. Some capture methods, other screens, cameras pointed at your display, and security, proctoring or monitoring software may still see the window. It does not and must not be used to avoid monitoring.
```

Paragraph 4 (line 54):

```
You remain responsible for following the rules of any interview, meeting or assessment where you use this app. If those rules do not allow AI assistance, do not use it there.
```

**Native confirmation dialog** (`apps/desktop/src/main/index.ts:268-282`):

| Field | Exact value | Line |
|---|---|---|
| type | `warning` | 271 |
| title | `CAPTURE_PRIVACY_TITLE`, i.e. `Hide this window from screen capture` | 272 |
| message | `${CAPTURE_PRIVACY_TITLE}?`, i.e. `Hide this window from screen capture?` | 273 |
| detail | `CAPTURE_PRIVACY_EXPLANATION.join("\n\n")` (paragraphs 1-4 above) | 274 |
| buttons | `["I understand, turn on", "Cancel"]` | 275 |
| defaultId / cancelId | `1` / `1` (Cancel is the default and the Escape result) | 276-277 |

**Settings buttons** (`apps/desktop/src/renderer/src/ui/CapturePrivacyPanel.tsx`):

| String | Line |
|---|---|
| `Turn on…` (disabled when the device is unsupported or while busy) | 88 |
| `Waiting for confirmation…` (while the native dialog is open) | 88 |
| `Turn off` | 78 |
| `Working…` (while turning off) | 78 |

**Indicator** (`CAPTURE_PRIVACY_INDICATOR`, lines 57-58). Shown in a banner on every page while the setting is on and the status is `requested`; its accessible name is the literal `Screen capture privacy` (`CapturePrivacyPanel.tsx:31`):

```
Hidden from screen capture: this window has been requested to be left out of supported captures. Not guaranteed.
```

**Status messages** (`CAPTURE_PRIVACY_MESSAGES`, lines 60-68). Shown as a status line in the Settings section; the banner shows the `failed` and `unsupported` messages instead of the indicator text when the setting is on but not `requested`.

`off` (line 61):

```
Off. This window can be captured like any other window.
```

`requested` (lines 62-63):

```
On. Windows was asked to leave this window out of supported screen capture. This is not a guarantee, and the app does not check whether a given tool honours it.
```

`unsupported` (lines 64-65):

```
Not available on this device. It needs Windows 10 version 2004 (build 19041) or later. The app works normally and the window is shown as usual.
```

`failed` (lines 66-67):

```
Windows did not accept the request. The window is shown as usual and the app works normally. You can try again.
```

**Acknowledgement required** (`CAPTURE_PRIVACY_ACK_REQUIRED_CODE` line 71, `CAPTURE_PRIVACY_ACK_REQUIRED_MESSAGE` lines 72-73):

```
acknowledgement_required
```

```
Read and accept the acceptable-use notice before turning on screen-capture privacy.
```

**Other user-visible messages for this feature:**

Withdrawal not confirmed by Windows (`apps/desktop/src/main/capture-privacy.ts:151-152`, shown as the status line in Settings):

```
Windows did not confirm that the window can be captured again. Restart the app to be sure. The setting is saved as off.
```

Declined, unsupported and failed outcomes are shown as an alert under the button using the `off` or `unsupported` or `failed` message above (`apps/desktop/src/renderer/src/store.ts:463-465`: the message of the unchanged state is shown when turning on did not result in "enabled"). A declined confirmation therefore shows the `off` message: `Off. This window can be captured like any other window.`

Other error strings (`safeMessage` of a thrown error):

| String | File:line |
|---|---|
| `The confirmation is already open.` | `apps/desktop/src/main/capture-privacy.ts:103` |
| `Choose on or off.` | `apps/desktop/src/main/ipc.ts:390` |
| `Screen-capture privacy is not available in this build.` | `apps/desktop/src/main/ipc.ts:386` and `:391` |
| `The window did not report the requested capture-protection state.` (internal; shown to the user as the `failed` message) | `apps/desktop/src/main/capture-privacy.ts:62` |

**Stored consent record** (not text, shown for the approval record): `capturePrivacy: true` plus `capturePrivacyConsent = { version: 1, acceptedAt }` in `settings.json` (`apps/desktop/src/main/capture-privacy.ts:123-126`, validation in `apps/desktop/src/main/settings-store.ts:106-118`).

### 1c. Approval block

Wording covered: acknowledgement version 2 (`ACCEPTABLE_USE_VERSION = 2`) and M-15 consent version 1 (`CAPTURE_PRIVACY_CONSENT_VERSION = 1`), exactly as reproduced in sections 1a and 1b.

- [ ] Approved as written
- [ ] Approved with changes (owner writes the changes below)
- [ ] Rejected

Changes or reasons:

```
(owner to write here)




```

Name: ______________________________

Date: ______________________________

Note: any approved change to the wording must be followed by raising the relevant version constant so existing users consent again, and by updating the tests that assert the wording (`apps/desktop/tests/capture-privacy.test.ts`).

## 2. Review notes for the owner (no wording was changed)

1. **Indicator begins with "Hidden from screen capture:".** The sentence continues with "requested ... Not guaranteed", but the lead words state an outcome. The same file's header says the feature must never claim it hides the window, and the settings message uses "asked ... not a guarantee". Consider whether the banner could say "Requested" first. Also the Settings title "Hide this window from screen capture" is phrased as an outcome.
2. **Acknowledgement v2 statement 4 and consent paragraph 3 differ.** Statement 4 says the setting "does not hide the app from the taskbar, tray or monitoring software"; paragraph 3 says monitoring software "may still see the window". Both are cautious, but they are not the same claim. The acknowledgement does not mention the Windows process list or remote sessions; the consent does.
3. **Where the acknowledgement is first shown.** The acknowledgement is displayed only inside the Audio and Screenshot panels (`AudioPanel.tsx:113`, `ScreenshotPanel.tsx:65`). Its intro says "Before you use audio or screenshot assistance". The Settings section for M-15 does not show it and does not say where to accept it. A user who only wants M-15 and presses Turn on… before accepting gets the message "Read and accept the acceptable-use notice before turning on screen-capture privacy." with no pointer to the panel. Also, the notice is shown only when audio or screenshot is not active.
4. **Outdated consent version gives no explanation.** If `CAPTURE_PRIVACY_CONSENT_VERSION` is raised, or the consent record is missing or damaged, the saved "on" loads as off and the user sees only `Off. This window can be captured like any other window.` The code path is fail-closed (good) but the user is not told that re-consent is needed, or that protection stopped.
5. **Declined confirmation looks like an error.** After Cancel the `off` message is shown in a `role="alert"` paragraph styled as an error (`CapturePrivacyPanel.tsx:92-96`, class `inline-error`). A user choice is announced as an alert.
6. **Failed state while on has no action in the banner.** The `failed` banner text ends "You can try again", but the banner has no button; the user must go to Settings. When the setting is `enabled` but `failed`, the Settings button reads "Turn on…" (not "Turn off"), because the button depends on status `requested`. Turning off a failed-but-enabled setting would need the user to turn on again first. Owner to confirm this is acceptable.
7. **Withdrawal failure is visible only in Settings.** If Windows cannot confirm removal, the saved choice is off, so the banner is hidden (it needs `enabled`), and the "Restart the app to be sure" message appears only as a status line in Settings.
8. **Very small text.** `.settings-help` is 8px in a muted colour (`#76869b`) and `.inline-error` is 8px (`apps/desktop/src/renderer/src/styles.css:208` and `:189`). The four consent paragraphs and the status messages use that class, so the legally important explanation is the smallest text in the app. Contrast has not been measured. The acknowledgement list uses 12px (`.ack-list`, line 296). Needs a visual and low-vision check (area 8 below).
9. **Native dialog default.** Cancel is the default button and the Escape result in both dialogs (good). The dialog `detail` is about 1,000 characters; readability in the native dialog and with screen readers is NOT TESTED.
10. **Wording quirks left as is.** "It does not and must not be used to avoid monitoring." (paragraph 3) reads oddly because "it" is the setting; "keeping private material ... from showing in a screen share you are allowed to make" is the stated purpose and is the main sentence that relies on the user's own judgement. The product cannot verify what a given interview permits (`WINDOW_CAPTURE_PRIVACY.md` section 7).
11. **Missing limits in the explanation.** The text does not say that the effect is not instant (it takes effect with the next desktop composition, `WINDOW_CAPTURE_PRIVACY.md` section 3), that results differ by tool and Windows build, that a screenshot or recording already taken is unaffected, or that a Windows build below 2004 is refused. The `unsupported` message covers the last point. The text does not state that the setting is stored only on this device (`PRIVACY.md` does).
12. **Unused constant.** `CAPTURE_PRIVACY_ACK_REQUIRED_CODE` is exported but the code does not use it (only the message is thrown, `capture-privacy.ts:101`). It duplicates `ACKNOWLEDGEMENT_REQUIRED_CODE`. No user impact.
13. **Documentation counts differ.** `D-80` says 46 new tests and `PRODUCTION_ROADMAP.md` / `WINDOW_CAPTURE_PRIVACY.md` say 52 (the later figure includes the consent tests). This is a documentation matter only.
14. **Banner announcement.** The banner is a `role="status"` region that is present when the page renders; many screen readers do not announce a live region that exists at load, so the indicator may not be spoken when the app starts with the setting already on. Needs the screen-reader rows below.
15. **Persistent indicator is in-window only.** The indicator is visible only when the assistant window itself is visible. When the window is hidden to the tray, nothing else indicates the setting (the tray tooltip does not mention it). Owner to decide whether that is sufficient.

## 3. Manual validation plan

Applies to the packaged Windows app on Windows 11 (or the build under test). Do not use any real interview or other people's data. Use a test account.

Preparation (once):

1. Install the packaged Interview Assistant and sign in with a test account.
2. Record the Windows version: press Windows key, type `winver`, press Enter, note "OS Build". Record the app version from Settings or the installer name. Electron version is 45.2.3 unless the build changes.
3. Make a folder `M15-evidence` on the Desktop for screenshots and recordings. Name files with the row ID, for example `CAP-04-on.png`.
4. The Settings page of the app contains the section **Hide this window from screen capture**. "Protection on" means the status line reads "On. Windows was asked ...". "Protection off" means it reads "Off. ...".
5. Use something recognisable in the assistant window (for example the Settings page open) so you can tell whether it appears in a capture.
6. When a row says "wait a moment", wait about 3 seconds after turning protection on or off before capturing; the effect applies with the next desktop composition.

Result column: every row starts as NOT TESTED. Replace with MANUALLY-TESTED (with the observed outcome in Notes), or BLOCKED (with the reason). Do not use any other word.

Generic outcomes for capture rows: **hidden** (window absent, desktop or what is behind it shows), **black** (window area drawn black), **visible** (window appears normally). Results vary by tool and Windows build. No universal prevention is claimed or expected.

### Area 1. Persistent on-screen indicator

| ID | Step | Expected result | Result | Evidence to attach | Notes |
|---|---|---|---|---|---|
| IND-1 | Fresh settings (setting off). Open each page of the app in turn (Assistant, Sessions, Settings and any others in the sidebar). | No capture-privacy banner on any page. Settings status line reads "Off. This window can be captured like any other window." | NOT TESTED | IND-1-off-pages.png | |
| IND-2 | Turn the setting on (rows CON-1 to CON-4 first). Open every page in the sidebar one by one. | A banner reading "Hidden from screen capture: this window has been requested to be left out of supported captures. Not guaranteed." is visible at the top of the content area on every page. | NOT TESTED | IND-2-banner-page1.png, IND-2-banner-page2.png (one per page) | List pages checked. |
| IND-3 | Look for any close, dismiss or hide control on the banner. Press Escape. Click the banner. | No way to dismiss it. | NOT TESTED | IND-3.png | |
| IND-4 | Switch between compact and expanded layouts (the app's layout control). | Banner still visible and readable in both layouts. | NOT TESTED | IND-4-compact.png, IND-4-expanded.png | |
| IND-5 | Read the banner at normal viewing distance without zooming. | Text is legible (see review note 8). Record whether you could read it comfortably. | NOT TESTED | IND-5.png | Subjective; record the display size and scaling. |
| IND-6 | Hide the app to the tray (close button) while protection is on. Look at the tray icon tooltip and menu. | Record what the tray shows. (Review note 15: the indicator is in-window only.) Tray icon is present. | NOT TESTED | IND-6-tray.png | |
| IND-7 | With protection on, force the failed state is not possible manually; instead check the warning style only if a `failed` or `unsupported` state occurs naturally (for example in area 10). | Banner switches to a warning showing the status message and never says protection is on. | NOT TESTED | IND-7.png | Mark BLOCKED if the state cannot occur on the test machine. |

### Area 2. Consent acceptance and rejection

| ID | Step | Expected result | Result | Evidence to attach | Notes |
|---|---|---|---|---|---|
| CON-1 | With a freshly reset acknowledgement (new test account or new profile), go to Settings and press **Turn on…** in the capture-privacy section without accepting the acceptable-use notice. | No native dialog opens. Message "Read and accept the acceptable-use notice before turning on screen-capture privacy." appears. Setting stays off, no banner. | NOT TESTED | CON-1.png | |
| CON-2 | Open the Audio or Screenshot panel. Read the acceptable-use notice (version 2). Confirm statement 4 mentions the optional Windows setting. Press **Review and accept…**. | A native dialog titled "Acceptable use" appears with the intro, the four numbered statements and the closing note, and buttons "I understand and agree" and "Cancel". Cancel is the default. | NOT TESTED | CON-2-native-ack.png | |
| CON-3 | In that dialog press **Cancel**. | Dialog closes. Message "The acceptable-use notice was not accepted." appears. Notice still shown. Audio and screenshot still refuse to start. Retry CON-1: still refused. | NOT TESTED | CON-3.png | |
| CON-4 | Press **Review and accept…** again and choose **I understand and agree**. | Notice disappears from the panel. | NOT TESTED | CON-4.png | |
| CON-5 | Go to Settings. Read the section before the button. | The title "Hide this window from screen capture" and all four explanation paragraphs are shown above the button, which reads "Turn on…". Status line reads "Off. ...". | NOT TESTED | CON-5-section.png | Compare to section 1b text word for word. |
| CON-6 | Press **Turn on…**. | A native warning dialog titled "Hide this window from screen capture" with message "Hide this window from screen capture?" and the four paragraphs; buttons "I understand, turn on" and "Cancel"; Cancel is highlighted by default. The button in the window reads "Waiting for confirmation…". | NOT TESTED | CON-6-native-consent.png | |
| CON-7 | In that dialog press **Cancel** (repeat once with the Escape key). | Dialog closes. Setting stays off, no banner, the "Off." message is shown, and no protection is applied (confirm with one capture, area 6, Snipping Tool, shows the window). | NOT TESTED | CON-7.png | |
| CON-8 | Press **Turn on…** again and choose **I understand, turn on**. Wait a moment. | Status line reads "On. Windows was asked to leave this window out of supported screen capture. This is not a guarantee, and the app does not check whether a given tool honours it." Button now reads "Turn off". Banner appears on every page. | NOT TESTED | CON-8.png | |
| CON-9 | Press **Turn on…** twice quickly (before the dialog opens) while off, then answer the dialog. | Only one dialog opens; no second confirmation. | NOT TESTED | CON-9.png | May be hard to reproduce; BLOCKED is acceptable with a note. |
| CON-10 | Outdated consent version: follow the safe edit procedure in Area 4 (PERS-5) but set `capturePrivacyConsent` to `{ "version": 0, "acceptedAt": "2026-01-01T00:00:00.000Z" }` while `capturePrivacy` is `true`. Start the app. | Setting loads as off, no banner, the window is not excluded from capture. The user is not told why (review note 4). The user can turn it on again with a fresh dialog. | NOT TESTED | CON-10.png | Use a copy of settings.json as described in Area 4. |
| CON-11 | Acknowledgement outdated: restore a settings file whose `acceptableUse.version` is 1 (or delete the `acceptableUse` entry on a backup copy). Start the app and press **Turn on…**. | Refused with the acknowledgement-required message until the notice is accepted again. | NOT TESTED | CON-11.png | |

### Area 3. Consent withdrawal

| ID | Step | Expected result | Result | Evidence to attach | Notes |
|---|---|---|---|---|---|
| WDR-1 | With protection on, press **Turn off**. | No confirmation is needed. Status line reads "Off. This window can be captured like any other window." Banner disappears from every page. Button reads "Turn on…". | NOT TESTED | WDR-1.png | |
| WDR-2 | After WDR-1 wait a moment and capture the assistant with Snipping Tool. | Window is visible in the capture. | NOT TESTED | WDR-2.png | |
| WDR-3 | After WDR-1 close the app fully (tray menu, Quit) and start it again. | Setting is still off, no banner. | NOT TESTED | WDR-3.png | |
| WDR-4 | After WDR-3 open a copy of `settings.json` (see Area 4). | No `capturePrivacy` true and no `capturePrivacyConsent` entry. | NOT TESTED | WDR-4.png | Look only; do not edit the live file. |
| WDR-5 | If Windows does not confirm removal: this cannot be forced manually. If it ever happens, record it. Expected: status line "Windows did not confirm that the window can be captured again. Restart the app to be sure. The setting is saved as off." and nothing claims the window is capturable. | The message above is shown, the saved setting is off, and after restarting the app the window is capturable. | NOT TESTED | WDR-5.png | Mark BLOCKED with the reason "cannot be induced manually" unless it occurs. Automated test covers the code path. |
| WDR-6 | Turn it on again after withdrawal. | The full native consent dialog appears again (consent was deleted, not remembered). | NOT TESTED | WDR-6.png | |

### Area 4. Application restart

| ID | Step | Expected result | Result | Evidence to attach | Notes |
|---|---|---|---|---|---|
| PERS-1 | With protection on, quit the app completely (tray menu, Quit) and start it. | Setting still on, banner visible on every page, status "On. ...". No consent dialog is shown. | NOT TESTED | PERS-1.png | |
| PERS-2 | Immediately after PERS-1 (and after waiting a moment) capture the assistant with Snipping Tool. | Record the outcome (hidden, black or visible). It should match the outcome before the restart. | NOT TESTED | PERS-2.png | |
| PERS-3 | Sign out and sign in again with protection on. | Setting stays on, banner remains. | NOT TESTED | PERS-3.png | |
| PERS-4 | Restart Windows with protection on, then start the app. | Same as PERS-1. | NOT TESTED | PERS-4.png | |
| PERS-5 | **No auto-enable without a valid consent record.** Safe edit on a copy: (a) quit the app fully (tray menu, Quit) and check Task Manager that no "Interview Assistant" process remains; (b) press Windows+R, type `%APPDATA%`, Enter, and open the app's data folder (normally named `Interview Assistant`; if not found, search the folder for `settings.json`); (c) copy `settings.json` to the Desktop as `settings.backup.json` (this is the safe copy; keep it unchanged); (d) open the original `settings.json` in Notepad; with protection previously off, add `"capturePrivacy": true,` as a new top-level line (keep valid JSON, commas, braces) and make sure there is no `capturePrivacyConsent` entry; save; (e) start the app. | Setting loads as **off**: no banner, status "Off. ...", the window is not excluded (confirm with one Snipping Tool capture), and no dialog opened by itself. | NOT TESTED | PERS-5-settings-edited.png, PERS-5-app.png | A damaged file may reset other settings; this is why the backup exists. |
| PERS-6 | Restore: quit the app, delete the edited `settings.json`, copy `settings.backup.json` back to the data folder as `settings.json`, start the app. | App starts with the original settings. | NOT TESTED | PERS-6.png | |
| PERS-7 | Repeat PERS-5 using `"capturePrivacy": "true"` (text, not boolean) and `"capturePrivacy": 1`. | Both load as off. | NOT TESTED | PERS-7.png | Restore afterwards as PERS-6. |
| PERS-8 | Local-tamper case, for information: with a valid consent record present, a file edited to `capturePrivacy: true` loads as on and the banner shows (documented residual risk, `WINDOW_CAPTURE_PRIVACY.md` section 8). Do only by editing a settings file that already holds a consent from a real Turn on. | Banner is shown whenever it is on. | NOT TESTED | PERS-8.png | Optional. |

### Area 5. Window recreation

| ID | Step | Expected result | Result | Evidence to attach | Notes |
|---|---|---|---|---|---|
| WIN-1 | With protection on, close the window to the tray (the close button), then reopen from the tray icon. Wait a moment, then capture. | Banner shown, same capture outcome as before closing. | NOT TESTED | WIN-1.png | |
| WIN-2 | With protection on, minimize the window, then restore it from the taskbar. Wait a moment, capture. | Banner shown, same outcome. | NOT TESTED | WIN-2.png | |
| WIN-3 | Switch between compact and expanded layouts several times, capturing after each. | Banner shown each time; same outcome in both layouts. | NOT TESTED | WIN-3-compact.png, WIN-3-expanded.png | |
| WIN-4 | Renderer crash recovery. The app's recovery behaviour is described in `docs/DESKTOP_TROUBLESHOOTING.md`; use the method documented there for a controlled renderer crash. If no documented non-developer method exists, mark BLOCKED. After the app reloads, check the banner and capture. | The app reports recovery. The banner is shown and the capture outcome matches the outcome before the crash. Nothing started recording. | NOT TESTED | WIN-4.png | Do not use developer tools on a packaged build. |
| WIN-5 | Display change: with protection on, change display resolution or scaling in Windows Settings, then capture. | Banner shown; same outcome. | NOT TESTED | WIN-5.png | Also see Area 7. |
| WIN-6 | Lock the PC (Windows+L) and unlock with protection on. Capture. | Same outcome. | NOT TESTED | WIN-6.png | |
| WIN-7 | With protection on, turn off then on again several times, capturing after each. | Outcome follows the setting each time. | NOT TESTED | WIN-7.png | |

### Area 6. Windows capture methods

State for every row: results vary by tool and Windows build. This plan does not assume any outcome; record hidden, black or visible. Capture or record with the assistant window clearly on screen (Settings page open) and, for each tool, run once with protection off and once with protection on, waiting a moment after each change. Note the Windows build and the tool version in Notes.

Each row's Expected result is: "Record the outcome (hidden, black or visible). With protection off the window should be visible." and the Result column starts as NOT TESTED.

| ID | Step | Expected result | Result | Evidence to attach | Notes |
|---|---|---|---|---|---|
| CAP-1a | Snipping Tool (Windows key, type Snipping Tool): take a rectangle snip covering the assistant window. **Protection off.** | Visible. | NOT TESTED | CAP-1a-snip-off.png | Outcome: |
| CAP-1b | Same, **protection on.** | Record hidden, black or visible. | NOT TESTED | CAP-1b-snip-on.png | Outcome: |
| CAP-1c | Snipping Tool, window snip mode and full-screen snip mode, protection on. | Record each outcome. | NOT TESTED | CAP-1c-window.png, CAP-1c-full.png | Outcome: |
| CAP-1d | Snipping Tool video recording (if the build has it), protection on, 5 seconds. | Record outcome in the video. | NOT TESTED | CAP-1d-video.mp4 | Outcome: |
| CAP-2a | Win+Shift+S rectangle capture over the window, **protection off**, paste into Paint. | Visible. | NOT TESTED | CAP-2a.png | Outcome: |
| CAP-2b | Win+Shift+S, **protection on**. | Record outcome. | NOT TESTED | CAP-2b.png | Outcome: |
| CAP-3a | Xbox Game Bar (Windows+G) screenshot or recording while the assistant is in front, **protection off**. | Visible. Note: Game Bar may refuse to capture some windows; record if it does. | NOT TESTED | CAP-3a.png | Outcome: |
| CAP-3b | Same, **protection on**. | Record outcome. | NOT TESTED | CAP-3b.png | Outcome: |
| CAP-4a | PrintScreen with the full screen, paste into Paint, **protection off**. | Visible. | NOT TESTED | CAP-4a.png | Outcome: |
| CAP-4b | PrintScreen, **protection on**. Also Alt+PrintScreen with the assistant active. | Record each outcome. | NOT TESTED | CAP-4b-full.png, CAP-4b-alt.png | Outcome: |
| CAP-5a | OBS Studio, source "Window Capture" with the Windows Graphics Capture method, **protection off**. | Visible. | NOT TESTED | CAP-5a.png | Outcome: |
| CAP-5b | OBS Window Capture, Windows Graphics Capture method, **protection on**. | Record outcome. | NOT TESTED | CAP-5b.png | Outcome: |
| CAP-5c | OBS Window Capture, BitBlt method, protection off then on. | Record both outcomes. | NOT TESTED | CAP-5c-off.png, CAP-5c-on.png | Outcome: |
| CAP-5d | OBS "Display Capture" source, Windows Graphics Capture (or the default DXGI method) and, if available, the other method, protection off then on. | Record each outcome. | NOT TESTED | CAP-5d-off.png, CAP-5d-on.png | Outcome: |
| CAP-6a | Microsoft Teams (test meeting with a second account or device): share a single window (the assistant), **protection off**; the viewer reports what they see. | Visible to the viewer. | NOT TESTED | CAP-6a-viewer.png | Outcome: |
| CAP-6b | Teams window share, **protection on**. | Record outcome as seen by the viewer. | NOT TESTED | CAP-6b-viewer.png | Outcome: |
| CAP-6c | Teams full-screen share, protection off then on. | Record both outcomes. | NOT TESTED | CAP-6c-off.png, CAP-6c-on.png | Outcome: |
| CAP-7a | Zoom (test meeting): window share and full-screen share, protection off then on. | Record outcomes for each. | NOT TESTED | CAP-7a-*.png | Outcome: |
| CAP-8a | Google Meet in a browser (test meeting): share a window, a tab and the entire screen, protection off then on. | Record outcomes for each. | NOT TESTED | CAP-8a-*.png | Outcome: |
| CAP-9a | A Windows recorder tool: Xbox Game Bar recorder is covered in CAP-3. Test any other built-in or installed recorder or Steps Recorder style tool that exists on the test build (Steps Recorder is not present on all Windows 11 builds; record which tool and build), protection off then on. | Record outcomes. | NOT TESTED | CAP-9a-*.png | Outcome: |
| CAP-10a | Remote desktop or remote-support tool (see Area 9 for the required expected result): connect a second device and look at the assistant window, protection off then on. | Visible to the remote viewer in both states (owner decision). | NOT TESTED | CAP-10a-off.png, CAP-10a-on.png | Outcome: |
| CAP-11 | Photograph or video the screen with a phone, protection on. | Visible (a camera is outside the setting, as the consent text says). | NOT TESTED | CAP-11.jpg | Outcome: |

### Area 7. Multiple monitors

Requires two physical monitors (a mix of different DPI settings is preferable). If only one monitor is available, mark BLOCKED.

| ID | Step | Expected result | Result | Evidence to attach | Notes |
|---|---|---|---|---|---|
| MON-1 | Protection on. Drag the assistant window to the second monitor. Wait a moment. Capture that monitor with Snipping Tool. | Banner stays visible; record capture outcome. | NOT TESTED | MON-1.png | Outcome: |
| MON-2 | Move it back to the first monitor and capture the first monitor. | Same outcome as MON-1. | NOT TESTED | MON-2.png | Outcome: |
| MON-3 | Set the two monitors to different scaling (for example 100% and 150%) and repeat MON-1 and MON-2. | Banner readable on both; same capture outcome. | NOT TESTED | MON-3-a.png, MON-3-b.png | Outcome: |
| MON-4 | Capture each display in turn with the protection off (baseline) and on. | Record outcomes for each display. | NOT TESTED | MON-4-*.png | Outcome: |
| MON-5 | With the assistant on the second monitor and protection on, disconnect that monitor (unplug or Windows+P to "PC screen only"). | Window comes back onto the remaining display, banner shown, no error, outcome unchanged. | NOT TESTED | MON-5.png | Outcome: |
| MON-6 | Reconnect the monitor, then repeat MON-1. | App behaves normally; setting still on. | NOT TESTED | MON-6.png | |
| MON-7 | Screen-share the second display only (Teams or Zoom), with the assistant on the first and then on the second. | Record outcomes. | NOT TESTED | MON-7-*.png | Outcome: |

### Area 8. Screen-reader compatibility

Run with Narrator (Windows+Ctrl+Enter) and, if available, NVDA. Protection must not break accessibility: nothing about the screen reader should change when protection is on.

| ID | Step | Expected result | Result | Evidence to attach | Notes |
|---|---|---|---|---|---|
| SR-1 | Narrator on, protection off. Navigate to the Settings section. | The heading "Hide this window from screen capture" is announced; the four paragraphs can be read; the status line is readable; the button is announced as a button with its label ("Turn on…") and its description (the explanation). | NOT TESTED | SR-1-recording.mp4 or SR-1-notes.txt | Repeat with NVDA in SR-1n. |
| SR-2 | Press **Turn on…** with the keyboard (Enter or Space). | The native dialog is read: title, message, the four paragraphs and the buttons "I understand, turn on" and "Cancel". Focus starts on Cancel. | NOT TESTED | SR-2.mp4 | |
| SR-3 | Choose "I understand, turn on". | The result is announced (status line) and the banner text is announced or can be found. Note whether the banner is announced at all. | NOT TESTED | SR-3.mp4 | Review note 14. |
| SR-4 | Navigate the page with protection on: Tab through all controls. | Focus order is logical (banner is not a focus trap); every control has an accessible name; the banner is exposed as the region "Screen capture privacy". | NOT TESTED | SR-4.mp4 | |
| SR-5 | Restart the app with protection on while Narrator is running. | Banner is reachable and readable. | NOT TESTED | SR-5.mp4 | |
| SR-6 | Press **Turn off** with the keyboard. | "Off" status is announced; banner disappears. | NOT TESTED | SR-6.mp4 | |
| SR-7 | Cancel on the consent dialog with a screen reader running. | The "Off" message appears as an alert (review note 5); record how it is announced. | NOT TESTED | SR-7.mp4 | |
| SR-8 | Acceptable-use notice (version 2) read by Narrator in the Audio panel and in its native dialog. | All four statements and the note are readable in order. | NOT TESTED | SR-8.mp4 | |
| SR-9 | Windows magnifier at 200 percent and Windows high contrast mode: view the Settings section and the banner. | Text and banner remain readable. | NOT TESTED | SR-9.png | Review note 8. |

### Area 9. Remote-support visibility

Owner decision: the assistant stays visible in remote-support sessions; exclusion in remote sessions stays disabled and must not be changed. Use a harmless test setup between two machines you own, with consent from every participant.

| ID | Step | Expected result | Result | Evidence to attach | Notes |
|---|---|---|---|---|---|
| REM-1 | Start a remote-support or remote-access session that captures the desktop (for example a Quick Assist session, a vendor remote-support tool). Protection **off**. On the remote viewer, look at the assistant window. | Visible. | NOT TESTED | REM-1-viewer.png | Name the tool and version in Notes. |
| REM-2 | Same session, protection **on**. | Window **visible** to the remote viewer. If it is not visible, record BLOCKED/failed expectation and report to the owner; do not change settings. | NOT TESTED | REM-2-viewer.png | |
| REM-3 | In REM-2 read the banner on the viewer's screen. | Indicator text is readable by the remote viewer. | NOT TESTED | REM-3-viewer.png | |
| REM-4 | Windows Remote Desktop (mstsc) into the test PC and sign in a remote session; start the assistant inside the session with protection on. | Window visible in the remote session; record outcome. | NOT TESTED | REM-4.png | Needs Windows Pro for the host; mark BLOCKED on Home. |
| REM-5 | Confirm the configuration without reading code: ask the engineering owner to confirm the build contains the setting `AllowWindowCaptureExclusionInRemoteSessions` in the disabled list (`apps/desktop/src/main/index.ts:97`) and that the guard test passes. | Confirmed in writing. | NOT TESTED | REM-5-confirmation.txt | The visual result in REM-2 is the real confirmation. |

### Area 10. Unsupported Windows versions

| ID | Step | Expected result | Result | Evidence to attach | Notes |
|---|---|---|---|---|---|
| UNS-1 | On a real machine or virtual machine running Windows 10 earlier than version 2004 (build below 19041, for example 1909 build 18363): install the app, sign in, open Settings. | The capture-privacy section shows its explanation; the button "Turn on…" is disabled; the status line reads "Not available on this device. It needs Windows 10 version 2004 (build 19041) or later. The app works normally and the window is shown as usual." | NOT TESTED | UNS-1.png | Record build from `winver`. |
| UNS-2 | On that machine use the app normally: sign in, open pages, start a session. | Behaviour unchanged. No banner, no black window. | NOT TESTED | UNS-2.png | |
| UNS-3 | On that machine capture the assistant with Snipping Tool. | Window visible (the app deliberately does not use the black-window fallback). | NOT TESTED | UNS-3.png | |
| UNS-4 | Edit a copy of `settings.json` (Area 4 method) to hold `capturePrivacy: true` with a valid consent record, start the app on the old build. | Banner (warning style) with the "Not available on this device..." message; nothing applied; app works. | NOT TESTED | UNS-4.png | Optional. |
| UNS-5 | No old machine available: no run-time switch to fake the Windows version exists in the app and none should be added for this plan. A virtual machine image of Windows 10 1909 (or older) is the supported way to test. If neither is possible mark BLOCKED. The unsupported logic is covered by automated tests with a mocked platform and release (`capture-privacy.test.ts`, "unsupported platform and native failure"), which do not prove real-OS behaviour. | BLOCKED with reason, or as UNS-1 to UNS-3. | NOT TESTED | UNS-5.txt | |
| UNS-6 | Other platforms are not in scope (Windows only product). | Not tested. | NOT TESTED | | Not applicable unless the owner requests it. |

### Additional rows

| ID | Step | Expected result | Result | Evidence to attach | Notes |
|---|---|---|---|---|---|
| ADD-1 | Protection on. Look at the taskbar. | The assistant's taskbar button is visible. | NOT TESTED | ADD-1.png | |
| ADD-2 | Protection on. Look at the system tray (including the hidden icons arrow) and right-click the tray icon. | Tray icon present; menu opens. | NOT TESTED | ADD-2.png | |
| ADD-3 | Protection on. Press Ctrl+Shift+Esc and look at Task Manager (Processes tab, and Details tab). | "Interview Assistant" processes are listed. | NOT TESTED | ADD-3.png | |
| ADD-4 | Protection on. Use the Alt+Tab switcher. | The app is listed (record; the setting is expected not to change this). | NOT TESTED | ADD-4.png | The thumbnail may appear blank; record it. |
| ADD-5 | Protection on. With a test account and test setup, turn on audio input (native consent), press Start, speak a test phrase, then Stop. | Listening indicators show; tray tooltip changes; transcript works as without the setting. | NOT TESTED | ADD-5.png | Use the audio steps of `DESKTOP_MANUAL_CHECKLIST.md`. |
| ADD-6 | Protection on. Turn on screenshot input (native consent), capture a test screen using the 3-second countdown, check the preview. | Countdown and preview behave as in `DESKTOP_MANUAL_CHECKLIST.md`; the captured image content is what the user sees. Record whether the assistant window is in that captured image. | NOT TESTED | ADD-6.png | The assistant's own screenshot feature uses Electron capture; the earlier test (section 0) suggests the window is excluded when protection is on. Record it. |
| ADD-7 | Keyboard only (no mouse): Tab to the capture-privacy section, press **Turn on…** with Enter or Space, answer the native dialog with the keyboard, then Tab to **Turn off** and press it. | Every step is possible; a visible focus indicator is shown on the button; focus is not lost after the dialog closes. | NOT TESTED | ADD-7.mp4 | |
| ADD-8 | Turn the setting on and use the app for 10 minutes (answers, navigation). | No new errors, no flicker of the window; the app works normally. | NOT TESTED | ADD-8.txt | |

## 4. What automated tests already cover

These prove the code makes the calls and enforces the rules with a mocked or Electron-free environment. They do **not** prove any capture tool honours the setting.

Run on 2026-11-23 from `apps/desktop`: `npx vitest run tests/capture-privacy.test.ts tests/capture-guards.test.ts` (does not launch the app). Exact result: **Test Files 2 passed (2); Tests 70 passed (70)**; duration 1.98 s. (This run reports the combined count only; the per-file count was not separated. `docs/WINDOW_CAPTURE_PRIVACY.md` states 52 tests for `capture-privacy.test.ts`.)

`apps/desktop/tests/capture-privacy.test.ts` asserts:

- Support detection: Windows build parsing; supported on win32 build 19041 and later only.
- Default and explicit activation: off by default with no native call from construction, `state()` or `reapply()`; refusal before acknowledgement without asking or calling Windows; declined confirmation changes nothing; turning on only after acknowledgement and confirmation, saved, status `requested` and never "protected"; no second confirmation while one is open; repeated on does not repeat the dialog.
- Explicit deactivation: saves off first then asks Windows to stop; stays off afterwards; a removal Windows cannot confirm is reported and still saved as off; off with no window.
- Window recreation, crash recovery, layout and display changes: re-apply to a recreated window; destroyed windows never touched; re-apply after reload and on show/restore; no activation if the user never chose it; compact and expanded and display-change sequences.
- Unsupported platform and native failure: unsupported reporting with no native calls; failure while turning on, on re-apply, read-back mismatches, and save failure with removal.
- Persistence and restart with the real settings file: default file loads off; saved choice survives restart; off survives restart; only an exact boolean `true` counts; unrelated settings preserved.
- Dedicated consent record: version and time recorded and survive restart; withdrawal deletes the record; a saved "on" without a valid current consent loads as off (fail closed); a consent record alone does not turn it on; the store refuses to save "on" without consent; a stale choice is never re-applied.
- IPC: default off; refusal while nobody accepted the notice; non-boolean rejected; turning off without a prompt; untrusted sender refused on both channels; no manager reports "not available".
- Source guards: only `main/capture-privacy.ts` calls `setContentProtection`; nothing hides the taskbar, menu bar or tray or prevents listing; the setting is enabled only by the IPC handler (startup, recovery, display and update paths only re-apply); the renderer shows the indicator on every page and never claims invisibility; the remote-session switch is present; the setting button states the explanation first and is disabled when unsupported; the acknowledgement wording mentions the setting.

`apps/desktop/tests/capture-guards.test.ts` asserts (screenshot and preload boundary, which M-15 relies on and extended by two channels):

- Screenshot capture can start only from the one gated path: `desktopCapturer` used only by the capture adapter, adapter imported only by the main entry point, capture invoked only from `CaptureService` after the countdown; no hotkey, timer, display-media or background capture; no images or text written to disk, clipboard or log; the renderer never imports Electron.
- The preload bridge exposes exactly the approved channels (including `app:get-capture-privacy` and `app:set-capture-privacy`) and no generic invoke; the audio and capture bridges stay narrow.
- Electron hardening unchanged: strict CSP, context isolation, sandbox, denied permissions.
- Settings defaults: screenshot and audio input off by default; older settings files load with them off; non-boolean values rejected.
- Phase 11.5 session creation adds no browser launch, generic API channel, logging or storage of form content.

Other evidence (not tests of this plan): the real-Windows `desktopCapturer` measurement in `docs/WINDOW_CAPTURE_PRIVACY.md` section 10; the D-80 addendum baseline of the full suite (`docs/DECISIONS.md`), which was not re-run for this document.

## 5. Sign-off

M-15 stays at **AUTOMATED-TESTED** until the owner attaches the following. Evidence files belong in the owner's own folder, not the repository.

1. The signed approval block in section 1c for the acknowledgement v2 and M-15 consent wording (approved as written, or the approved changes applied, versions raised, and tests updated).
2. Area 1 (indicator): IND-1, IND-2, IND-3, IND-4 results.
3. Area 2 (consent): CON-1 to CON-8 and CON-10 results with the native dialog screenshots.
4. Area 3 (withdrawal): WDR-1 to WDR-4 and WDR-6 results (WDR-5 may remain BLOCKED with a reason).
5. Area 4 (restart): PERS-1, PERS-2, PERS-5 and PERS-6 results.
6. Area 5 (recreation): WIN-1 to WIN-3 and WIN-4 (or BLOCKED with the reason).
7. Area 6 (capture methods): at least Snipping Tool, Win+Shift+S, Xbox Game Bar, PrintScreen, OBS (both capture methods), and one conferencing app (window and full-screen share), each with protection on and off. Results are recorded per tool as they are; a tool that shows the window is a valid result and must be listed as a limitation, not a failure of the plan.
8. Area 7 (monitors): MON-1 to MON-5 on two real monitors, or BLOCKED with the reason recorded.
9. Area 8 (screen reader): SR-1 to SR-4 with Narrator, and NVDA if available.
10. Area 9 (remote support): REM-1 to REM-3 showing the window **visible** to the remote viewer with protection on. A result where it is hidden would contradict the owner decision and must be reported before anything else.
11. Area 10 (unsupported Windows): UNS-1 to UNS-3, or BLOCKED with the reason (no pre-2004 machine or VM).
12. Additional rows ADD-1 to ADD-3, ADD-5 to ADD-7.
13. The owner's decisions on the review notes in section 2 (at least notes 1, 3, 4, 8 and 14), recorded as accepted or as requested changes.

Moving a status to **MANUALLY-TESTED** is allowed only for the specific tools, Windows build and rows that were actually run. Anything not run stays NOT TESTED or BLOCKED. The words "protected" or "invisible" must not be used in any report of these results.

Owner sign-off for moving beyond AUTOMATED-TESTED:

Name: ______________________________ Date: ______________________________ Signature or approval reference: ______________________________
