"""Phase 11A: the VisionProvider port, the deterministic fake, the vision router and its config."""

import asyncio
import logging
import uuid

import pytest

from app.ai.errors import ContentFiltered, RateLimited, Timeout, Unavailable
from app.ai.fake_vision import FakeVisionProvider, fake_vision_spec
from app.ai.usage import MemoryUsageSink
from app.ai.vision import ImageInput
from app.ai.vision_router import (
    RegisteredVision,
    VisionRouter,
    VisionRouterConfig,
    build_vision_router,
)
from app.core.config import Settings
from tests.png_helpers import make_png

SENTINEL = "VISION-TEXT-SENTINEL-8841"


def image(width: int = 64, height: int = 64) -> ImageInput:
    return ImageInput(make_png(width, height), width, height)


def router(
    providers: list[FakeVisionProvider],
    chain: list[str] | None = None,
    config: VisionRouterConfig | None = None,
) -> tuple[VisionRouter, MemoryUsageSink]:
    sink = MemoryUsageSink()
    entries = [RegisteredVision(p, fake_vision_spec(p.provider_id)) for p in providers]
    return VisionRouter(entries, chain, config, usage_sink=sink), sink


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- the fake


def test_the_fake_is_deterministic_and_reports_which_image_it_saw() -> None:
    fake = FakeVisionProvider()
    img = image()
    first = asyncio.run(fake.extract_text("instructions", img, "fake-vision"))
    second = asyncio.run(fake.extract_text("instructions", img, "fake-vision"))
    assert first.text == second.text == f"Fake extraction: 64x64 image {img.sha256()[:12]}"
    assert fake.digests_seen == [img.sha256()] * 2
    assert fake.calls == 2
    other = asyncio.run(fake.extract_text("instructions", image(65, 64), "fake-vision"))
    assert other.text != first.text


def test_the_fake_replays_scripted_failures_in_order() -> None:
    fake = FakeVisionProvider(failures=[Unavailable(), None])
    with pytest.raises(Unavailable):
        asyncio.run(fake.extract_text("i", image(), "m"))
    assert asyncio.run(fake.extract_text("i", image(), "m")).text


def test_image_and_result_reprs_never_show_content() -> None:
    img = image()
    assert "x89PNG" not in repr(img) and "data" not in repr(img)
    result = asyncio.run(FakeVisionProvider(reply=SENTINEL).extract_text("i", img, "m"))
    assert SENTINEL not in repr(result)


# ------------------------------------------------------------------------------ router


def test_a_successful_extraction_reports_provider_model_and_meters_usage_without_content() -> None:
    fake = FakeVisionProvider(reply=SENTINEL)
    vision, sink = router([fake])
    uid = uuid.uuid4()
    result = asyncio.run(vision.extract("instructions", image(), user_id=uid))
    assert (result.text, result.provider, result.model, result.fallback_used) == (
        SENTINEL,
        "fake",
        "fake-vision",
        False,
    )
    [record] = sink.records
    assert record.ok and record.user_id == uid and record.provider == "fake"
    assert SENTINEL not in repr(record) and SENTINEL not in repr(result)


def test_fallback_to_the_next_vision_provider_on_an_eligible_error() -> None:
    a = FakeVisionProvider("a", failures=[Unavailable()])
    b = FakeVisionProvider("b", reply="from b")
    vision, sink = router([a, b])
    result = asyncio.run(vision.extract("i", image()))
    assert (result.text, result.provider, result.fallback_used) == ("from b", "b", True)
    assert [r.ok for r in sink.records] == [False, True]


def test_content_filtering_is_never_retried_elsewhere() -> None:
    a = FakeVisionProvider("a", failures=[ContentFiltered()])
    b = FakeVisionProvider("b")
    vision, _ = router([a, b])
    with pytest.raises(ContentFiltered):
        asyncio.run(vision.extract("i", image()))
    assert b.calls == 0


def test_all_providers_failing_raises_the_last_safe_error() -> None:
    a = FakeVisionProvider("a", failures=[Unavailable()])
    b = FakeVisionProvider("b", failures=[RateLimited()])
    vision, _ = router([a, b])
    with pytest.raises(RateLimited):
        asyncio.run(vision.extract("i", image()))


def test_the_breaker_opens_and_skips_a_failing_provider() -> None:
    a = FakeVisionProvider("a", failures=[Unavailable()] * 5)
    b = FakeVisionProvider("b", reply="b")
    vision, _ = router([a, b], config=VisionRouterConfig(breaker_failure_threshold=2))
    for _ in range(3):
        assert asyncio.run(vision.extract("i", image())).provider == "b"
    assert vision.breaker_state("a") == "open"
    assert a.calls == 2  # the third request skipped it without calling


def test_a_slow_provider_times_out_and_falls_back() -> None:
    slow = FakeVisionProvider("slow", delay=5)
    fast = FakeVisionProvider("fast", reply="fast")
    vision, _ = router([slow, fast], config=VisionRouterConfig(request_timeout_seconds=0.05))
    assert asyncio.run(vision.extract("i", image())).provider == "fast"
    only_slow, _ = router(
        [FakeVisionProvider("slow", delay=5)],
        config=VisionRouterConfig(request_timeout_seconds=0.05),
    )
    with pytest.raises(Timeout):
        asyncio.run(only_slow.extract("i", image()))


def test_cancellation_stops_the_provider_and_is_not_a_health_failure() -> None:
    fake = FakeVisionProvider(delay=5)
    vision, sink = router([fake], config=VisionRouterConfig(breaker_failure_threshold=1))

    async def scenario() -> None:
        task = asyncio.create_task(vision.extract("i", image()))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert fake.cancelled == 1
    assert (
        vision.breaker_state("fake") == "closed"
    )  # a client leaving says nothing about the provider
    assert sink.records == []  # no usage record for an abandoned call


def test_an_unexpected_provider_exception_is_hidden_behind_a_safe_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Broken:
        provider_id = "fake"

        async def extract_text(self, instructions: str, image: ImageInput, model: str) -> object:
            raise RuntimeError(f"boom {SENTINEL}")

    vision = VisionRouter([RegisteredVision(Broken(), fake_vision_spec("fake"))])  # type: ignore[arg-type]
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Unavailable):
            asyncio.run(vision.extract("i", image()))
    assert SENTINEL not in caplog.text  # the exception text is never logged


def test_a_router_without_providers_is_unavailable() -> None:
    vision, _ = router([])
    assert not vision.available
    with pytest.raises(Unavailable):
        asyncio.run(vision.extract("i", image()))


def test_duplicate_or_mismatched_registrations_are_rejected() -> None:
    with pytest.raises(ValueError):
        router([FakeVisionProvider("a"), FakeVisionProvider("a")])
    with pytest.raises(ValueError):
        VisionRouter([RegisteredVision(FakeVisionProvider("a"), fake_vision_spec("b"))])


def test_build_vision_router_only_knows_the_fake() -> None:
    assert not build_vision_router([]).available
    assert build_vision_router(["fake"]).available
    with pytest.raises(ValueError):
        build_vision_router(["openai"])


# ----------------------------------------------------------------------------- config


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/db")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("JWT_SECRET", "test-only-jwt-secret-0123456789abcdef0123456789")
    monkeypatch.setenv("APP_ENV", "local")
    return monkeypatch


def settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_vision_is_disabled_by_default_and_independent_of_text_providers(
    base_env: pytest.MonkeyPatch,
) -> None:
    base_env.setenv("LLM_ENABLED_PROVIDERS", "fake")
    s = settings()
    assert (
        s.vision_enabled_provider_list == []
    )  # enabling a text provider never enables screenshots
    assert s.screenshot_max_bytes == 5 * 1024 * 1024
    assert (s.screenshot_max_long_edge, s.screenshot_max_pixels) == (4096, 16_000_000)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("VISION_ENABLED_PROVIDERS", "openai"),
        ("VISION_ENABLED_PROVIDERS", "fake,fake"),
        ("VISION_FALLBACK_CHAIN", "fake"),  # not in the enabled list
        ("SCREENSHOT_MAX_BYTES", "999999999"),
    ],
)
def test_invalid_vision_configuration_fails_fast(
    base_env: pytest.MonkeyPatch, name: str, value: str
) -> None:
    base_env.setenv(name, value)
    with pytest.raises(ValueError):
        settings()


def test_the_fake_vision_provider_is_refused_outside_local(base_env: pytest.MonkeyPatch) -> None:
    base_env.setenv("VISION_ENABLED_PROVIDERS", "fake")
    assert settings().vision_enabled_provider_list == ["fake"]
    base_env.setenv("APP_ENV", "staging")
    base_env.setenv("REDIS_URL", "rediss://127.0.0.1:1/0")
    with pytest.raises(ValueError):
        settings()
