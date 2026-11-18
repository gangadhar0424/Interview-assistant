# ruff: noqa: F811  (pytest fixtures are imported and then used as test arguments)
"""The answer pipeline end to end (D-66): router, exact text, general answers, cache, isolation.

Real PostgreSQL with pgvector and the deterministic fake embedder; fake providers only. The fake
provider's reply hook plays the writer, the judge and the question rewriter.
"""

import json
from collections.abc import Sequence
from typing import Any

import pytest

from app.ai.types import Message
from app.qa.cache import AnswerCache
from app.qa.events import (
    AnswerDelta,
    AnswerDone,
    AnswerFailed,
    AnswerStarted,
    QaEvent,
    RetrievalDone,
)
from app.qa.source import source_payload
from tests.conftest import RunSql
from tests.qa_fakes import RecordingProvider
from tests.test_ingestion_jobs import Env, env  # noqa: F401  (fixture reused)
from tests.test_qa_service import (  # noqa: F401  (scenario fixtures reused as-is)
    BOB_CANARY,
    ISOLATION_SETTINGS,
    RESUME_CANARY,
    SELECTED_CANARY,
    UNSELECTED_CANARY,
    Scenario,
    alice,
    answers,
    bob,
    ingest,
    kinds,
    make_router,
    make_session,
    scenario,
    scenario_full,
    user_id,
)

ROUTER_ON = ISOLATION_SETTINGS.model_copy(
    update={
        "qa_router_mode": "on",
        "qa_retrieval_mode": "hybrid",
        "qa_validate_general": "on",
        "qa_judge_timeout_seconds": 5.0,
        "qa_rewrite_timeout_seconds": 5.0,
    }
)
GENERAL_TEXT = (
    "A hash map stores key value pairs. It applies a hash function to the key to pick a bucket, "
    "and resolves collisions by chaining or probing."
)
JUDGE_PASS = json.dumps({"verdict": "pass", "reason": "ok"})
JUDGE_FAIL = json.dumps({"verdict": "fail", "reason": "incorrect"})
VALIDATED = source_payload("general", [{"validated": True}])
NOT_VALIDATED = source_payload("general", [{"validated": False}])
NO_REFS = source_payload("general", [])


class Script:
    """A scripted model; it tells the writer, judge and rewriter apart by system prompt."""

    def __init__(
        self,
        *,
        writer: Sequence[str] = (GENERAL_TEXT,),
        judge: Sequence[str] = (JUDGE_PASS,),
        rewrite: str = '{"query": "billing migration", "keywords": ["billing"]}',
    ) -> None:
        self.writer, self.judge, self.rewrite_reply = list(writer), list(judge), rewrite
        self.writes = self.judged = self.rewrites = 0

    def __call__(self, messages: Sequence[Message]) -> str:
        system = messages[0].content
        if "review a draft answer" in system:
            reply = self.judge[min(self.judged, len(self.judge) - 1)]
            self.judged += 1
            return reply
        if "short search query" in system:
            self.rewrites += 1
            return self.rewrite_reply
        reply = self.writer[min(self.writes, len(self.writer) - 1)]
        self.writes += 1
        return reply


def provider_for(script: Script) -> RecordingProvider:
    return RecordingProvider(reply_fn=script)


def text_of(events: Sequence[QaEvent]) -> str:
    return "".join(e.text for e in events if isinstance(e, AnswerDelta))


def done_of(events: Sequence[QaEvent]) -> AnswerDone:
    done = events[-1]
    assert isinstance(done, AnswerDone), kinds(list(events))
    return done


def ask(scenario: Scenario, provider: RecordingProvider, question: str, **kw: Any) -> list[QaEvent]:
    kw.setdefault("settings", ROUTER_ON)
    return scenario.ask(make_router([provider]), question, **kw)


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, name: str) -> str | None:
        return self.data.get(name)

    async def set(self, name: str, value: str, *, ex: int) -> bool:
        self.data[name] = value
        return True


# ------------------------------------------------------------------ exact text


def test_a_question_matching_a_selected_document_returns_its_exact_text_without_a_model(
    scenario_full: Scenario, run_sql: RunSql
) -> None:
    provider = provider_for(Script())
    events = ask(scenario_full, provider, f"{SELECTED_CANARY} led the billing migration")
    assert kinds(events)[:3] == ["QuestionCreated", "RetrievalDone", "AnswerStarted"]
    started = next(e for e in events if isinstance(e, AnswerStarted))
    assert started.provider == "extractive" and started.model == "document"
    assert text_of(events) == f"{SELECTED_CANARY} led the billing migration."  # verbatim
    assert provider.recorded == [] and provider.calls == 0  # nothing reached any provider
    done = done_of(events)
    assert done.source is not None and done.source["kind"] == "document"
    ref = done.source["refs"][0]
    assert ref["source_type"] == "document" and ref["name"] == "notes.txt"
    stored = answers(run_sql)[0]
    assert stored["status"] == "complete" and stored["text"] == text_of(events)
    assert stored["source_kind"] == "document" and stored["provider"] == "extractive"
    assert UNSELECTED_CANARY not in stored["text"] and BOB_CANARY not in stored["text"]


def test_a_question_matching_the_resume_returns_the_resume_text_exactly(
    scenario: Scenario, run_sql: RunSql
) -> None:
    provider = provider_for(Script())
    events = ask(scenario, provider, f"{RESUME_CANARY} Python backend engineer")
    assert f"{RESUME_CANARY} Python backend engineer" in text_of(events)
    assert provider.calls == 0
    assert done_of(events).source["kind"] == "resume"  # type: ignore[index]
    assert answers(run_sql)[0]["source_kind"] == "resume"


def test_tell_me_about_yourself_opens_the_resume_without_a_model(scenario: Scenario) -> None:
    provider = provider_for(Script())
    events = ask(scenario, provider, "Tell me about yourself")
    assert text_of(events).startswith(RESUME_CANARY)
    assert provider.calls == 0
    assert done_of(events).source["kind"] == "resume"  # type: ignore[index]


def test_a_retrieved_exact_answer_is_fast_and_reports_zero_provider_work(
    scenario: Scenario,
) -> None:
    provider = provider_for(Script())
    events = ask(scenario, provider, "tell me about yourself")
    done = done_of(events)
    assert done.input_tokens is None and done.output_tokens is None
    assert done.first_token_ms <= done.total_ms


# --------------------------------------------------------------- general answers


def test_a_question_in_neither_source_gets_a_validated_general_answer(
    scenario_full: Scenario, run_sql: RunSql
) -> None:
    script = Script()
    provider = provider_for(script)
    events = ask(scenario_full, provider, "Explain how a hash map works internally")
    assert text_of(events) == GENERAL_TEXT
    done = done_of(events)
    assert done.source == VALIDATED
    (call,) = provider.recorded  # the writer only; the judge uses `complete`
    assert call.block("candidate_material") == "(none provided)"  # no candidate facts reach it
    for secret in (RESUME_CANARY, SELECTED_CANARY, UNSELECTED_CANARY, BOB_CANARY):
        assert secret not in call.user and secret not in call.system
    assert script.judged == 1
    stored = answers(run_sql)[0]
    assert stored["source_kind"] == "general" and stored["retrieved_chunk_ids"] == []


def test_a_general_answer_with_a_candidate_claim_is_regenerated_once(scenario: Scenario) -> None:
    script = Script(writer=["I have worked on hash maps for ten years.", GENERAL_TEXT])
    provider = provider_for(script)
    events = ask(scenario, provider, "Explain how a hash map works internally")
    assert text_of(events) == GENERAL_TEXT  # only the good answer was ever shown
    assert script.writes == 2
    assert "Reviewer note" in provider.recorded[1].block("question")
    assert not any(isinstance(e, AnswerFailed) for e in events)


def test_a_general_answer_that_fails_twice_is_never_shown(
    scenario: Scenario, run_sql: RunSql
) -> None:
    script = Script(writer=["I have worked on hash maps for ten years."])
    provider = provider_for(script)
    events = ask(scenario, provider, "Explain how a hash map works internally")
    assert isinstance(events[-1], AnswerFailed) and events[-1].code == "answer_not_validated"
    assert text_of(events) == ""  # not one word of a rejected answer reached the user
    assert not any(isinstance(e, AnswerStarted) for e in events)
    assert script.writes == 2 and script.judged == 0  # rules failed it before the judge ran
    stored = answers(run_sql)[0]
    assert stored["status"] == "error" and stored["error_code"] == "answer_not_validated"
    assert stored["text"] == ""  # the rejected draft is not kept anywhere


def test_the_judge_can_reject_an_answer_and_a_second_try_can_pass(scenario: Scenario) -> None:
    script = Script(judge=[JUDGE_FAIL, JUDGE_PASS])
    provider = provider_for(script)
    events = ask(scenario, provider, "Explain how a hash map works internally")
    assert text_of(events) == GENERAL_TEXT and script.writes == 2 and script.judged == 2
    assert done_of(events).source == VALIDATED


def test_an_unavailable_judge_labels_the_answer_not_fully_validated(scenario: Scenario) -> None:
    script = Script(judge=["not json at all"])
    provider = provider_for(script)
    events = ask(scenario, provider, "Explain how a hash map works internally")
    assert text_of(events) == GENERAL_TEXT
    assert done_of(events).source == NOT_VALIDATED


def test_with_validation_off_a_general_answer_streams_straight_through(
    scenario: Scenario,
) -> None:
    script = Script()
    settings = ROUTER_ON.model_copy(update={"qa_validate_general": "off"})
    events = ask(scenario, provider_for(script), "Explain how a hash map works", settings=settings)
    assert text_of(events) == GENERAL_TEXT and script.judged == 0
    assert done_of(events).source == NO_REFS


def test_a_repeated_general_question_is_served_from_the_cache(scenario: Scenario) -> None:
    redis = FakeRedis()
    cache = AnswerCache(redis, 300)
    first = Script()
    one = ask(scenario, provider_for(first), "Explain how a hash map works", cache=cache)
    second = Script(writer=["SHOULD NOT BE USED"])
    provider = provider_for(second)
    two = ask(scenario, provider, "  explain HOW a hash map works?  ", cache=cache)
    assert text_of(one) == text_of(two) == GENERAL_TEXT
    assert provider.calls == 0 and second.judged == 0  # the second answer cost nothing
    assert done_of(two).source == VALIDATED
    assert len(redis.data) == 1


def test_the_cache_is_per_user_session_and_preferences(
    scenario: Scenario, client: Any, run_sql: RunSql
) -> None:
    redis = FakeRedis()
    cache = AnswerCache(redis, 300)
    ask(scenario, provider_for(Script()), "Explain how a hash map works", cache=cache)
    other = Script(writer=["A different answer about hash maps and buckets and chaining."])
    events = ask(
        scenario,
        provider_for(other),
        "Explain how a hash map works",
        cache=cache,
        override="coding",  # a different question type is a different preference digest
    )
    assert other.writes == 1 and text_of(events).startswith("A different answer")
    assert len(redis.data) == 2


# --------------------------------------------------------- routing details


def test_a_vague_question_triggers_one_rewrite_and_then_an_exact_answer(
    scenario: Scenario,
) -> None:
    script = Script(rewrite='{"query": "billing migration", "keywords": ["billing"]}')
    provider = provider_for(script)
    events = ask(scenario, provider, "billing migration owner")
    assert script.rewrites == 1
    assert text_of(events) == f"{SELECTED_CANARY} led the billing migration."
    assert done_of(events).source["kind"] == "document"  # type: ignore[index]
    assert provider.recorded == []  # the rewrite is a short call; no answer was generated


def test_a_failed_rewrite_degrades_to_the_related_passages(scenario: Scenario) -> None:
    script = Script(rewrite="not json")
    provider = provider_for(script)
    events = ask(scenario, provider, "billing migration owner")
    assert done_of(events).source["kind"] == "synthesis"  # type: ignore[index]
    (call,) = provider.recorded
    assert SELECTED_CANARY in call.block("candidate_material")


def test_the_router_in_shadow_mode_decides_but_the_answer_follows_the_old_path(
    scenario: Scenario, run_sql: RunSql
) -> None:
    script = Script()
    provider = provider_for(script)
    settings = ROUTER_ON.model_copy(update={"qa_router_mode": "shadow"})
    events = ask(
        scenario, provider, f"{SELECTED_CANARY} led the billing migration", settings=settings
    )
    assert len(provider.recorded) == 1  # the model answered, as before the router existed
    assert done_of(events).source is None
    assert answers(run_sql)[0]["source_kind"] is None


def test_exact_text_in_another_language_is_translated_not_returned(
    client: Any,
    env: Env,
    run_sql: RunSql,
    test_db_url: str,
    alice: dict[str, str],
    bob: dict[str, str],  # noqa: F811
) -> None:
    scenario_hi = Scenario(client, env, run_sql, test_db_url, alice, bob, language="hi")
    provider = provider_for(Script(writer=["यह एक उत्तर है जो हिंदी में लिखा गया है और पर्याप्त लंबा है।"]))
    events = ask(scenario_hi, provider, f"{SELECTED_CANARY} led the billing migration")
    assert done_of(events).source["kind"] == "synthesis"  # type: ignore[index]
    assert len(provider.recorded) == 1


def test_other_users_and_unselected_files_never_reach_an_exact_answer(
    scenario_full: Scenario,
) -> None:
    for question in (f"{UNSELECTED_CANARY} private notes", f"{BOB_CANARY} Bob document"):
        provider = provider_for(Script())
        events = ask(scenario_full, provider, question)
        shown = text_of(events)
        assert UNSELECTED_CANARY not in shown and BOB_CANARY not in shown
        for call in provider.recorded:  # the user's own words may recur; their files must not
            material = call.block("candidate_material")
            assert UNSELECTED_CANARY not in material and BOB_CANARY not in material
        assert done_of(events).source["kind"] in ("general", "synthesis", "resume", "document")  # type: ignore[index]


def test_the_retrieval_event_reports_the_passages_used(scenario: Scenario) -> None:
    events = ask(scenario, provider_for(Script()), f"{SELECTED_CANARY} led the billing migration")
    retrieval = next(e for e in events if isinstance(e, RetrievalDone))
    assert retrieval.chunk_count == 1 and retrieval.ms >= 0


@pytest.mark.parametrize("mode", ["dense", "hybrid"])
def test_with_the_router_off_the_answer_still_comes_from_the_model(
    scenario: Scenario, mode: str
) -> None:
    provider = provider_for(Script())
    settings = ISOLATION_SETTINGS.model_copy(update={"qa_retrieval_mode": mode})
    events = ask(scenario, provider, "Tell me about your experience", settings=settings)
    assert len(provider.recorded) == 1 and done_of(events).source is None


# ------------------------------------------------------- named sections (D-66)

LONG_END = "END-OF-ENHANCEMENT-ONE"
SECTIONS = chr(10) * 2


def sectioned_document() -> str:
    long_body = " ".join(
        f"Step {i} of the receiving flow explains what the operator does at the dock."
        for i in range(70)
    )
    return SECTIONS.join(
        [
            "Enhancement1: Receiving Excess Qty",
            long_body + " " + LONG_END + ".",
            "Enhancement2: Putaway by Cart",
            "Totes are assigned to a cart and put away shelf by shelf.",
        ]
    )


def sectioned_scenario(
    client: Any, env: Env, run_sql: RunSql, alice: dict[str, str]
) -> tuple[str, Any]:
    resume = ingest(client, env, alice, "resumes", "Python engineer. Built backend services.")
    doc = ingest(client, env, alice, "documents", sectioned_document())
    return make_session(client, alice, resume, [doc]), user_id(run_sql, "alice@example.com")


def ask_section(
    test_db_url: str,
    sid: str,
    uid: Any,
    provider: RecordingProvider,
    question: str,
    detail: str | None = None,
) -> list[QaEvent]:
    from tests.test_qa_service import run_ask

    return run_ask(
        test_db_url, make_router([provider]), uid, sid, question, settings=ROUTER_ON, detail=detail
    )


def test_a_named_section_longer_than_the_length_setting_is_answered_in_full(
    client: Any, env: Env, run_sql: RunSql, test_db_url: str, alice: dict[str, str]
) -> None:
    sid, uid = sectioned_scenario(client, env, run_sql, alice)
    script = Script(
        writer=["Enhancement 1 lets the operator receive excess stock on the handheld."]
    )
    provider = provider_for(script)
    events = ask_section(test_db_url, sid, uid, provider, "enhancement 1")
    (call,) = provider.recorded  # the model composed the answer ...
    material = call.block("candidate_material")
    assert "Receiving Excess Qty" in material and LONG_END in material  # ... from the WHOLE section
    assert "Putaway by Cart" not in material  # and only that section
    assert "Aim for 250 to 450 words" in call.system  # a wider budget than the session's own
    assert call.config.max_output_tokens >= 450
    done = done_of(events)
    assert done.source is not None and done.source["kind"] == "synthesis"
    assert done.source["refs"][0]["heading"] == "Enhancement1: Receiving Excess Qty"
    assert done.provider == "fake"


def test_a_short_named_section_is_still_returned_as_exact_text(
    client: Any, env: Env, run_sql: RunSql, test_db_url: str, alice: dict[str, str]
) -> None:
    sid, uid = sectioned_scenario(client, env, run_sql, alice)
    provider = provider_for(Script())
    events = ask_section(test_db_url, sid, uid, provider, "enhancement 2")
    assert provider.recorded == []  # no model call
    shown = text_of(events)
    assert shown.startswith("Enhancement2: Putaway by Cart")
    assert "Totes are assigned to a cart and put away shelf by shelf." in shown
    assert done_of(events).source["kind"] == "document"  # type: ignore[index]


def test_enhancement_one_never_returns_a_different_enhancement(
    client: Any, env: Env, run_sql: RunSql, test_db_url: str, alice: dict[str, str]
) -> None:
    sid, uid = sectioned_scenario(client, env, run_sql, alice)
    provider = provider_for(Script())
    events = ask_section(test_db_url, sid, uid, provider, "enhancement 1")
    assert "Putaway by Cart" not in text_of(events)
    for call in provider.recorded:
        assert "Putaway by Cart" not in call.block("candidate_material")


# ------------------------------------------------------------- role guard (D-66)

CAVEAT = "Confirm before using. I have not written the code myself. I worked with the ABAP team."


def guarded_document() -> str:
    return SECTIONS.join([sectioned_document(), "Custom development", CAVEAT])


def test_a_section_answer_also_receives_the_passage_that_limits_the_candidates_role(
    client: Any, env: Env, run_sql: RunSql, test_db_url: str, alice: dict[str, str]
) -> None:
    resume = ingest(client, env, alice, "resumes", "Python engineer. Built backend services.")
    doc = ingest(client, env, alice, "documents", guarded_document())
    sid = make_session(client, alice, resume, [doc])
    uid = user_id(run_sql, "alice@example.com")
    provider = provider_for(Script(writer=["We built it with the ABAP team."]))
    events = ask_section(test_db_url, sid, uid, provider, "enhancement 1")
    (call,) = provider.recorded
    material = call.block("candidate_material")
    assert LONG_END in material  # the section the user asked about
    assert "I have not written the code myself" in material  # and the limit on the candidate's role
    assert "obey it strictly" in call.system  # which the prompt tells the model to respect
    assert done_of(events).source["kind"] == "synthesis"  # type: ignore[index]


def test_the_role_guard_never_reads_another_users_files(
    client: Any,
    env: Env,
    run_sql: RunSql,
    test_db_url: str,
    alice: dict[str, str],
    bob: dict[str, str],
) -> None:
    resume = ingest(client, env, alice, "resumes", "Python engineer. Built backend services.")
    doc = ingest(client, env, alice, "documents", sectioned_document())
    ingest(
        client,
        env,
        bob,
        "documents",
        "Custom development" + SECTIONS + "BOB-CAVEAT I have not written the code.",
    )
    sid = make_session(client, alice, resume, [doc])
    uid = user_id(run_sql, "alice@example.com")
    provider = provider_for(Script())
    ask_section(test_db_url, sid, uid, provider, "enhancement 1")
    for call in provider.recorded:
        assert "BOB-CAVEAT" not in call.user


def test_exact_text_answers_do_not_need_the_guard(
    client: Any, env: Env, run_sql: RunSql, test_db_url: str, alice: dict[str, str]
) -> None:
    resume = ingest(client, env, alice, "resumes", "Python engineer. Built backend services.")
    doc = ingest(client, env, alice, "documents", guarded_document())
    sid = make_session(client, alice, resume, [doc])
    uid = user_id(run_sql, "alice@example.com")
    provider = provider_for(Script())
    events = ask_section(test_db_url, sid, uid, provider, "enhancement 2")
    assert provider.recorded == []  # verbatim text of the user's own file: nothing to guard
    assert text_of(events).startswith("Enhancement2: Putaway by Cart")


# ------------------------------------------------- answer detail: full / summary (D-66)


def test_a_summary_request_uses_the_short_length_band_for_a_named_section(
    client: Any, env: Env, run_sql: RunSql, test_db_url: str, alice: dict[str, str]
) -> None:
    sid, uid = sectioned_scenario(client, env, run_sql, alice)
    provider = provider_for(Script(writer=["It lets the operator receive excess stock."]))
    events = ask_section(test_db_url, sid, uid, provider, "enhancement 1", detail="summary")
    (call,) = provider.recorded
    assert LONG_END in call.block("candidate_material")  # still written from the whole section
    assert "Aim for 60 to 90 words" in call.system  # but as a short spoken answer
    assert call.config.max_output_tokens < 450
    assert done_of(events).source["kind"] == "synthesis"  # type: ignore[index]


def test_full_detail_is_the_default_and_uses_the_wide_budget(
    client: Any, env: Env, run_sql: RunSql, test_db_url: str, alice: dict[str, str]
) -> None:
    sid, uid = sectioned_scenario(client, env, run_sql, alice)
    for detail in (None, "full"):
        provider = provider_for(Script())
        ask_section(test_db_url, sid, uid, provider, "enhancement 1", detail=detail)
        assert "Aim for 250 to 450 words" in provider.recorded[0].system


def test_a_summary_also_shortens_a_general_answer(scenario: Scenario) -> None:
    provider = provider_for(Script())
    events = ask(scenario, provider, "Explain how a hash map works", detail="summary")
    assert text_of(events) == GENERAL_TEXT
    assert "Aim for 60 to 90 words" in provider.recorded[0].system


def test_a_short_section_stays_exact_text_in_summary_mode(
    client: Any, env: Env, run_sql: RunSql, test_db_url: str, alice: dict[str, str]
) -> None:
    sid, uid = sectioned_scenario(client, env, run_sql, alice)
    provider = provider_for(Script())
    events = ask_section(test_db_url, sid, uid, provider, "enhancement 2", detail="summary")
    assert provider.recorded == []
    assert text_of(events).startswith("Enhancement2: Putaway by Cart")


def test_the_detail_option_is_validated_everywhere_it_is_accepted(scenario: Scenario) -> None:
    import pytest
    from pydantic import ValidationError

    from app.core.errors import AppError
    from app.qa.schemas import QuestionCreate
    from app.realtime.schemas import QuestionSubmitPayload

    for model in (QuestionCreate, QuestionSubmitPayload):
        assert model(text="hello", detail="summary").detail == "summary"  # type: ignore[call-arg]
        assert model(text="hello").detail is None  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            model(text="hello", detail="everything")  # type: ignore[call-arg]
    with pytest.raises(AppError) as error:
        ask(scenario, provider_for(Script()), "hello", detail="everything")
    assert error.value.status_code == 422
