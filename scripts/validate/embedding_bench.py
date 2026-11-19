# ruff: noqa: E501
"""Real embedding latency (M-3): the configured fastembed model, in process, on synthetic text.

Run from services/api with the embeddings extra installed and EMBEDDING_PROVIDER=fastembed:
    cd services/api; EMBEDDING_PROVIDER=fastembed .venv/Scripts/python.exe ../../scripts/validate/embedding_bench.py

Reports model load time from a warm disk cache, single-query latency, and document-batch latency.
Only timings are printed. Run it with nothing else heavy open: on a small machine memory pressure
dominates the tail.
"""

import os
import sys
import time

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bench"))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://bench:bench@127.0.0.1:1/bench")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:1/0")
os.environ.setdefault("JWT_SECRET", "bench-only-placeholder-secret-0123456789abcdef0123456789")

from stats import summarize  # noqa: E402

QUERIES = [
    "Tell me about the Quillon event pipeline.",
    "How did you reduce settlement delays?",
    "What did you build at Brindle Freight?",
    "Do you have open source experience?",
    "Where did you study?",
    "What are the responsibilities of the role?",
    "Explain the difference between a process and a thread.",
    "How would you design a rate limiter for a public API?",
]
PARAGRAPH = (
    "Designed the event pipeline that processes forty thousand events per second with Kafka and "
    "PostgreSQL, reduced settlement delays by sixty percent by introducing idempotent retries, "
    "and mentored five engineers while leading the ledger rewrite and the on-call rotation. "
)


def main() -> None:
    from app.core.config import get_settings
    from app.embeddings.factory import build_embedding_provider

    settings = get_settings()
    started = time.perf_counter()
    provider = build_embedding_provider(settings)
    provider.embed_query("warm up")
    load_ms = (time.perf_counter() - started) * 1000
    print(f"provider={settings.embedding_provider_name} model load + first query: {load_ms:.0f} ms")

    singles = []
    for i in range(80):
        t = time.perf_counter_ns()
        provider.embed_query(QUERIES[i % len(QUERIES)])
        singles.append((time.perf_counter_ns() - t) / 1e6)
    print("embed_query (n=80) ms:", summarize(singles))

    for size in (1, 8, 32):
        texts = [PARAGRAPH] * size
        times = []
        for _ in range(8):
            t = time.perf_counter_ns()
            provider.embed_documents(texts)
            times.append((time.perf_counter_ns() - t) / 1e6)
        print(
            f"embed_documents batch={size} (n=8, ~{len(PARAGRAPH)} chars each) ms:",
            summarize(times),
        )


if __name__ == "__main__":
    main()
