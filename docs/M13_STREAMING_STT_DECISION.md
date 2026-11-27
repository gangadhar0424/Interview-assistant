# M-13 streaming speech-to-text: go / no-go recommendation

Prepared 2026-11-24. **Recommendation: NO-GO for now. Do not implement M-13.** The decision rests on measurements that already exist (M-2 baseline, M-3 provider validation) and on what the code does today. One input the task asked for, end-to-end speech latency on the packaged application with real speech, **has not been measured** and cannot be measured without the owner; the recommendation says what that measurement would have to show to change it.

## 1. The roadmap's own condition

`docs/PRODUCTION_ROADMAP.md` allows M-13 only if **(a)** M-2 shows silence plus upload dominates latency and **(b)** M-3 shows a provider supports streaming. Neither is met.

## 2. Where the time goes, from measured numbers

A spoken question today takes this path (batch speech-to-text, one utterance at a time, `app/ai/stt.py`: "utterance-at-a-time (no streaming partials)"):

| Stage | Value | Source | Kind |
|---|---|---|---|
| Desktop local work (segmenter, WAV encoding, queue) | about 1 ms median, under 5 ms worst | `PERFORMANCE_BASELINE.md` sections 3 | measured, synthetic audio |
| End-of-speech silence window | **700 ms** (`END_SILENCE_WINDOWS = 35` windows of 20 ms; a constant, not a measurement) | `audio-vad.ts` | configured value |
| Upload plus provider speech-to-text, Groq `whisper-large-v3-turbo` | **p50 347 ms, p95 751 ms** end to end, server-reported p50 250 ms | `PROVIDER_VALIDATION.md` section 4 (20 clean synthetic English clips) | measured |
| Upload plus provider speech-to-text, Gemini `gemini-3.1-flash-lite` | **p50 3,434 ms, p95 9,372 ms** | same | measured |
| Question detection | about 0.02 ms | baseline section 2 | measured |
| Retrieval inside the answer | about 250 ms median | `PROVIDER_VALIDATION.md` section 5 | measured (fake or real embedder as stated there) |
| Language model time to first token, Gemini `gemini-3.1-flash-lite` | **p50 2,810 ms, p95 9,362 ms** | same | measured |
| Language model time to first token, Groq `gpt-oss-120b` | p50 2,018 ms (only 3 model answers completed; 7 refused by the 8,000 tokens-per-minute limit) | same | measured, small sample |

**Arithmetic for the configuration that works well today (Groq speech, Gemini language model), medians only:** about 0.70 s of silence + 0.35 s speech-to-text + 2.8 s first token, roughly **3.9 s from end of speech to the first word of the answer**, of which speech-to-text including upload is about **9 percent** and the silence window about **18 percent**. The language model's first token is about **72 percent**, and its tail (p95 9.4 s) is far larger than anything in the speech path. This sums medians of different samples taken in different runs, so it is an illustration, not a measured end-to-end figure.

## 3. What streaming speech-to-text could and could not save

- **It cannot shorten the 700 ms silence window.** That window decides when an utterance ended. Streaming transcription returns partial text while the person is still speaking, but the app still has to decide the question is finished. Shortening the window is a voice-activity-detection tuning question, and it trades speed for cut-off sentences.
- **At best it removes most of the 250 to 350 ms** between "utterance ended" and "transcript available", because the audio would already have been sent and mostly transcribed. That is roughly 0.3 s of about 3.9 s.
- **Speculative answering on partial text** (starting retrieval or generation early) could save more, but it would answer questions the person has not finished asking and would multiply provider cost. It is a product decision, not a transport optimisation, and is not part of this recommendation.
- **The slow provider is already avoidable without streaming.** Gemini speech-to-text (p50 3.4 s, p95 9.4 s) is about ten times slower than Groq's batch (p50 0.35 s). Choosing the voice provider (D-57, D-62) removes that cost with no new protocol.

## 4. Which configured provider supports streaming

- **Our adapters:** the speech-to-text port and the adapters in `app/ai/adapters/stt.py` implement **batch only**. There is no streaming port, no protocol message for audio chunks, and no stream-capable adapter.
- **Provider capabilities: not verified by me.** I did not consult provider documentation or call any provider's streaming interface in this session. M-3 tested batch calls only. I will not assert from memory which of Groq, Gemini, OpenAI or Sarvam offers live streaming transcription, or on what terms. If the owner wants this question answered, it needs a documented check per provider against current provider documentation, plus a real call.
- The recommendation does **not** depend on that answer, because the measured bottleneck is elsewhere.

## 5. Batch versus streaming, summarised

| | Batch (today) | Streaming (M-13) |
|---|---|---|
| Latency saved | none | about 0.2 to 0.3 s of about 3.9 s (illustrative, from the medians above) |
| Protocol change | none | new messages in `protocol.schema.json`, `packages/protocol`, the gateway; contract tests for each |
| Desktop change | none | audio chunks sent while speaking; backpressure; reconnect and partial-result handling |
| Consent and privacy | audio leaves the device per utterance after the user's start | audio leaves the device continuously while speaking; the consent and indicator text (acknowledgement, M-1) may need to say so, which resets consent |
| Failure handling | one request, one retry | partial transcripts, mid-stream provider failures, ordering |
| Cost and limits | one request per utterance | long-lived connections per user; provider concurrency limits (Groq's account already refused 7 of 20 on tokens per minute) |
| Risk | known | large (roadmap: "large; behind a feature flag with batch path retained") |

## 6. What would change this recommendation

Re-open M-13 only if **all** of the following are shown by measurement on the packaged application with real speech and the owner's provider accounts:

1. End-of-speech to transcript (silence window excluded) is more than about a third of end-of-speech to first answer token **on the provider the owner intends to use**. Today the best measured provider is under a tenth.
2. A provider the owner is willing to use offers a streaming transcription interface that has been tested, with its data-handling terms reviewed (OQ-4).
3. Cheaper changes have been tried first: choosing the faster voice provider, tuning the silence window with real recordings, and addressing language-model first-token latency (provider choice, a smaller model, shorter prompts) and its tail.

## 7. What was not measured (owner action)

| Item | Why not | How |
|---|---|---|
| End-to-end packaged-application latency with real human speech | Needs a person speaking, the packaged build, real provider keys and a ready session; not automatable here, and sending real speech to providers needs the owner's go-ahead | Run `INTERVIEW_PERF=1` with the packaged app, speak 20 questions, read the report block (`docs/PERFORMANCE_BASELINE.md` section 5), and also run `scripts/bench/backend_e2e_bench.py` against a ready session |
| Accents, noise, Hindi and Telugu speech-to-text quality and latency | M-3 used clean synthetic English only | The same run with real recordings |
| Real language-model first token on the faster providers | Several provider accounts had no credit or quota during M-3 | Fund or raise limits, rerun the answer part of M-3 |

**Next step if the owner agrees:** no engineering work. Run the packaged-app measurement above and add the numbers to `PERFORMANCE_BASELINE.md`; this document's section 6 then decides.
