# Phase 8C — Manual Verification Checklist (typed Q&A page)

Browser-level behaviour is **not** covered by automated tests (no Playwright, same rule as D-37).
Automated coverage is: `npm run lint`, `npm run typecheck`, `npm run build`, `npm test` (SSE
parser, event validation, state machine, `runAsk` flows, streaming request, source guards) and the
backend contract test `tests/test_qa_sse_contract.py` (golden SSE transcript shared with the web
tests). Everything below must be checked by a person in a real browser.

**Status:** not yet executed by a person. An automated real-stack smoke test (Next proxy, real API,
real worker, dedicated database, no browser) passed on 2026-11-06: streaming spread over 12 s with no
`Content-Encoding`, `[fail]`/`[partial]` errors, a disconnect stored as `cancelled` with partial text,
406 and 404 refusals. Record the date, browser and result next to each item when you run it.

## 0. Setup
The built-in `fake` provider answers instantly, so use the dev-only launcher
`services/api/tests/manual_qa_server.py` (slow canned answer, nothing under `app/` changed, refuses to
start unless `APP_ENV=local`). Use a separate database so your dev data is untouched (PowerShell,
from the repo root):
1. `docker compose up -d postgres redis`
2. One-time: create the database and migrate it:
   `docker exec interview-assistant-postgres-1 psql -U <POSTGRES_USER> -d postgres -c "CREATE DATABASE interview_assistant_manual"`
3. In the terminals that run alembic, the API and the worker, first run (the web app does not need it):
   `$env:DATABASE_URL = ((Get-Content .env | Where-Object { $_ -match '^DATABASE_URL=' }) -replace '^DATABASE_URL=','') -replace '/interview_assistant$','/interview_assistant_manual'`
4. `cd services/api; uv run alembic upgrade head` (one time), then:
   - API: `uv run uvicorn tests.manual_qa_server:app --port 8000`
   - Worker (resumes only become Ready after it runs): `uv run dramatiq app.workers.tasks --processes 1 --threads 2`
   - Web: `cd apps/web; npm run dev` (or `npm run build; npm start`), then open http://localhost:3000
5. Optional: `$env:MANUAL_QA_TOKEN_DELAY = "0.15"` (seconds per word) and `$env:QA_RATE_LIMIT_PER_MINUTE = "3"`
   before starting the API, to reach the rate limit quickly.
6. Failure markers: put `[fail]` (provider unavailable, no text) or `[partial]` (fails after 8 words)
   anywhere in a question.
7. Have: a Ready resume (upload, wait for Ready), a Ready session (create it with the wizard), a
   second user account, and a session whose selected document you can delete afterwards.

## 1. Entry
- [ ] Dashboard: the company name in "Call Sessions" is a link; it opens `/sessions/<id>`.
- [ ] Signed out: `/sessions/<id>` redirects to `/login`. A made-up id (`/sessions/abc`) shows
      "This session was not found." and makes no API call (Network tab).
- [ ] Another user's session id shows "This session was not found." (same text as an unknown id).

## 2. Asking and streaming (production build: `npm run build && npm start`)
- [ ] Ask "Tell me about yourself": the status goes Sending, Searching, Writing, Answering; text
      appears **gradually** (not all at once), then "Done." with provider/model and timings.
- [ ] The question box is cleared after acceptance; Ask is disabled while answering.
- [ ] Question/answer text containing `<b>x</b>` or `<script>` shows as literal text.
- [ ] The finished turn appears in "Earlier questions" after a reload, oldest first.
- [ ] Question type dropdown: choosing a type changes the stored type of that question only.

## 3. Stop, navigation and failures
- [ ] **Stop** mid-answer: "Stopped. The part generated so far is kept."; after reload the answer is
      listed with "Stopped before it finished." (server status `cancelled`).
- [ ] Navigating away (Back to dashboard, closing the tab) mid-answer leaves a `cancelled` answer,
      never a `streaming` one, and the next question works immediately (no slot leak).
- [ ] Provider failure (ask with `[fail]`, then with `[partial]`): "The answer could not be
      completed." plus a plain sentence ("The AI service is unavailable right now."); with `[partial]`
      the first words stay visible; **no** stack trace or raw server text anywhere.
- [ ] 21 questions within a minute (or 4 with `QA_RATE_LIMIT_PER_MINUTE=3`): "You are asking too
      quickly. Try again in N seconds."
- [ ] Two tabs asking at once, then a third attempt: "Another answer is still being generated...".
- [ ] Delete a selected document, then ask: "A resume or document selected for this session was
      deleted or is no longer Ready..." and nothing appears in the history.
- [ ] Empty question: "Type a question first."; over 2000 characters: a length message, nothing sent.
- [ ] A session that is not `ready`: the form is disabled with an explanation.
- [ ] Access token expiry (wait 15 min or clear the cookie mid-session): the next ask refreshes once
      and works, or sends you to login if the refresh cookie is gone.

## 4. Browser and layout
- [ ] Mobile width (375 px): no horizontal scroll; long unbroken text wraps.
- [ ] Keyboard / screen reader: the status line is announced politely, the form is reachable by Tab,
      focus is not stolen while text streams.
- [ ] Network tab: the POST shows `Content-Type: text/event-stream` and no `Content-Encoding`.
