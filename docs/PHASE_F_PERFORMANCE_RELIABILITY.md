# Phase F: performance and reliability baseline and results

Run 2026-11-24 on a developer laptop. **This is a baseline for this one machine, not a capacity statement for production.** No threshold was invented: every number below is a measurement, a configured value (labelled) or UNMEASURED with the reason. The harness is `scripts/perf/backend_reliability.py`; desktop numbers use the existing `scripts/bench/desktop_app_bench.py` (M-2). Raw result files were kept outside the repository (scratch) and are not committed.

## 1. Test environment

| Item | Value |
|---|---|
| Machine | Intel Core i5-1035G1 (4 cores, 8 threads, 1.0 GHz base), 7.8 GB RAM, one display |
| OS | Windows 11 Home build 26300 |
| Software | Python 3.12.13, Electron 45.2.3, desktop 0.1.0, PostgreSQL 16 and Redis 7 in Docker Desktop (the same machine, so database and application compete for the same four cores) |
| Backend under test | one `uvicorn` process (one worker), database pool `pool_size 10 + max_overflow 10`, pool wait 10 s, statement timeout 60 s, fake LLM and STT providers, fake embedder, throwaway database `ia_perf`, Redis database 1, loopback only, synthetic users and files |
| Code | the working tree of 2026-11-24 including the SEC-15 quotas, the SEC-35 connection fix and the SEC-36 storage fix described below |
| Not representative of | production hardware, network latency, TLS, a reverse proxy, multiple workers, real providers, real data volume |

Methodology notes: monotonic clocks; percentiles are nearest-rank; samples are small (see each row); a laptop under a browser and Docker Desktop is noisy, so rerun before relying on a difference of less than about 20 percent. A first, aborted run was discarded: it overlapped other work on the machine and left orphaned worker processes that polluted later results (see section 9).

## 2. Defects found by running the real thing (fixed in this phase)

| ID | Finding | Evidence | Fix | Regression test |
|---|---|---|---|---|
| **SEC-35** (Medium, availability) | Every authenticated request kept a pooled database connection until the whole request ended, because authentication left its transaction open. A slow request body, a long answer stream or a slow provider call therefore pinned one connection each. **25 stalled uploads from one account (more than 10 + 10) made every other user's request wait 10 s and fail with `503`**; 15 did not. Closing the sockets restored service at once | Live probe, before: other-user `auth/me` 10,010 ms and `503` x4 at 25 stalled uploads. After: 60 stalled uploads, other-user `auth/me` p50 about 30 ms, all `200` | `get_current_user` ends its transaction after reading the user; the upload route also releases it before reading the body | `test_authentication_leaves_no_database_connection_checked_out` (fails with 1 checked out before the fix), `test_no_database_connection_is_held_while_the_upload_body_is_read` (mutation-checked) |
| **SEC-36** (Low; Windows development) | Concurrent first uploads by one user failed with `InvalidStorageKey: storage key escapes the storage root` (HTTP 500): `Path.resolve()` of a path whose parent directories another thread was creating returned a path outside the root | 25 of 40 trials failed in a standalone reproduction; 0 of 40 after the fix. Found by the SEC-15 concurrency tests. Linux production behaviour was not tested | A per-storage lock around path resolution and directory creation (`app/storage/local.py`) | `test_concurrent_first_uploads_by_one_user_all_succeed_in_storage` |

**Trade-off of the SEC-35 fix (owner should know):** ending the transaction costs an extra round trip per request, because the handler's first query starts a new transaction. Measured in a clean A/B (two runs each, alternating, same machine), `GET /auth/me`, which does almost no other work: concurrency 10 gave about 100 requests a second with the change and about 140 without; concurrency 1 gave 48 against 70. That is the worst case, since this endpoint is nearly all overhead; requests that call a provider or stream an answer pay the same fixed cost on top of seconds of work. It buys protection against connection starvation. If the cost matters, the alternative is to release the connection only on routes that read a body or stream, at the price of leaving the others pinned.

## 3. Application performance (desktop)

Existing M-2 harness, 10 launches of the real packaged-mode build (`npm run build`, `electron .`, throwaway profile, no session, 12 s each). Baseline column is the M-2 baseline of 2026-11-18 (`PERFORMANCE_BASELINE.md`).

| Metric | Now: p50 | Now: p95 / max | M-2 baseline: p50 | Note |
|---|---|---|---|---|
| Start to visible window (ms) | **1,026** | 3,521 | 468 | slower; see below |
| App ready to window created (ms) | 146 | 271 | 104 | |
| Window created to ready to show (ms) | 692 | 2,958 | 296 | |
| Memory peak, main process (MB) | 99.9 | 102.3 | 99.6 | unchanged |
| Memory peak, renderer (MB) | 75.7 | 78.5 | 76.9 | unchanged |
| Memory peak, GPU process (MB) | 99.5 | 102.1 | 101.1 | unchanged |
| Memory peak, utility (MB) | 46.8 | 47.0 | 46.6 | unchanged |
| CPU, largest sample, main process (%) | 8.1 | 15.7 | 15.0 | includes the startup burst |

**Startup is slower than the baseline (median 1.0 s against 0.47 s, with one 3.5 s launch). I did not find the cause.** Differences since the baseline: Docker Desktop and its VM now run on the same machine, and the app gained the updater, the capture-privacy module and other code. The numbers cannot attribute the difference. It is a regression candidate, not a finding. Recommended: rerun with Docker Desktop stopped, then bisect with the `INTERVIEW_PERF` marks if it persists.

| Item | Status |
|---|---|
| CPU at idle over time | UNMEASURED beyond the sample above (the app has no periodic timer by design, so sampling is at events) |
| Multi-monitor behaviour | **UNMEASURED: this machine has one display.** Logic is covered by automated tests (D-71); `docs/MULTI_MONITOR_TESTING.md` is the manual procedure |
| Long-running session stability | **UNMEASURED.** Needs a multi-hour packaged session with a real or fake provider and a memory trace |
| Desktop crash and recovery, shutdown during active work | **NOT RUN in this phase.** Renderer-crash recovery is covered by `recovery.test.ts` and earlier packaged probes; no forced crash or kill during capture was performed now |

## 4. Interview response latency

**No real provider was called in this phase, and no human speech was available, so no new end-to-end latency was measured.** What exists is below; each row says where it came from.

| Stage | Value | Kind and source |
|---|---|---|
| End-of-speech (silence) window | 700 ms | configured constant (`END_SILENCE_WINDOWS = 35` of 20 ms), not measured |
| Silence detection and segmentation on the desktop | about 0.01 ms per frame, 18.8 ms for 30 s of audio | measured, synthetic audio (M-2) |
| Audio upload alone | UNMEASURED | `fetch` does not separate upload from server time |
| Upload plus STT, Groq `whisper-large-v3-turbo` | p50 347 ms, p95 751 ms | measured, 20 clean synthetic clips (M-3) |
| Upload plus STT, Gemini | p50 3,434 ms, p95 9,372 ms | measured (M-3) |
| LLM time to first token, Gemini `gemini-3.1-flash-lite` | p50 2,810 ms, p95 9,362 ms | measured (M-3) |
| Total answer, Gemini | p50 3,150 ms, p95 9,405 ms | measured (M-3) |
| Packaged-application end to end with real speech | **UNMEASURED** | needs the owner: `docs/M13_STREAMING_STT_DECISION.md`, section 7 |

## 5. Backend performance (this run)

### 5.1 Concurrent API requests

Authenticated `GET /auth/me` (token check plus a database read), 5 users, 8 seconds per level, one API process. Two clean runs; the table shows the range. Every request returned `200`.

| Concurrent clients | Requests a second | Latency p50 (ms) | p95 (ms) |
|---|---|---|---|
| 1 | 48 | 19 to 20 | 31 to 33 |
| 10 | 100 to 104 | 86 to 93 | 132 to 159 |
| 50 | 99 to 100 | 367 to 382 | 1,227 to 1,258 |
| 100 | 82 to 83 | 824 to 843 | 3,562 to 3,665 |
| 200 | 60 to 62 | 1,879 to 1,981 | 7,478 to 7,500 |

Throughput saturates near 100 requests a second for this trivial endpoint on this laptop (one process, one core busy), and latency then grows with the queue, as expected. Production capacity is **not known**.

### 5.2 Database pool under overload

300 concurrent clients for 10 s against the same endpoint (far more than the 20 connections): 758 requests, **no errors**, 46 requests a second, latency p50 5.4 s, p95 10.3 s, max 13.7 s. The pool queues rather than failing here; the 10 s pool wait only produced `503` when connections were pinned (SEC-35, section 2).

### 5.3 Large valid payloads and oversized bodies

| Case | Result |
|---|---|
| Upload 1, 5, 10, 19 MB (document limit 20 MB) | `201` in 265, 178, 508, 619 ms |
| Four concurrent 8 MB uploads, distinct users | all `201`, 945 to 966 ms |
| 21 MB upload (over the limit) | refused in 8 ms; the server closed the connection, so the client sees a reset instead of the `413` body (documented in `API.md`) |
| 3 MiB JSON body (over the 2 MiB cap) | refused in 20 ms, same behaviour |

### 5.4 Slow clients

| Case | Result |
|---|---|
| 60 connections that never finish their request headers | no effect: normal requests 27 ms median, all `200`. Whether the server ever closes them was **not measured**; the production command sets no read deadline, so a reverse proxy must |
| 10, 25 and 60 authenticated uploads that promise a body and stall (one user) | after the SEC-35 fix: the same user and other users stay at about 20 to 50 ms, `200`, `/health` `200`; closing the sockets changes nothing. Before the fix: `503` after 10 s at 25 |

### 5.5 Sustained load

20 concurrent users for 90 s: about 80 percent `GET /auth/me`, 10 percent document lists and 10 percent small text uploads, and the ingestion worker (fake embeddings) running on the same machine. **2,814 requests, no errors** (2,534 `200`, 280 `201`); latency p50 536 ms, p95 1,360 ms, p99 1,907 ms, max 2,921 ms. API CPU used 69.5 s in 90 s (about 0.8 of a core); the worker 53 s over the same period.

**Memory is not conclusively measured.** Working-set readings of the API process tree moved between 56 and 151 MB during the run with no clear trend, and Windows trims working sets on its own; 90 seconds is too short to show a leak. UNMEASURED: a multi-hour soak with a stable measure (private bytes) is needed.

### 5.6 Worker

| Case | Result |
|---|---|
| Normal ingestion (extract, chunk, embed with the fake embedder) | 4.3 s from upload to completed |
| Worker killed hard (process tree), then an upload | the job stays `pending`; no data lost |
| Worker restarted | the queued job completed 7.5 s later **without** running the recovery sweep (the Redis queue held the message) |
| Recovery sweep (`python -m app.workers.recovery`) | not needed in this case; it exists for messages lost when Redis loses its queue (SEC-13) |

## 6. Reliability (local drills; not production validation)

| Failure | What happened | Recovery |
|---|---|---|
| **Redis stopped** (12 s) | `/health` `200`; `/ready` `503` (about 2 s); authenticated reads (`auth/me`, resumes) kept answering `200` within about 30 ms; a login with an unknown email returned the normal `401` in 118 ms (whether the login limiter fails open or closed was not determined) | `/ready` and authenticated requests healthy 0.1 s after Redis reported healthy |
| **PostgreSQL stopped** (14 s) | `/health` `200`; `/ready` `503`; every database-bound request returned `503` with `Retry-After: 5` in about 4.1 s (no hang, no stack trace); a document upload during the outage returned `503` in 4.1 s and **left no stored file and no temporary file** | healthy 0.2 s after PostgreSQL reported healthy |
| **Worker killed** | see 5.6 | queued work resumed on restart |
| Provider timeouts and failures | **NOT RUN** (fake providers). Covered by automated tests of the router, breaker and error mapping, and by real failures seen in M-3 (`llm_quota_exhausted`, `llm_rate_limited`) | |
| Network interruption between desktop and API | NOT RUN beyond the two outages above | |
| API shutdown during active work | NOT RUN in this phase (a graceful-stop drill was done in M-12, D-79) | |

## 7. What this baseline does not cover

Real providers and speech; the packaged desktop under a long session; multiple monitors; production hardware and networking; the real reverse proxy (slow-header handling, request timeouts); multiple API workers; a memory soak; provider timeout behaviour with real providers; behaviour with the real embedding model (the fake embedder was used, so worker CPU and memory are understated).

## 8. How to reproduce

```powershell
# Backend (starts its own API and worker on 127.0.0.1:8766, throwaway DB ia_perf, Redis db 1;
# stops and restarts THIS project's postgres and redis containers during the outage scenarios)
cd services/api
.venv/Scripts/python.exe ../../scripts/perf/backend_reliability.py --out result.json --sustain 90
# one scenario: --only a_concurrency | b_large_payloads | c_slow_clients | d_pool | g_worker | h_sustained | e_redis_outage | f_db_outage
# Desktop startup and memory (existing M-2 harness)
cd apps/desktop; npm run build; cd ../..; python scripts/bench/desktop_app_bench.py --runs 10 --exit-ms 12000 --out bench-app.json
```

## 9. Honest notes on the run

- My first run was aborted (a harness bug in the slow-client scenario). Killing its launcher processes left two **orphaned worker processes that kept consuming the shared Redis queue** with a deleted storage path, which made later ingestion look as if it failed with `file_missing` and polluted worker and sustained-load numbers. I found them by reading the worker's behaviour, killed them, and reran the affected scenarios; the numbers in this document are from the reruns. This is a harness problem, not an application defect.
- An early version of the probe reported memory and CPU for the 5 MB virtual-environment launcher instead of the server; those numbers were discarded and the harness now sums the process tree (still noisy, see 5.5).
- Several earlier-looking numbers were noisy because other work ran on the machine; the concurrency table comes from two clean alternating runs.

## 10. Phase F test plan (prepared 2026-11-26; PROPOSED, not started)

Built from the baseline above, `PERFORMANCE_BASELINE.md`, `PROVIDER_VALIDATION.md` and `M13_STREAMING_STT_DECISION.md`. Exit criteria are **proposals for the owner to set**, not measured facts or agreed targets. No new dependency is assumed: the plan extends `scripts/perf/backend_reliability.py`, `scripts/bench/desktop_app_bench.py` and `scripts/bench/backend_e2e_bench.py`, and uses the existing fake providers for load.

**M-13 rule:** streaming speech-to-text stays NO-GO until F3 below is measured and meets the conditions in `M13_STREAMING_STT_DECISION.md` section 6.

| # | Area | What exists | Gap | Method | Metrics | Proposed exit criterion (owner to set) | Needs owner |
|---|---|---|---|---|---|---|---|
| F1 | Speech-to-text latency | Groq p50 347 / p95 751 ms, Gemini p50 3.4 / p95 9.4 s (20 clean synthetic English clips, M-3) | real speech, accents, noise, Hindi and Telugu; packaged app | packaged app with `INTERVIEW_PERF=1`, at least 20 real utterances per language on the intended voice provider | p50/p95 upload plus STT; server-reported STT time; error rate | p95 within an owner-set budget; error rate recorded per class | yes: speech, keys, OQ-4 terms |
| F2 | LLM response latency | Gemini time to first token p50 2.8 / p95 9.4 s; Groq 3 samples only (token-per-minute limit) | funded accounts; the intended production model; prompt-size effect | rerun the answer part of M-3 with at least 50 questions per provider and model, short and long context | time to first token and total, p50/p95; refusals by error class | owner-set first-token budget; no unexplained error class | yes: funded accounts |
| F3 | End-to-end interview response | UNMEASURED (only a sum of medians, about 3.9 s, illustrative) | the whole chain on the packaged app | end of speech to first answer token on screen, decomposed: silence window, upload plus STT, detection, retrieval, first token, render | per-stage p50/p95 | decides M-13 (section 6 of the decision document) | yes |
| F4 | Concurrent users | `/auth/me` saturates near 100 requests a second on one process; 20 users for 90 s with no errors | realistic mix with answer streams (SSE and WebSocket) against a fake provider with realistic latency; several uvicorn workers | new scenario: N WebSocket sessions each asking every X s against a fake provider with about 3 s first token; levels 10, 25, 50, 100; 1 and 2 workers | p50/p95 first token added by the server, errors, event-loop lag, pool wait | no `5xx` and no dropped stream at the owner's target concurrency | target concurrency |
| F5 | Database | pool queues under 300 clients without errors; SEC-35 cost measured | query latency at realistic data volume (chunks, embeddings, hybrid retrieval) | seed a throwaway database with synthetic volume; time retrieval; `EXPLAIN ANALYZE` the heaviest queries; repeat the SEC-35 A/B | retrieval p50/p95; sequential scans; pool wait | retrieval within an owner-set budget; no sequential scan on user-scoped hot paths | SEC-35 trade-off decision |
| F6 | Redis | outage drill: `/ready` `503`, authenticated reads continue, recovery in 0.1 s | login limiter fail-open or fail-closed is unknown; WebSocket ticket issuance during an outage; cache memory growth; persistence per the SEC-13 decision | outage during login bursts and ticket requests; `INFO memory` during sustained load; after the SEC-13 decision, restart with the chosen persistence and the recovery sweep | limiter behaviour; error codes; memory growth | documented behaviour that matches `OPERATIONS.md`; no unbounded growth | SEC-13 Decision 2 |
| F7 | Worker recovery | hard kill: job stays pending, resumes on restart | lost Redis queue plus recovery sweep; real `fastembed` embedder CPU and memory; concurrent ingestion | flush the queue, run `python -m app.workers.recovery`, confirm completion; ingest 20 documents with the real embedder | time to recover; worker CPU and memory | every job reaches a terminal state; no lost upload | the 2 GB model download |
| F8 | Desktop resources | startup median 1.0 s vs 0.47 s baseline (cause not found); memory unchanged | startup regression; idle CPU; multi-hour session | rerun the bench with Docker Desktop stopped, bisect with `INTERVIEW_PERF` marks if it persists; 2-hour packaged session with a fake provider and an external private-bytes sampler (no in-app timers) | startup p50/p95; private bytes per process over time | startup back near baseline or explained; no steady memory growth | none for startup; a second monitor for multi-monitor |
| F9 | Provider failures and network interruptions | router, breaker and error mapping unit-tested; real `llm_quota_exhausted` and `llm_rate_limited` seen in M-3 | injected slow and failing providers end to end; desktop network drop mid-answer (the 9C `resume_from_seq` replay exists); API restart mid-answer | fault-injecting fake provider (latency, timeout, error, stall); stop the API during a stream; disable the network adapter on the desktop during an answer (manual) | user-visible outcome, recovery time, no stuck state | each failure ends in a fixed, safe error or a resumed stream within an owner-set time | manual network drop on the desktop |

**Sequencing.** Without the owner, in this order: F8 startup rerun; F6 limiter behaviour; F7 queue loss and sweep; F9 fault-injection provider; F4 concurrency scenario; F5 data volume. With the owner: F1, F2 and F3 together in one packaged-app session (this decides M-13). On production-like infrastructure (Phase G): repeat F4 to F6 on the target host, since the numbers above are for one laptop only.
