# Phase D — M-9 to M-12 revalidation report

Date: 2026-11-23. Scope: re-check M-9, M-10, M-11 and M-12 against their written acceptance criteria (`docs/PRODUCTION_ROADMAP.md`). Nothing was committed, pushed, tagged, published or deployed. Phase F, Phase G and M-13 were not touched.

Status labels used (and only these): IMPLEMENTED, AUTOMATED-TESTED, MANUALLY-TESTED, ENVIRONMENT-VERIFIED, SECURITY-REVIEWED, PRODUCTION-VALIDATED, BLOCKED, FAILED, NOT TESTED.

"AUTOMATED-TESTED" means automated tests passed. It does not mean the milestone is finished. No milestone below is PRODUCTION-VALIDATED.

## Environment for this run

| Item | State |
|---|---|
| Desktop | `npx tsc --noEmit` passed; `npx vitest run` 55 files, 1143 tests passed, 0 failed |
| Web | `npx tsc --noEmit` passed; `npm test` 125 passed, 0 failed; `npx eslint .` passed |
| Backend | `uv run ruff check .` passed; `uv run mypy app` passed (148 files); full `uv run pytest` result in section "Regression" below |
| Postgres / Redis | dev compose containers (`pgvector/pgvector:pg16`, `redis:7-alpine`), started for this run because Docker Desktop was not running |
| Port 47821 | nothing was listening at the start of the run; the two presence-server tests passed. The owner's installed app was not stopped or touched |
| Other processes | an unrelated project's test run (`royalhrms`) was using the CPU and a second Postgres container was running; neither was touched |

## M-9 Session management — AUTOMATED-TESTED; desktop manual checks owner-reported; web browser checks NOT TESTED

| Acceptance criterion | Test performed | Command | Result | Evidence | Gap |
|---|---|---|---|---|---|
| Switch sessions without restart; audio stops on switch | Desktop store and IPC tests (switch clears history before load, stops audio, discards pending screenshot, cancels answer) | `npx vitest run tests/session-management-store.test.ts tests/session-management-ipc.test.ts tests/session-management-guards.test.ts tests/session-view.test.ts` (inside `apps/desktop`) | passed | 183 tests passed across the 8 session-related desktop files (`session-management-*`, `session-view`, `session-create-*`, `session-form`) | real switching with live audio is manual only |
| Create, idempotency | Backend tests; live probe | `uv run pytest tests/test_session_management.py`; probe PT-08h/PT-08i | passed | same `Idempotency-Key` twice returned 201 then 200 with the same id; a malformed key was refused | idempotency needs Redis; with Redis down a lost-response retry can duplicate (documented) |
| Listing and pagination | Backend tests (inside the clean full run) | `uv run pytest tests/test_session_management.py tests/test_sessions.py tests/test_sessions_schema.py tests/test_session_handoff.py` | passed (full run) | `tests/test_sessions.py`, `tests/test_session_management.py` | list is ordered by creation time, not activity (documented) |
| Rename, complete, reopen, delete | Backend tests; live probe (owner path) | same | passed | probe PT-08c: owner's session unchanged by other user's attempts | |
| Ownership and tenant isolation, cross-user attempts | Live probe against a local API and throwaway DB with two synthetic accounts; `tests/test_rls.py` | `python -I pentest.py` (see Phase E) | passed, 35 of 35 probe checks | other user got 404 on 11 session routes (GET, PATCH, DELETE, end, reopen, export markdown and pdf, questions GET and POST, handoff, ws-ticket) and 404 when creating a session with another user's resume; 404 body identical to an unknown id | ENVIRONMENT-VERIFIED on a local instance only |
| Audio and screenshot cleanup on switch/delete | Desktop tests; backend stores neither | desktop session tests above | passed | server never stores audio or screenshots (PRIVACY.md) | not exercised with real devices |
| Export (Markdown, PDF) | Backend export tests; live probe | `uv run pytest tests/test_session_management.py`; probe PT-08f/PT-08g | passed | `Content-Disposition: attachment`, `Cache-Control: no-store`, no email or user id in the file, `format=json` refused with 422 (D-76) | PDF was not opened and read by a person; layout is not checked beyond tests |
| Desktop and web behaviour | Desktop tests above; web `npm test` 125 passed | | passed | | owner confirmed 12 desktop manual checks (reported 2026-11-22, no evidence files); web checks W1 to W6 NOT TESTED |
| Error handling and recovery | Desktop store tests (failed load shows nothing, deleted-elsewhere message) | desktop session tests | passed | | |

Owner blockers: none for the automated work. Manual: web W1 to W6 in `docs/M9_MANUAL_TEST_SHEET.md`; attach evidence for the 12 desktop checks.

## M-10 Windows signing and CI — IMPLEMENTED, AUTOMATED-TESTED; real signing BLOCKED

| Acceptance criterion | Test performed | Command | Result | Evidence | Gap |
|---|---|---|---|---|---|
| Fuses verified in CI | verifier on the local build | `node scripts/release/verify-build.mjs --mode signed` | fuses PASS | "Electron fuses: ok" | |
| Checksums and artifact integrity | re-hash `release/` against `SHA256SUMS.txt` | `node scripts/release/verify-build.mjs --check-sums` | passed | "Checksums match." | sums are generated and checked inside one job, so they do not protect against a compromised runner; the sums file itself is unsigned |
| Missing or invalid signature is refused | verifier in signed mode on the unsigned local build | `node scripts/release/verify-build.mjs --mode signed` | refused (the correct result) | installer and executable: `Authenticode status is "NotSigned"`, no timestamp, digest unknown; final line "Build verification FAILED. This build must not be released." | |
| Publisher validation | unit tests, plus the gap fixed in this phase (below) | `npx vitest run tests/release-lib.test.ts tests/release-scripts.test.ts tests/release-workflow.test.ts` | 88 passed | | never run against a real signed file |
| Timestamp validation | unit tests of `evaluateSignature` | same | passed | | checks that a timestamp certificate exists; the countersignature chain is not validated separately |
| Expired or invalid certificate; missing signing configuration | preflight unit tests (missing, partial, mixed configuration; expiry; usage; private key) | same | passed | | no real certificate was used |
| CI workflow protections | static review of the three workflows plus `release-workflow.test.ts` text checks | read-only review | actions pinned by commit SHA, `contents: read`, manual dispatch only, no `pull_request_target`, actor allow-list that fails closed, secrets only in the environment-scoped signing job, PFX removed in an `always()` step | | GitHub repository settings (environment reviewers, `DESKTOP_RELEASE_ACTORS`, branch protection) cannot be seen from the repository: NOT TESTED |
| `Get-AuthenticodeSignature` reports Valid with timestamp on a real installer | not possible | | BLOCKED | | needs the owner's certificate |

Defect found and fixed (**P-01, Medium**): the signed verifier checked each file for `Valid` but never compared the installer's signer with the executable's signer or with `SIGN_PUBLISHER_NAME`, so any Windows-trusted code-signing certificate would pass for the installer. A new check `signerConsistencyErrors` (`scripts/release/release-lib.mjs`) requires one identity for both files and, when set, the configured publisher. Regression tests: `tests/release-lib.test.ts` (4 cases). Verified live: the signed-mode run above now also reports "installer and application share the configured signer" and fails closed when the signer cannot be read. Still never run on a real signed binary.

Owner blockers: code-signing certificate or cloud signing account; GitHub `desktop-release` environment with required reviewers; `DESKTOP_RELEASE_ACTORS`; `SIGN_PUBLISHER_NAME`; clean-VM install, upgrade and uninstall test.

## M-11 Secure automatic updates — IMPLEMENTED, AUTOMATED-TESTED; real update validation BLOCKED

| Acceptance criterion | Test performed | Command | Result | Evidence | Gap |
|---|---|---|---|---|---|
| Manifest validation, downgrade and same-version rejection | unit tests with a fake engine | `npx vitest run tests/update-manifest.test.ts tests/updater.test.ts tests/update-store.test.ts tests/updater-guards.test.ts` | 108 passed | | engine behaviour is faked |
| Signature and publisher checks | source-guard tests (no `setFeedURL`, no `allowInsecure`, https only) and the packaged `app-update.yml` pin | same | passed | | the real updater has never verified a real signed artifact: BLOCKED |
| Corrupt, interrupted download; network failure; disk full | unit tests with injected errors | same | passed | | library behaviour is delegated; disk check measures userData, not the updater cache volume |
| Concurrent update events | unit tests | same | passed | | |
| Installation and restart; settings preserved | NSIS template check only | `updater-guards.test.ts` | passed | | not exercised: BLOCKED (needs a signed N to N+1 update on a real machine) |
| No unintended restart of capture or audio | IPC tests | `npx vitest run tests/audio-ipc.test.ts` | 35 passed | | see defect below |

Defect found and fixed (**P-02, Medium**): when an install started, listening and a held screenshot were stopped, but nothing refused `audio:start` or `capture:start` afterwards, so a live renderer could start capture again during the install window (up to the 45 second watchdog, and after `install_failed`). Both channels now refuse while the updater state is `installing` and recover when it returns to an error state. Regression test: `tests/audio-ipc.test.ts` ("while an install is in progress audio and capture cannot be started; a failed install releases them"). The older test only checked the state at the moment of the call.

Not fixed, recorded (**P-03, Medium, design gap**): the manifest `version` is unsigned text and is not tied to the installer's own embedded version. An attacker who controls the update feed could serve an older, validly signed installer with a forged newer version. The feed host's write controls are the real defence. Recommended: check the downloaded installer's FileVersion against the manifest before install, once a signed build exists to test it on.

Owner blockers: signed release; HTTPS update host with controlled write access; a real N to N+1 test.

## M-12 Backend hardening and deployment — IMPLEMENTED, AUTOMATED-TESTED; local Docker drill NOT re-run; production BLOCKED

Docker Desktop was started for the dev Postgres and Redis only. The production compose stack, the backup, restore and prune scripts and the restore drill were **not run in this phase**, so the drill results in `docs/DEPLOYMENT.md` are carried over from the earlier session and are labelled NOT TESTED here. Local Docker verification and production validation are different things: no production environment exists.

| Acceptance criterion | Test performed | Command | Result | Evidence | Gap |
|---|---|---|---|---|---|
| Database connection and timeout configuration; health checks; worker heartbeat | backend tests against the real dev Postgres | `uv run pytest tests/test_health.py tests/test_fault_injection.py tests/test_worker_heartbeat.py tests/test_config.py` | passed (inside the clean full run) | static review: bounded pool, statement and idle-in-transaction timeouts, `SET LOCAL ROLE` per transaction | |
| Redis and PostgreSQL failure handling | fault-injection tests | `tests/test_fault_injection.py` | passed | infrastructure failure maps to 503 with `Retry-After` and a fixed body; 500 with a fixed body otherwise; logs carry the class name only | |
| Container security (non-root, read-only filesystem, capability drop, limits) | static review of `services/api/Dockerfile`, `docker-compose.prod.yml` | read-only | API, worker, web: non-root, read-only root, `cap_drop: ALL`, `no-new-privileges`, limits, no published DB or Redis ports | | not started; ENVIRONMENT-VERIFIED status is not claimed. Postgres and Redis containers have none of those hardening options (finding P-07) |
| Backup encryption, integrity, restore, tenant isolation after restore | static review of `infra/backup/*.sh` | read-only | AES-256-CBC with PBKDF2 (600000 iterations), passphrase from a file, restore refuses a non-empty database | | not run. Findings P-04 and P-05 below |
| Configuration validation and production settings | `tests/test_config.py` | | passed | JWT length, entropy and character class; `rediss://` required; CORS wildcard refused; docs only in local | `APP_ENV` defaults to `local`, so these checks are off when it is unset (P-06) |

Findings from the M-12 review (all recorded, none Critical or High):

| ID | Severity | Finding | State |
|---|---|---|---|
| P-04 | Medium | Backups silently fall back to plaintext when `BACKUP_PASSPHRASE_FILE` is unset (`infra/backup/backup.sh:28-36`); only `encrypted=no` in the manifest | Open, recommend refusing to run unencrypted outside an explicit opt-in |
| P-05 | Medium | Backup integrity is not authenticated: CBC without a MAC, and `SHA256SUMS` sits beside the files, so it detects accidental damage only | Open, recommend `age` or GPG, or a signed sums file |
| P-06 | Medium | `APP_ENV` defaults to `local`; every production safeguard is off when it is unset (`app/core/config.py:55`). The image and compose set it, and no test asserts that a production-like environment without it is refused | Open, recommend making it required |
| P-07 | Low | Postgres and Redis containers have no read-only root, capability drop, `no-new-privileges` or non-root user; no internal-only network | Open |
| P-08 | Medium | Runtime containers receive the maintenance database credential (`docker-compose.prod.yml` `runtime-env`); the migrate service receives all provider keys | Open, acknowledged in `docs/DEPLOYMENT.md` |
| P-09 | Low | The deployment docs say "non-root, read-only root, cap_drop ALL" without saying that this covers only the app containers | Open (doc) |

Owner blockers: production host, TLS and DNS, managed or hardened Postgres and Redis, backup storage and key custody, a restore drill on that infrastructure, and a decision on P-04, P-05, P-06 and P-08.

## Regression and clean desktop run (D5)

- **Desktop:** clean run after the owner's app was found not to be holding port 47821: 55 files, 1143 tests, 0 failed. The two earlier failures (the presence-server tests) now pass. The count is the previous 1134 plus 9 new regression tests added in this phase.
- **D-75 (answer router and exact-text answers):** intact. Tests in `tests/test_answer_router.py` and `tests/test_extractive.py` are part of the backend run below.
- **D-76 (session export PDF replaces JSON):** intact. Backend export tests in `tests/test_session_management.py`; the desktop PDF save path in the desktop IPC tests; live probe PT-08g confirmed `format=json` returns 422.
- **Backend full run:** clean run with nothing else touching the test database, after all fixes: `uv run pytest -q -p no:cacheprovider` from `services/api` gave **2324 passed, 4 skipped, 2 deselected, 0 failed in 28 minutes 4 seconds** (the previous 2314 plus 10 new regression tests). An earlier attempt was discarded: it hung on a stale database lock after I stopped a first run, and a second attempt was contaminated by my own targeted tests sharing the same test database. The skip reasons were not captured.

## Summary of status

| Milestone | Status |
|---|---|
| M-9 | AUTOMATED-TESTED; local multi-user isolation ENVIRONMENT-VERIFIED; desktop manual checks owner-reported; web W1 to W6 NOT TESTED |
| M-10 | IMPLEMENTED, AUTOMATED-TESTED; real signing validation BLOCKED |
| M-11 | IMPLEMENTED, AUTOMATED-TESTED; real update validation BLOCKED |
| M-12 | IMPLEMENTED, AUTOMATED-TESTED; local Docker drill NOT TESTED in this phase; production validation BLOCKED |
