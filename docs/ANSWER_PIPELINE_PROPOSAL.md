# Answer pipeline proposal: resume first, then documents, then validated general answer

Status: **APPROVED and implemented as D-66 (owner testing pending).** It would supersede the retrieval limits of D-38 and amend D-64 (new decision D-66 on approval).

## 1. Requirement (as stated by the owner)

1. Question matches the **resume** -> return the **exact resume text**.
2. Else it matches a **selected document** -> return the **exact text from that document**. Up to **10 documents** per session.
3. Else (in neither) -> answer from **general knowledge**, and the answer is **validated before it is sent**.
4. **Response time must be as low as possible.**

## 2. What is wrong today (measured on the owner's EWM session, 2026-11-16)

| Finding | Evidence |
|---|---|
| Dense-only retrieval ranks generic chunks above the right section for a short question | "enhancement in ewm": the real enhancement chunks ranked 11, 12 and 18; generic queue/config chunks ranked 1-3 |
| Cosine scores are low and close together, so absolute thresholds fail | related 0.3-0.5, unrelated 0.2-0.3; a 0.80 floor dropped every document passage |
| Chunks are fixed-size slices of the text, not sections | the "Enhancement in EWM" section is spread over several chunks with no heading attached |
| The LLM always rewrites, so exact text is only "usually" preserved, and it invents details | answer said "custom BAdI/PPF" for an enhancement the document describes as an RF transaction |
| Latency | warm path 1-3 s end to end; cold or memory-starved 5-30 s. Free RAM was 0.5-0.8 GB of 7.8 GB |

## 3. Proposed design (industry-standard pattern: hybrid retrieval + deterministic router + extractive answers + guarded fallback)

```
question
  -> normalise (trim, lowercase for lexical, keep original for the model)
  -> [cache hit?] per-session Redis cache of normalised question -> decision + answer   (~2 ms)
  -> embed query (resident model)                                                       (~10-30 ms warm)
  -> ONE SQL per corpus, run in parallel (resume | selected documents), each = 
       dense top-N (pgvector) + lexical top-N (Postgres full-text, BM25-style ranking)
       fused with Reciprocal Rank Fusion                                                (~10-20 ms)
  -> router (deterministic, no LLM):
       A. resume confident   -> EXTRACTIVE answer from the resume   (verbatim, no LLM)
       B. else document conf.-> EXTRACTIVE answer from the document (verbatim, no LLM)
       C. else               -> GENERAL answer -> validators -> send
```

### 3.1 Indexing (done once at upload, so it costs nothing at question time)

- **Structure-aware chunking:** split on headings and numbered questions ("40. Enhancement in EWM", "Self Introduction", "Project #2"), one chunk per section; only split a section that exceeds the size limit, with small overlap. Keep `doc_id`, `heading`, `section_path`, `page`.
- **Contextual headers:** the text that is embedded and indexed is `Document name > Heading > body`, so a question that names the topic matches the section title (this is the fix for "enhancement in ewm"). The stored, returned text stays the original body only.
- **Lexical index:** a generated `tsvector` column with a GIN index on chunks (use the `simple` configuration so Telugu/Hindi text is not mangled by English stemming). No new service, still Postgres.
- **Text clean-up** of PDF extraction artefacts (bullet glyphs, broken line wraps) before chunking, keeping the original offsets so exact quoting is still possible.
- **Optional, phase 2:** generate 3-5 likely questions per section at ingest ("doc2query") and index them with the chunk. Strongest recall boost for Q&A-style documents; costs one offline LLM call per chunk.

### 3.2 Retrieval and ranking

- **Hybrid (dense + lexical) with RRF.** Standard because it needs no score calibration (ranks, not scores), which is exactly what failed with the 0.80 floor.
- **Resume and documents are searched separately,** so the order "resume first, then documents" is a rule, not an accident of scores. All searches stay `user_id`-scoped and restricted to the session's selected sources.
- **Many documents (up to 10):** one query over all selected document ids; **per-document cap** (for example at most 3 chunks per document in the candidate set) so one large file cannot crowd out the others; **near-duplicate removal** by text fingerprint (your two PDFs repeat the same summary); on conflict the **resume wins**.
- **Optional cross-encoder rerank** of the top 20 (about 100-200 ms on CPU). Not in phase 1; add only if the evaluation set shows it is needed.
- **Confidence without magic numbers:** a result is "confident" when it is lexically anchored (query terms appear in the heading or body) AND its fused rank beats the next candidate by a margin, with the margin and minimum evidence calibrated from the evaluation set (section 6), not guessed.

### 3.3 Answer modes

| Mode | When | How | Typical latency |
|---|---|---|---|
| **A. Resume extractive** | confident resume match | return the matching resume section verbatim; if it is longer than the session length setting, pick the best sentence window (still verbatim) | 50-150 ms |
| **B. Document extractive** | no confident resume match, confident document match | same, from the document section (a whole Q&A block, as written) | 50-150 ms |
| **C. General** | neither is confident | LLM answer without candidate facts, then validation (3.4) | about 1.5-2.5 s |
| **Synthesis (rare)** | question needs several passages merged, or the session language differs from the source text | LLM with the retrieved passages and the "keep wording, add nothing" rule (today's v4 prompt) | 1-3 s |

Extractive answers bypass the model entirely: exact text is **guaranteed** (we verify the returned string is a substring of the stored source text), it cannot invent details, and it is the fastest possible path. Trade-off: tone, format and language preferences do not apply to extractive answers.

### 3.4 Validation before sending

- **Extractive (A/B):** substring check against the stored source (hash and offsets). Nothing else can go wrong.
- **Synthesis:** existing citation-integrity check, plus a quote check that numbers, tool names and system objects in the answer occur in the supplied passages.
- **General (C), in this order, stop at the first failure:**
  1. *Deterministic, a few ms:* not empty, language matches the session, length within bounds, no evidence tags or source IDs, no URLs or personal data echoed, no first-person claims about the candidate (employers, dates, projects, years) because the model has no candidate facts on this path.
  2. *Fast judge, about 300-500 ms:* a small, fast model returns a JSON verdict (on-topic, factually plausible, no unsafe content, no claims about the candidate). Runs on the general path only.
  3. On failure: one regeneration with the verdict as feedback; if that also fails, a safe "I could not produce a reliable answer, please rephrase" message.
- The answer carries a **source label for the UI** (Resume / Document name and page / General knowledge). It is a separate field, not part of the spoken text. Validation reduces but cannot prove factual correctness of general knowledge; the label tells the user which answers deserve a second look.

### 3.5 Latency plan (target: extractive p95 under 300 ms on the server; general p50 about 2 s)

1. Keep the embedding model **resident and warmed at API start** (one dummy query), so the first real question is not a cold load.
2. **Prewarm at "Start Interview":** open the provider's keep-alive HTTPS connection and touch the session's chunks.
3. Resume and document searches run **in parallel**; lexical and dense run in **one round trip**.
4. **Per-session cache** of normalised question -> answer (repeats and rephrases are common in practice).
5. Extractive modes use **no LLM call**, which removes the 1-3 s that dominates today.
6. Streaming stays for synthesis and general answers; first token is the metric.
7. **Memory:** the current embedding model (multilingual-e5-large, about 2 GB) is a poor fit for an 8 GB machine that also runs Docker, the web app and Chrome. Option: evaluate `multilingual-e5-small` (much smaller and faster). It changes the vector size (migration plus full re-embed), so it is adopted only if the evaluation set shows equal retrieval quality.
8. At current scale (hundreds to low thousands of chunks) the exact vector scan is fine; add an HNSW index only past roughly 50k chunks (D-31 revisited then).

## 4. Changes this implies

- **DB:** migration 0011 (chunk heading/section/page columns, `tsvector` + GIN index); re-ingest or re-index existing uploads (the existing reindex worker can do it).
- **Code:** chunker (structure-aware), `search_chunks` (hybrid + RRF + per-doc cap + dedupe), a router module in `app/qa`, extractive answerer, validators, source field on the answer event. All AI calls stay behind the `ai/` ports; prompts stay in `PromptBuilder` templates (new template version for the judge and general prompts).
- **Contracts:** additive `source` field on the SSE and WebSocket answer events and in the history GET (`docs/API.md`), regenerated protocol package.
- **Docs and decisions:** D-66 supersedes the "no hybrid / top-k 8" parts of D-38 and replaces the D-64 floor; `ARCHITECTURE.md` section 5; `TESTING_STRATEGY.md` (evaluation set).
- **Tests (mandatory):** user isolation across 10 documents, unselected documents never searched, property test "extractive output is a substring of its source", per-document cap, duplicate handling, router order (resume beats document beats general), validator unit tests, regression on the Phase 8/9 contracts, latency smoke test.

## 5. Delivery in phases (each independently useful and reversible)

| Phase | Scope | Why this order |
|---|---|---|
| 1 | Structure-aware chunking + contextual headers + lexical index + RRF + per-doc cap + dedupe + source label | Biggest quality gain, fixes "enhancement in ewm" |
| 2 | Evaluation harness and calibrated router + extractive modes A/B | Delivers the exact-text and low-latency requirements |
| 3 | General path validators (deterministic + fast judge) | Delivers "validated before sending" |
| 4 | Latency work (warm-up, cache, optional smaller embedding model) | Measured against the harness |

## 6. How we will know it works (instead of guessing thresholds)

- Build an evaluation set from the owner's real files: about 50 questions (vague like "enhancement in ewm", exact like "what is POSC", resume questions, and questions in neither source) with the expected section.
- Report recall@5, MRR, verbatim rate, fallback precision, and latency p50/p95 per mode. A phase ships only if it improves these numbers.
- Thresholds and margins come from this set, not from assumptions (the 0.80 floor was an unmeasured assumption and broke retrieval).

## 7. Decisions needed from the owner

1. **Exact text vs formatted answer.** Extractive answers ignore tone/length/language settings. Recommended: yes, exact text wins when confident.
2. **Resume and document both match.** Recommended: resume answer only; the document is used when the resume has no confident match.
3. **Session language differs from the source text** (for example Telugu session, English document). Recommended: use the synthesis mode that keeps wording and translates.
4. **Validation cost on general answers.** Recommended: deterministic checks always, fast judge model on the general path (about +0.4 s).
5. **Smaller embedding model.** Recommended: evaluate in phase 4, adopt only if quality is equal.
6. **Hypothetical-question indexing (phase 2 option).** Recommended: yes if phase 1 results leave gaps.
