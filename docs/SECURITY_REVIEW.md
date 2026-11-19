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

- **Web CSP has no script policy (Phase 13A, D-54):** the web app sends framing, base-URI, form-action and object restrictions but no `script-src`/`style-src`, because Next.js needs per-request nonces and dynamic rendering for that. Injected inline script would therefore not be blocked by CSP. Keep the web app on a trusted network and revisit before wider exposure.
- **Scanner coverage (Phase 13A):** CodeQL, gitleaks, `pip-audit` and `npm audit` run in `.github/workflows/security.yml`. Triage results are in the Phase 13A report, not here.

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

---

# Addendum (2026-11-18): Desktop and product-upgrade review

Status: **PROPOSED review notes. Source inspection only; nothing was executed and no code was changed.** Covers the Electron client and audio/screenshot paths added after the Phase 3 review above. It is not a penetration test.

## Controls verified by reading the source
| Control | Evidence |
|---|---|
| Context isolation and renderer sandbox on | `apps/desktop/src/main/index.ts:200-202` |
| Navigation and new windows denied | `index.ts:209-218` |
| Permissions denied by default; microphone only while armed; display video never; system audio only as loopback with no video source | `main/media-permissions.ts`, wiring in `index.ts:220-240` |
| Audio only starts from an explicit user action, with window visible; auto-stop on hide, sign-out, session change, 2 h cap; buffers zero-filled | `main/audio-service.ts` header and constants |
| Screenshots only after explicit start and a cancellable visible countdown; memory only; one module touches `desktopCapturer` | `main/capture-service.ts`, `main/capture-electron.ts` |
| Refresh token encrypted with DPAPI via `safeStorage`; refuses to persist if unavailable | `main/secure-refresh-store.ts` |
| Electron fuses (RunAsNode off, ASAR integrity, only-load-from-ASAR, etc.) | `apps/desktop/package.json` `electronFuses` |
| Provider keys never on the desktop | `docs/DESKTOP_RELEASE.md` section 3 |

## Findings and recommendations
| # | Severity | Finding | Recommendation (milestone) |
|---|---|---|---|
| D-1 | Medium | No acceptable-use acknowledgement before audio/screenshot use (OQ-15 open) | M-1 |
| D-2 | Medium | Installer is unsigned; no update integrity path | M-10, M-11 |
| D-3 | Medium | Providers never validated with real accounts; data-handling terms unverified (OQ-4/5) | M-3 before sending real interview content |
| D-4 | Low | No renderer crash recovery or local diagnostics | M-5 |
| D-5 | Low | No audit-event writers; no account deletion/export (PRIVACY.md, PHASE_13_14_PLAN R5/R6) | M-12 |
| D-6 | Low | Desktop CI runs on Linux only; Windows-specific controls (DPAPI, NSIS, loopback) are manual-only | M-10 |
| D-7 | Info | `desktopCapturer.getSources` renders every display and drops unselected ones (documented in `capture-electron.ts`); not kept or sent | Accept; revisit if Electron adds per-display capture |
| D-8 | Info | Earlier review items (deployment gates, `/metrics` exposure, single-process limits) still apply and are unchanged | PHASE_13_14_PLAN |

## Capture protection and window privacy (Step 5 position)
- **Current state:** `setContentProtection` is not used anywhere in `apps/desktop/src` (searched). The window is a normal visible window with a tray icon and visible listening/capture indicators. This is the correct baseline and must be preserved: indicators, countdown and consent are never suppressed.
- **Legitimate privacy purposes** that are in scope: protecting the user's own resume/notes from accidental exposure during an allowed screen share; accessibility (high contrast, scalable text, reduced motion); a visible "recording" state that the user can always see; a one-click "pause everything" that stops audio and clears held screenshots.
- **Recommendation:** do **not** build a feature whose effect is to hide the assistant from screen sharing, recording, proctoring or monitoring software by default. Excluding the window from capture is exactly the capability that would conceal use from other parties, and this product cannot know whether a given interview or assessment permits assistance.
- **If the owner later approves a content-protection toggle (M-14)**, minimum conditions: off by default; explicit label stating what it does and that the user must follow the rules of the interview, meeting or assessment; visible indicator while on; never combined with hiding the taskbar/tray entry; no attempt to evade or detect monitoring software; included in the M-1 acknowledgement; documented in PRIVACY.md.
- **Safe additions regardless:** a "Pause all" control (stops audio, discards held screenshot), a panic-visible indicator that stays on screen while listening, and accessibility options.

## Verification record for this addendum
Executed: file reads and text searches only. Not executed: tests, build, packaging, any provider call. Statements of "missing" mean no match in the searched directories.

## M-8 addendum: rendering model output (2026-11-18)
- The desktop answer view now interprets a small Markdown subset (`renderer/src/answer/markdown.ts`). It builds React elements and text nodes only: no `dangerouslySetInnerHTML`, no HTML pass-through, no links, no images, no remote resource. A hostile language label on a code fence is reduced to a known name or the word "Code" and is never used as markup or a class name. Tests: `answer-markdown.test.ts`, `answer-render.test.ts`.
- Code the model writes is displayed and copied, never executed by the app. The evaluation runner (`scripts/coding_exec.py`) executes model-written Python for synthetic evaluation prompts only; it is **not a sandbox** (file and network access are not blocked) and must not be run on output from real interview content.
- The general-answer validator now excludes fenced code from some checks. Evidence tags, prompt delimiters and e-mail addresses are still rejected inside code; test `test_evidence_tags_and_prompt_delimiters_still_count_as_leaks_even_inside_code`.
- The v5 system prompt adds a rule that the model must not claim code was run. It is an instruction, not a guarantee; the evaluation counted zero such claims in 84 answers (three runs of 28).
