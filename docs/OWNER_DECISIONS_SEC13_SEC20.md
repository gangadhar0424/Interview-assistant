# Owner decisions: SEC-13 (data lifecycle) and SEC-20 (composite foreign keys)

Date: 2026-11-23. Status: **FOR OWNER DECISION. Nothing in this document has been implemented, and nothing will be executed against any real database before the owner approves.** This document makes no decision for the owner. Every irreversible choice is written as a decision with options, consequences and a recommendation.

Evidence labels used on every claim:

* **Verified**: read in the code, or executed, in the session that wrote this document.
* **Reported**: stated in another project document and not re-checked here.
* **Not known**: could not be determined from the repository (usually depends on infrastructure that does not exist yet).

Related documents: `docs/DATA_LIFECYCLE.md` (the earlier SEC-13 proposal this document extends), `docs/SECURITY_ASSESSMENT_PHASE_E.md` (findings SEC-13 and SEC-20), `docs/PRIVACY.md`, `docs/DEPLOYMENT.md` section 8 (backups), `docs/DATABASE.md`.

How the evidence was gathered: source files were read (paths and lines are cited). For SEC-20 only, two empty throwaway databases (`ia_sec20_probe`, `ia_sec20_probe2`) were created on the local Docker PostgreSQL 16.15, the project's migrations 0001 to 0011 were applied to them with `alembic upgrade head`, test statements were run, and **both databases were dropped afterwards** (verified: no database named `ia_sec20%` remains). No other database was touched. Redis was only asked read-only questions (`CONFIG GET`, `INFO`, `DBSIZE`). No existing file was edited.

---

# PART A: SEC-13, data lifecycle and deletion

## A.0 What this means and what you must decide

Today, a user can delete a session, a resume or a document, and those disappear from the live database. Four things are not covered. (1) A copy of a generated answer can stay in the Redis cache for up to five minutes after the session is deleted, and a record of it can stay in Redis's own log file on disk for longer. (2) Backups keep deleted data until the backup is removed, and backups cannot be edited per user. (3) There is no way for a user to delete their whole account. (4) Nothing in the product states a deletion promise yet, so the privacy notice cannot honestly claim one.

You must decide five things: how strictly to purge Redis (A.1, A.2), how long backups live and whether to keep a small "deletion ledger" so deleted data is not brought back by a restore (A.3), whether and how to offer account deletion (A.4), and which plain-language promise the product will make (A.5). The decision form is at the end of Part A (A.6).

Two facts shape everything below. First, every user-owned database row is already removed when the user row is removed (verified, A.4.1), so account deletion in the database is cheap. Second, files on disk and Redis are **not** removed by a database delete, and backups are outside the application's reach entirely.

## A.1 Decision 1: session deletion and the Redis answer cache

### A.1.1 Current verified behaviour

* A session delete is a hard delete of the session row; questions, answers, preferences and document links go with it by cascade, and resumes and documents are untouched. `services/api/app/sessions/service.py:528-533` (`delete_session`) and `repository.py:33-40` (`delete_for_user`: `DELETE ... WHERE id AND user_id`). **Verified.** Route: `sessions/router.py:130-133`.
* Executed test (probe database, runtime role, tenant A): deleting a session removed its document links, questions and answers (0 left) and left the documents and resume in place. **Verified (executed).**
* `delete_session` makes **no call** to Redis or to the answer cache. **Verified** (the function above is the whole path).
* The cache key is `qa_general:` plus the SHA-256 of `user_id | session_id | normalised question | preferences digest | prompt version | sorted source ids`. `services/api/app/qa/cache.py:26,42-62`. **Verified.** The value is the JSON of the answer (it must contain a `text` field, `cache.py:90`), written with `SET key value EX ttl` (`cache.py:96-102`).
* TTL: `QA_ANSWER_CACHE_TTL_SECONDS`, default 300 s, allowed range 0 to 3600 s (`core/config.py:260`). **Verified.** A value of 0 disables the cache (`cache.py:72-74`).
* Because the key is a one-way hash, **the system cannot list or delete "all keys of session X"** today. Per-session purge is not possible with the current key design without adding an index. **Verified by reading the key function.**
* The cache is best effort: Redis errors are swallowed (`cache.py:76-102`). A purge call can follow the same rule (failure must not block deletion) or be made mandatory; that is a sub-choice below.
* Only validated general answers are cached; document-grounded answers are not (docstring `cache.py:1-14`). **Verified.**

### A.1.2 Options

| Option | What it is | User effect | Security effect | Operations effect | Compliance-claim effect |
|---|---|---|---|---|---|
| 1a. Keep as is, document the window | No code. Privacy notice says cached answers disappear within the cache lifetime (5 minutes by default). | None | Residue of a deleted session's general answers for at most the TTL | None | Can truthfully say "within 5 minutes", not "immediately". The Redis log file (A.2) is a longer residue unless Decision 2 also changes |
| 1b. Per-session index set, purge on delete | On every cache write also `SADD qa_idx:<user>:<session> <key>` and set the same expiry; on session delete read the set, `DEL` the keys and the set. | Same | Closes the live-keyspace residue at delete time (race below) | One extra Redis round trip per cache write (pipelined `SET`+`SADD`+`EXPIRE`) and one `SMEMBERS`+`DEL` per session delete. Memory: one 71-byte key string per cached answer, living no longer than the TTL. Negligible at the default TTL | Can truthfully say "removed immediately from the live service" for the live keyspace, **not** from the Redis log (A.2) or backups |
| 1c. Key prefix scheme | Change the key to `qa_general:<session_id>:<hash>` and find keys with `SCAN MATCH qa_general:<session_id>:*` on delete. | Same | Same as 1b. Exposes the session id in Redis key names (ids are not secret, but the current design deliberately hashes everything) | `SCAN` walks the whole keyspace in slices; cost grows with the number of keys, not the number of matches. Small at a 300 s TTL, unbounded if the TTL is raised. Not suitable on a shared Redis with large queues | Same as 1b |
| 1d. Disable the cache (TTL 0) | `QA_ANSWER_CACHE_TTL_SECONDS=0` | Slower and costlier for repeated generic questions (cost not measured: Not known) | Removes the residue entirely | None | Strongest and simplest claim; loses the D-66 Phase 4 feature |
| 1e. Epoch counter per session (not a purge) | Put a per-session counter in the key and bump it on delete. | Same | **Does not delete data**: old entries stay until expiry, only unreachable. Not acceptable for a deletion claim | Small | None: do not use this to claim deletion |

Race to design for in 1b: an answer being generated while the session is deleted can write its cache entry after the purge. Mitigations: skip the cache write when the session no longer exists (one extra read), or accept a residue bounded by the TTL and say so. The index entry shares the TTL, so nothing outlives it.

### A.1.3 Recommendation

**1b**, with the cache TTL left at its default and capped at 300 s in production. Reason: it is small, the cost on Redis is negligible, it makes "removed immediately from the live service" true, and the same index design extends to account deletion (A.4) by purging `qa_idx:<user>:*`. 1d is the right choice if the owner prefers the simplest possible statement and accepts the cost of losing the cache. 1a is acceptable only if the privacy notice is worded as in A.5 set 1.

### A.1.4 What would be built if approved, and the proof

* `AnswerCache.put` records the key in a per-session index set with the same expiry; a new `AnswerCache.purge_session(user_id, session_id)`; `service.delete_session` calls it after the database commit (failure logged by id, never blocks the delete); small change to `qa/cache.py` and `sessions/service.py`; `docs/DATABASE.md`/`PRIVACY.md` updated.
* Test: with a fake Redis client (the cache already takes a protocol, `cache.py:30-36`), cache two answers for session S1 and one for S2, delete S1, assert S1's keys and index are gone and S2's remain; assert a Redis error during purge does not fail the delete; assert the key and index contain no question text; user-isolation test: user B deleting their session never removes user A's keys.
* Effort estimate (Not measured): about half a day.

## A.2 Decision 2: Redis persistence (append-only file)

### A.2.1 What Redis holds today (every use of the Redis client was searched)

| Data | Key pattern | Lifetime | Holds personal content? | Source |
|---|---|---|---|---|
| Answer cache | `qa_general:<sha256>` | 300 s default, 3600 s max | **Yes**: the answer text | `qa/cache.py:26,100` (**Verified**) |
| WebSocket tickets | `ws_ticket:<token>` | 30 s default (`config.py:283`) | No: value binds user and session ids | `realtime/tickets.py:18,67` (**Verified**) |
| Web-to-desktop handoff codes | `handoff:<token>` | 60 s default (`config.py:286`) | No: ids only | `main.py:183-185` (**Verified**) |
| Session-create idempotency | `idem:session:<sha256(user:key)>` | 24 h (`idempotency.py:23`) | No: value is a session id | `sessions/idempotency.py:22-23,75` (**Verified**) |
| Worker heartbeat | `ia:worker:heartbeat:*` | seconds | No | `workers/heartbeat.py:23` (**Verified**) |
| Dramatiq queue (ingestion jobs) | broker keys | until processed | No content: the message carries `job_id` and `user_id` only | `workers/tasks.py:51` (`ingest_job(job_id, user_id)`), `workers/broker.py` (**Verified**) |
| Rate limits (questions, audio, auth, job import, handoff, ws tickets) | **not in Redis**: in-process memory | process life | No | `qa/limits.py:3` and `jobs_import/limiter.py:1-5` say "no Redis"; no other file in `app/` creates a Redis key (**Verified by search**) |

Persistence as configured: development `docker-compose.yml:27` runs `redis-server --appendonly yes`; production `docker-compose.prod.yml:205-207` runs `--appendonly yes` with TLS and a password. **Verified.** The running dev Redis reports `appendonly yes`, `appendfsync everysec`, auto-rewrite at 100 percent and 64 MiB minimum, **and RDB snapshotting also enabled** (`save 3600 1 300 100 60 10000`). **Verified (read-only query).** This means switching the append-only file off alone would **not** stop answer text reaching disk: the snapshot file would still contain it. The production Redis uses the same image defaults (`redis:7-alpine`), but its effective `save` setting was **not verified** (no production instance exists).

How the residue arises (Reported from Redis behaviour, not executed here): every `SET` is appended to the log with its value; an expiry or `DEL` appends a further command but does not erase the earlier line; the old line disappears only when the log is rewritten, which Redis does automatically only when the file has doubled in size and exceeded 64 MiB. With a few small cache entries per day the file may never reach that size, so the residue can last indefinitely.

### A.2.2 What actually needs to survive a Redis restart

* Answer cache: nothing (re-creatable; best effort by design). **Verified** (`cache.py` docstring).
* Tickets and handoff codes: nothing (30 to 60 s; clients re-request). **Verified by TTLs.**
* Heartbeats: nothing.
* Idempotency keys: loss means a retried "create session" within 24 h after a Redis restart could create a duplicate session (the store already reports `unavailable` and proceeds when Redis is down, `idempotency.py:41`). Low harm. **Verified by reading.**
* Dramatiq queue: loss means queued ingestion messages vanish. The database row stays `pending`, and the recovery sweep re-dispatches jobs pending longer than `ingestion_pending_redispatch_seconds` (`ingestion/dispatch.py:3`, `ingestion/service.py:531-544`, **Verified**). **But** `docs/DEPLOYMENT.md` line 126 lists "scheduled backups and recovery sweep" as **not done**, so today the sweep is an operator task, not a running service. **Reported.**

### A.2.3 Options

| Option | Change | Effect on residue | Operations / reliability | Compliance-claim effect |
|---|---|---|---|---|
| 2a. Keep AOF and RDB, document | none | Answer text can stay on the Redis volume until a rewrite happens; unbounded in practice | None | Must state "cached answers may remain in a technical log for an unspecified time" |
| 2b. Turn both off on the single Redis | `--appendonly no --save ""` | No persistence of any Redis content; a restart clears everything | A restart loses queued ingestion messages (recovered only if the sweep runs) and idempotency keys. Requires scheduling the recovery sweep first | Strongest simple claim for Redis |
| 2c. Two Redis instances | One without persistence (cache, tickets, handoff, idempotency, heartbeat); one with AOF for the Dramatiq queue (ids only, no content) | No content ever reaches disk; queue survives restarts | One more container, secret and TLS certificate in production; two connection settings (`REDIS_URL` plus a broker URL: a config change). New infrastructure: needs owner approval under CLAUDE.md rule 4 | Strongest claim with no reliability loss |
| 2d. Keep AOF, force an expiry-aware rewrite | Lower `auto-aof-rewrite-min-size`/percentage, or run `BGREWRITEAOF` on a schedule (for example hourly), and disable RDB | Residue bounded by the rewrite interval (rewrite omits expired and deleted keys). The old file is deleted by the rewrite but its blocks are not scrubbed from the disk | Rewrite costs a short background fork and disk write; scheduling is a new operator duty | Can state "removed from Redis's log within about N hours", not "immediately". Not a guarantee against disk forensics |
| 2e. Keep AOF but stop caching content | Combine with 1d (TTL 0) | No content in Redis at all, so the log holds only ids | Cost of 1d | Simple and strong |

### A.2.4 Recommendation

**2b**, but only after the recovery sweep is scheduled (otherwise queued ingestion jobs are silently delayed after a Redis restart). Reason: nothing in Redis needs to survive a restart except the job queue, and the queue is already backed by a database source of truth with a recovery mechanism. 2c is the better engineering answer if the owner accepts a second Redis; 2d is the fallback if the owner wants persistence kept. Whatever is chosen, **RDB must be addressed together with AOF**.

### A.2.5 What would be built if approved, and the proof

* Compose and production compose: change the `redis-server` arguments (and, for 2c, a second service); operations runbook entry for scheduling the sweep; `DEPLOYMENT.md`/`PRIVACY.md` updates.
* Proof: start the changed Redis, write a cache entry containing a known marker string, run `redis-cli CONFIG GET appendonly`/`save`, restart the container, assert the marker is absent from the keyspace and (for 2b/2c) that no `appendonly.aof*` or `dump.rdb` file under the data volume contains the marker (`grep` over the volume). For 2d: write marker, delete the key, trigger `BGREWRITEAOF`, assert the marker is gone from the file set. Ingestion test: stop Redis with a pending job, restart, run the sweep, assert the job completes.
* Effort estimate (Not measured): 2b about half a day plus scheduling the sweep; 2c about one day; 2d about half a day.

## A.3 Decision 3: backup retention, expiry and "deletion replay after restore"

### A.3.1 Current verified behaviour

* `backup.sh` writes an encrypted, authenticated set: database dump, role definitions, uploads archive, manifest. A backup cannot be edited per user: it is one encrypted archive of the whole database and the whole uploads tree. `docs/DEPLOYMENT.md:94` (**Reported** for the description; the scripts exist in `infra/backup/`, **Verified** present).
* Retention is `infra/backup/prune.sh`: `RETENTION_DAYS` (required, whole number of at least 1) removes backups older than the cutoff, **but the newest `MIN_KEEP` (default 3) are always kept regardless of age**. `prune.sh:3-9,17,31-33`. It runs only as a dry run unless `--apply`, and nothing schedules it (**Reported**, `DEPLOYMENT.md:126`). **Verified** for the script behaviour.
* Consequence of `MIN_KEEP`: if backups stop being made (a failed schedule), the last three can persist **beyond** the stated retention, so a stated retention period is not guaranteed by the script alone. **Verified by reading.**
* `restore.sh` refuses a non-empty database or uploads location and checks schema version and row counts against the manifest (`DEPLOYMENT.md:94`, **Reported**; `restore.sh:50` runs `pg_restore`, **Verified**). It has **no deletion-aware step**.
* `docs/PRIVACY.md:36-37` already says backups retain deleted data until expiry and that a restore "must not silently restore deleted records into a live account". Today nothing implements the second half. **Verified (document and scripts).**
* No backup of production exists and no off-host copy exists (**Reported**, `DEPLOYMENT.md:126`). The retention period is a proposal, not something currently running.

### A.3.2 Options: retention period

| Option | Effect |
|---|---|
| 3a. 14 days (daily backups, 14 kept) | Deleted data gone from backups within 14 days. Lowest recovery depth: a problem discovered after 14 days cannot be rolled back |
| 3b. 35 days (7 daily + 4 weekly, as proposed in `DATA_LIFECYCLE.md`; note `prune.sh` is purely age-based, so a daily-plus-weekly scheme needs a script change, while a flat 35 days does not) | Reasonable recovery depth; "within 35 days" is the statement |
| 3c. 90 days | Deep recovery; weakest deletion statement; larger storage |
| 3d. No fixed period (current default state) | No statement possible. Not recommended |

### A.3.3 Options: deletion replay

Problem: if production is restored from a backup taken before a user deleted something, the deleted data comes back. Backups cannot be edited, so the fix must happen after the restore.

| Option | What it is | Consequences |
|---|---|---|
| 3-R1. Procedure only | Operators keep a manual record of deletion requests and re-delete after a restore | No code. Depends on a person remembering; the record is easy to lose; no evidence it was done |
| 3-R2. Deletion ledger (recommended) | A small append-only list written at deletion time, kept outside the database that gets restored, and re-applied by a script after every restore and before traffic is allowed | Small build; gives an auditable guarantee that a restore does not resurrect deleted data inside the ledger's retention window |
| 3-R3. Per-user encryption keys ("crypto-shredding") | Encrypt user text with a per-user key; deleting the key makes every copy, including backups, unreadable | Strongest and covers backups completely, but a major redesign of storage, search (embeddings and full-text index operate on plaintext), key management and backups. Not proportionate now; listed so the owner knows the alternative exists |
| 3-R4. Very short backup retention only (for example 7 days) | No replay; the window is the guarantee | Cheapest; sacrifices recovery depth |

### A.3.4 Design of the ledger (3-R2), and why it must not store personal content

What it stores, one row per deletion: `entry_id` (random UUID), `kind` (`account`, `session`, `resume`, `document`), `subject_id` (the UUID of the deleted object), `owner_user_id` (UUID), `deleted_at` (UTC timestamp), `applied_after_restore_at` (null until replayed). **Nothing else.** In particular: no email, name, file name, company name, job text, question or answer text, storage path, IP address or user agent; no hash of an email (a hash of an email is still personal data and can be reversed by guessing). All stored values are random identifiers that mean nothing without the deleted data. A schema test should enforce the column list.

Where it lives: it must **not** live only in the application database, because restoring an old backup would also restore an old ledger and erase the newer entries. It needs a store that a restore does not overwrite: an append-only file on a separate volume or location, copied off-host with the backups (this location **Not known**: infrastructure not chosen). It may also be mirrored in a database table for convenience. The ledger file is written in the same request that performs the deletion, after the database commit succeeds; if the ledger write fails the deletion is still successful but an alert is raised (a ledger gap is a risk to report, not a reason to refuse a user's deletion).

How it is applied: after `restore.sh` completes and before the application starts, a script reads the ledger and, for each entry newer than the backup's own timestamp, deletes the object by id: account (delete the user row, cascade as in A.4, and remove `uploads/<owner_user_id>/`), session (delete by id), resume or document (read `storage_key` from the restored row, delete the file, then the row, with dependent ingestion rows as in `ingestion/repository.py:63-92`). It is idempotent: an entry whose object no longer exists is marked applied. It logs ids and counts only.

Ledger retention: entries are kept for the backup retention period plus a safety margin (for example retention plus 7 days), then removed, so the ledger does not become a permanent record. The privacy notice should say that a deletion is recorded by an anonymous identifier for this period.

Residual limits: a backup older than the ledger window must not be restored without owner review; a ledger that was lost cannot replay (operations must back it up with the same care as backups); the ledger cannot remove data from a backup, only from the restored system.

### A.3.5 Recommendation

Retention **3b with a flat 35 days** (or 3a 14 days if recovery depth is not a priority), **plus 3-R2**. Reason: it gives a bounded, checkable statement ("gone from backups within 35 days; never restored back") at the cost of a small script and an off-host file. Also fix `MIN_KEEP` so it cannot extend retention indefinitely (for example a hard maximum age, after which a backup is removed even if it is among the newest, and an alert if no backup is newer than 48 hours).

### A.3.6 What would be built if approved, and the proof

* A `deletion_ledger` writer in the API (called from the delete paths in A.1, A.4 and `files/service.py:206-236`), a `replay_deletions.sh`/Python script under `infra/backup/`, `restore.sh` documentation ("run the replay before starting the API"), and a change to `prune.sh` for a maximum age.
* Test (restore drill, extending the existing drill in `DEPLOYMENT.md` section 8): create user U with a resume, a document, a session; take a backup; delete the session, the document and finally the account (ledger entries written); restore the old backup into an empty cluster; assert the objects are back; run the replay; assert user, files and rows are gone and that other users' data is intact. Unit test: ledger rows contain only the five allowed fields (column-list assertion) and no row contains an email or text. Test that a failed ledger write does not fail the delete.
* Effort estimate (Not measured): about 2 days including the drill.

## A.4 Decision 4: account deletion

### A.4.1 Current verified behaviour

There is **no** account deletion route. Auth routes are `POST /signup`, `/login`, two token routes and `GET /me` (`auth/router.py:53,68,91,108,129`). **Verified.**

**Database cascade, verified from the generated schema (migrations 0001-0011 applied to a throwaway database) and the models.** Every user-owned table has a foreign key to `users(id)` with `ON DELETE CASCADE`:

| Table | Cascade path to the user | Verified how |
|---|---|---|
| `refresh_tokens` | `fk_refresh_tokens_user_id_users` CASCADE | schema dump |
| `resumes` | `fk_resumes_user_id_users` CASCADE | schema dump |
| `documents` | `fk_documents_user_id_users` CASCADE | schema dump |
| `ingestion_jobs` | `fk_ingestion_jobs_user_id_users` CASCADE | schema dump |
| `extracted_texts` | `fk_extracted_texts_user_id_users` CASCADE | schema dump |
| `chunks` | `fk_chunks_user_id_users` CASCADE | schema dump |
| `chunk_embeddings` | `fk_chunk_embeddings_user_id_users` CASCADE (and via chunks) | schema dump |
| `sessions` | `fk_sessions_user_id_users` CASCADE | schema dump |
| `session_documents` | `fk_session_documents_user_id_users` CASCADE (and via sessions, documents) | schema dump |
| `session_preferences` | **no `user_id` column**; removed through `session_id -> sessions` CASCADE | schema dump; executed |
| `detected_questions` | `fk_detected_questions_user_id_users` CASCADE | schema dump |
| `generated_answers` | `fk_generated_answers_user_id_users` CASCADE | schema dump |

That is 12 user-owned tables plus `users` itself (13 tables under row-level security, matching migration 0008 `USER_TABLES`). Executed on the throwaway database: `DELETE FROM users WHERE id = <tenant A>` left 0 rows for A in resumes, documents, sessions, session_preferences, chunks, chunk_embeddings and generated_answers (executed as the database owner/superuser, rolled back). **Not executed as the runtime role**: the runtime role has DELETE privilege on all tables (migration 0008) and the `users` policy allows `id = tenant`, but a runtime-role account delete was not tried; the test in A.4.6 must prove it.

**Not removed by the database cascade (must be handled explicitly):**

| Location | What remains | Evidence |
|---|---|---|
| Uploaded files on disk | `<root>/uploads/<user_id>/<namespace>/<hex>` for every resume and document, plus any `<root>/tmp/<hex>.part` from an interrupted upload | `storage/local.py` layout (lines 3-6, 95). **Verified.** `FileStorage` has only a per-key `delete(key)` (`storage/base.py:77-78`): **there is no per-user delete**, so a new method is needed |
| Redis | cached answers (hashed keys, not enumerable per user today), idempotency keys (24 h, hashed) | A.1, A.2. Purgeable by user only if the index of 1b exists, else expire by TTL |
| Process memory in the API | rate-limiter entries keyed by user id; WebSocket resume buffers and detached generations for that user's sessions | `jobs_import/limiter.py`, `main.py:173-200`. **Verified** to be in-process. Buffers are dropped at session end or restart; the socket is closed with `account_inactive` when the user is disabled (`docs/API.md:115`, **Reported**) |
| Application logs | user and object ids (never content) | `PRIVACY.md`, `DATA_LIFECYCLE.md` (**Reported**); log rotation 20 MB x 5 (**Reported**) |
| Backups | everything, until expiry | A.3 |
| Exports saved by the user | files on the user's own disk | `PRIVACY.md:23-30` (**Verified**) |
| Desktop device | the desktop app's local settings and any stored credentials | **Not known** in detail (not inspected here); the desktop signs out on a failed token refresh (**Reported**, `DATA_LIFECYCLE.md`) |
| Model provider | whatever the provider keeps under its own terms | open question OQ-4 (**Reported**) |
| An `audit_events` table | **does not exist**: `docs/DATABASE.md:134` describes it, but it is in no migration or model (**Verified** by search of `alembic/` and `app/`). An "audit line" for deletion is therefore a log line or the ledger (A.3), not a table |

Other verified facts: every authenticated request re-reads the user and rejects a missing or non-`active` user (`auth/deps.py:55`), and the access token lasts 15 minutes by default (`config.py:96`), so a disabled or deleted account loses access on its next request, not after 15 minutes. Refresh tokens last 14 days (`config.py:97`) and are deleted with the user row. `users.email` is unique (`uq_users_email`), so a hard delete frees the email address for re-registration. Whether sign-up is open to anyone is **Not known** (the route exists; invitation control was not checked).

### A.4.2 Options

| Option | What it is | User effect | Security effect | Operations effect | Compliance-claim effect |
|---|---|---|---|---|---|
| 4a. Immediate hard delete | User confirms with password (and a typed phrase); the server disables, removes files, deletes the user row (cascade), purges Redis, writes the ledger entry | Instant and final; no undo. Mitigation: warn, and point to the existing per-session export | Smallest window in which data exists after the request | Needs no scheduler. One transaction plus file removal | Simplest honest claim: "deleted immediately from the live service" |
| 4b. Grace period (for example 7 or 14 days) | The account is disabled at once (status other than `active`, which already blocks all access, `deps.py:55`) and a purge job hard-deletes after N days; sign-in during the period can cancel | Safety net against accidents | Data stays for N more days, in a disabled account | **Needs a scheduler that does not exist** (the recovery sweep is itself not scheduled), plus a restricted login-to-cancel path (today non-`active` users cannot log in, `auth/service.py:90`), plus a new status value or column and migration | The claim must say "within N days" |
| 4c. Operator-initiated deletion only | An administrator runs a maintenance command on a user's behalf (uses the separate maintenance credential, `MAINTENANCE_DATABASE_URL`; **Reported** in `DATABASE.md`) | Users must ask someone | Needs an identity-verification procedure (who may ask) | Manual work, auditable | Can state a response time, not an instant |
| 4d. Combination (4a or 4b for users plus 4c for operators) | Same code path, two triggers | Both | Operator path needs an access log | Same code | Both statements |

Order of operations (for any option that deletes), designed to be retryable: (1) set the user non-active so every request and socket stops; (2) revoke refresh tokens (they go with the row); (3) delete the user row in one database transaction (cascade removes all 12 tables); (4) remove `uploads/<user_id>/` and any stale `tmp` parts by prefix (retryable from the user id alone, so a failure after step 3 leaves nothing orphaned that cannot be removed later); (5) purge Redis keys (needs the index of 1b, otherwise TTL); (6) write the ledger entry; (7) log id and time only (no email). The existing single-file delete removes the file before the row (`files/service.py:216-235`); for the account the order is deliberately the reverse, because the file path is derivable from the user id and does not need the row.

Re-registration and abuse: after a hard delete the same email can register again as a new account with no history. Options: (i) accept it (internal-use product; and email verification is deferred, D-24, **Reported**); (ii) for accounts an operator suspended for abuse, keep the account row disabled rather than deleting it (a retained identity, which must be stated in the privacy notice and is the owner's call); (iii) keep a blocklist entry (a salted hash of the email is still personal data). Recommendation: (i), plus (ii) only if an abuse case arises, as a documented operator decision.

Desktop: the next call after deletion gets 401 (user missing), the refresh fails, and the desktop signs out (**Reported**). An open live session socket is closed (`account_inactive`, **Reported**). The desktop's local files were not inspected (**Not known**); the desktop checklist should include "after account deletion, no credentials or session data remain on the device".

### A.4.3 Recommendation

**4d with 4a as the user path** (immediate hard delete after password confirmation, a typed confirmation phrase, and a clear warning that backups keep data until the retention period and that exports on the user's disk are theirs to delete) and operator deletion for support requests. Reason: it needs no scheduler or new status value, gives the simplest and most honest statement, and matches the fact that interview preparation material is something a user wants gone when they say so. A grace period (4b) is the right choice if the owner values accident protection over the simplest statement and accepts building a scheduler; it should then be at most 7 days.

### A.4.4 What would be built if approved

`DELETE /account` (or `POST /account/delete`) in `auth/`, `API.md` first (contract-first); a `FileStorage.delete_user_tree(user_id)` method and its local implementation; an account-deletion service as in the order above; Redis purge (depends on 1b); ledger writer (A.3); desktop handling already exists (confirm in test); web page for the action with the warning text; `PRIVACY.md` and `DATA_LIFECYCLE.md` updated. No migration is needed for 4a (cascades already exist). 4b would need a migration.

### A.4.5 Effort estimate (Not measured)

4a: about 2 days including web page and tests; 4b: about 4 days; operator command: about half a day.

### A.4.6 Proof (tests)

* Isolation: two users each with resume, document (files on disk), embedded chunks, sessions, questions, answers, refresh tokens, cache entries; delete user A through the endpoint as the runtime role; assert for A: zero rows in all 12 tables, no files under `uploads/<A>/`, no `tmp` parts, no Redis keys in the index, login and refresh fail, an open WebSocket is closed; assert for B: every row, file and key unchanged.
* Wrong password and missing confirmation phrase: nothing deleted. Another user's token cannot delete A. Repeating the call is safe.
* Failure injection: file removal fails after the database commit: the account is gone, the entry is queued for retry by prefix, nothing refers to the missing files.
* A scan test that fails when a new table with a `user_id` column is added without a cascade to `users` (prevents a future table escaping deletion).

## A.5 Overall lifecycle and the deletion guarantees the product can honestly state

### A.5.1 Which guarantee each combination supports

| What the user deletes | Gone from live database | Gone from Redis | Gone from Redis disk files | Gone from backups | Notes |
|---|---|---|---|---|---|
| Session (today) | immediately | within the cache TTL (5 minutes) | not guaranteed | at expiry | what can be said truthfully today |
| Session (1b + 2b) | immediately | immediately | nothing was written | within the backup period; not restored back (3-R2) | |
| Resume or document | immediately (file, text, chunks, embeddings: `files/service.py:206-236`, `ingestion/repository.py:63-92`, **Verified**) | n/a (not cached) | n/a | at expiry | the existing path already does this |
| Account (4a) | immediately | immediately (with 1b) | nothing written (with 2b/2c) | within the backup period; not restored back (3-R2) | files on the user's disk, provider copies and logs of ids excepted |

Never state: "permanently erased everywhere"; "no copies exist"; anything about model-provider retention (Not known, OQ-4); any statutory term such as "GDPR compliant" or "DPDP compliant" (legal review needed; the project documents already say this is not legal advice).

### A.5.2 DRAFT privacy-notice wording (NOT APPROVED, for owner and legal review)

**Option set 1: minimal (keep Redis as is, no account deletion, no ledger; 1a + 2a + 3d/3b + no account deletion)**

> DRAFT, NOT APPROVED. When you delete a session, a resume or a document, it is removed from our live database straight away. Answers we generated for a session may stay in a short-lived cache for up to five minutes, and a technical log used to restart our cache may keep a copy for longer; we do not control exactly when that log is cleaned up. Backups keep deleted information until the backup expires, which is [N] days. We cannot yet delete a whole account on request through the app; contact [Operator contact] and we will handle it manually. Files you exported to your own computer are yours to delete.

**Option set 2: recommended (1b + 2b or 2c + 3b with ledger + 4a/4d)**

> DRAFT, NOT APPROVED. When you delete a session, a resume or a document, it is removed from our live service immediately, including any short-lived cached answers. You can delete your whole account from [Account settings]; this immediately removes your account, your files, your sessions, questions and answers, and your sign-in sessions from the live service. For safety we keep encrypted backups for [35] days. Deleted information remains in those backups until they expire, but it is not brought back if we restore from a backup: we keep a short list of anonymous identifiers of deleted items for that purpose. Files you exported to your own computer, and anything an AI service provider keeps under its own terms, are outside our control. [Provider retention statement: to be added once OQ-4 is resolved.]

**Option set 3: strict (set 2 plus short retention 14 days, cache disabled, grace period not used)**

> DRAFT, NOT APPROVED. Deleting a session, a resume, a document or your account removes it from our live service immediately. We do not cache your answers. Encrypted backups are kept for [14] days and deleted information is not restored from them. After [14] days no copy remains in our systems. Files you exported to your own computer, and anything an AI service provider keeps under its own terms, are outside our control. [Provider statement pending OQ-4.]

Caution on set 3: "no copy remains in our systems" is only true if logs, the Redis volume and every off-host backup copy follow the same period, which cannot be confirmed until the production infrastructure exists (Not known).

## A.6 Decision form for Part A (owner to fill in)

Tick one option per row. "Rec." marks the recommendation. Nothing is built until the matching row is ticked and signed.

| # | Decision | Option | Rec. | Approve | Approve with change (write change) | Defer |
|---|---|---|---|---|---|---|
| 1 | Session delete and Redis answer cache | 1a keep, document the window | | [ ] | | [ ] |
| 1 | | 1b per-session index set, purge on delete | **Rec.** | [ ] | | [ ] |
| 1 | | 1c key-prefix scheme with SCAN | | [ ] | | [ ] |
| 1 | | 1d disable the cache | | [ ] | | [ ] |
| 2 | Redis persistence | 2a keep AOF and RDB, document | | [ ] | | [ ] |
| 2 | | 2b turn both off (after scheduling the recovery sweep) | **Rec.** | [ ] | | [ ] |
| 2 | | 2c two Redis instances | | [ ] | | [ ] |
| 2 | | 2d keep AOF, scheduled rewrite, RDB off | | [ ] | | [ ] |
| 3 | Backup retention | 3a 14 days / 3b 35 days / 3c 90 days (circle one) | 35 | [ ] | days = ____ | [ ] |
| 3 | Deletion replay after restore | 3-R1 manual procedure | | [ ] | | [ ] |
| 3 | | 3-R2 deletion ledger (content-free, kept outside the restored database) | **Rec.** | [ ] | | [ ] |
| 3 | | 3-R4 short retention only | | [ ] | | [ ] |
| 3 | Ledger location (needs an infrastructure answer) | file path / off-host location: ______________ | | [ ] | | [ ] |
| 3 | Backup script hardening (maximum age overrides `MIN_KEEP`, stale-backup alert) | yes / no | yes | [ ] | | [ ] |
| 4 | Account deletion | 4a immediate hard delete | **Rec.** | [ ] | | [ ] |
| 4 | | 4b grace period of ____ days (needs scheduler and migration) | | [ ] | | [ ] |
| 4 | | 4c operator-initiated only | | [ ] | | [ ] |
| 4 | | 4d user path plus operator path | **Rec.** (with 4a) | [ ] | | [ ] |
| 4 | Abuse and re-registration | (i) accept / (ii) keep disabled row for suspended accounts | (i) | [ ] | | [ ] |
| 5 | Privacy-notice wording | Set 1 / Set 2 / Set 3 (circle one; legal review before publishing) | Set 2 | [ ] | | [ ] |
| 5 | Retention limits (from `DATA_LIFECYCLE.md` 3.3) | A keep and say so / B "delete older than N days" action / C automatic expiry | A now, B later | [ ] | | [ ] |

Owner: ____________________   Date: ____________   Signature: ____________________

---

# PART B: SEC-20, composite foreign keys so that row-level security cannot be bypassed by a cross-tenant reference

## B.0 What this means and what you must decide

The database already hides one user's rows from another user (row-level security, migration 0008). One gap remains: a row owned by user A can be **pointed at** a row owned by user B, because the link between the two rows only checks that the target exists, not that it has the same owner. Today only application code prevents that. The application check is sound and no cross-tenant access has been found, so this is a defence-in-depth hardening, not an active leak. The fix is to make the database itself refuse such a link, by changing seven foreign keys so that they include the owner column.

This change touches the schema of live tables and cannot be undone casually in production, so it needs your approval. The plan below uses the safe PostgreSQL pattern: add the new rule without checking old rows, check old rows separately, then remove the old rule. The risk is that existing data might already contain a cross-tenant reference, in which case the check would fail; the read-only queries in B.5 find any such rows before anything is changed. **On the throwaway database, the plan was executed end to end and worked. Nothing has been run against the development, test or any real database. Nothing will be executed before you approve.**

Headline numbers: **7 foreign keys** to be replaced by composite keys; **5 new unique constraints** on referenced tables; **1 foreign key needs no change** (session_preferences); **3 polymorphic references** cannot be foreign keys at all.

## B.1 Facts about the project and the database version

* PostgreSQL target: `pgvector/pgvector:pg16` in compose (**Reported** by the task; compose files list the Postgres image; the dev server executed was `PostgreSQL 16.15`, **Verified (executed)**). The version matters: `ON DELETE SET NULL (column_list)` exists only from PostgreSQL 15, so it is available on 16 and must not be used if production is ever older than 15 (**Not known** for production: no production database exists).
* Row-level security: migration `0008_tenant_rls.py` enables and forces policies on 13 tables; policies compare `user_id` to `app.user_id` (**Verified**, file read). The runtime role `interview_app_runtime` is `NOLOGIN NOSUPERUSER NOBYPASSRLS` (**Verified**, executed: role attributes queried). Foreign-key checks run inside PostgreSQL's referential-integrity triggers and are **not** subject to row-level security (Reported PostgreSQL behaviour, and **Verified (executed)** by the "before" test below: the cross-tenant inserts succeeded as the runtime role).
* The ORM (`app/db/models.py`) has **no `relationship()`** definitions (**Verified**: 0 matches), and the naming convention is `fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s` / `uq_%(table_name)s_%(column_0_name)s` (`db/base.py`, **Verified**).
* `docs/DATABASE.md` section 3 states the design intent "Child rows must match parent's user_id (composite FK `(id, user_id)` references or trigger)". The implementation never did this (**Verified**: the generated schema contains only single-column FKs). The same document should be corrected when this is implemented.

## B.2 Every foreign key examined (verified from the generated schema of migrations 0001-0011 and from the models)

The database has 19 foreign keys in total (Verified, catalog query). 11 of them go to `users(id)` from a table that has `user_id`, and are correct as they are (the child's own `user_id` is the tenant). The other 8 point at another user-owned table (rows 1 to 8 below); row 9 only records the 11 user keys:

| # | Referencing column | Current constraint name | References | ON DELETE | Verdict |
|---|---|---|---|---|---|
| 1 | `sessions.resume_id` | `fk_sessions_resume_id_resumes` | `resumes(id)` | SET NULL | **Replace** (composite, column-list SET NULL) |
| 2 | `session_documents.session_id` | `fk_session_documents_session_id_sessions` | `sessions(id)` | CASCADE | **Replace** |
| 3 | `session_documents.document_id` | `fk_session_documents_document_id_documents` | `documents(id)` | CASCADE | **Replace** |
| 4 | `detected_questions.session_id` | `fk_detected_questions_session_id_sessions` | `sessions(id)` | CASCADE | **Replace** |
| 5 | `generated_answers.question_id` | `fk_generated_answers_question_id_detected_questions` | `detected_questions(id)` | CASCADE | **Replace** |
| 6 | `generated_answers.session_id` | `fk_generated_answers_session_id_sessions` | `sessions(id)` | CASCADE | **Replace** |
| 7 | `chunk_embeddings.chunk_id` | `fk_chunk_embeddings_chunk_id_chunks` | `chunks(id)` | CASCADE | **Replace** |
| 8 | `session_preferences.session_id` | `fk_session_preferences_session_id_sessions` | `sessions(id)` | CASCADE | **No change**: the table has no `user_id` column (**Verified**: models.py:338-363 and the table definition). It has no tenant of its own, so it cannot hold a "wrong" tenant; its row-level-security policy already requires the owning session to belong to the current user (`0008_tenant_rls.py`, EXISTS subquery). Executed: inserting a preferences row for another tenant's session as the runtime role was refused by row-level security both before and after the migration (T9). Adding a `user_id` column here is possible but is a larger change with no added protection; not recommended |
| 9 | the `user_id` references to `users(id)` (11 constraints) | `fk_<table>_user_id_users` | `users(id)` | CASCADE | **No change** |

**Polymorphic references that cannot be foreign keys (no constraint exists today; verified):**

| Table.column | Why | Existing protection | Cannot be composite because |
|---|---|---|---|
| `ingestion_jobs.source_id` (with `source_type`) | points at `resumes` or `documents` depending on `source_type` (`models.py:126-170`) | application code; explicit cleanup in `ingestion/repository.py:63-92`; unique `(source_type, source_id, stage)` | one column cannot reference two tables |
| `extracted_texts.source_id` | same | same | same |
| `chunks.source_id` | same | same | same |

Possible future hardening for these (not part of this proposal): replace each polymorphic pair with two nullable columns, `resume_id` and `document_id`, each with a composite foreign key and a check that exactly one is set; or a trigger that verifies the owner. This is a larger redesign (the unique constraints and queries use `(source_type, source_id)`) and should be a separate decision. The read-only query D8 in B.5 shows whether any existing row already violates the intent (0 rows on the throwaway database).

Not a tenant issue but found while checking: `generated_answers` carries both `question_id` and `session_id`, and nothing makes them consistent (an answer could name a question of a different session of the same user). Query D6b detects it. It is out of scope here; mention only.

## B.3 The replacement constraints (SQL, with executed/proposed status)

**Status of the SQL below: every statement in this section was EXECUTED on the throwaway database `ia_sec20_probe` (PostgreSQL 16.15) except where marked PROPOSED.** The throwaway database is now dropped. The statements were run through `psql`, **not** through an Alembic migration file: the Alembic migration itself (including `autocommit_block`) is PROPOSED and was not written or run.

### B.3.1 Unique constraints needed on the referenced tables (5)

A foreign key needs a unique key on the columns it references, and `(user_id, id)` is not unique-constrained today (only `id` is the primary key). `id` is already unique, so `(user_id, id)` is trivially unique and can never fail.

| Table | New constraint (and index) name | Columns |
|---|---|---|
| `resumes` | `uq_resumes_user_id_id` | `(user_id, id)` |
| `documents` | `uq_documents_user_id_id` | `(user_id, id)` |
| `sessions` | `uq_sessions_user_id_id` | `(user_id, id)` |
| `detected_questions` | `uq_detected_questions_user_id_id` | `(user_id, id)` |
| `chunks` | `uq_chunks_user_id_id` | `(user_id, id)` |

```sql
-- EXECUTED (throwaway). Each CREATE UNIQUE INDEX CONCURRENTLY must run outside a transaction.
CREATE UNIQUE INDEX CONCURRENTLY uq_resumes_user_id_id ON resumes (user_id, id);
CREATE UNIQUE INDEX CONCURRENTLY uq_documents_user_id_id ON documents (user_id, id);
CREATE UNIQUE INDEX CONCURRENTLY uq_sessions_user_id_id ON sessions (user_id, id);
CREATE UNIQUE INDEX CONCURRENTLY uq_detected_questions_user_id_id ON detected_questions (user_id, id);
CREATE UNIQUE INDEX CONCURRENTLY uq_chunks_user_id_id ON chunks (user_id, id);
ALTER TABLE resumes ADD CONSTRAINT uq_resumes_user_id_id UNIQUE USING INDEX uq_resumes_user_id_id;
-- ... the same ADD CONSTRAINT ... UNIQUE USING INDEX for documents, sessions, detected_questions, chunks (all executed)
```

### B.3.2 The 7 composite foreign keys

Names below are the ones executed; in the real migration use explicit `name=` in the models, or the project's convention names (which for composite keys derive from the first column and produce `fk_sessions_user_id_resumes` and so on; either works if models and migration agree).

| # | Replaces | New constraint |
|---|---|---|
| 1 | `fk_sessions_resume_id_resumes` | `fk_sessions_tenant_resume FOREIGN KEY (user_id, resume_id) REFERENCES resumes (user_id, id) ON DELETE SET NULL (resume_id)` |
| 2 | `fk_session_documents_session_id_sessions` | `fk_session_documents_tenant_session FOREIGN KEY (user_id, session_id) REFERENCES sessions (user_id, id) ON DELETE CASCADE` |
| 3 | `fk_session_documents_document_id_documents` | `fk_session_documents_tenant_document FOREIGN KEY (user_id, document_id) REFERENCES documents (user_id, id) ON DELETE CASCADE` |
| 4 | `fk_detected_questions_session_id_sessions` | `fk_detected_questions_tenant_session FOREIGN KEY (user_id, session_id) REFERENCES sessions (user_id, id) ON DELETE CASCADE` |
| 5 | `fk_generated_answers_question_id_detected_questions` | `fk_generated_answers_tenant_question FOREIGN KEY (user_id, question_id) REFERENCES detected_questions (user_id, id) ON DELETE CASCADE` |
| 6 | `fk_generated_answers_session_id_sessions` | `fk_generated_answers_tenant_session FOREIGN KEY (user_id, session_id) REFERENCES sessions (user_id, id) ON DELETE CASCADE` |
| 7 | `fk_chunk_embeddings_chunk_id_chunks` | `fk_chunk_embeddings_tenant_chunk FOREIGN KEY (user_id, chunk_id) REFERENCES chunks (user_id, id) ON DELETE CASCADE` |

Each is added with `NOT VALID` first, then `VALIDATE CONSTRAINT`, then the old constraint is dropped (B.6). Generic form of the replacement (EXECUTED for all seven):

```sql
ALTER TABLE <child> ADD CONSTRAINT <new_name>
  FOREIGN KEY (user_id, <x_id>) REFERENCES <parent> (user_id, id) ON DELETE <same action> NOT VALID;
ALTER TABLE <child> VALIDATE CONSTRAINT <new_name>;
ALTER TABLE <child> DROP CONSTRAINT <old_name>;
```

### B.3.3 ON DELETE SET NULL on a composite key (sessions.resume_id)

On a composite foreign key, a plain `ON DELETE SET NULL` sets **every** referencing column to NULL. For `sessions` that includes `user_id`, which is `NOT NULL`, so deleting a resume would fail. **EXECUTED:** with a plain `ON DELETE SET NULL` constraint, `DELETE FROM resumes` failed with `null value in column "user_id" of relation "sessions" violates not-null constraint` (the failing statement shown by PostgreSQL was `UPDATE ONLY sessions SET user_id = NULL, resume_id = NULL`).

Correct syntax on PostgreSQL 15 and later (so valid on the project's pg16): `ON DELETE SET NULL (resume_id)`. **EXECUTED:** with that clause, deleting a resume as the runtime role left the session row in place with its `user_id` intact and only `resume_id` set to NULL, exactly the current behaviour (`models.py:277` documents that "deleting a resume only clears `resume_id`").

Also relevant: a composite key with a NULL in any column is not checked at all (default `MATCH SIMPLE`). `user_id` is never null, and a NULL `resume_id` is still allowed, so sessions without a resume continue to work.

If the project ever had to run on PostgreSQL 14 or older, the alternative is a trigger or application-level clearing of `resume_id` before the delete, plus a composite key with `ON DELETE NO ACTION`; this is **not** needed for pg16.

## B.4 Index implications (verified from the schema)

* The 5 new unique constraints each create one new index `(user_id, id)`. Cost: disk and a small write cost on insert and on `user_id`/`id` updates (these columns are never updated by the application, Not verified for every code path, but the models treat both as immutable). Table sizes in production are **Not known**.
* Referencing-side indexes: the planner needs an index on the referencing columns for fast parent deletes and cascades. Existing indexes already cover the new keys, so **no new referencing indexes are needed**: `ix_sessions_resume_id (resume_id)`; `pk_session_documents (session_id, document_id)` and `ix_session_documents_document_id (document_id)`; `ix_detected_questions_session_id_detected_at (session_id, detected_at)`; `ix_generated_answers_question_id (question_id)` and `ix_generated_answers_session_id_created_at (session_id, created_at)`; `pk_chunk_embeddings (chunk_id, embedding_model)`. PostgreSQL's cascade query will filter by `user_id` and the id column and use these indexes for the id part. (Reading of the index list: **Verified**; planner behaviour at production size: **Not known**.)
* The existing single-column primary keys and the old foreign keys' behaviour are unchanged; the old single-column FKs are dropped at the end, so there is no double checking afterwards. During the transition both old and new constraints are checked on writes (slightly more work for a short time).
* `chunk_embeddings` holds large vector data; the new constraint is on `chunks` (small rows), so the `chunks (user_id, id)` index does not touch the vector index.

## B.5 Detecting violating rows BEFORE migrating (read-only SQL)

All are plain `SELECT` statements. **EXECUTED** on the throwaway database after deliberately planting one violating row per case (as the database owner, which bypasses row-level security); each query returned exactly the planted row. A real run must be executed as a role that can see all tenants (the owner or the maintenance role), otherwise row-level security hides the very rows being looked for. Expected result on a healthy database: zero rows for every query.

```sql
-- D1 sessions.resume_id points at a resume of another user         (planted: 1 row found)
SELECT s.id FROM sessions s JOIN resumes r ON r.id = s.resume_id WHERE r.user_id <> s.user_id;
-- D2 session_documents.session_id points at another user's session  (planted: none planted, 0 rows)
SELECT sd.session_id, sd.document_id FROM session_documents sd JOIN sessions s ON s.id = sd.session_id WHERE s.user_id <> sd.user_id;
-- D3 session_documents.document_id points at another user's document (planted: 1 row found)
SELECT sd.session_id, sd.document_id FROM session_documents sd JOIN documents d ON d.id = sd.document_id WHERE d.user_id <> sd.user_id;
-- D4 detected_questions.session_id points at another user's session  (planted: 1 row found)
SELECT q.id FROM detected_questions q JOIN sessions s ON s.id = q.session_id WHERE s.user_id <> q.user_id;
-- D5 generated_answers.session_id points at another user's session   (none planted, 0 rows)
SELECT a.id FROM generated_answers a JOIN sessions s ON s.id = a.session_id WHERE s.user_id <> a.user_id;
-- D6 generated_answers.question_id points at another user's question (planted: 1 row found)
SELECT a.id FROM generated_answers a JOIN detected_questions q ON q.id = a.question_id WHERE q.user_id <> a.user_id;
-- D6b (extra, not a tenant check) answer whose question belongs to a different session (found the same planted row)
SELECT a.id FROM generated_answers a JOIN detected_questions q ON q.id = a.question_id WHERE q.session_id <> a.session_id;
-- D7 chunk_embeddings.chunk_id points at another user's chunk        (planted: 1 row found)
SELECT e.chunk_id, e.embedding_model FROM chunk_embeddings e JOIN chunks c ON c.id = e.chunk_id WHERE c.user_id <> e.user_id;
-- D8 polymorphic references with a missing or foreign source (informational; no FK exists)   (0 rows)
SELECT 'chunks' AS t, c.id FROM chunks c
  LEFT JOIN resumes r ON c.source_type='resume' AND r.id=c.source_id
  LEFT JOIN documents d ON c.source_type='document' AND d.id=c.source_id
  WHERE COALESCE(r.user_id, d.user_id) IS DISTINCT FROM c.user_id
UNION ALL
SELECT 'extracted_texts', x.id FROM extracted_texts x
  LEFT JOIN resumes r ON x.source_type='resume' AND r.id=x.source_id
  LEFT JOIN documents d ON x.source_type='document' AND d.id=x.source_id
  WHERE COALESCE(r.user_id, d.user_id) IS DISTINCT FROM x.user_id
UNION ALL
SELECT 'ingestion_jobs', j.id FROM ingestion_jobs j
  LEFT JOIN resumes r ON j.source_type='resume' AND r.id=j.source_id
  LEFT JOIN documents d ON j.source_type='document' AND d.id=j.source_id
  WHERE COALESCE(r.user_id, d.user_id) IS DISTINCT FROM j.user_id;
```

Risk of existing data failing the new constraints: the application always sets `user_id` from the authenticated user and looks parents up with the same `user_id` (**Reported**; the assessment states no cross-tenant access was found by live probes). So production data is expected to be clean, but this is **Not known** until D1 to D8 are run on a copy or on the real database. If any query returns rows: stop, do not migrate, investigate each row (how it was created, which user is the real owner), and decide per row with the owner. The probe's remediation (deleting the planted rows) is not a recommendation for real data; real violating rows may be evidence of a bug.

## B.6 Phased plan (proposed Alembic migrations; the SQL was executed on the throwaway database as shown)

Run migrations as the table owner or migration role, never the runtime role. Set `SET lock_timeout = '5s'` per statement (PROPOSED) so a blocked statement fails fast and is retried instead of queueing behind a long transaction and blocking everything behind it.

| Phase | Migration | Statements | Executed? |
|---|---|---|---|
| 0 | none | run D1 to D8 on a production-size copy and on production; all must return zero rows | queries executed on throwaway; real run PROPOSED |
| 1 | 0012a | `CREATE UNIQUE INDEX CONCURRENTLY` (5), then `ALTER TABLE ... ADD CONSTRAINT ... UNIQUE USING INDEX` (5) | SQL executed (throwaway); migration file PROPOSED |
| 2 | 0012b | `ALTER TABLE ... ADD CONSTRAINT <new> FOREIGN KEY (...) ... NOT VALID` (7). Old FKs stay in place. New rows are already checked from this moment | SQL executed (throwaway) |
| 3 | 0012c | `ALTER TABLE ... VALIDATE CONSTRAINT <new>` (7), one per statement, preferably in separate short transactions | SQL executed (throwaway); verified to fail while a violating row exists and to pass after it was removed |
| 4 | 0012d | in one transaction `ALTER TABLE ... DROP CONSTRAINT <old>` (7) | SQL executed (throwaway) |

Alembic notes (PROPOSED, not run): `env.py` runs migrations in a transaction (`Will assume transactional DDL` in the executed `alembic upgrade head` output), and `CREATE INDEX CONCURRENTLY` cannot run inside a transaction, so phase 1 must use `with op.get_context().autocommit_block():`; phases 2 and 3 are separate revisions so that a failed validation leaves a clean, resumable state rather than rolling everything back; the migration must be idempotent about leftover INVALID indexes (a failed concurrent build leaves an invalid index that must be dropped and rebuilt).

Observed results on the throwaway (all **Verified (executed)**):

* Phase 2 with violating rows present succeeded (NOT VALID skips old rows) and the table was immediately protected: a new cross-tenant insert was refused with `violates foreign key constraint "fk_sessions_tenant_resume"` even before validation.
* Phase 3 failed on the three constraints tried while planted violations existed (sessions, session_documents.document_id, chunk_embeddings), with the offending key in the error detail; after the planted rows were removed all seven validated.
* Final state: seven new foreign keys, all `convalidated = true`, with the expected definitions.

### B.6.1 Lock levels and expected duration

Lock modes below were read from `pg_locks` inside a transaction (**Verified (executed)**):

| Statement | Locks held | Meaning |
|---|---|---|
| `ADD CONSTRAINT ... FOREIGN KEY ... NOT VALID` | `SHARE ROW EXCLUSIVE` on **both** the referencing and the referenced table | blocks inserts, updates and deletes on both tables, but not reads; no table scan, so it is short once the lock is obtained, but it must wait for in-flight transactions on those tables and anything queued behind it waits too |
| `VALIDATE CONSTRAINT` | `SHARE UPDATE EXCLUSIVE` on the referencing table, `ROW SHARE` on the referenced table | does not block reads or normal writes; takes a table scan of the referencing table, so duration grows with its size |
| `CREATE UNIQUE INDEX CONCURRENTLY` | no write-blocking lock (PostgreSQL documentation, **Reported**, not measured) | two table scans; slower than a normal build; a failure leaves an invalid index |
| `ALTER TABLE ... ADD CONSTRAINT ... UNIQUE USING INDEX` | brief `ACCESS EXCLUSIVE` (PostgreSQL documentation, **Reported**, not measured here) | catalog-only, short |
| `DROP CONSTRAINT` (old FK) | brief `ACCESS EXCLUSIVE` on the table (**Reported**, not measured) | short, but it also waits for in-flight transactions |

Expected durations at production size: **Not known** (the throwaway had a handful of rows; no timing is meaningful). The one place that can be slow is VALIDATE on `generated_answers` and `chunk_embeddings` if they are large; it does not block writes. Recommended: rehearse on a restored copy of production (the backup scripts can produce one) and record the real timings before approving the production run, and run in a low-traffic window with `lock_timeout`.

## B.7 Full rollback plan

* **Before phase 4 (old FKs still present):** simply `ALTER TABLE ... DROP CONSTRAINT <new>` for the seven new constraints and `ALTER TABLE ... DROP CONSTRAINT uq_<table>_user_id_id` for the five unique constraints. No data effect. Fully safe at any point.
* **After phase 4 (old FKs dropped), downgrade steps (PROPOSED; the first FK step was EXECUTED on the throwaway inside a transaction that was rolled back, which showed the single-column key can be re-added alongside the composite and the composite dropped):** (1) re-add the seven single-column foreign keys `NOT VALID` with their original names and `ON DELETE` actions (sessions.resume_id: `ON DELETE SET NULL`, others `CASCADE`); (2) `VALIDATE` them (this cannot fail: if the composite keys were valid, every single-column reference is valid); (3) drop the seven composite keys; (4) drop the five unique constraints and their indexes. The Alembic `downgrade()` would do exactly this.
* **Data lost by rollback: none.** The migration changes constraints only; it adds and removes no data and no columns. The only information lost is the protection itself.
* **During the window between steps:** the database is never without a foreign key on those columns (new ones are added before the old ones are dropped, and the reverse on downgrade), so referential integrity is never weaker than today.
* If phase 1 is interrupted, an INVALID unique index may remain (`\d <table>` shows it); drop it (`DROP INDEX CONCURRENTLY`) and re-run.

## B.8 Compatibility with the application code and the ORM

* **Application behaviour:** no change is required for inserts or deletes. Every insert already supplies `user_id` and parents are fetched under the same `user_id`, and the cascade and set-null behaviour is preserved (**Verified (executed)**: session delete cascaded to links, questions, answers; resume delete cleared only `resume_id`; user delete cascaded everywhere).
* **SQLAlchemy models (`app/db/models.py`):** change **seven** single-column `ForeignKey("x.id", ...)` declarations (lines 304, 327, 330, 422, 476, 479 and the `chunk_id` at 249) into table-level `ForeignKeyConstraint(["user_id", "<col>"], ["<table>.user_id", "<table>.id"], name=..., ondelete=...)` entries, and declare the five `UniqueConstraint("user_id", "id", name=...)` entries so that the models match the migrated schema. For the SET NULL case the ORM option `ondelete="SET NULL (resume_id)"` is needed in `ForeignKeyConstraint` (SQLAlchemy 2 passes the string through; **Not verified** here, confirm in the implementation). Because the models have no relationships, nothing else in the ORM changes. If a test compares models with the migrated schema (none found: no `compare_metadata` use, **Verified** by search), it would need the names to match.
* **Row-level security:** policies are unchanged. The new constraints add a database-level check that the policy cannot express.
* **Tests that already touch this:** `tests/test_rls.py` and `tests/test_db_privilege_separation.py` use the runtime role (**Verified** by file search) and are the right place for the new tests.
* **Documents to update with the change:** `docs/DATABASE.md` section 3 (it describes composite keys as if they existed), `docs/SECURITY_ASSESSMENT_PHASE_E.md` (SEC-20 status), `docs/DECISIONS.md` (a new decision entry, per CLAUDE.md).

## B.9 Compatibility with backups

* **Restore of a backup made BEFORE the migration into a migrated schema:** `restore.sh` restores the dump, which carries its own schema; it requires an empty target and checks the schema version in the manifest. An old backup therefore comes back as the old schema (revision 0011). The safe procedure is: restore it as is, then run the migrations (0012) on the restored database, running D1 to D8 first. The old data passes if and only if it contains no cross-tenant reference. Restoring the old dump **into a database that already has the new constraints** is not how the scripts work (they refuse a non-empty target), and `pg_restore --exit-on-error` (`restore.sh:50`) would stop at the first conflict. **Not executed** in this session.
* **Restore of a backup made AFTER the migration with the application at the old version (reverse):** the restored schema has the composite keys; the old application code works against it unchanged (B.8), so the old code runs correctly. If an old code version is combined with `alembic downgrade`, the downgrade in B.7 applies. **Not executed.**
* **Version coupling:** the manifest records the schema version and the restore compares it (**Reported**, `DEPLOYMENT.md:94`), so a mismatch is detected; the drill in `DEPLOYMENT.md` section 8 should be repeated once after this migration (the document already says to repeat it after every migration).
* **Deletion ledger (Part A):** independent; it works by object id and is unaffected by foreign-key shape.

## B.10 Test plan (the database itself must refuse a cross-tenant reference, as the runtime role)

Tests belong in `services/api/tests/test_rls.py` style (real PostgreSQL, runtime role via `SET LOCAL ROLE interview_app_runtime`, `app.user_id` set). The following was **EXECUTED** by hand on the throwaway as a proof of concept; the same statements become automated tests.

Executed results, runtime role, tenant A acting, before and after the migration:

| Test | Statement | Before (original schema, `ia_sec20_probe2`) | After (migrated, `ia_sec20_probe`) |
|---|---|---|---|
| T0 | same-tenant session with own resume | accepted | accepted |
| T1 | session of A pointing at B's resume | **accepted (the gap)** | refused: `fk_sessions_tenant_resume` |
| T2 | session_documents of A pointing at B's document | **accepted** | refused: `fk_session_documents_tenant_document` |
| T3 | session_documents of A pointing at B's session | **accepted** | refused: `fk_session_documents_tenant_session` |
| T4 | detected_questions of A pointing at B's session | **accepted** | refused: `fk_detected_questions_tenant_session` |
| T5 | generated_answers of A pointing at B's question | **accepted** | refused: `fk_generated_answers_tenant_question` |
| T6 | generated_answers of A pointing at B's session | **accepted** | refused: `fk_generated_answers_tenant_session` |
| T7 | chunk_embeddings of A pointing at B's chunk | **accepted** | refused: `fk_chunk_embeddings_tenant_chunk` |
| T8 | UPDATE of A's session to B's resume id | **accepted** | refused: `fk_sessions_tenant_resume` |
| T9 | session_preferences for B's session | refused by row-level security | refused by row-level security (unchanged) |

Further automated tests to add: (a) delete a resume as the runtime role: the session survives with `user_id` intact and `resume_id` NULL (executed as B1); (b) delete a session: links, questions, answers cascade, resume and documents survive (executed as B3); (c) delete the user: all owned rows disappear (executed as B4, as owner); repeat (c) as the runtime role; (d) a metadata test that every foreign key from one user-owned table to another includes `user_id` (catalog query over `pg_constraint`), failing for a future single-column key, with `session_preferences` and the three polymorphic columns listed as documented exceptions; (e) the D1 to D8 queries return zero rows on a freshly migrated test database after the standard test data is loaded; (f) migration test: upgrade from 0011 with planted violations must fail at validation with a clear message and leave the old constraints in place; downgrade restores the exact original constraint set.

## B.11 Effort estimate (Not measured; engineer-days)

| Item | Estimate |
|---|---|
| Migrations 0012a to 0012d with `autocommit_block`, `lock_timeout`, idempotent re-run, downgrade | 0.5 to 1 day |
| Model changes (7 constraints, 5 unique constraints) | 0.25 day |
| Automated tests (B.10) | 0.5 to 1 day |
| Rehearsal on a restored production-size copy, timings recorded | 0.5 day (after infrastructure exists) |
| Documentation updates (DATABASE.md, assessment, DECISIONS) | 0.25 day |
| **Total** | **about 2 to 3 days** |

## B.12 Recommendation

Approve the plan as written (7 composite foreign keys, 5 unique constraints, `session_preferences` and the polymorphic columns left as documented exceptions), to be executed in the phases of B.6 only after: D1 to D8 are run on production data, a rehearsal on a restored copy has recorded timings, and a fresh backup has been taken and verified. Priority is low (the application check is sound); doing it before real user data exists is cheaper than after, because the validation phase is then trivial.

## B.13 Approval block (owner)

Nothing described in Part B will be executed against any development, test or production database before this block is signed. The throwaway databases used for verification have been dropped.

| Decision | Choice |
|---|---|
| SEC-20 composite foreign keys as described in B.3 and B.6 | [ ] Approve   [ ] Approve with changes (write below)   [ ] Reject   [ ] Defer until: ____________ |
| Leave `session_preferences` without a `user_id` column (no change) | [ ] Agree   [ ] Add the column instead (larger change) |
| Treat polymorphic `source_id` columns as a separate later decision | [ ] Agree   [ ] Include now |
| Require a rehearsal on a restored production-size copy first | [ ] Yes   [ ] No |

Changes requested: _______________________________________________

Owner: ____________________   Date: ____________   Signature: ____________________

---

## Appendix: exactly what was run, and what was not

**Executed in this session:**

* Created `ia_sec20_probe` and `ia_sec20_probe2` (empty) on the local Docker PostgreSQL 16.15; ran `uv run alembic upgrade head` against each (migrations 0001 to 0011; note that migration 0008 issues `ALTER ROLE`/`GRANT` statements for the cluster-wide role `interview_app_runtime`, which already existed with the same attributes; no attribute was changed).
* Queried the generated catalog (`pg_constraint`, `pg_indexes`, `information_schema.columns`) for foreign keys, unique constraints, indexes and required columns.
* On `ia_sec20_probe`: seeded two tenants with valid rows and planted five cross-tenant rows; ran detection queries D1 to D9; created the 5 unique indexes concurrently and attached them as constraints; read lock modes inside a rolled-back transaction; added the 7 composite foreign keys `NOT VALID`; showed that a new cross-tenant insert was refused at once; showed validation failing on three constraints while violating rows existed; removed the planted rows (probe only); validated all 7; dropped the 7 old foreign keys; ran tests T0 to T9 as `interview_app_runtime`; ran B1 to B5 (set-null, plain set-null failure, session cascade, user cascade as owner, downgrade fragment) all inside rolled-back transactions.
* On `ia_sec20_probe2`: seeded the same valid rows (no planted rows), then ran T0 to T9 on the original schema to show the gap.
* Read-only Redis queries: `CONFIG GET appendonly`, `appendfsync`, `auto-aof-rewrite-percentage`, `auto-aof-rewrite-min-size`, `save`; `INFO persistence`; `DBSIZE`.
* Dropped both probe databases and confirmed none remains.

**Proposed only, not executed:** the Alembic migration files and `autocommit_block` usage; `lock_timeout` settings; the ledger, account deletion, cache purge and Redis configuration changes; the downgrade steps beyond the rolled-back fragment; restore of an old backup into a migrated schema; any run against production-sized data; every timing and effort figure.

**Not changed:** no existing file was edited; no git command that changes state was run; no migration was run against the development, test or any other database; no process was stopped.
