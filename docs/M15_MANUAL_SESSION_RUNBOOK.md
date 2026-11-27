# M-15 manual validation session: runbook

**Update 2026-11-24:** the owner has run the manual session and confirmed M-15 works successfully (overall confirmation only; per-row results and evidence are not recorded in this file, and no tool or application is claimed as tested beyond what the owner records). M-15 status: implemented and manually validated. The text below is the original runbook and is kept as written.

Prepared 2026-11-23. This is the order in which to run the checks in `docs/M15_OWNER_APPROVAL_AND_TEST_PLAN.md` (the row IDs below, such as CAP-1a, are that document's; run every row of an area, and note that its section 5 lists the minimum set required for sign-off). It adds no new checks and changes no wording. **Nothing here has been run.** Every row stays NOT TESTED until you run it and record the result.

Owner decisions that this session must not change, and any result that contradicts them is reported first: remote-session exclusion stays **disabled**; remote-support visibility stays **enabled** (the assistant window must remain visible to a remote viewer).

## 0. Before you start (about 45 minutes, once)

| What | Why | Done |
|---|---|---|
| Decide the wording first: approve, change or reject the acknowledgement v2 and the M-15 consent text in section 1 of the plan. If you want the accessibility changes in `docs/M15_PROPOSED_REVISION.md`, approve them **before** this session, have them built, and then test the revised build. | Testing wording you are about to change wastes the session, and a wording change raises the consent version, which resets consent. | ☐ |
| A Windows 11 machine you may use, with a **second monitor** if possible. Note `winver` (OS build). | Areas 6, 7 and the baseline. | ☐ |
| A packaged build to test: from `apps/desktop`, `npm run package:win:release` (an unsigned internal build; it prints where the installer is). Install it on the test machine. | The packaged app is what users run; `npm run dev` behaves differently. | ☐ |
| A **test account** and a prepared session on the web app. No real interview material, no other person's data. | Privacy. | ☐ |
| Capture tools: Snipping Tool, Xbox Game Bar (built in), **OBS Studio** (free), a conferencing app you already use (Teams, Zoom or Meet) and a second person or account to join a test call, a **remote-support or remote-desktop tool** you are allowed to use with a colleague. | Area 6 and Area 9. | ☐ |
| Screen readers: **Narrator** (built in: Win+Ctrl+Enter) and, if possible, **NVDA** (free). | Area 8. | ☐ |
| A way to test Windows older than 2004: a virtual machine image of Windows 10 1909 or earlier, if you have one. If not, UNS rows are recorded as BLOCKED with that reason. | Area 10. | ☐ |
| An evidence folder `M15-evidence` on the Desktop. Name each file with its row ID, for example `CAP-1b-snip-on.png`. | Sign-off needs attachments. | ☐ |

## 1. Session order (about 3 to 4 hours of work in total; the times are estimates, not measurements)

Run in this order. Each part says which rows to run. Wait about 3 seconds after switching protection on or off before every capture.

| Part | Rows | Estimate | Notes |
|---|---|---|---|
| A. Consent and indicator | all CON rows (CON-1 to CON-11), then all IND rows (IND-1 to IND-7) | 30 min | Do CON first: the indicator only exists after consent. Include the **decline** paths (cancel in the native dialog; try turning on without accepting the acknowledgement) and write down exactly what the app says: you will need it for review notes 3 and 5. |
| B. Withdrawal | all WDR rows (WDR-1 to WDR-6) | 15 min | Note what the app says if Windows does not confirm removal (WDR-5 may stay BLOCKED: it needs a failure to be provoked). |
| C. Restart and recovery | all PERS rows (PERS-1 to PERS-8) | 20 min | The PERS rows that edit a copy of `settings.json` to simulate a missing or old consent. Follow the plan's safe-copy steps (PERS rows); never edit the live file while the app runs. |
| D. Window recreation | all WIN rows (WIN-1 to WIN-7) | 20 min | Close to tray and reopen, minimise and restore, compact and expanded, and the renderer-recovery case if you can provoke it. |
| E. Capture tools | all CAP rows (CAP-1a to CAP-11; every tool with protection **off** then **on**) | 60 to 90 min | Record hidden, black or visible per tool. Results differ between tools and Windows builds; that is expected and is the point. Do **not** write "protected" or "invisible" anywhere. |
| F. Multiple monitors | all MON rows (MON-1 to MON-7) | 20 min | Move between monitors, different scaling if you can, unplug one. |
| G. Screen readers | all SR rows (SR-1 to SR-9), Narrator first, then NVDA | 30 min | Listen for: the indicator announced when the app starts with the setting already on (review note 14), the Settings control's name and state, the consent dialog read in full, focus order. |
| H. Remote support | all REM rows (REM-1 to REM-5) | 20 min | With a colleague, share your screen through the remote-support tool with protection **on**. The assistant window must be **visible** to them. If it is hidden, stop and report it before anything else. |
| I. Unsupported Windows | all UNS rows (UNS-1 to UNS-6) | 15 min | Only on the old-Windows VM. Expected: the setting reports unsupported and changes nothing. |
| J. Additional rows | all ADD rows (ADD-1 to ADD-8) | 20 min | Taskbar and tray entry still visible, process visible in Task Manager, audio and screenshot features unaffected, keyboard-only operation. |

## 2. Stop conditions

Stop the session and report immediately if any of these happen:

1. The assistant window is **hidden** from a remote-support viewer (REM rows).
2. The setting turns itself **on** without your confirmation (any restart, recreation, update or crash-recovery row).
3. The taskbar entry, tray icon or process disappears while the setting is on.
4. The app cannot be used with the keyboard only, or a screen reader cannot reach the setting or its explanation.

## 3. After the session

1. Fill the Result column in the plan: **MANUALLY-TESTED** (with the observed outcome in Notes) or **BLOCKED** (with the reason). No other word.
2. Complete the approval block (section 1c) and the sign-off list (section 5), with your decisions on the review notes and on `docs/M15_PROPOSED_REVISION.md`.
3. Give the filled tables and the evidence folder to an engineer, who records the results in `docs/WINDOW_CAPTURE_PRIVACY.md` and `docs/PRODUCTION_ROADMAP.md` for exactly the tools, Windows build and rows that were run.
4. Anything not run stays **NOT TESTED**. M-15 stays **AUTOMATED-TESTED** until the sign-off list in section 5 of the plan is complete.

## 4. What this session cannot tell you

It tests one Windows build, one set of tools and one set of monitors. It cannot show that any other capture tool, Windows version or driver behaves the same way. The setting is a request to Windows; the product never claims more than that.
