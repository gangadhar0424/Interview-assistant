# Benchmark scripts (M-2)

Synthetic inputs only. No real audio, screenshots or user data is read, generated from real data, or stored. Results and methodology: `docs/PERFORMANCE_BASELINE.md`.

| Script | Measures | Needs |
|---|---|---|
| `backend_local_bench.py` | Backend's own code: WAV/PNG validation, question detection, transcription pipeline around a zero-latency fake STT | `services/api` virtualenv (no database) |
| `desktop_local.bench.ts` | Desktop's own code: segmenter, WAV encoding, recorder overhead, AudioService local path | `apps/desktop`: `npx vitest run --config vitest.bench.config.ts` |
| `desktop_app_bench.py` | Real Electron startup and idle memory/CPU over N launches (throw-away profile) | `npm run build` in `apps/desktop`; opens the window for a few seconds per run |
| `backend_e2e_bench.py` | API round trip, server-reported time, derived network overhead, retrieval, first token, total | A running API, a ready session with providers chosen, an account; env `BENCH_API_URL`, `BENCH_EMAIL`, `BENCH_PASSWORD`, `BENCH_SESSION_ID` |

`stats.py` and `synth.py` are shared helpers. Time kinds are never mixed: local time, client round trip, server-reported time (provider inside), and a *derived* network-and-client overhead. A metric that cannot be collected prints UNMEASURED.

Do not commit result files from a real account. Do not point `backend_e2e_bench.py` at an account holding real interview material.
