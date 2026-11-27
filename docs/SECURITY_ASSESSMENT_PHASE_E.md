# Phase E — project-wide security assessment

Date: 2026-11-23. Scope: threat model, security review of the backend, desktop and web clients, sensitive data handling, M-15, dependencies and supply chain, plus a bounded penetration test of a local instance. Complements `docs/SECURITY_REVIEW.md` (earlier reviews) and `docs/PHASE_D_REVALIDATION_REPORT.md`.

Status labels used (and only these): IMPLEMENTED, AUTOMATED-TESTED, MANUALLY-TESTED, ENVIRONMENT-VERIFIED, SECURITY-REVIEWED, PRODUCTION-VALIDATED, BLOCKED, FAILED, NOT TESTED.

Overall: no Critical and no High finding was found. This is not a statement that the product is secure or production ready: nothing here is PRODUCTION-VALIDATED, and several areas below are NOT TESTED.

## How the review was done

- Source review of the backend (authentication, authorization, every router, SQL, uploads, SSRF, errors, configuration), the Electron main, preload and IPC code, the web client, the release and update tooling, workflows, backup and deployment files. Three independent read-only reviews ran in parallel; the findings were then re-checked against the code before being recorded. Findings marked "reported by review" were not independently reproduced.
- Tools: `npm audit` (lockfile only), `pip-audit` through `uvx` on the pinned production requirements, regex secret scan (gitleaks is not installed; git history was not scanned), `ruff`, `mypy --strict`, the existing test suites, and a purpose-written black-box probe (section "Penetration test").

## E1. Threat model

### Assets

| Asset | Where it lives | Sensitivity |
|---|---|---|
| Resumes, supporting documents, job descriptions | server disk (file storage), Postgres (extracted text, chunks, embeddings) | high (personal data) |
| Questions and answers per session | Postgres | high |
| Live audio, screenshots | memory only (desktop, API request); never stored server side | high, short-lived |
| Account credentials, refresh tokens | Postgres (Argon2id hash, SHA-256 of refresh token); desktop: DPAPI-encrypted file; web: HttpOnly cookie | critical |
| Provider API keys, JWT secret, maintenance DB credential | server environment | critical |
| Signing certificate, update feed | owner infrastructure (not yet provisioned) | critical |
| Exported session files | user-chosen path on the user's machine | high, outside our control |

### Trust boundaries and entry points

1. Internet to the reverse proxy to the web app and API (`/api/v1/*`, `/ws/sessions/{id}`, `/health`, `/ready`, `/metrics`).
2. Browser to web app (HttpOnly refresh cookie, in-memory access token).
3. Desktop renderer (untrusted UI code) to the Electron main process (IPC) to the API.
4. Custom URL scheme `interviewassistant://` to the desktop (one-time handoff code).
5. API to model providers (interview content leaves the system; OQ-4 on provider data terms is still open).
6. API to job-posting websites (job import: attacker-controlled server and content).
7. Uploaded files (attacker-controlled bytes) to validators, parsers and the embedding pipeline.
8. Update feed to desktop updater (not yet live).
9. Build and release pipeline (GitHub Actions, signing secrets).
10. Backups and restore.

### Attackers and capabilities

| Attacker | Capability | Main goals |
|---|---|---|
| Anonymous internet user | any request to public endpoints | account takeover, enumeration, denial of service |
| Registered user (invite holder) | valid account, own data | read or change another user's data, abuse quotas |
| Malicious web page or other local website | cross-origin requests from the user's browser | CSRF, token theft |
| Malicious job-posting site or file | controls fetched content and uploaded bytes | SSRF, parser exploits, prompt injection |
| Local process running as the same Windows user | read user files, set environment, register URL schemes | steal tokens, redirect the app, intercept the handoff code |
| Network attacker on the user's path | observe or alter traffic | token theft, update tampering |
| Compromised or rogue update feed or CI | publish builds | malicious update or rollback |
| Insider or leaked backup | access to backup files | read all user data |

### Security assumptions

- TLS terminates at a correctly configured proxy; the API port is not exposed directly. `TRUSTED_PROXY_HOPS` matches the real chain (not verified, no proxy was run).
- `APP_ENV=production` is set in production (SEC-07 shows this is an assumption, not enforced).
- The user's Windows account and profile are not already compromised: a same-user process is outside the protection of DPAPI and of `settings.json`.
- The owner controls write access to the update host and the signing key.

### Principal attack paths and mitigations

| Path | Mitigation in place | Residual risk |
|---|---|---|
| Read another user's session or file by id (IDOR) | every query takes `user_id`; identical 404 for foreign and unknown ids; row-level security forced on 13 tables with a non-bypass runtime role | RLS does not check that referenced rows share the tenant (SEC-20); live probe found no cross-tenant access |
| Forged or replayed tokens | HS256 only with required claims; refresh rotation and family revoke on reuse; single-use WebSocket tickets | access tokens cannot be revoked for 15 minutes (SEC-26) |
| Credential stuffing, enumeration | Argon2id, dummy hash for unknown emails, per-email and per-client throttles, identical error bodies | throttles may collapse to one bucket behind the web proxy (SEC-14); per-email lockout can be abused |
| Oversized request bodies | upload caps; JSON cap added in this phase (SEC-01) | no upload quota, synchronous PDF export (SEC-15) |
| SSRF through job import | public-address-only, ports 80 and 443, re-validation of every redirect hop, connection pinned to the validated IP, uniform error | none found |
| Malicious uploads | extension, declared type and signature must agree; server-generated storage keys; size and decompression limits | parser load in the worker for crafted DOCX (reported by review) |
| Prompt injection from uploaded or fetched text | untrusted blocks with neutralised delimiters, evidence tags | model behaviour is not fully controllable; answers remain advisory |
| Renderer compromise to privileged actions | context isolation, sandbox, no Node integration, narrow named IPC, sender URL check on every channel, deny-by-default permissions, strict CSP | see E4 |
| Local process intercepts desktop handoff | single-use 256-bit code, 60 s lifetime, rate limit | the code redeems into a full-account desktop sign-in (SEC-08) |
| Update tampering or rollback | https feed, pinned publisher, no feed override, downgrade refusal | forged manifest version with an older signed installer (SEC-09); never exercised for real |
| Leaked backup | AES-256 with PBKDF2 when a passphrase is configured | plaintext fallback (SEC-10), no authentication tag (SEC-11), deleted data persists until expiry (SEC-13) |
| Compromised CI or signing | actions pinned by SHA, least-privilege permissions, environment-scoped secrets, actor allow-list | GitHub settings not visible (NOT TESTED); secrets passed to the whole build step (SEC-17) |

## E2 to E3. Authentication, authorization and backend security

Endpoint inventory (every router under `services/api/app`): all routes except signup, login, refresh, logout, handoff redeem, `/health`, `/ready` and `/metrics` require a bearer token that is decoded with required claims and re-checked against the user's status. Each user-owned query is filtered by `user_id` in SQL and again by row-level security. There are no administrative or internal API routes and no mass-assignment path (request models forbid extra fields; probe PT-08e confirmed `user_id` and `status` are refused).

Sound, with evidence (read by two reviewers and spot-checked): Argon2id with rehash, dummy-hash timing equalisation, JWT algorithm pinning, refresh rotation and reuse detection (`tests/test_auth.py`), WebSocket tickets (`tests/test_realtime_tickets.py`), SSRF controls (`tests/test_job_import_ssrf.py`, `tests/test_job_import_fetch.py`), storage key handling (`tests/test_storage.py`), fixed-body error handlers (`tests/test_fault_injection.py`), security headers (`tests/test_security_headers.py`), CORS allow-list and refresh Origin check (probe PT-09b, PT-09c).

## E4. Electron security — SECURITY-REVIEWED (read, not run against a packaged build in this phase)

| Control | Finding | Evidence |
|---|---|---|
| Context isolation, sandbox, node integration | on, on, off; `webSecurity` on; insecure content off; `webviewTag` off | `apps/desktop/src/main/index.ts` window options |
| New windows and navigation | all new windows denied; `will-navigate` allowed only to the trusted renderer URL | `index.ts` |
| Permissions | deny by default; microphone audio only while armed or listening and only for the trusted renderer; display media only returns system audio during an armed capture; video and everything else denied | `media-permissions.ts` (pure functions, tested) |
| IPC authorization | every `handle` call runs `assertSender` (sender frame URL must be the trusted renderer); audio chunks from other senders are ignored; stream cancel is limited to the owning sender | `ipc.ts`, `ipc-security.ts` |
| CSP | `default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'none'; object-src 'none'; frame-src 'none'; form-action 'none'` | `renderer/index.html` |
| Fuses | `runAsNode` off, node options variable off, node inspect arguments off, ASAR integrity on, only load from ASAR, cookie encryption on | `package.json` `electronFuses` |
| External URLs | none opened from the renderer; the optional "open web app" button is not built | grep: no `shell.openExternal` |
| Local filesystem | only the export path chosen in a native save dialog and the settings and encrypted token files in user data | `session-files.ts`, `settings-store.ts` |

Defect fixed (**SEC-02, Low**): a packaged build read `ELECTRON_RENDERER_URL` from the environment, loaded that URL into the window with the preload bridge and treated its origin as trusted for IPC. A process that can set environment variables for the app could therefore point the window at its own page. The variable is now honoured only in an unpackaged run (`rendererDevUrl` in `ipc-security.ts`). Regression: `apps/desktop/tests/renderer-url-packaged.test.ts` (4 tests, including a source check that `index.ts` no longer reads the variable directly).

Recommendations, not changed here: set the `grantFileProtocolExtraPrivileges` fuse to false after checking that the bundled renderer still loads (it is not set, so Electron's default applies; SEC-24); pin `electron`, `electron-updater` and `electron-builder` exactly (SEC-21).

## E5. Sensitive data — SECURITY-REVIEWED

| Data | Finding |
|---|---|
| Audio, screenshots | memory only on the desktop and in the API request; no disk write found (`capture-electron.ts` uses an in-memory PNG; the API reads the stream without spooling) |
| Transcripts | not stored |
| Refresh token | DPAPI through Electron `safeStorage`; refuses to save or load when encryption is unavailable (no plaintext fallback) |
| `settings.json` | no tokens or content; holds the acknowledged user id, window state and the capture-privacy consent record |
| Logs and diagnostics | no `console.*` in the desktop or web source; backend logs carry ids and counts; copy-diagnostics is built from closed sets and run through a redaction pass; the renderer never sees the report |
| Crash reports | no crash reporter or error-tracking service is configured |
| Exports | file name carries only eight characters of the session id; the PDF escapes all user text; the desktop save path is chosen in a native dialog and the renderer never receives the content |
| Retention and deletion | session delete cascades to questions, answers and links; resume and documents are removed by their own delete; there is no account deletion; the answer cache and the Redis append-only log can keep cached answers and tickets after a session is deleted (SEC-13); backups keep deleted data until they expire (documented) |

Defect fixed (**SEC-03, Medium**): the JSON log formatter wrote the full exception message and traceback for every `logger.exception` call. Messages from the database driver or from document parsers can contain parameter values or document text, which contradicted the documented rule that only the exception class is logged. The formatter now writes frames and exception class names for the whole cause chain and never a message (`format_exception_without_messages` in `app/core/logging.py`). Regression: `tests/test_logging_exception_redaction.py` (3 tests). Not reproduced with a real driver failure; fixed from the code path.

## E6. M-15 review — SECURITY-REVIEWED for the code read; capture behaviour NOT TESTED

Read: `main/capture-privacy.ts`, the settings store consent handling, and the IPC handlers. Not repeated: the owner's capture matrix.

| Area | Finding |
|---|---|
| Separate consent, versioning | a recorded consent with the current wording version is stored; a saved "on" without a valid record for the current version loads as off (fail closed) |
| Withdrawal | `disable()` saves "off" first, then asks Windows to stop, and reports a failure to confirm |
| IPC enforcement | the renderer can only send a boolean; turning on needs the acceptable-use acknowledgement (read from the main-process settings store) and a native confirmation the renderer cannot fake; a second concurrent confirmation is refused |
| Read-back | the flag is set and read back; a mismatch counts as failure and the flag is removed |
| Window lifecycle | the saved choice is re-applied, never newly enabled, after creation, load, show and display changes |
| Status honesty | the reported status is "requested" or "failed", never "protected" |
| OS compatibility | unsupported platforms and Windows versions report "unsupported" and change nothing |
| Indicator visibility | an in-window, non-dismissible indicator is a documented requirement and has automated tests; it was not looked at on screen in this phase (NOT TESTED visually) |
| Unsupported capture paths | the app does not claim universal capture prevention. Snipping Tool, Game Bar, PrintScreen, conferencing apps, Graphics Capture recorders, remote sessions, two monitors, older Windows and screen readers remain NOT TESTED |

Residual (accepted, **SEC-23**, Info): the consent record is a record, not a credential. Any process running as the same user can edit `settings.json` and forge it; the file mode `0600` has no effect on Windows. This is outside the protection model, as it is for every desktop setting. Owner decisions in M-15 were not changed. The transparency and accessibility conditions stand: the app hides no process, tray or taskbar entry and does not evade any monitoring.

## E7. Dependencies and supply chain — SECURITY-REVIEWED, tool-limited

| Area | Result |
|---|---|
| `npm audit` desktop | production: 0; all (dev included): 6 moderate, all in the electron-builder build chain (a `minimatch` denial-of-service advisory, build-time only). npm's suggested fix is a downgrade to `electron-builder` 24.x and was **not** applied |
| `npm audit` web | production: 0; all: 7 moderate, all in the `@typescript-eslint` and `minimatch` dev chain |
| Python | `pip-audit` on the pinned production export: no known vulnerabilities. Dev dependencies were not audited |
| Lockfiles | integrity hashes present; every resolved URL is the npm registry; no git, http or file dependencies |
| Install scripts | only dev-only packages |
| Secrets scan | regex scan found only obviously fake fixtures in `tests/diagnostics.test.ts`; no `.env`, `.pfx`, `.p12`, `.pem` or `.key` files tracked or untracked. gitleaks is not installed, so git history was not scanned |
| Workflows | all actions pinned by commit SHA, read-only default permissions, release by manual dispatch only, no `pull_request_target`, version input passed through `env:` and validated |
| Release scripts | see SEC-05 (fixed) and SEC-17 |
| Limitations | no dependency was upgraded; Electron 45's upstream support status could not be checked offline; GitHub repository settings could not be inspected |

## Findings register

Severity count: Critical 0, High 0, Medium 12 (4 fixed, 8 open), Low 10 (2 fixed, 8 open), Info 4.

| ID | Sev | Component | Finding | Status |
|---|---|---|---|---|
| SEC-01 | Medium | API | no body cap on JSON endpoints: a 500 MB body to the unauthenticated login route was read into memory | **FIXED** (2 MiB cap on POST/PUT/PATCH under `/api/`); regression `tests/test_body_limit_default.py` (7); live retest PT-05 returned 413 |
| SEC-02 | Low | Desktop | packaged app trusted `ELECTRON_RENDERER_URL` | **FIXED**; `tests/renderer-url-packaged.test.ts` (4) |
| SEC-03 | Medium | API logging | exception messages written to logs | **FIXED**; `tests/test_logging_exception_redaction.py` (3) |
| SEC-04 | Low | API | `X-Request-ID` accepted unbounded and echoed (a 5000 character value came back in the header) | **FIXED** (64 character plain-text rule, else a new id); regression in the same test file; live retest PT-09a echoed 32 characters |
| SEC-05 | Medium | Release tooling | installer signer not compared with the executable's signer or the pinned publisher | **FIXED**; `tests/release-lib.test.ts` (4); the check ran live in signed mode and failed closed on the unsigned build; never run on a real signed file |
| SEC-06 | Medium | Desktop updater | audio and capture could be started while an install was in progress | **FIXED**; `tests/audio-ipc.test.ts` regression |
| SEC-07 | Medium | API config | `APP_ENV` defaults to `local`; production safeguards are off when unset | Open; make it required or default to production |
| SEC-08 | Medium | Handoff | a redeemed handoff code gives a full-account desktop sign-in; the code travels in a custom URL scheme (reported by review, not exploited) | Open; bind the code to a secret generated by the desktop, or narrow the token |
| SEC-09 | Medium | Updater | manifest version is unsigned and not tied to the installer's own version (rollback with a validly signed older installer) | Open; BLOCKED on a real signed build to test |
| SEC-10 | Medium | Backups | silent plaintext fallback when no passphrase file is set | Open |
| SEC-11 | Medium | Backups | no authentication tag; the sums file sits beside the data | Open |
| SEC-12 | Medium | Deployment | runtime containers hold the maintenance database credential; migrate holds all provider keys | Open |
| SEC-13 | Medium | Privacy | deleted data persists in the Redis answer cache and append-only log, and in backups; no account deletion; `docs/PRIVACY.md` does not list the Redis residue | Open |
| SEC-14 | Medium | Auth throttling | behind the web `/api` rewrite the API may see one shared client address (suspected; needs a running proxy) | Open, NOT TESTED |
| SEC-15 | Low | API | no upload quota; PDF export of 2000 questions runs synchronously on the event loop | Open |
| SEC-16 | Low | Deployment | Postgres and Redis containers lack read-only root, capability drop and no-new-privileges; no internal-only network | Open |
| SEC-17 | Low | Release tooling | signing secrets are exported to the whole packaging step including the type-check and bundler | Open |
| SEC-18 | Low | API | `/metrics` has no authentication in the app (returned 200 in the local probe); the proxy example blocks it | Open; confirm the proxy rule at deployment |
| SEC-19 | Low | API | Markdown export writes company, title and job URL unescaped; affects the user's own file only | Open |
| SEC-20 | Low | Database | row-level security does not check that referenced rows belong to the same tenant (defence in depth; application check is sound) | Open |
| SEC-21 | Low | Desktop | caret ranges on `electron`, `electron-updater`, `electron-builder` | Open |
| SEC-22 | Info | Auth | signup distinguishes a taken email (409) from success (201) for anyone holding the invite code | Accepted |
| SEC-23 | Info | Desktop | consent record and settings are editable by a same-user process; file mode has no effect on Windows | Accepted residual |
| SEC-24 | Low | Desktop | `grantFileProtocolExtraPrivileges` fuse not set | Open |
| SEC-25 | Info | Dependencies | dev-only advisories (13 moderate in total); no production advisories | Tracked |
| SEC-26 | Info | Auth | access tokens (15 minutes) cannot be revoked | Accepted |

Fixed items satisfy the rule to add a regression test for every defect fixed. No Critical or High finding exists to fix.

## Penetration test

### Authorized scope (defined before testing)

| Item | Definition |
|---|---|
| Target | one API instance started for this test on `127.0.0.1:8765`, `APP_ENV=local`, with a random 48-byte JWT secret generated for the run |
| Data | a throwaway database `ia_pentest` created in the dev compose Postgres and migrated to head; Redis database 14 of the dev compose Redis |
| Accounts | two synthetic users (A and B) and two more for rate-limit and refresh tests, all with `@example.com` addresses created by the script |
| Test data | synthetic PDF resume generated in memory; synthetic job text; no real documents, no provider keys, no real provider calls |
| Allowed techniques | black-box HTTP requests: missing and forged tokens, cross-user access, injection strings, malformed and oversized bodies, hostile filenames, header abuse, rate-limit exhaustion, refresh-token replay |
| Prohibited | any third-party or production system; denial-of-service beyond a few oversized bodies; destructive actions outside the throwaway database; killing any process not started by this test |
| Teardown | the API process was stopped and the database dropped after the run |

Not in scope or not performed: WebSocket attacks against a running gateway (covered by unit tests only), the packaged Electron app and IPC fuzzing against it, update-integrity attacks against a real updater, SSRF against a live fetch (covered by unit tests), a reverse proxy, TLS, production mode (`APP_ENV=production` needs TLS Redis and was not stood up), file-parser fuzzing, load testing.

### Method and result

Script: a Python probe kept in the session scratchpad (not part of the repository). Command: `uv run python -I pentest.py <jwt-secret-file>` from `services/api` against the instance above.

Final run: **35 PASS, 0 FAIL**. Checks: PT-01 every protected route returns 401 without a token; PT-02 `alg: none`, bad signature, swapped payload and garbage tokens are refused; PT-03 login for a known and an unknown email returns the same status and body; PT-04 SQL injection strings in login and path parameters change nothing and the database stays up; PT-05 a 3 MiB body to unauthenticated login is refused with 413 and a normal body is still accepted; PT-06 upload of a valid PDF works and an `.exe`, a path-traversal file name, a fake PDF and a name with a null byte are refused, an 11 MiB file is refused before it is read, and client file names are never reflected as paths; PT-07 another user's GET, DELETE, download, retry and document lookup of a resume all return a 404 identical to an unknown id, and the owner's data is intact; PT-08 the other user's attempts on 11 session routes (including export and the SSE question endpoint) return 404, creating a session with another user's resume returns 404, mass-assignment fields are rejected, export carries no email or user id, `format=json` is refused, an idempotency key is honoured and a malformed key refused; PT-09 an oversized `X-Request-ID` is replaced, a cross-origin preflight gets no allow-origin, refresh from an untrusted Origin gets 403; PT-10 malformed input returns fixed bodies without traces; PT-11 five failed logins for one email trigger 429 with `Retry-After`; PT-12 refresh rotation works, replay of the old token is refused and revokes the family.

Reproducible findings from this test: SEC-01 (a 3 MiB body was accepted before the fix; refused after) and SEC-04 (a 5000 character request id was echoed before the fix; 32 characters after). Both were seen in the earlier runs of the probe, then fixed and re-tested on a restarted instance. Informational: `/docs` and `/metrics` answered 200 on this local-mode instance (SEC-18).

Probe honesty notes: the early runs had defects in the script itself (a comparison that included the per-request id, an upload type the resume endpoint does not accept, a header with a line break that the HTTP client refuses to send, and a route that needs an `Accept` header before it checks ownership). These were corrected and the test re-run; the final result above is from the corrected script. Limits of a pass: PT-04 in the final run also saw 429 on some requests because of throttling from earlier runs, so injection coverage in that run is partial; earlier runs saw 401 and 422.

## Regression and verification of fixes

See `docs/PHASE_D_REVALIDATION_REPORT.md` for the suite results. Fix-specific tests: `tests/test_body_limit_default.py`, `tests/test_logging_exception_redaction.py` (backend); `tests/renderer-url-packaged.test.ts`, `tests/audio-ipc.test.ts`, `tests/release-lib.test.ts` (desktop).

## What remains before production readiness (priority order)

1. Owner infrastructure that blocks validation: a production host with TLS, DNS and a reverse proxy; Postgres and Redis hardened and not internet reachable; a code-signing certificate and the GitHub release environment with reviewers; an HTTPS update host with controlled write access. Then run the real checks that are BLOCKED or NOT TESTED: a signed build verified with `Get-AuthenticodeSignature`, a signed N to N+1 update on a clean machine, the backup and restore drill on the real stack, `APP_ENV=production` start-up, and a proxy-chain test for SEC-14.
2. Close the open Medium findings: SEC-07 (require `APP_ENV`), SEC-08 (bind the handoff code), SEC-09 (tie the manifest to the installer version), SEC-10 and SEC-11 (backup encryption required and authenticated), SEC-12 (split the maintenance credential), SEC-13 (document or purge the Redis residue; decide on account deletion).
3. Real provider validation and the OQ-4 data-handling terms for sending interview content to providers.
4. Manual test passes still open: M-9 web W1 to W6 and evidence for the desktop checks; the M-15 capture matrix on real Windows (Snipping Tool, Game Bar, conferencing apps, remote session, two monitors, older Windows, screen readers); a visual check of the capture indicator.
5. Low findings and hygiene: SEC-15 to SEC-21 and SEC-24, exact version pins for the Electron packages, a gitleaks scan of history, dev dependency audit for Python, a re-check of Electron 45 support status before the first release.
6. A second security pass after the above on a production-like deployment, including WebSocket and IPC testing against the packaged app.

---

# Remediation log, 2026-11-23 (security remediation sprint, D-82)

The register above is the original Phase E record and is left as written. This section records what happened to each finding afterwards. Status words: IMPLEMENTED, AUTOMATED-TESTED, ENVIRONMENT-VERIFIED, BLOCKED, NOT TESTED. A finding is closed only when its residual risk is acceptable and stated; changed code alone does not close one.

## The eight Medium findings that were open

The register listed SEC-07 to SEC-14 as open Medium. The eighth is **SEC-14** (login throttling behind the web proxy), which Phase E reported as "suspected".

| ID | Root cause | Change | Before | After | State |
|---|---|---|---|---|---|
| SEC-07 | `APP_ENV` defaults to `local`, and every production check is skipped for `local` | `local` is refused unless the database, Redis and every CORS origin are on this machine (`LOCAL_ALLOW_REMOTE_SERVICES` is the explicit developer override) | a process with no `APP_ENV` and a remote database started with `/docs` open, no `Secure` cookie, plaintext Redis, signup without a code | the same process stops at start-up with a message that names `APP_ENV` and the variables, and never prints their values | **Fixed.** AUTOMATED-TESTED (new cases in `test_config.py`). Residual: an all-loopback deployment that omits `APP_ENV` still starts as local; the compose file always sets it |
| SEC-08 | a handoff code redeems into a full-account desktop login and travels in a link any local program could capture | optional PKCE-style binding on the server (`code_challenge` at issue, `code_verifier` at redeem), mandatory when `HANDOFF_REQUIRE_CHALLENGE=true` | a captured code was enough | a captured code is useless without a verifier that never leaves the desktop (12 new tests: wrong verifier, missing verifier, burned code, malformed values, unbound code refused once binding is required, store contents) | **Partially mitigated, still Open.** The web and desktop do not send a challenge yet and the default is off, so today's behaviour is unchanged. Recommended architecture and trade-offs: `docs/SECURITY_REVIEW.md`, "SEC-08 handoff" |
| SEC-09 | `latest.yml` is unauthenticated text, so a feed writer could announce a new version for an old, validly signed installer | Ed25519 signature over the manifest, public key pinned inside the app, verification before any field is read, and the downloaded installer's SHA-512 compared with the signed manifest | forged version plus matching hash passed every check | tampered, unsigned, wrongly signed, swapped, replayed and rollback inputs are each refused (19 tests; mutation check: 8 and 3 tests fail when either check is disabled) | **Fixed in code. Real validation BLOCKED** (needs the owner's key pair, a signed build and an https feed). Not closed until that validation is done |
| SEC-10 | backup silently wrote plaintext when no passphrase file was set | the plaintext path was removed; a valid passphrase file is mandatory | `encrypted=no` backups of resumes, documents and Q&A | refused with exit 2 and nothing written | **Fixed.** AUTOMATED-TESTED (`tests/test_backup_scripts.py`) |
| SEC-11 | AES-CBC with no authentication; checksums beside the data | format `ia-backup-v2`: AES-256-CTR with PBKDF2 (600000 iterations), plus an HMAC over a manifest that lists every file with its size and SHA-256; restore verifies the whole set before decrypting anything | swapped or altered files and sums went unnoticed | wrong passphrase, flipped bits, truncation, deleted, extra and swapped files, and old-format backups all fail closed before any database or uploads write | **Fixed.** AUTOMATED-TESTED; one round trip and one tamper case against a real PostgreSQL 16 container were run by the delegated run (reported, not repeated by me). Residuals below |
| SEC-12 | the privileged maintenance credential was in every runtime container, and `migrate` held the JWT secret and all provider keys | not required at start-up; only the opt-in `maintenance` service (profile `ops`) holds it; `migrate` gets only `APP_ENV` and the migration URL (Alembic reads `DATABASE_URL` directly) | credential present in api, worker, migrate | present only in `maintenance` | **Fixed.** AUTOMATED-TESTED, including against the real database: a least-privilege login that may only `SET ROLE interview_app_runtime` is refused DDL, `TRUNCATE`, `pg_authid`, `CREATE ROLE`, `COPY ... PROGRAM`, `ALTER SYSTEM`, role changes and `SET ROLE` to the maintenance or any owner role, sees no rows without a tenant, and cannot turn row security off, while the separate `BYPASSRLS` maintenance login sees all rows (which is why the two must not be mixed). The production stack itself was not started |
| SEC-13 | retention and deletion were undocumented and incomplete | `docs/DATA_LIFECYCLE.md`: current state, gaps, proposed policy, decisions needed | no single description | written | **Open, by design.** Needs owner decisions (account deletion, retention, backup period, AOF). No irreversible change was made |
| SEC-14 | the web path through the Next.js rewrite appends a second `X-Forwarded-For` entry, so `TRUSTED_PROXY_HOPS=1` would key every web user to the proxy | the proxy example routes `/api/*` straight to the API so every request has one hop | two entries, hop 1 returns the proxy | one entry, hop 1 returns the client; semantics pinned by tests, including the failure mode | **Mitigated in configuration. Real proxy NOT TESTED**, so the finding stays Open until a deployed proxy is checked with two clients |

## Low and informational findings

| ID | State | Detail |
|---|---|---|
| SEC-15 | **Partly fixed** | PDF rendering now runs off the event loop and one at a time. **Still open:** no per-user upload quota (needs product limits; not invented here) |
| SEC-16 | **Fixed, ENVIRONMENT-VERIFIED locally** | Postgres and Redis run unprivileged, read-only, capability-free, `no-new-privileges`, on an internal network. Checked on real containers from the production compose file (ids 999, `ReadonlyRootfs=true`, `CapDrop=[ALL]`, outbound connection refused, both healthy). Production host NOT TESTED |
| SEC-17 | **Fixed**, AUTOMATED-TESTED | signing credentials removed from the build and verify steps (`scrubbedEnv`); a real GitHub run NOT TESTED |
| SEC-18 | **Mitigated** | optional bearer token on `/metrics`; off by default, so it must be set and the proxy rule kept |
| SEC-19 | **Fixed**, AUTOMATED-TESTED | header fields are one line with Markdown and HTML punctuation escaped; the job link is a literal code span |
| SEC-20 | **Open, deferred** | composite foreign keys need a schema migration; owner approval of a schema change required (defence in depth; the application check is sound and live probes found no cross-tenant access) |
| SEC-21 | **Fixed** | `electron`, `electron-updater`, `electron-builder` and `@electron/fuses` are pinned to the versions already locked (45.2.3, 6.8.11, 26.18.0, 1.8.0); no version changed; `npm ls` confirms |
| SEC-24 | **Open, not changed** | the `grantFileProtocolExtraPrivileges` fuse is not set. The renderer is a module bundle loaded from `file://`, and turning the fuse off can stop such a load. It was **not** changed because it cannot be verified without running the packaged app, which was not done. NOT TESTED. Recommended: try it on a packaged build during the M-15 manual session |
| SEC-22, SEC-23, SEC-26 | Accepted | unchanged |
| SEC-25 | **Updated** | Python dev dependencies were audited: one advisory, `pytest 8.4.2` (PYSEC-2026-1845, fixed in 9.0.3); it is a test tool, not in the production image, and a major upgrade was **not** applied without a compatibility review. npm: no production advisories (unchanged). gitleaks ran through its Docker image (`ghcr.io/gitleaks/gitleaks:latest`, 46 commits): one finding, a fake JWT fixture in `diagnostics.test.ts` whose signature decodes to the word "signature"; it was allow-listed by exact fingerprint with an explanation. The working tree, scanned without git history, shows only the same kinds of fixtures and placeholders (`JWT_SECRET` patterns, a sentinel key, a comment in `.env.example`); after the next commit, scan again and add fingerprints for the new commit |

New items recorded while fixing:

| ID | Sev | Finding | State |
|---|---|---|---|
| SEC-27 | Info | `pytest 8.4.2` development advisory (see SEC-25) | Tracked |
| SEC-28 | Low | the app does not delete an installer that failed verification against the signed manifest (a guard test limits the app's file-system use to a short allow-list, and widening it was judged worse than leaving the file). It cannot be installed, because installing requires the verified state | Accepted residual |

## Residual weaknesses of the backup design (reported by the delegated run, accepted for now)

The authentication proves the files belong together and were made with the passphrase; it does not prove freshness, so an attacker with write access can delete a whole backup or put back an older complete one. The manifest (sizes, row counts, schema version) is readable. CTR mode is only safe because restore checks the MAC first; code that calls the decrypt helper directly would skip that. `prune.sh` has no passphrase, so it checks structure only. The row-count comparison in restore runs after the data is written (a sanity check, not a gate). The passphrase staying out of process arguments rests on design and review, not on a captured process listing. The `UPLOADS_VOLUME` (busybox) path and `RESTORE_ROLES=1` were not exercised. Only Windows (Git Bash) and Debian (inside the Postgres container) were used.

## Penetration probes after the fixes

Re-run on 2026-11-23 against a fresh throwaway instance (same authorised scope as Phase E: loopback only, throwaway database `ia_pentest`, synthetic accounts, torn down afterwards; the instance started with a metrics token, and the database was migrated with **only** `DATABASE_URL` and `APP_ENV` in the environment, which exercised the minimal migrate-job design for real).

Result: **44 PASS, 0 FAIL.** This is the 35 original Phase E checks unchanged (so the earlier fixes and the new ones were regression-tested together) plus nine new live checks: PT-14a to PT-14f (handoff binding: a bound code redeemed without the verifier is refused; a wrong verifier is refused; the burned code cannot be retried with the right verifier; the right verifier works; the unbound legacy flow still works while binding is optional; a malformed challenge is rejected) and PT-15a to PT-15c (`/metrics` refused without or with a wrong token, served with the right one). Also checked live: a process started with no `APP_ENV` against a remote database refuses to start with a message that never contains the database password (SEC-07).

Not covered by the probe, and tested only at unit or integration level: the updater, release tooling and backup attacks (run against the real code with fake feeds and stub database commands), database privilege separation (15 tests as a least-privilege login on the real test database, plus 12 on the compose wiring), the proxy topology (no proxy was run), WebSocket attacks, the packaged Electron app and IPC.


## Verification results for the sprint (2026-11-23)

| Check | Result |
|---|---|
| Desktop `npx tsc --noEmit` | passed |
| Desktop `npx vitest run` | 56 files, **1180 passed, 0 failed** (1143 at the start of the sprint) |
| Desktop production build (`npm run build`) | exit 0 |
| Desktop `electron-builder --dir` with the manifest key wiring (temp output, not `release/`) | built; `resources/update-manifest-key.txt` contains exactly the public key that was configured; `app-update.yml` is written by the installer target and was not part of this build |
| Web `npx tsc --noEmit`, `npm test`, `npx eslint .`, `npm run build` | passed; 125 of 125 tests; build exit 0 (web code was not changed) |
| Backend `ruff check .` | passed |
| Backend `mypy` (`app`, 148 files) | passed |
| Backend `pytest` (clean full run, nothing else using the test database) | **2421 passed, 5 skipped, 2 deselected, 2 failed** in 35 minutes (2324 passed at the start of the sprint; 100 tests added). The 5 skips are the 4 existing ones plus the loose-file-permission backup test, which cannot apply on Windows |
| The two failures | see below |
| Phase E penetration probe, re-run with nine new checks | 44 PASS, 0 FAIL |
| `gitleaks` over git history (Docker image, 46 commits) | no leaks, after allow-listing one verified fake JWT by exact fingerprint |
| `npm audit`, `pip-audit` | unchanged for npm (no production advisories); Python production: none; Python development: `pytest 8.4.2` (PYSEC-2026-1845), not upgraded |

**Failure 1, `test_phase3_closure.py::test_local_default_root_is_usable_and_blank_value_means_default`: caused by SEC-07, fixed.** The test's settings helper combined `APP_ENV=local` with a remote Redis URL, which the new rule correctly refuses. The helper now uses a loopback Redis for local cases and the remote TLS URL for deployed ones; the test still checks the same storage-root rules. The file then passed (33 tests). Security assertions were not weakened: the SEC-07 tests that the rule refuses a remote service in local mode are unchanged.

**Failure 2, `test_realtime_qa_live.py::test_one_slow_client_does_not_stall_another_user`: not caused by this sprint; still failing; not hidden.** It is a heavy test (a 25 MB flood to a stalled client, then a second user must still get a ticket within 10 seconds). It failed in the full run and failed 3 of 3 times when run alone, with a `ReadTimeout` on the second user's ticket request; the sibling flood test in the same file passed but took 16 seconds. To find out whether this sprint caused it, the same test was run against an untouched copy of the last commit (`git archive HEAD`, same virtual environment and database, verified to be importing the old code): it **failed the same way**. It passed in the previous sprint's full run, so it is sensitive to host load (the machine was running a browser, an editor and Docker Desktop at about 70 percent CPU while idle). Classification: pre-existing, environment-dependent timing test; real status on a quiet machine NOT TESTED in this sprint. Recommended: re-run it on an otherwise idle machine or CI before trusting or changing it. It was not modified and not disabled.


---

# Remediation log, sprint 3 (2026-11-23, D-83): closure work, real retests, stability

Earlier sections are left as written. Status words: IMPLEMENTED, AUTOMATED-TESTED, MANUALLY-TESTED, ENVIRONMENT-VERIFIED, BLOCKED, NOT TESTED. A finding is not closed because code changed.

## Finding status after sprint 3

| ID | Before | After | Evidence and remaining gap |
|---|---|---|---|
| SEC-08 handoff | Partially mitigated (server only) | **Fixed in code.** AUTOMATED-TESTED in desktop, web and API; ENVIRONMENT-VERIFIED end to end with the real pieces. **Open for one item:** a person has not run it through a real browser, the `interviewassistant://` protocol handler and the installed app (NOT TESTED) | Desktop: `handoff-challenge.ts` (single use, 120 s, bounded, rate limited, memory only), the beacon endpoint, the strict link parser, the controller. Web: challenge fetch, bound code request, bound link, fallback to the unbound flow. API: production compose and `production.env.example` now default `HANDOFF_REQUIRE_CHALLENGE=true`. Live check `handoff-e2e.mjs`: 11 of 11, including the intercepted link replayed without a verifier (404), a guessed verifier (404), the burned code (404 even with the right verifier), replay of the pair, an unbound request while binding is required (422), a DNS-rebinding Host header (404) and the private-network preflight (204). Residual: any web page can ask the beacon for a challenge (rate limited and bounded: an interruption, never a way in) |
| SEC-13 retention and deletion | Open | **Open, decision document ready** (`docs/OWNER_DECISIONS_SEC13_SEC20.md`, Part A) | Nothing irreversible implemented. New related facts in SEC-29, SEC-30 and the note below |
| SEC-14 login throttling behind the proxy | Mitigated in configuration, real proxy NOT TESTED | **Not reproduced.** ENVIRONMENT-VERIFIED on one real stack | With real Caddy, the web image and the API image and clients with distinct fixed addresses, the API receives a single-entry `X-Forwarded-For` (the real client) in both layouts, and one client's failures do not throttle another. The Next.js rewrite does not append a hop in this build. My first attempt was invalid (clients reused one address) and wrongly suggested the new layout failed. Re-run `infra/deploy/proxy-check/run.sh` against the real images and proxy before relying on it |
| SEC-20 composite foreign keys | Open, deferred | **Open, decision document ready** (Part B of the same document) | 7 composite foreign keys and 5 new unique constraints; three polymorphic `source_id` columns cannot be foreign keys; `session_preferences` is covered by row-level security already. The phased SQL was executed on throwaway databases (a cross-tenant insert succeeded before and was refused after); the Alembic files, timings and effort are proposed only. Nothing runs without approval |
| SEC-24 file-protocol fuse | Open, not changed | **Resolved by evidence, as an accepted residual** | Packaged build with `GrantFileProtocolExtraPrivileges` disabled does not render (the DevTools connection is dropped, no UI); the same build with it enabled renders and passes 19 of 19 live checks. The fuse must stay enabled while the renderer loads from `file://`. Improvement path (not approved or built): a custom `app://` protocol, then disable the fuse |
| SEC-09 update rollback | Fixed in code, real validation BLOCKED | unchanged, **BLOCKED**; more evidence | New tests use real files and the real hash function through the real update manager: a genuine installer is accepted; a one-byte change is refused; an older genuine installer served under the newer signed version is refused; a missing file is refused. The real updater against a real signed feed is still not run |
| SEC-10, SEC-11 backups | Fixed, AUTOMATED-TESTED | **ENVIRONMENT-VERIFIED** against a real PostgreSQL by me | `infra/backup/drill-tamper.sh`: 25 of 25. A genuine backup restored with every row count and a byte-identical upload; thirteen kinds of damage (flipped bits in each file and in the MAC, edited manifest, truncation, swapped files, deleted files, an extra file, an old-format backup, a plaintext dump) and a wrong passphrase were each refused with exit 4 before any database or uploads write; a non-empty database is still refused (exit 3); backup refuses to run with no or a short passphrase and writes nothing |
| SEC-07, SEC-12, SEC-16, SEC-17, SEC-18, SEC-19, SEC-21 | Fixed | unchanged | Covered again by the full suites below |
| SEC-15, SEC-20, SEC-22, SEC-23, SEC-25, SEC-26 | as before | unchanged | Upload quota (SEC-15) still needs product limits |

## New findings from this sprint

| ID | Sev | Finding | State |
|---|---|---|---|
| SEC-29 | Low | Redis also writes RDB snapshots (Redis's default), so turning off the append-only file alone would not stop cached answer text reaching disk (reported by the decision-document review; consistent with Redis defaults; not separately verified) | Part of the SEC-13 decision |
| SEC-30 | Low | `prune.sh` always keeps the newest three backups whatever their age, so the real retention can exceed the stated one (reported by the same review) | Part of the SEC-13 decision |
| SEC-31 | Info | `docs/DATABASE.md` describes an `audit_events` table that no migration creates (reported) | Documentation fix pending |
| SEC-32 | Low | The desktop beacon answers any web page's request for a challenge (`Access-Control-Allow-Origin: *`), rate limited at 30 a minute and bounded at 16: it can interrupt a legitimate attempt, not hijack one | Accepted residual |
| SEC-33 | Low | Finishing an answer ran the citation-integrity check, and its `clean_text` per-character pure-Python loop (about a microsecond per character), on the event loop, so a very long answer delayed every other user (about 14 s for 25 MiB on a quiet machine, 25 s under load; real answers are capped by the provider token limit, so at most about 0.1 s) | **FIXED**: an ASCII fast path in `clean_text` (proven equivalent to the original by 19 tests against a verbatim copy) and the check runs in a worker thread. `tests/test_qa_event_loop.py` fails without the offload and passes with it; `tests/test_sanitize_fast_path.py` pins the equivalence. See the account below, including the intermediate mistake |

Dependency recorded for SEC-13: turning Redis persistence off first needs the ingestion recovery sweep scheduled, because queued ingestion messages are lost on a Redis restart (reported by the decision-document review; the sweep is not scheduled in compose today).

## Realtime slow-client test: root cause and resolution

* **Symptom.** `test_one_slow_client_does_not_stall_another_user` failed 3 of 3 times on a loaded machine (a second user's ticket request timed out after 10 s), also against an untouched copy of the last commit. On a quieter machine it passed (17 s), which I confirmed, so it passed only by margin.
* **Root cause (measured by a delegated investigation with a loop-lag sampler and stack sampling of the server thread).** The test's flood is 400 x 64 KiB = 25 MiB, about 200 times the largest answer a provider can legally produce. Pushing it through the provider, router and JSON encoding took 0.125 s on the loop, so the gateway's send path and backpressure were not the problem. What blocked the event loop was the work after the answer finished: `check_citations` and `clean_text`, a per-character pure-Python loop (about a microsecond per character; 14 to 16 s for 25 MiB on this machine, 25 s under load), 69 of 71 stack samples. Whether a second user's 10 s request timed out therefore depended on machine speed and load.
* **Is it an application defect?** In structure, yes (SEC-33, Low): bounded in practice by the provider token cap (0.03 s at 131 KB, 0.37 s at 1 MB), but one user's long answer should never delay everyone.
* **What I tried, and a mistake worth recording.** First fix: run the citation check in a worker thread. A deterministic regression test proved the check now leaves the event loop (it fails without the change). But the offload alone made the sibling test `test_a_client_that_stops_reading_...` fail 2 of 2 when run alone (it passed 2 of 2 without the offload): the scan, now sharing the interpreter lock with the busy loop, finished later than that test's 10 s windows. In the full run that test failed, left a worker thread still scanning, and a later test (`test_load_smoke_..._reconnect_storm`) then failed with an idle-timeout close (4002) because the loop was starved. That full run was therefore **not** green and I did not treat it as such.
* **The fix that removes the cause.** `clean_text` now has an ASCII fast path (`str.translate`, in C) that is proven equivalent to the original loop: `tests/test_sanitize_fast_path.py` keeps a verbatim copy of the original rules and compares them on every ASCII character, 4000 seeded random strings dense in control characters and line endings, and 13 non-ASCII cases (bidi overrides, zero-width and tag characters, Hindi and Telugu joiners, separators, private use, a lone surrogate); a monkeypatch test shows ASCII text never reaches the character loop and non-ASCII text still does. The worker-thread offload stays as the structural fix for the non-ASCII case. Measured effect, same machine: the slow-consumer test 12 to 19 s down to 5.8 s; the original slow-client test 4.5 s; the load smoke test passes.
* **The test.** The investigation rewrote the slow-client test with a smaller flood and a direct loop-lag measurement. Once the cause was fixed the **original test passed quickly and unchanged**, so I restored it byte for byte from git (verified) and kept the new loop-lag test next to it as `test_one_slow_client_does_not_delay_the_event_loop`. Nothing was weakened and no timeout was raised. The new test no longer asserts the 4010 close for the stalled user (loopback buffers on Windows swallow a few MB, so a realistic volume cannot reliably wedge the socket); that close is asserted by the original sibling test and, for a write stuck in the kernel, deterministically by `test_realtime_channel.py`. Its 2 s loop-lag bound is a generous upper limit (the loop lagged a few milliseconds in the investigation), not a tight measurement.
* **Not changed.** Non-ASCII text (for example a very long Hindi or Telugu answer) still goes through the per-character loop, now off the event loop; it is bounded by the same token cap.

## Retest evidence (all authorized, local, throwaway)

| Area | Harness and result |
|---|---|
| Real reverse proxy (SEC-14) | `infra/deploy/proxy-check/run.sh` run end to end: both layouts give the API a single-entry `X-Forwarded-For`; one client throttled at 20 failures, two others unaffected |
| WebSocket attacks | `ws_attack_probe.py`: **28 PASS, 0 FAIL** (handshake without, with unknown, replayed, expired, mismatched and another user's ticket; hostile Origin; oversized, binary, malformed and flooded frames; wrong version, no hello; per-user cap and supersede; ticket rate limit; no ticket in logs; the API stays healthy and a legitimate client still works) |
| Packaged Electron app | `packaged-app-probe.mjs` on a real build: **19 PASS, 0 FAIL** (fuses in the exe; no Node or Electron globals; the bridge exposes only named functions; navigation and `window.open` blocked; external and local-file fetches, external images and inline scripts blocked by the CSP; camera, unarmed microphone and screen capture denied; capture privacy off by default; IPC input validation; the renderer cannot enable capture privacy). Dynamic `eval()` could not be tested through DevTools (it is exempt), so the policy text is verified instead |
| IPC authorization | the trusted renderer was probed live; sender validation is covered by unit tests; an **untrusted sender** (a second frame or window) was not created in a running app: NOT TESTED |
| Update tampering and downgrade | real manager, real signature checks, real file hashing, fake engine and feed (above); real updater BLOCKED |
| Backup tampering and restore rejection | `drill-tamper.sh`: **25 PASS, 0 FAIL** on a real PostgreSQL |
| Handoff binding end to end | `handoff-e2e.mjs`: **11 PASS, 0 FAIL** |
| Earlier API probe | the Phase E probe passed 44 of 44 earlier in this sprint series; it was not re-run after the handoff changes except through the new handoff checks and the backend tests |

## Verification results (final)

| Check | Result |
|---|---|
| Desktop `tsc`; `vitest` | passed; **57 files, 1225 passed, 0 failed** |
| Desktop production build | exit 0 |
| Web `tsc`; `npm test`; `eslint`; `npm run build` | passed; **133 of 133**; clean; build exit 0 |
| Backend `ruff check .`, `mypy` (148 files) | passed (ruff after scoping a lint exemption to the dev-only probe scripts, below) |
| Backend `pytest`, complete run with every fix in | **2450 passed, 5 skipped, 2 deselected, 0 failed** in 48 minutes (the previous sprint's run was 2421 passed with 2 failures; this is the first complete green run). The 5 skips are the 4 earlier ones plus the file-permission backup test, which cannot apply on Windows |

Honest limits: nothing here is PRODUCTION-VALIDATED; the real updater, a signed build, a hosted proxy, the owner's M-15 capture session, a person running the web-to-app handoff, and an untrusted IPC sender remain NOT TESTED or BLOCKED.

Lint note: `ruff check .` flagged the five probe scripts saved under `services/api/scripts/security/` (asserts, `docker` through `subprocess`, long output lines). `pyproject.toml` now exempts only `scripts/security/**` from S101, S603, S607, E501 and E402, with the reason recorded there; no rule was relaxed for `app/` or `tests/`.


---

# Post-M-15 closure (2026-11-24, D-84)

Earlier sections are left as written. Status words as before. A finding is closed only when its residual risk is acceptable and stated; changed code alone does not close one. Test counts below are from runs on 2026-11-24.

## Finding status

| ID | Before | After | Evidence and remaining gap |
|---|---|---|---|
| **SEC-15** upload quota | Partly fixed | **Fixed in code. AUTOMATED-TESTED; mutation-checked.** Defaults are engineering assumptions awaiting owner confirmation | Per-user limits: 20 resumes, 100 documents, 500 MB in total (resumes and documents together), plus the existing per-file limits. Enforced server-side twice: a cheap check before the body is read, and the authoritative check under a per-user PostgreSQL advisory lock just before the row is inserted. Over the limit: `409 file_quota_exceeded` or `storage_quota_exceeded`; the stored file is removed again. 14 tests in `tests/test_upload_quota.py`: exact boundaries (count and bytes), per kind, freed by delete, per user, unauthenticated, failed commit leaves no orphan, **8 concurrent uploads yield exactly 2 successes at a limit of 2** (and 6 concurrent uploads of 400 KB yield exactly 2 against 1 MB). The concurrency tests were mutation-checked: with the lock removed they fail (8 and 6 successes instead of 2). Not covered: no total cap for the whole volume (operational: see `docs/OPERATIONS.md`); a crash between the file move and the insert can still leave an unreferenced file (pre-existing, documented in `files/service.py`) |
| **SEC-29** Redis RDB | Open (part of SEC-13) | **Confirmed by reproduction; not changed (policy decision)** | `redis:7-alpine` started with the production flag `--appendonly yes` returned `save = 3600 1 300 100 60 10000`; with `--save ""` it returned empty. The production compose file sets no `--save`, so cached answer text can reach disk through snapshots as well as the append-only file. The fix is a policy choice (`docs/OWNER_RECOMMENDATIONS_FINAL_SEC13_SEC20.md`, Decision 2) |
| **SEC-30** `prune.sh` floor | Open (part of SEC-13) | **Confirmed by test; made visible, behaviour unchanged** | `test_prune_default_floor_keeps_old_backups_and_says_so` shows years-old backups surviving `RETENTION_DAYS=1`. `prune.sh` now prints a NOTE for each backup kept past its cutoff and its header states the consequence; `MIN_KEEP=0` gives strict retention (also tested). The retention policy itself is the owner's decision. Residual: the default still keeps the newest three whatever their age |
| **SEC-31** `audit_events` | Documentation fix pending | **Fixed (documentation)** | Confirmed no migration creates the table and no code writes it. `docs/DATABASE.md` now says it is planned, not implemented, and that no persistent audit trail exists |
| **SEC-32** beacon CORS | Accepted residual | **Accepted residual, now pinned by a test** | `handoff-challenge.test.ts` shows a flooding page can evict a legitimate pending challenge (an interruption), cannot obtain a verifier (only hashes leave the app), and the legitimate page recovers by retrying after the rate window. Restricting the origin would need a configured web origin that the desktop does not have; not changed |
| **SEC-08** human handoff | Fixed in code; one manual item open | **Still open: a person has not run it.** Runbook written | `docs/SEC08_HANDOFF_MANUAL_RUNBOOK.md`: 12 rows (running, closed, expired, replay, invalid challenge, cancel and retry, flooding, sensitive values), results all NOT TESTED. See SEC-34 |
| SEC-13, SEC-20 | Decision documents ready | **Final recommendations prepared for owner approval; nothing implemented** | `docs/OWNER_RECOMMENDATIONS_FINAL_SEC13_SEC20.md` |

## New findings in this phase

| ID | Sev | Finding | State |
|---|---|---|---|
| **SEC-34** | Low to Medium (availability, usability) | **Suspected from reading the code, not run.** With `HANDOFF_REQUIRE_CHALLENGE=true` (the production default), a closed desktop app cannot be started from the web: "open it anyway" skips the presence check, no challenge can be fetched from an app that is not running, the unbound request is refused (`422`), and the page shows "Please update the desktop app", which is the wrong advice. Each step is individually tested (the page logic, the `422`), but the chain was not run end to end | **Open.** Confirm in runbook row H3b. Options for the owner: (a) change the page message to say to start the app first (smallest, web only); (b) allow a challenge-less cold launch that makes the app start first and then complete the handoff through a second, app-initiated step (a design change, needs approval). Not changed |
| **SEC-35** | **Medium** (availability) | Every authenticated request pinned a database connection for its whole duration; 25 stalled uploads from one account (above the pool of 10 + 10) made all other requests fail with `503` after 10 s. Any signed-in user could do it; production signup is invite-gated, and a reverse proxy with body deadlines would reduce it | **FIXED.** AUTOMATED-TESTED (two regression tests, one mutation-checked) and ENVIRONMENT-VERIFIED live (60 stalled uploads: other users unaffected). Residual: the fix costs an extra database round trip per request (about 25 to 35 percent of throughput on the cheapest endpoint, see `docs/PHASE_F_PERFORMANCE_RELIABILITY.md` section 2); raw-body routes (`transcribe`, `screenshots`) now also release the connection after authentication, but a live stalled-body test was run only for uploads |
| **SEC-36** | Low | Concurrent first uploads by one user failed with `InvalidStorageKey` (HTTP 500) on Windows because `Path.resolve()` raced with directory creation (25 of 40 standalone trials failed) | **FIXED** with a lock in the storage object; 0 of 40 afterwards; regression test added. Linux production behaviour was not tested; the lock is harmless there |

## Verification results (2026-11-24)

| Check | Result |
|---|---|
| Desktop `npx vitest run` | 57 files, **1226 passed, 0 failed** (1225 before, plus the SEC-32 test) |
| Backend `ruff check .`, `mypy` (148 files) | passed |
| Backend targeted suites (quota, files, storage, config, phase 3 closure, backup scripts, ingestion) | passed: 169 + 2 skipped; 32 passed + 1 skipped; 57 passed |
| Backend full `pytest` (after all code changes) | **2464 passed, 5 skipped, 2 deselected, 1 failed** in 25 minutes. The one failure, `test_realtime_resume_live.py::test_load_smoke_concurrent_connections_with_a_reconnect_storm`, was **caused by the new SEC-15 quota** (the test makes one user upload 50 resumes as fixtures; it failed the same way when run alone, so it was not load noise). The test now sets `USER_MAX_RESUMES=1000` for its own server only; no quota test was changed. That file then passed (3 of 3). **The full suite was not re-run end to end after that test-only change.** |
| Web | not changed; not rerun |
| Phase F probes | `docs/PHASE_F_PERFORMANCE_RELIABILITY.md` |

Not done in this phase: a gitleaks scan of the new working tree (nothing was committed; scan after the next commit), a container image scan, any production deployment, the manual SEC-08 run, and the owner decisions on SEC-13 and SEC-20.


---

# Phase E closure audit (2026-11-26)

Earlier sections are left as written. This audit compares the documented status with the code and with tests re-run on 2026-11-26. Status words as before. **Phase E is not closed:** the items marked "blocks closure" below need the owner.

## Evidence re-run for this audit

| Check | Result |
|---|---|
| Backend: `test_upload_quota`, `test_backup_scripts`, `test_body_limit_default`, `test_logging_exception_redaction`, `test_export_hardening`, `test_storage` | 82 passed, 3 skipped (Windows-only skips as documented) |
| Backend: `test_realtime_resume_live` (the file changed after the last full run in D-84), `test_qa_event_loop`, `test_sanitize_fast_path` | 24 passed |
| Backend: new `test_raw_body_connection_release` plus `test_audio_api`, `test_screenshot_api` | 69 passed; ruff, format and `mypy --strict` clean on the new file |
| Desktop: `handoff-challenge`, `handoff`, `capture-privacy`, `capture-guards` | 138 passed |
| Web: `npm test` | 133 of 133 |
| Full backend suite | **not re-run** (milestone-boundary run; Phase E is not at its boundary yet) |

## Per-item status

| Item | Implementation (verified in code) | Existing tests | Missing tests | Risk | Next action | Blocks Phase E closure |
|---|---|---|---|---|---|---|
| **SEC-15** upload quota | IMPLEMENTED: `user_max_resumes/documents/storage_mb` in `app/core/config.py`; pre-check plus authoritative check under `pg_advisory_xact_lock` (`app/files/repository.py`); `409 file_quota_exceeded` / `storage_quota_exceeded` (`app/files/service.py`) | 14 in `test_upload_quota.py` (boundaries, per kind, per user, delete frees, concurrency, mutation-checked) | none for the quota itself; no total volume cap (operational) | Low | Owner confirms or changes the defaults (20 / 100 / 500 MB) | No (defaults are configuration; confirm before real users) |
| **SEC-29** Redis RDB snapshots | Unchanged by design: `docker-compose.prod.yml` runs `--appendonly yes` with no `--save`, so RDB defaults apply (reproduced in D-84) | none (configuration) | after the decision: a compose test that pins the chosen `--appendonly`/`--save` flags | Low (cached answer text can reach disk) | Decided together with SEC-13 (Decision 2) | Only through SEC-13 |
| **SEC-30** `prune.sh` floor | Visible, behaviour unchanged: NOTE printed; `MIN_KEEP=0` gives strict retention | `test_prune_default_floor_keeps_old_backups_and_says_so` and the `MIN_KEEP=0` case | none until the retention decision | Low (stated retention can be untrue) | Decided with SEC-13 (Decision 3) | Only through SEC-13 |
| **SEC-31** `audit_events` | Fixed in documentation: `docs/DATABASE.md` marks it planned, not implemented | n/a | n/a | Info | none | No |
| **SEC-32** beacon CORS | Accepted residual | `handoff-challenge.test.ts` SEC-32 case | none | Low (interruption, not takeover) | none | No |
| **SEC-35** residual (raw-body routes) | By construction: `transcribe` and `screenshots` check the session in their own short transaction, and authentication ends its transaction, before the body is read | **New:** `test_raw_body_connection_release.py` (2), pool count is 0 at body read; mutation-checked (both fail with 1 connection pinned when rollback is disabled) | a live stalled-body probe for these two routes (only uploads were stalled live) | Low | optional live probe in Phase F | No |
| **SEC-08** browser to installed desktop | IMPLEMENTED in API, web and desktop; production default `HANDOFF_REQUIRE_CHALLENGE=true` | automated in all three components; `handoff-e2e.mjs` 11 of 11 | the manual runbook `docs/SEC08_HANDOFF_MANUAL_RUNBOOK.md`: all 12 rows NOT TESTED | Medium | Owner runs the runbook in modes U and R | **Yes** (closure rule in the runbook section 3) |
| **SEC-34** closed app, binding required | **Confirmed by code reading (not run):** `fetchChallenge` returns null when the app is not running, the unbound request gets `422`, and `apps/web/lib/handoff.ts:67` maps `422` to "Please update the desktop app, then try again." | each step unit-tested; chain not run | runbook row H3b | Low to Medium (wrong advice; no security impact) | Owner chooses option (a) message change or (b) cold-launch design; (a) is a small web-only change with a test | **Yes** (runbook requires an H3b decision) |
| **SEC-13** data lifecycle | Nothing implemented by design; recommendations ready (`docs/OWNER_RECOMMENDATIONS_FINAL_SEC13_SEC20.md`) | n/a | per decision item, listed in that document | Medium (deleted data persists in cache, Redis files and backups; no account deletion) | **Owner decision required.** Decision form is blank; no approval is recorded | **Yes, for the decision.** Implementation can be scheduled as a gate before real user data |
| **SEC-20** composite foreign keys | Nothing implemented; plan and phased SQL ready (Part B of `docs/OWNER_DECISIONS_SEC13_SEC20.md`) | throwaway-database rehearsal of the SQL (D-83) | Alembic revisions and their tests, after approval | Low (defence in depth; application check sound) | **Owner decision required** (B.13 approval block is blank) | **Yes, for the decision** (approve and schedule, or accept as deferred residual) |
| SEC-09 update rollback | Fixed in code | 19 tests plus real-file tests | real signed feed | Medium until validated | Carried to Phase G (needs signing certificate and update host) | No (moved to Phase G, BLOCKED on owner infrastructure) |
| SEC-14 proxy throttling | Mitigated; not reproduced on a real local proxy chain | `proxy-check/run.sh` | the deployed proxy | Medium until validated | Carried to Phase G | No (moved to Phase G) |

## Proposed Phase E closure criteria (for owner agreement)

Phase E can be marked closed when: (1) the SEC-08 runbook rows named in its section 3 are recorded as passed by a person; (2) SEC-34 is decided and, if option (a) is chosen, implemented and H3b re-run; (3) the SEC-13 decision form is completed (implementation may follow as a pre-real-data gate); (4) the SEC-20 approval block is completed either way; (5) a full backend, desktop and web regression run is green at that point; (6) SEC-09 and SEC-14 are explicitly carried into the Phase G tracker. Until then Phase E stays open.
