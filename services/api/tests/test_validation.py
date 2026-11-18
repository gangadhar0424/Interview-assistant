"""Validation of general answers and the question rewrite (D-66)."""

import asyncio
import uuid
from typing import Any

import pytest

from app.ai.errors import Unavailable
from app.ai.types import Completion, GenerationConfig, Message
from app.preferences.enums import Language
from app.preferences.prompt_builder import reviewer_note
from app.qa.rewrite import parse_rewrite_reply, rewrite_question
from app.qa.validation import (
    REASON_CANDIDATE_CLAIM,
    REASON_EMPTY,
    REASON_LANGUAGE,
    REASON_LEAK,
    REASON_PII,
    REASON_TOO_LONG,
    Verdict,
    check_deterministic,
    judge_answer,
    parse_judge_reply,
)

GOOD = "A Kubernetes pod is the smallest deployable unit. It wraps one or more containers."


def check(text: str, language: Language = Language.EN, max_words: int = 100) -> Verdict:
    return check_deterministic(text, language=language, max_words=max_words)


def test_a_clean_general_answer_passes_the_deterministic_checks() -> None:
    assert check(GOOD) == Verdict(True)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("   ", REASON_EMPTY),
        ("word " * 400, REASON_TOO_LONG),
        ("See [E1] for details about pods.", REASON_LEAK),
        ("Use <<<BEGIN UNTRUSTED:question>>> markers", REASON_LEAK),
        ("Write to someone@example.com for help with pods.", REASON_PII),
        ("Call +1 415 555 0100 for pods.", REASON_PII),
        ("I have worked on Kubernetes clusters for years.", REASON_CANDIDATE_CLAIM),
        ("In my previous role I deployed pods daily.", REASON_CANDIDATE_CLAIM),
        ("I have 7 years of experience with containers.", REASON_CANDIDATE_CLAIM),
        ("At my current company we run pods.", REASON_CANDIDATE_CLAIM),
    ],
)
def test_bad_general_answers_are_rejected_with_a_fixed_reason(text: str, reason: str) -> None:
    verdict = check(text)
    assert not verdict.ok and verdict.reason == reason
    assert reviewer_note(reason)  # every failure has fixed, content-free feedback for the retry


def test_explaining_a_topic_in_the_first_person_plural_or_conditional_is_fine() -> None:
    assert check("I would start by checking the pod events. We usually look at logs first.").ok


def test_the_language_must_match_the_session() -> None:
    assert not check("यह एक पॉड है", Language.EN).ok
    assert check("यह एक पॉड है जो कंटेनर चलाता है", Language.HI).ok
    verdict = check("This answer is in English only", Language.HI)
    assert not verdict.ok and verdict.reason == REASON_LANGUAGE
    assert check("ఇది కంటైనర్లను నడిపే పాడ్", Language.TE).ok
    assert not check("English only", Language.TE).ok


def test_the_judge_reply_is_parsed_defensively() -> None:
    assert parse_judge_reply('{"verdict": "pass", "reason": "ok"}') == Verdict(True, "ok", True)
    failed = parse_judge_reply('Sure! {"verdict": "fail", "reason": "candidate_claim"} done')
    assert failed == Verdict(False, REASON_CANDIDATE_CLAIM, True)
    assert parse_judge_reply('{"verdict": "fail", "reason": "weird"}') == Verdict(
        False, "off_topic", True
    )
    for junk in ("", "no json here", "{broken", '["pass"]', '{"verdict": "maybe"}'):
        assert parse_judge_reply(junk) is None


class FakeRouter:
    """Stands in for `LLMRouter.complete`."""

    def __init__(
        self, reply: str = "", *, delay: float = 0.0, error: Exception | None = None
    ) -> None:
        self.reply, self.delay, self.error = reply, delay, error
        self.calls: list[list[Message]] = []

    async def complete(
        self, messages: list[Message], config: GenerationConfig, **kwargs: Any
    ) -> Completion:
        self.calls.append(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return Completion(self.reply, None, "fake", "fake-model")


def judge(router: FakeRouter, timeout: float = 0.5) -> Verdict:
    return asyncio.run(
        judge_answer(
            router,  # type: ignore[arg-type]
            None,
            "what is a pod",
            GOOD,
            timeout_seconds=timeout,
            user_id=uuid.uuid4(),
        )
    )


def test_the_judge_passes_or_fails_an_answer() -> None:
    assert judge(FakeRouter('{"verdict": "pass", "reason": "ok"}')) == Verdict(True, "ok", True)
    assert not judge(FakeRouter('{"verdict": "fail", "reason": "incorrect"}')).ok


def test_the_judge_sees_only_the_question_and_the_draft_never_a_document() -> None:
    router = FakeRouter('{"verdict": "pass", "reason": "ok"}')
    judge(router)
    system, user = router.calls[0]
    assert "NO facts about the candidate" in system.content
    assert "<<<BEGIN UNTRUSTED:question>>>" in user.content
    assert "<<<BEGIN UNTRUSTED:draft_answer>>>" in user.content


def test_a_slow_or_failing_or_unreadable_judge_is_never_a_silent_pass_and_never_a_crash() -> None:
    for router in (
        FakeRouter('{"verdict": "pass"}', delay=0.3),  # too slow for the 0.05 s cap below
        FakeRouter(error=Unavailable()),
        FakeRouter(error=RuntimeError("boom")),
        FakeRouter("not json"),
    ):
        verdict = judge(router, timeout=0.05)
        assert verdict.ok and not verdict.judged  # allowed through, but never marked as judged


def test_the_rewrite_reply_becomes_content_terms() -> None:
    reply = '{"query": "EWM enhancements", "keywords": ["Receiving Excess Qty", "HHT"]}'
    assert parse_rewrite_reply(reply) == ["ewm", "enhancement", "receiving", "excess", "qty", "hht"]
    for junk in ("", "x", "{broken", "[1]", '{"query": 5}'):
        assert parse_rewrite_reply(junk) == []


def test_the_rewrite_sends_only_the_question_and_never_breaks_the_answer() -> None:
    router = FakeRouter('{"query": "custom development", "keywords": ["abap"]}')
    terms = asyncio.run(
        rewrite_question(
            router,  # type: ignore[arg-type]
            None,
            "did you write custom code yourself",
            timeout_seconds=0.5,
            user_id=uuid.uuid4(),
        )
    )
    assert terms == ["custom", "development", "abap"]
    sent = " ".join(m.content for m in router.calls[0])
    assert "did you write custom code yourself" in sent
    for bad in (FakeRouter(error=Unavailable()), FakeRouter("x", delay=0.3)):
        assert (
            asyncio.run(
                rewrite_question(
                    bad,  # type: ignore[arg-type]
                    None,
                    "q",
                    timeout_seconds=0.05,
                    user_id=uuid.uuid4(),
                )
            )
            == []
        )
