# CLAUDE.md — Project Instructions

## Purpose
Internal-use, AI-assisted interview preparation and live interview support platform. Two coordinated components:
1. **Web app** — accounts, resumes, documents, session creation/config, session history.
2. **Desktop client** (later phase) — user-controlled floating window for live sessions (typed questions, screenshots, real-time transcription, streamed answers).

Independently designed. Do not copy or reverse engineer any proprietary product.

## Current Status
**Phase 0 validated. Phases 1–6 accepted. Phase 7 is split 7A→7B→7C→7D (D-34, `docs/PHASE_7_PLAN.md`); 7A (compiler/PromptBuilder) is accepted; 7B (sessions backend, D-35) is accepted; 7C (SSRF-safe job import, D-36) is accepted; 7D (web wizard + dashboard sessions list, frontend only, D-37) is implemented, pending owner review. Phase 7 is complete. Phase 8 is split 8A→8B→8C (D-38/D-39): 8A (backend core) is accepted; 8B (SSE `POST /sessions/{id}/questions`, history GETs, per-user limits, disconnect + stale handling; no schema change) is accepted; 8C (web Q&A page at `/sessions/[id]`, frontend only, D-40) is implemented, pending owner review. Phase 9 is split 9A→9B→9C (D-41, `docs/PHASE_9_PLAN.md`): 9A (protocol schemas + generated `packages/protocol`, Redis single-use tickets via `POST /auth/ws-ticket`, WS handshake/hello/heartbeat/limits; no migration) is accepted; 9B (typed Q&A over WS, `answer.cancel`, backpressure, D-42; no migration) is accepted; 9C (detached generation, grace period, `resume_from_seq` replay, load smoke, Phase 9 closure, D-43; no migration) is accepted. Phase 9 is accepted and complete (owner-approved); the web page stays on SSE. The web page stays on SSE.** Phase 6 background (provider layer core): `app/ai/` — `LLMProvider` port, registry/allowlist, router with fallback + circuit breaker, error taxonomy, usage records, fake provider, `GET /ai/options`, SDK import boundary (D-33). **No real LLM adapter yet (OQ-4 open).** (At the end of Phase 6 there was no answers/RAG/sessions/desktop/realtime code; later phases added the first three, and the realtime gateway is Phase 9.) Phase 5 background — chunking, embeddings, secure vector retrieval: deterministic chunker, `EmbeddingProvider` abstraction (fake + fastembed `multilingual-e5-large`, 1024 dims), `chunks`/`chunk_embeddings` on pgvector, `embed` stage in `ingestion_jobs`, internal user-scoped `search_chunks` (no public endpoint). (At the end of Phase 5 there were no RAG answers, sessions, AI provider selection, desktop or realtime code.) RLS still deferred (D-27).

## Technology Decisions (see `docs/DECISIONS.md` for reasoning)
| Area | Choice |
|---|---|
| Web | Next.js (App Router) + React + TypeScript |
| Backend | Python 3.12 + FastAPI, Pydantic v2, SQLAlchemy 2 (async) + Alembic |
| Database | PostgreSQL 16+ |
| Vector store | **pgvector** (in Postgres) behind a `VectorStore` interface; Qdrant is the documented upgrade path |
| Cache / pub-sub / queue broker | Redis |
| Background jobs | Dramatiq workers on Redis; `ingestion_jobs` table is the source of truth for status |
| File storage | Local persistent server storage behind a `FileStorage` interface (D-26); no S3/MinIO for now. Uploads must live on a persistent volume in deployment. |
| Realtime | WebSockets (FastAPI native) |
| Desktop | Tauri 2 + React + TypeScript (Rust for native audio/screenshot) |
| AI | Provider abstraction layer (LLM, embeddings, STT, vision/OCR as separate ports) |
| Auth | Argon2id passwords, short-lived access JWT + rotating refresh token |

Approved (Accepted in `docs/DECISIONS.md`): web, backend, PostgreSQL, pgvector, Dramatiq/Redis, Tauri 2 (subject to PoC; Electron fallback), WebSockets, AI port abstraction, isolation model. Email verification/password reset are deferred (D-24). Anything still "Proposed" is not binding until Accepted.

## Repository Conventions (planned layout)
```
/apps/web        Next.js app
/apps/desktop    Tauri app (later phase)
/services/api    FastAPI app + workers (same codebase, separate entrypoints)
/packages/       shared TS types / generated API client
/docs            planning + architecture docs (source of truth)
/infra           docker/compose, deployment (introduced in the phase that needs it)
```
- Python: `ruff` + `mypy --strict` on core modules; type hints required. TypeScript: `strict: true`, ESLint, Prettier.
- Conventional Commits. Small, reviewable changes. One concern per PR.
- API is contract-first: change `docs/API.md` (and OpenAPI schema once it exists) before or with the code.
- All AI calls go through `ai/` ports. **No provider SDK imports outside provider adapter modules.**
- All prompt construction goes through the single `PromptBuilder` (see ARCHITECTURE §7). No inline prompt strings elsewhere.
- Every DB query touching user data must be scoped by `user_id` via the repository layer. No raw unscoped queries in route handlers.

## Scope Control Rules
1. Work only on the phase the user has explicitly named. **Do not start a future phase without explicit instruction.**
2. Do not add features outside `docs/PRD.md` in-scope list. Out-of-scope items (resume builder, mock interviews, headshots, general calling) must not be built.
3. If a task needs something not in the docs, stop and ask or propose a doc change first.
4. Don't add dependencies, services, or infrastructure not required by the current phase.
5. Don't refactor unrelated code while delivering a phase item.

## Documentation Rules
- **Before implementing, read** the relevant docs: PRD (scope), ARCHITECTURE (design), DATABASE (schema), API (contracts), PHASES (current phase), TESTING_STRATEGY (what to run), DECISIONS (don't re-litigate).
- **When architecture, schema, API, or a decision changes, update the affected docs in the same change.** Add a new entry to `docs/DECISIONS.md` for any significant change; don't silently rewrite accepted ones (supersede them).
- Resolve items in `docs/RISKS_AND_OPEN_QUESTIONS.md` by moving them to DECISIONS when answered.

## Testing Rules
- Follow `docs/TESTING_STRATEGY.md`. Run **targeted tests** for the change plus tests selected via the **test impact matrix**. Do not run the full suite after every small change.
- Full regression only at milestone boundaries defined in `docs/PHASES.md`.
- Contract tests must be updated/run when an API, WebSocket event, or provider port changes.
- User-isolation tests are mandatory for any new endpoint or query touching user-owned data.
- Never claim tests pass without running them; report failures with output.

## Security & Privacy Rules
- Never commit secrets; use env vars / secret manager. `.env*` in `.gitignore`.
- Never log passwords, tokens, document text, transcript text, or screenshots at INFO or above. Log IDs only.
- Treat uploaded files and job-page content as untrusted input (validation, size limits, SSRF protection for job URLs, prompt-injection awareness).
- Audio capture and screenshots are strictly user-initiated and visibly indicated; no hidden capture.

## Phase Gate
After finishing a phase: report deliverables vs. acceptance criteria, list what was tested, update docs, then **stop and wait** for explicit instruction.
