# RAG Evaluation Method

## Scope and pipeline

The current path is:

1. Ingestion extracts and normalizes a source, chunks its text, and stores chunk metadata.
2. The worker embeds chunks with the configured `EmbeddingProvider`; `chunks` and
   `chunk_embeddings` are persisted together in PostgreSQL/pgvector.
3. Q&A validates the session and selected materials, embeds the raw question once, searches the
   selected resume and documents with authenticated-user and model filters, then merges results
   using the resume quota, top-k limit, and character budget.
4. `PromptBuilder` keeps the job description separate from candidate material. The prompt goes to
   the configured LLM provider; answer text and metrics are persisted once.

The retrieval and context implementation is in `app/retrieval/service.py` and `app/qa/context.py`;
the Q&A orchestration is in `app/qa/service.py`. The synthetic benchmark does not replace these
components or use a database.

## Context quality inspected

- Chunk defaults are 800 characters with 100-character overlap; the chunker prefers sentence,
  line, and word boundaries. Defaults were not changed.
- Stored chunks preserve source type/id, index, character offsets, and a content hash. Page and
  section metadata are not currently preserved.
- Context defaults are top-k 8, resume quota 3, 6,000 characters, and the last three exchanges.
  Query embedding is cached between the separate resume/document searches.
- Session source IDs are derived server-side. The retrieval SQL scopes both rows by `user_id` and
  filters by embedding model. Company and job description are passed separately to the prompt.
- Retrieval has no minimum-similarity cutoff: nearest chunks are returned even for unsupported or
  irrelevant questions. Prompt v2 therefore explicitly asks the model to acknowledge weak or
  missing evidence.

No chunking or retrieval parameter change was justified by this evaluation: it uses the
deterministic fake embedder, not the production embedding model or pgvector.

## Synthetic retrieval benchmark

Dataset: `services/api/tests/fixtures/rag_eval_dataset.json` (version 1). It contains synthetic
resume and supporting-document chunks and ten cases across resume-specific, job-description,
multi-document, unsupported, ambiguous, and irrelevant questions. The unselected-session canary
is excluded from the evaluated corpus. Job-description-only cases are marked as not applicable to
candidate-material retrieval because the JD is supplied in its own prompt block, not indexed as a
candidate skill.

Run from the API directory:

```powershell
python -m tests.rag_evaluation
python -m pytest tests/test_rag_evaluation.py tests/test_prompt_builder.py tests/test_qa_context.py
```

Metrics:

- Precision@K is the relevant retrieved chunks divided by K, averaged over answerable retrieval
  cases.
- Recall@K is the fraction of the case's gold chunks retrieved in the top K, averaged over
  answerable retrieval cases.
- MRR is the mean reciprocal rank of the first relevant chunk, over answerable retrieval cases.
- Unanswerable non-empty retrieval rate records how often nearest-neighbor search still returns
  chunks despite an empty gold set; this is a retrieval exposure measure, not a model hallucination
  rate.
- Retrieval latency is measured around fake query embedding and in-memory exact ranking. It
  excludes database, network, production embeddings, and model generation.

## Prompt and answer evaluation

Prompt contract coverage checks five explicit requirements in the rendered system prompt:
candidate grounding, missing-information behavior, separation of job requirements from candidate
skills, evidence attribution, and session isolation. This is a deterministic prompt inspection,
not evidence that a model will obey the instructions.

Actual answer relevance, retrieved-context relevance, faithfulness, hallucination rate, and answer
latency require judgments of generated responses. The harness accepts human-adjudicated rows:

```json
[
  {
    "question_id": "case-id",
    "relevant": true,
    "context_relevant": true,
    "faithful": true,
    "factual_claims": 2,
    "unsupported_claims": 0,
    "latency_ms": 850.0
  }
]
```

`relevant`, `context_relevant`, and `faithful` are binary human judgments against the criteria
below. Annotators count factual claims and claims unsupported by the retrieved candidate evidence;
the harness reports unsupported-claim rate and the fraction of answers containing any unsupported
claim. `latency_ms` is the measured end-to-end generation latency for that output. Evaluate a
minimum of two reviewers for quality-critical comparisons and resolve disagreements before using
scores as gates.

### Answer judgment criteria

- **Relevance:** answers the requested question at the requested level, without substituting a
  different question.
- **Context relevance:** selected candidate excerpts directly support the candidate-specific
  answer; a job-description requirement alone is not candidate evidence.
- **Faithfulness:** every candidate-specific factual claim is supported by supplied candidate
  material, and uncertainty/absence is not converted into a fact.
- **Hallucination:** at least one factual candidate claim is unsupported, contradicted, or attributed
  to the candidate solely because it appears in the job description.
- **Latency:** time from accepted question to terminal answer event; report median and p95 when the
  sample is large enough. The current CLI summary reports mean for a small hand-judged set.

Answer judgments can be added to a JSON file and run with:

```powershell
python -m tests.rag_evaluation --answers path\to\answer_judgments.json
```

No live-provider answer output is embedded in the repository's synthetic fixtures. Results must
identify the model/provider, prompt version, embedding model, dataset version, and judgment
procedure before being interpreted as an answer-quality comparison.
