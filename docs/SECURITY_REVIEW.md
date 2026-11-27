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

## M-15 addendum: capture privacy toggle implemented (2026-11-22)
- The owner approved the conditional toggle (M-14, merged into M-15) in the 2026-11-22 directive. It meets every minimum condition above: off by default; explanation and rules reminder before enabling and in the native confirmation; non-dismissible in-window indicator; tray, taskbar and process untouched (guard-tested); no detection or evasion of monitoring software; wording added to the acknowledgement (version 2); documented in PRIVACY.md.
- The original recommendation is not withdrawn. Residual risk: the setting works against ordinary screen sharing by design, and the product cannot verify intent or whether the interview rules allow it.
- Added control: Electron's remote-session window exclusion is disabled so remote-access tools still see the window (not verified in a real remote session).
- Evidence and limits: `docs/WINDOW_CAPTURE_PRIVACY.md`.



---

## 2026-11-23: remediation sprint (D-82)

Full evidence and per-finding state: `docs/SECURITY_ASSESSMENT_PHASE_E.md`, "Remediation log". This section keeps the reasoning that belongs with the standing review.

### SEC-08 handoff: assessment, recommended architecture and trade-offs

**Today.** The signed-in web user asks for a code; the web page opens `interviewassistant://start?h=<code>`; the desktop redeems the code once, without any other credential, and receives a full login for that account (a separate 14-day refresh-token family of type `desktop`) plus the prepared session. The code is 256-bit random, lives 60 seconds, is consumed by the first attempt, is stored only as a hash, is never logged, and redemption is rate-limited per client.

**Threat.** Whoever sees the link in its 60 second life and redeems first becomes that user on a desktop session: another program registered for the same URL scheme, a browser extension, a shared screen, shell history of a launcher. The code authenticates the *web user*, not the *desktop that is meant to receive it*, and what it buys is the whole account, not one session.

**Options considered.**

| Option | Closes the interception? | Cost | Verdict |
|---|---|---|---|
| A. Keep as is (short life, single use, rate limit) | no | none | acceptable only as a stop-gap |
| B. PKCE-style binding: the desktop makes a random verifier, keeps it, and gives the web page only its SHA-256 (the challenge); the code is bound to the challenge; redemption needs the verifier | **yes**: a captured link is useless without the verifier, which never leaves the desktop | the desktop must be running (or start first) to create the challenge before the web page asks for the code: a flow change in both clients | **recommended** |
| C. Challenge handed over through the running app's local presence server (`127.0.0.1:47821`) | yes, for the case where the app is already running | any local process can also read a challenge from that port, so it stops a *link* thief but not a local attacker who can talk to the port; the app must be running | useful as the transport for B, not a substitute |
| D. Bind the code to the redeeming client's IP address | partly | web and desktop on the same machine can still show different addresses (IPv4 and IPv6, split tunnels, proxies), so real users would be locked out | rejected |
| E. Mint a narrower credential (one session, short life, no refresh) | limits the damage, does not stop the theft | needs scoped-token support across the API and the desktop | worthwhile later, in addition to B |
| F. Make the user confirm in the desktop before it signs in | no (the thief runs the same step) | friction | not a control |

**Recommended architecture (B).** The desktop generates the verifier (43 to 128 URL-safe characters) in memory, derives the challenge (`base64url(SHA-256(verifier))`) and exposes it to the web page through the existing desktop-presence handshake or a first deep link (`interviewassistant://prepare`); the page sends it in the body of `POST /sessions/{id}/handoff`; the link carries only the code; the desktop sends `code_verifier` with `POST /sessions/handoff/redeem`. Then set `HANDOFF_REQUIRE_CHALLENGE=true` so unbound codes are refused. A wrong or missing verifier still burns the code and gets the same `404 handoff_invalid` as every other failure. After that, consider E (a narrower token) so a stolen desktop session is not a full account.

**What was built in this sprint.** Only the server half (optional challenge and verifier, the enforcement switch, tests, `docs/API.md`). It changes nothing for users because the default is off and neither client sends a challenge. **The finding stays Open until both clients adopt it and the switch is turned on.** Trade-offs accepted: the web flow needs the desktop to be reachable before the code is requested; if the desktop restarts between the two steps its verifier is gone and the user clicks again; a wrong verifier burns the code (a denial of service for that one attempt, never an access).

### Other standing-review changes

* **Configuration (SEC-07).** The default `APP_ENV=local` is now only valid when every service is on this machine. This turns "I forgot to set it" from a silent downgrade into a start-up failure.
* **Credentials (SEC-12).** The privileged operations identity exists only in the opt-in `maintenance` service. Application containers hold the runtime login only, and the migration job holds only the migration login. This was verified against real database permissions, not only configuration text.
* **Updates (SEC-09).** The update manifest is authenticated with a pinned Ed25519 key and the installer is bound to it by hash, so a feed writer can no longer present an old signed build as a new one. The code-signing certificate and the manifest key are deliberately separate. Real-world validation is BLOCKED on the owner's key, a signed build and a feed.
* **Backups (SEC-10, SEC-11).** Encryption is mandatory and backups are authenticated; restore verifies everything before it writes anything. Old-format backups are refused.
* **Data stores (SEC-16).** Postgres and Redis run unprivileged, read-only and capability-free on a network with no internet route. Verified on real containers locally.
* **Deletion and retention (SEC-13).** Documented in `docs/DATA_LIFECYCLE.md` as a proposal. Nothing irreversible was implemented. Owner decisions are listed there.
* **Logs (SEC-03, earlier).** Exception messages are never written; frames and class names only.

### Still not claimed

No production environment, signed build, real update feed, proxy or restore drill on production infrastructure exists, so none of the above is PRODUCTION-VALIDATED. M-15 behaviour on real Windows capture tools is NOT TESTED (`docs/M15_OWNER_APPROVAL_AND_TEST_PLAN.md`).


---

## 2026-11-23, second sprint: closure work (D-83)

Evidence and per-finding state: `docs/SECURITY_ASSESSMENT_PHASE_E.md`, "Remediation log, sprint 3". Harnesses: `docs/SECURITY_TESTING.md`.

### SEC-08 handoff: now built in both clients

The architecture recommended above (option B) is implemented, with option C's loopback transport:

1. The desktop keeps a **verifier** (32 random bytes, 43 URL-safe characters) in memory and exposes only its SHA-256, the **challenge**, at `http://127.0.0.1:47821/ia-presence/challenge`. The endpoint has the presence beacon's Host-header check (DNS-rebinding hygiene), answers GET and the private-network preflight, returns exactly `{"challenge","expires_in"}`, is rate limited (30 a minute) and bounded (16 pending).
2. The web page fetches it after the presence check and before asking for a code, and sends it in the body of `POST /sessions/{id}/handoff` (`code_challenge`).
3. The link becomes `interviewassistant://start?h=<code>&c=<challenge>`. It contains no secret: the challenge is a hash.
4. The desktop takes the verifier for `c` (single use, 120 s lifetime) and sends it as `code_verifier` in the redeem body. If the verifier is gone the code is not sent at all.
5. The API checks `base64url(SHA-256(verifier)) == challenge` in constant time. A missing or wrong verifier gives the same 404 as every other failure and still consumes the code. With `HANDOFF_REQUIRE_CHALLENGE=true` (the production default) an unbound code is neither issued nor accepted.

What this does and does not stop. It stops a program that captures the link (or any observer of it) from signing in: it has the code and the challenge but not the verifier. It does **not** stop a program running as the same Windows user from reading the desktop app's memory, or from asking the beacon for a challenge of its own and then using the real web session: those already have the user's privileges. Residual: any web page can ask the beacon for a challenge, which can interrupt (not hijack) a legitimate attempt; rate limiting and the bound limit it.

Trade-offs accepted: the desktop app must be running (the web page already required that, through the presence check); a desktop older than this change fails against a server that requires binding and the user is told to update; a wrong verifier burns the code (a denial of one attempt, never access).

### Measured, and a correction

* **SEC-14.** Real Caddy, web and API containers with distinct client addresses showed the API receiving a single-entry `X-Forwarded-For` with the real client in both layouts, so the per-client login limit is correct through the Next.js rewrite as well as through the direct route. The earlier concern was not reproduced. My first attempt at this test was wrong (clients reused one address) and wrongly suggested the new layout failed; the corrected result is in the Phase E log.
* **SEC-24.** The `GrantFileProtocolExtraPrivileges` fuse cannot be disabled in the current design: with it off the packaged renderer does not render. It stays enabled; the controls that compensate for it were exercised on the real packaged app (isolation, navigation, CSP, permissions, IPC validation).
* **WebSocket gateway.** 28 attack checks on a live loopback instance, all refused or contained as designed: handshake without, with unknown, replayed, expired and mismatched tickets; another user's ticket; a bad Origin; oversized and binary frames; malformed and flooded messages; wrong protocol version and a missing hello; the per-user connection cap and the supersede rule; the ticket-request rate limit; no ticket in the server log. A client without an `Origin` header (not a browser) is accepted by design, because browsers always send one and the single-use ticket is still required.
* **Backups.** A real PostgreSQL round trip restored every row count and a byte-identical upload; thirteen kinds of damage and a wrong passphrase were each refused with exit 4 before any database or uploads write.

### Still not claimed

No production system, signed build, real update feed, hosted proxy or restore drill on production infrastructure exists; none of this is PRODUCTION-VALIDATED. The M-15 capture behaviour on real tools, multiple monitors and screen readers is NOT TESTED until the owner's session (`docs/M15_MANUAL_SESSION_RUNBOOK.md`).

## Update 2026-11-26: M-15 manual validation owner-confirmed

The owner has confirmed that M-15 was manually validated and works successfully (recorded first in D-84). This supersedes the two "NOT TESTED" statements about M-15 above only to the extent of that overall confirmation: no individual tool, Windows version, monitor layout or screen reader result was reported, so none is claimed. The security position is unchanged: the setting is a request to Windows and not a guarantee; it hides no process, tray or taskbar entry; remote-session exclusion stays disabled; it does not evade monitoring or proctoring software. Nothing here is PRODUCTION-VALIDATED.
