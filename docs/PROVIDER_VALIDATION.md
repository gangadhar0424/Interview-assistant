# Provider Validation (M-3)

Status: **executed 2026-11-18; pending owner review.** Real calls to the configured providers, a real PostgreSQL/pgvector database and the real embedding model. Nothing here is mocked or estimated unless it is labelled **ESTIMATE** or **ASSUMPTION**. Where a provider could not be reached, the result is a blocker, not a number.

## 1. Headline

| Area | Result |
|---|---|
| STT: Groq `whisper-large-v3-turbo` | **Works.** 20/20, p50 347 ms, mean word error rate 3.1 % (English, synthetic speech) |
| STT: Gemini `gemini-3.1-flash-lite` | **Works.** 20/20, p50 3.4 s, p95 9.4 s, mean WER 1.1 % |
| STT: OpenAI `gpt-4o-mini-transcribe` | **Blocked: the OpenAI account has no credit** (HTTP 429 `credit_balance_exhausted`) |
| STT: Sarvam `saarika:v2.5` | **Blocked: the Sarvam account has no quota** (HTTP 402 `insufficient_quota_error`) |
| LLM: Gemini `gemini-3.1-flash-lite` | **Works.** 20/20 completed (10 by the model), time to first token p50 2.8 s |
| LLM: Groq `openai/gpt-oss-120b` | **Partly usable.** 13/20 completed; 7 refused by the account's **8,000 tokens-per-minute limit**; model answers n = 3 only |
| LLM: OpenAI `gpt-4o-mini` | **Blocked: no credit** |
| LLM: Anthropic `claude-sonnet-5-5` | **Blocked: "credit balance is too low"** |
| Vision | **Blocked in the code:** only a `fake` vision provider exists (D-51); no real adapter has been approved or built |
| Retrieval (real `multilingual-e5-large`, pgvector) | recall@8 **0.94** (dense) and **1.00** (hybrid) on a 16-question synthetic set; query embedding p50 212 ms, p95 420 ms |
| Fallback / failure behaviour | Verified with real failing accounts: safe error, circuit breaker, **no silent substitution** (D-57) |

Two of four reasoning providers and two of four speech providers could not be validated because of vendor account billing, not application defects. They need credit added, then a re-run of the same commands (section 11).

## 2. Environment

| Item | Value |
|---|---|
| Machine | Intel Core i5-1035G1 @ 1.00 GHz, 4 cores / 8 threads, **7.8 GB RAM** (about 0.7-0.9 GB free during runs) |
| OS | Windows 11 Home build 26300 |
| Commit | base `f0e1075` plus the uncommitted M-1, M-2 and M-3 work (nothing committed) |
| Backend | Python 3.12.13; API (uvicorn) and the Dramatiq ingestion worker run from `services/api/.venv` |
| Database | PostgreSQL 16.15 with pgvector (the project's own `pgvector/pgvector:pg16` container), **a throw-away database `m3_validation`** migrated from 0001 to **0011 (head)**. The owner's `interview_assistant` database was not touched. |
| Redis | the project's `redis:7-alpine` container |
| Embeddings | `EMBEDDING_PROVIDER=fastembed`, `intfloat/multilingual-e5-large`, 1024 dimensions, CPU. The optional `embeddings` extra (declared in `pyproject.toml`, version pinned by `uv.lock`) was installed into the venv; `pyproject.toml` and `uv.lock` are unchanged. About 2 GB of model files are cached in the temp folder. |
| Settings overridden for the run (process environment only, `.env` untouched) | `DATABASE_URL` (throw-away DB), `EMBEDDING_PROVIDER=fastembed` |
| Network | the developer's normal internet connection, calls made from this laptop. Latencies therefore include real network time to each vendor. |

Models as configured: STT `gpt-4o-mini-transcribe` (OpenAI), `whisper-large-v3-turbo` (Groq), `saarika:v2.5` (Sarvam), `gemini-3.1-flash-lite` (Gemini). Reasoning `gpt-4o-mini`, `openai/gpt-oss-120b`, `claude-sonnet-5-5`, `gemini-3.1-flash-lite`. `QA_ROUTER_MODE=on`.

## 3. Method and data

- **Data is synthetic.** A fictional candidate resume and a fictional job description (DOCX), built by `scripts/validate/fixtures.py`. Speech is generated with Windows' built-in text-to-speech (Microsoft David and Zira), 10 interview-style sentences x 2 voices = 20 utterances of about 3.6 s each (71.9 s total), 16 kHz mono 16-bit. No real person, resume, recording or screenshot is involved.
- **Speech fixtures were normalised** from the speech engine's 18-byte `fmt` chunk to the canonical 16-byte form, because the API's WAV validator (correctly, and unchanged) only accepts the canonical form. The first run failed 80/80 with `invalid_audio` for this reason; that run is discarded.
- **Harness:** `scripts/validate/provider_validation.py` drives the real HTTP API (signup, upload, session creation, `/transcribe`, `/questions` SSE). It prints and stores only timings, counts, status codes, provider ids and error codes. Transcripts were compared with the reference text in memory for word error rate (WER) and discarded.
- **Pacing:** 1 s between requests to stay under the per-user limits (transcribe 40/min, questions 20/min). A first run that did not pace hit `rate_limited`; those results were discarded.
- **Sample sizes:** STT 20 requests per provider. LLM 20 questions per provider (10 distinct questions x 2). Retrieval 16 questions. Query embedding 80 calls.
- **p95 with 20 samples is close to the maximum**; read it as an indication, not a stable estimate.
- **Fixed by the harness during the run (not product defects):** a bug that stopped reading before a stream's final `data:` line (so error codes and token counts were first missed), a null `indexing` field before the worker picked a file up, a reserved email domain, and the fixture format above.

## 4. A. Speech-to-text

20 utterances per provider; end-to-end = client request to response (upload + server + provider + network); server = the API's own `latency_ms` (validation + provider + question detection).

| Provider | Success | End-to-end p50 / p95 / max (ms) | Server-reported p50 / p95 (ms) | Mean WER | Exact match | Flagged as question |
|---|---|---|---|---|---|---|
| Groq `whisper-large-v3-turbo` | 20/20 | 347 / 751 / 992 | 250 / 515 | 3.13 % | 85 % | 100 % |
| Gemini `gemini-3.1-flash-lite` | 20/20 | 3,434 / 9,372 / 18,250 | 3,375 / 9,312 | 1.11 % | 95 % | 100 % |
| OpenAI `gpt-4o-mini-transcribe` | 0/20 | not measurable | - | - | - | - |
| Sarvam `saarika:v2.5` | 0/20 | not measurable | - | - | - | - |

- **Accuracy:** WER is word-level edit distance over lower-cased text with punctuation removed, against the exact sentences the speech engine was given. These are clean, synthetic, English, single-speaker clips. They say nothing about accents, noise, overlapping speech or Hindi/Telugu.
- **Question detection:** all 20 utterances were questions and all 20 were flagged (the heuristic is advisory). Statements were not tested in this run, so this is a recall figure only.
- **Failures:** OpenAI returned HTTP 429 `credit_balance_exhausted` (mapped to `llm_quota_exhausted`; later calls hit the open circuit breaker and returned `llm_unavailable`). Sarvam returned HTTP 402 `insufficient_quota_error`.
- **Gemini's tail** (p95 9.4 s, max 18.3 s) is large relative to its median; with 20 samples this may be provider variability or my memory-starved machine (see limitations).

## 5. B. Reasoning (LLM)

20 questions per provider. Half of the questions are answered **verbatim from the candidate's resume without any model call** (the D-66 router's exact-text path; provider reported as `extractive`). Those are excluded from the model timings below and counted separately. Timings are client-measured from the moment the request was sent; the server's own figures are shown for the same answers.

| Provider | Completed | Model answers | Exact-text (no model call) | Failures | First token p50 / p95 (ms, client) | Total p50 / p95 (ms, client) | Server first token p50 (ms) |
|---|---|---|---|---|---|---|---|
| Gemini `gemini-3.1-flash-lite` | 20/20 (100 %) | 10 | 10 | none | 2,810 / 9,362 | 3,150 / 9,405 | 2,781 |
| Groq `openai/gpt-oss-120b` | 13/20 (65 %) | **3** | 10 | 7 x `llm_rate_limited` | 2,018 / 2,055 | 2,096 / 2,484 | 1,967 |
| OpenAI `gpt-4o-mini` | 10/20 | 0 | 10 | 10 x `llm_unavailable` (no credit) | - | - | - |
| Anthropic `claude-sonnet-5-5` | 10/20 | 0 | 10 | 10 x `llm_unavailable` (credit too low) | - | - | - |

- **Success rate for model answers:** Gemini 10/10, Groq 3/10 (the other 7 were refused by the account's 8,000 tokens-per-minute limit: each request sends about 1,460 input tokens, so roughly five requests a minute saturate it; the diagnostic call showed `x-ratelimit-limit-tokens: 8000`), OpenAI 0/10, Anthropic 0/10.
- **Groq's three timed answers are too few for percentiles.** Treat 2.0 s as a single observation range, not a p50.
- **Retrieval time inside the answer path** (server-reported, all 20 questions): Gemini p50 250 ms / p95 375 ms; Groq 250 / 516; the OpenAI and Anthropic runs had p95 of 1,984 and 1,719 ms with maxima up to 8.8 s (memory pressure, section 8).
- **Fallback used:** 0 of 80 answers (expected; see section 7).
- Measured token usage per model answer: Gemini about 2,002 input / 100 output; Groq about 1,458 input / 190 output.

## 6. C. Vision

**Not validated.** `VISION_ENABLED_PROVIDERS` is empty and the code recognises only a deterministic `fake` provider (`KNOWN_VISION_PROVIDER_IDS`, D-51: "a real adapter needs a separate approval"). Building a real vision adapter is new feature work outside M-3, so I did not. Screenshot latency, extraction rate and quality, and provider failure behaviour are all **UNMEASURED**. Only the backend's local PNG validation was measured earlier (`PERFORMANCE_BASELINE.md`, under 0.3 ms).

To unblock: the owner approves a vision provider and adapter (a new decision), supplies credit, and the harness gets a `--section vision` using controlled synthetic test images.

## 7. Failure and fallback behaviour (real provider failures)

The OpenAI, Anthropic and Sarvam accounts failed for real, so no failure had to be simulated. Selection is exclusive per session (D-57): the chosen provider is called alone and its failure is returned as a safe error, never retried elsewhere. Five consecutive requests per failing selection, then a healthy control (Gemini):

| Selection | Request 1 | Requests 2-5 | Substitute provider answered? |
|---|---|---|---|
| LLM OpenAI | `answer.error` `llm_quota_exhausted` after **17.7 s** | `llm_unavailable` after 0.6-3.2 s (circuit breaker open) | **No** |
| LLM Anthropic | `answer.error` `llm_quota_exhausted` after 3.2 s | `llm_unavailable` after 0.5-0.7 s | **No** |
| STT OpenAI | HTTP 503 `llm_quota_exhausted` after 1.8 s | HTTP 503 `llm_unavailable` after 0.1-0.25 s | **No** |
| STT Sarvam | HTTP 503 `llm_unavailable` after 0.5 s | HTTP 503 `llm_unavailable` after 0.07-0.11 s | **No** |
| Control: LLM Gemini | `answer.done`, answered by Gemini | - | - |
| Control: STT Gemini | HTTP 200 in 1.7 s | - | - |

Findings:
1. **No silent fallback** to another provider in any case, as D-57 requires. The circuit breaker opens after repeated failures and later requests fail fast.
2. **First failure can be slow:** OpenAI took 17.7 s to report the first failure for an answer. A user would wait that long before seeing an error. The cause (provider response time vs. our timeout and retry handling) was not isolated.
3. **Billing errors are reported inconsistently.** The same account problem appears as `llm_quota_exhausted` once and then `llm_unavailable` (breaker), Sarvam's HTTP 402 is reported only as `llm_unavailable`, and Anthropic's "credit too low" (HTTP 400) first appeared as `llm_quota_exhausted` and then `llm_unavailable`. The user-facing message therefore does not reliably say "this provider needs credit". This is an error-mapping gap (the adapter's `raise_for_status` does not handle 402), not a routing defect; **I changed nothing**. A small follow-up would map 402 explicitly.
4. The legacy `LLM_FALLBACK_CHAIN` (used only for sessions that predate D-57) is empty in this configuration and was not exercised.

## 8. D. Retrieval and embeddings

Real model `multilingual-e5-large` (CPU), real PostgreSQL/pgvector, the real worker. Corpus: 1 synthetic resume + 1 synthetic job description (72 chunks and 72 embeddings were in the throw-away database across the validation accounts; the evaluation used one account's two files).

**Relevance** (`services/api/scripts/eval_retrieval.py` with `--questions` pointing at the synthetic set; I added that optional argument, default unchanged). 16 questions, each with a marker phrase that must appear in a retrieved passage:

| Mode | recall@5 | recall@8 | MRR | Misses |
|---|---|---|---|---|
| Dense (the pre-D-66 pipeline) | 15/16 = 0.94 | 15/16 = 0.94 | 0.875 | 1 ("What eviction filter did you write?") |
| Hybrid (D-66, current) | 16/16 = 1.00 | 16/16 = 1.00 | 0.778 | none |

Hybrid found every expected passage; its lower MRR means the right passage was on average ranked lower within the top 8 than with dense retrieval on this set. 16 questions on a two-document corpus is a **small, easy test**; it validates that the real model and database path works, not that retrieval is production-grade.

**Router mode accuracy (4/16) is not a meaningful result here:** my labels expected exact-text answers for specific questions, while the router only chooses exact text for vague or section-style requests and otherwise synthesises. I did not tune either side. Exact-text answers did contain the expected text 4/4 times.

**Latency**, measured with the API and worker stopped so only one model process ran:

| Operation | n | p50 | p95 | Notes |
|---|---|---|---|---|
| Model load from a warm cache + first query | 1 | 33.2 s | - | cold start; the very first download took 164.6 s on this connection |
| `embed_query` (one question) | 80 | **212 ms** | **420 ms** | min 158, max 1,625 ms |
| `embed_documents` batch of 1 (~270 chars) | 8 | 530 ms | 712 ms | |
| `embed_documents` batch of 8 | 8 | 5,166 ms | 7,777 ms | |
| `embed_documents` batch of 32 | 8 | 44,871 ms | 71,947 ms | |
| Upload to fully indexed, 2 small DOCX files (API + worker both loaded) | 5 runs | 16-39 s (one first run 196 s incl. model load) | | |
| Server-reported retrieval inside answers | 80 | about 250 ms | 375-516 ms (up to 2 s / 8.8 s in memory-starved runs) | |

**Important:** document-embedding time grows faster than linearly with batch size here (batch 32 took 44 s, far more than 4 x batch 8). On a 7.8 GB machine with under 1 GB free, with API, worker and evaluation each holding a ~2 GB model, this is almost certainly memory pressure (swapping), so the large-batch and tail numbers are a property of this machine, not of the model. A machine with more RAM is needed for a representative ingestion benchmark.

## 9. E. Cost

Prices were taken from the vendors' official pricing pages on **2026-11-18** through a web search that returned the pages' figures (sources below). They are list prices, standard tier, before tax; they were not confirmed against an invoice and vendors change them. **Measured** = usage observed in this validation. **ESTIMATE / ASSUMPTION** is stated per line.

| Provider / model | Listed price | Source |
|---|---|---|
| OpenAI `gpt-4o-mini-transcribe` | about $0.003 per minute (listed as $1.25 / $5.00 per 1M tokens) | [OpenAI pricing](https://developers.openai.com/api/docs/pricing?latest-pricing=standard) |
| OpenAI `gpt-4o-mini` | $0.15 in / $0.60 out per 1M tokens. **Both OpenAI model pages were marked Deprecated in the search results**; check before relying on them | [OpenAI model page](https://developers.openai.com/api/docs/models/gpt-4o-mini) |
| Groq `whisper-large-v3-turbo` | $0.04 per hour | [Groq docs](https://console.groq.com/docs/model/whisper-large-v3-turbo) |
| Groq `openai/gpt-oss-120b` | $0.15 in / $0.60 out per 1M tokens | [Groq docs](https://console.groq.com/docs/model/openai/gpt-oss-120b) |
| Anthropic `claude-sonnet-5-5` | $2 in / $10 out per 1M tokens | [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing) |
| Gemini `gemini-3.1-flash-lite` | $0.25 in (text) / $0.50 in (audio) / $1.50 out per 1M tokens | [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing) |
| Sarvam speech-to-text | INR 0.49 per minute pay-as-you-go; search results did not mention `saarika` (they named a newer model), so the price for the configured model is **unverified** | [Sarvam pricing](https://www.sarvam.ai/api-pricing) |

**Per request / per minute**

| Item | Value | Basis |
|---|---|---|
| Gemini LLM answer | **$0.00065** | measured 2,002 in / 100 out tokens x listed price |
| Groq LLM answer | **$0.00033** | measured 1,458 in / 190 out (n = 3 answers) x listed price |
| OpenAI `gpt-4o-mini` answer | about $0.00036 | **ESTIMATE:** Gemini's measured token counts at OpenAI's price; not measured (no credit); token counts differ by tokenizer |
| Anthropic Sonnet 5.5 answer | about $0.0050 | **ESTIMATE:** same method; not measured |
| Groq STT | **$0.00067 per minute** of audio | listed price; measured audio was 71.9 s. Providers often bill a minimum per request; **not verified for Groq**, and the short clips here would cost more if one applies |
| Gemini STT | about $0.00096 per minute | **ASSUMPTION:** audio at 32 tokens per second (Google's documented rate, not observed here) x $0.50 per 1M. The API did not record STT token usage |
| OpenAI STT | about $0.003 per minute | listed estimate; not measured |
| Sarvam STT | INR 0.49 per minute | listed; not measured. No exchange rate was assumed |
| Vision per screenshot | **UNMEASURED** | no real vision provider |
| Embeddings | $0 | local model; cost is CPU time and RAM |

**Average cost per interview session (ASSUMPTION-based):** a 45-minute session with 18 minutes of speech heard by STT and 20 answered questions, using the measured per-answer cost and the STT prices above:

| STT | LLM | Estimated cost per session |
|---|---|---|
| Groq | Groq | about $0.019 |
| Groq | Gemini | about $0.025 |
| Gemini | Groq | about $0.024 |
| Gemini | Gemini | about $0.030 |

The profile (45 minutes, 18 minutes of speech, 20 answers) is my assumption, not a measurement of real interviews; real sessions vary with the interviewer's speech time and the number of questions. Half of this validation's questions were answered without any model call, which lowers real cost versus the assumed 20 model answers. Screenshots are not included.

## 10. Unmeasured and known limitations

| Item | Why |
|---|---|
| OpenAI and Sarvam STT; OpenAI and Anthropic LLM | vendor accounts out of credit/quota |
| Vision (latency, extraction rate, quality, failure) | no real adapter exists (D-51) |
| Hindi and Telugu STT and answers | no Hindi/Telugu voices available for synthetic speech; English only |
| Real human speech, accents, noise, overlapping speakers | only clean synthetic single-speaker clips were used |
| Statements (non-questions) for the question detector | not in the spoken set |
| Groq percentiles | only 3 model answers completed (8,000 tokens/min account limit) |
| Legacy `LLM_FALLBACK_CHAIN` behaviour | empty in this configuration; sessions are exclusive per D-57 |
| Representative ingestion/embedding throughput | the machine has 7.8 GB RAM; models swapped |
| Streaming smoothness, desktop-side timings, packaged-app latency | not part of this API-level run (desktop instrumentation exists from M-2) |
| Provider data-handling terms (OQ-4) | not a measurement; still needs owner review before real interview content is sent |
| Larger samples | 20 per cell; p95 values are indicative |

## 11. Reproduce

```powershell
# infrastructure (the project's own containers) and a throw-away database
docker start interview-assistant-postgres-1 interview-assistant-redis-1
# create database m3_validation; alembic upgrade head with DATABASE_URL pointing at it
# API and worker, with DATABASE_URL overridden and EMBEDDING_PROVIDER=fastembed (uv sync --frozen --extra embeddings first)
cd services/api
uv run uvicorn app.main:get_app --factory --port 8000 --no-proxy-headers
uv run dramatiq app.workers.tasks --processes 1 --threads 2
# fixtures (synthetic) and speech (Windows SAPI; then normalise the WAV header to 16 bytes)
python scripts/validate/fixtures.py <dir>
# measurements
python scripts/validate/provider_validation.py --fixtures <dir> --section stt --reps 1 --out stt.json
python scripts/validate/provider_validation.py --fixtures <dir> --section llm --reps 2 --out llm.json
python scripts/validate/provider_validation.py --fixtures <dir> --section failure --bad-llm openai,anthropic --bad-stt openai,sarvam --good-llm gemini --good-stt gemini
python services/api/scripts/eval_retrieval.py --session <id> --user <id> --mode hybrid --questions <dir>/truth.json
EMBEDDING_PROVIDER=fastembed python scripts/validate/embedding_bench.py   # run from services/api, nothing else heavy open
```

After adding credit to OpenAI, Anthropic and Sarvam, re-run the `stt` and `llm` sections; on a machine with 16 GB or more also re-run retrieval and embedding. Never run this against an account that holds real interview material.

## 12. Safety record

- Only synthetic data was used; no real audio, transcript, resume or screenshot.
- No credential was printed or written. Credential presence was checked by name only; the three diagnostic calls printed HTTP status and vendor error codes (and, for Anthropic, the vendor's generic billing message), never a key or any content.
- Throw-away accounts were created on the throw-away database only. The owner's `interview_assistant` database was not read or written.
- Consent and acknowledgement enforcement, provider routing and answer-generation code were not changed. The only repository code changes are the new files in `scripts/validate/` and an optional `--questions` argument on `eval_retrieval.py`.
