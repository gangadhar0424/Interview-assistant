# Phase 12 Report — Live Audio to Answer (12A+12B combined, D-53)

Status: **ACCEPTED and COMPLETE** (2026-11-11). Automated verification passed and the owner passed all manual Windows tests on the packaged app.

## 1. Features
- Backend: `POST /sessions/{id}/transcribe` (raw `audio/wav`, in memory only), separate `STTProvider` port + router (allow-list `STT_ENABLED_PROVIDERS` empty by default; deterministic fake only in `APP_ENV=local`), WAV validation without decoding, per-user limits, usage metering, heuristic English question detector, `input_source=audio` on the existing Q&A pipeline. No migration, no transcript table, no WebSocket change.
- Desktop: audio disabled by default, native consent dialog, explicit Start/Stop, microphone (primary) and Windows system audio (loopback, audio-only), main-process indicator (tray tooltip/flash) plus in-app indicator, VAD segmentation, live transcript (memory only), detected-question confirmation, auto-answer OFF by default.

## 2. Security changes
- The deny-all permission handler is replaced by pure rules `decideMediaPermission` / `decideDisplayMedia` (default deny; audio-only; only while the main process is in the armed/listening state). Video and all other permissions remain denied.
- `getDisplayMedia` is granted only as audio loopback with no video source, only while system audio is arming.
- New narrow IPC: `audio:start`, `audio:stop`, one-way `audio:chunk` (sender-validated, size/rate limited). Capture auto-stops on hide/minimize/sign-out/session change/disable/reload/quit/2 h/arming timeout.
- Audio buffers are zero-filled after use; no audio or transcript in logs, metrics or storage. Sandbox, context isolation, CSP, fuses and token handling are unchanged (capture uses ScriptProcessor, so CSP needed no change).

## 3. Tests executed
- Desktop: `tsc --noEmit` clean; vitest 16 files / 320 tests passed; `npm run build` succeeded.
- Backend: ruff clean; mypy strict (`--no-incremental`) 201 files clean. Earlier DB-backed run: 119 passed.
- Latest re-run on this machine: the non-DB audio/STT/detection tests passed (56), but 120 DB-backed tests errored with ConnectionRefused because Docker/Postgres was not running (Docker Desktop could not be started). These were NOT re-verified in this final pass.

## 4. Not testable automatically
Real microphone and loopback signal, Windows privacy prompts, tray indicator rendering, packaged-app (`package:win`) smoke, real STT provider quality.

## 5. Manual tests required
See "Phase 12 — Live audio" in `docs/DESKTOP_MANUAL_CHECKLIST.md`.

## 6. Loopback
Feasible (audio-only grant works, no video/pixels); included as a second source; needs the manual signal test.

## 7. Known issues / deferred
Real STT provider, multilingual/LLM detection, transcript persistence, WebSocket audio, packaged-app smoke, Linux-container parity run, and a DB-backed backend regression re-run after Docker is restored. `test_one_slow_client_does_not_stall_another_user` fails on this Windows machine even without these changes.

## 8. Acceptance (owner manual results, packaged Windows app)
PASS: default audio-off; native consent; microphone Start/Stop; recording and Windows mic indicators; transcript; detected-question confirmation; statements do not trigger answers; auto-answer OFF by default; system audio; auto-stop on minimize/hide, sign-out, session change, disabling audio; Windows mic privacy denial; typed Q&A and screenshot regressions; no WAV/transcript files in userData.

## 9. Untested / deferred (non-blocking)
- Real STT provider and transcription quality (fake only; OQ-4 data-handling terms still open).
- Multi-monitor, code signing, auto-update, other Windows versions/audio devices (e.g. Bluetooth headsets, exclusive-mode devices).
- No automated packaged-app permission/audio smoke test (manual only).
- Linux-container parity run of the new desktop/backend tests not repeated.

## 10. Separate maintenance proposal (not part of Phase 12)
Pre-existing flaky tests, reproduced on unmodified HEAD (2/30 runs): `test_qa_live.py::test_the_service_keeps_working_after_disconnects` reads the answer row right after the limiter drains; fix by polling for status `cancelled` with a timeout. `test_realtime_qa_live.py::test_one_slow_client_does_not_stall_another_user` is environment-dependent on this Windows machine and passes on Linux CI. Propose as a small `test:` change.
