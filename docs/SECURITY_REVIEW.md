# Phase 3 Security Review

## Scope and method

Reviewed authentication and authorization, user-scoped repositories, file upload and storage,
job-page URL import, WebSocket ticket authentication, request validation, secrets/configuration,
tenant isolation, and the relevant database and worker paths. This was a source review plus the
targeted automated checks listed below; it is not a penetration test or a production configuration
audit. The developer `.env` was not inspected.

The review confirmed that API resource access is derived from the authenticated identity and
repository calls carry `user_id`. Foreign-owned resources use indistinguishable not-found behavior.
Upload handling enforces body and per-file byte limits, allow-listed types and file signatures,
stores data under private server-generated keys, and checks ownership on read/delete. It does not
include per-user storage quotas or malware scanning.

The job importer restricts schemes and ports, resolves and rejects non-public addresses, pins the
validated destination, revalidates redirects, and enforces response-size and time limits. It does
not report a confirmed SSRF bypass in this review. WebSocket authentication uses short-lived
single-use Redis tickets, validates the session binding and owner before accepting, and redacts
tickets from access logs.

## Findings and remediation

No confirmed exploitable vulnerability was identified in the reviewed paths. The review did identify
three defense-in-depth gaps; the owner approved fixing all three:

| # | Severity | Finding | Remediation |
|---|---|---|---|
| 1 | MEDIUM | Production JWT settings did not enforce sufficient key material. | Enforce at least 43 URL-safe characters and a diversity/entropy heuristic outside local mode; configuration errors do not print the value. |
| 2 | MEDIUM | Non-local Redis configuration could use plaintext `redis://`. | Require `rediss://` outside local mode. |
| 3 | LOW | Application tenant isolation relied only on repository predicates. | Add forced PostgreSQL RLS, a non-login `NOBYPASSRLS` runtime role, transaction-local tenant identity, and a separate maintenance connection for cross-tenant recovery/reindex operations. |

These are remediations in the code and migration, not proof that a deployed database role has been
provisioned correctly. Production rollout remains gated on granting the runtime login membership in
`interview_app_runtime`, using a non-superuser/non-owner application login, and verifying the
isolation test against the deployment database.

## RLS implementation

Migration `0008_tenant_rls` enables and forces RLS on the user-owned tables. Standard tenant reads
and writes require `app.user_id`, set transaction-locally from authenticated identity or the
database-backed ingestion job owner. Login and refresh-token lookups have narrowly scoped
transaction-local lookup settings. Session preferences are scoped through their owning session.
The runtime role has `NOBYPASSRLS` and is entered with `SET LOCAL ROLE` at the start of each runtime
transaction. Migration and recovery/reindex credentials must remain separate from normal request
credentials.

Run the dedicated `tests/test_rls.py` and the full user-isolation suite against PostgreSQL after any
change to this migration or database session setup. A test skipped for unavailable PostgreSQL is
not an RLS verification.

## Remaining risks and controls

- **Process-local coordination:** job-import and Q&A rate/concurrency limits, WebSocket connection
  caps, and detached WebSocket replay state are per API process. Their limits multiply across
  replicas; WebSocket resume requires routing a reconnect to the process holding the channel.
  Keep the deployment single-process/sticky until shared coordination is implemented and tested.
- **Storage exhaustion and malicious files:** there are no per-user byte quotas or malware scanning.
  Restrict account creation and monitor the persistent volume; evaluate ClamAV and quotas before
  broader exposure.
- **Operational metrics endpoint:** `/metrics` has no application authentication. Expose it only
  to the monitoring network through the reverse proxy/firewall.
- **Third-party AI:** no live-provider security or data-retention terms were verified. Review the
  selected provider account's data handling before sending interview content.
- **Account-wide deletion:** file and session deletion exist, but a complete account deletion flow
  and backup-expiry behavior are not implemented.

## Verification record

Executed:

- 216 targeted non-database tests passed across configuration, health/metrics, logging redaction,
  AI routing, SSRF protections, and WebSocket limits/configuration.
- Full backend Ruff lint passed.
- Strict mypy over application code and changed tests passed with `unused-ignore` disabled; the
  unfiltered repository mypy run still reports unrelated existing errors in `tests/ai_contract.py`,
  `tests/test_ai_router.py`, `tests/test_realtime_session_channel.py`, and
  `tests/test_realtime_resume.py`.
- Python compile check and `uv lock --check` passed.
- The pytest run emitted one upstream Starlette/httpx deprecation warning from the installed
  TestClient integration.

Not executed: migration upgrade/downgrade and the PostgreSQL-backed RLS/isolation suite. No
`DATABASE_URL` environment variable or Docker CLI was available, and the configured `.env` and
unknown databases were intentionally not read or modified. A real PostgreSQL run remains a
deployment gate; source inspection and the non-database tests do not establish RLS correctness.
