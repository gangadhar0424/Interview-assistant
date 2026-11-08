# Phase 7D — Manual Verification Checklist (web wizard and dashboard)

Browser-level behaviour is **not** covered by automated tests (no Playwright, owner decision, D-37).
Automated coverage is: `npm run lint`, `npm run typecheck`, `npm run build`, `npm test` (pure logic
and source guards) and the backend API-level tests (`tests/test_wizard_api_flow.py`). Everything
below must be checked by a person in a real browser.

**Status:** not yet executed (the automation that built Phase 7D has no browser). Record the date,
browser and result next to each item when you run it.

## 0. Setup
1. `docker compose up -d postgres redis` and `uv run alembic upgrade head` (in `services/api`).
2. Start the API (`uv run uvicorn app.main:app --port 8000`) **and the worker** (resumes only become
   Ready after the Dramatiq worker has extracted and indexed them). Set `LLM_ENABLED_PROVIDERS=fake`
   to see provider choices; leave it unset to see the "server default" text.
3. Start the web app (`npm run dev` in `apps/web`) and open http://localhost:3000.
4. Have: one PDF/DOCX resume, two TXT documents, a small TXT/PDF that will fail (e.g. an image-only
   PDF), a public job page link, and a second user account.

## 1. Dashboard ("Call Sessions")
- [ ] New user: dashboard shows the empty-state text and a **Create session** button.
- [ ] Resumes/Documents panels still work (upload, status, retry, download, delete).
- [ ] After a session exists: shows company, optional title, status badge (Ready), created date,
      resume file name and document count, newest first.
- [ ] More than 20 sessions: **Load more** appends the next page without duplicates.
- [ ] Deleting the session's resume afterwards shows "(deleted)" next to the resume name.
- [ ] Log in as the second user: their dashboard shows none of the first user's sessions.

## 2. Wizard access
- [ ] Opening `/sessions/new` while logged out redirects to `/login`.
- [ ] While logged in, **Create session** opens the wizard; "Back to dashboard" returns.
- [ ] Loading state appears first; if the API is stopped, a clear error is shown (no blank page).

## 3. Step 1 — job and materials
- [ ] Step indicator reads "Step 1 of 2"; focus lands on the heading.
- [ ] **Next** with an empty form shows errors for company, job description and resume (and no others).
- [ ] Company/title: 200 characters accepted, 201 rejected. Job description counter updates; exactly
      20,000 characters accepted, 20,001 rejected (try pasted Hindi/Telugu text too).
- [ ] Resume list shows **only Ready** files; a processing or failed file is not selectable and the
      hint says how many are still processing/failed. **Refresh files** updates the list; a file that
      becomes Ready appears after refresh. The "Upload or check files" link goes to the dashboard
      file section. There is **no** upload control inside the wizard.
- [ ] Documents: checkbox list of Ready files only; the counter reads "n of 10 chosen"; once 10 are
      chosen the remaining boxes are disabled; unticking re-enables them.
- [ ] If a chosen file is deleted in another tab, **Refresh files** drops it from the selection.

## 4. Job link import
- [ ] A valid public job page with structured data: **Import details** fills company, job title and
      description and says what was filled. Check the text is shown as plain text (no formatting).
- [ ] If you had already typed a company or description, import does **not** overwrite it and says
      what it kept.
- [ ] Invalid link text (`not a url`, `ftp://…`) shows an inline error and does not call the server.
- [ ] A link to an internal address (`http://127.0.0.1/`, `http://192.168.0.1/`) gives the generic
      "page could not be retrieved" message, the same as a dead link. Every field stays editable.
- [ ] A non-HTML link (a PDF URL) and a page with no job info give their own short messages.
- [ ] Clicking import repeatedly (> 10 times in a minute) shows the rate-limit message, and manual
      entry still works afterwards.
- [ ] With the API stopped, import shows an error message and the form is still usable.

## 5. Step 2 — preferences
- [ ] **Next** with valid step-1 data shows "Step 2 of 2"; **Back** returns with all values kept.
- [ ] Language shows English/Hindi/Telugu; the other selects list the server's values with readable
      labels (e.g. "Tell me about yourself", "Script as bullets").
- [ ] With no provider enabled: text says the server default is used. With `fake` enabled: provider
      (default "Server default") and model selects appear; changing provider resets the model.
- [ ] Extra instructions: counter updates; 1,000 characters accepted, 1,001 rejected.

## 6. Create
- [ ] **Create session** shows "Creating…", cannot be double-clicked into two sessions, then returns
      to the dashboard with the new session at the top.
- [ ] Press Enter in a text field on step 1: goes to step 2 (does not submit); on step 2 it creates.
- [ ] Delete a selected resume in another tab, then create: the wizard returns to step 1 with the
      "no longer exists" message and keeps what you typed.
- [ ] Stop the API, then create: a clear error appears, the button re-enables, nothing is lost.

## 7. Security spot checks (browser dev tools)
- [ ] Network tab: requests go only to `/api/v1/…` on the web origin; none contain a user id.
- [ ] Application tab: no access token in localStorage/sessionStorage; only the HttpOnly refresh
      cookie.
- [ ] Import a page whose title contains `<img src=x onerror=alert(1)>`: it appears as literal text
      and no alert runs.

## 8. Accessibility and layout
- [ ] Everything is reachable and operable with the keyboard; labels are read by a screen reader;
      validation messages are announced.
- [ ] At phone width (~360 px) there is no horizontal scrolling and buttons remain usable.
