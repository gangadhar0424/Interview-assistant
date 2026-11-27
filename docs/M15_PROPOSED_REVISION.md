# M-15 Proposed Revision of Screen-Capture Privacy Wording and Behaviour (PROPOSAL, not applied)

Prepared 2026-11-23 for the product owner. **This is a proposal. Nothing in it has been applied to code, tests or any existing document. Nothing may be applied until the owner approves each point in section 9.** It answers the review notes in `docs/M15_OWNER_APPROVAL_AND_TEST_PLAN.md` section 2 (notes 1, 3, 4, 5, 6, 7, 8, 14, 15 directly; 11 in part). The wording is a policy statement for the owner to approve, not legal advice.

Evidence labels: **Verified** = read in this session from the file and line given. **Not known** = not read, not measured or not tested in this session. No manual or screen-reader test was run for this document. Line numbers are from the working tree on 2026-11-23, which has uncommitted changes, so they may move.

## 0. Owner decisions that stay unchanged (restated)

1. Remote-session exclusion stays **disabled** (`AllowWindowCaptureExclusionInRemoteSessions`). Remote-access and remote-support tools keep seeing the window. (Verified: stated in `M15_OWNER_APPROVAL_AND_TEST_PLAN.md` section 0, D-80 in `docs/DECISIONS.md:684`. The code line `index.ts:97` quoted by the plan was not re-opened here.)
2. Remote-support visibility stays **enabled**. The assistant is not concealed from anyone entitled to monitor the device.
3. The feature is only ever described as a **request to Windows**. No text may say or imply that the window is hidden, protected, invisible, or that capture is prevented. The existing source guard (`capture-privacy.test.ts:660-662`, Verified) forbids `invisible|undetectable|cannot be seen|untraceable|100%|guaranteed to` and requires "not a guarantee".
4. **No concealment** of the process, the tray icon or the taskbar entry. Every proposal below only adds visibility. The existing guard tests for this (`capture-privacy.test.ts:627-631`, Verified) stay as they are.
5. **Accessibility must be maintained.** Each point has its own accessibility rationale. Nothing proposed removes a label, a role, or keyboard access.
6. The setting stays off by default, is turned on only by an explicit user action in Settings behind the acknowledgement and a native confirmation, and a saved "on" without a valid current consent record loads as off. No proposal weakens this.

## 1. Point 1: Replace outcome-claiming indicator text

### 1.1 Problem

- Banner text, `apps/desktop/src/shared/capture-privacy.ts:57-58` (Verified):
  `Hidden from screen capture: this window has been requested to be left out of supported captures. Not guaranteed.`
  It is rendered at `CapturePrivacyPanel.tsx:35` inside a `role="status"` region labelled "Screen capture privacy" (`:30-34`). The first words state an outcome ("Hidden"). The rest says "requested ... Not guaranteed", so the sentence contradicts itself. A user, or a person looking at a shared screenshot of the banner, can read only the first words. This conflicts with the file's own header (`capture-privacy.ts:10-12`: "must never claim").
- Settings section title, `capture-privacy.ts:48` (Verified): `Hide this window from screen capture`. Rendered as the heading at `CapturePrivacyPanel.tsx:56` and used as the native dialog title and, with a "?", the dialog message (per plan section 1b; `index.ts:268-282` not re-opened). It is an imperative outcome ("Hide"). The same words are the accessible name that a screen-reader user hears and that the toggle sits under.
- Toggle labels, `CapturePrivacyPanel.tsx:78` and `:88` (Verified): `Turn on…`, `Turn off`, `Working…`, `Waiting for confirmation…`. They do not say what is being turned on. With the heading as the only context, "Turn on…" read alone (for example in a screen-reader button list) does not say what it turns on.
- Status for `unsupported` and `failed` in the banner reuse the Settings messages (`CapturePrivacyPanel.tsx:35`), which are fine in meaning but are not led by a short status word.

### 1.2 Proposed replacement

Settings section title and native dialog title (`CAPTURE_PRIVACY_TITLE`):

```
Ask Windows to leave this window out of screen capture
```

Native dialog message (replaces `${CAPTURE_PRIVACY_TITLE}?`):

```
Send this request to Windows?
```

Native dialog buttons (replace `I understand, turn on` / `Cancel`; Cancel stays the default and the Escape result):

```
Yes, send the request
Cancel
```

Settings buttons:

```
Turn on request…        (was: Turn on…)
Turn off request        (was: Turn off)
Waiting for confirmation…   (unchanged)
Turning off…            (was: Working…)
```

Settings status line, one per status (`CAPTURE_PRIVACY_MESSAGES`):

```
off:
Off. Windows has not been asked to leave this window out of screen capture.

requested:
On. Windows was asked to leave this window out of screen capture. This is not a guarantee. The app cannot check whether a tool follows the request.

unsupported:
Not available on this device. It needs Windows 10 version 2004 (build 19041) or later. The app works normally, and this window may be captured as usual.

failed:
Windows did not accept the request. This window may be captured as usual. You can try again, or turn the request off.
```

Indicator banner text, one per status (`CAPTURE_PRIVACY_INDICATOR` becomes a per-status map `CAPTURE_PRIVACY_INDICATORS`):

```
off:
(no banner is shown. The Settings status line above is the only text.)

requested:
Request sent: Windows was asked to leave this window out of screen capture. Not guaranteed. Some tools may still capture it.

failed:
Request not accepted: Windows did not accept the request, so this window may be captured as usual. Open Settings to try again or turn it off.

unsupported:
Request not active: this version of Windows cannot do this, so this window may be captured as usual.
```

The "off" banner stays absent on purpose. Showing a banner on every page for a setting the user never used would be noise. "Unsupported" can only show in the banner when a saved "on" loads on a device that cannot do it (row UNS-4 of the plan).

Explanation paragraphs (`CAPTURE_PRIVACY_EXPLANATION`). Same four ideas as today, shorter sentences, plus one new paragraph for the limits listed in review note 11. Statement text that the tests look for is kept (see 1.5):

```
1. When this is on, the app asks Windows to leave this window out of screen capture. Many screen-sharing and recording tools follow the request. You might use it to keep private notes from showing in a screen share that you are allowed to make. It can also help with accessibility.

2. The request covers only this window. The app still shows in the taskbar and the system tray. It still shows its listening and capture indicators. It is still listed as a running program. In a Windows remote session the window is not left out, so remote-support tools can still see it.

3. This is not a guarantee. Some tools ignore the request. Other screens, cameras pointed at your display, and security, proctoring or monitoring software may still see this window. You must not use this setting to avoid monitoring.

4. Windows may take a few seconds to apply a change. A screenshot or recording made earlier is not affected. This choice is saved only on this device.

5. You are still responsible for following the rules of any interview, meeting or assessment where you use this app. If those rules do not allow AI assistance, do not use it there.
```

The sentence "It does not and must not be used to avoid monitoring" (review note 10) is replaced by "You must not use this setting to avoid monitoring" so "it" no longer points at the wrong thing. Paragraph 4: the timing and the "saved only on this device" claim are taken from the plan's review note 11, which cites `WINDOW_CAPTURE_PRIVACY.md` section 3 and `PRIVACY.md`. **Not known** here: those two sources were not re-read in this session. The owner or engineer must confirm both before approval.

### 1.3 Rationale

The first words of a status are what people read and what a screenshot of the banner shows. "Request sent" and "Request not accepted" say what the app did, not what Windows did. "Ask Windows to leave this window out of screen capture" names the real mechanism and the real limit in the heading itself. "May be captured as usual" is used for failed and unsupported because the app cannot see what other tools do. The word "hide" no longer appears in any heading, button or banner.

### 1.4 Accessibility rationale

- Screen readers: the section heading, button names and banner label now carry meaning on their own. "Turn on request…" is clear in a button list. The banner keeps `aria-label="Screen capture privacy"` (Verified `CapturePrivacyPanel.tsx:31`).
- Live region: unchanged in this point (see point 3 for the live-region design and point 5 for what happens when the banner appears after load).
- Focus: no change to focus order.
- Contrast and size: the banner text uses the existing `.recovery-text` at 12px (`styles.css:308`) in `#cdeff5` on a banner tint. Calculated contrast is 10.7:1 against the lightest background (see point 2). The warning style uses `#fde9b8`, 10.9:1. No change is needed. The text is longer than today's (about 130 characters), so at 360 px width it wraps to roughly four lines. **Not known:** exact wrap count; check in the compact layout.
- Meaning is not carried by colour alone: the leading status words ("Request sent", "Request not accepted", "Request not active") carry it.

### 1.5 Code and test changes if approved

Files (list only, not done):

- `apps/desktop/src/shared/capture-privacy.ts`: new `CAPTURE_PRIVACY_TITLE`, `CAPTURE_PRIVACY_EXPLANATION` (5 items), `CAPTURE_PRIVACY_MESSAGES`, `CAPTURE_PRIVACY_INDICATOR` replaced by `CAPTURE_PRIVACY_INDICATORS: Record<CapturePrivacyStatus, string | null>`, new `CAPTURE_PRIVACY_CONFIRM_MESSAGE`, `CAPTURE_PRIVACY_CONFIRM_BUTTON`.
- `apps/desktop/src/renderer/src/ui/CapturePrivacyPanel.tsx`: banner uses the per-status map; button labels.
- `apps/desktop/src/main/index.ts` (dialog at about lines 268-282): message and button strings come from the new constants.
- `apps/desktop/src/main/capture-privacy.ts:151-152` only in point 5.

Existing test assertions that change (`apps/desktop/tests/capture-privacy.test.ts`, Verified by grep):

- `:19` imports `CAPTURE_PRIVACY_INDICATOR`; `:657` expects the panel source to contain the string `CAPTURE_PRIVACY_INDICATOR`; `:660` joins it into the wording check. These change to `CAPTURE_PRIVACY_INDICATORS` and `Object.values(...)` (ignoring null).
- `:137-138`, `:283` use `CAPTURE_PRIVACY_MESSAGES.requested` and `.failed` by constant, so they follow the new text. `:138` needs `/not a guarantee/i`: the new `requested` message contains it.
- `:190` `/restart/i` (withdrawal failure) and `:265` `/Windows 10 version 2004/` still match.
- `:661` forbidden words: none appear in the new text (the new text uses "Not guaranteed" and "not a guarantee", which are allowed). `:662-665`, `:672`: "not a guarantee", "taskbar", "system tray", "responsible for following the rules", "remote session" are all kept. No change.
- `:677` `CAPTURE_PRIVACY_EXPLANATION.map` stays true unless point 2 moves the paragraphs into a disclosure that maps differently; see 2.5.
- New tests: every status maps to an indicator or null; no indicator starts with "Hidden"; every string in the new constants passes the forbidden-word regex; a test that the dialog message and buttons come from the constants.

### 1.6 Consent-version consequence

The title, dialog message, dialog buttons and explanation paragraphs are what the user agrees to in the native confirmation. They change, so **`CAPTURE_PRIVACY_CONSENT_VERSION` must be raised 1 to 2.** Effect: see section 8. If the owner approves only the banner and status wording and keeps the dialog text byte-for-byte, no version raise is strictly needed for this point, but the title change alone is enough to need it, so a split is not recommended.

## 2. Point 2: The 8 px consent and status text

### 2.1 Problem (Verified from CSS)

- `.settings-help`, `styles.css:208`: `font-size: 8px; line-height: 1.55; color: #76869b; margin: 5px 0 0`.
- `.inline-error`, `styles.css:189`: `font-size: 8px; padding: 0 12px; color: #ff9cac`.
- Used for: the four consent paragraphs and the status line (`CapturePrivacyPanel.tsx:59,65`), the cancel and error notice (`:93`), the acknowledgement intro and closing note (`AcceptableUseNotice.tsx:27,33`) and the acknowledgement error (`:35`), and `UpdatePanel.tsx:88`.
- For comparison, the acknowledgement list is 12px in `#d7e3ea` (`styles.css:296`) and the banner text is 12px (`styles.css:308`). So the app's legally most important explanation is its smallest text, and the acknowledgement intro and note are 8px next to a 12px list.
- Effective size: the app does not set a root font size in the file read (`:root` block, `styles.css:3-19`). Windows display scaling and the Electron zoom setting change the real size. **Not known:** actual on-screen height in points at the owner's scaling, and whether the app has a zoom control.

### 2.2 Contrast measurement (calculated from CSS values, not measured on screen)

Method: WCAG 2.x relative luminance, L = 0.2126 R + 0.7152 G + 0.0722 B on linearised sRGB channels, contrast = (L1 + 0.05) / (L2 + 0.05). A small script was used to compute all values below; the first one was also checked by hand.

What is behind the text (Verified): the text sits in `.settings-section`, `background: rgba(255,255,255,.025)` (`styles.css:206`), inside `.window-shell`, whose base is `--surface: rgba(11,18,31,.92)` with two soft radial gradients and `backdrop-filter: blur(26px)` (`styles.css:10, 28-42`). The window is transparent (`backgroundColor: "#00000000"`, `index.ts:456`, Verified), so 8% of whatever is on the desktop behind it shows through. **Not known:** the actual desktop behind it. So three cases are computed:

| Case | Resulting background (R,G,B) | How it is built |
|---|---|---|
| A: dark desktop | 16.2, 22.5, 34.2 | surface over black, plus 2.5% white section tint |
| B: bright desktop | 36.1, 42.4, 54.1 | surface over white, plus 2.5% white section tint |
| C: brightest gradient spot | 23.6, 41.4, 58.3 | cyan-blue gradient at 18% over case A base, plus section tint (an estimate of the gradient's brightest area) |

Hand check of the muted colour `#76869b` on case A:

- Channels 118, 134, 155 give linear values 0.1811, 0.2384, 0.3276. L = 0.2126 x 0.1811 + 0.7152 x 0.2384 + 0.0722 x 0.3276 = 0.0385 + 0.1705 + 0.0237 = 0.2327.
- Case A channels 16.2, 22.5, 34.2 give linear 0.00525, 0.00828, 0.01616. L = 0.001116 + 0.005922 + 0.001167 = 0.008205.
- Contrast = (0.2327 + 0.05) / (0.008205 + 0.05) = 0.2827 / 0.0582 = **4.86:1**. The script gave 4.85.

Results (ratio, text colour on background):

| Text colour (where used) | A dark | B bright desktop | C gradient spot | Meets 4.5:1 in all three? |
|---|---|---|---|---|
| `#76869b` (`.settings-help`, 8px) | 4.85 | **3.86** | **3.98** | **No** (fails B and C) |
| `#91a1b7` (`--muted`, `styles.css:16`) | 6.86 | 5.45 | 5.62 | Yes |
| `#b4c2d4` (proposed) | 9.97 | 7.92 | 8.16 | Yes (also above 7:1) |
| `#ff9cac` (`.inline-error`, 8px) | 9.10 | 7.23 | 7.46 | Yes (colour is fine; size is the issue) |
| `#d7e3ea` (`.ack-list`, 12px) | 13.80 | 10.96 | 11.30 | Yes |
| `#cdeff5` on banner tint (12px) | 14.8 (A, before banner tint) | 10.67 on tinted B | n/a | Yes |
| `#fde9b8` warning banner (12px) | 15.05 | 10.86 on tinted B | n/a | Yes |

Thresholds used (WCAG 2.2, level AA; from the published standard, not re-read from a file in this repository, so label: **external knowledge, not verified in this session**): 1.4.3 Contrast (Minimum) 4.5:1 for normal text and 3:1 for large text (at least 24 px, or 18.66 px bold); 1.4.11 Non-text Contrast 3:1 for interface component boundaries and focus indicators; 1.4.4 Resize Text: text can be enlarged to 200% without loss of content; 1.4.10 Reflow: no two-dimensional scrolling at 320 CSS px width; 1.4.12 Text Spacing; 2.5.8 Target Size (Minimum) 24 x 24 CSS px. WCAG sets **no minimum font size**, so the 8px problem is a usability and 1.4.4 problem, not by itself a 1.4.3 failure. The contrast failure of `#76869b` in cases B and C is a 1.4.3 failure. Case B and C are plausible (any bright wallpaper or a white document behind the window), so the colour should be treated as failing.

### 2.3 Proposed minimums

```
Consent paragraphs and status text:   13px minimum, line-height 1.5, colour #b4c2d4  (worst case 7.9:1)
Error and notice text:                13px minimum, colour #ffb3c0 (worst case 8.5:1) for real errors only
Acknowledgement intro and note:       13px, same colour as above (so they match the 12px list or larger)
Banner text:                          keep 12px today; raise to 13px so it is not smaller than the section text
Do not set sizes in px below 12 for any text a user must read to decide
Allow user zoom: no maximum-scale, no overflow that clips text (the page already scrolls, `.page-scroll`, styles.css:83)
```

`#ffb3c0` on cases A, B, C was computed at 10.70, 8.50, 8.76 (Verified by calculation). Everything stays well above 4.5:1. The existing `#ff9cac` also passes, so keeping it is acceptable; the main change is size.

### 2.4 Dialog or expandable region

Options:

- **Keep the native dialog as the act of consent** (it already exists, is outside the renderer so the renderer cannot fake it, and is read by Windows screen readers as a dialog). Native dialog readability for about 1,000 to 1,150 characters is **NOT TESTED** (review note 9).
- **Proposed in Settings:** show a two-sentence summary always, at 13px, and put the five paragraphs in a native `<details>` disclosure labelled "What this does and does not do". Summary text:

```
This sends a request to Windows. Some tools may ignore it, so it is not a guarantee. The app stays in the taskbar and tray, and stays visible to remote support.
```

```
What this does and does not do
```

- A custom in-app modal dialog is **not** recommended. It would add a focus trap, Escape handling and inert-background work for no gain, because the native confirmation already exists.

Why not hide the full text behind the disclosure only: the consent that matters is given in the native dialog, which always contains all five paragraphs. The Settings disclosure is for reading ahead. The summary keeps the three main limits (request, not a guarantee, still visible) on screen at all times. The disclosure starts **collapsed**, which saves space in the 360 x 460 window. If the owner wants the full text always visible (as today), the alternative is to keep all paragraphs open at 13px; the page already scrolls, so this is acceptable and is the lower-risk option for consent. Owner decision.

### 2.5 Accessibility rationale

- Screen reader: `<details>/<summary>` is exposed as a button with expanded or collapsed state by the browser engine. **Not known:** exact Narrator and NVDA announcement in Electron's Chromium; to be checked in the plan's SR rows. The button keeps `aria-describedby` pointing at the summary (short), not at all five paragraphs, so the button description is not a 1,000-character read.
- Focus: the summary is a normal tab stop with a visible focus ring (needs `:focus-visible { outline: 2px solid #8bb9ff; outline-offset: 2px }`, the colour already used at `styles.css:297,299,304`, whose 3:1 contrast against the background is expected but not calculated here; **Not known** until calculated).
- Size and contrast: see 2.2 and 2.3. Compact layout: the window minimum is 360 x 460 (`index.ts:451-452`, Verified). The text column is narrower than 360 px: `.page-scroll` has 19 px side padding (`styles.css:83`) and `.settings-section` has 13 px (`:206`), plus the navigation column (`.nav-item` is 44 px wide, `styles.css:71`; its container width **Not known**). Roughly 250 to 270 px of text width is a reasonable estimate (Not measured). At 13px that is about 38 to 42 characters per line. All text wraps; none is cut off, because the page scrolls vertically and `overflow-x: hidden` only hides horizontal overflow (so long unbroken strings, if any, would be clipped: none exist in the proposed text).
- 200% text zoom or Windows scaling: the section grows vertically and scrolls. **Not known** until tested (plan row SR-9).

### 2.6 Code and test changes if approved

- `apps/desktop/src/renderer/src/styles.css`: `.settings-help` (font-size 13px, colour `#b4c2d4`), `.inline-error` (13px, `#ffb3c0`), new `.settings-summary`, `.settings-details` rules, focus style, banner `.recovery-text` size in `.capture-privacy-banner`.
- `CapturePrivacyPanel.tsx`: summary plus `<details>`; `aria-describedby` points at the summary id.
- `AcceptableUseNotice.tsx`: no wording change; picks up the new CSS only.
- Tests: `capture-privacy.test.ts:677-678` (Verified) asserts the panel source contains `CAPTURE_PRIVACY_EXPLANATION.map` and `!state.supported`. If the paragraphs move inside a `<details>`, the `.map` string can stay; the `!state.supported` check changes if point 3 changes the disabled logic. Add a CSS source test that no `font-size` below 12px applies to `.settings-help`, `.inline-error` (the project's tests already read source files as text, see `capture-privacy.test.ts:627-684`, so this style fits). No existing test reads `styles.css` for these classes (Not known: a repository-wide search was not run).

### 2.7 Consent-version consequence

Size and colour changes alone do not change wording, so no version raise is needed for them. Adding the two-sentence summary and the disclosure label is new user-facing text, but it is a summary of the consent paragraphs, not additional consent content. It is covered by the raise already proposed in 1.6 (consent version 1 to 2). No acknowledgement version change.

## 3. Point 3: Users who decline or cannot proceed

### 3.1 Problem

- Acknowledgement not accepted: `capture-privacy.ts:101` throws `Read and accept the acceptable-use notice before turning on screen-capture privacy.`; the store puts it in `capturePrivacyNotice` (`store.ts:467`) and the panel shows it in `<p class="inline-error" role="alert">` (`CapturePrivacyPanel.tsx:92-96`, Verified). It does not say where the notice is. The notice is shown only in the Audio and Screenshot panels (`AudioPanel.tsx:113`, `ScreenshotPanel.tsx:65`) and only when they are not active. A user who only wants this setting is sent nowhere. (Review note 3.)
- Native confirmation cancelled: `store.ts:464` sets `notice = enabled && !next.enabled ? next.message : null`. After Cancel the state is unchanged, so the notice is the `off` message `Off. This window can be captured like any other window.` shown as an alert in the error style. A user's own choice is announced assertively as an error. (Review note 5.)
- Unsupported: the button is `disabled` when `!state.supported` (`CapturePrivacyPanel.tsx:84`). A disabled button is skipped by keyboard Tab in most browsers and may be ignored by screen-reader button navigation, so the reason (the status line) can be missed. Pressing it does nothing and says nothing. (Related: the same `store.ts:464` path would also show the `unsupported` message as an alert if the button were somehow pressed.)
- The acknowledgement notice has the same pattern: `AcceptableUseNotice.tsx:34-38` shows "The acceptable-use notice was not accepted." as `role="alert"` after the user cancels the native dialog (message per plan section 1a, `ipc.ts:419`).

### 3.2 Proposed replacement

Messages (plain, polite, no error style for the user's own choice):

```
Acknowledgement not yet accepted (shown after pressing Turn on request…):
First accept the acceptable-use notice. It is shown just above this section.

Link-style button next to it:
Go to the acceptable-use notice

User cancelled the Windows confirmation:
No change. The request was not sent.

User cancelled the acceptable-use dialog (replaces "The acceptable-use notice was not accepted."):
The notice was not accepted. You can accept it when you are ready.

Device cannot do it (always visible under the status line, no need to press anything):
This setting is not available on this device. See the status above.

A real error (the setting could not be saved; the only case that stays an alert):
The setting could not be saved. Nothing was changed. Please try again.
```

Placement and behaviour:

- **Show the existing `AcceptableUseNotice` component inside the Settings page**, directly above this section, whenever the acknowledgement is not accepted (`useAcknowledged() === false`). Pressing "Go to the acceptable-use notice" moves keyboard focus to the notice heading (`tabIndex={-1}` on the `ack-title` paragraph, which becomes a heading element) and scrolls it into view. This is preferred to sending the user to another page because the user stays where they are. Alternative if the owner does not want a second place for the notice: the button switches to the Assistant page and focuses the notice there (needs navigation code and the notice only renders when the panels are idle; weaker).
- The notice intro, `ACCEPTABLE_USE_INTRO` ("Before you use audio or screenshot assistance, please confirm the following."), would then be read in the Settings context where it is slightly off, because this setting is not audio or screenshots. Statement 4 of the notice does name the setting, so consent is valid. Option for the owner: leave the intro alone (no acknowledgement version change) or change it to "Before you use audio, screenshots or the screen-capture request, please confirm the following." (this changes acknowledged wording, so `ACCEPTABLE_USE_VERSION` would rise 2 to 3, see 3.6). **Recommendation: leave it for now.**
- **Unsupported:** keep the button focusable with `aria-disabled="true"` and `aria-describedby` pointing at the status text (so a screen reader says the button is unavailable and why) and ignore presses. Alternative: keep `disabled` and rely on the visible status line (the lower-change option). Owner decision.
- **Focus after the native dialog:** while busy the button is `disabled` (`CapturePrivacyPanel.tsx:74,84`), and a disabled focused element loses focus in browsers. **Not known** whether focus is lost in this app after the native dialog closes (plan row ADD-7 would show it). Proposed: use `aria-disabled` during busy instead of `disabled`, or return focus to the button when the action finishes.

### 3.3 Rationale

The user should always know what happened and what to do next. A choice to cancel is not a failure and should not be announced like one. The acknowledgement and this setting are tied together by statement 4 of the notice, so the notice belongs where the user is when the setting asks for it.

### 3.4 Accessibility rationale and live-region approach

- Use **one** persistent polite live region for results. Today there are two status paragraphs (`CapturePrivacyPanel.tsx:65` is `role="status"`, rendered only after state loads; `:92` is `role="alert"` rendered only when a notice exists).
  - Live regions are announced reliably only when the region already exists in the page and its text changes. A region inserted together with its text is often not announced (this is the point of review note 14). So the region must be in the DOM at first render, empty, and filled afterwards.
  - Proposed: one always-rendered element `<div role="status" aria-live="polite" aria-atomic="true">` per section that holds the single current message: the status line after a change, or the cancel or "go to notice" message. Update its text instead of mounting and unmounting elements. The visible status line and the live text are the same element, so nothing is announced twice.
  - `role="alert"` (assertive) is kept only for true errors: could not save, the confirmation is already open, withdrawal not confirmed (point 5), unexpected failure.
- Focus: no focus is moved for cancel. For "Go to the acceptable-use notice", focus moves to the notice heading and the heading text is announced by the screen reader. After the native dialog closes, focus returns to the button that opened it.
- Contrast: same colours as point 2. Do not use the red error style for cancel; use the normal help colour.
- Do not rely on colour: every message begins with a plain statement ("No change", "First accept").

### 3.5 Code and test changes if approved

- `apps/desktop/src/renderer/src/store.ts`: replace `capturePrivacyNotice: string | null` with a typed outcome `{ kind: "info" | "error"; text: string } | null`; `setCapturePrivacy` (`:458-469`) sets `info` for declined, `ack_required`, `unsupported` and `error` for thrown errors. Same for `acknowledgementNotice` (the acknowledgement store field was not read in this session; **Not known** exact name and location beyond `AcceptableUseNotice.tsx:18`).
- `CapturePrivacyPanel.tsx`, `AcceptableUseNotice.tsx`, `App.tsx` (Settings page, `App.tsx:750` area): render the notice above the section when not acknowledged; the live region; the "Go to" button; focus handling.
- `apps/desktop/src/main/capture-privacy.ts:101` and `shared/capture-privacy.ts:72-73`: new acknowledgement-required message text. The tests (`capture-privacy.test.ts:119` `/acceptable-use/i`) still match "acceptable-use notice".
- Tests: `capture-privacy.test.ts:119` still passes; add tests that the declined path produces an `info` outcome and not an alert; that the live region element is always rendered; that no `role="alert"` is bound to the declined path (source guard); that the Settings page renders the notice when not acknowledged (needs a renderer test harness: **Not known** whether the desktop project has one; if not, use source guards as the existing tests do).
- `capture-guards.test.ts`: no change expected, because no new IPC channel is needed for this point (Verified: channel allowlist at lines 97-109 lists only the existing capture-privacy channels).

### 3.6 Consent-version consequence

Messages shown only in the window (not in the native dialog) do not need a version change. **No version raise for this point alone.** If the owner chooses the optional intro change in 3.2, `ACCEPTABLE_USE_VERSION` rises 2 to 3: every signed-in user would see the acceptable-use notice again in Audio, Screenshot and (new) Settings, and would have to accept it in the native dialog again before audio, screenshots or this setting work. Recommendation: do not change it in this batch.

## 4. Point 4: Explain when stale or missing consent turns the setting off

### 4.1 Problem

`settings-store.ts:154-157` (Verified): a saved `capturePrivacy: true` is kept only when `parseCapturePrivacyConsent` returns a valid record for the current version; otherwise the setting loads as off. The window is no longer asked to be excluded. The user sees only the `off` message (`Off. This window can be captured like any other window.`) in Settings and no banner. If the user once turned it on, they are not told that protection stopped, or why. This happens when: the consent record is missing, damaged, or for an older wording version (the tests list these, `capture-privacy.test.ts:462-465`). It will happen to every user who has the setting on if the consent version is raised, which is exactly what this proposal does. (Review note 4.) Also: later settings writes omit the flag when there is no consent (`settings-store.ts:267`, Verified), so the old `true` is erased from disk at the next unrelated save, and the app cannot tell afterwards that it ever was on.

### 4.2 Proposed notice

Shown **once** when, at load, the saved choice was `true` and the consent could not be accepted. Two variants, chosen from the raw record (older number below the current version, versus missing or unreadable):

```
Title:
Screen-capture request switched off

Older wording variant:
The wording for this setting changed, so the request to Windows is now off. This window may be captured like any other window. To use the setting again, read the new wording in Settings and turn it on again.

Missing or damaged variant:
The request to Windows to leave this window out of screen capture is now off, because the app cannot confirm your earlier agreement. This window may be captured like any other window. To use the setting again, turn it on again in Settings.

Buttons:
Go to Settings
Got it
```

Where it appears:

- In the same slot as the capture-privacy banner, top of the main content, on whichever page is open (`App.tsx:136-140`, Verified), as a separate small component so the "banner cannot be dismissed" guard test (`capture-privacy.test.ts:659`, which forbids "Dismiss" and hide handlers in `CapturePrivacyPanel.tsx`) stays true for the live-indicator banner.
- Also as a line in the Settings section, directly under the status line, so a user who misses the banner finds it where they would turn the setting on.

How it is dismissed:

- "Got it" removes it and records that it was shown. "Go to Settings" opens Settings and also counts as seen. Neither action turns anything on.
- It is not dismissed by Escape or by outside click. It appears again after a restart if not dismissed, so it cannot be silently lost. (See the owner choice below.)

Hard rules:

- **It must never turn anything on.** The notice has no "turn on" button. "Go to Settings" only navigates. Turning the setting on still needs the acknowledgement, the five-paragraph native confirmation and a fresh consent record.
- The saved state stays off. A guard test must assert that dismissing never calls `enable`.

Owner choice for storage of the "notice pending or shown" flag:

- **Option A (recommended):** a small validated field in `settings.json`, for example `capturePrivacyLapse: { reason: "older_wording" | "missing_or_damaged", at: <ISO time> }`, written when the loader detects the lapse and removed on dismissal. It survives restarts and the later erasure of the old flag. It can only record that a lapse happened; it cannot enable anything.
- **Option B:** in memory only, shown for the session where the lapse was detected. Simpler, no new stored field, but a user who does not look at the window that session never sees it.

### 4.3 Rationale

Fail-closed is the right behaviour. The gap is only that the user is not told. Telling them respects the owner decision that the user should know when the request is not in force, and avoids the false belief that the window is still being excluded. Because the consent wording is raised in this proposal, every existing user with the setting on would otherwise be switched off silently.

### 4.4 Accessibility rationale

- Live region: a notice inserted with its text at page load is often not announced. Use the always-present polite region from point 3 for a short announcement ("Screen-capture request switched off. See the notice at the top of the page."), and place the same notice in Settings where it is read as ordinary content. **Not known:** exact Narrator and NVDA behaviour in this app (plan SR rows).
- Focus: the notice never steals focus. Its two buttons are normal tab stops and appear before the page content in reading order, so a keyboard user meets them early; "Got it" returns focus to the main content heading.
- Contrast and size: the same neutral banner colours as the live banner (calculated above, 10.7:1 or better), 13px text. Use the warning colours (`#fde9b8`, 10.9:1 on tint) since the user has lost a setting they chose.
- Not colour-only: the title says what changed.

### 4.5 Code and test changes if approved

- `apps/desktop/src/main/settings-store.ts`: during `load()` compute the lapse reason; add `capturePrivacyLapse()` getter and `clearCapturePrivacyLapse()`; extend the write function (`:256-267`) and validation.
- `apps/desktop/src/shared/contracts.ts`, `preload/index.ts`, `main/ipc.ts`, `main/ipc-security.ts`: expose the lapse in the capture-privacy state (preferred, no new read channel) and add one new channel to dismiss it (for example `app:dismiss-capture-privacy-lapse`). **`capture-guards.test.ts:97-109` is an exact allowlist and must gain this channel deliberately**, and the IPC trust test (`capture-privacy.test.ts:598`, untrusted sender refused) must cover it.
- `apps/desktop/src/shared/capture-privacy.ts`: new strings and the state field `lapse`.
- New renderer component file for the notice, mounted at `App.tsx:140`.
- Tests: loader tests for each reason (older version, missing, damaged, wrong type, raised version); dismissal clears only the notice and leaves `enabled` false; `enable` is never called by dismissal (extend the guard regexes at `capture-privacy.test.ts:645,650`); a lapse is not raised when the saved choice was off; the existing fail-closed tests (`:462-465,488-489,503`) still pass unchanged.

### 4.6 Consent-version consequence

This point creates new user-facing text but no new consent. It is what makes the version raise (1.6, consent version 1 to 2) understandable. **Recommendation: approve points 1 and 4 together.** If the consent version is raised without this notice, existing users with the setting on are switched off with no explanation (today's behaviour, review note 4).

## 5. Point 5: Make withdrawal understandable and accessible

### 5.1 Problems (Verified)

- Turn-off flow: `Turn off` (`CapturePrivacyPanel.tsx:78`) needs no confirmation, saves off first and then asks Windows to stop (`main/capture-privacy.ts:138-156`). The result is the `off` message, and nothing says the request was withdrawn or that Windows may take a moment.
- Failed to confirm: `main/capture-privacy.ts:150-152` sets the status to `failed` with `Windows did not confirm that the window can be captured again. Restart the app to be sure. The setting is saved as off.` It appears only as the Settings status line (a `role="status"` paragraph) at 8px. Because the saved choice is off, the banner is hidden (`CapturePrivacyPanel.tsx:27` needs `state.enabled`), so on any other page nothing indicates that the window may still be excluded from capture. (Review note 7.)
- Enabled but failed: `on` is `state.enabled && state.status === "requested"` (`CapturePrivacyPanel.tsx:53`). When `enabled` is true and the status is `failed`, `on` is false, so the button reads `Turn on…` and the user cannot turn it off without first "turning on" again (review note 6). The banner then says "...You can try again" (`failed` message) but has no button.
- Tray: the tray tooltip is `Interview Assistant — visible desktop assistant` and changes only for audio and screenshot states (`index.ts:149-173,607`, Verified). The tray menu has "Show Assistant", "Reload window" and "Quit Interview Assistant" (`index.ts:608-625`). Nothing shows the request status when the window is in the tray. (Review note 15.)

### 5.2 Proposed changes

**5.2a Turn-off flow.** Keep it one step with no confirmation (withdrawal must stay easy). Wording after a successful turn off, in the polite live region and the status line:

```
Off. The request was withdrawn. Windows may take a few seconds to apply this.
```

The button after turning off reads `Turn on request…`. The banner disappears. Focus stays on the button (point 3).

**5.2b Withdrawal not confirmed.** Replace `main/capture-privacy.ts:151-152` text with:

```
The request is off in the app, but Windows did not confirm it. This window may still be left out of screen capture until you restart the app. Restart the app to be sure.
```

Show it in the assertive alert region (this is a true problem, not the user's own choice) once, and **also show a warning banner on every page** until the app restarts (or a later successful turn off or on), using this text:

```
Withdrawal not confirmed: the request is off in the app, but Windows did not confirm it. Restart the app to be sure.
```

The banner needs a flag in the state for "withdrawal unconfirmed" (a new field in `CapturePrivacyState`), so the existing rule "banner only when `enabled`" gets one extra condition. An optional "Restart now" button would need a relaunch capability in the main process (`app.relaunch`), which is a new capability, not in the approved scope (**Not known** whether one exists); **not proposed**. Text only.

**5.2c Enabled but failed.** Change the button logic to depend on `state.enabled`, not on `status === "requested"`:

```
When enabled and requested:   [Turn off request]
When enabled and failed:      [Try again…]  [Turn off request]
When enabled and unsupported: [Turn off request]
When not enabled:             [Turn on request…]  (aria-disabled when unsupported)
```

"Try again…" reuses the existing turn-on path (`set(true)`), which asks for the native confirmation again, so no new IPC and no new silent action is needed. (Verified from `main/capture-privacy.ts:96`: `enable()` returns early only when already `requested`; otherwise it asks for confirmation.) Failed-while-on banner text is the `failed` text of point 1 and now has its action in Settings; add a "Go to Settings" button to that banner so the banner has an action (the owner may prefer no button; then rely on "Open Settings" in the text).

**5.2d Tray and window-hidden visibility. OWNER MUST DECIDE.** Options:

- **Option 0, no change.** The indicator stays in the window only. Plan row IND-6 records what the tray shows. Lowest effort. Review note 15 stays open.
- **Option 1, tooltip only.** While the saved setting is on and the status is `requested`, the tray tooltip reads for example `Interview Assistant — asked Windows to leave this window out of screen capture`; other states (countdown, capturing, recording) keep priority and their text. Needs one function that builds the tooltip from three inputs (`index.ts:149-173` currently sets it from audio or screenshot only). Windows limits tray tooltip length (commonly stated as 127 characters; **Not known** or verified here, so shorten and test). Hover-only, so not available to touch or keyboard users unless a screen reader reads tray tooltips (**Not known**).
- **Option 2, tray menu line.** Add a disabled, informational first line to the existing context menu (`index.ts:608`): `Screen-capture request: on`, `Screen-capture request: not accepted by Windows`, or `Screen-capture request: off`. Menus are keyboard and screen-reader reachable (tray menu accessibility in this app is **Not known**). Needs the menu to be rebuilt when the state changes.
- **Option 3, both 1 and 2.**
- Not proposed under any option: hiding or changing the tray icon, taskbar entry or process listing; a taskbar overlay badge (new visual machinery for little gain).

Recommendation: **Option 3** (smallest change that makes the setting visible both on hover and by menu). Any option only adds visibility, so it fits the "no concealment" decision.

### 5.3 Rationale

Withdrawal must be at least as easy as turning on, must say what happened, and must not leave the user thinking the window is exposed when it is not (or the reverse). An unconfirmed withdrawal is the only case where the real state may differ from the saved state in the direction of continued exclusion, so it deserves a banner, not a single small line in Settings. The "enabled but failed" case should never require turning something on in order to turn it off.

### 5.4 Accessibility rationale

- Screen reader: the turn-off result is announced once through the polite region (point 3); the unconfirmed withdrawal is announced assertively because the user needs to act (restart). The banner is a `role="status"` region; to avoid a double announcement the assertive text is spoken once and the banner is `role="status"` with `aria-live="off"` at first mount, or the same message is not repeated (implementation detail for the change; testable in plan rows SR-3 and SR-6).
- Focus: two buttons in "enabled but failed" are in logical order (Try again, then Turn off). Focus returns to the first available button after any action; it is not lost to a disabled element.
- Contrast and size: 13px, same colours as above. Warning banner: `#fde9b8` on the warning tint, 10.9:1 (calculated).
- Tray options: tray menu entries are keyboard reachable in Windows; tooltips are not reliably accessible. That is why Option 2 is suggested alongside Option 1.

### 5.5 Code and test changes if approved

- `apps/desktop/src/main/capture-privacy.ts`: new messages; `withdrawalUnconfirmed` flag; clear the flag on the next successful turn on or off.
- `apps/desktop/src/shared/capture-privacy.ts`: `CapturePrivacyState.withdrawalUnconfirmed: boolean`; message constants.
- `CapturePrivacyPanel.tsx`: banner conditions, button logic based on `enabled`, "Try again…".
- `apps/desktop/src/main/index.ts`: tooltip builder and menu line if Option 1, 2 or 3.
- Tests: `capture-privacy.test.ts:190` (`/restart/i`) still passes; `:658` (`if (!state || !state.enabled) return null` is asserted as source text) changes because the banner condition gains the unconfirmed case; `:293,296` (enabled and failed) still true; new tests: button choice for each of the four combinations above; unconfirmed withdrawal sets and clears the flag; tray text contains "screen capture" only when the state is `requested` and never when off; the existing tray guards (`:627-631`) stay. Any new tray test may need an Electron-free fake (**Not known** how `index.ts` is tested; `capture-privacy.test.ts:631` reads it as text).

### 5.6 Consent-version consequence

Withdrawal wording is not part of the consent the user gives. Strings in the status line, banner and tray are not in the native dialog. **No separate version raise for this point.** The tray text, if added, is a status text, not a consent term.

## 6. Consolidated before and after (every proposed user-visible string)

"Before" is quoted from the source files at the lines given (Verified). "After" is this proposal. "Dialog" means the native confirmation, which is part of the consent.

| # | Where | Before | After |
|---|---|---|---|
| 1 | Settings title and dialog title (`capture-privacy.ts:48`) | Hide this window from screen capture | Ask Windows to leave this window out of screen capture |
| 2 | Dialog message (`index.ts:273` per plan) | Hide this window from screen capture? | Send this request to Windows? |
| 3 | Dialog buttons (`index.ts:275` per plan) | I understand, turn on / Cancel | Yes, send the request / Cancel |
| 4 | Settings button, off (`CapturePrivacyPanel.tsx:88`) | Turn on… | Turn on request… |
| 5 | Settings button, on (`:78`) | Turn off | Turn off request |
| 6 | Button while turning off (`:78`) | Working… | Turning off… |
| 7 | Button while dialog open (`:88`) | Waiting for confirmation… | (unchanged) |
| 8 | New button when enabled and failed | (none; button read "Turn on…") | Try again… |
| 9 | Explanation, para 1 (`capture-privacy.ts:51`) | When this is on, the app asks Windows to leave the Interview Assistant window out of screen capture that supports the request, such as many screen-sharing and recording apps. It is meant for keeping private material, like your resume or notes, from showing in a screen share you are allowed to make, and for accessibility. | When this is on, the app asks Windows to leave this window out of screen capture. Many screen-sharing and recording tools follow the request. You might use it to keep private notes from showing in a screen share that you are allowed to make. It can also help with accessibility. |
| 10 | Explanation, para 2 (`:52`) | It only affects this app's own window. The app still appears in the taskbar and the system tray, still shows its listening and capture indicators, and is still listed as a running program. In a Windows remote session the window is not excluded, so remote-access and remote-support tools still see it. | The request covers only this window. The app still shows in the taskbar and the system tray. It still shows its listening and capture indicators. It is still listed as a running program. In a Windows remote session the window is not left out, so remote-support tools can still see it. |
| 11 | Explanation, para 3 (`:53`) | It is not a guarantee. Some capture methods, other screens, cameras pointed at your display, and security, proctoring or monitoring software may still see the window. It does not and must not be used to avoid monitoring. | This is not a guarantee. Some tools ignore the request. Other screens, cameras pointed at your display, and security, proctoring or monitoring software may still see this window. You must not use this setting to avoid monitoring. |
| 12 | Explanation, new para 4 | (none) | Windows may take a few seconds to apply a change. A screenshot or recording made earlier is not affected. This choice is saved only on this device. |
| 13 | Explanation, old para 4 now 5 (`:54`) | You remain responsible for following the rules of any interview, meeting or assessment where you use this app. If those rules do not allow AI assistance, do not use it there. | You are still responsible for following the rules of any interview, meeting or assessment where you use this app. If those rules do not allow AI assistance, do not use it there. |
| 14 | Settings summary (new) | (none) | This sends a request to Windows. Some tools may ignore it, so it is not a guarantee. The app stays in the taskbar and tray, and stays visible to remote support. |
| 15 | Disclosure label (new) | (none) | What this does and does not do |
| 16 | Status, off (`:61`) | Off. This window can be captured like any other window. | Off. Windows has not been asked to leave this window out of screen capture. |
| 17 | Status, requested (`:62-63`) | On. Windows was asked to leave this window out of supported screen capture. This is not a guarantee, and the app does not check whether a given tool honours it. | On. Windows was asked to leave this window out of screen capture. This is not a guarantee. The app cannot check whether a tool follows the request. |
| 18 | Status, unsupported (`:64-65`) | Not available on this device. It needs Windows 10 version 2004 (build 19041) or later. The app works normally and the window is shown as usual. | Not available on this device. It needs Windows 10 version 2004 (build 19041) or later. The app works normally, and this window may be captured as usual. |
| 19 | Status, failed (`:66-67`) | Windows did not accept the request. The window is shown as usual and the app works normally. You can try again. | Windows did not accept the request. This window may be captured as usual. You can try again, or turn the request off. |
| 20 | Banner, requested (`:57-58`) | Hidden from screen capture: this window has been requested to be left out of supported captures. Not guaranteed. | Request sent: Windows was asked to leave this window out of screen capture. Not guaranteed. Some tools may still capture it. |
| 21 | Banner, failed | (the failed status text above) | Request not accepted: Windows did not accept the request, so this window may be captured as usual. Open Settings to try again or turn it off. |
| 22 | Banner, unsupported | (the unsupported status text above) | Request not active: this version of Windows cannot do this, so this window may be captured as usual. |
| 23 | Banner, off | (no banner) | (no banner; unchanged) |
| 24 | After turn off (new) | (the off message) | Off. The request was withdrawn. Windows may take a few seconds to apply this. |
| 25 | Withdrawal not confirmed (`main/capture-privacy.ts:151-152`) | Windows did not confirm that the window can be captured again. Restart the app to be sure. The setting is saved as off. | The request is off in the app, but Windows did not confirm it. This window may still be left out of screen capture until you restart the app. Restart the app to be sure. |
| 26 | Withdrawal banner (new) | (none) | Withdrawal not confirmed: the request is off in the app, but Windows did not confirm it. Restart the app to be sure. |
| 27 | Acknowledgement required (`capture-privacy.ts:72-73`) | Read and accept the acceptable-use notice before turning on screen-capture privacy. | First accept the acceptable-use notice. It is shown just above this section. |
| 28 | Pointer button (new) | (none) | Go to the acceptable-use notice |
| 29 | Cancel of the confirmation (`store.ts:464` shows the off message as an alert) | Off. This window can be captured like any other window. (alert) | No change. The request was not sent. (polite) |
| 30 | Cancel of the acknowledgement dialog (`ipc.ts:419` per plan) | The acceptable-use notice was not accepted. (alert) | The notice was not accepted. You can accept it when you are ready. (polite) |
| 31 | Unsupported helper (new) | (none) | This setting is not available on this device. See the status above. |
| 32 | Save error (replaces raw error text) | (error text shown as is) | The setting could not be saved. Nothing was changed. Please try again. |
| 33 | Lapse notice title (new) | (none) | Screen-capture request switched off |
| 34 | Lapse notice, older wording (new) | (none) | The wording for this setting changed, so the request to Windows is now off. This window may be captured like any other window. To use the setting again, read the new wording in Settings and turn it on again. |
| 35 | Lapse notice, missing or damaged (new) | (none) | The request to Windows to leave this window out of screen capture is now off, because the app cannot confirm your earlier agreement. This window may be captured like any other window. To use the setting again, turn it on again in Settings. |
| 36 | Lapse notice buttons (new) | (none) | Go to Settings / Got it |
| 37 | Tray tooltip, Option 1 or 3 (new; `index.ts:149-173,607`) | Interview Assistant — visible desktop assistant | Interview Assistant — asked Windows to leave this window out of screen capture (text must be shortened if Windows' tooltip length limit requires it; Not known) |
| 38 | Tray menu line, Option 2 or 3 (new) | (none) | Screen-capture request: on / not accepted by Windows / off |

Unchanged (not proposed to change): the acceptable-use title, intro, four statements and closing note (`acknowledgement.ts:10-23`); the banner accessible name "Screen capture privacy"; the unsupported-version number in the status; every text about audio and screenshots.

## 7. Risks

1. **Users must consent again.** Raising the consent version switches every saved "on" off. Mitigated only if point 4 ships with it (section 8). Without it the switch-off is silent.
2. **Longer text in a small window.** The new texts are a little longer; at 13px and 360 px minimum width the section is taller. The page scrolls, but the compact layout needs a visual check. **Not known.**
3. **"May be captured as usual" could be read as a promise.** It is meant as a limit. The owner may prefer "may still be captured".
4. **Disclosure hides text.** If the owner treats Settings text as the consent record, collapsing it weakens that. Mitigation: the native dialog always has the full text, or keep paragraphs always open (section 2.4).
5. **Focus and live-region behaviour is untested.** The plan's SR and ADD-7 rows are the only way to learn it; a fix may need another iteration.
6. **New IPC channel (point 4) and extra state fields enlarge the audited surface** and must be added deliberately to the preload allowlist test.
7. **A stored lapse flag (Option A) is new data in `settings.json`.** It holds only a reason and a time and cannot turn anything on; the validator must reject anything else.
8. **Tray tooltip limits and accessibility** are **Not known**.
9. **"Try again…" reopens the native confirmation.** Safe, but one more dialog for a user who just wants a retry.
10. **Wording is a policy statement, not legal advice**, and review by the owner or counsel (OQ-15) is still required.
11. **Statements about timing and device-only storage in new paragraph 4** are taken from the plan's review note 11 and were not re-read in their source documents here.

## 8. Consent-version summary (what existing users would see)

Recommended:

- **`CAPTURE_PRIVACY_CONSENT_VERSION` 1 to 2.** The dialog title, message, buttons and explanation change (points 1 and 2). Every saved consent record with `version: 1` no longer matches, so a saved "on" loads as **off** for every user who had it on (`settings-store.ts:154-157`; tests `capture-privacy.test.ts:462-465`). With point 4 approved those users see the "Screen-capture request switched off" notice (older wording variant) once, with "Go to Settings" and "Got it", and the setting is off until they press **Turn on request…** and answer the new native dialog. Users who had it off see nothing new until they open Settings; they see the new text then.
- **`ACCEPTABLE_USE_VERSION` stays 2.** The acknowledgement text is not changed in this proposal. Users who already accepted version 2 are not asked again. Only if the owner chooses the optional intro change in 3.2 does it rise to 3; then every user would see the notice again before audio, screenshots or this setting.
- The new consent record, once the user agrees, is `{ version: 2, acceptedAt }`.
- Test changes that follow from the raise: tests that use `CAPTURE_PRIVACY_CONSENT_VERSION` by constant follow it automatically; any test with a literal `1` as the current version needs to change (the file uses `CAPTURE_PRIVACY_CONSENT_VERSION - 1` and `+ 1` at `:464` and `:462-463`, which also follow the constant; a literal `99` at `:489` stays). **Not known:** whether other test files hard-code `1`; a repository search was not run.

If the owner approves only part of the proposal, the version rule is: any change to the title, dialog message, dialog buttons or explanation paragraphs raises the consent version; any change to the acknowledgement statements, intro or note raises the acknowledgement version; status, banner, tray, button and notice wording alone raises neither.

## 9. Approval block

Nothing below is applied until the owner marks it. "Approve with changes" requires the owner to write the changes.

| Point | Approve | Approve with changes | Reject |
|---|---|---|---|
| 1. Accurate indicator, title, toggle and status wording (section 1) | [ ] | [ ] | [ ] |
| 2. Readable size and colour; summary plus disclosure or always-open text (section 2) | [ ] | [ ] | [ ] |
| 3. Clear direction when declining or blocked; polite live region (section 3; includes the optional acknowledgement intro change: yes / no) | [ ] | [ ] | [ ] |
| 4. One-time notice when consent lapses (section 4; storage Option A / B) | [ ] | [ ] | [ ] |
| 5. Withdrawal, failed states, enabled-but-failed button, tray hint (section 5; tray Option 0 / 1 / 2 / 3) | [ ] | [ ] | [ ] |

Owner confirmations of the decisions in section 0 (they are not changed by this proposal):

- [ ] Remote-session exclusion stays disabled.
- [ ] Remote-support visibility stays enabled.
- [ ] The feature is described only as a request to Windows.
- [ ] No concealment of the process, tray or taskbar entry.
- [ ] Accessibility is maintained.

Changes or reasons:

```
(owner to write here)




```

Name: ______________________________

Date: ______________________________

## 10. After approval

Applying an approved point is a **separate small change**, not part of this document. It would include the code and tests listed per point, the raised version constant or constants, and updates to `docs/WINDOW_CAPTURE_PRIVACY.md`, `docs/DECISIONS.md` (a new entry, not a rewrite of D-80) and `docs/PRIVACY.md` where wording is quoted. It would be followed by a **manual re-check of the plan in `docs/M15_OWNER_APPROVAL_AND_TEST_PLAN.md`**: at least the rows for the indicator (IND-1 to IND-6), consent (CON-1 to CON-10 including the new lapse notice), withdrawal (WDR-1 to WDR-6), restart (PERS-1, PERS-5), screen reader (SR-1 to SR-9, which are the only evidence for the live-region choices) and keyboard (ADD-7). Remote visibility rows (REM-1 to REM-3) are re-run to confirm nothing changed. The expected-result columns of the plan that quote the old wording (for example IND-2, CON-5 to CON-8, WDR-1, WDR-5) would need updating at that point; this document does not edit that plan. Until then, M-15 stays at AUTOMATED-TESTED and nothing in this document is MANUALLY-TESTED.

## 11. What was and was not checked for this proposal

Verified (read in this session): `M15_OWNER_APPROVAL_AND_TEST_PLAN.md`; `acknowledgement.ts`; `shared/capture-privacy.ts`; `CapturePrivacyPanel.tsx`; `AcceptableUseNotice.tsx`; `main/capture-privacy.ts` lines 85-175; `store.ts` lines 444-469; `settings-store.ts` lines 145-163 and grep of the consent lines; `styles.css` lines 1-60 and the grep lines for the named classes; `index.ts` lines 145-185 and 596-625 and a grep for window size, background colour and tooltip; grep results for `capture-privacy.test.ts` and `capture-guards.test.ts`; section headings of `WINDOW_CAPTURE_PRIVACY.md`; D-80 in `DECISIONS.md`. Contrast numbers were calculated with a short throwaway script (outside the repository) from the CSS values.

Not known: the actual desktop behind the transparent window; real on-screen text size; compact-layout wrapping; screen-reader and keyboard behaviour; tray tooltip length limit and accessibility; the body of `WINDOW_CAPTURE_PRIVACY.md` sections 3, 7 and 9 and of `PRIVACY.md`; the dialog code at `index.ts:268-282` and `ipc.ts` (quoted only through the plan); whether other tests hard-code consent or acknowledgement versions; whether the desktop project has a renderer component test harness. WCAG thresholds are from the published standard and were not read from a repository file.
