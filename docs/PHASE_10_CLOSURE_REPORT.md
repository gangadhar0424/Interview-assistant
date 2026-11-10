# Phase 10 — Closure Report (Electron Windows Desktop MVP)

Date: 2026-11-10. Decision: **Phase 10B accepted; Phase 10 accepted and complete** (owner-approved after manual testing). Nothing has been committed or pushed; that needs separate owner approval.

## 1. Deliverables vs acceptance criteria

Acceptance (docs/PHASES.md, Phase 10): login/logout; select session; typed Q streams and can be cancelled; history loads; credentials stay outside the renderer; IPC rejects untrusted renderers; window remains visible and user-controlled.

| Criterion | Evidence | Result |
|---|---|---|
| Login / logout | 10A and 10B end-to-end against the real local backend; wrong password gives a safe error; sign-out deletes the stored token; restart resumes the session | Met |
| Select session | Session list, readiness and resume shown; cross-user/unknown session returns a safe 404 | Met |
| Typed Q streams; cancel | Streamed to Complete in order; cancel issued right after start produced a single cancelled history entry. Cancel against a slow model was **not** tested (see §4) | Met for the deterministic fake provider; slow-stream cancel deferred |
| History loads, isolated per session | Verified | Met |
| Credentials outside the renderer | No tokens in DOM, storage, cookies or IPC payloads; refresh token only as a DPAPI-encrypted blob; no JWT in any app-data file | Met |
| IPC rejects untrusted senders | Sender-URL validation per handler (code review, unit tests); bridge exposes 15 named functions | Met |
| Window visible and user-controlled | Visible floating window, tray, always-on-top, opacity floor of 65%, close-to-tray; no capture or hidden behaviour. Owner confirmed tray, minimize/restore, tray Quit and scaling | Met |

## 2. What was done

- **10A (validation):** typecheck, unit tests, build, unsigned NSIS package, packaged launch, full end-to-end run against the real local backend, security probes. No source change.
- **10B (hardening), decision D-49:** CSP `style-src 'self'` and `form-action 'none'` (0 violations, positive control confirmed); release fuses (RunAsNode, NODE_OPTIONS, inspect arguments off; cookie encryption, asar integrity, only-load-from-asar on; asar tamper test crashed the app as intended); one fuse (`GrantFileProtocolExtraPrivileges` off) tested and **rejected** because it breaks startup; project-owned app icon; specific error messages for out-of-range opacity and non-string copy; "Minimize" label corrected; 6 new unit tests.
- **Documentation:** `PHASE_10A_REPORT.md`, `PHASE_10B_REPORT.md`, `PHASE_10B_MANUAL_TEST_PLAN.md`, `DESKTOP_RELEASE.md` (release process, signing and auto-update options), D-49 (accepted) and D-50 (proposed), `PHASES.md`, `DESKTOP_WINDOWS.md`, `DESKTOP_MANUAL_CHECKLIST.md`, and the status lines in `CLAUDE.md` and `DECISIONS.md` (D-47 addendum).

## 3. Tests

- **Automated:** typecheck pass; 33/33 desktop unit tests; production build; Windows package build; packaged-app smoke and end-to-end regression against a real local backend (fake LLM provider, scratch database); `npm audit --omit=dev` 0 findings.
- **Owner manual (packaged build, 2026-11-10):** NSIS installation PASS; first launch PASS; tray icon and menu PASS; minimize and restore from tray PASS; tray Quit PASS; display scaling PASS; uninstall and reinstall PASS. The 10A observation (window found minimized with always-on-top off) did **not** occur.
- **Not tested:** multi-monitor; mid-stream cancel against a slow provider.

## 4. Deferred items (none block closure)

| Item | Why deferred | Where tracked |
|---|---|---|
| Multi-monitor manual test | No second display available | `PHASE_10B_MANUAL_TEST_PLAN.md` §7; re-run when a second display is available |
| Mid-stream cancel against a slow provider | No safe slow provider (fake answers in ms) | Manual plan §8; can be covered when a real/local model is validated |
| Real-provider validation (OpenAI/Ollama), including provider data-handling terms | Not needed for Phase 10 acceptance (owner decision) | OQ-4 in `RISKS_AND_OPEN_QUESTIONS.md` |
| Code signing | Release/deployment decision for when a production release is prepared | D-50 (Proposed), `DESKTOP_RELEASE.md`; Phase 14 |
| Auto-update | Same; requires signing first | D-50, Phase 14 |
| WebSocket client in the desktop app | The web page and desktop stay on SSE; WS gateway exists from Phase 9 | Phase 9 notes |
| Unconfirmed 10A minimization observation | Not reproduced in 10A, 10B controlled test, or owner testing | Recorded as observed once, unconfirmed; reopen if it recurs |

## 5. Known risks and technical debt carried forward

1. `GrantFileProtocolExtraPrivileges` cannot be disabled until the renderer is served from a custom protocol instead of `file://`.
2. `--remote-debugging-port` is not covered by any fuse; the inspector fuses are set but their runtime effect was not demonstrated.
3. Six moderate dev-only advisories (one minimatch advisory in the electron-builder chain) are accepted, not suppressed; revisit after upstream updates.
4. Encrypted refresh token and settings remain in `%APPDATA%` after uninstall by default (documented; sign out first to remove the token).
5. The installer is unsigned: SmartScreen warnings are expected, and builds must not be distributed beyond trusted testers.
6. No packaged-app smoke in CI.

## 6. Scope boundaries respected

No code signing or auto-update was implemented. No audio, screenshot, hidden-capture or evasion code exists. The backend, the web app, and the separate uncommitted grounding work (Issue 1/Issue 2, migration 0009 and its documentation) were not touched in the closure edits. Documentation edits made at closure: `CLAUDE.md` (status lines only), `docs/PHASES.md`, `docs/DECISIONS.md` (D-47 addendum wording only), `docs/PHASE_10B_REPORT.md`, `docs/PHASE_10B_MANUAL_TEST_PLAN.md`, and this report.

## 7. Recommended next phase

**Phase 11 — Screenshot Input.** It is the next phase in the roadmap, depends only on Phase 10 and the Phase 6 provider layer (both complete), and has no proof-of-concept prerequisite. It builds directly on the new desktop shell and adds a user-initiated capture path with a bounded, testable surface (explicit capture action, image validation/EXIF stripping, a `VisionProvider` port with a fake adapter, retention purge). Phase 12A/12B (audio) is riskier, needs PoC-2/PoC-3 first, and should come after the desktop capture pattern is proven. Phase 13/14 (security hardening, signing, deployment) fit after the capture features, when a production release is being prepared.

Pre-conditions to settle when Phase 11 starts: a vision provider sends screenshots to an external service, so the provider data-handling question (OQ-4) must be answered for images; capture must stay explicit, visibly indicated and user-initiated per `CLAUDE.md`.

I have stopped. No new phase has been started, and nothing is committed or pushed.
