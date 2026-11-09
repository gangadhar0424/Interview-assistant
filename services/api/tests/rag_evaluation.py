"""Repeatable, offline retrieval benchmark over the synthetic gold dataset."""

import argparse
import json
import math
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from app.embeddings.fake import FakeEmbeddingProvider

DATASET = Path(__file__).parent / "fixtures" / "rag_eval_dataset.json"
PROMPT_CONTRACTS = {
    "candidate_grounding": (
        ("facts about the candidate", "candidate facts"),
        ("never invent", "must be supported by candidate-material evidence"),
    ),
    "missing_information": (
        ("say what is missing", "say that the supplied material does not establish"),
    ),
    "role_and_candidate_separation": (
        ("job description describes the role",),
        ("job requirement is a candidate skill",),
    ),
    "evidence_attribution": (("[e1]",), ("evidence (not spoken)",)),
    "session_isolation": (("current session",), ("other sessions",)),
}


@dataclass(frozen=True)
class Chunk:
    id: str
    source_type: str
    text: str
    selected: bool = True


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    query: str
    relevant: tuple[str, ...]
    retrieval_applicable: bool = True


@dataclass(frozen=True)
class Dataset:
    k: int
    chunks: tuple[Chunk, ...]
    cases: tuple[Case, ...]


@dataclass(frozen=True)
class AnswerJudgment:
    """Human-adjudicated output metrics; this suite does not infer factual claims from prose."""

    question_id: str
    relevant: bool
    context_relevant: bool
    faithful: bool
    factual_claims: int
    unsupported_claims: int
    latency_ms: float


def prompt_contract_coverage(system_prompt: str) -> dict[str, bool]:
    normalized = " ".join(system_prompt.casefold().split())
    return {
        name: all(any(phrase in normalized for phrase in alternatives) for alternatives in groups)
        for name, groups in PROMPT_CONTRACTS.items()
    }


def load_dataset(path: Path = DATASET) -> Dataset:
    raw = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    k = raw.get("k")
    if type(k) is not int or k < 1:
        raise ValueError("evaluation K must be a positive integer")
    chunks = tuple(
        Chunk(
            id=str(item["id"]),
            source_type=str(item["source_type"]),
            text=str(item["text"]),
            selected=bool(item.get("selected", True)),
        )
        for item in cast(list[dict[str, object]], raw["chunks"])
    )
    cases = tuple(
        Case(
            id=str(item["id"]),
            category=str(item["category"]),
            query=str(item["query"]),
            relevant=tuple(str(value) for value in cast(list[str], item["relevant"])),
            retrieval_applicable=bool(item.get("retrieval_applicable", True)),
        )
        for item in cast(list[dict[str, object]], raw["cases"])
    )
    selected_ids = {chunk.id for chunk in chunks if chunk.selected}
    if any(not set(case.relevant) <= selected_ids for case in cases):
        raise ValueError("evaluation gold set references an unselected or missing chunk")
    if len(selected_ids) != sum(chunk.selected for chunk in chunks):
        raise ValueError("evaluation chunk IDs must be unique")
    return Dataset(
        k=k,
        chunks=tuple(chunk for chunk in chunks if chunk.selected),
        cases=cases,
    )


def evaluate(dataset: Dataset, embedder: FakeEmbeddingProvider | None = None) -> dict[str, object]:
    """Measure exact ranking metrics; latency covers query embedding and in-memory ranking."""
    provider = embedder or FakeEmbeddingProvider()
    chunks = dataset.chunks
    vectors = provider.embed_documents([chunk.text for chunk in chunks])
    ranked_cases: list[tuple[Case, list[str]]] = []
    latencies_ms: list[float] = []
    for case in dataset.cases:
        if not case.retrieval_applicable:
            continue
        started = time.perf_counter()
        query_vector = provider.embed_query(case.query)
        similarities = [
            (
                sum(left * right for left, right in zip(query_vector, vector, strict=True)),
                chunk.id,
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        ranked_ids = [
            chunk_id
            for _, chunk_id in sorted(similarities, key=lambda item: (-item[0], item[1]))[
                : dataset.k
            ]
        ]
        latencies_ms.append((time.perf_counter() - started) * 1000)
        ranked_cases.append((case, ranked_ids))

    answerable = [(case, ranked) for case, ranked in ranked_cases if case.relevant]
    unanswerable = [(case, ranked) for case, ranked in ranked_cases if not case.relevant]
    precision = [
        len(set(ranked[: dataset.k]) & set(case.relevant)) / dataset.k
        for case, ranked in answerable
    ]
    recall = [
        len(set(ranked[: dataset.k]) & set(case.relevant)) / len(case.relevant)
        for case, ranked in answerable
    ]
    reciprocal_ranks = []
    for case, ranked in answerable:
        first_relevant = next(
            (rank for rank, chunk_id in enumerate(ranked, 1) if chunk_id in case.relevant),
            None,
        )
        reciprocal_ranks.append(1 / first_relevant if first_relevant is not None else 0.0)
    sorted_latencies = sorted(latencies_ms)
    p95_index = max(0, math.ceil(0.95 * len(sorted_latencies)) - 1)

    return {
        "k": dataset.k,
        "chunk_count": len(chunks),
        "case_count": len(dataset.cases),
        "retrieval_case_count": len(ranked_cases),
        "answerable_case_count": len(answerable),
        "unanswerable_case_count": len(unanswerable),
        "precision_at_k": statistics.mean(precision) if precision else 0.0,
        "recall_at_k": statistics.mean(recall) if recall else 0.0,
        "mean_reciprocal_rank": statistics.mean(reciprocal_ranks) if reciprocal_ranks else 0.0,
        "unanswerable_nonempty_retrieval_rate": (
            sum(bool(ranked) for _, ranked in unanswerable) / len(unanswerable)
            if unanswerable
            else 0.0
        ),
        "mean_retrieval_latency_ms": statistics.mean(latencies_ms) if latencies_ms else 0.0,
        "p95_retrieval_latency_ms": sorted_latencies[p95_index] if sorted_latencies else 0.0,
        "ranked_cases": [
            {"id": case.id, "category": case.category, "ranked_ids": ranked}
            for case, ranked in ranked_cases
        ],
    }


def evaluate_answers(judgments: tuple[AnswerJudgment, ...]) -> dict[str, float | int]:
    if not judgments:
        raise ValueError("at least one answer judgment is required")
    if any(
        not judgment.question_id
        or type(judgment.relevant) is not bool
        or type(judgment.context_relevant) is not bool
        or type(judgment.faithful) is not bool
        or type(judgment.factual_claims) is not int
        or type(judgment.unsupported_claims) is not int
        or type(judgment.latency_ms) not in (int, float)
        or not math.isfinite(judgment.latency_ms)
        or judgment.factual_claims < 0
        or judgment.unsupported_claims < 0
        or judgment.unsupported_claims > judgment.factual_claims
        or judgment.latency_ms < 0
        for judgment in judgments
    ):
        raise ValueError("answer judgments contain invalid counts or latency")
    factual_claims = sum(judgment.factual_claims for judgment in judgments)
    unsupported_claims = sum(judgment.unsupported_claims for judgment in judgments)
    return {
        "case_count": len(judgments),
        "answer_relevance_rate": sum(j.relevant for j in judgments) / len(judgments),
        "context_relevance_rate": sum(j.context_relevant for j in judgments) / len(judgments),
        "faithfulness_rate": sum(j.faithful for j in judgments) / len(judgments),
        "hallucination_case_rate": sum(j.unsupported_claims > 0 for j in judgments)
        / len(judgments),
        "unsupported_claim_rate": unsupported_claims / factual_claims if factual_claims else 0.0,
        "mean_answer_latency_ms": statistics.mean(j.latency_ms for j in judgments),
    }


def parse_answer_judgments(raw: object) -> tuple[AnswerJudgment, ...]:
    if not isinstance(raw, list):
        raise ValueError("answer judgments must be a JSON array")
    judgments: list[AnswerJudgment] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each answer judgment must be a JSON object")
        question_id = item.get("question_id")
        relevant = item.get("relevant")
        context_relevant = item.get("context_relevant")
        faithful = item.get("faithful")
        factual_claims = item.get("factual_claims")
        unsupported_claims = item.get("unsupported_claims")
        latency_ms = item.get("latency_ms")
        if (
            not isinstance(question_id, str)
            or type(relevant) is not bool
            or type(context_relevant) is not bool
            or type(faithful) is not bool
            or type(factual_claims) is not int
            or type(unsupported_claims) is not int
            or type(latency_ms) not in (int, float)
        ):
            raise ValueError("answer judgment fields have invalid types")
        judgments.append(
            AnswerJudgment(
                question_id=question_id,
                relevant=relevant,
                context_relevant=context_relevant,
                faithful=faithful,
                factual_claims=factual_claims,
                unsupported_claims=unsupported_claims,
                latency_ms=float(cast(int | float, latency_ms)),
            )
        )
    return tuple(judgments)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--answers", type=Path)
    args = parser.parse_args()
    output: dict[str, object] = {"retrieval": evaluate(load_dataset())}
    if args.answers is not None:
        raw_judgments = json.loads(args.answers.read_text("utf-8"))
        output["answers"] = evaluate_answers(parse_answer_judgments(raw_judgments))
    print(json.dumps(output, indent=2, sort_keys=True))
