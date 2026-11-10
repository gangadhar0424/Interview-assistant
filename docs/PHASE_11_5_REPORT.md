# Phase 11.5 — Desktop Session Creation + Connection-Status Fix: Report

Date: 2026-11-10. One combined batch (11.5A integration foundation + 11.5B UI, plus sub-item 11.5-F), implemented and verified automatically per the approved plan (`docs/PHASE_11_5_PLAN.md`, decision D-52). **Nothing is committed or pushed.** Phase 12 (Audio) is not started.

Note on the working tree: the earlier **Phase 11B** desktop changes (screenshot input) are still uncommitted in the same tree and are included in the file lists and test totals below; Phase 11.5 sits on top of them.

## 1. Features implemented

1. **Create a session from the desktop.** Sessions page → **New session** (also in the empty state). Fields: company name, optional job title, pasted job description, one **ready** resume (not-ready ones are shown disabled with their status), up to 10 **ready** documents, and an **Answer style** line showing the actual defaults (English · Auto · Full script · Balanced · Simple) with **Customize** (language, question type, format, length, tone, optional extra instructions). Values and labels come from `GET /ai/options` at form-open time; defaults match the web wizard, falling back to the first offered value. On success the list is refreshed, the new session is selected and the user lands on the Assistant page.
2. **No-ready-resume guidance, no dead end.** The form explains that a ready resume is required (and that this app cannot upload files), lists any resumes with their processing status, shows the web link `http://localhost:3000/sessions/new` as text with **Copy link** (known only for a local backend; otherwise it explains in words how to find the web app), and offers **Refresh** and **Back**.
3. **Connection-status fix (11.5-F).** After a successful **Refresh sessions** (and any other successful backend call such as loading history, the create form or the health check), the connection indicator becomes "connected" immediately and a stale network-error banner is cleared. A failed refresh never shows "connected"; only a network failure marks the backend unavailable (an HTTP error means the server answered); an unrelated error banner is not silently cleared. The 15-second poll and the clickable header label are unchanged.

Not built, as agreed: file uploads, editing or deleting sessions, job-link import, provider/model selection, audio, any screenshot change, any backend change.

## 2. Files changed (Phase 11.5 only)

New: `src/shared/session-form.ts` (shared validation and request building), `src/renderer/src/ui/CreateSessionPage.tsx`; tests `session-form.test.ts`, `session-create-ipc.test.ts`, `session-create-api-client.test.ts`, `session-create-store.test.ts`.
Changed: `src/main/api-client.ts` (`listFiles`, `getSessionOptions`, `createSession`, validation hints), `src/main/ipc.ts` (`sessions:create-options`, `sessions:create`), `src/preload/index.ts`, `src/shared/contracts.ts`, `src/renderer/src/store.ts` (create slice, error kinds, `markReachable`), `src/renderer/src/ui/App.tsx`, `styles.css`, `tests/capture-guards.test.ts` (bridge allow-list and new guards).
Docs: `PHASES.md`, `DECISIONS.md` (D-52), `PHASE_11_5_PLAN.md`, `DESKTOP_ARCHITECTURE.md`, `DESKTOP_MANUAL_CHECKLIST.md`, `TESTING_STRATEGY.md`, `README.md`, `CLAUDE.md`, this report.

## 3. Backend/API changes

**None.** No endpoint, contract, migration, dependency or backend file changed. The desktop reuses `POST /sessions`, `GET /resumes`, `GET /documents`, `GET /ai/options` and `GET /sessions` exactly as documented. (Finding from planning, unchanged: the `API.md` resume-list row is stale about the `ingestion`/`indexing` fields; the code is authoritative and the readiness rule matches the web.)

## 4. Security and design points

- All calls are in the Electron main process through the existing `DesktopApiClient` (bearer added there, refresh-once-on-401). The renderer receives only file names, ids, sizes and a ready/processing/failed status, the five option lists, and the new session's **summary**.
- Two narrow channels, both through the existing `handle()` wrapper (sender validation, safe errors, session-expiry handling); no generic API channel. The bridge allow-list test now lists exactly the approved channels.
- **Strict input handling in main:** the draft is parsed with exact keys and types (unknown keys such as `user_id`/`status` are refused), trimmed (never rewritten or truncated), counted in code points, and bounded (company/title 200, description 20,000, extra instructions 1,000, 10 unique UUID documents) before any request; refusals make **zero** network calls. The request body is built in one place and carries no identity, status, provider or job URL. The backend remains authoritative (readiness, ownership, enum values, operator-configured limits); its fixed messages and field hints (capped, filtered) are what the user sees.
- **A defect found by the new tests and fixed:** `createSession` first returned the backend's full session detail (including the pasted job description) to the renderer; it now returns only the summary fields.
- No logging anywhere in the main process (guard test: no `console.*` in `main/`, none in the form code or store); no storage APIs used for form content (guard test); the draft lives in renderer memory only and is dropped on success, Cancel, sign-out and session expiry.
- No new Electron capability: **no `shell.openExternal`** (guard test), no CSP, sandbox, fuse or token-storage change. `webAppUrl` setting and the "Open web app" button were not built (§7).

## 5. Verification (all actually executed)

| Check | Result |
|---|---|
| `npm run typecheck` (desktop, strict, tests included) | pass |
| `npm test` (desktop) | **270 passed** in 14 files (174 before this batch; **+96** new: `session-form` 25, `session-create-ipc` 36, `session-create-store` 20, `session-create-api-client` 13, guards +2; 25+36+20+13+2 = 96) |
| Lint | **Not run: the desktop project has no lint script or ESLint configuration** (strict `tsc` is its static check). The web app's lint is unaffected (untouched) |
| `npm run build` | pass |
| `npm run package:win` (unsigned NSIS + `win-unpacked`) | pass; fuses read back unchanged |
| Backend drift check (`test_sessions`, `test_wizard_api_flow`, `test_ai_api`, `test_sessions_schema`, `test_screenshot_api`, `test_qa_api`) against a scratch Postgres | **202 passed** (backend code is unchanged; the full backend suite was last run for 11A) |
| GitHub Actions / PR checks | **Not checked**: the `gh` CLI is not available in this environment. Please confirm CI on the pushed branch |

Tests cover: IPC sender validation (untrusted sender causes no request on either channel); invalid and oversized input (blank/over-limit fields, bad UUIDs, too many/duplicate documents, malformed enums, unknown keys, wrong types) refused before any request; the no-ready-resume data path; API error mapping (`material_not_ready`, `material_not_found`, `duplicate_material`, `too_many_documents`, `invalid_provider_selection`, `validation_error` with field hints, `429` with retry hint, network failure, `401` → one refresh → expiry reported); creation success and the exact request body; list refresh and selection of the new session; the refresh fix in both directions (success clears a stale network error and shows connected immediately; failure and network errors never show connected; HTTP errors do not flip the indicator; unrelated errors are not cleared; history load and the health check behave); drafts cleared on cancel/sign-out/expiry; typed Q&A unchanged (exact arguments and request body); screenshot suites unchanged and passing; no form content, token or job description in logs or in any payload sent to the renderer.

**Live, against the real local backend (fake LLM and vision providers, synthetic data), using the packaged app driven over CDP:**
- A real account created a session through the UI form (validation messages shown first, then filled, **Customize** changed the length, **Create session**): it appeared in the list as Ready and was selected on the Assistant page; a **typed question in the new session streamed to Complete**.
- A fresh account with **no resumes** saw the guidance panel, the link `http://localhost:3000/sessions/new`, working **Copy link** and **Refresh**, and no create form.
- **Refresh fix:** with the API stopped, ↻ showed the red banner and "Backend unavailable"; after the API returned, one press of ↻ cleared the banner and showed "Backend connected" in about **0.9 s**, with no header click and no 15 s wait.
- **Privacy:** with a sentinel job description typed and created: nothing in `localStorage`/`sessionStorage`/cookies while the form was open or after, the draft was gone after Cancel and after creating, the session-list payload contained neither the description nor a token, and no file in the app data folder contained the description or a JWT. 0 CSP violations.

## 6. Manual testing still required

The concise checklist is in `docs/DESKTOP_MANUAL_CHECKLIST.md` ("Session creation and refresh (Phase 11.5)"): create a session; the validation messages; the no-ready-resume guidance and Copy link; backend unavailable and recovery with the refresh arrow; create-session with the API stopped keeps your text; session selection and typed Q&A still work; the screenshot flow still works if enabled. The local servers used for testing are still running (see below). Not manually or automatically tested: a second monitor/high-DPI view of the new form, the installer, and the form at the expanded window size.

## 7. Known limitations and deferred items

- **"Open web app" button not built:** it needs `shell.openExternal` and a validated `webAppUrl` setting, a new Electron capability that the plan reserved for a separate owner decision. The link is shown and copyable instead. For a non-local backend the app cannot know the web address, so it explains in words (the production address is a Phase 14 matter).
- Limits are UX hints mirroring the documented backend defaults; an operator who changes the server limits will see the server's message instead (limits are not exposed by any endpoint).
- Provider/model selection, job title beyond the optional field, job-link import, uploads, and session editing/deletion remain web-only or later.
- The form is a single scrolling screen; very long resume/document lists scroll within the window (not exercised with hundreds of files; the lists are capped at 200 by the API call).
- The earlier Phase 11B items still pending owner acceptance remain as reported in `docs/PHASE_11B_REPORT.md`.

## 8. Ready for acceptance and commit?

Yes, subject to your manual run and CI confirmation: the implementation is complete for the approved scope, builds, passes all automated checks above, and was exercised end to end in the packaged app. Recommended commit boundaries when you approve: (1) the Phase 11B desktop code and docs (still uncommitted), (2) Phase 11.5 desktop code and tests, (3) Phase 11.5 docs/status (`PHASES.md`, `DECISIONS.md` D-52, plan, report, checklist, README, CLAUDE.md), splitting hunks where files mix 11B and 11.5 changes.

## 9. Recommended next batch

**Phase 12A, Real-Time Transcription**, but first its required proof-of-concept (PoC-2/PoC-3: Windows microphone and system-audio capture feasibility, latency and the STT provider choice, OQ-5), because those decide the design. A sensible batch: the PoC plus the `STTProvider` port with a deterministic fake provider and the transcript storage/UI skeleton, with real audio capture behind the same explicit-initiation, visible-indicator, no-persisted-audio rules used for screenshots. Alternatively, if you prefer to harden before adding audio: the small "Open web app" capability (one decision) and Phase 13 security/observability items.

## 10. Test environment left running

Docker Postgres/Redis and the API (`http://127.0.0.1:8000`, scratch database `ia_manual`, fake LLM and vision providers) are still running for your manual testing, with two synthetic accounts (`manual.tester@example.com`, and `noresume.tester@example.com` which has no resumes). Tell me when you are done and I will stop the servers and delete the database and accounts.
