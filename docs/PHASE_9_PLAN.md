# Phase 9 Plan — WebSocket Gateway & Realtime Session Core

**Status: the owner approved this plan with the decisions recorded in `docs/DECISIONS.md` D-41 (resume Option A, contract changes, Redis tickets, in-repo generator, web stays on SSE, reject a new question during generation, default limits, housekeeping). Sub-phase 9A is accepted; 9B is accepted (design in D-42); 9C is accepted (design in D-43). **Phase 9 is accepted and complete (owner-approved)**.** The text below is the original proposal, kept for reference; where it differs from D-41 (for example `answer.error` and `audio_params`, and the in-memory ticket store that exists only in tests as a fake Redis client), D-41 and `docs/API.md` section 8 are authoritative.

Written for: the project owner (planning and approval).

Sources: `docs/PHASES.md` (Phase 9), `docs/API.md` §1 and §8, `docs/ARCHITECTURE.md` §8, `docs/DECISIONS.md` (D-08, D-38 to D-40), `docs/RISKS_AND_OPEN_QUESTIONS.md` (OQ-2, R-14, R-38, R-43), `docs/TESTING_STRATEGY.md`, `CLAUDE.md`, and the Phase 8 code under `services/api/app/qa/`.

## 0. Phase 8 manual testing versus Phase 9 readiness

**Phase 8 manual testing (still pending):** `docs/QA_MANUAL_CHECKLIST.md` has not been run by a person in a real browser. Phase 8 is technically approved (automated verification), not "accepted". Phase 9 does not depend on the manual run.

**Phase 9 work that is ready to begin:** all backend work, contract and doc updates, and tests. Phase 9 needs no browser, desktop client or real LLM provider. It runs with the fake provider and a Python WebSocket test client.

## 1. Approved scope (from the docs, nothing added)

**PHASES.md Phase 9, "WebSocket Gateway & Realtime Session Core".** Objective: a real-time channel without audio.

Scope:
- WebSocket endpoint
- tickets
- event schema plus generated types
- heartbeat
- reconnect/resume
- cancellation
- backpressure
- typed Q&A over WS

Acceptance:
- protocol events match API.md
- reconnect resumes
- an unauthorized or other-user session is rejected
- cancellation works

Targeted tests: WS contract tests, auth/ticket tests, reconnect/cancel tests, load smoke (N concurrent connections).

Supporting detail: API.md §8 and the `/auth/ws-ticket` row; ARCHITECTURE §8; D-08 (SSE remains a fallback); R-14 and R-43.

**Not Phase 9:**
- audio and transcript events
- `question.detected`, `question.possible`, `force_answer`, `dismiss`
- screenshots
- `answer.regenerate`
- `prefs.update`
- `provider.fallback`
- `/export`, `/metrics`, `/me/history`
- the desktop client

## 2. Integration points in the Phase 8 code

| Piece | What Phase 9 reuses or must handle |
|---|---|
| `QuestionAnswerer.ask(user_id, session_id, text, override)` in `app/qa/service.py` | A transport-agnostic async generator of `QaEvent` objects. It persists as it goes. Closing it records a `cancelled` answer with the partial text. This is the only generation path. |
| `app/qa/events.py`, `app/qa/sse.py` | Six event shapes already exist. The WS adapter maps the same `QaEvent` objects to envelopes, so no Q&A logic is duplicated. |
| `QuestionLimits.admit()` returning `Slot` | Per-user rate limit (default 20/min) and 2 concurrent generations. Reused unchanged. The slot must always be released. |
| `history.py` GET endpoints | Remain the source of truth for completed answers and for resyncing after a reconnect. |
| `CurrentUser` / `get_current_user` (`app/auth/deps.py`) | HTTP-only (Bearer header); it cannot authenticate a WS handshake. That is why tickets are needed. |
| `require_trusted_origin`, `settings.cors_origin_list` | `CORSMiddleware` does not cover WebSockets, so the Origin check must be done by hand in the WS handler. |
| Middleware | `BodySizeLimitMiddleware` explicitly passes non-HTTP scopes through, and `@app.middleware("http")` does not touch WebSockets. No conflict found. |
| Redis | Only used as the Dramatiq broker and in the readiness check. There is no shared Redis client in `app.state`. |
| `uvicorn[standard]` | Already a declared dependency. Confirm the WS library is present at implementation. No new runtime dependency is planned. |

## 3. Contradictions and gaps found in the docs

Items 1 to 5 are contract items: they need an owner decision and a doc change before code (CLAUDE.md rule 3). Items 6 to 9 are context.

1. WS `answer.done` in API.md omits `question_id`, `provider`, `model` and the token counts that the SSE version carries.
2. WS has no `question.created`, but SSE does.
3. The reconnect field is `resume_from_seq` in API.md and `last_event_id` / a resume token in ARCHITECTURE §8.
4. `session.ready` includes `audio_params`, but audio is out of scope.
5. In-band cancel has no terminal event. SSE cancel is just a disconnect, so there is nothing to tell the client a cancel finished.
6. `/packages` for generated types does not exist, and the generator tool is unspecified.
7. Ticket storage location and binding rules are undefined.
8. OQ-2 (expected concurrent sessions) is listed as needed before Phase 9 and is still open.
9. D-08 and R-43 do not say whether the web page moves to WS in this phase.

## 4. Proposed design

### 4.1 Backend: new module `app/realtime/`

- `schemas.py`: Pydantic envelope `{type, id, seq, ts, payload}` with a discriminated union per event.
  - Client events: `client.hello`, `question.submit`, `answer.cancel`, `ping`.
  - Server events: `session.ready`, `question.created`, `retrieval.done`, `answer.started`, `answer.delta`, `answer.done`, `answer.error`, `answer.cancelled`, `error`, `pong`.
  - Q&A event payloads are identical to the SSE ones.
- `tickets.py`: a ticket store interface with a Redis implementation and an in-memory one for tests.
  - A ticket is 32 random bytes, urlsafe.
  - Only its sha256 hash is stored, with the user id and session id, and a TTL of about 30 s.
  - Redemption is atomic and single-use (`GETDEL`).
- `gateway.py`: the WS route `/api/v1/ws/sessions/{session_id}?ticket=...` and the connection handler.
- `channel.py`: the per-session channel (event sequence, bounded buffer, active generation).
- `adapter.py`: maps `QaEvent` to envelopes.
- `limits.py`: connection and message limits.
- Auth router: `POST /auth/ws-ticket {session_id}` returns `{ticket, expires_in}`. It reuses the session-ownership lookup, so another user's session returns the same `404 session_not_found` as an unknown one.

### 4.2 Connection flow

1. **Ticket request.** `POST /auth/ws-ticket` requires a Bearer token, checks ownership and a per-user rate limit, then stores the ticket hash.
2. **Handshake checks, before the connection is accepted.**
   - The Origin must be in the allow-list when present. Non-browser clients send none, as with `require_trusted_origin`.
   - The ticket must redeem, and its stored session must equal the session id in the path.
   - The user must still be active.
   - The session must still exist and belong to that user.
   - Every failure produces an identical rejection, so there is no oracle.
3. **Welcome.** The server sends `session.ready {session_id, server_time, resume_seq, protocol_version, limits}`.
4. **Hello.** The client's first message must be `client.hello` carrying `protocol_version`.
5. **One socket per user and session.** A newer connection supersedes the older one, which is closed with a "superseded" code. This makes reconnect simple when the server has not yet noticed that the old socket died.

### 4.3 Typed Q&A, cancellation, backpressure, resume

- **`question.submit {text, question_type_override?}`.** Calls `limits.admit()` and then `ask(...)`. Identity comes only from the ticket, never from the message. Events go out as envelopes with an increasing `seq`. Pre-stream refusals (`404`, `409 session_not_ready`, `409 materials_not_ready`, `422`, `429`) arrive as `error` events carrying the same codes.
- **`answer.cancel {question_id}`.** Cancels the generation. The answer ends `cancelled` with its partial text, the slot is released, and the server sends `answer.cancelled`. Cancelling an unknown, foreign or already finished question returns a non-fatal `error` and never reveals whether the id exists for someone else.
- **Heartbeat.** The client sends `ping` about every 20 s and the server answers `pong`. The server closes the socket if it hears nothing for about 45 s.
- **Backpressure.** Generation is decoupled from the socket through the per-session bounded buffer. If a client falls behind the buffer's oldest event, the server closes with a "slow consumer" code, and the client can resume or resync. Frame size and message rate limits apply as well.
- **Reconnect/resume (recommended option A, see decision 1).** Generation continues for a short grace period after the socket drops, and events keep going into the bounded in-memory buffer with their `seq`. A reconnect with `resume_from_seq` replays what was missed and continues live. If nobody reattaches within the grace period, generation is cancelled, the answer is stored `cancelled` with its partial text (today's behaviour), and the buffer is released. A `resume_from_seq` that has fallen out of the buffer returns a non-fatal `error resume_unavailable`, and the client resyncs through `GET /sessions/{id}/questions`.

### 4.4 Database and migrations

None proposed. Answers are persisted exactly as in Phase 8, and the resume buffer is in memory. A Redis or database buffer would need a decision and possibly a migration, which is not recommended for now.

### 4.5 API changes (contract-first; API.md updated before or with the code)

- Add `POST /auth/ws-ticket` and mark the §8 WebSocket section implemented.
- Add `question.created`, `answer.cancelled`, and an optional `audio_params` (absent in Phase 9).
- Make the WS `answer.done` payload identical to the SSE one.
- Pick one name for the resume field.
- Define close codes, limits and the single-connection rule.
- Define where `protocol_version` lives (the `client.hello` payload).
- The REST and SSE endpoints do not change.

### 4.6 Frontend

None. The web page keeps SSE (D-08 keeps it as the fallback). See decision 5 for the alternative.

### 4.7 Generated types

Create `packages/protocol/` with a JSON Schema exported from the Pydantic models (committed) and TypeScript types generated from it by a small in-repo script. No new dependency is added. A test fails if the committed files are out of date.

### 4.8 Settings (all configurable, proposed defaults)

| Setting | Proposed default |
|---|---|
| Ticket TTL | 30 s |
| Heartbeat | 20 s |
| Idle timeout | 45 s |
| Max frame | 16 KiB |
| Messages per second | 10 |
| Connections per user | 3 |
| Total connections (OQ-2) | 200 |
| Resume buffer events | 2000 |
| Resume grace | 30 s |
| Ticket requests | 30 per minute per user |

## 5. Sub-phases

| Step | Contents | Migration |
|---|---|---|
| **9A** | Protocol schemas, `packages/protocol` generation, ticket endpoint and store, WS handshake, `client.hello`, `session.ready`, ping/pong, idle timeout, close codes, Origin check, connection limits, doc updates | none |
| **9B** | `question.submit` over WS through `ask`, the `QaEvent` adapter, the per-session channel with `seq` and buffer, `answer.cancel` and `answer.cancelled`, rate and concurrency reuse, backpressure, frame and message limits | none |
| **9C** | Detached generation with grace period, `resume_from_seq` replay, the resume-unavailable path, load smoke, closure docs (a new D-41, risks, TESTING_STRATEGY), full regression | none |

## 6. Security and user isolation

- **Ticket:** random, hashed at rest, single-use, about 30 s, bound to the user and session, and never logged. Uvicorn's access log writes the query string, so the ticket must be redacted from it or access logging turned off for the WS path. Verify during implementation.
- **Identity** comes only from the ticket. No client message can carry a user id, session id or material ids.
- **Rejection:** an invalid, expired, reused or mismatched ticket, an unknown session, another user's session, a bad Origin and a disabled user all fail identically.
- **Resume** replays only from the channel keyed by `(user_id, session_id)`, so one user cannot read another's buffer.
- **Cancel** works only on questions in the caller's own session channel.
- **Logs** carry ids, codes and timings only, never question text, answer text or tickets.
- **Safe errors:** WS `error` and `answer.error` events carry fixed messages, as in SSE.
- **Abuse limits:** frame limits, message rate limits and connection caps.
- **Concurrency:** a slot is always released, whether the generation finishes, fails, is cancelled, or is abandoned after the grace period.

## 7. Test strategy

- **WS contract tests:** every event validated against the JSON Schema; golden transcripts for a successful answer, `[fail]`, `[partial]` and cancel; parity with the SSE golden transcripts for the shared payloads.
- **Auth and ticket tests:** missing, garbage, expired, reused and wrong-session tickets; wrong user; disabled user; bad Origin; rate limit; deleted session.
- **Isolation tests (two and three users):** a ticket request for a foreign session is indistinguishable from an unknown one; no cross-user resume; no cross-session cancel; canary strings from an unselected document never appear in events.
- **Protocol tests:** hello and version mismatch, unknown message type, oversized frames, malformed JSON, message-rate limiting, a first message that is not `client.hello`.
- **Reconnect and cancel tests:**
  - Replay from `resume_from_seq`, with no duplicate or missing `seq`.
  - Resume after a grace expiry, and a resume that fell out of the buffer.
  - A superseded connection.
  - Cancel at each stage: before the first token, mid-stream, and after done (a no-op that cannot downgrade a completed answer).
- **Regression:** the Phase 8 suite unchanged (SSE tests, golden transcripts, the 80 frontend tests), run in full at the 9C boundary.
- **Real-socket tests (the 8B pattern with a real uvicorn):** disconnect cleanup, slot release, a slow consumer, and a load smoke of N concurrent connections (default 50) with fake providers.
- **Mutation and security checks (the project's earlier pattern; each mutation must make at least one test fail):**
  - drop the ownership check on ticket issue
  - make a ticket reusable
  - skip the path-versus-ticket session match
  - skip the Origin check
  - replay without the session binding
  - skip the slot release
  - allow a downgrade of a completed answer
  - log the ticket
- **Quality gates:** `ruff format`, `ruff check`, `mypy --strict`, plus the real-stack script extended for WS.

## 8. Explicitly out of scope

Audio and binary frames; transcripts; question detection; screenshots; `answer.regenerate`; `prefs.update`; `provider.fallback`; export; aggregated metrics; `/me/history`; the desktop client; a real LLM adapter; Redis-backed shared limiters (R-38 stays); database changes; any web UI change; Playwright; multi-node WebSocket scaling (R-14 stays documented).

## 9. Decisions requiring owner approval before implementation

1. **Resume semantics (largest decision).**
   - **Option A (recommended):** detached generation with a grace period and an in-memory buffer, as in 4.3.
   - **Option B:** cancel on drop and resync from history. This is simpler but does not resume the in-flight answer.
   - Cost of A: per-process state and sticky sessions (R-14), and a generation that outlives its socket by up to the grace period.
2. **Contract changes in API.md:** approve adding `question.created`, `answer.cancelled` and `POST /auth/ws-ticket`, making the WS `answer.done` payload equal to the SSE one, and naming the resume field `resume_from_seq`.
3. **Ticket storage:** Redis (recommended, already in the stack and works across workers) versus in-process (breaks when the ticket request and the WS handshake hit different workers). Tests would use the in-memory store.
4. **Types generation:** the in-repo generator with no new dependency (recommended), or a dev dependency such as `json-schema-to-typescript`.
5. **Does the web page move to WS in this phase?** Recommended: no, keep SSE there, so Phase 10's desktop is the first real WS consumer. The catch is that R-43 (an answer is lost when the user leaves the page) stays open for the web. If the page should move, first verify that Next's `/api` rewrite proxies WebSocket upgrades, which is unverified.
6. **A new question while one is generating:** reject it with a non-fatal error (recommended, matching the Phase 8 web page where Ask is disabled) or cancel the old one and start the new one, as ARCHITECTURE §8 says.
7. **Scale inputs (OQ-2):** approve the proposed defaults in 4.8, or provide the expected concurrent sessions.
8. **Doc housekeeping:** may the stale status text from the Phase 8 closure audit be fixed as part of 9A (`API.md` line 3 and the 7D/8C status lines, without marking anything accepted unless the owner says so)? Is the first git commit still the owner's call? (No commit will be made by the assistant.)
