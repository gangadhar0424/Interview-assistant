# Window Capture Privacy (M-15, merges M-14)

**Current status (2026-11-24, D-84): IMPLEMENTED and MANUALLY VALIDATED (owner-confirmed).** Implementation is complete and automated testing is complete as previously reported. The owner reports having run the manual validation and that the feature works successfully. That is one overall confirmation: individual rows, capture tools, Windows versions and evidence files were not reported and are not recorded here, so no specific tool (including any conferencing application) is claimed as tested. Every limitation in this document stands unchanged: the setting is a request to Windows, not a guarantee and not invisibility; remote-session exclusion stays disabled; capture paths that ignore the flag are not covered. Code, consent wording and behaviour were not changed by this status update. The dated status lines below are kept as history.

Status (2026-11-22): **IMPLEMENTED and AUTOMATED-TESTED; one capture tool verified on real Windows; the rest of the manual capture matrix is NOT TESTED.** Decision record: D-80. Owner authorisation: the 2026-11-22 milestone directive (M-13 to M-15), which carries the security-review conditions listed in section 8.

## 1. Purpose

An optional, off-by-default setting that asks Windows to leave **this app's own window** out of supported screen-capture operations. Legitimate uses: keeping private material (a resume, notes) from showing in a screen share the user is allowed to make, and accessibility/privacy preference.

It is **not** a stealth feature. It must not be used to avoid monitoring or to break the rules of an interview, meeting or assessment. The acceptable-use acknowledgement (wording version 2) says so and must be accepted before the setting can be turned on.

## 2. User experience

- **Settings, "Hide this window from screen capture":** the full explanation (four paragraphs, section 6) is always visible before the button. The button reads **Turn on…** (disabled on unsupported devices) or **Turn off**.
- **Turning on** needs, in the main process: (1) the signed-in user has accepted the current acceptable-use notice; (2) a native confirmation dialog that repeats the explanation (a compromised renderer cannot fake or skip it). Declining changes nothing.
- **Indicator:** while it is on, a banner on **every page** says the window was requested to be left out of supported captures and that this is not guaranteed. It cannot be dismissed. If the request fails the banner switches to a warning instead (it never claims protection that is not there).
- **Statuses:** `off`, `requested` (Windows was asked and the window reports the flag), `unsupported` (platform or Windows build), `failed` (the call threw or the window did not report the flag). In every status the app works normally.
- **Not changed:** the taskbar entry, the tray icon and menu, the process, the listening and capture indicators, the screenshot countdown, audio consent. Nothing in the app hides them (guard-tested).

## 3. API and Windows behaviour

- **API:** Electron `BrowserWindow.setContentProtection(enable)`, with `isContentProtected()` read back. No native module, no new dependency. Pinned Electron: **45.2.3** (`apps/desktop/package.json` `^45.2.3`).
- **What Electron documents for Windows** (from the installed `electron.d.ts` 45.2.3): it calls `SetWindowDisplayAffinity` with `WDA_EXCLUDEFROMCAPTURE`; on Windows 10 version 2004 and later the window is removed from capture entirely; on older versions it behaves like `WDA_MONITOR` (a black window); the change takes effect with the **next desktop composition**, so a capture started immediately after the call can still contain the window.
- **Minimum supported by this app: Windows 10 version 2004, build 19041.** Older builds are reported `unsupported` and the call is **not made** (this app deliberately does not use the black-window fallback). macOS and Linux: `unsupported` (M-15 is a Windows feature; Electron's macOS behaviour is not claimed).
- **`isContentProtected()` is Electron's record of the flag**, not an OS proof and not proof that any given tool honours it. The manager treats a mismatch as `failed`; a match is reported only as `requested`, never as "protected" or "hidden".
- **Remote sessions:** Electron documents that protection by default also applies in a Windows remote session, where it hides the window from remote-access software that captures the desktop. This app **disables that behaviour** (`app.commandLine.appendSwitch("disable-features", "AllowWindowCaptureExclusionInRemoteSessions")`, before `ready`), so remote-access and remote-support tools still see the window. Reason: the app must not conceal itself from someone entitled to monitor the device. Guard-tested (presence of the switch); **not verified in a real remote session**.

## 4. Where it is applied (and where it is not)

The only code that calls `setContentProtection` is `apps/desktop/src/main/capture-privacy.ts` (a test fails if any other source file does). The user's *saved* choice is re-applied (never newly enabled) by `reapply()` on: window creation, `did-finish-load` (covers renderer crash reload, M-5), window `show` and `restore`, and display added/removed/metrics changes (M-6). Layout changes (compact/expanded) resize the same window object, so the flag persists; the re-apply on show/display events is idempotent. `enable()` is called from exactly one place, the IPC handler `app:set-capture-privacy`.

## 5. Persistence and "no automatic activation"

- Stored as `capturePrivacy: true` in `settings.json` (per device, written only when on; absent means off; any value other than the boolean `true` loads as off).
- Nothing turns it on by itself: not first run, update, restart, window recreation, crash recovery or a display change. A restart restores only a choice the user made earlier, and the indicator is shown again.
- It never starts audio or screenshot capture, and it does not change how either is gated.

## 6. User-facing text (source of truth: `src/shared/capture-privacy.ts`)

1. When on, the app asks Windows to leave the window out of screen capture that supports the request. For private material and accessibility.
2. Only this window is affected. Still in the taskbar and tray, still shows indicators, still listed as running. In a remote session the window is not excluded.
3. Not a guarantee: some capture methods, other screens, cameras, and security, proctoring or monitoring software may still see it. Must not be used to avoid monitoring.
4. The user remains responsible for the rules of any interview, meeting or assessment.

A test forbids words such as "invisible", "undetectable" and "guaranteed to" in this text.

## 7. Limitations

- Works per capture tool; tools that do not honour display affinity (a phone camera, an external capture card, some drivers, some remote-control and monitoring software) will still show the window.
- Effect is not instantaneous (next composition).
- Verified here only with Electron's own `desktopCapturer` (section 10). Snipping Tool, Game Bar, a conferencing app's screen share, `PrintScreen`, a Graphics Capture recorder and a Windows build below 2004 are **NOT TESTED**.
- Multi-monitor and DPI behaviour is not tested on real hardware; only a single display was available.
- The app cannot tell whether a given interview or assessment allows assistance.

## 8. Security analysis

| Threat | Control | Residual |
|---|---|---|
| Used to conceal the app from a party entitled to monitor | Off by default; acknowledgement v2 (naming this setting) required in main; native confirmation; non-dismissible in-window indicator; tray, taskbar and process untouched; remote-session exclusion disabled; wording forbids invisibility claims | The setting still works against ordinary screen sharing by design. That is the purpose and the owner's recorded decision (D-80). The product cannot verify intent. |
| Compromised renderer turns it on | Enable path is main-only: IPC validates a boolean, requires acknowledgement and a native dialog | None identified |
| Silent re-enable after restart/recovery | Only a saved explicit choice is re-applied; tests prove `enable()` is called from one place and `reapply()` never turns it on | Settings file edited by a local attacker can set it on (local attacker already controls the profile; the indicator still shows) |
| False sense of protection | Status never says "protected"; failure shows a warning | Users may still over-trust it; wording discourages this |
| Native failure leaves a half state | Failure or read-back mismatch reports `failed`, saves nothing, attempts removal; save failure removes protection again | Removal failure is reported with a restart hint |

Security-review conditions carried over from `SECURITY_REVIEW.md` (M-14): off by default (met), explicit label of what it does and the rules reminder (met), visible indicator (met), never combined with hiding the taskbar/tray (met), no evasion or detection of monitoring (met), included in the M-1 acknowledgement (met, version 2), documented in PRIVACY.md (met).

## 9. Accessibility

The banner is a `role="status"` region; the settings section is a labelled region; the button is a native `<button>` with `aria-describedby` pointing at the explanation; status and errors are announced (`role="status"` / `role="alert"`). Protection does not affect Windows accessibility APIs in any way this app changes (no accessibility feature is touched); **screen-reader behaviour was not tested manually.**

## 10. Test results

Automated (`apps/desktop/tests/capture-privacy.test.ts`, 52 tests; mocked window, platform and settings port plus the real settings file and IPC registration): default off and no native call from construction/`state()`/`reapply()`; explicit activation (acknowledgement required, declined, concurrent dialog, idempotent); explicit deactivation (including removal failure and read-back mismatch); window recreation and destroyed windows; crash-reload and show/restore re-apply; no activation without a saved choice; compact/expanded and display-change sequences; unsupported platform, old Windows build and unparseable release; native failure on enable, re-apply and disable; read-back mismatch; save failure; persistence and restart with the real file; damaged values load as off; unrelated settings preserved; IPC (default, no-acknowledgement refusal, non-boolean rejection, untrusted sender, no manager); source guards (single caller of the native API, taskbar/tray untouched, one enable call site, indicator on every page and not dismissible, no invisibility wording, remote-session switch, acknowledgement wording). Full desktop suite after the change: see PRODUCTION_ROADMAP M-15.

Real Windows, 2026-11-22, Windows 11 build 10.0.26300, Electron 45.2.3, a throwaway Electron script (not part of the repository) that opened a 320x320 solid-magenta frameless window and counted magenta pixels in a **memory-only** `desktopCapturer` screen capture (nothing saved; only the test window analysed):

| Window | Protection off | Protection on | Off again | `isContentProtected()` on / off |
|---|---|---|---|---|
| opaque | 104,284 px | **0 px** | 104,284 px | true / false |
| transparent, frameless (like the app) | 106,276 px | **0 px** | 106,275 px | true / false |

Meaning: for Chromium/Electron's own screen capture on this build, protection excluded the window, including a transparent frameless one, and turning it off restored it. It says nothing about other tools.

## 10a. Dedicated consent record (added 2026-11-22)

Consent to this setting is separate from the acceptable-use notice. Turning on requires, in the main process: the acceptable-use notice accepted, **and** an affirmative native dialog ("I understand, turn on") that repeats the explanation. On success the app stores `capturePrivacyConsent = { version, acceptedAt }` next to `capturePrivacy: true` in `settings.json` (device-local, never sent anywhere). Rules, all tested:

- The saved "on" counts only with a valid consent record for the **current** `CAPTURE_PRIVACY_CONSENT_VERSION` (1). A missing, damaged, stale or future-version record loads as **off** and is never re-applied (fail closed). A consent record without the flag does nothing.
- The store refuses to save "on" without a valid consent.
- Turning off (withdrawal) deletes the record.
- A settings file written by the first M-15 build (flag without consent record) therefore loads as off; the user turns it on once more. This is intended.
- Raising the consent version makes every saved "on" stop applying until the user consents to the new wording. The wording of the acceptable-use notice (version 2) and of this consent text still need **owner approval** before they are final.

## 10b. Competitor capability comparison (public sources only; 2026-11-22)

Method: web searches only. No product was installed, run or reverse-engineered. Almost all sources are vendor marketing, competing vendors' blogs, aggregator listings and open-source clones, so every technical claim is secondhand.

| Product | Publicly claimed privacy behaviour | Mechanism publicly documented? | Activation / consent | Stated limits | Accessibility |
|---|---|---|---|---|---|
| Cluely | Marketed as invisible during screen sharing. Third-party articles speculate about GPU-level overlays or manipulating share output. | **No** official documentation found. Articles and open-source clones name `WDA_EXCLUDEFROMCAPTURE` (Windows) and `NSWindowSharingNone` (macOS), but none confirms Cluely uses them. | Not found | One open-source project notes newer conferencing software may detect such windows | Not found |
| Parakeet AI | "Undetectable" and "invisible on screen share" per vendor listings; its FAQ reportedly recommends window-filtered capture in Zoom | Not found | Not found | Hidden only if the user shares a window rather than the whole screen (per competitors' blogs); native process visible in Activity Monitor (user report, FAQ reportedly says the process name cannot be changed); user feedback reports it appearing in a share; reports of failures mid-session | Not found |
| Clear My Round AI | **No relevant results found** for this product. Unverified. | Not found | Not found | Not found | Not found |

What cannot be verified: whether any of them uses a documented Windows API, what consent they ask for, whether their indicators exist, and any accessibility behaviour. No claim of parity is made. What our own design does differently, by owner requirement: off by default; separate recorded consent; non-dismissible in-window indicator; tray, taskbar and process left visible; remote-session exclusion disabled; wording that forbids "invisible"/"undetectable" claims and tells users to follow interview rules. The competitors' marketing (undetectable use in interviews) is the use this app's design is meant **not** to enable.

Sources: web search results on 2026-11-22 (e.g. hyring.com guide on Cluely, Analytics Vidhya article on Cluely, ToolRadar and G2 listings for Parakeet AI, parakeetai.featurebase.app user reports, finalroundai.com and interviewcoder.co competitor blogs, vervecopilot.com). Search snippets were used; the pages were not individually fetched or archived.

## 10c. Remote-session evaluation (owner decision unchanged)

- **Electron documents** (installed typings, 45.2.3): protection by default also applies in a Windows remote session; a Remote Desktop client still shows the window to the remote user, but remote-access software that works by capturing the desktop cannot. It can be disabled with the Chromium feature `AllowWindowCaptureExclusionInRemoteSessions`, and `isContentProtected()` still returns `true` in that case.
- **Current app behaviour (owner decision, D-80):** the feature is disabled, so the window is **not** excluded in remote sessions. Remote-support and remote-monitoring tools that capture the desktop see it.
- **Tested on a real remote session: NOT TESTED.** Only the documented behaviour and the presence of the switch (guard test) are established.
- **Trade-off:** leaving it disabled keeps the window visible to legitimate remote support, IT management and remote monitoring, which the owner requires; the cost is that a user who also works over a remote session gets no capture privacy there. Enabling the exclusion would add concealment from remote-access tools, which conflicts with the transparency boundaries. **Recommendation: keep it disabled.** No change has been made and none will be without owner approval.

## 11. Manual matrix (owner to fill; none of these has been run)

For every row record: Windows build, Electron version (45.2.3 unless the build changes), capture mechanism, environment, expected, actual, evidence (screenshot or recording file name, kept off-repo), known limitations.


| Tool | Off: window visible | On: window excluded | After restart | After forced renderer crash | Compact | Expanded | 2nd monitor |
|---|---|---|---|---|---|---|---|
| Snipping Tool | | | | | | | |
| Xbox Game Bar capture | | | | | | | |
| `PrintScreen` | | | | | | | |
| A conferencing app screen share | | | | | | | |
| A Graphics Capture based recorder | | | | | | | |
| Remote Desktop / remote-support tool (expect: window **visible**) | | | | | | | |
| Windows build below 2004 (expect: setting unavailable) | | | | | | | |
| Screen reader (Narrator/NVDA): Turn on, banner announced, Turn off by keyboard only | | | | | | | |
| Consent withdrawal (Turn off, restart: stays off, record gone) | | | | | | | |
| Display add/remove and DPI change while on | | | | | | | |

Also check: tray icon and taskbar entry present; indicator visible on every page; Turn on is refused before acknowledgement; Turn off restores capture; setting still on after restarting the app (with indicator).

## 12. Failure behaviour

Unsupported device: setting disabled and explained, app normal. `setContentProtection` throws or reads back wrong: status `failed`, nothing saved, removal attempted, warning shown, app normal, retry possible. Cannot save the choice: protection removed again and an error shown. Cannot confirm removal on Turn off: saved as off, user told to restart. No window (startup race): `failed`, nothing saved. Diagnostics: none recorded with content; the diagnostics event list is unchanged.


## 13. Owner approval package and manual validation preparation (2026-11-23, D-82)

**Nothing about the feature, its wording or its owner decisions changed.** The package is `docs/M15_OWNER_APPROVAL_AND_TEST_PLAN.md`:

1. **Wording for approval**, copied character for character from the source with file and line references: the acknowledgement version 2 (`ACCEPTABLE_USE_VERSION = 2`) and the dedicated M-15 consent (`CAPTURE_PRIVACY_CONSENT_VERSION = 1`), with an approval block (approved as written, approved with changes, rejected). A check of every quoted string of 30 characters or more in the two shared source files found all 19 in the document verbatim. If the owner approves a change, the version constant must be raised so existing users consent again, and the tests that assert the wording updated.
2. **Review notes (15, none acted on).** The ones that bear on transparency and accessibility: the indicator's lead words ("Hidden from screen capture:") state an outcome while the rest says requested and not guaranteed; the acknowledgement and consent make slightly different claims about monitoring software; the acknowledgement is shown only in the Audio and Screenshot panels, so the Settings section gives a refusal with no pointer to it; a stale or missing consent loads as off with no explanation; the consent and status text is 8 px; the declined confirmation is announced as an alert; the failed-but-enabled state offers "Turn on" instead of "Turn off"; the persistent indicator is in-window only (nothing shows when the window is in the tray); the explanation omits that the effect is not instant, varies by tool and Windows build, and does not affect a screenshot already taken. These are the owner's to decide because they touch consent wording and presentation.
3. **Manual validation plan**, ten areas as tables with steps, expected result, a result column pre-filled NOT TESTED and the evidence to attach: persistent indicator, consent acceptance and rejection, withdrawal, restart, window recreation, Windows capture methods (Snipping Tool, Win+Shift+S, Game Bar, PrintScreen, OBS window and display capture, conferencing apps, remote-support tools), multiple monitors, screen readers (Narrator and NVDA), remote-support visibility, unsupported Windows versions, plus rows for the taskbar and tray entry, the process in Task Manager, unaffected audio and screenshot features, and keyboard-only operation.
4. **Sign-off section** listing what the owner must attach before M-15 may move beyond AUTOMATED-TESTED.

**Status after this preparation:** IMPLEMENTED and AUTOMATED-TESTED (`capture-privacy.test.ts` and `capture-guards.test.ts`, 70 tests passing in the sprint run). MANUALLY-TESTED: one capture mechanism only (section 10). Every row of section 11 and of the new plan is **NOT TESTED**. The setting is still described only as a request to Windows; no universal capture prevention is claimed and none must be.

**Related security finding.** SEC-24 (the `grantFileProtocolExtraPrivileges` Electron fuse) is best checked during the same packaged-app session: build a package with the fuse off and confirm the window still loads and renders. It was not changed because it was not tested.

## 14. Finalisation preparation (2026-11-23, D-83)

Nothing about the feature, its wording or the owner decisions changed. Added for the owner:

* **Proposed revision (not applied): `docs/M15_PROPOSED_REVISION.md`.** Addresses the documented concerns: indicator wording that states an outcome; the 8 px consent text (the muted text colour fails the 4.5:1 contrast threshold on bright backgrounds, 3.86:1 and 3.98:1, and passes on a dark one, 4.85:1, by the document's own calculation from the CSS); users who decline in Settings not being pointed at the explanation; an unexplained "stale consent" switch-off; withdrawal and the failed-but-enabled state. It proposes raising `CAPTURE_PRIVACY_CONSENT_VERSION` from 1 to 2 (keeping `ACCEPTABLE_USE_VERSION` at 2), so every existing user would consent again to the new wording. Two choices are the owner's: where a lapse notice is stored, and whether a tray hint is added. It needs your approval before any code or wording changes.
* **Manual session runbook: `docs/M15_MANUAL_SESSION_RUNBOOK.md`.** Order, preparation, stop conditions and what to hand back. Every row of the plan stays NOT TESTED until you run it.
* **Live check of a real packaged build (new evidence, not a capture test).** An unpacked build was launched with an isolated profile and inspected through DevTools: the renderer has no Node or Electron globals and the bridge exposes only named functions; navigating away, `window.open`, external fetches and images, local-file fetches and injected inline scripts were each blocked (the renderer logged the CSP violations); camera, microphone (not armed) and `getDisplayMedia` requests were denied; a fresh profile reports screen-capture privacy **off** with no consent record; the renderer cannot turn it on without the acknowledgement and the native confirmation (a non-boolean is refused as invalid input). This verifies the protections around the setting; it does not test what any capture tool sees.
* **Fuse note (SEC-24).** `GrantFileProtocolExtraPrivileges` must stay enabled in the current design: a build with it disabled did not render. This does not affect the capture setting.

Status: IMPLEMENTED and AUTOMATED-TESTED; the packaged-app checks above are ENVIRONMENT-VERIFIED for isolation and defaults only; every capture, monitor, screen-reader, remote-support and unsupported-Windows row is **NOT TESTED**.

