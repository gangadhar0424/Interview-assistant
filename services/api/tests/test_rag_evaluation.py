import pytest

from app.preferences.answer_config import AnswerConfig
from app.preferences.enums import AnswerFormat, AnswerLength, AnswerTone, Language, QuestionType
from app.preferences.prompt_builder import PromptBuilder, PromptInputs
from tests.rag_evaluation import (
    AnswerJudgment,
    evaluate,
    evaluate_answers,
    load_dataset,
    parse_answer_judgments,
    prompt_contract_coverage,
)


def test_synthetic_dataset_measures_retrieval_and_excludes_unselected_material() -> None:
    dataset = load_dataset()
    result = evaluate(dataset)

    assert len(dataset.cases) == 10
    assert all(chunk.id != "unselected-session-canary" for chunk in dataset.chunks)
    assert result["retrieval_case_count"] == 9
    assert result["answerable_case_count"] == 6
    assert result["unanswerable_case_count"] == 3
    assert result["precision_at_k"] == pytest.approx(4 / 15)
    assert result["recall_at_k"] == 1.0
    assert result["mean_reciprocal_rank"] == pytest.approx(19 / 24)
    for metric in (
        "precision_at_k",
        "recall_at_k",
        "mean_reciprocal_rank",
        "mean_retrieval_latency_ms",
        "p95_retrieval_latency_ms",
    ):
        metric_value = result[metric]
        assert isinstance(metric_value, float)
        assert metric_value >= 0
    assert result["unanswerable_nonempty_retrieval_rate"] == 1.0


def test_answer_judgment_metrics_measure_relevance_faithfulness_hallucinations_and_latency() -> (
    None
):
    judgments = parse_answer_judgments(
        [
            {
                "question_id": "supported",
                "relevant": True,
                "context_relevant": True,
                "faithful": True,
                "factual_claims": 2,
                "unsupported_claims": 0,
                "latency_ms": 800.0,
            },
            {
                "question_id": "hallucinated",
                "relevant": True,
                "context_relevant": False,
                "faithful": False,
                "factual_claims": 2,
                "unsupported_claims": 1,
                "latency_ms": 1200.0,
            },
        ]
    )
    result = evaluate_answers(judgments)

    assert result == {
        "case_count": 2,
        "answer_relevance_rate": 1.0,
        "context_relevance_rate": 0.5,
        "faithfulness_rate": 0.5,
        "hallucination_case_rate": 0.5,
        "unsupported_claim_rate": 0.25,
        "mean_answer_latency_ms": 1000.0,
    }


def test_answer_judgment_parser_rejects_bad_scores() -> None:
    with pytest.raises(ValueError, match="invalid counts or latency"):
        evaluate_answers((AnswerJudgment("bad", True, True, False, 1, 2, 0.0),))
    with pytest.raises(ValueError, match="invalid types"):
        parse_answer_judgments(
            [
                {
                    "question_id": "bad",
                    "relevant": "yes",
                    "context_relevant": True,
                    "faithful": True,
                    "factual_claims": 0,
                    "unsupported_claims": 0,
                    "latency_ms": 1,
                }
            ]
        )


def test_prompt_contract_coverage_improves_without_editing_v1() -> None:
    config = AnswerConfig(
        language=Language.EN,
        question_type=QuestionType.AUTO,
        answer_format=AnswerFormat.FULL_SCRIPT,
        answer_length=AnswerLength.BALANCED,
        answer_tone=AnswerTone.SIMPLE,
        extra_instructions="",
        target_min_words=120,
        target_max_words=180,
    )
    v1 = prompt_contract_coverage(
        PromptBuilder("v1").build(config, _prompt_inputs()).messages[0].content
    )
    v2 = prompt_contract_coverage(
        PromptBuilder("v2").build(config, _prompt_inputs()).messages[0].content
    )

    assert sum(v1.values()) == 2
    assert sum(v2.values()) == 5


def _prompt_inputs() -> PromptInputs:
    return PromptInputs(question="What experience do I have?")
