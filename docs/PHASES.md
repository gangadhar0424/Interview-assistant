# Phased Implementation Plan

Rules: each phase is independently testable; do not start a phase without explicit instruction. **Milestone boundaries (M1–M5)** are where full regression runs. "Targeted tests" = tests to run during the phase (see TESTING_STRATEGY).

| Milestone | Phases | Outcome |
|---|---|---|
| **M1 Foundation & Auth** | 1, 2 | Running skeleton, users can sign up/in |
| **M2 Content & RAG** | 3, 4, 5 | Resumes/docs ingested and retrievable with isolation |
| **M3 Sessions & AI** | 6, 7, 8 | Web session creation, provider layer, typed Q&A via API |
| **M4 Real-time & Desktop** | 9, 10, 11, 12A, 12B | Desktop client, WS, screenshot, audio |
| **M5 Hardening & Release** | 13, 14 | Security, perf, deployment |

---
## Phase 1 — Repository & Dev Environment Foundation
- **Objective:** Runnable empty skeleton with tooling.
- **Scope:** monorepo layout; FastAPI app with `/healthz`; Next.js shell; Postgres(+pgvector)/Redis via docker compose (no object-storage service; see D-26); Alembic wired; lint/format/type-check; CI skeleton; config/env loading; structured logging; `.env.example`.
- **Deliverables:** compose up works; health endpoints; CI running lint+unit; `README` quickstart.
- **Dependencies:** Phase 0 docs; OQ-1 not required.
- **Acceptance:** `docker compose up` brings all services healthy; API health returns 200 and DB/Redis readiness; web renders placeholder; CI green.
- **Targeted tests:** health/readiness tests, config loading unit tests, lint/typecheck, compose smoke.

## Phase 2 — Authentication & User Isolation Base
> **Owner-narrowed scope (implemented):** users + refresh_tokens tables, signup/login/refresh/logout/me, Argon2id, JWT + rotating refresh, `CurrentUser` dependency, web login/signup/protected placeholder. **Deferred (not built in Phase 2):** auth rate limiting, `audit_events`, RLS scaffolding and the two-user isolation fixtures (needed with the first user-owned table in Phase 3), web E2E tests.

- **Objective:** Secure accounts and the isolation mechanics.
- **Scope:** users, refresh_tokens, audit_events tables+migrations; signup/login/refresh/logout/me; Argon2id; JWT; rate limiting on auth; repository base with mandatory `user_id`; RLS scaffolding; web login/signup pages + protected layout.
- **Deliverables:** auth endpoints, web auth pages, isolation helper + test fixtures (two-user).
- **Dependencies:** Phase 1.
- **Acceptance:** signup/login/logout work; refresh rotation + reuse detection; passwords hashed Argon2id; unauthenticated calls rejected; rate limit triggers; RLS test proves cross-user blocking on a sample table.
- **Targeted tests:** auth unit/integration, token rotation/reuse, rate limit, RLS policy test, web auth e2e (happy path), contract tests for auth schema.

## Phase 3 — File Upload, Storage & Resume/Document CRUD
> **Implemented as owner-scoped:** PDF/DOCX resumes (10 MB) and PDF/DOCX/TXT documents (20 MB); upload, list, authenticated streaming download, delete; `FileStorage` + local filesystem; simple `/home` UI. **Deferred:** Postgres RLS (D-27), ingestion `status` fields and `GET /{id}`/retry (Phase 4), cursor pagination, malware scanning, per-user quotas, API-level `title`/`doc_type`.

- **Objective:** Safe upload and management (no processing yet).
- **Scope:** `FileStorage` interface + local-filesystem implementation (D-26: server-generated keys, path-traversal protection, storage root from config, persistent-volume requirement documented); upload validation (ext/MIME/magic/size); resumes+documents tables; CRUD endpoints; delete (file removal); web pages for Resumes/Documents (list/upload/delete); dashboard shell (4 nav items).
- **Deliverables:** endpoints, UI, validation suite.
- **Dependencies:** Phase 2.
- **Acceptance:** valid files stored under `user_id` prefix; invalid/oversized/spoofed rejected; user B gets 404 on user A's files; delete removes file and row.
- **Targeted tests:** validation matrix (fixtures incl. spoofed/zip bomb/encrypted PDF), `FileStorage` tests on a temp directory (incl. path traversal, foreign-user access, atomic write, delete), isolation tests, upload e2e.

## Phase 4 — Ingestion Pipeline (Extract, Chunk, Queue)
> **Implemented as owner-scoped ("File Ingestion Foundation"):** database-backed job state (`pending/processing/completed/failed`), Dramatiq worker, PDF (PyMuPDF) / DOCX (python-docx) / TXT extraction, conservative normalisation, `extracted_texts`, status + retry API and UI, recovery sweep (stuck `processing`, undelivered `pending`). **Moved to Phase 5:** chunking (and embeddings/retrieval, as before). **Deferred:** OCR for scanned PDFs, Markdown, ClamAV stage, automatic backoff retries, scheduled recovery.

- **Objective:** Reliable background processing up to chunks.
- **Scope:** Dramatiq workers; `ingestion_jobs`; stage machine; text extraction (PDF/DOCX/TXT/MD); chunker; retry/backoff; reaper; status surfaced in API/UI; retry endpoint; optional ClamAV stage (feature flag).
- **Deliverables:** worker entrypoint, job lifecycle, status UI.
- **Dependencies:** Phase 3.
- **Acceptance:** upload → Ready status with chunks stored (text only); transient failure retries then succeeds; permanent failure → `failed` with reason; worker kill mid-job recovers; re-run idempotent (no duplicate chunks).
- **Targeted tests:** extractor/chunker unit tests with golden files, job state-machine tests, retry/idempotency/reaper tests, failure injection.

## Phase 5 — Embeddings & Vector Retrieval (RAG core)
> **Implemented ("Chunking, Embeddings and Secure Vector Retrieval"):** deterministic chunker; `EmbeddingProvider` abstraction + fake and fastembed (`intfloat/multilingual-e5-large`) implementations; `chunks`/`chunk_embeddings` (pgvector); `embed` job stage with retry/recovery; internal user-scoped `search_chunks` (no public endpoint); `python -m app.workers.reindex`. **Deferred:** ANN index, cross-encoder rerank, hybrid keyword search, dev retrieval endpoint, retrieval quality evaluation set (PoC-4), session-scoped retrieval (Phase 7+).

- **Objective:** Index chunks and retrieve with strict isolation.
- **Scope:** `EmbeddingProvider` port + 1–2 adapters (+ fake deterministic adapter for tests); pgvector column/index; `VectorStore` interface; retrieval service (user + selected IDs filter, top-k, resume quota); re-embedding job; deletion cascades; **embedding model decision (OQ-3) must be resolved first.**
- **Deliverables:** retrieval service + internal/debug endpoint (dev only) or service-level API.
- **Dependencies:** Phase 4; OQ-3.
- **Acceptance:** retrieval returns only selected sources of that user; cross-user attempts return nothing; deleting a source removes vectors; Hindi/Telugu sample retrieval quality meets agreed threshold on a small eval set.
- **Targeted tests:** isolation/adversarial retrieval tests, delete cascade, retrieval relevance eval (small gold set), embedding adapter contract tests. **→ M2 full regression.**

## Phase 6 — AI Provider Abstraction Layer
> **Implemented as "Provider Layer Core" (D-33), accepted:** `LLMProvider` port, registry/allowlist, router with fallback + circuit breaker, error taxonomy, usage records, fake provider, `GET /ai/options`, import-boundary check (ruff TID251 + test), contract suite. **Deferred pending OQ-4:** real adapters (Gemini, OpenAI, Anthropic, Groq, Sarvam) and recorded fixtures for them.

- **Objective:** Provider-neutral LLM access.
- **Scope:** `LLMProvider` port, registry/config, router with fallback + circuit breaker, error taxonomy, usage metering; adapters for Gemini, OpenAI, Anthropic, Groq, Sarvam (as available/credentialed); fake provider; `/ai/options`.
- **Deliverables:** adapters + contract test suite + provider config.
- **Dependencies:** Phase 1 (can run parallel to 3–5 logically, but sequenced after for focus).
- **Acceptance:** same contract suite passes on all enabled adapters (recorded fixtures); fallback triggers on injected failures; no provider SDK imports outside adapters (lint rule).
- **Targeted tests:** adapter contract tests, router/fallback tests, streaming tests, import-boundary check.

## Phase 7 — Session Creation (Backend + Web Wizard) & Preference Compiler
> **Split approved (D-34): 7A → 7B → 7C → 7D.** Plan: `docs/PHASE_7_PLAN.md`. **7A (Preference Compiler + PromptBuilder) accepted:** enums, `PreferenceCompiler`, `AnswerConfig`, versioned prompt templates, `PromptBuilder`, `/ai/options` enum lists. **7B (sessions backend: migration 0006, `/sessions` CRUD, material validation) accepted (D-35).** **7C (SSRF-safe job import: `POST /sessions/job-import`, no schema change) accepted (D-36).** **7D (web wizard at `/sessions/new` + dashboard "Call Sessions" list; frontend only, D-37) implemented, pending owner review.** Phase 8 followed (below).

- **Objective:** Create sessions with job context and preferences.
- **Scope:** sessions, session_preferences, session_documents tables; session endpoints; job-import (SSRF-safe) with manual fallback; PreferenceCompiler + PromptBuilder (templates, versioning); web 2-step wizard; dashboard "Call Sessions" list.
- **Deliverables:** wizard UI, endpoints, compiler/builder modules.
- **Dependencies:** Phases 3, 5 (for ready resumes), 6 (for options).
- **Acceptance:** session created with validated ownership of resume/docs; job URL failure still allows manual entry; SSRF cases blocked; each preference value maps to expected `GenerationConfig`; prompt snapshots stable.
- **Targeted tests:** session API + isolation, SSRF test suite, compiler/prompt snapshot tests, wizard e2e.

## Phase 8 — Typed Question Answering (API + minimal web test UI)
> **Split approved (D-38): 8A → 8B → 8C.** **8A (backend core: migration 0007, router metadata extension, context assembly, orchestration + persistence with a fake provider) accepted.** **8B (API + SSE streaming + history GETs + per-user limits + disconnect/stale handling, D-39; no schema change) accepted.** **8C (minimal web Q&A page `/sessions/[id]`, frontend only, D-40) implemented, pending owner review.** Phase 8 is complete once 8C is accepted; Phase 9 is accepted and complete (see Phase 9 below). No real LLM provider exists (OQ-4).

- **Objective:** End-to-end RAG answer generation with streaming (the first AI slice).
- **Scope:** `/sessions/{id}/questions`; context assembly; streamed answers (SSE initially or WS skeleton); answers/questions persistence; latency metrics; basic history view.
- **Deliverables:** typed Q&A flow, stored Q/A with metrics.
- **Dependencies:** Phases 5–7.
- **Acceptance:** answer uses only selected materials (verified by canary facts in other user's/unselected docs never appearing); preferences visibly affect output structure; first-token latency recorded; provider fallback works. **→ M3 full regression.**
- **Targeted tests:** RAG grounding/canary isolation tests, format/length conformance checks, streaming tests, latency metric tests.

## Phase 9 — WebSocket Gateway & Realtime Session Core
> **Split (plan in `docs/PHASE_9_PLAN.md`, owner approvals recorded in D-41): 9A → 9B → 9C.** **9A (protocol schemas + generated `packages/protocol`, Redis tickets and `POST /auth/ws-ticket`, WS handshake, hello, heartbeat, limits, close codes; no migration) is accepted (owner-approved and closed).** 9B (typed Q&A over WS through the shared `ask` core, `answer.cancel`/`answer.cancelled`, bounded send queue with slow-consumer close 4010, per-question account re-check; no migration, D-42) is accepted (owner-approved and closed); 9C (per-(user, session) channel, detached generation with a 30 s grace period, bounded retained buffer, `resume_from_seq` replay, database-derived cancel result, load smoke; no migration, D-43) is accepted (owner-approved). Phase 9 is accepted and complete. The web page stays on SSE.
- **Objective:** Real-time channel without audio.
- **Scope:** WS endpoint, tickets, event schema + generated types, heartbeat, reconnect/resume, cancellation, backpressure; typed Q&A over WS.
- **Dependencies:** Phase 8.
- **Acceptance:** protocol events match API.md; reconnect resumes; unauthorized/other-user session rejected; cancellation works.
- **Targeted tests:** WS contract tests, auth/ticket tests, reconnect/cancel tests, load smoke (N concurrent connections).
- **Acceptance evidence (9C closure):** protocol events match `API.md` (strict models, generated `packages/protocol`, drift test, SSE payload parity); reconnect resumes (replay tests, a live abrupt-drop resume, a 50-connection load smoke with half the clients dropping and resuming); unauthorized or other-user sessions are rejected (handshake tests, isolation tests across resume, disabled-user reattach); cancellation works (9B tests plus the cancel-versus-final-commit race, where the database decides). See `docs/DECISIONS.md` D-41 to D-43.

## Phase 10 — Desktop Client Shell (Electron MVP)
> **Electron is the preferred Phase 10 desktop technology (D-47, evaluated 2026-11-09). Phase 10 is ACCEPTED and COMPLETE (owner-approved 2026-11-10; see [PHASE_10_CLOSURE_REPORT.md](./PHASE_10_CLOSURE_REPORT.md)).** **Phase 10A (validation) is done** (see [PHASE_10A_REPORT.md](./PHASE_10A_REPORT.md)): typecheck, 27 unit tests, production build, `package:win` (unsigned NSIS built), packaged-app launch, and an end-to-end run against the real local backend (login, sessions, streamed Q&A, cancel, history isolation, restart persistence, backend-down/recovery) all passed; renderer isolation, no credential exposure and CSP were verified live. **Phase 10B (hardening) is accepted after owner manual testing** ([PHASE_10B_MANUAL_TEST_PLAN.md](./PHASE_10B_MANUAL_TEST_PLAN.md)) (see [PHASE_10B_REPORT.md](./PHASE_10B_REPORT.md), D-49): `style-src 'self'` applied and verified (0 CSP violations), release fuses applied and the packaged app re-tested, project-owned icon, clearer IPC error messages, release process and signing/auto-update proposals documented ([DESKTOP_RELEASE.md](./DESKTOP_RELEASE.md), D-50 *Proposed*). **Deferred (not blocking closure):** multi-monitor manual test, mid-stream cancel against a slow provider, real-provider validation (OQ-4), code signing and auto-update (release/deployment decisions, D-50), WebSocket client; audio and screenshots are deliberately not built.
>
> **Windows MVP implemented, pending owner review (D-47):** `apps/desktop/` contains the Electron main/preload/renderer app, visible floating window and tray, login/signup, session picker, typed Q&A over existing SSE, cancellation, history/copy, settings, secure refresh-token storage, and unit tests. `apps/web/` and backend business logic remain unchanged. No screenshot, audio, stealth/evasion, WebSocket client, signing, or auto-update work is included.
- **Objective:** Floating, user-controlled client with typed Q&A.
- **Scope:** Electron app with main/preload/renderer separation, login/signup, session picker, visible floating window (always-on-top, opacity, expand/collapse, tray), typed question through the existing SSE API, cancel, streamed answer, history, copy, and backend settings. Windows-focused.
- **Dependencies:** Phase 9.
- **Acceptance:** login/logout; select session; typed Q streams answer and can be cancelled; history loads; credentials stay outside the renderer; IPC rejects untrusted renderers; window remains visible and user-controlled.
- **Targeted tests:** desktop unit tests, strict TypeScript check, production build, Windows startup/package smoke, and [desktop manual QA checklist](./DESKTOP_MANUAL_CHECKLIST.md). WebSocket, cross-OS and capture tests are deferred with those features.

## Phase 11 — Screenshot Input
> **Status: MVP plan APPROVED (D-51). Phase 11A (backend) is accepted; Phase 11B (desktop) is implemented and verified automatically, awaiting owner review ([PHASE_11B_REPORT.md](./PHASE_11B_REPORT.md)); Phase 11 is NOT complete; 11C is optional and unapproved.** 11A: `VisionProvider` port + deterministic fake (no real vision provider), `POST /sessions/{id}/screenshots` (PNG only, memory only, no storage/migration), `input_source` on questions; see [PHASE_11_PLAN.md](./PHASE_11_PLAN.md). Original plan note: the architecture and implementation plan (proposed MVP, privacy/security design, API proposal, 11A backend / 11B desktop / 11C optional split, test strategy, open questions) is in [PHASE_11_PLAN.md](./PHASE_11_PLAN.md) and awaits owner review. The scope below is the original roadmap text and is refined, not replaced, by that plan.
- **Objective:** User-initiated screenshot → answer.
- **Scope:** native capture trigger, upload endpoint, validation/EXIF strip, `VisionProvider` port + adapters, question extraction, retention job.
- **Dependencies:** Phase 10, Phase 6.
- **Acceptance:** screenshot only on explicit action; invalid files rejected; extracted question → answer; expired screenshots purged.
- **Targeted tests:** image validation, vision adapter contracts, OCR/extraction fixtures, retention sweep test.

## Phase 11.5 — Desktop Session Creation
> **Status: IMPLEMENTED (11.5A and 11.5B combined in one batch) and verified automatically; awaiting owner manual testing and acceptance** ([PHASE_11_5_REPORT.md](./PHASE_11_5_REPORT.md)). Plan: [PHASE_11_5_PLAN.md](./PHASE_11_5_PLAN.md); decision D-52. Inserted between Phase 11 and Phase 12A, without renumbering any existing phase. The optional "Open web app" button (`shell.openExternal` + a `webAppUrl` setting) was **not** built: it needs a separate owner decision; the no-ready-resume guidance shows the web link as text with a Copy button instead.
- **Objective:** a desktop-only user can create an interview session (and then ask questions) without the web app for the normal workflow. Today the desktop can only list and select ready sessions.
- **Scope:** one create form (company, optional job title, pasted job description, one ready resume, up to the server limit of ready documents, answer style from `GET /ai/options` with the web wizard's defaults, collapsed with the chosen values visible); no-ready-resume guidance with Refresh and a safe path to the web app; refresh and select the new session. All calls through the Electron main process and the existing API client; narrow validated IPC; no tokens in the renderer. **Not in scope:** file uploads, editing or deleting sessions, job-link import, provider/model selection, audio, screenshot changes, any backend change or migration.
- **Split:** **11.5A** integration foundation (API client methods, narrow IPC, validation, orchestration, tests, plus the connection-status fix below) → **11.5B** UI and workflow.
- **Also included (sub-item 11.5-F, usability fix):** after a successful "Refresh sessions", clear a stale "Could not reach the backend" banner and mark the connection as connected immediately; a failed refresh must not show "connected".
- **Dependencies:** Phase 10; existing session/resume/document/AI-options APIs (verified in the plan). No new AI infrastructure; fake providers suffice for all tests; no real provider needed.
- **Acceptance:** see the plan (§11): create through the existing API, new session visible without restart, explicit ready resume, backend limits respected, no dead end without a resume, safe errors, tokens never in the renderer, typed Q&A and screenshots unchanged, no backend/migration/dependency change, typecheck/tests/build/package smoke pass.
- **Targeted tests:** desktop unit/IPC tests (sender and input validation, error mapping, no-ready-resume, refresh after create, regressions), a live check of the real client against a local backend with fake providers, packaged-app smoke, owner manual checklist.

## Phase 12A — Real-Time Transcription
- **Objective:** Audio → stored transcript.
- **Scope:** audio capture (mic first, then system audio per OS), `STTProvider` port + adapters, VAD, utterance segmentation, transcript persistence, transcript UI. **Requires PoC-2/PoC-3 first.**
- **Dependencies:** Phase 10; PoC results.
- **Acceptance:** partial/final transcripts stream within agreed latency on reference hardware; transcript stored and viewable; no raw audio persisted.
- **Targeted tests:** STT adapter contracts, VAD/segmentation tests, audio replay test (prerecorded audio), latency measurement.

## Phase 12B — Question Detection & Auto-Answer
- **Objective:** Transcript → detected questions → answers.
- **Scope:** two-stage detector (heuristics + small-LLM classifier), confidence thresholds, possible-question UX, follow-up resolution, trigger into the RAG/answer pipeline.
- **Dependencies:** Phase 12A, Phases 8/9; PoC-6.
- **Acceptance:** detector meets agreed precision/recall on labeled en/hi/te set; non-questions don't trigger; user override works.
- **Targeted tests:** detector eval set (en/hi/te), heuristic unit tests, audio replay → answer end to end. **→ M4 full regression.**

## Phase 13 — Security Hardening & Observability
- **Objective:** Production-grade protections.
- **Scope:** email verification and password reset (deferred per D-24, if wanted), security review, headers/CSP, dependency/SAST scanning, ClamAV on, audit logging coverage, retention jobs, metrics/alerts/dashboards, backup/restore drill, load tests, privacy features (export/delete account).
- **Acceptance:** threat-model items closed or accepted; restore drill successful; alerts fire in test; pen-test checklist passed.
- **Targeted tests:** security test suite, retention/delete tests, load/perf tests vs budget.

## Phase 14 — Deployment & Release
- **Objective:** Staging and production deployment.
- **Scope:** infra-as-config, CI/CD, secrets management, TLS, backups, desktop signed builds/auto-update, runbooks.
- **Acceptance:** staging mirrors prod; smoke tests pass post-deploy; rollback rehearsed. **→ M5 full regression.**
- **Targeted tests:** deployment smoke, migration up/down on staging copy, critical-path e2e.

### Notes
- Phases 6 may be pulled earlier if provider PoCs are needed. Phase 12A PoCs (audio/STT) can be run early as separate spikes **only if the user instructs**.
- Phases 1–8 deliver the entire web product's backend value without desktop dependencies.
