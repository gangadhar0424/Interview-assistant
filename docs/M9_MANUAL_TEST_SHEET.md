# M-9 manual test sheet

Status (2026-11-22): the **12 desktop checks (section 1) are recorded as passed on the owner's confirmation** (the owner personally ran all 12; no screenshots, logs or recordings were provided, so none are attached or invented). Check 8 was amended on 2026-11-22 to export Markdown and **PDF** instead of JSON (D-76); the owner confirmation is recorded against the sheet as it stood when the owner reported it, and a re-run of check 8 on the PDF build is recommended. **The six browser checks W1-W6 (section 2) are NOT recorded as passed**: the owner has not confirmed that they were performed. A person fills in the Result column. Procedure for the 12 rows of `docs/DESKTOP_MANUAL_CHECKLIST.md` ("Session management (M-9)"); behaviour is described in `docs/SESSION_MANAGEMENT.md`.

## 0. Build and launch (PowerShell, repository root `interview assistant`)

The packaged installer in `apps/desktop/release/` is dated 2026-11-17 and `out/` 2026-11-18; neither contains M-9, so **rebuild before testing**. Quit the running app first (tray icon, Quit).

```powershell
# Infrastructure (Postgres + Redis)
docker compose up -d --wait

# Backend (terminal 1)
cd services/api
uv sync
uv run alembic upgrade head          # no new M-9 migration; brings an older dev DB up to date
uv run uvicorn app.main:get_app --factory --reload --port 8000

# Ingestion worker (terminal 2; only needed if you upload a NEW resume/document)
cd services/api
uv run dramatiq app.workers.tasks --processes 1 --threads 2

# Web app (terminal 3)
cd apps/web
npm run dev                          # http://localhost:3000

# Desktop test build (terminal 4)
cd apps/desktop
npm run typecheck
$env:CSC_IDENTITY_AUTO_DISCOVERY = "false"
npm run package:win                  # = npm run build + electron-builder --win nsis (unsigned, internal testing)
# Output: apps/desktop/release/Interview Assistant-0.1.0-Windows-x64.exe  -> run it to install over the old one
```

Confirm the build is M-9: the file's timestamp is today, and Sessions cards show **Rename / Complete / Export / PDF / Delete** links. The desktop has no password sign-in: sign in on the web, open a session, press **Start Interview** (desktop handoff); the installed app must be the one registered for `interviewassistant://`.

**Prepare data:** use a non-production account. Create **two sessions** (A and B) in the web wizard with a ready resume; in A ask 2–3 typed questions (the dev `.env` uses real providers, so working provider keys are needed for any answer). Keep DevTools closed in the packaged app.

## 1. Desktop tests (packaged app)

| # | Do | Expected | Evidence | Result |
|---|---|---|---|---|
| 1 | Open Sessions | Each card shows name, company, state pill (Active/Completed/Interrupted), "N questions", relative last activity; the open session has a **Current** pill | Screenshot of the list | **Passed** (owner-confirmed 2026-11-22; no evidence files attached) |
| 2 | **Rename** B, type a name, Enter. Then Rename again and press Escape | Name updates in the list, Ask selector and History; green "Session renamed."; Escape cancels with no change | Screenshots of list and Ask selector | **Passed** (owner-confirmed 2026-11-22; no evidence files attached) |
| 3 | Open A (Ask page shows its answers), switch to B (Ask selector or card) | B shows only B's history; none of A's questions or answers at any moment; switching back shows A's | Screenshots of A, B, A | **Passed** (owner-confirmed 2026-11-22; no evidence files attached) |
| 4 | Ask a long question in A; while it streams switch to B | Streaming stops; A's partial text never appears under B; B's history is its own | Screenshot of B right after the switch | **Passed** (owner-confirmed 2026-11-22; no evidence files attached) |
| 5 | Settings: enable audio input and accept the notice; in A press **Listen**; switch to B | Listening indicator and tray state go idle; Listen must be pressed again (it never restarts alone) | Screenshot before and after; tray tooltip | **Passed** (owner-confirmed 2026-11-22; no evidence files attached) |
| 6 | **Complete** A (open session) then **Reopen** | Complete: listening stops, state Completed, Ask disabled; Reopen: state Active, Ask enabled, nothing recording | Screenshots of each state | **Passed** (owner-confirmed 2026-11-22; no evidence files attached) |
| 7 | **Delete** B; first choose Cancel, then repeat and confirm | Native dialog appears; Cancel changes nothing; confirm removes B, history cleared, the next session opens, nothing records | Screenshot of the dialog and of the list after | **Passed** (owner-confirmed 2026-11-22; no evidence files attached) |
| 8 | **Export** A: Cancel at the warning; then Cancel at the Save dialog; then save as `.md`; then **PDF** save | No file after either cancel; saved files open: `.md` has the job description, questions and answers and a "Not included" section; `.pdf` opens in a PDF viewer with the same sections (details, "What this file contains", "Not included", job description, questions and answers); no resume text, token or your e-mail | Screenshots of both dialogs; the two files (open, do not share) | **Passed** (owner-confirmed 2026-11-22; no evidence files attached) |
| 9 | Turn the network off (Wi-Fi off, or stop the backend). Try Rename, Complete, Delete (confirm), Export (pick a path) | Each shows "could not reach the backend / was not changed" (export: an error); nothing changes; **no file at the export path**; after reconnecting, each works | Screenshots of the messages; folder listing showing no file | **Passed** (owner-confirmed 2026-11-22; no evidence files attached) |
| 10 | In the web dashboard delete session B (browser confirm). In the desktop, without refreshing, click **Complete** on B | "This session is no longer available."; the list refreshes and B is gone | Screenshot of the message and the list | **Passed** (owner-confirmed 2026-11-22; no evidence files attached) |
| 11 | Quit from the tray and start the app from the Start menu (not a web link) | Signed in; the most recently used **ready** session is selected; nothing is recording | Screenshot after start; tray state | **Passed** (owner-confirmed 2026-11-22; no evidence files attached) |
| 12 | In the desktop **New session**, fill the form, press **Create** and cut the network at that moment; restore it and press Create again with the same form | Never two sessions with the same details; the list ends with exactly one (if the first request never reached the server it is created now) | Screenshot of the list; count rows | **Passed** (owner-confirmed 2026-11-22; no evidence files attached) |

## 2. Browser tests (web, `http://localhost:3000`, signed in)

These are the **web** part of the same features (checks 8, 9, 10 and 12 also have a browser side). Not part of the 12 desktop rows; record separately.

| # | Do | Expected | Evidence | Result |
|---|---|---|---|---|
| W1 | Dashboard **Call Sessions** | Each row: name, company, state label, "Last activity", question count; Rename, Complete or Reopen, Export, Export PDF, Delete | Screenshot | |
| W2 | **Export**: cancel the browser confirmation, then accept | Cancel: no download; accept: `session-xxxxxxxx.md` downloads, green "Export ready"; **Export PDF** gives `session-xxxxxxxx.pdf`; contents as in desktop check 8 | Downloaded files; screenshot | |
| W3 | **Delete**: cancel the confirm, then accept | Cancel: nothing; accept: row removed, "Session deleted." | Screenshot | |
| W4 | **Complete** then **Reopen** | State Completed (Reopen shown), then Active | Screenshots | |
| W5 | Offline, then click Export / Complete | "Could not reach the server… not changed"; no download | Screenshot | |
| W6 | Wizard: **Create session**, set DevTools Network to Offline right after clicking, go back online, click again | One session only | Dashboard count | |

Optional API proof for check 12 (and the server side of W6), which is deterministic: send `POST /api/v1/sessions` twice with the same `Idempotency-Key` header and body (a tool such as curl with your access token); expect `201` then `200` with the same `id`. Do not paste tokens into notes.

## 3. Known limitations that can affect testing

- The packaged app must be **rebuilt**; the existing installer is pre-M-9.
- Answers (checks 3, 4, 6) need working provider keys in `.env`; without them questions fail and check 4 cannot be observed.
- Check 5 needs the real microphone, the audio opt-in, and the acceptable-use acknowledgement.
- Check 11 only applies when starting without a web link; a **Start Interview** link selects the handed-off session instead.
- Check 12 is timing dependent: whether the first request reached the server depends on when the network drops, so the pass condition is "never two", not "always one new".
- The Interrupted state appears only after a cut-off answer (for example stopping the backend mid-answer, then waiting past the generation timeout or reopening the history); it is not forced by any button.
- List order is by creation time; "most recently used" is shown by last-activity text and used only to choose the opened session.
- The web session page (`/sessions/{id}`) has no complete/reopen/export controls; use the dashboard.
- The installer is unsigned, so Windows SmartScreen may warn.
- Nothing here has been run by me beyond the automated suites already reported.
