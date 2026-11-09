# Merge Completion Report — `origin/dheeraj-branch`

Date: 2026-11-09. Companion to `docs/BRANCH_MERGE_ANALYSIS.md`.
**Status: integrated into the working tree, verified, NOT committed, NOT pushed. Awaiting owner review.**

## 1. Changes successfully integrated

The whole branch (1 commit, `66b7248`, 98 files, +13,480/−429, 0 deletions) is staged as an uncommitted merge.

| Feature | Integrated | Notes |
|---|---|---|
| Tenant RLS, migration `0008_tenant_rls` (D-45) | Yes, with fixes (§2) | 13 tables, forced RLS, `interview_app_runtime` role, maintenance DB URL for recovery/reindex |
| OpenAI-compatible adapter (D-44) | Yes | Mock-tested only; fake provider retained |
| Prompt v2 + evidence tags | Yes | v1 still selectable |
| RAG evaluation harness | Yes | Synthetic, fake embedder; no quality claim |
| Prometheus metrics, `GET /metrics` (D-46) | Yes | Unauthenticated by design: restrict at network layer |
| Production config hardening | Yes | JWT ≥43 URL-safe chars (local min was 32), `rediss://`, maintenance URL required outside local |
| Electron desktop MVP (D-47) | Yes | See §7 |
| Docs (security review, privacy, operations, desktop, RAG, baseline) | Yes | See caveats in the analysis report |

## 2. Changes modified during integration

| File | Change | Why |
|---|---|---|
| `alembic/versions/0008_tenant_rls.py` | Upgrade: one SQL statement per `op.execute`. Downgrade: same, and **no `DROP ROLE`** | asyncpg rejects multi-statement execution (every DB test errored); the role is cluster-wide so dropping it fails while other databases hold grants. Same revision id/SQL otherwise. Not applied to any non-test DB. |
| `app/auth/service.py` | `ruff format` | Branch code failed `ruff format --check` |
| `.gitignore` | added `release/` | electron-builder output dir was not ignored |
| `tests/helpers.py` | `run_worker(..., user_id=None)` | A real queue message carries the owner; needed when the job row is already deleted |
| `tests/test_ingestion_jobs.py`, `test_embedding_pipeline.py`, `test_phase4_closure.py` | job-id lookup through maintenance session; pass captured owner for post-delete duplicate delivery | Tests read `ingestion_jobs` via the RLS-bound runtime session (returns nothing by design) / helper looked up an owner of a deleted job. Assertions unchanged. |
| `tests/test_ai_api.py`, `test_embeddings.py`, `test_phase3_closure.py` | valid deployment env (strong JWT, `rediss://`, maintenance URL) | Intentional config hardening made them fail on an unrelated check before reaching the one they test. Assertions unchanged. |
| `tests/test_qa_schema.py` | expected head 0007 → 0008 (+ parent chain 0008→0007→0006) | Intentional new migration |
| `tests/test_qa_service.py` | passage splitter accepts prompt-v2 `[E<n> \| … ]` tags | Format changed intentionally; budget assertion unchanged |
| `tests/ai_contract.py`, `tests/test_ai_router.py` | handle `Usage \| None` | Usage made optional (D-44); counts still checked when present; fake-provider test stays strict |
| `CLAUDE.md`, `docs/{ARCHITECTURE,DECISIONS,PHASES,RISKS_AND_OPEN_QUESTIONS}.md` | Electron as preferred Phase 10 direction; D-47 evaluation addendum; Phase 10 marked **not complete** | Owner instruction |

## 3. Changes intentionally not integrated

None. No branch change was excluded. (Nothing was unsafe enough to drop once the two migration defects were fixed.)

## 4. Conflicts and resolutions

- Git-level conflicts: **none** (branch is a linear descendant of `main`; no local-only commits).
- Single Alembic head `0008` (parent `0007`); no migration fork.
- Logical issues and their resolutions are tabulated in `BRANCH_MERGE_ANALYSIS.md` §5 (C2–C13).
- Dramatiq `ingest_job` now takes `(job_id, user_id)`: **drain the `ingestion` queue before deploying**, or old one-argument messages fail.

## 5. Verification results

Environment: Windows 11, Python 3.12 (uv), Node 22, Docker Postgres(pgvector)+Redis from `docker-compose.yml`; tests use only the disposable `<db>_test` database. Containers were started for the run and stopped afterwards.

| Check | Result |
|---|---|
| Backend full suite (final run) | **Passed: 1377 · Failed: 0 · Skipped: 4 · Deselected: 2** (19m18s) |
| Backend `-m integration` (the 2 deselected) | Passed: 2 |
| After the last migration edit: `test_rls`, `test_qa_schema`, `test_auth` | Passed: 52 |
| `ruff check` / `ruff format --check` | Pass / 193 files formatted |
| `mypy --strict` (app only) | Pass, 108 files |
| `mypy --strict` (configured scope: app+tests+alembic) | **6 errors, all pre-existing**: baseline (`safety` tag, checked in a throwaway worktree) had 8; none in files this work touched |
| Alembic validation (scratch DB, then dropped) | upgrade head → downgrade 0007 → downgrade base → upgrade head: all OK; single head |
| App startup | `/health` ok; `/ready` postgres ok + redis ok; `/metrics` serves Prometheus text |
| Web (`apps/web`) | lint pass, typecheck pass, 80/80 tests, production build pass |
| Desktop (`apps/desktop`) | typecheck pass, 27/27 vitest, production build pass; `npm audit --omit=dev`: 0 vulnerabilities |

Skipped: 2 real-embedding tests (opt-in `RUN_REAL_EMBEDDING_TESTS=1`, needs the model), 2 Windows-incompatible storage tests (symlinks, POSIX permission bits).
Not run: live OpenAI/Ollama calls (no credentials, by design); Windows desktop launch/package smoke; desktop manual checklist; end-to-end desktop-to-backend run; real-embedding tests; real-model RAG/prompt evaluation.

Failure history (full disclosure): the first full run showed 36 failures + 31 errors (caused by the broken migration plus machine load); the second showed 23 failures. All 23 were individually diagnosed (§2: two product defects in the migration, the rest tests needing legitimate updates). None were weakened to pass.

## 6. Known issues

- `mypy --strict` over tests reports 6 errors in `test_realtime_*` (pre-existing at the base commit).
- `/metrics` has no application auth.
- Real-provider behaviour, retention terms and key ownership are unverified (OQ-4 remains open).
- JWT minimum rose 32→43: existing deployments with shorter secrets will refuse to start (your local `.env` is compatible).
- RLS in production needs a non-superuser runtime login that can `SET ROLE interview_app_runtime`, and a separate maintenance login; this is documented, not automated. The downgrade leaves the cluster-wide role behind by design.
- Test run duration varied 19 min – 90 min depending on machine load.

## 7. Electron evaluation (Phase 10 foundation)

**Verdict: technically sound as a foundation; Electron is now documented as the preferred direction. Phase 10 is NOT complete.**

Verified by source review and tests:
- Architecture: main process owns API calls and credentials; sandboxed, context-isolated renderer; typed preload bridge with no raw `ipcRenderer`; renderer talks only through ~15 named channels.
- Security boundaries: IPC sender URL check on every handler; `nodeIntegration` off, `webviewTag` off, navigation/`window.open` blocked, all permission requests denied; CSP `connect-src 'none'` (renderer cannot reach the network); backend URL must be HTTPS or loopback HTTP.
- Secrets: access token only in main memory; refresh token only via `safeStorage` (fails closed if unavailable); renderer's user type has no tokens; no API/provider keys anywhere in `apps/desktop`.
- Hard rules: no `desktopCapturer`, `getUserMedia`, `setContentProtection`, screenshots or taskbar hiding; visible always-on-top window + tray.
- Integration: uses only existing endpoints (auth, sessions, SSE Q&A); web app and backend logic untouched by it; no second desktop codebase.
- Build/tests: typecheck, 27 unit tests, production build, 0 prod-dependency audit findings.

Remaining Phase 10 work (not done):
1. Windows launch and `package:win` smoke test, and the manual QA checklist (`docs/DESKTOP_MANUAL_CHECKLIST.md`).
2. End-to-end run against a live backend (login → session → streamed answer → cancel).
3. Decide signing/auto-update; reconsider the `unsafe-inline` style allowance in the CSP.
4. WebSocket client, audio, screenshots: separate, explicitly approved later work (not built, intentionally).

## 8. Repository state

- Branch `main`, HEAD `9917a45` (unchanged). Safety tag `safety/pre-dheeraj-merge-20261109` **preserved**.
- Merge is **in progress and uncommitted** (`.git/MERGE_HEAD` present): 98 branch files staged, plus my follow-up edits unstaged, plus new untracked `docs/BRANCH_MERGE_ANALYSIS.md` and this report.
- **Nothing committed, nothing pushed.** Rollback: `git merge --abort` (or reset to the tag; note this would also discard my follow-up edits).
- `.env` untouched. Docker containers stopped (volumes retained). `apps/desktop/node_modules` was installed (git-ignored).

## 9. Final decision

- Existing functionality preserved: yes — full backend suite, web and desktop checks pass; no files deleted; fake provider, SSE, WebSocket and all prior migrations intact.
- Integrated: everything in the branch. Excluded: nothing.
- **Recommended next phase: Phase 10 completion/hardening** — Windows package smoke + manual checklist + live-backend end-to-end for the Electron MVP, before adding any new desktop capability. Not started.
