# Backend deployment (M-12, D-79)

Status (2026-11-22): **deployment definition, backup/restore and failure handling implemented and drilled on a local Docker host. NO production deployment exists and none was performed.** What a real production needs from the owner is listed in section 11. Read `docs/OPERATIONS.md` for the day-to-day runbooks this document builds on.

## 1. What is provided

| Piece | Where | Notes |
|---|---|---|
| API / worker / migration image | `services/api/Dockerfile` | One image, three roles by command. Non-root (uid 10001), no secrets, no `.env`, runs with a read-only root filesystem |
| Web image | `apps/web/Dockerfile` | `API_INTERNAL_URL` is a **build argument** (Next.js resolves the `/api/*` rewrite at build time) |
| Stack definition | `docker-compose.prod.yml` | migrate (one-shot), api, worker, web, postgres, redis (TLS); limits, health checks, graceful-stop windows. The `web` service was validated (`docker compose config`) but the drills above ran without it |
| Environment template | `infra/deploy/production.env.example` | Names and structure only; every value is `CHANGE_ME` |
| TLS proxy example | `infra/deploy/Caddyfile.example` | **Not exercised**; validate in your environment |
| Backup / restore / retention | `infra/backup/backup.sh`, `restore.sh`, `prune.sh` | Encrypted, verified, drilled (section 8) |
| Worker liveness | `app/workers/heartbeat.py`, `python -m app.workers.healthcheck` | Used as the worker container's health check |

Local development still uses `docker-compose.yml` (databases only) and the commands in `README.md`.

## 2. Architecture and trust boundaries

```
 Internet --TLS--> [reverse proxy: you provide] --http--> 127.0.0.1:8000 api ---> postgres (private network)
                                                          worker ---------------> redis (TLS, private network)
 api/worker share the uploads volume; nothing but the proxy listens on a public address.
```

* **No database or Redis port is published to the host** (verified: `docker port` is empty for both).
* The API runs with `--no-proxy-headers` and, behind your proxy, `TRUSTED_PROXY_ENABLED=true`, `TRUSTED_PROXY_HOPS=1`.
* Request limiters, the WebSocket registry and detached-answer replay are **per API process** (`docs/OPERATIONS.md`, "Known deployment boundaries"). Run **one API replica**, or put a sticky router in front, until they are Redis-backed. Do not claim global limits.
* CORS allows `GET`/`POST` only. The web app is same-origin through its `/api` rewrite and the desktop app is not a browser, so `PATCH`/`DELETE` (rename, delete) work today; a future cross-origin browser client would need this widened deliberately.

## 3. Configuration and startup validation

Production startup fails early and clearly when configuration is missing or unsafe (these gates predate M-12 and are covered by `tests/test_config.py`; a run in the image confirmed an incomplete production configuration exits non-zero with a message that names the problem and never prints the value):

* `JWT_SECRET`: at least 32 random bytes, not a placeholder, entropy-checked.
* `REDIS_URL` must be `rediss://`; `FILE_STORAGE_ROOT` absolute; `SIGNUP_INVITE_CODE` at least 16 characters when set (signup is closed without it).
* `EMBEDDING_PROVIDER` may not be `fake`; the fake LLM, vision and speech providers are refused outside `APP_ENV=local`; each enabled provider must have its key.
* **New in M-12:** database pool and timeout limits are validated (`DB_*`, see `.env.example`); unreasonable values are rejected at startup. `/docs`, `/redoc` and `/openapi.json` are served **only** with `APP_ENV=local`.

**Resolved (SEC-12, D-82):** `MAINTENANCE_DATABASE_URL` is no longer required at start-up and is no longer given to the API, worker, web or migrate containers. Only the opt-in `maintenance` service (compose profile `ops`) holds it, and the recovery and reindex commands refuse to run outside local without it. The one-shot `migrate` job now receives only `APP_ENV` and the migration `DATABASE_URL` (Alembic reads that variable directly).

## 4. First deployment

1. Provision PostgreSQL roles as in `docs/OPERATIONS.md` ("PostgreSQL RLS role provisioning"): an owner/migration login, a **non-superuser, non-owner** runtime login granted `interview_app_runtime`, and a maintenance login. Confirm the runtime login is not `BYPASSRLS`.
2. Create the Redis TLS material (`redis.crt`, `redis.key`, `ca.crt`). Give the API and worker **only** `ca.crt` (`REDIS_CA_FILE`); the key stays with Redis. Or use a managed Redis and set `REDIS_URL` to its `rediss://` address (then remove the `redis` service).
3. Copy `infra/deploy/production.env.example` to `infra/deploy/production.env` (git-ignored), fill it from your secret manager, and build and tag the image: `docker build -f services/api/Dockerfile -t interview-assistant-api:<tag> services/api`.
4. `docker compose -f docker-compose.prod.yml --env-file infra/deploy/production.env up -d --wait`. The one-shot `migrate` service applies all migrations in **one transaction**; if it fails nothing is half-applied and the API and worker do not start.
5. Smoke test: `GET /health` (liveness) and `GET /ready` (Postgres and Redis) on the loopback port, then through the proxy; sign up with the invite code, upload a file, wait for `completed`, ask a question with the configured provider. `/ready` deliberately does not test an LLM provider.
6. Put the proxy in front (`Caddyfile.example`), restrict `/metrics` and `/ready` to your monitoring network, and verify HTTPS and the security headers.

`EMBEDDING_PROVIDER=fastembed` needs the optional extra (`uv sync --extra embeddings`, about 2 GB of model data on first use). The image built here does **not** include it; add `--extra embeddings` to the Dockerfile's `uv sync` and a persistent cache volume if you enable it. This was not built or tested.

## 5. Health, readiness, graceful shutdown

* `/health` never depends on infrastructure. `/ready` checks Postgres and Redis with a 2 s bound each and returns only `ok`/`unavailable` plus an exception **class name**; no address, credential or message.
* Worker liveness: each running worker writes `ia:worker:heartbeat:<host>:<pid>` to Redis every 15 s with a 45 s expiry; `python -m app.workers.healthcheck` exits 0 if at least one is fresh. The heartbeat carries no job or user data, and a Redis failure never affects job processing.
* Stopping: uvicorn stops accepting, lets in-flight requests finish (`--timeout-graceful-shutdown 25`) and cancels detached generations (their answers are stored); the compose grace period (40 s) is longer. The worker has 60 s; ingestion jobs that do not finish are re-queued by the recovery sweep (schedule `python -m app.workers.recovery` about every minute; it is not scheduled by this compose file).

## 6. Database reliability

| Control | Setting | Default |
|---|---|---|
| Pool size per process | `DB_POOL_SIZE` + `DB_MAX_OVERFLOW` | 10 + 10 |
| Wait for a free connection | `DB_POOL_TIMEOUT_SECONDS` | 10 s, then a fast 503 |
| Connect timeout | `DB_CONNECT_TIMEOUT_SECONDS` | 5 s |
| Statement timeout (server side) | `DB_STATEMENT_TIMEOUT_MS` | 60 000 |
| Idle-in-transaction timeout (server side) | `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` | 300 000 |
| Connection recycle / pre-ping | `DB_POOL_RECYCLE_SECONDS` / always on | 1800 s |

Size: processes x (pool + overflow) must stay below Postgres `max_connections` minus headroom for operators and the maintenance identity. The 5-minute idle limit is far longer than any answer stream; if you see "terminating connection due to idle-in-transaction timeout" in Postgres logs, find the code path that held a transaction open rather than raising the limit.

Migrations run in one transaction (`alembic/env.py`), so an interrupted upgrade leaves the previous schema and version untouched (`tests/test_fault_injection.py` proves this with an injected failure and a retry). `head -> base -> head` was verified on a scratch database. **Review every new migration for lock duration and data volume before production**; there is no automated check for table-rewriting DDL, and downgrades drop data by design.

## 7. Failure behaviour (verified)

Drilled on the compose stack with the image built from this repository (`APP_ENV=local`; see section 11 for what that excludes):

| Event | Observed |
|---|---|
| Redis stopped | `/health` 200; `/ready` 503 naming `redis`; worker health check reports "cannot check workers"; after restart `/ready` and the worker heartbeat recover on their own |
| PostgreSQL stopped | requests return **503 `service_unavailable`** with `Retry-After` (a first drill returned 500: a DNS failure was not classified as an outage; fixed and re-run); after restart the API serves normally with no restart |
| Worker process exits unexpectedly | Docker restarted it (restart count 1) and the heartbeat resumed. A hard kill from the host counts as a manual stop for Docker, so it was not restarted; that is Docker behaviour, not the application's |
| API stopped | exit 0 in about 2 s with an orderly shutdown sequence |
| Migration re-run | no-op, exit 0 |
| Incomplete production configuration | the process refuses to start (exit 1) with a message that names the setting only |

Automated failure injection (`tests/test_fault_injection.py`, 49 tests): classification of outages versus bugs, a safe JSON 503/500 with no stack trace, SQL, driver text or user data in the response or the log, pool exhaustion failing in about 0.5 s and recovering, statement timeout cancelling a runaway query, idle-in-transaction termination, rollback leaving no partial change (including DDL), an injected mid-migration failure leaving the database unchanged and retryable, and the full migration chain round trip.

Already covered by earlier suites and cited here: duplicate and concurrent task delivery (`test_ingestion_jobs.py`, `test_embedding_pipeline.py`), retry safety, stuck-job recovery, auth throttling, security headers, body limits, RLS isolation, log redaction, readiness with unreachable dependencies.

## 8. Backup and disaster recovery

`infra/backup/backup.sh` writes `<BACKUP_DIR>/<UTC timestamp>/` containing the database (`pg_dump` custom format), the role definitions (`pg_dumpall --globals-only --no-role-passwords`), the uploads archive, a `MANIFEST` (schema version, row counts, and the length and SHA-256 of every file; no content) and `MANIFEST.mac`. **`BACKUP_PASSPHRASE_FILE` is mandatory** (SEC-10): a regular file whose first line is a passphrase of at least 32 bytes, not readable by group/other (the mode check is skipped under Git Bash/Windows; protect the file with ACLs). `backup.sh` refuses to run otherwise (non-zero exit, nothing written under `BACKUP_DIR`); there is no plaintext mode. Format `ia-backup-v2` (SEC-11): each file is AES-256-CTR with a PBKDF2-HMAC-SHA256 key (600000 iterations, fresh random salt per file, nothing written to disk in clear text), and `MANIFEST.mac` is an HMAC-SHA256 over `MANIFEST` under a separately derived key, so any change, truncation, swap (even with a validly encrypted file from another backup), missing, extra or reordered file is detected before anything is decrypted. Keys never appear on a command line. Only `openssl` 3, `bash` and coreutils are needed. `restore.sh` authenticates the whole set first and exits 4 before touching the database or uploads on a wrong passphrase or any tampering, and **refuses old-format backups** (plaintext, or encrypted without a MAC) with no downgrade; take a fresh backup with the new scripts instead. It then refuses a non-empty database or uploads location, restores, and compares the schema version and every recorded row count with the manifest. `prune.sh` is a dry run unless `--apply`, accepts only timestamp-named folders that are structurally valid `ia-backup-v2` backups, and always keeps the newest `MIN_KEEP`. Limit: an attacker who can write the backup directory can still delete a whole backup or replay an older complete one (the MAC proves authenticity, not freshness); keep an off-host copy and track the latest expected timestamp.

**Restore drill (2026-11-22, local Docker, encrypted backup of a stack holding two users, two ingested documents, chunks and embeddings):** restored into a brand-new empty PostgreSQL cluster and a new volume. Result: schema version `0011` and all nine recorded row counts matched; row-level-security flags identical to the source; the runtime role's privileges preserved; **tenant isolation held on the restored data** (each of the two users saw exactly their own document, and an empty identity saw none); a pgvector similarity query worked; all three uploaded files were byte-identical. Negative checks: restoring over a database that had data was refused (exit 3); a one-byte change to a backup was detected before anything was restored (0 tables written); a wrong passphrase failed; `prune.sh` refused unsafe settings. **The first attempt failed** because the dump refers to roles that do not exist in a fresh cluster; the backup now records the role definitions and the restore applies them on request (`RESTORE_ROLES=1`). Passwords are never in a backup: provision login passwords from your secret manager.

What this does **not** establish: restore from off-host storage, backups of a production-sized database, point-in-time recovery (WAL archiving is not configured), or any agreed RPO/RTO. Decide those, schedule the backup, copy it off the host, and repeat the drill on a schedule and after every migration.

Recovery steps: provision a new cluster and roles, create an empty database, run `restore.sh`, start the API against it **without** traffic, run the checks in section 4 step 5, then switch traffic. Redis is not a system of record: queue messages are repairable from `ingestion_jobs` with the recovery sweep.

## 9. Security review of M-12 (what was checked)

* Container: non-root, read-only root, `cap_drop: ALL`, `no-new-privileges`, CPU/memory/PID limits, no database or Redis port published, API bound to loopback, log rotation, no secrets in the image (the build context excludes `.env*`, tests and scripts).
* Secrets only from the environment; compose `:?` guards stop the stack when one is missing; the example environment file contains no credentials and the real one is git-ignored.
* API docs and OpenAPI schema off outside local; error responses carry fixed text and a request id only; the log records the exception class, never the message (driver messages can contain SQL parameters).
* Dependencies: no new Python dependency (PyMuPDF already present); the backup scripts use `openssl`, `tar`, `pg_*` and `busybox` only.
* Left unchanged and noted: CORS methods, per-process limiters, the `MAINTENANCE_DATABASE_URL` inconsistency, no WAF or global rate limit, no automated scan of the built image (run a scanner of your choice on `interview-assistant-api`), no SBOM or image signing.

## 10. Rollback

* **Application:** redeploy the previous image tag. Migrations are forward-applied by `migrate`; rolling back a release that included a migration needs a deliberate `alembic downgrade` (which can drop data) or a restore. Prefer forward fixes, and keep migrations backward compatible with the previous image for one release so an image rollback is safe.
* **Bad configuration:** restore the previous environment file; the startup checks make a bad value fail at start, not at run time.
* **Data:** section 8.

## 11. Verified, blocked and not done

| Item | Status |
|---|---|
| API image builds; migrate/api/worker/redis(TLS)/postgres stack comes up healthy | **Verified** locally (Docker) |
| Non-root, read-only filesystem, dropped capabilities, no published DB/Redis ports | **Verified** on the running containers |
| Redis outage, PostgreSQL outage and recovery, worker restart, graceful stop, migration re-run, incomplete-config refusal | **Verified** by drill |
| Backup, encrypted restore into a fresh cluster, isolation after restore, tamper and wrong-key refusal | **Verified** by drill |
| Web image builds and runs | **Verified** (healthy, non-root `node`, read-only root with a `.next/cache` tmpfs, security headers present, the local 124 MB installer is excluded from the image). It is **1.01 GB**; enabling Next.js `output: "standalone"` would shrink it and was not done. It was started alone: the full web + api + proxy path through the `/api` rewrite was not exercised |
| A run with `APP_ENV=production` end to end | **BLOCKED**: needs `fastembed` (about 2 GB model), a real LLM provider key, a runtime/owner role split and TLS material for Redis |
| Off-host database, TLS proxy, DNS, certificates, secret manager, scheduled backups and recovery sweep, monitoring and alerting wiring | **Not done**: owner infrastructure decisions (no cloud assumptions were made) |
| Load or capacity test, RPO/RTO, point-in-time recovery, image vulnerability scan, SBOM and signing | **Not done** |
| Production deployment | **Not performed** |

## 12. Security remediation changes (2026-11-23, D-82)

Each item names the finding in `docs/SECURITY_ASSESSMENT_PHASE_E.md`. All are AUTOMATED-TESTED; the production stack was not deployed.

* **SEC-07, `APP_ENV` default.** `APP_ENV=local` (the default) is now refused unless the database, Redis and every CORS origin are on this machine (`localhost`, `127.0.0.1`, `::1`, `host.docker.internal`). A deployment that forgets `APP_ENV` therefore fails at start-up with a message naming the variables, instead of starting with local security defaults. `LOCAL_ALLOW_REMOTE_SERVICES=true` is the explicit developer override and must never be set in a deployment. Residual: a deployment whose database and Redis are on loopback and whose CORS origin is localhost still starts as local; production compose always sets `APP_ENV`.
* **SEC-12, credentials.** See section 3. Run maintenance on demand: `docker compose --profile ops run --rm maintenance python -m app.workers.recovery --dry-run` (or `app.workers.reindex`). `MAINTENANCE_DATABASE_URL` stays in the operations environment file only.
* **SEC-16, data stores.** Postgres and Redis run as their own unprivileged users, with a read-only root file system, all capabilities dropped, `no-new-privileges`, a process limit, and on the `data` network, which is `internal` (no route to the internet). The API and worker also join `edge` (provider calls and the loopback ports); the web app is on `edge` only. Verified on real containers locally (user ids 999, `ReadonlyRootfs=true`, `CapDrop=[ALL]`, outbound connection from Postgres refused). Existing volumes created when Redis ran as root must be re-owned once (`chown -R 999:1000` on the Redis volume) before switching.
* **SEC-14, client address.** Checked on a real chain (Caddy, the web image and the API image, clients with fixed distinct addresses; `infra/deploy/proxy-check/run.sh` repeats it): the API receives `X-Forwarded-For` with exactly one entry, the real client, both when requests go through the Next.js rewrite and when `/api/*` is routed straight to the API, and one client's login failures do not throttle another in either layout. The earlier suspicion that the rewrite appends a second hop was **not reproduced**. The example still routes `/api/*` directly so the limit does not depend on that behaviour of a particular Next.js version. Re-run the check against your own images and proxy before relying on it: a different proxy or Next.js version could behave differently.
* **SEC-18, metrics.** Set `METRICS_BEARER_TOKEN` (24 characters or more) to require `Authorization: Bearer <token>` on `/metrics`; keep the proxy rule that hides `/metrics` and `/ready` from the public hosts as well. Without the token `/metrics` is open to anything that can reach the API port.
* **SEC-08, handoff binding (D-83).** The web app and the desktop app now bind every handoff code to a verifier that stays in the desktop app's memory, so production compose defaults `HANDOFF_REQUIRE_CHALLENGE=true` (unbound codes refused). Set it to `false` only to support a desktop build older than this change. The desktop's loopback beacon at `127.0.0.1:47821/ia-presence/challenge` hands the web page the challenge; see `docs/SECURITY_REVIEW.md`.
* **SEC-10 and SEC-11, backups.** `BACKUP_PASSPHRASE_FILE` is now mandatory and backups are authenticated; see section 8 and `infra/backup/`. Backups made by the previous scripts cannot be restored by the new `restore.sh`: make a fresh backup after upgrading.
