# ruff: noqa: E501
"""Backend LOCAL processing benchmark (no database, no network, no provider).

Measures only our own code: WAV/PNG validation, question detection, and the transcription pipeline
around a zero-latency fake STT provider. Provider time and network time are NOT in these numbers.

Run from services/api so the project's virtualenv and `app` package are used:
    cd services/api
    .venv/Scripts/python.exe ../../scripts/bench/backend_local_bench.py --samples 200 --out result.json
"""

import argparse
import asyncio
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

# Placeholders so Settings can be constructed. Nothing here connects to a database or Redis, and no
# real credential is involved.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://bench:bench@127.0.0.1:1/bench")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:1/0")
os.environ.setdefault("JWT_SECRET", "bench-only-placeholder-secret-0123456789abcdef0123456789")

from stats import dump, print_table, summarize  # noqa: E402
from synth import QUESTIONS, make_png, make_wav  # noqa: E402


def ms_since(start_ns: int) -> float:
    return (time.perf_counter_ns() - start_ns) / 1e6


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--out")
    args = parser.parse_args()

    from app.ai.fake_stt import FakeSTTProvider, fake_stt_spec
    from app.ai.stt_router import RegisteredStt, SttRouter
    from app.audio.service import Transcriber, VoiceSelection
    from app.audio.validation import validate_wav
    from app.core.config import Settings
    from app.qa.detection import classify
    from app.screenshots.validation import validate_png

    results: dict[str, dict[str, float] | None] = {}

    for seconds in (3, 10, 28):
        wav = make_wav(seconds)
        samples = []
        for _ in range(args.samples):
            t = time.perf_counter_ns()
            validate_wav(wav, max_seconds=30)
            samples.append(ms_since(t))
        results[f"validate_wav {seconds}s ({len(wav) // 1024} KiB)"] = summarize(samples)

    for w, h in ((1280, 720), (1920, 1080)):
        png = make_png(w, h)
        samples = []
        for _ in range(max(20, args.samples // 5)):
            t = time.perf_counter_ns()
            validate_png(png, max_long_edge=2560, max_pixels=8_000_000)
            samples.append(ms_since(t))
        results[f"validate_png {w}x{h} ({len(png) // 1024} KiB)"] = summarize(samples)

    samples = []
    for i in range(args.samples * 5):
        text = QUESTIONS[i % len(QUESTIONS)]
        t = time.perf_counter_ns()
        classify(text)
        samples.append(ms_since(t))
    results["question detection (classify)"] = summarize(samples)

    router = SttRouter([RegisteredStt(FakeSTTProvider("fake"), fake_stt_spec("fake"))])
    transcriber = Transcriber(None, Settings(_env_file=None), router)  # type: ignore[arg-type]
    wav = make_wav(5)
    voice = VoiceSelection("fake", None, "en")
    user_id, session_id = uuid.uuid4(), uuid.uuid4()

    async def run() -> list[float]:
        out = []
        for _ in range(args.samples):
            t = time.perf_counter_ns()
            await transcriber.transcribe(user_id, session_id, wav, voice)
            out.append(ms_since(t))
        return out

    results["Transcriber.transcribe 5s, zero-latency fake STT"] = summarize(asyncio.run(run()))

    print_table(
        "Backend local processing (ms). Provider and network time are NOT included.", results
    )
    dump(
        args.out,
        {
            "kind": "backend_local",
            "python": sys.version.split()[0],
            "samples_per_metric": args.samples,
            "unit": "ms",
            "results": results,
        },
    )


if __name__ == "__main__":
    main()
