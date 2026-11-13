"""Phase 12 API: POST /sessions/{id}/transcribe (DB-backed) and `input_source = audio`.

The speech-to-text provider is always the deterministic fake. Production wiring leaves it disabled
(`STT_ENABLED_PROVIDERS` empty), so most tests install a transcriber with a scripted fake.
"""

import asyncio
import base64
import hashlib
import logging
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from app.ai.errors import ContentFiltered, RateLimited, Timeout, Unavailable
from app.ai.fake_stt import FakeSTTProvider, fake_stt_spec
from app.ai.stt import AudioInput
from app.ai.stt_router import RegisteredStt, SttRouter, SttRouterConfig
from app.ai.usage import MemoryUsageSink
from app.audio.limits import AudioLimits
from app.audio.router import _await_or_cancel
from app.audio.service import Transcriber
from app.audio.validation import canonical_wav, validate_wav
from app.core.config import get_settings
from tests.conftest import RunSql
from tests.helpers import register
from tests.qa_fakes import RecordingProvider
from tests.test_qa_api import SSE, ask, count, install
from tests.test_qa_service import quick_session
from tests.wav_helpers import make_wav, pcm_tone, wav_of_size

SENTINEL = "SPOKEN-TEXT-SENTINEL-6643"
AUDIO_SENTINEL = b"AUDIO-BYTES-SENTINEL-1189"


def url(session_id: str) -> str:
    return f"/api/v1/sessions/{session_id}/transcribe"


def install_stt(
    client: TestClient,
    providers: list[FakeSTTProvider] | None = None,
    *,
    limits: AudioLimits | None = None,
    config: SttRouterConfig | None = None,
) -> tuple[list[FakeSTTProvider], AudioLimits, MemoryUsageSink]:
    """Swap in a transcriber with fake providers (production wiring leaves audio off)."""
    app: Any = client.app
    fakes = providers if providers is not None else [FakeSTTProvider()]
    sink = MemoryUsageSink()
    router = SttRouter(
        [RegisteredStt(p, fake_stt_spec(p.provider_id)) for p in fakes],
        config=config,
        usage_sink=sink,
    )
    app.state.transcriber = Transcriber(app.state.sessionmaker, get_settings(), router)
    app.state.audio_limits = limits or AudioLimits(1000, 1000)
    return fakes, app.state.audio_limits, sink


def post(
    client: TestClient,
    headers: dict[str, str],
    session_id: str,
    body: bytes | Iterator[bytes],
    *,
    content_type: str | None = "audio/wav",
) -> Response:
    request_headers = {**headers}
    if content_type is not None:
        request_headers["Content-Type"] = content_type
    response: Response = client.post(url(session_id), headers=request_headers, content=body)
    return response


def uid(client: TestClient, headers: dict[str, str]) -> Any:
    return uuid.UUID(client.get("/api/v1/auth/me", headers=headers).json()["id"])


@pytest.fixture
def alice(client: TestClient) -> dict[str, str]:
    return register(client, "alice@example.com")


@pytest.fixture
def bob(client: TestClient) -> dict[str, str]:
    return register(client, "bob@example.com")


@pytest.fixture
def ready(client: TestClient, run_sql: RunSql, alice: dict[str, str]) -> str:
    sid, _, _ = quick_session(client, run_sql, alice)
    return sid


# ------------------------------------------------------------------ availability and auth


def test_audio_input_is_disabled_when_no_voice_provider_is_enabled(
    make_client: Callable[..., TestClient], run_sql: RunSql
) -> None:
    client = make_client(STT_ENABLED_PROVIDERS="")  # the production default
    alice = register(client, "alice@example.com")
    # The enabled check comes before any session lookup, so any id shows the refusal.
    response = post(client, alice, "00000000-0000-4000-8000-000000000001", make_wav())
    assert response.status_code == 503 and response.json()["error"]["code"] == "stt_unavailable"


def test_authentication_is_required(client: TestClient, ready: str) -> None:
    install_stt(client)
    assert post(client, {}, ready, make_wav()).status_code == 401
    assert post(client, {"Authorization": "Bearer nope"}, ready, make_wav()).status_code == 401


def test_a_valid_utterance_is_transcribed_and_flagged_by_the_fake_provider(
    client: TestClient, alice: dict[str, str], ready: str
) -> None:
    [fake], _, sink = install_stt(
        client, [FakeSTTProvider(script=["can you walk me through your resume", "okay thanks"])]
    )
    wav = make_wav(1.2)
    first = post(client, alice, ready, wav)
    assert first.status_code == 200, first.text
    body = first.json()
    assert set(body) == {
        "text",
        "truncated",
        "is_question",
        "confidence",
        "provider",
        "model",
        "latency_ms",
    }
    assert body["text"] == "can you walk me through your resume"
    assert body["is_question"] is True and body["confidence"] >= 0.6
    assert (body["provider"], body["model"], body["truncated"]) == ("fake", "fake-stt", False)
    canonical = validate_wav(wav, max_seconds=30).data
    assert fake.digests_seen == [
        hashlib.sha256(canonical).hexdigest()
    ]  # exactly the validated audio
    second = post(client, alice, ready, make_wav(0.8)).json()
    assert second["text"] == "okay thanks" and second["is_question"] is False
    assert sink.records and sink.records[0].ok


# ---------------------------------------------------------------------- request validation


def test_only_wav_content_type_is_accepted(
    client: TestClient, alice: dict[str, str], ready: str
) -> None:
    [fake], _, _ = install_stt(client)
    for content_type in (
        "audio/mpeg",
        "audio/webm",
        "multipart/form-data; boundary=x",
        "application/json",
        None,
    ):
        response = post(client, alice, ready, make_wav(), content_type=content_type)
        assert response.status_code == 415, content_type
        assert response.json()["error"]["code"] == "unsupported_audio_type"
    assert post(client, alice, ready, make_wav(), content_type="audio/x-wav").status_code == 200
    assert fake.calls == 1


def test_an_invalid_body_is_a_422_and_never_reaches_the_provider(
    client: TestClient, alice: dict[str, str], ready: str
) -> None:
    [fake], limits, _ = install_stt(client)
    good = make_wav()
    bad = [
        b"",
        b"hello",
        good[:50],
        good + b"trailing",
        make_wav(0.05),  # shorter than the minimum
        make_wav(31),  # longer than the maximum
        canonical_wav(b"\x00\x00" * 1000).replace(
            b"\x80>\x00\x00", b"D\xac\x00\x00", 1
        ),  # wrong rate
    ]
    for body in bad:
        response = post(client, alice, ready, body)
        assert response.status_code == 422, body[:16]
        assert response.json()["error"]["code"] == "invalid_audio"
    assert fake.calls == 0
    assert limits.active(uid(client, alice)) == 0  # every refusal released its slot


def test_an_oversized_body_is_refused_with_413_declared_and_streamed(
    make_client: Callable[..., TestClient], run_sql: RunSql
) -> None:
    client = make_client(AUDIO_MAX_BYTES="20000")
    alice = register(client, "alice@example.com")
    sid, _, _ = quick_session(client, run_sql, alice)
    [fake], _, _ = install_stt(client)
    big = wav_of_size(30_000)
    declared = post(client, alice, sid, big)
    assert declared.status_code == 413
    assert declared.json()["error"]["code"] in {"audio_too_large", "file_too_large"}

    def chunks() -> Iterator[bytes]:  # no Content-Length: counted while reading
        for start in range(0, len(big), 4096):
            yield big[start : start + 4096]

    assert post(client, alice, sid, chunks()).status_code == 413
    assert fake.calls == 0
    assert post(client, alice, sid, wav_of_size(15_000)).status_code == 200


# ------------------------------------------------------------- authorization and isolation


def test_another_users_or_unknown_session_is_an_indistinguishable_404(
    client: TestClient, alice: dict[str, str], bob: dict[str, str], ready: str
) -> None:
    [fake], _, _ = install_stt(client)
    foreign = post(client, bob, ready, make_wav())
    unknown = post(client, bob, "00000000-0000-4000-8000-000000000000", make_wav())
    assert (foreign.status_code, unknown.status_code) == (404, 404)
    assert foreign.json()["error"]["code"] == unknown.json()["error"]["code"] == "session_not_found"
    assert foreign.json()["error"]["message"] == unknown.json()["error"]["message"]
    assert fake.calls == 0
    assert post(client, bob, ready, b"garbage").status_code == 404
    assert post(client, alice, ready, make_wav()).status_code == 200


@pytest.mark.parametrize("state", ["draft", "active", "ended", "archived"])
def test_the_session_must_be_ready(
    client: TestClient, run_sql: RunSql, alice: dict[str, str], ready: str, state: str
) -> None:
    [fake], _, _ = install_stt(client)
    run_sql("UPDATE sessions SET status = :s WHERE id = :i", s=state, i=ready)
    response = post(client, alice, ready, make_wav())
    assert (response.status_code, response.json()["error"]["code"]) == (409, "session_not_ready")
    assert fake.calls == 0


# ------------------------------------------------------------------------------- limits


def test_rate_and_concurrency_limits_apply_per_user(
    client: TestClient, alice: dict[str, str], bob: dict[str, str], ready: str, run_sql: RunSql
) -> None:
    install_stt(client, limits=AudioLimits(2, 10))
    assert post(client, alice, ready, make_wav()).status_code == 200
    assert post(client, alice, ready, make_wav()).status_code == 200
    third = post(client, alice, ready, make_wav())
    assert third.status_code == 429 and third.json()["error"]["code"] == "rate_limited"
    assert int(third.headers["Retry-After"]) >= 1
    bob_sid, _, _ = quick_session(client, run_sql, bob)
    assert post(client, bob, bob_sid, make_wav()).status_code in {200, 404}

    _, limits, _ = install_stt(client, limits=AudioLimits(1000, 1))
    held = limits.admit(uid(client, alice))
    busy = post(client, alice, ready, make_wav())
    assert (busy.status_code, busy.json()["error"]["code"]) == (429, "too_many_transcriptions")
    held.release()
    assert post(client, alice, ready, make_wav()).status_code == 200


# ------------------------------------------------------------ text handling and failures


@pytest.mark.parametrize(
    ("reply", "expected", "truncated"),
    [
        ("  What is a mutex?\x00​  ", "What is a mutex?", False),
        ("", "", False),
        ("x" * 2000, "x" * 2000, False),
        ("y" * 2500, "y" * 2000, True),
    ],
)
def test_the_transcript_is_cleaned_and_capped(
    client: TestClient,
    alice: dict[str, str],
    ready: str,
    reply: str,
    expected: str,
    truncated: bool,
) -> None:
    install_stt(client, [FakeSTTProvider(reply=reply)])
    body = post(client, alice, ready, make_wav()).json()
    assert (body["text"], body["truncated"]) == (expected, truncated)
    if not expected:
        assert body["is_question"] is False and body["confidence"] == 0.0


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (Unavailable(), 503, "llm_unavailable"),
        (Timeout(), 504, "llm_timeout"),
        (RateLimited(), 429, "llm_rate_limited"),
        (ContentFiltered(), 422, "llm_content_filtered"),
    ],
)
def test_provider_failures_map_to_fixed_safe_errors(
    client: TestClient, alice: dict[str, str], ready: str, failure: Any, status: int, code: str
) -> None:
    _, limits, _ = install_stt(client, [FakeSTTProvider(failures=[failure])])
    response = post(client, alice, ready, make_wav())
    assert (response.status_code, response.json()["error"]["code"]) == (status, code)
    assert response.json()["error"]["message"] == failure.message
    assert limits.active(uid(client, alice)) == 0
    assert post(client, alice, ready, make_wav()).status_code == 200  # simply retry


def test_there_is_no_fallback_between_stt_providers(
    client: TestClient, alice: dict[str, str], ready: str, run_sql: RunSql
) -> None:
    run_sql("UPDATE session_preferences SET stt_provider = 'a' WHERE session_id = :s", s=ready)
    fakes, _, _ = install_stt(
        client,
        [FakeSTTProvider("a", failures=[Unavailable()]), FakeSTTProvider("b", reply="from b")],
    )
    response = post(client, alice, ready, make_wav())
    assert (response.status_code, response.json()["error"]["code"]) == (503, "llm_unavailable")
    assert [f.calls for f in fakes] == [1, 0]


def test_a_slow_provider_times_out_with_504_and_releases_everything(
    client: TestClient, alice: dict[str, str], ready: str
) -> None:
    _, limits, _ = install_stt(
        client, [FakeSTTProvider(delay=5)], config=SttRouterConfig(request_timeout_seconds=0.1)
    )
    response = post(client, alice, ready, make_wav())
    assert (response.status_code, response.json()["error"]["code"]) == (504, "llm_timeout")
    assert limits.active(uid(client, alice)) == 0


def test_a_client_that_disconnects_cancels_the_transcription() -> None:
    class Request:
        def __init__(self, after: float) -> None:
            self._after = after
            self._started = asyncio.get_event_loop().time()

        async def is_disconnected(self) -> bool:
            return asyncio.get_event_loop().time() - self._started >= self._after

    async def scenario() -> tuple[FakeSTTProvider, float]:
        slow = FakeSTTProvider(delay=10)
        stt = SttRouter([RegisteredStt(slow, fake_stt_spec("fake"))])
        clip = validate_wav(make_wav(), max_seconds=30)

        async def work() -> Any:
            audio = AudioInput(clip.data, 16_000, clip.duration_ms)
            return await stt.transcribe(audio, "en", provider="fake")

        task = asyncio.create_task(work())
        started = asyncio.get_event_loop().time()
        with pytest.raises(Exception) as raised:
            await _await_or_cancel(Request(0.3), task)  # type: ignore[arg-type]
        assert getattr(raised.value, "status_code", None) == 499
        return slow, asyncio.get_event_loop().time() - started

    slow, elapsed = asyncio.run(scenario())
    assert slow.cancelled == 1 and elapsed < 3


# ------------------------------------------------------------------------ no persistence


def test_nothing_is_persisted_anywhere(
    client: TestClient, run_sql: RunSql, alice: dict[str, str], ready: str, storage_root: Any
) -> None:
    install_stt(client, [FakeSTTProvider(reply=SENTINEL)])
    tables = {
        t: count(run_sql, t)
        for t in ("detected_questions", "generated_answers", "resumes", "documents")
    }
    files = sorted(p.name for p in storage_root.rglob("*") if p.is_file())
    for _ in range(3):
        assert post(client, alice, ready, make_wav()).status_code == 200
    assert {t: count(run_sql, t) for t in tables} == tables
    assert sorted(p.name for p in storage_root.rglob("*") if p.is_file()) == files
    names = {
        r["table_name"]
        for r in run_sql(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
    }
    assert not any(
        "transcript" in n or "audio" in n for n in names
    )  # no transcript table, no migration


# ------------------------------------------------------------------ logs and metrics


def test_no_audio_or_transcript_reaches_logs_metrics_usage_or_errors(
    client: TestClient, alice: dict[str, str], ready: str, caplog: pytest.LogCaptureFixture
) -> None:
    pcm = pcm_tone(0.5)
    wav = canonical_wav(pcm[:200] + AUDIO_SENTINEL + pcm[200 + len(AUDIO_SENTINEL) :])
    secrets = [
        SENTINEL,
        AUDIO_SENTINEL.decode(),
        base64.b64encode(wav)[40:80].decode(),
        hashlib.sha256(wav).hexdigest(),
    ]
    fakes, _, sink = install_stt(client, [FakeSTTProvider(reply=SENTINEL)])
    with caplog.at_level(logging.DEBUG):
        ok = post(client, alice, ready, wav)
        invalid = post(client, alice, ready, b"junk " + AUDIO_SENTINEL)
        wrong_type = post(client, alice, ready, wav, content_type="audio/mpeg")
        failed_fakes, _, _ = install_stt(client, [FakeSTTProvider(failures=[Unavailable()])])
        failed = post(client, alice, ready, wav)
        metrics = client.get("/metrics").text
    assert ok.status_code == 200 and ok.json()["text"] == SENTINEL  # only the user sees it
    logs = caplog.text
    for secret in secrets:
        assert secret not in logs and secret not in metrics, secret[:12]
        for response in (invalid, wrong_type, failed):
            assert secret not in response.text
    assert SENTINEL not in repr(sink.records)
    assert "interview_audio_transcriptions" in metrics
    assert fakes[0].calls == 1 and failed_fakes[0].calls == 1
    assert "audio transcribed" in logs  # the safe, content-free line is present


# ----------------------------------------------- audio-derived questions use the Q&A pipeline


def test_an_audio_question_goes_through_the_existing_pipeline_with_source_audio(
    client: TestClient, run_sql: RunSql, alice: dict[str, str], ready: str
) -> None:
    install_stt(client, [FakeSTTProvider(script=["how would you design a rate limiter"])])
    install(client, [RecordingProvider()])
    heard = post(client, alice, ready, make_wav()).json()
    assert heard["is_question"] is True
    answered = ask(client, alice, ready, heard["text"], input_source="audio")
    typed = ask(client, alice, ready, "A typed question")
    assert answered.status_code == typed.status_code == 200
    assert answered.headers["content-type"].startswith("text/event-stream")
    rows = run_sql("SELECT raw_text, source FROM detected_questions ORDER BY detected_at")
    assert [(r["raw_text"], r["source"]) for r in rows] == [
        ("how would you design a rate limiter", "audio"),
        ("A typed question", "typed"),
    ]
    assert count(run_sql, "generated_answers") == 2  # one answer each, via the same pipeline


@pytest.mark.parametrize("value", ["video", "", "AUDIO", 1, None])
def test_an_invalid_input_source_is_still_rejected(
    client: TestClient, run_sql: RunSql, alice: dict[str, str], ready: str, value: Any
) -> None:
    install(client, [RecordingProvider()])
    assert ask(client, alice, ready, "Question?", input_source=value).status_code == 422
    assert count(run_sql, "detected_questions") == 0


def test_the_existing_question_contract_is_unchanged(
    client: TestClient, alice: dict[str, str], ready: str
) -> None:
    install(client, [RecordingProvider()])
    response = ask(client, alice, ready, "Contract check")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    extra = client.post(
        f"/api/v1/sessions/{ready}/questions",
        headers={**alice, **SSE},
        json={"text": "x", "user_id": "spoof"},
    )
    assert extra.status_code == 422
