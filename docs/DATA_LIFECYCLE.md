# Data lifecycle and deletion policy (proposal for owner approval)

Date: 2026-11-23. Resolves the documentation part of finding SEC-13. **Status: PROPOSED.** Nothing in this document changes behaviour. Account deletion, automatic expiry and any other irreversible retention change are **not implemented** and need the owner's approval first (see "Decisions needed").

Evidence labels: **Verified** means read in the code in this session. **Reported** means stated by a review or by existing documentation and not re-checked here. **Not known** means it depends on infrastructure that does not exist yet.

## 1. Where user data lives today

| Data | Where | Kept until | Deleted by | Evidence |
|---|---|---|---|---|
| Account (email, password hash) | PostgreSQL `users` | indefinitely | **nothing: there is no account deletion** | Verified (no route); `docs/PRIVACY.md` |
| Refresh tokens | PostgreSQL (SHA-256 only) | expiry (14 days desktop family) or logout/reuse revoke | expiry, logout, family revoke | Reported by review |
| Resume and document files | server file storage | until the user deletes the file | `DELETE /resumes/{id}`, `DELETE /documents/{id}` (removes the stored file first, then the rows) | Reported by review (`files/service.py`) |
| Extracted text, chunks, embeddings | PostgreSQL | with their source file | cascade from the file's deletion | Reported |
| Sessions, questions, answers | PostgreSQL | until the user deletes the session | `DELETE /sessions/{id}`: hard delete with cascade to questions, answers and document links; never touches the resume or documents | Verified (`sessions/service.py` `delete_session`) |
| Audio, screenshots, transcripts | memory only (desktop, API request) | seconds to minutes | process memory is released; nothing is written | Reported by two reviews |
| Answer cache | **Redis**, key `qa_general:<hash>` | `QA_ANSWER_CACHE_TTL_SECONDS` (default 300 s, maximum 3600 s) | expiry only; **not purged when the session is deleted** | Verified (`qa/cache.py`, `config.py`) |
| WebSocket and handoff tickets | Redis (hash only) | 60 seconds | single use or expiry | Reported |
| WebSocket replay buffers | API process memory | session end or process restart | process | Reported |
| Redis append-only file | Redis volume (`--appendonly yes`) | until Redis rewrites the file | rewrite or volume removal | Reported (compose file); the production Redis is `docker-compose.prod.yml` |
| Exports (Markdown, PDF) | the user's own disk, at a path the user chose | user's decision | the user (the app warns that deleting a session does not remove an exported file) | Verified (documented in `SESSION_MANAGEMENT.md`) |
| Database and uploads backups | backup directory | `prune.sh` age-based retention | expiry of the backup | Verified (scripts); no deletion-aware step |
| Application logs | stdout of the containers | log rotation (20 MB x 5 files) | rotation | Reported; logs carry ids and counts, and since SEC-03 never exception messages |
| Model provider side | the provider | **Not known** | provider's own terms | Open question OQ-4 |

## 2. The gaps (what SEC-13 is about)

1. **No account deletion.** A user cannot remove their account, and with it every row and file they own. Deleting resumes, documents and sessions one by one is possible, but the account row, auth history and any remaining rows stay.
2. **Redis residue after a session delete.** Cached general answers for a deleted session stay in Redis until their TTL ends (at most one hour), and a copy can remain in the append-only file after the key has expired, until Redis rewrites that file.
3. **Backups keep deleted data** until the backup itself expires. This is normal for backups but must be stated to users and bounded.
4. **No automatic expiry.** Data lives until the user deletes it. For interview material this may be longer than users expect.
5. **Exports and provider-side copies are outside the system's control.**

## 3. Proposed lifecycle

### 3.1 What can be deleted immediately (no owner decision beyond approval of the policy)

| Action | Proposed behaviour | Effort |
|---|---|---|
| Delete a session | also delete that session's answer-cache keys (the cache key already binds user and session; store a per-session key set, or shorten to a prefix scan) | small |
| Delete a resume or document | already removes file, text, chunks, embeddings; add a test that proves no row or file remains | small |
| Redis persistence | either disable the append-only file for this Redis (the cache and tickets are re-creatable, so persistence has no value), or keep it and document the residue window | small, and **recommended: disable AOF** since nothing in Redis needs to survive a restart |
| Cache lifetime | keep the default at 300 s; consider a hard maximum of 300 s in production | trivial |

### 3.2 Account deletion (needs approval)

Proposed user-initiated flow: signed-in user confirms with their password, then the server (a) revokes all refresh-token families, (b) deletes the user's stored files, (c) deletes the user row, which cascades to every user-owned table (all 13 have `user_id` with cascade or are removed through their parents), (d) purges the user's Redis keys, (e) records a content-free audit line (user id and time only). The response says what remains: backups until they expire, and any exported files.

Open design points: a short grace period (for example 7 days, account disabled and recoverable) or immediate; whether an operator can delete on a user's behalf; what the desktop does when its account disappears (it already handles a failed refresh by signing out).

### 3.3 Retention limits (needs approval)

Options, in increasing strictness: (A) keep the current behaviour and say so plainly in the privacy notice; (B) offer "delete sessions older than N days" as a user action; (C) automatic expiry of sessions after N days of inactivity (default off, configurable). Recommendation: **A now, B next, C only if the owner wants it**, because automatic deletion of a user's interview history is irreversible and surprising.

### 3.4 Backups

Backups cannot be edited per user. Proposed rules: a fixed maximum retention (suggest 35 days: 7 daily and 4 weekly), stated in the privacy notice as the time within which deleted data disappears from backups; no restore of an old backup into production without re-applying deletions made since (record deletion requests in a small separate log that is replayed after a restore); backups are encrypted and authenticated (SEC-10 and SEC-11), so a stolen backup is not readable.

### 3.5 What stays outside the system

An exported file on the user's disk, and anything a model provider keeps under its own terms, are not removed by any action here. The product should keep saying so (the export warning already does) and the provider terms must be reviewed before real interview content is sent (OQ-4).

## 4. Summary table for the privacy notice

| What you delete | Gone from the live service | Still possible afterwards |
|---|---|---|
| A session | immediately (answers, questions); cached answers within 5 minutes today (immediately once 3.1 is built) | encrypted backups until they expire; files you exported |
| A resume or document | immediately (file, text, search data) | backups until they expire |
| Your account | **not available yet** | |

## 5. Decisions needed from the owner

1. Approve 3.1 (session-delete cache purge, AOF off) so it can be built and tested.
2. Account deletion: approve the flow in 3.2, choose immediate or grace period, and who may trigger it.
3. Retention: choose A, B or C in 3.3.
4. Backup retention period and the deletion-replay rule in 3.4.
5. Wording for the privacy notice (section 4) once the above is decided; it must not claim more than the system does.

No code was changed for SEC-13. The finding stays **Open** until decisions 1 to 4 are made and implemented.
