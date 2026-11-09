# Branch Merge Analysis — `origin/dheeraj-branch`

Analysis date: 2026-11-09. Method: read-only Git inspection, full diff review, then verification on the
merged tree (see `docs/MERGE_COMPLETION_REPORT.md` for results and the final decisions).

## 1. Branch summary

| Item | Value |
|---|---|
| Source branch | `origin/dheeraj-branch` @ `66b7248b29085b26adf63727941b216743afac85` |
| Current branch | `main` @ `9917a456739f73edfabd3666747aa7d367548b82` |
| Merge base | `9917a456739f73edfabd3666747aa7d367548b82` (= local HEAD) |
| Relationship | Branch is a **strict descendant** of local `main`: 1 unique commit on the branch (`Implement interview assistant enhancements`, DheerajGujarathi, 2026-11-09), **0** unique commits locally. No three-way divergence; a Git-level conflict is impossible. |
| Commits analyzed | 1 |
| Files added | 43 (24 under `apps/desktop`, 9 docs, 10 under `services/api`) |
| Files modified | 55 |
| Files deleted | 0 |
| Size | 13,480 insertions, 429 deletions (6,566 lines are `apps/desktop/package-lock.json`; `uv.lock` also changed) |
| Base-branch confidence | High. Merge base equals local HEAD, so no guessing was needed. |

## 2. Current working tree protection

- The working tree was **clean** at the start (`git status` empty); no uncommitted work existed.
- Safety checkpoint: local tag `safety/pre-dheeraj-merge-20261109` on `9917a45`. Restoring is `git merge --abort`
  (while the merge is uncommitted) or resetting to that tag.
- The branch was fetched with `git fetch --all --prune` (adds `origin/dheeraj-branch` only; no working-tree change).
- Integration used `git merge --no-commit --no-ff`: stages the result, does not commit, does not push.
- No stash, reset, clean, forced checkout or deletion was used. The developer `.env` (git-ignored) was never
  modified and its values were not displayed; only its key names, the JWT secret length, and URL scheme were checked
  for compatibility with the stricter config rules (compatible).
- Verification used local Docker Postgres/Redis from the existing `docker-compose.yml`. Tests only create and drop the
  dedicated `<db>_test` database; the development database was not touched and no migration was applied to it.

## 3. Implemented changes

### F1 — Tenant row-level security (D-45, migration `0008_tenant_rls`)
- Files: `alembic/versions/0008_tenant_rls.py`, `app/db/session.py`, `app/auth/{deps,service}.py`, `app/qa/service.py`,
  `app/realtime/{gateway,generation}.py`, `app/ingestion/{dispatch,service}.py`, `app/files/service.py`,
  `app/workers/{tasks,recovery,reindex}.py`, `tests/test_rls.py`, `tests/helpers.py`, DATABASE.md.
- What: enables and **forces** RLS on 13 user-owned tables, creates `NOLOGIN NOBYPASSRLS` role
  `interview_app_runtime`; every runtime session does `SET LOCAL ROLE` and `set_config('app.user_id', …)`.
  Login / refresh get narrowly scoped context (`app.login_email`, `app.refresh_token_hash`). Recovery and reindex use a
  separate `MAINTENANCE_DATABASE_URL`. Dramatiq `ingest_job` now takes `(job_id, user_id)`.
- Supersedes D-27 (RLS deferral), which was an explicit "revisit later" decision.
- Tests available: `tests/test_rls.py` (real runtime role, two users), plus every DB-backed test now runs under RLS.
- Status: **Broken or conflicting as authored** (upgrade failed on this stack, and downgrade also failed; both
  fixed during integration, see §5 C2/C12 and the completion report).

### F2 — OpenAI-compatible LLM adapter (D-44)
- Files: `app/ai/adapters/openai.py`, `app/ai/registry.py`, `app/ai/{router,types,usage}.py`, `app/core/config.py`,
  `app/main.py`, `pyproject.toml`, `uv.lock`, `tests/test_openai_provider.py`, API.md, `.env.example`.
- What: async Chat Completions adapter behind the existing `LLMProvider` port, streaming + one-shot, error mapping to
  the existing taxonomy, stream closed in `finally`, client closed at shutdown, single configured model allowlisted,
  Ollama Cloud via `OPENAI_BASE_URL`. `Usage` is now optional (`None` when the SDK gives no counts).
- Fits the port: yes. Fake provider retained (`KNOWN_PROVIDER_IDS = {fake, openai}`); fake still forbidden outside local.
  Keys come from env (`SecretStr`), are not returned by any endpoint; SDK import stays inside `app/ai/adapters/`.
- Tests: mocked-HTTP only (443 lines). **No live test**; no claim of real-provider correctness.
- Status: **Implemented but needs testing** (live behaviour unverified; OQ-4 data-terms question still open).

### F3 — Prompt v2 and evidence-tagged context
- Files: `app/preferences/prompt_builder.py`, `templates/v2/{system,user}.md`, `app/qa/service.py`, 13 prompt snapshots,
  `tests/test_prompt_builder.py`.
- What: `PROMPT_VERSION` v1 → v2; v2 overrides only two templates and inherits the rest from v1; retrieved chunks are
  passed as `EvidencePassage` (source type/id/chunk index) and rendered `[E# | …]` with quoted tags neutralised.
- Risk: changes the default prompt for all Q&A (SSE and WebSocket). v1 remains selectable.
- Status: **Implemented but needs testing** (structure/snapshot tested only; model behaviour unmeasured).

### F4 — RAG evaluation harness
- Files: `tests/rag_evaluation.py`, `tests/fixtures/rag_eval_dataset.json`, `tests/test_rag_evaluation.py`, RAG_EVALUATION.md,
  RAG_RESULTS.md.
- What: synthetic 10-question gold set, fake embedder, in-memory retrieval. Its own report states no retrieval
  improvement and that fake embeddings do not establish semantic quality. Honest and compatible.
- Status: **Experimental** (offline harness; not production retrieval evidence).

### F5 — Metrics (D-46)
- Files: `app/core/metrics.py`, `app/main.py`, `app/ai/router.py`, `app/db/session.py`, `app/realtime/gateway.py`,
  `app/workers/tasks.py`, `tests/test_health.py`.
- What: `prometheus-client` counters/histograms; `GET /metrics` mounted on the public app, **no application auth**
  (documented as "expose only on a trusted monitoring network"). Low-cardinality labels, no user/content values.
- Status: **Implemented but needs testing** (one endpoint test).

### F6 — Production configuration hardening
- Files: `app/core/config.py`, `tests/test_config.py`, `.env.example`.
- What: outside `APP_ENV=local`: JWT secret ≥43 URL-safe chars with entropy check, `rediss://` required,
  `MAINTENANCE_DATABASE_URL` required. Local mode unchanged apart from the JWT minimum rising 32→43.
- Status: **Complete and verified** by its config tests (see completion report for the run).

### F7 — Electron desktop MVP (D-47, Phase 10)
- Files: `apps/desktop/**` (24 files incl. lockfile), docs DESKTOP_*.md, ARCHITECTURE/PHASES/DECISIONS/API edits.
- What: Electron + React: main process owns API calls and credentials; sandboxed, context-isolated renderer; IPC sender
  validation; refresh token only via OS `safeStorage`; HTTPS-or-loopback backend URL; visible always-on-top window +
  tray; typed Q&A over the existing SSE endpoint; cancel, history, copy.
- Reviewed for the project's hard rules: **no audio capture, screenshots, `setContentProtection`, `desktopCapturer`,
  hidden-window or stealth APIs** (grep confirmed). Backend untouched by this feature.
- **Governance note (resolved):** it supersedes accepted decision **D-09 (Tauri 2)** and starts **Phase 10**. The owner
  subsequently stated, in this task, that Electron is the preferred desktop direction; docs now reflect that. Phase 10 is
  still not complete.
- Tests: vitest (4 files), strict typecheck, production build. No Windows launch/package smoke; manual checklist unrun.
- Status: **Implemented but needs testing**.

### F8 — Documentation
BASELINE_REPORT, SECURITY_REVIEW, PRIVACY, OPERATIONS, RAG_*, DESKTOP_*, plus edits to API/ARCHITECTURE/DATABASE/DECISIONS/
PHASES/RISKS/TESTING_STRATEGY/README/CLAUDE.md. Status: **Documentation only.**
Caveats: BASELINE_REPORT describes the *author's* environment (no uv/Docker) and is point-in-time; SECURITY_REVIEW labels
its work "Phase 3", which collides with the repo's accepted Phase 3 numbering; API.md now renames `/healthz,/readyz` to the
actually implemented `/health,/ready` (correct).

## 4. Incomplete or experimental work

- Real-LLM adapter: mock-tested only; no live run; production key ownership and data-retention terms (OQ-4) unresolved.
- RAG harness: synthetic data, fake embedder; says nothing about real retrieval quality.
- Desktop: no Windows runtime/package smoke; no WebSocket; no capture (intentional).
- RLS: production role provisioning/grants (non-superuser runtime login, separate maintenance login) is documentation
  and a runtime requirement, not automated; migration needs a role able to create/grant roles.
- Metrics: process-local; worker metrics are logs only; endpoint unauthenticated.
- Dramatiq signature change: messages enqueued **before** deployment with the old one-argument form will fail on a
  new worker. Drain the `ingestion` queue before deploying.

## 5. Conflicts and risks

| # | Item | Current (`main`) | `dheeraj-branch` | Risk | Resolution |
|---|---|---|---|---|---|
| C1 | Git conflicts | n/a | n/a | None: linear history | — |
| C2 | Migration 0008 | head is 0007; one head | 0008 sets `down_revision=0007`; single head, no fork | **Real defect**: one `op.execute` contains many statements; asyncpg rejects it ("cannot insert multiple commands into a prepared statement"), so every DB test errored at setup | Split into one statement per `op.execute`; same SQL, same revision id. Migration had not been applied anywhere here. |
| C12 | Migration 0008 downgrade | n/a | `DROP ROLE interview_app_runtime` | **Second real defect**: the role is cluster-wide, so a per-database downgrade fails while other databases (e.g. the test DB) hold grants | Revoke everything in this database; leave the role in place (documented in the migration) |
| C13 | Tests not updated for intentional changes | n/a | 23 failures in the full suite | Config tests (weak JWT, `redis://`, no maintenance DB URL), migration-head assertion pinned to 0007, passage-tag parser expecting `[1]` instead of prompt-v2 `[E1 | …]`, tests reading `ingestion_jobs` through the RLS-bound session, worker run after job deletion, `Usage` now optional | Legitimate test updates; assertions preserved |
| C3 | RLS vs. decision D-27 | RLS deferred | RLS enabled+forced | Superseded intentionally by D-45; stronger isolation | Keep; branch is the more secure implementation and is documented |
| C4 | Desktop vs. D-09 / phase gate | Tauri decision; Phase 10 not started | Electron MVP, Phase 10 started | Governance, not code | Resolved: owner confirmed Electron preference; Phase 10 still incomplete |
| C5 | JWT minimum 32→43 | 32 chars | 43 URL-safe | Existing deployments with shorter secrets fail to start | Document; local `.env` verified compatible |
| C6 | `.env.example` rewritten | many tunables listed | trimmed to core vars | Dropped example entries for settings that still have defaults | Defaults live in `config.py`; low risk |
| C7 | `/metrics` unauthenticated | none | public route | Information exposure if internet-facing | Keep (documented); deployment must restrict at network layer |
| C8 | Prompt default v1→v2 | v1 | v2 | Behaviour change across all Q&A | Keep; v1 remains available; snapshots updated |
| C9 | `release/` build output | not ignored | electron-builder output dir | Could commit installers | Add `release/` to `.gitignore` (small modification) |
| C10 | Dramatiq actor signature | `ingest_job(job_id)` | `ingest_job(job_id, user_id)` | In-flight queued jobs | Operational note above |
| C11 | Formatting | — | `auth/service.py` fails `ruff format --check` | CI would fail | Apply formatter to that file |

Duplicate implementations: none found (no existing real adapter, RLS, metrics or desktop app on `main`).
Auth/authorization regressions: none found by inspection; the new context calls take identity from the token or a DB row,
and `set_tenant_context` refuses rebinding a session to a different tenant. SSE/WebSocket: only tenant-context and a
metrics counter were added; protocol unchanged.

## 6. Merge recommendation

| Change | Recommendation |
|---|---|
| F1 RLS | **Integrate after modification** (split migration statements); needs the full DB suite |
| F2 OpenAI adapter | **Needs additional testing** (integrate; live test before real use) |
| F3 Prompt v2 | **Needs additional testing** (integrate; evaluate with a real model on non-sensitive data) |
| F4 RAG harness | **Safe to integrate** (experimental, test-only) |
| F5 Metrics | **Safe to integrate** (restrict `/metrics` at network layer) |
| F6 Config hardening | **Safe to integrate** |
| F7 Electron desktop | **Integrate after modification** (`.gitignore`); owner has since confirmed Electron is the preferred direction (docs updated) |
| F8 Docs | **Safe to integrate** (note the point-in-time caveats above) |

No change is classed "Do not integrate".
