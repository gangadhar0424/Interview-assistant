# Phase 11A — Screenshot Input, Backend: Report

Date: 2026-11-10. Scope: backend only (D-51, `docs/PHASE_11_PLAN.md`). **11B (desktop) is not started. Nothing is committed or pushed.** No desktop, web, migration, dependency or storage change.

## 1. What was built

| Area | Change |
|---|---|
| Contracts first | `API.md` (new endpoint, `input_source`, error codes, check order), `ARCHITECTURE.md` §10, `DATABASE.md` (no screenshots table), `PRIVACY.md`, `TESTING_STRATEGY.md`, `README.md`, `.env.example`, `PHASES.md`, and **D-51** (Accepted) in `DECISIONS.md` |
| Port | `app/ai/vision.py`: `VisionProvider` protocol, `ImageInput` (bytes hidden from `repr`), `VisionResult`, `VisionSpec`. `LLMProvider`, `Message`, the text router/registry, SSE and WS are **unchanged** |
| Fake | `app/ai/fake_vision.py`: deterministic reply (placeholders for size/digest), scripted failures, optional delay, cancel counter |
| Router | `app/ai/vision_router.py`: separate allow-list, fallback chain, timeout, circuit breaker, usage metering; `build_vision_router` knows only `fake` |
| Config | `VISION_ENABLED_PROVIDERS` (default empty = screenshots off; `fake` only, `APP_ENV=local` only), `VISION_FALLBACK_CHAIN`, `VISION_REQUEST_TIMEOUT_SECONDS`, `SCREENSHOT_MAX_BYTES` (5 MiB), `..._MAX_LONG_EDGE` (4096), `..._MAX_PIXELS` (16 M), `..._RATE_LIMIT_PER_MINUTE` (6), `..._MAX_CONCURRENT` (1), all validated at startup |
| Validation | `app/screenshots/validation.py`: PNG only; CRC-checked bounded chunk walk; header dimensions checked first; strict chunk order; no trailing data; only IHDR/PLTE/tRNS/IDAT/IEND re-emitted (metadata dropped); no decoding, no new dependency |
| Endpoint | `POST /api/v1/sessions/{id}/screenshots` (raw `image/png`, not multipart; `200 {extracted_text, truncated, provider, model, latency_ms}`); order: auth → vision enabled → content type → declared size → limiter → session → capped body read → validation → provider; disconnect cancels the provider call |
| Limits | `ScreenshotLimits` (per-user rate and concurrency, slot always released) and an extension of `BodySizeLimitMiddleware` to id-bearing paths (pattern limits; the previous exact-path behaviour is unchanged) |
| Prompt | `templates/vision/v1/extract_question.md` built by `build_vision_extraction_prompt()` (no inline prompt prose) |
| Questions | optional `input_source: "typed"\|"screenshot"` on `POST /sessions/{id}/questions` (default `typed`; unknown values rejected); stored in the existing `detected_questions.source` column |
| Metrics | new `interview_screenshot_extractions{status}`; vision calls also feed the existing LLM request metrics by provider id and outcome |

## 2. Privacy properties (and how they are checked)

- **Nothing is persisted:** no table/column/file/queue/cache. Test: row counts, storage-root files and `information_schema` before/after, no screenshot-named table or column.
- **Nothing sensitive is logged, metered or returned in errors:** logs carry IDs, byte counts, dimensions, character counts, status and latency only. Test with sentinels in the extracted text, in PNG metadata, base64 slices of the original and normalised image and their SHA-256 digests: none appear in captured DEBUG logs, `/metrics`, usage records or any error body (success, invalid, wrong type and provider failure).
- **Provider exceptions** with content in their text are hidden behind a safe error and only the class name is logged (tested).
- **Extracted text is untrusted:** cleaned, capped at 2000 characters, returned to the user; hostile on-screen text is returned inertly. It only enters the answer pipeline if the user asks it as a question, where the existing untrusted-question wrapping applies.
- **No real vision provider exists or is called.** Enabling a text LLM provider does not enable screenshots (separate allow-list, tested).

## 3. Tests and static checks (all actually executed)

Database: this project's compose Postgres, dedicated scratch database (`ia_p11a_test`, dropped afterwards).

| Run | Result |
|---|---|
| New: `test_screenshot_validation.py` | 19 passed |
| New: `test_vision_provider.py` (fake, router, config) | 20 passed |
| New: `test_body_limit_patterns.py` | 4 passed |
| New: `test_screenshot_api.py` (DB-backed) | 35 passed |
| Affected set (prompt, inline-string guard, evidence tags, QA API, SSE contract, sessions, files, plus the four new files) | 443 passed |
| **Full backend suite, final run** | **1649 passed, 4 skipped, 2 deselected, 0 failed** (27 min) |
| `ruff check .`, `ruff format --check .` (212 files), `mypy --strict app` (118 files) | clean |

Skipped: 2 real-embedding tests (opt-in, need the real model), 2 Windows-only storage tests (symlinks/POSIX permissions). Deselected: 2 `integration`-marked tests (repo default). Baseline before this work was 1571 passed.

Coverage requested: validation (corrupt/truncated/CRC/trailing/order/colour type/bomb headers/dimensions/ancillary stripping), authorization (401, foreign and unknown session indistinguishable 404 with and without a body, token-only identity), session readiness (409 for draft/active/ended/archived), size limits (declared and streamed 413, middleware pattern limits), rate and concurrency limits with slot release on every path, provider failures (503/504/429/422 fixed messages, fallback, breaker, content filter never retried), timeout (504), cancellation (provider call cancelled, breaker not penalised, no usage record, slot released; disconnect race tested), persistence (none), log/metric redaction, `input_source`, and the unchanged question contract.

## 4. Defects found and fixed during the work

The first full run had 4 failures, all caused by my changes and all fixed:
1. Two prompt tests (released template set is immutable / no orphaned template): I had put the vision template inside `templates/v1`. Moved to `templates/vision/v1/`, outside the immutable released prompt versions.
2. `test_an_unexpected_failure_inside_the_stream…`: a test double overriding `QuestionAnswerer.ask` used the old signature; updated to accept the new optional `input_source` (the production class has no other override).
3. `test_deferred_endpoints_do_not_exist` listed `screenshots` as absent; it now exists, so the test no longer asserts that.

No defect was found in the existing grounding, SSE or WebSocket code.

## 5. Deviations from the approved plan

1. **No `"vision"` added to the `Capability` literal.** The separate vision allow-list makes it unnecessary, so the text provider types stay completely untouched (plan §6 said "additive").
2. **`503 vision_unavailable` is checked right after authentication** (before content type and session), documented in `API.md`.
3. **A defensive `499 client_closed_request` error** exists on the disconnect path (the response may never reach a client that left).
4. **`input_source` is also validated in `QuestionAnswerer.ask`**, not only by the request schema (defence in depth for non-HTTP callers).
5. **Vision calls reuse the existing LLM request/duration metrics** (provider id and outcome only) in addition to the new screenshot counter.
6. **Two existing tests were edited** (see §4); no production behaviour for existing endpoints changed.
7. Not done (as planned): no real vision adapter, no storage, no migration, no `GET /ai/options` advertisement of vision, no desktop work.

## 6. Not tested / known limitations

- A real client disconnect against a live ASGI server was not exercised: the cancel-on-disconnect race is tested with a stub request, the provider-cancellation and slot-release paths with real tasks. A live `uvicorn` smoke with a real disconnect was not run.
- No real vision provider; extraction quality, latency and cost are unknown.
- The WebSocket `question.submit` has no `input_source` (the desktop uses SSE); a WS-submitted question is recorded as `typed`.
- Limits are per API process (as for questions, R-30).
- PNG validation does not decode pixels, so a structurally valid PNG whose compressed data is corrupt is only caught by the provider; the dimension limits bound the worst case.
- `API.md` status rows and `PRIVACY.md` describe the backend only; the desktop disclosure text is 11B work.

## 7. Repository state

Uncommitted: the files above plus `docs/PHASE_11_PLAN.md` (from the planning step). Scratch databases dropped; no processes left running; compose containers left running. Awaiting your review before 11B.
