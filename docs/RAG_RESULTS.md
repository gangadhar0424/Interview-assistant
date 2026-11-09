# RAG Evaluation Results

## Run metadata

- Dataset: synthetic gold corpus v1, 10 questions, 6 selected candidate-material chunks
- Retrieval K: 5
- Evaluation embedder: deterministic `FakeEmbeddingProvider` (`fake-hash-v1`)
- Answer prompt versions: v1 baseline and v2 candidate
- Database/pgvector and live LLM provider: not used
- Command: `python -m tests.rag_evaluation` from `services/api`

## Retrieval results

| Metric | Baseline | Post-change |
|---|---:|---:|
| Precision@5 | 0.267 | 0.267 |
| Recall@5 | 1.000 | 1.000 |
| Mean Reciprocal Rank | 0.792 | 0.792 |
| Unanswerable queries returning chunks | 100% (3/3) | 100% (3/3) |
| Mean fake-embedding/in-memory retrieval latency | 0.436 ms | 0.436 ms |
| p95 fake-embedding/in-memory retrieval latency | 0.450 ms | 0.450 ms |

Retrieval and chunking code/defaults did not change, so the baseline and post-change retrieval
columns represent the same measured path; this phase does not claim a retrieval improvement. The
latencies are one local synthetic run and are not comparable to production Postgres or end-to-end
latency. The 100% non-empty rate reflects the existing nearest-neighbor behavior without a
similarity threshold, not 100% hallucination.

The fake embedder ranks all gold evidence within K in this small dataset (Recall@5 1.0), but
Precision@5 is 0.267 because it also returns unrelated chunks. This does not establish semantic
retrieval performance: the fake provider is a hashed bag-of-words model.

## Prompt-contract results

| Requirement detected in system prompt | v1 baseline | v2 |
|---|---:|---:|
| Candidate-fact grounding | Yes | Yes |
| Missing-information behavior | Yes | Yes |
| Job requirements distinct from candidate skills | No | Yes |
| Evidence ID/source attribution | No | Yes |
| Current-session-only candidate evidence | No | Yes |
| Total contract coverage | 2/5 | 5/5 |

This is a deterministic template coverage result only. It does not show that an LLM follows these
instructions or that generated answers became more relevant or faithful.

## Answer-quality status

| Metric | Baseline | Post-change |
|---|---|---|
| Answer relevance | Not measured | Not measured |
| Answer-level context relevance | Not measured | Not measured |
| Faithfulness | Not measured | Not measured |
| Hallucination rate | Not measured | Not measured |
| End-to-end answer latency | Not measured | Not measured |

No generated answer set was run or human-adjudicated. The evaluation harness accepts explicit
human judgments for these metrics, but test fixtures that validate its arithmetic are not quality
results. A real before/after answer-quality claim requires generated outputs from the same
provider/model settings, dataset, and blinded judgment procedure.

## Validation

- `tests/test_prompt_builder.py`, `tests/test_rag_evaluation.py`, and `tests/test_qa_context.py`:
  109 passed.
- Ruff lint/format checks on changed Python files: passed.
- Mypy on the changed prompt, Q&A, and evaluation modules: passed.
- DB-backed `test_qa_service.py` and `test_sessions.py` could not run because no `DATABASE_URL` is
  configured for the test fixture; no schema or migration was changed.

## Follow-up based on measurements

1. Run the synthetic evaluator after changes to retrieval, chunking, or embedding configuration.
2. Add a measured pgvector/production-embedding evaluation before tuning chunk defaults or ranking.
3. Collect and adjudicate outputs from a configured provider to establish answer relevance,
   faithfulness, hallucination, and generation-latency baselines.
4. Investigate a retrieval similarity cutoff only with database-backed relevance and unsupported
   question measurements; this phase intentionally made no cutoff or schema change.
