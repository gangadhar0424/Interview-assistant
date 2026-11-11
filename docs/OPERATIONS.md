# Operations Runbook

## Runtime components

- FastAPI API, PostgreSQL 16 with pgvector, Redis, and one or more Dramatiq ingestion workers.
- PostgreSQL is the source of truth for ingestion state; Redis/Dramatiq messages are recoverable.
- Resume/document files are stored under `FILE_STORAGE_ROOT`. API and workers must see the same
  persistent filesystem.
- `/health` is a liveness check; `/ready` checks PostgreSQL and Redis. `/metrics` exposes
  Prometheus text metrics and must be restricted to a trusted monitoring network.

## Configuration and secrets

Start local development from the repository root by copying `.env.example` to `.env`, replacing the
local placeholders, and following the commands in `README.md`. Never commit `.env` or put provider
credentials in frontend variables.

For staging/production:

1. Set `APP_ENV=staging` or `production`.
2. Generate `JWT_SECRET` with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
   Keep the output in a secret manager; the application rejects weak/placeholder secrets.
3. Use `REDIS_URL=rediss://...`; provision TLS and certificate validation at the Redis endpoint.
4. Set `FILE_STORAGE_ROOT` to an absolute persistent volume path shared by API and workers.
5. Set `MAINTENANCE_DATABASE_URL` to a separate operations identity used only for the recovery and
   reindex commands. Do not copy it into ordinary API/worker runtime configuration.
6. Configure `DATABASE_URL` with a non-superuser, non-owner login that can `SET ROLE
   interview_app_runtime`. Never run the API as a PostgreSQL superuser.
7. Store `OPENAI_API_KEY` (or `OLLAMA_API_KEY` for Ollama Cloud) only in the API environment when a
   real provider is explicitly enabled. Review provider data terms first.

### PostgreSQL RLS role provisioning

Migration `0008_tenant_rls` creates the `interview_app_runtime` role when the migration principal
has `CREATEROLE` (or is a superuser), grants it required table/sequence privileges, and grants that
role to the migration principal. For separate production identities, provision role membership
explicitly for the runtime login:

```sql
GRANT interview_app_runtime TO app_runtime_login;
```

The application login must be a non-superuser, must not have `BYPASSRLS`, and should not own the
protected tables. Its session setup assumes it can `SET LOCAL ROLE interview_app_runtime`.
Provision a separate maintenance identity with the privileges required for cross-tenant recovery and
reindexing; keep its DSN only in the scheduler/operator environment. Validate the role's
`rolbypassrls` value, runtime `current_user`, and two-user isolation before deployment. `FORCE ROW
LEVEL SECURITY` is defense in depth, not a substitute for correct role grants.

## Start, health, and deploy

The repository root README documents local startup and frontend/backend checks. In an environment
with deployment-grade settings:

1. Apply migrations with the migration identity: `uv run alembic upgrade head`.
2. Start the API and worker using the commands in `README.md`; ensure they share the persistent
   storage mount.
3. Restrict `/metrics` at the ingress/network layer. Scrape each API process; counters/histograms
   are process-local and reset on process restart.
4. Check `/health` and `/ready`; verify a registration/login, upload-to-ready flow, and a session
   question with the configured provider.

`/ready` deliberately reports dependency availability, not whether an LLM provider is usable.

## Ingestion and Redis runbooks

### Queue or Redis unavailable

Uploads can leave ingestion jobs pending when dispatch fails. Restore Redis, then run:

```powershell
cd services\api
uv run python -m app.workers.recovery --dry-run
uv run python -m app.workers.recovery
```

The recovery command identifies stale jobs and re-dispatches eligible jobs; exhausted jobs are
marked failed. Inspect job status through the authenticated file endpoints and check worker logs
using job IDs only. Do not replay broker messages manually unless the database state has been
checked.

### Stuck or failed job

Run the recovery dry-run first. A processing job older than
`INGESTION_PROCESSING_TIMEOUT_SECONDS` is requeued when attempts remain and marked failed when they
are exhausted. A pending job is re-dispatched after
`INGESTION_PENDING_REDISPATCH_SECONDS`. Confirm the worker uses the same database and persistent
storage mount as the API. Never attach document contents to an incident ticket or log.

### Embedding model changed

Back up the database, set the intended embedding model consistently for workers and API, then run:

```powershell
cd services\api
uv run python -m app.workers.reindex
```

Monitor pending/processing/failed jobs and disk/RAM use. Reindexing is cross-tenant maintenance and
must use the configured maintenance connection.

## Database and file backup / restore

- Back up PostgreSQL and the file-storage volume. Restore them as a coordinated set because
  database metadata points to stored files.
- Use encrypted backups and least-privilege access; define retention with the data owner.
- Redis is not the system of record. Queue messages can be reconciled from `ingestion_jobs`.
- Perform periodic restore drills into an isolated environment. Verify migration head, sample file
  reads, session ownership, and RLS role behavior before reopening traffic.
- Storage currently has no automated orphan-file reconciliation or disk quota. Alert on free space,
  inode exhaustion, and unexpected growth; restrict signup until per-user quotas are available if
  disk capacity is at risk.

## Observability and alert response

The API emits structured JSON logs with request IDs. Current Prometheus metrics include:

- `interview_http_requests_total` and `interview_http_request_duration_seconds` (bounded route,
  method, and status labels).
- `interview_llm_requests_total` and `interview_llm_request_duration_seconds` (provider and fixed
  result taxonomy; no user or content labels).
- `interview_database_errors_total`.
- `interview_ingestion_tasks_total` and `interview_ingestion_task_duration_seconds`.
- `interview_websocket_disconnects_total` (normalized close class).

Worker logs include job ID, outcome, and duration. They must not include passwords, tokens,
provider keys, prompts, resume/document text, answers, or screenshots. Metrics are currently
process-local; the API metrics endpoint does not aggregate replicas, and the worker has no scrape
endpoint. Queue depth/age and first-token latency are not exposed as Prometheus metrics yet.

- **API 5xx or database errors:** correlate by request ID, inspect DB availability/pool and recent
  migrations; do not log SQL bind values or request bodies.
- **LLM failures/latency:** inspect provider result counters and router fallback/circuit-breaker
  logs; verify provider status/configuration without printing credentials. Use the fake provider to
  separate provider outage from application failure.
- **Ingestion backlog:** inspect pending age and worker availability; use the recovery runbook.
- **WebSocket disconnects:** group by normalized close class and inspect worker/API restarts and
  routing affinity. A reconnect may not resume if routed to a different process.
- **Disk pressure:** pause or restrict uploads, preserve storage backups, and identify growth using
  metadata/size totals only.

## Security headers and scanning (Phase 13A)

The API adds baseline security headers to every response (`nosniff`, `no-referrer`, `X-Frame-Options: DENY`, a restrictive API CSP, and `no-store` on `/api/v1/auth/*`). It does **not** send `Strict-Transport-Security`: the TLS-terminating reverse proxy must add it (for example `max-age=31536000`) once HTTPS is in place (Phase 14A), and must not strip the API's other headers. The web app sets its own headers in `next.config.ts`. CI runs `.github/workflows/security.yml` on every push and pull request and weekly: CodeQL, gitleaks, `pip-audit` and `npm audit`. A failing gitleaks or production-dependency audit job blocks merging; rotate any credential it reports instead of only deleting it.

## Auth abuse protection and signup access (Phase 13A Batch 2)

- **Run uvicorn with `--no-proxy-headers` in every deployment.** uvicorn's default trusts forwarded headers from `127.0.0.1`/`::1` (`FORWARDED_ALLOW_IPS`) and silently replaces the client address with whatever `X-Forwarded-For` says; the Next.js `/api` rewrite adds no such header itself but passes a browser-supplied one through. `tests/test_auth_proxy_socket.py` proves, over a real socket, that the default lets a forged header become the peer and that the flag prevents it.
- **Trusted proxy (`TRUSTED_PROXY_ENABLED`, default false):** set it to true only when the API is reachable ONLY through a reverse proxy that appends the real client address to `X-Forwarded-For` (`TRUSTED_PROXY_HOPS` = number of such proxies, normally 1). The client is then the header entry `hops` from the right; entries further left are ignored. With it off, forwarded headers are never read and a per-client limit applies only to a globally routable direct peer (a loopback/private peer, such as a local proxy, gets no per-client limit instead of a shared bucket). The per-email login limit is always on.
- **Limits** (all settings, per API process, reset on restart): `LOGIN_FAILURES_PER_EMAIL=5`, `LOGIN_FAILURES_PER_CLIENT=20`, `LOGIN_WINDOW_SECONDS=900`, `SIGNUP_ATTEMPTS_PER_CLIENT_PER_HOUR=10`, `SIGNUP_FAILURES_GLOBAL_PER_HOUR=30`. Limiter keys are salted digests held in memory; nothing is logged.
- **Signup access:** outside `APP_ENV=local` signup is CLOSED unless `SIGNUP_INVITE_CODE` is set (at least 16 random characters, e.g. `python -c "import secrets; print(secrets.token_urlsafe(24))"`; startup fails if it is shorter). Give the code to invited users out of band; rotate it by changing the setting and restarting. The code is a secret: keep it in the secret manager, never in the repository or logs. Desktop users register on the web first; the desktop signup button shows the generic failure when the gate is closed.
- **Lockout trade-off:** someone who knows an email can keep that account's login blocked by failing five times every 15 minutes (no permanent lock). The window is short on purpose. Check logs by request ID; the limiter itself logs nothing about emails or addresses.

## Known deployment boundaries

The job-import and Q&A rate/concurrency limiters, WebSocket connection registry, and detached
WebSocket replay channels are process-local. Their configured limits are per API process, and
detached answer replay requires process affinity. Do not claim strict global limits or seamless
multi-worker resume until Redis-backed coordination/channel state is implemented and tested.
