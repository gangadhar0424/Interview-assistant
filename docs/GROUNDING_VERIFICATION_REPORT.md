# Grounding Work (Issue 1 + Issue 2) — Verification Report

Date: 2026-11-10. **Verification only: no application code, migration or existing documentation was changed to produce this report, and nothing was committed or pushed.** This file is the only addition.

## 1. Working tree, classified

| Group | Files |
|---|---|
| **Phase 10 (Electron) code** | `apps/desktop/electron.vite.config.ts`, `package.json`, `src/main/ipc.ts`, `src/main/ipc-validation.ts` (new), `src/renderer/index.html`, `src/renderer/src/ui/App.tsx`, `tests/ipc-validation.test.ts` (new), `build/` (icon, new), `scripts/generate-icon.mjs` (new) |
| **Phase 10 documentation** | new: `DESKTOP_RELEASE.md`, `PHASE_10A_REPORT.md`, `PHASE_10B_REPORT.md`, `PHASE_10B_MANUAL_TEST_PLAN.md`, `PHASE_10_CLOSURE_REPORT.md`; modified: `CLAUDE.md`, `DESKTOP_WINDOWS.md`, `DESKTOP_MANUAL_CHECKLIST.md`, `PHASES.md`; `DECISIONS.md` (D-47 addendum wording, D-49, D-50) |
| **Issue 1** (evidence-tag neutralisation in untrusted channels) | `services/api/app/preferences/prompt_builder.py` (`neutralize_evidence_tags`, `wrap_untrusted(neutralize_tags=…)`, v2 only), new `tests/test_prompt_evidence_tags.py`; part of the `ARCHITECTURE.md` "prompt-injection and grounding" line |
| **Issue 2** (citation-integrity check, D-48) | `prompt_builder.py` (`EvidenceRef`, `BuiltPrompt.evidence`), new `app/qa/grounding.py`, `app/qa/service.py`, `app/qa/repository.py`, `app/db/models.py`, `app/core/metrics.py`; tests: new `test_qa_grounding.py`, `test_qa_grounding_pipeline.py`, edits to `test_qa_service.py`, `test_qa_schema.py`; docs: `DATABASE.md`, `TESTING_STRATEGY.md`, D-48 in `DECISIONS.md` |
| **Migration 0009** | new `services/api/alembic/versions/0009_answer_grounding_check.py` (+ model change, `DATABASE.md`) |
| **Unrelated** | none found |

Files shared across groups: `prompt_builder.py` (Issue 1 + 2) and `DECISIONS.md` (Phase 10 + Issue 2 hunks). The environment also had other processes running (see §5).

## 2. What Issue 1 and Issue 2 implement

- **Issue 1:** Prompt v2 evidence tags (`[E<n> …]`) are a trust signal, so look-alikes inside any user, job-posting, history or style text (case/spacing/full-width/bracket variants, unclosed tags) are rewritten to `[quoted evidence tag…`, and retrieved passages are neutralised the same way. Only `PromptBuilder` creates tags. Prompt v1 output is unchanged byte for byte.
- **Issue 2:** After a **completed** answer finishes streaming, a pure, strict-grammar check records whether every cited tag is well formed and refers to evidence actually rendered into that request's prompt. Result `{v,status,shown_count,valid_ids,unknown_ids,malformed_count}` is stored in the new nullable `generated_answers.grounding_check` JSONB and counted in a low-cardinality metric. It never alters, delays or blocks the stream, never changes the answer status, swallows its own errors (`check_error`), and logs counts only. It is **not** proof of grounding; it is not exposed in any API/SSE/WS schema.

## 3. Migration 0009 verification (scratch database on this project's compose Postgres)

| Check | Result |
|---|---|
| Upgrade 0008 → 0009 with an existing answer row | success; existing row kept, `grounding_check` = NULL |
| Schema vs SQLAlchemy model (`alembic check`) | "No new upgrade operations detected" |
| CHECK `ck_generated_answers_grounding_check_object` | accepts SQL NULL, `{}`, `{"v":1,"status":"ok"}`; **rejects** `[]`, `"x"`, `1` and JSON `null` |
| Downgrade 0009 → 0008 | success; column and constraint gone; rows kept |
| Upgrade again | success; `alembic check` clean again |
| Grants/RLS | runtime role has table-level SELECT/INSERT/UPDATE/DELETE on `generated_answers`; RLS enabled and forced; role sees 0 rows without a tenant |

Note: a downgrade discards stored `grounding_check` values (inherent to dropping the column; data is observational only).

## 4. Tests actually executed (this project's compose Postgres/Redis; dedicated scratch databases)

| Run | Result |
|---|---|
| Grounding set: `test_qa_grounding`, `test_qa_grounding_pipeline`, `test_prompt_evidence_tags`, `test_prompt_builder`, `test_prompt_inline_strings`, `test_qa_service`, `test_qa_schema`, `test_rls` (DB-backed ones included) | **377 passed, 0 failed, 0 skipped** |
| `test_auth` (after isolating the database, see §5) | 26 passed |
| **Full backend suite** (`pytest -q`) after the migration checks | **1571 passed, 4 skipped, 2 deselected, 0 failed** (21 min) |
| Skips | 2 real-embedding tests (opt-in, need the real model), 2 Windows-only storage tests (symlinks/POSIX permissions). 2 `integration`-marked tests are deselected by the repo's default config and were **not** run |
| `ruff check`, `ruff format --check` (changed backend files), `mypy --strict app/qa app/preferences app/core/metrics.py` | clean (19 source files) |

Not run: real-embedding tests; integration-marked tests; any real LLM provider; the web (`apps/web`) test suite (untouched by this work).

## 5. Defects found

**No defect in the grounding implementation.** One environmental finding: my first run reported 24 failures and 86 errors (login 401, duplicate-signup 409). Cause: other pytest processes (not started by me) were using the shared `interview_assistant_test` database, whose `TRUNCATE`/recreate interferes with concurrent runs. Re-running on dedicated database names gave clean results. This is a test-isolation limitation, not a code defect: the conftest derives one fixed `<db>_test` name, so **two simultaneous test runs against the same Postgres collide**. Another process is still using `ia_issue12_test`; I left it and the compose containers running for that reason.

## 6. Combined review (Issue 1 + Issue 2)

- **API/streaming:** deltas and event order are unchanged (pipeline tests assert identical events with the check working, failing, or finding forged tags); the check runs only after the last delta; `AnswerDone` payload unchanged.
- **SSE/WS contracts:** no schema change; `grounding_check` appears in no client-facing schema (explicit test); contract suites (SSE golden transcript, realtime Q&A/resume) all pass in the full run.
- **Isolation:** the write goes through the existing `finish_answer(user_id, …)` in a tenant-scoped session; RLS/ownership tests pass.
- **Sensitive logging:** log fields are IDs, counts and statuses only; failure logs carry the exception type only (tests assert no answer or question text).
- **Fake provider/provider abstraction:** no change to `app/ai/`; the fake provider drives all pipeline tests.
- **Migration safety:** additive, nullable, no rewrite, reversible (§3).
- **Cost:** the check is linear in answer length with bounded per-tag scanning; no I/O.

## 7. Known limitations

- Citation integrity only: a valid tag does not show the cited excerpt supports the claim; uncited or invented claims pass (`no_citations` is informational).
- Cancelled/errored answers are not checked (`NULL`); pre-0009 rows are `NULL`.
- Neutralisation rewrites tag-like text a user types into a question or job description (as seen by the model only; stored text is unchanged).
- Real model citation behaviour (does it actually emit `[E<n>]`) is unverified without a real provider.
- Test isolation limitation in §5.

## 8. Recommended commit boundaries (for your approval; nothing committed)

1. **Backend grounding** — `app/preferences/prompt_builder.py`, `app/qa/*`, `app/db/models.py`, `app/core/metrics.py`, migration `0009`, the backend tests, plus the grounding doc hunks (`ARCHITECTURE.md`, `DATABASE.md`, `TESTING_STRATEGY.md`, D-48). `prompt_builder.py` mixes Issue 1 and 2, so one commit is simplest; splitting needs `git add -p`.
2. **Desktop Phase 10 code** — everything under `apps/desktop`.
3. **Phase 10 documentation/status** — the Phase 10 docs, `CLAUDE.md`, `PHASES.md`, `DESKTOP_*`, and the D-47/D-49/D-50 hunks of `DECISIONS.md` (needs `git add -p` because that file also holds D-48), plus this report if you want to keep it.

Suggested order: 1, 2, 3 (or 2, 1, 3). Line-ending warnings (LF→CRLF) are cosmetic.

## 9. State left behind

My scratch databases (`ia_mig0009`, `ia_verify`, `ia_verify_test`) were dropped. Compose Postgres/Redis and Docker Desktop are left running because another process is using `ia_issue12_test`. The Electron app and Phase 10 files were not modified in this step.
