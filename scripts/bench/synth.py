# ruff: noqa: E501
"""Synthetic benchmark inputs. No real audio, screenshots or user data is ever used or stored.

`make_wav` writes 16 kHz mono PCM16 with speech-like bursts (tone bursts separated by silence), which is
what the segmenter and the backend's WAV validator accept. `make_png` writes a PNG of blocky
text-like pixels using only the standard library. Both are deterministic (seeded).
"""

import io
import math
import random
import struct
import zlib

SAMPLE_RATE = 16_000


def make_pcm(seconds: float, *, seed: int = 7) -> bytes:
    rng = random.Random(seed)  # noqa: S311 - synthetic data, not security
    total = int(seconds * SAMPLE_RATE)
    out = bytearray()
    i = 0
    while i < total:
        burst = min(total - i, int(rng.uniform(0.6, 1.6) * SAMPLE_RATE))
        gap = min(total - i - burst, int(rng.uniform(0.8, 1.2) * SAMPLE_RATE))
        freq = rng.uniform(120, 320)
        for n in range(burst):
            env = min(1.0, n / 800, (burst - n) / 800)
            out += struct.pack(
                "<h", int(9000 * env * math.sin(2 * math.pi * freq * n / SAMPLE_RATE))
            )
        out += b"\x00\x00" * max(0, gap)
        i += burst + max(0, gap)
    return bytes(out[: total * 2])


def make_wav(seconds: float, *, seed: int = 7) -> bytes:
    pcm = make_pcm(seconds, seed=seed)
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(pcm))
    )
    return header + pcm


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def make_png(width: int, height: int, *, seed: int = 7) -> bytes:
    rng = random.Random(seed)  # noqa: S311 - synthetic data, not security
    rows = bytearray()
    block_row: list[int] = []
    for y in range(height):
        if y % 14 == 0:
            block_row = [255 if rng.random() < 0.18 else 20 for _ in range(width // 8 + 1)]
        row = bytearray([0])
        for x in range(width):
            v = block_row[x // 8]
            row += bytes((v, v, v))
        rows += row
    raw = zlib.compress(bytes(rows), 6)
    buf = io.BytesIO()
    buf.write(b"\x89PNG\r\n\x1a\n")
    buf.write(_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
    buf.write(_chunk(b"IDAT", raw))
    buf.write(_chunk(b"IEND", b""))
    return buf.getvalue()


QUESTIONS = [
    "Can you explain how a mutex differs from a semaphore?",
    "Tell me about a time you resolved a conflict on your team.",
    "What is the time complexity of quicksort in the worst case?",
    "How would you design a rate limiter for an API?",
    "I mostly worked on backend services for the last three years.",
    "Why do you want to join our company?",
    "Walk me through your most challenging project.",
    "What happens when you type a URL into a browser?",
]
