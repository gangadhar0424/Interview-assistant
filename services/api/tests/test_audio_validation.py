"""Phase 12: WAV validation (pure, no database)."""

import struct

import pytest

from app.audio.validation import (
    INVALID_AUDIO_MESSAGE,
    MIN_DURATION_MS,
    InvalidAudio,
    canonical_wav,
    validate_wav,
)
from tests.wav_helpers import chunk, fmt_chunk, make_wav, pcm_tone, riff


def check(data: bytes, *, max_seconds: int = 30) -> None:
    with pytest.raises(InvalidAudio) as raised:
        validate_wav(data, max_seconds=max_seconds)
    assert str(raised.value) == INVALID_AUDIO_MESSAGE  # fixed message, nothing echoed


def test_a_valid_utterance_is_accepted_and_reports_its_duration() -> None:
    result = validate_wav(make_wav(1.5), max_seconds=30)
    assert result.duration_ms == 1500
    assert result.data.startswith(b"RIFF") and result.data[8:12] == b"WAVE"
    assert "data=" not in repr(result)  # the samples are never shown by repr


def test_extra_chunks_are_dropped_and_the_output_is_canonical() -> None:
    pcm = pcm_tone(0.5)
    tagged = riff(
        fmt_chunk(),
        chunk(b"LIST", b"INFOISFT" + b"SECRET-TAG-SENTINEL"),
        chunk(b"data", pcm),
        chunk(b"cue ", b"\x00" * 8),
    )
    result = validate_wav(tagged, max_seconds=30)
    assert b"SENTINEL" not in result.data and b"LIST" not in result.data
    assert result.data == canonical_wav(pcm)
    assert validate_wav(result.data, max_seconds=30).data == result.data  # idempotent


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"hello",
        b"RIFF\x00\x00\x00\x00WAVE",
        b"\xff\xd8\xff\xe0" + b"\x00" * 60,
        b"ID3" + b"\x00" * 80,
    ],
)
def test_non_wav_input_is_refused(data: bytes) -> None:
    check(data)


def test_truncated_or_padded_files_are_refused() -> None:
    good = make_wav(1.0)
    for cut in (10, 30, 44, len(good) // 2, len(good) - 1):
        check(good[:cut])
    check(good + b"\x00\x00")  # trailing bytes
    check(good + good)


def test_the_riff_length_must_match_the_body() -> None:
    pcm = pcm_tone(0.5)
    check(riff(fmt_chunk(), chunk(b"data", pcm), size_delta=10))
    check(riff(fmt_chunk(), chunk(b"data", pcm), size_delta=-10))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"fmt": 3},  # IEEE float
        {"channels": 2},
        {"rate": 44_100},
        {"rate": 8_000},
        {"bits": 8},
        {"bits": 24},
    ],
)
def test_only_16khz_mono_pcm16_is_accepted(kwargs: dict[str, int]) -> None:
    check(riff(fmt_chunk(**kwargs), chunk(b"data", pcm_tone(0.5))))


def test_chunk_order_and_shape_rules() -> None:
    pcm = pcm_tone(0.5)
    check(riff(chunk(b"data", pcm), fmt_chunk()))  # data before fmt
    check(riff(fmt_chunk(), fmt_chunk(), chunk(b"data", pcm)))  # two fmt chunks
    check(riff(fmt_chunk(), chunk(b"data", pcm), chunk(b"data", pcm)))  # two data chunks
    check(riff(fmt_chunk()))  # no data
    check(riff(chunk(b"data", pcm)))  # no fmt
    odd = riff(fmt_chunk(), chunk(b"data", pcm[:-1]))  # odd sample bytes
    check(odd)
    # a chunk length that lies about the remaining bytes
    lying = (
        b"RIFF"
        + struct.pack("<I", 4 + 8 + 16 + 8)
        + b"WAVE"
        + fmt_chunk()
        + b"data"
        + struct.pack("<I", 10**9)
    )
    check(lying)


def test_too_many_chunks_are_refused() -> None:
    many = [chunk(b"junk", b"\x00\x00")] * 40
    check(riff(fmt_chunk(), *many, chunk(b"data", pcm_tone(0.5))))


def test_duration_limits() -> None:
    check(make_wav((MIN_DURATION_MS - 20) / 1000))  # shorter than the minimum
    validate_wav(make_wav(MIN_DURATION_MS / 1000), max_seconds=30)  # exactly the minimum
    validate_wav(make_wav(2.0), max_seconds=2)  # exactly the maximum is allowed
    check(make_wav(2.1), max_seconds=2)
    check(make_wav(31.0), max_seconds=30)
