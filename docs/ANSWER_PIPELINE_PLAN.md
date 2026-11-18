# Answer pipeline implementation plan (adaptive routing)

Status: **IMPLEMENTED in one batch (phases 0-4) as D-66; deviations and measurements are recorded in docs/DECISIONS.md D-66. Owner testing pending.** Direction approved by the owner on 2026-11-16: the design in `docs/ANSWER_PIPELINE_PROPOSAL.md` plus **adaptive routing** (a deterministic router decides; a small fast AI step rewrites the question only when the router is unsure). The recommended answers in section 7 of the proposal are taken as the defaults unless the owner says otherwise.

Per `CLAUDE.md` the work stops and reports after **each phase**; the next phase starts only on the owner's word.

## 0. Ground truth in the current code

| Area | Today | File |
|---|---|---|
| Chunking | fixed 800-char slices, 100 overlap, no headings | `services/api/app/ingestion/chunking.py` |
| Chunk table | no heading, page, section range or full-text column | `app/db/models.py` (`Chunk`), migrations 0005, 0008 (RLS) |
| Search | dense only, `search_chunks` | `app/retrieval/service.py` |
| Context | top-k 8, resume quota 3, char budget | `app/qa/context.py` (D-38) |
| Answer flow | retrieve -> prompt -> LLM stream -> persist | `app/qa/service.py` (`QuestionAnswerer.ask`) |
| Reindex | worker exists | `app/workers/reindex.py` |
| Contracts | SSE/WS events, generated `packages/protocol` | `docs/API.md` sections 3 and 8 |
| Limits | 10 documents per session already enforced | `session_max_documents` in `app/core/config.py` |
| Latest migration | 0010 (next is 0011) | `alembic/versions/` |

## 1. Phase 0: decision and baseline (no behaviour change; about 1-2 days)

| # | Task | Output |
|---|---|---|
| 0.1 | Write **D-66** (supersedes the retrieval rules of D-38 and the D-64 floor); update `ARCHITECTURE.md` section 5, `DATABASE.md`, `TESTING_STRATEGY.md` | docs only |
| 0.2 | Build the **evaluation set** from the owner's real files: about 50 questions in four groups (vague like "enhancement in ewm", exact like "what is POSC", resume questions, questions in neither source), each with the expected section or "general" | `services/api/tests/eval/questions.yaml` (questions and expected section ids only; no document text) |
| 0.3 | **Baseline script** measuring recall@5, MRR and latency p50/p95 on the current dense pipeline | `services/api/scripts/eval_retrieval.py` and a stored baseline report |
| 0.4 | Add the feature flags (all default **off**): `QA_RETRIEVAL_MODE=dense|hybrid`, `QA_ROUTER_MODE=off|shadow|on`, `QA_VALIDATE_GENERAL=off|on` | `app/core/config.py` |

**Gate:** owner confirms the evaluation questions cover what matters, and approves D-66.

## 2. Phase 1: structure-aware indexing and hybrid retrieval (about 4-6 days)

| # | Task | Detail |
|---|---|---|
| 1.1 | **Migration 0011** | `chunks`: `heading text`, `section_path text`, `page int`, `section_start int`, `section_end int` (the whole section's offsets, so the full section can be returned verbatim even when it spans several chunks), generated `tsv tsvector` (`to_tsvector('simple', heading || ' ' || text)`) + GIN index. `generated_answers`: `source_kind text` (`resume`/`document`/`general`/`synthesis`), `source_refs jsonb` (document id, page, heading). Reversible downgrade. RLS from 0008 covers the new columns |
| 1.2 | **Page offsets from extraction** | extend `extractors.py` to keep page boundaries so chunks know their page |
| 1.3 | **Section-aware chunker** | new `chunk_sections()` in `chunking.py`: detect headings (numbered "N. Title", "Project #N", "Enhancement N", "Part N", short unpunctuated title lines, Markdown `#`); one chunk per section; split an over-long section with the existing sentence logic while keeping its heading. **Fallback to today's fixed chunker** when no headings are found, so a plain resume still works. Chunks stay contiguous slices of the extracted text (fidelity rule unchanged) |
| 1.4 | **Contextual embedding text** | the vector and the `tsv` are built from `Document name > Heading > body`; the stored and returned `text` stays the original body only |
| 1.5 | **Hybrid search** | `search_hybrid()` in `retrieval/service.py`: one SQL with CTEs, dense top-N plus lexical top-N (`ts_rank_cd` with an OR-style tsquery of content words, so a short question still matches), fused by **Reciprocal Rank Fusion (k=60)**, **per-document cap** (window `row_number() over (partition by source_id)`), all scoped by `user_id` and `source_id = ANY(session sources)` exactly as today |
| 1.6 | **De-duplication** | drop near-duplicate chunks across documents (exact `content_hash`, then normalised fingerprint); resume copy wins |
| 1.7 | **Wire behind the flag** | `context.py` uses `search_hybrid` when `QA_RETRIEVAL_MODE=hybrid`; dense stays the default until accepted |
| 1.8 | **Reindex** | extend `workers/reindex.py` to re-chunk and re-embed existing uploads (contextual text changes the vectors). Runs per user, resumable, never deletes old chunks until the new set is complete |
| 1.9 | **Source label** | `source_kind` and `source_refs` stored on each answer; additive `source` field on `answer.done` (SSE and WS) and history GETs; `docs/API.md`, protocol schemas and generated package updated; web page and desktop show a small label (Resume / Document name and page / General). Not part of the spoken text |

**Tests:** user-isolation across 10 documents and two users; unselected documents never returned; per-document cap; duplicate handling; fixed-chunker fallback on a heading-less file; migration up/down; contract tests for the additive field; Phase 8/9 regression.

**Acceptance:** on the evaluation set, recall@5 clearly above the Phase 0 baseline; "enhancement in ewm" returns the real enhancement sections in the top 3; no isolation regression.

**Gate:** owner reviews the evaluation report and flips `QA_RETRIEVAL_MODE=hybrid`.

## 3. Phase 2: router, exact-text answers and adaptive rewrite (about 4-5 days)

| # | Task | Detail |
|---|---|---|
| 2.1 | **Answer router** | new `app/qa/answer_router.py` (the existing `app/qa/router.py` is the HTTP router): returns `Decision(mode, evidence, reason)` where mode is `resume_extractive`, `document_extractive`, `synthesis`, `general` or `uncertain`. Pure function over search results, unit-tested |
| 2.2 | **Confidence rules** | confident = lexically anchored (content words in the heading, or enough of them in the body) **and** a fused-rank margin over the next candidate. Constants (`QA_ROUTER_*`) are calibrated from the evaluation set, not guessed. Resume is searched and judged first; documents only when the resume is not confident |
| 2.3 | **Intent shortcuts** | rule-based, no AI: introduction/experience/background questions prefer the resume's summary section |
| 2.4 | **Extractive answerer** | loads the full section by offsets, shortens to a sentence window if over the session length band, verifies the output is a substring of the stored source, then streams it through the **existing events** (`answer.started` with provider `extractive`, a few paragraph `answer.delta`s, `answer.done`). Same persistence, history, WebSocket, resume and cancel behaviour. **Nothing is sent to any AI provider on this path** |
| 2.5 | **Adaptive rewrite** | only for `uncertain`: one small fast model call through the `ai/` ports returns `{rewritten_query, keywords[]}`, hard-capped at 800 ms (skipped on timeout); search runs again; still uncertain -> `synthesis` (today's v4 "keep wording, add nothing" prompt). Only the question text is sent, never document content. New template `templates/router/v1/rewrite.md` (all prompts stay in templates) |
| 2.6 | **Language rule** | session language differs from the source text (script check) -> `synthesis` with translation instead of extractive |
| 2.7 | **Shadow mode** | `QA_ROUTER_MODE=shadow` logs the decision (ids and mode only, never text) while the answer still comes from today's path, so the router can be compared with real traffic before it takes over |

**Tests:** router decision table (resume beats document beats general); property test "extractive output is always a substring of its source"; extractive answers never call a provider; adaptive step timeout; isolation again; SSE/WS contract parity; cancel and resume unchanged.

**Acceptance:** extractive path p95 under 300 ms server-side on the evaluation set; verbatim rate 100% on extractive answers; router mode accuracy at or above the agreed target on the evaluation set.

**Gate:** owner reviews shadow-mode results, then sets `QA_ROUTER_MODE=on`.

## 4. Phase 3: validated general answers (about 2-3 days)

| # | Task | Detail |
|---|---|---|
| 3.1 | **Deterministic validators** | `app/qa/validation.py`: not empty, language matches, length in bounds, no evidence tags/IDs, no URLs or personal data echoed, **no first-person claims about the candidate** (employers, dates, projects, years) because this path has no candidate facts |
| 3.2 | **Fast judge** | small model through the `ai/` ports returns a JSON verdict (on-topic, plausible, no unsafe content, no candidate claims); sees only the question and the answer, never documents (no injection surface); template `templates/validation/v1/judge.md`; 700 ms cap |
| 3.3 | **Flow** | general path is generated, validated, then emitted. On failure one regeneration with the verdict as feedback; second failure -> a fixed safe message with `answer.error` code `answer_not_validated` (new, additive) |
| 3.4 | **Judge unavailable** | default: deterministic result stands and the answer is labelled "General (not fully validated)" (owner decision below) |

**Tests:** validator unit tests including adversarial answers ("I worked at Google for 5 years"); judge timeout; regeneration path; error code contract.

**Acceptance:** general path p50 around 2 s including validation; the adversarial set is blocked.

## 5. Phase 4: latency and operations (about 2-3 days)

| # | Task | Detail |
|---|---|---|
| 4.1 | **Warm-up at API start** | load the embedder and run one dummy query; create provider clients; warm the DB pool |
| 4.2 | **Prewarm on Start Interview** | the handoff issue call triggers a background warm of the session's sources and provider connection (no new endpoint) |
| 4.3 | **Parallel retrieval** | resume and document searches via `asyncio.gather` |
| 4.4 | **Per-session answer cache** | Redis, key = user + session + normalised question + hash of the selected materials, short TTL, invalidated when materials change; strictly per user |
| 4.5 | **Metrics** | Prometheus histograms per mode (retrieval, router, extractive, rewrite, general, validation) and a counter of decisions |
| 4.6 | **Embedding model review** | measure `multilingual-e5-small` against the evaluation set; adopt only with equal quality (needs migration for the vector size and a full re-embed, so it is its own decision D-67) |

**Acceptance:** cold start no longer shows a multi-second first question; numbers reported against the Phase 0 baseline.

## 6. Cross-cutting rules for every phase

- Targeted tests plus the test impact matrix; full regression only at the end (milestone).
- User-isolation test for every new query; no document, question or answer text in logs at INFO or above.
- All AI calls through `ai/` ports; all prompt text in versioned templates; no SDK imports outside adapters.
- Docs and `DECISIONS.md` updated in the same change; contract tests run when an event changes.
- **Rollback is a flag flip:** `QA_RETRIEVAL_MODE=dense`, `QA_ROUTER_MODE=off`, `QA_VALIDATE_GENERAL=off`. The migration only adds nullable columns, so old code keeps working.

## 7. Risks and how they are handled

| Risk | Handling |
|---|---|
| Heading detection is a heuristic (PDF text has no formatting) | fixed-chunker fallback; evaluated on the owner's real files; owner can see which files were section-split |
| Re-embedding existing uploads is heavy on an 8 GB machine | resumable per-user reindex, run when the machine is idle, old chunks kept until done |
| Router constants wrong | calibrated on the evaluation set, shadow mode before it takes over, flag rollback |
| Extractive ignores tone/length/language settings | accepted trade-off (owner decision 1); length is fitted by sentence window |
| Judge model rate limits or outage | tight timeout, defined degradation (label), small prompts |
| Provider name `extractive` appears in the UI | UI shows the source label instead of provider for these answers |

## 8. Decisions still open

1. **Judge unavailable:** recommended "send with a not-fully-validated label". Alternative: refuse to answer.
2. **Evaluation questions:** the owner confirms or adds questions (especially the ones that matter most for real interviews).
3. **Reindex timing:** run it right after Phase 1 approval, or at an agreed quiet time.
4. **Embedding model change (4.6):** decided only after Phase 4 measurements.
