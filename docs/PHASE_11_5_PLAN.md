# Phase 11.5 — Desktop Session Creation: Plan

Status: **IMPLEMENTED in one combined batch (11.5A + 11.5B) and verified automatically; see [PHASE_11_5_REPORT.md](./PHASE_11_5_REPORT.md).** The text below is the approved plan; the implementation deviations are listed in the report (notably: the link to the web app is shown and copyable, and the `shell.openExternal` "Open web app" button is deferred pending a separate owner decision).

Written for: the project owner, as the review basis for approving the phase.

## 1. Why this phase, and where it goes

**Placement: after Phase 11 (Screenshot Input) and before Phase 12A (Audio), named "Phase 11.5" with sub-phases 11.5A and 11.5B.**

- The roadmap's later phases (12A, 12B, 13, 14) are referenced by number in `CLAUDE.md`, `DECISIONS.md`, `RISKS_AND_OPEN_QUESTIONS.md` and the PRD, and the repository has no precedent for renumbering. A fractional number inserts the phase without rewriting any completed or planned phase. (If the owner prefers integer numbering, the alternative is to renumber 12A–14 in one dedicated documentation change; that is a decision, see §12.)
- A desktop-only user currently hits a dead end: the Sessions page says "Create a session in the web app" and nothing else. Closing that gap completes the existing desktop workflow (sign in → create/select session → ask, with typed or screenshot input).
- It reuses backend functionality that already exists and is tested (sessions, resume/document listing, AI options). It needs **no** new backend feature, API contract, migration, AI provider or real model.
- It is a small, independently testable change that should be stable before a major new input modality (audio, Phase 12A, which also needs proof-of-concept work first) is added to the same window.

PRD check: session creation (FR-SES-1, journey J3) and the desktop client (J5) are in scope; "create a session from the desktop" is a new surface for an in-scope feature rather than a new feature. The owner should confirm this reading (§12).

## 2. Verified contracts (checked in the code, not assumed)

| Contract | Verified fact (source) |
|---|---|
| Create session | `POST /api/v1/sessions` → `201` session detail; the new session's `status` is **`ready`** immediately (`sessions/service.py`). Body: `type:"interview"`, optional `title` (≤200), `company_name` (1–200), optional `job_url` (≤2048, stored, never fetched), `job_description` (required), `resume_id` (required), `document_ids` (default `[]`), `preferences`. Unknown fields are rejected (`extra="forbid"`) (`sessions/schemas.py`, `API.md`) |
| Limits | job description ≤ 20,000 characters after cleaning; extra instructions ≤ 1,000 after cleaning; documents ≤ 10 with no duplicates; over-limit text is rejected, never truncated (D-35). These are **server settings** (`SESSION_JOB_DESCRIPTION_MAX_CHARS`, `SESSION_MAX_DOCUMENTS`, `EXTRA_INSTRUCTIONS_MAX_CHARS`), not exposed by any endpoint |
| Preferences | required `language`, `question_type`, `answer_format`, `answer_length`, `answer_tone`; optional `extra_instructions`, `provider`, `model` (omitted = server default chain; a model needs its provider) |
| Preference values | `GET /api/v1/ai/options` returns `languages` (en/hi/te), `question_types` (9, incl. `auto`), `formats`, `lengths`, `tones` straight from the backend enums, plus the server provider allow-list (`ai/router_api.py`) |
| Resume/document lists | `GET /api/v1/resumes` and `/documents`, `?limit=` 1–200 (default 100); each item carries `ingestion` and `indexing` statuses. **Ready = both `completed`** (`sessions/materials.py`; same rule as the web `isReady`) |
| Errors on create | `404 material_not_found` (unknown or foreign id, indistinguishable), `422 material_not_ready`, `422 duplicate_material`, `422 too_many_documents`, `422 validation_error`, `422 invalid_provider_selection`; messages are fixed and echo no input (`API.md`, error handler) |
| Existing session list | `GET /api/v1/sessions` (`limit` ≤ 100, cursor) is already used by the desktop (`api-client.listSessions`); `PATCH`/`DELETE /sessions/{id}` exist and stay out of scope |
| Web defaults to reuse | the web wizard (`apps/web/lib/wizard.ts`) starts with `en` / `auto` / `full_script` / `balanced` / `simple`, falls back to the first offered value if a default is not offered, mirrors the limits as UX hints, and lets the API be the authority |
| Desktop link handling | the desktop denies **all** navigation and `window.open` and has **no** `shell.openExternal` anywhere (`main/index.ts`); there is no setting for the web app's address |

Findings to carry into the work: (a) `API.md`'s `GET /resumes` row still says "no `status`", which is stale: the code returns `ingestion`/`indexing`; the code is authoritative and the doc line is fixed in 11.5A. (b) There is no generated REST client; `packages/protocol` covers the WebSocket only, so desktop REST types are hand-written (as for existing desktop calls).

## 3. Objective and smallest useful MVP

**Objective:** a desktop-only user can create a useful interview session, and then ask questions in it, without the web app for the normal workflow.

**MVP form (one screen, reachable from the Sessions page):**

- **Company name** (required), **Job title** (optional; it is the display title in the session list), **Job description** (required, pasted).
- **Resume:** choose exactly one **ready** resume; not-ready resumes are listed disabled with their status. No default selection.
- **Documents:** choose 0 to the server limit (10 by default) of **ready** documents; the cap is enforced in the UI and rechecked by the server.
- **Answer style:** language, question type, answer format, answer length, tone, optional extra instructions. Values and labels come from `GET /ai/options` at form-open time (nothing hard-coded or invented); defaults are the same as the web wizard's.
- **Provider/model: not in the MVP** (see §11). The session uses the server default chain, exactly as the web does when the field is left empty.
- After success: refresh the session list, select the new session, and take the user to the Assistant page.

### Quick Start (recommended shape)

The web wizard already starts every preference at a sensible default, so a separate "mode" would add a second path to build and test without removing any decision from the user. Recommendation: **one form whose Answer-style section is collapsed by default and shows the actual chosen values in one line ("English · Auto · Full script · Balanced · Simple")**, with a visible "Customize" control. The **Create session** button is enabled only when the minimum is present: non-blank company, non-blank job description within limits, and an explicitly selected ready resume. This gives the "quick" path you prefer, never creates a session with hidden or misleading assumptions (the defaults are shown before creating), and keeps one normal form. If the owner would rather have a distinct Quick Start button, the same validation applies; it is a UI choice, not an architecture one (§12).

## 4. Explicit scope boundaries (not in this phase)

Uploading resumes or documents from the desktop; editing or deleting sessions; job-link import (§7); provider/model selection; audio; any screenshot change; any backend feature, contract change or migration. Files and document management stay in the web app.

### No ready resume (no dead end)

If the user has no ready resume (none uploaded, or all still processing/failed), the form shows a plain explanation ("A ready resume is required. Upload one in the web app; this app can't upload files yet.") with the resume list's statuses, plus a **Refresh** action and a safe way to reach the web app:

- **Recommended:** an **Open web app** button. This needs one new, narrow Electron capability that does not exist today: opening the user's default browser. To keep it safe: a purpose-specific `app:open-web-app` IPC with **no renderer-supplied URL**; the main process builds the URL from a new, validated `webAppUrl` setting (same rules as the backend URL: HTTPS, or HTTP only on loopback; no credentials, query or fragment) plus the fixed path `/sessions/new`; sender validation as for every handler; `shell.openExternal` appears in exactly one module (source-guard test); existing navigation and `window.open` blocking stay unchanged; no token is ever put in the URL.
- **Always shown as a fallback:** the link as text with a **Copy link** button, using the existing `app:copy-text` channel, so the user is never stuck even if no web address is configured.
- The web app's address is not known to the desktop today (the backend URL is the API's origin). The default for local development is `http://localhost:3000` (documented in `QA_MANUAL_CHECKLIST.md`); production needs it set (Phase 14). See §12.

## 5. Architecture and reuse

All backend calls stay in the Electron **main** process through the existing `DesktopApiClient` (bearer token added there; refresh-on-401 pattern reused). Proposed narrow channels, all through the existing `handle()` wrapper with sender validation and safe `DesktopResult` errors:

| Channel | Direction | Purpose |
|---|---|---|
| `sessions:create-options` | invoke, no arguments | loads, in one call, ready/not-ready resumes and documents (id, filename, size, status only) and the AI options |
| `sessions:create` | invoke, one validated object | validates and sends the create request; returns the new session summary |
| `app:open-web-app` | invoke, no arguments | opens the configured web address in the default browser (§4) |

Rules: no generic "call API" channel; strict payload validation in main (UUID ids, type checks, `extra` keys rejected, length limits as hints); the renderer never receives tokens, only the lists above; identity is always the signed-in user (no `user_id` in any payload). CSP, context isolation, sandbox, fuses and token storage are unchanged. No new npm dependency. The existing Zustand store gains a create-session slice and reuses `sessions`, `selectedSessionId` and `refreshSessions` rather than a parallel session system.

## 6. Security and privacy

- **Untrusted text.** The job description and extra instructions are untrusted. The desktop trims, counts in code points (as the server does) and refuses over-limit text with a clear message; it does not truncate or otherwise rewrite it. Cleaning (control characters, normalisation) stays on the backend, which is authoritative; the desktop's limits are UX hints that mirror the documented defaults, so they cannot conflict with the server (if the operator changed a server limit, the server's `422` message is shown). Required fields and enum values are checked against `/ai/options`, never against a copy.
- **Safe errors.** Known codes map to plain messages; any other server message is shown only if it is one of the backend's fixed strings (the validation handler never echoes input); field-level hints use the backend's `details[].field/message`.
- **No logging or telemetry of form content.** The main process has no logging of request bodies; a guard test asserts the new modules contain no `console.*`/log calls and that job descriptions and instructions never appear in any emitted event or error.
- **No new persistence.** Unfinished form data lives only in renderer memory for the app run and is cleared on success, cancel, sign-out, session expiry and quit. Nothing is written to settings or disk. The only persisted new value is the `webAppUrl` setting (a URL, not user content), if approved.
- Existing protections stay: ownership checks and `404`s on the backend, rate limits where they exist, no secrets or provider keys on the desktop.

## 7. Job-link import: not in the MVP

The backend endpoint is safe (SSRF-protected, failures are `200 failed`), but exposing it adds UI and state that the MVP does not need: a second input path with `succeeded`/`partial`/`failed` results to review, autofill that can overwrite what the user typed, its own rate-limit states (`429 rate_limited`, `429 import_busy` with `Retry-After`), and fixed-message handling. Pasting the description works for every case and is the web wizard's documented manual fallback. **Recommendation: leave it out; add it later as an optional enhancement (11.5C, separate approval)** that calls the existing endpoint from the main process and treats the result as untrusted prefill. `job_url` is therefore not sent by the MVP.

## 8. Connection-status usability fix (sub-item 11.5-F, done first in 11.5A)

Current behaviour: "Refresh sessions" reloads the list but neither clears a stale "Could not reach the backend…" banner nor updates the connection indicator; the indicator is re-checked only by the 15-second timer or by clicking the header label.

Planned behaviour, reusing the existing `connection` state and `checkBackend` mechanism (no second connectivity state):

- Any authenticated backend call that **succeeds** (starting with refresh-sessions) marks the connection `connected` immediately and clears the error banner **if that banner came from a network failure**. The store records the error's kind (the IPC result already carries `kind: "network"`; today it is lost when the message is unwrapped), so unrelated errors (for example a failed question) are not silently cleared.
- A **failed** refresh never marks the backend connected. Only a `network`-kind failure marks it `unavailable`; an HTTP error response (401, 422, 5xx) proves the server answered, so the indicator is left as it was and the specific error is shown.
- The 15-second poll and the clickable header label keep working unchanged.

Automated tests: a successful refresh clears a stale network error; a successful refresh sets the indicator to connected without waiting for the timer; a failed refresh (network) does not mark connected and keeps/sets the unavailable state; a non-network failure does not flip the indicator; an unrelated error is not cleared by a successful refresh; the periodic check and the header-label check still update the state.

## 9. Phase split

**11.5A — Desktop integration foundation (no UI)**
1. 11.5-F connection-status fix and its tests.
2. `DesktopApiClient`: `listResumes`, `listDocuments`, `getAiOptions`, `createSession`; error mapping; refresh-on-401 as for existing calls.
3. IPC `sessions:create-options` and `sessions:create`, input validation module (pure, tested), sender validation; `app:open-web-app` plus the `webAppUrl` setting **only if approved** (otherwise the copy-link fallback only).
4. Orchestration in main: validate → create → return the summary; ready filtering for materials.
5. Contract types in `shared/contracts.ts`; doc fix for the stale `API.md` resume row; decision record accepted.
6. Tests (§10), including a live check of the real client against the real backend with fake providers.

**11.5B — Desktop UI and workflow**
1. Create-session form (company, title, description, resume, documents, collapsed answer style with visible defaults), validation messages, character counters.
2. No-ready-resume state with Refresh, Open web app and Copy link.
3. Entry points: a "New session" button on the Sessions page and in the empty state.
4. Success path: refresh list, select new session, go to Assistant; failure states with safe messages; backend-unavailable and recovery.
5. Draft handling in memory only; cleared on success, cancel, sign-out, expiry.
6. Styling consistent with the existing window sizes (compact and expanded), manual validation, docs and manual checklist updates.

Each sub-phase is independently testable; 11.5B does not start until 11.5A is accepted.

## 10. Test plan (planned, none run yet)

**Automated (desktop, deterministic, fake backend and the existing fake providers):**
- IPC sender validation on every new channel (an untrusted sender causes no network call, no browser launch, no state change).
- Invalid and oversized input: blank/whitespace company and description, over-limit company/title/description/instructions (counted in code points), non-UUID ids, unknown keys, wrong types, values not in `/ai/options`.
- Required fields; resume required; document limit and duplicates; non-ready materials refused.
- No-ready-resume state (none, all pending, all failed).
- API error mapping for `404/422` codes, `429`, `401` (refresh once), network failure and backend unavailable.
- Create success; session list refresh; new session selected; the session appears without restart.
- Regression: typed Q&A request body and flow unchanged; screenshot flow unchanged (existing `capture-*` suites keep passing).
- The renderer cannot access tokens (bridge/event payload assertions); bridge allow-list updated to exactly the new channels; `shell.openExternal` only in the one module; no `console.*` or content in logs; no new storage writes beyond `webAppUrl`.
- 11.5-F tests (§8).
- Live compatibility run: the compiled desktop client against a real local backend (fake LLM and vision providers, scratch database), creating a session and asking one question.

**Always:** desktop `typecheck`, all desktop unit tests, production build, `package:win` and the packaged-app launch smoke (CDP), plus the backend suites that cover sessions (`test_sessions`, `test_wizard_api_flow`) as a drift check, since no backend change is expected.

## 11. Acceptance criteria

1. A signed-in desktop user can create a valid session through the existing API.
2. The new ready session appears in the desktop list and is selected, without a restart.
3. A resume must be chosen explicitly and be ready; documents are ready-only and respect the backend limit.
4. Preference values and labels come from `/ai/options`; defaults match the web wizard.
5. With no ready resume the user sees clear guidance and a safe path to the web app (open or copy link).
6. Invalid input is refused with useful messages and no backend internals.
7. Tokens never reach the renderer; every new IPC channel validates sender and input; no generic API channel exists.
8. Typed Q&A and the screenshot flow are unchanged.
9. 11.5-F behaves as in §8.
10. No backend contract, migration or dependency change; CSP, sandbox, fuses and token storage unchanged.
11. Typecheck, tests, build and packaged-app smoke pass; the owner's manual checklist passes (multi-monitor may stay "not tested" as before).

**Manual checklist (for 11.5B):** sign in; open New session; create a normal session (company, title, description, a ready resume, two documents); check the answer-style summary and change one preference; verify the new session appears, is selected and can be used; ask a typed question; sign in with an account that has no ready resume and check the guidance, Refresh and the web-app link/copy; stop and restart the API and confirm the error and recovery (including the refresh-clears-banner behaviour); confirm the draft is gone after sign-out.

## 12. Dependencies, risks and owner decisions

**Dependencies:** the session, resume/document and AI-options APIs (verified above); the existing desktop store, API client and IPC pattern. Not needed: any new AI infrastructure, a real provider (the fake providers suffice for all tests), a migration, or a backend change.

**Risks:** (1) *Option or limit drift* between desktop and backend: mitigated because enum values and labels are read at runtime from `/ai/options`, limits are hints with the server authoritative, the existing web/backend contract tests act as a drift check, and the live compatibility run is part of 11.5A. (2) *Too much form for a small window*: mitigated by collapsed answer style, one screen, no provider/model and no job-link import. (3) *Stale list/connection state*: addressed by 11.5-F and by refreshing after creation. (4) *New browser-launch capability*: mitigated by the main-built URL, a validated setting, a single module and a guard test, or avoided entirely with copy-link only.

**Owner decisions that remain after inspecting the code:**
1. **Numbering:** "Phase 11.5" (recommended; no renumbering) or renumber 12A–14.
2. **Web-app link:** approve `shell.openExternal` through `app:open-web-app` and a new `webAppUrl` setting (recommended, with copy-link always shown), or copy-link only (no new capability). The web app's production address is unknown until deployment (Phase 14), so it must be configurable.
3. **Quick Start shape:** one form with collapsed, visible defaults (recommended) or a separate Quick Start button.
4. **Optional job title** in the MVP (recommended: yes) and **no provider/model selection** (recommended).
5. **PRD reading:** confirm that desktop session creation is an extension of in-scope FR-SES-1/J3 and J5.

Already resolved by the repository (not open): the ready-resume rule, the document limit, the preference values and defaults, that no migration or backend change is needed, that fake providers suffice, and that job-link import and uploads are out of scope.
