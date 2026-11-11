# Phases 13–14 Plan — Hardening, Observability, Deployment and Release

Status: **PROPOSED. Nothing here is approved or started.** Planning review only: no application code, dependency, migration or production configuration was changed. Sources: PHASES.md (Phase 13/14), CLAUDE.md, DECISIONS.md (D-24, D-26, D-45, D-46, D-49, D-50), SECURITY_REVIEW.md, OPERATIONS.md, DESKTOP_RELEASE.md, RISKS_AND_OPEN_QUESTIONS.md, DATABASE.md §6, `.github/workflows/ci.yml`.

Goal: a **production-ready internal release**, with less development and more verification. No new scope is invented; every item below comes from the existing Phase 13/14 text, the open risks, or the "deployment gate" notes in the docs.

Facts found while reviewing (to avoid planning on assumptions):
- CI runs api (ruff, mypy, pytest), web (lint, typecheck, build) and desktop (typecheck, tests, build). There is **no** dependency/vulnerability scan, SAST, container build or deploy job. Web unit tests are not in CI (R-34).
- There is **no Dockerfile or deployment config** in the repo; `docker-compose.yml` is dev-only (Postgres, Redis). `infra/` only holds a Postgres init script.
- The `audit_events` table is in DATABASE.md, but **no application code writes audit events** (grep found none). Retention sweeps, account export/deletion and ClamAV are not implemented.
- I did not find login/registration throttling in the API code by search; it must be **verified first** (13A) and added if missing, because signup is open (D-25).
- Latest migration is `0009`; RLS (`0008`) exists but has only been verified on dev/test Postgres, not on a deployment database.
- Desktop: unsigned installer, fuses verified, NSIS installer never executed (only `win-unpacked` launched), no signing, no updater (D-50 Proposed).

---

## 1. Remaining scope

### 1.1 Required for a production-ready INTERNAL release
| # | Item | Source |
|---|---|---|
| R1 | Verify/add auth abuse protection (login/register throttling) and a shared (Redis) rate limiter for expensive endpoints if more than one API process is used; otherwise document and enforce single-process/sticky deployment | SECURITY_REVIEW, R-17/R-30/R-38/R-46/R-53 |
| R2 | Security headers on API and web (CSP for web, `X-Content-Type-Options`, frame protections; HSTS at the TLS edge) | Phase 13 "headers/CSP" |
| R3 | Dependency and secret scanning in CI (Python, npm for web and desktop), plus a triage of current findings | Phase 13 "dependency/SAST scanning" |
| R4 | Per-user upload quotas and disk-usage alerts; decide ClamAV (on) or restrict signup (D-15, R-17) | Phase 13 "ClamAV on", SECURITY_REVIEW |
| R5 | Audit logging for security-relevant events (login, failed login, password/session changes, deletes, consent changes), no content | Phase 13 "audit logging coverage", DATABASE §6 |
| R6 | Retention jobs for data with a defined expiry, and the account deletion/export flow | Phase 13 "retention jobs", "privacy features"; R-35, OQ-11 |
| R7 | Production configuration gates verified on a staging-like database: non-superuser app login, `interview_app_runtime` membership, `rolbypassrls`, RLS tests against that database, migration up/down | SECURITY_REVIEW deployment gate, OPERATIONS |
| R8 | Observability: scrape/protect `/metrics`, alerts for 5xx, DB errors, LLM failure rate, ingestion backlog, disk; worker visibility; request-ID logging confirmed content-free | D-46, OPERATIONS |
| R9 | Backup and restore drill (DB + file volume as a coordinated set) | Phase 13 acceptance, OPERATIONS |
| R10 | Load/perf check against a stated budget at the internal scale (OQ-2 still unknown) | Phase 13 targeted tests |
| R11 | Deployment definition: container build for API/worker/web, TLS, secrets manager, persistent file volume, migration step, smoke tests, rollback | Phase 14 |
| R12 | Desktop installer: run the NSIS installer on a clean VM/account (install, upgrade over, uninstall); publish SHA-256 and release notes; **unsigned installers only for a small trusted group** | DESKTOP_RELEASE §4–6 |
| R13 | Runbooks: deploy, rollback, key rotation, incident/disk/Redis/queue (extend OPERATIONS.md) | Phase 14 |
| R14 | Threat-model/pen-test checklist pass and a refreshed security review of Phases 7–12 (the existing review covers Phase 3 only) | Phase 13 acceptance |

### 1.2 Optional improvements (not required for the internal release)
- Email verification and password reset (deferred by D-24; open signup approved by D-25, so keep deferred unless the owner wants it).
- Redis-backed shared limiters and WebSocket channel state for multi-process scale (needed only when running more than one API process).
- HNSW index and Indic retrieval measurements (R-21/R-22), persisted usage/cost table (R-24).
- Queue-depth/first-token Prometheus metrics, aggregated multi-replica metrics.
- Playwright browser tests (R-33/R-41, needs owner approval as a new dependency).
- Backend minimum-supported-desktop-version check (needs its own approval).
- ClamAV, if signup stays restricted and uploads stay small (alternative to R4's scanner).
- Moving the web CI `npm test` step in (R-34) — small and low risk; propose with 13A.

### 1.3 Deferred features / decisions (do not build now)
- Code signing and auto-update implementation (D-50 Proposed) — blocked on owner decisions below.
- Real STT provider and real-LLM quality evaluation (OQ-4, OQ-5, R-23, R-25, R-37) — blocked on data-handling approval.
- macOS/Linux desktop, multi-monitor, Hindi/Telugu STT and detection, transcript persistence, WebSocket audio, WebSocket for the web page.
- PyMuPDF licence review (R-18) before any external/commercial distribution; robots.txt/ToS enforcement for job import (R-29).
- Legal review of retention periods and recording consent (OQ-11, H).

---

## 2. Recommended batching

Principle: merge **verification and low-risk, config-level** items; keep **auth, data-deletion, database-role, signing and update** changes separate so each can be reviewed and rolled back alone. Each batch ends with an owner acceptance and its own commit(s); no batch starts before the previous is accepted.

| Batch | Contents | Why together / apart | Security-sensitive? |
|---|---|---|---|
| **M0 (maintenance, separate)** | Flaky-test fix (§5) | Test-only, unrelated to any phase | No |
| **13A — Security baseline & scanning** | R1 verify/add auth throttling, R2 headers, R3 CI scanning + triage, web `npm test` in CI | All are additive, config/CI-level, small; findings from scanning feed the rest | Auth throttling is sensitive → its own commit |
| **13B — Audit, quotas, retention, privacy** | R4 quotas (+ ClamAV decision), R5 audit events, R6 retention + account export/delete | One data-lifecycle theme; needs a migration (audit/quota fields) → plan separately, **migration reviewed before merge** | **Yes — keep separate from 13A and 13C** (deletes data) |
| **13C — Observability & operational readiness** | R8 alerts/dashboards/worker visibility, R9 backup/restore drill, R10 load test | Verification-heavy, mostly docs/config/tests; no behavior change | No |
| **14A — Deployment definition** | R11 containers, TLS, secrets, migration step, smoke tests, R7 role/RLS gate on staging, rollback rehearsal, R13 runbooks | Needs OQ-1 (hosting) decided; produces the staging that 13C/R14 verify against | **Yes — DB roles and secrets: separate commits** |
| **14B — Desktop release (unsigned internal)** | R12 installer VM test, hashes, release notes, release checklist | No signing/updater code; pure process + verification | No |
| **14C — Signing then update (OPTIONAL until approved)** | D-50 A then B | Signing credentials and an update channel are remote-code-execution paths; **always last and separate**; B only after A | **Yes — fully separate** |
| **Closeout (R14)** | Refreshed security review of Phases 7–12, pen-test checklist, M5 full regression | Gate before declaring production-ready | — |

Sequence: **M0 → 13A → 13B → 14A → 13C → 14B → R14 closeout → (14C if approved)**. 13C moves after 14A so the restore drill, alerts and load test run on the real staging environment, not on a laptop.

What must pass before moving to the next batch is defined per batch in §4.

---

## 3. Production readiness review (existing state, no implementation proposed)

| Area | Current state | Gap / risk | Where addressed |
|---|---|---|---|
| Desktop security / Electron hardening | Sandbox, context isolation, strict CSP, fuses verified in the packaged build (D-49); sender-validated narrow IPC; default-deny permissions with audio-only exception (D-53); tokens only in the main process | NSIS installer never executed; no packaged-app audio/permission smoke test (manual only); `GrantFileProtocolExtraPrivileges` stays enabled by design | 14B (installer VM test), keep manual checklist per release |
| Authentication & tokens | Argon2id, short JWT + rotating refresh with family reuse, DPAPI-protected refresh token on desktop, WS single-use tickets | Login/register throttling **not found** (verify); email verification/reset deferred (D-24); access to disabled users on idle sockets (R-44) | 13A; owner may keep D-24 deferred |
| API security & rate limiting | Ownership checks, 404 on foreign access, RLS (D-45), body/size limits, SSRF-safe import | Limiters are **per process** (R-30/38/46/53); `/metrics` unauthenticated; no security-header coverage confirmed; no upload quotas/ClamAV | 13A, 13B; single-process/sticky deployment until shared limiters exist |
| Logging & privacy | Structured JSON logs with request IDs; redaction; privacy sentinel tests incl. audio/transcript; no content in metrics | R-39 noisy SQLAlchemy error on disconnects; no audit-event writer; third-party provider data terms unknown (OQ-4) | 13B (audit), owner decision on provider |
| Database & migration safety | Alembic 0001–0009, RLS forced, runtime role, maintenance connection separated | Role provisioning and RLS only verified on dev/test; no migration up/down rehearsal on a production-like copy; no restore drill | 14A (R7), 13C (R9) |
| Dependency & vulnerability review | Lockfiles (uv, npm); PyMuPDF AGPL note (R-18) | No automated scan, no SBOM, no update policy; Electron 45 patch cadence not tracked | 13A |
| Installer & release process | NSIS per-user installer; documented process; fuses check; rollback = reinstall | Not executed on a clean VM; no checksums published; no version/compat policy enforcement | 14B |
| Code signing | **None** (D-50 Proposed) | SmartScreen warnings; blocks safe auto-update; unsigned only for trusted testers | Owner decision D1; 14C |
| Auto-update | **None** | Users lag behind security fixes; no minimum-version mechanism | Owner decision D2; 14C after signing |
| Observability & operations | Prometheus metrics (process-local), `/health`, `/ready`, runbook for ingestion/Redis/backup | No alert rules, no dashboards, no worker scrape endpoint, no queue-depth/first-token metrics, no log shipping decision | 13C |

---

## 4. Testing-first completion strategy (per batch)

Common to every batch: targeted tests chosen with the test impact matrix; ruff/mypy (`--no-incremental`)/CI green; scan diffs for secrets; user-isolation tests for any new data path; no audio/transcript/token content in logs; owner accepts before the next batch.

| Batch | Automated tests | CI checks | Packaged-app checks | Manual acceptance | Rollback / recovery |
|---|---|---|---|---|---|
| **M0** | The test run 30+ times (alone and in file) with zero failures; full file passes | api job | — | — | `git revert` of the test commit |
| **13A** | Auth throttling tests (lockout, no user enumeration, per-IP and per-account, resets); header tests (API responses, web build output); isolation unaffected | New: dependency audit (pip-audit/npm audit or equivalent, owner-approved tool), secret scan; web `npm test` | Desktop unaffected: confirm CSP unchanged | Try repeated bad logins from web and desktop; review the scan report and the triage decisions | Config flags default to previous behavior; revert commit |
| **13B** | Quota enforcement; audit-event writing (no content, one row per event); retention sweep (dry-run and real, boundary dates); account export/delete incl. files, chunks, embeddings, sessions, answers; user-isolation + RLS suites; migration up/down | api job incl. RLS tests on real Postgres | Desktop sign-out/session flows unaffected | Delete a test account end to end; confirm nothing remains in DB or volume; confirm the other user is untouched | **Backup before migration; downgrade tested; deletion is dry-run first** |
| **14A** | Smoke tests post-deploy (`/health`, `/ready`, register→upload→ready→question); migration up/down on a staging copy; RLS two-user test against the deployment DB with the runtime login; role checks (`rolbypassrls=false`, not superuser) | Container build and image scan in CI; deploy to staging from CI | Desktop (unsigned) pointed at staging over HTTPS: sign in, session, typed Q&A, screenshot, audio with fake providers | Owner runs the critical path on staging | **Rollback rehearsal**: deploy previous image, previous migration head; verify data intact |
| **13C** | Alert fire tests (induce 5xx, stop Redis, fill disk threshold on staging); load test within budget (concurrent sessions per OQ-2 assumption); recovery of a failed ingestion job | Load/alert definitions kept in repo; CI unaffected | — | Owner views dashboards and receives a test alert; **restore drill**: restore DB + file volume into an isolated environment, check migration head, sample files, ownership, RLS | Documented and timed restore (RTO/RPO recorded) |
| **14B** | Desktop typecheck/tests/build; fuse read; installer hash recorded | desktop job + release build job (unsigned artifact, internal only) | **Install on a clean VM/account**, install over previous, uninstall, settings survive, token file protected | `DESKTOP_MANUAL_CHECKLIST.md` incl. Phase 12 audio, run on the packaged installer | Reinstall previous installer; keep every installer + SHA-256 |
| **14C-signing** (if approved) | Signature verification script; tampered-binary check | Signing only in the release pipeline; secret never in repo; "signed and timestamped" gate | `Get-AuthenticodeSignature` valid on exe, installer, uninstaller | Fresh install shows publisher, no unknown-publisher block | Credential revocation/rotation runbook rehearsed on paper |
| **14C-update** (if approved) | Automated test that an **unsigned/tampered update is rejected**; no-downgrade; HTTPS-only | Update-feed publish step restricted and audited | Update from vN-1 to N on a clean VM; staged rollout | Owner updates a test machine; withdraws a release | **Withdraw procedure**: pull the feed entry; users stay on vN; reinstall path |
| **Closeout (R14)** | Full backend + desktop + web regression (M5) | All CI green on the release commit | Final packaged smoke | Final owner acceptance checklist | Release tag + rollback plan on record |

---

## 5. Flaky test maintenance fix (separate change, M0)

**Evidence (2026-11-11):** `tests/test_qa_live.py::test_the_service_keeps_working_after_disconnects` failed 2/17 runs on the Phase 12 tree and 2/30 on an unmodified HEAD worktree, always with one row still `streaming` instead of `cancelled`; it passes alone most of the time. Not a Phase 12 regression.

**Cause:** the test waits for `limits.active(user_id) == 0` and then immediately reads `generated_answers`. The limiter slot is released before the shielded "finish" database write that sets `cancelled` has committed, so the read can win the race.

**Smallest fix (test only, one place, `tests/test_qa_live.py`):** after the existing limiter wait, replace the immediate read with a wait for the database state:

```python
def all_cancelled() -> bool:
    rows = setup.run_sql("SELECT status FROM generated_answers")
    return len(rows) == 3 and all(r["status"] == "cancelled" for r in rows)

assert wait_for(all_cancelled)
statuses = [r["status"] for r in setup.run_sql("SELECT status FROM generated_answers")]
assert statuses == ["cancelled"] * 3
```

(Optionally apply the same wait after each loop iteration. The existing `wait_for` helper, 10 s timeout, is reused.) The assertions keep their strictness, so a real leak still fails. No application code changes.

**Verification:** run the test 50 times alone and the file 5 times; zero failures required. Separately, `test_realtime_qa_live.py::test_one_slow_client_does_not_stall_another_user` fails consistently on this Windows machine but passes on Linux CI; it is **environment-dependent**, not fixed by this change, and should stay as a documented Windows-only known issue unless the owner wants it skipped on Windows.

Commit as `test(api): wait for the cancelled status instead of the limiter in the disconnect test`.

---

## 6. Owner decisions (recommended defaults)

| # | Decision | Recommended default | Needed before |
|---|---|---|---|
| D1 | **Code-signing approach and owner** (D-50 A: legal entity/identity, budget, who holds signing access) | Keep **unsigned, trusted-testers-only**; prefer managed cloud signing (A3), else OV cert on a cloud HSM; decide the signing owner. Not required for the internal MVP if distribution stays tiny | 14C only |
| D2 | **Auto-update and feed hosting** (D-50 B) | **Manual signed updates first (B1)**; `electron-updater` on a dedicated HTTPS static host later; defer until D1 is done | 14C only |
| D3 | **Hosting / deployment target and budget** (OQ-1): cloud provider, single VM vs managed services, who operates it | One VM or small container host in one region, managed Postgres 16+pgvector and Redis with TLS, **single API process behind a TLS proxy with sticky routing** (per documented process-local limits) | 14A |
| D4 | **Expected users and concurrency** (OQ-2) | Assume a small internal group (tens of users, handful of concurrent live sessions) and keep the default limits | 13C load budget |
| D5 | **Real provider approval** (OQ-4, OQ-5): who owns the key, data-handling terms, whether interview content may be sent | **Keep fakes for staging verification; approve one provider account only after reviewing its terms**; real STT stays deferred | Before any real interview content; not a blocker for 13/14 verification |
| D6 | **Signup policy and malware scanning** (D-25, R-17) | Keep open signup only on a trusted network; otherwise invite-only; enable per-user quotas now; ClamAV only if uploads are opened to untrusted users | 13B |
| D7 | **Retention periods and account-deletion SLA** (OQ-11, DATABASE §6) | Use the proposed defaults in DATABASE §6 (audit 12 months, hard delete within 30 days); legal review is the owner's call | 13B |
| D8 | **New tooling approvals**: dependency scanner and secret scanner (and optionally Playwright, R-33) | Approve one Python and one npm scanner plus a secret scan in CI; **no Playwright** for now | 13A |
| D9 | **Email verification/password reset** (D-24) | Keep **deferred** for the internal release | 13A |
| D10 | **Backend minimum desktop version check** (D-50) | Skip for now | 14C |

Decisions not on this list (batch order, test design, flaky fix) are mine to recommend and need only your approval of this plan.

---

## 7. Proposed acceptance criteria for "production-ready internal release"
1. M0, 13A, 13B, 14A, 13C, 14B each accepted by the owner with the checks in §4.
2. Staging mirrors production settings; post-deploy smoke tests pass; rollback rehearsed; restore drill succeeded with recorded timings.
3. RLS and runtime-role checks pass on the deployment database; migrations up/down verified on a staging copy.
4. CI green on the release commit (api, web, desktop, scans); no unresolved high/critical dependency findings (or each is explicitly accepted).
5. Refreshed security review of Phases 7–12 closed or accepted; known limits (per-process limiters, single-process deployment, unsigned installer, fake STT, provider terms) listed in the release notes.
6. Unsigned installer distributed only to trusted testers; signing/update remain a separate decision.
