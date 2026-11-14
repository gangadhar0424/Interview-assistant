"""Phase 12: the STTProvider port, the deterministic fake, the router and its configuration."""

import asyncio
import logging
import uuid

import pytest

from app.ai.errors import (
    ContentFiltered,
    InvalidSelection,
    LLMError,
    RateLimited,
    Timeout,
    Unavailable,
)
from app.ai.fake_stt import FakeSTTProvider, fake_stt_spec
from app.ai.stt import AudioInput
from app.ai.stt_router import (
    RegisteredStt,
    SttRouter,
    SttRouterConfig,
    Transcription,
    build_stt_router,
)
from app.ai.usage import MemoryUsageSink
from app.audio.validation import validate_wav
from app.core.config import Settings
from tests.wav_helpers import make_wav

SENTINEL = "STT-TRANSCRIPT-SENTINEL-3307"


def audio(seconds: float = 1.0) -> AudioInput:
    wav = validate_wav(make_wav(seconds), max_seconds=30)
    return AudioInput(wav.data, 16_000, wav.duration_ms)


def router(
    providers: list[FakeSTTProvider], config: SttRouterConfig | None = None
) -> tuple[SttRouter, MemoryUsageSink]:
    sink = MemoryUsageSink()
    entries = [RegisteredStt(p, fake_stt_spec(p.provider_id)) for p in providers]
    return SttRouter(entries, config, usage_sink=sink), sink


def transcribe(stt: SttRouter, provider: str, **kw: object) -> Transcription:
    return asyncio.run(stt.transcribe(audio(), "en", provider=provider, **kw))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- the fake


def test_the_fake_is_deterministic_and_reports_which_audio_it_heard() -> None:
    fake = FakeSTTProvider()
    clip = audio()
    first = asyncio.run(fake.transcribe(clip, "en", "fake-stt"))
    second = asyncio.run(fake.transcribe(clip, "en", "fake-stt"))
    assert first.text == second.text == f"Fake transcript: 1000 ms {clip.sha256()[:12]}"
    assert fake.digests_seen == [clip.sha256()] * 2 and fake.languages_seen == ["en", "en"]
    assert asyncio.run(fake.transcribe(audio(1.5), "en", "m")).text != first.text


def test_the_fake_plays_a_script_then_falls_back_to_the_reply() -> None:
    fake = FakeSTTProvider(
        script=["Tell me about yourself.", "I built payment systems."], reply="done"
    )
    texts = [asyncio.run(fake.transcribe(audio(), "en", "m")).text for _ in range(3)]
    assert texts == ["Tell me about yourself.", "I built payment systems.", "done"]


def test_the_fake_replays_scripted_failures_in_order() -> None:
    fake = FakeSTTProvider(failures=[Unavailable(), None])
    with pytest.raises(Unavailable):
        asyncio.run(fake.transcribe(audio(), "en", "m"))
    assert asyncio.run(fake.transcribe(audio(), "en", "m")).text


def test_reprs_never_show_audio_or_transcript() -> None:
    clip = audio()
    assert "RIFF" not in repr(clip) and "data=" not in repr(clip)
    result = asyncio.run(FakeSTTProvider(reply=SENTINEL).transcribe(clip, "en", "m"))
    assert SENTINEL not in repr(result)


# ------------------------------------------------------------------------------ router


def test_success_reports_provider_model_and_meters_usage_without_content() -> None:
    stt, sink = router([FakeSTTProvider(reply=SENTINEL)])
    uid = uuid.uuid4()
    result = asyncio.run(stt.transcribe(audio(), "en", provider="fake", user_id=uid))
    assert (result.text, result.provider, result.model) == (SENTINEL, "fake", "fake-stt")
    [record] = sink.records
    assert record.ok and record.user_id == uid
    assert SENTINEL not in repr(record) and SENTINEL not in repr(result)


def test_the_selected_provider_is_exclusive_and_never_falls_back() -> None:
    a = FakeSTTProvider("a", failures=[Unavailable()])
    b = FakeSTTProvider("b", reply="from b")
    stt, sink = router([a, b])
    with pytest.raises(Unavailable):
        transcribe(stt, "a")
    assert b.calls == 0  # D-57: the other provider is never asked
    assert transcribe(stt, "b").text == "from b"
    assert [r.ok for r in sink.records] == [False, True]


@pytest.mark.parametrize("error", [ContentFiltered, RateLimited, Timeout])
def test_every_failure_is_raised_as_its_own_safe_error(error: type[LLMError]) -> None:
    a = FakeSTTProvider("a", failures=[error()])
    stt, _ = router([a, FakeSTTProvider("b")])
    with pytest.raises(error):
        transcribe(stt, "a")


def test_unknown_or_mismatched_selections_are_refused_before_any_call() -> None:
    a = FakeSTTProvider("a")
    stt, _ = router([a])
    with pytest.raises(InvalidSelection):
        transcribe(stt, "nope")
    with pytest.raises(InvalidSelection):
        transcribe(stt, "a", model="some-other-model")
    assert a.calls == 0
    assert transcribe(stt, "a", model="fake-stt").provider == "a"


def test_the_breaker_opens_and_refuses_the_selected_provider() -> None:
    a = FakeSTTProvider("a", failures=[Unavailable()] * 5)
    b = FakeSTTProvider("b", reply="b")
    stt, _ = router([a, b], config=SttRouterConfig(breaker_failure_threshold=2))
    for _ in range(2):
        with pytest.raises(Unavailable):
            transcribe(stt, "a")
    with pytest.raises(Unavailable):  # open: refused without calling, and still no fallback
        transcribe(stt, "a")
    assert stt.breaker_state("a") == "open" and a.calls == 2 and b.calls == 0


def test_a_slow_provider_times_out() -> None:
    only, _ = router(
        [FakeSTTProvider("slow", delay=5)], config=SttRouterConfig(request_timeout_seconds=0.05)
    )
    with pytest.raises(Timeout):
        transcribe(only, "slow")


def test_cancellation_stops_the_provider_and_is_not_a_health_failure() -> None:
    fake = FakeSTTProvider(delay=5)
    stt, sink = router([fake], config=SttRouterConfig(breaker_failure_threshold=1))

    async def scenario() -> None:
        task = asyncio.create_task(stt.transcribe(audio(), "en", provider="fake"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert fake.cancelled == 1 and stt.breaker_state("fake") == "closed" and sink.records == []


def test_an_unexpected_provider_exception_is_hidden_behind_a_safe_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Broken:
        provider_id = "fake"

        async def transcribe(self, audio: AudioInput, language: str | None, model: str) -> object:
            raise RuntimeError(f"boom {SENTINEL}")

    stt = SttRouter([RegisteredStt(Broken(), fake_stt_spec("fake"))])  # type: ignore[arg-type]
    with caplog.at_level(logging.DEBUG), pytest.raises(Unavailable):
        transcribe(stt, "fake")
    assert SENTINEL not in caplog.text


def test_no_providers_means_unavailable_and_registrations_are_checked() -> None:
    stt, _ = router([])
    assert not stt.available
    with pytest.raises(InvalidSelection):
        transcribe(stt, "fake")
    with pytest.raises(ValueError):
        router([FakeSTTProvider("a"), FakeSTTProvider("a")])
    with pytest.raises(ValueError):
        SttRouter([RegisteredStt(FakeSTTProvider("a"), fake_stt_spec("b"))])


def test_build_stt_router_knows_the_real_providers_and_demands_their_keys() -> None:
    assert not build_stt_router([]).available
    assert build_stt_router(["fake"]).available
    for pid in ("openai", "groq", "sarvam"):
        with pytest.raises(ValueError):  # a real provider is never built without its key
            build_stt_router([pid])
    with pytest.raises(ValueError):
        build_stt_router(["nope"])
    built = build_stt_router(
        ["openai", "groq", "sarvam"], openai_api_key="k1", groq_api_key="k2", sarvam_api_key="k3"
    )
    assert built.ids() == ["openai", "groq", "sarvam"]
    assert [e.spec.model_id for e in built.entries()] == [
        "gpt-4o-mini-transcribe",
        "whisper-large-v3-turbo",
        "saarika:v2.5",
    ]


# ----------------------------------------------------------------------------- config


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/db")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("JWT_SECRET", "test-only-jwt-secret-0123456789abcdef0123456789")
    monkeypatch.setenv("APP_ENV", "local")
    return monkeypatch


def settings() -> Settings:
    return Settings(_env_file=None)


def test_audio_is_disabled_by_default_and_independent_of_other_providers(
    base_env: pytest.MonkeyPatch,
) -> None:
    base_env.setenv("LLM_ENABLED_PROVIDERS", "fake")
    base_env.setenv("VISION_ENABLED_PROVIDERS", "fake")
    s = settings()
    assert s.stt_enabled_provider_list == []  # enabling text or vision never enables audio input
    assert (s.audio_max_bytes, s.audio_max_seconds) == (1_500_000, 30)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("STT_ENABLED_PROVIDERS", "openai"),
        ("STT_ENABLED_PROVIDERS", "fake,fake"),
        ("AUDIO_MAX_BYTES", "999999999"),
        ("AUDIO_MAX_SECONDS", "0"),
    ],
)
def test_invalid_audio_configuration_fails_fast(
    base_env: pytest.MonkeyPatch, name: str, value: str
) -> None:
    base_env.setenv(name, value)
    with pytest.raises(ValueError):
        settings()


def test_the_fake_stt_provider_is_refused_outside_local(base_env: pytest.MonkeyPatch) -> None:
    base_env.setenv("STT_ENABLED_PROVIDERS", "fake")
    assert settings().stt_enabled_provider_list == ["fake"]
    base_env.setenv("APP_ENV", "staging")
    base_env.setenv("REDIS_URL", "rediss://127.0.0.1:1/0")
    with pytest.raises(ValueError):
        settings()
