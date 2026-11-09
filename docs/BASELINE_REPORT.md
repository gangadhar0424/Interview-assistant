# Phase 0: Existing Project Baseline Validation

**Validation date:** 2026-11-08  
**Scope:** Read-only baseline inspection plus the requested checks that could run with the tools already installed. No product code, dependency manifests, lockfiles, or database schemas were changed. This report is the only requested file created.

## Executive summary

This is an existing monorepo with a FastAPI backend, a Next.js web application, Postgres/pgvector, Redis/Dramatiq integration, file ingestion and retrieval, session management, typed Q&A, and WebSocket infrastructure. It is substantially beyond a Phase 0 skeleton.

The frontend's Node-based unit tests ran successfully: **80 passed**. A dependency-free Python syntax scan parsed 170 backend Python files with no syntax errors. The backend test suite, backend lint/type checks, frontend lint/typecheck/build, migration execution, Compose validation, and application startup could not be verified because the environment lacks the relevant dependencies and Docker; the root `.env` is also absent. These are blockers to establishing a full working runtime baseline, not evidence that the project code itself fails those checks.

There are **7 linear Alembic migration files** by source inspection. No migrations were applied or checked against a database in this run.

## Environment observed

| Requirement | Observed state |
|---|---|
| Python | Python 3.13.11 is installed; this satisfies `services/api/pyproject.toml` (`>=3.12,<3.14`). |
| `uv` | Not installed/on PATH. No `services/api/.venv` was found. |
| Backend packages | `fastapi`, `uvicorn`, `pytest`, `ruff`, and `mypy` are unavailable in the active Python environment. |
| Node.js / npm | Node v22.19.0 and npm 10.8.2 are installed. |
| Web dependencies | `apps/web/node_modules` is absent. |
| Docker Compose | Docker is not installed/on PATH, so Compose configuration and services could not be run. |
| Root `.env` | Absent. `.env.example` exists and contains development placeholders, not deployment credentials. |
| Service ports | TCP 5432 was reachable; TCP 6379, 8000, and 3000 were not. A reachable Postgres port does not verify its version, credentials, database, or pgvector extension. |
| Git metadata | The workspace is not a Git repository; tracked-file status and worktree changes cannot be verified. |

The backend settings require `DATABASE_URL`, `REDIS_URL`, and `JWT_SECRET`. Non-local environments additionally reject the placeholder JWT secret and fake embeddings; `fastembed` is an optional dependency and model download. Keep real credentials out of source control and configure a persistent `FILE_STORAGE_ROOT` for deployed environments.

## Implemented features found by code inspection

- **Authentication:** signup, login, refresh-token rotation, logout, and current-user lookup. Passwords use Argon2id; access tokens are short-lived JWTs and refresh tokens are HttpOnly cookies for the web flow.
- **File management:** authenticated resume/document upload, listing, details, download, retry, and deletion. Upload validation checks size, file type and content; private files use local filesystem storage through `FileStorage`.
- **Ingestion and retrieval:** Dramatiq/Redis dispatch, database-backed ingestion state, PDF/DOCX/TXT extraction, chunking, optional local multilingual embeddings, pgvector storage, and internal user-scoped retrieval. No public retrieval endpoint was found.
- **Sessions and job import:** session create/list/read/update/delete; selected-material ownership/readiness validation; preference compilation and versioned prompt templates; best-effort public job-page import with SSRF controls.
- **Typed Q&A:** question-answer orchestration and persistence, selected-material retrieval, SSE streaming, history endpoints, per-process limits, and disconnect/stale-stream handling.
- **WebSockets:** single-use Redis-backed tickets, authenticated handshake, typed Q&A, cancellation, backpressure, bounded event retention, and `resume_from_seq` replay.
- **Web frontend:** signup/login, protected dashboard and file panels, session wizard/list, and typed Q&A page. The web Q&A page uses SSE.
- **Operational artifacts:** Docker Compose for Postgres/pgvector and Redis; API/web dependency manifests and lockfiles; CI workflow; Alembic migrations; generated WebSocket protocol package.

## API endpoints identified in the application routers

Base prefix for versioned HTTP routes: `/api/v1`. Health routes are unprefixed.

| Area | Methods and paths |
|---|---|
| Health | `GET /health`; `GET /ready` |
| Authentication | `POST /api/v1/auth/signup`; `POST /api/v1/auth/login`; `POST /api/v1/auth/refresh`; `POST /api/v1/auth/logout`; `GET /api/v1/auth/me`; `POST /api/v1/auth/ws-ticket` |
| Resumes | `POST` and `GET /api/v1/resumes`; `GET /api/v1/resumes/{file_id}`; `POST /api/v1/resumes/{file_id}/retry`; `GET /api/v1/resumes/{file_id}/file`; `DELETE /api/v1/resumes/{file_id}` |
| Documents | `POST` and `GET /api/v1/documents`; `GET /api/v1/documents/{file_id}`; `POST /api/v1/documents/{file_id}/retry`; `GET /api/v1/documents/{file_id}/file`; `DELETE /api/v1/documents/{file_id}` |
| AI options | `GET /api/v1/ai/options` |
| Job import | `POST /api/v1/sessions/job-import` |
| Sessions | `POST` and `GET /api/v1/sessions`; `GET`, `PATCH`, and `DELETE /api/v1/sessions/{session_id}` |
| Q&A and history | `POST /api/v1/sessions/{session_id}/questions` (SSE); `GET /api/v1/sessions/{session_id}/questions`; `GET /api/v1/sessions/{session_id}/answers/{answer_id}` |
| WebSocket | Upgrade `/api/v1/ws/sessions/{session_id}?ticket=...` |

The route inventory above is from source inspection, not a live OpenAPI response. Planned endpoints such as password reset, screenshots, session start/end, export, and desktop/audio APIs were not found as implemented routes.

## Database migrations found

The migration source chain is linear from `0001` through `0007`:

1. `0001_enable_pgvector.py` — pgvector extension.
2. `0002_users_and_refresh_tokens.py` — users and refresh-token storage.
3. `0003_resumes_and_documents.py` — resume/document metadata.
4. `0004_ingestion.py` — ingestion jobs and extracted text.
5. `0005_chunks_and_embeddings.py` — chunk/vector schema and embedding stage.
6. `0006_sessions.py` — sessions, selected documents, and preferences.
7. `0007_questions_answers.py` — typed questions and generated answers.

This list and chain were inspected from migration source. `alembic history`, `upgrade head`, clean-database migration tests, and downgrade checks were not verified because `uv` and the usable Alembic/SQLAlchemy environment are unavailable; Docker is also unavailable.

## Dependencies and project commands

- Backend manifest: `services/api/pyproject.toml`; pinned/resolved environment lock: `services/api/uv.lock`. Runtime dependencies include FastAPI/Uvicorn, Pydantic, SQLAlchemy async/asyncpg, Alembic, Redis, Dramatiq, Argon2, JWT, multipart support, PyMuPDF, and python-docx. `fastembed` is optional.
- Frontend manifest: `apps/web/package.json`; lockfile: `apps/web/package-lock.json`. Runtime dependencies are Next.js 16 and React 19; development tooling includes ESLint and TypeScript.
- Frontend `test` uses Node's built-in runner and experimental TypeScript stripping. The package's declared Node engine (`>=20.9`) is broader than that test command's documented/runtime requirement (Node >=22.6).
- The CI workflow runs backend format/lint/mypy/pytest and frontend `npm ci`, lint, typecheck, and build. It does **not** run the frontend `npm test` script.
- Compose defines Postgres with the pgvector image and Redis with AOF persistence. It is intentionally infrastructure-only: API, worker, and web are run on the host. Compose requires values from `.env`; there are no API or web containers in the file.

Documented local run sequence (not completed in this environment):

```powershell
Copy-Item .env.example .env
# Review .env and replace development-only values as appropriate.
docker compose up -d --wait
cd services/api
uv sync --frozen
uv run alembic upgrade head
uv run uvicorn app.main:get_app --factory --reload --port 8000
# In another services/api terminal:
uv run dramatiq app.workers.tasks --processes 1 --threads 2
# In apps/web:
npm ci
npm run dev
```

For CI-equivalent local checks, run the commands listed in `README.md`: backend `ruff format --check .`, `ruff check .`, `mypy`, and `pytest -q`; frontend `npm test`, `npm run lint`, `npm run typecheck`, and `npm run build`. Backend database tests use a dedicated `<db>_test` database; do not point them at a database containing user data.

## Validation results

| Check | Result | Evidence / limitation |
|---|---|---|
| Frontend unit tests | **Passed: 80/80** | `cd apps/web; npm test` using Node v22.19.0. Node emitted a module-type warning; type stripping is experimental. |
| Backend unit tests | **Not run** | `cd services/api; python -m pytest -q` stopped immediately: `No module named pytest`. |
| Backend format/lint/type checks | **Not run** | Ruff and mypy are missing from the active Python environment; `uv` is unavailable. |
| Frontend lint | **Not run** | `npm run lint` failed because ESLint is not installed (`apps/web/node_modules` absent). |
| Frontend typecheck | **Not run** | `npm run typecheck` failed because `tsc` is not installed. |
| Frontend production build | **Not run** | `npm run build` failed because Next.js is not installed. |
| Python syntax scan | **Passed (static only)** | Python AST parsing completed for 170 backend `.py` files, with 0 syntax errors. This is not a type check or test run. |
| Database migration check | **Not verified at runtime** | Seven linear migration files were identified from source. Alembic history/upgrade/downgrade could not run; no database was modified. |
| Docker Compose validation | **Not run** | Docker is unavailable. Compose source was inspected only. |
| Backend startup | **Failed before app import/startup** | `python -m uvicorn app.main:get_app --factory ...` failed: `No module named uvicorn`; FastAPI is also absent. |
| Frontend startup | **Failed before app start** | `npm run dev` failed: `next` is not recognized. |
| Service reachability | **Partial** | Postgres port 5432 reachable; Redis, API, and web ports are not. No credentials or database-level checks were attempted. |

## LLM, fake, and incomplete implementations

- `services/api/app/ai/` provides the provider port, registry, router, circuit breaker, error taxonomy, usage logging, and deterministic fake provider.
- **There is no real LLM adapter.** `KNOWN_PROVIDER_IDS` contains only `fake`; `services/api/app/ai/adapters/` has no provider implementation. The fake returns scripted/echoed output, not model-generated answers. OQ-4 remains open.
- Embeddings have a deterministic fake and an optional `fastembed` implementation. The default local/test fake has no semantic retrieval quality; deployed non-local configuration rejects fake embeddings.
- Tests use fakes/mocks for LLM, queue, Redis and other external dependencies. These are deliberate test seams, not production model implementations.
- OCR for scanned PDFs, malware scanning, per-user file quotas, application-level encryption, RLS, shared multi-worker rate limits/WS resume state, desktop client, screenshots, STT, vision, and automated browser tests are not implemented.
- `docs/QA_MANUAL_CHECKLIST.md` and `docs/WIZARD_MANUAL_CHECKLIST.md` say browser verification is still pending. The Q&A checklist records a prior automated real-stack smoke test, but that evidence was not rerun as part of this baseline.

## Known technical debt and documentation drift

- `README.md` says “Current scope: Phase 1” while describing features through Phase 9.
- Phase 7D and 8C are described as implemented but pending owner review in `CLAUDE.md`/`PHASES.md`, while other text calls the phases complete.
- `DATABASE.md` begins by saying no migrations exist, but seven migration files are present and later sections describe implemented tables.
- `ARCHITECTURE.md` contains design-era language (for example RLS in the target design) alongside later implementation notes. The accepted decision D-27 explicitly defers RLS.
- Manual Q&A checklist uses `uvicorn tests.manual_qa_server:app`; wizard checklist refers to `app.main:app`, while `app.main` exposes `get_app()` and the README uses the factory form. Reconcile the checklist command before relying on it.
- The frontend test command relies on experimental Node TypeScript stripping, and CI currently does not run that test command.
- Operational risks documented in `RISKS_AND_OPEN_QUESTIONS.md` include no OCR, unproven Indic retrieval/real-model quality, per-process controls and WS resume state, local-volume backup/restore requirements, and unresolved retention/privacy/legal decisions.

## Recommended next steps

1. Provision the intended local toolchain: install/use `uv`, sync from `services/api/uv.lock`, and run `npm ci` in `apps/web`. Do not change manifests as a workaround for a missing local environment.
2. Create a local `.env` from `.env.example`, replace any values that should not remain placeholders, and verify database credentials/settings without exposing them in logs or reports.
3. Start Docker Desktop and run `docker compose up -d --wait`; verify Postgres/pgvector and Redis readiness. Confirm the reachable Postgres listener is the intended development instance before applying migrations.
4. Against a dedicated test database, run Alembic upgrade from an empty database, verify the current revision, run the database-backed backend tests, and test downgrade/upgrade only where migration policy supports it.
5. Run the backend checks (`ruff format --check`, `ruff check`, `mypy`, `pytest -q`) and frontend checks (`npm test`, lint, typecheck, build); record exact outputs and resolve failures before declaring the baseline green.
6. Start the API, worker, and web app using the documented commands; check `/health`, `/ready`, browser login/file/session/Q&A flows, and run both manual checklists. Keep browser checks distinct from automated test evidence.
7. Before expanding beyond internal fake-provider use, resolve OQ-4 and evaluate a real LLM adapter with provider contract, privacy/data-handling, latency, quality, and failure tests.
8. Reconcile the README, database introduction, phase acceptance labels, and manual-start commands; decide and document RLS, retention/encryption, upload quotas/scanning, and multi-worker operational requirements before broader deployment.

## Baseline conclusion

Source inspection shows a broad, modular implementation with substantial deterministic test coverage and explicit security boundaries. The frontend unit suite and Python syntax scan passed in this environment. A **full working baseline is not yet established**: backend tests and quality checks, frontend static/build checks, migrations, Compose services, and application startup remain unverified until the missing toolchain, dependencies, configuration, and Redis service are available.
