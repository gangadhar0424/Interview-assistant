"""Evaluate retrieval and routing on the question set in tests/eval/questions.json (D-66).

    uv run python scripts/eval_retrieval.py --session <session-id> --user <user-id> --mode hybrid
    uv run python scripts/eval_retrieval.py ... --mode dense     # baseline: the previous pipeline
    uv run python scripts/eval_retrieval.py ... --mode router    # routing + exact-text answers

For every question it checks whether one of the expected marker phrases appears in what the
pipeline would put in front of the user, and reports recall@5 / recall@8, MRR, the router's mode
accuracy and verbatim rate, and latency percentiles. It reads the real database and writes nothing.
Only question ids and counts are printed; no document text.
"""

import argparse
import asyncio
import json
import statistics
import time
import uuid
from pathlib import Path

from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import create_engine, create_sessionmaker, set_tenant_context
from app.embeddings.factory import build_embedding_provider
from app.qa.answer_router import Mode, RouterSettings, decide
from app.qa.context import ContextConfig, order_for_prompt, retrieve_context
from app.qa.extractive import extractive_text
from app.qa.ranking import retrieve_ranked
from app.retrieval.terms import content_terms

QUESTIONS = Path(__file__).resolve().parents[1] / "tests" / "eval" / "questions.json"
EXPECTED_MODES = {
    "document": {Mode.DOCUMENT_EXTRACTIVE},
    "resume": {Mode.RESUME_EXTRACTIVE},
    "general": {Mode.GENERAL},
}


def contains_marker(value: str, markers: list[str]) -> bool:
    lowered = value.lower()
    return any(marker.lower() in lowered for marker in markers)


def first_rank(texts: list[str], markers: list[str]) -> int | None:
    for index, value in enumerate(texts, 1):
        if contains_marker(value, markers):
            return index
    return None


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1))))]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--mode", choices=["dense", "hybrid", "router"], default="hybrid")
    parser.add_argument("--max-words", type=int, default=150)
    parser.add_argument(
        "--questions",
        type=Path,
        default=QUESTIONS,
        help="question set JSON (default: tests/eval/questions.json)",
    )
    args = parser.parse_args()
    session_id, user_id = uuid.UUID(args.session), uuid.UUID(args.user)

    settings = get_settings()
    engine = create_engine(settings.database_url)
    maker = create_sessionmaker(engine)
    embedder = build_embedding_provider(settings)
    config = ContextConfig.from_settings(settings)
    router_cfg = RouterSettings.from_settings(settings)
    questions = json.loads(args.questions.read_text(encoding="utf-8"))["questions"]

    async with maker() as db:
        await set_tenant_context(db, user_id)
        resume_id = (
            await db.execute(
                text("SELECT resume_id FROM sessions WHERE id = :i"), {"i": session_id}
            )
        ).scalar_one()
        docs = [
            r[0]
            for r in (
                await db.execute(
                    text("SELECT document_id FROM session_documents WHERE session_id = :i"),
                    {"i": session_id},
                )
            ).all()
        ]

    embedder.embed_query("warm up")  # exclude the one-off model load from the timings
    rows: list[dict[str, object]] = []
    for q in questions:
        markers: list[str] = q["contains"]
        started = time.monotonic()
        row: dict[str, object] = {"id": q["id"], "group": q["group"], "expect": q["expect"]}
        if args.mode == "dense":
            async with maker() as db:
                await set_tenant_context(db, user_id)
                hits = order_for_prompt(
                    await retrieve_context(
                        db,
                        embedder,
                        user_id,
                        q["question"],
                        resume_id=resume_id,
                        document_ids=docs,
                        config=config,
                    )
                )
            texts = [h.text for h in hits]
        else:
            ranked = await retrieve_ranked(
                maker,
                embedder,
                user_id,
                q["question"],
                resume_id=resume_id,
                document_ids=docs,
                config=config,
            )
            hits = ranked.all_hits
            texts = [f"{h.heading or ''} {h.text}" for h in hits]
            if args.mode == "router":
                terms = content_terms(q["question"])
                decision = decide(
                    q["question"],
                    ranked.resume,
                    ranked.documents,
                    router_cfg,
                    resume_opening=ranked.opening,
                )
                row["mode"] = decision.mode.value
                row["mode_ok"] = decision.mode in EXPECTED_MODES[q["expect"]]
                if decision.mode in (Mode.RESUME_EXTRACTIVE, Mode.DOCUMENT_EXTRACTIVE):
                    answer = await extractive_text(maker, user_id, decision, terms, args.max_words)
                    row["verbatim_hit"] = answer is not None and contains_marker(answer, markers)
        row["ms"] = (time.monotonic() - started) * 1000
        if markers:
            row["rank"] = first_rank(texts[:8], markers)
        rows.append(row)

    scored = [r for r in rows if r["expect"] != "general"]
    print(f"mode={args.mode} questions={len(rows)} scored={len(scored)}")  # noqa: T201
    for k in (5, 8):
        hit = sum(1 for r in scored if r.get("rank") and int(str(r["rank"])) <= k)
        print(f"recall@{k}: {hit}/{len(scored)} = {hit / len(scored):.2f}")  # noqa: T201
    mrr = statistics.fmean(1 / int(str(r["rank"])) if r.get("rank") else 0.0 for r in scored)
    print(f"MRR: {mrr:.3f}")  # noqa: T201
    if args.mode == "router":
        ok = sum(1 for r in rows if r.get("mode_ok"))
        print(f"router mode accuracy: {ok}/{len(rows)} = {ok / len(rows):.2f}")  # noqa: T201
        for group in ("vague", "numbered", "exact", "intro", "resume", "general"):
            subset = [r for r in rows if r["group"] == group]
            if subset:
                good = sum(1 for r in subset if r.get("mode_ok"))
                print(f"  {group:8s} mode ok {good}/{len(subset)}")  # noqa: T201
        extractive = [r for r in rows if "verbatim_hit" in r]
        if extractive:
            good = sum(1 for r in extractive if r["verbatim_hit"])
            print(f"extractive answers containing the expected text: {good}/{len(extractive)}")  # noqa: T201
        wrong = [f"{r['id']}:{r['mode']}" for r in rows if not r.get("mode_ok")]
        print("mode misses:", wrong)  # noqa: T201
    times = [float(str(r["ms"])) for r in rows]
    print(f"latency ms p50={percentile(times, 50):.0f} p95={percentile(times, 95):.0f}")  # noqa: T201
    misses = [str(r["id"]) for r in scored if not r.get("rank")]
    print("no expected text in top 8:", misses)  # noqa: T201
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
