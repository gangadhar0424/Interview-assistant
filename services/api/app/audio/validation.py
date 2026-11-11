"""WAV validation for one utterance, without decoding audio and without a new dependency.

The bytes come from an untrusted client, so nothing here trusts them: the RIFF/WAVE header, a `fmt `
chunk that must be exactly PCM, mono, 16-bit, 16 kHz, a `data` chunk whose declared length must
match the body, a bounded chunk walk, and duration limits. Only the canonical 44-byte header and
the samples are returned (other chunks, such as tags or cue lists, are dropped). Error messages are
fixed strings and never echo any content.
"""

import struct
from dataclasses import dataclass, field

SAMPLE_RATE = 16_000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # mono, 16-bit
MIN_DURATION_MS = 200
MAX_CHUNKS = 16

INVALID_AUDIO_MESSAGE = "The audio is not a valid 16 kHz mono 16-bit WAV within the allowed length."


class InvalidAudio(Exception):
    """The bytes are not an acceptable utterance (the message is fixed and safe to show)."""

    def __init__(self) -> None:
        super().__init__(INVALID_AUDIO_MESSAGE)


@dataclass(frozen=True, slots=True)
class ValidatedWav:
    data: bytes = field(repr=False)  # canonical WAV: 44-byte header + samples
    duration_ms: int


def canonical_wav(pcm: bytes) -> bytes:
    """The canonical header for 16 kHz mono PCM16 followed by the samples."""
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE, BYTES_PER_SECOND, 2, 16)
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )


def validate_wav(data: bytes, *, max_seconds: int) -> ValidatedWav:
    """Validate and normalise an utterance; raise `InvalidAudio` for anything not acceptable."""
    if len(data) < 12 + 8 + 16 + 8 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise InvalidAudio
    riff_size = struct.unpack("<I", data[4:8])[0]
    if riff_size != len(data) - 8:  # also refuses trailing bytes and truncation
        raise InvalidAudio
    pos = 12
    seen_fmt = False
    pcm: bytes | None = None
    chunks = 0
    while pos < len(data):
        chunks += 1
        if chunks > MAX_CHUNKS or pos + 8 > len(data):
            raise InvalidAudio
        chunk_id = data[pos : pos + 4]
        size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        body_start = pos + 8
        if body_start + size > len(data):
            raise InvalidAudio
        if chunk_id == b"fmt ":
            if seen_fmt or size != 16:
                raise InvalidAudio
            fmt, channels, rate, byte_rate, block_align, bits = struct.unpack(
                "<HHIIHH", data[body_start : body_start + 16]
            )
            if (fmt, channels, rate, byte_rate, block_align, bits) != (
                1,
                1,
                SAMPLE_RATE,
                BYTES_PER_SECOND,
                2,
                16,
            ):
                raise InvalidAudio
            seen_fmt = True
        elif chunk_id == b"data":
            if not seen_fmt or pcm is not None:  # `fmt ` must come first; one data chunk only
                raise InvalidAudio
            pcm = data[body_start : body_start + size]
        pos = body_start + size + (size & 1)  # chunks are word aligned
    if pos != len(data) and pos != len(data) + 1:  # a final pad byte may be missing
        raise InvalidAudio
    if not seen_fmt or pcm is None or len(pcm) % 2 != 0:
        raise InvalidAudio
    duration_ms = len(pcm) * 1000 // BYTES_PER_SECOND
    if duration_ms < MIN_DURATION_MS or duration_ms > max_seconds * 1000:
        raise InvalidAudio
    return ValidatedWav(canonical_wav(pcm), duration_ms)
