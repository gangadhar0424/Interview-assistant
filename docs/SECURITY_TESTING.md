# Security testing harnesses

Prepared 2026-11-23 (D-83). These are the repeatable probes used in the security sprints. Each one attacks **only a system you started yourself for the purpose**: a loopback instance, a throwaway database, synthetic accounts, a throwaway container. None of them may be pointed at production, at a shared environment, or at anyone else's system. None starts or stops a process by name; each stops only what it started.

| Harness | What it attacks | Where | Last result |
|---|---|---|---|
| `services/api/scripts/security/api_pentest_probe.py` | the API over HTTP: missing and forged tokens, cross-user access (IDOR), injection strings, uploads, body limits, headers and CORS, error leakage, rate limits, refresh-token reuse, handoff binding, `/metrics` token | `127.0.0.1` API on a throwaway database | 44 PASS, 0 FAIL (2026-11-23) |
| `services/api/scripts/security/ws_attack_probe.py` | the WebSocket gateway: handshake, ticket replay, wrong session or user, Origin, oversized and binary frames, malformed and flooded messages, version and timeout handling, connection caps, ticket rate limit, ticket in logs | `127.0.0.1` API (short test timeouts) | 28 PASS, 0 FAIL (2026-11-23) |
| `apps/desktop/scripts/security/packaged-app-probe.mjs` | the **packaged** Windows app: the real fuses in the executable, renderer isolation, the exposed bridge, navigation, CSP, permissions, IPC input validation, capture-privacy default | an unpacked build launched with an isolated profile and a DevTools port | 19 PASS, 0 FAIL on the production build (2026-11-23) |
| `infra/deploy/proxy-check/run.sh` | a real reverse proxy in front of the web app and API: what `X-Forwarded-For` the API receives, and whether one client's login failures throttle another | Docker network with fixed client addresses | both layouts correct (2026-11-23) |
| `infra/backup/drill-tamper.sh` | backup and restore with every kind of damage, against a real PostgreSQL | disposable `pgvector/pgvector:pg16` container on `127.0.0.1:55432` | 25 PASS, 0 FAIL (2026-11-23) |

## How to run them

* **API probes:** create an empty database, run the migrations against it (`DATABASE_URL=... APP_ENV=local uv run alembic upgrade head`), start the API on a loopback port with that database (and `LLM_ENABLED_PROVIDERS=fake STT_ENABLED_PROVIDERS=fake` for the WebSocket probe; short `WS_TICKET_TTL_SECONDS=5 WS_HELLO_TIMEOUT_SECONDS=4 WS_TICKET_RATE_LIMIT_PER_MINUTE=100` for it too), then run the script with the paths its header names. The ports in the scripts (8765 and 8767) are constants at the top. Drop the database and stop only the process you started when done.
* **Packaged-app probe:** build an unpacked app into a temporary folder (`electron-builder --dir` with a temporary `directories.output`), then `node packaged-app-probe.mjs "<path to exe>" <debug port> <apps/desktop path> desktop`. It opens the app's window briefly and ends the process tree it started.
* **Proxy check and backup drill:** read the header of each script; both create and remove their own containers.

## What these harnesses do **not** cover (NOT TESTED)

* Production, any real network, TLS termination, or a hosted reverse proxy other than the Caddy used here.
* The real updater against a real signed build and update feed (see `docs/DESKTOP_UPDATES.md` section 11): update tampering is tested with the real manager, real signature checks and real file hashing, but with a fake engine and fake feed.
* IPC calls from a **different, untrusted sender** (a second frame or window): sender validation is unit-tested (`tests/ipc-validation.test.ts` and the IPC tests) and the trusted renderer was probed live, but an untrusted sender was not created in a running app.
* Dynamic `eval()` in the page: DevTools evaluation is exempt from the page's `unsafe-eval` restriction, so the probe verifies the enforced policy text instead.
* The M-15 capture behaviour on real Windows capture tools, multiple monitors and screen readers: that is the owner's manual session (`docs/M15_MANUAL_SESSION_RUNBOOK.md`).
* Fuzzing of file parsers, load and denial-of-service testing beyond the limits checks, and any attack on the model providers.

A passing run proves that these specific attacks failed on that day against that build. It is not a statement that the product is secure.
