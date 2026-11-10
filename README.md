# Interview Assistant

Internal-use, AI-assisted interview preparation and live interview support platform.
Read `CLAUDE.md` and `docs/` first. Current implementation status and phase gates are tracked there.

## Prerequisites
- Docker Desktop (running)
- [uv](https://docs.astral.sh/uv/) (installs Python 3.12 automatically)
- Node.js 20.9+ (22 recommended) and npm

## Setup (Windows PowerShell or any shell; run from the repository root)
```powershell
Copy-Item .env.example .env          # then review values; never commit .env
docker compose up -d --wait          # Postgres (pgvector) + Redis
```
File storage is NOT a compose service: uploads use local persistent storage behind a
`FileStorage` interface (docs/DECISIONS.md D-26). In deployment the storage root must be a persistent volume.

## Backend (`services/api`)
```powershell
cd services/api
uv sync                                        # install deps
uv run alembic upgrade head                    # apply all migrations, including tenant RLS
uv run uvicorn app.main:get_app --factory --reload --port 8000   # deployment: add --ws-max-size 65536 (R-47)
# in another terminal:
curl http://localhost:8000/health              # liveness (never depends on infra)
curl http://localhost:8000/ready               # Postgres + Redis (503 if either is down)
curl http://localhost:8000/metrics             # Prometheus metrics; keep internal in deployment
uv run dramatiq app.workers.tasks --processes 1 --threads 2   # ingestion worker (needs Redis + same .env)
uv run python -m app.workers.reindex              # queue re-embedding after EMBEDDING_MODEL changed
uv run python -m app.workers.recovery --dry-run   # list stuck/undelivered ingestion jobs
uv run python -m app.workers.recovery             # repair them (run periodically, e.g. every minute)
```
Quality checks:
```powershell
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest -q                               # needs `docker compose up -d --wait` + .env (auth tests use a
                                               # throwaway <db>_test database built from the migrations)
uv run pytest -m integration -q                # Phase 1 readiness test against the real dev stack
```
Ingestion (Phase 4): each upload gets a `pending` job; the worker extracts text (PDF via PyMuPDF, DOCX via python-docx, TXT) and the status appears on `GET /api/v1/resumes|documents[/{id}]`; failed files can be retried with `POST .../{id}/retry`. Run the API **and** a worker for files to reach `completed`.
Embeddings (Phase 5): after extraction each file is chunked and embedded (`indexing` status; Ready = both stages complete). Local development/tests use the deterministic fake embedder; for real multilingual embeddings set `EMBEDDING_PROVIDER=fastembed` and install the optional extra on the worker host (`uv sync --extra embeddings`, downloads ~2.2 GB on first use). Retrieval is an internal service (`app.retrieval.service.search_chunks`); there is no public search endpoint.

LLM provider layer (D-33/D-44): `app/ai/` holds the `LLMProvider` port, router (fallback + circuit breaker), error taxonomy and usage records. The fake provider remains for tests/local use; the official async OpenAI SDK adapter supports OpenAI and OpenAI-compatible services. For Ollama Cloud, configure `OLLAMA_API_KEY`, `OPENAI_BASE_URL=https://ollama.com/v1`, `OPENAI_MODEL=deepseek-v4.1-flash`, and `LLM_ENABLED_PROVIDERS=openai` in the API server environment. For OpenAI itself, use `OPENAI_API_KEY`; `OPENAI_BASE_URL` defaults to `https://api.openai.com/v1` and `OPENAI_MODEL` defaults to `gpt-4o-mini`. `OPENAI_TIMEOUT` is in seconds (default 30). For local fake fallback, use `LLM_ENABLED_PROVIDERS=fake,openai` and optionally `LLM_FALLBACK_CHAIN=fake,openai`. Keys are server-only and never returned to the frontend. Adapter tests use mocked HTTP responses: `uv run pytest tests/test_openai_provider.py tests/test_ai_api.py -q` from `services/api`. Review the selected provider account's data-handling terms before sending interview content. Provider SDKs may only be imported in `app/ai/adapters/`.

Preferences and prompts (Phase 7A): `app/preferences/` holds the preference enums, `PreferenceCompiler` (preferences to `AnswerConfig` + sampling `GenerationConfig`) and `PromptBuilder` with versioned template files (`templates/v1/`). `GET /api/v1/ai/options` also lists the allowed languages, question types, formats, lengths and tones. Sessions backend (Phase 7B): `app/sessions/` provides `POST/GET /api/v1/sessions`, `GET/PATCH/DELETE /api/v1/sessions/{id}`: a session = job context + one ready resume + up to 10 ready documents + compiled preferences (hard delete never touches the files). Job import (Phase 7C): `POST /api/v1/sessions/job-import` fetches a public job page under strict SSRF controls (public addresses only, ports 80/443, pinned connection, re-validated redirects, size/time caps) and returns suggested company/title/description; failures never block manual entry and nothing is stored. Web wizard (Phase 7D): the dashboard (`/home`) lists your Call Sessions and links to the 2-step **Create session** wizard (`/sessions/new`: job details and ready materials, then answer preferences; optional job-link import; no upload inside the wizard). Frontend checks: `npm run lint`, `npm run typecheck`, `npm run build`, `npm test` (Node >= 22.6; pure-logic and source-guard tests); browser behaviour is checked with `docs/WIZARD_MANUAL_CHECKLIST.md`.

Typed question answering, backend core (Phase 8A): `app/qa/` retrieves context from a session's selected, ready materials (top-k 8, resume quota 3, character budget, last-3 history), builds the prompt with `PromptBuilder`, streams the answer through the provider router and persists questions/answers with latency metrics (`detected_questions`, `generated_answers`, migration 0007). The HTTP endpoints were added in 8B (below). It uses whichever server-allowlisted provider is configured; the fake provider remains useful for deterministic local tests.

Screenshot input, backend (Phase 11A, D-51): `POST /api/v1/sessions/{id}/screenshots` takes a raw `image/png` body (<= 5 MiB, validated without decoding), asks a **vision provider** (a separate allow-list, `VISION_ENABLED_PROVIDERS`, empty = disabled; only the deterministic `fake` provider exists, `APP_ENV=local`) and returns the extracted question text. Screenshots are never stored, logged or sent to a real provider; the user reviews the text and asks it through the normal question endpoint (`input_source: "screenshot"`). The desktop capture UI is Phase 11B (not started). Tests: `uv run pytest tests/test_screenshot_*.py tests/test_vision_provider.py tests/test_body_limit_patterns.py -q`.

Typed Q&A API (Phase 8B): `POST /api/v1/sessions/{id}/questions` with `Accept: text/event-stream` streams the answer as Server-Sent Events (`question.created`, `retrieval.done`, `answer.started`, `answer.delta`, `answer.done` / `answer.error`; other `Accept` values get `406`); `GET /api/v1/sessions/{id}/questions` and `GET /api/v1/sessions/{id}/answers/{answer_id}` return the history. Per-user limits (20 requests/minute, 2 concurrent generations), safe failure events, disconnect handling (an interrupted answer is stored as `cancelled` with its partial text) and stale-`streaming` handling are included. A selected resume/document that was deleted or is no longer Ready refuses the question with `409 materials_not_ready`.

Typed Q&A page (Phase 8C): `/sessions/[id]` (linked from the dashboard's Call Sessions list) lets a signed-in user ask a typed question in a ready session and watch the answer stream in (`fetch` streaming, plain-text rendering, Stop button, safe error messages) above the session's question history. Frontend only: `apps/web/lib/qa.ts` (SSE parser, event validation, state machine), `apps/web/components/SessionChat.tsx`. Browser checks are manual: `docs/QA_MANUAL_CHECKLIST.md`.
Files (Phase 3): `POST/GET /api/v1/resumes|documents`, `GET /api/v1/resumes|documents/{id}/file`, `DELETE /api/v1/resumes|documents/{id}`.
Uploads are stored under `FILE_STORAGE_ROOT` (default `<repo>/app-data`, git-ignored). In any deployed environment that directory must be a persistent volume.
Auth endpoints (Phase 2): `POST /api/v1/auth/signup|login|refresh|logout`, `GET /api/v1/auth/me` (see `docs/API.md`).
Required setting: `JWT_SECRET` in `.env`; generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Staging/production also require `REDIS_URL=rediss://...`, an absolute persistent `FILE_STORAGE_ROOT`, and a separate `MAINTENANCE_DATABASE_URL`. See [docs/OPERATIONS.md](docs/OPERATIONS.md) for role provisioning, RLS validation, monitoring, recovery, and backup runbooks, [docs/SECURITY_REVIEW.md](docs/SECURITY_REVIEW.md) for reviewed controls and remaining risks, and [docs/PRIVACY.md](docs/PRIVACY.md) for data handling and retention.
Migrations: `uv run alembic revision -m "message"`, `uv run alembic upgrade head`, `uv run alembic downgrade -1`.

## Web (`apps/web`)
```powershell
cd apps/web
npm install
npm run dev                                    # http://localhost:3000
npm run lint
npm run typecheck
npm run build
```
The web app loads the root `.env` (see `next.config.ts`). The browser only calls the web origin; Next.js proxies `/api/*` to `API_INTERNAL_URL` (rewrite resolved at build time for `npm run start`).
Pages: `/signup`, `/login`, and the protected `/home` (upload, list, download and delete resumes and documents).

## Windows desktop MVP (`apps/desktop`)
Requires Node.js 22+ and the backend API running (default local URL `http://127.0.0.1:8000`). From the repository root:
```powershell
cd apps/desktop
npm ci
npm run dev
```
In another terminal, run desktop checks/build or create a local Windows NSIS package:
```powershell
npm run typecheck
npm test
npm run build
npm run package:win
```
The desktop app has its own Settings page for the backend URL; production URLs must use HTTPS. It uses the existing API authentication and SSE question endpoint. Refresh credentials are stored through Windows OS-backed Electron `safeStorage`; API/provider keys are never placed in the desktop app. See [desktop architecture](docs/DESKTOP_ARCHITECTURE.md), [Windows development instructions](docs/DESKTOP_WINDOWS.md), and the [manual QA checklist](docs/DESKTOP_MANUAL_CHECKLIST.md). Audio and screenshot capture are not implemented.

## Stop / reset
```powershell
docker compose down            # stop, keep data
docker compose down -v         # stop and DELETE local data volumes
```

## Known issues
- Starlette's TestClient prints a deprecation warning about `httpx`; harmless.
