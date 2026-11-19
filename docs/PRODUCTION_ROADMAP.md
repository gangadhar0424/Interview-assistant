# Production Roadmap

Status: **PROPOSED. Nothing here is approved or started.** Date: 2026-11-18. Per CLAUDE.md, each milestone needs explicit owner instruction before work begins, and the project's Phase Gate applies after each one. Inputs: PRODUCT_GAP_ANALYSIS.md, ARCHITECTURE_REVIEW.md, SECURITY_REVIEW.md (addendum), PERFORMANCE_BASELINE.md, PHASE_13_14_PLAN.md.

Relationship to existing plans: PHASE_13_14_PLAN.md covers backend hardening and deployment (R1-R14). This roadmap covers the **desktop/product-quality** gaps and references that plan rather than duplicating it. M-12 below points at it.

Global rules for every milestone: no existing feature removed; no new dependency unless named; new IPC channels go through `shared/contracts.ts` + `ipc-validation.ts`; no content in logs; user-isolation tests for any new backend endpoint; targeted tests per TESTING_STRATEGY.md; rollback = revert the milestone's commits (each is independent unless a dependency is listed).

## Priority order

| Order | Milestone | Why now |
|---|---|---|
| 1 | M-2 Baseline instrumentation | Unblocks every performance claim; code-only; no schema |
| 2 | M-1 Acceptable-use acknowledgement | Largest privacy/policy gap (OQ-15); small |
| 3 | M-3 Real-provider validation (STT + LLM) | Biggest unknown in the product (OQ-4/5); needs owner keys |
| 4 | M-4 Keyboard shortcuts | Clear usability gap, small, low risk |
| 5 | M-5 Crash recovery and diagnostics | Reliability |
| 6 | M-6 Multi-monitor and window placement | Deferred test debt |
| 7 | M-7 Question-detection evaluation and upgrade | Needs M-3 data |
| 8 | M-8 Coding-assistance quality | Needs M-3 data |
| 9 | M-9 Session management polish | Quality of life |
| 10-11 | M-10 Signing + Windows CI, M-11 Auto-update | Needs certificate decision |
| 12 | M-12 Backend hardening/deployment | PHASE_13_14_PLAN.md |
| later | M-13 Streaming STT; M-14 Optional content-protection toggle (owner decision); M-15 Windows screen-capture privacy protection (PROPOSED, owner decision, overlaps M-14) | Only if justified |

**Recommended first milestone: M-2.**

---

## M-1 Acceptable-use acknowledgement
- **Status (2026-11-18): implemented, pending owner review; see D-68 and PRIVACY.md.** Differences from this plan: acceptance is stored in the existing `settings.json` (per account, per wording version) instead of a database column, so there is no migration or API change; the web wizard only shows an information note because it never captures (the desktop enforces and asks). The wording needs owner approval.
- **Objective:** Before first audio or screenshot use, the user acknowledges that they are responsible for following the rules of the interview, meeting or assessment they use it in; text shown in the existing native consent flow and the web wizard. Resolves OQ-15.
- **Existing files affected:** `apps/desktop/src/main/audio-service.ts` (gate), `capture-service.ts` (gate), `shared/contracts.ts`, `ui/AudioPanel.tsx`, `ui/ScreenshotPanel.tsx`, `apps/web/components/SessionWizard.tsx`, `docs/PRIVACY.md`.
- **New files:** `apps/desktop/src/shared/acknowledgement.ts` (versioned text id), `apps/desktop/tests/acknowledgement.test.ts`.
- **Dependencies:** owner-approved wording.
- **Database:** optional `users.ack_version`/`acked_at` (migration 0012); alternative is device-local only (no migration). Decide first; default to local-only.
- **API:** none if local-only; one preference field if stored.
- **Security:** acknowledgement is enforced in main, not renderer; a stale version re-prompts.
- **Tests:** unit (gate refuses without ack, accepts after, re-prompts on version bump); IPC test; manual consent flow.
- **Acceptance:** audio/screenshot start is refused until acknowledged; text is visible and versioned; no behaviour change afterwards.
- **Risks/rollback:** wording is a legal/policy matter, not a code matter; revert the commits to remove the gate.

## M-2 Baseline instrumentation and benchmark
- **Status (2026-11-18): implemented, pending owner review; see D-67 and PERFORMANCE_BASELINE.md.** Local, startup and idle-resource rows are measured; provider, retrieval and LLM rows are UNMEASURED (no database or provider here) and depend on M-3. Differences from this plan: the report is printed to stdout instead of written to a file, and resources are sampled at events instead of every 5 s, because existing guard tests forbid disk writes and periodic timers in the main process.
- **Objective:** Fill PERFORMANCE_BASELINE.md with measured numbers.
- **Existing files:** `services/api/app/core/metrics.py`, `audio/service.py`, `qa/` SSE path, `apps/desktop/src/main/audio-service.ts`, `api-client.ts`, `sse.ts`, `index.ts`.
- **New files:** `apps/desktop/src/main/perf-marks.ts`, `services/api/tests/test_metrics_labels.py`, `scripts/bench/` (harness, no real audio committed).
- **Dependencies:** none new (`performance` API, `app.getAppMetrics`).
- **Database/API:** none (metrics only; `/metrics` stays network-restricted).
- **Security:** low-cardinality labels; no content; perf IPC is dev-only or aggregate-only.
- **Tests:** unit for marks and label sets; run the harness and record results with machine spec and commit.
- **Acceptance:** every cell in PERFORMANCE_BASELINE section 2 is measured or marked unmeasurable with a reason.
- **Risks/rollback:** negligible overhead; revert the commits.

## M-3 Real-provider validation (STT + LLM)
- **Status (2026-11-18): executed; results in `docs/PROVIDER_VALIDATION.md`, pending owner review.** Validated: Groq and Gemini (STT), Gemini (LLM, Groq limited to 3 timed answers by its 8,000 tokens/min limit), real embeddings and retrieval, failure behaviour. **Blocked by vendor accounts:** OpenAI (no credit), Anthropic (credit too low), Sarvam (no quota). **Blocked in code:** vision (only a fake provider exists, D-51). Hindi/Telugu and real human speech not tested.
- **Objective:** Run the existing STT, reasoning and vision adapters against real accounts; record quality and latency per language (en/hi/te); resolve OQ-4/OQ-5 or document blockers.
- **Existing files:** none changed unless defects are found; results into `docs/RAG_RESULTS.md`-style report `docs/PROVIDER_VALIDATION.md`.
- **Dependencies:** owner supplies keys and confirms data-handling terms; uses M-2 metrics.
- **Database/API:** none.
- **Security:** keys only in backend env; test audio is synthetic or consented; nothing committed.
- **Tests:** manual scripted runs plus the existing adapter contract tests.
- **Acceptance:** per-provider table of success rate, WER or manual accuracy sample, p50/p95; defect list.
- **Risks/rollback:** cost; cap spend; no code to roll back.

## M-4 Keyboard shortcuts
- **Status (2026-11-18): implemented, pending owner review and Windows manual testing; see D-69 and `docs/DESKTOP_SHORTCUTS.md`.** Differences from this plan: **no shortcut starts listening or capture** (only show/hide/toggle and "go to the audio/screenshot panel"); every shortcut must be Shift plus Ctrl or Alt plus a key (Ctrl+Alt is refused because it is AltGr on many layouts); preferences are stored in `settings.json` outside `DesktopSettings` with two new channels, `app:get-shortcuts` and `app:set-shortcuts`. Automated tests are mocked and do not establish real Windows behaviour.
- **Objective:** Configurable global shortcuts: show/hide window, focus question box, submit, start/stop listening, start screenshot countdown, cancel answer.
- **Existing files:** `main/index.ts`, `main/ipc.ts`, `main/settings-store.ts`, `shared/contracts.ts`, `ui/App.tsx`, `renderer/src/store.ts`.
- **New files:** `main/shortcuts.ts`, `tests/shortcuts.test.ts`.
- **Dependencies:** Electron `globalShortcut` (built in).
- **Database/API:** none.
- **Security:** shortcuts invoke the same service methods, so every gate still applies: window must be visible, countdown still shown, indicators still shown, audio still needs the native consent and Start. No shortcut starts hidden capture. Conflicts reported to the user; accelerators validated against an allow-list pattern.
- **Tests:** unit with a fake `globalShortcut` port; gate-bypass tests (shortcut while window hidden is refused); settings validation.
- **Acceptance:** each action works from another focused app; hidden-window capture attempts are refused; user can disable/rebind.
- **Risks/rollback:** OS shortcut conflicts; settings flag to disable all shortcuts; revert commits.

## M-5 Crash recovery and diagnostics
- **Status (2026-11-18): implemented, pending owner review and Windows manual testing; see D-70 and `docs/DESKTOP_TROUBLESHOOTING.md`.** Differences from this plan: diagnostics are **in memory only** (not a rotated file in `userData`), because the main process may write only the settings and token files and a guard test enforces it; unhandled main-process errors are **not** intercepted (registering handlers would change Electron's default behaviour); a tray "Reload window" item and a native failure dialog provide the user-triggered retry. One real renderer crash was exercised on Windows with the built app; the rest of the Windows checks remain.
- **Objective:** Recover from renderer crash and unhandled main errors without losing sign-in; local IDs-only diagnostic log; "copy diagnostics" action.
- **Existing files:** `main/index.ts`, `main/ipc.ts:227`, `ui/App.tsx`.
- **New files:** `main/recovery.ts`, `main/diagnostics-log.ts`, tests.
- **Dependencies:** none. No remote crash reporter (owner decision).
- **Database/API:** none.
- **Security:** log contains event codes and IDs only; size-capped rotation in `userData`; audio/capture stop on any crash path.
- **Tests:** unit with fake window events (`render-process-gone`, `unresponsive`); log-redaction test.
- **Acceptance:** killing the renderer reloads once and shows a clear message; listening and capture are stopped; log has no content.
- **Risks/rollback:** reload loops; cap at one automatic reload per minute; revert commits.

## M-6 Multi-monitor and window placement
- **Status (2026-11-18): implemented, pending owner review and multi-monitor Windows testing; see D-71 and `docs/MULTI_MONITOR_TESTING.md`.** Differences from this plan: placement is stored per mode in `settings.json` (`windowPlacement`, written only after the window is placed) and the capture picker is unchanged; a Settings, Displays section lists displays and moves the assistant to one; placement saves the size it intended because Electron at 125% scaling returns wider windows and growing ones when re-applied (measured). Only a single-display laptop could be tested on real Windows; two-monitor, mixed-scaling, docking, sleep and remote-desktop checks remain.
- **Objective:** Complete the deferred multi-monitor test; persist window position per display; handle display add/remove and DPI change.
- **Existing files:** `main/window-controller.ts`, `main/settings-store.ts`, `capture-electron.ts`, `docs/DESKTOP_MANUAL_CHECKLIST.md`.
- **New files:** `main/window-placement.ts`, tests.
- **Dependencies:** a two-monitor test machine.
- **Database/API:** none.
- **Security:** placement never moves the window off every display; no capture behaviour change.
- **Tests:** unit with fake `screen` port; manual checklist on two monitors with different DPI.
- **Acceptance:** window restores to a valid display after disconnect; capture picker lists correct displays; manual checklist signed off.
- **Risks/rollback:** fall back to default placement on any invalid saved state.

## M-7 Question-detection evaluation and upgrade
- **Status (2026-11-18): implemented, pending owner review; see D-72 and `docs/QUESTION_DETECTION_EVALUATION.md`.** Differences from this plan: the dataset is **synthetic, single-author and not native-speaker reviewed** (no M-3 transcripts were available for Hindi or Telugu), it is split into a tuning and a held-out part plus a harder challenge set written after the freeze; the existing detector was **extended, not replaced**, because it did not detect Hindi or Telugu at all (recall 0) and had a 0.36 English false-positive rate; the held-out result meets every pre-registered threshold but the challenge set does not (precision 0.67, false-positive rate 0.39), so real human-speech validation is still required. The milestone brief assumed confirmation and auto-answer off by default; D-59 already answers a detected question at once after Listen, and that was preserved.
- **Objective:** Build an en/hi/te labelled set; measure the heuristic; improve only if it fails an agreed threshold.
- **Existing files:** `services/api/app/qa/detection.py`, `audio/service.py`.
- **New files:** `services/api/tests/eval/detection_set.jsonl` (synthetic), `scripts/eval_detection.py`.
- **Dependencies:** M-3 transcripts for realistic noise.
- **Database/API:** none; response schema unchanged.
- **Security:** synthetic data only.
- **Tests:** eval script plus unit tests for new rules.
- **Acceptance:** documented precision/recall per language; threshold agreed with owner; no regression on the existing set.
- **Risks/rollback:** feature flag to the old heuristic.

## M-8 Coding-assistance quality
- **Status (2026-11-18): implemented, pending owner review; see D-73 and `docs/CODING_ASSISTANCE_EVALUATION.md`.** Delivered: prompt v5 (structured coding answers, assumptions, never claim code was run), a coding output-token floor, validator fix for code, screenshot extraction prompt v2, a dependency-free Markdown and code-block renderer with per-block copy, and a 28-problem synthetic evaluation with provider latency and cost. **Not done:** human review of any answer or of the dataset; the compact and expanded window sizes were not inspected in the running app (manual checklist added); Hindi and Telugu coding answers were not evaluated; the general-answer judge was not run on code; the web Q&A page is unchanged. Differences from this plan: no new prompt snapshot file was added (the existing snapshots were regenerated for v5); no markdown renderer was present, so a minimal one was written instead of adding a dependency.
- **Objective:** Make coding answers usable in a small window: code blocks with copy, language handling, approach/complexity/test sections, scroll behaviour; screenshot-of-problem flow tuned.
- **Existing files:** `preferences/templates/` (via `PromptBuilder` only), `ui/App.tsx` answer rendering, `screenshots/service.py` prompt.
- **New files:** renderer code-block component, template snapshot updates (`tests/snapshots/prompts`).
- **Dependencies:** M-3 results; a markdown renderer only if one is not already present (check first).
- **Database/API:** none.
- **Security:** render answer text as text/markdown with no raw HTML.
- **Tests:** prompt snapshot tests, renderer tests, manual coding walkthrough.
- **Acceptance:** a sample of 10 coding prompts renders correctly in compact and expanded sizes.
- **Risks/rollback:** template version bump; previous version kept (versioned templates exist: `v3`, `v4`).

## M-9 Session management polish
- **Status (2026-11-19): implemented, pending owner review; see D-74 and `docs/SESSION_MANAGEMENT.md`.** Delivered: derived session state, last activity and question count; complete/reopen endpoints; idempotent create (`Idempotency-Key`); user-triggered JSON/Markdown export (desktop: native warning and save dialog in the main process; web: confirmation then download); desktop and web list controls (rename, complete/reopen, export, delete with confirmation, current/active/interrupted indicators); resume-most-recent selection; switching stops audio, a pending screenshot and a running answer. **Not done:** human exercise of the native dialogs, the browser download and the new controls (checklist added); the web session page has no complete/export controls; list order is still by creation time; no stored `interrupted` status. Differences from this plan: it said "no new API" and "transcript export"; completing a session and export needed new endpoints, and there is no transcript to export (none is stored), so the export is session details plus questions and answers. No migration.
- **Objective:** Desktop session switcher/list, resume last session, optional user-initiated transcript export (never automatic).
- **Existing files:** `ui/CreateSessionPage.tsx`, `ui/App.tsx`, `main/api-client.ts`, `contracts.ts`.
- **Dependencies:** none. **Database/API:** reuse existing session GETs; none new expected.
- **Security:** export is explicit, user-chosen path, warns that it writes content to disk.
- **Tests:** IPC and store tests; user-isolation test unchanged.
- **Acceptance:** switch sessions without restart; audio stops on switch (existing behaviour preserved).
- **Risks/rollback:** low; revert.

## M-10 Signing, installer verification, Windows CI
- **Objective:** Signed installer; `windows-latest` CI job that runs `package:win`, verifies fuses, records SHA-256.
- **Existing files:** `apps/desktop/package.json`, `.github/workflows/ci.yml`, `docs/DESKTOP_RELEASE.md`.
- **New files:** `.github/workflows/desktop-release.yml`.
- **Dependencies:** owner buys/chooses certificate or cloud signing (D-50). **Database/API:** none.
- **Security:** signing secrets in CI secret store only; actions pinned by SHA as in `security.yml`.
- **Tests:** CI job; manual clean-VM install, upgrade-over, uninstall.
- **Acceptance:** `Get-AuthenticodeSignature` reports Valid with timestamp; fuses verified in CI.
- **Risks/rollback:** keep unsigned path for internal testers.

## M-11 Auto-update
- **Objective:** Verified, user-visible updates.
- **Existing files:** `main/index.ts`, `package.json` (`publish` config). **New files:** `main/updater.ts`, tests.
- **Dependencies:** M-10; `electron-updater` (new dependency, needs approval); an update host.
- **Security:** only signed updates from the pinned publisher; no silent install without user setting; rollback to previous version documented.
- **Tests:** unit with fake updater port; manual update from N to N+1 on a VM.
- **Acceptance:** update applies, restarts, preserves sign-in; tampered artifact refused.
- **Risks/rollback:** feature flag; disable via setting.

## M-12 Backend hardening and deployment
Execute from PHASE_13_14_PLAN.md (R1-R14). Not duplicated here.

## M-15 Windows screen-capture privacy protection (PROPOSED; requested as "M-12", renumbered because M-12 is taken)
- **Status: PROPOSED. Not approved, not started, nothing implemented, and `docs/WINDOW_CAPTURE_PRIVACY.md` is not created yet (it is a deliverable of this milestone).** The owner asked for it to sit below M-12; the number M-12 already means backend hardening, and renumbering M-12 to M-14 would change existing references, so it is numbered M-15. Owner to confirm the number.
- **Relationship to M-14 and the security position (owner decision needed):** this is the same capability as M-14, a window that screen sharing and recording can omit. `SECURITY_REVIEW.md` ("Capture protection and window privacy") recommends against building it by default because it can conceal the assistant's use from other parties, and requires a written owner decision first. The conditions below include that review's minimum conditions; approving M-15 should replace or close M-14 so there is one record.
- **Objective:** a user-controlled setting that asks Windows to leave the assistant's own window out of supported screen-capture operations, for accessibility and for keeping private material (resume, notes) from being exposed in an allowed screen share.
- **Technical investigation (first deliverable; no code until reported):** Windows `SetWindowDisplayAffinity` and `WDA_EXCLUDEFROMCAPTURE` (Windows 10 version 2004 and later; earlier versions fall back to a black window, `WDA_MONITOR`); Electron `BrowserWindow.setContentProtection()` (preferred if it maps to the same flag on the pinned Electron 45); whether any native Windows code is needed (expected: no, and no new native dependency).
- **Functional requirements:** (1) a setting, off by default; (2) explanation of behaviour and limits shown before it can be turned on; (3) applied only to the app's own top-level window; (4) can be turned on and off; (5) re-applied after window creation, window recreation and renderer crash recovery (D-70); (6) correct in compact and expanded layouts; (7) holds when the window moves between monitors (D-71); (8) detects an unsupported Windows version or an API failure; (9) reports whether the request was accepted; (10) a clear message and unchanged behaviour when protection is unavailable.
- **Privacy and transparency (binding):** never claims the window is invisible to everything; modifies no other application's window; hides no process, tray icon or taskbar entry; does not detect, evade or bypass third-party security, proctoring or monitoring tools; leaves accessibility features working; does not change audio, screenshot or answer generation; states that users remain responsible for the rules of their interview, workplace or assessment.
- **Conditions carried over from the security review:** off by default; a visible indicator inside the window while it is on; never combined with hiding the taskbar or tray entry; included in the M-1 acknowledgement wording; documented in `PRIVACY.md`.
- **Existing files:** `main/window-controller.ts`, `main/window-placement.ts`, `main/recovery.ts`, `main/settings-store.ts`, `main/ipc.ts`, `shared/contracts.ts`, a settings panel.
- **New files:** `docs/WINDOW_CAPTURE_PRIVACY.md` (API choice and rationale, supported Windows versions, Electron details, limitations, user-facing behaviour, test method), a main-process module for the setting, tests.
- **Database/API:** none.
- **Tests:** unit tests with a fake window for apply, remove, recreate, crash recovery, compact and expanded, unsupported API and failure fallback (these prove the call is made, not that capture is blocked). **Real Windows verification is separate and manual:** capture the window with the Windows capture APIs actually in use (Snipping Tool, Game Bar, a screen-share app, `PrintScreen`, a Graphics Capture based recorder) with the setting on and off, after recreation and a forced renderer crash, in both layouts, on two monitors, and on a Windows version below 2004 if available. Record which tools honour it and which do not; do not generalise.
- **Proposed acceptance criteria:** (a) written owner decision and the M-1 wording update exist before any code; (b) the investigation report names the API used and the minimum Windows version; (c) setting off by default, with explanation shown before enabling; (d) applied to the app window only, and re-applied after recreation, crash recovery, layout change and monitor move; (e) the UI reports accepted, unsupported or failed, and the app works normally in each case; (f) an in-window indicator while on, tray and taskbar entry unchanged; (g) automated tests pass and the real-Windows capture matrix is filled in by a person, with tools that ignore the setting listed; (h) `WINDOW_CAPTURE_PRIVACY.md` and `PRIVACY.md` state the limits and the user's responsibility; (i) M-1 to M-11 tests unchanged and passing.
- **Risks/rollback:** conceals use from other parties (the reason for the owner gate); behaviour differs by Windows version and capture tool; a feature flag defaulting to off, and removal by reverting the milestone.

## M-13 Streaming STT (conditional)
- **Objective:** Reduce transcript latency by streaming audio over the Phase 9 WebSocket.
- **Condition:** only if M-2 shows silence+upload dominates and M-3 shows a provider supports streaming.
- **Affected:** `protocol.schema.json`, `packages/protocol`, `realtime/`, `audio-service.ts`, STT adapters. New protocol messages; contract tests required.
- **Security:** same consent gates; audio still memory-only; backpressure limits.
- **Risks/rollback:** large; behind a feature flag with batch path retained.

## M-14 Optional content-protection toggle (owner decision required)
Not recommended by default. See SECURITY_REVIEW addendum for the required conditions. Do not start without a written owner decision and acceptable-use policy (M-1) in place.
