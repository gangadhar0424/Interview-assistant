# Performance Baseline

Status: **M-2 implemented; first baseline recorded 2026-11-18. Pending owner review.** Only what could be measured on this machine is in the tables. Everything else is marked **UNMEASURED** with the reason. No value here is estimated or copied from elsewhere.

## 1. Test configuration

| Item | Value |
|---|---|
| Machine | Intel Core i5-1035G1 @ 1.00 GHz, 4 cores / 8 threads, 7.8 GB RAM (a laptop-class CPU, not a fast workstation) |
| OS | Windows 11 Home, build 26300 |
| App version / commit | desktop `0.1.0`, Electron 45.2.3, Node 24.22.2 (Electron) / 22.16.0 (vitest); base commit `f0e1075` **plus the uncommitted M-2 changes** (nothing was committed, per the task) |
| Backend runtime | Python 3.12.13 (`services/api/.venv`) |
| Providers | None. No real STT, LLM or vision provider was called. A zero-latency fake STT was used where a provider is involved. |
| Data | Synthetic only (`scripts/bench/synth.py`, `desktop_local.bench.ts`): tone bursts as "speech", blocky pixels as a "screenshot". No real audio, screenshots or user data. |
| Database / Redis | **Not running** (Docker daemon was stopped; no Postgres/Redis on this machine). |
| Method | Monotonic clocks only: `time.perf_counter_ns` (Python), `performance.now()` (Node/Electron). Percentiles are nearest-rank; with 10 samples p95 equals the maximum. |

## 2. Measured: backend local processing

Our own code only: **no network, no provider, no database.** Run: `cd services/api && .venv/Scripts/python.exe ../../scripts/bench/backend_local_bench.py --samples 200`. Unit: ms.

| Metric | n | min | p50 | p95 | max |
|---|---|---|---|---|---|
| `validate_wav` 3 s (93 KiB) | 200 | 0.02 | 0.02 | 0.02 | 0.08 |
| `validate_wav` 10 s (312 KiB) | 200 | 0.04 | 0.07 | 0.12 | 0.17 |
| `validate_wav` 28 s (875 KiB) | 200 | 0.07 | 0.08 | 0.15 | 0.37 |
| `validate_png` 1280x720 (39 KiB) | 40 | 0.04 | 0.09 | 0.21 | 0.64 |
| `validate_png` 1920x1080 (87 KiB) | 40 | 0.07 | 0.07 | 0.10 | 0.12 |
| Question detection (`classify`) | 1000 | 0.02 | 0.02 | 0.03 | 0.08 |
| `Transcriber.transcribe`, 5 s audio, zero-latency fake STT (validate + route + detect + metrics) | 200 | 0.34 | 0.65 | 1.20 | 4.12 |

**Finding:** the backend's own work per utterance or screenshot is about a millisecond. Validation and question detection are not where latency is. Any latency a user feels comes from the silence window, the upload, the provider and the model.

## 3. Measured: desktop local processing

Our own main-process code only. Run: `cd apps/desktop && BENCH_OUT=result.json npx vitest run --config vitest.bench.config.ts`. Unit: ms.

| Metric | n | min | p50 | p95 | max |
|---|---|---|---|---|---|
| `UtteranceSegmenter.push` per 100 ms audio frame | 300 | 0.01 | 0.01 | 0.18 | 3.55 |
| Segmenter, 30 s of audio, whole run | 1 | 18.75 | 18.75 | 18.75 | 18.75 |
| `buildWav`, 10 s utterance | 200 | 0.10 | 0.13 | 0.26 | 3.44 |
| `PerfRecorder` full trace (begin, 4 marks, value, end): the instrumentation's own cost | 2000 | 0.00 | 0.01 | 0.02 | 1.47 |
| `AudioService`: segment created to utterance emitted, zero-latency fake transcriber | 50 | 0.33 | 0.54 | 0.94 | 1.27 |
| `AudioService`: segment queue wait, zero-latency fake transcriber | 50 | 0.09 | 0.14 | 0.25 | 0.48 |

**Finding:** segmentation of 30 s of audio took 18.75 ms (about 0.06 % of real time), and the local path from a finished phrase to an emitted utterance is under 2 ms. The instrumentation costs about 0.01 ms per trace. The max values (single-digit ms) are one-off scheduler or garbage-collection pauses.

## 4. Measured: Electron startup and idle resources

Real packaged-mode build (`npm run build`, then `electron .`), a throw-away user-data directory, no session, no audio, no capture. 10 runs, one sample per run. Run: `python scripts/bench/desktop_app_bench.py --runs 10 --exit-ms 12000`. Each run opens the real window for about 12 s.

| Metric | n | min | p50 | p95 | max |
|---|---|---|---|---|---|
| `module_loaded` to `app_ready` (ms) | 10 | 53.09 | 60.80 | 72.27 | 72.27 |
| `app_ready` to `window_created` (ms): **window creation duration** | 10 | 93.08 | 103.95 | 127.20 | 127.20 |
| `window_created` to `ready_to_show` (ms) | 10 | 229.08 | 296.18 | 383.01 | 383.01 |
| `window_created` to `did_finish_load` (ms) | 10 | 215.08 | 295.58 | 382.73 | 382.73 |
| **`module_loaded` to `ready_to_show` (ms): app startup to visible window** | 10 | 401.47 | 468.32 | 558.58 | 558.58 |
| Memory peak, Browser (main) process (MB) | 10 | 98.33 | 99.64 | 102.89 | 102.89 |
| Memory peak, renderer ("Tab") process (MB) | 10 | 74.66 | 76.88 | 78.76 | 78.76 |
| Memory peak, GPU process (MB) | 10 | 96.12 | 101.13 | 112.46 | 112.46 |
| Memory peak, Utility process (MB) | 10 | 46.43 | 46.56 | 46.69 | 46.69 |
| CPU, largest sample, Browser process (%) | 10 | 12.92 | 14.99 | 16.37 | 16.37 |
| CPU, largest sample, renderer process (%) | 10 | 0.10 | 0.18 | 0.44 | 0.44 |
| CPU, largest sample, GPU process (%) | 10 | 0.72 | 0.83 | 1.00 | 1.00 |

Reading these correctly:
- Startup is measured from the first line of our main module. Electron's own boot before that was **0.07 s** of process uptime in the report's last run (field `processUptimeAtModuleLoadS`; earlier runs showed up to 0.39 s), so the user-visible total is that plus the figures above.
- Memory is the peak of the samples taken (app ready, finished startup, quit), as `workingSetSize`. It is an **idle** figure. It says nothing about memory during a live session with audio, screenshots or a long answer.
- CPU is Electron's `percentCPUUsage`, averaged between samples. The largest sample for the main process includes the startup burst, so it is **not** a pure idle figure.
- With 10 runs, p95 equals the maximum.

## 5. Instrumentation now in the code

| Layer | What is measured | Where |
|---|---|---|
| Desktop audio | arm to listening; segment created; queue wait; request start/response (client round trip); server-reported time; derived network and client overhead; segment to utterance (end to end) | `main/perf-marks.ts`, `main/audio-service.ts`, `main/api-client.ts` |
| Desktop screenshot | acquisition (after the visible countdown, so the countdown is not counted); extraction round trip; server-reported time; derived overhead; capture to answer done (chained) | `main/capture-service.ts` |
| Desktop answer | submit to first event; retrieval (server-reported); submit to first token; first token to done; submit to done; input (capture or phrase) to done | `main/ipc.ts` |
| Desktop startup/resources | module load, app ready, window created, ready to show, finished load; per-process memory and CPU | `main/index.ts`, `perf-marks.ts` |
| Backend (Prometheus, `/metrics`) | `interview_stt_request_seconds{provider}`, `interview_vision_request_seconds{provider}`, `interview_audio_validate_seconds`, `interview_screenshot_validate_seconds`, `interview_question_detection_seconds`, `interview_answer_first_token_seconds{provider}`, `interview_answer_total_seconds{result}` (existing `interview_answer_stage_seconds{stage}` covers retrieval, rewrite, role guard, extractive, general, validation) | `core/metrics.py`, `audio/service.py`, `screenshots/service.py`, `qa/service.py` |

How local, network and provider time are kept apart:
- **Local:** desktop stages that never leave the machine (queue wait, acquisition, arm).
- **Round trip:** one client-measured number per HTTP request.
- **Server-reported:** the API's own `latency_ms` (validation + provider + detection) and `retrieval.done.ms`. Provider time sits inside this; the backend histograms isolate the provider call.
- **Network and client overhead:** *derived* as round trip minus server-reported, floored at 0. It includes connection setup, loopback or TLS, and body read. It is labelled derived everywhere and is never a direct measurement.

Reporting: set `INTERVIEW_PERF=1` before launching the desktop app. On quit it prints one JSON block (`INTERVIEW_PERF_REPORT_BEGIN` ... `END`) to stdout with summaries and raw millisecond samples. It is **not** written to disk (the main process may write only settings and the token store, enforced by `tests/capture-guards.test.ts`), **not** sent anywhere, and **not** reachable from the renderer: there is no IPC channel for it. `INTERVIEW_PERF_EXIT_MS` quits after a delay (used by the benchmark).

## 6. UNMEASURED (and why)

| Metric | Why unmeasured | What is needed |
|---|---|---|
| Audio capture end to end: silence wait to transcript | Needs a real STT provider and real speech. The 700 ms end-of-silence window is a constant in `audio-vad.ts`, not a measurement. | Owner-provided STT key (OQ-4/OQ-5), recorded or TTS speech |
| Audio upload duration alone | `fetch` does not separate upload from server time. Only the round trip and server-reported time exist; overhead is derived (section 5). | Not separable without a lower-level client; accept the derived figure |
| STT request / response latency (provider) | No provider has been called; Docker (Postgres/Redis) was not running, so the API could not start | Docker running, API with `STT_ENABLED_PROVIDERS`, real or fake provider; `backend_e2e_bench.py` |
| Question detection inside a live request | Only the local function was timed (section 2). The in-request histogram exists but there is no running server. | Same as above |
| Retrieval duration | Needs database and embeddings; dev uses the fake embedder, which is not representative (`fastembed` not enabled) | Postgres, `EMBEDDING_PROVIDER=fastembed`, a seeded session |
| LLM provider time, first token, total answer, backend request duration | No API server, no provider | Same, plus an LLM key |
| Screenshot: acquisition on a real display | `desktopCapturer` needs a user-started capture with the visible countdown; deliberately not automated | Manual run with `INTERVIEW_PERF=1` and a screenshot; read the report |
| Screenshot: vision model time, total screenshot-to-answer | No provider / server | As above |
| Streaming smoothness (chunk cadence in the renderer) | Needs a live stream; renderer-side timing deliberately not added (nothing sensitive may be exposed over IPC) | Defer to a later milestone if needed |
| Memory and CPU **during** a live session | Needs a session, audio and answers | Manual run; the report samples at each completed operation |
| Other machines, multi-monitor, high-DPI | One machine only | More hardware |

`scripts/bench/backend_e2e_bench.py` is the harness for the API rows above. It was **executed only against a local stub server, to check its own logic**; those stub numbers are not recorded here. Run without configuration it prints UNMEASURED and exits with status 2.

## 7. Methodology and known limitations

- One machine, one run session, small samples (10 app launches; 40 to 1000 samples for the in-process micro-benchmarks). Treat the app numbers as a first baseline, not a distribution.
- Micro-benchmarks run warm, in-process, with synthetic data; real utterances and screenshots differ. They show that our own code is cheap, not what a user experiences.
- The segmenter benchmark runs on synthetic tone bursts. A noisy room changes where the fixed energy threshold cuts, which this does not test.
- The AudioService benchmark feeds audio at about 3x real time (under the 4x input limit) and uses a zero-latency fake transcriber, so it isolates local overhead only.
- Resource sampling has **no timer** (the project forbids periodic timers in the main process). It samples at app ready, at each finished operation, and at quit, so peaks between samples can be missed.
- Startup is measured from our module load, not from process creation.
- Instrumentation overhead (about 0.01 ms per trace) is far below every figure it measures.

## 8. How to reproduce

```powershell
# Backend local
cd services/api; .venv/Scripts/python.exe ../../scripts/bench/backend_local_bench.py --samples 200 --out ../../bench-backend-local.json
# Desktop local (no Electron)
cd apps/desktop; $env:BENCH_OUT="bench-desktop-local.json"; npx vitest run --config vitest.bench.config.ts
# Electron startup and idle resources (build first)
cd apps/desktop; npm run build; cd ../..; python scripts/bench/desktop_app_bench.py --runs 10 --exit-ms 12000 --out bench-app.json
# API end to end (needs a running API, a ready session with providers, and synthetic data only)
$env:BENCH_API_URL="http://127.0.0.1:8000"; $env:BENCH_EMAIL="..."; $env:BENCH_PASSWORD="..."; $env:BENCH_SESSION_ID="<uuid>"
python scripts/bench/backend_e2e_bench.py --samples 30 --out bench-e2e.json
```
Do not commit result files that came from a real account. Never run the end-to-end script against an account that holds real interview material.

## 9. Candidate optimisations

None are justified by this baseline. The measured local costs are all under a few milliseconds, so optimising our own code would not change what a user feels. The open question is the part this baseline could not reach: the silence window, the upload, the STT provider and the model. Those must be measured with real providers (M-3) before any change such as shortening `END_SILENCE_WINDOWS`, overlapping retrieval with generation, or streaming STT is proposed.
