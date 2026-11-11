"""Synthetic WAV builders for the Phase 12 audio tests (no real recordings, no audio libraries)."""

import math
import struct

from app.audio.validation import BYTES_PER_SECOND, SAMPLE_RATE, canonical_wav


def pcm_tone(seconds: float, *, freq: int = 440, amplitude: int = 8000) -> bytes:
    """A deterministic sine tone as PCM16 little-endian samples."""
    count = int(SAMPLE_RATE * seconds)
    return b"".join(
        struct.pack("<h", int(amplitude * math.sin(2 * math.pi * freq * i / SAMPLE_RATE)))
        for i in range(count)
    )


def pcm_silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(SAMPLE_RATE * seconds)


def make_wav(seconds: float = 1.0, *, amplitude: int = 8000) -> bytes:
    return canonical_wav(pcm_tone(seconds, amplitude=amplitude))


def wav_of_size(target_bytes: int) -> bytes:
    """A valid WAV close to `target_bytes` long (silence), for size-limit tests."""
    samples = max(0, (target_bytes - 44) // 2)
    return canonical_wav(b"\x00\x00" * samples)


def chunk(chunk_id: bytes, body: bytes) -> bytes:
    pad = b"\x00" if len(body) % 2 else b""
    return chunk_id + struct.pack("<I", len(body)) + body + pad


def fmt_chunk(*, fmt: int = 1, channels: int = 1, rate: int = SAMPLE_RATE, bits: int = 16) -> bytes:
    block = channels * bits // 8
    return chunk(b"fmt ", struct.pack("<HHIIHH", fmt, channels, rate, rate * block, block, bits))


def riff(*chunks: bytes, size_delta: int = 0) -> bytes:
    body = b"WAVE" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body) + size_delta) + body


__all__ = [
    "BYTES_PER_SECOND",
    "SAMPLE_RATE",
    "chunk",
    "fmt_chunk",
    "make_wav",
    "pcm_silence",
    "pcm_tone",
    "riff",
    "wav_of_size",
]
