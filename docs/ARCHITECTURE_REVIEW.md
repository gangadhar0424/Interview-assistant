# Architecture Review

Status: **PROPOSED. Review only.** Date: 2026-11-18. Same method and limits as PRODUCT_GAP_ANALYSIS.md.

## What is sound and should be preserved

| Area | Evidence | Verdict |
|---|---|---|
| Electron isolation | `contextIsolation: true`, `sandbox: true`, navigation and window-open denied (`apps/desktop/src/main/index.ts:200-233`); fuses in `package.json` | Keep |
| Typed IPC | `shared/contracts.ts` (310 lines) is the single contract; `ipc-validation.ts` validates settings patches; `ipc-security.ts` checks sender | Keep; extend per milestone |
| Ports-and-adapters in main | `audio-service.ts` and `capture-service.ts` take side effects as ports (clock, window, network), so logic is unit-testable without Electron | Keep as the pattern for every new service |
| Single module touches the screen | `capture-electron.ts` is the only `desktopCapturer` user | Keep |
| Secrets | Refresh token via DPAPI `safeStorage` (`secure-refresh-store.ts`); provider keys backend-only | Keep |
| Backend AI boundary | `ai/` ports, router with fallback and breaker, SDK imports only in `ai/adapters/` | Keep |
| Prompt construction | Single `PromptBuilder` (`preferences/prompt_builder.py`) | Keep |
| User scoping | Repository layer plus RLS (docs); isolation tests mandatory | Keep |

## Findings

**A-1 (medium) Two large files carry most desktop UI state.** `renderer/src/store.ts` (994 lines) and `ui/App.tsx` (982 lines). They work and are tested (`tests/store.test.ts` etc.), but each new feature (shortcuts, recovery UI, session switching) increases coupling. Recommendation: split by feature slice only when a milestone touches it; do not do a standalone refactor (CLAUDE.md scope rule 5).

**A-2 (medium) Batch audio path sets a latency floor.** Audio is segmented in the main process, WAV-encoded (`wav.ts`) and POSTed per utterance to `/sessions/{id}/transcribe` (`audio/router.py`). Minimum delay before a transcript exists = end-of-silence window (700 ms, `audio-vad.ts`) + encode + upload + provider time. Streaming STT over the existing WebSocket gateway (Phase 9) would remove the silence wait but is a large change (new protocol messages, provider streaming support, backpressure). Do not commit to it before the baseline (PERFORMANCE_BASELINE.md) shows the floor matters.

**A-3 (medium) Question detection is an English heuristic on the backend.** `qa/detection.py` is advisory. The pipeline already has an LLM router for answers (`qa/answer_router.py`); detection quality for en/hi/te is unmeasured. Add an evaluation set first; consider a cheap model-assisted check only if the heuristic fails the set. Keep it on the backend (no duplicate logic in the desktop).

**A-4 (low) No shortcut layer.** Add a small main-process `shortcuts.ts` using Electron `globalShortcut` (built in; no new dependency). Shortcuts must call the same service entry points as the UI so every consent gate (window visible, countdown, indicator) still applies.

**A-5 (low) No crash-recovery layer.** `render-process-gone` is handled only to stop audio (`ipc.ts:227`). Add: reload the renderer once, restore the signed-in state from the main process, show a non-content error, and write an IDs-only local log. Do not add a remote crash reporter without an owner decision (data-handling).

**A-6 (low) Update path undefined.** `electron-builder` is configured; `electron-updater` is not. Because the updater must verify a publisher, signing (M-10) strictly precedes updating (M-11).

**A-7 (low) CI does not exercise Windows.** Desktop CI runs typecheck/test/build on `ubuntu-latest`. Packaging, fuses verification, and any Windows-only behaviour (DPAPI, loopback audio, NSIS) are manual. Add a `windows-latest` packaging job in M-10.

**A-8 (info) Duplicate-logic risk to avoid.** Provider choice, fallback, detection, and prompt logic live on the backend. Desktop code only presents and transports. Every milestone must keep it that way (the directive's "no duplicate backend logic").

## Target architecture (no replacement, only additions)

```
Renderer (React, sandboxed)            Main (owns sensitive ops)             Backend
  store.ts / App.tsx   --typed IPC-->   audio-service  capture-service   -->  /transcribe  /screenshots
                                        shortcuts (new) recovery (new)        SSE/WS answers
                                        telemetry marks (new, local only)     metrics histograms (extend)
```

New code in main follows the existing port pattern; new IPC channels are added to `contracts.ts` first, validated in `ipc-validation.ts`, and covered by an IPC test beside `audio-ipc.test.ts` / `capture-ipc.test.ts`.

## Decisions that need the owner (not made here)

1. Acceptable-use acknowledgement wording and where it appears (OQ-15).
2. Whether any content-protection capability may exist (see SECURITY_REVIEW addendum; recommendation: not by default).
3. Signing certificate type and owner (D-50).
4. Real provider accounts for STT and LLM validation (OQ-4/OQ-5).
5. Whether remote crash reporting is acceptable at all.
