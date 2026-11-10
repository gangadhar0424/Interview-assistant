# Testing Strategy

Principle: **risk-based and layered. Run the smallest test set that gives confidence for the change; run everything only at milestone boundaries.**

## 1. Test Layers
| Layer | Scope | Speed | Tools (planned) |
|---|---|---|---|
| L0 Static | lint, format, types (ruff, mypy, ESLint, tsc), import-boundary rules, secret scan | seconds | CI + pre-commit |
| L1 Unit | pure logic: validators, chunker, PreferenceCompiler, PromptBuilder, question heuristics | seconds | pytest, vitest |
| L2 Component/Integration | service + real Postgres/Redis (compose) and a temp-directory filesystem for `FileStorage`; workers; repositories; RLS | tens of seconds | pytest |
| L3 Contract | REST (OpenAPI), WS events (JSON Schema), provider ports (adapter contract suite), DB schema/migrations | seconds–minute | schemathesis/jsonschema, pytest |
| L4 E2E | web flows (Playwright); desktop (WebDriver/manual checklist) | minutes | Playwright |
| L5 Non-functional | security, load/latency, retrieval/detection evals | minutes–hours | k6/locust, eval scripts |

AI-dependent tests use **fake/recorded providers** by default (deterministic). Live-provider smoke tests are tagged `@live`, run manually or nightly, never in the default selection.

## 2. Phase-Specific Targeted Tests
Defined per phase in `PHASES.md`. During a phase run: (a) new tests for the change, (b) L0 on touched code, (c) tests selected by the impact matrix.

### Phase 3 (security, reliability, observability)
Run `tests/test_config.py`, `tests/test_rls.py`, the complete user-isolation suite, upload and SSRF
tests, auth tests, worker recovery tests, `tests/test_health.py` (including `/metrics`), and
`tests/test_logging_redaction.py`. `test_rls.py` and migration checks require a disposable real
PostgreSQL database; a skipped or mocked test does not verify the RLS role/policies.

### Phase 9 (WebSocket gateway) targeted set
`tests/test_realtime_schemas.py`, `test_realtime_tickets.py` (the Redis-integration test is marked `integration` and uses Redis database 15 and its own keys only), `test_realtime_limits.py`, `test_realtime_codegen.py` (drift of `packages/protocol`), `test_realtime_config.py`, `test_logging_redaction.py`, `test_realtime_gateway.py` (TestClient WebSocket, real DB, `FakeRedis` behind the production `TicketStore`), `test_realtime_live.py` (real uvicorn + `websockets` client). After changing `app/realtime/schemas.py` run `uv run python -m app.realtime.codegen` and commit the regenerated files. Changing a Q&A event or the SSE framer also requires `test_realtime_schemas.py` (payload parity).

### Phase 9B (Q&A over WebSocket) additions to the targeted set
`tests/test_realtime_qa.py` (TestClient WebSocket, real DB, fake LLM provider behind the production `QuestionAnswerer`: event order and `seq`, SSE payload parity, one generation at a time, cancel at each stage and its isolation, every pre-stream refusal, provider failure and the safety net, isolation between two users, account re-check, cleanup on close and on supersede, no question/answer/ticket text in logs), `tests/test_realtime_channel.py` (the bounded queue and single writer with a fake socket: overflow, send timeout, gone peer, bound on memory), `tests/test_realtime_qa_live.py` (a REAL uvicorn socket: abrupt and clean disconnects mid-stream, a client that stops reading is closed 4010 and cleaned up while another user is unaffected). Changing `app/qa/events.py`, `app/qa/sse.py`, `app/qa/limits.py` or `QuestionAnswerer.ask` also requires `test_realtime_qa.py` and `test_realtime_schemas.py` (parity and the refusal-code test). The live tests use real sockets and take about 30 s; run them whenever `channel.py`, `generation.py` or the gateway close/cleanup paths change. Mutation checks for 9B (each must make a test fail): slot not released, stream not closed, cancel ignoring the question id, no `answer.cancelled`, account not re-checked, refusals collapsed to `internal_error`, limits skipped, a second question not refused, cancel never refused, generation not stopped on close, wrong slow-consumer code, queued error not delivered before close, queue overflow undetected, send timeout ignored, events not numbered, a gone peer reported as slow, an answer field dropped from the payload.

### Phase 9C (detached generation and resume) additions to the targeted set
`tests/test_realtime_session_channel.py` (no database: numbering, retention by events and bytes, the resume decision table, attach/detach identity, grace timer, expiry, the cap on detached channels, shutdown), `tests/test_realtime_channel.py` (rewritten for the cursor-based writer: control queue overflow, falling behind the buffer, send timeout, gone peer, replay then live order), `tests/test_realtime_resume.py` (TestClient WebSocket with the real database: reconnect and replay with no gap or duplicate, a terminal event missed while detached, unavailable resumes, grace expiry, supersede, isolation across users and sessions, disabled-user reattach, resource caps, the cancel-versus-final-commit race decided by the stored row, the real lifespan shutdown, no text in logs), `tests/test_realtime_resume_live.py` (a REAL uvicorn socket: an abrupt drop and resume, resync after a slow-consumer close, and the load smoke: `LOAD_SMOKE_N` concurrent connections, default 50, half of them dropping and resuming, each answer checked to be its own). The 9B tests that asserted a closing socket cancels at once were changed to the 9C semantics (a 1 s grace in the tests). Changing `session_channel.py`, `channel.py`, `generation.py` or the gateway attach/detach paths requires all of these plus `test_realtime_qa.py` and `test_realtime_qa_live.py`; the live files take about 2 minutes. Mutation checks for 9C (each must make a test fail): events not numbered, retention unbounded, a resume point ahead of the channel or already dropped accepted, replay repeating or replaying everything, a replaced socket's late detach not ignored, reattach not cancelling the expiry, expiry or shutdown not cancelling the generation, no cap on detached channels, channel not discarded on expiry, an empty channel retained, grace never started, the attached socket never woken, lag and control overflow undetected, send timeout ignored, the cancel result taken from the interrupted task instead of the database, the one-generation rule ignoring other sockets, the hello dropping the resume point, `resume_unavailable` never sent, a closing socket never detaching, `resume_seq` always 0, the server shutdown not cancelling.

## 3. Impact-Based Regression
How to decide what to rerun:
1. Identify changed **areas** (table below) from the diff paths.
2. Run the union of "required tests" for each changed area; plus anything that **imports/depends** on the changed module (use `pytest --testmon`/`vitest --changed` or path-mapping as available).
3. If a **shared/cross-cutting** module changed (auth, repository base, config, DB schema, provider ports, event schemas), escalate to the "wide" set in the matrix.
4. If uncertain, choose the larger set; never reduce below the area's required tests.
5. Record the selected set in the PR/commit notes ("tests run: …").

## 4. Test Impact Matrix
| Area changed | Required tests | Also run if… |
|---|---|---|
| `auth/*` (passwords, tokens) | auth unit+integration, token rotation, rate limit, contract(auth), isolation smoke | token claims changed → all authenticated-endpoint contract tests |
| Repository base / RLS / `user_id` scoping | **all isolation tests**, `tests/test_rls.py`, migration up/down, repo integration | always full isolation suite |
| DB schema / migration | migration up/down, affected repo tests, schema contract | touches `chunks` → retrieval + ingestion tests |
| Upload validation / storage adapter | validation matrix, storage adapter tests, resume+doc API tests | storage keys changed → delete cascade tests |
| Extractors / chunker | unit golden-file tests, ingestion integration | chunk shape changed → retrieval + embedding tests |
| Ingestion jobs / worker / queue | job state machine, retry/idempotency/reaper, failure injection, ingestion e2e | status enum changed → API contract + web status UI tests |
| Embedding adapter / model | embedding contract, retrieval relevance eval, re-embed test | dimension changed → migration + retrieval tests |
| VectorStore / retrieval | retrieval isolation (adversarial), relevance eval, delete cascade | filter logic changed → **full isolation suite** |
| Provider ports/adapters | adapter contract suite (all adapters), router/fallback, streaming | port signature changed → all callers (answers, classifier, vision) |
| Router/fallback config | router tests, latency-timeout tests | — |
| PreferenceCompiler / PromptBuilder / templates | compiler unit, **prompt snapshot tests**, conformance checks | new `prompt_version` → answer pipeline integration |
| Session API / wizard | session API + isolation, contract(sessions); wizard: `npm run lint/typecheck/build`, `npm test` (pure logic + source guards), `tests/test_wizard_api_flow.py` (API contract), manual checklist `docs/WIZARD_MANUAL_CHECKLIST.md` (no Playwright, D-37) | job-import changed → SSRF suite |
| Typed Q&A page (8C) | `npm run lint/typecheck/build`, `npm test` (`tests/qa.test.ts`: SSE parser, event validation, state machine, `runAsk`, streaming request; source guards), backend `tests/test_qa_sse_contract.py` (golden SSE transcript shared with the web tests), manual checklist `docs/QA_MANUAL_CHECKLIST.md` | SSE event/payload or header change → update `tests/fixtures/sse_*.txt` and both suites; `lib/api.ts` or `auth.tsx` changed → whole `npm test` |
| Job-import (URL fetch) | SSRF suite, timeout/size limits, manual fallback test | — |
| Screenshot input (Phase 11A, D-51) | `tests/test_screenshot_validation.py` (PNG validation, pure), `tests/test_vision_provider.py` (port, fake, router, config), `tests/test_body_limit_patterns.py`, DB-backed `tests/test_screenshot_api.py` (auth, isolation, limits, failures, cancellation/timeout, no persistence, log/metric redaction, `input_source`) | vision port, PNG validation or the endpoint changed → these suites; question contract or `input_source` changed → also `test_qa_api.py`, `test_qa_sse_contract.py`, `test_realtime_qa.py`; every vision test uses the deterministic fake (no real provider) |
| Screenshot input, desktop (Phase 11B) | `apps/desktop`: `tests/capture-service.test.ts` (state machine, gating, cancellation, wiping), `tests/capture-ipc.test.ts` (the real IPC handlers with fakes: sender validation, flow, discard triggers), `tests/capture-api-client.test.ts`, `tests/capture-store.test.ts` (renderer flow), `tests/capture-guards.test.ts` (source guards, bridge allow-list, CSP/hardening unchanged), plus `npm run typecheck`, build, `package:win`, packaged launch smoke; owner manual checklist for the real screen capture | capture/IPC/bridge/settings changed → all of these; backend screenshot or question contract changed → the 11A backend suites too |
| Desktop session creation (Phase 11.5) | `apps/desktop`: `tests/session-form.test.ts` (shared rules), `tests/session-create-ipc.test.ts` (real IPC handlers with fakes: sender validation, input refusal with zero requests, error mapping, no content in logs or renderer payloads), `tests/session-create-api-client.test.ts`, `tests/session-create-store.test.ts` (create flow and the refresh-clears-stale-error fix), plus the source guards in `tests/capture-guards.test.ts`; `typecheck`, build, `package:win`, packaged-app smoke against a local backend with fake providers | session/resume/document/options contracts changed → also `test_sessions`, `test_wizard_api_flow`, `test_ai_api` on the backend |
| Answer generation pipeline | grounding/canary tests, streaming, latency metric tests, contract(WS/SSE); citation-integrity check (D-48): `tests/test_qa_grounding.py` (pure validator + builder evidence), `tests/test_qa_grounding_pipeline.py` (DB-free orchestration: streaming unchanged, failure isolation, privacy), DB-backed persistence cases in `tests/test_qa_service.py` and `tests/test_qa_schema.py` | retrieval or prompt changed → their suites; evidence tag format changed → `tests/test_qa_grounding.py` and the prompt snapshot tests |
| WS gateway / event schema | WS contract tests, auth/ticket, reconnect/cancel, client mock tests | schema change → desktop + web client type-gen + contract tests |
| Question detector | detector eval (en/hi/te) precision/recall gate, heuristic unit tests | model/prompt change → full eval |
| STT adapter / audio pipeline | STT contract, audio replay test, latency measurement | — |
| Screenshot pipeline | image validation, vision adapter contract, retention sweep | — |
| Desktop client | client unit/component, WS mock integration, manual OS checklist | native (Rust) change → capture/keychain checklist |
| Web UI only (no API change) | component tests, affected e2e flow | API client regenerated → contract tests |
| Config/env/secrets | config loading unit, startup smoke | — |
| Metrics and telemetry | health/metrics, AI router, worker and gateway tests | verify no user/content values occur in metric labels |
| Infra/compose/Docker | compose smoke, health/readiness | — |
| Docs only | none (link check) | — |

## 5. Contract Tests (mandatory when interfaces change)
- **REST:** OpenAPI generated from FastAPI is committed/snapshotted; CI diff flags breaking changes (removed fields, type changes, status codes). Property-based checks (schemathesis) on changed endpoints. Web/desktop API clients are generated from the schema; type-check failure = contract break.
- **WebSocket:** JSON Schema per event; server and client both validated against it in tests; golden transcripts of event sequences.
- **Provider ports:** one parameterized suite (streaming order, usage reporting, error mapping, cancellation, language handling) executed against every adapter using recorded fixtures.
- **DB:** migration tests (upgrade/downgrade on seeded DB), schema snapshot.
- Any change to API.md/ports requires updating contract tests in the same change.

### Electron desktop MVP
From `apps/desktop/`, run `npm run typecheck`, `npm test`, and `npm run build`. The Vitest suite covers backend URL policy, authentication/refresh, API error mapping, SSE chunk/order/termination/cancellation, secure token persistence, settings validation, renderer trust checks, window controls, and renderer stream-state handling. Use `docs/DESKTOP_MANUAL_CHECKLIST.md` for visible startup, tray, authentication, API, and interaction checks; the desktop test suite does not claim to replace a real Windows UI run.

## 6. Critical-Path Smoke Tests (fast, run on every PR touching backend/web; minutes)
1. Health/readiness.
2. Sign up → login → refresh → logout.
3. Upload resume → ingestion → Ready (fake embeddings).
4. Create session (valid + invalid ownership).
5. Typed question → streamed answer (fake LLM) with persisted metrics.
6. Two-user isolation spot check (list, get, retrieve).
Post-deploy: same flow against staging.

## 7. Full Regression (only at boundaries)
Run at: milestone ends (M1–M5), before release, after changes to shared/cross-cutting modules flagged "wide", nightly on main, and after dependency major upgrades. Includes L0–L4 all, full isolation suite, contract suites, non-functional evals/load.

## 8. Mandatory Special Suites
- **User isolation suite:** two (and three) user fixtures; every user-owned endpoint tested for list/get/update/delete/retrieve/WS/export cross-access; adversarial retrieval with *canary strings* planted in other users' and unselected documents that must never appear in prompts or outputs.
- **Security suite:** auth brute-force/rate limit, JWT tampering/expiry, refresh reuse, upload spoofing, zip/PDF bombs, SSRF (private IPs, DNS rebinding, redirects, metadata endpoints), prompt-injection samples, log-redaction checks.
- **AI quality evals (non-gating at first, gating after baselines):** run `cd services/api; python -m tests.rag_evaluation` for the synthetic retrieval gold set (Precision@K, Recall@K, MRR, and fake-embedding latency). Pass manually adjudicated answer judgments with `--answers <json>` to measure answer/context relevance, faithfulness, unsupported-claim rate, hallucination-case rate, and answer latency. These answer metrics are human-scored; do not infer them from canned/fake-provider outputs.

## 9. Test Data & Environments
Synthetic resumes/documents only (no real PII). Fixtures versioned in repo. Deterministic fake providers. Ephemeral Postgres/Redis and a per-test temporary storage directory. Flaky tests are quarantined with an owner and issue, not ignored.

## 10. Reporting Rules
State exactly which tests were run and results; failures reported with output; skipped tests called out; never claim passing without running.
