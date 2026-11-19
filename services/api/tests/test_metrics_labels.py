"""M-2: the performance metrics carry timing only, with bounded labels and never content."""

import asyncio
import uuid

import pytest
from prometheus_client import REGISTRY, generate_latest

from app.ai.fake_stt import FakeSTTProvider, fake_stt_spec
from app.ai.stt_router import RegisteredStt, SttRouter
from app.audio.service import Transcriber, VoiceSelection
from app.core import metrics
from app.core.config import Settings
from tests.wav_helpers import make_wav

ALLOWED_LABELS = {"method", "route", "status", "provider", "result", "reason", "mode", "stage"}
FORBIDDEN_LABEL_PARTS = ("user", "session", "question", "text", "transcript", "email", "token")
SECRET = "SECRET-TRANSCRIPT-TEXT-91f3"


def _interview_collectors() -> list[object]:
    seen: dict[int, object] = {}
    for collector, names in list(REGISTRY._collector_to_names.items()):  # noqa: SLF001
        if any(name.startswith("interview_") for name in names):
            seen[id(collector)] = collector
    return list(seen.values())


def test_every_interview_metric_uses_only_allowed_bounded_labels() -> None:
    collectors = _interview_collectors()
    assert collectors, "no interview_* metrics registered"
    for collector in collectors:
        labels = set(getattr(collector, "_labelnames", ()))
        assert labels <= ALLOWED_LABELS, (collector, labels - ALLOWED_LABELS)
        for label in labels:
            assert not any(part in label for part in FORBIDDEN_LABEL_PARTS), label
            assert label != "id" and not label.endswith("_id"), label


def test_the_new_performance_metrics_are_registered() -> None:
    names = {
        "interview_stt_request_seconds",
        "interview_vision_request_seconds",
        "interview_audio_validate_seconds",
        "interview_screenshot_validate_seconds",
        "interview_question_detection_seconds",
        "interview_answer_first_token_seconds",
        "interview_answer_total_seconds",
    }
    exposed = generate_latest(REGISTRY).decode()
    for name in names:
        assert f"# TYPE {name} histogram" in exposed


def test_unknown_answer_results_are_folded_so_labels_cannot_grow() -> None:
    before = generate_latest(REGISTRY).decode()
    metrics.record_answer_timing("some-new-status-" + uuid.uuid4().hex, "none", None, 1000)
    after = generate_latest(REGISTRY).decode()
    assert "some-new-status" not in after
    assert 'result="error"' in after
    assert before != after or 'result="error"' in before


def test_missing_timings_are_skipped_not_recorded_as_zero() -> None:
    def total_count() -> float:
        return sum(
            sample.value
            for family in REGISTRY.collect()
            if family.name == "interview_answer_first_token_seconds"
            for sample in family.samples
            if sample.name.endswith("_count")
        )

    start = total_count()
    metrics.record_answer_timing("complete", "fake", None, None)
    assert total_count() == start
    metrics.record_answer_timing("complete", "fake", 120, 900)
    assert total_count() == start + 1


def test_a_failed_answer_still_records_its_total_but_never_its_text() -> None:
    metrics.record_answer_timing("error", None, None, 250)
    exposed = generate_latest(REGISTRY).decode()
    assert 'interview_answer_total_seconds_count{result="error"}' in exposed
    assert SECRET not in exposed


def test_transcription_records_timing_without_content_or_identifiers() -> None:
    provider = FakeSTTProvider("fake", reply=SECRET + " is that right?")
    router = SttRouter([RegisteredStt(provider, fake_stt_spec("fake"))])
    transcriber = Transcriber(
        sessionmaker=None,  # type: ignore[arg-type]  # transcribe() never touches the database
        settings=Settings(_env_file=None),
        stt=router,
    )
    user_id, session_id = uuid.uuid4(), uuid.uuid4()
    result = asyncio.run(
        transcriber.transcribe(user_id, session_id, make_wav(1), VoiceSelection("fake", None, "en"))
    )
    assert SECRET in result.text  # the API response carries it; metrics must not
    exposed = generate_latest(REGISTRY).decode()
    assert SECRET not in exposed
    assert str(user_id) not in exposed and str(session_id) not in exposed
    assert 'interview_stt_request_seconds_count{provider="fake"}' in exposed
    assert "interview_question_detection_seconds_count" in exposed
    assert "interview_audio_validate_seconds_count" in exposed


def test_cancellation_does_not_record_a_provider_duration() -> None:
    provider = FakeSTTProvider("fake", delay=5.0)
    router = SttRouter([RegisteredStt(provider, fake_stt_spec("fake"))])
    transcriber = Transcriber(
        sessionmaker=None,  # type: ignore[arg-type]
        settings=Settings(_env_file=None),
        stt=router,
    )

    def count() -> float:
        return sum(
            s.value
            for f in REGISTRY.collect()
            if f.name == "interview_stt_request_seconds"
            for s in f.samples
            if s.name.endswith("_count") and s.labels.get("provider") == "fake"
        )

    async def run() -> None:
        task = asyncio.create_task(
            transcriber.transcribe(
                uuid.uuid4(), uuid.uuid4(), make_wav(1), VoiceSelection("fake", None, "en")
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    before = count()
    asyncio.run(run())
    assert count() == before
