# Final owner recommendations: SEC-13 (data lifecycle) and SEC-20 (composite foreign keys)

Prepared 2026-11-24. **FOR OWNER DECISION. Nothing here has been implemented, and no irreversible data-lifecycle or schema change has been made.** This is the short form; the full options, evidence, SQL and decision forms are in `docs/OWNER_DECISIONS_SEC13_SEC20.md` (cited by section). This is engineering guidance, not legal or compliance advice: have the privacy-notice wording reviewed by the person responsible for compliance before it is published.

## What changed since the 2026-11-23 document

| Fact | Evidence | Effect on the recommendation |
|---|---|---|
| Redis writes RDB snapshots as well as the append-only file | Reproduced 2026-11-24 on a throwaway `redis:7-alpine` container started with the production flag `--appendonly yes`: `CONFIG GET save` returned `3600 1 300 100 60 10000`; with `--save ""` it returned empty. The production compose file sets no `--save` (SEC-29, confirmed) | Decision 2 must address both. Turning off the append-only file alone leaves cached answer text on disk |
| The newest three backups are never pruned, so real retention can exceed the stated one | `tests/test_backup_scripts.py::test_prune_default_floor_keeps_old_backups_and_says_so` reproduces it (years-old backups survive `RETENTION_DAYS=1`). `prune.sh` now prints a NOTE for each backup kept past its cutoff; behaviour is otherwise unchanged (SEC-30) | The retention you choose is only true if `MIN_KEEP=0` or a hard maximum age is added. Needs your decision (below) |
| There is no audit table | No migration creates `audit_events` and no code writes to it (SEC-31). `docs/DATABASE.md` now says so | "Audit-event retention" has nothing to retain today. Decide it only if you approve building an audit trail |
| Per-user upload quotas now exist (SEC-15) | `docs/OPERATIONS.md`, section "Upload quotas" | Account deletion and file deletion already free quota, because quota is counted from the database rows. No new lifecycle work |

## SEC-13: recommended policy, one line per topic

| Topic | Recommendation | Why | Needs |
|---|---|---|---|
| **Redis cache lifecycle** (A.1) | Option 1b: delete cached answers when a session, resume or document is deleted (index by user and session), cap the cache lifetime at 300 s in production | Makes "removed immediately from the live service" true at negligible cost | Code change (small), tests, your approval |
| **RDB versus AOF** (A.2) | Option 2b: **no Redis persistence** (`--appendonly no --save ""`), **only after** the ingestion recovery sweep is scheduled. If you want persistence kept, use 2d and say in the privacy notice that cached answers can reach disk | Nothing in Redis needs to survive a restart except the job queue, which the database backs. RDB and AOF must be decided together (SEC-29) | Schedule `python -m app.workers.recovery` (for example every minute) first; change the compose command; restart note: queued jobs are lost on restart until the sweep re-sends them |
| **Backup retention** (A.3) | A flat **35 days** with `MIN_KEEP=0` plus an alert if no backup is newer than 48 hours; 14 days if recovery depth matters less than deletion speed | A stated, checkable period. With the default floor the statement would be false (SEC-30) | Set `RETENTION_DAYS`/`MIN_KEEP` in the scheduled job; add the freshness alert |
| **Off-host backup storage** | Required before real data: copy the encrypted backup set to a separate failure domain with a retention no longer than the stated period (and no object lock that outlasts it), and the passphrase held separately from the backups | A backup on the same host does not survive the host. The off-host copy is a second place deleted data lives, so its retention must match the stated period | Your infrastructure choice (BLOCKED, see the production tracker) |
| **Deletion after a restore** (A.3, option 3-R2) | A small **deletion ledger** kept outside the restored database (anonymous IDs only, never content), re-applied by a script after any restore | Otherwise a restore silently brings back data a user had deleted | Small script, one off-host file, tests |
| **Account deletion** (A.4) | Option 4d with 4a as the user path: immediate hard delete after password confirmation and a typed phrase, with a plain warning that backups keep data until the retention period. Operator deletion for support requests. A 7-day grace period (4b) only if accident protection matters more than the simplest statement | Simple, honest, no scheduler. This is the largest piece of work in Decision 4 and is not built | New endpoint and purge (database rows, stored files, Redis keys, refresh tokens), tests with two users, your approval |
| **Uploaded-file deletion** | Already done for a single file (file first, then the row, retryable). Account deletion must reuse it, and an orphan sweep (stored file with no row, row with no file) should run on a schedule and report counts only | Files are the part a user most expects to be gone | Orphan sweep is new (small) |
| **Audit-event retention** | No audit table exists (SEC-31). If you want one, approve it as its own change, store **IDs and event types only**, never content, and set retention to the same period as backups | An audit trail that outlives account deletion is itself personal data | Decision only; nothing to implement now |
| **Backup expiry and deletion implications** | After a deletion, the data remains in encrypted backups until the oldest backup holding it expires (at most the retention period, plus off-host copies). It is never restored if 3-R2 is built. Say this in the privacy notice; draft wording is in A.5.2 (not approved) | The guarantee a user can honestly be given | Your approval of the wording after compliance review |

**Recommended set:** 1b + 2b + 3b (35 days, `MIN_KEEP=0`) with the 3-R2 ledger + 4d/4a. **Order of work if approved:** (1) schedule the recovery sweep; (2) Redis persistence off; (3) retention and freshness alert; (4) cache purge on deletion; (5) account deletion; (6) ledger; (7) orphan sweep. Each is independently testable and reversible by configuration except account deletion itself, which is irreversible by design.

**Honest limits:** nothing above has run against real user data. Retention figures are recommendations, not facts about the current system, whose real retention is "newest three backups forever, others until pruned".

## SEC-20: composite foreign keys

**Recommendation: approve the plan in Part B as written, but run it before real user data exists, or after a rehearsal on a restored production copy.** Priority is low (defence in depth: the application check is sound and live probes found no cross-tenant access), and it is cheapest now.

| Item | Detail (section of the full document) |
|---|---|
| Scope | **7 composite foreign keys** `(parent_id, user_id)` replacing single-column keys, and **5 new unique constraints** `(id, user_id)` on the referenced tables (B.2, B.3). `session_preferences` is already protected by row-level security. Three polymorphic `source_id` columns cannot be foreign keys and stay as documented exceptions |
| Existing-data validation | Run the read-only queries D1 to D8 (B.5) on production first. Any row whose parent belongs to another user must be investigated, not auto-fixed. The application always sets `user_id` from the authenticated user, so zero violations are expected, which is a hypothesis to verify, not a fact |
| Migration sequencing | Phased (B.6): (1) create the unique indexes `CONCURRENTLY` and attach them as constraints; (2) add each composite key `NOT VALID`; (3) `VALIDATE` each; (4) drop the old single-column keys last. Each phase is its own Alembic revision so a failure leaves a working schema. Alembic files are proposed only, not written |
| Locks and duration | `ADD ... NOT VALID` takes `SHARE ROW EXCLUSIVE` on both tables (blocks writes briefly). `VALIDATE` does not block normal writes but scans the table. **Durations at production size are not known**: rehearse and record them, run in a low-traffic window with `lock_timeout` (B.6.1) |
| Backup and recovery | Take a fresh encrypted backup **and verify it by restoring it** to a scratch database immediately before. Restore drills use `infra/backup/` (B.9). A backup taken before the migration restores into the old schema and must be migrated again |
| Rollback limits | Rolling back (drop the new composite keys, recreate the old single-column keys; B.7) loses no data, but it returns the system to application-only protection against cross-tenant references. A rollback after a failed `VALIDATE` is safe because the old keys are dropped only in the last phase. Once the old keys are dropped, rolling back means re-creating them, which needs a lock on the same tables. A backup taken before the migration restores into the old schema and must be migrated again |
| Production deployment procedure | (1) owner approval signed (B.13); (2) D1 to D8 clean; (3) rehearsal on a restored copy with timings recorded; (4) fresh verified backup; (5) maintenance window, API and workers draining, `lock_timeout` set; (6) run revisions one at a time, checking the application's health and a cross-tenant insert test after each; (7) record outcome in `docs/DECISIONS.md`. Effort is an estimate in B.11 (not measured) |
| What this does not fix | Polymorphic references; tables outside the 7; anything the application does with the maintenance (`BYPASSRLS`) login |

**Needs from you:** the approval block (B.13) and, for production, the infrastructure in the production-readiness tracker (`docs/PRODUCTION_READINESS_TRACKER.md`) so a rehearsal copy can exist.

## Decision form (short)

| Decision | Recommended | Your choice | Date |
|---|---|---|---|
| Cache purge on deletion, 300 s cap | 1b | | |
| Redis persistence | 2b after sweep is scheduled | | |
| Backup retention and `MIN_KEEP` | 35 days, `MIN_KEEP=0`, 48-hour freshness alert | | |
| Deletion ledger | build (3-R2) | | |
| Account deletion | 4d with 4a | | |
| Audit trail | not now | | |
| SEC-20 | approve; run before real data | | |
