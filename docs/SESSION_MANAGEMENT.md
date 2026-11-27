# Session management (M-9, D-74)

Status: implemented 2026-11-19, pending owner review. Contract: `docs/API.md` (Sessions). Decision: `docs/DECISIONS.md` D-74. No database migration.

## What a session is

One interview preparation: a company, a job description the user entered, one resume, up to ten supporting documents, answer preferences (language, question type, format, length, tone, answer provider and voice provider), and the questions and answers asked in it. Everything belongs to exactly one user.

## Lifecycle and states

Stored `status` (unchanged domain, `sessions.status`): `draft`, `ready`, `active`, `ended`, `archived`.

| Transition | How | Notes |
|---|---|---|
| (new) → `ready` | `POST /sessions` | Materials must be owned and ready. |
| `ready`/`active` → `ended` | `POST /sessions/{id}/end` | "Complete". Idempotent. Questions and handoff are refused afterwards; only the title can change. History is kept. |
| `ended` → `ready` | `POST /sessions/{id}/reopen` | Idempotent. Starts no audio, no screenshot, no answer. |
| any → deleted | `DELETE /sessions/{id}` | Hard delete; questions and answers cascade. Resume and documents are never touched. |

`draft` and `archived` are reserved; the API refuses to end or reopen them (`409 session_not_transitionable`). Nothing sets `active` today.

**Displayed state** (`activity_state`, derived on every read, never stored):

- `completed`: status is `ended` or `archived`.
- `interrupted`: the newest answer of the session was cut off, meaning it is stored as `error` with `generation_interrupted`, or it is still `streaming` for longer than `QA_REQUEST_TIMEOUT_SECONDS` (no generation can legitimately run that long). A later good answer clears it.
- `active`: everything else, including a brand-new session.

`last_activity_at` is the later of the last edit and the last question. `question_count` is the number of questions. The list is still ordered by creation time (the pagination cursor is unchanged); activity is shown, not sorted on.

## Data ownership and isolation

- Every session query takes the authenticated `user_id` (`sessions/repository.py`); there is no lookup by session id alone. Database row-level security is a second layer (migration 0008).
- An unknown id and another user's id return the **same** `404 session_not_found` for get, edit, end, reopen, export and delete (tested field by field).
- Resume and job description are stored **per session** (`resume_id`/`resume_snapshot`, `job_description`). Editing or switching sessions never copies or shares them; a new session starts with the values in its own create request, an empty question history and its own preferences. Answers are generated from the session being asked, and the history endpoints filter by session and user.
- If a session's resume or document was deleted afterwards, the session shows the saved file name marked deleted, and asking a question returns `409 materials_not_ready` (existing behaviour), never another session's material.
- Desktop: switching sessions stops audio, discards a pending screenshot, cancels a running answer, and clears the previous session's history and answer **before** the new history loads, so a failed load shows nothing rather than the old session's content.

## Resume behaviour

- "Resume" means reopening a session and carrying on. The server keeps all history, so reopening a completed session (`reopen`) or opening an interrupted one shows its questions and answers again. An interrupted answer appears in the history as an error (`generation_interrupted`); the user asks again. Nothing regenerates automatically, and one generation per question is enforced as before (per-user concurrent-generation cap, one live answer per desktop window).
- The desktop opens, when none is chosen, the most recently used `ready` session. This only **selects**; audio, screenshots and answers always need the user's own action. Existing detached generation and WebSocket resume (D-43) are unchanged.
- **Network failure** while switching: the previous session's content is already cleared, a clear banner is shown, and opening the session again retries. **Expired sign-in:** the existing refresh path runs; if it fails the user is signed out as before. **A session deleted elsewhere:** the action reports "This session is no longer available" and the list refreshes.

## Duplicate requests

`POST /sessions` accepts an optional `Idempotency-Key` header (8 to 128 characters of `A-Za-z0-9_.:-`). A retry of the same create with the same key returns the session already made (`200`, not a second record). Keys are scoped to the user (stored as a hash of user id and key in Redis for 24 hours), a create still running returns `409 request_in_progress`, a failed create releases its key, and a key whose session was deleted creates a new one. Without Redis the create proceeds unprotected (see limitations). The web wizard and the desktop create form both send a key and reuse it only when the identical request is retried.

## Export

User triggered only. `GET /sessions/{id}/export?format=markdown|pdf` (default `markdown`; JSON was replaced by PDF, D-76) returns one owned session as a download (`Content-Disposition: attachment`, `Cache-Control: no-store`; the file name is `session-<first 8 characters of the id>.md|pdf`, with no company or title).

- **Web:** a confirmation states what the file contains and that nothing is sent anywhere, then the browser saves it.
- **Desktop:** the main process shows the same warning in a native dialog, asks for a path in a native save dialog, fetches the export **only after a path is chosen**, and writes it there (mode 0600). The renderer never receives the content or the path, only the saved file name.

**Format** (`interview-assistant.session-export`, version 1): `format`, `version`, `exported_at`, `contents` and `excluded` (plain-language lists that travel inside the file), `session` (title, company, job link, job description, status, state, created and last-activity time, resume file name, document file names, preferences including provider and model names), `questions` (oldest first; asked-at time, source, question type, text, and its answers with status, text, provider, model, source kind, time) and `truncated` (at most 2,000 questions). The Markdown form carries the same content.

**Included:** what the service already stores for the session. **Never included:** resume or document text, audio, transcripts and screenshots (never stored), prompts, retrieved passages, embeddings, passwords, tokens, API keys, e-mail address or user id. Tests assert this for both formats.

## Data retention

Unchanged (`docs/PRIVACY.md`): questions and answers are stored until the user deletes the session; there is no automatic expiry. Export adds **no** server-side storage: the file is produced on request and not kept, and there is no permanent transcript store. A file the user saves is outside the service's control and is not removed when a session is deleted (the confirmation says so).

## Known limitations

- `interrupted` is a derived hint from the newest answer; it is not a stored status and there is no "resume this exact answer" (a cut-off answer is asked again).
- Nothing sets `active`; "Active" in the UI is a display state of a `ready` session.
- The list is ordered by creation time, not by last activity (the desktop opens the most recently used session by activity).
- Idempotency needs Redis; during a Redis outage creates still work but a lost-response retry can create a duplicate.
- Export covers up to 2,000 questions per session and cannot include audio or transcripts (not stored).
- Web: the session page itself has no complete/reopen/export controls; they live on the dashboard list. The web Q&A page was left unchanged.
- Delete, export and reopen were verified by automated tests only. The desktop dialogs and the browser download need the manual checks in `docs/DESKTOP_MANUAL_CHECKLIST.md` (M-9).
