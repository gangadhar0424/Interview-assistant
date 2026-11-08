# Phase 7 — Planning and Validation Review

**Status:** Planning only. No implementation has started. Awaiting owner approval (see section 7).
**Date:** 2026-11-05
**Basis:** PHASES.md, PRD.md, ARCHITECTURE.md, API.md, DATABASE.md, DECISIONS.md, RISKS_AND_OPEN_QUESTIONS.md, TESTING_STRATEGY.md, CLAUDE.md, and the current code after Phases 1–6.

---

## 1. Exact Phase 7 requirements

**PHASES.md:** "Session Creation (Backend + Web Wizard) & Preference Compiler".

- **Scope:** `sessions`, `session_preferences` and `session_documents` tables; session endpoints; job-import (SSRF-safe) with manual fallback; `PreferenceCompiler` + `PromptBuilder` (templates, versioning); web 2-step wizard; dashboard "Call Sessions" list.
- **Deliverables:** wizard UI, endpoints, compiler/builder modules.
- **Dependencies:** Phases 3, 5 (ready resumes) and 6 (options).
- **Acceptance:**
  - A session is created with validated ownership of the resume and documents.
  - A job URL failure still allows manual entry.
  - SSRF cases are blocked.
  - Each preference value maps to the expected `GenerationConfig`.
  - Prompt snapshots are stable.
- **Targeted tests:** session API + isolation, SSRF test suite, compiler/prompt snapshot tests, wizard e2e.

**Product requirements that apply:** FR-SES-1, FR-SES-2, FR-HIS-1, FR-AUTH-3 (cross-user access returns 404), PRD acceptance criteria 5 and 6.

## 2. Already completed in Phases 1–6

| Prerequisite | State |
|---|---|
| Auth, `CurrentUser`, 404 on foreign access, user-scoped repositories | Done (Phases 2–3) |
| Resumes and documents with ingestion and indexing status (`ready` = both stages complete) | Done |
| Retrieval service accepting `source_ids` filters | Done (Phase 5); not needed until Phase 8 |
| `/ai/options` with providers, models and languages | Done (Phase 6) |
| `LLMProvider` port and router | Done (Phase 6); not used in Phase 7 |
| Web shell: login, signup, `/home`, `FilesPanel` | Done |

## 3. Still to implement

- The three tables and a migration; session endpoints (create, list, get, patch, delete).
- `PreferenceCompiler`, `PromptBuilder` and versioned templates.
- Job-import (SSRF-hardened).
- Wizard UI and the dashboard sessions list.
- The `question_types`, `formats`, `lengths` and `tones` lists in `/ai/options` (deferred from Phase 6).
- Documentation and decision records.

**Not present today:** any session tables or code; a rate limiter (R-17); an audit table; web test tooling (no Playwright or vitest in `apps/web`); a runtime HTTP client (`httpx` is a dev-only dependency).

## 4. Recommendation: split Phase 7 into four sub-phases

Phase 7 spans four different risk profiles: pure logic, database/API, network security, and frontend. Splitting keeps each piece independently buildable and testable.

**Order: 7A → 7B → 7C → 7D.** 7A has no dependencies and 7B consumes it. 7C needs only auth and could run before 7B, but is placed after so the core flow lands first. 7D needs all three.

### 7A — Preference Compiler and PromptBuilder (pure logic)

- **Scope:**
  - Enums for language, question type, format, length and tone as the single source of truth.
  - `PreferenceCompiler`: maps enums to a typed answer config plus the Phase 6 sampling `GenerationConfig`. Length targets come from settings.
  - `PromptBuilder`: versioned template files (base, per-question-type, per-format, per-tone) and `prompt_version`.
  - Sanitization of `extra_instructions` (length cap, control characters); untrusted content is placed in delimited blocks.
  - The enum lists in `/ai/options`.
- **Out of scope:** no DB, no endpoints, no LLM calls, no context assembly. The builder takes typed inputs for context, JD and question, exercised with synthetic data.
- **Dependencies:** Phase 6 types only.
- **Database/API changes:** `/ai/options` response gains `question_types`, `formats`, `lengths`, `tones`.
- **Testing:** L1 unit tests; golden-file prompt snapshots per combination class; prompt-injection samples; static check that no inline prompt strings exist outside the builder.
- **Acceptance:**
  - Every enum value maps to its expected config.
  - Snapshots are stable.
  - Injection samples stay inside their delimited blocks.
  - Unknown enum values are rejected.

### 7B — Sessions backend (tables and API)

- **Scope:**
  - Migration for `sessions`, `session_preferences`, `session_documents`.
  - Endpoints: `POST /sessions`, `GET /sessions` (cursor, `status`, `q`), `GET /sessions/{id}`, `PATCH /sessions/{id}`, `DELETE /sessions/{id}`.
  - Ownership validation of resume and documents; materials snapshot (labels kept for history).
  - Preferences validated against the registry allowlist, compiled with 7A, and stored (`generation_config`, `prompt_version`).
  - Configurable limits for JD size and document count.
- **Dependencies:** 7A; Phases 3, 5, 6.
- **Database/API changes:** three new tables; five endpoints; `docs/API.md` and `docs/DATABASE.md` updated first (contract-first).
- **Testing:** L2 API and isolation tests against real Postgres; migration from a clean DB; DB CHECK vs enum parity test.
- **Targeted regression:** file-delete cascade, Phase 3 isolation, `/ai/options`, config.
- **Acceptance:**
  - Foreign or unknown resume/document IDs return 404.
  - Non-ready materials are rejected (see decision 3).
  - User B cannot list, read, patch or delete user A's sessions.
  - Deleting a document removes its link; deleting a resume keeps the session with a snapshot label.
  - CHECK constraints match the enums.
  - The migration applies cleanly.

### 7C — Job import (SSRF-safe)

- **Scope:**
  - `POST /sessions/job-import` and the `jobs_import` module.
  - **Network rules:** http(s) only; resolve DNS and block private, loopback, link-local and metadata ranges; connect to the validated IP so DNS rebinding cannot swap it; re-validate every redirect; no cookies or credentials; size and time caps; content-type allowlist; descriptive User-Agent.
  - **Extraction:** JSON-LD `JobPosting` first, then generic HTML-to-text fallback.
  - **Result:** `succeeded`, `partial` or `failed`; never blocks manual entry.
  - Per-user rate limit.
- **Dependencies:** auth only (stateless endpoint).
- **Database/API changes:** none to the DB; one endpoint; new settings for caps and limits.
- **Testing:** SSRF suite using a local fake HTTP server and a fake resolver (no real internet): private IPv4/IPv6 and IPv4-mapped forms, `localhost`, metadata addresses, decimal/hex/octal IPs, redirects to internal hosts, DNS rebinding, oversize and slow responses, non-HTML content, manual-fallback behaviour.
- **Acceptance:**
  - All SSRF cases are blocked.
  - Failures and timeouts return a safe message with no exception text.
  - The endpoint requires authentication.
  - The rate limit is enforced.

### 7D — Web wizard and dashboard list

- **Scope:**
  - 2-step wizard. Step 1: job link with autofill, company, JD, resume and document picker. Step 2: language, provider/model, question type, format, length, tone, extra instructions.
  - "Call Sessions" list on `/home` (status, company, created date).
  - Empty and error states; manual-entry fallback visible when import fails.
- **Dependencies:** 7A, 7B, 7C.
- **Testing:** `tsc`, ESLint, build; e2e per decision 14.
- **Acceptance:**
  - PRD journey J3 works end to end.
  - The form stays usable after an import failure.
  - The list shows only the signed-in user's sessions.

## 5. Contradictions and gaps found

1. **Composite FKs.** DATABASE.md §3 requires child rows to match the parent's `user_id` via composite FKs; existing models use plain `user_id` FKs. Following the spec means adding `UNIQUE (id, user_id)` to `resumes` and `documents` (Phase 3 tables).
2. **`ON DELETE SET NULL` with a composite FK** would try to null the NOT NULL `user_id`. Postgres 16 supports `SET NULL (resume_id)` with a column list, so this is solvable.
3. **`materials_snapshot`** is mentioned only in a DATABASE.md note, not in the column list.
4. **`deleted_at` vs hard delete.** The schema has `deleted_at`, but API.md and the PRD say delete removes all child data.
5. **Name clash.** ARCHITECTURE §7 defines `GenerationConfig` as the full compiled object; Phase 6 already shipped `GenerationConfig` with only temperature and max tokens.
6. **API.md** says create validates materials are "ready (or allows pending with warning)", which is ambiguous.
7. **`/sessions/{id}/start` and `/end`** serve the desktop client (Phase 10).
8. **Provider choice with no real adapter.** Outside local only the fake provider exists, so `/ai/options` is empty and no valid selection exists.
9. **Audit logging.** ARCHITECTURE lists session create/delete as audited, but no `audit_events` table exists.
10. **Rate limiting.** API.md says job-import is rate limited; no limiter exists.
11. **PromptBuilder inputs.** Delivered now, but its context, history and question inputs only come to life in Phase 8 (interface-mismatch risk).
12. **Route order.** `/sessions/job-import` must be registered before `/sessions/{id}`.

## 6. Open decisions and risks that could block Phase 7

- D-11 (compiler and builder) and D-20 (job import) are only "Proposed", not Accepted.
- OQ-9, OQ-13, OQ-16 and OQ-17 are open (see section 7).

**Risks:**
- SSRF is the main security risk (7C).
- Job sites such as LinkedIn and Indeed often block scraping or forbid it in their terms, so import will fail often. Acceptable, because manual entry is the fallback.
- Prompt injection via `extra_instructions` and imported JD text (R-9). Phase 7 can deliver only structural protections (delimiting, sanitizing, tests); end-to-end robustness is verified in Phase 8.
- No web test tooling exists, so there is no e2e coverage unless tooling is added.
- Prompt wording cannot be validated against real models while no real adapter exists (OQ-4 still open).

## 7. Decisions requiring owner approval

| # | Decision | Recommendation |
|---|---|---|
| 1 | Approve the 7A–7D split and order | Yes |
| 2 | Accept D-11 and D-20 as written | Accept both |
| 3 | Materials readiness at create | Require `ready` (422 otherwise). The wizard lists only ready files; simpler and avoids stale statuses. |
| 4 | Provider and model in preferences | Optional; null means "server default chain". If provided, validate against the allowlist. Otherwise sessions cannot be created outside local. |
| 5 | Name of the compiled config | New `AnswerConfig` holding the preferences and wrapping the existing sampling `GenerationConfig`; update ARCHITECTURE §7. |
| 6 | Cross-user integrity | Composite FKs per DATABASE.md (small migration touching `resumes` and `documents`). The alternative is service-level checks plus tests, which departs from the spec. |
| 7 | Session delete | Hard delete; drop `deleted_at` use. |
| 8 | `start` / `end` endpoints | Defer to Phase 9/10. Phase 7 is create, read, update, delete only. |
| 9 | OQ-9 question type | Store `auto` or a fixed type. Per-question override is Phase 8; the classifier is Phase 12B. |
| 10 | OQ-13 languages | UI is English only for now; `language` means the answer language (en, hi, te). Code-mixed handling is out of scope. |
| 11 | OQ-16 limits | JD up to 20,000 characters; `extra_instructions` up to 1,000 (per DATABASE.md); up to 10 documents per session. All configurable. These numbers are a proposal. |
| 12 | OQ-17 job sites | Generic extraction only, no per-site adapters; robots.txt not enforced in v1 (the architecture says "where applicable"). Failures fall back to manual entry. |
| 13 | New dependencies and infrastructure | Move `httpx` to a runtime dependency for 7C; use the standard-library HTML parser; a small Redis fixed-window limiter scoped to job-import only. |
| 14 | Wizard e2e tooling | Playwright is a new dependency. Alternatives: vitest component tests plus a manual checklist, or API-level tests plus manual verification. Add Playwright in 7D only if approved. |
| 15 | Inline upload inside the wizard | PRD J3 says "pick or upload". Limit v1 to picking existing ready files with a link to upload on the dashboard; inline upload requires waiting for indexing. |
| 16 | Audit events | Defer to Phase 13; Phase 7 logs IDs only. |

## 8. Scope check against Phase 8 and later

**Excluded from this plan:**
- `/sessions/{id}/questions` and anything under it.
- Context assembly and any retrieval call from sessions.
- LLM calls and SSE streaming.
- Answer and question persistence, and latency metrics.
- The question classifier.
- Metrics in the sessions list.
- `start` and `end` endpoints, WebSocket, desktop, screenshots and STT.
- Real provider adapters (still blocked on OQ-4).

**Deliberate touchpoint:** `PromptBuilder`, which Phase 7 owns per PHASES.md. It is built and tested with synthetic inputs and no retrieval wiring.

**Expected changes outside the new code:**
- **Docs:** DECISIONS (new entries), DATABASE, API, ARCHITECTURE §7, PHASES, RISKS, CLAUDE.md, `.env.example`.
- **Code and config:** a migration touching `resumes` and `documents` (if decision 6 is accepted), `config.py` (limits and import settings), `pyproject.toml` (`httpx` runtime dependency).

## 9. Testing strategy summary

- Follows TESTING_STRATEGY.md: targeted tests per sub-phase plus impact-matrix regression. The full regression (M3) runs at the end of Phase 8, not in Phase 7.
- AI-dependent tests use fakes. The SSRF suite uses a local fake server and fake resolver, never the real internet.
- User-isolation tests are mandatory for every new endpoint and query.
- Each sub-phase includes at least two mutation checks on its key protections (for example: ownership check removed, SSRF IP block removed, prompt delimiter removed).

**Next step:** wait for explicit approval and answers to the section 7 table before any implementation.
