# Next milestone execution plan (proposed)

Prepared 2026-11-24. **PROPOSED. Nothing here is approved or started.** Per CLAUDE.md, each item needs explicit owner instruction before work begins. Order is by value and by what blocks what.

## Step 1: owner actions that unblock everything (no engineering)

| # | Action | Why | Reference |
|---|---|---|---|
| 1 | Confirm or change the upload quota defaults (20 resumes, 100 documents, 500 MB per user) | They are my assumptions, not product decisions | `docs/OPERATIONS.md`, "Upload quotas" |
| 2 | Run the SEC-08 manual handoff runbook, including row H3b, and record results | The only way SEC-08 can close; also confirms or refutes SEC-34 | `docs/SEC08_HANDOFF_MANUAL_RUNBOOK.md` |
| 3 | Decide the SEC-13 and SEC-20 recommendations (or change them) | Both are blocked on you; neither may be implemented without approval | `docs/OWNER_RECOMMENDATIONS_FINAL_SEC13_SEC20.md` |
| 4 | Decide whether the SEC-35 throughput cost is acceptable | About 25 to 35 percent on the cheapest endpoint, for protection against connection starvation | `docs/PHASE_F_PERFORMANCE_RELIABILITY.md`, section 2 |
| 5 | Choose hosting, signing certificate and secret manager | Unblocks nine of the ten production rows | `docs/PRODUCTION_READINESS_TRACKER.md` |
| 6 | Optionally run the packaged-app speech latency measurement | Only input that could reopen M-13 | `docs/M13_STREAMING_STT_DECISION.md`, section 7 |

## Step 2: small engineering items, each independently testable (after approval)

| # | Item | Size | Done when |
|---|---|---|---|
| E1 | SEC-34: if confirmed, change the web message for a closed app when binding is required (option a), with a test | small | page says to start the app first; the web test suite passes; runbook H3b rerun |
| E2 | Desktop startup regression: rerun the M-2 bench with Docker Desktop stopped; if still about 1 s, bisect with the `INTERVIEW_PERF` marks | small | cause named or the difference explained by the environment |
| E3 | SEC-13 sequence, if approved: schedule the recovery sweep; Redis persistence off; retention and freshness alert; cache purge on deletion; account deletion; deletion ledger; orphan sweep | medium, one change at a time | each with tests and a rollback; privacy notice wording reviewed |
| E4 | SEC-20 migration, if approved: rehearsal on a restored copy, then the phased migrations | medium | timings recorded; cross-tenant insert refused by the database |
| E5 | Extend the Phase F harness: multiple API workers, a memory soak of several hours with a stable measure, real embedder, provider timeout injection with a fake slow provider | medium | results added to `PHASE_F_PERFORMANCE_RELIABILITY.md` |
| E6 | Live stalled-body probe for `transcribe` and `screenshots` (the fix covers them by construction; only uploads were tested live). **2026-11-26: regression test added** (`tests/test_raw_body_connection_release.py`, mutation-checked); the live probe is still not run | small | probe shows other users unaffected |
| E7 | Container image scan and SBOM in CI; gitleaks after the next commit | small | reports archived |

## Step 3: production path (owner infrastructure; engineering supports)

1. Production-mode start behind real TLS with production PostgreSQL and Redis, and a proxy with explicit header and body read deadlines.
2. Off-host encrypted backups, then a restore drill from the off-host copy.
3. Monitoring and alerting wired and tested with a deliberate failure.
4. Certificate, release workflow run, signed update feed; a real update tested end to end.
5. Real-provider validation on production configuration; provider terms reviewed (OQ-4).

## Suggested sequence

Owner steps 1 to 4 first (about a day of attention), engineering E1, E2 and E6 in parallel as they are small, then E3 and E4 in the order the owner approves, E5 alongside, and Step 3 as soon as hosting is chosen. Stop and wait for instruction after each item, per the Phase Gate.
